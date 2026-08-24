"""
Local text embeddings via Ollama (bge-m3) — replaces Voyage voyage-multilingual-2.

Everything stays on the machine. bge-m3 is a strong multilingual (incl.
Ukrainian) embedder, 1024-dim (matches the existing Qdrant schema), and is
SYMMETRIC — unlike Voyage there is no query/document input-type distinction, so
indexing and querying use the exact same encoding (→ compatible vectors).

Served through the Ollama instance the project already runs (reuses infra +
health-gating). Pull once:  ollama pull bge-m3

Two entry points, both hitting the same model → identical vectors:
    - aembed(texts)  : async  (serving path — rag.py)
    - embed(texts)   : sync   (batch indexing — index_meeting.py)
"""

from __future__ import annotations

import os

import httpx
from dotenv import load_dotenv


# Load .env before reading it — see shared/health.py for why these constants
# must not depend on someone else having called load_dotenv() first.
load_dotenv()

EMBED_MODEL = os.getenv("EMBED_MODEL", "bge-m3")
EMBED_DIM = 1024
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
_EMBED_TIMEOUT = int(os.getenv("EMBED_TIMEOUT", "120"))


def _parse(data: dict) -> list[list[float]]:
    """Extract embeddings from an Ollama /api/embed response."""
    embs = data.get("embeddings")
    if not embs:
        raise RuntimeError(
            f"Ollama /api/embed returned no embeddings for model {EMBED_MODEL!r} "
            f"(is it pulled? run: ollama pull {EMBED_MODEL})"
        )
    return embs


async def aembed(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts (async). Returns vectors in input order."""
    if not texts:
        return []
    async with httpx.AsyncClient(timeout=_EMBED_TIMEOUT) as client:
        resp = await client.post(
            f"{OLLAMA_URL}/api/embed",
            json={"model": EMBED_MODEL, "input": texts},
        )
        resp.raise_for_status()
        return _parse(resp.json())


async def aembed_one(text: str) -> list[float]:
    """Embed a single text (async) — serving convenience."""
    vecs = await aembed([text])
    return vecs[0]


def embed(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts (sync) — for the indexing CLI."""
    if not texts:
        return []
    with httpx.Client(timeout=_EMBED_TIMEOUT) as client:
        resp = client.post(
            f"{OLLAMA_URL}/api/embed",
            json={"model": EMBED_MODEL, "input": texts},
        )
        resp.raise_for_status()
        return _parse(resp.json())


# ─────────────────────────────────────────────────────────────
# CLI smoke test:  uv run python -m church_assistant.shared.local_embed
# ─────────────────────────────────────────────────────────────

def _smoke_test() -> None:
    print(f"Embedding via Ollama model {EMBED_MODEL!r} @ {OLLAMA_URL}")
    vecs = embed(["Що обговорювали на зустрічі?", "адміністративні питання"])
    assert len(vecs) == 2, vecs
    dim = len(vecs[0])
    print(f"  ✓ got {len(vecs)} vectors, dim={dim}")
    assert dim == EMBED_DIM, f"expected {EMBED_DIM}-dim, got {dim}"
    # sanity: normalized cosine of a text with itself ≈ 1
    import math
    a = vecs[0]
    dot = sum(x * x for x in a)
    print(f"  ✓ |v|²={dot:.3f} (dim {dim})")
    print("  ✓ OK")


if __name__ == "__main__":
    _smoke_test()
