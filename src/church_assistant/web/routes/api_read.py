"""
The JSON API, step 3 — what the app reads.

    GET /api/v1/home                     counters, what needs attention, what is processing
    GET /api/v1/meetings                 every meeting: folder-backed or protocol-only
    GET /api/v1/meetings/{date}          protocol + topics + стенограма for one meeting
    GET /api/v1/meetings/{date}/audio    the recording, with HTTP Range (seek by timecode)
    GET /api/v1/search?q=                keyword search across topics

SAME SOURCES AS THE WEB, on purpose. A meeting is a folder on disk (polished.md,
annotated.md) and/or a protocol in the database (018); the web merges the two
in summaries_with_protocols() and this module calls the same function, so the
app and the browser can never disagree about which meetings exist.

An encrypted church (cloud_plan §3.1) answers with `sealed: true` and metadata
only — the same degradation the web shows — until the app holds the key, which
is a later step.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from church_assistant.db import ingestion_jobs_repo as jobs_repo
from church_assistant.db import protocols_repo
from church_assistant.db.connection import get_pool
from church_assistant.shared import meetings_index, tenant_paths
from church_assistant.shared.field_crypto import NotDecryptable
from church_assistant.web import encrypted
from church_assistant.web.routes.api import ApiUser, api_user
from church_assistant.web.routes.meetings import (
    _audio_media_type, _find_audio, summaries_with_protocols,
)


router = APIRouter(prefix="/api/v1")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# "- **Три практичні уроки** (06:21)" → text + timecode. polished.md writes the
# timecode at the end of a bullet, in parentheses; the app shows it as a chip.
_BULLET_RE = re.compile(r"^(?P<indent>\s*)-\s+(?P<text>.*?)\s*(?:\((?P<tc>\d{1,2}:\d{2}(?::\d{2})?)\))?[.;,]?\s*$")


def _meetings_dir(user: ApiUser) -> Path:
    return tenant_paths.paths_for(user.tenant_slug).meetings


def _iso(d: Any) -> Optional[str]:
    return None if d is None else (d.isoformat() if hasattr(d, "isoformat") else str(d))


def _protocol_meta(p: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if p is None:
        return None
    return {
        "id": int(p["id"]), "number": protocols_repo.number(p), "status": p["status"],
        "approved_at": _iso(p.get("approved_at")),
    }


def _topic(t: meetings_index.Topic) -> dict[str, Any]:
    """A topic as the app draws it: a title, a lead paragraph, and bullet lines
    with their timecodes split out."""
    lead: list[str] = []
    lines: list[dict[str, Any]] = []
    for raw in t.body.splitlines():
        if not raw.strip():
            continue
        m = _BULLET_RE.match(raw)
        if m:
            text = re.sub(r"\*\*(.+?)\*\*", r"\1", m.group("text")).strip()
            lines.append({"text": text, "tc": m.group("tc"), "level": 1 if m.group("indent") else 0})
        elif not lines:
            lead.append(raw.strip())
    return {"order": t.order, "title": t.title, "lead": " ".join(lead), "lines": lines}


def _turn(t: meetings_index.TranscriptTurn) -> dict[str, Any]:
    return {"tc": t.timestamp, "speaker": t.speaker, "text": t.text}


def _job(j: dict[str, Any]) -> dict[str, Any]:
    return {"date": _iso(j.get("meeting_date")), "status": j.get("status")}


# ─────────────────────────────────────────────────────────────

@router.get("/home")
async def home(request: Request):
    user = await api_user(request)
    pool = await get_pool()
    summaries = await summaries_with_protocols(_meetings_dir(user), pool, user.tenant_id)
    protocols = {f"{p['meeting_date']:%Y-%m-%d}": p for p in await protocols_repo.list_all(pool, user.tenant_id)}
    by_status: dict[str, int] = {"draft": 0, "review": 0, "approved": 0}
    for p in protocols.values():
        by_status[p["status"]] = by_status.get(p["status"], 0) + 1
    attention = [
        {"date": d, "protocol": _protocol_meta(p)}
        for d, p in sorted(protocols.items(), reverse=True) if p["status"] in ("draft", "review")
    ][:5]
    jobs = await jobs_repo.list_active(pool, user.tenant_id)
    return {
        "counts": {
            "meetings": len(summaries),
            "topics": sum(s.topic_count for s in summaries),
            "action_items": sum(s.action_item_count for s in summaries),
            "protocols": by_status,
        },
        "attention": attention,
        "processing": [_job(j) for j in jobs],
    }


@router.get("/meetings")
async def meetings(request: Request):
    user = await api_user(request)
    pool = await get_pool()
    mdir = _meetings_dir(user)
    summaries = await summaries_with_protocols(mdir, pool, user.tenant_id)
    protocols = {f"{p['meeting_date']:%Y-%m-%d}": p for p in await protocols_repo.list_all(pool, user.tenant_id)}
    active = {_iso(j.get("meeting_date")): j.get("status") for j in await jobs_repo.list_active(pool, user.tenant_id)}
    out = []
    for s in summaries:
        audio = _find_audio(mdir, s.date)
        out.append({
            "date": s.date,
            "topics": s.topic_count,
            "attendees": len(s.attendees),
            "action_items": s.action_item_count,
            "has_recording": s.topic_count > 0 or audio is not None,
            "has_audio": audio is not None,
            "audio_bytes": audio.stat().st_size if audio is not None else None,
            "processing": active.get(s.date),
            "protocol": _protocol_meta(protocols.get(s.date)),
            "sealed": encrypted.folder_is_sealed(mdir / s.date),
        })
    return {"meetings": out}


@router.get("/meetings/{date}")
async def meeting(request: Request, date: str):
    user = await api_user(request)
    if not _DATE_RE.match(date):
        raise HTTPException(status_code=404, detail="Немає такої зустрічі.")
    pool = await get_pool()
    mdir = _meetings_dir(user)

    detail = meetings_index.load_detail(mdir, date)
    sealed = encrypted.folder_is_sealed(mdir / date)
    protocol: Optional[dict[str, Any]] = None
    items: list[dict[str, Any]] = []
    try:
        protocol = await protocols_repo.get_by_date(pool, user.tenant_id, date)
        if protocol is not None:
            items = await protocols_repo.list_items(pool, user.tenant_id, int(protocol["id"]))
    except NotDecryptable:
        sealed = True
        protocol = await protocols_repo.meta_by_date(pool, user.tenant_id, date)

    if detail is None and protocol is None and not sealed:
        raise HTTPException(status_code=404, detail="Немає такої зустрічі.")

    audio = _find_audio(mdir, date)
    job = await jobs_repo.get_by_date(pool, user.tenant_id, date)

    proto_out = None
    if protocol is not None:
        proto_out = _protocol_meta(protocol)
        if not sealed:
            proto_out.update({
                "chair": protocol.get("chair_name") or protocol.get("chair_username") or "",
                "chair_id": protocol.get("chair_id"),
                "secretary": protocol.get("secretary") or "",
                "attendees": list(protocol.get("attendees") or []),
                "quorum": protocol.get("quorum") or "",
                "items": [{
                    "id": int(i["id"]), "position": i["position"], "question": i["question"],
                    "heard": i.get("heard") or "", "resolved": i.get("resolved") or "",
                    "status": i.get("status"),
                    "votes": {"for": i.get("votes_for"), "against": i.get("votes_against"), "abstain": i.get("votes_abstain")},
                    "rulings": [{"id": int(r["id"]), "text": r["text"], "responsible": r.get("responsible") or "", "due": r.get("due") or ""}
                                for r in i.get("rulings", [])],
                } for i in items],
            })

    return {
        "date": date,
        "sealed": sealed,
        "protocol": proto_out,
        "attendees": detail.attendees if detail else [],
        "topics": [_topic(t) for t in detail.topics] if detail else [],
        "transcript": [_turn(t) for t in detail.transcript] if detail else [],
        "has_audio": audio is not None,
        "audio_bytes": audio.stat().st_size if audio is not None else None,
        "processing": job["status"] if job and job["status"] in jobs_repo.ACTIVE_STATUSES else None,
    }


@router.get("/meetings/{date}/audio")
async def meeting_audio(request: Request, date: str):
    """The recording, resolved inside the caller's own folder. Starlette's
    FileResponse answers Range requests, so the app can seek by timecode."""
    user = await api_user(request)
    audio = _find_audio(_meetings_dir(user), date)
    if audio is None or not audio.exists():
        raise HTTPException(status_code=404, detail="Запису немає.")
    return FileResponse(audio, media_type=_audio_media_type(audio))


@router.get("/search")
async def search(request: Request, q: str = Query("", max_length=200)):
    """Keyword search over topics — the search the design asked for: topics
    with timecodes, never a synthesised answer."""
    user = await api_user(request)
    q = q.strip()
    if len(q) < 2:
        return {"query": q, "hits": []}
    mdir = _meetings_dir(user)
    matches = meetings_index.search_topics(mdir, q, limit=60)
    by_date: dict[str, dict[str, Any]] = {}
    for m in matches:
        entry = by_date.setdefault(m.meeting_date, {"date": m.meeting_date, "topics": []})
        entry["topics"].append({"order": m.topic_order, "title": m.topic_title, "snippet": m.snippet})
    return {"query": q, "hits": list(by_date.values())}


# ─────────────────────────────────────────────────────────────
# Past questions (the RAG history the web keeps in `queries`)
# ─────────────────────────────────────────────────────────────

def _hit(h: dict[str, Any]) -> dict[str, Any]:
    """A stored hit is {score, payload:{meeting_date, topic_title, body…}} —
    the Qdrant point as the worker saved it. Older rows kept the fields flat."""
    p = h.get("payload") if isinstance(h.get("payload"), dict) else h
    return {
        "date": p.get("meeting_date") or h.get("meeting_date"),
        "title": p.get("topic_title") or h.get("topic_title"),
        "score": h.get("score", h.get("rerank_score", h.get("vector_score"))),
    }


def _query_row(q: dict[str, Any]) -> dict[str, Any]:
    hits = q.get("hits") or []
    return {
        "id": int(q["id"]),
        "question": q["question"],
        "asked_at": _iso(q.get("asked_at")),
        "status": q["status"],
        "source": q.get("source"),
        "sources": list(q.get("sources") or []),
        "synthesis": q.get("synthesis") if q["status"] == "completed" else None,
        "hits": [_hit(h) for h in hits if isinstance(h, dict)][:12],
        "seconds": round((q.get("total_time_ms") or 0) / 1000, 1) if q.get("total_time_ms") else None,
    }


@router.get("/queries")
async def queries(request: Request, limit: int = Query(30, ge=1, le=100)):
    """What the church has already asked — with the answers. The same rows the
    web's /history shows; the app lists them under the search box so a
    question asked at the desk is there on the phone."""
    from church_assistant.db import queries_repo
    user = await api_user(request)
    pool = await get_pool()
    rows = await queries_repo.list_recent(pool, user.tenant_id, limit=limit)
    return {"queries": [_query_row(q) for q in rows if q["status"] in ("completed", "pending", "processing")]}
