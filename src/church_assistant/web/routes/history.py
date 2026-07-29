"""
History route: GET /history — list of past queries.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from church_assistant.db import queries_repo
from church_assistant.db.connection import get_pool
from church_assistant.shared import meetings_index, tenant_paths
from church_assistant.web.main import templates
from church_assistant.web.tenant import current_tenant, current_tenant_slug


router = APIRouter()


@router.get("/history", response_class=HTMLResponse)
async def history(request: Request, limit: int = 50):
    """Show this church's recent queries — both web and telegram."""
    pool = await get_pool()
    tenant_id = current_tenant(request)

    queries = await queries_repo.list_recent(pool, tenant_id, limit=limit)
    meetings_dir = tenant_paths.paths_for(current_tenant_slug(request)).meetings
    summaries = meetings_index.list_all_summaries(meetings_dir)

    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "queries": queries,
            "meetings": summaries,
        },
    )
