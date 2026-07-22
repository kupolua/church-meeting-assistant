"""
Home routes:
    GET /          — redirect to the dashboard (default landing)
    GET /meetings  — "Зустрічі": corpus overview + RAG query form
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from church_assistant.shared import meetings_index
from church_assistant.web.main import templates


router = APIRouter()


@router.get("/")
async def index():
    """Default landing → monitoring dashboard."""
    return RedirectResponse("/dashboard", status_code=307)


@router.get("/meetings", response_class=HTMLResponse)
async def meetings_home(request: Request):
    """'Зустрічі' — meetings corpus overview + RAG query form."""
    summaries = meetings_index.list_all_summaries()
    return templates.TemplateResponse(
        request,
        "home.html",
        {"meetings": summaries},
    )
