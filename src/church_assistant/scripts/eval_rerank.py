"""
Measure retrieval quality on the real corpus — and compare rerankers on it.

Backlog #1 has said "tune RERANK_SCORE_GOOD/OK" since Voyage was replaced, and
the thresholds have been moved twice by eye because there was nothing to move
them by. This produces the numbers: how often the right topic actually comes
back first, how much of that is Qdrant's doing versus the reranker's, and what
the score distribution looks like so a threshold can come from data.

GROUND TRUTH. Each protocol chunk carries `topic_title` and `body` separately,
so the title is a natural query whose right answer is known: the chunk it came
from.

Candidates here are the BODY ONLY, while production reranks `title\nbody`
(rag._hit_text_for_rerank). That difference is deliberate and it matters: with
the title present, the gold candidate would contain the query verbatim and the
whole thing would measure string matching. Body-only asks "given how a topic was
summarised, find the discussion it summarises", which is both non-trivial and
close to what a minister actually types.

The consequence is that these numbers are a LOWER BOUND, not production
accuracy — production also gets to match on the title. Use them to compare
models and to place thresholds, not to claim how often the archive answers
correctly.

TWO PHASES, because retrieval is the expensive part and does not depend on the
reranker. Phase one embeds each title and pulls candidates from Qdrant once,
saving the set. Phase two scores any number of models against that same set, so
a comparison is fair by construction and a second model costs only its own
reranking.

    # build the evaluation set once (needs Ollama + Qdrant)
    uv run python -m church_assistant.scripts.eval_rerank --build -o eval.json

    # score models against it
    uv run python -m church_assistant.scripts.eval_rerank -i eval.json \\
        --model BAAI/bge-reranker-v2-m3 --model BAAI/bge-reranker-base

RECALL IS REPORTED SEPARATELY. A reranker cannot rank a document Qdrant never
returned, so those cases are counted as misses for the end-to-end number AND
broken out — otherwise a retrieval problem reads as a reranker problem and the
wrong component gets replaced.

⚠️ Run this when Gemma is NOT resident. She is 17 GB of unified memory and
evicts the cross-encoder's weights, which costs ~9× per call on the M1 — enough
to dominate any timing this prints. The quality numbers are unaffected.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from qdrant_client import AsyncQdrantClient

from church_assistant.shared import collections as coll
from church_assistant.shared import local_embed


DEFAULT_POOL = 20          # matches rag.RERANK_POOL_MULTIPLIER × DEFAULT_LIMIT
DEFAULT_TENANT = "default"


async def build_eval_set(
    tenant_slug: str, qdrant_url: str, pool: int, limit: Optional[int],
) -> dict[str, Any]:
    """Embed each topic title, pull candidates, record where the gold chunk landed."""
    collection = coll.all_collections(tenant_slug)["protocols"]
    client = AsyncQdrantClient(url=qdrant_url)

    print(f"  читаю {collection}…", flush=True)
    points: list[Any] = []
    offset = None
    while True:
        batch, offset = await client.scroll(
            collection_name=collection, limit=256, offset=offset, with_payload=True,
        )
        points.extend(batch)
        if offset is None:
            break
    print(f"  точок: {len(points)}", flush=True)

    titles = {p.id: (p.payload or {}).get("topic_title", "").strip() for p in points}
    bodies = {p.id: (p.payload or {}).get("body", "").strip() for p in points}

    # A title shared by several chunks has no single right answer, and a chunk
    # with no body has nothing to find. Dropping both is honest; silently
    # picking one of the duplicates would not be.
    dupes = {t for t, n in Counter(t for t in titles.values() if t).items() if n > 1}
    usable = [pid for pid, t in titles.items() if t and t not in dupes and bodies[pid]]
    skipped = len(points) - len(usable)
    if skipped:
        print(f"  пропущено {skipped} (неунікальний заголовок або порожнє тіло)", flush=True)
    if limit:
        usable = usable[:limit]

    items: list[dict[str, Any]] = []
    t0 = time.perf_counter()
    for n, pid in enumerate(usable, 1):
        title = titles[pid]
        vector = await local_embed.aembed_one(title)
        # query_points, not search(): same call rag._qdrant_search makes, so the
        # candidate set here is the one production would have seen.
        res = await client.query_points(
            collection_name=collection, query=vector,
            limit=pool, with_payload=True,
        )
        hits = res.points
        cand_ids = [h.id for h in hits]
        cands = [(h.payload or {}).get("body", "").strip() for h in hits]
        items.append({
            "query": title,
            "candidates": cands,
            "gold_index": cand_ids.index(pid) if pid in cand_ids else None,
        })
        if n % 25 == 0:
            rate = (time.perf_counter() - t0) / n
            print(f"    {n}/{len(usable)}  (~{rate*(len(usable)-n)/60:.1f} хв лишилось)", flush=True)

    await client.close()
    return {"collection": collection, "pool": pool, "items": items}


def score_model(model_name: str, evalset: dict[str, Any]) -> dict[str, Any]:
    """Rerank every item with one model and report where the gold chunk lands."""
    os.environ["RERANK_MODEL"] = model_name
    # Imported here, after the env var is set: the module reads it at import.
    import importlib
    from church_assistant.shared import local_rerank
    importlib.reload(local_rerank)

    items = evalset["items"]
    top1 = top3 = top5 = 0
    rr_sum = 0.0
    not_retrieved = 0
    gold_scores: list[float] = []
    other_scores: list[float] = []

    t0 = time.perf_counter()
    for n, it in enumerate(items, 1):
        gold = it["gold_index"]
        if gold is None:
            not_retrieved += 1          # counted as a miss below, not skipped
            continue
        ranked = local_rerank.rerank(it["query"], it["candidates"], top_k=None)
        order = [i for i, _ in ranked]
        pos = order.index(gold)
        if pos == 0: top1 += 1
        if pos < 3:  top3 += 1
        if pos < 5:  top5 += 1
        rr_sum += 1.0 / (pos + 1)
        for i, sc in ranked:
            (gold_scores if i == gold else other_scores).append(sc)
        if n % 50 == 0:
            rate = (time.perf_counter() - t0) / n
            print(f"    {model_name}: {n}/{len(items)}  "
                  f"(~{rate*(len(items)-n)/60:.1f} хв)", flush=True)

    total = len(items)                  # misses included — end-to-end number
    return {
        "model": model_name,
        "total": total,
        "not_retrieved": not_retrieved,
        "top1": top1, "top3": top3, "top5": top5,
        "mrr": rr_sum / total if total else 0.0,
        "seconds": time.perf_counter() - t0,
        "gold_scores": gold_scores,
        "other_scores": other_scores,
    }


def _pct(n: int, total: int) -> str:
    return f"{n}/{total} ({n/total:.0%})" if total else "—"


def _quantiles(xs: list[float]) -> str:
    if not xs:
        return "—"
    s = sorted(xs)
    q = lambda p: s[min(int(p * len(s)), len(s) - 1)]
    return f"p10 {q(0.10):.3f} · медіана {q(0.50):.3f} · p90 {q(0.90):.3f}"


def report(results: list[dict[str, Any]]) -> None:
    print()
    print("=" * 78)
    print("  Якість реранкінгу на корпусі")
    print("=" * 78)
    for r in results:
        t = r["total"]
        print(f"\n  {r['model']}")
        print(f"    top-1 {_pct(r['top1'], t)} · top-3 {_pct(r['top3'], t)} · "
              f"top-5 {_pct(r['top5'], t)}")
        print(f"    MRR {r['mrr']:.3f} · {r['seconds']:.0f} с на {t} запитів "
              f"({r['seconds']/max(t,1):.2f} с/запит)")
        if r["not_retrieved"]:
            print(f"    ⚠ {r['not_retrieved']} тем Qdrant узагалі не повернув — "
                  f"це стеля, реранкер тут ні до чого")
        # The thresholds exist to colour hits green/yellow/dim. They should come
        # from where gold and non-gold scores actually separate.
        print(f"    скори правильних:  {_quantiles(r['gold_scores'])}")
        print(f"    скори решти:       {_quantiles(r['other_scores'])}")
    print()


async def _amain(args: argparse.Namespace) -> int:
    if args.build:
        evalset = await build_eval_set(
            args.tenant, args.qdrant_url, args.pool, args.limit,
        )
        Path(args.output).write_text(
            json.dumps(evalset, ensure_ascii=False), encoding="utf-8",
        )
        n = len(evalset["items"])
        missing = sum(1 for i in evalset["items"] if i["gold_index"] is None)
        print(f"\n  ✓ {n} запитів → {args.output}")
        print(f"    Qdrant не повернув правильну тему в {missing} випадках "
              f"({missing/max(n,1):.0%}) — це стеля для будь-якого реранкера")
        return 0

    evalset = json.loads(Path(args.input).read_text(encoding="utf-8"))
    print(f"  набір: {len(evalset['items'])} запитів, пул {evalset['pool']}")
    results = [score_model(m, evalset) for m in args.model]
    report(results)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(
            [{k: v for k, v in r.items() if not k.endswith("_scores")} for r in results],
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"  ✓ підсумок → {args.json_out}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--build", action="store_true", help="побудувати набір (Ollama + Qdrant)")
    p.add_argument("-o", "--output", default="eval_rerank_set.json")
    p.add_argument("-i", "--input", default="eval_rerank_set.json")
    p.add_argument("--model", action="append", default=[],
                   help="модель для оцінки (можна кілька разів)")
    p.add_argument("--tenant", default=DEFAULT_TENANT)
    p.add_argument("--qdrant-url", default=os.getenv("QDRANT_URL", "http://localhost:6333"))
    p.add_argument("--pool", type=int, default=DEFAULT_POOL)
    p.add_argument("--limit", type=int, default=None, help="взяти лише перші N тем")
    p.add_argument("--json-out", default=None)
    args = p.parse_args()

    if not args.build and not args.model:
        args.model = [os.getenv("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")]
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
