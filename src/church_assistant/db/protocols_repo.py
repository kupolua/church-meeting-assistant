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
    try:
        async with tenant_cursor(pool, tenant_id) as cur:
            await cur.execute(sql, (
                tenant_id, meeting_date, chair_id, secretary.strip(),
                created_by, tenant_id, meeting_date,
            ))
            row = await cur.fetchone()
            if row is None:
                raise RuntimeError("INSERT ... RETURNING did not return an id")
            return int(row[0])
    except UniqueViolation as e:
        raise ProtocolExists(
            f"protocol for {meeting_date} already exists"
        ) from e


async def get_by_date(
    pool: AsyncConnectionPool, tenant_id: int, meeting_date: str | _date,
) -> Optional[dict[str, Any]]:
    """The protocol for a date, or None. Joins the chair's name for display."""
    sql = """
        SELECT p.*, u.username AS chair_username, u.full_name AS chair_name
          FROM meeting_protocols p
     LEFT JOIN web_users u ON u.id = p.chair_id
         WHERE p.meeting_date = %s
    """
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute(sql, (meeting_date,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def list_all(
    pool: AsyncConnectionPool, tenant_id: int,
) -> list[dict[str, Any]]:
    """
    Every protocol in the church, newest first.

    The meetings list is built from folders on disk, and a meeting whose agenda
    exists but whose audio does not has no folder — so the page needs this to
    show it at all. A protocol is the earliest thing a meeting has.
    """
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM meeting_protocols ORDER BY meeting_date DESC"
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
    """Change the parts above the agenda. None means 'leave this one alone'."""
    sets, params = ["updated_at = NOW()"], []
    if chair_id is not None:
        sets.append("chair_id = %s"); params.append(chair_id)
    if secretary is not None:
        sets.append("secretary = %s"); params.append(secretary.strip())
    if attendees is not None:
        sets.append("attendees = %s::jsonb")
        params.append(json.dumps(attendees, ensure_ascii=False))
    if quorum is not None:
        sets.append("quorum = %s"); params.append(quorum.strip())
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
