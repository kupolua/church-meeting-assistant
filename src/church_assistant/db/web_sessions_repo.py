"""
Web sessions repository — server-side, revocable sessions (migration 008).

The cookie carries an opaque token; this table decides whether it still means
anything. That inversion is the whole point: with a self-contained cookie,
"remove this person's access" could only be answered by rotating the signing
key and removing everyone's.

Two access patterns, and they need different plumbing:

    resolve()  — the hot path, once per request. It runs BEFORE we know the
                 tenant (the cookie is all we have), so it goes through the
                 SECURITY DEFINER resolver, exactly like the login and the bot's
                 whitelist lookup. That function is also the single definition
                 of "still valid" — revocation, expiry, account active, church
                 active — so nothing here re-implements those rules.

    everything else — ordinary tenant-scoped work through tenant_cursor(), by an
                 admin who is already inside a church's context.

Sessions are revoked (revoked_at set), never deleted on sign-out: "this session
existed and ended at this time" is exactly the kind of thing the наглядова рада
is there to be able to check. purge_expired() clears rows only long after they
stopped being usable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from church_assistant.db.tenant_context import tenant_cursor


# How stale last_seen_at may get before a request bothers to update it. Without
# a throttle every page view (and every 5 s dashboard poll) would be a write for
# a column nobody reads in real time.
TOUCH_INTERVAL_SECONDS = 60


# ─────────────────────────────────────────────────────────────
# CREATE
# ─────────────────────────────────────────────────────────────

async def create(
    pool: AsyncConnectionPool,
    tenant_id: int,
    *,
    web_user_id: int,
    token_hash: str,
    ttl_seconds: int,
    user_agent: Optional[str] = None,
    ip: Optional[str] = None,
) -> int:
    """
    Open a session for a signed-in user. Returns the new session id.

    Takes the token's HASH, never the token: the plaintext exists only in the
    login handler and the browser, and this module has no reason to see it.
    `user_agent` / `ip` are recorded so an admin looking at "3 active sessions"
    can tell whether that is expected.
    """
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    sql = """
        INSERT INTO web_sessions (
            tenant_id, web_user_id, token_hash, expires_at, user_agent, ip
        ) VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id
    """
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(sql, (
            tenant_id, web_user_id, token_hash, expires_at,
            (user_agent or "")[:400] or None, ip,
        ))
        row = await cur.fetchone()
        if row is None:
            raise RuntimeError("INSERT RETURNING did not return an id")
        return int(row[0])


# ─────────────────────────────────────────────────────────────
# RESOLVE (hot path)
# ─────────────────────────────────────────────────────────────

async def resolve(
    pool: AsyncConnectionPool, token_hash: str,
) -> Optional[dict[str, Any]]:
    """
    Who, if anyone, does this token authorize right now? None if it doesn't.

    None covers every reason at once — unknown, revoked, expired, account
    disabled, church suspended — and deliberately does not distinguish them: the
    caller's response is the same in all cases, and the difference is not the
    browser's business.

    The returned role and full_name are read live, so a demotion or a rename
    takes effect on the next request rather than at the next sign-in.
    """
    if not token_hash:
        return None
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM resolve_web_session(%s)", (token_hash,)
            )
            row = await cur.fetchone()
            return dict(row) if row else None


async def touch(
    pool: AsyncConnectionPool, tenant_id: int, session_id: int,
) -> None:
    """
    Record that the session was used just now (throttled, best-effort).

    The WHERE clause does the throttling, so this stays one statement with no
    read first — and a no-op update costs nothing. Never raises: failing to
    refresh a display timestamp must not break the request that succeeded.
    """
    try:
        async with tenant_cursor(pool, tenant_id) as cur:
            await cur.execute(
                "UPDATE web_sessions SET last_seen_at = NOW() "
                "WHERE id = %s AND last_seen_at < NOW() - make_interval(secs => %s)",
                (session_id, TOUCH_INTERVAL_SECONDS),
            )
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# REVOKE
# ─────────────────────────────────────────────────────────────

async def revoke(
    pool: AsyncConnectionPool, tenant_id: int, session_id: int,
) -> bool:
    """End one session (sign-out). Returns True if it was live until now."""
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(
            "UPDATE web_sessions SET revoked_at = NOW() "
            "WHERE id = %s AND revoked_at IS NULL RETURNING 1",
            (session_id,),
        )
        return await cur.fetchone() is not None


async def revoke_all_for_user(
    pool: AsyncConnectionPool,
    tenant_id: int,
    web_user_id: int,
    *,
    except_session_id: Optional[int] = None,
) -> int:
    """
    End every live session of one account. Returns how many were live.

    This is what "sign out everywhere", account deactivation and password resets
    all call — the single action that used to be impossible without rotating the
    signing key and taking every other church down with it.

    `except_session_id` spares the caller's own session: an admin changing their
    own password should not be thrown out of the page they did it on, while
    every other device holding that account still is.
    """
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(
            "UPDATE web_sessions SET revoked_at = NOW() "
            "WHERE web_user_id = %s AND revoked_at IS NULL AND expires_at > NOW() "
            "  AND (%s::bigint IS NULL OR id <> %s::bigint)",
            (web_user_id, except_session_id, except_session_id),
        )
        return cur.rowcount


# ─────────────────────────────────────────────────────────────
# READ (admin views)
# ─────────────────────────────────────────────────────────────

async def count_live_by_user(
    pool: AsyncConnectionPool, tenant_id: int,
) -> dict[int, int]:
    """{web_user_id: live session count} for this church — one query for the list."""
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(
            "SELECT web_user_id, count(*) FROM web_sessions "
            "WHERE revoked_at IS NULL AND expires_at > NOW() "
            "GROUP BY web_user_id"
        )
        return {int(uid): int(n) for uid, n in await cur.fetchall()}


async def list_for_user(
    pool: AsyncConnectionPool, tenant_id: int, web_user_id: int,
    *, limit: int = 20,
) -> list[dict[str, Any]]:
    """Recent sessions of one account, live ones first (for the board / an admin)."""
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM web_sessions WHERE web_user_id = %s "
            "ORDER BY (revoked_at IS NULL AND expires_at > NOW()) DESC, "
            "         last_seen_at DESC LIMIT %s",
            (web_user_id, limit),
        )
        return [dict(r) for r in await cur.fetchall()]


# ─────────────────────────────────────────────────────────────
# HOUSEKEEPING
# ─────────────────────────────────────────────────────────────

async def purge_expired(
    pool: AsyncConnectionPool, *, keep_days: int = 30,
) -> int:
    """
    Delete rows that expired more than `keep_days` ago. Returns the count.

    Cross-tenant (SECURITY DEFINER): no single church's context can see the
    whole table. Called opportunistically at login rather than per request —
    it's cleanup, not correctness.
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT purge_expired_web_sessions(%s)", (int(keep_days),)
            )
            row = await cur.fetchone()
            return int(row[0]) if row else 0
