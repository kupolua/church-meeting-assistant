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
import subprocess
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
        # Titles too, so the same set can be scored the way PRODUCTION reranks
        # (rag._hit_text_for_rerank sends "title\nbody"). Kept separate rather
        # than pre-joined: which text an arm sees is the variable under test,
        # and baking one choice into the file would hide it.
        cand_titles = [(h.payload or {}).get("topic_title", "").strip() for h in hits]
        items.append({
            "query": title,
            "candidates": cands,
            "candidate_titles": cand_titles,
            "gold_index": cand_ids.index(pid) if pid in cand_ids else None,
        })
        if n % 25 == 0:
            rate = (time.perf_counter() - t0) / n
            print(f"    {n}/{len(usable)}  (~{rate*(len(usable)-n)/60:.1f} хв лишилось)", flush=True)

    await client.close()
    return {"collection": collection, "pool": pool, "items": items}


BASELINE_LABEL = "(без реранку — порядок Qdrant)"


def score_vector_only(evalset: dict[str, Any]) -> dict[str, Any]:
    """
    The baseline every rerank number should be read against: change nothing.

    Free, and fair by construction. build_eval_set stores candidates in the
    order query_points returned them, so `gold_index` already IS the gold
    chunk's rank under pure vector search — no model, no embedding, no second
    retrieval. This arm differs from the others in exactly one thing, which is
    the entire point of having it.

    ⚠️ IT WAS MISSING, AND ITS ABSENCE SHAPED A DECISION. The 25.08 comparison
    ran bge-reranker-v2-m3 against bge-reranker-base and kept v2-m3 — a true
    answer to "which reranker", read ever since as if it answered "a reranker at
    all". Those are different questions, and the second one is the expensive
    one: reranking costs ~18 s per query on this machine once Gemma is resident
    (backlog 1b), against ~1.4 s cold. Nobody had measured what that buys.

    Reported with seconds = 0 and no score distributions, both honestly: there
    is no model to time and no rerank score to place a threshold on.
    """
    items = evalset["items"]
    top1 = top3 = top5 = 0
    rr_sum = 0.0
    not_retrieved = 0

    for it in items:
        gold = it["gold_index"]
        if gold is None:
            not_retrieved += 1          # a miss end-to-end, same as elsewhere
            continue
        pos = gold                      # its rank is where Qdrant already put it
        if pos == 0: top1 += 1
        if pos < 3:  top3 += 1
        if pos < 5:  top5 += 1
        rr_sum += 1.0 / (pos + 1)

    total = len(items)
    return {
        "model": BASELINE_LABEL,
        "total": total,
        "not_retrieved": not_retrieved,
        "top1": top1, "top3": top3, "top5": top5,
        "mrr": rr_sum / total if total else 0.0,
        "seconds": 0.0,
        "gold_scores": [],
        "other_scores": [],
    }


def _candidate_texts(it: dict[str, Any], mode: str) -> list[str]:
    """
    The text an arm actually sees. This is the variable under test.

    "body"       — body only. Non-trivial by construction, which is what makes
                   it right for comparing two rerankers: with the title present
                   the gold candidate contains the query verbatim.
    "production" — "title\nbody", exactly rag._hit_text_for_rerank. Right for
                   the different question of whether to rerank AT ALL, because
                   the vector arm it is compared against retrieved against
                   embeddings that include the title (index_meeting builds
                   "title\n\nbody"). Scoring bodies against a title-aware
                   retrieval compares two arms that saw different documents, and
                   the gap that produces is measurement, not quality.
    """
    if mode == "body":
        return it["candidates"]
    titles = it.get("candidate_titles")
    if not titles:
        raise SystemExit(
            "у наборі немає candidate_titles — перезберіть його через --build "
            "(режим 'production' потребує заголовків кандидатів)"
        )
    return [f"{t}\n{b}".strip() if b else t
            for t, b in zip(titles, it["candidates"])]


def score_model(
    model_name: str, evalset: dict[str, Any], text_mode: str = "body",
) -> dict[str, Any]:
    """Rerank every item with one model and report where the gold chunk lands."""
    os.environ["RERANK_MODEL"] = model_name
    # Imported here, after the env var is set: the module reads it at import.
    from church_assistant.shared import local_rerank

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
        ranked = local_rerank.rerank(
            it["query"], _candidate_texts(it, text_mode), top_k=None)
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
        "model": model_name if text_mode == "body" else f"{model_name} [production text]",
        "total": total,
        "not_retrieved": not_retrieved,
        "top1": top1, "top3": top3, "top5": top5,
        "mrr": rr_sum / total if total else 0.0,
        "seconds": time.perf_counter() - t0,
        "gold_scores": gold_scores,
        "other_scores": other_scores,
    }


_MARKER = "@@RESULT@@"


def _score_in_subprocess(
    model_name: str, evalset_path: str, text_mode: str = "body",
) -> Optional[dict[str, Any]]:
    """
    Score one model in a process of its own.

    Not tidiness — survival. Reranking 482 items and then loading a second model
    in the same interpreter died at item 450 with no traceback, only a leaked
    semaphore at shutdown: the signature of a process killed rather than one that
    raised. Whatever accumulates (MPS allocator growth is the likely candidate)
    is not worth diagnosing to run an offline script, and one model taking the
    comparison down with it is the part that actually hurts.

    A fresh interpreter also means the second model cannot inherit the first's
    memory state, so the timings are comparable rather than order-dependent.
    """
    # Streamed, not captured: a quarter of an hour of silence looks identical to
    # a hung process, and capture_output swallows the child's progress until it
    # exits. The result comes back on a marked line; everything else is echoed.
    proc = subprocess.Popen(
        [sys.executable, "-m", "church_assistant.scripts.eval_rerank",
         "-i", evalset_path, "--model", model_name, "--single",
         "--text-mode", text_mode],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
    )
    result: Optional[dict[str, Any]] = None
    tail: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip()
        if line.startswith(_MARKER):
            result = json.loads(line[len(_MARKER):])
            continue
        if line:
            print(f"    {line}", flush=True)
            tail = (tail + [line])[-3:]
    proc.wait()
    if result is None:
        for t in tail:
            print(f"      {t}", flush=True)
    return result


def _pct(n: int, total: int) -> str:
    return f"{n}/{total} ({n/total:.0%})" if total else "—"


def _quantiles(xs: list[float]) -> str:
    if not xs:
        return "—"
    s = sorted(xs)
    q = lambda p: s[min(int(p * len(s)), len(s) - 1)]
    return f"p10 {q(0.10):.3f} · медіана {q(0.50):.3f} · p90 {q(0.90):.3f}"


def _threshold_table(gold: list[float], other: list[float]) -> list[str]:
    """
    What each candidate threshold would actually do.

    RERANK_SCORE_GOOD/OK only colour hits green / yellow / dim, but a threshold
    that lets everything through is worse than none: it tells the reader every
    result is good. Both distributions start at 0.500 here, which is exactly what
    the current 0.50 does. Precision at a cutoff — of everything scoring at least
    t, how much of it is the right answer — is the number that should set it.
    """
    if not gold or not other:
        return []
    rows = ["      поріг   precision   покриття правильних"]
    for t in (0.50, 0.52, 0.55, 0.60, 0.65, 0.70, 0.75):
        g = sum(1 for x in gold if x >= t)
        o = sum(1 for x in other if x >= t)
        prec = g / (g + o) if (g + o) else 0.0
        rows.append(f"      {t:.2f}    {prec:6.1%}      {g/len(gold):6.1%}")
    return rows


def report(results: list[dict[str, Any]]) -> None:
    print()
    print("=" * 78)
    print("  Якість реранкінгу на корпусі")
    print("=" * 78)
    base = next((r for r in results if r["model"] == BASELINE_LABEL), None)
    for r in results:
        t = r["total"]
        print(f"\n  {r['model']}")
        if base is not None and r is not base:
            d = lambda k: (r[k] - base[k]) / max(t, 1) * 100
            print(f"    Δ проти вектора: top-1 {d('top1'):+.1f} в.п. · "
                  f"top-3 {d('top3'):+.1f} · top-5 {d('top5'):+.1f} · "
                  f"MRR {r['mrr'] - base['mrr']:+.3f}")
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
        for line in _threshold_table(r["gold_scores"], r["other_scores"]):
            print(line)
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

    if args.single:
        # One model, one process — the parent reads this back off stdout.
        print(_MARKER + json.dumps(
            score_model(args.model[0], evalset, args.text_mode)))
        return 0

    print(f"  набір: {len(evalset['items'])} запитів, пул {evalset['pool']}")
    # Always first, always present. It is free, and a rerank number without it
    # cannot answer the question anyone actually has.
    results = [score_vector_only(evalset)]
    for m in args.model:
        r = _score_in_subprocess(m, args.input, args.text_mode)
        if r is None:
            print(f"  ✗ {m}: процес не дожив до результату — пропускаю", flush=True)
            continue
        results.append(r)
    if len(results) == 1:
        print("  ✗ жодна модель не оцінилась — нижче лише базова лінія")
    report(results)
    if args.json_out:
        # Scores included: ~200 KB for a corpus this size, and re-deriving them
        # costs a quarter of an hour of reranking. Cheap insurance against
        # having to run the whole thing again to ask a different question.
        Path(args.json_out).write_text(
            json.dumps(results, ensure_ascii=False), encoding="utf-8")
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
    p.add_argument("--text-mode", choices=("body", "production"), default="body",
                   help="який текст бачить реранкер: лише тіло (порівняння "
                        "моделей) чи 'заголовок\\nтіло', як у продакшні "
                        "(порівняння з чистим вектором)")
    p.add_argument("--json-out", default=None)
    p.add_argument("--single", action="store_true",
                   help=argparse.SUPPRESS)   # internal: score one model, emit JSON
    args = p.parse_args()

    if not args.build and not args.model:
        args.model = [os.getenv("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")]
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
