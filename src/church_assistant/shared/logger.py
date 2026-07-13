"""
Thin logger — convenient wrapper over `db.logs_repo` for cleaner call sites
in web / bot / worker code.

Instead of:
    from church_assistant.db import logs_repo
    from church_assistant.db.connection import get_pool
    pool = await get_pool()
    await logs_repo.log_event(pool, process="worker", level="INFO",
                              event="query.started", query_id=q.id)

Write:
    from church_assistant.shared.logger import Logger
    log = Logger(process="worker")
    await log.info("query.started", query_id=q.id)

Design:
    - Instance is bound to one `process` name (avoid passing on each call).
    - Convenience methods per level: debug, info, warn, error.
    - Uses the shared pool (get_pool) internally.
    - Never raises — logging must not crash the app.
"""

from __future__ import annotations

from typing import Any, Optional

from church_assistant.db import logs_repo
from church_assistant.db.connection import get_pool


VALID_PROCESSES = ("web", "bot", "worker", "cli")


class Logger:
    """Bound-to-process convenience logger."""

    def __init__(self, process: str):
        if process not in VALID_PROCESSES:
            # Silent normalize — don't crash the caller
            process = "cli"
        self.process = process

    async def _emit(
        self,
        level: str,
        event: str,
        message: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        query_id: Optional[int] = None,
        user_id: Optional[int] = None,
    ) -> None:
        try:
            pool = await get_pool()
            await logs_repo.log_event(
                pool,
                process=self.process,
                level=level,
                event=event,
                message=message,
                metadata=metadata,
                query_id=query_id,
                user_id=user_id,
            )
        except Exception:
            # Silent — logging must never crash
            pass

    async def debug(
        self,
        event: str,
        message: Optional[str] = None,
        *,
        metadata: Optional[dict[str, Any]] = None,
        query_id: Optional[int] = None,
        user_id: Optional[int] = None,
    ) -> None:
        await self._emit("DEBUG", event, message, metadata, query_id, user_id)

    async def info(
        self,
        event: str,
        message: Optional[str] = None,
        *,
        metadata: Optional[dict[str, Any]] = None,
        query_id: Optional[int] = None,
        user_id: Optional[int] = None,
    ) -> None:
        await self._emit("INFO", event, message, metadata, query_id, user_id)

    async def warn(
        self,
        event: str,
        message: Optional[str] = None,
        *,
        metadata: Optional[dict[str, Any]] = None,
        query_id: Optional[int] = None,
        user_id: Optional[int] = None,
    ) -> None:
        await self._emit("WARN", event, message, metadata, query_id, user_id)

    async def error(
        self,
        event: str,
        message: Optional[str] = None,
        *,
        metadata: Optional[dict[str, Any]] = None,
        query_id: Optional[int] = None,
        user_id: Optional[int] = None,
    ) -> None:
        await self._emit("ERROR", event, message, metadata, query_id, user_id)

    async def record_error(
        self,
        *,
        error_type: str,
        error_message: str,
        traceback: str,
        query_id: Optional[int] = None,
        user_id: Optional[int] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Optional[int]:
        """
        Convenience for except blocks: also inserts an `errors` row
        (in addition to a logs ERROR line, which callers may want to skip).

        Returns the errors.id, or None on failure.
        """
        try:
            pool = await get_pool()
            return await logs_repo.record_error(
                pool,
                process=self.process,
                error_type=error_type,
                error_message=error_message,
                traceback=traceback,
                query_id=query_id,
                user_id=user_id,
                metadata=metadata,
            )
        except Exception:
            return None
