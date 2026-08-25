"""
Web accounts repository — the per-tenant web UI whitelist (MT Phase 3).

Tenant-aware exactly like users_repo (the Telegram whitelist): every op runs via
tenant_cursor(pool, tenant_id) → RLS scopes it to one church. Login resolves the
tenant FIRST (tenant_context.resolve_tenant_for_web_user, a SECURITY DEFINER
lookup) and then calls these with that tenant_id.

`username` is unique WITHIN a church (migration 014). It used to be unique
across the server, because the login form carries only a name and a password
and the name had to be what identified the church; since 014 login asks which
church when a name is shared, so churches no longer compete for names.
add_web_user therefore raises WebUserAlreadyExists only for a clash in the SAME
tenant — a name taken in another church is not this church's problem, and is
not visible to it either.

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
    """username already registered IN THIS tenant (migration 014)."""


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


async def list_all(
    pool: AsyncConnectionPool, tenant_id: int,
) -> list[dict[str, Any]]:
    """
    Every account, disabled ones included — the management view.

    Deactivation is a soft delete, so an admin has to be able to see and restore
    what they switched off; active first so the working set stays at the top.
    """
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM web_users ORDER BY is_active DESC, created_at ASC"
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


async def set_role(
    pool: AsyncConnectionPool, tenant_id: int, user_id: int, role: str,
) -> bool:
    """Promote/demote. Returns True if a row was updated."""
    if role not in ROLES:
        raise ValueError(f"Invalid role: {role!r} (expected one of {ROLES})")
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(
            "UPDATE web_users SET role = %s WHERE id = %s RETURNING 1",
            (role, user_id),
        )
        return await cur.fetchone() is not None


async def deactivate(
    pool: AsyncConnectionPool, tenant_id: int, user_id: int,
) -> tuple[bool, int]:
    """
    Soft-delete (is_active=FALSE). Returns (row updated, invites killed).

    Cutting access has to close every door at once, and an unspent invite is a
    door: redeeming one sets a password AND `is_active = TRUE`, so a link handed
    out an hour before the decision would walk the account straight back in.
    Harmless while the only invites were the ones minted at account creation;
    real since /admin/users started issuing them to accounts that already live.

    Both statements are one statement on purpose. Two calls in sequence leave a
    window — crash, disconnect, RLS refusal on the second — where the account is
    off and the link is still good, which is precisely the state this exists to
    prevent. `expires_at = NOW()` rather than `used_at`, because nobody redeemed
    it: the audit trail should not have to guess which of the two happened.
    """
    sql = """
        WITH switched_off AS (
            UPDATE web_users SET is_active = FALSE WHERE id = %s RETURNING id
        ), killed AS (
            UPDATE web_invites SET expires_at = NOW()
             WHERE web_user_id IN (SELECT id FROM switched_off)
               AND used_at IS NULL AND expires_at > NOW()
             RETURNING 1
        )
        SELECT (SELECT count(*) FROM switched_off), (SELECT count(*) FROM killed)
    """
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(sql, (user_id,))
        row = await cur.fetchone()
        n_users, n_invites = (int(row[0]), int(row[1])) if row else (0, 0)
        return n_users > 0, n_invites


async def reactivate(
    pool: AsyncConnectionPool, tenant_id: int, user_id: int,
) -> bool:
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(
            "UPDATE web_users SET is_active = TRUE WHERE id = %s RETURNING 1",
            (user_id,),
        )
        return await cur.fetchone() is not None
