"""
Protocol signatures (migration 023): who signed which text, verified.

Verification happens HERE, before the insert, with the public key the person
registered (022): a row in this table means the server checked the Ed25519
signature over the hash it stored beside it. A signature it could not verify
is never written, so a reader never has to.
"""

from __future__ import annotations

from typing import Any, Optional

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from church_assistant.db.tenant_context import tenant_cursor


class BadSignature(Exception):
    """The bytes do not verify under this key for this hash."""


class AlreadySigned(Exception):
    """This person already signed this exact text."""


def verify(public_key: bytes, content_hash_hex: str, signature: bytes) -> None:
    try:
        VerifyKey(public_key).verify(bytes.fromhex(content_hash_hex), signature)
    except (BadSignatureError, ValueError) as e:
        raise BadSignature() from e


async def add(
    pool: AsyncConnectionPool, tenant_id: int, *,
    protocol_id: int, web_user_id: int, key_id: int, content_hash: str, signature: bytes,
) -> int:
    sql = """
        INSERT INTO protocol_signatures
            (tenant_id, protocol_id, web_user_id, key_id, content_hash, signature)
        VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
    """
    try:
        async with tenant_cursor(pool, tenant_id) as cur:
            await cur.execute(sql, (tenant_id, protocol_id, web_user_id, key_id, content_hash, signature))
            row = await cur.fetchone()
            await cur.execute(
                "UPDATE signing_keys SET last_used_at = NOW() WHERE id = %s", (key_id,)
            )
            return int(row[0]) if row else 0
    except UniqueViolation as e:
        raise AlreadySigned() from e


async def list_for_protocol(
    pool: AsyncConnectionPool, tenant_id: int, protocol_id: int,
) -> list[dict[str, Any]]:
    """Every signature on this protocol, newest first, with the signer and key."""
    sql = """
        SELECT s.id, s.web_user_id, s.key_id, s.content_hash, s.signed_at,
               u.full_name, k.label AS key_label
          FROM protocol_signatures s
          JOIN web_users u ON u.id = s.web_user_id
          JOIN signing_keys k ON k.id = s.key_id
         WHERE s.protocol_id = %s
         ORDER BY s.signed_at DESC
    """
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute(sql, (protocol_id,))
        return [dict(r) for r in await cur.fetchall()]


async def count_for_protocols(
    pool: AsyncConnectionPool, tenant_id: int, hashes: dict[int, str],
) -> dict[int, int]:
    """{protocol_id: signatures on its CURRENT hash} for a set of protocols."""
    if not hashes:
        return {}
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(
            "SELECT protocol_id, content_hash, count(DISTINCT web_user_id) "
            "  FROM protocol_signatures WHERE protocol_id = ANY(%s) "
            " GROUP BY protocol_id, content_hash",
            (list(hashes.keys()),),
        )
        out: dict[int, int] = {}
        for pid, h, n in await cur.fetchall():
            if hashes.get(int(pid)) == h:
                out[int(pid)] = int(n)
        return out
