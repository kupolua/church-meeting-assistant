"""
Worker configuration — read WORKER_* settings from the environment (.env).

Defaults mirror .env.example so the worker runs sensibly even if some vars
are unset.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv


def _int_env(name: str, default: int) -> int:
    """Read an int env var, falling back to default on missing/invalid."""
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def get_poll_interval() -> int:
    """Seconds to sleep when the queue is empty (idle poll)."""
    load_dotenv()
    return _int_env("WORKER_POLL_INTERVAL", 10)


def get_health_check_interval() -> int:
    """Seconds between health snapshots."""
    load_dotenv()
    return _int_env("WORKER_HEALTH_CHECK_INTERVAL", 60)


def get_retry_sleep() -> int:
    """Seconds to wait when a dependency (Ollama/Qdrant) is down."""
    load_dotenv()
    return _int_env("WORKER_RETRY_SLEEP", 60)


def get_max_retries() -> int:
    """Max attempts before a query is left permanently failed."""
    load_dotenv()
    return _int_env("WORKER_MAX_RETRIES", 3)
