"""
Telegram MarkdownV2 formatting helpers.

Telegram's MarkdownV2 requires every one of these characters to be escaped
with a backslash whenever it appears as *literal* text:

    _ * [ ] ( ) ~ ` > # + - = | { } . !

If any dynamic content (topic titles, dates, synthesis text) contains an
unescaped special char, Telegram rejects the whole message with HTTP 400.
So we escape all dynamic pieces with `md2()` and only inject formatting
markers (`*bold*`, etc.) around already-escaped text.

Used by both the /verbose command and the worker delivery module.
"""

from __future__ import annotations

# Characters that MUST be escaped in MarkdownV2 literal text.
_MDV2_SPECIALS = r"_*[]()~`>#+-=|{}.!"
_MDV2_TRANS = str.maketrans({c: "\\" + c for c in _MDV2_SPECIALS})


def md2(text: object) -> str:
    """
    Escape arbitrary text for safe inclusion in a MarkdownV2 message.

    Accepts any value (coerced via str) so callers can pass dates/numbers
    without wrapping. Returns the escaped string.
    """
    return str(text).translate(_MDV2_TRANS)


# Emoji per score-color hint (from rag.score_color_hint).
_SCORE_EMOJI = {
    "green": "🟢",
    "yellow": "🟡",
    "dim": "⚪",
}


def score_emoji(hint: str) -> str:
    """Map a 'green'|'yellow'|'dim' hint to an emoji dot."""
    return _SCORE_EMOJI.get(hint, "⚪")
