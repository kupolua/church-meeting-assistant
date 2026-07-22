"""
Ingestion-jobs repository: CRUD for the `ingestion_jobs` table (MVP-C).

Handles the async meeting-ingestion pipeline queue:
    - Insert a new job (from the web upload form)
    - Fetch the next runnable job (worker consumer, FOR UPDATE SKIP LOCKED)
    - Drive status transitions across the long pipeline:
        pending → transcribing → awaiting_review → queued_analysis
        → analyzing → indexing → completed | failed | cancelled
    - Record fine-grained progress (stage + progress_note)
    - Load a job by ID / by meeting date (history, dashboard, editor)

Design (mirrors queries_repo):
    - Stateless functions taking a pool.
    - Return plain dicts (not ORM objects).
    - Timestamps are timezone-aware (TIMESTAMPTZ).

Note: speakers.json itself lives on disk in the meeting folder — this table
tracks the *job*, not the transcript. The speakers editor reads/writes the
file directly.
"""

from __future__ import annotations

from typing import Any, Optional

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


# ─────────────────────────────────────────────────────────────
# Types (documented shape of dicts)
# ─────────────────────────────────────────────────────────────
#
# Ingestion-job row = {
#     "id": int,
#     "meeting_date": str,                 # 'YYYY-MM-DD'
#     "meeting_dir": str,                  # abs path
#     "original_filename": str | None,
#     "audio_filename": str | None,
#     "status": "pending" | "transcribing" | "awaiting_review"
#               | "queued_analysis" | "analyzing" | "indexing"
#               | "completed" | "failed" | "cancelled",
#     "stage": str | None,
#     "progress_note": str | None,
#     "created_at": datetime,
#     "started_at": datetime | None,
#     "transcribed_at": datetime | None,
#     "reviewed_at": datetime | None,
#     "completed_at": datetime | None,
#     "speaker_count": int | None,
#     "indexed": bool,
#     "index_points": int | None,
#     "error_message": str | None,
#     "error_traceback": str | None,
#     "retry_count": int,
#     "notes": str | None,
# }

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
    *,
    meeting_date: str,
    meeting_dir: str,
    original_filename: Optional[str] = None,
    audio_filename: Optional[str] = None,
) -> int:
    """
    Insert a new ingestion job with status='pending'.

    Returns the new job ID.

    Raises psycopg.errors.UniqueViolation if a job already exists for this
    meeting_date (one job per date — the caller should get_by_date first and
    decide whether to resume).
    """
    sql = """
        INSERT INTO ingestion_jobs (
            meeting_date, meeting_dir, original_filename, audio_filename, status
        ) VALUES (
            %s, %s, %s, %s, 'pending'
        )
        RETURNING id
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (
                meeting_date, meeting_dir, original_filename, audio_filename,
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
    job_id: int,
) -> Optional[dict[str, Any]]:
    """Load a single job by ID. Returns None if not found."""
    sql = "SELECT * FROM ingestion_jobs WHERE id = %s"
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (job_id,))
            return await cur.fetchone()


async def get_by_date(
    pool: AsyncConnectionPool,
    meeting_date: str,
) -> Optional[dict[str, Any]]:
    """Load the job for a given meeting date. Returns None if not found."""
    sql = "SELECT * FROM ingestion_jobs WHERE meeting_date = %s"
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (meeting_date,))
            return await cur.fetchone()


async def list_recent(
    pool: AsyncConnectionPool,
    *,
    limit: int = 50,
    offset: int = 0,
    status: Optional[str] = None,
) -> list[dict[str, Any]]:
    """List jobs ordered by created_at DESC (for the ingestion dashboard)."""
    where_sql = ""
    params: list[Any] = []
    if status is not None:
        where_sql = "WHERE status = %s"
        params.append(status)

    sql = f"""
        SELECT * FROM ingestion_jobs
        {where_sql}
        ORDER BY created_at DESC
        LIMIT %s OFFSET %s
    """
    params.extend([limit, offset])

    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            return list(await cur.fetchall())


async def list_active(pool: AsyncConnectionPool) -> list[dict[str, Any]]:
    """
    List non-terminal jobs (pending … indexing), oldest first.

    Used for the live ingestion panel — what's currently in the pipeline.
    """
    sql = """
        SELECT * FROM ingestion_jobs
        WHERE status = ANY(%s)
        ORDER BY created_at ASC
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (list(ACTIVE_STATUSES),))
            return list(await cur.fetchall())


# ─────────────────────────────────────────────────────────────
# WORKER: fetch next runnable
# ─────────────────────────────────────────────────────────────

async def fetch_next_runnable(
    pool: AsyncConnectionPool,
    *,
    allowed_statuses: Optional[tuple[str, ...]] = None,
) -> Optional[dict[str, Any]]:
    """
    Atomically fetch the next runnable job and move it to its in-flight status.

    Runnable statuses and their transitions:
        pending          → transcribing   (run diarization + transcription)
        queued_analysis  → analyzing      (run merge → analyze → polish → index)

    `allowed_statuses` restricts which runnable statuses to consider (defaults
    to both). The worker passes only ('pending',) when Ollama/Qdrant are down —
    diarization + transcription don't need them, but analysis/indexing do.

    Uses FOR UPDATE SKIP LOCKED (future-proof for concurrent workers).
    Returns None if nothing is runnable.
    """
    if allowed_statuses is None:
        allowed_statuses = tuple(_RUNNABLE_TRANSITIONS)
    else:
        invalid = set(allowed_statuses) - set(_RUNNABLE_TRANSITIONS)
        if invalid:
            raise ValueError(f"Not runnable statuses: {sorted(invalid)}")

    select_sql = """
        SELECT id, status FROM ingestion_jobs
        WHERE status = ANY(%s)
        ORDER BY created_at ASC
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    """
    update_sql = """
        UPDATE ingestion_jobs
        SET status = %s,
            started_at = COALESCE(started_at, NOW()),
            error_message = NULL,
            error_traceback = NULL
        WHERE id = %s
        RETURNING *
    """
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(select_sql, (list(allowed_statuses),))
            picked = await cur.fetchone()
            if picked is None:
                return None

            next_status = _RUNNABLE_TRANSITIONS[picked["status"]]
            await cur.execute(update_sql, (next_status, picked["id"]))
            return await cur.fetchone()


# ─────────────────────────────────────────────────────────────
# UPDATE: progress + status transitions
# ─────────────────────────────────────────────────────────────

async def set_stage(
    pool: AsyncConnectionPool,
    job_id: int,
    *,
    stage: Optional[str],
    progress_note: Optional[str] = None,
) -> None:
    """Update the fine-grained progress marker (does not change status)."""
    sql = """
        UPDATE ingestion_jobs
        SET stage = %s, progress_note = %s
        WHERE id = %s
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (stage, progress_note, job_id))


async def mark_awaiting_review(
    pool: AsyncConnectionPool,
    job_id: int,
    *,
    speaker_count: Optional[int] = None,
) -> None:
    """
    Transcription finished — pause for the human speakers.json review.

    (transcribing → awaiting_review)
    """
    sql = """
        UPDATE ingestion_jobs
        SET status = 'awaiting_review',
            transcribed_at = NOW(),
            stage = NULL,
            progress_note = 'Очікує ревʼю speakers.json',
            speaker_count = COALESCE(%s, speaker_count)
        WHERE id = %s
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (speaker_count, job_id))


async def mark_queued_analysis(
    pool: AsyncConnectionPool,
    job_id: int,
    *,
    speaker_count: Optional[int] = None,
) -> None:
    """
    Speakers review submitted — hand back to the worker for analysis.

    (awaiting_review → queued_analysis). Called by the web speakers editor.
    """
    sql = """
        UPDATE ingestion_jobs
        SET status = 'queued_analysis',
            reviewed_at = NOW(),
            stage = NULL,
            progress_note = 'У черзі на аналіз',
            speaker_count = COALESCE(%s, speaker_count)
        WHERE id = %s
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (speaker_count, job_id))


async def mark_indexing(
    pool: AsyncConnectionPool,
    job_id: int,
) -> None:
    """Polish done — move into the auto-index step (analyzing → indexing)."""
    sql = """
        UPDATE ingestion_jobs
        SET status = 'indexing',
            stage = 'index',
            progress_note = 'Індексація у Qdrant'
        WHERE id = %s
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (job_id,))


async def mark_completed(
    pool: AsyncConnectionPool,
    job_id: int,
    *,
    indexed: bool = True,
    index_points: Optional[int] = None,
) -> None:
    """Pipeline finished (indexing → completed)."""
    sql = """
        UPDATE ingestion_jobs
        SET status = 'completed',
            completed_at = NOW(),
            stage = NULL,
            progress_note = 'Готово',
            indexed = %s,
            index_points = COALESCE(%s, index_points)
        WHERE id = %s
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (indexed, index_points, job_id))


async def mark_failed(
    pool: AsyncConnectionPool,
    job_id: int,
    *,
    error_message: str,
    error_traceback: str,
    increment_retry: bool = True,
) -> int:
    """
    Mark a job as failed. Returns the new retry_count.

    The worker decides whether to requeue (see requeue) based on this count.
    completed_at marks *when* it stopped (failed jobs are terminal until requeued).
    """
    if increment_retry:
        sql = """
            UPDATE ingestion_jobs
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
            UPDATE ingestion_jobs
            SET status = 'failed',
                completed_at = NOW(),
                error_message = %s,
                error_traceback = %s
            WHERE id = %s
            RETURNING retry_count
        """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (error_message, error_traceback, job_id))
            row = await cur.fetchone()
            return int(row[0]) if row else 0


async def requeue(
    pool: AsyncConnectionPool,
    job_id: int,
    *,
    to_status: str,
) -> None:
    """
    Reset a failed job back to a runnable status for another attempt.

    to_status must be 'pending' (retry transcription) or 'queued_analysis'
    (retry analysis) — the worker knows which stage failed.
    """
    if to_status not in _RUNNABLE_TRANSITIONS:
        raise ValueError(f"Invalid requeue target: {to_status!r}")
    sql = """
        UPDATE ingestion_jobs
        SET status = %s,
            completed_at = NULL,
            stage = NULL,
            progress_note = NULL,
            error_message = NULL,
            error_traceback = NULL
        WHERE id = %s
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (to_status, job_id))


async def cancel(
    pool: AsyncConnectionPool,
    job_id: int,
) -> None:
    """Mark a job as cancelled (manual, from dashboard)."""
    sql = """
        UPDATE ingestion_jobs
        SET status = 'cancelled',
            completed_at = NOW(),
            progress_note = 'Скасовано'
        WHERE id = %s
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (job_id,))


# ─────────────────────────────────────────────────────────────
# Aggregations (for the ingestion dashboard)
# ─────────────────────────────────────────────────────────────

async def get_depth(pool: AsyncConnectionPool) -> dict[str, int]:
    """Return per-status counts from v_ingestion_depth."""
    sql = "SELECT * FROM v_ingestion_depth"
    keys = (
        "pending", "transcribing", "awaiting_review", "queued_analysis",
        "analyzing", "indexing", "completed", "failed",
    )
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql)
            row = await cur.fetchone()
            if row is None:
                return {k: 0 for k in keys}
            return {k: int(row.get(k) or 0) for k in keys}


# ─────────────────────────────────────────────────────────────
# CLI smoke test (uv run python -m church_assistant.db.ingestion_jobs_repo)
# ─────────────────────────────────────────────────────────────

async def _smoke_test() -> None:
    """Round-trip a job through the full pipeline state machine, then clean up."""
    from church_assistant.db.connection import get_pool, close_pool

    print("=" * 70)
    print("  ingestion_jobs_repo — smoke test")
    print("=" * 70)
    print()

    pool = await get_pool()
    test_date = "1999-01-01"  # obviously-fake date, unlikely to collide

    # Clean any leftover from a previous failed run.
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM ingestion_jobs WHERE meeting_date = %s", (test_date,)
            )

    # 1. Insert
    print("1. Inserting pending job...")
    job_id = await insert_job(
        pool,
        meeting_date=test_date,
        meeting_dir=f"/tmp/data/meetings/{test_date}",
        original_filename="recording.m4a",
        audio_filename="audio.m4a",
    )
    print(f"   ✓ Inserted, id={job_id}")

    # 2. Read back
    print("\n2. Reading back by ID + by date...")
    j = await get_by_id(pool, job_id)
    assert j is not None and j["status"] == "pending"
    jd = await get_by_date(pool, test_date)
    assert jd is not None and jd["id"] == job_id
    print(f"   ✓ status={j['status']}, dir={j['meeting_dir']}")

    # 3. Worker picks it up → transcribing
    print("\n3. fetch_next_runnable (pending → transcribing)...")
    picked = await fetch_next_runnable(pool)
    assert picked is not None and picked["id"] == job_id
    assert picked["status"] == "transcribing"
    assert picked["started_at"] is not None
    print(f"   ✓ status={picked['status']}, started_at set")

    # 4. Progress + pause for review
    print("\n4. set_stage + mark_awaiting_review...")
    await set_stage(pool, job_id, stage="diarization", progress_note="Діаризація…")
    await mark_awaiting_review(pool, job_id, speaker_count=7)
    j = await get_by_id(pool, job_id)
    assert j["status"] == "awaiting_review"
    assert j["transcribed_at"] is not None
    assert j["speaker_count"] == 7
    print(f"   ✓ status={j['status']}, speakers={j['speaker_count']}")

    # 5. Review submitted → queued_analysis, worker resumes → analyzing
    print("\n5. review → queued_analysis → fetch → analyzing...")
    await mark_queued_analysis(pool, job_id)
    j = await get_by_id(pool, job_id)
    assert j["status"] == "queued_analysis" and j["reviewed_at"] is not None
    picked = await fetch_next_runnable(pool)
    assert picked is not None and picked["id"] == job_id
    assert picked["status"] == "analyzing"
    print(f"   ✓ status={picked['status']}")

    # 6. indexing → completed
    print("\n6. mark_indexing → mark_completed...")
    await mark_indexing(pool, job_id)
    await mark_completed(pool, job_id, indexed=True, index_points=512)
    j = await get_by_id(pool, job_id)
    assert j["status"] == "completed" and j["indexed"] is True
    assert j["index_points"] == 512 and j["completed_at"] is not None
    print(f"   ✓ status={j['status']}, indexed={j['indexed']}, points={j['index_points']}")

    # 7. Depth view
    print("\n7. get_depth (v_ingestion_depth)...")
    depth = await get_depth(pool)
    print(f"   {depth}")
    assert depth["completed"] >= 1

    # 8. Failure + requeue path
    print("\n8. mark_failed → requeue(pending)...")
    rc = await mark_failed(
        pool, job_id,
        error_message="boom", error_traceback="Traceback…",
    )
    assert rc == 1
    await requeue(pool, job_id, to_status="pending")
    j = await get_by_id(pool, job_id)
    assert j["status"] == "pending" and j["error_message"] is None
    print(f"   ✓ retry_count={rc}, requeued to status={j['status']}")

    # 9. Cleanup
    print("\n9. Cleanup — deleting test row...")
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("DELETE FROM ingestion_jobs WHERE id = %s", (job_id,))
    print(f"   ✓ Deleted job id={job_id}")

    await close_pool()

    print()
    print("=" * 70)
    print("  ✓ ALL SMOKE TESTS PASSED")
    print("=" * 70)


if __name__ == "__main__":
    import asyncio
    asyncio.run(_smoke_test())
