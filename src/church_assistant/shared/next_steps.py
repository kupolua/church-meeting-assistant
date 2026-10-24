"""
«Наступні кроки» of polished.md, read back as hints for the protocol.

The analysis already writes, for every meeting, a section of actions grouped
by the person who took them on, each with the timecode where it was said:

    ## Наступні кроки

    ### Павло Кулаковський
    - Створити та вести календар чергувань пасторів (21:44)

    ### [Відповідальні особи]
    - Надати звіт про виконання попередніх рішень ради (17:43)

Since 24.10.2026 the protocol is assembled from the topics of the recording,
and these lines are the model's own first guess at «Постановили»: what, who,
and — buried in the wording — until when. This module turns them into hints
attached to the topic they were said in, so the chair confirms instead of
retyping. Nothing here calls a model: the VPS has none, and the guess was
already made on the processing node when the file was written.

A hint is a SUGGESTION. It never enters the protocol by itself — the chair
taps it in (api_minutes), and that tap is what makes it a decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional


_SECTION_RE = re.compile(r"^##\s+(?:Наступні кроки|Дії)\s*$", re.MULTILINE)
_HEADING_RE = re.compile(r"^##\s+")
_PERSON_RE = re.compile(r"^###\s+(.+?)\s*$")
_TS = r"\d{1,2}:\d{2}(?::\d{2})?"
_BULLET_RE = re.compile(
    rf"^\s*-\s+(?P<text>.*?)\s*(?:\((?P<tcs>\s*{_TS}\s*(?:[;,]\s*{_TS}\s*)*)\))?\s*$"
)
_TC_RE = re.compile(_TS)

# "до вересня", "до кінця цього тижня", "до 14 вересня", "до наступної зустрічі",
# "до середи" — a deadline as councils say it. Restricted to time words so that
# "прийти до пастора" is not mistaken for a term; the result is only a prefilled
# field the chair edits, so a miss costs a few taps and a false hit costs trust.
_DUE_RE = re.compile(
    r"\bдо\s+("
    r"(?:кінця\s+|середини\s+|початку\s+)?"
    r"(?:цього\s+|цієї\s+|наступного\s+|наступної\s+|найближчого\s+|найближчої\s+)?"
    r"(?:\d{1,2}(?:[./-]\d{1,2}(?:[./-]\d{2,4})?)?(?:-го)?(?:\s+(?:січня|лютого|березня|квітня|травня|червня|липня|серпня|вересня|жовтня|листопада|грудня))?"
    r"|січня|лютого|березня|квітня|травня|червня|липня|серпня|вересня|жовтня|листопада|грудня"
    r"|понеділка|вівторка|середи|четверга|п[ʼ']ятниці|суботи|неділі"
    r"|тижня|місяця|року|зустрічі|ради|зібрання|служіння|свят|різдва|великодня|пасхи)"
    r"(?:\s+\d{4})?"
    r")",
    re.IGNORECASE,
)


@dataclass
class Hint:
    text: str
    responsible: str
    due: str
    tc: Optional[str]            # the first timecode of the line, if any
    topic_order: Optional[int] = None

    def as_json(self) -> dict[str, Any]:
        return {"text": self.text, "responsible": self.responsible, "due": self.due, "tc": self.tc}


def _tc_seconds(tc: str) -> int:
    parts = [int(p) for p in tc.split(":")]
    return parts[0] * 3600 + parts[1] * 60 + parts[2] if len(parts) == 3 else parts[0] * 60 + parts[1]


def due_of(text: str) -> str:
    """The deadline phrase inside an action line, or ''."""
    m = _DUE_RE.search(text)
    return f"до {m.group(1).strip()}" if m else ""


def _responsible_of(heading: str) -> str:
    """A heading is a name, a group («Пастори (всі учасники)»), or a placeholder
    the analysis could not resolve («[Відповідальні особи]»). Placeholders
    become '' — a bracketed role in the responsible field would read as a
    decision the council made."""
    h = heading.strip()
    if h.startswith("[") and h.endswith("]"):
        return ""
    return h


def parse(text: str) -> list[Hint]:
    """Every action line of the «Наступні кроки» section, in file order."""
    m = _SECTION_RE.search(text)
    if not m:
        return []
    body = text[m.end():]
    hints: list[Hint] = []
    responsible = ""
    for raw in body.splitlines():
        if _HEADING_RE.match(raw):
            break                                     # next ## section: done
        pm = _PERSON_RE.match(raw)
        if pm:
            responsible = _responsible_of(pm.group(1))
            continue
        bm = _BULLET_RE.match(raw)
        if not bm or not bm.group("text").strip():
            continue
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", bm.group("text")).strip().rstrip(".;,")
        tcs = _TC_RE.findall(bm.group("tcs") or "")
        hints.append(Hint(text=line, responsible=responsible, due=due_of(line), tc=tcs[0] if tcs else None))
    return hints


def attach(hints: list[Hint], topics: list[Any]) -> dict[int, list[Hint]]:
    """Hints by topic order. A hint lands on the topic whose body quotes the
    same timecode; failing that, on the topic that was being discussed at that
    second (the last topic to start before it). A hint with no timecode has no
    place and is dropped — there is nothing to point the chair at."""
    starts: list[tuple[int, int]] = []               # (seconds, order)
    bodies: list[tuple[int, str]] = []
    for t in topics:
        found = _TC_RE.findall(t.body or "")
        bodies.append((t.order, t.body or ""))
        if found:
            starts.append((min(_tc_seconds(x) for x in found), t.order))
    starts.sort()
    out: dict[int, list[Hint]] = {}
    for h in hints:
        if not h.tc:
            continue
        order: Optional[int] = None
        for o, body in bodies:
            if f"({h.tc}" in body or f", {h.tc}" in body or f"; {h.tc}" in body:
                order = o
                break
        if order is None and starts:
            s = _tc_seconds(h.tc)
            order = starts[0][1]
            for start, o in starts:
                if start <= s:
                    order = o
                else:
                    break
        if order is None:
            continue
        h.topic_order = order
        out.setdefault(order, []).append(h)
    return out


def hints_by_topic(polished_text: str, topics: list[Any]) -> dict[int, list[Hint]]:
    return attach(parse(polished_text), topics)
