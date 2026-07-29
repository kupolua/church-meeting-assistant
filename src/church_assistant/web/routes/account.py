"""
Your own account (every role):

    GET  /account/sessions                  where you are signed in
    GET  /account/sessions/panel            HTMX target — just the table
    POST /account/sessions/{id}/revoke      end one of them
    POST /account/sessions/revoke-others    end all except the one you're using

The admin page answers "who has access"; this one answers "where am I signed
in", which is the question a member can act on without needing an admin. It is
also the only honest way to use the sessions table: a person who suspects a
forgotten session on a shared machine should not have to ask someone else.

Everything here is scoped to the CALLER — the user id comes from the session,
never from the URL, so there is no id to tamper with. Revoking still goes
through the tenant-scoped repo, so RLS is the backstop underneath that.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from church_assistant.db import audit_repo, web_sessions_repo
from church_assistant.db.connection import get_pool
from church_assistant.shared import meetings_index, tenant_paths
from church_assistant.web import security
from church_assistant.web.main import templates
from church_assistant.web.tenant import current_tenant_slug, current_user


router = APIRouter(prefix="/account")


def _hours(seconds: int) -> int:
    """Whole hours, for the plain-language note under the table (0 = disabled)."""
    return round(seconds / 3600) if seconds > 0 else 0


async def _panel_context(request: Request) -> dict[str, Any]:
    me = current_user(request)
    pool = await get_pool()
    sessions = await web_sessions_repo.list_for_user(
        pool, me.tenant_id, me.user_id, limit=30
    )
    return {
        "me": me,
        "sessions": sessions,
        # Shown to the user rather than left implicit: "why did it log me out"
        # is otherwise unanswerable from inside the UI.
        "ttl_hours": _hours(security.SESSION_TTL_SECONDS),
        "idle_hours": _hours(security.SESSION_IDLE_SECONDS),
    }


def _render_panel(request: Request, ctx: dict[str, Any]) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "partials/account_sessions_panel.html", ctx
    )


async def _panel_with(request: Request, *, ok: str = "", error: str = "") -> HTMLResponse:
    ctx = await _panel_context(request)
    ctx["ok"] = ok
    ctx["error"] = error
    return _render_panel(request, ctx)


@router.get("/sessions", response_class=HTMLResponse)
async def sessions_page(request: Request):
    """Full page: every session of the signed-in account, current one marked."""
    ctx = await _panel_context(request)
    ctx["meetings"] = meetings_index.list_all_summaries(
        tenant_paths.paths_for(current_tenant_slug(request)).meetings
    )
    return templates.TemplateResponse(request, "account_sessions.html", ctx)


@router.get("/sessions/panel", response_class=HTMLResponse)
async def sessions_panel(request: Request):
    return await _panel_with(request)


@router.post("/sessions/{session_id}/revoke", response_class=HTMLResponse)
async def revoke_one(request: Request, session_id: int):
    """
    End one of your own sessions.

    The ownership check is explicit rather than implied by RLS: RLS scopes the
    row to the church, which would still let one member close a colleague's
    session. Ending sessions that are not yours is the admin page's job.
    """
    me = current_user(request)
    pool = await get_pool()

    mine = await web_sessions_repo.list_for_user(
        pool, me.tenant_id, me.user_id, limit=100
    )
    if not any(int(s["id"]) == session_id for s in mine):
        return await _panel_with(request, error="Сесію не знайдено.")

    await web_sessions_repo.revoke(pool, me.tenant_id, session_id)
    await audit_repo.record(
        pool,
        tenant_id=me.tenant_id,
        action="auth.session_revoked",
        actor=me.actor,
        resource=f"web_sessions/{session_id}",
        detail={"self_service": True,
                "was_current": session_id == me.session_id},
    )

    if session_id == me.session_id:
        # They just closed the session rendering this response; the swap will
        # land, and the next request bounces to /login.
        return await _panel_with(
            request, ok="Поточну сесію закрито — наступний перехід поверне на вхід."
        )
    return await _panel_with(request, ok="Сесію закрито.")


@router.post("/sessions/revoke-others", response_class=HTMLResponse)
async def revoke_others(request: Request):
    """
    Sign out everywhere except here — the "I left it open somewhere" button.

    Keeping the current session is what makes this usable without an admin: the
    alternative logs you out too, and then you cannot see that it worked.
    """
    me = current_user(request)
    pool = await get_pool()

    n = await web_sessions_repo.revoke_all_for_user(
        pool, me.tenant_id, me.user_id, except_session_id=me.session_id
    )
    await audit_repo.record(
        pool,
        tenant_id=me.tenant_id,
        action="auth.sessions_revoked_others",
        actor=me.actor,
        resource=f"web_users/{me.user_id}",
        detail={"sessions_revoked": n},
    )

    if not n:
        return await _panel_with(request, ok="Інших активних сесій не було.")
    return await _panel_with(request, ok=f"Закрито інших сесій: {n}.")
