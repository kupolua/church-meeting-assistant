"""
Web accounts repository — the per-tenant web UI whitelist (MT Phase 3).

Tenant-aware exactly like users_repo (the Telegram whitelist): every op runs via
tenant_cursor(pool, tenant_id) → RLS scopes it to one church. Login resolves the
tenant FIRST (tenant_context.resolve_tenant_for_web_user, a SECURITY DEFINER
lookup) and then calls these with that tenant_id.

`username` stays globally UNIQUE: a person belongs to exactly one church — that's
what makes login-time routing unambiguous. add_web_user therefore raises
WebUserAlreadyExists even when the clashing row lives in another tenant
(invisible under RLS, but the unique index is global).

Password hashing/verification lives in web/security.py — this module only stores
and returns the opaque hash string.

Soft delete only (is_active=false) — never DELETE rows.
"""

from __future__ import annotations

from typing import Any, Optional

from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from church_assistant.db.tenant_context import tenant_cursor


ROLES = ("member", "admin")


class WebUserAlreadyExists(Exception):
    """username already registered (in this or another tenant)."""


# ─────────────────────────────────────────────────────────────
# CREATE
# ─────────────────────────────────────────────────────────────

async def add_web_user(
    pool: AsyncConnectionPool,
    tenant_id: int,
    *,
    username: str,
    password_hash: str,
    full_name: str,
    role: str = "member",
    notes: Optional[str] = None,
) -> int:
    """Add a web account to a tenant. Returns the new id."""
    username = username.strip().lower()
    if role not in ROLES:
        raise ValueError(f"Invalid role: {role!r} (expected one of {ROLES})")
    if not username:
        raise ValueError("username cannot be empty")
    if not password_hash:
        raise ValueError("password_hash cannot be empty")
    if not full_name.strip():
        raise ValueError("full_name cannot be empty")

    sql = """
        INSERT INTO web_users (
            tenant_id, username, password_hash, full_name, role, notes
        ) VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
    """
    try:
        async with tenant_cursor(pool, tenant_id) as cur:
            await cur.execute(sql, (
                tenant_id, username, password_hash, full_name.strip(), role, notes,
            ))
            row = await cur.fetchone()
            if row is None:
                raise RuntimeError("INSERT RETURNING did not return an id")
            return int(row[0])
    except UniqueViolation as e:
        raise WebUserAlreadyExists(
            f"Web user {username!r} already exists"
        ) from e


# ─────────────────────────────────────────────────────────────
# READ
# ─────────────────────────────────────────────────────────────

async def get_by_username(
    pool: AsyncConnectionPool, tenant_id: int, username: str,
) -> Optional[dict[str, Any]]:
    """The account row (incl. password_hash) — for login verification."""
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM web_users WHERE username = %s", (username.strip().lower(),)
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_by_id(
    pool: AsyncConnectionPool, tenant_id: int, user_id: int,
) -> Optional[dict[str, Any]]:
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM web_users WHERE id = %s", (user_id,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def list_active(
    pool: AsyncConnectionPool, tenant_id: int,
) -> list[dict[str, Any]]:
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM web_users WHERE is_active = TRUE ORDER BY created_at ASC"
        )
        return [dict(r) for r in await cur.fetchall()]


async def count_active(pool: AsyncConnectionPool, tenant_id: int) -> int:
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute("SELECT count(*) FROM web_users WHERE is_active = TRUE")
        row = await cur.fetchone()
        return int(row[0]) if row else 0


# ─────────────────────────────────────────────────────────────
# UPDATE
# ─────────────────────────────────────────────────────────────

async def touch_login(
    pool: AsyncConnectionPool, tenant_id: int, user_id: int,
) -> None:
    """Record a successful login (last_login_at = now)."""
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(
            "UPDATE web_users SET last_login_at = NOW() WHERE id = %s", (user_id,)
        )


async def set_password_hash(
    pool: AsyncConnectionPool, tenant_id: int, user_id: int, password_hash: str,
) -> bool:
    """Replace the stored hash. Returns True if a row was updated."""
    if not password_hash:
        raise ValueError("password_hash cannot be empty")
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(
            "UPDATE web_users SET password_hash = %s WHERE id = %s RETURNING 1",
            (password_hash, user_id),
        )
        return await cur.fetchone() is not None


async def deactivate(
    pool: AsyncConnectionPool, tenant_id: int, user_id: int,
) -> bool:
    """Soft-delete (is_active=FALSE). Returns True if a row was updated."""
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(
            "UPDATE web_users SET is_active = FALSE WHERE id = %s RETURNING 1",
            (user_id,),
        )
        return await cur.fetchone() is not None


async def reactivate(
    pool: AsyncConnectionPool, tenant_id: int, user_id: int,
) -> bool:
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(
            "UPDATE web_users SET is_active = TRUE WHERE id = %s RETURNING 1",
            (user_id,),
        )
        return await cur.fetchone() is not None
