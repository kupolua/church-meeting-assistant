"""
Auth routes:
    GET  /login   — login form
    POST /login   — verify credentials → issue session cookie → redirect
    POST /logout  — drop the session cookie

The login flow is the tenant bootstrap for the web side, and mirrors the bot's
(bot/middleware/whitelist.py) step for step:

    1. resolve_login_tenant(username, church)  — SECURITY DEFINER, bypasses RLS,
       because web_users is RLS-gated and we don't know the tenant yet;
    2. everything after that runs INSIDE that tenant's context (tenant_cursor),
       so a bug below step 1 can't read another church's rows;
    3. the tenant must be active — a suspended church can't be logged into even
       with valid credentials.

Since migration 014 a login name is unique within a church, not across the
server, so step 1 can come back with "more than one church has this name". Then
the form asks which one — BEFORE any password is checked. The other way round
(verify against every candidate, ask only if two match) would pay one hash per
candidate for a single guess, test that guess against several people's secrets
at once, and make the question itself proof to whoever triggered it that the
password was right. Which churches exist is not a secret worth this; whether a
password is valid is.

Failures are deliberately indistinguishable to the user (same message, same
cost) so the form can't be used to enumerate accounts. A wrong church is one of
those failures: same message, same burnt scrypt, so it cannot be used to ask
"is this name in THAT church?".
"""

from __future__ import annotations

import time
from typing import Any, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from church_assistant.db import (
    audit_repo, tenants_repo, web_sessions_repo, web_users_repo,
)
from church_assistant.db.connection import get_pool
from church_assistant.db.tenant_context import login_tenants
from church_assistant.shared.logger import Logger
from church_assistant.web import auth, headers, security
from church_assistant.web.main import templates


router = APIRouter()

_logger = Logger(process="web")

GENERIC_ERROR = "Невірний логін або пароль."
LOCKED_ERROR = "Забагато невдалих спроб. Спробуйте за кілька хвилин."
# Not an error — a question. The name is fine, it just belongs to more than one
# church, and only its owner knows which. Nothing has been checked yet.
# Which church this browser signed into last. A hint, never an authority: it
# only decides who to check the password against first, and the password still
# has to match that church's row. A stale or hand-edited value costs one extra
# scrypt round and nothing else, so it needs no signature.
CHURCH_HINT_COOKIE = "cma_church"
CHURCH_HINT_MAX_AGE = 365 * 24 * 3600

AMBIGUOUS_NOTICE = (
    "Такий логін є в кількох церквах. Вкажіть свою — і введіть пароль ще раз."
)


def _hinted_tenant(request: Request) -> Optional[int]:
    """The church this browser used last, if it left a readable trace."""
    raw = request.cookies.get(CHURCH_HINT_COOKIE)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _remember_church(response, tenant_id: int, *, secure: bool) -> None:
    """
    Leave the hint, so this browser is never asked again.

    Set on every successful sign-in, including the one that ends an invite —
    which is the important one: the person who has just chosen their password
    knows their church least, and that flow already knows it for them.
    """
    response.set_cookie(
        CHURCH_HINT_COOKIE,
        str(tenant_id),
        max_age=CHURCH_HINT_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )

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


def _record_failure(key: str, checks: int = 1) -> None:
    """
    Spend `checks` of the budget — one entry per password verification, not per
    submitted form. Since 015 a single submission is checked against every
    account carrying the name, so counting submissions would let one request buy
    N guesses. The person who pays for that is a namesake with a typo, who gets
    fewer tries than someone with an unshared login; the person it protects is
    whoever among the namesakes has the weakest password.
    """
    cutoff = time.time() - LOCKOUT_WINDOW
    recent = [t for t in _failures.get(key, []) if t > cutoff]
    recent.extend([time.time()] * max(1, checks))
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
    notice: Optional[str] = None,
    username: str = "",
    church: str = "",
    ask_church: bool = False,
    next_url: str = "/",
    status_code: int = 200,
) -> HTMLResponse:
    """
    The form. `ask_church` adds the church field — shown only once we know the
    name is shared, so nobody is asked a question they don't need to answer.

    The password is never passed back. Re-rendering it into a hidden field
    would put it in the page source, in the browser's history, and in every
    error page after it; one retype in a rare case is the cheaper side.
    """
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": error, "notice": notice, "username": username,
         "church": church, "ask_church": ask_church, "next": next_url},
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
    church: str = Form(""),
    next: str = Form("/"),
):
    """Verify credentials and start a session."""
    username = username.strip().lower()
    church = church.strip().lower()
    next_url = _safe_next(next)
    key = _client_key(request, username)

    if _is_locked(key):
        return _render_login(
            request, error=LOCKED_ERROR, username=username, church=church,
            next_url=next_url, status_code=429,
        )

    pool = await get_pool()

    # ─── 1. Who could this be? (bypasses RLS by design) ──────
    # Candidates, not a decision: since 015 the password is what picks between
    # namesakes, and it is checked against each of them below.
    candidates = await login_tenants(pool, username, church or None)

    if not candidates:
        # Unknown account — or a church that does not hold this name, which must
        # be indistinguishable from it. Burn the same scrypt round a real check
        # would, so timing doesn't reveal which usernames exist or where.
        security.waste_time_like_a_real_check()
        _record_failure(key)
        # No tenant: nothing to scope this to, so it goes to `_system`.
        # (The bad-password branch below DOES know the church and says so.)
        await _logger.warn(
            "web.login_failed",
            message=f"unknown web user {username!r}",
            metadata={"username": username, "reason": "unknown_user",
                      "church_given": bool(church)},
        )
        return _render_login(
            request, error=GENERIC_ERROR, username=username, church=church,
            next_url=next_url, status_code=401,
        )

    # ─── 2. Check the password — inside each candidate's own tenant ──
    # The hint first, when this browser has one: a returning member pays one
    # scrypt round instead of N, and the church question never reaches them.
    # It grants nothing on its own — the password still has to match that
    # church's row — so a stale or forged value only costs the extra round.
    hint = _hinted_tenant(request)
    order = candidates if hint not in candidates else (
        [hint] + [t for t in candidates if t != hint]
    )

    async def _row(tid: int):
        row = await web_users_repo.get_by_username(pool, tid, username)
        return row if row is not None and row["is_active"] else None

    if hint in candidates:
        row = await _row(hint)
        if row is not None and security.verify_password(
            password, row["password_hash"]
        ):
            return await _finish_login(request, pool, hint, row, key, next_url)
        # The hint was wrong for this person — a shared computer, or they moved
        # church. Fall through and check the rest; a wrong hint must never turn
        # a valid login into a refusal.
        order = [t for t in order if t != hint]

    # No early exit: stopping at the first match would let response time say
    # WHICH namesake answered, which is the one thing the church question is
    # there to keep private.
    matches: list[tuple[int, Any]] = []
    checked = 0
    for tid in order:
        row = await _row(tid)
        if row is None:
            continue
        checked += 1
        if security.verify_password(password, row["password_hash"]):
            matches.append((tid, row))

    if not matches:
        _record_failure(key, checks=max(1, checked))
        if len(candidates) == 1:
            # One church, so the failure has an address and its board should see
            # it. With namesakes there is no telling who was aimed at.
            row = await _row(candidates[0])
            if row is not None:
                await audit_repo.record(
                    pool,
                    tenant_id=candidates[0],
                    action="auth.login_failed",
                    actor=f"web:{username}",
                    resource=f"web_users/{row['id']}",
                    detail={"reason": "bad_password"},
                )
        await _logger.warn(
            "web.login_failed",
            message=f"bad password for {username!r}",
            metadata={"username": username, "reason": "bad_password",
                      "candidates": len(candidates)},
        )
        # Deliberately no church field here: a wrong password is a wrong
        # password, and asking a member for an identifier they have never seen
        # — over a typo — is what this design exists to avoid.
        return _render_login(
            request, error=GENERIC_ERROR, username=username, church=church,
            next_url=next_url, status_code=401,
        )

    if len(matches) > 1:
        # Two people, one name, one password. This is the only case the password
        # cannot settle, so now — and only now — the person is asked. The
        # question says nothing about the password: it names no church, and the
        # same page appears whichever of them is signing in.
        _record_failure(key, checks=max(1, checked))
        return _render_login(
            request, username=username, ask_church=True,
            notice=AMBIGUOUS_NOTICE, next_url=next_url,
        )

    tenant_id, user_row = matches[0]
    return await _finish_login(request, pool, tenant_id, user_row, key, next_url)


async def _finish_login(
    request: Request, pool: Any, tenant_id: int, user_row: Any,
    key: str, next_url: str,
):
    """The part after the password is right: church still open, then a session."""
    username = str(user_row["username"])
    church = ""

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
            username=username, church=church,
            next_url=next_url, status_code=403,
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
    secure = headers.cookie_secure(request)
    auth.set_session_cookie(response, token, secure=secure)
    _remember_church(response, tenant_id, secure=secure)
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
