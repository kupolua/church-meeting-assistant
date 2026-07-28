"""
Meetings routes:
    GET  /meetings/{date}           — meeting detail page (attendees + topics + стенограма)
    GET  /meetings/{date}/audio     — stream the meeting recording (HTTP Range support)
    GET  /meetings/{date}/speakers  — edit speaker→name mapping for a processed meeting
    POST /meetings/{date}/speakers  — save speakers.json + queue a full re-run
"""

from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from church_assistant.db import ingestion_jobs_repo as jobs_repo
from church_assistant.db.connection import get_pool
from church_assistant.ingestion import speakers as speakers_util
from church_assistant.ingestion.paths import resolve as resolve_paths
from church_assistant.shared import meetings_index
from church_assistant.shared.logger import Logger
from church_assistant.web.main import templates


router = APIRouter(prefix="/meetings")

_logger = Logger(process="web")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Browser-friendly media types (mimetypes guesses e.g. 'audio/mp4a-latm' for
# .m4a, which several browsers refuse to play in <audio>).
_AUDIO_MEDIA_TYPES = {
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".webm": "audio/webm",
}


def _audio_media_type(path: Path) -> str:
    """Pick a browser-friendly media type for an audio file."""
    return _AUDIO_MEDIA_TYPES.get(
        path.suffix.lower(),
        mimetypes.guess_type(path.name)[0] or "application/octet-stream",
    )


def _find_audio(date: str) -> Optional[Path]:
    """Locate data/meetings/<date>/audio.* (None if the date is bad or no file)."""
    if not _DATE_RE.match(date):
        return None
    folder = meetings_index.DATA_MEETINGS / date
    if not folder.is_dir():
        return None
    matches = sorted(folder.glob("audio.*"))
    return matches[0] if matches else None


@router.get("/{date}", response_class=HTMLResponse)
async def meeting_detail(request: Request, date: str):
    """Render meeting detail page for a given date (YYYY-MM-DD)."""
    detail = meetings_index.load_detail(date)
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail=f"Meeting {date!r} not found",
        )

    summaries = meetings_index.list_all_summaries()

    # If a speakers re-edit is currently re-running, surface it (the page still
    # shows the old names until the new protocol is ready).
    pool = await get_pool()
    active_job = await jobs_repo.get_by_date(pool, date)
    reprocessing = (
        active_job is not None and active_job["status"] in jobs_repo.ACTIVE_STATUSES
    )

    return templates.TemplateResponse(
        request,
        "meeting_detail.html",
        {
            "detail": detail,
            "meetings": summaries,
            "current_date": date,
            "has_audio": _find_audio(date) is not None,
            "reprocessing": reprocessing,
            "reprocess_job": active_job if reprocessing else None,
        },
    )


@router.get("/{date}/audio")
async def meeting_audio(date: str):
    """
    Serve the meeting recording.

    Starlette's FileResponse handles HTTP Range natively (async file I/O, proper
    client-disconnect handling), returning 206 for range requests. That lets the
    browser's <audio> element seek to any timestamp — the basis for the clickable
    timestamps in topics and the стенограма — without a hand-rolled streamer that
    would tie up threadpool workers on every seek.
    """
    audio_path = _find_audio(date)
    if audio_path is None or not audio_path.exists():
        raise HTTPException(status_code=404, detail="Audio not found")

    return FileResponse(audio_path, media_type=_audio_media_type(audio_path))


# ─────────────────────────────────────────────────────────────
# Speaker editor for an already-processed meeting
# ─────────────────────────────────────────────────────────────

def _meeting_folder(date: str) -> Optional[Path]:
    """Return the meeting folder if the date is valid and the folder exists."""
    if not _DATE_RE.match(date):
        return None
    folder = meetings_index.DATA_MEETINGS / date
    return folder if folder.is_dir() else None


@router.get("/{date}/speakers", response_class=HTMLResponse)
async def edit_speakers(request: Request, date: str):
    """
    Edit the speaker→name mapping (speakers.json) of a processed meeting.

    Same review UI as the ingestion pause (talk-time hints, audio playback), but
    standalone: reachable from the meeting's Учасники section. Saving triggers a
    full re-run so corrected names propagate everywhere.
    """
    folder = _meeting_folder(date)
    if folder is None:
        raise HTTPException(status_code=404, detail=f"Meeting {date!r} not found")

    paths = resolve_paths(folder)
    if not paths.speakers.exists():
        return RedirectResponse(
            f"/meetings/{date}?error=speakers.json+відсутній", status_code=303
        )

    meta, mapping = speakers_util.load_speakers(paths.speakers)
    stats = speakers_util.rttm_speaker_stats(paths.rttm)
    rows = speakers_util.build_review_rows(meta, mapping, stats)

    return templates.TemplateResponse(
        request,
        "ingest_speakers.html",
        {
            "page_title": f"Редагування голосів {date}",
            "header": f"🎙️ Редагування голосів — {date}",
            "subtitle": "Зустріч",
            "rows": rows,
            "n_flagged": sum(1 for r in rows if r["flag"]),
            "has_audio": _find_audio(date) is not None,
            "audio_src": f"/meetings/{date}/audio",
            "form_action": f"/meetings/{date}/speakers",
            "submit_label": "💾 Зберегти та перезібрати зустріч",
            "back_url": f"/meetings/{date}",
            "help_tail": (
                "Після збереження зустріч ПЕРЕЗБИРАЄТЬСЯ з новими іменами "
                "(merge → аналіз Gemma → протокол → індекс) — це триває довго й "
                "потребує запущеного ingestion-worker. Протокол лишається доступним, "
                "поки готується новий."
            ),
            "meetings": meetings_index.list_all_summaries(),
        },
    )


@router.post("/{date}/speakers", response_class=HTMLResponse)
async def save_speakers(request: Request, date: str):
    """
    Save edited speaker names and queue a full re-run of the meeting.

    Writes speakers.json (preserving _meta), then enqueues an ingestion job at
    'queued_analysis' with force_reprocess=TRUE. The worker regenerates the
    стенограма, protocol, and Qdrant index in place with the corrected names.
    """
    folder = _meeting_folder(date)
    if folder is None:
        raise HTTPException(status_code=404, detail=f"Meeting {date!r} not found")

    paths = resolve_paths(folder)
    if not paths.speakers.exists():
        return RedirectResponse(
            f"/meetings/{date}?error=speakers.json+відсутній", status_code=303
        )

    pool = await get_pool()

    # Guard: don't re-run while an ingestion job for this meeting is already active.
    existing = await jobs_repo.get_by_date(pool, date)
    if existing is not None and existing["status"] in jobs_repo.ACTIVE_STATUSES:
        return RedirectResponse(
            f"/meetings/{date}?error=Зустріч+уже+обробляється+"
            f"(job+%23{existing['id']},+{existing['status']})",
            status_code=303,
        )

    meta, mapping = speakers_util.load_speakers(paths.speakers)
    form = await request.form()
    new_mapping: dict[str, str] = {}
    for label in mapping:
        submitted = str(form.get(f"name_{label}", "")).strip()
        new_mapping[label] = submitted or label

    speakers_util.save_speakers(paths.speakers, meta, new_mapping)

    audio_path = _find_audio(date)
    job_id = await jobs_repo.enqueue_reprocess(
        pool,
        meeting_date=date,
        meeting_dir=str(folder.resolve()),
        audio_filename=audio_path.name if audio_path else None,
        speaker_count=len(new_mapping),
    )

    await _logger.info(
        "meeting.speakers_reprocess",
        message=f"meeting {date} speakers edited → full re-run queued (job #{job_id})",
        metadata={"job_id": job_id, "meeting_date": date, "speaker_count": len(new_mapping)},
    )

    return RedirectResponse(
        f"/ingest?ok=Голоси+збережено.+Зустріч+{date}+у+черзі+на+переобробку+"
        f"(job+%23{job_id}).+Стеж+за+прогресом+тут.",
        status_code=303,
    )
