"""
Ingestion routes (MVP-C): GET/POST /ingest — upload audio → protocol pipeline.

    GET  /ingest                       full page: upload form + self-polling job list
    GET  /ingest/panel                 HTMX poll target — just the job-list panel
    POST /ingest                       multipart upload (audio + date) → create job
    POST /ingest/{id}/cancel           cancel an active job
    POST /ingest/{id}/requeue          retry a failed job (at the phase it stopped)

The heavy lifting runs in a separate process (church_assistant.ingestion.main);
these routes only enqueue work and show status. The audio is copied straight
into data/meetings/<date>/audio.<ext> so the web flow and the CLI (new_meeting.py)
share the same folder layout.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from church_assistant.db import ingestion_jobs_repo as jobs_repo
from church_assistant.db.connection import get_pool
from church_assistant.ingestion import speakers as speakers_util
from church_assistant.ingestion.paths import resolve as resolve_paths
from church_assistant.shared import meetings_index
from church_assistant.shared.logger import Logger
from church_assistant.web.main import templates


router = APIRouter()

_logger = Logger(process="web")

# Project root: …/src/church_assistant/web/routes/ingest.py → parents[4]
PROJECT_ROOT = Path(__file__).resolve().parents[4]
MEETINGS_DIR = PROJECT_ROOT / "data" / "meetings"

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(-\d+)?$")
ALLOWED_AUDIO_SUFFIXES = {
    ".m4a", ".mp3", ".wav", ".aac", ".ogg", ".flac", ".opus", ".mp4", ".webm",
}


# ─────────────────────────────────────────────────────────────
# Context helpers
# ─────────────────────────────────────────────────────────────

async def _panel_context(pool: Any) -> dict[str, Any]:
    """Everything the refreshable ingestion panel needs."""
    depth = await jobs_repo.get_depth(pool)
    active = await jobs_repo.list_active(pool)
    recent = await jobs_repo.list_recent(pool, limit=20)
    # "Done" list = terminal jobs (completed/failed/cancelled), newest first.
    done = [j for j in recent if j["status"] in ("completed", "failed", "cancelled")]
    return {"depth": depth, "active": active, "done": done}


def _render_panel(request: Request, ctx: dict[str, Any]) -> HTMLResponse:
    return templates.TemplateResponse(request, "partials/ingest_panel.html", ctx)


def _requeue_target(job: dict[str, Any]) -> str:
    """
    Which runnable status a failed job should return to.

    If the human review already happened (reviewed_at set), the failure was in
    the analysis phase → retry from 'queued_analysis'. Otherwise it failed during
    transcription → retry from 'pending'.
    """
    return "queued_analysis" if job.get("reviewed_at") is not None else "pending"


# ─────────────────────────────────────────────────────────────
# Views
# ─────────────────────────────────────────────────────────────

@router.get("/ingest", response_class=HTMLResponse)
async def ingest_page(request: Request, error: Optional[str] = None, ok: Optional[str] = None):
    """Full ingestion page: upload form + self-polling job list."""
    pool = await get_pool()
    ctx = await _panel_context(pool)
    ctx["meetings"] = meetings_index.list_all_summaries()
    ctx["error"] = error
    ctx["ok"] = ok
    return templates.TemplateResponse(request, "ingest.html", ctx)


@router.get("/ingest/panel", response_class=HTMLResponse)
async def ingest_panel(request: Request):
    """HTMX poll target — returns only the refreshable panel."""
    pool = await get_pool()
    ctx = await _panel_context(pool)
    return _render_panel(request, ctx)


@router.get("/ingest/{job_id}", response_class=HTMLResponse)
async def ingest_detail(request: Request, job_id: int):
    """
    Job detail page: timeline, progress, results / error, and stage-appropriate
    actions. Reachable from the panel rows. ('panel' can't match here — job_id
    is int-typed — so the poll route above is unambiguous.)
    """
    pool = await get_pool()
    job = await jobs_repo.get_by_id(pool, job_id)
    if job is None:
        return RedirectResponse("/ingest?error=Job+не+знайдено", status_code=303)

    paths = resolve_paths(Path(job["meeting_dir"]), job.get("audio_filename"))
    return templates.TemplateResponse(
        request,
        "ingest_detail.html",
        {
            "job": job,
            "polished_exists": paths.polished.exists(),
            "meetings": meetings_index.list_all_summaries(),
        },
    )


# ─────────────────────────────────────────────────────────────
# Upload
# ─────────────────────────────────────────────────────────────

@router.post("/ingest", response_class=HTMLResponse)
async def ingest_upload(
    request: Request,
    date: str = Form(...),
    audio: UploadFile = File(...),
):
    """
    Accept an audio upload for a given meeting date, create the folder, copy the
    file in, and enqueue a pending ingestion job. Redirects back to /ingest.
    """
    date = date.strip()

    # ─── Validate date ───────────────────────────────────────
    if not DATE_RE.match(date):
        return RedirectResponse(
            f"/ingest?error=Дата+має+бути+у+форматі+YYYY-MM-DD", status_code=303
        )

    # ─── Validate audio ──────────────────────────────────────
    src_name = audio.filename or "audio"
    suffix = Path(src_name).suffix.lower()
    if suffix not in ALLOWED_AUDIO_SUFFIXES:
        allowed = ", ".join(sorted(ALLOWED_AUDIO_SUFFIXES))
        return RedirectResponse(
            f"/ingest?error=Непідтримуваний+формат+({suffix or 'без+розширення'}).+"
            f"Дозволені:+{allowed.replace(' ', '')}",
            status_code=303,
        )

    pool = await get_pool()

    # ─── Don't clobber an existing job for this date ─────────
    existing = await jobs_repo.get_by_date(pool, date)
    if existing is not None:
        return RedirectResponse(
            f"/ingest?error=Для+{date}+вже+є+job+(%23{existing['id']},+"
            f"статус:+{existing['status']}).+Скасуйте+його+перед+повторним+завантаженням.",
            status_code=303,
        )

    # ─── Create folder + copy audio ──────────────────────────
    meeting_dir = MEETINGS_DIR / date
    meeting_dir.mkdir(parents=True, exist_ok=True)
    audio_filename = f"audio{suffix}"
    dest_path = meeting_dir / audio_filename

    try:
        with dest_path.open("wb") as out:
            shutil.copyfileobj(audio.file, out, length=1024 * 1024)
    finally:
        await audio.close()

    size_mb = dest_path.stat().st_size / 1024 / 1024

    # ─── Enqueue job ─────────────────────────────────────────
    job_id = await jobs_repo.insert_job(
        pool,
        meeting_date=date,
        meeting_dir=str(meeting_dir),
        original_filename=src_name,
        audio_filename=audio_filename,
    )

    await _logger.info(
        "ingestion.uploaded",
        message=f"job #{job_id} ({date}) audio uploaded ({size_mb:.1f} MB): {src_name}",
        metadata={"job_id": job_id, "size_mb": round(size_mb, 1)},
    )

    return RedirectResponse(
        f"/ingest?ok=Завантажено+{size_mb:.0f}+МБ.+Job+%23{job_id}+у+черзі+"
        f"(worker+почне+діаризацію).",
        status_code=303,
    )


# ─────────────────────────────────────────────────────────────
# Actions (POST → mutate → return refreshed panel)
# ─────────────────────────────────────────────────────────────

def _is_htmx(request: Request) -> bool:
    """True when the request came from HTMX (list panel) vs a plain form (detail page)."""
    return request.headers.get("HX-Request") == "true"


@router.post("/ingest/{job_id}/cancel", response_class=HTMLResponse)
async def cancel_job(request: Request, job_id: int):
    """Cancel an active job (stops it being picked up; in-flight step still finishes)."""
    pool = await get_pool()
    await jobs_repo.cancel(pool, job_id)
    if _is_htmx(request):
        return _render_panel(request, await _panel_context(pool))
    return RedirectResponse(f"/ingest/{job_id}", status_code=303)


@router.post("/ingest/{job_id}/requeue", response_class=HTMLResponse)
async def requeue_job(request: Request, job_id: int):
    """Retry a failed job from the phase it stopped at."""
    pool = await get_pool()
    job = await jobs_repo.get_by_id(pool, job_id)
    if job is not None and job["status"] == "failed":
        await jobs_repo.requeue(pool, job_id, to_status=_requeue_target(job))
    if _is_htmx(request):
        return _render_panel(request, await _panel_context(pool))
    return RedirectResponse(f"/ingest/{job_id}", status_code=303)


# ─────────────────────────────────────────────────────────────
# Speakers review editor (human-in-the-loop pause)
# ─────────────────────────────────────────────────────────────

@router.get("/ingest/{job_id}/speakers", response_class=HTMLResponse)
async def speakers_editor(request: Request, job_id: int):
    """
    Edit speakers.json for a job paused at 'awaiting_review'.

    Shows one row per SPEAKER_XX with talk-time hints and weak-match / no-match
    flags, so the human can confirm or fix each name before analysis resumes.
    """
    pool = await get_pool()
    job = await jobs_repo.get_by_id(pool, job_id)
    if job is None:
        return RedirectResponse("/ingest?error=Job+не+знайдено", status_code=303)
    if job["status"] != "awaiting_review":
        return RedirectResponse(
            f"/ingest?error=Job+%23{job_id}+не+очікує+ревʼю+(статус:+{job['status']})",
            status_code=303,
        )

    paths = resolve_paths(Path(job["meeting_dir"]), job.get("audio_filename"))
    if not paths.speakers.exists():
        return RedirectResponse(
            f"/ingest?error=speakers.json+відсутній+для+job+%23{job_id}", status_code=303
        )

    meta, mapping = speakers_util.load_speakers(paths.speakers)
    stats = speakers_util.rttm_speaker_stats(paths.rttm)
    rows = speakers_util.build_review_rows(meta, mapping, stats)

    return templates.TemplateResponse(
        request,
        "ingest_speakers.html",
        {
            "job": job,
            "rows": rows,
            "n_flagged": sum(1 for r in rows if r["flag"]),
            "meetings": meetings_index.list_all_summaries(),
        },
    )


@router.post("/ingest/{job_id}/speakers", response_class=HTMLResponse)
async def speakers_save(request: Request, job_id: int):
    """
    Save the edited speaker names and resume the pipeline (→ queued_analysis).

    Reads one form field per known speaker label (`name_SPEAKER_XX`); values are
    taken verbatim as the final names. _meta is preserved. Empty fields fall back
    to the label so no speaker is silently dropped.
    """
    pool = await get_pool()
    job = await jobs_repo.get_by_id(pool, job_id)
    if job is None:
        return RedirectResponse("/ingest?error=Job+не+знайдено", status_code=303)
    if job["status"] != "awaiting_review":
        return RedirectResponse(
            f"/ingest?error=Job+%23{job_id}+не+очікує+ревʼю+(статус:+{job['status']})",
            status_code=303,
        )

    paths = resolve_paths(Path(job["meeting_dir"]), job.get("audio_filename"))
    if not paths.speakers.exists():
        return RedirectResponse(
            f"/ingest?error=speakers.json+відсутній+для+job+%23{job_id}", status_code=303
        )

    meta, mapping = speakers_util.load_speakers(paths.speakers)
    form = await request.form()

    # Only accept labels that already exist in the file (ignore stray fields).
    new_mapping: dict[str, str] = {}
    for label in mapping:
        submitted = str(form.get(f"name_{label}", "")).strip()
        new_mapping[label] = submitted or label  # keep the label if left blank

    speakers_util.save_speakers(paths.speakers, meta, new_mapping)

    await jobs_repo.mark_queued_analysis(pool, job_id, speaker_count=len(new_mapping))
    await _logger.info(
        "ingestion.review_submitted",
        message=f"job #{job_id} ({job['meeting_date']}) speakers reviewed — queued for analysis",
        metadata={"job_id": job_id, "speaker_count": len(new_mapping)},
    )

    return RedirectResponse(
        f"/ingest?ok=Спікерів+збережено.+Job+%23{job_id}+у+черзі+на+аналіз.",
        status_code=303,
    )
