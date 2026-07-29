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
