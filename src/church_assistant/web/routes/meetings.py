"""
Meetings routes:
    GET  /meetings/{date}           — meeting detail page (attendees + topics + стенограма)
    GET  /meetings/{date}/topics.pdf — the Теми section as a printable PDF
    GET  /meetings/{date}/audio     — stream the meeting recording (HTTP Range support)
    GET  /meetings/{date}/speakers  — edit speaker→name mapping for a processed meeting
    POST /meetings/{date}/speakers  — save speakers.json + queue a full re-run
"""

from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from typing import Optional

from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from church_assistant.db import ingestion_jobs_repo as jobs_repo
from church_assistant.db.connection import get_pool
from church_assistant.ingestion import speaker_review as review
from church_assistant.ingestion import speakers as speakers_util
from church_assistant.ingestion.paths import resolve as resolve_paths
from church_assistant.shared import meetings_index, pdf_export, tenant_paths
from church_assistant.shared.logger import Logger
from church_assistant.web.main import templates
from church_assistant.web.tenant import current_tenant, current_tenant_slug


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


def _tenant_paths(request: Request) -> tenant_paths.TenantPaths:
    """This request's tenant's artifact folders (meetings + voice profiles)."""
    return tenant_paths.paths_for(current_tenant_slug(request))


def _find_audio(meetings_dir: Path, date: str) -> Optional[Path]:
    """Locate <meetings>/<date>/audio.* (None if the date is bad or no file)."""
    if not _DATE_RE.match(date):
        return None
    folder = meetings_dir / date
    if not folder.is_dir():
        return None
    matches = sorted(folder.glob("audio.*"))
    return matches[0] if matches else None


@router.get("/{date}", response_class=HTMLResponse)
async def meeting_detail(request: Request, date: str):
    """Render meeting detail page for a given date (YYYY-MM-DD)."""
    tpaths = _tenant_paths(request)
    detail = meetings_index.load_detail(tpaths.meetings, date)
    if detail is None:
        # Also the answer when the meeting belongs to ANOTHER church: the
        # lookup never leaves this tenant's folder, so it simply isn't there.
        raise HTTPException(
            status_code=404,
            detail=f"Meeting {date!r} not found",
        )

    summaries = meetings_index.list_all_summaries(tpaths.meetings)

    # If a speakers re-edit is currently re-running, surface it (the page still
    # shows the old names until the new protocol is ready).
    pool = await get_pool()
    tenant_id = current_tenant(request)
    active_job = await jobs_repo.get_by_date(pool, tenant_id, date)
    reprocessing = (
        active_job is not None and active_job["status"] in jobs_repo.ACTIVE_STATUSES
    )

    # Names for the "change speaker" picker (existing profiles + this meeting's).
    speakers_json = detail.folder / "speakers.json"
    known_names: list[str] = []
    if speakers_json.exists():
        try:
            import json as _json
            sp = _json.loads(speakers_json.read_text(encoding="utf-8"))
            speaker_map = {k: v for k, v in sp.items() if not k.startswith("_")}
            known_names = review.list_known_names(tpaths.voice_profiles, speaker_map)
        except (OSError, ValueError):
            known_names = review.list_known_names(tpaths.voice_profiles, {})

    return templates.TemplateResponse(
        request,
        "meeting_detail.html",
        {
            "detail": detail,
            "meetings": summaries,
            "current_date": date,
            "has_audio": _find_audio(tpaths.meetings, date) is not None,
            "reprocessing": reprocessing,
            "reprocess_job": active_job if reprocessing else None,
            "known_names": known_names,
            "changes": review.load_changes(detail.folder),
        },
    )


@router.get("/{date}/topics.pdf")
async def meeting_topics_pdf(request: Request, date: str):
    """
    The Розглянуті питання section as a printable PDF.

    Built from the same parsed topics the page renders, so the document cannot
    drift from what the user sees. Timestamps are dropped — they exist to seek
    the recording, which paper cannot do.
    """
    detail = meetings_index.load_detail(_tenant_paths(request).meetings, date)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Meeting {date!r} not found")

    try:
        pdf = pdf_export.build_topics_pdf(
            detail.date, detail.topics, detail.attendees
        )
    except pdf_export.FontNotFound as e:
        # A server without a Cyrillic font would otherwise emit a protocol of
        # black boxes; say what to install instead.
        raise HTTPException(status_code=503, detail=str(e)) from e

    filename = pdf_export.pdf_filename(detail.date)
    # The name is Cyrillic, which a bare filename= cannot carry. RFC 5987
    # filename* does; the ASCII filename= stays as a fallback for old clients.
    ascii_name = f"meeting-{detail.date}-topics.pdf"
    disposition = (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(filename)}"
    )
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": disposition},
    )


@router.get("/{date}/audio")
async def meeting_audio(request: Request, date: str):
    """
    Serve the meeting recording — resolved inside the caller's tenant folder,
    so a guessed date from another church yields 404, not their audio.

    Starlette's FileResponse handles HTTP Range natively (async file I/O, proper
    client-disconnect handling), returning 206 for range requests. That lets the
    browser's <audio> element seek to any timestamp — the basis for the clickable
    timestamps in topics and the стенограма — without a hand-rolled streamer that
    would tie up threadpool workers on every seek.
    """
    audio_path = _find_audio(_tenant_paths(request).meetings, date)
    if audio_path is None or not audio_path.exists():
        raise HTTPException(status_code=404, detail="Audio not found")

    return FileResponse(audio_path, media_type=_audio_media_type(audio_path))


# ─────────────────────────────────────────────────────────────
# Speaker editor for an already-processed meeting
# ─────────────────────────────────────────────────────────────

def _meeting_folder(meetings_dir: Path, date: str) -> Optional[Path]:
    """Return the meeting folder if the date is valid and the folder exists."""
    if not _DATE_RE.match(date):
        return None
    folder = meetings_dir / date
    return folder if folder.is_dir() else None


@router.get("/{date}/speakers", response_class=HTMLResponse)
async def edit_speakers(request: Request, date: str):
    """
    Edit the speaker→name mapping (speakers.json) of a processed meeting.

    Same review UI as the ingestion pause (talk-time hints, audio playback), but
    standalone: reachable from the meeting's Учасники section. Saving triggers a
    full re-run so corrected names propagate everywhere.
    """
    tpaths = _tenant_paths(request)
    folder = _meeting_folder(tpaths.meetings, date)
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
            "has_audio": _find_audio(tpaths.meetings, date) is not None,
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
            "meetings": meetings_index.list_all_summaries(tpaths.meetings),
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
    tpaths = _tenant_paths(request)
    folder = _meeting_folder(tpaths.meetings, date)
    if folder is None:
        raise HTTPException(status_code=404, detail=f"Meeting {date!r} not found")

    paths = resolve_paths(folder)
    if not paths.speakers.exists():
        return RedirectResponse(
            f"/meetings/{date}?error=speakers.json+відсутній", status_code=303
        )

    pool = await get_pool()
    tenant_id = current_tenant(request)

    # Guard: don't re-run while an ingestion job for this meeting is already active.
    existing = await jobs_repo.get_by_date(pool, tenant_id, date)
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

    audio_path = _find_audio(tpaths.meetings, date)
    job_id = await jobs_repo.enqueue_reprocess(
        pool,
        tenant_id,
        meeting_date=date,
        meeting_dir=str(folder.resolve()),
        audio_filename=audio_path.name if audio_path else None,
        speaker_count=len(new_mapping),
    )

    await _logger.info(
        "meeting.speakers_reprocess",
        message=f"meeting {date} speakers edited → full re-run queued (job #{job_id})",
        metadata={"job_id": job_id, "meeting_date": date, "speaker_count": len(new_mapping)},
        tenant_id=tenant_id,
    )

    return RedirectResponse(
        f"/ingest?ok=Голоси+збережено.+Зустріч+{date}+у+черзі+на+переобробку+"
        f"(job+%23{job_id}).+Стеж+за+прогресом+тут.",
        status_code=303,
    )


# ─────────────────────────────────────────────────────────────
# Per-cluster speaker reassignment from the transcript
# ─────────────────────────────────────────────────────────────

def _review_panel(request: Request, date: str, folder: Path) -> HTMLResponse:
    """Render the pending-changes panel (HTMX target)."""
    return templates.TemplateResponse(
        request,
        "partials/speaker_review_panel.html",
        {"date": date, "changes": review.load_changes(folder)},
    )


@router.get("/{date}/speaker-changes", response_class=HTMLResponse)
async def speaker_changes_panel(request: Request, date: str):
    """Return just the pending-changes panel (HTMX poll/refresh target)."""
    folder = _meeting_folder(_tenant_paths(request).meetings, date)
    if folder is None:
        raise HTTPException(status_code=404, detail=f"Meeting {date!r} not found")
    return _review_panel(request, date, folder)


@router.post("/{date}/speaker-change", response_class=HTMLResponse)
async def speaker_change(
    request: Request,
    date: str,
    label: str = Form(...),
    new_name: str = Form(...),
):
    """
    Queue a per-cluster speaker reassignment (from a transcript "change speaker"
    link). is_new is inferred: a name without an existing voice profile is a new
    participant that will be fingerprinted on "run analysis".
    """
    tpaths = _tenant_paths(request)
    folder = _meeting_folder(tpaths.meetings, date)
    if folder is None:
        raise HTTPException(status_code=404, detail=f"Meeting {date!r} not found")

    label = label.strip()
    new_name = new_name.strip()
    if label and new_name:
        # "New participant" is judged against THIS church's profiles: a person
        # already fingerprinted in another church is still new here.
        is_new = not review.has_profile(tpaths.voice_profiles, new_name)
        review.upsert_change(folder, label=label, new_name=new_name, is_new=is_new)
    return _review_panel(request, date, folder)


@router.post("/{date}/speaker-change/{label}/remove", response_class=HTMLResponse)
async def speaker_change_remove(request: Request, date: str, label: str):
    """Drop one pending change."""
    folder = _meeting_folder(_tenant_paths(request).meetings, date)
    if folder is None:
        raise HTTPException(status_code=404, detail=f"Meeting {date!r} not found")
    review.remove_change(folder, label)
    return _review_panel(request, date, folder)


@router.post("/{date}/run-analysis", response_class=HTMLResponse)
async def run_analysis(request: Request, date: str):
    """
    The separate "🔁 Запустити аналіз" button. For every queued change:
      - relabel the cluster in speakers.json (propagates on re-run),
      - for a NEW participant, save a voice profile (.npy) from the cluster
        embedding (so future meetings recognize them),
    then queue a full force re-run and clear the draft.
    """
    tpaths = _tenant_paths(request)
    folder = _meeting_folder(tpaths.meetings, date)
    if folder is None:
        raise HTTPException(status_code=404, detail=f"Meeting {date!r} not found")

    changes = review.load_changes(folder)
    if not changes:
        return RedirectResponse(
            f"/meetings/{date}?error=Немає+змін+для+аналізу", status_code=303
        )

    paths = resolve_paths(folder)
    if not paths.speakers.exists():
        return RedirectResponse(
            f"/meetings/{date}?error=speakers.json+відсутній", status_code=303
        )

    pool = await get_pool()
    tenant_id = current_tenant(request)
    existing = await jobs_repo.get_by_date(pool, tenant_id, date)
    if existing is not None and existing["status"] in jobs_repo.ACTIVE_STATUSES:
        return RedirectResponse(
            f"/meetings/{date}?error=Зустріч+уже+обробляється+(job+%23{existing['id']})",
            status_code=303,
        )

    audio_path = _find_audio(tpaths.meetings, date)
    audio_name = audio_path.name if audio_path else None

    meta, mapping = speakers_util.load_speakers(paths.speakers)
    new_profiles = 0
    profile_warnings: list[str] = []
    for c in changes:
        label = str(c.get("label", "")).strip()
        name = str(c.get("new_name", "")).strip()
        if not label or not name:
            continue
        mapping[label] = name                       # relabel cluster
        if c.get("is_new"):
            ok, msg = review.save_voice_profile_from_cluster(
                folder,
                tpaths.voice_profiles,
                label=label, name=name, audio_filename=audio_name,
            )
            if ok:
                new_profiles += 1
            else:
                profile_warnings.append(msg)

    speakers_util.save_speakers(paths.speakers, meta, mapping)
    review.clear_draft(folder)

    job_id = await jobs_repo.enqueue_reprocess(
        pool,
        tenant_id,
        meeting_date=date,
        meeting_dir=str(folder.resolve()),
        audio_filename=audio_name,
        speaker_count=len(mapping),
    )

    await _logger.info(
        "meeting.speaker_review_run",
        message=f"meeting {date}: {len(changes)} speaker change(s), "
                f"{new_profiles} new voice profile(s) → re-run queued (job #{job_id})",
        metadata={
            "job_id": job_id, "meeting_date": date,
            "changes": len(changes), "new_profiles": new_profiles,
            "profile_warnings": profile_warnings,
        },
        tenant_id=tenant_id,
    )

    msg = f"Застосовано+змін:+{len(changes)}"
    if new_profiles:
        msg += f",+нових+голосових+профілів:+{new_profiles}"
    if profile_warnings:
        msg += f".+Увага:+{len(profile_warnings)}+профіль(ів)+не+збережено+(артефакт)"
    return RedirectResponse(
        f"/ingest?ok={msg}.+Зустріч+{date}+у+черзі+на+переобробку+(job+%23{job_id}).",
        status_code=303,
    )
