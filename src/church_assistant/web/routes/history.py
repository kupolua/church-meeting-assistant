"""
History route: GET /history — list of past queries.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from church_assistant.db import queries_repo
from church_assistant.db.connection import get_pool
from church_assistant.shared import meetings_index
from church_assistant.web.main import templates


router = APIRouter()


@router.get("/history", response_class=HTMLResponse)
async def history(request: Request, limit: int = 50):
    """Show recent queries — both web (Pavlo) and telegram (team)."""
    pool = await get_pool()

    queries = await queries_repo.list_recent(pool, limit=limit)
    summaries = meetings_index.list_all_summaries()

    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "queries": queries,
            "meetings": summaries,
        },
    )
