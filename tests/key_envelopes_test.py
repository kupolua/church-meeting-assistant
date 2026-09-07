"""
Migration 019 as executable claims: the envelopes are stored, they are isolated,
and the server that stores them cannot open them.

    uv run python tests/key_envelopes_test.py

BUILDS ITS OWN DATABASE AND DROPS IT AFTERWARDS. `cma_keys_test` is created from
schema.sql plus every migration in order, so this also proves the chain still
applies from nothing — the thing that stops being true quietly, one migration at
a time, until the day a second church has to be created.

⚠️ NEVER POINT THIS AT `cma`. It drops the database it works on. The scratch
name is hard-coded rather than configurable for exactly that reason.

Needs the local `cma-postgres` container (the M1 rollback copy is fine — it is
used only as an engine here) and DB_PASSWORD from .env for the cma_app role.
Superuser work goes through `docker exec`, because the container's own password
is not in .env and does not need to be: inside the container psql authenticates
locally.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "src")

import psycopg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

from church_assistant.shared import crypto as c  # noqa: E402


DB = "cma_keys_test"
CONTAINER = os.getenv("CMA_PG_CONTAINER", "cma-postgres")
# Docker Desktop puts `docker` behind a shell alias that subprocesses do not
# inherit — the same trap deploy/backup_from_vps.sh documents.
DOCKER = next(
    (p for p in ("/opt/homebrew/bin/docker",
                 "/usr/local/bin/docker",
                 "/Applications/Docker.app/Contents/Resources/bin/docker")
     if Path(p).exists()),
    "docker",
)
ROOT = Path(__file__).resolve().parents[1]
PASSED = 0


def ok(label: str) -> None:
    global PASSED
    PASSED += 1
    print(f"  ✓ {label}")


def sup(sql: str, read: bool = False) -> subprocess.CompletedProcess:
    """Superuser statement, inside the container."""
    flags = ["-tAc"] if read else ["-q", "-c"]
    return subprocess.run(
        [DOCKER, "exec", CONTAINER, "psql", "-U", "cma", "-d", DB, *flags, sql],
        capture_output=True, text=True,
    )


def build() -> None:
    subprocess.run(
        [DOCKER, "exec", CONTAINER, "psql", "-U", "cma", "-d", "postgres", "-q",
         "-c", f"DROP DATABASE IF EXISTS {DB}", "-c", f"CREATE DATABASE {DB}"],
        capture_output=True, text=True, check=True,
    )
    files = [ROOT / "src/church_assistant/db/schema.sql"]
    files += sorted((ROOT / "src/church_assistant/db/migrations").glob("*.sql"))
    for f in files:
        r = subprocess.run(
            [DOCKER, "exec", "-i", CONTAINER, "psql", "-U", "cma", "-d", DB,
             "-q", "-v", "ON_ERROR_STOP=1"],
            stdin=f.open("rb"), capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise SystemExit(f"✗ {f.name}\n{r.stderr[:400]}")
    ok(f"схема й {len(files) - 1} міграцій лягли на порожню базу")


def main() -> None:
    load_dotenv(ROOT / ".env")
    pw = os.getenv("DB_PASSWORD")
    if not pw:
        raise SystemExit("немає DB_PASSWORD у .env")

    print("Конверти ключів (міграція 019)")
    build()

    sup("INSERT INTO tenants (slug, name) VALUES ('a','A'),('b','B')")
    rows = [l.split("|") for l in
            sup("SELECT id, kek_version FROM tenants WHERE slug IN ('a','b') ORDER BY slug",
                True).stdout.strip().splitlines()]
    ta, tb = int(rows[0][0]), int(rows[1][0])
    assert rows[0][1] == "0"
    ok("нова церква стартує з kek_version = 0 — не зашифрована")

    kek, dek = c.new_key(), c.new_key()
    phone, recovery = c.new_device_keys(), c.new_device_keys()
    aad = c.context(ta, "dek", "2026-07-27")

    dsn = f"host=127.0.0.1 port=5433 dbname={DB} user=cma_app password={pw}"
    with psycopg.connect(dsn, connect_timeout=8, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("SELECT set_config('app.current_tenant', %s, false)", (str(ta),))

        cur.execute(
            "INSERT INTO church_devices (tenant_id, public_key, kind, label) "
            "VALUES (%s,%s,'device','Телефон Романа'),(%s,%s,'recovery','Сейф парафії') "
            "RETURNING id",
            (ta, phone.public, ta, recovery.public))
        dev, rec = (r[0] for r in cur.fetchall())
        ok("пристрій і recovery-ключ — рядки однієї таблиці, один код-шлях")

        for did, pub in ((dev, phone.public), (rec, recovery.public)):
            cur.execute("INSERT INTO key_envelopes (tenant_id, device_id, kek_version, wrapped_kek) "
                        "VALUES (%s,%s,1,%s)", (ta, did, c.wrap_for(pub, kek)))
        cur.execute("INSERT INTO dek_envelopes (tenant_id, scope, kek_version, wrapped_dek) "
                    "VALUES (%s,'2026-07-27',1,%s)", (ta, c.wrap_dek(kek, dek, aad)))
        cur.execute("UPDATE tenants SET kek_version = 1 WHERE id = %s", (ta,))
        ok("конверти KEK і DEK збережені, версія церкви піднята")

        # Повний шлях пристрою: дістати з БД → розгорнути KEK → розгорнути DEK.
        cur.execute("SELECT wrapped_kek FROM key_envelopes WHERE device_id = %s", (dev,))
        got_kek = c.unwrap(phone.private, bytes(cur.fetchone()[0]))
        cur.execute("SELECT wrapped_dek FROM dek_envelopes WHERE scope = '2026-07-27'")
        got_dek = c.unwrap_dek(got_kek, bytes(cur.fetchone()[0]), aad)
        assert (got_kek, got_dek) == (kek, dek)
        ok("телефон: БД → KEK → DEK")

        cur.execute("SELECT wrapped_kek FROM key_envelopes WHERE device_id = %s", (rec,))
        assert c.unwrap(recovery.private, bytes(cur.fetchone()[0])) == kek
        ok("папір із сейфа дає той самий KEK")

        cur.execute("SELECT set_config('app.current_tenant', %s, false)", (str(tb),))
        for t in ("church_devices", "key_envelopes", "dek_envelopes"):
            cur.execute(f"SELECT count(*) FROM {t}")
            assert cur.fetchone()[0] == 0, t
        ok("RLS: інша церква не бачить ні пристроїв, ні конвертів")

        try:
            cur.execute("INSERT INTO church_devices (tenant_id, public_key, kind) "
                        "VALUES (%s,%s,'device')", (ta, c.new_device_keys().public))
            raise AssertionError("WITH CHECK пропустив запис у чужу церкву")
        except psycopg.errors.InsufficientPrivilege:
            ok("RLS: не можна записати пристрій у чужу церкву")

    for n in (31, 33):
        r = sup(f"INSERT INTO church_devices (tenant_id, public_key, kind) "
                f"VALUES ({ta}, repeat('\\000',{n})::bytea, 'device')")
        assert "violates check constraint" in r.stderr, r.stderr[:200]
    ok("CHECK: публічний ключ мусить бути рівно 32 байти")

    r = sup(f"INSERT INTO church_devices (tenant_id, public_key, kind) "
            f"VALUES ({ta}, repeat('\\000',32)::bytea, 'watch')")
    assert "violates check constraint" in r.stderr, r.stderr[:200]
    ok("CHECK: kind обмежений device/recovery/processor")

    v = sup("SELECT max(version) FROM schema_version", True).stdout.strip()
    assert v == "20", v
    ok(f"schema_version = {v}")

    subprocess.run([DOCKER, "exec", CONTAINER, "psql", "-U", "cma", "-d", "postgres",
                    "-q", "-c", f"DROP DATABASE {DB}"], capture_output=True)
    print(f"\n✅ {PASSED} перевірок пройдено (базу {DB} прибрано)")


if __name__ == "__main__":
    main()
