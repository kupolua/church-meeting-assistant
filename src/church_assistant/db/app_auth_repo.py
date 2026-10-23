"""
App sign-in repository — email codes and the accounts behind an address
(migration 021).

Two kinds of row live here and neither is tenant-scoped, which is unusual in
this package and is the point: a code is issued before anyone knows which
church the person belongs to, and the email → accounts lookup is the bootstrap
that finds out. Both therefore use a plain pooled connection, and the lookup is
a SECURITY DEFINER function, like login_tenants.

Codes follow the rules the rest of web auth lives by: the plaintext exists only
in the request that issued it and in the mailbox; the row holds a hash; a row
dies on use, on expiry or after a few wrong guesses, whichever comes first; and
verification is ONE statement, so two phones typing the same code cannot both
win.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


CODE_TTL_SECONDS = 10 * 60
MAX_ATTEMPTS = 5
# How many codes one address may ask for in the window before we stop sending.
MAX_CODES_PER_WINDOW = 5
WINDOW_SECONDS = 15 * 60

PURPOSES = ("signup", "login")


def new_code() -> str:
    """Six digits, leading zeros kept — '004213' is a valid code."""
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_code(email: str, code: str) -> str:
    """
    The address is folded in so that an identical code for two addresses hashes
    differently; a leaked table then yields nothing per row without the address.
    """
    return hashlib.sha256(f"{email.lower().strip()}|{code}".encode()).hexdigest()


def normalize_email(raw: str) -> str:
    return (raw or "").strip().lower()


def looks_like_email(email: str) -> bool:
    """Enough to refuse garbage; the code arriving is the real check."""
    if not email or len(email) > 254 or " " in email:
        return False
    local, sep, domain = email.rpartition("@")
    return bool(sep and local and "." in domain and not domain.startswith("."))


# ─────────────────────────────────────────────────────────────
# Codes
# ─────────────────────────────────────────────────────────────

async def recent_code_count(pool: AsyncConnectionPool, email: str) -> int:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM email_codes "
                "WHERE email = %s AND created_at > NOW() - make_interval(secs => %s)",
                (email, WINDOW_SECONDS),
            )
            row = await cur.fetchone()
            return int(row[0]) if row else 0


async def issue(
    pool: AsyncConnectionPool,
    *,
    email: str,
    purpose: str,
    payload: Optional[dict[str, Any]] = None,
    ip: Optional[str] = None,
) -> str:
    """
    Mint a code for an address. Returns the PLAINTEXT code — to be mailed, and
    then forgotten. Older live codes for the same address and purpose are
    retired first, so the person's mailbox holds exactly one that works.
    """
    if purpose not in PURPOSES:
        raise ValueError(f"Invalid purpose: {purpose!r}")
    code = new_code()
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=CODE_TTL_SECONDS)
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "UPDATE email_codes SET consumed_at = NOW() "
                "WHERE email = %s AND purpose = %s AND consumed_at IS NULL",
                (email, purpose),
            )
            await cur.execute(
                "INSERT INTO email_codes (email, purpose, code_hash, payload, expires_at, ip) "
                "VALUES (%s, %s, %s, %s::jsonb, %s, %s)",
                (email, purpose, hash_code(email, code),
                 json.dumps(payload or {}, ensure_ascii=False), expires_at, ip),
            )
    return code


async def verify(
    pool: AsyncConnectionPool, *, email: str, code: str, consume: bool = True,
) -> Optional[dict[str, Any]]:
    """
    Spend the live code for this address if `code` matches. Returns the row
    (with purpose and payload) on success, None otherwise.

    One transaction: the row is locked, compared in Python with a constant-time
    check, and either consumed or charged an attempt. After MAX_ATTEMPTS wrong
    guesses the row is dead even if the next guess would have been right —
    six digits are only safe because guessing is expensive in attempts, not in
    entropy.

    `consume=False` checks without spending: a right code is reported and left
    live, a wrong one is still charged. Needed when the server has to ask a
    question back ("which church?") before it can finish — the person's
    second answer must find the code still there. Found 23.10 the hard way:
    the first answer spent the code and the second was refused.
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT id, purpose, code_hash, payload, attempts FROM email_codes "
                "WHERE email = %s AND consumed_at IS NULL AND expires_at > NOW() "
                "ORDER BY created_at DESC LIMIT 1 FOR UPDATE",
                (email,),
            )
            row = await cur.fetchone()
            if row is None:
                return None
            ok = hmac.compare_digest(row["code_hash"], hash_code(email, code))
            if ok and row["attempts"] < MAX_ATTEMPTS:
                if consume:
                    await cur.execute(
                        "UPDATE email_codes SET consumed_at = NOW() WHERE id = %s", (row["id"],)
                    )
                return {"purpose": row["purpose"], "payload": row["payload"] or {}}
            # Wrong, or right but too late. Charge it; the last charge kills it.
            await cur.execute(
                "UPDATE email_codes SET attempts = attempts + 1, "
                "consumed_at = CASE WHEN attempts + 1 >= %s THEN NOW() END "
                "WHERE id = %s",
                (MAX_ATTEMPTS, row["id"]),
            )
            return None


async def purge_expired(pool: AsyncConnectionPool, *, older_than_days: int = 7) -> int:
    """Housekeeping: codes are evidence of nothing once dead."""
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM email_codes WHERE created_at < NOW() - make_interval(days => %s)",
                (older_than_days,),
            )
            return cur.rowcount


# ─────────────────────────────────────────────────────────────
# Accounts behind an address (bootstrap, bypasses RLS by design)
# ─────────────────────────────────────────────────────────────

async def accounts_by_email(pool: AsyncConnectionPool, email: str) -> list[dict[str, Any]]:
    """Every active account carrying this address, with its church's state."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM app_accounts_by_email(%s)", (email,))
            return [dict(r) for r in await cur.fetchall()]
