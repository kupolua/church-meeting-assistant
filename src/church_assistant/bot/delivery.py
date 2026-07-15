"""
Delivery — send a completed (or failed) query result back to Telegram.

This module is imported and called by the background worker (MVP-A.4) once it
finishes running the RAG pipeline for a queued Telegram query. It is kept in the
bot package (not the worker) because message formatting is a bot concern and it
reuses the bot's MarkdownV2 helpers.

Usage from the worker:
    from telegram import Bot
    from church_assistant.bot import delivery

    bot = Bot(token)
    await delivery.send_answer(bot, query_row)     # completed query dict
    # or
    await delivery.send_failure(bot, query_row)    # failed query dict

`query_row` is a dict from queries_repo (get_by_id / fetch_next_pending shape):
    telegram_chat_id, telegram_message_id, question, synthesis, sources,
    total_time_ms, ...
"""

from __future__ import annotations

from telegram import Bot
from telegram.constants import ParseMode

from church_assistant.bot.formatting import md2
from church_assistant.shared.logger import Logger


_log = Logger(process="worker")

# Telegram hard cap per message is 4096 chars; leave margin for safety.
_CHUNK_LEN = 3900


def _chunk(text: str, size: int = _CHUNK_LEN) -> list[str]:
    """
    Split text into <=size pieces, preferring line boundaries.

    Guarantees no piece exceeds `size` even if a single line is longer.
    """
    if len(text) <= size:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        # A single over-long line: hard-split it.
        while len(line) > size:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:size])
            line = line[size:]

        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) > size:
            chunks.append(current)
            current = line
        else:
            current = candidate

    if current:
        chunks.append(current)
    return chunks


def _format_answer(query_row: dict) -> str:
    """Render a completed query as a MarkdownV2 message."""
    synthesis = (query_row.get("synthesis") or "").strip() or "(порожня відповідь)"
    sources = query_row.get("sources") or []
    total_ms = query_row.get("total_time_ms")

    parts = [md2(synthesis)]

    if sources:
        src = ", ".join(md2(s) for s in sources)
        parts.append("")
        parts.append(f"📎 *Джерела:* {src}")

    footer_bits = []
    if total_ms is not None:
        secs = total_ms / 1000
        footer_bits.append(f"⏱ {md2(f'{secs:.1f}с')}")
    footer_bits.append("/verbose — фрагменти")
    parts.append("")
    parts.append("  •  ".join(md2(b) if not b.startswith("⏱") else b for b in footer_bits))

    return "\n".join(parts)


async def send_answer(bot: Bot, query_row: dict) -> bool:
    """
    Send a completed answer to the query's Telegram chat.

    Returns True on success, False on failure (logged, never raises).
    Replies to the original question message when possible.
    """
    chat_id = query_row.get("telegram_chat_id")
    if chat_id is None:
        return False

    reply_to = query_row.get("telegram_message_id")
    query_id = query_row.get("id")
    text = _format_answer(query_row)

    try:
        for i, chunk in enumerate(_chunk(text)):
            await bot.send_message(
                chat_id=chat_id,
                text=chunk,
                parse_mode=ParseMode.MARKDOWN_V2,
                # Reply only on the first chunk; keep the thread tidy.
                reply_to_message_id=reply_to if i == 0 else None,
                # Original question may have been deleted — don't hard-fail.
                allow_sending_without_reply=True,
            )
        await _log.info(
            "bot.delivered",
            message=f"Delivered answer to chat_id={chat_id}",
            query_id=query_id,
        )
        return True
    except Exception as e:
        await _log.record_error(
            error_type=type(e).__name__,
            error_message=f"Delivery failed: {e}",
            traceback="",
            query_id=query_id,
        )
        return False


async def send_failure(bot: Bot, query_row: dict) -> bool:
    """
    Notify the user that their query could not be answered.

    Returns True on success, False on failure (logged, never raises).
    """
    chat_id = query_row.get("telegram_chat_id")
    if chat_id is None:
        return False

    reply_to = query_row.get("telegram_message_id")
    query_id = query_row.get("id")

    text = (
        "⚠️ Не вдалося обробити ваше питання.\n"
        "Система тимчасово недоступна — спробуйте, будь ласка, пізніше."
    )

    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_to_message_id=reply_to,
            allow_sending_without_reply=True,
        )
        await _log.info(
            "bot.failure_notified",
            message=f"Notified failure to chat_id={chat_id}",
            query_id=query_id,
        )
        return True
    except Exception as e:
        await _log.record_error(
            error_type=type(e).__name__,
            error_message=f"Failure notice send failed: {e}",
            traceback="",
            query_id=query_id,
        )
        return False
