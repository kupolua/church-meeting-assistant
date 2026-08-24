"""
Local cross-encoder reranker (bge-reranker-v2-m3) — replaces Voyage rerank-2.

Runs fully on the machine via sentence-transformers. The model (~2.2 GB) is
loaded lazily as a process-wide singleton on first use and reused thereafter,
so importing this module is cheap (no torch import until you actually rerank).

bge-reranker-v2-m3 is a multilingual cross-encoder (strong on Ukrainian). It
scores (query, document) pairs; we sigmoid the logit to a 0-1 relevance score
to match the previous rerank_score semantics used by the UI.

Config (env):
    RERANK_MODEL   default 'BAAI/bge-reranker-v2-m3'
    RERANK_DEVICE  default 'auto'  → mps (Apple Silicon) / cuda / cpu, whichever
                   is available. Set explicitly to force ('cpu' | 'mps' | 'cuda').
    RERANK_MAX_LEN default 512
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional
from dotenv import load_dotenv

# Load .env before reading it — see shared/health.py for why these constants
# must not depend on someone else having called load_dotenv() first.
load_dotenv()

RERANK_MODEL = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
RERANK_DEVICE = os.getenv("RERANK_DEVICE", "auto")
RERANK_MAX_LEN = int(os.getenv("RERANK_MAX_LEN", "512"))

_std = logging.getLogger("church_assistant.rerank")
_model = None
_lock = threading.Lock()


def _resolve_device(pref: str) -> str:
    """Resolve 'auto' → mps / cuda / cpu; honor an explicit choice as-is."""
    if pref and pref != "auto":
        return pref
    try:
        import torch
        if torch.backends.mps.is_available():
            return "mps"
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    return "cpu"


def _load(device: str):
    from sentence_transformers import CrossEncoder  # heavy: lazy
    return CrossEncoder(RERANK_MODEL, max_length=RERANK_MAX_LEN, device=device)


def _get_model():
    """Load the CrossEncoder once (thread-safe lazy singleton), with cpu fallback."""
    global _model
    if _model is None:
        with _lock:
            if _model is None:
                device = _resolve_device(RERANK_DEVICE)
                try:
                    _model = _load(device)
                    _std.info("reranker loaded on %s", device)
                except Exception as e:
                    if device != "cpu":
                        _std.warning("reranker on %s failed (%s) — falling back to cpu", device, e)
                        _model = _load("cpu")
                    else:
                        raise
    return _model


def _sigmoid(x: float) -> float:
    import math
    # numerically stable
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def rerank(
    query: str,
    documents: list[str],
    top_k: Optional[int] = None,
) -> list[tuple[int, float]]:
    """
    Score each document against the query, return [(original_index, score), ...]
    sorted by score DESC, truncated to top_k. Scores are sigmoid(logit) ∈ (0,1).

    Blocking (CPU/torch) — callers on the event loop should use asyncio.to_thread.
    """
    if not documents:
        return []

    # Timed in two parts on purpose. A rerank that is slow because the model is
    # loading for the first time and one that is slow every single call are
    # different problems, and the single number the query row records cannot
    # tell them apart — which is exactly how a tenfold gap between this call in
    # the worker and the same call in a one-off process stayed unexplained.
    t0 = time.perf_counter()
    model = _get_model()
    t_load = time.perf_counter() - t0

    pairs = [(query, doc) for doc in documents]
    t1 = time.perf_counter()
    raw = model.predict(pairs)  # logits (np.ndarray), one per pair
    t_predict = time.perf_counter() - t1

    _std.info(
        "rerank: %d pairs, %d chars → load %.2fs + predict %.2fs (device=%s)",
        len(pairs), sum(len(d) for d in documents), t_load, t_predict,
        getattr(getattr(model, "model", None), "device", "?"),
    )

    scored = [(i, _sigmoid(float(s))) for i, s in enumerate(raw)]
    scored.sort(key=lambda t: t[1], reverse=True)
    if top_k is not None:
        scored = scored[:top_k]
    return scored


# ─────────────────────────────────────────────────────────────
# CLI smoke test:  uv run python -m church_assistant.shared.local_rerank
# ─────────────────────────────────────────────────────────────

def _smoke_test() -> None:
    print(f"Loading reranker {RERANK_MODEL!r} on {RERANK_DEVICE} (first run downloads ~2.2GB)...")
    q = "адміністративні питання церкви"
    docs = [
        "Обговорення бюджету, реєстрації та адміністративних рішень громади.",
        "Роздуми над Псалмом 84 про блаженство людини.",
        "Питання членства та прийняття нових членів церкви.",
    ]
    out = rerank(q, docs, top_k=3)
    print("  ranked:")
    for idx, score in out:
        print(f"    [{idx}] {score:.3f}  {docs[idx][:50]}")
    assert out[0][0] in (0, 2), "expected an admin/membership doc on top"
    print("  ✓ OK")


if __name__ == "__main__":
    _smoke_test()
