"""
The JSON API, step 5 — opening a meeting and keeping its minutes from the phone.

    GET    /api/v1/council                                    any member: names of the active council
    POST   /api/v1/meetings                                   admin: open a meeting (date, chair, secretary)
    POST   /api/v1/meetings/{date}/protocol/header            chair: secretary, quorum, attendees
    POST   /api/v1/meetings/{date}/protocol/items             chair: add an agenda question
    POST   /api/v1/meetings/{date}/protocol/items/{id}        chair: question, status, Слухали, Вирішили, vote
    DELETE /api/v1/meetings/{date}/protocol/items/{id}        chair: remove a question
    POST   /api/v1/meetings/{date}/protocol/items/{id}/move   chair: swap with a neighbour (delta ±1)
    POST   /api/v1/meetings/{date}/protocol/items/{id}/rulings   chair: add a Постановили
    POST   /api/v1/meetings/{date}/protocol/rulings/{id}      chair: edit one
    DELETE /api/v1/meetings/{date}/protocol/rulings/{id}      chair: remove one

THE SAME RULES AS THE WEB (routes/meetings.py), not new ones: only the chair of
THIS meeting edits it, an approved protocol refuses every change (the trigger
in 018 and the checks in protocols_repo — this module never enforces that
itself), and the vote is all three numbers or none. Where the web redirects,
these routes answer with the protocol's fresh editable part, so the phone
redraws from what the server now holds instead of from what it hoped to send.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from church_assistant.db import audit_repo, protocols_repo, web_users_repo
from church_assistant.db.connection import get_pool
from church_assistant.shared.field_crypto import NotDecryptable
from church_assistant.web.routes.api import ApiUser, api_admin, api_user
from church_assistant.web.routes.api_read import _DATE_RE


router = APIRouter(prefix="/api/v1")

MAX_TEXT = 8000


class OpenIn(BaseModel):
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    chair_id: int
    secretary: str = Field(default="", max_length=120)


class HeaderIn(BaseModel):
    secretary: str = Field(default="", max_length=120)
    quorum: str = Field(default="", max_length=200)
    attendees: list[str] = Field(default_factory=list, max_length=200)


class QuestionIn(BaseModel):
    question: str = Field(min_length=1, max_length=500)


class Votes(BaseModel):
    for_: int = Field(alias="for", ge=0)
    against: int = Field(ge=0)
    abstain: int = Field(ge=0)


class ItemIn(BaseModel):
    """Every field optional: None leaves it alone. `votes` is the exception —
    sent as `clear_votes: true` to go back to «not put to a vote»."""
    question: Optional[str] = Field(default=None, min_length=1, max_length=500)
    status: Optional[str] = Field(default=None, pattern="^(considered|not_considered)$")
    heard: Optional[str] = Field(default=None, max_length=MAX_TEXT)
    resolved: Optional[str] = Field(default=None, max_length=MAX_TEXT)
    votes: Optional[Votes] = None
    clear_votes: bool = False


class MoveIn(BaseModel):
    delta: int = Field(ge=-1, le=1)


class RulingIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)
    responsible: str = Field(default="", max_length=200)
    due: str = Field(default="", max_length=100)


def _frozen() -> HTTPException:
    return HTTPException(status_code=409, detail="Протокол затверджено — редагування неможливе.")


async def _chair_protocol(request: Request, date: str) -> tuple[Any, ApiUser, dict[str, Any]]:
    user = await api_user(request)
    if not _DATE_RE.match(date):
        raise HTTPException(status_code=404, detail="Протоколу немає.")
    pool = await get_pool()
    try:
        protocol = await protocols_repo.get_by_date(pool, user.tenant_id, date)
    except NotDecryptable:
        raise HTTPException(status_code=409, detail="Протокол зашифровано; сервер не може його прочитати.")
    if protocol is None:
        raise HTTPException(status_code=404, detail="Протоколу немає.")
    if protocol["chair_id"] != user.user_id:
        raise HTTPException(status_code=403, detail="Протокол веде інша людина.")
    return pool, user, protocol


async def _item_of(pool: Any, user: ApiUser, protocol: dict[str, Any], item_id: int) -> dict[str, Any]:
    item = await protocols_repo.get_item(pool, user.tenant_id, item_id)
    if item is None or item["protocol_id"] != protocol["id"]:
        raise HTTPException(status_code=404, detail="Питання не знайдено.")
    return item


async def _state(pool: Any, user: ApiUser, date: str) -> dict[str, Any]:
    """The editable part of the protocol as it is NOW — what every write returns."""
    from church_assistant.web.routes.api_read import protocol_payload
    protocol = await protocols_repo.get_by_date(pool, user.tenant_id, date)
    items = await protocols_repo.list_items(pool, user.tenant_id, int(protocol["id"]))
    return await protocol_payload(pool, user, protocol, items)


@router.get("/council")
async def council(request: Request):
    """Who can be named present. The names must match the accounts' full names
    exactly — that match is what makes someone a required signer — so the
    chair picks from this list instead of typing them."""
    user = await api_user(request)
    pool = await get_pool()
    rows = await web_users_repo.list_active(pool, user.tenant_id)
    return {"council": [{"id": int(r["id"]), "name": r["full_name"]} for r in rows
                        if r.get("full_name") and not r.get("is_platform_admin")]}


@router.post("/meetings", status_code=201)
async def open_meeting(request: Request, body: OpenIn):
    """Same as the web's /meetings/open: ADMIN opens, because the chair of a
    meeting that does not exist yet cannot name themselves. The date is not
    constrained — a council writes up a meeting it forgot to open."""
    admin = await api_admin(request)
    pool = await get_pool()
    try:
        d = datetime.strptime(body.date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=422, detail="Такої дати не існує.")
    chair = await web_users_repo.get_by_id(pool, admin.tenant_id, body.chair_id)
    if chair is None or not chair["is_active"]:
        raise HTTPException(status_code=422, detail="Такого ведучого немає.")
    try:
        protocol_id = await protocols_repo.create(
            pool, admin.tenant_id, meeting_date=d, chair_id=body.chair_id,
            secretary=body.secretary, created_by=admin.actor,
        )
    except protocols_repo.ProtocolExists:
        raise HTTPException(status_code=409, detail="На цю дату протокол уже відкрито.")
    await audit_repo.record(
        pool, tenant_id=admin.tenant_id, action="protocol.opened", actor=admin.actor,
        resource=f"meeting_protocols/{protocol_id}",
        detail={"meeting_date": body.date, "chair": chair["username"], "via": "app"},
    )
    return {"ok": True, "date": body.date}


@router.post("/meetings/{date}/protocol/header")
async def save_header(request: Request, date: str, body: HeaderIn):
    pool, user, protocol = await _chair_protocol(request, date)
    names = [n.strip() for n in body.attendees if n.strip()]
    try:
        await protocols_repo.update_header(
            pool, user.tenant_id, int(protocol["id"]),
            secretary=body.secretary, quorum=body.quorum, attendees=names,
        )
    except protocols_repo.ProtocolFrozen:
        raise _frozen()
    await audit_repo.record(
        pool, tenant_id=user.tenant_id, action="protocol.header_edited", actor=user.actor,
        resource=f"meeting_protocols/{protocol['id']}",
        detail={"meeting_date": date, "attendees": len(names), "via": "app"},
    )
    return {"protocol": await _state(pool, user, date)}


@router.post("/meetings/{date}/protocol/items", status_code=201)
async def add_item(request: Request, date: str, body: QuestionIn):
    pool, user, protocol = await _chair_protocol(request, date)
    try:
        await protocols_repo.add_item(pool, user.tenant_id, int(protocol["id"]), body.question)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except protocols_repo.ProtocolFrozen:
        raise _frozen()
    return {"protocol": await _state(pool, user, date)}


@router.post("/meetings/{date}/protocol/items/{item_id}")
async def save_item(request: Request, date: str, item_id: int, body: ItemIn):
    pool, user, protocol = await _chair_protocol(request, date)
    await _item_of(pool, user, protocol, item_id)
    # Validated before anything is written — a refused save leaves the
    # question exactly as it was (the bug behind the web's copy of this rule).
    try:
        await protocols_repo.update_item(
            pool, user.tenant_id, item_id, question=body.question, status=body.status,
            heard=body.heard, resolved=body.resolved,
        )
        if body.clear_votes:
            await protocols_repo.set_votes(pool, user.tenant_id, item_id,
                                           votes_for=None, votes_against=None, votes_abstain=None)
        elif body.votes is not None:
            await protocols_repo.set_votes(pool, user.tenant_id, item_id, votes_for=body.votes.for_,
                                           votes_against=body.votes.against, votes_abstain=body.votes.abstain)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except protocols_repo.ProtocolFrozen:
        raise _frozen()
    return {"protocol": await _state(pool, user, date)}


@router.delete("/meetings/{date}/protocol/items/{item_id}")
async def delete_item(request: Request, date: str, item_id: int):
    pool, user, protocol = await _chair_protocol(request, date)
    await _item_of(pool, user, protocol, item_id)
    try:
        await protocols_repo.delete_item(pool, user.tenant_id, item_id)
    except protocols_repo.ProtocolFrozen:
        raise _frozen()
    return {"protocol": await _state(pool, user, date)}


@router.post("/meetings/{date}/protocol/items/{item_id}/move")
async def move_item(request: Request, date: str, item_id: int, body: MoveIn):
    if body.delta == 0:
        raise HTTPException(status_code=422, detail="Напрямок має бути -1 або 1.")
    pool, user, protocol = await _chair_protocol(request, date)
    await _item_of(pool, user, protocol, item_id)
    try:
        await protocols_repo.move_item(pool, user.tenant_id, item_id, body.delta)
    except protocols_repo.ProtocolFrozen:
        raise _frozen()
    return {"protocol": await _state(pool, user, date)}


@router.post("/meetings/{date}/protocol/items/{item_id}/rulings", status_code=201)
async def add_ruling(request: Request, date: str, item_id: int, body: RulingIn):
    pool, user, protocol = await _chair_protocol(request, date)
    await _item_of(pool, user, protocol, item_id)
    try:
        await protocols_repo.add_ruling(pool, user.tenant_id, item_id, text=body.text,
                                        responsible=body.responsible, due=body.due)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except protocols_repo.ProtocolFrozen:
        raise _frozen()
    return {"protocol": await _state(pool, user, date)}


async def _ruling_of(pool: Any, user: ApiUser, protocol: dict[str, Any], ruling_id: int) -> None:
    """A ruling reaches its protocol through its item; check before writing, or
    an id from another meeting would be edited under this one's permissions."""
    ruling = await protocols_repo.get_ruling(pool, user.tenant_id, ruling_id)
    if ruling is None:
        raise HTTPException(status_code=404, detail="Постанову не знайдено.")
    await _item_of(pool, user, protocol, int(ruling["item_id"]))


@router.post("/meetings/{date}/protocol/rulings/{ruling_id}")
async def edit_ruling(request: Request, date: str, ruling_id: int, body: RulingIn):
    pool, user, protocol = await _chair_protocol(request, date)
    await _ruling_of(pool, user, protocol, ruling_id)
    try:
        await protocols_repo.update_ruling(pool, user.tenant_id, ruling_id, text=body.text,
                                           responsible=body.responsible, due=body.due)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except protocols_repo.ProtocolFrozen:
        raise _frozen()
    return {"protocol": await _state(pool, user, date)}


@router.delete("/meetings/{date}/protocol/rulings/{ruling_id}")
async def delete_ruling(request: Request, date: str, ruling_id: int):
    pool, user, protocol = await _chair_protocol(request, date)
    await _ruling_of(pool, user, protocol, ruling_id)
    try:
        await protocols_repo.delete_ruling(pool, user.tenant_id, ruling_id)
    except protocols_repo.ProtocolFrozen:
        raise _frozen()
    return {"protocol": await _state(pool, user, date)}
