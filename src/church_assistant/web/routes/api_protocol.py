"""
The JSON API, step 4 — signing a protocol, and «Стан справ».

    POST /api/v1/meetings/{date}/protocol/status   chair/admin: draft → review, review → approved
    POST /api/v1/meetings/{date}/protocol/sign     a member signs the current text with their phone
    GET  /api/v1/tasks                              every task of the church
    POST /api/v1/tasks/{id}                         change a task's status (+ note)

WHO SIGNS. The protocol lists who was present as names (018: confirmed by
hand, deliberately). The members whose full name matches an attendee are the
required signers; anyone else in the church may sign too, and it counts as
a signature, not as one of the required ones. When every required signer
has signed the CURRENT text, the protocol approves itself; the chair or an
admin can also approve by hand from the app — the web's typed-number
ceremony stays for the browser. Approval freezes the text (018) and turns
the rulings into tasks (023).

THE HASH TRAVELS WITH THE PROTOCOL. GET /meetings/{date} carries
content_hash; the phone signs those 32 bytes after its biometric or
password gate and sends the hash back with the signature. If the hash it
sends is not the server's current one, the text changed under it and the
answer is 409 with the new hash — sign again, having read again.
"""

from __future__ import annotations

import base64
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from church_assistant.db import (
    audit_repo, protocols_repo, signatures_repo, signing_keys_repo, tasks_repo, web_users_repo,
)
from church_assistant.db.connection import get_pool
from church_assistant.shared import protocol_hash
from church_assistant.shared.field_crypto import NotDecryptable
from church_assistant.shared.logger import Logger
from church_assistant.web.routes.api import ApiUser, api_user


router = APIRouter(prefix="/api/v1")
_logger = Logger(process="web")


class StatusIn(BaseModel):
    status: str = Field(pattern="^(review|approved)$")


class SignIn(BaseModel):
    content_hash: str = Field(min_length=64, max_length=64, pattern="^[0-9a-f]{64}$")
    key_id: int
    signature: str = Field(min_length=80, max_length=96)   # base64 of 64 bytes


class TaskIn(BaseModel):
    status: str = Field(pattern="^(new|in_progress|done|cancelled)$")
    note: Optional[str] = Field(default=None, max_length=1000)


def _norm(name: str) -> str:
    return " ".join((name or "").split()).casefold()


async def signing_block(
    pool: Any, user: ApiUser, protocol: dict[str, Any], items: list[dict[str, Any]],
) -> dict[str, Any]:
    """What the protocol tab shows under the text: who has to sign, who has,
    whether this person may, and the hash the phone will sign."""
    h = protocol_hash.content_hash(protocol, items)
    sigs = await signatures_repo.list_for_protocol(pool, user.tenant_id, int(protocol["id"]))
    current = {int(s["web_user_id"]): s for s in sigs if s["content_hash"] == h}
    stale = {int(s["web_user_id"]) for s in sigs if s["content_hash"] != h} - set(current)

    members = await web_users_repo.list_active(pool, user.tenant_id)
    by_name = {_norm(m["full_name"]): m for m in members}
    required = []
    for a in protocol.get("attendees") or []:
        m = by_name.get(_norm(str(a)))
        required.append({
            "name": str(a), "user_id": int(m["id"]) if m else None,
            "signed": bool(m and int(m["id"]) in current),
            "stale": bool(m and int(m["id"]) in stale),
        })
    extra = [
        {"user_id": uid, "name": s["full_name"], "signed_at": s["signed_at"].isoformat(), "key_label": s["key_label"]}
        for uid, s in current.items()
        if uid not in {r["user_id"] for r in required}
    ]
    my_keys = await signing_keys_repo.list_for_user(pool, user.tenant_id, user.user_id)
    is_chair = protocol.get("chair_id") == user.user_id
    status = protocol["status"]
    return {
        "content_hash": h,
        "required": required,
        "required_signed": sum(1 for r in required if r["signed"]),
        "extra": extra,
        "my_signature": "current" if user.user_id in current else ("stale" if user.user_id in stale else None),
        "can_sign": status == "review" and bool(my_keys) and user.user_id not in current,
        "needs_key": status == "review" and not my_keys,
        "can_send_to_review": status == "draft" and (is_chair or user.is_admin) and bool(items),
        "can_approve": status == "review" and (is_chair or user.is_admin),
    }


async def _protocol_for(pool: Any, user: ApiUser, date: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        protocol = await protocols_repo.get_by_date(pool, user.tenant_id, date)
        if protocol is None:
            raise HTTPException(status_code=404, detail="Протоколу немає.")
        items = await protocols_repo.list_items(pool, user.tenant_id, int(protocol["id"]))
    except NotDecryptable:
        raise HTTPException(status_code=409, detail="Протокол зашифровано; сервер не може його прочитати.")
    return protocol, items


async def approve(pool: Any, user: ApiUser, protocol: dict[str, Any], items: list[dict[str, Any]], *, how: str) -> None:
    """Freeze the protocol and turn its rulings into tasks — the one function
    both the hand-approval and the last-signature path call."""
    await protocols_repo.set_status(pool, user.tenant_id, int(protocol["id"]), "approved", approved_by=user.user_id)
    created = await tasks_repo.materialize(
        pool, user.tenant_id, protocol_id=int(protocol["id"]), meeting_date=protocol["meeting_date"], items=items,
    )
    number = protocols_repo.number(protocol)
    await audit_repo.record(
        pool, tenant_id=user.tenant_id, action="protocol.approved", actor=user.actor,
        resource=f"meeting_protocols/{protocol['id']}",
        detail={"number": number, "how": how, "tasks_created": created},
    )
    await _logger.warn("protocol.approved",
                       message=f"{user.username} approved {number} ({how}); {created} tasks",
                       tenant_id=user.tenant_id)


@router.post("/meetings/{date}/protocol/status")
async def set_status(request: Request, date: str, body: StatusIn):
    user = await api_user(request)
    pool = await get_pool()
    protocol, items = await _protocol_for(pool, user, date)
    is_chair = protocol.get("chair_id") == user.user_id
    if not (is_chair or user.is_admin):
        raise HTTPException(status_code=403, detail="Лише ведучий цієї зустрічі або адміністратор.")
    if not items:
        raise HTTPException(status_code=400, detail="Порожній протокол нікуди не надіслати.")
    cur = protocol["status"]
    if body.status == "review":
        if cur != "draft":
            raise HTTPException(status_code=409, detail="Протокол уже не чернетка.")
        await protocols_repo.set_status(pool, user.tenant_id, int(protocol["id"]), "review")
        await audit_repo.record(pool, tenant_id=user.tenant_id, action="protocol.sent_to_review", actor=user.actor,
                                resource=f"meeting_protocols/{protocol['id']}", detail={"number": protocols_repo.number(protocol)})
    else:
        if cur != "review":
            raise HTTPException(status_code=409, detail="Затвердити можна лише протокол на підписі.")
        await approve(pool, user, protocol, items, how="by hand")
    return {"ok": True, "status": body.status}


@router.post("/meetings/{date}/protocol/sign")
async def sign(request: Request, date: str, body: SignIn):
    user = await api_user(request)
    pool = await get_pool()
    protocol, items = await _protocol_for(pool, user, date)
    if protocol["status"] != "review":
        raise HTTPException(status_code=409, detail="Підписують протокол на підписі; цей — " + ("уже затверджений." if protocol["status"] == "approved" else "ще чернетка."))
    h = protocol_hash.content_hash(protocol, items)
    if body.content_hash != h:
        raise HTTPException(status_code=409, detail={"message": "Текст протоколу змінився — перечитайте і підпишіть знову.", "content_hash": h})

    keys = {k["id"]: k for k in await signing_keys_repo.list_for_user(pool, user.tenant_id, user.user_id)}
    key = keys.get(body.key_id)
    if key is None:
        raise HTTPException(status_code=403, detail="Цей ключ не ваш або відкликаний.")
    try:
        sig = base64.b64decode(body.signature, validate=True)
        signatures_repo.verify(base64.b64decode(key["public_key"]), h, sig)
    except (ValueError, signatures_repo.BadSignature):
        raise HTTPException(status_code=400, detail="Підпис не перевірився.")
    try:
        await signatures_repo.add(pool, user.tenant_id, protocol_id=int(protocol["id"]), web_user_id=user.user_id,
                                  key_id=body.key_id, content_hash=h, signature=sig)
    except signatures_repo.AlreadySigned:
        raise HTTPException(status_code=409, detail="Ви вже підписали цей текст.")
    await audit_repo.record(pool, tenant_id=user.tenant_id, action="protocol.signed", actor=user.actor,
                            resource=f"meeting_protocols/{protocol['id']}",
                            detail={"number": protocols_repo.number(protocol), "key_id": body.key_id, "hash": h[:16]})

    block = await signing_block(pool, user, protocol, items)
    approved = False
    if block["required"] and all(r["signed"] for r in block["required"]):
        await approve(pool, user, protocol, items, how="all present signed")
        approved = True
        block = await signing_block(pool, user, {**protocol, "status": "approved"}, items)
    return {"ok": True, "approved": approved, "signing": block}


# ─────────────────────────────────────────────────────────────
# Tasks
# ─────────────────────────────────────────────────────────────

def _task(t: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": int(t["id"]), "text": t["text"], "responsible": t["responsible"], "due": t["due"],
        "status": t["status"], "note": t.get("note") or "",
        "meeting_date": t["meeting_date"].isoformat() if t.get("meeting_date") else None,
        "protocol_number": (f"{t['protocol_date']:%d-%m-%Y}/{t['seq']}" if t.get("protocol_date") and t.get("seq") is not None else None),
        "updated_at": t["updated_at"].isoformat() if t.get("updated_at") else None,
        "updated_by": t.get("updated_by_name"),
        "done_at": t["done_at"].isoformat() if t.get("done_at") else None,
    }


@router.get("/tasks")
async def tasks(request: Request):
    user = await api_user(request)
    pool = await get_pool()
    rows = await tasks_repo.list_all(pool, user.tenant_id)
    return {"tasks": [_task(t) for t in rows], "counts": await tasks_repo.counts(pool, user.tenant_id)}


@router.post("/tasks/{task_id}")
async def patch_task(request: Request, task_id: int, body: TaskIn):
    user = await api_user(request)
    pool = await get_pool()
    row = await tasks_repo.set_status(pool, user.tenant_id, task_id, status=body.status, by=user.user_id, note=body.note)
    if row is None:
        raise HTTPException(status_code=404, detail="Немає такого доручення.")
    await audit_repo.record(pool, tenant_id=user.tenant_id, action="task.status", actor=user.actor,
                            resource=f"tasks/{task_id}", detail={"status": body.status})
    rows = await tasks_repo.list_all(pool, user.tenant_id)
    t = next((x for x in rows if int(x["id"]) == task_id), None)
    return {"task": _task(t) if t else None}
