"""
Ingestion-job processor — run one dequeued job to its next checkpoint.

fetch_next_runnable() hands us a job already moved to an in-flight status:
    'transcribing' → run diarization + transcription, then pause for review
                     (→ awaiting_review)
    'analyzing'    → run merge → analyze → polish, then (auto-)index
                     (→ indexing → completed)

On failure, record it and requeue to the phase's runnable status if under the
retry cap, else leave it permanently 'failed'.

Never raises — the caller's loop must survive any single job.
"""

from __future__ import annotations

import traceback
from pathlib import Path
from typing import Any

from psycopg_pool import AsyncConnectionPool

from church_assistant.db import ingestion_jobs_repo as jobs_repo
from church_assistant.ingestion import stages
from church_assistant.ingestion.paths import resolve as resolve_paths
from church_assistant.shared.logger import Logger


_log = Logger(process="worker")


def _polish_date(meeting_date: str) -> str:
    """Convert '2026-06-15' → '15/06/2026' for polish_protocol --date (any -N suffix stripped)."""
    parts = meeting_date.split("-")
    if len(parts) >= 3:
        y, m, d = parts[0], parts[1], parts[2]
        return f"{d}/{m}/{y}"
    return meeting_date


async def process_job(
    pool: AsyncConnectionPool,
    job: dict[str, Any],
    *,
    max_retries: int,
    sequential: bool,
    auto_index: bool,
) -> None:
    """Route a dequeued job to the phase matching its in-flight status."""
    status = job["status"]
    if status == "transcribing":
        await _run_transcription(pool, job, max_retries=max_retries, sequential=sequential)
    elif status == "analyzing":
        await _run_analysis(pool, job, max_retries=max_retries, auto_index=auto_index)
    else:  # pragma: no cover — fetch_next_runnable only yields the two above
        await _log.warn(
            "ingestion.unexpected_status",
            message=f"job #{job['id']} in unexpected status {status!r}",
        )


# ─────────────────────────────────────────────────────────────
# Phase A: diarization + transcription → awaiting_review
# ─────────────────────────────────────────────────────────────

async def _run_transcription(
    pool: AsyncConnectionPool,
    job: dict[str, Any],
    *,
    max_retries: int,
    sequential: bool,
) -> None:
    job_id = job["id"]
    paths = resolve_paths(Path(job["meeting_dir"]), job.get("audio_filename"))

    await _log.info(
        "ingestion.transcription.started",
        message=f"job #{job_id} ({job['meeting_date']}) diarization + transcription",
    )

    async def progress(stage: str, note: str) -> None:
        await jobs_repo.set_stage(pool, job_id, stage=stage, progress_note=note)

    try:
        await stages.run_transcription_phase(
            paths, sequential=sequential, progress=progress
        )
    except Exception as e:
        await _handle_failure(pool, job, e, requeue_status="pending", max_retries=max_retries)
        return

    speaker_count = stages.count_speakers(paths.speakers)
    await jobs_repo.mark_awaiting_review(pool, job_id, speaker_count=speaker_count)
    await _log.info(
        "ingestion.awaiting_review",
        message=f"job #{job_id} transcribed ({speaker_count} speakers) — awaiting review",
        metadata={"speaker_count": speaker_count},
    )


# ─────────────────────────────────────────────────────────────
# Phase B: merge → analyze → polish → (index) → completed
# ─────────────────────────────────────────────────────────────

async def _run_analysis(
    pool: AsyncConnectionPool,
    job: dict[str, Any],
    *,
    max_retries: int,
    auto_index: bool,
) -> None:
    job_id = job["id"]
    meeting_dir = Path(job["meeting_dir"])
    paths = resolve_paths(meeting_dir, job.get("audio_filename"))

    await _log.info(
        "ingestion.analysis.started",
        message=f"job #{job_id} ({job['meeting_date']}) merge → analyze → polish",
    )

    async def progress(stage: str, note: str) -> None:
        await jobs_repo.set_stage(pool, job_id, stage=stage, progress_note=note)

    try:
        await stages.run_analysis_phase(
            paths, polish_date=_polish_date(job["meeting_date"]), progress=progress
        )

        if auto_index:
            await jobs_repo.mark_indexing(pool, job_id)
            await stages.run_index(meeting_dir, progress=progress)
            await jobs_repo.mark_completed(pool, job_id, indexed=True)
        else:
            await jobs_repo.mark_completed(pool, job_id, indexed=False)
    except Exception as e:
        await _handle_failure(
            pool, job, e, requeue_status="queued_analysis", max_retries=max_retries
        )
        return

    await _log.info(
        "ingestion.completed",
        message=f"job #{job_id} ({job['meeting_date']}) done (indexed={auto_index})",
        metadata={"indexed": auto_index, "polished": str(paths.polished)},
    )


# ─────────────────────────────────────────────────────────────
# Failure handling
# ─────────────────────────────────────────────────────────────

async def _handle_failure(
    pool: AsyncConnectionPool,
    job: dict[str, Any],
    exc: Exception,
    *,
    requeue_status: str,
    max_retries: int,
) -> None:
    """Record the failure, then requeue (if under cap) or give up permanently."""
    job_id = job["id"]
    tb = traceback.format_exc()

    retry_count = await jobs_repo.mark_failed(
        pool,
        job_id,
        error_message=f"{type(exc).__name__}: {exc}",
        error_traceback=tb,
        increment_retry=True,
    )

    await _log.record_error(
        error_type=type(exc).__name__,
        error_message=str(exc),
        traceback=tb,
        metadata={
            "job_id": job_id,
            "meeting_date": job.get("meeting_date"),
            "retry_count": retry_count,
            "max_retries": max_retries,
            "requeue_status": requeue_status,
        },
    )

    if retry_count < max_retries:
        await jobs_repo.requeue(pool, job_id, to_status=requeue_status)
        await _log.warn(
            "ingestion.requeued",
            message=f"job #{job_id} failed (attempt {retry_count}/{max_retries}), "
                    f"requeued → {requeue_status}",
        )
        return

    await _log.error(
        "ingestion.gave_up",
        message=f"job #{job_id} failed permanently after {retry_count} attempts",
    )
