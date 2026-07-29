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
import sys
from typing import Any, Optional

from church_assistant.db import logs_repo, tenants_repo
from church_assistant.db.connection import get_pool


VALID_PROCESSES = ("web", "bot", "worker", "cli")

# Tenant for platform events with no specific church (worker.started, health
# warnings, …) — the reserved `_system` tenant from migration 007, NOT the first
# church, whose dashboard would otherwise show the platform's operational noise.
#
# A constant rather than a registry lookup on purpose: this module must pick a
# tenant before any tenant context exists, and it is the component that has to
# keep working while other things break. Overridable for tests/one-offs, but a
# deployment that has not applied 007 must set it back to 1 — an unknown
# tenant_id violates the foreign key and every system log would be dropped
# silently by the never-raise guard below.
SYSTEM_TENANT_ID = int(
    os.getenv("SYSTEM_TENANT_ID", str(tenants_repo.SYSTEM_TENANT_ID))
)


_warned = False


def _warn_once(exc: Exception) -> None:
    """
    Report the FIRST swallowed logging failure on stderr, then stay quiet.

    Swallowing is required — a broken log table must not take down the bot — but
    swallowing silently once cost us a whole class of invisible breakage: an
    unset/unmigrated SYSTEM_TENANT_ID makes every system event vanish with no
    trace anywhere. One line is enough to find that; repeating it per event
    would flood the console of a service that is already in trouble.
    """
    global _warned
    if _warned:
        return
    _warned = True
    print(
        f"[logger] DB logging is failing, events are being dropped: "
        f"{type(exc).__name__}: {exc}",
        file=sys.stderr,
        flush=True,
    )


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
        except Exception as e:
            _warn_once(e)   # logging must never crash — but not silently, either

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
        except Exception as e:
            _warn_once(e)
            return None
