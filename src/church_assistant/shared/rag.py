"""
Async RAG service — refactored from CLI `query.py` for use in web + worker.

Behavior is a 1-to-1 async port of church_assistant.query, keeping the same:
    - Voyage voyage-multilingual-2 embeddings (input_type='query')
    - Qdrant vector search with 4×limit pool for rerank
    - Voyage rerank-2 for precise ordering
    - Gemma 4 26B synthesis via Ollama /api/chat
    - Same system prompt (Ukrainian, strict citation rules)
    - Same score thresholds

Two APIs:
    - answer(question, ...) → full RAG pipeline, returns AnswerResult
    - retrieve(question, ...) → retrieval-only (for /verbose, dashboards)

The CLI query.py is NOT changed; this module is a parallel async
implementation for use by web (FastAPI) and worker (background) processes.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

import httpx
from qdrant_client import AsyncQdrantClient


# ─────────────────────────────────────────────────────────────
# Constants — must match index_meeting.py and query.py
# ─────────────────────────────────────────────────────────────

COLLECTION_PROTOCOLS = "cma_protocols"
COLLECTION_ANALYSES = "cma_analyses"
COLLECTION_TURNS = "cma_turns"
COLLECTION_PROTOCOL_FULL = "cma_protocol_full"

COLLECTION_ALIASES = {
    "protocols": COLLECTION_PROTOCOLS,
    "analyses": COLLECTION_ANALYSES,
    "turns": COLLECTION_TURNS,
    "protocol_full": COLLECTION_PROTOCOL_FULL,
    # Full names also accepted
    COLLECTION_PROTOCOLS: COLLECTION_PROTOCOLS,
    COLLECTION_ANALYSES: COLLECTION_ANALYSES,
    COLLECTION_TURNS: COLLECTION_TURNS,
    COLLECTION_PROTOCOL_FULL: COLLECTION_PROTOCOL_FULL,
}

EMBEDDING_MODEL = "voyage-multilingual-2"
RERANK_MODEL = "rerank-2"
DEFAULT_LIMIT = 5
RERANK_POOL_MULTIPLIER = 4

GEMMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4:26b")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_TIMEOUT = int(os.getenv("OLLAMA_TIMEOUT", "600"))

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")

# Score thresholds — informational only in this module
# (kept for downstream UI to show green/yellow/dim)
SCORE_GOOD = 0.55
SCORE_OK = 0.40
RERANK_SCORE_GOOD = 0.50
RERANK_SCORE_OK = 0.20


# ─────────────────────────────────────────────────────────────
# Gemma system prompt (identical to query.py)
# ─────────────────────────────────────────────────────────────

GEMMA_SYSTEM_PROMPT = """Ти асистент по архіву протоколів пасторської ради. Користувач задає питання, ти отримуєш релевантні фрагменти з протоколів та маєш дати точну, лаконічну відповідь українською мовою.

ПРАВИЛА:
1. Відповідай ТІЛЬКИ на основі наданих фрагментів. Не вигадуй фактів.
2. Якщо фрагменти не містять відповіді, чесно скажи "У наявних протоколах не знайдено інформації про це."
3. Цитуй джерела у форматі [N] (де N — номер фрагмента). Кожне твердження має посилання.
4. Якщо інформація з кількох зустрічей — згадай дати.
5. Коротко, по суті. Без вступу та зайвих фраз."""


# ─────────────────────────────────────────────────────────────
# Data types
# ─────────────────────────────────────────────────────────────

@dataclass
class Hit:
    """
    One search result. Fields match query.py's Hit, plus a `to_dict()` for
    JSONB storage in the queries.hits column.
    """
    score: float                     # active score (rerank if reranked, else vector)
    payload: dict[str, Any]
    collection: str
    vector_score: float = 0.0        # original vector score, always preserved
    reranked: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSONB storage (must be json-safe)."""
        return {
            "score": self.score,
            "vector_score": self.vector_score,
            "reranked": self.reranked,
            "collection": self.collection,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Hit":
        """Deserialize from JSONB (used when re-showing cached queries)."""
        return cls(
            score=float(d["score"]),
            payload=d["payload"],
            collection=d["collection"],
            vector_score=float(d.get("vector_score", 0.0)),
            reranked=bool(d.get("reranked", False)),
        )


@dataclass
class Timings:
    """Per-stage wall-clock timings (milliseconds)."""
    embed_ms: int = 0
    qdrant_ms: int = 0
    rerank_ms: int = 0
    gemma_ms: int = 0
    total_ms: int = 0


@dataclass
class AnswerResult:
    """Result of answer() — full RAG pipeline."""
    question: str
    collection: str
    hits: list[Hit]
    synthesis: str
    sources: list[str] = field(default_factory=list)   # unique meeting dates
    timings: Timings = field(default_factory=Timings)

    def hits_as_json(self) -> list[dict[str, Any]]:
        """Convenience for storing in queries.hits JSONB."""
        return [h.to_dict() for h in self.hits]


@dataclass
class RetrievalResult:
    """Result of retrieve() — retrieval only, no synthesis."""
    question: str
    collection: str
    hits: list[Hit]
    timings: Timings = field(default_factory=Timings)


# ─────────────────────────────────────────────────────────────
# Voyage helpers (SDK is sync, wrap in to_thread)
# ─────────────────────────────────────────────────────────────

def _resolve_voyage_key() -> str:
    key = os.getenv("VOYAGE_API_KEY")
    if not key:
        raise RuntimeError("VOYAGE_API_KEY missing from environment")
    return key


def _voyage_embed_sync(text: str, api_key: str) -> list[float]:
    """Sync Voyage embed — called via asyncio.to_thread."""
    import voyageai
    client = voyageai.Client(api_key=api_key)
    result = client.embed(
        texts=[text],
        model=EMBEDDING_MODEL,
        input_type="query",
    )
    return result.embeddings[0]


def _voyage_rerank_sync(
    question: str,
    documents: list[str],
    top_k: int,
    api_key: str,
) -> list[tuple[int, float]]:
    """
    Sync Voyage rerank — called via asyncio.to_thread.
    Returns list of (original_index, relevance_score).
    """
    import voyageai
    client = voyageai.Client(api_key=api_key)
    result = client.rerank(
        query=question,
        documents=documents,
        model=RERANK_MODEL,
        top_k=min(top_k, len(documents)),
    )
    return [(item.index, item.relevance_score) for item in result.results]


async def embed_query(question: str) -> list[float]:
    """Async wrapper around Voyage embed."""
    api_key = _resolve_voyage_key()
    return await asyncio.to_thread(_voyage_embed_sync, question, api_key)


# ─────────────────────────────────────────────────────────────
# Qdrant retrieval (async native)
# ─────────────────────────────────────────────────────────────

async def _open_qdrant() -> AsyncQdrantClient:
    """
    Open an AsyncQdrantClient. Caller responsible for close().

    We don't cache a global client because Qdrant may restart independently
    (docker container). Opening is cheap.
    """
    # Parse host/port from URL
    from urllib.parse import urlparse
    parsed = urlparse(QDRANT_URL)
    host = parsed.hostname or "localhost"
    port = parsed.port or 6333
    return AsyncQdrantClient(host=host, port=port)


async def _search_qdrant(
    client: AsyncQdrantClient,
    collection: str,
    vector: list[float],
    limit: int,
) -> list[Hit]:
    result = await client.query_points(
        collection_name=collection,
        query=vector,
        limit=limit,
        with_payload=True,
    )
    return [
        Hit(
            score=p.score,
            payload=p.payload,
            collection=collection,
            vector_score=p.score,
        )
        for p in result.points
    ]


# ─────────────────────────────────────────────────────────────
# Rerank helpers
# ─────────────────────────────────────────────────────────────

def _hit_text_for_rerank(hit: Hit) -> str:
    """Build candidate document text for rerank-2. Matches query.py."""
    p = hit.payload
    if hit.collection == COLLECTION_TURNS:
        speaker = p.get("speaker", "?")
        return f"{speaker}: {p.get('text', '')}"
    title = p.get("topic_title", "")
    body = p.get("body", "")
    return f"{title}\n{body}".strip() if body else title


async def rerank_hits(
    question: str,
    hits: list[Hit],
    top_k: int,
) -> list[Hit]:
    """Async wrapper around Voyage rerank."""
    if not hits:
        return hits
    api_key = _resolve_voyage_key()
    documents = [_hit_text_for_rerank(h) for h in hits]
    idx_scores = await asyncio.to_thread(
        _voyage_rerank_sync, question, documents, top_k, api_key,
    )
    reranked: list[Hit] = []
    for original_idx, score in idx_scores:
        original = hits[original_idx]
        reranked.append(Hit(
            score=score,
            payload=original.payload,
            collection=original.collection,
            vector_score=original.score,
            reranked=True,
        ))
    return reranked


# ─────────────────────────────────────────────────────────────
# Gemma via Ollama (async httpx)
# ─────────────────────────────────────────────────────────────

def _format_hit_for_context(hit: Hit, idx: int) -> str:
    """Detailed hit for LLM context — matches query.py format_hit_for_context."""
    p = hit.payload
    date = p.get("meeting_date", "?")

    if hit.collection == COLLECTION_PROTOCOLS:
        topic = p.get("topic_title", "?")
        attendees = ", ".join(p.get("attendees", [])[:5])
        return (
            f"[{idx}] Meeting {date}, topic '{topic}'\n"
            f"  Attendees: {attendees}\n"
            f"  (Score: {hit.score:.3f})"
        )

    if hit.collection == COLLECTION_ANALYSES:
        topic = p.get("topic_title", "?")
        tr = p.get("time_range", "?")
        return (
            f"[{idx}] Meeting {date}, chunk {tr}, topic '{topic}'\n"
            f"  (Score: {hit.score:.3f})"
        )

    if hit.collection == COLLECTION_TURNS:
        speaker = p.get("speaker", "?")
        ts = p.get("start_timestamp", "?")
        text = p.get("text", "")
        return (
            f"[{idx}] Meeting {date}, at {ts}, {speaker} said:\n"
            f"  \"{text}\"\n"
            f"  (Score: {hit.score:.3f})"
        )

    if hit.collection == COLLECTION_PROTOCOL_FULL:
        return f"[{idx}] Meeting {date} (overview)  (Score: {hit.score:.3f})"

    return f"[{idx}] {hit.payload}  (Score: {hit.score:.3f})"


def _build_gemma_user_prompt(question: str, hits: list[Hit]) -> str:
    """Compose the user_prompt from question + hits. Matches query.py."""
    context_blocks = [
        _format_hit_for_context(h, i) for i, h in enumerate(hits, 1)
    ]
    context = "\n\n".join(context_blocks)

    body_blocks = []
    for i, hit in enumerate(hits, 1):
        p = hit.payload
        if hit.collection == COLLECTION_TURNS:
            body_text = p.get("text", "")
        elif hit.collection in (COLLECTION_PROTOCOLS, COLLECTION_ANALYSES):
            body_text = p.get("body", "") or p.get("topic_title", "")
            title = p.get("topic_title", "")
            if title and not body_text.startswith(title):
                body_text = f"{title}\n{body_text}"
        else:
            body_text = p.get("topic_title", "")
        body_blocks.append(f"[{i}]: {body_text}")
    body_section = "\n\n".join(body_blocks)

    return (
        f"Питання: {question}\n\n"
        f"Релевантні фрагменти з протоколів:\n\n"
        f"{context}\n\n"
        f"Зміст фрагментів:\n\n"
        f"{body_section}\n\n"
        f"Дай відповідь на основі цих фрагментів."
    )


async def call_gemma(question: str, hits: list[Hit]) -> str:
    """
    Call Gemma via Ollama /api/chat, return synthesized text.

    Raises httpx.HTTPError on connection failure (worker will catch and retry).
    """
    user_prompt = _build_gemma_user_prompt(question, hits)

    async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
        response = await client.post(
            f"{OLLAMA_URL}/api/chat",
            json={
                "model": GEMMA_MODEL,
                "messages": [
                    {"role": "system", "content": GEMMA_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": False,
                "options": {
                    "temperature": 0.1,
                    "num_ctx": 16384,
                },
            },
        )
        response.raise_for_status()
        data = response.json()
        return data.get("message", {}).get("content", "(empty response)")


# ─────────────────────────────────────────────────────────────
# Public API — retrieve() and answer()
# ─────────────────────────────────────────────────────────────

async def retrieve(
    question: str,
    *,
    collection: str = "protocols",
    limit: int = DEFAULT_LIMIT,
    rerank: bool = True,
) -> RetrievalResult:
    """
    Retrieval-only: embed → Qdrant search → optional rerank.

    Does NOT call Gemma. Use for /verbose, dashboards, or preview.

    Raises:
        RuntimeError: if VOYAGE_API_KEY missing
        ValueError: if collection invalid
        qdrant errors: connection/collection issues
    """
    full_collection = COLLECTION_ALIASES.get(collection)
    if full_collection is None:
        raise ValueError(f"Unknown collection: {collection!r}")

    t_total_start = time.perf_counter()

    # 1. Embed
    t = time.perf_counter()
    vec = await embed_query(question)
    embed_ms = int((time.perf_counter() - t) * 1000)

    # 2. Qdrant search
    pool_size = limit * RERANK_POOL_MULTIPLIER if rerank else limit
    client = await _open_qdrant()
    try:
        t = time.perf_counter()
        hits = await _search_qdrant(client, full_collection, vec, pool_size)
        qdrant_ms = int((time.perf_counter() - t) * 1000)
    finally:
        await client.close()

    # 3. Rerank (optional)
    rerank_ms = 0
    if rerank and hits:
        t = time.perf_counter()
        hits = await rerank_hits(question, hits, top_k=limit)
        rerank_ms = int((time.perf_counter() - t) * 1000)
    elif not rerank:
        hits = hits[:limit]

    total_ms = int((time.perf_counter() - t_total_start) * 1000)

    return RetrievalResult(
        question=question,
        collection=full_collection,
        hits=hits,
        timings=Timings(
            embed_ms=embed_ms,
            qdrant_ms=qdrant_ms,
            rerank_ms=rerank_ms,
            gemma_ms=0,
            total_ms=total_ms,
        ),
    )


async def answer(
    question: str,
    *,
    collection: str = "protocols",
    limit: int = DEFAULT_LIMIT,
    rerank: bool = True,
) -> AnswerResult:
    """
    Full RAG pipeline: retrieve + Gemma synthesis.

    Raises:
        RuntimeError: if VOYAGE_API_KEY missing
        ValueError: if collection invalid
        httpx.HTTPError: if Ollama unreachable / times out
        qdrant errors: connection/collection issues

    Callers (web/worker) should catch exceptions and record them via logs_repo.
    """
    ret = await retrieve(
        question,
        collection=collection,
        limit=limit,
        rerank=rerank,
    )

    if not ret.hits:
        # No hits: skip Gemma (would hallucinate)
        return AnswerResult(
            question=question,
            collection=ret.collection,
            hits=[],
            synthesis="У наявних протоколах не знайдено інформації про це.",
            sources=[],
            timings=ret.timings,
        )

    # Gemma synthesis
    t = time.perf_counter()
    synthesis = await call_gemma(question, ret.hits)
    gemma_ms = int((time.perf_counter() - t) * 1000)

    sources = sorted({
        h.payload.get("meeting_date", "?")
        for h in ret.hits
        if h.payload.get("meeting_date")
    })

    return AnswerResult(
        question=question,
        collection=ret.collection,
        hits=ret.hits,
        synthesis=synthesis,
        sources=sources,
        timings=Timings(
            embed_ms=ret.timings.embed_ms,
            qdrant_ms=ret.timings.qdrant_ms,
            rerank_ms=ret.timings.rerank_ms,
            gemma_ms=gemma_ms,
            total_ms=ret.timings.total_ms + gemma_ms,
        ),
    )


# ─────────────────────────────────────────────────────────────
# Formatting helpers (usable in web templates + telegram messages)
# ─────────────────────────────────────────────────────────────

def format_hit_short(hit: Hit, idx: int) -> str:
    """
    Plain-text one-line hit summary (no color codes).

    Format: "[N] [YYYY-MM-DD] 'Topic title'  (rerank 0.844 ← vec 0.435)"
    """
    p = hit.payload
    date = p.get("meeting_date", "?")

    if hit.reranked:
        score_str = f"rerank {hit.score:.3f} ← vec {hit.vector_score:.3f}"
    else:
        score_str = f"score {hit.score:.3f}"

    if hit.collection == COLLECTION_PROTOCOLS:
        topic = p.get("topic_title", "?")
        return f"[{idx}] [{date}] '{topic}'  ({score_str})"

    if hit.collection == COLLECTION_ANALYSES:
        topic = p.get("topic_title", "?")
        tr = p.get("time_range", "?")
        return f"[{idx}] [{date} chunk {tr}] '{topic}'  ({score_str})"

    if hit.collection == COLLECTION_TURNS:
        speaker = p.get("speaker", "?")
        ts = p.get("start_timestamp", "?")
        text = p.get("text", "")[:100].replace("\n", " ")
        return f"[{idx}] [{date} {ts}] {speaker}: \"{text}...\"  ({score_str})"

    if hit.collection == COLLECTION_PROTOCOL_FULL:
        return f"[{idx}] [{date}] meeting summary  ({score_str})"

    return f"[{idx}] [{date}] unknown  ({score_str})"


def score_color_hint(hit: Hit) -> str:
    """
    Return 'green' | 'yellow' | 'dim' for UI color coding.

    Templates and Telegram formatters can map these to CSS or emoji.
    """
    if hit.reranked:
        if hit.score >= RERANK_SCORE_GOOD:
            return "green"
        if hit.score >= RERANK_SCORE_OK:
            return "yellow"
        return "dim"
    else:
        if hit.score >= SCORE_GOOD:
            return "green"
        if hit.score >= SCORE_OK:
            return "yellow"
        return "dim"


# ─────────────────────────────────────────────────────────────
# CLI smoke test — hits real Qdrant + real Voyage + real Gemma
# ─────────────────────────────────────────────────────────────

async def _smoke_test() -> None:
    """
    Live end-to-end test against real Ollama/Qdrant/Voyage.

    Uses a real question that we know has answers in the corpus.
    Requires:
        - VOYAGE_API_KEY set
        - Qdrant docker running
        - Ollama with gemma4:26b running
    """
    from dotenv import load_dotenv
    load_dotenv()

    print("=" * 70)
    print("  shared/rag — end-to-end smoke test")
    print("=" * 70)
    print()

    # Test 1: retrieve() with rerank
    print("Test 1: retrieve() — 'Хто такий Назар?'")
    print("-" * 70)
    r = await retrieve(
        "Хто такий Назар?",
        collection="protocols",
        limit=5,
        rerank=True,
    )
    print(f"  Collection: {r.collection}")
    print(f"  Hits:       {len(r.hits)}")
    print(f"  Timings:    embed={r.timings.embed_ms}ms, "
          f"qdrant={r.timings.qdrant_ms}ms, "
          f"rerank={r.timings.rerank_ms}ms, "
          f"total={r.timings.total_ms}ms")
    print()
    print("  Top hits:")
    for i, h in enumerate(r.hits, 1):
        print(f"    {format_hit_short(h, i)}")
    print()
    assert len(r.hits) > 0, "Expected hits for 'Назар' — check Qdrant has data"

    # Test 2: retrieve() without rerank
    print()
    print("Test 2: retrieve() — same question WITHOUT rerank")
    print("-" * 70)
    r2 = await retrieve(
        "Хто такий Назар?",
        collection="protocols",
        limit=5,
        rerank=False,
    )
    print(f"  Hits:       {len(r2.hits)}")
    print(f"  Timings:    total={r2.timings.total_ms}ms (should be faster)")
    print()
    for i, h in enumerate(r2.hits, 1):
        assert not h.reranked
        print(f"    {format_hit_short(h, i)}")

    # Test 3: answer() — full pipeline with Gemma
    print()
    print("Test 3: answer() — 'Що вирішили про членство Леоніда?'")
    print("-" * 70)
    print("  (this hits Gemma — 10-30 seconds expected)")
    a = await answer(
        "Що вирішили про членство Леоніда?",
        collection="protocols",
        limit=5,
        rerank=True,
    )
    print()
    print(f"  Sources: {a.sources}")
    print(f"  Timings: embed={a.timings.embed_ms}ms, "
          f"qdrant={a.timings.qdrant_ms}ms, "
          f"rerank={a.timings.rerank_ms}ms, "
          f"gemma={a.timings.gemma_ms}ms, "
          f"TOTAL={a.timings.total_ms}ms")
    print()
    print("  Synthesis:")
    for line in a.synthesis.splitlines():
        print(f"    {line}")

    # Test 4: to_dict / from_dict round-trip
    print()
    print("Test 4: Hit.to_dict / from_dict round-trip")
    print("-" * 70)
    h_dict = a.hits[0].to_dict()
    h_back = Hit.from_dict(h_dict)
    assert h_back.score == a.hits[0].score
    assert h_back.payload == a.hits[0].payload
    assert h_back.collection == a.hits[0].collection
    print(f"  ✓ Serialization round-trip works")

    # Test 5: hits_as_json for DB storage
    print()
    print("Test 5: AnswerResult.hits_as_json (for JSONB storage)")
    print("-" * 70)
    hits_json = a.hits_as_json()
    assert isinstance(hits_json, list)
    assert all(isinstance(h, dict) for h in hits_json)
    print(f"  ✓ hits_as_json returns {len(hits_json)} plain dicts, JSON-safe")

    print()
    print("=" * 70)
    print("  ✓ ALL RAG SMOKE TESTS PASSED")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(_smoke_test())
