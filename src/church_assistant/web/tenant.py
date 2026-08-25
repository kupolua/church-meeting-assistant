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


def require_platform(request: Request) -> SessionUser:
    """
    The logged-in user, but only if they run the platform rather than a church.

    404, not 403: a church admin has no business learning that a platform panel
    exists. Everywhere else in this file 403 is right, because those features
    are openly part of the product and the reader simply lacks a role.
    """
    user = current_user(request)
    if not user.is_platform_admin:
        raise HTTPException(status_code=404, detail="Not found")
    return user


def forbid_platform(request: Request) -> SessionUser:
    """
    The logged-in user, but only if they belong to a church.

    The other half of "the panels do not intersect". A platform account lives in
    `_system`, which owns no meetings, no voice profiles and no Qdrant
    collections — tenant_paths and collections refuse it by name. Without this
    guard a platform session reaching a church route would not leak anything; it
    would raise SystemTenantHasNoArtifacts somewhere deep and read as a crash.
    Refusing at the door says what actually happened.
    """
    user = current_user(request)
    if user.is_platform_admin:
        raise HTTPException(
            status_code=403,
            detail="Платформовий акаунт не має церкви — цей розділ не для нього.",
        )
    return user
