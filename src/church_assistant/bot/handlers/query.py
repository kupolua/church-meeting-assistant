"""
Query handler — free-text question → queue as pending → immediate ack.

Unlike the web route (which runs RAG synchronously while Pavlo waits), the bot
uses the async queue model: we INSERT a pending query and acknowledge instantly.
The background worker (MVP-A.4) picks it up, runs the RAG pipeline, and delivers
the answer later via bot/delivery.py.

Flow:
    1. Validate message length (3–500 chars).
    2. INSERT into queries (source='telegram', status='pending').
    3. Reply with an immediate ack.
"""

from __future__ import annotations

import traceback

from telegram import Update
from telegram.ext import ContextTypes

from church_assistant.bot.config import (
    DEFAULT_COLLECTION,
    MAX_QUESTION_LEN,
    MIN_QUESTION_LEN,
)
from church_assistant.bot.middleware.whitelist import USER_KEY
from church_assistant.db import queries_repo
from church_assistant.db.connection import get_pool
from church_assistant.shared.logger import Logger


_log = Logger(process="bot")

_ACK_TEXT = (
    "✅ Прийняв ваше питання. Обробляю...\n"
    "Я надішлю відповідь, як тільки система буде вільна."
)


async def query_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle a plain-text question from a whitelisted user."""
    message = update.message
    if message is None or message.text is None:
        return

    question = message.text.strip()

    # ─── Validation ──────────────────────────────────────────
    if len(question) < MIN_QUESTION_LEN:
        await message.reply_text(
            f"Питання занадто коротке — мінімум {MIN_QUESTION_LEN} символи."
        )
        return

    if len(question) > MAX_QUESTION_LEN:
        await message.reply_text(
            f"Питання занадто довге — максимум {MAX_QUESTION_LEN} символів "
            f"(ваше: {len(question)})."
        )
        return

    # The auth gate guarantees this exists for whitelisted users.
    db_user = (context.user_data or {}).get(USER_KEY) or {}
    user_id = db_user.get("id")

    pool = await get_pool()

    # ─── Queue as pending ────────────────────────────────────
    try:
        query_id = await queries_repo.insert_pending(
            pool,
            source="telegram",
            question=question,
            user_id=user_id,
            telegram_chat_id=message.chat_id,
            telegram_message_id=message.message_id,
            collection=DEFAULT_COLLECTION,
        )
    except Exception as e:  # DB down, validation, etc. — never leave user hanging
        tb = traceback.format_exc()
        await _log.record_error(
            error_type=type(e).__name__,
            error_message=str(e),
            traceback=tb,
            user_id=user_id,
        )
        await message.reply_text(
            "⚠️ Не вдалося прийняти питання (проблема з базою). "
            "Спробуйте, будь ласка, ще раз за хвилину."
        )
        return

    await _log.info(
        "bot.query_received",
        message=f"telegram query: {question[:80]}",
        query_id=query_id,
        user_id=user_id,
        metadata={
            "telegram_chat_id": message.chat_id,
            "telegram_message_id": message.message_id,
            "length": len(question),
        },
    )

    # ─── Immediate ack (reply to the question) ───────────────
    await message.reply_text(_ACK_TEXT)
