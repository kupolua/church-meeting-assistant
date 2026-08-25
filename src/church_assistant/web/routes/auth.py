"""
Auth routes:
    GET  /login   — login form
    POST /login   — verify credentials → issue session cookie → redirect
    POST /logout  — drop the session cookie

The login flow is the tenant bootstrap for the web side, and mirrors the bot's
(bot/middleware/whitelist.py) step for step:

    1. resolve_tenant_for_web_user(username)  — SECURITY DEFINER, bypasses RLS,
       because web_users is RLS-gated and we don't know the tenant yet;
    2. everything after that runs INSIDE that tenant's context (tenant_cursor),
       so a bug below step 1 can't read another church's rows;
    3. the tenant must be active — a suspended church can't be logged into even
       with valid credentials.

Failures are deliberately indistinguishable to the user (same message, same
cost) so the form can't be used to enumerate accounts.
"""

from __future__ import annotations

import time
from typing import Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from church_assistant.db import (
    audit_repo, tenants_repo, web_sessions_repo, web_users_repo,
)
from church_assistant.db.connection import get_pool
from church_assistant.db.tenant_context import resolve_tenant_for_web_user
from church_assistant.shared.logger import Logger
from church_assistant.web import auth, headers, security
from church_assistant.web.main import templates


router = APIRouter()

_logger = Logger(process="web")

GENERIC_ERROR = "Невірний логін або пароль."
LOCKED_ERROR = "Забагато невдалих спроб. Спробуйте за кілька хвилин."

# Brute-force brake. scrypt already costs ~100 ms per attempt, so this is about
# stopping a slow grind, not a fast one — hence generous limits and a short
# window. In-process only: the web app is a single uvicorn process, and a
# restart clearing the counters is an acceptable trade for zero extra state.
MAX_FAILURES = 8
LOCKOUT_WINDOW = 15 * 60          # seconds

_failures: dict[str, list[float]] = {}


def _client_key(request: Request, username: str) -> str:
    host = request.client.host if request.client else "?"
    return f"{host}|{username}"


def _is_locked(key: str) -> bool:
    cutoff = time.time() - LOCKOUT_WINDOW
    recent = [t for t in _failures.get(key, []) if t > cutoff]
    if recent:
        _failures[key] = recent
    else:
        _failures.pop(key, None)
    return len(recent) >= MAX_FAILURES


def _record_failure(key: str) -> None:
    cutoff = time.time() - LOCKOUT_WINDOW
    recent = [t for t in _failures.get(key, []) if t > cutoff]
    recent.append(time.time())
    _failures[key] = recent


def _safe_next(raw: Optional[str]) -> str:
    """
    Where to land after login. Only same-site absolute paths are honoured — an
    attacker-supplied ?next=https://evil.example would otherwise turn our login
    form into an open redirect.
    """
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return "/"
    parts = urlsplit(raw)
    if parts.scheme or parts.netloc:
        return "/"
    return raw


def _render_login(
    request: Request,
    *,
    error: Optional[str] = None,
    username: str = "",
    next_url: str = "/",
    status_code: int = 200,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": error, "username": username, "next": next_url},
        status_code=status_code,
    )


# ─────────────────────────────────────────────────────────────
# Views
# ─────────────────────────────────────────────────────────────

@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, next: str = "/"):
    """Show the login form (or bounce an already-signed-in user onward)."""
    if await auth.read_session(request) is not None:
        return RedirectResponse(_safe_next(next), status_code=303)
    return _render_login(request, next_url=_safe_next(next))


@router.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    """Verify credentials and start a session."""
    username = username.strip().lower()
    next_url = _safe_next(next)
    key = _client_key(request, username)

    if _is_locked(key):
        return _render_login(
            request, error=LOCKED_ERROR, username=username,
            next_url=next_url, status_code=429,
        )

    pool = await get_pool()

    # ─── 1. Tenant bootstrap (bypasses RLS by design) ────────
    tenant_id = await resolve_tenant_for_web_user(pool, username)
    if tenant_id is None:
        # Unknown/disabled account. Burn the same scrypt round a real check
        # would, so timing doesn't reveal which usernames exist.
        security.waste_time_like_a_real_check()
        _record_failure(key)
        # No tenant_id: we could not resolve one, so this goes to `_system`.
        # (The bad-password branch below DOES know the church and says so.)
        await _logger.warn(
            "web.login_failed",
            message=f"unknown web user {username!r}",
            metadata={"username": username, "reason": "unknown_user"},
        )
        return _render_login(
            request, error=GENERIC_ERROR, username=username,
            next_url=next_url, status_code=401,
        )

    # ─── 2. Everything below is tenant-scoped (RLS active) ───
    user_row = await web_users_repo.get_by_username(pool, tenant_id, username)
    if user_row is None or not user_row["is_active"]:
        security.waste_time_like_a_real_check()
        _record_failure(key)
        return _render_login(
            request, error=GENERIC_ERROR, username=username,
            next_url=next_url, status_code=401,
        )

    if not security.verify_password(password, user_row["password_hash"]):
        _record_failure(key)
        await audit_repo.record(
            pool,
            tenant_id=tenant_id,
            action="auth.login_failed",
            actor=f"web:{username}",
            resource=f"web_users/{user_row['id']}",
            detail={"reason": "bad_password"},
        )
        await _logger.warn(
            "web.login_failed",
            message=f"bad password for {username!r}",
            metadata={"username": username, "reason": "bad_password"},
            tenant_id=tenant_id,
        )
        return _render_login(
            request, error=GENERIC_ERROR, username=username,
            next_url=next_url, status_code=401,
        )

    # ─── 3. The church itself must still be active ───────────
    # `_system` (tenant 0) is inactive by design since 007, so that nobody signs
    # in "as the platform" for free. A platform account is the one legitimate
    # exception (migration 012), and the exception is narrow: the tenant must be
    # 0 AND the account must carry the flag. This mirrors resolve_web_session
    # exactly — if the two ever disagree, one of them lets somebody in that the
    # other would refuse, and which one wins depends on the request.
    tenant = await tenants_repo.get_by_id(pool, tenant_id)
    is_platform = bool(user_row.get("is_platform_admin")) and tenant_id == 0
    if tenant is None or (not tenant["is_active"] and not is_platform):
        _record_failure(key)
        await _logger.warn(
            "web.login_refused",
            message=f"tenant {tenant_id} inactive — login refused for {username!r}",
            tenant_id=tenant_id,
        )
        return _render_login(
            request,
            error="Доступ до цієї церкви призупинено. Зверніться до адміністратора.",
            username=username, next_url=next_url, status_code=403,
        )

    # ─── 4. Open a server-side session ───────────────────────
    _failures.pop(key, None)
    user_id = int(user_row["id"])

    # The plaintext token exists only here and in the browser; the row keeps
    # nothing but its hash.
    token = security.new_session_token()
    session_id = await web_sessions_repo.create(
        pool,
        tenant_id,
        web_user_id=user_id,
        token_hash=security.hash_token(token),
        ttl_seconds=security.SESSION_TTL_SECONDS,
        user_agent=request.headers.get("user-agent"),
        ip=request.client.host if request.client else None,
    )

    await web_users_repo.touch_login(pool, tenant_id, user_id)
    await audit_repo.record(
        pool,
        tenant_id=tenant_id,
        action="auth.login",
        actor=f"web:{username}",
        resource=f"web_users/{user_id}",
        detail={"role": str(user_row["role"]), "session_id": session_id},
    )
    await _logger.info(
        "web.login",
        message=f"{username} signed in (tenant {tenant['slug']}, session {session_id})",
        tenant_id=tenant_id,
    )

    # Opportunistic cleanup — long-dead rows, not the ones an admin still wants
    # to look at. Login is rare enough to carry it and never on the hot path.
    await web_sessions_repo.purge_expired(pool)

    response = RedirectResponse(next_url, status_code=303)
    auth.set_session_cookie(response, token, secure=headers.cookie_secure(request))
    return response


@router.post("/logout")
async def logout(request: Request):
    """
    End the session server-side, then drop the cookie.

    Order matters: revoking first means the session is dead even if the browser
    keeps (or has already copied) the cookie. Clearing the cookie alone would
    have been the whole of "sign out" under the old self-contained scheme.
    """
    session = await auth.read_session(request)
    if session is not None:
        pool = await get_pool()
        await web_sessions_repo.revoke(pool, session.tenant_id, session.session_id)
        await audit_repo.record(
            pool,
            tenant_id=session.tenant_id,
            action="auth.logout",
            actor=session.actor,
            resource=f"web_users/{session.user_id}",
            detail={"session_id": session.session_id},
        )

    response = RedirectResponse("/login", status_code=303)
    auth.clear_session_cookie(response)
    return response
