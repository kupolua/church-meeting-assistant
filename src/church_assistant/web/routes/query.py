"""
Query routes: POST /api/query enqueues, GET /api/query/{id} polls for the answer.

Flow:
    1. Form POST with 'question' field.
    2. Validate (non-empty, reasonable length).
    3. INSERT into queries (status='pending', source='web').
    4. Return a partial that polls itself every few seconds.
    5. The worker picks the query up, runs RAG, writes the row.
    6. A poll finds status='completed' and swaps in the answer.

WHY NOT INLINE. This route used to call rag.answer() and block until Gemma
finished, which meant the web process needed Ollama, the reranker and 17 GB of
model weights in its own memory. That is fine on the machine that hosts the
models and impossible anywhere else, so it pinned the web tier to the M1 (see
docs/cloud_plan.md §13, backlog #8). Telegram has queued since MVP-A.3; this
just puts the web on the same path, which also means one failure story instead
of two: Ollama being down is now "still queued", not a 503.

Errors:
    - Empty / oversized question → 400 with the error partial.
    - DB refuses the insert    → 400 with the error partial.
    - Anything the worker hits → recorded on the row, shown by the poll.
"""

from __future__ import annotations

import traceback
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from church_assistant.db import queries_repo
from church_assistant.db.connection import get_pool
from church_assistant.shared import rag
from church_assistant.shared.logger import Logger
from church_assistant.web.main import templates
from church_assistant.web.tenant import current_tenant


router = APIRouter(prefix="/api")

# Sensible caps for direct-typed input
MAX_QUESTION_LEN = 500
MIN_QUESTION_LEN = 3

# How often the pending partial re-asks. Gemma answers in 10–70s, so a tighter
# interval would just be load; a looser one makes a finished answer feel stale.
POLL_INTERVAL_S = 3

# A query waiting longer than this has stopped looking like "the worker is busy"
# and started looking like "the worker is not there". We cannot tell the two
# apart from here without a health check on every poll, so the wording covers
# both rather than guessing (see _pending_context).
SLOW_HINT_AFTER_S = 120


def _error(request: Request, title: str, detail: str, status_code: int) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "partials/query_error.html",
        {"error_title": title, "error_detail": detail},
        status_code=status_code,
    )


def _elapsed_s(row: dict[str, Any]) -> int:
    """
    Whole seconds since the query was accepted.

    The column is `asked_at` — `queries` names it that, while `ingestion_jobs`
    uses `created_at`, and reaching for the wrong one here fails silently: the
    counter simply sits at 0 forever, because a missing key is indistinguishable
    from "no time has passed".
    """
    asked = row.get("asked_at")
    if not isinstance(asked, datetime):
        return 0
    if asked.tzinfo is None:
        asked = asked.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - asked).total_seconds()))


def _pending_context(row: dict[str, Any]) -> dict[str, Any]:
    """Template context for a query that has not finished yet."""
    elapsed = _elapsed_s(row)
    status = row.get("status")
    return {
        "query_id": row["id"],
        "question": row.get("question") or "",
        "status": status,
        "elapsed_s": elapsed,
        "poll_interval_s": POLL_INTERVAL_S,
        # Only for queries that have not even been claimed: once a worker holds
        # one, 'processing' already explains the wait.
        "slow_hint": status == "pending" and elapsed >= SLOW_HINT_AFTER_S,
    }


def _render_pending(request: Request, row: dict[str, Any]) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "partials/query_pending.html", _pending_context(row)
    )


def _render_finished(request: Request, row: dict[str, Any]) -> HTMLResponse:
    """Render a terminal query: the answer, the failure, or the cancellation."""
    status = row.get("status")

    if status == "completed":
        return templates.TemplateResponse(
            request,
            "partials/query_result.html",
            {
                "question": row.get("question") or "",
                "result": rag.AnswerResult.from_query_row(row),
                "score_color_hint": rag.score_color_hint,
                "query_id": row["id"],
            },
        )

    if status == "cancelled":
        return _error(
            request,
            "Запит скасовано",
            "Цей запит зняли з черги — його можна поставити наново.",
            200,
        )

    # failed — the worker already exhausted its retries, so the row's message is
    # the whole story. Status stays 200: the poll succeeded, the query did not.
    return _error(
        request,
        "Запит не виконався",
        row.get("error_message") or "Причина не записана.",
        200,
    )


@router.post("/query", response_class=HTMLResponse)
async def query_endpoint(
    request: Request,
    question: str = Form(...),
    collection: str = Form("protocols"),
):
    """Accept a question, queue it, and hand back a partial that watches it."""

    # ─── Validation ──────────────────────────────────────────
    question = question.strip()

    if len(question) < MIN_QUESTION_LEN:
        return _error(
            request,
            "Питання занадто коротке",
            f"Мінімум {MIN_QUESTION_LEN} символи.",
            400,
        )

    if len(question) > MAX_QUESTION_LEN:
        return _error(
            request,
            "Питання занадто довге",
            f"Максимум {MAX_QUESTION_LEN} символів.",
            400,
        )

    # ─── Queue as pending ────────────────────────────────────
    pool = await get_pool()
    tenant_id = current_tenant(request)
    log = Logger("web", tenant_id=tenant_id)

    try:
        query_id = await queries_repo.insert_pending(
            pool,
            tenant_id,
            source="web",
            question=question,
            collection=collection,
        )
    except ValueError as e:
        return _error(request, "Помилка валідації", str(e), 400)
    except Exception as e:
        # DB unreachable and friends — never leave the form with nothing back.
        tb = traceback.format_exc()
        await log.record_error(
            error_type=type(e).__name__, error_message=str(e), traceback=tb,
        )
        return _error(
            request,
            "Не вдалося прийняти питання",
            "Проблема з базою. Спробуйте, будь ласка, ще раз за хвилину.",
            503,
        )

    await log.info(
        "query.received",
        message=f"web query: {question[:80]}",
        query_id=query_id,
    )

    row = await queries_repo.get_by_id(pool, tenant_id, query_id)
    if row is None:  # pragma: no cover — it was inserted one statement ago
        return _error(
            request, "Запит зник", f"Щойно створений запит #{query_id} не читається.", 500,
        )
    return _render_pending(request, row)


@router.get("/query/{query_id}", response_class=HTMLResponse)
async def query_poll(request: Request, query_id: int):
    """
    One poll of a queued query.

    Returns the pending partial (which keeps polling) while the query is on the
    queue or in flight, and a terminal partial — answer, failure, cancellation —
    once it is not. The terminal partials carry no hx-trigger, which is what
    stops the polling.
    """
    pool = await get_pool()
    tenant_id = current_tenant(request)

    # tenant_cursor scopes this to the caller's church, so another church's
    # query is simply not found — never their answer.
    row = await queries_repo.get_by_id(pool, tenant_id, query_id)
    if row is None:
        return _error(request, "Запит не знайдено", f"Немає запиту #{query_id}.", 404)

    if row.get("status") in ("pending", "processing"):
        return _render_pending(request, row)
    return _render_finished(request, row)
