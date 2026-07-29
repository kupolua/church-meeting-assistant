"""
CLI: check whether the live database and .env are ready for the MT cutover.

READ-ONLY. This script changes nothing, ever — it connects, looks, and prints a
verdict plus the exact steps to run. The cutover itself is deliberately not
automated: it is destructive, it needs a backup taken at a moment a human
chooses, and RLS is fail-closed, so a half-applied cutover means all four
services see zero rows. That is a decision to make with your eyes open, not
something a script should do because it was invoked.

WHY IT MATTERS THAT IT'S ALL-OR-NOTHING: migrations 003-009 turn on row-level
security. Code that does not set a tenant context sees nothing at all — no
errors, just empty pages and an idle queue. So the DB migration, the DB_USER
switch and the service restart have to land together.

Usage:
    # Against the live DB, using .env as the services see it:
    uv run python -m church_assistant.scripts.cutover_preflight

    # Against some other database:
    DB_NAME=cma_staging uv run python -m church_assistant.scripts.cutover_preflight
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import Any, Optional

from dotenv import load_dotenv
from psycopg_pool import AsyncConnectionPool

from church_assistant.db.connection import _build_conninfo


# Every migration the cutover applies, with the schema_version it records.
REQUIRED_MIGRATIONS = [
    ("003_multitenancy.sql", 4, "tenants + tenant_id + audit_log + RLS"),
    ("004_app_role_and_claim.sql", 5, "cma_app role + cross-tenant claim"),
    ("005_mt_fixups.sql", 6, "per-tenant ingestion uniqueness + alert helpers"),
    ("006_web_auth.sql", 7, "web_users + login resolver"),
    ("007_system_tenant.sql", 8, "_system tenant (id 0)"),
    ("008_web_sessions.sql", 9, "server-side sessions"),
    ("009_session_idle_timeout.sql", 10, "session idle timeout"),
]

RLS_TABLES = [
    "users", "queries", "logs", "errors", "ingestion_jobs",
    "audit_log", "web_users", "web_sessions",
]

OK, WARN, BAD = "✓", "!", "✗"


class Report:
    """Collects findings so the summary can be a verdict, not a scroll-back."""

    def __init__(self) -> None:
        self.blockers: list[str] = []
        self.warnings: list[str] = []

    def line(self, mark: str, text: str) -> None:
        print(f"  {mark} {text}")
        if mark == BAD:
            self.blockers.append(text)
        elif mark == WARN:
            self.warnings.append(text)


async def _scalar(pool: AsyncConnectionPool, sql: str, *params: Any) -> Any:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, params or None)
            row = await cur.fetchone()
            return row[0] if row else None


async def check_env(r: Report) -> Optional[str]:
    """Environment the SERVICES will use. Returns the configured DB user."""
    load_dotenv()
    print("\nEnvironment (.env)")
    print("-" * 66)

    db_user = os.getenv("DB_USER", "cma")
    if db_user == "cma_app":
        r.line(OK, "DB_USER=cma_app (non-superuser → RLS applies)")
    else:
        r.line(WARN, f"DB_USER={db_user!r} — must become 'cma_app' AT cutover. "
                     f"A superuser bypasses RLS entirely, so isolation would be "
                     f"a no-op while everything still appears to work.")

    if os.getenv("WEB_SECRET_KEY", "").strip():
        r.line(OK, "WEB_SECRET_KEY is set")
    else:
        r.line(BAD, "WEB_SECRET_KEY is missing — the web app refuses to start. "
                    'Generate: python -c "import secrets; '
                    'print(secrets.token_urlsafe(48))"')

    sys_tenant = os.getenv("SYSTEM_TENANT_ID")
    if sys_tenant not in (None, "", "0"):
        r.line(WARN, f"SYSTEM_TENANT_ID={sys_tenant!r} — after 007 this should be "
                     f"unset (defaults to 0). A wrong id silently drops system logs.")
    else:
        r.line(OK, "SYSTEM_TENANT_ID unset/0 (the _system tenant)")

    cookie_secure = os.getenv("WEB_COOKIE_SECURE", "auto")
    r.line(OK, f"WEB_COOKIE_SECURE={cookie_secure} "
               f"({'Secure only over https' if cookie_secure == 'auto' else 'explicit'})")
    return db_user


async def check_migrations(pool: AsyncConnectionPool, r: Report) -> None:
    print("\nMigrations applied to this database")
    print("-" * 66)

    exists = await _scalar(
        pool, "SELECT to_regclass('public.schema_version') IS NOT NULL"
    )
    if not exists:
        r.line(BAD, "no schema_version table — is this the right database?")
        return

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT version FROM schema_version")
            applied = {int(v[0]) for v in await cur.fetchall()}

    for filename, version, what in REQUIRED_MIGRATIONS:
        if version in applied:
            r.line(OK, f"{filename} (v{version}) — {what}")
        else:
            r.line(WARN, f"{filename} (v{version}) NOT applied — {what}")


async def check_role(pool: AsyncConnectionPool, r: Report) -> None:
    print("\nApplication role")
    print("-" * 66)

    row = await _scalar(
        pool,
        "SELECT rolsuper::text || '/' || rolbypassrls::text || '/' || rolcanlogin::text "
        "FROM pg_roles WHERE rolname = 'cma_app'",
    )
    if row is None:
        r.line(WARN, "role cma_app does not exist yet (migration 004 creates it; "
                     "the password is set separately, by you)")
        return

    is_super, bypass, can_login = row.split("/")
    if is_super == "true" or bypass == "true":
        r.line(BAD, "cma_app is SUPERUSER or has BYPASSRLS — it would ignore every "
                    "isolation policy. Recreate it NOSUPERUSER NOBYPASSRLS.")
    else:
        r.line(OK, "cma_app is NOSUPERUSER / NOBYPASSRLS")

    if can_login == "true":
        r.line(OK, "cma_app can log in")
    else:
        r.line(BAD, "cma_app cannot log in — set LOGIN and a password")

    # Roles are CLUSTER-wide while grants are per-database, so the role can
    # exist here having been created by migrating some other database (a
    # sandbox, a staging copy). Then it can connect to this one and read the
    # catalogs, with whatever password that other setup gave it.
    n_grants = await _scalar(
        pool,
        "SELECT count(*) FROM information_schema.role_table_grants "
        "WHERE grantee = 'cma_app'",
    )
    if not n_grants:
        r.line(WARN, "cma_app exists but has NO table grants in this database — "
                     "it was created by migrating a different one. Its password "
                     "is whatever that setup used: set a strong one at cutover "
                     "(step 2), or DROP ROLE cma_app and let 004 recreate it.")
    else:
        r.line(OK, f"cma_app holds {n_grants} table grant(s) here")


async def check_rls(pool: AsyncConnectionPool, r: Report) -> None:
    print("\nRow-level security")
    print("-" * 66)

    for table in RLS_TABLES:
        state = await _scalar(
            pool,
            "SELECT relrowsecurity::text || '/' || relforcerowsecurity::text "
            "FROM pg_class WHERE relname = %s AND relkind = 'r'",
            table,
        )
        if state is None:
            r.line(WARN, f"{table}: table missing (migration not applied yet)")
            continue
        enabled, forced = state.split("/")
        n_pol = await _scalar(
            pool, "SELECT count(*) FROM pg_policies WHERE tablename = %s", table
        )
        if enabled == "true" and forced == "true" and n_pol:
            r.line(OK, f"{table}: RLS enabled + forced, {n_pol} policy")
        else:
            r.line(WARN, f"{table}: RLS enabled={enabled} forced={forced} "
                         f"policies={n_pol} (expected true/true/1)")


async def check_data(pool: AsyncConnectionPool, r: Report) -> None:
    print("\nExisting data")
    print("-" * 66)

    if not await _scalar(pool, "SELECT to_regclass('public.tenants') IS NOT NULL"):
        r.line(WARN, "tenants table missing — nothing to check until 003 runs")
        return

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT id, slug, is_active FROM tenants ORDER BY id")
            for tid, slug, active in await cur.fetchall():
                r.line(OK, f"tenant {tid}: {slug} (active={active})")

    n_web = await _scalar(pool, "SELECT count(*) FROM web_users") \
        if await _scalar(pool, "SELECT to_regclass('public.web_users') IS NOT NULL") \
        else None
    if n_web is None:
        r.line(WARN, "web_users missing — create the first admin AFTER migrating")
    elif n_web == 0:
        r.line(WARN, "no web accounts yet — nobody can sign in until you create one "
                     "(scripts.add_web_user --tenant 1 --role admin)")
    else:
        r.line(OK, f"{n_web} web account(s) exist")


def print_runbook(db_user: Optional[str]) -> None:
    docker = "/Applications/Docker.app/Contents/Resources/bin/docker"
    print("\n" + "=" * 66)
    print("  CUTOVER RUNBOOK (nothing above changed anything)")
    print("=" * 66)
    print(f"""
 0. STOP the four services first. RLS is fail-closed: between the migration
    and the restart, old code sees zero rows — better stopped than confusing.

 1. BACK UP. This is the only step that makes the rest reversible:
      {docker} exec cma-postgres pg_dump -U cma -Fc cma > cma_before_mt.dump
      cp .env .env.before_mt
      # data/ and Qdrant are NOT touched by the cutover (the legacy tenant keeps
      # its existing folders and cma_* collections), so they need no backup for
      # this change.

 2. Create the app role's password (pick a strong one; it goes in .env):
      {docker} exec cma-postgres psql -U cma -d cma -c \\
        "ALTER ROLE cma_app PASSWORD '<STRONG-PASSWORD>';"
    (Run this AFTER step 3 if the role does not exist yet — 004 creates it.)

 3. Apply the migrations, in order, stopping on the first error.
    NOTE the -i: without it the heredoc never reaches psql.
      for f in 003_multitenancy 004_app_role_and_claim 005_mt_fixups \\
               006_web_auth 007_system_tenant 008_web_sessions \\
               009_session_idle_timeout; do
        {docker} exec -i cma-postgres psql -U cma -d cma -v ON_ERROR_STOP=1 \\
          < src/church_assistant/db/migrations/$f.sql || break
      done

 4. Point the app at the non-superuser role, in .env:
      DB_USER=cma_app
      DB_PASSWORD=<the password from step 2>
      WEB_SECRET_KEY=<already set if the check above said so>

 5. Re-run this preflight. Everything should be {OK}.

 6. Create the first web account (until this exists nobody can sign in):
      uv run python -m church_assistant.scripts.add_web_user \\
        --tenant 1 --username <you> --name "<Your Name>" --role admin

 7. Start the four services. Then verify, in this order:
      - sign in at /login  (proves web auth + sessions + RLS context)
      - /dashboard shows the queue  (proves tenant-scoped reads)
      - ask the bot a question      (proves the worker's cross-tenant claim)

 ROLLBACK, if step 7 goes wrong:
      # restore .env first — the old code cannot work as cma_app
      cp .env.before_mt .env
      {docker} exec -i cma-postgres pg_restore -U cma -d cma --clean --if-exists \\
        < cma_before_mt.dump
      # then restart the services on the old code (git checkout main)

 AFTERWARDS (optional, separate day):
      uv run python -m church_assistant.scripts.migrate_tenant_fs          # dry run
      uv run python -m church_assistant.scripts.migrate_tenant_fs --apply
""")
    if db_user != "cma_app":
        print(f" {WARN} Reminder: DB_USER is still {db_user!r}. Until it is "
              f"'cma_app',\n     RLS is bypassed and the isolation this whole "
              f"branch adds does nothing.\n")


async def async_main() -> int:
    print("=" * 66)
    print("  MT cutover preflight — READ-ONLY, nothing is modified")
    print("=" * 66)

    r = Report()
    db_user = await check_env(r)

    load_dotenv()
    print(f"\nDatabase: {os.getenv('DB_NAME', 'cma')} "
          f"@ {os.getenv('DB_HOST', '127.0.0.1')}:{os.getenv('DB_PORT', '5433')}")

    try:
        pool = AsyncConnectionPool(conninfo=_build_conninfo(), min_size=1,
                                   max_size=2, open=False)
        await pool.open()
    except Exception as e:
        print(f"\n  {BAD} cannot connect: {type(e).__name__}: {e}")
        print("\n  Fix the connection first — everything below needs it.")
        return 2

    try:
        await check_migrations(pool, r)
        await check_role(pool, r)
        await check_rls(pool, r)
        await check_data(pool, r)
    finally:
        await pool.close()

    print("\n" + "=" * 66)
    if r.blockers:
        print(f"  {BAD} NOT READY — {len(r.blockers)} blocker(s):")
        for b in r.blockers:
            print(f"      - {b}")
    elif r.warnings:
        print(f"  {WARN} Pre-cutover state: {len(r.warnings)} step(s) still to do.")
        print("      That is expected BEFORE the cutover — see the runbook.")
    else:
        print(f"  {OK} READY — migrations applied, RLS forced, role correct.")
    print("=" * 66)

    print_runbook(db_user)
    return 1 if r.blockers else 0


def main() -> None:
    sys.exit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
