"""
MT Phase 3 integration test — web auth, per-tenant FS, per-tenant Qdrant naming.

Runs against a throwaway sandbox DB as the NON-superuser `cma_app` role, so RLS
is actually in force (a superuser bypasses it and every isolation check would
pass vacuously). The live `cma` database is never touched. Needs no Ollama and
no Qdrant — collection naming is checked, not searched.

SETUP (once per run — recreates the sandbox from scratch):

    ⚠️ NEVER drop or re-password the cma_app ROLE here. Roles are CLUSTER-wide
    while grants are per-database, so after the live cutover that role is what
    the four production services log in as: dropping it or setting a test
    password takes the live system down. The sandbox reuses the existing role
    and its real password (read from .env) — migration 004 grants it privileges
    inside cma_mt3, which is per-database and harmless.

    DOCKER=/Applications/Docker.app/Contents/Resources/bin/docker

    # The DROP is not optional and it is not "IF EXISTS" being polite: the
    # migrations are not all re-runnable over a populated database (009
    # redefines resolve_web_session and Postgres refuses to change an existing
    # function's OUT columns). Applying them on top gets you a half-migrated
    # sandbox and a test failure that points at the wrong thing.
    #
    # The DROP fails while anything still holds a connection — a sandbox web
    # instance you left running, or the idle pool of one you killed. The
    # terminate below is scoped to the sandbox database by name; never widen it.
    $DOCKER exec cma-postgres psql -U cma -d postgres -q \
      -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity
           WHERE datname = 'cma_mt3';" \
      -c "DROP DATABASE IF EXISTS cma_mt3;" \
      -c "CREATE DATABASE cma_mt3;"

    # heredoc/stdin NEEDS `docker exec -i` — without -i stdin never arrives
    # Globbed, not listed: an enumerated recipe goes stale the first time a
    # migration is added and nobody rebuilds the sandbox for a month, and the
    # failure then looks like a broken test rather than a missing table.
    # Lexicographic order is the migration order (003…013…).
    for f in src/church_assistant/db/schema.sql \
             src/church_assistant/db/migrations/0*.sql; do
      $DOCKER exec -i cma-postgres psql -U cma -d cma_mt3 -q -v ON_ERROR_STOP=1 < $f
    done

    $DOCKER exec cma-postgres psql -U cma -d cma_mt3 -q \
      -c "INSERT INTO tenants (slug, name) VALUES
            ('church-a','Церква А'),('church-b','Церква Б'),
            ('church-off','Призупинена церква')
          ON CONFLICT (slug) DO NOTHING;
          UPDATE tenants SET is_active = FALSE WHERE slug = 'church-off';"

RUN:

    uv run python tests/mt_phase3_smoke.py

The tenant ids below assume that seed order (_system=0, default=1, a=2, b=3,
off=4).
"""

from __future__ import annotations

import asyncio
import os
import re
import secrets
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

# ─── Environment BEFORE any church_assistant import (config is read at import) ─
#
# DB_NAME is redirected, and so are host and port. User and password still come
# from .env — the same cma_app credentials the live services use, because the
# role is cluster-wide and inventing a test password here would mean changing
# the LIVE one. What keeps this safe is the database name: every connection
# below goes to cma_mt3.
#
# Host and port are pinned to the LOCAL container rather than followed from
# .env, because after the split (docs/vps_deploy.md) .env points at the VPS and
# the sandbox does not live there — nor should it. The suite must never open a
# connection to the machine a church is being served from. Override with
# CMA_SANDBOX_DB_HOST / _PORT if the sandbox moves.
TMP = Path(tempfile.mkdtemp(prefix="cma_mt3_data_"))
os.environ.update(
    DB_NAME="cma_mt3",
    DB_HOST=os.environ.get("CMA_SANDBOX_DB_HOST", "127.0.0.1"),
    DB_PORT=os.environ.get("CMA_SANDBOX_DB_PORT", "5433"),
    WEB_SECRET_KEY=secrets.token_urlsafe(48),
    DATA_ROOT=str(TMP),
    LEGACY_TENANT_SLUG="default",
)

# Refuse to run against anything but the sandbox: this file wipes web_users and
# revokes sessions, which would be a very bad afternoon on the live database.
if os.environ["DB_NAME"] != "cma_mt3":
    raise SystemExit("refusing to run: DB_NAME must be the cma_mt3 sandbox")

from fastapi.testclient import TestClient  # noqa: E402

from church_assistant.db import audit_repo, web_users_repo  # noqa: E402
from church_assistant.db.connection import close_pool, get_pool  # noqa: E402
from church_assistant.db.tenant_context import (  # noqa: E402
    login_tenants,
    tenant_cursor,
)
from church_assistant.shared import collections, tenant_paths  # noqa: E402
from church_assistant.web import security  # noqa: E402


TENANT_A, TENANT_B, TENANT_OFF = 2, 3, 4
PASSWORD_A = "pravylnyi-parol-A"
PASSWORD_B = "pravylnyi-parol-B"

SPEAKERS_STALE = '{"SPEAKER_00": "старе"}'
SPEAKERS_FIXED = '{"SPEAKER_00": "виправлене"}'

passed: list[str] = []


def ok(msg: str) -> None:
    passed.append(msg)
    print(f"  ✓ {msg}")


async def seed(pool) -> None:
    """
    One web account per church (plus one in the suspended church).

    Wipes first so the file is re-runnable: section 7 drives the real admin UI
    and leaves accounts behind, which would otherwise make section 1's exact
    list assertions fail on the second run. (RLS scopes each DELETE to its own
    tenant; audit_log is append-only and deliberately accumulates.)
    """
    for tid in (TENANT_A, TENANT_B, TENANT_OFF):
        async with tenant_cursor(pool, tid) as cur:
            await cur.execute("DELETE FROM web_users")

    for tid, uname, pw, name, role in [
        (TENANT_A, "anna", PASSWORD_A, "Анна А", "admin"),
        (TENANT_B, "borys", PASSWORD_B, "Борис Б", "member"),
        (TENANT_OFF, "olha", PASSWORD_A, "Ольга О", "admin"),
    ]:
        try:
            await web_users_repo.add_web_user(
                pool, tid, username=uname, password_hash=security.hash_password(pw),
                full_name=name, role=role,
            )
        except web_users_repo.WebUserAlreadyExists:
            pass


# ─────────────────────────────────────────────────────────────
# 1. DB layer: login bootstrap + RLS isolation of web_users
# ─────────────────────────────────────────────────────────────

async def test_db_layer(pool) -> None:
    print("\n1. web_users — resolver + RLS isolation")
    print("-" * 66)

    assert await login_tenants(pool, "anna") == [TENANT_A]
    assert await login_tenants(pool, "borys") == [TENANT_B]
    assert await login_tenants(pool, "nobody") == []
    ok("login_tenants reports the one church an unshared login can be in")

    # Church A's context sees only Anna; Borys is invisible even by name.
    a_user = await web_users_repo.get_by_username(pool, TENANT_A, "anna")
    assert a_user is not None and a_user["tenant_id"] == TENANT_A
    assert await web_users_repo.get_by_username(pool, TENANT_A, "borys") is None
    assert await web_users_repo.get_by_username(pool, TENANT_B, "anna") is None
    ok("RLS hides another church's web accounts (both directions)")

    a_list = await web_users_repo.list_active(pool, TENANT_A)
    assert [u["username"] for u in a_list] == ["anna"], a_list
    ok("list_active returns only this church's accounts")

    # The name has to be free INSIDE the church, and only there (014).
    try:
        await web_users_repo.add_web_user(
            pool, TENANT_A, username="anna",
            password_hash=security.hash_password("x" * 12), full_name="Клон",
        )
        raise AssertionError("duplicate username within a tenant should be refused")
    except web_users_repo.WebUserAlreadyExists:
        pass
    ok("the same login twice in one church is refused")

    assert security.verify_password(PASSWORD_A, a_user["password_hash"])
    assert not security.verify_password(PASSWORD_B, a_user["password_hash"])
    ok("stored scrypt hash verifies the right password only")

    # A name in two churches resolves to neither until somebody says which —
    # and the count is what tells "ask" apart from "no such account".
    shared_hash = security.hash_password("spilnyi-parol-000")
    a_id = await web_users_repo.add_web_user(
        pool, TENANT_A, username="spilne", password_hash=shared_hash,
        full_name="Спільне Імʼя А")
    b_id = await web_users_repo.add_web_user(
        pool, TENANT_B, username="spilne", password_hash=shared_hash,
        full_name="Спільне Імʼя Б")
    ok("the same login may now exist in two churches (migration 014)")

    # And a second pair sharing a name but NOT a password — the ordinary case,
    # and since 015 the one that must never be asked anything.
    await web_users_repo.add_web_user(
        pool, TENANT_A, username="riznyi",
        password_hash=security.hash_password("riznyi-parol-AAA"),
        full_name="Різні Паролі А")
    await web_users_repo.add_web_user(
        pool, TENANT_B, username="riznyi",
        password_hash=security.hash_password("riznyi-parol-BBB"),
        full_name="Різні Паролі Б")

    assert await login_tenants(pool, "spilne") == [TENANT_A, TENANT_B]
    assert await login_tenants(pool, "spilne", "church-a") == [TENANT_A]
    assert await login_tenants(pool, "spilne", "church-b") == [TENANT_B]
    ok("a shared login reports both churches, and narrows when one is named")

    # A church that does not hold the name answers exactly like a church that
    # does not exist — the caller must not be able to tell them apart.
    assert await login_tenants(pool, "spilne", "church-off") == []
    assert await login_tenants(pool, "spilne", "no-such-church") == []
    ok("naming the wrong church answers the same as naming a fictional one")

    # The display name works too — a member knows it better than the identifier
    # — but only while it points at one candidate.
    assert await login_tenants(pool, "spilne", "Церква А") == [TENANT_A]
    ok("the display name is accepted when it is unambiguous")

    # Candidates are ACTIVE accounts: an account nobody has claimed yet does
    # not make somebody else's name shared.
    await web_users_repo.deactivate(pool, TENANT_B, b_id)
    assert await login_tenants(pool, "spilne") == [TENANT_A]
    await web_users_repo.reactivate(pool, TENANT_B, b_id)
    ok("an inactive account is not a candidate")

    # ── 017: the state of the CHURCH counts too, and the two are not the
    #    same test. archive() never touches accounts, so before 017 an
    #    archived church went on holding its people's names — and the live
    #    namesake was asked to choose between their own church and one that
    #    no longer existed.
    from church_assistant.db import tenants_repo as _tenants

    await _tenants.archive(pool, TENANT_B)
    assert await login_tenants(pool, "spilne") == [TENANT_A]
    ok("an archived church stops holding a login name (017)")

    # Naming it explicitly is refused the same way a fictional church is: the
    # form must not become a way to ask whether a name lives in there.
    assert await login_tenants(pool, "spilne", "church-b") == []
    assert await login_tenants(pool, "spilne", "Церква Б") == []
    ok("naming the archived church answers like naming one that never existed")

    await _tenants.restore(pool, TENANT_B)          # comes back suspended…
    await _tenants.set_active(pool, TENANT_B, True)  # …and letting people in is its own step

    # ⚠️ THE REGRESSION THIS GUARDS. The filter is deleted_at, never is_active.
    # A SUSPENDED church must stay a candidate so its members reach the message
    # that says access is paused — and `_system` (tenant 0) is inactive by
    # design, so filtering on is_active would take the platform login with it.
    await _tenants.set_active(pool, TENANT_B, False)
    assert await login_tenants(pool, "spilne") == [TENANT_A, TENANT_B], \
        "a suspended church stopped being a candidate — 017 filtered is_active"
    assert await login_tenants(pool, "spilne", "church-b") == [TENANT_B]
    await _tenants.set_active(pool, TENANT_B, True)
    ok("a suspended church is still a candidate — suspension is not the archive")



# ─────────────────────────────────────────────────────────────
# 2. Filesystem isolation
# ─────────────────────────────────────────────────────────────

def test_fs_isolation() -> None:
    print("\n2. Per-tenant filesystem layout")
    print("-" * 66)

    a = tenant_paths.paths_for("church-a")
    b = tenant_paths.paths_for("church-b")

    assert a.meetings != b.meetings and a.voice_profiles != b.voice_profiles
    ok(f"separate subtrees: {a.meetings.name} under {a.root.name} vs {b.root.name}")

    # Same meeting date in both churches → different folders.
    da, db = a.meeting_dir("2026-06-15"), b.meeting_dir("2026-06-15")
    da.mkdir(parents=True); db.mkdir(parents=True)
    (da / "polished.md").write_text("## Присутні\n\n- Анна А\n\n### Тема А\nсекрет А\n")
    (db / "polished.md").write_text("## Присутні\n\n- Борис Б\n\n### Тема Б\nсекрет Б\n")
    assert da != db
    ok("same date, two churches → two folders (no collision)")

    # A date that exists ONLY in church B. The shared date above proves folders
    # don't collide, but it can't prove a listing is filtered — both churches
    # would show it. This one can: if it ever appears in church A's sidebar,
    # the listing leaked.
    db2 = b.meeting_dir("2026-06-22")
    db2.mkdir(parents=True)
    (db2 / "polished.md").write_text("## Присутні\n\n- Борис Б\n\n### Лише Б\nтільки Б\n")

    from church_assistant.shared import meetings_index
    sa = meetings_index.list_all_summaries(a.meetings)
    sb = meetings_index.list_all_summaries(b.meetings)
    assert [s.date for s in sa] == ["2026-06-15"], sa
    assert [s.date for s in sb] == ["2026-06-22", "2026-06-15"], sb
    assert [s.attendees for s in sa] == [["Анна А"]], sa
    ok("meetings_index lists only the church it was pointed at")

    hits = meetings_index.search_topics(a.meetings, "секрет")
    assert len(hits) == 1 and "А" in hits[0].snippet
    assert meetings_index.search_topics(a.meetings, "секрет Б") == []
    ok("keyword search cannot reach the other church's protocols")

    # Voice profiles: A's fingerprints must not be offered in B's review.
    from church_assistant.ingestion import speaker_review
    a.voice_profiles.mkdir(parents=True, exist_ok=True)
    b.voice_profiles.mkdir(parents=True, exist_ok=True)
    (a.voice_profiles / "Анна А.npy").write_bytes(b"\x00")
    assert speaker_review.has_profile(a.voice_profiles, "Анна А")
    assert not speaker_review.has_profile(b.voice_profiles, "Анна А")
    assert "Анна А" not in speaker_review.list_known_names(b.voice_profiles, {})
    ok("voice profiles are per-church (no cross-church name suggestions)")

    for bad in ("../../etc", "a/b", "UPPER"):
        try:
            tenant_paths.paths_for(bad)
            raise AssertionError(f"slug {bad!r} should have been rejected")
        except tenant_paths.InvalidTenantSlug:
            pass
    ok("path-traversal slugs rejected before touching the filesystem")


# ─────────────────────────────────────────────────────────────
# 2b. Portable meeting folder
# ─────────────────────────────────────────────────────────────

def test_portable_meeting_dir() -> None:
    """
    A meeting folder is located by (tenant, date), never by the path stored on
    the job row.

    ingestion_jobs.meeting_dir holds an ABSOLUTE path written by whichever
    process created the job. While web and worker share a machine that is
    harmless; split them across hosts (or move the checkout, or change
    DATA_ROOT) and the reader looks for a directory that does not exist, then
    fails with a missing-file error naming the wrong cause. These checks pin
    the derivation down, including for rows written before it existed.
    """
    print("\n2b. Meeting folder derived from tenant + date")
    print("-" * 66)

    from church_assistant.ingestion import paths as ing_paths

    slug, date = "church-a", "2026-09-07"

    # 1. Byte-identical to what the writers concatenate. Every existing row was
    #    stored as `paths_for(slug).meetings / date`; if the derivation drifted
    #    from that, live jobs would silently point somewhere new.
    stored_equivalent = tenant_paths.paths_for(slug).meetings / date
    assert ing_paths.meeting_dir_for(slug, date) == stored_equivalent
    ok("derived folder == the absolute path writers already store")

    # 2. It follows DATA_ROOT. Same tenant, same date, another machine's root →
    #    the folder moves with the host instead of staying pinned to one laptop.
    other_root = Path(tempfile.mkdtemp(prefix="cma_mt3_otherhost_"))
    previous = os.environ["DATA_ROOT"]
    try:
        os.environ["DATA_ROOT"] = str(other_root)
        relocated = ing_paths.meeting_dir_for(slug, date)
        assert relocated != stored_equivalent
        assert relocated == other_root.resolve() / "tenants" / slug / "meetings" / date
        ok("another DATA_ROOT → folder relocates (no machine binding left)")
    finally:
        os.environ["DATA_ROOT"] = previous
        shutil.rmtree(other_root, ignore_errors=True)

    # 3. Artifacts resolve through the derived folder, and a job row carrying a
    #    path from a DIFFERENT host does not divert them.
    folder = stored_equivalent
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "audio.m4a").write_bytes(b"not really audio")
    (folder / "polished.md").write_text("## Присутні\n\n- Анна А\n", encoding="utf-8")
    job = {
        "meeting_date": date,
        "audio_filename": "audio.m4a",
        # what a VPS would have written; nonsense on this machine
        "meeting_dir": "/srv/cma/data/tenants/church-a/meetings/2026-09-07",
    }
    mp = ing_paths.resolve_for(slug, job["meeting_date"], job.get("audio_filename"))
    assert mp.polished.exists() and mp.audio.exists(), mp.meeting_dir
    assert not str(mp.meeting_dir).startswith("/srv/cma")
    ok("job row from another host → artifacts still found locally")

    # 4. And nothing reads the column any more. The equivalence above holds only
    #    while that stays true, so guard it at the source rather than trusting
    #    that a future edit will remember (same tactic as the template scanner).
    offenders: list[str] = []
    src_root = tenant_paths.REPO_ROOT / "src" / "church_assistant"
    for py in sorted(src_root.rglob("*.py")):
        text = py.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if '["meeting_dir"]' in line or '.get("meeting_dir")' in line:
                rel = py.relative_to(tenant_paths.REPO_ROOT)
                offenders.append(f"{rel}:{lineno}")
    assert not offenders, (
        "these read the stored meeting_dir instead of deriving it from "
        f"(tenant, date): {', '.join(offenders)}"
    )
    ok("source scan: no module reads the stored meeting_dir column")

    # 4b. Same tactic, different silent failure: a module that reads config with
    #     a bare os.getenv at import level is correct only while something else
    #     happened to call load_dotenv() earlier in the import chain. That is not
    #     a style question — shared/health.py did exactly this, and after the
    #     split it health-checked the laptop's leftover Qdrant on localhost:6333
    #     instead of the VPS's, looking healthy only because the leftover was
    #     still up. The worker would have kept indexing into nothing.
    stale: list[str] = []
    for py in sorted(src_root.rglob("*.py")):
        text = py.read_text(encoding="utf-8")
        head = text.split("\ndef ", 1)[0].split("\nclass ", 1)[0]
        if not re.search(r"^[A-Z_]+ *= *(int\()?os\.getenv", head, re.M):
            continue
        # An actual call at the start of a line — not the string appearing in a
        # comment. The first version of this check was fooled by its own
        # explanatory comment mentioning load_dotenv(), and passed with the bug
        # deliberately put back.
        if re.search(r"^load_dotenv\(\)", head, re.M):
            continue
        stale.append(str(py.relative_to(tenant_paths.REPO_ROOT)))
    assert not stale, (
        "these read configuration at import without loading .env first, so their "
        f"values depend on import order: {', '.join(stale)}"
    )
    ok("source scan: no module reads config at import without load_dotenv()")

    # 5. And the same thing through the real routes. The three readers in
    #    web/routes/ingest.py had no coverage, which is exactly how the CSP
    #    regressions of 31.07 got through: the server answers 200 either way.
    #    So give the job row a path from another host and drive the pages.
    import json as _json
    from fastapi.testclient import TestClient
    from church_assistant.db import ingestion_jobs_repo as jobs_repo
    from church_assistant.web.main import app

    (folder / "diarization.rttm").write_text(
        "SPEAKER audio 1 0.000 40.000 <NA> <NA> SPEAKER_00 <NA> <NA>\n"
        "SPEAKER audio 1 40.000 30.000 <NA> <NA> SPEAKER_01 <NA> <NA>\n",
        encoding="utf-8",
    )
    (folder / "speakers.json").write_text(_json.dumps({
        "_meta": {"needs_review": [], "no_match": [], "invalid_embedding": []},
        "SPEAKER_00": "Анна А", "SPEAKER_01": "Богдан Б",
    }, ensure_ascii=False), encoding="utf-8")

    async def _make_job() -> int:
        pool = await get_pool()
        try:
            async with tenant_cursor(pool, TENANT_A) as cur:
                await cur.execute(
                    "DELETE FROM ingestion_jobs WHERE meeting_date = %s", (date,)
                )
            job_id = await jobs_repo.insert_job(
                pool, TENANT_A,
                meeting_date=date,
                meeting_dir=job["meeting_dir"],   # the other host's path
                audio_filename="audio.m4a",
            )
            await jobs_repo.mark_awaiting_review(pool, TENANT_A, job_id, speaker_count=2)
            return job_id
        finally:
            await close_pool()

    job_id = asyncio.run(_make_job())

    with TestClient(app, follow_redirects=False) as client:
        assert client.post(
            "/login", data={"username": "anna", "password": PASSWORD_A}
        ).status_code == 303

        r = client.get(f"/ingest/{job_id}")
        assert r.status_code == 200, r.status_code
        # polished.md was found through the derived folder. Assert on the row
        # driven by polished_exists, not on a link to the meeting — the sidebar
        # lists every folder and would satisfy a looser check either way.
        assert "✅ polished.md" in r.text, (
            "detail page says the protocol is missing → it looked in the stored "
            "path, not in the derived folder"
        )
        assert "— ще немає" not in r.text
        ok("GET /ingest/<id> resolves artifacts despite a foreign stored path")

        r = client.get(f"/ingest/{job_id}/speakers")
        assert r.status_code == 200, r.status_code
        assert "SPEAKER_00" in r.text and "Богдан Б" in r.text
        ok("GET /ingest/<id>/speakers reads speakers.json from the derived folder")


# ─────────────────────────────────────────────────────────────
# 3. Qdrant collection naming
# ─────────────────────────────────────────────────────────────

def test_collections() -> None:
    print("\n3. Per-tenant Qdrant collections")
    print("-" * 66)

    a = collections.all_collections("church-a")
    b = collections.all_collections("church-b")
    assert not (set(a.values()) & set(b.values()))
    ok(f"disjoint namespaces: {a['protocols']} vs {b['protocols']}")

    legacy = collections.all_collections("default")
    assert legacy["protocols"] == "cma_protocols"
    assert legacy["protocol_full"] == "cma_protocol_full"
    ok("legacy tenant keeps cma_* (existing corpus needs no re-index)")

    # A stored hit from before per-tenant collections still renders.
    from church_assistant.shared import rag
    old_hit = rag.Hit.from_dict({
        "score": 0.9, "collection": "cma_turns", "payload": {"speaker": "Х", "text": "т"},
    })
    new_hit = rag.Hit.from_dict({
        "score": 0.9, "collection": "t_church-a_turns", "payload": {"speaker": "Х", "text": "т"},
    })
    assert old_hit.kind == new_hit.kind == collections.KIND_TURNS
    ok("Hit.kind resolves both legacy and per-tenant collection names")

    # A crafted collection param cannot select another church's index.
    try:
        collections.resolve_kind("t_church-b_protocols")
        raise AssertionError("a physical collection name must not be a valid alias")
    except collections.UnknownCollectionKind:
        pass
    ok("request-supplied collection alias can't name another church's index")


# ─────────────────────────────────────────────────────────────
# 4. HTTP surface: the auth gate and the login flow
# ─────────────────────────────────────────────────────────────

def test_http() -> None:
    print("\n4. Web auth over real HTTP (TestClient)")
    print("-" * 66)

    from church_assistant.web.main import app

    with TestClient(app, follow_redirects=False) as client:
        r = client.get("/dashboard")
        assert r.status_code == 303 and r.headers["location"].startswith("/login"), r.status_code
        ok("anonymous page request → 303 to /login (deny by default)")

        r = client.get("/ingest/panel", headers={"HX-Request": "true"})
        assert r.status_code == 401 and r.headers.get("HX-Redirect") == "/login"
        ok("anonymous HTMX poll → 401 + HX-Redirect (no login page in a fragment)")

        assert client.get("/login").status_code == 200
        ok("/login itself is reachable without a session")

        r = client.post("/login", data={"username": "anna", "password": "wrong"})
        assert r.status_code == 401 and security.SESSION_COOKIE not in r.cookies
        ok("wrong password → 401, no session issued")

        r = client.post("/login", data={"username": "ghost", "password": "whatever"})
        assert r.status_code == 401
        ok("unknown username → same 401 (no account enumeration)")

        r = client.post("/login", data={"username": "olha", "password": PASSWORD_A})
        assert r.status_code == 403, r.status_code
        ok("valid credentials in a SUSPENDED church → 403, still no session")

        r = client.post(
            "/login",
            data={"username": "anna", "password": PASSWORD_A, "next": "/history"},
        )
        assert r.status_code == 303 and r.headers["location"] == "/history"
        assert security.SESSION_COOKIE in r.cookies
        ok("correct credentials → session cookie + redirect to ?next")

        # The cookie is a pointer only — identity is resolved server-side from
        # web_sessions on every request (see section 8).
        payload = security.load_session(r.cookies[security.SESSION_COOKIE])
        assert set(payload) == {"sid", "exp"}, payload
        ok("cookie holds an opaque session id, no tenant/role of its own")

        # Open redirect: an off-site ?next must not be honoured.
        r = client.post(
            "/login",
            data={"username": "anna", "password": PASSWORD_A,
                  "next": "https://evil.example/steal"},
        )
        assert r.headers["location"] == "/", r.headers["location"]
        ok("off-site ?next is ignored (no open redirect)")

        # Authenticated: the sidebar lists church A's meeting and not the date
        # that exists only in church B. (Asserting on a NAME would be a false
        # positive — the header prints the signed-in user's own name.)
        r = client.get("/meetings")
        assert r.status_code == 200
        assert "2026-06-15" in r.text and "2026-06-22" not in r.text
        ok("logged in as church A → sees A's meetings, not B's")

        r = client.get("/meetings/2026-06-15")
        assert r.status_code == 200 and "секрет А" in r.text and "секрет Б" not in r.text
        ok("meeting detail serves this church's protocol only")

        r = client.get("/api/search?q=секрет")
        assert "Тема А" in r.text and "Тема Б" not in r.text
        ok("search results stay inside the logged-in church")

        # A forged cookie claiming church B must not be accepted.
        forged = security.sign_session(
            {"uid": 1, "tid": TENANT_B, "slug": "church-b", "usr": "anna",
             "nam": "Анна А", "rol": "admin"}
        )
        body, _, _ = forged.partition(".")
        tampered = f"{body}.{'A' * 43}"
        client.cookies.set(security.SESSION_COOKIE, tampered)
        r = client.get("/meetings")
        assert r.status_code == 303 and r.headers["location"].startswith("/login")
        ok("cookie with a broken signature is rejected (can't switch tenant)")

        client.cookies.clear()
        r = client.post("/logout")
        assert r.status_code == 303 and r.headers["location"] == "/login"
        ok("logout redirects to /login")


# ─────────────────────────────────────────────────────────────
# 5. Audit trail
# ─────────────────────────────────────────────────────────────

async def test_audit(pool) -> None:
    print("\n5. Audit log (supervisory board backbone)")
    print("-" * 66)

    events = await audit_repo.list_recent(pool, TENANT_A, limit=50)
    actions = [e["action"] for e in events]
    assert "auth.login" in actions, actions
    assert "auth.login_failed" in actions, actions
    ok(f"church A's logins recorded ({len(events)} events: {sorted(set(actions))})")

    b_events = await audit_repo.list_recent(pool, TENANT_B, limit=50)
    assert all(e["tenant_id"] == TENANT_B for e in b_events)
    assert not any(e["actor"] == "web:anna" for e in b_events)
    ok("church B's audit log contains none of church A's events")

    # Append-only: the app role may not rewrite history.
    from psycopg.errors import InsufficientPrivilege
    try:
        async with tenant_cursor(pool, TENANT_A) as cur:
            await cur.execute("UPDATE audit_log SET action = 'tampered'")
        raise AssertionError("audit_log UPDATE should be denied to cma_app")
    except InsufficientPrivilege:
        pass
    ok("audit_log is append-only for the app role (UPDATE denied)")


# ─────────────────────────────────────────────────────────────
# 6. The reserved `_system` tenant
# ─────────────────────────────────────────────────────────────

async def test_system_tenant(pool) -> None:
    print("\n6. `_system` tenant — platform events live outside every church")
    print("-" * 66)

    from church_assistant.db import logs_repo, tenants_repo
    from church_assistant.shared.logger import SYSTEM_TENANT_ID, Logger

    assert SYSTEM_TENANT_ID == tenants_repo.SYSTEM_TENANT_ID == 0
    assert Logger("worker").tenant_id == 0
    ok("an unbound Logger defaults to tenant 0, not to the first church")

    marker = f"worker.started {secrets.token_hex(4)}"
    await Logger("worker").info("worker.started", message=marker)

    sys_logs = await logs_repo.list_recent(pool, SYSTEM_TENANT_ID, limit=20)
    assert any(row["message"] == marker for row in sys_logs), sys_logs
    ok("platform event landed in the `_system` tenant")

    for tid, label in [(1, "default"), (TENANT_A, "church-a")]:
        rows = await logs_repo.list_recent(pool, tid, limit=50)
        assert not any(r["message"] == marker for r in rows), label
    ok("no church's log (nor its dashboard) shows the platform's noise")

    # A church-scoped log still goes where it belongs.
    church_marker = f"query.completed {secrets.token_hex(4)}"
    await Logger("web", tenant_id=TENANT_A).info("query.completed", message=church_marker)
    rows = await logs_repo.list_recent(pool, TENANT_A, limit=20)
    assert any(r["message"] == church_marker for r in rows)
    assert not any(
        r["message"] == church_marker
        for r in await logs_repo.list_recent(pool, TENANT_B, limit=20)
    )
    ok("per-church events still route to their own church (and only there)")

    # The platform is a tenant row, but not a church: no login, no artifacts.
    system = await tenants_repo.get_by_id(pool, SYSTEM_TENANT_ID)
    assert system is not None and system["slug"] == "_system"
    assert system["is_active"] is False
    assert not any(
        t["id"] == SYSTEM_TENANT_ID for t in await tenants_repo.list_active(pool)
    )
    ok("`_system` is inactive → invisible to list_active and unusable for login")

    for fn in (
        lambda: tenant_paths.paths_for("_system"),
        lambda: collections.collection_name("_system", collections.KIND_TURNS),
    ):
        try:
            fn()
            raise AssertionError("_system must have neither folders nor collections")
        except tenant_paths.SystemTenantHasNoArtifacts:
            pass
    ok("`_system` has no data directory and no Qdrant collections")


# ─────────────────────────────────────────────────────────────
# 6b. A login name shared by two churches (migration 014)
# ─────────────────────────────────────────────────────────────

def test_shared_login() -> None:
    """
    One login page for everyone, and the church asked as rarely as possible.

    Two pairs of namesakes: `riznyi` with DIFFERENT passwords in church A and B,
    and `spilne` with the SAME password in both. The password settles the first
    pair by itself; only the second can reach the question.
    """
    print("\n6b. Один вхід на всі церкви — питання лише коли пароль не вирішує")
    print("-" * 66)

    from fastapi.testclient import TestClient
    from church_assistant.web.main import app

    SHARED = "spilnyi-parol-000"
    HINT = "cma_church"

    def only_hint(client) -> None:
        """
        Drop the session but keep the browser's church hint.

        Read from the jar rather than by name: httpx keeps one entry per
        (name, domain, path) and hands back a CookieConflict when several match,
        which a real browser would have collapsed. The last one written is the
        one that browser would send.
        """
        hints = [c.value for c in client.cookies.jar if c.name == HINT]
        client.cookies.clear()
        if hints:
            client.cookies.set(HINT, hints[-1])

    with TestClient(app, follow_redirects=False) as client:
        # An unshared name is untouched: one field, one step, straight in.
        r = client.post("/login", data={"username": "anna", "password": PASSWORD_A})
        assert r.status_code == 303 and security.SESSION_COOKIE in r.cookies
        ok("a login only one church uses still signs in in one step")

        # ── The point of 015: the password already knows which church ──
        for pw, church, other in (("riznyi-parol-AAA", "church-a", "church-b"),
                                  ("riznyi-parol-BBB", "church-b", "church-a")):
            client.cookies.clear()
            r = client.post("/login", data={"username": "riznyi", "password": pw})
            assert r.status_code == 303, (church, r.status_code, r.text[:200])
            page = client.get("/dashboard").text
            assert f"⛪ {church}" in page and other not in page, church
            ok(f"«riznyi» + пароль церкви {church} → всередину, без питання")

        # A typo on a shared name is a typo, not an interrogation. Asking a
        # member for an identifier they have never seen, over a wrong password,
        # is exactly what this design exists to avoid.
        client.cookies.clear()
        r = client.post("/login", data={"username": "riznyi",
                                        "password": "ne-toi-parol-vzagali"})
        assert r.status_code == 401
        assert 'name="church"' not in r.text, "a wrong password must not ask anything"
        ok("a wrong password on a shared name asks nothing — it just fails")

        # ── The only case the password cannot settle ──
        client.cookies.clear()
        r = client.post("/login", data={"username": "spilne", "password": SHARED})
        assert r.status_code == 200, r.status_code
        assert 'name="church"' in r.text
        assert security.SESSION_COOKIE not in r.cookies
        ok("one name, one password, two people → the church is asked")

        # The password is never handed back to the browser — not in a hidden
        # field, not as a value. One retype beats a password in page source.
        assert SHARED not in r.text
        ok("the re-rendered form does not carry the password")

        # Naming the church resolves it — and the two accounts are genuinely
        # separate people in separate churches.
        for church, other in (("church-a", "church-b"), ("church-b", "church-a")):
            client.cookies.clear()
            r = client.post("/login", data={"username": "spilne",
                                            "password": SHARED, "church": church})
            assert r.status_code == 303, (church, r.status_code)
            page = client.get("/dashboard").text
            assert f"⛪ {church}" in page and other not in page, church
            ok(f"«{church}» named → signed into that church, and only it")

        # ── The browser remembers, so nobody is asked twice ──
        # The last loop left a hint pointing at church B.
        only_hint(client)
        r = client.post("/login", data={"username": "spilne", "password": SHARED})
        assert r.status_code == 303, "the hint should have answered for them"
        assert f"⛪ church-b" in client.get("/dashboard").text
        ok("the browser's remembered church answers the question next time")

        # A hint must never turn a valid login into a refusal — a shared
        # computer, or somebody who moved church, still gets in.
        only_hint(client)                       # still pointing at church B
        r = client.post("/login", data={"username": "riznyi",
                                        "password": "riznyi-parol-AAA"})
        assert r.status_code == 303, "a wrong hint must fall through, not refuse"
        assert "⛪ church-a" in client.get("/dashboard").text
        ok("a hint for the wrong church falls through instead of failing")

        # The display name works too — it is what people actually know.
        client.cookies.clear()
        r = client.post("/login", data={"username": "spilne",
                                        "password": SHARED, "church": "Церква А"})
        assert r.status_code == 303
        ok("the church's display name is accepted as well as its identifier")

        # A church that does not hold the name answers exactly like a wrong
        # password. Otherwise the field becomes a way to ask "is this name in
        # THAT church?".
        client.cookies.clear()
        r_wrong_church = client.post("/login", data={
            "username": "spilne", "password": SHARED, "church": "church-off"})
        r_wrong_pw = client.post("/login", data={
            "username": "spilne", "password": "ne-toi", "church": "church-a"})
        assert r_wrong_church.status_code == r_wrong_pw.status_code == 401
        assert "Невірний логін або пароль" in r_wrong_church.text
        assert security.SESSION_COOKIE not in r_wrong_church.cookies
        ok("a wrong church answers exactly like a wrong password")

        # Nothing here leaks the list of churches: the field is free text, and
        # church B is never named to somebody probing church A's name.
        client.cookies.clear()
        asked = client.post("/login", data={"username": "spilne", "password": SHARED})
        assert "church-b" not in asked.text and "<select" not in asked.text
        ok("no church is ever listed on the login page")



    # ── 017, end to end: the case found by hand on 27.08 ──
    # Outside the TestClient block on purpose. The pool is a module singleton
    # bound to whichever loop opened it, and the app's lifespan owns it while a
    # client is alive — so tenant state is changed between clients, each of
    # which opens and closes its own, exactly as section 14 does.
    import asyncio as _aio

    from church_assistant.db import tenants_repo as _tenants

    async def _tenant_state(tid: int, *, archived: bool) -> None:
        pool = await get_pool()
        try:
            if archived:
                await _tenants.archive(pool, tid)
            else:
                # restore() brings it back suspended; letting people in is its
                # own decision, so the test has to make it too.
                await _tenants.restore(pool, tid)
                await _tenants.set_active(pool, tid, True)
        finally:
            await close_pool()

    _aio.run(_tenant_state(TENANT_B, archived=True))
    try:
        # Archive church B and the question disappears: a church in the archive
        # stops holding the name. Before 017 the live member of church A was
        # asked to choose between their own church and one that no longer
        # existed — and answering with the dead one was met with "доступ
        # призупинено. Зверніться до адміністратора", of a church that has none.
        with TestClient(app, follow_redirects=False) as client:
            r = client.post("/login", data={"username": "spilne", "password": SHARED})
            assert r.status_code == 303, (r.status_code, r.text[:200])
            assert 'name="church"' not in r.text
            assert "⛪ church-a" in client.get("/dashboard").text
            ok("archived church B: the namesake in A signs straight in, unasked")

        # And the archived church's own member gets the ordinary refusal — the
        # same one a church that never held the name gives, not a separate
        # sentence that would announce the church's state to anyone asking.
        with TestClient(app, follow_redirects=False) as client:
            r = client.post("/login", data={"username": "spilne",
                                            "password": SHARED, "church": "church-b"})
            assert r.status_code == 401, r.status_code
            assert "Невірний логін або пароль" in r.text
            assert "призупинено" not in r.text, \
                "an archived church is being reported as suspended"
            ok("its own member gets the generic refusal, not 'призупинено'")
    finally:
        _aio.run(_tenant_state(TENANT_B, archived=False))


# ─────────────────────────────────────────────────────────────
# 7. Web account management UI
# ─────────────────────────────────────────────────────────────

def test_admin_ui() -> None:
    print("\n7. /admin/users — account management")
    print("-" * 66)

    from fastapi.testclient import TestClient
    from church_assistant.web.main import app

    def login(client, username: str, password: str) -> None:
        r = client.post("/login", data={"username": username, "password": password})
        assert r.status_code == 303, (username, r.status_code)

    with TestClient(app, follow_redirects=False) as client:
        # A member must not reach the page — and gets sent somewhere useful
        # rather than a raw 403 body.
        login(client, "borys", PASSWORD_B)
        r = client.get("/admin/users")
        assert r.status_code == 303 and r.headers["location"].startswith("/dashboard")
        assert client.post("/admin/users/1/deactivate").status_code == 403
        ok("member: page redirects away, action endpoint returns 403")
        client.cookies.clear()

        login(client, "anna", PASSWORD_A)
        r = client.get("/admin/users")
        assert r.status_code == 200 and "anna" in r.text
        assert "borys" not in r.text          # church B's account
        ok("admin sees only their own church's accounts")

        # Create → no password is asked for, and none comes back. What comes
        # back is a link that exists on screen once.
        import re as _re
        r = client.post("/admin/users", data={
            "username": "dmytro", "full_name": "Дмитро Д", "role": "member",
        })
        assert r.status_code == 200 and "створено" in r.text, r.text[:300]
        m = _re.search(r'class="church-password">([^<]+)<', r.text)
        assert m and "/invite/" in m.group(1), r.text[:400]
        dmytro_link = m.group(1).strip().replace("http://testserver", "")
        ok("admin creates an account and gets a link, not a password")

        # Until it is redeemed the account is inactive, and the row says why —
        # "waiting to sign in", not "switched off", which call for opposite
        # actions from the admin looking at them.
        page = client.get("/admin/users").text
        assert "чекає на вхід" in page
        ok("an unclaimed account reads as pending, not as deactivated")

        # Redeem on the SAME client — a nested TestClient would start a second
        # event loop and close this one's pool underneath it (the pool is a
        # module singleton bound to the loop that opened it).
        client.cookies.clear()
        assert client.get(dmytro_link).status_code == 200
        r = client.post(dmytro_link, data={"password": "novyi-parol-123",
                                           "password_repeat": "novyi-parol-123"})
        assert r.status_code == 303 and security.SESSION_COOKIE in r.cookies
        # A member lands on the dashboard: /admin/users would bounce them back
        # with a permissions error, which is a strange first sentence to read.
        assert r.headers["location"] == "/dashboard", r.headers["location"]
        ok("the invited member sets their own password and is signed in")

        # Landed in church A: they see A's meeting and not the B-only date, and
        # as a member they get no account-management link.
        page = client.get("/meetings").text
        assert "2026-06-15" in page and "2026-06-22" not in page
        assert "/admin/users" not in page
        ok("the new account is in the inviting admin's church, as member")

        # The link is spent — the same one cannot make a second account real.
        assert client.get(dmytro_link).status_code == 404
        ok("a redeemed invite is dead")

        client.cookies.clear()
        login(client, "anna", PASSWORD_A)

        # Since 014 a name taken in another church is not this church's
        # problem. It lands inactive (unclaimed), so it does not yet make
        # church B's borys ambiguous — see the resolver checks in section 1.
        r = client.post("/admin/users", data={
            "username": "borys", "full_name": "Тезка", "role": "member",
        })
        assert "створено" in r.text and "church-b" not in r.text
        ok("a login used in another church can be taken here (014)")

        # Inside one church the name still has to be free, and the message
        # says where the clash is — the admin can see that row themselves.
        r = client.post("/admin/users", data={
            "username": "borys", "full_name": "Ще один", "role": "member",
        })
        assert "у вашій церкві" in r.text
        ok("the same login twice in ONE church is still refused")

        # Guard rails.
        me = client.get("/admin/users")
        anna_id = int(_re.search(r"/admin/users/(\d+)/password", me.text).group(1))

        r = client.post(f"/admin/users/{anna_id}/deactivate")
        assert "власний доступ" in r.text, r.text[:300]
        ok("an admin cannot deactivate themselves")

        r = client.post(f"/admin/users/{anna_id}/role", data={"role": "member"})
        assert "власний доступ" in r.text
        ok("an admin cannot demote themselves")

        # Promote Dmytro, then Anna is no longer the last admin — but Dmytro,
        # once alone, is protected in turn.
        dmytro_id = anna_id
        page = client.get("/admin/users").text
        for m in _re.finditer(r"/admin/users/(\d+)/role", page):
            if int(m.group(1)) != anna_id:
                dmytro_id = int(m.group(1))
        r = client.post(f"/admin/users/{dmytro_id}/role", data={"role": "admin"})
        assert "→ admin" in r.text
        r = client.post(f"/admin/users/{dmytro_id}/deactivate")
        assert "вимкнено" in r.text
        ok("with a second admin present, deactivation goes through")

        # Cross-church: church A's admin must not touch church B's account by id.
        r = client.post("/admin/users/2/deactivate")   # borys is id 2
        assert "не знайдено" in r.text
        ok("another church's account id is simply 'not found' (RLS)")

        r = client.post(f"/admin/users/{dmytro_id}/reactivate")
        assert "увімкнено" in r.text
        ok("a deactivated account can be restored (soft delete)")

        # ── Re-issuing: the repair that leaves the secret with its owner ──
        def _issue(uid: int) -> str:
            resp = client.post(f"/admin/users/{uid}/invite")
            assert resp.status_code == 200, resp.status_code
            link = _re.search(r'class="church-password">([^<]+)<', resp.text)
            assert link, resp.text[:400]
            return link.group(1).strip().replace("http://testserver", "")

        first = _issue(dmytro_id)
        assert client.get(first).status_code == 200
        second = _issue(dmytro_id)
        assert client.get(second).status_code == 200
        assert client.get(first).status_code == 404
        ok("issuing a new invite kills the previous one — never two live links")

        # An invite for an ACTIVE account is a password reset that the admin
        # cannot read. It must not cut the person off before they use it: the
        # old password keeps working until the new one replaces it.
        client.cookies.clear()
        r = client.post("/login", data={"username": "dmytro",
                                        "password": "novyi-parol-123"})
        assert r.status_code == 303
        ok("a pending invite does not invalidate the password already in use")

        r = client.post(second, data={"password": "tretii-parol-456",
                                      "password_repeat": "tretii-parol-456"})
        assert r.status_code == 303
        client.cookies.clear()
        assert client.post("/login", data={"username": "dmytro",
                                           "password": "novyi-parol-123"}).status_code != 303
        assert client.post("/login", data={"username": "dmytro",
                                           "password": "tretii-parol-456"}).status_code == 303
        ok("redeeming it replaces the password — the old one stops working")

        # Redeeming is where the secret actually changes, so that is where the
        # sessions the OLD password opened have to end — otherwise the invite is
        # a weaker repair than the 🔑 reset while the panel offers them as
        # siblings, and an admin re-credentialing a suspect account leaves
        # whoever is holding it signed in.
        client.cookies.clear()
        assert client.post("/login", data={"username": "dmytro",
                                           "password": "tretii-parol-456"}).status_code == 303
        old_session = client.cookies[security.SESSION_COOKIE]
        assert client.get("/dashboard").status_code == 200

        client.cookies.clear()
        login(client, "anna", PASSWORD_A)
        third = _issue(dmytro_id)
        client.cookies.clear()
        assert client.post(third, data={"password": "chetvertyi-parol-000",
                                        "password_repeat": "chetvertyi-parol-000"}
                           ).status_code == 303
        client.cookies.clear()
        client.cookies.set(security.SESSION_COOKIE, old_session)
        assert client.get("/dashboard").status_code == 303
        ok("redeeming ends the sessions the replaced password had opened")

        # ── Switching an account off has to close the invite too ──
        # An unspent link is a door: redeeming sets a password AND is_active,
        # so a link handed out before the decision walks the account back in.
        client.cookies.clear()
        login(client, "anna", PASSWORD_A)
        doomed = _issue(dmytro_id)
        assert client.get(doomed).status_code == 200
        r = client.post(f"/admin/users/{dmytro_id}/deactivate")
        assert "вимкнено" in r.text and "запрошення анульовано" in r.text
        assert client.get(doomed).status_code == 404
        ok("deactivating an account kills its unspent invite")

        # And the dead link cannot resurrect the account by being POSTed anyway
        # — the GET above only proves the page hides it.
        client.cookies.clear()
        assert client.post(doomed, data={"password": "obhid-zaboroni-77",
                                         "password_repeat": "obhid-zaboroni-77"}
                           ).status_code == 404      # the "gone" page, not a session
        assert client.post("/login", data={"username": "dmytro",
                                           "password": "obhid-zaboroni-77"}).status_code != 303
        ok("the revoked link cannot set a password or bring the account back")

        # The row must not let an outstanding invite hide the switched-off
        # state: they are separate facts and they call for opposite actions.
        client.cookies.clear()
        login(client, "anna", PASSWORD_A)
        revived = _issue(dmytro_id)                  # deliberately, on an OFF account
        page = client.get("/admin/users").text
        row = [tr for tr in _re.findall(r"<tr.*?</tr>", page, _re.S) if "dmytro" in tr][0]
        assert "вимкнено" in row and "запрошення видано" in row, row[:400]
        assert "ВИМКНЕНО" in row                     # the confirm says what it restores
        ok("an invite on a switched-off account shows both facts, and warns")

        client.cookies.clear()
        assert client.post(revived, data={"password": "povernennia-123",
                                          "password_repeat": "povernennia-123"}
                           ).status_code == 303
        ok("issuing one deliberately is still the way back — it just says so")

        client.cookies.clear()
        login(client, "anna", PASSWORD_A)
        r = client.post("/admin/users/2/invite")       # borys, church B
        assert "не знайдено" in r.text
        ok("no inviting your way into another church's account (RLS)")


# ─────────────────────────────────────────────────────────────
# 8. Server-side sessions — the point is that access can be TAKEN AWAY
# ─────────────────────────────────────────────────────────────

def test_sessions() -> None:
    print("\n8. web_sessions — revocation takes effect immediately")
    print("-" * 66)

    import asyncio as _asyncio
    from fastapi.testclient import TestClient
    from psycopg_pool import AsyncConnectionPool
    from church_assistant.db import tenants_repo, web_sessions_repo, web_users_repo
    from church_assistant.db.connection import _build_conninfo
    from church_assistant.web.main import app

    def as_admin(client) -> None:
        r = client.post("/login", data={"username": "anna", "password": PASSWORD_A})
        assert r.status_code == 303, r.status_code

    async def _db(fn):
        """
        Run one out-of-band DB step alongside the running app.

        Opens a private pool instead of the module singleton: the app's pool
        belongs to TestClient's event loop, and touching it from this one
        fails ("attached to a different loop"). These steps stand in for an
        admin acting from another browser while a session is open.
        """
        pool = AsyncConnectionPool(conninfo=_build_conninfo(), min_size=1,
                                   max_size=2, open=False)
        await pool.open()
        try:
            return await fn(pool)
        finally:
            await pool.close()

    with TestClient(app, follow_redirects=False) as client:
        as_admin(client)
        cookie = client.cookies[security.SESSION_COOKIE]

        # The cookie is a pointer, not the identity: no tenant/role inside it.
        payload = security.load_session(cookie)
        assert set(payload) == {"sid", "exp"}, payload
        assert "church-a" not in cookie and "admin" not in cookie
        ok("cookie carries only an opaque session id (identity lives in the DB)")

        assert client.get("/dashboard").status_code == 200
        ok("session works while it is live")

        # …and stops working the moment the row is revoked — no waiting for TTL.
        async def revoke_mine(pool):
            row = await web_sessions_repo.resolve(
                pool, security.hash_token(security.load_session(cookie)["sid"])
            )
            assert row is not None
            return await web_sessions_repo.revoke(
                pool, TENANT_A, int(row["session_id"])
            )

        assert _asyncio.run(_db(revoke_mine)) is True
        r = client.get("/dashboard")
        assert r.status_code == 303 and r.headers["location"].startswith("/login")
        ok("revoked session → next request is bounced (no TTL wait)")

        # The stale cookie is cleared, so the browser stops presenting it.
        assert client.cookies.get(security.SESSION_COOKIE) in (None, "")
        ok("the rejecting response clears the dead cookie")

        # Deactivating an account cuts its live session off.
        client.cookies.clear()
        r = client.post("/login", data={"username": "borys", "password": PASSWORD_B})
        assert r.status_code == 303
        borys_client_cookie = client.cookies[security.SESSION_COOKIE]
        assert client.get("/dashboard").status_code == 200

        async def disable_borys(pool):
            u = await web_users_repo.get_by_username(pool, TENANT_B, "borys")
            await web_users_repo.deactivate(pool, TENANT_B, int(u["id"]))
            return int(u["id"])

        borys_id = _asyncio.run(_db(disable_borys))
        client.cookies.set(security.SESSION_COOKIE, borys_client_cookie)
        r = client.get("/dashboard")
        assert r.status_code == 303
        ok("disabling an account ends its open session on the next request")

        async def restore_borys(pool):
            await web_users_repo.reactivate(pool, TENANT_B, borys_id)
        _asyncio.run(_db(restore_borys))

        # "Sign out everywhere" through the admin UI, across two browsers.
        client.cookies.clear()
        as_admin(client)
        first = client.cookies[security.SESSION_COOKIE]

        client.cookies.clear()
        as_admin(client)
        second = client.cookies[security.SESSION_COOKIE]
        assert first != second
        ok("two logins → two independent sessions")

        page = client.get("/admin/users").text
        assert "вийти скрізь" in page
        import re as _re
        anna_id = int(_re.search(r"/admin/users/(\d+)/sessions/revoke", page).group(1))

        r = client.post(f"/admin/users/{anna_id}/sessions/revoke")
        assert r.status_code == 200 and "Сесії" in r.text
        ok("admin can end every session of an account from the UI")

        for label, c in (("first", first), ("second", second)):
            client.cookies.set(security.SESSION_COOKIE, c)
            assert client.get("/dashboard").status_code == 303, label
        ok("both browsers are signed out, including the one that clicked")

        # Logout revokes server-side, not just client-side: replaying the exact
        # cookie afterwards must not work.
        client.cookies.clear()
        as_admin(client)
        replay = client.cookies[security.SESSION_COOKIE]
        assert client.post("/logout").status_code == 303
        client.cookies.set(security.SESSION_COOKIE, replay)
        assert client.get("/dashboard").status_code == 303
        ok("a cookie captured before logout is dead afterwards (replay fails)")

    # The resolver's remaining clauses, exercised directly.
    async def resolver_rules(pool):
        u = await web_users_repo.get_by_username(pool, TENANT_A, "anna")

        # A live session resolves to its OWN church — the tenant comes from the
        # row, so no token can be presented "as" another church.
        token = security.new_session_token()
        await web_sessions_repo.create(
            pool, TENANT_A, web_user_id=int(u["id"]),
            token_hash=security.hash_token(token), ttl_seconds=600,
        )
        row = await web_sessions_repo.resolve(pool, security.hash_token(token))
        assert row["tenant_id"] == TENANT_A and row["tenant_slug"] == "church-a"

        # Expired → refused by the same function, no separate check to forget.
        expired = security.new_session_token()
        await web_sessions_repo.create(
            pool, TENANT_A, web_user_id=int(u["id"]),
            token_hash=security.hash_token(expired), ttl_seconds=-60,
        )
        assert await web_sessions_repo.resolve(pool, security.hash_token(expired)) is None

        # An unknown token is simply nobody.
        assert await web_sessions_repo.resolve(pool, "0" * 64) is None

        # A session in a SUSPENDED church is dead even though the row itself is
        # live and the account is active. (church-off is inactive in the
        # fixture; cma_app may not flip that flag — the tenants registry is
        # platform-administered, which migration 004 enforces.)
        olha = await web_users_repo.get_by_username(pool, TENANT_OFF, "olha")
        off_token = security.new_session_token()
        await web_sessions_repo.create(
            pool, TENANT_OFF, web_user_id=int(olha["id"]),
            token_hash=security.hash_token(off_token), ttl_seconds=600,
        )
        assert await web_sessions_repo.resolve(pool, security.hash_token(off_token)) is None

    _asyncio.run(_db(resolver_rules))
    ok("resolver: own church only; expired, unknown and suspended-church → nobody")


# ─────────────────────────────────────────────────────────────
# 9. Idle timeout, Secure cookie, and the self-service sessions page
# ─────────────────────────────────────────────────────────────

def test_no_pinned_service_addresses() -> None:
    """
    Nothing may pin a service address in code — the addresses moved once.

    index_meeting.py and query.py both built their Qdrant client as
    `QdrantClient(host="localhost", port=6333)`. That was true until 24.08, when
    the plane split moved Qdrant to the VPS, and then it was silently false: the
    web's own queries go through shared/rag.py (which reads QDRANT_URL), so
    nothing complained, and nothing had been ingested since 18.08 to notice. The
    next upload would have failed on the last step, after three hours of
    transcription — and re-indexing from artifacts, which is the recovery path
    after restoring a backup, could not run at all.

    A grep, not a unit test, because the failure is a literal in source and the
    only reliable moment to catch it is before it ships.
    """
    print("\n9b. Жодної зашитої адреси сервісу в коді")
    print("-" * 66)

    import re as _re
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parent.parent / "src" / "church_assistant"
    # host= together with an explicit port is the shape that pins an address.
    # `url=os.getenv("QDRANT_URL", "http://localhost:6333")` is the correct form
    # and is not matched: the literal there is a default, not a destination.
    pinned = _re.compile(r'Client\(\s*host\s*=\s*["\']')
    offenders = []
    for f in sorted(root.rglob("*.py")):
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if pinned.search(line):
                offenders.append(f"{f.relative_to(root)}:{i}: {line.strip()}")
    assert not offenders, "зашита адреса сервісу:\n  " + "\n  ".join(offenders)
    ok("жоден клієнт не будується з host=\"...\" — адреси лише з оточення")

    # And the ones that read it must agree on the variable and the default.
    for mod in ("index_meeting.py", "query.py"):
        text = (root / mod).read_text(encoding="utf-8")
        assert 'os.getenv("QDRANT_URL"' in text, f"{mod} не читає QDRANT_URL"
    assert 'QDRANT_URL' in (root / "shared" / "rag.py").read_text(encoding="utf-8")
    ok("index_meeting, query і rag читають ту саму QDRANT_URL")


def test_hardening() -> None:
    print("\n9. Idle timeout / Secure cookie / «мої сесії»")
    print("-" * 66)

    import asyncio as _asyncio
    from fastapi.testclient import TestClient
    from psycopg_pool import AsyncConnectionPool
    from church_assistant.db import web_sessions_repo, web_users_repo
    from church_assistant.db.connection import _build_conninfo
    from church_assistant.web import headers
    from church_assistant.web.main import app

    async def _db(fn):
        pool = AsyncConnectionPool(conninfo=_build_conninfo(), min_size=1,
                                   max_size=2, open=False)
        await pool.open()
        try:
            return await fn(pool)
        finally:
            await pool.close()

    # ─── Idle timeout ────────────────────────────────────────
    async def idle_rules(pool):
        u = await web_users_repo.get_by_username(pool, TENANT_A, "anna")
        token = security.new_session_token()
        h = security.hash_token(token)
        sid = await web_sessions_repo.create(
            pool, TENANT_A, web_user_id=int(u["id"]), token_hash=h,
            ttl_seconds=12 * 3600,        # absolute cap: far away
        )
        # Fresh session: fine under any idle window.
        assert await web_sessions_repo.resolve(pool, h, idle_seconds=3600)

        # Age last_seen_at past the window without touching expires_at, so the
        # ONLY thing that can reject it is the idle rule.
        async with tenant_cursor(pool, TENANT_A) as cur:
            await cur.execute(
                "UPDATE web_sessions SET last_seen_at = NOW() - interval '3 hours' "
                "WHERE id = %s", (sid,),
            )
        assert await web_sessions_repo.resolve(pool, h, idle_seconds=2 * 3600) is None
        # …and the same row is still valid when the idle check is off, which
        # proves the absolute cap did not quietly do the work.
        assert await web_sessions_repo.resolve(pool, h, idle_seconds=0) is not None
        return sid

    _asyncio.run(_db(idle_rules))
    ok("idle timeout rejects an untouched session; absolute cap unaffected")

    # The two limits are independent: expiry fires even inside the idle window.
    async def absolute_still_applies(pool):
        u = await web_users_repo.get_by_username(pool, TENANT_A, "anna")
        token = security.new_session_token()
        h = security.hash_token(token)
        await web_sessions_repo.create(
            pool, TENANT_A, web_user_id=int(u["id"]), token_hash=h,
            ttl_seconds=-60,              # already past the absolute cap
        )
        assert await web_sessions_repo.resolve(pool, h, idle_seconds=24 * 3600) is None

    _asyncio.run(_db(absolute_still_applies))
    ok("absolute cap still fires for a session used seconds ago")

    # Misconfiguration is called out rather than silently logging people out.
    import os as _os
    saved = _os.environ.get("WEB_SESSION_IDLE_TIMEOUT")
    try:
        import importlib
        _os.environ["WEB_SESSION_IDLE_TIMEOUT"] = "30"
        importlib.reload(security)
        assert any("close to the" in p for p in security.check_session_config())
        _os.environ["WEB_SESSION_IDLE_TIMEOUT"] = str(24 * 3600)
        importlib.reload(security)
        assert any("never does anything" in p for p in security.check_session_config())
    finally:
        if saved is None:
            _os.environ.pop("WEB_SESSION_IDLE_TIMEOUT", None)
        else:
            _os.environ["WEB_SESSION_IDLE_TIMEOUT"] = saved
        importlib.reload(security)
    ok("startup warns on an idle window that is too short, or pointless")

    # ─── Secure cookie ───────────────────────────────────────
    assert security.cookie_secure_for("https") is True
    assert security.cookie_secure_for("http") is False       # auto
    _os.environ["WEB_COOKIE_SECURE"] = "true"
    assert security.cookie_secure_for("http") is True        # forced (TLS proxy)
    assert headers.hsts_enabled() is True
    _os.environ["WEB_COOKIE_SECURE"] = "false"
    assert security.cookie_secure_for("https") is False      # explicitly off
    assert headers.hsts_enabled() is False
    _os.environ.pop("WEB_COOKIE_SECURE")
    ok("Secure flag follows scheme, and can be forced on/off for a TLS proxy")

    # ─── Live: headers, and the self-service page ────────────
    with TestClient(app, follow_redirects=False) as client:
        r = client.get("/login")
        assert r.headers.get("X-Content-Type-Options") == "nosniff"
        assert r.headers.get("X-Frame-Options") == "DENY"
        assert "Strict-Transport-Security" not in r.headers   # plain http
        ok("baseline headers on every response; no HSTS over plain http")

        r = client.get("/dashboard")     # anonymous → redirect from middleware
        assert r.status_code == 303 and r.headers.get("X-Frame-Options") == "DENY"
        ok("headers also on middleware responses that never reach a handler")

        # CSP: strict, and strict in the ways that actually matter. An escape
        # hatch added "just to fix one page" would quietly undo the point.
        csp = client.get("/login").headers.get("Content-Security-Policy", "")
        assert "script-src 'self'" in csp and "unsafe-inline" not in csp
        assert "unsafe-eval" not in csp
        assert "style-src 'self'" in csp
        assert "img-src 'self' data:" in csp      # Pico inlines SVG icons
        assert "frame-ancestors 'none'" in csp and "form-action 'self'" in csp
        ok("CSP present with no unsafe-inline / unsafe-eval escape hatch")

        # A member — not just an admin — can manage their own sessions.
        r = client.post("/login", data={"username": "borys", "password": PASSWORD_B})
        assert r.status_code == 303
        page = client.get("/account/sessions")
        assert page.status_code == 200 and "цей браузер" in page.text
        assert "/admin/users" not in page.text        # still not an admin
        ok("member reaches /account/sessions (no admin role needed)")

        # A second sign-in for the same account, then "sign out others".
        # Clear borys's earlier sessions first (section 8 left some live) so the
        # count below is exact rather than "whatever accumulated".
        assert client.post("/logout").status_code == 303

        async def clear_borys(pool):
            u = await web_users_repo.get_by_username(pool, TENANT_B, "borys")
            await web_sessions_repo.revoke_all_for_user(pool, TENANT_B, int(u["id"]))
        _asyncio.run(_db(clear_borys))

        client.cookies.clear()
        client.post("/login", data={"username": "borys", "password": PASSWORD_B})
        first = client.cookies[security.SESSION_COOKIE]
        client.cookies.clear()
        client.post("/login", data={"username": "borys", "password": PASSWORD_B})
        second = client.cookies[security.SESSION_COOKIE]

        r = client.post("/account/sessions/revoke-others")
        assert r.status_code == 200 and "Закрито інших сесій: 1" in r.text, r.text[:300]
        ok("«вийти на всіх інших» closes the others and keeps this one")

        assert client.get("/dashboard").status_code == 200       # still signed in
        client.cookies.set(security.SESSION_COOKIE, first)
        assert client.get("/dashboard").status_code == 303       # the other one died
        ok("current session survives; the other browser is signed out")

        # One member must not close another member's session.
        client.cookies.clear()
        client.cookies.set(security.SESSION_COOKIE, second)
        mine = client.get("/account/sessions").text
        import re as _re
        my_ids = {int(m) for m in _re.findall(r"/account/sessions/(\d+)/revoke", mine)}

        async def anna_session(pool):
            u = await web_users_repo.get_by_username(pool, TENANT_A, "anna")
            token = security.new_session_token()
            return await web_sessions_repo.create(
                pool, TENANT_A, web_user_id=int(u["id"]),
                token_hash=security.hash_token(token), ttl_seconds=600,
            )

        foreign = _asyncio.run(_db(anna_session))
        assert foreign not in my_ids
        r = client.post(f"/account/sessions/{foreign}/revoke")
        assert "не знайдено" in r.text
        ok("cannot close someone else's session by id (ownership checked)")

    # The CSP above is only honest while the UI stays self-hosted: one CDN tag
    # or one inline handler re-introduces exactly what it forbids — and that
    # breaks in a browser, not here. So assert it at the source instead.
    from pathlib import Path as _Path
    import re as _re2

    tpl_dir = _Path(__file__).resolve().parent.parent / "src/church_assistant/web/templates"
    templates = sorted(tpl_dir.rglob("*.html"))
    offenders: list[str] = []
    for tpl in templates:
        body = tpl.read_text(encoding="utf-8")
        # Strip HTML comments first: a comment cannot violate a CSP, and the
        # comments explaining WHY there are no inline styles would otherwise
        # trip the very check they document.
        body = _re2.sub(r"<!--.*?-->", "", body, flags=_re2.S)
        for pattern, what in (
            (r'(src|href)="https?://', "external origin (CDN)"),
            (r"\bon(click|change|submit|input|load)=", "inline event handler"),
            # A <script> with no src= is an inline block. This one is the
            # reason the check exists in its current form: the first version
            # listed <style> and forgot <script>, and three inline blocks
            # shipped — the meeting page's timestamp links are BUILT by one of
            # them, so under the CSP they stopped appearing at all, with
            # nothing in any log to say why.
            (r"<script(?![^>]*\ssrc=)[^>]*>", "inline <script> block"),
            (r'"javascript:', "javascript: URL"),
            (r"<style[ >]", "inline <style> block"),
            (r'\sstyle="', "inline style attribute"),
        ):
            if _re2.search(pattern, body):
                offenders.append(f"{tpl.name}: {what}")
    assert not offenders, "the CSP would block these: " + "; ".join(offenders)
    ok(f"no CDN links / inline handlers / inline styles in {len(templates)} templates")

    # htmx attributes it does not understand are not ignored — they are acted on.
    # hx-swap-oob marks an element as out-of-band, and htmx REMOVES such an
    # element from the document while processing. Written as hx-swap-oob="false"
    # in the hope of "not out of band", it silently deleted the church form the
    # moment anything swapped: server 200, tests green, section simply gone.
    # Only htmx's own vocabulary is allowed here.
    OOB_OK = ("true", "outerHTML", "innerHTML", "beforebegin", "afterbegin",
              "beforeend", "afterend", "delete", "none")
    bad_oob: list[str] = []
    for tpl in templates:
        body = _re2.sub(r"<!--.*?-->", "", tpl.read_text(encoding="utf-8"), flags=_re2.S)
        for m in _re2.finditer(r'hx-swap-oob="([^"]*)"', body):
            if m.group(1).split(":")[0] not in OOB_OK:
                bad_oob.append(f"{tpl.name}: hx-swap-oob=\"{m.group(1)}\"")
    assert not bad_oob, (
        "htmx treats these as out-of-band and removes the element: "
        + "; ".join(bad_oob)
    )
    ok("no hx-swap-oob with a value htmx does not define")

    static_dir = _Path(__file__).resolve().parent.parent / "src/church_assistant/web/static"
    for asset in ("htmx.min.js", "app.js", "app.css", "pico.min.css"):
        p = static_dir / asset
        assert p.is_file() and p.stat().st_size > 0, f"missing/empty: {asset}"
    ok("every front-end asset the UI loads is vendored in static/")


# ─────────────────────────────────────────────────────────────
# 10. Topics → PDF export
# ─────────────────────────────────────────────────────────────

def test_pdf_export() -> None:
    print("\n10. Теми → PDF")
    print("-" * 66)

    import re as _re
    from fastapi.testclient import TestClient
    from church_assistant.shared import meetings_index, pdf_export
    from church_assistant.web.main import app

    # Timestamps go; anything that merely looks like one does not.
    for src, want in [
        ("Пункт (01:51)", "Пункт"),
        ("Список (24:11, 28:16)", "Список"),
        ("Крапка з комою (31:30; 33:52)", "Крапка з комою"),
        ("Години (1:02:03)", "Години"),
        ("Псалом 84:6 лишається", "Псалом 84:6 лишається"),
        ("Дужки (не таймкод) лишаються", "Дужки (не таймкод) лишаються"),
    ]:
        got = meetings_index.strip_timestamps(src)
        assert got == want, f"{src!r} -> {got!r}"
    ok("strip_timestamps removes only timestamp parentheticals")

    # Markdown emphasis becomes real bold; escaping runs FIRST, so the
    # conversion cannot be used to inject reportlab markup.
    for src, want in [
        ("**Проблема розриву**", "<b>Проблема розриву</b>"),
        ("текст **жирний** далі", "текст <b>жирний</b> далі"),
        ("2 ** 3 непара", "2 ** 3 непара"),
        ("**незакритий", "**незакритий"),
        ("<b>вже тег</b>", "&lt;b&gt;вже тег&lt;/b&gt;"),
        ("**<b>вкладений</b>**", "<b>&lt;b&gt;вкладений&lt;/b&gt;</b>"),
    ]:
        got = pdf_export._inline_markup(src)
        assert got == want, f"{src!r} -> {got!r}"
    ok("**bold** becomes <b>, and escaping happens before conversion")

    a = tenant_paths.paths_for("church-a")
    detail = meetings_index.load_detail(a.meetings, "2026-06-15")
    assert detail is not None
    assert detail.attendees, "fixture should have an attendee"
    pdf = pdf_export.build_topics_pdf(detail.date, detail.topics, detail.attendees)
    assert pdf.startswith(b"%PDF-")
    ok(f"builds a PDF for a church-a meeting ({len(pdf) / 1024:.0f} KB)")

    assert pdf_export.document_title("2026-06-15") == "Пасторська зустріч 15.06.2026"
    assert pdf_export.build_topics_pdf("2026-01-01", []).startswith(b"%PDF-")
    assert pdf_export.build_topics_pdf("2026-01-01", [], []).startswith(b"%PDF-")
    ok("title format; no topics and no attendees still renders")

    with TestClient(app, follow_redirects=False) as client:
        # Anonymous must not reach it — it is meeting content like any other.
        r = client.get("/meetings/2026-06-15/topics.pdf")
        assert r.status_code == 303, r.status_code
        ok("PDF route is behind the auth gate")

        assert client.post(
            "/login", data={"username": "anna", "password": PASSWORD_A}
        ).status_code == 303

        r = client.get("/meetings/2026-06-15/topics.pdf")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/pdf"
        assert r.content.startswith(b"%PDF-")
        # Cyrillic filename must survive as RFC 5987, with an ASCII fallback.
        cd = r.headers["content-disposition"]
        assert "filename*=UTF-8''" in cd and 'filename="meeting-' in cd
        ok("served as application/pdf with an RFC 5987 Cyrillic filename")

        # church-b's meeting of the SAME date must not be reachable from A.
        r = client.get("/meetings/2026-06-22/topics.pdf")   # exists only in B
        assert r.status_code == 404
        ok("another church's meeting is 404, not a PDF of their protocol")

    # The document itself: title present, no timestamps, text extractable.
    import pdfplumber, io
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        text = "\n".join(p.extract_text() or "" for p in doc.pages)
    assert "Пасторська зустріч" in text
    assert not _re.search(r"\(\s*\d{1,2}:\d{2}(?::\d{2})?\s*[;,)]", text), "timestamp leaked"
    assert "(cid:" not in text, "bullet drawn in a non-embedded font"
    assert "**" not in text, "raw Markdown emphasis left in the document"
    assert "<b>" not in text, "markup leaked as literal text"
    # Who was present is part of the record, so it has to be IN the document.
    assert "Присутні" in text and detail.attendees[0] in text
    ok("rendered text: title, attendees, no timestamps / ** / cid artefacts")


def test_manual_speaker() -> None:
    """
    A participant diarization never heard: added by hand from a timestamp.

    The point of the feature is that the typed time becomes a REAL diarization
    segment — so the checks are about the RTTM, not about the form round-trip.
    """
    print("\n11. Ручне додавання спікера")
    print("-" * 66)

    import json as _json
    from fastapi.testclient import TestClient
    from church_assistant import polish_protocol
    from church_assistant.ingestion import manual_speakers, speakers as speakers_util
    from church_assistant.web.main import app

    a = tenant_paths.paths_for("church-a")
    folder = a.meeting_dir("2026-06-15")
    rttm = folder / "diarization.rttm"
    speakers_json = folder / "speakers.json"
    pristine = folder / manual_speakers.PRISTINE_NAME
    for stale in (pristine,):
        stale.unlink(missing_ok=True)

    # Two clustered speakers; the guest spoke inside SPEAKER_01's stretch.
    rttm.write_text(
        "SPEAKER audio 1 0.000 40.000 <NA> <NA> SPEAKER_00 <NA> <NA>\n"
        "SPEAKER audio 1 40.000 40.000 <NA> <NA> SPEAKER_01 <NA> <NA>\n",
        encoding="utf-8",
    )
    (folder / "audio_transcript.json").write_text(_json.dumps({"segments": [
        {"start": 0.0, "end": 40.0, "text": "довга репліка"},
        {"start": 40.0, "end": 52.0, "text": "ще одна"},
        {"start": 52.0, "end": 60.0, "text": "пара фраз гостя"},
        {"start": 60.0, "end": 80.0, "text": "далі знову"},
    ]}), encoding="utf-8")
    speakers_json.write_text(_json.dumps({
        "_meta": {"needs_review": [], "no_match": [], "invalid_embedding": []},
        "SPEAKER_00": "Анна А", "SPEAKER_01": "Богдан Б",
    }, ensure_ascii=False), encoding="utf-8")

    # A previous run may have left a job for this date; the route refuses to
    # re-run a meeting that is already being processed.
    async def _clear_jobs() -> None:
        pool = await get_pool()
        try:
            async with tenant_cursor(pool, TENANT_A) as cur:
                await cur.execute(
                    "DELETE FROM ingestion_jobs WHERE meeting_date = %s", ("2026-06-15",)
                )
        finally:
            await close_pool()

    asyncio.run(_clear_jobs())

    with TestClient(app, follow_redirects=False) as client:
        assert client.post(
            "/login", data={"username": "anna", "password": PASSWORD_A}
        ).status_code == 303

        r = client.get("/meetings/2026-06-15/speakers")
        assert r.status_code == 200
        assert 'name="manual_new_time"' in r.text and 'name="manual_new_name"' in r.text
        assert "SPEAKER_02" in r.text, "the row must offer the next free label"
        ok("editor offers an empty row with the next free SPEAKER_XX")

        # A time nobody can parse must not save anything and must not queue a
        # multi-hour re-run — it comes back with the message instead.
        before = speakers_json.read_text()
        r = client.post("/meetings/2026-06-15/speakers", data={
            "name_SPEAKER_00": "Анна А", "name_SPEAKER_01": "Богдан Б",
            "manual_new_time": "колись по обіді", "manual_new_name": "Гість",
        })
        assert r.status_code == 303 and "/speakers?error=" in r.headers["location"]
        assert speakers_json.read_text() == before, "nothing may be written on error"
        ok("an unparsable timestamp refuses the whole submission")

        r = client.post("/meetings/2026-06-15/speakers", data={
            "name_SPEAKER_00": "Анна А", "name_SPEAKER_01": "Богдан Б",
            "manual_new_time": "0:55", "manual_new_name": "Гість Іван",
        })
        assert r.status_code == 303, r.status_code
        assert "/ingest?ok=" in r.headers["location"], r.headers["location"]
        ok("saving a manual speaker queues the re-run like any other edit")

    meta, mapping = speakers_util.load_speakers(speakers_json)
    assert mapping["SPEAKER_02"] == "Гість Іван"
    entries = manual_speakers.load_entries(meta)
    assert [e.label for e in entries] == ["SPEAKER_02"]
    # 0:55 fell inside the 52–60 phrase, so the WHOLE phrase moves: attribution
    # is per Whisper segment and half a phrase cannot change speaker.
    assert entries[0].windows == [(52.0, 60.0)], entries[0].windows
    ok("the typed moment snapped to the phrase spoken at it (52–60s)")

    assert pristine.exists(), "pyannote's own diarization must be preserved"
    stats = speakers_util.rttm_speaker_stats(rttm)
    assert stats["SPEAKER_02"]["total_s"] == 8.0
    # Subtracted, not merely inserted: merge_transcript picks the DOMINANT
    # speaker, so an added segment sharing the window would lose 12s to 8s.
    assert stats["SPEAKER_01"]["total_s"] == 32.0, stats["SPEAKER_01"]
    assert stats["SPEAKER_00"]["total_s"] == 40.0, "other speakers untouched"
    ok("RTTM rewritten: window given to the guest and taken from SPEAKER_01")

    # Eight seconds is far below polish_protocol's 30s attendance threshold —
    # a machine heuristic that must not overrule a person saying "he was there".
    attendees = polish_protocol.detect_attendees(rttm, speakers_json, {})
    assert attendees == ["Анна А", "Богдан Б", "Гість Іван"], attendees
    ok("manual speaker counts as an attendee despite the 30s minimum")

    with TestClient(app, follow_redirects=False) as client:
        assert client.post(
            "/login", data={"username": "anna", "password": PASSWORD_A}
        ).status_code == 303
        r = client.get("/meetings/2026-06-15/speakers")
        assert r.status_code == 200
        assert 'name="time_SPEAKER_02"' in r.text and 'value="0:55"' in r.text
        assert 'name="remove_SPEAKER_02"' in r.text
        assert "SPEAKER_03" in r.text, "the add-row moves on to the next label"
        ok("saved row comes back editable (time + remove) with samples filled")

        # Another church cannot reach it — same guard as every meeting route.
        assert client.post("/meetings/2026-06-22/speakers", data={}).status_code == 404
        ok("another church's meeting is 404, not an editable speakers.json")

    # Removing restores pyannote's file exactly — the edit was never destructive.
    gone = manual_speakers.apply_edits(
        folder / "audio_transcript.json", meta, mapping,
        [manual_speakers.ManualInput(label="SPEAKER_02", spec="0:55",
                                     name="Гість Іван", remove=True)],
    )
    manual_speakers.rebuild_rttm(rttm, gone.entries)
    assert rttm.read_text() == pristine.read_text()
    assert manual_speakers.META_KEY not in gone.meta
    ok("removing the manual speaker restores the original diarization")


# ─────────────────────────────────────────────────────────────
# 12. Web queries go through the queue
# ─────────────────────────────────────────────────────────────

def test_query_queue() -> None:
    """
    POST /api/query enqueues; it does not answer.

    The route used to run rag.answer() inline, which needed Ollama, the
    reranker and Gemma's weights inside the web process. These checks pin the
    new contract: the request returns a partial that watches the row, and the
    answer appears only once something else wrote it. Nothing here touches
    Ollama — which is the point, and also why it can be tested at all.

    DB work happens BETWEEN client sessions, never inside one: the pool is a
    module singleton bound to whichever loop opened it, and TestClient's
    lifespan owns it for the duration of the `with`.
    """
    print("\n12. Запит через чергу (web → queries → worker)")
    print("-" * 66)

    from fastapi.testclient import TestClient
    from church_assistant.db import queries_repo
    from church_assistant.web.main import app

    def _client() -> TestClient:
        return TestClient(app, follow_redirects=False)

    def _qid(html: str) -> int:
        assert 'hx-get="/api/query/' in html, "response does not watch a query"
        return int(html.split('hx-get="/api/query/')[1].split('"')[0])

    # ── Enqueue two questions and try one that must be refused ──
    with _client() as client:
        assert client.post(
            "/login", data={"username": "anna", "password": PASSWORD_A}
        ).status_code == 303

        r = client.post("/api/query", data={"question": "Про що говорили у червні?"})
        assert r.status_code == 200, r.status_code
        assert "hx-trigger" in r.text, "pending partial must keep polling"
        answered_id = _qid(r.text)
        ok(f"POST /api/query queues and returns a watching partial (#{answered_id})")

        r = client.get(f"/api/query/{answered_id}")
        assert r.status_code == 200 and "hx-trigger" in r.text
        assert "черз" in r.text.lower() or "оброб" in r.text.lower()
        ok("polling an unfinished query returns the waiting partial")

        failed_id = _qid(
            client.post("/api/query", data={"question": "Друге питання для збою"}).text
        )
        slow_id = _qid(
            client.post("/api/query", data={"question": "Третє питання, яке чекає довго"}).text
        )

        r = client.post("/api/query", data={"question": "ні"})
        assert r.status_code == 400, r.status_code
        assert "hx-get" not in r.text, "a refused question must not be watched"

    # ── What a worker would do, done here instead ──────────────
    async def _advance() -> tuple[str, int]:
        pool = await get_pool()
        try:
            waiting = await queries_repo.get_by_id(pool, TENANT_A, answered_id)
            async with tenant_cursor(pool, TENANT_A) as cur:
                await cur.execute("SELECT max(id) FROM queries")
                highest = (await cur.fetchone())[0]

            await queries_repo.mark_completed(
                pool, TENANT_A, answered_id,
                hits=[{
                    "score": 0.81, "vector_score": 0.55, "reranked": True,
                    "collection": "cma_turns",
                    "payload": {"meeting_date": "2026-06-15", "speaker": "Анна А",
                                "topic_title": "Тема А"},
                }],
                synthesis="Це відповідь, яку написав воркер.",
                sources=["2026-06-15"],
                embed_time_ms=11, qdrant_time_ms=22, rerank_time_ms=33,
                gemma_time_ms=44, total_time_ms=110,
            )
            await queries_repo.mark_failed(
                pool, TENANT_A, failed_id,
                error_message="Ollama unreachable",
                error_traceback="(none)",
                increment_retry=False,
            )
            # Age one query so the waiting partial has to do arithmetic on a
            # real timestamp rather than render a constant.
            async with tenant_cursor(pool, TENANT_A) as cur:
                await cur.execute(
                    "UPDATE queries SET asked_at = now() - interval '5 minutes' "
                    "WHERE id = %s", (slow_id,)
                )
            return waiting["status"], highest
        finally:
            await close_pool()

    status_while_watching, highest_id = asyncio.run(_advance())

    assert status_while_watching == "pending", status_while_watching
    ok("the query sat 'pending' — no RAG ran inside the web process")

    # The refused question was posted last, so if it had been queued anyway it
    # would hold the highest id instead of the last accepted one.
    assert highest_id == slow_id, f"a refused question was queued (#{highest_id})"
    ok("too-short question → 400 and nothing enqueued")

    # ── The answer, and the failure, reach the page ────────────
    with _client() as client:
        assert client.post(
            "/login", data={"username": "anna", "password": PASSWORD_A}
        ).status_code == 303

        r = client.get(f"/api/query/{answered_id}")
        assert r.status_code == 200
        assert "Це відповідь, яку написав воркер." in r.text, "synthesis not rendered"
        assert "Тема А" in r.text and "2026-06-15" in r.text, "hit / source missing"
        # Rebuilt from the row, so the stored timings must survive the trip.
        assert "44ms" in r.text, "timings lost on the way back"
        assert "hx-trigger" not in r.text, "the answer must stop the polling"
        ok("once completed, the poll swaps in the answer and stops polling")

        r = client.get(f"/api/query/{failed_id}")
        assert r.status_code == 200
        assert "Ollama unreachable" in r.text, "the row's reason must reach the page"
        assert "hx-trigger" not in r.text, "a failed query must stop the polling"
        ok("a failed query shows the recorded reason and stops polling")

        # The counter reads `asked_at`; `queries` has no `created_at`, and the
        # wrong key would fail silently — a missing timestamp is indistinguishable
        # from "no time has passed", so the counter would just sit at 0 forever.
        # That is exactly what happened, and only the browser showed it.
        r = client.get(f"/api/query/{slow_id}")
        assert r.status_code == 200 and "hx-trigger" in r.text
        shown = int(r.text.split("</strong>")[-1].split("с</span>")[0].strip().split(">")[-1])
        assert shown >= 290, f"elapsed counter reads {shown}s for a 5-minute-old query"
        assert "недоступний" in r.text, "a long wait must say the worker may be absent"
        ok(f"a {shown}s-old query shows real elapsed time and the absent-worker hint")

    # ── Another church cannot poll it ──────────────────────────
    with _client() as client:
        assert client.post(
            "/login", data={"username": "borys", "password": PASSWORD_B}
        ).status_code == 303
        r = client.get(f"/api/query/{answered_id}")
        assert r.status_code == 404, r.status_code
        assert "воркер" not in r.text, "another church's answer leaked into the body"
        ok("another church polling this query gets 404, not the answer")


# ─────────────────────────────────────────────────────────────
# 13. Artifact sync between the processing node and the control plane
# ─────────────────────────────────────────────────────────────

def test_artifact_sync() -> None:
    """
    The worker copies a meeting folder instead of mounting it.

    rsync happily takes a local path where a remote one goes, so the real
    transfer semantics can be exercised with no SSH and no VPS — which matters,
    because the thing that actually breaks here is trailing slashes: get one
    wrong and the pull lands in <date>/<date>/ while every path check still
    passes.
    """
    print("\n13. Синхронізація артефактів (worker ↔ control plane)")
    print("-" * 66)

    from church_assistant.ingestion import artifact_sync

    local = Path(tempfile.mkdtemp(prefix="cma_sync_local_"))
    remote = Path(tempfile.mkdtemp(prefix="cma_sync_remote_"))
    prev_root = os.environ["DATA_ROOT"]
    prev_remote = os.environ.get("ARTIFACT_SYNC_REMOTE")

    try:
        os.environ["DATA_ROOT"] = str(local)
        os.environ["ARTIFACT_SYNC_REMOTE"] = str(remote)
        rel = Path("tenants") / "church-a" / "meetings" / "2026-09-21"
        profiles = Path("tenants") / "church-a" / "voice_profiles"

        # What the control plane has after an upload: audio and nothing else.
        (remote / rel).mkdir(parents=True)
        (remote / rel / "audio.m4a").write_bytes(b"pretend this is 68 MB")
        (remote / profiles).mkdir(parents=True)
        (remote / profiles / "Анна А.npy").write_bytes(b"\x00\x01")

        # ── Pull ──────────────────────────────────────────────
        asyncio.run(artifact_sync.pull_meeting(local / rel))
        assert (local / rel / "audio.m4a").exists(), "audio did not arrive"
        assert not (local / rel / "2026-09-21").exists(), "pull nested the folder in itself"
        ok("pull brings the audio into the folder, not into a copy of it")

        asyncio.run(artifact_sync.pull_voice_profiles(local / profiles))
        assert (local / profiles / "Анна А.npy").exists()
        ok("voice profiles arrive before diarization matches against them")

        # ── Push ──────────────────────────────────────────────
        (local / rel / "polished.md").write_text("## Присутні\n\n- Анна А\n", encoding="utf-8")
        (local / rel / "annotated.md").write_text("[00:01] Анна А: слово\n", encoding="utf-8")
        asyncio.run(artifact_sync.push_meeting(local / rel))
        assert (remote / rel / "polished.md").exists(), "protocol did not come back"
        assert (remote / rel / "annotated.md").exists()
        assert not (remote / rel / "2026-09-21").exists(), "push nested the folder in itself"
        ok("push returns what the pipeline produced")

        # No --delete in either direction: the recording is the church's only
        # copy, and the pipeline never has a reason to remove it.
        assert (remote / rel / "audio.m4a").exists(), "push deleted the recording"
        ok("push leaves the recording alone (no --delete, either way)")

        # ── The review round-trip ─────────────────────────────
        # speakers.json edited on the web must win over the worker's older copy,
        # or analysis would put the pre-review names into the protocol.
        (local / rel / "speakers.json").write_text(SPEAKERS_STALE, encoding="utf-8")
        (remote / rel / "speakers.json").write_text(SPEAKERS_FIXED, encoding="utf-8")
        os.utime(remote / rel / "speakers.json", (2_000_000_000, 2_000_000_000))
        asyncio.run(artifact_sync.pull_meeting(local / rel))
        assert "виправлене" in (local / rel / "speakers.json").read_text(encoding="utf-8"), \
            "the reviewer's edit did not reach the worker"
        ok("a speakers.json edited on the web wins the pull before analysis")

        # ── A folder the control plane has never seen ─────────
        # rsync creates no intermediate directories and reports the omission as
        # a bare "error in file IO" (exit 11). The first version of this test
        # missed it because the fixture created the remote folder first — which
        # is exactly the assumption that does not hold on a real server.
        fresh = Path("tenants") / "church-a" / "meetings" / "2026-11-02"
        (local / fresh).mkdir(parents=True)
        (local / fresh / "polished.md").write_text("новий протокол\n", encoding="utf-8")
        assert not (remote / fresh).exists()
        asyncio.run(artifact_sync.push_meeting(local / fresh))
        assert (remote / fresh / "polished.md").exists(), \
            "push did not create the remote path"
        ok("push creates a remote folder that does not exist yet")

        # ── Failure surfaces, it does not pass silently ───────
        os.environ["ARTIFACT_SYNC_REMOTE"] = str(remote / "does-not-exist")
        try:
            asyncio.run(artifact_sync.pull_meeting(local / rel))
            raise AssertionError("a broken remote must raise, not return quietly")
        except artifact_sync.SyncError:
            pass
        ok("a failed copy raises SyncError → the job requeues")

        # ── Disabled is the default ───────────────────────────
        # Empty, not deleted: load_dotenv() would refill a deleted variable from
        # .env, which on this machine now configures a real remote. An empty
        # variable still exists, so it wins — and empty IS the disabled state.
        os.environ["ARTIFACT_SYNC_REMOTE"] = ""
        assert not artifact_sync.enabled()
        untouched = local / "tenants" / "church-a" / "meetings" / "2026-10-05"
        asyncio.run(artifact_sync.pull_meeting(untouched))
        assert not untouched.exists(), "a disabled pull must not create anything"
        ok("no remote configured → every call is a no-op (single-machine setup)")
    finally:
        os.environ["DATA_ROOT"] = prev_root
        if prev_remote is None:
            os.environ.pop("ARTIFACT_SYNC_REMOTE", None)
        else:
            os.environ["ARTIFACT_SYNC_REMOTE"] = prev_remote
        shutil.rmtree(local, ignore_errors=True)
        shutil.rmtree(remote, ignore_errors=True)


# ─────────────────────────────────────────────────────────────
# 14. Creating a church (platform admin only)
# ─────────────────────────────────────────────────────────────

def test_create_church() -> dict:
    """
    Two panels that do not intersect.

    A platform account runs the fleet and belongs to no church; a church account
    runs its own congregation and cannot see that a fleet panel exists. The
    checks that matter are the refusals in both directions — the happy path is
    the easy half.
    """
    print("\n14. Платформова панель і панель церкви")
    print("-" * 66)

    from fastapi.testclient import TestClient
    from church_assistant.db import tenants_repo, web_invites_repo, web_users_repo
    from church_assistant.web import security as sec
    from church_assistant.web.main import app

    SLUG = f"smoke-church-{secrets.token_hex(3)}"
    CH_ADMIN = f"smoke-pastor-{secrets.token_hex(3)}"
    ROOT = f"smoke-root-{secrets.token_hex(3)}"
    ROOT_PW = "korin-parol-2026"

    def _client() -> TestClient:
        return TestClient(app, follow_redirects=False)

    async def _make_root() -> str:
        """A platform account in `_system`, claimed through an invite."""
        pool = await get_pool()
        try:
            uid = await web_users_repo.add_web_user(
                pool, 0, username=ROOT, full_name="Тест Корінь", role="admin",
                password_hash=sec.hash_password(sec.new_session_token()),
            )
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT set_config('app.current_tenant','0',true)")
                    await cur.execute(
                        "UPDATE web_users SET is_platform_admin=TRUE, is_active=FALSE"
                        " WHERE id=%s", (uid,))
            token = sec.new_session_token()
            await web_invites_repo.create(
                pool, 0, web_user_id=uid, token_hash=sec.hash_token(token),
                created_by="test",
            )
            return token
        finally:
            await close_pool()

    # ── The platform account claims itself ────────────────────
    token = asyncio.run(_make_root())
    with _client() as client:
        r = client.post(f"/invite/{token}",
                        data={"password": ROOT_PW, "password_repeat": ROOT_PW})
        assert r.status_code == 303, r.status_code
        # A church admin lands on their accounts page; a platform account has no
        # church and would be refused there.
        assert r.headers["location"] == "/platform", r.headers["location"]
        ok("a platform invite redeems into the platform panel, not a church page")

    # ── A church account cannot see the fleet ─────────────────
    with _client() as client:
        assert client.post(
            "/login", data={"username": "anna", "password": PASSWORD_A}
        ).status_code == 303
        assert "Нова церква" not in client.get("/admin/users").text
        for path in ("/platform", "/platform/panel"):
            assert client.get(path).status_code == 404, path
        r = client.post("/platform/churches", data={
            "slug": "sneaky", "name": "Х", "admin_username": "x", "admin_full_name": "Х"})
        assert r.status_code == 404, r.status_code
        ok("church admin: the platform panel is 404, and absent from their page")

    # ── The platform account cannot reach any church ──────────
    with _client() as client:
        assert client.post(
            "/login", data={"username": ROOT, "password": ROOT_PW}
        ).status_code == 303
        for path in ("/meetings", "/admin/users", "/ingest", "/history"):
            assert client.get(path).status_code == 403, path
        ok("platform admin: every church route refuses them (403, not a crash)")

        assert client.get("/platform").status_code == 200
        ok("platform admin: the fleet panel opens")

        # ── Registration ──────────────────────────────────────
        r = client.post("/platform/churches", data={
            "slug": "../etc", "name": "Х", "admin_username": "x", "admin_full_name": "Х"})
        assert r.status_code == 200 and "створено" not in r.text
        ok("a traversal slug is refused (same validator as paths and collections)")

        # Founding a church whose admin carries a name another church already
        # uses. Before 014 this was refused and the tenant rolled back; now it
        # is simply a different church's business. The founding account is
        # inactive until its invite is redeemed, so `borys` does not become an
        # ambiguous login here — see the resolver checks in section 1.
        r = client.post("/platform/churches", data={
            # An independent slug, not a suffix of SLUG: later checks ask
            # "is SLUG still listed?" with a substring test.
            "slug": f"smoke-tezka-{secrets.token_hex(3)}", "name": "Церква тезки",
            "admin_username": "borys", "admin_full_name": "Борис Тезка"})
        assert "створено" in r.text and "без адміна" not in r.text
        ok("a login used elsewhere no longer blocks founding a church (014)")

        r = client.post("/platform/churches", data={
            "slug": SLUG, "name": "Тестова церква",
            "admin_username": CH_ADMIN, "admin_full_name": "Новий Пастор"})
        assert r.status_code == 200 and "створено" in r.text, r.text[:200]
        import re as _re3
        m = _re3.search(r'class="church-password">([^<]+)<', r.text)
        assert m and "/invite/" in m.group(1), "no invite link"
        invite_url = m.group(1).strip()
        ok("church registered from the platform panel, with an invite link")

        # The new church appears in the list — the gap that started all this.
        assert SLUG in client.get("/platform/panel").text
        ok("the new church shows up in the fleet list")

    async def _founded() -> tuple[int, list[Any]]:
        pool = await get_pool()
        try:
            t = await tenants_repo.get_by_slug(pool, SLUG)
            return int(t["id"]), await web_users_repo.list_all(pool, int(t["id"]))
        finally:
            await close_pool()

    tid, users = asyncio.run(_founded())
    assert [u["username"] for u in users] == [CH_ADMIN]
    assert users[0]["role"] == "admin" and not users[0]["is_active"]
    assert not users[0]["is_platform_admin"], "a founded admin inherited the platform"
    ok("founding account: admin, inactive until claimed, no platform powers")

    # ── The founded admin claims it and stays inside their church ──
    with _client() as client:
        r = client.post(invite_url.replace("http://testserver", ""),
                        data={"password": "parol-pastora-1", "password_repeat": "parol-pastora-1"})
        assert r.status_code == 303 and r.headers["location"] == "/admin/users"
        body = client.get("/meetings").text
        assert "2026-06-15" not in body, "the new church sees church A's meetings"
        assert client.get("/platform").status_code == 404
        ok("the founded admin lands in their church, sees nothing of A, no platform")

    # ── Suspension cuts access without touching data ──────────
    with _client() as client:
        assert client.post(
            "/login", data={"username": ROOT, "password": ROOT_PW}
        ).status_code == 303
        r = client.post(f"/platform/churches/{tid}/suspend")
        assert r.status_code == 200 and SLUG in r.text
        ok("a church can be suspended from the panel")

    with _client() as client:
        r = client.post("/login", data={"username": CH_ADMIN, "password": "parol-pastora-1"})
        assert r.status_code != 303, "a suspended church still accepts sign-in"
        ok("its people can no longer sign in")

    async def _still_there() -> bool:
        pool = await get_pool()
        try:
            t = await tenants_repo.get_by_slug(pool, SLUG)
            return t is not None and not t["is_active"]
        finally:
            await close_pool()

    assert asyncio.run(_still_there()), "suspension removed the church"
    ok("suspension is about access — the church and its data stay")

    # Handed to test_archive_church, which goes on with this same church:
    # rebuilding one there would re-test registration and prove nothing new.
    return {"tid": tid, "slug": SLUG, "church_admin": CH_ADMIN,
            "root": ROOT, "root_pw": ROOT_PW}


# ─────────────────────────────────────────────────────────────
# 15. Renaming and archiving a church
# ─────────────────────────────────────────────────────────────
def test_archive_church(ctx: dict) -> None:
    """
    Renaming is cosmetic; archiving is not, and the difference has to hold.

    Archiving must stop access through every door at once — sessions, sign-in
    and any invite issued while the church was live — because a link handed out
    last week outlives the decision made today. And it must not be reachable by
    a mis-click: the panel asks for the identifier to be typed.
    """
    print("\n15. Renaming and archiving")

    from fastapi.testclient import TestClient

    from church_assistant.db import tenants_repo, web_invites_repo
    from church_assistant.web.main import app

    def _client() -> TestClient:
        return TestClient(app, follow_redirects=False)

    # The heading, not the word: every live row carries a button titled
    # "Архівувати …", so a bare "Архів" matches the page that has no archive.
    ARCHIVE_HEADING = "📦 Архів ("

    tid, SLUG = ctx["tid"], ctx["slug"]
    CH_ADMIN, ROOT, ROOT_PW = ctx["church_admin"], ctx["root"], ctx["root_pw"]

    async def _tenant() -> dict:
        pool = await get_pool()
        try:
            return await tenants_repo.get_by_slug(pool, SLUG)
        finally:
            await close_pool()

    # ── Renaming touches the name and nothing else ────────────
    with _client() as client:
        assert client.post(
            "/login", data={"username": ROOT, "password": ROOT_PW}
        ).status_code == 303

        r = client.post(f"/platform/churches/{tid}/rename",
                        data={"name": "Церква на Волі"})
        assert r.status_code == 200 and "Церква на Волі" in r.text
        ok("a church can be renamed from the panel")

        # Empty names are refused rather than silently blanking the label the
        # operator navigates by.
        r = client.post(f"/platform/churches/{tid}/rename", data={"name": "   "})
        assert r.status_code == 200 and "Церква на Волі" in r.text
        ok("an empty name is refused, the old one survives")

    t = asyncio.run(_tenant())
    assert t["name"] == "Церква на Волі" and t["slug"] == SLUG
    ok("renaming leaves the identifier alone — the disk path never moves")

    # ── Back to a fully live church ───────────────────────────
    # test_create_church left it suspended, and suspension already closes
    # sign-in and invites. Archiving has to be shown to close them on a church
    # that was open a second ago, or the check proves nothing.
    with _client() as client:
        assert client.post(
            "/login", data={"username": ROOT, "password": ROOT_PW}
        ).status_code == 303
        assert client.post(f"/platform/churches/{tid}/resume").status_code == 200

    with _client() as client:
        r = client.post("/login", data={"username": CH_ADMIN, "password": "parol-pastora-1"})
        assert r.status_code == 303, "the resumed church still refuses its admin"
        ok("resumed: its admin signs in again")

    # ── An invite issued while the church is live ─────────────
    invite_token = secrets.token_urlsafe(32)

    async def _issue_invite() -> None:
        pool = await get_pool()
        try:
            users = await web_users_repo.list_all(pool, tid)
            await web_invites_repo.create(
                pool, tid,
                web_user_id=users[0]["id"],
                token_hash=security.hash_token(invite_token),
                created_by="test",
            )
        finally:
            await close_pool()

    asyncio.run(_issue_invite())
    with _client() as client:
        assert client.get(f"/invite/{invite_token}").status_code == 200
        ok("an invite issued now works")

    # ── Archiving needs the identifier typed ──────────────────
    with _client() as client:
        assert client.post(
            "/login", data={"username": ROOT, "password": ROOT_PW}
        ).status_code == 303

        for wrong in ("", "  ", SLUG.upper(), SLUG + "x", "Церква на Волі"):
            r = client.post(f"/platform/churches/{tid}/archive",
                            data={"confirm_slug": wrong})
            assert r.status_code == 200, wrong
            assert SLUG in r.text, f"archived on a wrong confirmation: {wrong!r}"
            # The Архів section is rendered only when something is in it, so
            # its absence is the check that nothing slipped through.
            assert ARCHIVE_HEADING not in r.text, f"archived on {wrong!r}"
        ok("archiving refuses every near-miss confirmation, including the name")

        r = client.post(f"/platform/churches/{tid}/archive",
                        data={"confirm_slug": SLUG})
        assert r.status_code == 200
        ok("archiving accepts the identifier typed exactly")

        # Gone from the live list, present in the archive — one panel, two
        # tables, and the church must not appear in both.
        body = client.get("/platform/panel").text
        assert ARCHIVE_HEADING in body and SLUG in body
        live = body.split(ARCHIVE_HEADING)[0]
        assert SLUG not in live, "an archived church is still listed as live"
        ok("it leaves the church list and appears under Архів")

    t = asyncio.run(_tenant())
    assert t is not None, "archiving deleted the row"
    assert t["deleted_at"] is not None and not t["is_active"]
    ok("the tenant row survives — archiving is not deletion")

    # ── Every door is shut, not just the front one ────────────
    with _client() as client:
        r = client.post("/login", data={"username": CH_ADMIN, "password": "parol-pastora-1"})
        assert r.status_code != 303
        ok("archived: nobody signs in")

    with _client() as client:
        assert client.get(f"/invite/{invite_token}").status_code != 200
        ok("archived: the invite handed out last week stops working too")

    # ── The platform cannot archive itself ────────────────────
    with _client() as client:
        assert client.post(
            "/login", data={"username": ROOT, "password": ROOT_PW}
        ).status_code == 303
        r = client.post("/platform/churches/0/archive", data={"confirm_slug": "_system"})
        assert r.status_code == 200

    async def _system_intact() -> bool:
        pool = await get_pool()
        try:
            sys_t = await tenants_repo.get_by_id(pool, 0)
            return sys_t is not None and sys_t["deleted_at"] is None
        finally:
            await close_pool()

    assert asyncio.run(_system_intact()), "the platform archived itself"
    ok("the platform cannot archive itself out of its own panel")

    # ── Purging is refused while the year is running ──────────
    from church_assistant.scripts import purge_archived_tenants as purge

    async def _archived_row() -> dict:
        pool = await get_pool()
        try:
            rows = await tenants_repo.list_archived(pool)
            return next(r for r in rows if r["slug"] == SLUG)
        finally:
            await close_pool()

    row = asyncio.run(_archived_row())
    assert not row["overdue"], "a church archived seconds ago is already purgeable"
    assert 360 <= purge._days_left(row["purge_after"]) <= 365
    ok(f"the archive clock runs {tenants_repo.ARCHIVE_RETENTION_DAYS} days")

    # ── Restoring comes back suspended, not live ──────────────
    with _client() as client:
        assert client.post(
            "/login", data={"username": ROOT, "password": ROOT_PW}
        ).status_code == 303
        r = client.post(f"/platform/churches/{tid}/restore")
        assert r.status_code == 200
        live = (r.text.split(ARCHIVE_HEADING)[0]
                if ARCHIVE_HEADING in r.text else r.text)
        assert SLUG in live
        ok("a church can be brought back out of the archive")

    t = asyncio.run(_tenant())
    assert t["deleted_at"] is None, "restore left it archived"
    assert not t["is_active"], "restore also handed out access"
    ok("restored suspended: coming back and letting people in stay two decisions")

    with _client() as client:
        r = client.post("/login", data={"username": CH_ADMIN, "password": "parol-pastora-1"})
        assert r.status_code != 303
        ok("its people still cannot sign in until someone resumes it")



# ─────────────────────────────────────────────────────────────
# 16. Recovering a church that locked itself out
# ─────────────────────────────────────────────────────────────
def test_recover_admin(ctx: dict) -> None:
    """
    The operator can hand a church back its front door — and only that.

    A church issues its own links, but only an admin can, so a church whose last
    admin forgets their password has nobody inside left to ask and no email to
    ask instead. The panel can now re-reach one. The checks that matter are the
    limits: a LINK and never a password, the old secret untouched until the link
    is spent, no guessing between namesake admins, and a row in the CHURCH'S own
    log so the crossing is never quiet.

    Runs on the church test_archive_church leaves behind — restored from the
    archive and suspended, with one admin, `parol-pastora-1`.
    """
    print("\n16. Відновлення доступу церкви")
    print("-" * 66)

    import re as _re

    from fastapi.testclient import TestClient

    from church_assistant.db import web_users_repo
    from church_assistant.web import security as _sec
    from church_assistant.web.main import app

    tid, SLUG = ctx["tid"], ctx["slug"]
    CH_ADMIN, ROOT, ROOT_PW = ctx["church_admin"], ctx["root"], ctx["root_pw"]
    OLD_PW = "parol-pastora-1"

    def _client() -> TestClient:
        return TestClient(app, follow_redirects=False)

    def _as_root(client: TestClient) -> None:
        assert client.post(
            "/login", data={"username": ROOT, "password": ROOT_PW}
        ).status_code == 303

    def _link(body: str) -> str:
        m = _re.search(r'class="church-password">([^<]+)<', body)
        assert m and "/invite/" in m.group(1), body[:400]
        return m.group(1).strip().replace("http://testserver", "")

    # ── Only the platform may ask ─────────────────────────────
    with _client() as client:
        assert client.post(
            "/login", data={"username": CH_ADMIN, "password": OLD_PW}
        ).status_code != 303, "a suspended church still accepts sign-in"

    with _client() as client:
        _as_root(client)
        assert client.post(f"/platform/churches/{tid}/resume").status_code == 200

    with _client() as client:
        assert client.post(
            "/login", data={"username": CH_ADMIN, "password": OLD_PW}
        ).status_code == 303
        r = client.post("/platform/recover-admin", data={"church": tid})
        assert r.status_code == 404, r.status_code
        ok("recovery: a church admin cannot reach it, not even for their own church")

    # ── One admin: no need to name them, and they are named back ──
    with _client() as client:
        _as_root(client)
        r = client.post("/platform/recover-admin", data={"church": tid})
        assert r.status_code == 200, r.text[:300]
        assert CH_ADMIN in r.text, "the panel did not say whose link this is"
        first_link = _link(r.text)
        assert OLD_PW not in r.text, "the operator was shown a password"
        ok("one admin: a link is issued without naming anybody, and echoes who")

    # Issuing takes nothing away yet — the reason an operator reaches for this
    # is a lockout, not a revocation, and a link may never be followed.
    with _client() as client:
        assert client.post(
            "/login", data={"username": CH_ADMIN, "password": OLD_PW}
        ).status_code == 303
        ok("issuing a link changes nothing: the old password still works")

    # ── A second link kills the first (the shared invite invariant) ──
    with _client() as client:
        _as_root(client)
        second_link = _link(client.post(
            "/platform/recover-admin", data={"church": tid}).text)
        assert second_link != first_link

    with _client() as client:
        r = client.post(first_link, data={"password": "nova-parol-11",
                                          "password_repeat": "nova-parol-11"})
        assert r.status_code != 303, "the superseded link still worked"
        ok("re-issuing expires the previous link — never two doors at once")

    # ── Redeeming: the person sets their own secret, old sessions die ──
    # One client throughout: the cookie is captured, put aside while the link is
    # spent, and offered again afterwards. Two live TestClients would mean two
    # app lifespans over one pool singleton.
    with _client() as client:
        assert client.post(
            "/login", data={"username": CH_ADMIN, "password": OLD_PW}
        ).status_code == 303
        stale_cookie = client.cookies[_sec.SESSION_COOKIE]
        assert client.get("/meetings").status_code == 200

        client.cookies.clear()
        r = client.post(second_link, data={"password": "nova-parol-22",
                                           "password_repeat": "nova-parol-22"})
        assert r.status_code == 303, r.text[:300]
        ok("the admin redeems the link and is signed straight in")

        client.cookies.clear()
        client.cookies.set(_sec.SESSION_COOKIE, stale_cookie)
        assert client.get("/meetings").status_code != 200, "an old session survived"
        ok("redeeming ended the sessions the old password was holding open")

    with _client() as client:
        assert client.post(
            "/login", data={"username": CH_ADMIN, "password": OLD_PW}
        ).status_code != 303, "the old password still works after recovery"
        assert client.post(
            "/login", data={"username": CH_ADMIN, "password": "nova-parol-22"}
        ).status_code == 303
        ok("the new password is the only one that works, and nobody else set it")

    # ── The church can see it happened ────────────────────────
    async def _church_log() -> list:
        pool = await get_pool()
        try:
            return await audit_repo.list_recent(pool, tid, limit=50)
        finally:
            await close_pool()

    events = asyncio.run(_church_log())
    rec = [e for e in events if e["action"] == "platform.admin_recovery_issued"]
    assert len(rec) == 2, [e["action"] for e in events][:10]
    assert ROOT in str(rec[0]["actor"]), rec[0]["actor"]
    assert rec[0]["detail"]["username"] == CH_ADMIN
    ok("every issue is written to the church's own log, naming the operator")

    # ── Namesake admins: it refuses rather than guesses ───────
    def sec_hash() -> str:
        return _sec.hash_password(_sec.new_session_token())

    async def _second_admin() -> None:
        pool = await get_pool()
        try:
            await web_users_repo.add_web_user(
                pool, tid, username="drugiy-pastor",
                password_hash=sec_hash(), full_name="Другий Пастор", role="admin")
        finally:
            await close_pool()

    asyncio.run(_second_admin())

    with _client() as client:
        _as_root(client)
        r = client.post("/platform/recover-admin", data={"church": tid})
        assert "/invite/" not in r.text, "it picked one of two admins on its own"
        assert CH_ADMIN in r.text and "drugiy-pastor" in r.text
        ok("two admins: it refuses and lists them rather than guess")

        r = client.post("/platform/recover-admin",
                        data={"church": tid, "username": "nemaye-takoho"})
        assert "/invite/" not in r.text and CH_ADMIN in r.text
        ok("an admin login that does not exist is refused, with the real ones shown")

        r = client.post("/platform/recover-admin",
                        data={"church": tid, "username": "drugiy-pastor"})
        assert "/invite/" in r.text and "drugiy-pastor" in r.text
        ok("naming one of several admins issues that one's link")

    # ── No admins left: the case the CLI used to be the only answer to ──
    async def _switch_off_admins() -> None:
        pool = await get_pool()
        try:
            for a in await web_users_repo.list_admins(pool, tid):
                await web_users_repo.set_role(pool, tid, int(a["id"]), "member")
        finally:
            await close_pool()

    asyncio.run(_switch_off_admins())

    with _client() as client:
        _as_root(client)
        r = client.post("/platform/recover-admin", data={"church": tid})
        assert "/invite/" not in r.text and "не лишилось" in r.text
        ok("no admin at all: it asks for a login and a name instead of failing")

        r = client.post("/platform/recover-admin",
                        data={"church": tid, "username": CH_ADMIN,
                              "full_name": "Хтось Інший"})
        assert "/invite/" not in r.text, "it created a duplicate login"
        ok("a login already used in that church is refused, roles are not handed out")

        r = client.post("/platform/recover-admin",
                        data={"church": tid, "username": "novyi-pastor",
                              "full_name": "Новий Пастор"})
        assert "/invite/" in r.text, r.text[:300]
        new_link = _link(r.text)
        ok("a church with no admin gets a new one, as a link and not a password")

    async def _new_admin_row() -> dict:
        pool = await get_pool()
        try:
            u = await web_users_repo.get_by_username(pool, tid, "novyi-pastor")
            return dict(u)
        finally:
            await close_pool()

    row = asyncio.run(_new_admin_row())
    assert row["role"] == "admin" and not row["is_active"]
    assert not row["is_platform_admin"], "a recovered admin inherited the platform"
    ok("the created account is admin, inactive until claimed, with no platform powers")

    with _client() as client:
        r = client.post(new_link, data={"password": "parol-novoho-33",
                                        "password_repeat": "parol-novoho-33"})
        assert r.status_code == 303 and r.headers["location"] == "/admin/users"
        assert client.get("/platform").status_code == 404
        ok("they land in their own church's admin page, and see no fleet panel")

    # ── An archived church is not recoverable this way ────────
    with _client() as client:
        _as_root(client)
        assert client.post(f"/platform/churches/{tid}/archive",
                           data={"confirm_slug": SLUG}).status_code == 200
        r = client.post("/platform/recover-admin", data={"church": tid})
        assert "/invite/" not in r.text and "архів" in r.text.lower()
        ok("an archived church is refused: restore it first, on purpose")

        r = client.post("/platform/recover-admin", data={"church": 0})
        assert "/invite/" not in r.text
        ok("the platform tenant itself is not a church you can recover")



# ─────────────────────────────────────────────────────────────
# 17. Purging a church — and the churches that cannot be purged
# ─────────────────────────────────────────────────────────────
async def test_tenant_purge(pool) -> None:
    """
    Deleting a church has to work, and has to be impossible for the wrong one.

    Until 016 neither was true. Every foreign key into tenants was NO ACTION, so
    the purge died on the first audit_log row and had never once completed; and
    the refusal meant to protect the founding corpus asked where its artifact
    folder sat, which stopped being the shared data root when the folders moved
    on 25.08 — leaving `default` guarded by nothing but the same foreign keys
    the fix was about to remove.

    So the two halves are tested together, because shipping either alone is the
    bug: the cascade is what makes purge possible, the trigger is what keeps it
    aimed.
    """
    print("\n17. Видалення церкви — і церкви, які видалити не можна")
    print("-" * 66)

    import psycopg

    from church_assistant.db import (
        audit_repo, tenants_repo, web_invites_repo, web_sessions_repo,
        web_users_repo,
    )
    from church_assistant.db.tenant_context import tenant_cursor
    from church_assistant.scripts import purge_archived_tenants as purge
    from church_assistant.web import security as _sec

    TABLES = ("audit_log", "errors", "ingestion_jobs", "logs", "queries",
              "users", "web_invites", "web_sessions", "web_users")

    async def _delete_tenant(tid: int) -> None:
        """Its own connection: a refusal aborts the transaction it happens in."""
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("DELETE FROM tenants WHERE id = %s", (tid,))

    async def _counts(tid: int) -> dict:
        out = {}
        async with tenant_cursor(pool, tid) as cur:
            for t in TABLES:
                await cur.execute(f"SELECT count(*) FROM {t}")
                out[t] = (await cur.fetchone())[0]
        return out

    async def _populate(tid: int) -> None:
        uid = await web_users_repo.add_web_user(
            pool, tid, username="purge-victim",
            password_hash=_sec.hash_password("x" * 12),
            full_name="Той, кого приберуть", role="admin")
        await web_sessions_repo.create(
            pool, tid, web_user_id=uid,
            token_hash=_sec.hash_token(_sec.new_session_token()),
            ttl_seconds=3600, user_agent="smoke", ip="127.0.0.1")
        await web_invites_repo.create(
            pool, tid, web_user_id=uid,
            token_hash=_sec.hash_token(_sec.new_session_token()),
            created_by="smoke")
        await audit_repo.record(
            pool, tenant_id=tid, action="smoke.purge_fixture",
            actor="smoke", resource=f"web_users/{uid}", detail={})
        async with tenant_cursor(pool, tid) as cur:
            await cur.execute(
                "INSERT INTO logs (tenant_id, level, process, event, message) "
                "VALUES (%s, 'INFO', 'cli', 'smoke.purge_fixture', 'fixture')",
                (tid,))

    # ── The founding corpus cannot be deleted, by either name ──
    for tid, what in ((0, "_system"), (1, "the founding corpus")):
        try:
            await _delete_tenant(tid)
            raise AssertionError(f"tenant {tid} ({what}) was deleted")
        except psycopg.errors.RaiseException:
            pass
    ok("tenant 0 and tenant 1 are refused by the database itself")

    assert purge._protected_reason(1, "anything") is not None
    assert purge._protected_reason(0, "anything") is not None
    assert purge._protected_reason(9, "default") is not None, \
        "LEGACY_TENANT_SLUG is not protected by name"
    assert purge._protected_reason(9, "some-church") is None
    ok("the script refuses them too — before a vector or a file is touched")

    # Renaming must not unprotect it: the guard is by id, not by slug.
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT slug FROM tenants WHERE id = 1")
            was = (await cur.fetchone())[0]
            await cur.execute("UPDATE tenants SET slug = 'renamed-away' WHERE id = 1")
    try:
        await _delete_tenant(1)
        raise AssertionError("renaming the founding church unprotected it")
    except psycopg.errors.RaiseException:
        pass
    finally:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("UPDATE tenants SET slug = %s WHERE id = 1", (was,))
    ok("renaming it does not unprotect it — the guard is on the id")

    # ── A live church with people in it is not deletable at all ──
    live_id = await tenants_repo.create_tenant(
        pool, slug=f"smoke-live-{secrets.token_hex(3)}", name="Жива церква")
    await _populate(live_id)
    try:
        await _delete_tenant(live_id)
        raise AssertionError("a live church with accounts was deleted outright")
    except psycopg.errors.RaiseException:
        pass
    ok("a church that is not archived and has people cannot be deleted")

    # ── …but a half-registered empty one still can (delete_if_empty) ──
    empty_id = await tenants_repo.create_tenant(
        pool, slug=f"smoke-empty-{secrets.token_hex(3)}", name="Порожня")
    assert await tenants_repo.delete_if_empty(pool, empty_id) is True
    ok("an empty, never-claimed tenant is still removable — registration rollback")

    assert await tenants_repo.delete_if_empty(pool, live_id) is False
    ok("delete_if_empty still refuses a church that has accounts")

    # ── Archived: the purge completes, and takes every table with it ──
    await tenants_repo.archive(pool, live_id)
    before = await _counts(live_id)
    assert before["web_users"] and before["web_sessions"] and before["web_invites"]
    assert before["audit_log"] and before["logs"], before
    await _delete_tenant(live_id)
    ok(f"an archived church deletes, cascading {sum(before.values())} rows")

    # Counted through the tenant's own context, not with a bare WHERE: the
    # policies cast current_setting('app.current_tenant') to bigint, so a query
    # on these tables outside any context dies on the empty string rather than
    # answering. (That is the same trap the trigger fell into a moment ago.)
    after = await _counts(live_id)
    assert not any(after.values()), after
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT count(*) FROM tenants WHERE id = %s", (live_id,))
            assert (await cur.fetchone())[0] == 0
    ok("nothing survives in any of the nine tables, audit_log included")

    # The founding corpus is untouched by all of the above.
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT slug FROM tenants WHERE id = 1")
            assert (await cur.fetchone()) is not None, "the founding church is gone"
    ok("`default` is still there, which is the only result that matters")



async def phase_db() -> None:
    pool = await get_pool()
    try:
        await seed(pool)
        await test_db_layer(pool)
    finally:
        await close_pool()


async def phase_audit() -> None:
    pool = await get_pool()
    try:
        await test_audit(pool)
        await test_system_tenant(pool)
    finally:
        await close_pool()


async def phase_purge() -> None:
    """
    Last, and on its own pool: it deletes tenants, so nothing may run after it
    that still expects the sandbox seed to be intact.
    """
    pool = await get_pool()
    try:
        await test_tenant_purge(pool)
    finally:
        await close_pool()


def main() -> int:
    """
    Each async phase gets its own event loop and closes the connection pool
    before the next one starts — the pool is a module singleton bound to the
    loop that opened it, and TestClient runs the app in a loop of its own.
    """
    print("=" * 66)
    print("  MT Phase 3 — web auth / FS / Qdrant isolation (sandbox cma_mt3)")
    print("=" * 66)

    try:
        asyncio.run(phase_db())
        test_fs_isolation()
        test_portable_meeting_dir()
        test_collections()
        test_http()              # owns its pool via the app's lifespan
        test_shared_login()      # ditto
        test_admin_ui()          # ditto
        test_sessions()
        test_no_pinned_service_addresses()
        test_hardening()
        test_pdf_export()
        test_manual_speaker()
        test_query_queue()
        test_artifact_sync()
        church_ctx = test_create_church()
        test_archive_church(church_ctx)
        test_recover_admin(church_ctx)
        asyncio.run(phase_audit())
        asyncio.run(phase_purge())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)

    print()
    print("=" * 66)
    print(f"  ✓ ALL {len(passed)} MT PHASE 3 CHECKS PASSED")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
