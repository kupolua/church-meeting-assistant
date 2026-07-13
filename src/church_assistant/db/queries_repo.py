"""
Queries repository: CRUD for the `queries` table.

Handles:
    - Insert new query (from web or Telegram)
    - Fetch next pending (worker consumer, with FOR UPDATE SKIP LOCKED)
    - Update status transitions (pending → processing → completed/failed)
    - Load query by ID (for history, /verbose)
    - List recent queries (for history view, dashboard)

Design:
    - Repository functions are stateless — they take a pool or connection.
    - All functions return dicts (not ORM objects) — plain data.
    - Timestamps are timezone-aware (TIMESTAMPTZ).
    - JSONB fields (hits, sources) parsed/serialized here.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Optional

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


# ─────────────────────────────────────────────────────────────
# Types (documented shape of dicts)
# ─────────────────────────────────────────────────────────────
#
# Query row = {
#     "id": int,
#     "source": "web" | "telegram",
#     "user_id": int | None,
#     "telegram_chat_id": int | None,
#     "telegram_message_id": int | None,
#     "question": str,
#     "collection": str,
#     "verbose_mode": bool,
#     "status": "pending" | "processing" | "completed" | "failed" | "cancelled",
#     "asked_at": datetime,
#     "started_at": datetime | None,
#     "completed_at": datetime | None,
#     "hits": list[dict] | None,           # JSONB parsed
#     "synthesis": str | None,
#     "sources": list[str] | None,
#     "embed_time_ms": int | None,
#     "qdrant_time_ms": int | None,
#     "rerank_time_ms": int | None,
#     "gemma_time_ms": int | None,
#     "total_time_ms": int | None,
#     "error_message": str | None,
#     "error_traceback": str | None,
#     "retry_count": int,
# }


# ─────────────────────────────────────────────────────────────
# CREATE
# ─────────────────────────────────────────────────────────────

async def insert_pending(
    pool: AsyncConnectionPool,
    *,
    source: str,                          # 'web' | 'telegram'
    question: str,
    user_id: Optional[int] = None,
    telegram_chat_id: Optional[int] = None,
    telegram_message_id: Optional[int] = None,
    collection: str = "protocols",
    verbose_mode: bool = False,
) -> int:
    """
    Insert a new query with status='pending'.

    Returns the new query ID.

    Validates:
        - source ∈ {'web', 'telegram'}
        - collection ∈ {'protocols', 'analyses', 'turns', 'protocol_full'}
        - if source='telegram', user_id and telegram_chat_id must be set
    """
    if source not in ("web", "telegram"):
        raise ValueError(f"Invalid source: {source!r}")
    if collection not in ("protocols", "analyses", "turns", "protocol_full"):
        raise ValueError(f"Invalid collection: {collection!r}")
    if source == "telegram" and (user_id is None or telegram_chat_id is None):
        raise ValueError("telegram source requires user_id and telegram_chat_id")

    sql = """
        INSERT INTO queries (
            source, user_id, telegram_chat_id, telegram_message_id,
            question, collection, verbose_mode, status
        ) VALUES (
            %s, %s, %s, %s,
            %s, %s, %s, 'pending'
        )
        RETURNING id
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (
                source, user_id, telegram_chat_id, telegram_message_id,
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
    pool: AsyncConnectionPool,
    query_id: int,
) -> Optional[dict[str, Any]]:
    """Load a single query by ID. Returns None if not found."""
    sql = "SELECT * FROM queries WHERE id = %s"
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (query_id,))
            row = await cur.fetchone()
            return _normalize_row(row)


async def list_recent(
    pool: AsyncConnectionPool,
    *,
    limit: int = 50,
    offset: int = 0,
    source: Optional[str] = None,       # filter by 'web' | 'telegram'
    status: Optional[str] = None,       # filter by status
    user_id: Optional[int] = None,      # filter by user
) -> list[dict[str, Any]]:
    """
    List queries ordered by asked_at DESC.

    Used for:
        - History view (Pavlo's web UI)
        - Analytics dashboard
        - /stats command
    """
    where_clauses: list[str] = []
    params: list[Any] = []

    if source is not None:
        where_clauses.append("source = %s")
        params.append(source)
    if status is not None:
        where_clauses.append("status = %s")
        params.append(status)
    if user_id is not None:
        where_clauses.append("user_id = %s")
        params.append(user_id)

    where_sql = ""
    if where_clauses:
        where_sql = "WHERE " + " AND ".join(where_clauses)

    sql = f"""
        SELECT * FROM queries
        {where_sql}
        ORDER BY asked_at DESC
        LIMIT %s OFFSET %s
    """
    params.extend([limit, offset])

    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            rows = await cur.fetchall()
            return [_normalize_row(r) for r in rows if r is not None]  # type: ignore[misc]


async def get_last_completed_for_telegram(
    pool: AsyncConnectionPool,
    telegram_chat_id: int,
) -> Optional[dict[str, Any]]:
    """
    Fetch the most recent completed query for a Telegram chat.

    Used by /verbose to show retrieved hits of the last answer.
    """
    sql = """
        SELECT * FROM queries
        WHERE telegram_chat_id = %s
          AND source = 'telegram'
          AND status = 'completed'
        ORDER BY completed_at DESC
        LIMIT 1
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (telegram_chat_id,))
            row = await cur.fetchone()
            return _normalize_row(row)


# ─────────────────────────────────────────────────────────────
# WORKER: fetch next pending
# ─────────────────────────────────────────────────────────────

async def fetch_next_pending(
    pool: AsyncConnectionPool,
) -> Optional[dict[str, Any]]:
    """
    Atomically fetch the next pending query and mark it as 'processing'.

    Uses FOR UPDATE SKIP LOCKED to safely support multiple concurrent
    workers (currently we run one, but this is future-proof).

    Returns None if queue is empty.

    Transaction:
        BEGIN
        SELECT ... FROM queries WHERE status='pending'
        ORDER BY asked_at ASC LIMIT 1
        FOR UPDATE SKIP LOCKED
        → if row: UPDATE status='processing', started_at=NOW()
        COMMIT
    """
    select_sql = """
        SELECT id FROM queries
        WHERE status = 'pending'
        ORDER BY asked_at ASC
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    """
    update_sql = """
        UPDATE queries
        SET status = 'processing', started_at = NOW()
        WHERE id = %s
        RETURNING *
    """
    async with pool.connection() as conn:
        # psycopg's connection() context manager already wraps in a transaction;
        # commit happens on __aexit__ if no exception raised
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(select_sql)
            picked = await cur.fetchone()
            if picked is None:
                return None

            query_id = picked["id"]
            await cur.execute(update_sql, (query_id,))
            row = await cur.fetchone()
            return _normalize_row(row)


# ─────────────────────────────────────────────────────────────
# UPDATE: status transitions
# ─────────────────────────────────────────────────────────────

async def mark_completed(
    pool: AsyncConnectionPool,
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
    """
    Mark a query as completed with results.

    Called by worker after successful RAG pipeline.
    """
    sql = """
        UPDATE queries
        SET status = 'completed',
            completed_at = NOW(),
            hits = %s::jsonb,
            synthesis = %s,
            sources = %s,
            embed_time_ms = %s,
            qdrant_time_ms = %s,
            rerank_time_ms = %s,
            gemma_time_ms = %s,
            total_time_ms = %s
        WHERE id = %s
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (
                json.dumps(hits, ensure_ascii=False),
                synthesis,
                sources,
                embed_time_ms,
                qdrant_time_ms,
                rerank_time_ms,
                gemma_time_ms,
                total_time_ms,
                query_id,
            ))


async def mark_failed(
    pool: AsyncConnectionPool,
    query_id: int,
    *,
    error_message: str,
    error_traceback: str,
    increment_retry: bool = True,
) -> int:
    """
    Mark a query as failed.

    Returns the new retry_count (after increment if applicable).

    If retry_count reaches max, caller should decide what to do.
    """
    if increment_retry:
        sql = """
            UPDATE queries
            SET status = 'failed',
                completed_at = NOW(),
                error_message = %s,
                error_traceback = %s,
                retry_count = retry_count + 1
            WHERE id = %s
            RETURNING retry_count
        """
    else:
        sql = """
            UPDATE queries
            SET status = 'failed',
                completed_at = NOW(),
                error_message = %s,
                error_traceback = %s
            WHERE id = %s
            RETURNING retry_count
        """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (error_message, error_traceback, query_id))
            row = await cur.fetchone()
            return int(row[0]) if row else 0


async def requeue_for_retry(
    pool: AsyncConnectionPool,
    query_id: int,
) -> None:
    """
    Reset a failed query back to 'pending' for another attempt.

    Called after mark_failed when retry_count < max_retries.
    """
    sql = """
        UPDATE queries
        SET status = 'pending',
            started_at = NULL,
            completed_at = NULL,
            error_message = NULL,
            error_traceback = NULL
        WHERE id = %s
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (query_id,))


async def cancel(
    pool: AsyncConnectionPool,
    query_id: int,
) -> None:
    """Mark a query as cancelled (manual, from dashboard)."""
    sql = """
        UPDATE queries
        SET status = 'cancelled',
            completed_at = NOW()
        WHERE id = %s
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (query_id,))


# ─────────────────────────────────────────────────────────────
# Aggregations (for dashboard, MVP-B)
# ─────────────────────────────────────────────────────────────

async def get_queue_depth(pool: AsyncConnectionPool) -> dict[str, int]:
    """Return {pending, processing, failed} counts."""
    sql = "SELECT pending, processing, failed FROM v_queue_depth"
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql)
            row = await cur.fetchone()
            if row is None:
                return {"pending": 0, "processing": 0, "failed": 0}
            return {
                "pending": int(row["pending"] or 0),
                "processing": int(row["processing"] or 0),
                "failed": int(row["failed"] or 0),
            }


async def get_stats_today(pool: AsyncConnectionPool) -> dict[str, Any]:
    """Return today's stats (last 24h) from v_stats_today view."""
    sql = "SELECT * FROM v_stats_today"
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql)
            row = await cur.fetchone()
            if row is None:
                return {
                    "total": 0, "completed": 0, "failed": 0,
                    "from_web": 0, "from_telegram": 0, "avg_time_ms": None,
                }
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
    """
    Normalize a raw DB row:
        - hits: JSONB → Python list (already parsed by psycopg jsonb adapter)
        - sources: TEXT[] → Python list (already handled by psycopg)

    psycopg 3 already parses JSONB and TEXT[] automatically, but we normalize
    None-handling here in case future migrations touch these columns.
    """
    if row is None:
        return None

    # hits: if psycopg didn't parse (edge case), parse now
    if "hits" in row and isinstance(row["hits"], str):
        try:
            row["hits"] = json.loads(row["hits"])
        except (json.JSONDecodeError, TypeError):
            row["hits"] = None

    return row


# ─────────────────────────────────────────────────────────────
# CLI smoke test
# ─────────────────────────────────────────────────────────────

async def _smoke_test() -> None:
    """
    Round-trip test: insert → fetch → complete → verify.

    Cleanup: delete the test row at the end.
    """
    import asyncio  # noqa: F401
    from church_assistant.db.connection import get_pool, close_pool

    print("=" * 70)
    print("  queries_repo — smoke test")
    print("=" * 70)
    print()

    pool = await get_pool()

    # 1. Insert
    print("1. Inserting pending query (source=web)...")
    query_id = await insert_pending(
        pool,
        source="web",
        question="[SMOKE TEST] Що обговорювали 22 червня?",
    )
    print(f"   ✓ Inserted, id={query_id}")

    # 2. Read
    print()
    print("2. Reading back by ID...")
    q = await get_by_id(pool, query_id)
    assert q is not None
    assert q["status"] == "pending"
    assert q["source"] == "web"
    assert q["question"].startswith("[SMOKE TEST]")
    print(f"   ✓ Status={q['status']}, question={q['question'][:50]}...")

    # 3. Fetch next pending (worker consumer)
    print()
    print("3. Worker fetch_next_pending...")
    picked = await fetch_next_pending(pool)
    assert picked is not None
    assert picked["id"] == query_id
    assert picked["status"] == "processing"
    assert picked["started_at"] is not None
    print(f"   ✓ Picked query id={picked['id']}, status={picked['status']}")

    # 4. Verify queue depth
    print()
    print("4. Queue depth after processing pickup...")
    depth = await get_queue_depth(pool)
    print(f"   {depth}")
    assert depth["processing"] >= 1  # at least our test row

    # 5. Mark completed
    print()
    print("5. Marking completed with fake hits+synthesis...")
    await mark_completed(
        pool,
        query_id,
        hits=[
            {
                "meeting_date": "2026-06-22",
                "topic_title": "Fake topic",
                "vector_score": 0.5,
                "rerank_score": 0.7,
            }
        ],
        synthesis="Fake answer for smoke test.",
        sources=["2026-06-22"],
        embed_time_ms=100,
        qdrant_time_ms=50,
        rerank_time_ms=200,
        gemma_time_ms=8000,
        total_time_ms=8350,
    )
    print("   ✓ Marked completed")

    # 6. Re-read
    print()
    print("6. Re-reading after completion...")
    q = await get_by_id(pool, query_id)
    assert q is not None
    assert q["status"] == "completed"
    assert q["synthesis"] == "Fake answer for smoke test."
    assert q["sources"] == ["2026-06-22"]
    assert isinstance(q["hits"], list) and len(q["hits"]) == 1
    print(f"   ✓ Status={q['status']}, synthesis len={len(q['synthesis'])}")
    print(f"   ✓ hits={q['hits']}")
    print(f"   ✓ sources={q['sources']}")
    print(f"   ✓ total_time_ms={q['total_time_ms']}")

    # 7. Cleanup: delete test row
    print()
    print("7. Cleanup — deleting test row...")
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM queries WHERE id = %s", (query_id,))
    print(f"   ✓ Deleted query id={query_id}")

    # 8. Stats today
    print()
    print("8. Stats today (v_stats_today)...")
    stats = await get_stats_today(pool)
    print(f"   {stats}")

    await close_pool()

    print()
    print("=" * 70)
    print("  ✓ ALL SMOKE TESTS PASSED")
    print("=" * 70)


if __name__ == "__main__":
    import asyncio
    asyncio.run(_smoke_test())
