"""
Telegram bot entry point — long-polling application (MVP-A.3).

Run:
    uv run python -m church_assistant.bot.main

Architecture:
    - Long polling (no webhook — this runs on localhost).
    - Handler group -1: whitelist auth gate (runs before everything, blocks
      unauthorized/non-private updates via ApplicationHandlerStop).
    - Handler group 0: real handlers (commands + free-text queries).
    - post_init opens the shared asyncpg pool; post_shutdown closes it.

The bot only *queues* questions (status='pending'); the worker (MVP-A.4)
runs the RAG pipeline and calls bot/delivery.py to send answers back.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from church_assistant.bot.config import get_bot_token, get_poll_timeout
from church_assistant.bot.handlers import admin, help as help_handler, query, verbose
from church_assistant.bot.middleware.whitelist import auth_gate
from church_assistant.db.connection import close_pool, get_pool
from church_assistant.shared.logger import Logger


logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
# python-telegram-bot's httpx chatter is noisy at INFO — quiet it.
logging.getLogger("httpx").setLevel(logging.WARNING)

_std = logging.getLogger("church_assistant.bot")
_log = Logger(process="bot")


# ─────────────────────────────────────────────────────────────
# Lifecycle hooks
# ─────────────────────────────────────────────────────────────

async def _post_init(application: Application) -> None:
    """Open the DB pool once, before polling starts."""
    await get_pool()
    me = await application.bot.get_me()
    _std.info("Bot started as @%s (id=%s)", me.username, me.id)
    await _log.info("bot.started", message=f"@{me.username} polling")


async def _post_shutdown(application: Application) -> None:
    """Close the DB pool on shutdown."""
    await close_pool()
    _std.info("Bot stopped, pool closed")


# ─────────────────────────────────────────────────────────────
# Global error handler
# ─────────────────────────────────────────────────────────────

async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log any unhandled exception raised inside a handler."""
    err = context.error
    _std.exception("Unhandled error while processing update", exc_info=err)
    await _log.record_error(
        error_type=type(err).__name__ if err else "UnknownError",
        error_message=str(err) if err else "unknown",
        traceback="",
    )


# ─────────────────────────────────────────────────────────────
# Application factory
# ─────────────────────────────────────────────────────────────

def build_application() -> Application:
    """Construct and wire up the Telegram Application."""
    token = get_bot_token()

    application = (
        ApplicationBuilder()
        .token(token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    # Group -1: auth gate runs before all real handlers.
    application.add_handler(TypeHandler(Update, auth_gate), group=-1)

    # Group 0: real handlers (only reached by authorized private-chat users).
    application.add_handler(CommandHandler("start", help_handler.start_command))
    application.add_handler(CommandHandler("help", help_handler.help_command))
    application.add_handler(CommandHandler("verbose", verbose.verbose_command))
    application.add_handler(CommandHandler("stats", admin.stats_command))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, query.query_message)
    )

    application.add_error_handler(_on_error)

    return application


def main() -> None:
    """Build and run the bot with long polling (blocking)."""
    application = build_application()
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        poll_interval=0.0,
        timeout=get_poll_timeout(),
    )


if __name__ == "__main__":
    main()
