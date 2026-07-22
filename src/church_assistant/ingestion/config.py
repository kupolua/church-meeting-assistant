"""
Ingestion-worker configuration — read INGESTION_* settings from the environment.

Defaults are chosen for the local single-user setup (one worker, one M1). The
pipeline steps are expensive (diarization ~2h, Gemma analysis 10-30 min), so the
retry cap is deliberately low.
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


def _bool_env(name: str, default: bool) -> bool:
    """Read a bool env var ('1'/'true'/'yes'/'on' → True)."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def get_poll_interval() -> int:
    """Seconds to sleep when no job is runnable (idle poll)."""
    load_dotenv()
    return _int_env("INGESTION_POLL_INTERVAL", 15)


def get_health_check_interval() -> int:
    """Seconds between health snapshots."""
    load_dotenv()
    return _int_env("INGESTION_HEALTH_CHECK_INTERVAL", 60)


def get_retry_sleep() -> int:
    """Seconds to wait when Ollama/Qdrant are down (analysis/index blocked)."""
    load_dotenv()
    return _int_env("INGESTION_RETRY_SLEEP", 60)


def get_max_retries() -> int:
    """Max attempts before a job is left permanently failed."""
    load_dotenv()
    return _int_env("INGESTION_MAX_RETRIES", 2)


def get_sequential() -> bool:
    """Run diarization + transcription sequentially (lower peak memory)."""
    load_dotenv()
    return _bool_env("INGESTION_SEQUENTIAL", False)


def get_auto_index() -> bool:
    """Auto-run index_meeting into Qdrant after polish (full cycle)."""
    load_dotenv()
    return _bool_env("INGESTION_AUTO_INDEX", True)
