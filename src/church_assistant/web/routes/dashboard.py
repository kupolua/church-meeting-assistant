"""
Dashboard route: GET /dashboard — live monitoring panel (MVP-B).

Shows queue depth, today's stats, dependency health, active queries,
whitelist users, and open errors. The panel auto-refreshes via HTMX
polling (GET /dashboard/panel every few seconds).

Actions (all return the refreshed panel partial so the UI updates in place):
    POST /dashboard/queries/{query_id}/cancel     — cancel a pending/processing query
    POST /dashboard/queries/{query_id}/requeue    — requeue a failed query back to pending
    POST /dashboard/users/{telegram_user_id}/deactivate — soft-delete a whitelist user
    POST /dashboard/errors/{error_id}/resolve     — mark an open error resolved
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from church_assistant.db import logs_repo, queries_repo, users_repo
from church_assistant.db.connection import get_pool
from church_assistant.shared import meetings_index
from church_assistant.web.main import templates


router = APIRouter()


# ─────────────────────────────────────────────────────────────
# Shared context builder (used by full page, poll, and actions)
# ─────────────────────────────────────────────────────────────

# Statuses that still have a pending outcome — shown in the "active" table
# with per-row actions.
_ACTIVE_STATUSES = ("pending", "processing", "failed")


async def _panel_context(pool: Any) -> dict[str, Any]:
    """Gather everything the refreshable panel needs, in one place."""
    queue = await queries_repo.get_queue_depth(pool)
    stats = await queries_repo.get_stats_today(pool)
    health = await logs_repo.get_latest_health(pool)
    users = await users_repo.list_active(pool)
    errors = await logs_repo.list_unresolved_errors(pool, limit=20)

    # Recent queries that are still actionable (pending/processing/failed).
    recent = await queries_repo.list_recent(pool, limit=50)
    active = [q for q in recent if q["status"] in _ACTIVE_STATUSES]

    return {
        "queue": queue,
        "stats": stats,
        "health": health,
        "users": users,
        "errors": errors,
        "active": active,
    }


def _render_panel(request: Request, ctx: dict[str, Any]) -> HTMLResponse:
    """Render just the refreshable panel partial (for poll + action responses)."""
    return templates.TemplateResponse(
        request,
        "partials/dashboard_panel.html",
        ctx,
    )


# ─────────────────────────────────────────────────────────────
# Views
# ─────────────────────────────────────────────────────────────

@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Full dashboard page (sidebar + polling panel shell)."""
    pool = await get_pool()
    ctx = await _panel_context(pool)
    ctx["meetings"] = meetings_index.list_all_summaries()
    return templates.TemplateResponse(request, "dashboard.html", ctx)


@router.get("/dashboard/panel", response_class=HTMLResponse)
async def dashboard_panel(request: Request):
    """HTMX poll target — returns only the refreshable panel."""
    pool = await get_pool()
    ctx = await _panel_context(pool)
    return _render_panel(request, ctx)


# ─────────────────────────────────────────────────────────────
# Actions (POST → mutate → return refreshed panel)
# ─────────────────────────────────────────────────────────────

@router.post("/dashboard/queries/{query_id}/cancel", response_class=HTMLResponse)
async def cancel_query(request: Request, query_id: int):
    """Cancel a pending/processing query."""
    pool = await get_pool()
    await queries_repo.cancel(pool, query_id)
    ctx = await _panel_context(pool)
    return _render_panel(request, ctx)


@router.post("/dashboard/queries/{query_id}/requeue", response_class=HTMLResponse)
async def requeue_query(request: Request, query_id: int):
    """Reset a failed query back to pending for another attempt."""
    pool = await get_pool()
    await queries_repo.requeue_for_retry(pool, query_id)
    ctx = await _panel_context(pool)
    return _render_panel(request, ctx)


@router.post(
    "/dashboard/users/{telegram_user_id}/deactivate",
    response_class=HTMLResponse,
)
async def deactivate_user(request: Request, telegram_user_id: int):
    """
    Soft-delete a whitelist user (revokes bot access, keeps audit trail).

    Admin-role users are protected — deactivating the (sole) admin would
    lock management out of the bot, so it's a no-op here.
    """
    pool = await get_pool()
    target = await users_repo.get_by_telegram_id(pool, telegram_user_id)
    if target is not None and target["role"] != "admin":
        await users_repo.deactivate(pool, telegram_user_id)
    ctx = await _panel_context(pool)
    return _render_panel(request, ctx)


@router.post("/dashboard/errors/{error_id}/resolve", response_class=HTMLResponse)
async def resolve_error(request: Request, error_id: int):
    """Mark an open error as resolved."""
    pool = await get_pool()
    await logs_repo.mark_error_resolved(pool, error_id)
    ctx = await _panel_context(pool)
    return _render_panel(request, ctx)
