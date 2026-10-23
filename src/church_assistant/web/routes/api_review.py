"""
The JSON API — speaker review (звірка голосів) from the phone.

    GET  /api/v1/meetings/{date}/speakers    the clusters, their names, talk time, where to listen
    POST /api/v1/meetings/{date}/speakers    {names: {SPEAKER_XX: "Імʼя"}} → save and continue

The same step the web offers twice: while a job is paused at awaiting_review
(the pipeline waits for names before Gemma writes anything), and later as a
re-edit of a processed meeting (which queues a full re-run). This module
tells the two apart by the job's status and does what the web does in each
case — speakers_util + jobs_repo, nothing of its own.

Not here yet: adding a participant diarization never heard (the typed
time window of manual_speakers.py). The web keeps that until the app has a
place for it.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from church_assistant.db import audit_repo
from church_assistant.db import ingestion_jobs_repo as jobs_repo
from church_assistant.db import web_users_repo
from church_assistant.db.connection import get_pool
from church_assistant.ingestion import speaker_review as review
from church_assistant.ingestion import speakers as speakers_util
from church_assistant.ingestion.paths import resolve as resolve_paths
from church_assistant.shared import tenant_paths
from church_assistant.shared.logger import Logger
from church_assistant.web.routes.api import api_user
from church_assistant.web.routes.api_read import _DATE_RE
from church_assistant.web.routes.meetings import _find_audio


router = APIRouter(prefix="/api/v1")
_logger = Logger(process="web")


class NamesIn(BaseModel):
    names: dict[str, str] = Field(default_factory=dict)


def _folder(slug: str, date: str):
    if not _DATE_RE.match(date):
        raise HTTPException(status_code=404, detail="Немає такої зустрічі.")
    tp = tenant_paths.paths_for(slug)
    folder = tp.meetings / date
    if not folder.is_dir():
        raise HTTPException(status_code=404, detail="Теки зустрічі ще немає.")
    return tp, folder


@router.get("/meetings/{date}/speakers")
async def speakers(request: Request, date: str):
    user = await api_user(request)
    tp, folder = _folder(user.tenant_slug, date)
    pool = await get_pool()
    job = await jobs_repo.get_by_date(pool, user.tenant_id, date)
    paths = resolve_paths(folder, job.get("audio_filename") if job else None)
    if not paths.speakers.exists():
        raise HTTPException(status_code=409, detail="Голоси ще не розпізнано — розшифровка триває.")

    meta, mapping = speakers_util.load_speakers(paths.speakers)
    stats = speakers_util.rttm_speaker_stats(paths.rttm)
    rows = speakers_util.build_review_rows(meta, mapping, stats)

    known = set(review.list_known_names(tp.voice_profiles, {k: v for k, v in mapping.items()}))
    known |= {m["full_name"] for m in await web_users_repo.list_active(pool, user.tenant_id) if m.get("full_name")}

    status = job["status"] if job else None
    return {
        "date": date,
        "job_id": int(job["id"]) if job else None,
        "mode": "review" if status == "awaiting_review" else ("busy" if status in jobs_repo.ACTIVE_STATUSES else "edit"),
        "rows": [{
            "label": r["label"], "name": r["name"], "flag": r["flag"], "manual": r["manual"],
            "total_hms": r["total_hms"], "total_s": r["total_s"], "segments": r["segments"],
            "samples": r["samples"],
        } for r in rows],
        "known_names": sorted(known, key=str.lower),
        "has_audio": _find_audio(tp.meetings, date) is not None,
    }


@router.post("/meetings/{date}/speakers")
async def save_speakers(request: Request, date: str, body: NamesIn):
    user = await api_user(request)
    tp, folder = _folder(user.tenant_slug, date)
    pool = await get_pool()
    job = await jobs_repo.get_by_date(pool, user.tenant_id, date)
    paths = resolve_paths(folder, job.get("audio_filename") if job else None)
    if not paths.speakers.exists():
        raise HTTPException(status_code=409, detail="Голоси ще не розпізнано.")

    status = job["status"] if job else None
    if status in jobs_repo.ACTIVE_STATUSES and status != "awaiting_review":
        raise HTTPException(status_code=409, detail=f"Зустріч уже обробляється ({status}).")

    meta, mapping = speakers_util.load_speakers(paths.speakers)
    # Only labels the file knows; blank keeps the label, as the web does.
    new_mapping = {label: (body.names.get(label) or "").strip() or label for label in mapping}
    speakers_util.save_speakers(paths.speakers, meta, new_mapping)

    if status == "awaiting_review":
        await jobs_repo.mark_queued_analysis(pool, user.tenant_id, int(job["id"]), speaker_count=len(new_mapping))
        how, job_id = "review", int(job["id"])
    else:
        audio = _find_audio(tp.meetings, date)
        job_id = await jobs_repo.enqueue_reprocess(
            pool, user.tenant_id, meeting_date=date, meeting_dir=str(folder.resolve()),
            audio_filename=audio.name if audio else None, speaker_count=len(new_mapping),
        )
        how = "reprocess"

    await audit_repo.record(pool, tenant_id=user.tenant_id, action="meeting.speakers_saved", actor=user.actor,
                            resource=f"meetings/{date}", detail={"how": how, "job_id": job_id, "speakers": len(new_mapping)})
    await _logger.info("api.speakers_saved", message=f"{user.username} saved speakers for {date} ({how}, job #{job_id})",
                       tenant_id=user.tenant_id)
    return {"ok": True, "how": how, "job_id": job_id}
