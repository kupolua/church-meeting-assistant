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


async def issue(
    pool: AsyncConnectionPool,
    tenant_id: int,
    *,
    web_user_id: int,
    token_hash: str,
    created_by: str,
) -> int:
    """
    Replace whatever link an account had with this one. Returns the invite id.

    expire-then-create, in this order, is the invariant: an account must never
    hold two working links at once. The usual reason to issue a second is that
    the first went somewhere it should not have, and leaving it alive keeps open
    the exact door the re-issue was meant to close.

    It lives here rather than in the routes because there are now two callers
    with different authority — a church admin inside their own church, and the
    platform operator recovering one from outside — and the invariant belongs to
    invites, not to either caller. Two copies of it is one copy too many: the
    day one grows a condition, the other silently stops enforcing it.
    """
    await expire_pending_for_user(pool, tenant_id, web_user_id)
    return await create(
        pool,
        tenant_id,
        web_user_id=web_user_id,
        token_hash=token_hash,
        created_by=created_by,
    )


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


async def expire_pending_for_user(
    pool: AsyncConnectionPool, tenant_id: int, web_user_id: int,
) -> int:
    """
    Kill any live invite for this account. Returns how many were killed.

    Called before issuing a new one, so an account never has two working links
    at the same time. The old link was handed to somebody over some channel; if
    an admin re-issues because the first one went astray, leaving it alive keeps
    exactly the door they were trying to close.

    Expired rather than marked used: `used_at` means a person redeemed it, and
    the audit trail should not have to guess which of the two happened.
    """
    sql = """
        UPDATE web_invites SET expires_at = NOW()
        WHERE web_user_id = %s AND used_at IS NULL AND expires_at > NOW()
    """
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(sql, (web_user_id,))
        return cur.rowcount
