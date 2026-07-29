"""
Audit repository — append-only access log (MT Phase 1).

The technical backbone of the наглядова рада (supervisory board): every access
to tenant data is recorded here, and the board can inspect it. The table is
append-only at the DB level (UPDATE/DELETE revoked from the app role — see
migrations/003_multitenancy.sql), so records can't be silently altered.

audit_log is RLS-gated → writes/reads go through tenant_cursor(); a board
inspecting a church's log queries within that church's tenant context.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from church_assistant.db.tenant_context import tenant_cursor


async def record(
    pool: AsyncConnectionPool,
    *,
    tenant_id: int,
    action: str,                      # 'data.read' | 'query.answer' | 'admin.access' | ...
    actor: Optional[str] = None,      # 'web:<user>' | 'bot:<tg_id>' | 'worker' | 'system'
    resource: Optional[str] = None,   # 'queries/123' | 'meeting/2026-05-18' | ...
    detail: Optional[dict[str, Any]] = None,
) -> None:
    """Append one audit event for a tenant (never raises on the happy path)."""
    sql = """
        INSERT INTO audit_log (tenant_id, actor, action, resource, detail)
        VALUES (%s, %s, %s, %s, %s::jsonb)
    """
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(sql, (
            tenant_id, actor, action, resource,
            json.dumps(detail, ensure_ascii=False) if detail is not None else None,
        ))


async def list_recent(
    pool: AsyncConnectionPool,
    tenant_id: int,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Recent audit events for a tenant (for the board's review UI)."""
    sql = """
        SELECT * FROM audit_log
        ORDER BY timestamp DESC
        LIMIT %s
    """
    async with tenant_cursor(pool, tenant_id, row_factory=dict_row) as cur:
        await cur.execute(sql, (limit,))
        return list(await cur.fetchall())
