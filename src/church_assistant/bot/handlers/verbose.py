"""
/verbose handler — show the retrieved fragments (hits) behind the last answer.

Fetches the most recent *completed* Telegram query for this chat and renders
its stored hits: score dot, meeting date, topic/snippet, and rerank vs vector
scores. Purely informational — helps the pastor judge how grounded an answer is.

Hits are stored in queries.hits as JSONB (list of Hit.to_dict()); we rebuild
rag.Hit objects to reuse rag.format_hit_short / rag.score_color_hint.
"""

from __future__ import annotations

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from church_assistant.bot.formatting import md2, score_emoji
from church_assistant.bot.middleware.whitelist import USER_KEY
from church_assistant.db import queries_repo
from church_assistant.db.connection import get_pool
from church_assistant.shared import rag
from church_assistant.shared.logger import Logger


_log = Logger(process="bot")

# Telegram hard limit is 4096 chars; stay well under with a safety margin.
_MAX_MESSAGE_LEN = 3800


def _format_verbose(query_row: dict) -> str:
    """Render a completed query's hits as a MarkdownV2 message."""
    question = query_row.get("question") or "?"
    hits_raw = query_row.get("hits") or []
    asked_at = query_row.get("completed_at") or query_row.get("asked_at")

    header_lines = [
        "📋 *Джерела останньої відповіді*",
        "",
        f"❓ _{md2(question)}_",
    ]
    if asked_at is not None:
        # asked_at is a datetime; show date + HH:MM.
        try:
            stamp = asked_at.strftime("%Y-%m-%d %H:%M")
        except Exception:
            stamp = str(asked_at)
        header_lines.append(f"🕓 {md2(stamp)}")
    header_lines.append("")

    if not hits_raw:
        header_lines.append("_Ця відповідь не має збережених фрагментів\\._")
        return "\n".join(header_lines)

    hit_lines: list[str] = []
    for idx, hd in enumerate(hits_raw, 1):
        try:
            hit = rag.Hit.from_dict(hd)
        except Exception:
            continue
        emoji = score_emoji(rag.score_color_hint(hit))
        # format_hit_short returns plain text with special chars → escape it.
        line = md2(rag.format_hit_short(hit, idx))
        hit_lines.append(f"{emoji} {line}")

    body = "\n".join(header_lines + hit_lines)

    if len(body) > _MAX_MESSAGE_LEN:
        body = body[:_MAX_MESSAGE_LEN] + "\n\\.\\.\\."
    return body


async def verbose_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /verbose."""
    message = update.message
    chat_id = message.chat_id

    db_user = (context.user_data or {}).get(USER_KEY) or {}

    pool = await get_pool()
    last = await queries_repo.get_last_completed_for_telegram(pool, chat_id)

    await _log.info(
        "bot.verbose",
        message=f"/verbose chat_id={chat_id} found={last is not None}",
        query_id=(last or {}).get("id"),
        user_id=db_user.get("id"),
    )

    if last is None:
        await message.reply_text(
            "Поки що немає завершених відповідей. "
            "Спочатку задайте питання, і після відповіді ця команда "
            "покаже, на які фрагменти протоколів вона спиралась."
        )
        return

    await message.reply_text(
        _format_verbose(last),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
