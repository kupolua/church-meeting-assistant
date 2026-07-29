"""
Queries repository — CRUD for the `queries` table (tenant-aware, MT Phase 1).

Every tenant-scoped operation runs inside tenant_context.tenant_cursor(pool,
tenant_id): Postgres RLS then guarantees the caller only ever sees/writes that
tenant's rows (a forgotten filter can't leak another church).

The queue fetch is the exception: the shared query-worker must scan across ALL
tenants, so fetch_next_pending() calls the SECURITY DEFINER claim_next_query()
(bypasses RLS) and returns the claimed row WITH its tenant_id — the worker then
processes inside that tenant's context.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from church_assistant.db.tenant_context import tenant_cursor


# ─────────────────────────────────────────────────────────────
# CREATE
# ─────────────────────────────────────────────────────────────

async def insert_pending(
    pool: AsyncConnectionPool,
    tenant_id: int,
    *,
    source: str,                          # 'web' | 'telegram'
    question: str,
    user_id: Optional[int] = None,
    telegram_chat_id: Optional[int] = None,
    telegram_message_id: Optional[int] = None,
    collection: str = "protocols",
    verbose_mode: bool = False,
) -> int:
    """Insert a new query (status='pending') for a tenant. Returns the new id."""
    if source not in ("web", "telegram"):
        raise ValueError(f"Invalid source: {source!r}")
    if collection not in ("protocols", "analyses", "turns", "protocol_full"):
        raise ValueError(f"Invalid collection: {collection!r}")
    if source == "telegram" and (user_id is None or telegram_chat_id is None):
        raise ValueError("telegram source requires user_id and telegram_chat_id")

    sql = """
        INSERT INTO queries (
            tenant_id, source, user_id, telegram_chat_id, telegram_message_id,
            question, collection, verbose_mode, status
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, 'pending'
        )
        RETURNING id
    """
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(sql, (
            tenant_id, source, user_id, telegram_chat_id, telegram_message_id,
            question, collection, verbose_mode,
        ))
        row = await cur.fetchone()
        if row is None:
            raise RuntimeError("INSERT ... RETURNING did not return an id")
        return int(row[0])


# ─────────────────────────────────────────────────────────────
# READ
# ─────────────────────────────────────────────────────────────

async def get_by_id(
    pool: AsyncConnectionPool, tenant_id: int, query_id: int,
) -> Optional[dict[str, Any]]:
    """Load one query by id (only if it belongs to this tenant)."""
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM queries WHERE id = %s", (query_id,))
        return _normalize_row(await cur.fetchone())


async def list_recent(
    pool: AsyncConnectionPool,
    tenant_id: int,
    *,
    limit: int = 50,
    offset: int = 0,
    source: Optional[str] = None,
    status: Optional[str] = None,
    user_id: Optional[int] = None,
) -> list[dict[str, Any]]:
    """List this tenant's queries, newest first."""
    where: list[str] = []
    params: list[Any] = []
    if source is not None:
        where.append("source = %s"); params.append(source)
    if status is not None:
        where.append("status = %s"); params.append(status)
    if user_id is not None:
        where.append("user_id = %s"); params.append(user_id)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    sql = f"""
        SELECT * FROM queries
        {where_sql}
        ORDER BY asked_at DESC
        LIMIT %s OFFSET %s
    """
    params.extend([limit, offset])
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute(sql, params)
        return [_normalize_row(r) for r in await cur.fetchall() if r is not None]  # type: ignore[misc]


async def get_last_completed_for_telegram(
    pool: AsyncConnectionPool, tenant_id: int, telegram_chat_id: int,
) -> Optional[dict[str, Any]]:
    """Most recent completed telegram query for a chat (for /verbose)."""
    sql = """
        SELECT * FROM queries
        WHERE telegram_chat_id = %s
          AND source = 'telegram'
          AND status = 'completed'
        ORDER BY completed_at DESC
        LIMIT 1
    """
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute(sql, (telegram_chat_id,))
        return _normalize_row(await cur.fetchone())


# ─────────────────────────────────────────────────────────────
# WORKER: claim next pending (across all tenants — bypasses RLS)
# ─────────────────────────────────────────────────────────────

async def fetch_next_pending(
    pool: AsyncConnectionPool,
) -> Optional[dict[str, Any]]:
    """
    Atomically claim the next pending query across ALL tenants and mark it
    'processing'. Returns the row (including tenant_id) or None if the queue is
    empty. The caller processes it inside tenant_cursor(pool, row['tenant_id']).
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM claim_next_query()")
            row = await cur.fetchone()
            # A composite-returning function yields one all-NULL row when empty.
            if row is None or row.get("id") is None:
                return None
            return _normalize_row(row)


# ─────────────────────────────────────────────────────────────
# UPDATE (tenant-scoped)
# ─────────────────────────────────────────────────────────────

async def mark_completed(
    pool: AsyncConnectionPool,
    tenant_id: int,
    query_id: int,
    *,
    hits: list[dict[str, Any]],
    synthesis: str,
    sources: list[str],
    embed_time_ms: Optional[int] = None,
    qdrant_time_ms: Optional[int] = None,
    rerank_time_ms: Optional[int] = None,
    gemma_time_ms: Optional[int] = None,
    total_time_ms: Optional[int] = None,
) -> None:
    sql = """
        UPDATE queries
        SET status = 'completed', completed_at = NOW(),
            hits = %s::jsonb, synthesis = %s, sources = %s,
            embed_time_ms = %s, qdrant_time_ms = %s, rerank_time_ms = %s,
            gemma_time_ms = %s, total_time_ms = %s
        WHERE id = %s
    """
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(sql, (
            json.dumps(hits, ensure_ascii=False), synthesis, sources,
            embed_time_ms, qdrant_time_ms, rerank_time_ms,
            gemma_time_ms, total_time_ms, query_id,
        ))


async def mark_failed(
    pool: AsyncConnectionPool,
    tenant_id: int,
    query_id: int,
    *,
    error_message: str,
    error_traceback: str,
    increment_retry: bool = True,
) -> int:
    """Mark failed; return the new retry_count."""
    retry_sql = ", retry_count = retry_count + 1" if increment_retry else ""
    sql = f"""
        UPDATE queries
        SET status = 'failed', completed_at = NOW(),
            error_message = %s, error_traceback = %s{retry_sql}
        WHERE id = %s
        RETURNING retry_count
    """
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(sql, (error_message, error_traceback, query_id))
        row = await cur.fetchone()
        return int(row[0]) if row else 0


async def requeue_for_retry(
    pool: AsyncConnectionPool, tenant_id: int, query_id: int,
) -> None:
    sql = """
        UPDATE queries
        SET status = 'pending', started_at = NULL, completed_at = NULL,
            error_message = NULL, error_traceback = NULL
        WHERE id = %s
    """
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(sql, (query_id,))


async def cancel(pool: AsyncConnectionPool, tenant_id: int, query_id: int) -> None:
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(
            "UPDATE queries SET status='cancelled', completed_at=NOW() WHERE id=%s",
            (query_id,),
        )


# ─────────────────────────────────────────────────────────────
# Aggregations (per-tenant — RLS scopes the views automatically)
# ─────────────────────────────────────────────────────────────

async def get_queue_depth(pool: AsyncConnectionPool, tenant_id: int) -> dict[str, int]:
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM v_queue_depth")
        row = await cur.fetchone()
        if row is None:
            return {"pending": 0, "processing": 0, "failed": 0}
        return {k: int(row.get(k) or 0) for k in ("pending", "processing", "failed")}


async def get_stats_today(pool: AsyncConnectionPool, tenant_id: int) -> dict[str, Any]:
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM v_stats_today")
        row = await cur.fetchone()
        if row is None:
            return {"total": 0, "completed": 0, "failed": 0,
                    "from_web": 0, "from_telegram": 0, "avg_time_ms": None}
        return {
            "total": int(row["total"] or 0),
            "completed": int(row["completed"] or 0),
            "failed": int(row["failed"] or 0),
            "from_web": int(row["from_web"] or 0),
            "from_telegram": int(row["from_telegram"] or 0),
            "avg_time_ms": float(row["avg_time_ms"]) if row["avg_time_ms"] is not None else None,
        }


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _normalize_row(row: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if row is None:
        return None
    if "hits" in row and isinstance(row["hits"], str):
        try:
            row["hits"] = json.loads(row["hits"])
        except (json.JSONDecodeError, TypeError):
            row["hits"] = None
    return row
