"""
How the processing node reaches a church's keys — cloud_plan §5.1, tables 019.

The M1 is a recipient like any phone: `church_devices` has a row for it with
kind='processor', and `key_envelopes` holds the KEK sealed to its public key.
This module is the small amount of code that turns "a row in a table" into "the
bytes a pipeline stage can encrypt with".

WHY THE PROCESSOR HOLDS A KEY AT ALL, SAID PLAINLY. Diarization and Gemma work
on sound and text; neither runs over ciphertext. So the node that produces
artifacts must be able to read them, and it is inside the trust boundary by
construction — cloud_plan §5.0 point 3. The honest claim this supports is "the
VPS cannot read the minutes", never "nobody outside the church can".

NOT ENCRYPTED IS A REAL STATE, NOT AN ERROR. `tenants.kek_version = 0` is where
every existing church is and stays until someone deliberately migrates it, so
every function here returns None rather than raising. A caller that forgets to
handle None gets a TypeError immediately in tests, which is a better failure
than silently shipping plaintext because an exception was swallowed somewhere.

THE PRIVATE KEY COMES FROM THE ENVIRONMENT, in the same printed format the
recovery key uses (`crypto.recovery_from_text`). One encoding, one parser: a
second way to spell a key is a second way to get it subtly wrong.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from psycopg.rows import dict_row

from church_assistant.shared import crypto


ENV_PROCESSOR_KEY = "CMA_PROCESSOR_KEY"

# Secrets cached for the life of the process, keyed by what makes them distinct.
# A worker handles one church's meeting for minutes at a time and would
# otherwise re-read and re-unwrap for every file in a folder.
_KEK: dict[tuple[int, int], bytes] = {}
_DEK: dict[tuple[int, str, int], bytes] = {}


class NoProcessorKey(RuntimeError):
    """The node has no private key, so it cannot be a recipient.

    Distinct from "this church is not encrypted": that returns None. This is a
    misconfigured processing node, and continuing would mean writing plaintext
    for a church that asked for the opposite.
    """


def processor_private_key() -> bytes:
    raw = os.getenv(ENV_PROCESSOR_KEY, "").strip()
    if not raw:
        raise NoProcessorKey(
            f"{ENV_PROCESSOR_KEY} is not set — this node cannot open envelopes"
        )
    try:
        return crypto.recovery_from_text(raw)
    except ValueError as e:
        raise NoProcessorKey(f"{ENV_PROCESSOR_KEY} is not a readable key: {e}") from e


async def kek_version(pool: Any, tenant_id: int) -> int:
    """0 means this church stores plaintext. Read every time: a church can be
    switched on, or rotated, while a worker is running."""
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT kek_version FROM tenants WHERE id = %s", (tenant_id,))
        row = await cur.fetchone()
        return int(row["kek_version"]) if row else 0


async def kek_for(pool: Any, tenant_id: int) -> Optional[bytes]:
    """The church key, or None if this church is not encrypted."""
    version = await kek_version(pool, tenant_id)
    if version == 0:
        return None
    cached = _KEK.get((tenant_id, version))
    if cached is not None:
        return cached

    # The processor's own envelope, at the current version. A revoked processor
    # finds nothing here, which is the correct outcome: it should stop working,
    # not fall back to plaintext.
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT e.wrapped_kek FROM key_envelopes e "
            "JOIN church_devices d ON d.id = e.device_id "
            "WHERE d.tenant_id = %s AND d.kind = 'processor' "
            "  AND d.revoked_at IS NULL AND e.kek_version = %s "
            "LIMIT 1",
            (tenant_id, version),
        )
        row = await cur.fetchone()
    if row is None:
        raise NoProcessorKey(
            f"church {tenant_id} is at KEK v{version} but this node has no envelope "
            f"for it — pair the processor, or it will produce nothing readable"
        )

    kek = crypto.unwrap(processor_private_key(), bytes(row["wrapped_kek"]))
    _KEK[(tenant_id, version)] = kek
    return kek


def _dek_aad(tenant_id: int, scope: str) -> bytes:
    """Pins a wrapped DEK to its church and meeting. See crypto.context()."""
    return crypto.context(tenant_id, "dek", scope)


async def dek_for(pool: Any, tenant_id: int, scope: str) -> Optional[bytes]:
    """The data key for one meeting, creating it on first use.

    `scope` is the meeting date, or 'church' for what belongs to no meeting —
    one DEK per meeting, per cloud_plan §5.2.

    CREATE-OR-GET, NOT CREATE-THEN-GET. Two workers may reach the same new
    meeting at once; the unique index on (tenant, scope, version) decides, and
    the loser re-reads the winner's row instead of overwriting it. A second DEK
    for the same scope would not error anywhere — it would quietly make half the
    folder undecryptable.
    """
    kek = await kek_for(pool, tenant_id)
    if kek is None:
        return None
    version = await kek_version(pool, tenant_id)
    cached = _DEK.get((tenant_id, scope, version))
    if cached is not None:
        return cached

    aad = _dek_aad(tenant_id, scope)
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT wrapped_dek FROM dek_envelopes "
            "WHERE tenant_id = %s AND scope = %s AND kek_version = %s",
            (tenant_id, scope, version),
        )
        row = await cur.fetchone()
        if row is None:
            dek = crypto.new_key()
            await cur.execute(
                "INSERT INTO dek_envelopes (tenant_id, scope, kek_version, wrapped_dek) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (tenant_id, scope, kek_version) DO NOTHING",
                (tenant_id, scope, version, crypto.wrap_dek(kek, dek, aad)),
            )
            await cur.execute(
                "SELECT wrapped_dek FROM dek_envelopes "
                "WHERE tenant_id = %s AND scope = %s AND kek_version = %s",
                (tenant_id, scope, version),
            )
            row = await cur.fetchone()

    dek = crypto.unwrap_dek(kek, bytes(row["wrapped_dek"]), aad)
    _DEK[(tenant_id, scope, version)] = dek
    return dek


def forget(tenant_id: Optional[int] = None) -> None:
    """Drop cached secrets. Called after a rotation, and by tests between cases."""
    for store in (_KEK, _DEK):
        for key in [k for k in store if tenant_id is None or k[0] == tenant_id]:
            del store[key]
