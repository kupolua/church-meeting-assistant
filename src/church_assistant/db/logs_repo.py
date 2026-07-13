"""
Logs repository: CRUD for `logs`, `errors`, `health_checks` tables.

Handles:
    - Structured application logging (T1+T2+T3+T4)
    - Error recording (with Telegram alert flow)
    - Health check snapshots (60s cadence from worker)
    - Recent log queries for dashboard (MVP-B)

Design:
    - Logging must NEVER raise — write failures are swallowed silently
      to avoid cascading crashes in the caller (bot/web/worker).
    - Events use dot-separated namespaces: 'query.started', 'ollama.down', etc.
    - metadata is JSONB — arbitrary structured payload.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────

VALID_PROCESSES = ("web", "bot", "worker", "cli")
VALID_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")


# ─────────────────────────────────────────────────────────────
# LOGS: write
# ─────────────────────────────────────────────────────────────

async def log_event(
    pool: AsyncConnectionPool,
    *,
    process: str,
    level: str,
    event: str,
    message: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    query_id: Optional[int] = None,
    user_id: Optional[int] = None,
) -> None:
    """
    Write a structured log event.

    NEVER raises — writes failures are silently swallowed to protect the caller.
    A crash in logging must not crash the application.

    Args:
        process: 'web' | 'bot' | 'worker' | 'cli'
        level:   'DEBUG' | 'INFO' | 'WARN' | 'ERROR'
        event:   dot.separated.namespace (e.g. 'query.started')
        message: human-readable line (optional)
        metadata: arbitrary JSONB payload (optional)
        query_id: link to queries.id (optional)
        user_id: link to users.id (optional)
    """
    # Silent validation — don't raise
    if process not in VALID_PROCESSES:
        process = "cli"  # fallback
    if level not in VALID_LEVELS:
        level = "INFO"  # fallback

    sql = """
        INSERT INTO logs (process, level, event, message, metadata, query_id, user_id)
        VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
    """
    try:
        meta_json = json.dumps(metadata, ensure_ascii=False) if metadata else None
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (
                    process, level, event, message, meta_json, query_id, user_id,
                ))
    except Exception:
        # Silent — logging must not crash the app
        pass


# ─────────────────────────────────────────────────────────────
# LOGS: read
# ─────────────────────────────────────────────────────────────

async def list_recent(
    pool: AsyncConnectionPool,
    *,
    limit: int = 100,
    offset: int = 0,
    process: Optional[str] = None,
    level: Optional[str] = None,
    event_prefix: Optional[str] = None,  # e.g. 'query.' → match query.started, query.completed
    query_id: Optional[int] = None,
) -> list[dict[str, Any]]:
    """
    List logs, newest first, with optional filters.

    Used for:
        - Dashboard log stream (T4)
        - Trace by query_id (T4)
        - Filter by level (errors only, warnings, etc.)
    """
    where_clauses: list[str] = []
    params: list[Any] = []

    if process is not None:
        where_clauses.append("process = %s")
        params.append(process)
    if level is not None:
        where_clauses.append("level = %s")
        params.append(level)
    if event_prefix is not None:
        where_clauses.append("event LIKE %s")
        params.append(f"{event_prefix}%")
    if query_id is not None:
        where_clauses.append("query_id = %s")
        params.append(query_id)

    where_sql = ""
    if where_clauses:
        where_sql = "WHERE " + " AND ".join(where_clauses)

    sql = f"""
        SELECT * FROM logs
        {where_sql}
        ORDER BY timestamp DESC, id DESC
        LIMIT %s OFFSET %s
    """
    params.extend([limit, offset])

    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def get_trace(
    pool: AsyncConnectionPool,
    query_id: int,
) -> list[dict[str, Any]]:
    """
    Get all log events for a specific query, chronologically ordered.

    Used for T4 debugging: "trace by query_id" — full timeline of one query.
    """
    sql = """
        SELECT * FROM logs
        WHERE query_id = %s
        ORDER BY timestamp ASC, id ASC
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (query_id,))
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


# ─────────────────────────────────────────────────────────────
# ERRORS: write
# ─────────────────────────────────────────────────────────────

async def record_error(
    pool: AsyncConnectionPool,
    *,
    process: str,
    error_type: str,
    error_message: str,
    traceback: str,
    query_id: Optional[int] = None,
    user_id: Optional[int] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> Optional[int]:
    """
    Record an error that will potentially trigger a Telegram alert.

    Returns the new error ID, or None if write failed silently.

    Called from except blocks in web/bot/worker code.
    """
    if process not in VALID_PROCESSES:
        process = "cli"

    sql = """
        INSERT INTO errors (
            process, error_type, error_message, traceback,
            query_id, user_id, metadata
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
        RETURNING id
    """
    try:
        meta_json = json.dumps(metadata, ensure_ascii=False) if metadata else None
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (
                    process, error_type, error_message, traceback,
                    query_id, user_id, meta_json,
                ))
                row = await cur.fetchone()
                return int(row[0]) if row else None
    except Exception:
        # Silent — recording errors must not itself throw
        return None


# ─────────────────────────────────────────────────────────────
# ERRORS: read (for alerts loop and dashboard)
# ─────────────────────────────────────────────────────────────

async def list_unalerted_errors(
    pool: AsyncConnectionPool,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """
    Fetch errors that haven't been alerted yet (alerted_at IS NULL).

    Called by worker's alerts_loop (MVP-B).
    """
    sql = """
        SELECT * FROM errors
        WHERE alerted_at IS NULL
        ORDER BY timestamp ASC
        LIMIT %s
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (limit,))
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def mark_error_alerted(
    pool: AsyncConnectionPool,
    error_id: int,
) -> None:
    """Mark an error as alerted (Telegram message sent to Pavlo)."""
    sql = "UPDATE errors SET alerted_at = NOW() WHERE id = %s"
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (error_id,))
    except Exception:
        pass


async def list_unresolved_errors(
    pool: AsyncConnectionPool,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """
    Fetch errors not yet resolved (resolved_at IS NULL).

    Shown in dashboard "Open errors" widget.
    """
    sql = """
        SELECT * FROM errors
        WHERE resolved_at IS NULL
        ORDER BY timestamp DESC
        LIMIT %s
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (limit,))
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def mark_error_resolved(
    pool: AsyncConnectionPool,
    error_id: int,
) -> None:
    """Mark an error as resolved (Pavlo clicked 'resolve' in dashboard)."""
    sql = "UPDATE errors SET resolved_at = NOW() WHERE id = %s"
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (error_id,))


# ─────────────────────────────────────────────────────────────
# HEALTH_CHECKS: write and read
# ─────────────────────────────────────────────────────────────

async def record_health_check(
    pool: AsyncConnectionPool,
    *,
    ollama_up: bool,
    qdrant_up: bool,
    ollama_response_time_ms: Optional[int] = None,
    qdrant_response_time_ms: Optional[int] = None,
    notes: Optional[str] = None,
) -> None:
    """
    Insert a health check snapshot.

    Called by worker every ~60s. Silent on failure.
    """
    sql = """
        INSERT INTO health_checks (
            ollama_up, qdrant_up,
            ollama_response_time_ms, qdrant_response_time_ms,
            notes
        )
        VALUES (%s, %s, %s, %s, %s)
    """
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (
                    ollama_up, qdrant_up,
                    ollama_response_time_ms, qdrant_response_time_ms,
                    notes,
                ))
    except Exception:
        pass


async def get_latest_health(
    pool: AsyncConnectionPool,
) -> Optional[dict[str, Any]]:
    """
    Fetch the most recent health check snapshot.

    Used by dashboard T1 "Ollama status" widget.
    """
    sql = "SELECT * FROM v_latest_health"
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql)
            row = await cur.fetchone()
            return dict(row) if row else None


# ─────────────────────────────────────────────────────────────
# Aggregations for dashboard
# ─────────────────────────────────────────────────────────────

async def count_logs_by_level(
    pool: AsyncConnectionPool,
    *,
    hours: int = 24,
) -> dict[str, int]:
    """
    Return {'DEBUG': N, 'INFO': N, 'WARN': N, 'ERROR': N} for last N hours.
    """
    sql = f"""
        SELECT level, count(*) as n
        FROM logs
        WHERE timestamp > NOW() - INTERVAL '{int(hours)} hours'
        GROUP BY level
    """
    result: dict[str, int] = {lvl: 0 for lvl in VALID_LEVELS}

    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql)
            rows = await cur.fetchall()
            for row in rows:
                result[row[0]] = int(row[1])

    return result


async def count_errors_by_type(
    pool: AsyncConnectionPool,
    *,
    hours: int = 24,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """
    Return [{'error_type': str, 'count': int}, ...] top errors in last N hours.
    """
    sql = f"""
        SELECT error_type, count(*) as n
        FROM errors
        WHERE timestamp > NOW() - INTERVAL '{int(hours)} hours'
        GROUP BY error_type
        ORDER BY n DESC
        LIMIT %s
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (limit,))
            rows = await cur.fetchall()
            return [{"error_type": r[0], "count": int(r[1])} for r in rows]


# ─────────────────────────────────────────────────────────────
# CLI smoke test
# ─────────────────────────────────────────────────────────────

async def _smoke_test() -> None:
    """
    Test full write/read cycle for logs, errors, health_checks.
    """
    from church_assistant.db.connection import get_pool, close_pool

    print("=" * 70)
    print("  logs_repo — smoke test")
    print("=" * 70)
    print()

    pool = await get_pool()

    # 1. Write log events
    print("1. Writing log events...")
    await log_event(
        pool,
        process="cli",
        level="INFO",
        event="smoke_test.start",
        message="Starting smoke test",
        metadata={"test_run_id": "abc123"},
    )
    await log_event(
        pool,
        process="cli",
        level="WARN",
        event="smoke_test.warning",
        message="A warning during test",
    )
    await log_event(
        pool,
        process="cli",
        level="INFO",
        event="smoke_test.finish",
        message="Finished smoke test",
    )
    print("   ✓ Wrote 3 log events")

    # 2. Read them back
    print()
    print("2. Reading logs by event prefix 'smoke_test.'...")
    logs = await list_recent(pool, event_prefix="smoke_test.", limit=10)
    print(f"   Found {len(logs)} logs")
    for lg in logs[:3]:
        print(f"     {lg['level']:<5} {lg['event']:<30} {lg['message']}")
    assert len(logs) >= 3

    # 3. Record error
    print()
    print("3. Recording an error...")
    err_id = await record_error(
        pool,
        process="cli",
        error_type="SmokeTestError",
        error_message="This is a fake error for testing",
        traceback="File 'test.py', line 42\n  ...\nSmokeTestError: fake",
        metadata={"context": "smoke_test"},
    )
    assert err_id is not None
    print(f"   ✓ Error recorded, id={err_id}")

    # 4. List unalerted
    print()
    print("4. Listing unalerted errors...")
    unalerted = await list_unalerted_errors(pool, limit=5)
    ids = [e["id"] for e in unalerted]
    print(f"   Unalerted count: {len(unalerted)} (includes our id={err_id}: {err_id in ids})")
    assert err_id in ids

    # 5. Mark as alerted
    print()
    print("5. Marking error as alerted...")
    await mark_error_alerted(pool, err_id)
    unalerted_after = await list_unalerted_errors(pool, limit=5)
    ids_after = [e["id"] for e in unalerted_after]
    assert err_id not in ids_after
    print(f"   ✓ Not in unalerted list anymore")

    # 6. Health check write + read
    print()
    print("6. Writing health check...")
    await record_health_check(
        pool,
        ollama_up=True,
        qdrant_up=True,
        ollama_response_time_ms=42,
        qdrant_response_time_ms=15,
        notes="smoke test",
    )
    latest = await get_latest_health(pool)
    assert latest is not None
    assert latest["ollama_up"] is True
    assert latest["qdrant_up"] is True
    print(f"   ✓ Latest: ollama={latest['ollama_up']}, qdrant={latest['qdrant_up']}, "
          f"ollama_ms={latest['ollama_response_time_ms']}")

    # 7. Aggregations
    print()
    print("7. Aggregations...")
    by_level = await count_logs_by_level(pool, hours=1)
    print(f"   Logs by level (last 1h): {by_level}")

    by_error = await count_errors_by_type(pool, hours=1)
    print(f"   Errors by type (last 1h): {by_error}")

    # 8. Cleanup (only our test data)
    print()
    print("8. Cleanup — deleting smoke test rows...")
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM logs WHERE event LIKE 'smoke_test.%'")
            n_logs = cur.rowcount
            await cur.execute("DELETE FROM errors WHERE error_type = 'SmokeTestError'")
            n_errors = cur.rowcount
            await cur.execute("DELETE FROM health_checks WHERE notes = 'smoke test'")
            n_health = cur.rowcount
    print(f"   ✓ Deleted {n_logs} logs, {n_errors} errors, {n_health} health checks")

    await close_pool()

    print()
    print("=" * 70)
    print("  ✓ ALL LOGS_REPO SMOKE TESTS PASSED")
    print("=" * 70)


if __name__ == "__main__":
    import asyncio
    asyncio.run(_smoke_test())
