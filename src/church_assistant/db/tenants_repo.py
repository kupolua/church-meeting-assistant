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
    first admin did not — for whatever reason, and by then the tenant row is
    there, holding a slug the operator would otherwise have to abandon over a
    typo. (Before migration 014 the usual reason was a login already taken in
    some other church; names stopped being server-wide, so that one is gone.)

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
        WHERE t.deleted_at IS NULL
        ORDER BY t.id
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql)
            return list(await cur.fetchall())


# A year. Long enough that a church that left in anger and came back in spring
# still finds its history; short enough that "we keep it forever" is not a
# promise nobody made.
ARCHIVE_RETENTION_DAYS = 365


async def rename(pool: AsyncConnectionPool, tenant_id: int, name: str) -> None:
    """
    Change the display name. NOT the slug.

    The slug is a directory on disk and a prefix on four Qdrant collections;
    renaming it would mean moving 1.9 GB and reindexing, and getting half way
    through either would leave a church whose data the application cannot find.
    The name is a label, and labels are free to change.
    """
    name = name.strip()
    if not name:
        raise ValueError("name cannot be empty")
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE tenants SET name = %s WHERE id = %s AND id <> 0",
                (name, tenant_id),
            )


async def archive(pool: AsyncConnectionPool, tenant_id: int) -> None:
    """
    Retire a church: access stops now, data stays.

    Both fields are set together — deleted_at is what every resolver checks, and
    is_active keeps the rest of the system (which has known about it since
    Phase 1) telling the same story. `id <> 0` because archiving the platform
    would lock the operator out of the panel they are standing in.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE tenants SET deleted_at = NOW(), is_active = FALSE "
                "WHERE id = %s AND id <> 0 AND deleted_at IS NULL",
                (tenant_id,),
            )


async def restore(pool: AsyncConnectionPool, tenant_id: int) -> None:
    """
    Bring an archived church back. Its own action, not the suspension toggle.

    Restored suspended rather than live: coming back from the archive is a
    decision, and so is letting people in again. Making one imply the other
    would mean a mis-click on restore also hands out access.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE tenants SET deleted_at = NULL WHERE id = %s AND id <> 0",
                (tenant_id,),
            )


async def list_archived(pool: AsyncConnectionPool) -> list[dict[str, Any]]:
    """Archived churches with how long they have left."""
    sql = """
        SELECT id, slug, name, deleted_at,
               deleted_at + make_interval(days => %s) AS purge_after,
               (deleted_at + make_interval(days => %s)) < NOW() AS overdue
        FROM tenants
        WHERE deleted_at IS NOT NULL
        ORDER BY deleted_at DESC
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (ARCHIVE_RETENTION_DAYS, ARCHIVE_RETENTION_DAYS))
            return list(await cur.fetchall())
