"""
Home routes:
    GET /          — redirect to the dashboard (default landing)
    GET /meetings  — "Зустрічі": corpus overview + RAG query form
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from church_assistant.shared import meetings_index, tenant_paths
from church_assistant.web.main import templates
from church_assistant.web.tenant import current_tenant_slug


router = APIRouter()


@router.get("/")
async def index():
    """Default landing → monitoring dashboard."""
    return RedirectResponse("/dashboard", status_code=307)


@router.get("/meetings", response_class=HTMLResponse)
async def meetings_home(request: Request):
    """'Зустрічі' — meetings corpus overview + RAG query form."""
    meetings_dir = tenant_paths.paths_for(current_tenant_slug(request)).meetings
    summaries = meetings_index.list_all_summaries(meetings_dir)
    return templates.TemplateResponse(
        request,
        "home.html",
        {"meetings": summaries},
    )
