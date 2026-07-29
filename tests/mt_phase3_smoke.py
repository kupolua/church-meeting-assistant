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
    $DOCKER exec cma-postgres psql -U cma -d postgres -q \
      -c "DROP DATABASE IF EXISTS cma_mt3;" \
      -c "CREATE DATABASE cma_mt3;"

    # heredoc/stdin NEEDS `docker exec -i` — without -i stdin never arrives
    for f in schema.sql migrations/003_multitenancy.sql \
             migrations/004_app_role_and_claim.sql \
             migrations/005_mt_fixups.sql migrations/006_web_auth.sql \
             migrations/007_system_tenant.sql migrations/008_web_sessions.sql \
             migrations/009_session_idle_timeout.sql; do
      $DOCKER exec -i cma-postgres psql -U cma -d cma_mt3 -q -v ON_ERROR_STOP=1 \
        < src/church_assistant/db/$f
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
import secrets
import shutil
import sys
import tempfile
from pathlib import Path

# ─── Environment BEFORE any church_assistant import (config is read at import) ─
#
# Only DB_NAME is redirected. Host/user/password come from .env, i.e. the same
# cma_app credentials the live services use — the role is cluster-wide, so
# inventing a test password here would mean changing the LIVE one. What keeps
# this safe is the database name: every connection below goes to cma_mt3.
TMP = Path(tempfile.mkdtemp(prefix="cma_mt3_data_"))
os.environ.update(
    DB_NAME="cma_mt3",
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
    resolve_tenant_for_web_user,
    tenant_cursor,
)
from church_assistant.shared import collections, tenant_paths  # noqa: E402
from church_assistant.web import security  # noqa: E402


TENANT_A, TENANT_B, TENANT_OFF = 2, 3, 4
PASSWORD_A = "pravylnyi-parol-A"
PASSWORD_B = "pravylnyi-parol-B"

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

    assert await resolve_tenant_for_web_user(pool, "anna") == TENANT_A
    assert await resolve_tenant_for_web_user(pool, "borys") == TENANT_B
    assert await resolve_tenant_for_web_user(pool, "nobody") is None
    ok("resolve_tenant_for_web_user routes each login to its own church")

    # Church A's context sees only Anna; Borys is invisible even by name.
    a_user = await web_users_repo.get_by_username(pool, TENANT_A, "anna")
    assert a_user is not None and a_user["tenant_id"] == TENANT_A
    assert await web_users_repo.get_by_username(pool, TENANT_A, "borys") is None
    assert await web_users_repo.get_by_username(pool, TENANT_B, "anna") is None
    ok("RLS hides another church's web accounts (both directions)")

    a_list = await web_users_repo.list_active(pool, TENANT_A)
    assert [u["username"] for u in a_list] == ["anna"], a_list
    ok("list_active returns only this church's accounts")

    # Usernames are globally unique — the routing invariant.
    try:
        await web_users_repo.add_web_user(
            pool, TENANT_B, username="anna",
            password_hash=security.hash_password("x" * 12), full_name="Клон",
        )
        raise AssertionError("duplicate username across tenants should be refused")
    except web_users_repo.WebUserAlreadyExists:
        pass
    ok("username stays globally unique (one person → one church)")

    assert security.verify_password(PASSWORD_A, a_user["password_hash"])
    assert not security.verify_password(PASSWORD_B, a_user["password_hash"])
    ok("stored scrypt hash verifies the right password only")


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

        # Create → the new account can sign in immediately.
        r = client.post("/admin/users", data={
            "username": "dmytro", "full_name": "Дмитро Д",
            "role": "member", "password": "novyi-parol-123",
        })
        assert r.status_code == 200 and "створено" in r.text, r.text[:300]
        ok("admin creates an account in their church")

        # Sign in as the new account on the SAME client — a nested TestClient
        # would start a second event loop and close this one's pool underneath
        # it (the pool is a module singleton bound to the loop that opened it).
        client.cookies.clear()
        r = client.post("/login", data={"username": "dmytro",
                                        "password": "novyi-parol-123"})
        assert r.status_code == 303 and security.SESSION_COOKIE in r.cookies
        # Landed in church A: they see A's meeting and not the B-only date, and
        # as a member they get no account-management link.
        page = client.get("/meetings").text
        assert "2026-06-15" in page and "2026-06-22" not in page
        assert "/admin/users" not in page
        ok("the new account logs in, into the creating admin's church, as member")

        client.cookies.clear()
        login(client, "anna", PASSWORD_A)

        # Usernames are global; the clash message reveals nothing about church B.
        r = client.post("/admin/users", data={
            "username": "borys", "full_name": "Клон",
            "role": "member", "password": "shche-odyn-parol",
        })
        assert "зайнятий" in r.text and "church-b" not in r.text
        ok("clash with another church's login is refused without disclosing it")

        r = client.post("/admin/users", data={
            "username": "korotkyi", "full_name": "Х", "role": "member", "password": "abc",
        })
        assert "закороткий" in r.text
        ok("short password rejected")

        # Guard rails.
        me = client.get("/admin/users")
        import re as _re
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
        test_collections()
        test_http()              # owns its pool via the app's lifespan
        test_admin_ui()          # ditto
        test_sessions()
        test_hardening()
        asyncio.run(phase_audit())
    finally:
        shutil.rmtree(TMP, ignore_errors=True)

    print()
    print("=" * 66)
    print(f"  ✓ ALL {len(passed)} MT PHASE 3 CHECKS PASSED")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
