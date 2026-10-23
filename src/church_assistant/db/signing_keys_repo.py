"""
Signing keys repository — which phone may sign for which member (migration 022).

Tenant-scoped through tenant_cursor like every other per-church table. The
public key arrives from the phone as base64 and is stored as 32 raw bytes, so
a malformed registration fails at the CHECK rather than at the first signature.
"""

from __future__ import annotations

import base64
from typing import Any, Optional

from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from church_assistant.db.tenant_context import tenant_cursor


class BadPublicKey(ValueError):
    """Not 32 bytes of base64 — nothing Ed25519 could verify with."""


class KeyAlreadyRegistered(Exception):
    """This exact key is already on file in this church."""


def decode_public_key(b64: str) -> bytes:
    try:
        raw = base64.b64decode((b64 or "").strip(), validate=True)
    except Exception as e:  # noqa: BLE001
        raise BadPublicKey("public_key is not valid base64") from e
    if len(raw) != 32:
        raise BadPublicKey(f"public_key must be 32 bytes, got {len(raw)}")
    return raw


def fingerprint(public_key: bytes) -> str:
    """Eight hex pairs — enough for a person to match two screens by eye."""
    import hashlib
    return ":".join(hashlib.sha256(public_key).hexdigest()[i:i + 2] for i in range(0, 16, 2))


async def add(
    pool: AsyncConnectionPool, tenant_id: int, *,
    web_user_id: int, public_key: bytes, label: str = "",
) -> int:
    sql = """
        INSERT INTO signing_keys (tenant_id, web_user_id, public_key, label)
        VALUES (%s, %s, %s, %s) RETURNING id
    """
    try:
        async with tenant_cursor(pool, tenant_id) as cur:
            await cur.execute(sql, (tenant_id, web_user_id, public_key, (label or "")[:120]))
            row = await cur.fetchone()
            return int(row[0])
    except UniqueViolation as e:
        raise KeyAlreadyRegistered() from e


def _row(r: dict[str, Any]) -> dict[str, Any]:
    pk = bytes(r["public_key"])
    return {
        "id": int(r["id"]), "web_user_id": int(r["web_user_id"]),
        "algorithm": r["algorithm"], "label": r["label"],
        "public_key": base64.b64encode(pk).decode(), "fingerprint": fingerprint(pk),
        "created_at": r["created_at"], "last_used_at": r["last_used_at"],
        "revoked_at": r["revoked_at"],
    }


async def list_for_user(
    pool: AsyncConnectionPool, tenant_id: int, web_user_id: int, *, include_revoked: bool = False,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM signing_keys WHERE web_user_id = %s"
    if not include_revoked:
        sql += " AND revoked_at IS NULL"
    sql += " ORDER BY created_at DESC"
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute(sql, (web_user_id,))
        return [_row(dict(r)) for r in await cur.fetchall()]


async def live_counts(pool: AsyncConnectionPool, tenant_id: int) -> dict[int, int]:
    """{web_user_id: number of live keys} — for the members list."""
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(
            "SELECT web_user_id, count(*) FROM signing_keys "
            "WHERE revoked_at IS NULL GROUP BY web_user_id"
        )
        return {int(a): int(b) for a, b in await cur.fetchall()}


async def revoke(
    pool: AsyncConnectionPool, tenant_id: int, key_id: int, *, web_user_id: Optional[int] = None,
) -> bool:
    """
    Strike a key. With `web_user_id` only that person's key can be struck —
    the shape a member uses on their own phone. Without it, any key in the
    church — the admin's shape, for a lost phone.
    """
    sql = "UPDATE signing_keys SET revoked_at = NOW() WHERE id = %s AND revoked_at IS NULL"
    params: list[Any] = [key_id]
    if web_user_id is not None:
        sql += " AND web_user_id = %s"
        params.append(web_user_id)
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(sql + " RETURNING 1", params)
        return await cur.fetchone() is not None


async def revoke_all_for_user(pool: AsyncConnectionPool, tenant_id: int, web_user_id: int) -> int:
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(
            "UPDATE signing_keys SET revoked_at = NOW() "
            "WHERE web_user_id = %s AND revoked_at IS NULL", (web_user_id,),
        )
        return cur.rowcount
