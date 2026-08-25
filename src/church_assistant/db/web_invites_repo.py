"""
Single-use invitation links.

An invite exists so that nobody — including whoever registered the church — ever
holds its first password. The account is created inactive with an unusable
password hash; the link lets exactly one person set a real one, once.

The two operations that happen without a session live in SECURITY DEFINER
functions (migration 011), for the same reason login does: redeeming happens
before anyone is authenticated, so there is no tenant context and RLS cannot be
satisfied. Everything else here runs under the ordinary policy.

Like web_sessions, this module never sees a token — only its hash. The token
itself exists once, in the response that created it.
"""

from __future__ import annotations

from typing import Any, Optional

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from church_assistant.db.tenant_context import tenant_cursor


# Long enough to survive a weekend and a missed message, short enough that a
# link forgotten in a chat is not a standing key to a church.
DEFAULT_TTL_SECONDS = 72 * 3600


async def create(
    pool: AsyncConnectionPool,
    tenant_id: int,
    *,
    web_user_id: int,
    token_hash: str,
    created_by: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> int:
    """Issue an invite for an account. Takes the token's HASH, never the token."""
    sql = """
        INSERT INTO web_invites (tenant_id, web_user_id, token_hash, created_by, expires_at)
        VALUES (%s, %s, %s, %s, NOW() + make_interval(secs => %s))
        RETURNING id
    """
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(sql, (tenant_id, web_user_id, token_hash, created_by, ttl_seconds))
        row = await cur.fetchone()
        if row is None:
            raise RuntimeError("INSERT ... RETURNING did not return an id")
        return int(row[0])


async def resolve(
    pool: AsyncConnectionPool, token_hash: str,
) -> Optional[dict[str, Any]]:
    """
    Who, if anyone, does this invite let in? None if it does not.

    Expired, already used, and belonging-to-a-suspended-church all come back as
    None — the caller cannot tell them apart, and neither can somebody guessing.
    """
    if not token_hash:
        return None
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM resolve_web_invite(%s)", (token_hash,)
            )
            return await cur.fetchone()


async def redeem(
    pool: AsyncConnectionPool, token_hash: str, password_hash: str,
) -> Optional[int]:
    """
    Spend the invite and set the password. Returns the web_user_id, or None.

    One database function, one transaction: marking the invite used, storing the
    password and activating the account have to happen together or a crash
    between them leaves an account nobody can reach and an invite nobody can
    spend again. None means the invite was already gone — including the case
    where a second, simultaneous redeem won the race.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT redeem_web_invite(%s, %s)", (token_hash, password_hash)
            )
            row = await cur.fetchone()
            return int(row[0]) if row and row[0] is not None else None


async def list_pending(
    pool: AsyncConnectionPool, tenant_id: int,
) -> list[dict[str, Any]]:
    """Unused, unexpired invites in this church — so an admin can see who owes a sign-in."""
    sql = """
        SELECT i.id, i.web_user_id, i.expires_at, i.created_at, i.created_by,
               u.username, u.full_name
        FROM web_invites i
        JOIN web_users u ON u.id = i.web_user_id
        WHERE i.used_at IS NULL AND i.expires_at > NOW()
        ORDER BY i.created_at DESC
    """
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute(sql)
        return list(await cur.fetchall())
