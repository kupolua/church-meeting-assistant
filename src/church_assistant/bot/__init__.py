"""
Telegram bot package (MVP-A.3).

Whitelist-gated private-chat bot that accepts pastoral questions, queues them
as pending queries (worker processes them later, MVP-A.4), and serves helper
commands (/start, /help, /verbose, /stats).

Entry point:
    uv run python -m church_assistant.bot.main
"""
