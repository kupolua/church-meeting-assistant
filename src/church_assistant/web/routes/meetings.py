"""
Meetings routes:
    GET  /meetings/{date}           — meeting detail page (attendees + topics + стенограма)
    GET  /meetings/{date}/topics.pdf — the Теми section as a printable PDF
    GET  /meetings/{date}/audio     — stream the meeting recording (HTTP Range support)
    GET  /meetings/{date}/speakers  — edit speaker→name mapping for a processed meeting
    POST /meetings/open             — admin opens a FUTURE meeting (date + chair)
    POST /meetings/{date}/protocol/header — the chair fills in the parts above the agenda
    POST /meetings/{date}/protocol/items  — the chair writes the agenda (add/edit/move/delete)
    POST /meetings/{date}/speakers  — save speakers.json + queue a full re-run
"""

from __future__ import annotations

import mimetypes
from datetime import datetime
import re
from pathlib import Path
from typing import Any, Optional

from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from church_assistant.db import audit_repo
from church_assistant.db import ingestion_jobs_repo as jobs_repo
from church_assistant.db import protocols_repo, web_users_repo
from church_assistant.db.connection import get_pool
from church_assistant.ingestion import manual_speakers
from church_assistant.ingestion import speaker_review as review
from church_assistant.ingestion import speakers as speakers_util
from church_assistant.ingestion.paths import resolve as resolve_paths
from church_assistant.shared import meetings_index, pdf_export, tenant_paths
from church_assistant.shared.logger import Logger
from church_assistant.web.main import templates
from church_assistant.web.tenant import (
    current_tenant, current_tenant_slug, current_user, require_admin,
)


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


async def summaries_with_protocols(
    meetings_dir: Path, pool: Any, tenant_id: int,
) -> list[Any]:
    """
    Every meeting this church has — on disk OR in the database.

    A meeting used to be a folder, so listing them was a directory scan. Since
    018 the earliest thing a meeting has is its PROTOCOL: the agenda is written
    before anyone presses record, and the folder appears hours after the meeting
    ends. Scanning folders alone would hide exactly the meetings somebody is
    preparing for — the ones a list is most useful for.

    Folder-backed entries win where both exist: they carry topic counts and
    attendees, and a protocol adds nothing the summary shows.
    """
    summaries = meetings_index.list_all_summaries(meetings_dir)
    have = {s.date for s in summaries}
    for p in await protocols_repo.list_all(pool, tenant_id):
        d = f"{p['meeting_date']:%Y-%m-%d}"
        if d in have:
            continue
        summaries.append(meetings_index.MeetingSummary(
            date=d, folder=meetings_dir / d,
        ))
    summaries.sort(key=lambda s: s.date, reverse=True)
    return summaries


@router.get("/{date}", response_class=HTMLResponse)
async def meeting_detail(request: Request, date: str):
    """Render meeting detail page for a given date (YYYY-MM-DD)."""
    tpaths = _tenant_paths(request)
    pool = await get_pool()
    tenant_id = current_tenant(request)

    detail = meetings_index.load_detail(tpaths.meetings, date)
    protocol = await protocols_repo.get_by_date(pool, tenant_id, date)

    if detail is None:
        # A meeting with an agenda and no recording yet: the protocol is written
        # before anyone presses record, so the page has to open on it alone.
        # Without a protocol either, this is genuinely nothing — and it is also
        # the answer when the meeting belongs to ANOTHER church, because neither
        # lookup can leave this tenant (one is its folder, the other is RLS).
        if protocol is None:
            raise HTTPException(
                status_code=404,
                detail=f"Meeting {date!r} not found",
            )
        detail = meetings_index.MeetingDetail(date=date, folder=tpaths.meetings / date)

    summaries = await summaries_with_protocols(tpaths.meetings, pool, tenant_id)

    # If a speakers re-edit is currently re-running, surface it (the page still
    # shows the old names until the new digest is ready).
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
            "me": current_user(request),
            "protocol": protocol,
            "agenda": (
                await protocols_repo.list_items(pool, tenant_id, int(protocol["id"]))
                if protocol else []
            ),
            "protocol_number": (
                protocols_repo.number(protocol) if protocol else None
            ),
            # Only the chair of THIS meeting writes it up. Not a church-wide
            # role: two meetings can be written up by two people at once, which
            # a role could not express.
            "is_chair": bool(
                protocol and protocol["chair_id"] == current_user(request).user_id
            ),
            "chair_choices": await web_users_repo.list_active(pool, tenant_id),
        },
    )


@router.post("/open")
async def open_meeting(
    request: Request,
    meeting_date: str = Form(...),
    chair_id: int = Form(...),
    secretary: str = Form(""),
):
    """
    Open a meeting before it happens: a date, a chair, and a protocol to fill.

    ADMIN, not the chair — somebody has to be able to name the chair, and the
    chair of a meeting that does not exist yet cannot name themselves.

    The date is deliberately unconstrained. A council writes up a meeting it
    forgot to open beforehand, and refusing past dates would send them to psql
    to do it anyway. What IS refused is a second protocol for a date that
    already has one, and that refusal lives in the unique index rather than in
    this check, so a race loses cleanly.
    """
    require_admin(request)
    pool = await get_pool()
    tenant_id = current_tenant(request)

    try:
        d = datetime.strptime(meeting_date.strip(), "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="Дата має бути YYYY-MM-DD")

    chair = await web_users_repo.get_by_id(pool, tenant_id, chair_id)
    if chair is None or not chair["is_active"]:
        raise HTTPException(status_code=400, detail="Такого ведучого немає")

    try:
        protocol_id = await protocols_repo.create(
            pool, tenant_id,
            meeting_date=d,
            chair_id=chair_id,
            secretary=secretary,
            created_by=current_user(request).actor,
        )
    except protocols_repo.ProtocolExists:
        # Not an error worth a page: the meeting is open, which is what the
        # person wanted. Send them to it.
        return RedirectResponse(f"/meetings/{d:%Y-%m-%d}", status_code=303)

    await audit_repo.record(
        pool,
        tenant_id=tenant_id,
        action="protocol.opened",
        actor=current_user(request).actor,
        resource=f"meeting_protocols/{protocol_id}",
        detail={"meeting_date": f"{d:%Y-%m-%d}", "chair": chair["username"]},
    )
    return RedirectResponse(f"/meetings/{d:%Y-%m-%d}", status_code=303)


@router.post("/{date}/protocol/header")
async def save_protocol_header(
    request: Request,
    date: str,
    secretary: str = Form(""),
    quorum: str = Form(""),
    attendees: str = Form(""),
):
    """
    The parts above the agenda, written by the chair of this meeting.

    Attendees arrive as one name per line rather than as checkboxes over the
    diarized speakers, because the two lists are not the same thing: diarization
    knows who SPOKE, and a member who sat through the meeting in silence is
    absent to it. A quorum line cannot rest on that, so the chair types it.
    """
    pool = await get_pool()
    tenant_id = current_tenant(request)

    protocol = await protocols_repo.get_by_date(pool, tenant_id, date)
    if protocol is None:
        raise HTTPException(status_code=404, detail="Протокол не відкрито")
    if protocol["chair_id"] != current_user(request).user_id:
        # 403 and not 404: the protocol is openly there on the page they are
        # looking at, and pretending otherwise would only be confusing.
        raise HTTPException(
            status_code=403, detail="Протокол веде інша людина")

    names = [n.strip() for n in attendees.splitlines() if n.strip()]
    try:
        await protocols_repo.update_header(
            pool, tenant_id, int(protocol["id"]),
            secretary=secretary, quorum=quorum, attendees=names,
        )
    except protocols_repo.ProtocolFrozen:
        raise HTTPException(
            status_code=409,
            detail="Протокол затверджено — редагування неможливе",
        )

    await audit_repo.record(
        pool,
        tenant_id=tenant_id,
        action="protocol.header_edited",
        actor=current_user(request).actor,
        resource=f"meeting_protocols/{protocol['id']}",
        detail={"meeting_date": date, "attendees": len(names)},
    )
    return RedirectResponse(f"/meetings/{date}", status_code=303)


async def _chair_protocol(request: Request, date: str) -> tuple[Any, int, dict]:
    """
    Load this meeting's protocol and refuse anyone but its chair.

    Factored out because four routes need the identical three refusals, and a
    permission check copied four times is a permission check that will be four
    slightly different checks by winter.
    """
    pool = await get_pool()
    tenant_id = current_tenant(request)
    protocol = await protocols_repo.get_by_date(pool, tenant_id, date)
    if protocol is None:
        raise HTTPException(status_code=404, detail="Протокол не відкрито")
    if protocol["chair_id"] != current_user(request).user_id:
        # 403, not 404: the protocol is plainly on the page they are reading.
        raise HTTPException(status_code=403, detail="Протокол веде інша людина")
    return pool, tenant_id, protocol


def _frozen() -> HTTPException:
    return HTTPException(
        status_code=409, detail="Протокол затверджено — редагування неможливе")


@router.post("/{date}/protocol/items")
async def add_agenda_item(request: Request, date: str, question: str = Form(...)):
    """
    Add a question to the agenda.

    This is the point of the whole feature: an agenda is written by a person
    BEFORE the meeting, and that act is what groups the discussion afterwards.
    Nothing derived from the recording can do it — clustering the topics by
    similarity was tried on 28.08 and chained one cluster across 13 consecutive
    meetings.
    """
    pool, tenant_id, protocol = await _chair_protocol(request, date)
    try:
        await protocols_repo.add_item(
            pool, tenant_id, int(protocol["id"]), question)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except protocols_repo.ProtocolFrozen:
        raise _frozen()
    return RedirectResponse(f"/meetings/{date}", status_code=303)


@router.post("/{date}/protocol/items/{item_id}")
async def edit_agenda_item(
    request: Request, date: str, item_id: int,
    question: str = Form(...), status: str = Form("considered"),
):
    """Reword a question, or mark it as one the meeting never reached."""
    pool, tenant_id, protocol = await _chair_protocol(request, date)
    item = await protocols_repo.get_item(pool, tenant_id, item_id)
    if item is None or item["protocol_id"] != protocol["id"]:
        raise HTTPException(status_code=404, detail="Питання не знайдено")
    try:
        await protocols_repo.update_item(
            pool, tenant_id, item_id, question=question, status=status)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except protocols_repo.ProtocolFrozen:
        raise _frozen()
    return RedirectResponse(f"/meetings/{date}", status_code=303)


@router.post("/{date}/protocol/items/{item_id}/delete")
async def delete_agenda_item(request: Request, date: str, item_id: int):
    """Remove a question. Positions close up behind it."""
    pool, tenant_id, protocol = await _chair_protocol(request, date)
    item = await protocols_repo.get_item(pool, tenant_id, item_id)
    if item is None or item["protocol_id"] != protocol["id"]:
        raise HTTPException(status_code=404, detail="Питання не знайдено")
    try:
        await protocols_repo.delete_item(pool, tenant_id, item_id)
    except protocols_repo.ProtocolFrozen:
        raise _frozen()
    return RedirectResponse(f"/meetings/{date}", status_code=303)


@router.post("/{date}/protocol/items/{item_id}/move")
async def move_agenda_item(
    request: Request, date: str, item_id: int, delta: int = Form(...),
):
    """Swap a question with its neighbour. At the ends this does nothing."""
    pool, tenant_id, protocol = await _chair_protocol(request, date)
    item = await protocols_repo.get_item(pool, tenant_id, item_id)
    if item is None or item["protocol_id"] != protocol["id"]:
        raise HTTPException(status_code=404, detail="Питання не знайдено")
    try:
        await protocols_repo.move_item(pool, tenant_id, item_id, delta)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except protocols_repo.ProtocolFrozen:
        raise _frozen()
    return RedirectResponse(f"/meetings/{date}", status_code=303)


@router.post("/{date}/protocol/items/{item_id}/body")
async def save_item_body(
    request: Request, date: str, item_id: int,
    heard: str = Form(""), resolved: str = Form(""),
    votes_for: str = Form(""), votes_against: str = Form(""),
    votes_abstain: str = Form(""),
):
    """
    "Слухали", "Вирішили" and the vote — the chair's last word over Gemma's draft.

    THE DRAFT IS A SUGGESTION AND THIS IS WHY IT CAN BE ONE. Gemma writes into
    empty fields during ingestion and never touches a written one; without this
    form that would make a wrong draft permanent, and a wrong draft is exactly
    what a degenerating model produces (see protocol_draft._looks_degenerate).

    Votes come in as strings, not ints, so that BLANK is expressible. A form
    field typed `int` cannot say "this question was not put to a vote", and most
    of them are not — a council discusses far more than it divides on.
    """
    pool, tenant_id, protocol = await _chair_protocol(request, date)
    item = await protocols_repo.get_item(pool, tenant_id, item_id)
    if item is None or item["protocol_id"] != protocol["id"]:
        raise HTTPException(status_code=404, detail="Питання не знайдено")

    def _count(raw: str) -> Optional[int]:
        raw = raw.strip()
        if not raw:
            return None
        if not raw.isdigit():
            raise HTTPException(
                status_code=400, detail="Голоси вводяться цілим числом")
        return int(raw)

    # ⚠️ EVERYTHING IS VALIDATED BEFORE ANYTHING IS WRITTEN, and that order was
    # bought with a bug. The first version saved the texts and then validated
    # the vote — so a half-filled tally returned 400 with the texts already
    # committed, and since the failing request carried no `heard`, it wiped the
    # "Слухали" Gemma had spent two minutes drafting. A refused save must leave
    # the question exactly as it was.
    counts = (_count(votes_for), _count(votes_against), _count(votes_abstain))
    given = [c for c in counts if c is not None]
    if given and len(given) != 3:
        raise HTTPException(
            status_code=400,
            detail="Голосування вводиться повністю: за, проти, утрималось",
        )

    try:
        await protocols_repo.update_item(
            pool, tenant_id, item_id, heard=heard, resolved=resolved)
        await protocols_repo.set_votes(
            pool, tenant_id, item_id,
            votes_for=counts[0], votes_against=counts[1], votes_abstain=counts[2],
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except protocols_repo.ProtocolFrozen:
        raise _frozen()
    return RedirectResponse(f"/meetings/{date}", status_code=303)


@router.post("/{date}/protocol/items/{item_id}/rulings")
async def add_ruling(
    request: Request, date: str, item_id: int,
    text: str = Form(...), responsible: str = Form(""), due: str = Form(""),
):
    """Add one ruling under a question."""
    pool, tenant_id, protocol = await _chair_protocol(request, date)
    item = await protocols_repo.get_item(pool, tenant_id, item_id)
    if item is None or item["protocol_id"] != protocol["id"]:
        raise HTTPException(status_code=404, detail="Питання не знайдено")
    try:
        await protocols_repo.add_ruling(
            pool, tenant_id, item_id,
            text=text, responsible=responsible, due=due)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except protocols_repo.ProtocolFrozen:
        raise _frozen()
    return RedirectResponse(f"/meetings/{date}", status_code=303)


@router.post("/{date}/protocol/rulings/{ruling_id}")
async def edit_ruling(
    request: Request, date: str, ruling_id: int,
    text: str = Form(...), responsible: str = Form(""), due: str = Form(""),
    remove: str = Form(""),
):
    """
    Edit a ruling, or remove it.

    One route for both because they are one button-row on one form: a separate
    delete endpoint would need its own copy of the three ownership checks, and a
    permission check copied is a permission check that drifts.
    """
    pool, tenant_id, protocol = await _chair_protocol(request, date)

    # A ruling reaches its protocol through its item; check that before writing,
    # or an id from another meeting would be edited under this one's permissions.
    ruling = await protocols_repo.get_ruling(pool, tenant_id, ruling_id)
    if ruling is None:
        raise HTTPException(status_code=404, detail="Постанову не знайдено")
    item = await protocols_repo.get_item(pool, tenant_id, int(ruling["item_id"]))
    if item is None or item["protocol_id"] != protocol["id"]:
        raise HTTPException(status_code=404, detail="Постанову не знайдено")

    try:
        if remove:
            await protocols_repo.delete_ruling(pool, tenant_id, ruling_id)
        else:
            await protocols_repo.update_ruling(
                pool, tenant_id, ruling_id,
                text=text, responsible=responsible, due=due)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except protocols_repo.ProtocolFrozen:
        raise _frozen()
    return RedirectResponse(f"/meetings/{date}", status_code=303)


@router.post("/{date}/protocol/approve")
async def approve_protocol(request: Request, date: str, confirm: str = Form("")):
    """
    Freeze the minutes. One way, and the database is what makes it so.

    ⚠️ IT ASKS FOR THE NUMBER TO BE TYPED, not for a click. This is the archive
    pattern (platform.py), and it belongs here more than there: archiving keeps
    a church's data for a year and can be undone with one button, while approval
    can never be undone at all — 018's trigger refuses every later edit, from
    this route, from a future one, and from psql at midnight. By the second time
    a person sees a confirm dialog the hand answers it before the eye reads it;
    typing "07-09-2026/1" is not something the hand does on its own.

    An empty protocol cannot be approved. Not paternalism — a protocol with no
    agenda is a mis-click on a page somebody opened to look at, and freezing it
    would mean opening a second one for the same date, which the unique index
    refuses.
    """
    pool, tenant_id, protocol = await _chair_protocol(request, date)
    number = protocols_repo.number(protocol)

    if protocol["status"] == "approved":
        return RedirectResponse(f"/meetings/{date}", status_code=303)

    if confirm.strip() != number:
        raise HTTPException(
            status_code=400,
            detail=f"Щоб затвердити протокол, введіть його номер «{number}» точно.",
        )

    items = await protocols_repo.list_items(pool, tenant_id, int(protocol["id"]))
    if not items:
        raise HTTPException(
            status_code=400,
            detail="Порожній протокол затвердити не можна — немає жодного питання.",
        )

    await protocols_repo.set_status(
        pool, tenant_id, int(protocol["id"]), "approved",
        approved_by=current_user(request).user_id,
    )
    await audit_repo.record(
        pool,
        tenant_id=tenant_id,
        action="protocol.approved",
        actor=current_user(request).actor,
        resource=f"meeting_protocols/{protocol['id']}",
        detail={"meeting_date": date, "number": number, "items": len(items)},
    )
    await _logger.warn(
        "protocol.approved",
        message=f"{current_user(request).username} затвердив протокол {number} "
                f"({len(items)} питань) — редагування закрито назавжди",
        tenant_id=tenant_id,
    )
    return RedirectResponse(f"/meetings/{date}", status_code=303)


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


@router.get("/{date}/protocol.pdf")
async def meeting_protocol_pdf(request: Request, date: str):
    """
    The minutes, as the document a council signs.

    ⚠️ A DRAFT RENDERS TOO, and the first version refusing it was a design
    error, not caution. Circulating the draft for agreement is the step BEFORE
    approval, and approval is irreversible — so a chair who could only export an
    approved protocol would have had to freeze the document before anyone was
    allowed to read it, which inverts the whole review.

    The concern that produced the refusal was real: a draft still carries
    Gemma's wording in half its fields, and a file is the form in which a
    document leaves the system and stops being correctable. It is answered by
    STAMPING rather than withholding — "ПРОЕКТ" in the title, a warning as the
    first line, and a diagonal watermark on every page, because a draft gets
    printed and forwarded a page at a time to people who never saw the covering
    message.

    Readable by anyone in the church, not just the chair: it is their minutes.
    """
    pool = await get_pool()
    tenant_id = current_tenant(request)

    protocol = await protocols_repo.get_by_date(pool, tenant_id, date)
    if protocol is None:
        raise HTTPException(status_code=404, detail="Протокол не відкрито")
    items = await protocols_repo.list_items(pool, tenant_id, int(protocol["id"]))
    number = protocols_repo.number(protocol)
    try:
        pdf = pdf_export.build_protocol_pdf(protocol, items, number)
    except pdf_export.FontNotFound as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    # "07-09-2026/1" carries a slash, which is a path separator in every
    # filesystem this will land on.
    safe_number = number.replace("/", "-")
    draft = protocol["status"] != "approved"
    # The filename says it too: this file gets saved, mailed on and opened a
    # week later by someone who no longer remembers which one it was.
    ascii_name = f"{'draft-' if draft else ''}protocol-{date}.pdf"
    human = (f"ПРОЕКТ протоколу {safe_number}.pdf" if draft
             else f"Протокол {safe_number}.pdf")
    disposition = (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''{quote(human)}"
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
            "error": request.query_params.get("error"),
            "ok": request.query_params.get("ok"),
            "next_label": manual_speakers.next_free_label(
                mapping, manual_speakers.load_entries(meta)
            ),
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

    Also applies the manual-speaker rows (see ingestion/manual_speakers.py):
    a participant diarization missed is added as a new SPEAKER_XX from a typed
    timestamp, which rewrites diarization.rttm before the re-run reads it.

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

    # Hand-added participants (a voice diarization never clustered) — applied
    # before anything is written, so a mistyped time costs a redirect, not a
    # half-saved file plus a queued multi-hour re-run.
    manual = manual_speakers.apply_edits(
        paths.transcript, meta, new_mapping,
        manual_speakers.inputs_from_form(form, manual_speakers.manual_labels(meta)),
    )
    if manual.error:
        return RedirectResponse(
            f"/meetings/{date}/speakers?error={quote(manual.error)}", status_code=303
        )
    meta, new_mapping = manual.meta, manual.mapping

    speakers_util.save_speakers(paths.speakers, meta, new_mapping)
    if manual.changed:
        manual_speakers.rebuild_rttm(paths.rttm, manual.entries)

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
        metadata={
            "job_id": job_id, "meeting_date": date,
            "speaker_count": len(new_mapping),
            "manual_speakers": manual.notes,
        },
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
