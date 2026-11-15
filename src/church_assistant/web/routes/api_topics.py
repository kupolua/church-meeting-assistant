"""
The JSON API — the council rewords a topic the model wrote.

    PATCH /api/v1/meetings/{date}/topics/{order}   {title?, body?} → the whole meeting

WHY THIS EXISTS. The first time the council read «Теми» on their phones
(15.11.2026) the text was right and the words were wrong — office language
where the church says things plainly. Letting any signed-in member fix a
title or a paragraph does two things at once: the meeting reads as they
would have written it, and every fix is kept in `topic_corrections` as a
«model wrote → people say» pair — the raw material for prompt examples now
(shared/style.py) and for fine-tuning when there are enough.

WHO MAY. Any member of the church, not only the chair: it was the members who
noticed, and a topic is not the protocol — nothing is signed over it. The
pair records who, and the audit log too.

WHERE IT WRITES. polished.md is the source of truth for topics everywhere
(meetings_index parses it for the app, the site, search and the protocol
assembly), so the edit goes into that file, atomically, through
meetings_index.replace_topic — the same walk the parser does, so topic N is
topic N. A sealed (encrypted) folder cannot be edited from here yet. Qdrant
(the question-answering index) is not refreshed by this: that index is built
on the processing node, and a reworded title does not change the answers.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from church_assistant.db import audit_repo, topic_corrections_repo
from church_assistant.db.connection import get_pool
from church_assistant.shared import meetings_index, tenant_paths
from church_assistant.shared.logger import Logger
from church_assistant.web import encrypted
from church_assistant.web.routes.api import api_user
from church_assistant.web.routes.api_read import meeting_payload


router = APIRouter(prefix="/api/v1")
_logger = Logger(process="web")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class TopicEdit(BaseModel):
    title: Optional[str] = Field(default=None, max_length=300)
    body: Optional[str] = Field(default=None, max_length=20000)


@router.patch("/meetings/{date}/topics/{order}")
async def edit_topic(request: Request, date: str, order: int, body: TopicEdit):
    user = await api_user(request)
    if not _DATE_RE.match(date):
        raise HTTPException(status_code=404, detail="Немає такої зустрічі.")
    if body.title is None and body.body is None:
        raise HTTPException(status_code=400, detail="Нічого не змінено.")
    if body.title is not None and not body.title.strip():
        raise HTTPException(status_code=400, detail="Назва теми не може бути порожньою.")

    folder = tenant_paths.paths_for(user.tenant_slug).meetings / date
    if encrypted.folder_is_sealed(folder):
        raise HTTPException(status_code=409, detail="Ця зустріч зашифрована — редагувати теми з телефона поки неможливо.")
    polished = folder / "polished.md"
    if not polished.is_file():
        raise HTTPException(status_code=404, detail="У цієї зустрічі ще немає тем.")

    text = polished.read_text(encoding="utf-8")
    try:
        new_text, old = meetings_index.replace_topic(text, order, title=body.title, body=body.body)
    except meetings_index.NoSuchTopic:
        raise HTTPException(status_code=404, detail="Немає такої теми.")
    except ValueError:
        raise HTTPException(status_code=400, detail="Назва теми не може бути порожньою.")

    pool = await get_pool()
    changed: list[str] = []
    if new_text != text:
        tmp = polished.with_name(f"polished.md.{os.getpid()}.tmp")
        tmp.write_text(new_text, encoding="utf-8")
        os.replace(tmp, polished)
        new_title = old.title if body.title is None else " ".join(body.title.split())
        new_body = old.body if body.body is None else body.body.strip()
        for field, before, after in (("title", old.title, new_title), ("body", old.body, new_body)):
            if before != after:
                await topic_corrections_repo.record(
                    pool, user.tenant_id, meeting_date=date, topic_order=order,
                    field=field, before=before, after=after, user_id=user.user_id,
                )
                changed.append(field)
        await audit_repo.record(
            pool, tenant_id=user.tenant_id, action="topic.edited", actor=user.actor,
            resource=f"meeting/{date}", detail={"topic_order": order, "fields": changed, "title": new_title},
        )
        await _logger.info(
            "topic.edited", message=f"{date} topic {order}: {', '.join(changed)} — «{old.title}» → «{new_title}»",
            metadata={"date": date, "topic_order": order, "fields": changed},
            tenant_id=user.tenant_id, user_id=user.user_id,
        )
    return await meeting_payload(pool, user, date)
