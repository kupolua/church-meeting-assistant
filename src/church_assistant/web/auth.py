"""
Web session plumbing — who is logged in, and gate everything behind it.

MT Phase 3. Before this, web/tenant.py returned a constant: one operator, one
church. With many churches on one server the tenant MUST come from the logged-in
account, so an unauthenticated request has no tenant at all and must be turned
away before it reaches any repo.

Pieces:
    SessionUser        — the decoded, signature-verified cookie payload
    AuthMiddleware     — rejects/redirects anonymous requests, publishes
                         request.state.session for downstream handlers
    set/clear_session_cookie — the two cookie mutations, in one place

The cookie itself (signing, expiry) lives in web/security.py. Note the payload
carries tenant_slug as well as tenant_id: the filesystem and Qdrant layers are
addressed by slug, and re-reading the registry on every request would be a DB
round-trip for a value that never changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from church_assistant.web import security


LOGIN_PATH = "/login"

# Reachable without a session. Everything else — including every HTMX poll
# target — requires one.
PUBLIC_PATHS = frozenset({LOGIN_PATH, "/logout", "/healthz", "/favicon.ico"})
PUBLIC_PREFIXES = ("/static/",)


@dataclass(frozen=True)
class SessionUser:
    """The authenticated web user for one request."""
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

    def to_payload(self) -> dict[str, Any]:
        """Compact cookie payload (short keys — cookies have a size budget)."""
        return {
            "uid": self.user_id,
            "tid": self.tenant_id,
            "slug": self.tenant_slug,
            "usr": self.username,
            "nam": self.full_name,
            "rol": self.role,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Optional["SessionUser"]:
        """Rebuild from a verified payload. None if any field is missing/odd."""
        try:
            return cls(
                user_id=int(payload["uid"]),
                tenant_id=int(payload["tid"]),
                tenant_slug=str(payload["slug"]),
                username=str(payload["usr"]),
                full_name=str(payload.get("nam", "")),
                role=str(payload.get("rol", "member")),
            )
        except (KeyError, TypeError, ValueError):
            return None


# ─────────────────────────────────────────────────────────────
# Cookie helpers
# ─────────────────────────────────────────────────────────────

def set_session_cookie(response: Response, user: SessionUser) -> None:
    """Issue a fresh signed session cookie for this user."""
    token = security.sign_session(user.to_payload())
    response.set_cookie(
        security.SESSION_COOKIE,
        token,
        max_age=security.SESSION_TTL_SECONDS,
        httponly=True,          # not readable from JS → XSS can't lift the session
        samesite="lax",         # blocks cross-site POSTs while keeping normal nav
        secure=False,           # served over plain HTTP on the LAN; flip with TLS
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(security.SESSION_COOKIE, path="/")


def read_session(request: Request) -> Optional[SessionUser]:
    """Decode + verify this request's session cookie (None if absent/invalid)."""
    payload = security.load_session(request.cookies.get(security.SESSION_COOKIE))
    if payload is None:
        return None
    return SessionUser.from_payload(payload)


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
        session = read_session(request)
        request.state.session = session

        if session is not None or is_public(request.url.path):
            return await call_next(request)

        if request.headers.get("HX-Request") == "true":
            response = Response(status_code=401)
            response.headers["HX-Redirect"] = LOGIN_PATH
            return response

        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        next_param = "" if target == "/" else f"?next={_quote(target)}"
        return RedirectResponse(f"{LOGIN_PATH}{next_param}", status_code=303)


def _quote(value: str) -> str:
    from urllib.parse import quote
    return quote(value, safe="")
