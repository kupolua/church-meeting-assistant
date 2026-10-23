"""
«Стан справ» (migration 023): the rulings of approved protocols, as work.

materialize() is called from both places a protocol gets approved — the web
route and the app — and is idempotent on ruling_id, so calling it twice, or
on a protocol whose tasks already exist, adds nothing.
"""

from __future__ import annotations

from typing import Any, Optional

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from church_assistant.db.tenant_context import tenant_cursor


STATUSES = ("new", "in_progress", "done", "cancelled")


async def materialize(
    pool: AsyncConnectionPool, tenant_id: int, *, protocol_id: int,
    meeting_date: Any, items: list[dict[str, Any]],
) -> int:
    """One task per ruling that has none yet. Returns how many were created.

    Takes the OPENED items (protocols_repo.list_items) rather than reading the
    tables itself: for an encrypted church the ruling text in the table is a
    sealed blob, and the caller is the one holding the key."""
    rows = [
        (tenant_id, protocol_id, int(r["id"]), meeting_date,
         (r.get("text") or "").strip(), (r.get("responsible") or "").strip(), (r.get("due") or "").strip())
        for i in items for r in i.get("rulings", [])
        if (r.get("text") or "").strip()
    ]
    if not rows:
        return 0
    created = 0
    async with tenant_cursor(pool, tenant_id) as cur:
        for row in rows:
            await cur.execute(
                "INSERT INTO tasks (tenant_id, protocol_id, ruling_id, meeting_date, text, responsible, due) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (ruling_id) DO NOTHING",
                row,
            )
            created += cur.rowcount
    return created


async def list_all(
    pool: AsyncConnectionPool, tenant_id: int, *, include_closed: bool = True,
) -> list[dict[str, Any]]:
    sql = """
        SELECT t.*, u.full_name AS updated_by_name,
               p.seq, p.meeting_date AS protocol_date
          FROM tasks t
     LEFT JOIN web_users u ON u.id = t.updated_by
     LEFT JOIN meeting_protocols p ON p.id = t.protocol_id
    """
    if not include_closed:
        sql += " WHERE t.status IN ('new', 'in_progress')"
    sql += " ORDER BY CASE t.status WHEN 'in_progress' THEN 0 WHEN 'new' THEN 1 WHEN 'done' THEN 2 ELSE 3 END, t.meeting_date DESC, t.id"
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute(sql)
        return [dict(r) for r in await cur.fetchall()]


async def counts(pool: AsyncConnectionPool, tenant_id: int) -> dict[str, int]:
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute("SELECT status, count(*) FROM tasks GROUP BY status")
        out = {s: 0 for s in STATUSES}
        for s, n in await cur.fetchall():
            out[str(s)] = int(n)
        return out


async def set_status(
    pool: AsyncConnectionPool, tenant_id: int, task_id: int, *,
    status: str, by: int, note: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    sql = """
        UPDATE tasks
           SET status = %s, updated_at = NOW(), updated_by = %s,
               note = COALESCE(%s, note),
               done_at = CASE WHEN %s = 'done' THEN NOW() WHEN %s IN ('new','in_progress') THEN NULL ELSE done_at END
         WHERE id = %s
     RETURNING *
    """
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute(sql, (status, by, note, status, status, task_id))
        row = await cur.fetchone()
        return dict(row) if row else None
