"""
Minutes — the protocol of a meeting, its agenda items and their rulings.

⚠️ THE ONLY DATA HERE THAT CANNOT BE REGENERATED. Every other repo in this
package holds something derived from the audio: lose it, re-run the pipeline,
get it back. A protocol holds an agenda entered before the meeting, a vote
count, the chair's edits, a person made responsible and a date agreed on — none
of which the recording contains. See migration 018.

TWO INVARIANTS LIVE IN THE DATABASE, NOT HERE, and that is deliberate:
  * an approved protocol cannot be edited (trigger, 018);
  * a protocol belongs to exactly one church (RLS, like every other table).
This module is the convenient way to reach them, never the thing that enforces
them — a route that forgets to call it still meets both.

ENCRYPTION IS THIS MODULE'S JOB, NOT ITS CALLERS'. Since 07.09 the sensitive
columns of an encrypted church (cloud_plan §3.1) are sealed on the way in and
opened on the way out, here — a route calls the same function it always did and
never holds a key. See the "Encryption" section below for what that costs.

A PROTOCOL MAY EXIST WITH NO MEETING FOLDER. The agenda is written before the
meeting; the audio lands hours after it ends. So the row is keyed on
(tenant, date) and is created first, standing alone until artifacts appear under
the same date. It is also the first real record of a meeting in this database —
until now a meeting was a folder plus an ingestion_jobs row.
"""

from __future__ import annotations

import json
from datetime import date as _date
from typing import Any, Optional

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from church_assistant.db.tenant_context import tenant_cursor
from church_assistant.shared import field_crypto, keyring


STATUSES = ("draft", "review", "approved")
ITEM_STATUSES = ("considered", "not_considered")


class ProtocolExists(Exception):
    """A protocol for that date already exists in this church."""


class ProtocolFrozen(Exception):
    """Approved: the database refused the change, and it was right to."""


def number(row: dict[str, Any]) -> str:
    """
    'ДД-ММ-РРРР/N' — the number as a council writes it.

    Built, never stored: only N is a fact of its own, and a stored number would
    be a second copy of the date, free to disagree with it after a correction.
    """
    d = row["meeting_date"]
    return f"{d:%d-%m-%Y}/{row['seq']}"


# ─────────────────────────────────────────────────────────────
# Encryption — cloud_plan §3.1, codec in shared/field_crypto
# ─────────────────────────────────────────────────────────────
#
# EVERY WRITE HERE SEALS AND EVERY READ OPENS, so no route has to remember to.
# A church at kek_version = 0 — which is every church today — pays one indexed
# lookup per call and takes the same path it always did.
#
# THE KEY IS FETCHED BEFORE THE WRITE TRANSACTION OPENS, never inside it:
# keyring borrows its own connection, and reaching for a second one while
# holding the first is how a small pool deadlocks under load.
#
# READS DEGRADE, WRITES REFUSE. A node with no key that reads gets None and
# then a NotDecryptable it can turn into "open this in the app" (that is the
# whole point of the marker). A node with no key that WRITES must not get None:
# seal() would pass the text through and quietly store plaintext in a church
# that asked for the opposite. So NoProcessorKey propagates on the write path.


def _scope(meeting_date: Any) -> str:
    """The DEK scope for a meeting — one key per meeting, cloud_plan §5.2.

    The same string the artifact side uses for the same meeting (the folder
    name, `artifact_sync`), so a protocol and its transcript share one key
    rather than accumulating two per meeting for no benefit.
    """
    return meeting_date.isoformat() if hasattr(meeting_date, "isoformat") else str(meeting_date)


async def _is_encrypted(pool: AsyncConnectionPool, tenant_id: int) -> bool:
    return await keyring.kek_version(pool, tenant_id) != 0


async def _key(
    pool: AsyncConnectionPool, tenant_id: int, meeting_date: Any, *, writing: bool,
) -> Optional[bytes]:
    try:
        return await keyring.dek_for(pool, tenant_id, _scope(meeting_date))
    except keyring.NoProcessorKey:
        if writing:
            raise
        return None


async def _date_of(
    pool: AsyncConnectionPool, tenant_id: int, sql: str, key_id: int,
) -> Optional[_date]:
    """The meeting a row belongs to — its DEK scope. One join, only when the
    church is encrypted; a plaintext church never reaches this."""
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(sql, (key_id,))
        row = await cur.fetchone()
        return row[0] if row else None


_DATE_OF_PROTOCOL = "SELECT meeting_date FROM meeting_protocols WHERE id = %s"
_DATE_OF_ITEM = (
    "SELECT p.meeting_date FROM protocol_items i "
    "  JOIN meeting_protocols p ON p.id = i.protocol_id WHERE i.id = %s"
)
_DATE_OF_RULING = (
    "SELECT p.meeting_date FROM protocol_rulings r "
    "  JOIN protocol_items i ON i.id = r.item_id "
    "  JOIN meeting_protocols p ON p.id = i.protocol_id WHERE r.id = %s"
)


async def _key_for(
    pool: AsyncConnectionPool, tenant_id: int, sql: str, key_id: int, *, writing: bool,
) -> Optional[bytes]:
    """The DEK for whatever meeting the row identified by `key_id` belongs to."""
    if not await _is_encrypted(pool, tenant_id):
        return None
    meeting_date = await _date_of(pool, tenant_id, sql, key_id)
    if meeting_date is None:
        return None                     # no such row; the caller will find out
    return await _key(pool, tenant_id, meeting_date, writing=writing)


# ─────────────────────────────────────────────────────────────
# The protocol itself
# ─────────────────────────────────────────────────────────────

async def create(
    pool: AsyncConnectionPool,
    tenant_id: int,
    *,
    meeting_date: _date,
    created_by: str,
    chair_id: Optional[int] = None,
    secretary: str = "",
) -> int:
    """
    Open a protocol for a date. Returns its id.

    N is chosen inside the INSERT rather than read first and written after: two
    admins opening the September meetings at the same moment would otherwise
    both read the same maximum. The unique index on (tenant, year, seq) is the
    backstop, and it is what turns a lost race into an error instead of two
    protocols numbered 5.

    TWO WRITES FOR AN ENCRYPTED CHURCH, one transaction. The AAD binds a value
    to its row id (field_crypto), and the id does not exist until the INSERT
    has run — so the row is written plaintext and immediately overwritten
    sealed, before anything commits. Nothing outside this transaction ever sees
    the first version: an interruption between the two rolls back both, and the
    protocol simply was not opened.
    """
    sql = """
        INSERT INTO meeting_protocols
               (tenant_id, meeting_date, seq, chair_id, secretary, created_by)
        SELECT %s, %s,
               COALESCE(MAX(seq), 0) + 1,
               %s, %s, %s
          FROM meeting_protocols
         WHERE tenant_id = %s
           AND protocol_year = EXTRACT(YEAR FROM %s::date)::int
        RETURNING id
    """
    from psycopg.errors import UniqueViolation
    dek = await _key(pool, tenant_id, meeting_date, writing=True)
    try:
        async with tenant_cursor(pool, tenant_id) as cur:
            await cur.execute(sql, (
                tenant_id, meeting_date, chair_id, secretary.strip(),
                created_by, tenant_id, meeting_date,
            ))
            row = await cur.fetchone()
            if row is None:
                raise RuntimeError("INSERT ... RETURNING did not return an id")
            protocol_id = int(row[0])
            if dek is not None:
                sealed = field_crypto.seal_row(
                    dek, tenant_id, "meeting_protocols",
                    {"id": protocol_id, "secretary": secretary.strip(),
                     "created_by": created_by},
                )
                await cur.execute(
                    "UPDATE meeting_protocols SET secretary = %s, created_by = %s "
                    " WHERE id = %s",
                    (sealed["secretary"], sealed["created_by"], protocol_id),
                )
            return protocol_id
    except UniqueViolation as e:
        raise ProtocolExists(
            f"protocol for {meeting_date} already exists"
        ) from e


async def get_by_date(
    pool: AsyncConnectionPool, tenant_id: int, meeting_date: str | _date,
) -> Optional[dict[str, Any]]:
    """The protocol for a date, or None. Joins the chair's name for display.

    Raises field_crypto.NotDecryptable for an encrypted church on a node with
    no key — the web turns that into "open this in the app" rather than
    rendering a page with `enc.v1.…` where the secretary's name belongs.
    """
    sql = """
        SELECT p.*, u.username AS chair_username, u.full_name AS chair_name
          FROM meeting_protocols p
     LEFT JOIN web_users u ON u.id = p.chair_id
         WHERE p.meeting_date = %s
    """
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute(sql, (meeting_date,))
        row = await cur.fetchone()
    if row is None:
        return None
    protocol = dict(row)
    dek = await _key(pool, tenant_id, protocol["meeting_date"], writing=False)
    return field_crypto.open_row(dek, tenant_id, "meeting_protocols", protocol)


async def meta_by_date(
    pool: AsyncConnectionPool, tenant_id: int, meeting_date: str | _date,
) -> Optional[dict[str, Any]]:
    """What can be said about a protocol WITHOUT a key: its number, its date,
    its status. Metadata only, same column list as list_all() and for the same
    reason.

    This is what the web falls back to when get_by_date() refuses to open. The
    difference matters on the page: "there is no protocol for this meeting" and
    "protocol № 3, which this server cannot read" are opposite statements, and
    the first one invites an admin to open a second protocol for a date that
    already has one.
    """
    for row in await list_all(pool, tenant_id):
        if str(row["meeting_date"]) == str(meeting_date):
            return row
    return None


async def list_all(
    pool: AsyncConnectionPool, tenant_id: int,
) -> list[dict[str, Any]]:
    """
    Every protocol in the church, newest first.

    The meetings list is built from folders on disk, and a meeting whose agenda
    exists but whose audio does not has no folder — so the page needs this to
    show it at all. A protocol is the earliest thing a meeting has.

    ⚠️ METADATA ONLY, and the column list is why. Everything here is open by
    design under cloud_plan §3.1 — dates, numbering, status — so this needs no
    key and works on a node that has none, which is what keeps the meetings
    list alive for an encrypted church. Selecting `*` would drag `secretary`,
    `quorum` and `attendees` through unopened and hand a template ciphertext to
    render. Anything that needs those calls get_by_date().
    """
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, tenant_id, meeting_date, seq, protocol_year, chair_id, "
            "       status, approved_at, approved_by, created_at, updated_at "
            "  FROM meeting_protocols ORDER BY meeting_date DESC"
        )
        return [dict(r) for r in await cur.fetchall()]


async def update_header(
    pool: AsyncConnectionPool,
    tenant_id: int,
    protocol_id: int,
    *,
    chair_id: Optional[int] = None,
    secretary: Optional[str] = None,
    attendees: Optional[list[str]] = None,
    quorum: Optional[str] = None,
) -> bool:
    """Change the parts above the agenda. None means 'leave this one alone'.

    One statement, unlike the inserts: the id is already known here, so the
    values can be sealed before the UPDATE rather than after it.
    """
    dek = await _key_for(pool, tenant_id, _DATE_OF_PROTOCOL, protocol_id, writing=True)

    def enc(column: str, value: str) -> str:
        return field_crypto.seal(
            dek, value, tenant_id, "meeting_protocols", column, protocol_id)

    sets, params = ["updated_at = NOW()"], []
    if chair_id is not None:
        sets.append("chair_id = %s"); params.append(chair_id)
    if secretary is not None:
        sets.append("secretary = %s"); params.append(enc("secretary", secretary.strip()))
    if attendees is not None:
        sets.append("attendees = %s::jsonb")
        params.append(json.dumps(
            field_crypto.seal_json(dek, attendees, tenant_id,
                                   "meeting_protocols", "attendees", protocol_id),
            ensure_ascii=False))
    if quorum is not None:
        sets.append("quorum = %s"); params.append(enc("quorum", quorum.strip()))
    params.append(protocol_id)

    return await _write(
        pool, tenant_id,
        f"UPDATE meeting_protocols SET {', '.join(sets)} WHERE id = %s",
        tuple(params),
    )


async def set_status(
    pool: AsyncConnectionPool, tenant_id: int, protocol_id: int,
    status: str, *, approved_by: Optional[int] = None,
) -> bool:
    """
    Move between draft / review / approved.

    Approving stamps who and when in the same statement that changes the status:
    a protocol that is approved but cannot say by whom is not much of a record,
    and two statements can be interrupted between them.
    """
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r} (expected {STATUSES})")
    if status == "approved":
        return await _write(
            pool, tenant_id,
            "UPDATE meeting_protocols "
            "   SET status = 'approved', approved_at = NOW(), approved_by = %s,"
            "       updated_at = NOW() "
            " WHERE id = %s",
            (approved_by, protocol_id),
        )
    return await _write(
        pool, tenant_id,
        "UPDATE meeting_protocols SET status = %s, updated_at = NOW() WHERE id = %s",
        (status, protocol_id),
    )


# ─────────────────────────────────────────────────────────────
# Agenda items
# ─────────────────────────────────────────────────────────────

async def list_items(
    pool: AsyncConnectionPool, tenant_id: int, protocol_id: int,
) -> list[dict[str, Any]]:
    """The agenda in order, each item carrying its rulings.

    One key for the whole agenda: items and rulings belong to the same meeting,
    and the DEK is per meeting.
    """
    dek = await _key_for(pool, tenant_id, _DATE_OF_PROTOCOL, protocol_id, writing=False)
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM protocol_items WHERE protocol_id = %s ORDER BY position",
            (protocol_id,),
        )
        items = [field_crypto.open_row(dek, tenant_id, "protocol_items", dict(r))
                 for r in await cur.fetchall()]
        if not items:
            return []
        await cur.execute(
            "SELECT * FROM protocol_rulings WHERE item_id = ANY(%s) "
            "ORDER BY item_id, position",
            ([int(i["id"]) for i in items],),
        )
        by_item: dict[int, list[dict[str, Any]]] = {}
        for r in await cur.fetchall():
            by_item.setdefault(int(r["item_id"]), []).append(
                field_crypto.open_row(dek, tenant_id, "protocol_rulings", dict(r)))
        for i in items:
            i["rulings"] = by_item.get(int(i["id"]), [])
        return items


async def add_item(
    pool: AsyncConnectionPool, tenant_id: int, protocol_id: int, question: str,
) -> int:
    """
    Append a question to the agenda. Returns its id.

    Position is chosen inside the INSERT for the same reason the protocol number
    is: two people typing at once would otherwise read the same maximum, and the
    unique index on (protocol_id, position) turns that into an error rather than
    two items claiming to be third.

    Sealed in a second statement inside the same transaction, for the reason
    create() spells out: the AAD needs the id.
    """
    question = question.strip()
    if not question:
        raise ValueError("питання не може бути порожнім")
    from psycopg.errors import RaiseException
    dek = await _key_for(pool, tenant_id, _DATE_OF_PROTOCOL, protocol_id, writing=True)
    try:
        async with tenant_cursor(pool, tenant_id) as cur:
            await cur.execute(
                "INSERT INTO protocol_items (tenant_id, protocol_id, position, question) "
                "SELECT %s, %s, COALESCE(MAX(position), 0) + 1, %s "
                "  FROM protocol_items WHERE protocol_id = %s "
                "RETURNING id",
                (tenant_id, protocol_id, question, protocol_id),
            )
            row = await cur.fetchone()
            if row is None:
                raise RuntimeError("INSERT ... RETURNING did not return an id")
            item_id = int(row[0])
            if dek is not None:
                await cur.execute(
                    "UPDATE protocol_items SET question = %s WHERE id = %s",
                    (field_crypto.seal(dek, question, tenant_id,
                                       "protocol_items", "question", item_id),
                     item_id),
                )
            return item_id
    except RaiseException as e:
        raise ProtocolFrozen(str(e).strip()) from e


async def get_item(
    pool: AsyncConnectionPool, tenant_id: int, item_id: int,
) -> Optional[dict[str, Any]]:
    dek = await _key_for(pool, tenant_id, _DATE_OF_ITEM, item_id, writing=False)
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM protocol_items WHERE id = %s", (item_id,))
        row = await cur.fetchone()
    if row is None:
        return None
    return field_crypto.open_row(dek, tenant_id, "protocol_items", dict(row))


async def update_item(
    pool: AsyncConnectionPool,
    tenant_id: int,
    item_id: int,
    *,
    question: Optional[str] = None,
    status: Optional[str] = None,
    heard: Optional[str] = None,
    resolved: Optional[str] = None,
) -> bool:
    """Change one item. None means 'leave this field alone'."""
    if status is not None and status not in ITEM_STATUSES:
        raise ValueError(f"unknown item status {status!r}")
    sets, params = [], []
    pairs = (("question", question), ("status", status),
             ("heard", heard), ("resolved", resolved))
    if not any(v is not None for _, v in pairs):
        return False
    # Only now — an update that changes nothing should not cost a key lookup.
    dek = await _key_for(pool, tenant_id, _DATE_OF_ITEM, item_id, writing=True)
    for col, val in pairs:
        if val is not None:
            sets.append(f"{col} = %s")
            params.append(val if col == "status" else field_crypto.seal(
                dek, val.strip(), tenant_id, "protocol_items", col, item_id))
    if not sets:
        return False
    params.append(item_id)
    return await _write(
        pool, tenant_id,
        f"UPDATE protocol_items SET {', '.join(sets)} WHERE id = %s",
        tuple(params),
    )


async def delete_item(
    pool: AsyncConnectionPool, tenant_id: int, item_id: int,
) -> bool:
    """
    Remove an item, and close the gap it leaves.

    ⚠️ THE FREEZE DOES NOT COVER THIS ONE. 018's trigger guards INSERT and
    UPDATE but deliberately not DELETE: a DELETE guard also fires on the cascade
    from tenants, which would make a church holding one approved protocol
    impossible to purge. The migration says the residual risk is a route that
    deletes from an approved protocol — so the check lives here, explicitly,
    rather than being assumed.

    Positions are closed up afterwards so the agenda reads 1, 2, 3 rather than
    1, 3, 4. Same transaction: an interrupted renumber would leave a gap that
    the next insert would then collide with.
    """
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT i.protocol_id, i.position, p.status "
            "  FROM protocol_items i JOIN meeting_protocols p ON p.id = i.protocol_id "
            " WHERE i.id = %s",
            (item_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return False
        if row["status"] == "approved":
            raise ProtocolFrozen(
                f"protocol {row['protocol_id']} is approved; items cannot be removed"
            )
        await cur.execute("DELETE FROM protocol_items WHERE id = %s", (item_id,))
        await cur.execute(
            "UPDATE protocol_items SET position = position - 1 "
            " WHERE protocol_id = %s AND position > %s",
            (row["protocol_id"], row["position"]),
        )
        return True


async def move_item(
    pool: AsyncConnectionPool, tenant_id: int, item_id: int, delta: int,
) -> bool:
    """
    Swap an item with its neighbour. delta is -1 (up) or +1 (down).

    Three statements, not one: (protocol_id, position) is UNIQUE and not
    deferrable, so writing both new positions directly collides with the
    constraint half way through. The vacated slot is parked on a negative
    number nobody else can hold, and the whole swap is one transaction — an
    interruption must not leave an item stranded at position -1.
    """
    if delta not in (-1, 1):
        raise ValueError("delta має бути -1 або 1")
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT i.id, i.protocol_id, i.position, p.status "
            "  FROM protocol_items i JOIN meeting_protocols p ON p.id = i.protocol_id "
            " WHERE i.id = %s",
            (item_id,),
        )
        me = await cur.fetchone()
        if me is None:
            return False
        if me["status"] == "approved":
            raise ProtocolFrozen("protocol is approved; the agenda cannot be reordered")

        await cur.execute(
            "SELECT id, position FROM protocol_items "
            " WHERE protocol_id = %s AND position = %s",
            (me["protocol_id"], me["position"] + delta),
        )
        other = await cur.fetchone()
        if other is None:
            return False            # already first or last — not an error

        await cur.execute(
            "UPDATE protocol_items SET position = -1 WHERE id = %s", (me["id"],))
        await cur.execute(
            "UPDATE protocol_items SET position = %s WHERE id = %s",
            (me["position"], other["id"]))
        await cur.execute(
            "UPDATE protocol_items SET position = %s WHERE id = %s",
            (other["position"], me["id"]))
        return True


# ─────────────────────────────────────────────────────────────
# Постановили — the rulings a question produced
# ─────────────────────────────────────────────────────────────

async def add_ruling(
    pool: AsyncConnectionPool,
    tenant_id: int,
    item_id: int,
    *,
    text: str,
    responsible: str = "",
    due: str = "",
) -> int:
    """Append a ruling to a question. Returns its id.

    Two statements in one transaction when the church is encrypted — see
    create() for why the id has to exist before anything can be sealed.
    """
    text = text.strip()
    if not text:
        raise ValueError("постанова не може бути порожньою")
    from psycopg.errors import RaiseException
    dek = await _key_for(pool, tenant_id, _DATE_OF_ITEM, item_id, writing=True)
    try:
        async with tenant_cursor(pool, tenant_id) as cur:
            await cur.execute(
                "INSERT INTO protocol_rulings "
                "       (tenant_id, item_id, position, text, responsible, due) "
                "SELECT %s, %s, COALESCE(MAX(position), 0) + 1, %s, %s, %s "
                "  FROM protocol_rulings WHERE item_id = %s "
                "RETURNING id",
                (tenant_id, item_id, text, responsible.strip(), due.strip(), item_id),
            )
            row = await cur.fetchone()
            if row is None:
                raise RuntimeError("INSERT ... RETURNING did not return an id")
            ruling_id = int(row[0])
            if dek is not None:
                sealed = field_crypto.seal_row(
                    dek, tenant_id, "protocol_rulings",
                    {"id": ruling_id, "text": text,
                     "responsible": responsible.strip(), "due": due.strip()},
                )
                await cur.execute(
                    "UPDATE protocol_rulings "
                    "   SET text = %s, responsible = %s, due = %s WHERE id = %s",
                    (sealed["text"], sealed["responsible"], sealed["due"], ruling_id),
                )
            return ruling_id
    except RaiseException as e:
        raise ProtocolFrozen(str(e).strip()) from e


async def get_ruling(
    pool: AsyncConnectionPool, tenant_id: int, ruling_id: int,
) -> Optional[dict[str, Any]]:
    """One ruling — so a caller can check which question it belongs to."""
    dek = await _key_for(pool, tenant_id, _DATE_OF_RULING, ruling_id, writing=False)
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM protocol_rulings WHERE id = %s", (ruling_id,))
        row = await cur.fetchone()
    if row is None:
        return None
    return field_crypto.open_row(dek, tenant_id, "protocol_rulings", dict(row))


async def update_ruling(
    pool: AsyncConnectionPool,
    tenant_id: int,
    ruling_id: int,
    *,
    text: Optional[str] = None,
    responsible: Optional[str] = None,
    due: Optional[str] = None,
) -> bool:
    """Edit one ruling. None means 'leave this field alone'."""
    pairs = (("text", text), ("responsible", responsible), ("due", due))
    if not any(v is not None for _, v in pairs):
        return False
    dek = await _key_for(pool, tenant_id, _DATE_OF_RULING, ruling_id, writing=True)
    sets, params = [], []
    for col, val in pairs:
        if val is not None:
            sets.append(f"{col} = %s")
            params.append(field_crypto.seal(
                dek, val.strip(), tenant_id, "protocol_rulings", col, ruling_id))
    if not sets:
        return False
    params.append(ruling_id)
    return await _write(
        pool, tenant_id,
        f"UPDATE protocol_rulings SET {', '.join(sets)} WHERE id = %s",
        tuple(params),
    )


async def delete_ruling(
    pool: AsyncConnectionPool, tenant_id: int, ruling_id: int,
) -> bool:
    """
    Remove a ruling and close the gap behind it.

    ⚠️ Checks the freeze here, like delete_item and for the same reason: 018's
    trigger covers INSERT and UPDATE but not DELETE, because a DELETE guard also
    fires on the cascade from tenants and would make a church holding one
    approved protocol impossible to purge.
    """
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT r.item_id, r.position, p.status "
            "  FROM protocol_rulings r "
            "  JOIN protocol_items i ON i.id = r.item_id "
            "  JOIN meeting_protocols p ON p.id = i.protocol_id "
            " WHERE r.id = %s",
            (ruling_id,),
        )
        row = await cur.fetchone()
        if row is None:
            return False
        if row["status"] == "approved":
            raise ProtocolFrozen("protocol is approved; rulings cannot be removed")
        await cur.execute("DELETE FROM protocol_rulings WHERE id = %s", (ruling_id,))
        await cur.execute(
            "UPDATE protocol_rulings SET position = position - 1 "
            " WHERE item_id = %s AND position > %s",
            (row["item_id"], row["position"]),
        )
        return True


async def set_votes(
    pool: AsyncConnectionPool,
    tenant_id: int,
    item_id: int,
    *,
    votes_for: Optional[int],
    votes_against: Optional[int],
    votes_abstain: Optional[int],
) -> bool:
    """
    Record the vote, or clear it.

    All three or none: "за 5" with the other two blank is not a tally, it is a
    half-written one, and a protocol that shows it invites the reader to assume
    the rest were zero. Passing None for all three clears the row back to
    "not voted on", which is the honest state for a question that was only
    discussed.
    """
    given = [v for v in (votes_for, votes_against, votes_abstain) if v is not None]
    if given and len(given) != 3:
        raise ValueError("голосування вводиться повністю: за, проти, утрималось")
    if any(v < 0 for v in given):
        raise ValueError("голосів не може бути менше нуля")
    return await _write(
        pool, tenant_id,
        "UPDATE protocol_items "
        "   SET votes_for = %s, votes_against = %s, votes_abstain = %s "
        " WHERE id = %s",
        (votes_for, votes_against, votes_abstain, item_id),
    )


async def _write(
    pool: AsyncConnectionPool, tenant_id: int, sql: str, params: tuple,
) -> bool:
    """
    Run one statement, turning the freeze trigger into something catchable.

    The trigger raises a bare RaiseException, which a route would otherwise
    surface as a 500 — an approved protocol refusing an edit is expected
    behaviour, not a server fault, and the person should be told so.
    """
    from psycopg.errors import RaiseException
    try:
        async with tenant_cursor(pool, tenant_id) as cur:
            await cur.execute(sql + " RETURNING 1", params)
            return await cur.fetchone() is not None
    except RaiseException as e:
        raise ProtocolFrozen(str(e).strip()) from e
