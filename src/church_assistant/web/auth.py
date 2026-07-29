"""
Web session plumbing — who is logged in, and gate everything behind it.

MT Phase 3. Before this, web/tenant.py returned a constant: one operator, one
church. With many churches on one server the tenant MUST come from the logged-in
account, so an unauthenticated request has no tenant at all and must be turned
away before it reaches any repo.

Sessions are SERVER-SIDE (migration 008). The cookie is a signed pointer; the
web_sessions row is the authority. Two consequences worth knowing:

  - every gated request costs one indexed lookup. That is the price of being
    able to revoke access, and it is the right trade for a system whose whole
    premise is that a supervisory board can see and control who reads what;
  - identity is read fresh each time, so a demotion, a rename, a disabled
    account or a suspended church all take effect on the NEXT request — not at
    the next login.

Pieces:
    SessionUser        — the identity this request runs as
    AuthMiddleware     — rejects/redirects anonymous requests, publishes
                         request.state.session for downstream handlers
    set/clear_session_cookie — the two cookie mutations, in one place
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from church_assistant.db import web_sessions_repo
from church_assistant.db.connection import get_pool
from church_assistant.web import security


LOGIN_PATH = "/login"

# Reachable without a session. Everything else — including every HTMX poll
# target — requires one.
PUBLIC_PATHS = frozenset({LOGIN_PATH, "/logout", "/healthz", "/favicon.ico"})
PUBLIC_PREFIXES = ("/static/",)


@dataclass(frozen=True)
class SessionUser:
    """The authenticated web user for one request."""
    session_id: int
    user_id: int
    tenant_id: int
    tenant_slug: str
    username: str
    full_name: str
    role: str                      # 'member' | 'admin'

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def actor(self) -> str:
        """Audit-log actor string, e.g. 'web:pavlo'."""
        return f"web:{self.username}"

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "SessionUser":
        """Build from resolve_web_session()'s output."""
        return cls(
            session_id=int(row["session_id"]),
            user_id=int(row["web_user_id"]),
            tenant_id=int(row["tenant_id"]),
            tenant_slug=str(row["tenant_slug"]),
            username=str(row["username"]),
            full_name=str(row["full_name"] or ""),
            role=str(row["role"] or "member"),
        )


# ─────────────────────────────────────────────────────────────
# Cookie helpers
# ─────────────────────────────────────────────────────────────

def set_session_cookie(response: Response, token: str, *, secure: bool) -> None:
    """
    Hand the browser a signed pointer to its web_sessions row.

    `secure` is decided per request from the scheme (security.cookie_secure_for)
    rather than fixed at import: the same build runs on plain HTTP on a church
    LAN and behind TLS on the shared server, and hard-coding either one makes
    the other either impossible to log into or quietly insecure.
    """
    response.set_cookie(
        security.SESSION_COOKIE,
        security.sign_session({"sid": token}),
        max_age=security.SESSION_TTL_SECONDS,
        httponly=True,          # not readable from JS → XSS can't lift the session
        samesite="lax",         # blocks cross-site POSTs while keeping normal nav
        secure=secure,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(security.SESSION_COOKIE, path="/")


def read_token(request: Request) -> Optional[str]:
    """
    The session token from this request's cookie, if the signature checks out.

    Verifying the signature here means a junk or tampered cookie is rejected
    in-process and never reaches the database.
    """
    payload = security.load_session(request.cookies.get(security.SESSION_COOKIE))
    if not payload:
        return None
    token = payload.get("sid")
    return token if isinstance(token, str) and token else None


async def read_session(request: Request) -> Optional[SessionUser]:
    """Resolve this request's identity against the sessions table."""
    token = read_token(request)
    if token is None:
        return None
    pool = await get_pool()
    row = await web_sessions_repo.resolve(
        pool,
        security.hash_token(token),
        idle_seconds=security.SESSION_IDLE_SECONDS,
    )
    if row is None:
        return None
    return SessionUser.from_row(row)


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


# ─────────────────────────────────────────────────────────────
# Middleware
# ─────────────────────────────────────────────────────────────

class AuthMiddleware(BaseHTTPMiddleware):
    """
    Deny-by-default gate: no valid session → no handler runs.

    Whole-page requests get a redirect to /login (preserving where they were
    going). HTMX requests can't follow a redirect usefully — htmx would swap the
    login page into a fragment slot — so they get 401 + HX-Redirect, which tells
    htmx to navigate the whole window instead.
    """

    async def dispatch(self, request: Request, call_next):
        # Skip the lookup entirely for /login and /static: they don't need an
        # identity, and this is every request for the CSS on the login page.
        public = is_public(request.url.path)
        session = None if public else await read_session(request)
        request.state.session = session

        if public:
            return await call_next(request)

        if session is None:
            # A cookie that no longer resolves is stale (revoked, expired, or the
            # account was disabled) — clear it so the browser stops sending it.
            return _reject(request)

        response = await call_next(request)
        await self._touch(session)
        return response

    @staticmethod
    async def _touch(session: SessionUser) -> None:
        """Refresh last_seen_at (throttled inside the repo; never raises)."""
        pool = await get_pool()
        await web_sessions_repo.touch(pool, session.tenant_id, session.session_id)


def _reject(request: Request) -> Response:
    """Turn an unauthenticated request away in the form its caller can use."""
    if request.headers.get("HX-Request") == "true":
        response: Response = Response(status_code=401)
        response.headers["HX-Redirect"] = LOGIN_PATH
        clear_session_cookie(response)
        return response

    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    next_param = "" if target == "/" else f"?next={_quote(target)}"
    response = RedirectResponse(f"{LOGIN_PATH}{next_param}", status_code=303)
    clear_session_cookie(response)
    return response


def _quote(value: str) -> str:
    from urllib.parse import quote
    return quote(value, safe="")
