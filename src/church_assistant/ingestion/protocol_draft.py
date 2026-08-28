"""
Fill "Слухали" — match the meeting's topics to the agenda a person wrote.

WHERE THIS RUNS AND WHY. On the M1, as the last step of ingestion, because that
is where Gemma is. The web tier lives on the VPS and has no models at all (the
plane split, 24.08 — its unit even excludes torch), so a route could not call
her however long it was willing to wait. Running here also means the draft is
already sitting there when the chair opens the protocol, instead of behind a
button they press and then watch.

WHAT IT IS NOT ALLOWED TO DO:
  * invent agenda items — the agenda is written by a person BEFORE the meeting,
    and that authorship is the entire reason the protocol comes before the
    analytics (clustering topics by similarity was tried on 28.08 and chained
    one cluster across 13 consecutive meetings);
  * touch an item whose "Слухали" the chair has already written;
  * touch an approved protocol — the database would refuse anyway, and asking is
    cheaper than being refused;
  * fail the ingestion. A meeting that transcribed and analysed correctly is
    done; a missing draft costs the chair some typing, and losing three hours of
    pipeline over it would be the worse trade by far.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

import httpx

from church_assistant.db import protocols_repo
from church_assistant.shared import meetings_index, rag
from church_assistant.shared.logger import Logger


_log = Logger(process="worker")

# Deliberately low: this is a sorting task with a closed set of answers, not a
# writing task. Anything warmer starts inventing topic numbers.
_TEMPERATURE = 0.0

_SYSTEM = (
    "Ти секретар церковної ради. Тобі дають ПОРЯДОК ДЕННИЙ (питання, які "
    "внесла людина до зустрічі) і ТЕМИ, які система виділила із запису тієї "
    "самої зустрічі.\n"
    "Твоє завдання — розподілити теми між питаннями порядку денного.\n"
    "ПРАВИЛА:\n"
    "1. Не вигадуй нових питань. Використовуй лише номери з порядку денного.\n"
    "2. Тема, що не стосується жодного питання, йде в \"inshe\". Це нормально "
    "і трапляється часто: рада обговорює й те, чого не планувала.\n"
    "3. Одна тема належить рівно одному питанню.\n"
    "4. Відповідай ЛИШЕ у форматі JSON, без пояснень:\n"
    "{\"1\": [номери тем], \"2\": [...], \"inshe\": [...]}"
)


def _build_prompt(items: list[dict[str, Any]], topics: list[Any]) -> str:
    agenda = "\n".join(f"{i['position']}. {i['question']}" for i in items)
    lines = []
    for n, t in enumerate(topics, 1):
        summary = (t.body or "").strip().replace("\n", " ")
        lines.append(f"{n}. {t.title}" + (f" — {summary[:200]}" if summary else ""))
    return (
        f"ПОРЯДОК ДЕННИЙ:\n{agenda}\n\n"
        f"ТЕМИ ІЗ ЗАПИСУ:\n" + "\n".join(lines) + "\n\n"
        f"Розподіл у JSON:"
    )


def _parse(raw: str, n_items: int, n_topics: int) -> dict[int, list[int]]:
    """
    Read the assignment back, believing nothing.

    Gemma answers with JSON most of the time and with JSON wrapped in prose the
    rest of it, so the object is located rather than parsed from position zero.
    Every number is then checked against the ranges it must fall in: a topic
    index it invented would otherwise silently attach some other meeting's
    wording to a question, and that is precisely the kind of error nobody
    proofreads out of a document that looks finished.
    """
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError("Gemma не повернула JSON")
    data = json.loads(m.group(0))

    out: dict[int, list[int]] = {}
    seen: set[int] = set()
    for key, values in data.items():
        if not str(key).isdigit():
            continue                      # "inshe" and anything else: dropped
        pos = int(key)
        if not 1 <= pos <= n_items:
            continue
        picked = []
        for v in values if isinstance(values, list) else []:
            try:
                idx = int(v)
            except (TypeError, ValueError):
                continue
            # Out of range, or already claimed by an earlier question: rule 3
            # says one topic belongs to one item, and the first claim wins so
            # the result cannot depend on dict ordering.
            if 1 <= idx <= n_topics and idx not in seen:
                seen.add(idx)
                picked.append(idx)
        if picked:
            out[pos] = picked
    return out


def _heard_text(topics: list[Any], indices: list[int]) -> str:
    """The 'Слухали' body: the topics themselves, in the order they were said."""
    parts = []
    for i in sorted(indices):
        t = topics[i - 1]
        body = (t.body or "").strip()
        parts.append(f"{t.title}" + (f"\n{body}" if body else ""))
    return "\n\n".join(parts)


async def draft_heard(
    pool: Any, tenant_id: int, meeting_date: str, meeting_dir: Any,
) -> Optional[dict[str, int]]:
    """
    Draft "Слухали" for every agenda item that has none. Returns a small report,
    or None when there was nothing to do.

    Every early return is a legitimate state, not a failure: no protocol means
    nobody opened this meeting, no agenda means nobody wrote one yet, and an
    approved protocol is finished.
    """
    protocol = await protocols_repo.get_by_date(pool, tenant_id, meeting_date)
    if protocol is None:
        return None
    if protocol["status"] == "approved":
        return None

    items = await protocols_repo.list_items(pool, tenant_id, int(protocol["id"]))
    blank = [i for i in items if not i["heard"].strip()]
    if not items or not blank:
        return None

    detail = meetings_index.load_detail(meeting_dir.parent, meeting_date)
    topics = list(detail.topics) if detail else []
    if not topics:
        return None

    payload = {
        "model": rag.GEMMA_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": _build_prompt(items, topics)},
        ],
        "stream": False,
        "options": {"temperature": _TEMPERATURE, "num_ctx": 16384},
    }
    if rag.GEMMA_KEEP_ALIVE:
        payload["keep_alive"] = rag.GEMMA_KEEP_ALIVE

    async with httpx.AsyncClient(timeout=rag.OLLAMA_TIMEOUT) as client:
        r = await client.post(f"{rag.OLLAMA_URL}/api/chat", json=payload)
        r.raise_for_status()
        raw = r.json().get("message", {}).get("content", "")

    assignment = _parse(raw, len(items), len(topics))

    by_pos = {int(i["position"]): i for i in blank}
    filled = 0
    for pos, indices in assignment.items():
        item = by_pos.get(pos)
        if item is None:
            continue                      # the chair already wrote this one
        await protocols_repo.update_item(
            pool, tenant_id, int(item["id"]),
            heard=_heard_text(topics, indices),
        )
        filled += 1

    assigned = sum(len(v) for v in assignment.values())
    await _log.info(
        "protocol.drafted",
        message=f"{meeting_date}: «Слухали» для {filled} питань "
                f"({assigned} із {len(topics)} тем розподілено)",
        tenant_id=tenant_id,
    )
    return {"items": filled, "topics": len(topics), "assigned": assigned}
