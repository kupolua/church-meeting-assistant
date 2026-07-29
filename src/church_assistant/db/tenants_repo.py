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
