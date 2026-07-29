"""
Ingestion-jobs repository — CRUD for `ingestion_jobs` (tenant-aware, MT Phase 1).

Every tenant-scoped op runs via tenant_context.tenant_cursor(pool, tenant_id):
RLS then guarantees the caller only ever touches that church's jobs. The queue
fetch is the exception — the shared ingestion-worker scans across ALL tenants via
the SECURITY DEFINER claim_next_ingestion_job() (bypasses RLS) and returns the
claimed row WITH its tenant_id, so the worker processes it in-context.

meeting_date is unique PER TENANT (migration 005): two churches can meet the
same day.
"""

from __future__ import annotations

from typing import Any, Optional

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from church_assistant.db.tenant_context import tenant_cursor


# Statuses the worker may pick up, mapped to the in-flight status it moves to.
_RUNNABLE_TRANSITIONS = {
    "pending": "transcribing",
    "queued_analysis": "analyzing",
}

# Non-terminal statuses (shown as "active" on the ingestion dashboard).
ACTIVE_STATUSES = (
    "pending", "transcribing", "awaiting_review",
    "queued_analysis", "analyzing", "indexing",
)


# ─────────────────────────────────────────────────────────────
# CREATE
# ─────────────────────────────────────────────────────────────

async def insert_job(
    pool: AsyncConnectionPool,
    tenant_id: int,
    *,
    meeting_date: str,
    meeting_dir: str,
    original_filename: Optional[str] = None,
    audio_filename: Optional[str] = None,
) -> int:
    """Insert a new job (status='pending') for a tenant. Returns the new id.

    Raises UniqueViolation if a job already exists for this (tenant, meeting_date).
    """
    sql = """
        INSERT INTO ingestion_jobs (
            tenant_id, meeting_date, meeting_dir, original_filename, audio_filename, status
        ) VALUES (
            %s, %s, %s, %s, %s, 'pending'
        )
        RETURNING id
    """
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(sql, (
            tenant_id, meeting_date, meeting_dir, original_filename, audio_filename,
        ))
        row = await cur.fetchone()
        if row is None:
            raise RuntimeError("INSERT ... RETURNING did not return an id")
        return int(row[0])


# ─────────────────────────────────────────────────────────────
# READ
# ─────────────────────────────────────────────────────────────

async def get_by_id(
    pool: AsyncConnectionPool, tenant_id: int, job_id: int,
) -> Optional[dict[str, Any]]:
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM ingestion_jobs WHERE id = %s", (job_id,))
        return await cur.fetchone()


async def get_by_date(
    pool: AsyncConnectionPool, tenant_id: int, meeting_date: str,
) -> Optional[dict[str, Any]]:
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM ingestion_jobs WHERE meeting_date = %s", (meeting_date,)
        )
        return await cur.fetchone()


async def list_recent(
    pool: AsyncConnectionPool,
    tenant_id: int,
    *,
    limit: int = 50,
    offset: int = 0,
    status: Optional[str] = None,
) -> list[dict[str, Any]]:
    where_sql = ""
    params: list[Any] = []
    if status is not None:
        where_sql = "WHERE status = %s"; params.append(status)
    sql = f"""
        SELECT * FROM ingestion_jobs
        {where_sql}
        ORDER BY created_at DESC
        LIMIT %s OFFSET %s
    """
    params.extend([limit, offset])
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute(sql, params)
        return list(await cur.fetchall())


async def list_active(pool: AsyncConnectionPool, tenant_id: int) -> list[dict[str, Any]]:
    """Non-terminal jobs (pending … indexing) for a tenant, oldest first."""
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute(
            "SELECT * FROM ingestion_jobs WHERE status = ANY(%s) ORDER BY created_at ASC",
            (list(ACTIVE_STATUSES),),
        )
        return list(await cur.fetchall())


# ─────────────────────────────────────────────────────────────
# WORKER: claim next runnable (across all tenants — bypasses RLS)
# ─────────────────────────────────────────────────────────────

async def fetch_next_runnable(
    pool: AsyncConnectionPool,
    *,
    allowed_statuses: Optional[tuple[str, ...]] = None,
) -> Optional[dict[str, Any]]:
    """
    Atomically claim the next runnable job across ALL tenants and transition it
    (pending→transcribing, queued_analysis→analyzing). Returns the row incl
    tenant_id, or None. The worker then processes inside that tenant's context.

    `allowed_statuses` restricts which runnable statuses to consider — the worker
    passes only ('pending',) when Ollama/Qdrant are down.
    """
    if allowed_statuses is None:
        allowed_statuses = tuple(_RUNNABLE_TRANSITIONS)
    else:
        invalid = set(allowed_statuses) - set(_RUNNABLE_TRANSITIONS)
        if invalid:
            raise ValueError(f"Not runnable statuses: {sorted(invalid)}")

    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM claim_next_ingestion_job(%s)", (list(allowed_statuses),)
            )
            row = await cur.fetchone()
            # Composite-returning function yields one all-NULL row when empty.
            if row is None or row.get("id") is None:
                return None
            return row


# ─────────────────────────────────────────────────────────────
# UPDATE: progress + status transitions (tenant-scoped)
# ─────────────────────────────────────────────────────────────

async def set_stage(
    pool: AsyncConnectionPool, tenant_id: int, job_id: int,
    *, stage: Optional[str], progress_note: Optional[str] = None,
) -> None:
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(
            "UPDATE ingestion_jobs SET stage = %s, progress_note = %s WHERE id = %s",
            (stage, progress_note, job_id),
        )


async def mark_awaiting_review(
    pool: AsyncConnectionPool, tenant_id: int, job_id: int,
    *, speaker_count: Optional[int] = None,
) -> None:
    """transcribing → awaiting_review."""
    sql = """
        UPDATE ingestion_jobs
        SET status = 'awaiting_review', transcribed_at = NOW(), stage = NULL,
            progress_note = 'Очікує ревʼю speakers.json',
            speaker_count = COALESCE(%s, speaker_count)
        WHERE id = %s
    """
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(sql, (speaker_count, job_id))


async def mark_queued_analysis(
    pool: AsyncConnectionPool, tenant_id: int, job_id: int,
    *, speaker_count: Optional[int] = None,
) -> None:
    """awaiting_review → queued_analysis (web speakers editor)."""
    sql = """
        UPDATE ingestion_jobs
        SET status = 'queued_analysis', reviewed_at = NOW(), stage = NULL,
            progress_note = 'У черзі на аналіз',
            speaker_count = COALESCE(%s, speaker_count)
        WHERE id = %s
    """
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(sql, (speaker_count, job_id))


async def enqueue_reprocess(
    pool: AsyncConnectionPool,
    tenant_id: int,
    *,
    meeting_date: str,
    meeting_dir: str,
    audio_filename: Optional[str] = None,
    speaker_count: Optional[int] = None,
) -> int:
    """
    Queue a full re-run for an already-processed meeting (speakers edited).
    Upserts the tenant's job for this meeting at 'queued_analysis' with
    force_reprocess=TRUE. Returns the job id.
    """
    note = "У черзі на переобробку (нові імена)"
    sql_update = """
        UPDATE ingestion_jobs
        SET status = 'queued_analysis', force_reprocess = TRUE,
            audio_filename = COALESCE(%s, audio_filename),
            speaker_count = COALESCE(%s, speaker_count),
            reviewed_at = NOW(), started_at = NULL, completed_at = NULL,
            stage = NULL, progress_note = %s,
            error_message = NULL, error_traceback = NULL, retry_count = 0
        WHERE meeting_date = %s
        RETURNING id
    """
    sql_insert = """
        INSERT INTO ingestion_jobs (
            tenant_id, meeting_date, meeting_dir, audio_filename, status,
            force_reprocess, reviewed_at, speaker_count, progress_note
        ) VALUES (%s, %s, %s, %s, 'queued_analysis', TRUE, NOW(), %s, %s)
        RETURNING id
    """
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(sql_update, (audio_filename, speaker_count, note, meeting_date))
        row = await cur.fetchone()
        if row is not None:
            return int(row[0])
        await cur.execute(sql_insert, (
            tenant_id, meeting_date, meeting_dir, audio_filename, speaker_count, note,
        ))
        row = await cur.fetchone()
        if row is None:
            raise RuntimeError("INSERT ... RETURNING did not return an id")
        return int(row[0])


async def mark_indexing(pool: AsyncConnectionPool, tenant_id: int, job_id: int) -> None:
    """analyzing → indexing."""
    sql = """
        UPDATE ingestion_jobs
        SET status = 'indexing', stage = 'index', progress_note = 'Індексація у Qdrant'
        WHERE id = %s
    """
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(sql, (job_id,))


async def mark_completed(
    pool: AsyncConnectionPool, tenant_id: int, job_id: int,
    *, indexed: bool = True, index_points: Optional[int] = None,
) -> None:
    """indexing → completed."""
    sql = """
        UPDATE ingestion_jobs
        SET status = 'completed', completed_at = NOW(), stage = NULL,
            progress_note = 'Готово', indexed = %s,
            index_points = COALESCE(%s, index_points)
        WHERE id = %s
    """
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(sql, (indexed, index_points, job_id))


async def mark_failed(
    pool: AsyncConnectionPool, tenant_id: int, job_id: int,
    *, error_message: str, error_traceback: str, increment_retry: bool = True,
) -> int:
    """Mark failed; return the new retry_count."""
    retry_sql = ", retry_count = retry_count + 1" if increment_retry else ""
    sql = f"""
        UPDATE ingestion_jobs
        SET status = 'failed', completed_at = NOW(),
            error_message = %s, error_traceback = %s{retry_sql}
        WHERE id = %s
        RETURNING retry_count
    """
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(sql, (error_message, error_traceback, job_id))
        row = await cur.fetchone()
        return int(row[0]) if row else 0


async def requeue(
    pool: AsyncConnectionPool, tenant_id: int, job_id: int, *, to_status: str,
) -> None:
    """Reset a failed job to a runnable status ('pending' | 'queued_analysis')."""
    if to_status not in _RUNNABLE_TRANSITIONS:
        raise ValueError(f"Invalid requeue target: {to_status!r}")
    sql = """
        UPDATE ingestion_jobs
        SET status = %s, completed_at = NULL, stage = NULL, progress_note = NULL,
            error_message = NULL, error_traceback = NULL
        WHERE id = %s
    """
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(sql, (to_status, job_id))


async def cancel(pool: AsyncConnectionPool, tenant_id: int, job_id: int) -> None:
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(
            "UPDATE ingestion_jobs SET status='cancelled', completed_at=NOW(), "
            "progress_note='Скасовано' WHERE id = %s",
            (job_id,),
        )


# ─────────────────────────────────────────────────────────────
# Aggregations (per-tenant — RLS scopes the view automatically)
# ─────────────────────────────────────────────────────────────

async def get_depth(pool: AsyncConnectionPool, tenant_id: int) -> dict[str, int]:
    keys = (
        "pending", "transcribing", "awaiting_review", "queued_analysis",
        "analyzing", "indexing", "completed", "failed",
    )
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute("SELECT * FROM v_ingestion_depth")
        row = await cur.fetchone()
        if row is None:
            return {k: 0 for k in keys}
        return {k: int(row.get(k) or 0) for k in keys}
