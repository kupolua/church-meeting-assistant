"""
topic_corrections (migration 026): «the model wrote → the council says».

One row per changed field per save in the app. Read back by whoever builds
prompt examples or a fine-tuning set; nothing in the request path reads it.
"""

from __future__ import annotations

from typing import Any, Optional

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from church_assistant.db.tenant_context import tenant_cursor


FIELDS = ("title", "body")


async def record(
    pool: AsyncConnectionPool, tenant_id: int, *, meeting_date: str, topic_order: int,
    field: str, before: str, after: str, user_id: Optional[int],
) -> Optional[int]:
    """Keep one pair. An unchanged text is not a correction and is skipped."""
    if field not in FIELDS or before == after:
        return None
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(
            "INSERT INTO topic_corrections (tenant_id, meeting_date, topic_order, field, before, after, web_user_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (tenant_id, meeting_date, topic_order, field, before, after, user_id),
        )
        row = await cur.fetchone()
    return int(row[0]) if row else None


async def list_recent(pool: AsyncConnectionPool, tenant_id: int, *, limit: int = 200) -> list[dict[str, Any]]:
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT id, meeting_date::text AS meeting_date, topic_order, field, before, after, web_user_id, created_at "
            "FROM topic_corrections ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def count(pool: AsyncConnectionPool, tenant_id: int) -> int:
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute("SELECT count(*) FROM topic_corrections")
        row = await cur.fetchone()
    return int(row[0]) if row else 0
