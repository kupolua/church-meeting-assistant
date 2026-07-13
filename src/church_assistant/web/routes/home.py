"""
Home route: GET / — landing page with sidebar + placeholder main panel.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from church_assistant.shared import meetings_index
from church_assistant.web.main import templates


router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def home(request: Request):
    """Render the base layout with meetings sidebar and empty main panel."""
    summaries = meetings_index.list_all_summaries()
    return templates.TemplateResponse(
        request,
        "home.html",
        {"meetings": summaries},
    )
