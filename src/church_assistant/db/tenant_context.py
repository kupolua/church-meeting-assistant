"""
Tenant context — the bridge between a request and RLS-enforced isolation.

RLS (see migrations/003_multitenancy.sql) is FAIL-CLOSED: unless a transaction
sets `app.current_tenant`, the session sees no tenant rows and cannot insert.
Every DB operation that touches a tenant-scoped table (users, queries, logs,
errors, ingestion_jobs, audit_log) must therefore run inside `tenant_cursor()`.

IMPORTANT — DB role: RLS is bypassed by SUPERUSERS and BYPASSRLS roles. The app
must connect as a NON-superuser role (e.g. `cma_app`) or isolation is a no-op.
The current default `cma` role is the container superuser — switch DB_USER to a
dedicated app role at cutover (see the deploy notes).

Resolution bootstrap: to know which tenant a request belongs to we must query
`users` — but that is RLS-gated. resolve_tenant_for_telegram() calls a
SECURITY DEFINER function that bypasses RLS just for this lookup.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Optional

from psycopg_pool import AsyncConnectionPool


async def set_tenant(cur: Any, tenant_id: int) -> None:
    """
    Bind the current transaction to a tenant (SET LOCAL semantics).

    Call at the start of a transaction, before touching tenant tables. Because
    it is transaction-local (set_config is_local=true), a pooled connection
    resets automatically when the transaction ends — no leak to the next borrower.
    """
    await cur.execute(
        "SELECT set_config('app.current_tenant', %s, true)", (str(int(tenant_id)),)
    )


@asynccontextmanager
async def tenant_cursor(
    pool: AsyncConnectionPool,
    tenant_id: int,
    *,
    row_factory: Any = None,
) -> AsyncIterator[Any]:
    """
    Yield a cursor already bound to `tenant_id`. Commits on clean exit.

    Usage:
        async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM queries WHERE id = %s", (qid,))
            row = await cur.fetchone()   # RLS guarantees it's this tenant's row
    """
    async with pool.connection() as conn:
        if row_factory is not None:
            cur_cm = conn.cursor(row_factory=row_factory)
        else:
            cur_cm = conn.cursor()
        async with cur_cm as cur:
            await set_tenant(cur, tenant_id)
            yield cur


async def resolve_tenant_for_telegram(
    pool: AsyncConnectionPool, telegram_user_id: int
) -> Optional[int]:
    """
    Which tenant does this Telegram user belong to? None if unknown/inactive.

    Uses the SECURITY DEFINER resolver (bypasses RLS) — safe to call without a
    tenant context (it's the bootstrap that GIVES us the tenant).
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT resolve_tenant_for_telegram(%s)", (int(telegram_user_id),)
            )
            row = await cur.fetchone()
            return int(row[0]) if row and row[0] is not None else None


async def login_tenants(
    pool: AsyncConnectionPool, username: str, church: Optional[str] = None
) -> list[int]:
    """
    Which churches could this login belong to? Ordered, possibly empty.

    The login-side twin of resolve_tenant_for_telegram (migration 015, replacing
    014's resolve_login_tenant): the same SECURITY DEFINER bootstrap, because
    web_users is RLS-gated and login has no tenant context yet.

    It reports candidates rather than picking one, because the password is what
    picks — the caller checks it against each and asks the person only when two
    answer to the same name and the same secret. Hashes are still read afterwards
    inside each tenant's own context, so this call leaks at most "such a username
    exists", and now also how many churches carry it.

    With `church` given, the list is narrowed to that one (by identifier, or by
    display name while it points at a single candidate) — empty if it holds no
    such account, which the caller must not distinguish from a wrong password.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT tenant_id FROM login_tenants(%s, %s)", (username, church),
            )
            return [int(r[0]) for r in await cur.fetchall()]
