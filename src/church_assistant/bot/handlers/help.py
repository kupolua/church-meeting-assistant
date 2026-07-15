"""
/start and /help handlers — welcome + usage instructions.

These only run for whitelisted users (the auth gate in group=-1 already
blocked everyone else). Messages are Ukrainian, addressed to the pastor.
"""

from __future__ import annotations

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from church_assistant.bot.formatting import md2
from church_assistant.bot.middleware.whitelist import USER_KEY
from church_assistant.shared.logger import Logger


_log = Logger(process="bot")


def _welcome_text(first_name: str) -> str:
    """Build the MarkdownV2 welcome/help body."""
    name = md2(first_name or "пасторе")
    return (
        f"👋 Вітаю, *{name}*\\!\n\n"
        "Я — асистент по архіву протоколів пасторської ради\\. "
        "Просто надішліть мені питання звичайним текстом, наприклад:\n\n"
        "_Що вирішили щодо членства Леоніда\\?_\n\n"
        "Я прийму питання й обробляю його у фоновому режимі — "
        "відповідь надішлю, щойно система звільниться\\.\n\n"
        "*Команди:*\n"
        "• /verbose — показати джерела \\(фрагменти\\) останньої відповіді\n"
        "• /help — показати цю довідку\n"
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start."""
    user = update.effective_user
    db_user = context.user_data.get(USER_KEY) if context.user_data else None

    await _log.info(
        "bot.start",
        message=f"/start from user_id={user.id if user else '?'}",
        user_id=(db_user or {}).get("id"),
    )

    await update.message.reply_text(
        _welcome_text(user.first_name if user else ""),
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help (same content as /start)."""
    user = update.effective_user
    await update.message.reply_text(
        _welcome_text(user.first_name if user else ""),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
