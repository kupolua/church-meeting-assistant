"""
Web account management (admin only):

    GET  /admin/users                     list this church's web accounts
    GET  /admin/users/panel               HTMX target — just the table
    POST /admin/users                     create an account + its invite link
    POST /admin/users/{id}/invite         issue a fresh invite link
    POST /admin/users/{id}/deactivate     revoke access (soft delete)
    POST /admin/users/{id}/reactivate     restore access
    POST /admin/users/{id}/role           promote/demote
    POST /admin/users/{id}/password       set a new password
    POST /admin/users/{id}/sessions/revoke  sign the account out everywhere

NOBODY HERE TYPES SOMEBODY ELSE'S PASSWORD. Creating an account used to ask the
admin for one, which meant the head of every church knew the password of every
person in it — the one place in the system where a secret still travelled from
hand to hand. Now creation makes an account with no usable password, inactive,
plus a single-use link; the person sets their own password by following it, and
the admin cannot learn what they chose. Same mechanism the platform panel uses
to found a church (web_invites, migration 011), applied one level down.

The password reset stays. It is the answer to "I forgot mine and I need in
today", it ends every session the old password could still be holding open, and
an admin who resets one has to say so out loud — whereas issuing an invite is
the same repair without a secret in the middle. Both are audited.

Everything is scoped to the logged-in admin's own church: the tenant comes from
the session and every query runs through tenant_cursor, so an admin of church A
cannot see — let alone edit — church B's accounts even by guessing an id.

TWO GUARD RAILS, both about not locking yourself out of your own church:
  - you cannot deactivate or demote YOURSELF (one misclick would leave you
    staring at a page you can no longer open);
  - the LAST active admin cannot be removed or demoted (the church would keep
    working but no one could ever add or reset an account again — recoverable
    only by someone with shell access to the server).

Every change is written to audit_log: account changes are exactly what the
наглядова рада needs to see.

CREATING A CHURCH IS NOT HERE. It briefly was, and the seam showed: a church was
registered from inside another church's admin page and then had nowhere to
appear. That belongs to web/routes/platform.py, which no church account can
reach and which cannot reach any church's contents.
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
from church_assistant.shared import meetings_index, tenant_paths
from church_assistant.shared.logger import Logger
from church_assistant.web import security
from church_assistant.web.main import templates
from church_assistant.web.tenant import (
    current_tenant,
    current_tenant_slug,
    current_user,
    require_admin,
)


router = APIRouter(prefix="/admin")

_logger = Logger(process="web")

MIN_PASSWORD_LEN = 8
MAX_USERNAME_LEN = 40


# ─────────────────────────────────────────────────────────────
# Context
# ─────────────────────────────────────────────────────────────

async def _panel_context(request: Request, pool: Any, tenant_id: int) -> dict[str, Any]:
    """Everything the refreshable account table needs."""
    users = await web_users_repo.list_all(pool, tenant_id)
    me = current_user(request)
    return {
        "users": users,
        "me": me,
        # One grouped query rather than one per row — the table shows "who is
        # signed in right now", which is the point of having a sessions table.
        "live_sessions": await web_sessions_repo.count_live_by_user(pool, tenant_id),
        # Rendered per row so the template doesn't re-derive the rule: the last
        # active admin, and yourself, are not removable.
        "n_active_admins": sum(
            1 for u in users if u["is_active"] and u["role"] == "admin"
        ),
        # Who has been handed a link and has not used it yet. Without this the
        # state is invisible: the account looks the same as one whose owner
        # never got round to signing in, and the admin re-issues blind.
        "pending_invites": {
            int(i["web_user_id"]): i
            for i in await web_invites_repo.list_pending(pool, tenant_id)
        },
        "invite_hours": web_invites_repo.DEFAULT_TTL_SECONDS // 3600,
    }


def _render_panel(request: Request, ctx: dict[str, Any]) -> HTMLResponse:
    return templates.TemplateResponse(request, "partials/admin_users_panel.html", ctx)


async def _panel_with(
    request: Request,
    *,
    error: Optional[str] = None,
    ok: Optional[str] = None,
    invited: Optional[dict[str, Any]] = None,
) -> HTMLResponse:
    """
    Re-render the table with a one-shot message (the HTMX action response).

    `invited` carries a freshly minted link. It is one-shot in the strong sense:
    the table stores only the token's hash, so if the admin navigates away
    without copying it, the only recovery is to issue another one.
    """
    pool = await get_pool()
    ctx = await _panel_context(request, pool, current_tenant(request))
    ctx["error"] = error
    ctx["ok"] = ok
    ctx["invited"] = invited
    return _render_panel(request, ctx)


async def _audit(
    pool: Any, request: Request, action: str, target_id: int, **detail: Any
) -> None:
    me = current_user(request)
    await audit_repo.record(
        pool,
        tenant_id=me.tenant_id,
        action=action,
        actor=me.actor,
        resource=f"web_users/{target_id}",
        detail=detail or None,
    )


# ─────────────────────────────────────────────────────────────
# Views
# ─────────────────────────────────────────────────────────────

@router.get("/users", response_class=HTMLResponse)
async def users_page(request: Request):
    """Full page. A non-admin is sent back to the dashboard, not shown a 403 blob."""
    user = current_user(request)
    if not user.is_admin:
        return RedirectResponse(
            "/dashboard?error=Керування+акаунтами+доступне+лише+адміністраторам",
            status_code=303,
        )

    pool = await get_pool()
    ctx = await _panel_context(request, pool, user.tenant_id)
    ctx["meetings"] = meetings_index.list_all_summaries(
        tenant_paths.paths_for(current_tenant_slug(request)).meetings
    )
    return templates.TemplateResponse(request, "admin_users.html", ctx)


@router.get("/users/panel", response_class=HTMLResponse)
async def users_panel(request: Request):
    """HTMX refresh target — just the table."""
    require_admin(request)
    return await _panel_with(request)


# ─────────────────────────────────────────────────────────────
# Create
# ─────────────────────────────────────────────────────────────

@router.post("/users", response_class=HTMLResponse)
async def create_user(
    request: Request,
    username: str = Form(...),
    full_name: str = Form(...),
    role: str = Form("member"),
):
    """
    Create a web account in the admin's own church — as an invitation.

    No password is taken, because taking one would mean the admin knows it. The
    account lands inactive with a hash of something nobody has, and the response
    carries a link that lets its owner, once, set a password of their own.
    """
    require_admin(request)
    pool = await get_pool()
    tenant_id = current_tenant(request)

    username = username.strip().lower()
    full_name = full_name.strip()

    if not username or len(username) > MAX_USERNAME_LEN:
        return await _panel_with(
            request, error=f"Логін має бути 1–{MAX_USERNAME_LEN} символів."
        )
    if not full_name:
        return await _panel_with(request, error="Вкажіть імʼя.")
    if role not in web_users_repo.ROLES:
        return await _panel_with(request, error="Невідома роль.")

    try:
        user_id = await web_users_repo.add_web_user(
            pool,
            tenant_id,
            username=username,
            # A hash of something nobody has, ever. Until the invite is redeemed
            # this account cannot be signed into — and it is deactivated too, so
            # the state reads as "not yet claimed" rather than as an account
            # that mysteriously refuses every password.
            password_hash=security.hash_password(security.new_session_token()),
            full_name=full_name,
            role=role,
        )
    except web_users_repo.WebUserAlreadyExists:
        # Deliberately the same message whether the clash is in this church or
        # another: an admin has no business learning who exists elsewhere.
        return await _panel_with(
            request, error=f"Логін «{username}» уже зайнятий. Оберіть інший."
        )
    except ValueError as e:
        return await _panel_with(request, error=str(e))

    await web_users_repo.deactivate(pool, tenant_id, user_id)
    invite_url, hours = await _issue_invite(request, pool, tenant_id, user_id)

    await _audit(pool, request, "admin.web_user_created", user_id,
                 username=username, role=role, invited=True)
    await _logger.info(
        "web.user_created",
        message=f"{current_user(request).username} created web account {username} "
                f"({role}) and issued an invite",
        tenant_id=tenant_id,
    )
    return await _panel_with(request, invited={
        "username": username, "full_name": full_name, "role": role,
        "url": invite_url, "hours": hours, "is_new": True,
    })


async def _issue_invite(
    request: Request, pool: Any, tenant_id: int, user_id: int,
) -> tuple[str, int]:
    """
    Mint a single-use link for an account. Returns (url, hours it lives).

    Any live invite for the same account is expired first. Two working links for
    one person is one more than anybody intended: the usual reason to re-issue
    is that the first went somewhere it should not have, and leaving it alive
    keeps open the exact door the admin came here to close.

    The token exists in this response and nowhere else — the table gets its
    hash, the log gets neither.
    """
    await web_invites_repo.expire_pending_for_user(pool, tenant_id, user_id)

    token = security.new_session_token()
    await web_invites_repo.create(
        pool,
        tenant_id,
        web_user_id=user_id,
        token_hash=security.hash_token(token),
        created_by=current_user(request).actor,
    )
    base = str(request.base_url).rstrip("/")
    return f"{base}/invite/{token}", web_invites_repo.DEFAULT_TTL_SECONDS // 3600


@router.post("/users/{user_id}/invite", response_class=HTMLResponse)
async def reinvite_user(request: Request, user_id: int):
    """
    Issue a fresh link — for a link that expired, went astray, or a forgotten
    password the admin would rather not choose on someone's behalf.

    Redeeming it sets a new password and activates the account, so this doubles
    as a reset that leaves the secret with its owner. Issuing one deliberately
    changes nothing yet: nothing has been taken away, and cutting somebody off
    the moment a link is minted — a link they may never follow — would be a
    surprise. The old password keeps working, and so do the sessions it opened,
    until the link is spent; redeeming it ends both, exactly as the 🔑 reset
    does. The moment the secret changes is the moment the old one stops working,
    and not a click earlier.

    On an account an admin has switched OFF, this is a way back in — redeeming
    reactivates. That is sometimes the point (somebody returning, nobody left
    holding the password), so it is allowed, but the confirmation says so: the
    admin is restoring access, not just re-issuing a link.
    """
    require_admin(request)
    pool = await get_pool()
    tenant_id = current_tenant(request)

    target, err = await _load_target(request, pool, user_id)
    if err is not None:
        return err

    invite_url, hours = await _issue_invite(request, pool, tenant_id, user_id)
    await _audit(pool, request, "admin.web_user_invited", user_id,
                 username=target["username"], was_active=bool(target["is_active"]))
    await _logger.info(
        "web.invite_issued",
        message=f"{current_user(request).username} issued an invite for "
                f"{target['username']}",
        tenant_id=tenant_id,
    )
    return await _panel_with(request, invited={
        "username": target["username"], "full_name": target["full_name"],
        "role": target["role"], "url": invite_url, "hours": hours,
        "is_new": False, "was_active": bool(target["is_active"]),
    })


# ─────────────────────────────────────────────────────────────
# Modify
# ─────────────────────────────────────────────────────────────

async def _load_target(
    request: Request, pool: Any, user_id: int
) -> tuple[Optional[dict[str, Any]], Optional[HTMLResponse]]:
    """The target row, or (None, rendered error) if it isn't in this church."""
    target = await web_users_repo.get_by_id(pool, current_tenant(request), user_id)
    if target is None:
        # RLS already restricted the lookup, so "not found" also covers
        # "belongs to another church" — and says nothing about which.
        return None, await _panel_with(request, error="Акаунт не знайдено.")
    return target, None


async def _blocks_removal(
    request: Request, pool: Any, target: dict[str, Any]
) -> Optional[str]:
    """Why this account may not be deactivated/demoted right now (None = it may)."""
    me = current_user(request)
    if target["id"] == me.user_id:
        return "Не можна змінити власний доступ — попросіть іншого адміністратора."
    if target["role"] == "admin" and target["is_active"]:
        users = await web_users_repo.list_all(pool, current_tenant(request))
        n_admins = sum(1 for u in users if u["is_active"] and u["role"] == "admin")
        if n_admins <= 1:
            return ("Це останній активний адміністратор — церква лишилася б без "
                    "керування акаунтами.")
    return None


@router.post("/users/{user_id}/deactivate", response_class=HTMLResponse)
async def deactivate_user(request: Request, user_id: int):
    """Revoke web access (soft delete — the audit trail stays)."""
    require_admin(request)
    pool = await get_pool()

    target, err = await _load_target(request, pool, user_id)
    if err is not None:
        return err
    blocked = await _blocks_removal(request, pool, target)
    if blocked:
        return await _panel_with(request, error=blocked)

    tenant_id = current_tenant(request)
    # This also expires any unspent invite for the account, in the same
    # statement — see web_users_repo.deactivate. An invite is a door too: it
    # sets a password and reactivates, so a link issued before this click would
    # undo it.
    _, n_invites = await web_users_repo.deactivate(pool, tenant_id, user_id)
    # Revoking is belt-and-braces: resolve_web_session() already refuses a
    # session whose account is inactive, so access is gone either way. Doing it
    # explicitly means the rows also *read* as ended, which is what an admin —
    # and the board — will look at afterwards.
    n = await web_sessions_repo.revoke_all_for_user(pool, tenant_id, user_id)

    await _audit(pool, request, "admin.web_user_deactivated", user_id,
                 username=target["username"], sessions_revoked=n,
                 invites_expired=n_invites)
    suffix = f" Активних сесій закрито: {n}." if n else ""
    if n_invites:
        suffix += " Невикористане запрошення анульовано."
    return await _panel_with(
        request,
        ok=f"Акаунт «{target['username']}» вимкнено — доступ припинено одразу.{suffix}",
    )


@router.post("/users/{user_id}/reactivate", response_class=HTMLResponse)
async def reactivate_user(request: Request, user_id: int):
    require_admin(request)
    pool = await get_pool()

    target, err = await _load_target(request, pool, user_id)
    if err is not None:
        return err

    await web_users_repo.reactivate(pool, current_tenant(request), user_id)
    await _audit(pool, request, "admin.web_user_reactivated", user_id,
                 username=target["username"])
    return await _panel_with(request, ok=f"Акаунт «{target['username']}» увімкнено.")


@router.post("/users/{user_id}/role", response_class=HTMLResponse)
async def set_role(request: Request, user_id: int, role: str = Form(...)):
    """Promote to admin / demote to member."""
    require_admin(request)
    pool = await get_pool()

    if role not in web_users_repo.ROLES:
        return await _panel_with(request, error="Невідома роль.")

    target, err = await _load_target(request, pool, user_id)
    if err is not None:
        return err
    if role != "admin":
        blocked = await _blocks_removal(request, pool, target)
        if blocked:
            return await _panel_with(request, error=blocked)

    await web_users_repo.set_role(pool, current_tenant(request), user_id, role)
    await _audit(pool, request, "admin.web_user_role_changed", user_id,
                 username=target["username"], old_role=target["role"], new_role=role)
    return await _panel_with(
        request, ok=f"Роль «{target['username']}» → {role}."
    )


@router.post("/users/{user_id}/sessions/revoke", response_class=HTMLResponse)
async def revoke_sessions(request: Request, user_id: int):
    """
    Sign an account out of every browser it is open in.

    Unlike deactivation this leaves the account usable — it is the "someone left
    a session open on a shared machine" lever, not the "revoke access" one. An
    admin may do it to themselves; their current session goes too, and the very
    next request bounces them to the login page.
    """
    require_admin(request)
    pool = await get_pool()

    target, err = await _load_target(request, pool, user_id)
    if err is not None:
        return err

    n = await web_sessions_repo.revoke_all_for_user(
        pool, current_tenant(request), user_id
    )
    await _audit(pool, request, "admin.web_user_sessions_revoked", user_id,
                 username=target["username"], sessions_revoked=n)

    if not n:
        return await _panel_with(
            request, ok=f"У «{target['username']}» не було активних сесій."
        )
    return await _panel_with(
        request,
        ok=f"Сесії «{target['username']}» закрито: {n}. Наступний запит "
           f"поверне на сторінку входу.",
    )


@router.post("/users/{user_id}/password", response_class=HTMLResponse)
async def set_password(request: Request, user_id: int, password: str = Form(...)):
    """Set a new password (an admin resetting someone who forgot theirs)."""
    require_admin(request)
    pool = await get_pool()

    if len(password) < MIN_PASSWORD_LEN:
        return await _panel_with(
            request, error=f"Пароль закороткий (мінімум {MIN_PASSWORD_LEN} символів)."
        )

    target, err = await _load_target(request, pool, user_id)
    if err is not None:
        return err

    me = current_user(request)
    tenant_id = me.tenant_id
    await web_users_repo.set_password_hash(
        pool, tenant_id, user_id, security.hash_password(password)
    )
    # A password reset is usually a response to "this account may be
    # compromised", so the old sessions must not outlive the old password.
    # Spare the acting admin's own session when they reset their own password —
    # otherwise the action logs them out of the page they just used.
    n = await web_sessions_repo.revoke_all_for_user(
        pool, tenant_id, user_id,
        except_session_id=me.session_id if user_id == me.user_id else None,
    )

    # The new password is never logged, here or in the audit detail.
    await _audit(pool, request, "admin.web_user_password_reset", user_id,
                 username=target["username"], sessions_revoked=n)
    await _logger.info(
        "web.password_reset",
        message=f"{me.username} reset password for {target['username']} "
                f"({n} session(s) ended)",
        tenant_id=tenant_id,
    )
    suffix = f" Активних сесій закрито: {n}." if n else ""
    return await _panel_with(
        request, ok=f"Пароль для «{target['username']}» змінено.{suffix}"
    )
