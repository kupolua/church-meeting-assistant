"""
Logs repository — `logs`, `errors`, `health_checks` (tenant-aware, MT Phase 1).

Isolation model:
  - logs & errors are RLS-gated → tenant-scoped reads/writes via tenant_cursor.
    (System/platform events with no specific church pass a reserved system
    tenant_id — decided by the caller / Logger.)
  - health_checks is GLOBAL infra (not RLS-gated) → plain connection, no tenant.
  - The error-ALERT loop is platform-level (alerts go to the platform owner), so
    list_unalerted_errors / mark_error_alerted use SECURITY DEFINER helpers that
    scan/mark across ALL tenants (migration 005).

Logging must NEVER raise — write failures are swallowed to protect the caller.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from church_assistant.db.tenant_context import tenant_cursor


VALID_PROCESSES = ("web", "bot", "worker", "cli")
VALID_LEVELS = ("DEBUG", "INFO", "WARN", "ERROR")


# ─────────────────────────────────────────────────────────────
# LOGS
# ─────────────────────────────────────────────────────────────

async def log_event(
    pool: AsyncConnectionPool,
    tenant_id: int,
    *,
    process: str,
    level: str,
    event: str,
    message: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
    query_id: Optional[int] = None,
    user_id: Optional[int] = None,
) -> None:
    """Write a structured log event for a tenant. NEVER raises."""
    if process not in VALID_PROCESSES:
        process = "cli"
    if level not in VALID_LEVELS:
        level = "INFO"
    sql = """
        INSERT INTO logs (tenant_id, process, level, event, message, metadata, query_id, user_id)
        VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)
    """
    try:
        meta = json.dumps(metadata, ensure_ascii=False) if metadata else None
        async with tenant_cursor(pool, tenant_id) as cur:
            await cur.execute(sql, (
                tenant_id, process, level, event, message, meta, query_id, user_id,
            ))
    except Exception:
        pass  # logging must not crash the app


async def list_recent(
    pool: AsyncConnectionPool,
    tenant_id: int,
    *,
    limit: int = 100,
    offset: int = 0,
    process: Optional[str] = None,
    level: Optional[str] = None,
    event_prefix: Optional[str] = None,
    query_id: Optional[int] = None,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if process is not None:
        where.append("process = %s"); params.append(process)
    if level is not None:
        where.append("level = %s"); params.append(level)
    if event_prefix is not None:
        where.append("event LIKE %s"); params.append(f"{event_prefix}%")
    if query_id is not None:
        where.append("query_id = %s"); params.append(query_id)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"""
        SELECT * FROM logs {where_sql}
        ORDER BY timestamp DESC, id DESC
        LIMIT %s OFFSET %s
    """
    params.extend([limit, offset])
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute(sql, params)
        return [dict(r) for r in await cur.fetchall()]


async def get_trace(
    pool: AsyncConnectionPool, tenant_id: int, query_id: int,
) -> list[dict[str, Any]]:
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM logs WHERE query_id = %s ORDER BY timestamp ASC, id ASC",
            (query_id,),
        )
        return [dict(r) for r in await cur.fetchall()]


# ─────────────────────────────────────────────────────────────
# ERRORS
# ─────────────────────────────────────────────────────────────

async def record_error(
    pool: AsyncConnectionPool,
    tenant_id: int,
    *,
    process: str,
    error_type: str,
    error_message: str,
    traceback: str,
    query_id: Optional[int] = None,
    user_id: Optional[int] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> Optional[int]:
    """Record an error for a tenant. Returns the id, or None on silent failure."""
    if process not in VALID_PROCESSES:
        process = "cli"
    sql = """
        INSERT INTO errors (
            tenant_id, process, error_type, error_message, traceback,
            query_id, user_id, metadata
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        RETURNING id
    """
    try:
        meta = json.dumps(metadata, ensure_ascii=False) if metadata else None
        async with tenant_cursor(pool, tenant_id) as cur:
            await cur.execute(sql, (
                tenant_id, process, error_type, error_message, traceback,
                query_id, user_id, meta,
            ))
            row = await cur.fetchone()
            return int(row[0]) if row else None
    except Exception:
        return None


# ----- platform alert loop (cross-tenant, SECURITY DEFINER) -----

async def list_unalerted_errors(
    pool: AsyncConnectionPool, limit: int = 20,
) -> list[dict[str, Any]]:
    """Unalerted errors across ALL tenants (platform alert loop)."""
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM list_unalerted_errors_all(%s)", (limit,))
            return [dict(r) for r in await cur.fetchall()]


async def mark_error_alerted(pool: AsyncConnectionPool, error_id: int) -> None:
    """Mark an error alerted (platform alert loop, cross-tenant)."""
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT mark_error_alerted_any(%s)", (error_id,))
    except Exception:
        pass


# ----- per-tenant dashboard -----

async def list_unresolved_errors(
    pool: AsyncConnectionPool, tenant_id: int, *, limit: int = 50,
) -> list[dict[str, Any]]:
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM errors WHERE resolved_at IS NULL ORDER BY timestamp DESC LIMIT %s",
            (limit,),
        )
        return [dict(r) for r in await cur.fetchall()]


async def mark_error_resolved(
    pool: AsyncConnectionPool, tenant_id: int, error_id: int,
) -> None:
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(
            "UPDATE errors SET resolved_at = NOW() WHERE id = %s", (error_id,)
        )


# ─────────────────────────────────────────────────────────────
# HEALTH_CHECKS — global infra (not tenant-scoped, no RLS)
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
    sql = """
        INSERT INTO health_checks (
            ollama_up, qdrant_up, ollama_response_time_ms, qdrant_response_time_ms, notes
        ) VALUES (%s, %s, %s, %s, %s)
    """
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (
                    ollama_up, qdrant_up,
                    ollama_response_time_ms, qdrant_response_time_ms, notes,
                ))
    except Exception:
        pass


async def get_latest_health(pool: AsyncConnectionPool) -> Optional[dict[str, Any]]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM v_latest_health")
            row = await cur.fetchone()
            return dict(row) if row else None


# ─────────────────────────────────────────────────────────────
# Aggregations (per-tenant)
# ─────────────────────────────────────────────────────────────

async def count_logs_by_level(
    pool: AsyncConnectionPool, tenant_id: int, *, hours: int = 24,
) -> dict[str, int]:
    sql = f"""
        SELECT level, count(*) AS n FROM logs
        WHERE timestamp > NOW() - INTERVAL '{int(hours)} hours'
        GROUP BY level
    """
    result: dict[str, int] = {lvl: 0 for lvl in VALID_LEVELS}
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(sql)
        for row in await cur.fetchall():
            result[row[0]] = int(row[1])
    return result


async def count_errors_by_type(
    pool: AsyncConnectionPool, tenant_id: int, *, hours: int = 24, limit: int = 10,
) -> list[dict[str, Any]]:
    sql = f"""
        SELECT error_type, count(*) AS n FROM errors
        WHERE timestamp > NOW() - INTERVAL '{int(hours)} hours'
        GROUP BY error_type ORDER BY n DESC LIMIT %s
    """
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(sql, (limit,))
        return [{"error_type": r[0], "count": int(r[1])} for r in await cur.fetchall()]
