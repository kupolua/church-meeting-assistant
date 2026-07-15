"""
Bot configuration — read Telegram settings from the environment (.env).

Kept separate from main.py so handlers/tests can import limits without
constructing the whole Application.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv


# Message length bounds (chars) — same spirit as the web route.
MIN_QUESTION_LEN = 3
MAX_QUESTION_LEN = 500

# Default RAG collection for queued Telegram questions.
DEFAULT_COLLECTION = "protocols"


def get_bot_token() -> str:
    """
    Return the Telegram bot token from TELEGRAM_BOT_TOKEN.

    Raises:
        RuntimeError: if the token is missing (fail fast at startup).
    """
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token or token.strip() in ("", "1234567890:ABCdefGHIjklMNOpqrSTUvwxYZ"):
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set (or still the placeholder). "
            "Create a bot with @BotFather and put the token in .env."
        )
    return token.strip()


def get_poll_timeout() -> int:
    """Long-polling timeout in seconds (defaults to 30)."""
    load_dotenv()
    try:
        return int(os.getenv("TELEGRAM_POLL_TIMEOUT", "30"))
    except ValueError:
        return 30
