"""
Home routes:
    GET /          — redirect to the dashboard (default landing)
    GET /meetings  — "Зустрічі": corpus overview + RAG query form
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from datetime import datetime, timedelta, timezone

from church_assistant.db import queries_repo, web_users_repo
from church_assistant.db.connection import get_pool
from church_assistant.shared import tenant_paths
from church_assistant.web.main import templates
from church_assistant.web.tenant import (
    current_tenant, current_tenant_slug, current_user,
)


router = APIRouter()


@router.get("/")
async def index():
    """Default landing → monitoring dashboard."""
    return RedirectResponse("/dashboard", status_code=307)


@router.get("/meetings", response_class=HTMLResponse)
async def meetings_home(request: Request):
    """'Зустрічі' — meetings corpus overview + RAG query form."""
    meetings_dir = tenant_paths.paths_for(current_tenant_slug(request)).meetings
    pool = await get_pool()
    tenant_id = current_tenant(request)

    # Imported from the meetings routes rather than duplicated: a list that
    # merges folders with protocols has to merge them the same way in both
    # places, and two copies of "which meetings exist" would drift the first
    # time one of them learned something.
    from church_assistant.web.routes.meetings import summaries_with_protocols

    summaries = await summaries_with_protocols(meetings_dir, pool, tenant_id)
    is_admin = current_user(request).role == "admin"
    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "meetings": summaries,
            "last_query_html": await _last_query_card(request, pool, tenant_id),
            # Only an admin opens a meeting, and only they need the list of
            # people who could chair it.
            "chair_choices": (
                await web_users_repo.list_active(pool, tenant_id)
                if is_admin else []
            ),
        },
    )


async def _last_query_card(request: Request, pool, tenant_id: int) -> str:
    """The card of the church's newest web question, if it was asked in the
    last day — rendered by routes/query.render_card so a reload of this page
    shows the same thing the form showed: still in the queue (and polling),
    or the answer. Older than a day it is history, and /history has it."""
    recent = await queries_repo.list_recent(pool, tenant_id, limit=1)
    if not recent or recent[0].get("source") != "web":
        return ""
    row = recent[0]
    asked = row.get("asked_at")
    if not isinstance(asked, datetime):
        return ""
    if asked.tzinfo is None:
        asked = asked.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - asked > timedelta(hours=24):
        return ""
    from church_assistant.web.routes.query import render_card
    return render_card(request, row).body.decode("utf-8")
