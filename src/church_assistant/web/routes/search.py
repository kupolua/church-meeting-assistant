"""
Search route: GET /api/search?q=... — HTMX keyword search over topics.

Returns HTML partial (list of matching topics with links).
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from church_assistant.shared import meetings_index, tenant_paths
from church_assistant.web.main import templates
from church_assistant.web.tenant import current_tenant_slug


router = APIRouter(prefix="/api")


@router.get("/search", response_class=HTMLResponse)
async def search_endpoint(
    request: Request,
    q: str = Query("", description="Keyword to search"),
):
    """Instant keyword search across this tenant's topics."""
    q = q.strip()

    if not q:
        return templates.TemplateResponse(
            request,
            "partials/search_results.html",
            {"query": "", "matches": [], "empty_prompt": True},
        )

    meetings_dir = tenant_paths.paths_for(current_tenant_slug(request)).meetings
    matches = meetings_index.search_topics(meetings_dir, q, limit=30)

    return templates.TemplateResponse(
        request,
        "partials/search_results.html",
        {
            "query": q,
            "matches": matches,
            "empty_prompt": False,
        },
    )
