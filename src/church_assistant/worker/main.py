"""
Background worker entry point (MVP-A.4).

Run:
    uv run python -m church_assistant.worker.main

Loop:
    1. Every WORKER_HEALTH_CHECK_INTERVAL seconds, snapshot Ollama/Qdrant health
       (→ health_checks table). If a dependency is down, pause for
       WORKER_RETRY_SLEEP instead of failing every query.
    2. When healthy, fetch_next_pending() (atomic, FOR UPDATE SKIP LOCKED) and
       process it (RAG → store → deliver). Drain the queue back-to-back.
    3. When the queue is empty, idle-sleep WORKER_POLL_INTERVAL.

Graceful shutdown on SIGINT/SIGTERM: finishes the in-flight query, then closes
the Telegram bot and DB pool.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from telegram import Bot

from church_assistant.bot.config import get_bot_token
from church_assistant.db import logs_repo, queries_repo
from church_assistant.db.connection import close_pool, get_pool
from church_assistant.shared import health as health_mod
from church_assistant.shared.logger import Logger
from church_assistant.worker.config import (
    get_health_check_interval,
    get_max_retries,
    get_poll_interval,
    get_retry_sleep,
)
from church_assistant.worker.processor import process_query


logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)

_std = logging.getLogger("church_assistant.worker")
_log = Logger(process="worker")


async def _interruptible_sleep(stop: asyncio.Event, seconds: float) -> None:
    """Sleep up to `seconds`, but return early if `stop` is set."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


def _health_notes(sh: health_mod.SystemHealth) -> str:
    """Compact one-line summary of a health snapshot for the notes column."""
    parts = []
    for name, r in (("ollama", sh.ollama), ("qdrant", sh.qdrant), ("postgres", sh.postgres)):
        parts.append(f"{name}={'up' if r.up else 'DOWN'}")
        if r.notes and not r.up:
            parts[-1] += f"({r.notes})"
    return " ".join(parts)


async def run() -> None:
    """Main worker coroutine."""
    poll_interval = get_poll_interval()
    health_interval = get_health_check_interval()
    retry_sleep = get_retry_sleep()
    max_retries = get_max_retries()

    pool = await get_pool()

    # Telegram bot for delivery (standalone Bot, no polling).
    bot = Bot(get_bot_token())
    await bot.initialize()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # e.g. non-Unix
            pass

    _std.info(
        "Worker started (poll=%ss, health=%ss, retry_sleep=%ss, max_retries=%s)",
        poll_interval, health_interval, retry_sleep, max_retries,
    )
    await _log.info("worker.started", message="worker polling for pending queries")

    last_health: float | None = None
    health: health_mod.SystemHealth | None = None

    try:
        while not stop.is_set():
            now = loop.time()

            # ─── Periodic health snapshot ────────────────────
            if health is None or (now - last_health) >= health_interval:
                health = await health_mod.check_all()
                last_health = loop.time()
                await logs_repo.record_health_check(
                    pool,
                    ollama_up=health.ollama.up,
                    qdrant_up=health.qdrant.up,
                    ollama_response_time_ms=health.ollama.response_time_ms,
                    qdrant_response_time_ms=health.qdrant.response_time_ms,
                    notes=_health_notes(health),
                )
                if not health.can_process_queries:
                    _std.warning("Dependencies down: %s", _health_notes(health))
                    await _log.warn(
                        "worker.deps_down",
                        message=_health_notes(health),
                    )

            # ─── Pause if we can't process ───────────────────
            if not health.can_process_queries:
                await _interruptible_sleep(stop, retry_sleep)
                continue

            # ─── Dequeue + process ───────────────────────────
            query = await queries_repo.fetch_next_pending(pool)
            if query is None:
                await _interruptible_sleep(stop, poll_interval)
                continue

            await process_query(pool, bot, query, max_retries=max_retries)
            # Loop immediately to drain any remaining pending queries.

    finally:
        _std.info("Shutting down...")
        await _log.info("worker.stopped", message="worker shutting down")
        await bot.shutdown()
        await close_pool()
        _std.info("Worker stopped, bot + pool closed")


def main() -> None:
    """Blocking entry point."""
    asyncio.run(run())


if __name__ == "__main__":
    main()
