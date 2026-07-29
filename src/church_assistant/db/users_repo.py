"""
Users repository — the per-tenant Telegram whitelist (tenant-aware, MT Phase 1).

Every op runs via tenant_context.tenant_cursor(pool, tenant_id) → RLS scopes it
to one church. The bot resolves the tenant FIRST (tenant_context.
resolve_tenant_for_telegram, a SECURITY DEFINER lookup) and then calls these
with that tenant_id.

telegram_user_id stays globally UNIQUE: a person belongs to exactly one church
(that's the routing invariant). add_user therefore raises UserAlreadyExists even
if the clashing row lives in another tenant (invisible under RLS, but the unique
index is global).

Soft delete only (is_active=false) — never DELETE rows.
"""

from __future__ import annotations

from typing import Any, Optional

from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from church_assistant.db.tenant_context import tenant_cursor


class UserAlreadyExists(Exception):
    """telegram_user_id already registered (in this or another tenant)."""


class UserNotFound(Exception):
    """A lookup expected a user but none matched."""


# ─────────────────────────────────────────────────────────────
# CREATE
# ─────────────────────────────────────────────────────────────

async def add_user(
    pool: AsyncConnectionPool,
    tenant_id: int,
    *,
    telegram_user_id: int,
    full_name: str,
    role: str = "pastor",
    telegram_username: Optional[str] = None,
    notes: Optional[str] = None,
) -> int:
    """Add a user to a tenant's whitelist. Returns the new id."""
    if role not in ("pastor", "admin"):
        raise ValueError(f"Invalid role: {role!r} (expected 'pastor' or 'admin')")
    if telegram_user_id <= 0:
        raise ValueError(f"Invalid telegram_user_id: {telegram_user_id}")
    if not full_name.strip():
        raise ValueError("full_name cannot be empty")

    sql = """
        INSERT INTO users (
            tenant_id, telegram_user_id, telegram_username, full_name, role, notes
        ) VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
    """
    try:
        async with tenant_cursor(pool, tenant_id) as cur:
            await cur.execute(sql, (
                tenant_id, telegram_user_id, telegram_username,
                full_name.strip(), role, notes,
            ))
            row = await cur.fetchone()
            if row is None:
                raise RuntimeError("INSERT RETURNING did not return an id")
            return int(row[0])
    except UniqueViolation as e:
        raise UserAlreadyExists(
            f"User with telegram_user_id={telegram_user_id} already exists"
        ) from e


# ─────────────────────────────────────────────────────────────
# READ
# ─────────────────────────────────────────────────────────────

async def get_by_telegram_id(
    pool: AsyncConnectionPool, tenant_id: int, telegram_user_id: int,
) -> Optional[dict[str, Any]]:
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM users WHERE telegram_user_id = %s", (telegram_user_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_by_id(
    pool: AsyncConnectionPool, tenant_id: int, user_id: int,
) -> Optional[dict[str, Any]]:
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def is_authorized(
    pool: AsyncConnectionPool, tenant_id: int, telegram_user_id: int,
) -> bool:
    """True if the user is on this tenant's active whitelist."""
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(
            "SELECT 1 FROM users WHERE telegram_user_id = %s AND is_active = TRUE LIMIT 1",
            (telegram_user_id,),
        )
        return await cur.fetchone() is not None


async def is_admin(
    pool: AsyncConnectionPool, tenant_id: int, telegram_user_id: int,
) -> bool:
    """True if the user is an active admin of this tenant."""
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(
            "SELECT 1 FROM users WHERE telegram_user_id = %s "
            "AND is_active = TRUE AND role = 'admin' LIMIT 1",
            (telegram_user_id,),
        )
        return await cur.fetchone() is not None


async def list_active(
    pool: AsyncConnectionPool, tenant_id: int, *, role: Optional[str] = None,
) -> list[dict[str, Any]]:
    if role is not None and role not in ("pastor", "admin"):
        raise ValueError(f"Invalid role: {role!r}")
    where = "WHERE is_active = TRUE" + (" AND role = %s" if role else "")
    params: tuple = (role,) if role else ()
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute(f"SELECT * FROM users {where} ORDER BY added_at ASC", params)
        return [dict(r) for r in await cur.fetchall()]


async def count_active(pool: AsyncConnectionPool, tenant_id: int) -> int:
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute("SELECT count(*) FROM users WHERE is_active = TRUE")
        row = await cur.fetchone()
        return int(row[0]) if row else 0


# ─────────────────────────────────────────────────────────────
# UPDATE
# ─────────────────────────────────────────────────────────────

async def deactivate(
    pool: AsyncConnectionPool, tenant_id: int, telegram_user_id: int,
) -> bool:
    """Soft-delete (is_active=FALSE). Returns True if a row was updated."""
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(
            "UPDATE users SET is_active = FALSE WHERE telegram_user_id = %s RETURNING 1",
            (telegram_user_id,),
        )
        return await cur.fetchone() is not None


async def reactivate(
    pool: AsyncConnectionPool, tenant_id: int, telegram_user_id: int,
) -> bool:
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(
            "UPDATE users SET is_active = TRUE WHERE telegram_user_id = %s RETURNING 1",
            (telegram_user_id,),
        )
        return await cur.fetchone() is not None


async def update_notes(
    pool: AsyncConnectionPool, tenant_id: int, telegram_user_id: int, notes: Optional[str],
) -> bool:
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(
            "UPDATE users SET notes = %s WHERE telegram_user_id = %s RETURNING 1",
            (notes, telegram_user_id),
        )
        return await cur.fetchone() is not None
