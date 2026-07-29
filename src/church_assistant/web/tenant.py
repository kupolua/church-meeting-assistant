"""
Web request → tenant resolution (MT Phase 3).

Until auth existed this returned a constant (tenant 1). Now the tenant comes
from the signed session cookie: whoever is logged in determines which church's
data the request may touch. AuthMiddleware has already verified the signature
and rejected anonymous requests, so by the time a route calls these the session
is present — a missing one means the middleware was not installed, which is a
bug, not a user error, and must fail loudly rather than silently fall back to
tenant 1 (that fallback would be a cross-tenant data leak).

Three views of the same session, because different layers key on different
things: the DB uses tenant_id (RLS), while the filesystem and Qdrant use the
slug.
"""

from __future__ import annotations

from fastapi import HTTPException, Request

from church_assistant.web.auth import SessionUser


def current_user(request: Request) -> SessionUser:
    """The logged-in web user. 401 if there is no verified session."""
    session = getattr(request.state, "session", None)
    if session is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return session


def current_tenant(request: Request) -> int:
    """The tenant id this request belongs to (for tenant_cursor / RLS)."""
    return current_user(request).tenant_id


def current_tenant_slug(request: Request) -> str:
    """The tenant slug (for per-tenant filesystem paths and Qdrant collections)."""
    return current_user(request).tenant_slug


def require_admin(request: Request) -> SessionUser:
    """The logged-in user, but only if they administer this tenant."""
    user = current_user(request)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin role required")
    return user
