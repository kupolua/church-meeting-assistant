"""
Admin handlers — /stats (role='admin' only).

The whitelist gate already guarantees the sender is active; here we additionally
require the admin role. Non-admin whitelisted users get a short refusal (they
already know the bot exists, so no need to stay silent).
"""

from __future__ import annotations

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from church_assistant.bot.formatting import md2
from church_assistant.bot.middleware.whitelist import USER_KEY
from church_assistant.db import queries_repo, users_repo
from church_assistant.db.connection import get_pool
from church_assistant.shared.logger import Logger


_log = Logger(process="bot")


def _fmt_ms(ms: float | None) -> str:
    """Human-friendly ms → '12.3s' / '850ms' / '—'."""
    if ms is None:
        return "—"
    if ms >= 1000:
        return f"{ms / 1000:.1f}с"
    return f"{int(ms)}мс"


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /stats — admin-only queue + today snapshot."""
    message = update.message
    db_user = (context.user_data or {}).get(USER_KEY) or {}

    if db_user.get("role") != "admin":
        await _log.warn(
            "bot.stats_denied",
            message=f"Non-admin /stats by user_id={db_user.get('id')}",
            user_id=db_user.get("id"),
        )
        await message.reply_text("⛔ Ця команда доступна лише адміністратору.")
        return

    pool = await get_pool()
    depth = await queries_repo.get_queue_depth(pool)
    today = await queries_repo.get_stats_today(pool)
    active_users = await users_repo.count_active(pool)

    await _log.info("bot.stats", message="/stats", user_id=db_user.get("id"))

    text = (
        "📊 *Статистика*\n"
        "\n"
        "*Черга:*\n"
        f"• ⏳ pending: {md2(depth['pending'])}\n"
        f"• ⚙️ processing: {md2(depth['processing'])}\n"
        f"• ❌ failed: {md2(depth['failed'])}\n"
        "\n"
        "*Сьогодні \\(24г\\):*\n"
        f"• Всього: {md2(today['total'])}\n"
        f"• ✅ завершено: {md2(today['completed'])}\n"
        f"• ❌ помилок: {md2(today['failed'])}\n"
        f"• 🌐 web: {md2(today['from_web'])}  •  ✈️ telegram: {md2(today['from_telegram'])}\n"
        f"• ⌀ час: {md2(_fmt_ms(today['avg_time_ms']))}\n"
        "\n"
        f"👥 Активних користувачів: {md2(active_users)}"
    )

    await message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)
