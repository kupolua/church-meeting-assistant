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
# Used only for the second attempt at drafting a decision — see draft_decisions.
_RETRY_TEMPERATURE = 0.3

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


_DECIDE_SYSTEM = (
    "Ти секретар церковної ради. Тобі дають ПИТАННЯ порядку денного і те, що "
    "було сказано по ньому на зустрічі.\n"
    "Сформулюй проект двох розділів протоколу.\n"
    "ПРАВИЛА:\n"
    "1. «Вирішили» — одне-два речення про те, до чого рада дійшла. Якщо рада "
    "нічого не вирішила, а лише обговорила — так і напиши, не вигадуй рішення.\n"
    "2. «Постановили» — конкретні дії. Кожна: що зробити, хто відповідальний, "
    "до якого терміну. Якщо відповідального або терміну не називали — лиши "
    "порожнім, НЕ вигадуй імені й НЕ вигадуй дати.\n"
    "3. Бери лише те, що є в тексті. Нічого не додавай від себе.\n"
    "4. Відповідай ЛИШЕ у форматі JSON, без пояснень:\n"
    '{"vyrishyly": "текст", "postanovy": '
    '[{"text": "дія", "responsible": "імʼя або порожньо", '
    '"due": "термін або порожньо"}]}'
)


# A question that gathered ten topics carries several thousand words, and the
# answer wanted from it is two sentences. Found by watching the financial item
# come back as prose instead of JSON while the shorter ones parsed cleanly.
_HEARD_LIMIT = 6000


def _clip(text: str, limit: int = _HEARD_LIMIT) -> str:
    """Trim on a paragraph boundary, so a topic is not cut mid-sentence."""
    if len(text) <= limit:
        return text
    cut = text.rfind("\n\n", 0, limit)
    return text[: cut if cut > limit // 2 else limit].rstrip() + "\n\n[…]"


# ⚠️ WHY A DEGENERATION FILTER EXISTS AT ALL.
#
# Retrying a failed draft with a little sampling turned a VISIBLE failure into
# an invisible one. On 2026-08-03's financial question, greedy decoding fell
# into a loop and produced broken JSON — which the parser rejected, leaving the
# field blank for the chair to write. The retry then produced perfectly valid
# JSON containing: a ruling with Arabic characters spliced into a Ukrainian
# sentence, and "Чед може поспілкуватися з Україною Ukraines Ukraines Ukraines".
# Valid JSON, plausible shape, a name in the responsible field — and it would
# have gone into a document a council signs, past a chair skimming a draft that
# looks finished.
#
# A blank field is honest. Nonsense wearing the shape of a decision is not, and
# the whole reason this draft exists is to save reading, which is exactly the
# attention that would have caught it.

_CYRILLIC_LATIN = re.compile(r"[а-щьюяїієґА-ЩЬЮЯЇІЄҐa-zA-Z]")
_FOREIGN_SCRIPT = re.compile(
    r"[\u0600-\u06FF\u0400-\u04FF]*[\u0600-\u06FF\u0590-\u05FF"
    r"\u4e00-\u9fff\u0e00-\u0e7f]"
)


def _looks_degenerate(text: str) -> bool:
    """
    True when the model has clearly come off the rails.

    Three symptoms, all observed rather than imagined: a word repeated three
    times in a row, a script that has no business in a Ukrainian protocol, and
    a sentence made almost entirely of the same handful of words. Each is cheap
    to check and none of them fires on ordinary minutes — "Роман Вечерківський
    Роман Вечерківський Роман Вечерківський" is not a thing a secretary writes.
    """
    if not text.strip():
        return False                       # empty is handled elsewhere
    if _FOREIGN_SCRIPT.search(text):
        return True
    words = [w for w in re.split(r"\s+", text.strip()) if w]
    for i in range(len(words) - 2):
        if words[i] == words[i + 1] == words[i + 2]:
            return True
    if len(words) >= 12 and len(set(words)) / len(words) < 0.4:
        return True
    return False


def _parse_decision(raw: str) -> tuple[str, list[dict[str, str]]]:
    """
    Read back "Вирішили" and the rulings, keeping only what is a string.

    The rulings end up in a signed document with a person's name against them,
    so a hallucinated responsible is worse than an empty field: an empty field
    is visibly unfinished, a wrong name looks like a decision the council made.
    Anything that is not a string is dropped rather than coerced.
    """
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError("Gemma не повернула JSON")
    data = json.loads(m.group(0))

    resolved = data.get("vyrishyly")
    resolved = resolved.strip() if isinstance(resolved, str) else ""
    if _looks_degenerate(resolved):
        resolved = ""

    rulings: list[dict[str, str]] = []
    for r in data.get("postanovy") or []:
        if not isinstance(r, dict):
            continue
        text = r.get("text")
        if not isinstance(text, str) or not text.strip():
            continue                      # a ruling with no action is not one
        if _looks_degenerate(text):
            continue                      # nor is one the model hallucinated
        rulings.append({
            "text": text.strip(),
            "responsible": (r.get("responsible") or "").strip()
                           if isinstance(r.get("responsible"), str) else "",
            "due": (r.get("due") or "").strip()
                   if isinstance(r.get("due"), str) else "",
        })
    return resolved, rulings


async def draft_decisions(
    pool: Any, tenant_id: int, meeting_date: str,
) -> Optional[dict[str, int]]:
    """
    Draft "Вирішили" and "Постановили" for items that have "Слухали" and neither.

    One call per question rather than one for the meeting: the model is being
    asked what a council concluded, and giving it six questions at once invites
    it to carry a decision from one into another. Slower — a few dozen seconds
    each — inside a pipeline that already takes three hours.

    Items the chair has already written are left alone, and so is anything with
    an empty "Слухали": there is nothing to conclude from.
    """
    protocol = await protocols_repo.get_by_date(pool, tenant_id, meeting_date)
    if protocol is None or protocol["status"] == "approved":
        return None

    items = await protocols_repo.list_items(pool, tenant_id, int(protocol["id"]))
    todo = [
        i for i in items
        if i["heard"].strip() and not i["resolved"].strip() and not i["rulings"]
    ]
    if not todo:
        return None

    drafted = rulings_made = 0
    for item in todo:
        payload = {
            "model": rag.GEMMA_MODEL,
            "messages": [
                {"role": "system", "content": _DECIDE_SYSTEM},
                {"role": "user", "content":
                    f"ПИТАННЯ: {item['question']}\n\n"
                    f"СКАЗАНЕ НА ЗУСТРІЧІ:\n{_clip(item['heard'])}\n\nJSON:"},
            ],
            "stream": False,
            "format": "json",     # see draft_heard — same reason
            "options": {"temperature": _TEMPERATURE, "num_ctx": 16384},
        }
        if rag.GEMMA_KEEP_ALIVE:
            payload["keep_alive"] = rag.GEMMA_KEEP_ALIVE

        # ⚠️ ONE RETRY, AND IT CHANGES THE TEMPERATURE ON PURPOSE. Observed on
        # 2026-08-03's financial question: Gemma wrote sensible content and
        # then degenerated mid-object — two "text" keys in one ruling, then
        # noise — on an input of barely two thousand characters, so neither
        # length nor `format: json` was the problem. Greedy decoding fell into
        # a loop, and repeating the same call at temperature 0 would fall into
        # the same one. A little sampling is the cheapest way out; if it fails
        # again the question is left blank, which is the honest outcome and the
        # one the chair can fix in a sentence.
        resolved, rulings = "", []
        for attempt, temperature in enumerate((_TEMPERATURE, _RETRY_TEMPERATURE)):
            payload["options"]["temperature"] = temperature
            try:
                async with httpx.AsyncClient(timeout=rag.OLLAMA_TIMEOUT) as client:
                    r = await client.post(f"{rag.OLLAMA_URL}/api/chat", json=payload)
                    r.raise_for_status()
                    resolved, rulings = _parse_decision(
                        r.json().get("message", {}).get("content", ""))
                break
            except (httpx.HTTPError, ValueError, json.JSONDecodeError) as e:
                # One bad question must not cost the others their drafts.
                await _log.warn(
                    "protocol.decision_failed",
                    message=f"{meeting_date} «{item['question'][:40]}» "
                            f"(спроба {attempt + 1}, t={temperature}): "
                            f"{type(e).__name__}: {e}",
                    tenant_id=tenant_id,
                )
        if not resolved and not rulings:
            continue

        if resolved:
            await protocols_repo.update_item(
                pool, tenant_id, int(item["id"]), resolved=resolved)
            drafted += 1
        for r in rulings:
            await protocols_repo.add_ruling(
                pool, tenant_id, int(item["id"]),
                text=r["text"], responsible=r["responsible"], due=r["due"])
            rulings_made += 1

    await _log.info(
        "protocol.decisions_drafted",
        message=f"{meeting_date}: «Вирішили» для {drafted} питань, "
                f"{rulings_made} постанов",
        tenant_id=tenant_id,
    )
    return {"resolved": drafted, "rulings": rulings_made}


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
        # Ollama constrains generation to valid JSON. The regex below still
        # runs: this flag is honoured by the server, and a server is a thing
        # that gets upgraded, downgraded and swapped. Belt and braces cost one
        # regex on a path that already waited two minutes for the answer.
        "format": "json",
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
