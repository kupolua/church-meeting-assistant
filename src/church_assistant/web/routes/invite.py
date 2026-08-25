"""
Redeeming an invitation:

    GET  /invite/{token}   the "choose your password" form
    POST /invite/{token}   set it, activate the account, sign in

THE ONLY PUBLIC ROUTE THAT ENDS IN A SESSION. Everything else behind the auth
gate is reached by signing in; this is reached by holding a link. That makes it
worth the same care as /login, and it is written to the same rules:

  - the token is 256 bits, single-use, expiring, and stored only as a hash;
  - expired, spent, unknown and suspended-church all render the SAME page, so a
    prober learns nothing from which message comes back;
  - redemption is one database function in one transaction, so two people
    opening the link at once cannot both succeed;
  - the route can do exactly one thing — set a first password on an account
    that has none. It takes no tenant from the request; the tenant is whatever
    the token names.

WHY IT EXISTS. Registering a church used to end with a generated password on the
operator's screen, to be forwarded somehow. That put the church's credentials in
the operator's hands and left a standing secret in a chat log. Here the password
does not exist until the invited person types it: nobody can pass on what was
never created.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from church_assistant.db import (
    audit_repo,
    web_invites_repo,
    web_sessions_repo,
    web_users_repo,
)
from church_assistant.db.connection import get_pool
from church_assistant.shared.logger import Logger
from church_assistant.web import auth, headers, security
from church_assistant.web.main import templates


router = APIRouter()

_logger = Logger(process="web")

MIN_PASSWORD_LEN = 8


def _page(
    request: Request,
    *,
    invite: Optional[dict[str, Any]] = None,
    error: Optional[str] = None,
    gone: bool = False,
) -> HTMLResponse:
    """
    Render the invite page.

    `gone` covers every way a link can fail to work — unknown, expired, already
    used, church suspended — deliberately as one state with one message. Telling
    them apart would confirm to a stranger that a token existed, or that a
    church does.
    """
    return templates.TemplateResponse(
        request,
        "invite.html",
        {"invite": invite, "error": error, "gone": gone,
         "min_password_len": MIN_PASSWORD_LEN},
        status_code=404 if gone else 200,
    )


@router.get("/invite/{token}", response_class=HTMLResponse)
async def invite_form(request: Request, token: str):
    """Show the form, if the link is still good for anything."""
    pool = await get_pool()
    invite = await web_invites_repo.resolve(pool, security.hash_token(token))
    if invite is None:
        return _page(request, gone=True)
    return _page(request, invite=invite)


@router.post("/invite/{token}", response_class=HTMLResponse)
async def invite_redeem(
    request: Request,
    token: str,
    password: str = Form(...),
    password_repeat: str = Form(...),
):
    """Set the first password, activate the account, and sign the person in."""
    pool = await get_pool()
    token_hash = security.hash_token(token)

    # Resolved twice on purpose: once to redraw the form with an error still
    # showing who it is for, and again inside redeem_web_invite, which is what
    # actually decides. The check that matters is the one in the transaction.
    invite = await web_invites_repo.resolve(pool, token_hash)
    if invite is None:
        return _page(request, gone=True)

    if len(password) < MIN_PASSWORD_LEN:
        return _page(request, invite=invite,
                     error=f"Пароль закороткий — мінімум {MIN_PASSWORD_LEN} символів.")
    if password != password_repeat:
        return _page(request, invite=invite, error="Паролі не збігаються.")

    user_id = await web_invites_repo.redeem(
        pool, token_hash, security.hash_password(password),
    )
    if user_id is None:
        # Between the resolve above and here somebody else spent it, or it
        # expired. The transaction is the authority, so believe it.
        return _page(request, gone=True)

    tenant_id = int(invite["tenant_id"])
    username = str(invite["username"])

    # Signed in immediately: making someone who just proved they hold the link
    # type the password they set one second ago teaches them nothing and loses
    # people at the last step.
    session_token = security.new_session_token()
    session_id = await web_sessions_repo.create(
        pool,
        tenant_id,
        web_user_id=user_id,
        token_hash=security.hash_token(session_token),
        ttl_seconds=security.SESSION_TTL_SECONDS,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )
    await web_users_repo.touch_login(pool, tenant_id, user_id)

    await audit_repo.record(
        pool,
        tenant_id=tenant_id,
        action="auth.invite_redeemed",
        actor=f"web:{username}",
        resource=f"web_users/{user_id}",
        detail={"invite_id": int(invite["invite_id"]), "session_id": session_id},
    )
    await _logger.info(
        "web.invite_redeemed",
        message=f"{username} set their first password (tenant {invite['tenant_slug']})",
        tenant_id=tenant_id,
    )

    # Where they belong, which is not the same for everyone the invite serves.
    # A founding church admin lands on their own accounts page — the first thing
    # they need is to add the rest of the council. A platform account has no
    # church at all and would be refused there by the panel guard, so it goes to
    # the fleet panel instead. Tenant 0 IS the platform (migration 012).
    landing = "/platform" if tenant_id == 0 else "/admin/users"
    response = RedirectResponse(landing, status_code=303)
    auth.set_session_cookie(
        response, session_token, secure=headers.cookie_secure(request)
    )
    return response
