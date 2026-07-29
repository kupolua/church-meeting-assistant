"""
Whitelist authentication gate.

python-telegram-bot has no true "middleware", but the idiomatic equivalent is
a ``TypeHandler`` registered in a lower group (e.g. group=-1) that runs before
all normal handlers. When it decides a request is not allowed, it raises
``ApplicationHandlerStop`` to prevent any downstream handler (group 0) from
running.

Policy (per handoff brief MVP-A.3):
    - Private chat only. Non-private chats (groups/channels) → silently ignored.
    - Sender must be on the active whitelist (users.is_active = TRUE).
    - Unauthorized senders → silently ignored + logged as ``bot.unauthorized``.

On success, the authenticated user row is cached in ``context.user_data``
under ``USER_KEY`` so downstream handlers don't re-query the DB.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from church_assistant.db import users_repo
from church_assistant.db.connection import get_pool
from church_assistant.db.tenant_context import resolve_tenant_for_telegram
from church_assistant.shared.logger import Logger


_log = Logger(process="bot")

# Keys under which the authenticated context is cached per-user.
USER_KEY = "db_user"
TENANT_KEY = "tenant_id"


async def auth_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Gate every update: enforce private-chat + whitelist.

    Raises ApplicationHandlerStop to block downstream handlers when the
    request is not allowed. Returns normally (no exception) when allowed,
    letting the update fall through to the real handlers in group 0.
    """
    chat = update.effective_chat
    user = update.effective_user

    # No user (e.g. channel post, edited service message) — nothing to serve.
    if user is None or chat is None:
        raise ApplicationHandlerStop

    # Private chats only — ignore groups/channels/supergroups.
    if chat.type != chat.PRIVATE:
        await _log.warn(
            "bot.non_private",
            message=f"Ignored {chat.type} chat from user_id={user.id}",
            metadata={"telegram_user_id": user.id, "chat_type": chat.type},
        )
        raise ApplicationHandlerStop

    pool = await get_pool()
    # Bootstrap: which church (tenant) is this user on? (bypasses RLS)
    tenant_id = await resolve_tenant_for_telegram(pool, user.id)
    db_user = (
        await users_repo.get_by_telegram_id(pool, tenant_id, user.id)
        if tenant_id is not None else None
    )

    if tenant_id is None or db_user is None or not db_user.get("is_active", False):
        # Silent ignore — do NOT reveal the bot exists to strangers.
        # No tenant_id on purpose: this stranger belongs to no church, so the
        # event goes to `_system` rather than being filed against one.
        await _log.warn(
            "bot.unauthorized",
            message=(
                f"Unauthorized access: user_id={user.id} "
                f"username=@{user.username or '-'} name={user.full_name!r}"
            ),
            metadata={
                "telegram_user_id": user.id,
                "telegram_username": user.username,
                "full_name": user.full_name,
            },
        )
        raise ApplicationHandlerStop

    # Authorized — cache tenant + row for downstream handlers, then fall through.
    context.user_data[USER_KEY] = db_user
    context.user_data[TENANT_KEY] = tenant_id
