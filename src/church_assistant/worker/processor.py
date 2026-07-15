"""
Query processor — run the RAG pipeline for one dequeued query.

Given a query row already marked 'processing' (by fetch_next_pending), this:
    1. Runs shared.rag.answer() (embed → Qdrant → rerank → Gemma).
    2. On success: mark_completed + deliver to Telegram (if source='telegram').
    3. On failure: mark_failed; requeue if under the retry cap, else notify the
       user (Telegram) that we gave up.

Never raises — the caller's loop must keep running no matter what one query does.
"""

from __future__ import annotations

import traceback
from typing import Any, Optional

from telegram import Bot

from church_assistant.bot import delivery
from church_assistant.db import queries_repo
from church_assistant.shared import rag
from church_assistant.shared.logger import Logger


_log = Logger(process="worker")


async def process_query(
    pool: Any,
    bot: Optional[Bot],
    query: dict[str, Any],
    *,
    max_retries: int,
) -> None:
    """
    Process a single 'processing' query end-to-end.

    Args:
        pool: async DB pool
        bot: initialized telegram Bot (for delivery); may be None if bot
             delivery is disabled (e.g. web-only reprocessing)
        query: the query row (must include id, question, collection, source,
               telegram_chat_id, telegram_message_id)
        max_retries: attempts allowed before giving up permanently
    """
    query_id = query["id"]
    question = query["question"]
    collection = query.get("collection") or "protocols"
    source = query.get("source")

    await _log.info(
        "query.started",
        message=f"processing #{query_id}: {question[:80]}",
        query_id=query_id,
        user_id=query.get("user_id"),
    )

    # ─── Run RAG ─────────────────────────────────────────────
    try:
        result = await rag.answer(
            question,
            collection=collection,
            limit=5,
            rerank=True,
        )
    except Exception as e:
        await _handle_failure(pool, bot, query, e, max_retries=max_retries)
        return

    # ─── Persist success ─────────────────────────────────────
    try:
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
    except Exception as e:
        # DB write failed after a good answer — treat as a failure so it retries.
        await _handle_failure(pool, bot, query, e, max_retries=max_retries)
        return

    await _log.info(
        "query.completed",
        message=f"#{query_id} total={result.timings.total_ms}ms hits={len(result.hits)}",
        query_id=query_id,
        user_id=query.get("user_id"),
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

    # ─── Deliver (Telegram) ──────────────────────────────────
    if source == "telegram" and bot is not None:
        # Re-fetch canonical row so delivery sees exactly what was stored.
        completed = await queries_repo.get_by_id(pool, query_id)
        if completed is not None:
            await delivery.send_answer(bot, completed)


async def _handle_failure(
    pool: Any,
    bot: Optional[Bot],
    query: dict[str, Any],
    exc: Exception,
    *,
    max_retries: int,
) -> None:
    """
    Record a failure, then requeue (if under cap) or give up + notify.
    """
    query_id = query["id"]
    tb = traceback.format_exc()

    retry_count = await queries_repo.mark_failed(
        pool,
        query_id,
        error_message=f"{type(exc).__name__}: {exc}",
        error_traceback=tb,
        increment_retry=True,
    )

    await _log.record_error(
        error_type=type(exc).__name__,
        error_message=str(exc),
        traceback=tb,
        query_id=query_id,
        user_id=query.get("user_id"),
        metadata={"retry_count": retry_count, "max_retries": max_retries},
    )

    if retry_count < max_retries:
        await queries_repo.requeue_for_retry(pool, query_id)
        await _log.warn(
            "query.requeued",
            message=f"#{query_id} failed (attempt {retry_count}/{max_retries}), requeued",
            query_id=query_id,
        )
        return

    # Give up permanently.
    await _log.error(
        "query.gave_up",
        message=f"#{query_id} failed permanently after {retry_count} attempts",
        query_id=query_id,
    )

    if query.get("source") == "telegram" and bot is not None:
        await delivery.send_failure(bot, query)
