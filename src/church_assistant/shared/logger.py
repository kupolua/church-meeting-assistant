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

import os
from typing import Any, Optional

from church_assistant.db import logs_repo
from church_assistant.db.connection import get_pool


VALID_PROCESSES = ("web", "bot", "worker", "cli")

# Tenant for platform/system events with no specific church (worker.started,
# health warnings, …). Defaults to the default tenant for now.
# TODO(mt): a dedicated '_system' tenant so system logs don't sit in a church.
SYSTEM_TENANT_ID = int(os.getenv("SYSTEM_TENANT_ID", "1"))


class Logger:
    """
    Bound-to-process convenience logger (tenant-aware).

    Bind a tenant once (`Logger("web", tenant_id=t)`) or override per call
    (`await log.info(event, tenant_id=t)`). Unset → SYSTEM_TENANT_ID.
    """

    def __init__(self, process: str, tenant_id: int = SYSTEM_TENANT_ID):
        if process not in VALID_PROCESSES:
            process = "cli"  # silent normalize
        self.process = process
        self.tenant_id = tenant_id

    def _tenant(self, tenant_id: Optional[int]) -> int:
        return self.tenant_id if tenant_id is None else tenant_id

    async def _emit(
        self,
        level: str,
        event: str,
        message: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        query_id: Optional[int] = None,
        user_id: Optional[int] = None,
        tenant_id: Optional[int] = None,
    ) -> None:
        try:
            pool = await get_pool()
            await logs_repo.log_event(
                pool,
                self._tenant(tenant_id),
                process=self.process,
                level=level,
                event=event,
                message=message,
                metadata=metadata,
                query_id=query_id,
                user_id=user_id,
            )
        except Exception:
            pass  # logging must never crash

    async def debug(self, event, message=None, *, metadata=None, query_id=None, user_id=None, tenant_id=None) -> None:
        await self._emit("DEBUG", event, message, metadata, query_id, user_id, tenant_id)

    async def info(self, event, message=None, *, metadata=None, query_id=None, user_id=None, tenant_id=None) -> None:
        await self._emit("INFO", event, message, metadata, query_id, user_id, tenant_id)

    async def warn(self, event, message=None, *, metadata=None, query_id=None, user_id=None, tenant_id=None) -> None:
        await self._emit("WARN", event, message, metadata, query_id, user_id, tenant_id)

    async def error(self, event, message=None, *, metadata=None, query_id=None, user_id=None, tenant_id=None) -> None:
        await self._emit("ERROR", event, message, metadata, query_id, user_id, tenant_id)

    async def record_error(
        self,
        *,
        error_type: str,
        error_message: str,
        traceback: str,
        query_id: Optional[int] = None,
        user_id: Optional[int] = None,
        metadata: Optional[dict[str, Any]] = None,
        tenant_id: Optional[int] = None,
    ) -> Optional[int]:
        """Record an `errors` row for a tenant. Returns the id, or None."""
        try:
            pool = await get_pool()
            return await logs_repo.record_error(
                pool,
                self._tenant(tenant_id),
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
