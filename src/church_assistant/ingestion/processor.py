"""
Ingestion-job processor — run one dequeued job to its next checkpoint.

fetch_next_runnable() hands us a job already moved to an in-flight status:
    'transcribing' → run diarization + transcription, then pause for review
                     (→ awaiting_review)
    'analyzing'    → run merge → analyze → polish, then (auto-)index
                     (→ indexing → completed)

On failure, record it and requeue to the phase's runnable status if under the
retry cap, else leave it permanently 'failed'.

The worker is shared across churches (it claims jobs cross-tenant via a
SECURITY DEFINER function), so every job's tenant comes from the claimed row.
That tenant then decides three things: where the meeting folder itself is
(paths.meeting_dir_for — the job row's stored path is not trusted, so web and
worker may run on different machines), which voice-profile library diarization
matches against, and which Qdrant collections the result is indexed into.

On a split deployment (docs/vps_deploy.md) the artifacts live on the control
plane, so each phase pulls the folder before it starts and pushes it back when
it ends — see ingestion/artifact_sync.py. Those calls are inside the try, so a
tunnel that drops mid-copy fails the job into the same retry path as a stage
that failed, rather than leaving a half-written folder behind. With everything
on one machine they are no-ops.

Never raises — the caller's loop must survive any single job.
"""

from __future__ import annotations

import traceback
from typing import Any

from psycopg_pool import AsyncConnectionPool

from church_assistant.db import ingestion_jobs_repo as jobs_repo
from church_assistant.db import tenants_repo
from church_assistant.ingestion import artifact_sync
from church_assistant.ingestion import protocol_draft, stages
from church_assistant.ingestion.paths import meeting_dir_for
from church_assistant.ingestion.paths import resolve as resolve_paths
from church_assistant.ingestion.paths import resolve_for
from church_assistant.shared import tenant_paths
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
            tenant_id=job.get("tenant_id"),
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
    tenant_id = job["tenant_id"]
    tenant_slug = await tenants_repo.get_slug(pool, tenant_id)
    paths = resolve_for(tenant_slug, job["meeting_date"], job.get("audio_filename"))
    profiles_dir = tenant_paths.paths_for(tenant_slug).voice_profiles

    await _log.info(
        "ingestion.transcription.started",
        message=f"job #{job_id} ({job['meeting_date']}) diarization + transcription",
        tenant_id=tenant_id,
    )

    async def progress(stage: str, note: str) -> None:
        await jobs_repo.set_stage(pool, tenant_id, job_id, stage=stage, progress_note=note)

    try:
        # Bring the folder here first — on a split deployment this is where the
        # audio actually arrives, and the profile library has to be current or
        # diarization matches against fingerprints the web has since added to.
        # A no-op when everything lives on one machine.
        await progress("sync", "Отримую аудіо та голосові профілі")
        await artifact_sync.pull_meeting(paths.meeting_dir)
        await artifact_sync.pull_voice_profiles(profiles_dir)

        await stages.run_transcription_phase(
            paths,
            profiles_dir=profiles_dir,
            sequential=sequential,
            progress=progress,
        )

        # Push BEFORE awaiting_review: the reviewer opens speakers.json on the
        # web, so it has to be over there before the job says it is ready.
        await progress("sync", "Відправляю транскрипт")
        await artifact_sync.push_meeting(paths.meeting_dir)
    except Exception as e:
        await _handle_failure(pool, job, e, requeue_status="pending", max_retries=max_retries)
        return

    speaker_count = stages.count_speakers(paths.speakers)
    await jobs_repo.mark_awaiting_review(pool, tenant_id, job_id, speaker_count=speaker_count)
    await _log.info(
        "ingestion.awaiting_review",
        message=f"job #{job_id} transcribed ({speaker_count} speakers) — awaiting review",
        metadata={"speaker_count": speaker_count},
        tenant_id=tenant_id,
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
    tenant_id = job["tenant_id"]
    tenant_slug = await tenants_repo.get_slug(pool, tenant_id)
    meeting_dir = meeting_dir_for(tenant_slug, job["meeting_date"])
    paths = resolve_paths(meeting_dir, job.get("audio_filename"))

    await _log.info(
        "ingestion.analysis.started",
        message=f"job #{job_id} ({job['meeting_date']}) merge → analyze → polish",
        tenant_id=tenant_id,
    )

    async def progress(stage: str, note: str) -> None:
        await jobs_repo.set_stage(pool, tenant_id, job_id, stage=stage, progress_note=note)

    # force=True for a speakers re-edit: regenerate artifacts in place so the
    # corrected names propagate to стенограма, protocol, and the Qdrant index.
    force = bool(job.get("force_reprocess"))

    try:
        # Pull again: the review just happened on the web, so speakers.json here
        # is the pre-review copy. Analysing it would put the old names into the
        # protocol and quietly undo the correction someone just made.
        await progress("sync", "Отримую правки спікерів")
        await artifact_sync.pull_meeting(meeting_dir)

        await stages.run_analysis_phase(
            paths,
            polish_date=_polish_date(job["meeting_date"]),
            progress=progress,
            force=force,
        )

        # Push before indexing: index_meeting reads the artifacts, and if this
        # fails the retry should re-run against a folder the web can already show.
        await progress("sync", "Відправляю протокол")
        await artifact_sync.push_meeting(meeting_dir)

        if auto_index:
            await jobs_repo.mark_indexing(pool, tenant_id, job_id)
            await stages.run_index(
                meeting_dir, tenant_slug=tenant_slug, progress=progress, force=force
            )
            await jobs_repo.mark_completed(pool, tenant_id, job_id, indexed=True)
        else:
            await jobs_repo.mark_completed(pool, tenant_id, job_id, indexed=False)
    except Exception as e:
        await _handle_failure(
            pool, job, e, requeue_status="queued_analysis", max_retries=max_retries
        )
        return

    await _log.info(
        "ingestion.completed",
        message=f"job #{job_id} ({job['meeting_date']}) done (indexed={auto_index})",
        metadata={"indexed": auto_index, "polished": str(paths.polished)},
        tenant_id=tenant_id,
    )

    # ── "Слухали", drafted from the topics that now exist ────────
    #
    # AFTER mark_completed and in its own try, deliberately. The meeting is
    # processed and usable at this point; a draft is a convenience laid on top.
    # Failing the job over it would throw away three hours of transcription and
    # analysis to save the chair some typing — and the retry would redo all of
    # it. The chair writes "Слухали" by hand either way if this is quiet.
    try:
        report = await protocol_draft.draft_heard(
            pool, tenant_id, str(job["meeting_date"]), meeting_dir,
        )
        if report:
            await progress("protocol", "Чернетка «Слухали» готова")
            # Only after "Слухали" exists — there is nothing to conclude from
            # otherwise, and the second pass reads what the first one wrote.
            decided = await protocol_draft.draft_decisions(
                pool, tenant_id, str(job["meeting_date"]))
            if decided:
                await progress("protocol", "Чернетка «Вирішили» готова")
    except Exception as e:
        await _log.warn(
            "protocol.draft_failed",
            message=f"job #{job_id} ({job['meeting_date']}): {type(e).__name__}: {e}",
            tenant_id=tenant_id,
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
    tenant_id = job["tenant_id"]
    tb = traceback.format_exc()

    retry_count = await jobs_repo.mark_failed(
        pool,
        tenant_id,
        job_id,
        error_message=f"{type(exc).__name__}: {exc}",
        error_traceback=tb,
        increment_retry=True,
    )

    await _log.record_error(
        error_type=type(exc).__name__,
        error_message=str(exc),
        traceback=tb,
        tenant_id=tenant_id,
        metadata={
            "job_id": job_id,
            "meeting_date": job.get("meeting_date"),
            "retry_count": retry_count,
            "max_retries": max_retries,
            "requeue_status": requeue_status,
        },
    )

    if retry_count < max_retries:
        await jobs_repo.requeue(pool, tenant_id, job_id, to_status=requeue_status)
        await _log.warn(
            "ingestion.requeued",
            message=f"job #{job_id} failed (attempt {retry_count}/{max_retries}), "
                    f"requeued → {requeue_status}",
            tenant_id=tenant_id,
        )
        return

    await _log.error(
        "ingestion.gave_up",
        message=f"job #{job_id} failed permanently after {retry_count} attempts",
        tenant_id=tenant_id,
    )
