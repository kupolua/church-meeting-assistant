"""
The JSON API the mobile app speaks — step 1: signing in.

    POST /api/v1/auth/request-code   {email, church_name?, full_name?, note?}
    POST /api/v1/auth/verify         {email, code, device_name?, tenant_id?}
    GET  /api/v1/me                  who am I, which church, may I in
    POST /api/v1/auth/logout         revoke this token

Step 2 — the council and the phones that sign (migration 022):

    GET  /api/v1/members             admin: everyone in the church
    POST /api/v1/members             admin: invite by email (account + letter)
    POST /api/v1/members/{id}        admin: {role} and/or {active}
    GET  /api/v1/keys                my signing keys
    POST /api/v1/keys                register this phone's Ed25519 public key
    DELETE /api/v1/keys/{id}         revoke one of mine

AN INVITATION IS AN ACCOUNT. The admin types a colleague's address and name;
the server creates the member there and then, active and without a password,
and writes to the address: install the app, sign in with this email. The
member's first sign-in is the ordinary code flow — nothing to redeem, nothing
to type but six digits. This is deliberately not the web's link-invite: the
app has no password to set, so there is nothing for a link to do.

NO COOKIES. The app holds an opaque token in the phone's secure storage and
sends it as `Authorization: Bearer …`. The token is a web_sessions row with
client = 'app' (migration 021), so everything the web already knows how to do
with a session — list it, revoke it, cut it when the account is disabled or
the church suspended — applies to a phone unchanged. AuthMiddleware lets
/api/ through unexamined; api_user() below is the gate instead.

THE FLOW, as decided 23.10.2026: the code goes out at once and proves the
address; a church founded from here is 'pending' until the operator approves
it from the platform panel. /me says which, and the app keeps every tab but
the first shut until the answer is 'active'.

WHAT IS DELIBERATELY NOT REVEALED. request-code answers the same way whether
or not the address is known — a stranger learns nothing about who uses this.
A wrong code and an expired one are the same error. The only distinction the
app is told is the one it must act on: no account at all for a login-only
request, so it can offer to found a church instead.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from church_assistant.db import (
    app_auth_repo, audit_repo, signing_keys_repo, tenants_repo,
    web_sessions_repo, web_users_repo,
)
from church_assistant.db.connection import get_pool
from church_assistant.shared import mailer, tenant_paths
from church_assistant.shared.logger import Logger
from church_assistant.web import security
from church_assistant.web.routes import api_app


router = APIRouter(prefix="/api/v1")

_logger = Logger(process="web")

# A phone keeps its token for a year; the web's twelve hours assume a browser
# on a shared desk, and a phone is neither shared nor a desk. The idle check is
# off for the same reason. Revocation remains one click on the account page.
APP_SESSION_TTL_SECONDS = 365 * 24 * 3600
# The code inside an invitation mail: a week, because the mail is read when
# it is read. Every other code keeps app_auth_repo.CODE_TTL_SECONDS.
INVITE_CODE_TTL_SECONDS = 7 * 24 * 3600

MAX_CHURCH_NAME_LEN = 120
MAX_NOTE_LEN = 1000
MAX_NAME_LEN = 80

# Per-address and per-IP brake on code requests, in-process like the login
# form's: a restart clearing it is a fine trade for zero extra state.
_requests: dict[str, list[float]] = {}
REQ_WINDOW = 15 * 60
REQ_MAX_PER_IP = 20


def _throttled(key: str, limit: int) -> bool:
    cutoff = time.time() - REQ_WINDOW
    recent = [t for t in _requests.get(key, []) if t > cutoff]
    recent.append(time.time())
    _requests[key] = recent
    return len(recent) > limit


# ─────────────────────────────────────────────────────────────
# Shapes
# ─────────────────────────────────────────────────────────────

class RequestCode(BaseModel):
    email: str = Field(max_length=254)
    # Present → the person is founding a church. Absent → signing in.
    church_name: Optional[str] = Field(default=None, max_length=MAX_CHURCH_NAME_LEN)
    full_name: Optional[str] = Field(default=None, max_length=MAX_NAME_LEN)
    note: Optional[str] = Field(default=None, max_length=MAX_NOTE_LEN)


class Verify(BaseModel):
    email: str = Field(max_length=254)
    code: str = Field(min_length=6, max_length=6)
    device_name: Optional[str] = Field(default=None, max_length=120)
    # When one address sits on two councils the app asks, then sends this.
    tenant_id: Optional[int] = None


class Invite(BaseModel):
    email: str = Field(max_length=254)
    full_name: str = Field(min_length=1, max_length=MAX_NAME_LEN)
    role: str = Field(default="member", pattern="^(member|admin)$")


class MemberPatch(BaseModel):
    role: Optional[str] = Field(default=None, pattern="^(member|admin)$")
    active: Optional[bool] = None


class KeyIn(BaseModel):
    public_key: str = Field(min_length=40, max_length=64)   # base64 of 32 bytes
    label: Optional[str] = Field(default=None, max_length=120)


@dataclass(frozen=True)
class ApiUser:
    session_id: int
    user_id: int
    tenant_id: int
    tenant_slug: str
    username: str
    full_name: str
    role: str

    @property
    def actor(self) -> str:
        return f"app:{self.username}"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


async def api_user(request: Request) -> ApiUser:
    """The bearer token's owner. 401 for anything but a live app session."""
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="Потрібен токен.")
    pool = await get_pool()
    row = await web_sessions_repo.resolve(
        pool, security.hash_token(token.strip()), idle_seconds=0,
    )
    if row is None or row.get("is_platform_admin"):
        raise HTTPException(status_code=401, detail="Сесія не дійсна.")
    user = ApiUser(
        session_id=int(row["session_id"]), user_id=int(row["web_user_id"]),
        tenant_id=int(row["tenant_id"]), tenant_slug=str(row["tenant_slug"]),
        username=str(row["username"]), full_name=str(row["full_name"] or ""),
        role=str(row["role"] or "member"),
    )
    await web_sessions_repo.touch(pool, user.tenant_id, user.session_id)
    return user


async def api_admin(request: Request) -> ApiUser:
    """The bearer token's owner, if they administer their church."""
    user = await api_user(request)
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Лише для адміністратора церкви.")
    return user


async def _me_payload(pool: Any, user: ApiUser) -> dict[str, Any]:
    tenant = await tenants_repo.get_by_id(pool, user.tenant_id)
    if tenant is None:
        raise HTTPException(status_code=401, detail="Церкви більше немає.")
    status = (
        "archived" if tenant.get("deleted_at") else
        "suspended" if not tenant["is_active"] else
        str(tenant.get("approval") or "active")
    )
    account = await web_users_repo.get_by_id(pool, user.tenant_id, user.user_id)
    return {
        "user": {
            "id": user.user_id, "name": user.full_name, "role": user.role,
            "email": (account or {}).get("email"),
        },
        "church": {
            "id": user.tenant_id, "slug": user.tenant_slug, "name": tenant["name"],
            # pending | active | rejected | suspended | archived
            "status": status,
        },
        "signing_keys": len(await signing_keys_repo.list_for_user(pool, user.tenant_id, user.user_id)),
    }


# ─────────────────────────────────────────────────────────────
# Emails
# ─────────────────────────────────────────────────────────────

def _code_mail(code: str, founding: bool) -> tuple[str, str]:
    what = "створення церкви" if founding else "входу"
    return (
        f"{code} — код для {what} у CMA",
        f"Ваш код: {code}\n\n"
        f"Введіть його в застосунку CMA. Код діє {app_auth_repo.CODE_TTL_SECONDS // 60} хвилин.\n"
        f"Якщо ви нічого не просили — просто проігноруйте цей лист.\n",
    )


async def _notify_operator(name: str, email: str, note: str, tenant_id: int) -> None:
    if not mailer.PLATFORM_NOTIFY_EMAIL:
        return
    await mailer.send(
        mailer.PLATFORM_NOTIFY_EMAIL,
        f"CMA: нова церква чекає схвалення — «{name}»",
        f"Церква: {name}\nЗаявник: {email}\nПро себе: {note or '—'}\n\n"
        f"Схвалити або відхилити: /platform (запит #{tenant_id}).\n",
    )


# ─────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────

@router.post("/auth/request-code")
async def request_code(request: Request, body: RequestCode):
    """
    Send a six-digit code to an address.

    Founding (church_name given): the form is validated now and stored WITH the
    code; nothing is created until the code comes back. Sign-in (no church):
    a code is sent only if an account carries the address — but the answer is
    200 either way, and only the app's own next step differs.
    """
    email = app_auth_repo.normalize_email(body.email)
    if not app_auth_repo.looks_like_email(email):
        raise HTTPException(status_code=422, detail="Перевірте адресу електронної пошти.")

    ip = request.client.host if request.client else "?"
    if _throttled(f"ip|{ip}", REQ_MAX_PER_IP):
        raise HTTPException(status_code=429, detail="Забагато запитів. Спробуйте пізніше.")

    pool = await get_pool()
    if await app_auth_repo.recent_code_count(pool, email) >= app_auth_repo.MAX_CODES_PER_WINDOW:
        raise HTTPException(status_code=429, detail="Код уже надсилали кілька разів. Зачекайте 15 хвилин.")

    founding = bool((body.church_name or "").strip())
    payload: dict[str, Any] = {}
    if founding:
        church_name = body.church_name.strip()
        full_name = (body.full_name or "").strip()
        if not full_name:
            raise HTTPException(status_code=422, detail="Вкажіть, як вас звати.")
        payload = {
            "church_name": church_name, "full_name": full_name,
            "note": (body.note or "").strip(),
        }
        purpose = "signup"
    else:
        purpose = "login"
        if not await app_auth_repo.accounts_by_email(pool, email):
            # No account: say so, and only so. The app then offers the founding
            # form. The alternative — pretending to send — leaves a real person
            # staring at an empty inbox for ten minutes.
            return {"sent": False, "known": False}

    code = await app_auth_repo.issue(pool, email=email, purpose=purpose, payload=payload, ip=ip)
    subject, text = _code_mail(code, founding)
    sent = await mailer.send(email, subject, text)
    if not sent:
        # No mail provider (a developer's machine) or a delivery failure. The
        # code is printed where only the operator reads, never returned.
        print(f"[api] code for {email} ({purpose}): {code}  [mail not sent]", flush=True)
    await _logger.info(
        "api.code_requested",
        message=f"{purpose} code for {email} ({'sent' if sent else 'NOT sent'})",
        metadata={"email": email, "purpose": purpose, "sent": sent},
    )
    return {"sent": sent, "known": True, "ttl_seconds": app_auth_repo.CODE_TTL_SECONDS}


@router.post("/auth/verify")
async def verify(request: Request, body: Verify):
    """
    Trade a code for a token. For a founding code this is also where the
    church and its first administrator come into being — pending.
    """
    email = app_auth_repo.normalize_email(body.email)
    code = body.code.strip()
    if not code.isdigit():
        raise HTTPException(status_code=400, detail="Код — шість цифр.")
    pool = await get_pool()

    # A person on two councils has to be asked which door — and the code must
    # survive the question. So: look, ask, and only then spend.
    candidates = await app_auth_repo.accounts_by_email(pool, email)
    must_ask = len(candidates) > 1 and body.tenant_id is None
    spent = await app_auth_repo.verify(pool, email=email, code=code, consume=not must_ask)
    if spent is None:
        await _logger.warn("api.code_rejected", message=f"bad code for {email}",
                           metadata={"email": email})
        raise HTTPException(status_code=401, detail="Код не підійшов або прострочений.")
    if must_ask and spent["purpose"] == "login":
        raise HTTPException(status_code=409, detail={
            "choose": [{"tenant_id": c["tenant_id"], "name": c["tenant_name"]}
                       for c in candidates],
        })
    if must_ask:
        # A founding code from someone who already sits on two councils: the
        # question does not apply, spend it now.
        spent = await app_auth_repo.verify(pool, email=email, code=code)
        if spent is None:
            raise HTTPException(status_code=401, detail="Код не підійшов або прострочений.")

    ip = request.client.host if request.client else None
    device = (body.device_name or request.headers.get("user-agent") or "").strip()[:120]

    if spent["purpose"] == "signup":
        tenant_id, user_id = await _found_church(pool, email, spent["payload"], device)
    else:
        if not candidates:
            raise HTTPException(status_code=404, detail="Акаунта з цією адресою немає.")
        chosen = (
            next((c for c in candidates if c["tenant_id"] == body.tenant_id), None)
            if body.tenant_id is not None else candidates[0]
        )
        if chosen is None:
            raise HTTPException(status_code=404, detail="Цієї церкви у вас немає.")
        tenant_id, user_id = int(chosen["tenant_id"]), int(chosen["web_user_id"])

    token = security.new_session_token()
    await web_sessions_repo.create(
        pool, tenant_id, web_user_id=user_id,
        token_hash=security.hash_token(token),
        ttl_seconds=APP_SESSION_TTL_SECONDS,
        user_agent=device or None, ip=ip, client="app",
    )
    await web_users_repo.touch_login(pool, tenant_id, user_id)
    user = await api_user_from(pool, token)
    await audit_repo.record(
        pool, tenant_id=tenant_id, action="app.signed_in", actor=user.actor,
        resource=f"web_users/{user_id}", detail={"device": device or None},
    )
    return {"token": token, "me": await _me_payload(pool, user)}


async def api_user_from(pool: Any, token: str) -> ApiUser:
    row = await web_sessions_repo.resolve(pool, security.hash_token(token), idle_seconds=0)
    if row is None:
        raise HTTPException(status_code=500, detail="Сесію створено, але не знайдено.")
    return ApiUser(
        session_id=int(row["session_id"]), user_id=int(row["web_user_id"]),
        tenant_id=int(row["tenant_id"]), tenant_slug=str(row["tenant_slug"]),
        username=str(row["username"]), full_name=str(row["full_name"] or ""),
        role=str(row["role"] or "member"),
    )


async def _found_church(
    pool: Any, email: str, payload: dict[str, Any], device: str,
) -> tuple[int, int]:
    """
    A proven address, a church name, a person's name → a pending church with
    one administrator. Returns (tenant_id, user_id).

    Mirrors platform.create_church: a tenant with no account is unreachable,
    so the two are one operation and a failed second step undoes the first.
    The slug is derived, not chosen: the founder never sees it, and it has to
    satisfy the same rule the filesystem and Qdrant live by.
    """
    church_name = str(payload.get("church_name") or "").strip()[:MAX_CHURCH_NAME_LEN]
    full_name = str(payload.get("full_name") or "").strip()[:MAX_NAME_LEN]
    note = str(payload.get("note") or "").strip()[:MAX_NOTE_LEN]
    if not church_name or not full_name:
        raise HTTPException(status_code=400, detail="Запит на церкву неповний.")

    # Already asked and still waiting? Sign them back into the same request
    # rather than minting a second church per retry of the code.
    for acc in await app_auth_repo.accounts_by_email(pool, email):
        if acc["approval"] == "pending" and acc["role"] == "admin":
            return int(acc["tenant_id"]), int(acc["web_user_id"])

    slug = await _free_slug(pool, church_name)
    tenant_id = await tenants_repo.create_tenant(
        pool, slug=slug, name=church_name, approval="pending",
        requested_by=email, request_note=note or None,
    )
    try:
        user_id = await web_users_repo.add_web_user(
            pool, tenant_id,
            username=email,
            # No password: the app signs in by code. The hash of a secret
            # nobody holds keeps the column honest, as the panel does.
            password_hash=security.hash_password(security.new_session_token()),
            full_name=full_name, role="admin", email=email,
        )
    except Exception as e:
        removed = await tenants_repo.delete_if_empty(pool, tenant_id)
        await _logger.error(
            "api.church_found_failed",
            message=f"church {slug!r}: {type(e).__name__}: {e} (rolled back={removed})",
        )
        raise HTTPException(status_code=500, detail="Не вдалося створити церкву.") from e

    tenant_paths.paths_for(slug).ensure()
    await audit_repo.record(
        pool, tenant_id=tenant_id, action="app.church_requested", actor=f"app:{email}",
        resource=f"tenants/{tenant_id}",
        detail={"slug": slug, "name": church_name, "device": device or None},
    )
    await _logger.info(
        "api.church_requested",
        message=f"{email} asked for church {slug!r} («{church_name}») — pending",
        tenant_id=tenant_id,
    )
    await _notify_operator(church_name, email, note, tenant_id)
    return tenant_id, user_id


_TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e", "є": "ie",
    "ж": "zh", "з": "z", "и": "y", "і": "i", "ї": "i", "й": "i", "к": "k", "л": "l",
    "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ь": "",
    "ю": "iu", "я": "ia", "ы": "y", "э": "e", "ё": "e", "ъ": "", "'": "", "ʼ": "", "’": "",
})


def slug_from_name(name: str) -> str:
    """«Церква „Надія“, Київ» → 'tserkva-nadiia-kyiv'. Never empty."""
    import re
    raw = name.lower().translate(_TRANSLIT)
    raw = re.sub(r"[^a-z0-9]+", "-", raw).strip("-.")
    raw = re.sub(r"-{2,}", "-", raw)[:40].strip("-.")
    return raw or "church"


async def _free_slug(pool: Any, name: str) -> str:
    base = slug_from_name(name)
    tenant_paths.validate_slug(base)
    slug = base
    n = 2
    while await tenants_repo.slug_taken(pool, slug):
        slug = f"{base}-{n}"
        n += 1
    return slug


@router.get("/me")
async def me(request: Request):
    user = await api_user(request)
    pool = await get_pool()
    return await _me_payload(pool, user)


@router.post("/auth/logout")
async def logout(request: Request):
    user = await api_user(request)
    pool = await get_pool()
    await web_sessions_repo.revoke(pool, user.tenant_id, user.session_id)
    return {"ok": True}


# ─────────────────────────────────────────────────────────────
# Members (admin)
# ─────────────────────────────────────────────────────────────

def _member(row: dict[str, Any], keys: dict[int, int]) -> dict[str, Any]:
    return {
        "id": int(row["id"]), "name": row["full_name"], "email": row.get("email"),
        "username": row["username"], "role": row["role"], "active": bool(row["is_active"]),
        "last_login_at": row.get("last_login_at"), "created_at": row.get("created_at"),
        "signing_keys": keys.get(int(row["id"]), 0),
    }


@router.get("/members")
async def members(request: Request):
    admin = await api_admin(request)
    pool = await get_pool()
    rows = await web_users_repo.list_all(pool, admin.tenant_id)
    keys = await signing_keys_repo.live_counts(pool, admin.tenant_id)
    return {"members": [_member(r, keys) for r in rows if not r.get("is_platform_admin")]}


@router.post("/members", status_code=201)
async def invite_member(request: Request, body: Invite):
    """
    Add a member and tell them. Active at once, no password: their door is
    a code to this address, same as everyone's — and it rides IN the
    invitation, with a link to the app, so the person installs, taps
    «Увійти» → «У мене вже є код» and is in. Before 26.10.2026 the mail only
    said "type your address in the app", and the code came in a second mail
    after that: a step nobody needed. This code lives a week, not ten
    minutes, because an invitation is read whenever it is read; a later
    «Надіслати код» simply retires it (app_auth_repo.issue).

    When the server has no mail, the code comes back to the admin instead,
    to hand over in person.
    """
    admin = await api_admin(request)
    email = app_auth_repo.normalize_email(body.email)
    if not app_auth_repo.looks_like_email(email):
        raise HTTPException(status_code=422, detail="Перевірте адресу електронної пошти.")
    pool = await get_pool()
    tenant = await tenants_repo.get_by_id(pool, admin.tenant_id)
    try:
        user_id = await web_users_repo.add_web_user(
            pool, admin.tenant_id, username=email,
            password_hash=security.hash_password(security.new_session_token()),
            full_name=body.full_name.strip(), role=body.role, email=email,
            notes=f"запрошено з застосунку: {admin.username}",
        )
    except web_users_repo.WebUserAlreadyExists:
        raise HTTPException(status_code=409, detail="Людина з цією адресою вже є в церкві.")

    ip = request.client.host if request.client else None
    code = await app_auth_repo.issue(pool, email=email, purpose="login", ip=ip,
                                     ttl_seconds=INVITE_CODE_TTL_SECONDS)
    apk = api_app.latest_apk(api_app.apk_dir())
    download = (f"1. Встановіть застосунок (Android): {api_app.public_url()}/api/v1/app/{apk.name}\n"
                if apk else "1. Встановіть застосунок CMA.\n")
    sent = await mailer.send(
        email,
        f"Вас додано до церкви «{tenant['name']}» у CMA",
        f"{body.full_name.strip()}, вітаємо!\n\n"
        f"{admin.full_name or admin.username} додав(ла) вас до церкви «{tenant['name']}» "
        f"у застосунку CMA — протоколи зустрічей ради.\n\n"
        f"Ваш код для входу: {code}\n"
        f"Діє {INVITE_CODE_TTL_SECONDS // 86400} днів.\n\n"
        f"{download}"
        f"2. Відкрийте його, натисніть «Увійти» → «У мене вже є код», "
        f"вкажіть адресу {email} і цей код.\n\n"
        f"Пароля немає. Якщо код прострочиться, натисніть у застосунку "
        f"«Надіслати код на пошту» — прийде новий.\n",
    )
    await audit_repo.record(
        pool, tenant_id=admin.tenant_id, action="app.member_invited", actor=admin.actor,
        resource=f"web_users/{user_id}", detail={"email": email, "role": body.role, "mail_sent": sent},
    )
    await _logger.info("api.member_invited",
                       message=f"{admin.username} invited {email} as {body.role} (mail {'sent' if sent else 'NOT sent'})",
                       tenant_id=admin.tenant_id)
    row = await web_users_repo.get_by_id(pool, admin.tenant_id, user_id)
    out: dict[str, Any] = {"member": _member(row, {}), "mail_sent": sent}
    if not sent:
        out["code"] = code
    return out


@router.post("/members/{member_id}")
async def patch_member(request: Request, member_id: int, body: MemberPatch):
    """
    Change a role, switch a person off or back on.

    Not on yourself: an admin who demotes or disables themself leaves a church
    with nobody to undo it. Switching someone off also ends their sessions and
    strikes their keys — a departed member's phone must sign nothing further.
    """
    admin = await api_admin(request)
    if member_id == admin.user_id:
        raise HTTPException(status_code=400, detail="Себе змінити не можна — попросіть іншого адміністратора.")
    pool = await get_pool()
    row = await web_users_repo.get_by_id(pool, admin.tenant_id, member_id)
    if row is None or row.get("is_platform_admin"):
        raise HTTPException(status_code=404, detail="Немає такого учасника.")

    changed: dict[str, Any] = {}
    if body.role is not None and body.role != row["role"]:
        await web_users_repo.set_role(pool, admin.tenant_id, member_id, body.role)
        changed["role"] = body.role
    if body.active is not None and body.active != bool(row["is_active"]):
        if body.active:
            await web_users_repo.reactivate(pool, admin.tenant_id, member_id)
        else:
            await web_users_repo.deactivate(pool, admin.tenant_id, member_id)
            await web_sessions_repo.revoke_all_for_user(pool, admin.tenant_id, member_id)
            await signing_keys_repo.revoke_all_for_user(pool, admin.tenant_id, member_id)
        changed["active"] = body.active
    if changed:
        await audit_repo.record(
            pool, tenant_id=admin.tenant_id, action="app.member_changed", actor=admin.actor,
            resource=f"web_users/{member_id}", detail=changed,
        )
    row = await web_users_repo.get_by_id(pool, admin.tenant_id, member_id)
    keys = await signing_keys_repo.live_counts(pool, admin.tenant_id)
    return {"member": _member(row, keys)}


# ─────────────────────────────────────────────────────────────
# Signing keys (mine)
# ─────────────────────────────────────────────────────────────

def _key(k: dict[str, Any]) -> dict[str, Any]:
    return {kk: v for kk, v in k.items() if kk != "web_user_id"}


@router.get("/keys")
async def my_keys(request: Request):
    user = await api_user(request)
    pool = await get_pool()
    return {"keys": [_key(k) for k in await signing_keys_repo.list_for_user(pool, user.tenant_id, user.user_id)]}


@router.post("/keys", status_code=201)
async def register_key(request: Request, body: KeyIn):
    user = await api_user(request)
    try:
        raw = signing_keys_repo.decode_public_key(body.public_key)
    except signing_keys_repo.BadPublicKey as e:
        raise HTTPException(status_code=422, detail=str(e))
    pool = await get_pool()
    try:
        key_id = await signing_keys_repo.add(
            pool, user.tenant_id, web_user_id=user.user_id, public_key=raw, label=body.label or "",
        )
    except signing_keys_repo.KeyAlreadyRegistered:
        raise HTTPException(status_code=409, detail="Цей ключ уже зареєстровано.")
    await audit_repo.record(
        pool, tenant_id=user.tenant_id, action="app.signing_key_added", actor=user.actor,
        resource=f"signing_keys/{key_id}",
        detail={"fingerprint": signing_keys_repo.fingerprint(raw), "label": body.label},
    )
    keys = await signing_keys_repo.list_for_user(pool, user.tenant_id, user.user_id)
    return {"key": _key(next(k for k in keys if k["id"] == key_id))}


@router.delete("/keys/{key_id}")
async def revoke_key(request: Request, key_id: int):
    user = await api_user(request)
    pool = await get_pool()
    ok = await signing_keys_repo.revoke(pool, user.tenant_id, key_id, web_user_id=user.user_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Немає такого ключа.")
    await audit_repo.record(
        pool, tenant_id=user.tenant_id, action="app.signing_key_revoked", actor=user.actor,
        resource=f"signing_keys/{key_id}",
    )
    return {"ok": True}
