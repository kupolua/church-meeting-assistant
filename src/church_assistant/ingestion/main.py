"""
Ingestion worker entry point (MVP-C).

Run:
    uv run python -m church_assistant.ingestion.main

Loop:
    1. Periodically snapshot Ollama/Qdrant health (for gating only).
    2. Fetch the next runnable ingestion job and process it:
         - pending          → diarization + transcription → awaiting_review
         - queued_analysis  → merge → analyze → polish → index → completed
       When Ollama/Qdrant are down, only 'pending' jobs are picked up —
       diarization + transcription don't need them, but analysis + indexing do.
    3. When nothing is runnable, idle-sleep INGESTION_POLL_INTERVAL.

Graceful shutdown on SIGINT/SIGTERM: the check happens between jobs, so an
in-flight phase (e.g. a 2h transcription) runs to completion first, then the
DB pool closes. Send the signal twice to force-quit.

This is a SECOND worker, separate from the query worker (church_assistant.worker):
they consume different queues and can run side by side.
"""

from __future__ import annotations

import asyncio
import logging
import signal

from church_assistant.db import ingestion_jobs_repo as jobs_repo
from church_assistant.db.connection import close_pool, get_pool
from church_assistant.ingestion.config import (
    get_auto_index,
    get_health_check_interval,
    get_max_retries,
    get_poll_interval,
    get_sequential,
)
from church_assistant.ingestion.processor import process_job
from church_assistant.shared import health as health_mod
from church_assistant.shared.logger import Logger


logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)

_std = logging.getLogger("church_assistant.ingestion")
_log = Logger(process="worker")


async def _interruptible_sleep(stop: asyncio.Event, seconds: float) -> None:
    """Sleep up to `seconds`, but return early if `stop` is set."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def run() -> None:
    """Main ingestion-worker coroutine."""
    poll_interval = get_poll_interval()
    health_interval = get_health_check_interval()
    max_retries = get_max_retries()
    sequential = get_sequential()
    auto_index = get_auto_index()

    pool = await get_pool()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # e.g. non-Unix
            pass

    _std.info(
        "Ingestion worker started (poll=%ss, health=%ss, max_retries=%s, "
        "sequential=%s, auto_index=%s)",
        poll_interval, health_interval, max_retries, sequential, auto_index,
    )
    await _log.info(
        "ingestion.started",
        message="ingestion worker polling for runnable jobs",
    )

    last_health: float | None = None
    health: health_mod.SystemHealth | None = None

    try:
        while not stop.is_set():
            now = loop.time()

            # ─── Periodic health snapshot (gating only) ──────
            if health is None or (now - last_health) >= health_interval:
                health = await health_mod.check_all()
                last_health = loop.time()

            # Analysis + indexing need Ollama/Qdrant; transcription doesn't.
            if health.can_process_queries:
                allowed = ("pending", "queued_analysis")
            else:
                allowed = ("pending",)

            # ─── Dequeue + process ───────────────────────────
            job = await jobs_repo.fetch_next_runnable(pool, allowed_statuses=allowed)
            if job is None:
                await _interruptible_sleep(stop, poll_interval)
                continue

            await process_job(
                pool, job,
                max_retries=max_retries,
                sequential=sequential,
                auto_index=auto_index,
            )
            # Loop immediately to pick up the next runnable job.

    finally:
        _std.info("Shutting down...")
        await _log.info("ingestion.stopped", message="ingestion worker shutting down")
        await close_pool()
        _std.info("Ingestion worker stopped, pool closed")


def main() -> None:
    """Blocking entry point."""
    asyncio.run(run())


if __name__ == "__main__":
    main()
