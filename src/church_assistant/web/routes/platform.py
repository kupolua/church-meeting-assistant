"""
The platform panel — running the fleet, not a congregation:

    GET  /platform                 churches, their size, pending invites
    GET  /platform/panel           HTMX target — just the table
    POST /platform/churches        register a church + its first admin
    POST /platform/churches/{id}/suspend    stop a church signing in
    POST /platform/churches/{id}/resume     let it back
    POST /platform/churches/{id}/rename     change its display name
    POST /platform/churches/{id}/archive    retire it, keeping the data a year
    POST /platform/churches/{id}/restore    bring it back out of the archive

SEPARATE FROM /admin/users BY DESIGN. That page belongs to one church and shows
its people; this one belongs to no church and shows churches. They used to be
the same screen, and the seam showed the moment a church was registered and had
nowhere to appear.

WHAT THIS PANEL CANNOT DO, deliberately: read anything inside a church. Names,
identifiers, counts and dates — the facts needed to run a service — and not one
protocol, transcript or recording. Registering a church is not the same as being
allowed to read its conversations, and the isolation the churches were promised
has to hold against the operator too. RLS is what actually enforces it: a
platform session carries tenant 0, so every tenant-scoped query it could make
returns nothing.

The counts below therefore come from a SECURITY DEFINER function that returns
NUMBERS ONLY (migration 012's companion, v_platform_churches). Reaching them by
setting app.current_tenant per church would work and would be the wrong shape:
it would put "become that church for a moment" into the codebase, ready for the
next person to reuse for something less innocent.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from church_assistant.db import audit_repo, tenants_repo, web_users_repo
from church_assistant.db.connection import get_pool
from church_assistant.shared import tenant_paths
from church_assistant.shared.logger import Logger
from church_assistant.web import security
from church_assistant.web.main import templates
from church_assistant.web.tenant import current_user, require_platform


router = APIRouter(prefix="/platform")

_logger = Logger(process="web")

MAX_CHURCH_NAME_LEN = 120
MAX_USERNAME_LEN = 40


async def _panel_context(request: Request, pool: Any) -> dict[str, Any]:
    """Churches with their sizes — numbers only, never their contents."""
    return {
        "churches": await tenants_repo.list_with_counts(pool),
        "archived": await tenants_repo.list_archived(pool),
        "retention_days": tenants_repo.ARCHIVE_RETENTION_DAYS,
        "me": current_user(request),
    }


def _render_panel(request: Request, ctx: dict[str, Any]) -> HTMLResponse:
    return templates.TemplateResponse(request, "partials/platform_panel.html", ctx)


async def _panel_with(
    request: Request, *, error: Optional[str] = None, created: Optional[dict] = None,
) -> HTMLResponse:
    pool = await get_pool()
    ctx = await _panel_context(request, pool)
    ctx["error"] = error
    ctx["created"] = created
    return _render_panel(request, ctx)


@router.get("", response_class=HTMLResponse)
async def platform_page(request: Request):
    require_platform(request)
    pool = await get_pool()
    ctx = await _panel_context(request, pool)
    return templates.TemplateResponse(request, "platform.html", ctx)


@router.get("/panel", response_class=HTMLResponse)
async def platform_panel(request: Request):
    require_platform(request)
    return await _panel_with(request)


@router.post("/churches", response_class=HTMLResponse)
async def create_church(
    request: Request,
    slug: str = Form(...),
    name: str = Form(...),
    admin_username: str = Form(...),
    admin_full_name: str = Form(...),
):
    """
    Register a church and its first administrator.

    A church with no account is unreachable — nobody could sign in to create
    one — so the two are one operation. No password is set: the account is
    created inactive and what comes back is a single-use invite link, so the
    operator never holds the church's credentials.

    Qdrant collections are deliberately NOT created here; index_meeting makes
    them on first use. Creating them up front would add a second system that can
    fail halfway through registration in exchange for nothing.
    """
    require_platform(request)
    pool = await get_pool()

    slug = slug.strip().lower()
    name = name.strip()
    admin_username = admin_username.strip().lower()
    admin_full_name = admin_full_name.strip()

    # The slug becomes a directory name AND a Qdrant collection prefix, so it is
    # validated by the same function those two use rather than by a rule here
    # that could drift from them.
    try:
        tenant_paths.validate_slug(slug)
    except tenant_paths.InvalidTenantSlug as e:
        return await _panel_with(request, error=str(e))

    if not name or len(name) > MAX_CHURCH_NAME_LEN:
        return await _panel_with(
            request, error=f"Назва має бути 1–{MAX_CHURCH_NAME_LEN} символів."
        )
    if not admin_username or len(admin_username) > MAX_USERNAME_LEN:
        return await _panel_with(
            request, error=f"Логін адміна має бути 1–{MAX_USERNAME_LEN} символів."
        )
    if not admin_full_name:
        return await _panel_with(request, error="Вкажіть імʼя адміністратора.")

    if await tenants_repo.get_by_slug(pool, slug) is not None:
        return await _panel_with(request, error=f"Ідентифікатор «{slug}» уже зайнятий.")

    tenant_id = await tenants_repo.create_tenant(pool, slug=slug, name=name)

    try:
        user_id, invite_url, hours = await _found_admin(
            request, pool, tenant_id, admin_username, admin_full_name,
        )
    except Exception as e:
        # Since 014 a login only clashes inside its own church, and this church
        # is one statement old — so WebUserAlreadyExists here is close to
        # impossible. Kept anyway: whatever the failure, undo the tenant rather
        # than leave an empty church holding the slug. delete_if_empty refuses
        # anything that has accounts, so this cannot remove a real one.
        removed = await tenants_repo.delete_if_empty(pool, tenant_id)
        taken = isinstance(e, web_users_repo.WebUserAlreadyExists)
        detail = (
            f"Логін «{admin_username}» уже є в цій церкві. Оберіть інший."
            if taken else f"Не вдалося створити адміністратора: {e}"
        )
        if not removed:
            detail += f" ⚠️ Церкву «{slug}» створено, але без адміна — приберіть вручну."
        await _logger.error(
            "web.church_create_failed",
            message=f"church {slug!r}: {type(e).__name__}: {e} (rolled back={removed})",
        )
        return await _panel_with(request, error=detail)

    # Folders now, not on first upload: an operator who opens a new church and
    # finds nothing cannot tell "empty" from "broken".
    tenant_paths.paths_for(slug).ensure()

    await audit_repo.record(
        pool,
        tenant_id=tenant_id,
        action="platform.church_created",
        actor=current_user(request).actor,
        resource=f"tenants/{tenant_id}",
        detail={"slug": slug, "name": name, "admin_username": admin_username},
    )
    await _logger.info(
        "web.church_created",
        message=f"{current_user(request).username} created church {slug!r} "
                f"(admin {admin_username})",
    )

    return await _panel_with(request, created={
        "slug": slug, "name": name, "tenant_id": tenant_id,
        "username": admin_username, "user_id": user_id,
        "invite_url": invite_url, "invite_hours": hours,
    })


async def _found_admin(
    request: Request, pool: Any, tenant_id: int, username: str, full_name: str,
) -> tuple[int, str, int]:
    """Create the inactive founding account and its invite. Returns (id, url, hours)."""
    from church_assistant.db import web_invites_repo

    user_id = await web_users_repo.add_web_user(
        pool,
        tenant_id,
        username=username,
        # A hash of something nobody has. The account cannot be signed into
        # until the invite is redeemed, and it is deactivated as well so the
        # state is legible rather than being an account that merely refuses
        # every password.
        password_hash=security.hash_password(security.new_session_token()),
        full_name=full_name,
        role="admin",
    )
    await web_users_repo.deactivate(pool, tenant_id, user_id)

    token = security.new_session_token()
    await web_invites_repo.create(
        pool,
        tenant_id,
        web_user_id=user_id,
        token_hash=security.hash_token(token),
        created_by=current_user(request).actor,
    )
    base = str(request.base_url).rstrip("/")
    return (user_id, f"{base}/invite/{token}",
            web_invites_repo.DEFAULT_TTL_SECONDS // 3600)


@router.post("/churches/{tenant_id}/suspend", response_class=HTMLResponse)
async def suspend_church(request: Request, tenant_id: int):
    """
    Stop a church signing in, without touching a byte of its data.

    resolve_web_session refuses an inactive tenant, so every live session dies
    at its next request and no new one can start. The archive stays exactly
    where it is — suspension is about access, not deletion, and nothing here
    can delete a church that has accounts.
    """
    return await _set_active(request, tenant_id, False)


@router.post("/churches/{tenant_id}/resume", response_class=HTMLResponse)
async def resume_church(request: Request, tenant_id: int):
    return await _set_active(request, tenant_id, True)


async def _set_active(request: Request, tenant_id: int, active: bool) -> HTMLResponse:
    require_platform(request)
    pool = await get_pool()

    church = await tenants_repo.get_by_id(pool, tenant_id)
    if church is None or int(church["id"]) == 0:
        # Tenant 0 is the platform itself. Suspending it would lock the operator
        # out of the panel they are standing in.
        return await _panel_with(request, error="Немає такої церкви.")

    await tenants_repo.set_active(pool, tenant_id, active)
    await audit_repo.record(
        pool,
        tenant_id=tenant_id,
        action="platform.church_resumed" if active else "platform.church_suspended",
        actor=current_user(request).actor,
        resource=f"tenants/{tenant_id}",
        detail={"slug": church["slug"]},
    )
    verb = "відновлено" if active else "призупинено"
    await _logger.info(
        "web.church_active_changed",
        message=f"{current_user(request).username} {verb} church {church['slug']!r}",
    )
    return await _panel_with(request)


@router.post("/churches/{tenant_id}/rename", response_class=HTMLResponse)
async def rename_church(request: Request, tenant_id: int, name: str = Form(...)):
    """
    Change what a church is called. The identifier is not touched.

    The slug is a directory on disk and the prefix of four Qdrant collections;
    changing it would mean moving 1.9 GB and reindexing, and stopping half way
    through either leaves a church the application cannot find. The name is a
    label, so it is free.
    """
    require_platform(request)
    pool = await get_pool()

    name = name.strip()
    if not name or len(name) > MAX_CHURCH_NAME_LEN:
        return await _panel_with(
            request, error=f"Назва має бути 1–{MAX_CHURCH_NAME_LEN} символів."
        )

    church = await tenants_repo.get_by_id(pool, tenant_id)
    if church is None or int(church["id"]) == 0:
        return await _panel_with(request, error="Немає такої церкви.")
    was = church["name"]
    if name == was:
        return await _panel_with(request)

    await tenants_repo.rename(pool, tenant_id, name)
    await audit_repo.record(
        pool,
        tenant_id=tenant_id,
        action="platform.church_renamed",
        actor=current_user(request).actor,
        resource=f"tenants/{tenant_id}",
        detail={"slug": church["slug"], "from": was, "to": name},
    )
    return await _panel_with(request)


@router.post("/churches/{tenant_id}/archive", response_class=HTMLResponse)
async def archive_church(
    request: Request, tenant_id: int, confirm_slug: str = Form(""),
):
    """
    Retire a church. Access stops now; the data stays for a year.

    Deliberately not a delete. A congregation's archive is the only copy of
    conversations nobody wrote down twice, and the afternoon somebody clicks the
    wrong row is not the moment to find that out. Restoring is one action away
    for a year; after that scripts/purge_archived_tenants.py can remove it, and
    only when a person runs it.

    The identifier has to be typed. A confirm dialog is a reflex by the second
    time you see it; typing `first-baptist` is not something the hand does on
    its own, and it is the difference between the row you meant and the row
    above it.
    """
    require_platform(request)
    pool = await get_pool()

    church = await tenants_repo.get_by_id(pool, tenant_id)
    if church is None or int(church["id"]) == 0:
        return await _panel_with(request, error="Немає такої церкви.")

    if confirm_slug.strip() != church["slug"]:
        return await _panel_with(
            request,
            error=f"Щоб архівувати «{church['name']}», введіть її ідентифікатор "
                  f"«{church['slug']}» точно.",
        )

    await tenants_repo.archive(pool, tenant_id)
    await audit_repo.record(
        pool,
        tenant_id=tenant_id,
        action="platform.church_archived",
        actor=current_user(request).actor,
        resource=f"tenants/{tenant_id}",
        detail={"slug": church["slug"],
                "retention_days": tenants_repo.ARCHIVE_RETENTION_DAYS},
    )
    await _logger.warn(
        "web.church_archived",
        message=f"{current_user(request).username} archived church "
                f"{church['slug']!r} — data kept "
                f"{tenants_repo.ARCHIVE_RETENTION_DAYS} days",
    )
    return await _panel_with(request)


@router.post("/churches/{tenant_id}/restore", response_class=HTMLResponse)
async def restore_church(request: Request, tenant_id: int):
    """
    Take a church back out of the archive — suspended, not live.

    Coming back is one decision and letting people in is another. Making the
    first imply the second means a mis-click on restore also hands out access,
    to a church whose people may have moved on.
    """
    require_platform(request)
    pool = await get_pool()

    church = await tenants_repo.get_by_id(pool, tenant_id)
    if church is None or int(church["id"]) == 0:
        return await _panel_with(request, error="Немає такої церкви.")

    await tenants_repo.restore(pool, tenant_id)
    await audit_repo.record(
        pool,
        tenant_id=tenant_id,
        action="platform.church_restored",
        actor=current_user(request).actor,
        resource=f"tenants/{tenant_id}",
        detail={"slug": church["slug"]},
    )
    return await _panel_with(
        request,
        error=f"«{church['name']}» повернуто з архіву — і поки що призупинено. "
              f"Натисніть «відновити», щоб пустити людей.",
    )
