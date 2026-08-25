"""
Tenants repository — the church/council registry (MT Phase 1).

The `tenants` table is NOT RLS-gated (it's the registry that maps identity →
tenant), so these functions use a plain pooled connection. Creating/editing
tenants is an ADMIN operation (platform owner / supervisory board), not a
tenant-scoped one.
"""

from __future__ import annotations

from typing import Any, Optional

from psycopg.rows import dict_row
from psycopg.errors import ForeignKeyViolation
from psycopg_pool import AsyncConnectionPool


# ─────────────────────────────────────────────────────────────
# Reserved tenants
# ─────────────────────────────────────────────────────────────

# The platform itself (migration 007). Owns events that belong to no church —
# worker.started, health warnings, "Ollama unreachable". A fixed id rather than
# a lookup because shared/logger.py needs it before any tenant context exists
# and must never raise. It is is_active=FALSE, which is what stops anyone
# logging in "as the platform" and keeps it out of list_active().
SYSTEM_TENANT_ID = 0
SYSTEM_TENANT_SLUG = "_system"


def is_system_tenant(tenant_id: int) -> bool:
    """True for the reserved platform tenant (no church, no files, no vectors)."""
    return int(tenant_id) == SYSTEM_TENANT_ID


async def create_tenant(
    pool: AsyncConnectionPool,
    *,
    slug: str,
    name: str,
    telegram_bot_token: Optional[str] = None,
    settings: Optional[dict[str, Any]] = None,
) -> int:
    """Create a church/council. Returns the new tenant id."""
    import json
    sql = """
        INSERT INTO tenants (slug, name, telegram_bot_token, settings)
        VALUES (%s, %s, %s, %s::jsonb)
        RETURNING id
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (
                slug, name, telegram_bot_token,
                json.dumps(settings or {}, ensure_ascii=False),
            ))
            row = await cur.fetchone()
            if row is None:
                raise RuntimeError("INSERT ... RETURNING did not return an id")
            return int(row[0])


async def get_by_id(pool: AsyncConnectionPool, tenant_id: int) -> Optional[dict[str, Any]]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM tenants WHERE id = %s", (tenant_id,))
            return await cur.fetchone()


async def get_by_slug(pool: AsyncConnectionPool, slug: str) -> Optional[dict[str, Any]]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM tenants WHERE slug = %s", (slug,))
            return await cur.fetchone()


async def list_active(pool: AsyncConnectionPool) -> list[dict[str, Any]]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM tenants WHERE is_active ORDER BY name"
            )
            return list(await cur.fetchall())


async def set_active(pool: AsyncConnectionPool, tenant_id: int, active: bool) -> None:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE tenants SET is_active = %s WHERE id = %s", (active, tenant_id)
            )


# ─────────────────────────────────────────────────────────────
# id → slug (hot path for the shared workers)
# ─────────────────────────────────────────────────────────────

class TenantNotFound(Exception):
    """No tenant with that id — a claimed row points at a deleted church."""


# The background workers claim jobs across tenants and then need the slug for
# every one (it addresses the filesystem subtree and the Qdrant collections).
# Slugs never change — they're the key the storage layout is built on — so a
# process-lifetime cache is safe and saves a query per job.
_slug_cache: dict[int, str] = {}


async def get_slug(pool: AsyncConnectionPool, tenant_id: int) -> str:
    """The tenant's slug, cached for the process lifetime."""
    cached = _slug_cache.get(tenant_id)
    if cached is not None:
        return cached

    tenant = await get_by_id(pool, tenant_id)
    if tenant is None:
        raise TenantNotFound(f"No tenant with id={tenant_id}")
    slug = str(tenant["slug"])
    _slug_cache[tenant_id] = slug
    return slug


async def delete_if_empty(pool: AsyncConnectionPool, tenant_id: int) -> bool:
    """
    Remove a church that has no accounts. Returns True if it was removed.

    Exists for exactly one situation: create_tenant succeeded and creating its
    first admin did not. Logins are globally unique and RLS hides other
    churches', so that clash cannot be seen before the INSERT — and by then the
    tenant row is there, holding a slug the operator would otherwise have to
    abandon over a typo.

    The `NOT EXISTS` is the guard, in SQL rather than in the caller: a check
    that lives in the calling code protects only the callers that remember it.
    A church with a single account is not empty and will not be deleted here.
    """
    sql = """
        DELETE FROM tenants
        WHERE id = %s
          AND NOT EXISTS (SELECT 1 FROM web_users WHERE tenant_id = tenants.id)
          AND NOT EXISTS (SELECT 1 FROM users     WHERE tenant_id = tenants.id)
    """
    try:
      async with pool.connection() as conn:
        async with conn.cursor() as cur:
            # Both subqueries read RLS-protected tables, and the policy casts
            # current_setting('app.current_tenant') to bigint — with no tenant
            # context that setting is '' and the cast fails before the delete is
            # even considered. Set it to the tenant being removed, in the same
            # transaction, so RLS shows exactly the rows the guard needs to see.
            await cur.execute(
                "SELECT set_config('app.current_tenant', %s, true)", (str(tenant_id),)
            )
            await cur.execute(sql, (tenant_id,))
            return cur.rowcount > 0
    except ForeignKeyViolation:
        # Something else already points at this tenant — a log line from its
        # first sign-in, an audit row. The NOT EXISTS guard covers accounts
        # because those are what "empty" means to the caller, but it cannot
        # enumerate every table that might reference a church, and it should not
        # try: the honest answer to "is this safe to remove" is then no.
        # Caught outside the connection block so the aborted transaction is
        # rolled back by the context manager before we return.
        return False


async def list_with_counts(pool: AsyncConnectionPool) -> list[dict[str, Any]]:
    """
    Every church with its size — for the platform panel.

    Counts only: accounts, pending invites, ingestion jobs. Nothing that could
    be read as the church's content, because the panel that shows this is
    allowed to run the service and not to read the conversations.

    `_system` is excluded by the function: the platform is not a church, and
    listing it invites somebody to suspend the tenant they are standing in.
    """
    sql = """
        SELECT t.id, t.slug, t.name, t.is_active, t.created_at,
               c.accounts, c.active_accounts, c.pending_invites, c.jobs
        FROM tenants t
        JOIN platform_church_counts() c ON c.tenant_id = t.id
        ORDER BY t.id
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql)
            return list(await cur.fetchall())
