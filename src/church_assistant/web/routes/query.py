"""
Query route: POST /api/query — HTMX-driven RAG queries.

Flow:
    1. Form POST with 'question' field.
    2. Validate (non-empty, reasonable length).
    3. INSERT into queries (status='pending', source='web').
    4. Call shared.rag.answer() — inline (Pavlo waits).
    5. UPDATE queries: mark_completed with results.
    6. Return HTML partial (hits + synthesis + sources).

Errors:
    - Empty question → 400 with error partial.
    - Ollama down / Qdrant down → 503 with error partial.
    - Other exception → 500 with generic error, log to errors table.

Note: web is single-user (Pavlo). Unlike Telegram bot which queues async,
here we synchronously run the RAG pipeline. Pavlo sees loader + result.
"""

from __future__ import annotations

import traceback

import httpx
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from church_assistant.db import queries_repo
from church_assistant.db.connection import get_pool
from church_assistant.shared import rag
from church_assistant.shared.logger import Logger
from church_assistant.web.main import templates


router = APIRouter(prefix="/api")

_logger = Logger(process="web")

# Sensible caps for direct-typed input
MAX_QUESTION_LEN = 500
MIN_QUESTION_LEN = 3


@router.post("/query", response_class=HTMLResponse)
async def query_endpoint(
    request: Request,
    question: str = Form(...),
    collection: str = Form("protocols"),
):
    """Run RAG synchronously and return an HTMX partial."""

    # ─── Validation ──────────────────────────────────────────
    question = question.strip()

    if len(question) < MIN_QUESTION_LEN:
        return templates.TemplateResponse(
            request,
            "partials/query_error.html",
            {
                "error_title": "Питання занадто коротке",
                "error_detail": f"Мінімум {MIN_QUESTION_LEN} символи.",
            },
            status_code=400,
        )

    if len(question) > MAX_QUESTION_LEN:
        return templates.TemplateResponse(
            request,
            "partials/query_error.html",
            {
                "error_title": "Питання занадто довге",
                "error_detail": f"Максимум {MAX_QUESTION_LEN} символів.",
            },
            status_code=400,
        )

    # ─── Insert pending ─────────────────────────────────────
    pool = await get_pool()

    try:
        query_id = await queries_repo.insert_pending(
            pool,
            source="web",
            question=question,
            collection=collection,
        )
    except ValueError as e:
        return templates.TemplateResponse(
            request,
            "partials/query_error.html",
            {
                "error_title": "Помилка валідації",
                "error_detail": str(e),
            },
            status_code=400,
        )

    await _logger.info(
        "query.received",
        message=f"web query: {question[:80]}",
        query_id=query_id,
    )

    # ─── Run RAG pipeline ────────────────────────────────────
    try:
        result = await rag.answer(
            question,
            collection=collection,
            limit=5,
            rerank=True,
        )
    except httpx.ConnectError as e:
        await queries_repo.mark_failed(
            pool,
            query_id,
            error_message=f"Ollama unreachable: {e}",
            error_traceback=traceback.format_exc(),
        )
        await _logger.error(
            "ollama.down",
            message=str(e),
            query_id=query_id,
        )
        return templates.TemplateResponse(
            request,
            "partials/query_error.html",
            {
                "error_title": "Ollama недоступний",
                "error_detail": (
                    "Локальний Gemma не запущений. "
                    "Переконайтеся, що процес ollama працює."
                ),
            },
            status_code=503,
        )
    except httpx.HTTPError as e:
        await queries_repo.mark_failed(
            pool,
            query_id,
            error_message=f"HTTP error: {e}",
            error_traceback=traceback.format_exc(),
        )
        await _logger.error(
            "query.http_error",
            message=str(e),
            query_id=query_id,
        )
        return templates.TemplateResponse(
            request,
            "partials/query_error.html",
            {
                "error_title": "Помилка мережі",
                "error_detail": str(e),
            },
            status_code=502,
        )
    except Exception as e:
        # Catch-all — always record so we don't lose visibility
        tb = traceback.format_exc()
        await queries_repo.mark_failed(
            pool,
            query_id,
            error_message=str(e),
            error_traceback=tb,
        )
        await _logger.record_error(
            error_type=type(e).__name__,
            error_message=str(e),
            traceback=tb,
            query_id=query_id,
        )
        return templates.TemplateResponse(
            request,
            "partials/query_error.html",
            {
                "error_title": "Неочікувана помилка",
                "error_detail": f"{type(e).__name__}: {e}",
            },
            status_code=500,
        )

    # ─── Save completed ──────────────────────────────────────
    await queries_repo.mark_completed(
        pool,
        query_id,
        hits=result.hits_as_json(),
        synthesis=result.synthesis,
        sources=result.sources,
        embed_time_ms=result.timings.embed_ms,
        qdrant_time_ms=result.timings.qdrant_ms,
        rerank_time_ms=result.timings.rerank_ms,
        gemma_time_ms=result.timings.gemma_ms,
        total_time_ms=result.timings.total_ms,
    )

    await _logger.info(
        "query.completed",
        message=f"total={result.timings.total_ms}ms, hits={len(result.hits)}",
        query_id=query_id,
        metadata={
            "hits_count": len(result.hits),
            "sources": result.sources,
            "timings": {
                "embed_ms": result.timings.embed_ms,
                "qdrant_ms": result.timings.qdrant_ms,
                "rerank_ms": result.timings.rerank_ms,
                "gemma_ms": result.timings.gemma_ms,
                "total_ms": result.timings.total_ms,
            },
        },
    )

    # ─── Render result partial ───────────────────────────────
    return templates.TemplateResponse(
        request,
        "partials/query_result.html",
        {
            "question": question,
            "result": result,
            "score_color_hint": rag.score_color_hint,
            "query_id": query_id,
        },
    )
