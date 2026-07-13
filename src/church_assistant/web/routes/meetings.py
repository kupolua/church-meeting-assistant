"""
Meetings routes:
    GET /meetings/{date} — meeting detail page (attendees + collapsible topics)
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from church_assistant.shared import meetings_index
from church_assistant.web.main import templates


router = APIRouter(prefix="/meetings")


@router.get("/{date}", response_class=HTMLResponse)
async def meeting_detail(request: Request, date: str):
    """Render meeting detail page for a given date (YYYY-MM-DD)."""
    detail = meetings_index.load_detail(date)
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail=f"Meeting {date!r} not found",
        )

    summaries = meetings_index.list_all_summaries()

    return templates.TemplateResponse(
        request,
        "meeting_detail.html",
        {
            "detail": detail,
            "meetings": summaries,
            "current_date": date,
        },
    )
