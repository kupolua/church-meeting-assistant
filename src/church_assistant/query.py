"""Basic RAG over indexed meeting protocols.

Searches the Qdrant `cma_*` collections for content relevant to a question,
then asks Gemma to synthesize an answer with citations to source meetings.

Architecture (basic — Phase 2B.2):
    1. Embed the question (Voyage voyage-multilingual-2, input_type=query)
    2. Vector search top K in cma_protocols (topic-level decisions)
    3. Optionally also search cma_turns (literal phrases, "who said X")
    4. Pass results as context to Gemma; generate answer with citations

This is the simplest useful version — no multi-query expansion, no hybrid
BM25, no rerank. Those come later (Phase 2B.2+).

Usage:
    uv run python -m church_assistant.query "Що ми вирішили про Великдень?"

    # Search literal phrases in turns (speaker-attributed)
    uv run python -m church_assistant.query "хто сказав X?" --collection turns

    # Show only retrieved hits, skip Gemma synthesis
    uv run python -m church_assistant.query "тема" --no-synth

    # More hits
    uv run python -m church_assistant.query "тема" --limit 10
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


# Constants (must match index_meeting.py)
COLLECTION_PROTOCOLS = "cma_protocols"
COLLECTION_ANALYSES = "cma_analyses"
COLLECTION_TURNS = "cma_turns"
COLLECTION_PROTOCOL_FULL = "cma_protocol_full"

EMBEDDING_MODEL = "voyage-multilingual-2"
DEFAULT_LIMIT = 5

GEMMA_MODEL = "gemma4:26b"  # match installed Ollama model
GEMMA_HOST = "http://localhost:11434"

# Score thresholds for relevance commentary (calibrated from smoke tests)
SCORE_GOOD = 0.55
SCORE_OK = 0.40


# ----- Logging -----


def log(msg: str, color: str = "") -> None:
    codes = {
        "blue":   "\033[94m",
        "green":  "\033[92m",
        "yellow": "\033[93m",
        "red":    "\033[91m",
        "bold":   "\033[1m",
        "dim":    "\033[2m",
        "reset":  "\033[0m",
    }
    if color in codes:
        print(f"{codes[color]}{msg}{codes['reset']}", flush=True)
    else:
        print(msg, flush=True)


def load_env() -> None:
    """Read .env into os.environ."""
    env_file = Path(".env")
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(
            key.strip(),
            value.strip().strip('"').strip("'"),
        )


# ----- Voyage embedding -----


def embed_query(text: str, api_key: str) -> list[float]:
    """Embed a single query string."""
    import voyageai
    client = voyageai.Client(api_key=api_key)
    result = client.embed(
        texts=[text],
        model=EMBEDDING_MODEL,
        input_type="query",   # critical: queries use different embedding
    )
    return result.embeddings[0]


# ----- Qdrant retrieval -----


@dataclass
class Hit:
    """One search result."""
    score: float
    payload: dict[str, Any]
    collection: str


def search_collection(
    client,
    collection: str,
    vector: list[float],
    limit: int,
) -> list[Hit]:
    """Vector search in a Qdrant collection."""
    hits = client.query_points(
        collection_name=collection,
        query=vector,
        limit=limit,
        with_payload=True,
    ).points
    return [
        Hit(score=h.score, payload=h.payload, collection=collection)
        for h in hits
    ]


# ----- Formatting hits -----


def format_hit_short(hit: Hit, idx: int) -> str:
    """Compact human-readable hit summary."""
    p = hit.payload
    score_color = (
        "green" if hit.score >= SCORE_GOOD
        else "yellow" if hit.score >= SCORE_OK
        else "dim"
    )
    date = p.get("meeting_date", "?")

    if hit.collection == COLLECTION_PROTOCOLS:
        topic = p.get("topic_title", "?")
        return f"[{idx}] [{date}] '{topic}'  (score {hit.score:.3f})"

    if hit.collection == COLLECTION_ANALYSES:
        topic = p.get("topic_title", "?")
        tr = p.get("time_range", "?")
        return f"[{idx}] [{date} chunk {tr}] '{topic}'  (score {hit.score:.3f})"

    if hit.collection == COLLECTION_TURNS:
        speaker = p.get("speaker", "?")
        ts = p.get("start_timestamp", "?")
        text = p.get("text", "")[:100].replace("\n", " ")
        return (f"[{idx}] [{date} {ts}] {speaker}: \"{text}...\"  "
                f"(score {hit.score:.3f})")

    if hit.collection == COLLECTION_PROTOCOL_FULL:
        return f"[{idx}] [{date}] meeting summary  (score {hit.score:.3f})"

    return f"[{idx}] [{date}] unknown  (score {hit.score:.3f})"


def format_hit_for_context(hit: Hit, idx: int) -> str:
    """Detailed hit for LLM context (with body text)."""
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


# ----- Gemma synthesis -----


GEMMA_SYSTEM_PROMPT = """Ти асистент по архіву протоколів пасторської ради. Користувач задає питання, ти отримуєш релевантні фрагменти з протоколів та маєш дати точну, лаконічну відповідь українською мовою.

ПРАВИЛА:
1. Відповідай ТІЛЬКИ на основі наданих фрагментів. Не вигадуй фактів.
2. Якщо фрагменти не містять відповіді, чесно скажи "У наявних протоколах не знайдено інформації про це."
3. Цитуй джерела у форматі [N] (де N — номер фрагмента). Кожне твердження має посилання.
4. Якщо інформація з кількох зустрічей — згадай дати.
5. Коротко, по суті. Без вступу та зайвих фраз."""


def call_gemma(question: str, hits: list[Hit]) -> str:
    """Call Gemma via Ollama with question and retrieved context."""
    import requests

    # Build context from hits
    context_blocks = [
        format_hit_for_context(h, i) for i, h in enumerate(hits, 1)
    ]
    context = "\n\n".join(context_blocks)

    # Include the body text of each hit for actual reading
    body_blocks = []
    for i, hit in enumerate(hits, 1):
        p = hit.payload
        if hit.collection == COLLECTION_TURNS:
            body_text = p.get("text", "")
        elif hit.collection in (COLLECTION_PROTOCOLS, COLLECTION_ANALYSES):
            # Prefer body (added in re-indexed data); fall back to title
            body_text = p.get("body", "") or p.get("topic_title", "")
            title = p.get("topic_title", "")
            if title and not body_text.startswith(title):
                body_text = f"{title}\n{body_text}"
        else:
            body_text = p.get("topic_title", "")
        body_blocks.append(f"[{i}]: {body_text}")
    body_section = "\n\n".join(body_blocks)

    user_prompt = (
        f"Питання: {question}\n\n"
        f"Релевантні фрагменти з протоколів:\n\n"
        f"{context}\n\n"
        f"Зміст фрагментів:\n\n"
        f"{body_section}\n\n"
        f"Дай відповідь на основі цих фрагментів."
    )

    log("\n  → Calling Gemma for synthesis...", "dim")

    try:
        response = requests.post(
            f"{GEMMA_HOST}/api/chat",
            json={
                "model": GEMMA_MODEL,
                "messages": [
                    {"role": "system", "content": GEMMA_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": False,
                "options": {
                    "temperature": 0.1,   # deterministic
                    "num_ctx": 16384,
                },
            },
            timeout=120,
        )
        response.raise_for_status()
        data = response.json()
        return data.get("message", {}).get("content", "(empty response)")
    except requests.exceptions.RequestException as e:
        return f"(Gemma error: {e})"


# ----- Main pipeline -----


def run_query(
    question: str,
    collection: str,
    limit: int,
    synthesize: bool,
) -> None:
    """Run the full RAG pipeline for one question."""
    log(f"\n{'=' * 70}", "blue")
    log(f"  Query: {question}", "blue")
    log(f"{'=' * 70}", "blue")
    log(f"  Collection: {collection}")
    log(f"  Limit:      {limit}")
    log(f"  Synthesize: {synthesize}")

    # 1. Embed
    api_key = os.environ.get("VOYAGE_API_KEY")
    if not api_key:
        log("\n❌ VOYAGE_API_KEY missing from .env", "red")
        sys.exit(1)

    log("\n  Embedding query...", "dim")
    vec = embed_query(question, api_key)

    # 2. Search Qdrant
    log("  Searching Qdrant...", "dim")
    from qdrant_client import QdrantClient
    client = QdrantClient(host="localhost", port=6333)

    full_collection = (
        collection
        if collection.startswith("cma_")
        else f"cma_{collection}"
    )
    hits = search_collection(client, full_collection, vec, limit)

    # 3. Show hits
    log(f"\n{'─' * 70}")
    log(f"  Top {len(hits)} hits in '{full_collection}':", "bold")
    log(f"{'─' * 70}")
    for i, h in enumerate(hits, 1):
        log("  " + format_hit_short(h, i))

    if not hits:
        log("\n  (no hits — query did not match anything in this collection)",
            "yellow")
        return

    # 4. Synthesize answer with Gemma (optional)
    if synthesize:
        answer = call_gemma(question, hits)
        log(f"\n{'─' * 70}")
        log(f"  Answer:", "bold")
        log(f"{'─' * 70}")
        log(answer)

        # Show source dates for quick reference
        dates = sorted({h.payload.get("meeting_date", "?") for h in hits})
        log(f"\n  Sources: {', '.join(dates)}", "dim")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Query indexed meeting protocols (basic RAG)"
    )
    parser.add_argument(
        "question",
        type=str,
        help="The question to ask, in any language",
    )
    parser.add_argument(
        "--collection",
        type=str,
        default="protocols",
        choices=["protocols", "analyses", "turns", "protocol_full",
                 "cma_protocols", "cma_analyses", "cma_turns",
                 "cma_protocol_full"],
        help="Which collection to search (default: protocols)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Number of hits to retrieve (default: {DEFAULT_LIMIT})",
    )
    parser.add_argument(
        "--no-synth",
        action="store_true",
        help="Don't call Gemma — just show retrieval hits",
    )
    args = parser.parse_args()

    load_env()
    run_query(
        question=args.question,
        collection=args.collection,
        limit=args.limit,
        synthesize=not args.no_synth,
    )


if __name__ == "__main__":
    main()
