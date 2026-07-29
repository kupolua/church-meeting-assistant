"""
Meetings index — scans a meetings directory and parses polished.md files.

Provides read-only access to meeting metadata (for sidebar) and structured
topics (for meeting detail page + keyword search).

Design:
    - No DB dependency (reads flat files).
    - Small dataset (~14 meetings, ~500 topics total) — no need for anything
      fancier.
    - Every entry point takes the meetings directory EXPLICITLY. It used to be a
      module constant, but under multi-tenancy each church has its own subtree
      (shared/tenant_paths.py) and an implicit default would silently serve one
      church's protocols to another. Callers resolve it from the request's
      tenant: `tenant_paths.paths_for(slug).meetings`.
"""

from __future__ import annotations

import bisect
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Optional


# ─────────────────────────────────────────────────────────────
# Data types
# ─────────────────────────────────────────────────────────────

@dataclass
class Topic:
    """One ### section from polished.md."""
    title: str
    body: str = ""
    order: int = 0                          # position in file (0-indexed)

    def matches_keyword(self, keyword: str) -> bool:
        """Case-insensitive substring match in title or body."""
        kw = keyword.lower()
        return kw in self.title.lower() or kw in self.body.lower()


@dataclass
class TranscriptTurn:
    """One speaker turn from annotated.md — the meeting стенограма."""
    timestamp: str = ""                     # e.g. '00:05' ('' if the line had none)
    speaker: str = ""                       # e.g. 'Павло Кулаковський' ('' if unknown)
    text: str = ""
    speaker_label: str = ""                 # diarization cluster, e.g. 'SPEAKER_06'
                                            # (derived from RTTM by timestamp; '' if unknown)


@dataclass
class MeetingSummary:
    """Sidebar-friendly metadata (parsed lazily)."""
    date: str                               # 'YYYY-MM-DD'
    folder: Path
    attendees: list[str] = field(default_factory=list)
    topic_count: int = 0
    action_item_count: int = 0
    indexed: bool = False                   # True if index_state.json exists

    @property
    def date_display(self) -> str:
        """Human-friendly date, e.g. '2026-06-22'."""
        return self.date


@dataclass
class MeetingDetail:
    """Full meeting content — topics, attendees, path."""
    date: str
    folder: Path
    attendees: list[str] = field(default_factory=list)
    topics: list[Topic] = field(default_factory=list)
    transcript: list[TranscriptTurn] = field(default_factory=list)
    polished_md_path: Optional[Path] = None
    indexed: bool = False


# ─────────────────────────────────────────────────────────────
# Directory scan
# ─────────────────────────────────────────────────────────────

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def list_meeting_folders(meetings_dir: Path) -> list[Path]:
    """Return one tenant's meeting folders sorted by date (newest first)."""
    if not meetings_dir.exists():
        return []
    folders = [
        p for p in meetings_dir.iterdir()
        if p.is_dir() and _DATE_RE.match(p.name)
    ]
    return sorted(folders, key=lambda p: p.name, reverse=True)


# ─────────────────────────────────────────────────────────────
# Parsers
# ─────────────────────────────────────────────────────────────

def _parse_attendees_from_polished(text: str) -> list[str]:
    """
    Extract attendees list from polished.md.

    Expected format (varies slightly across meetings):

        ## Присутні

        - Роман Вечерківський
        - Павло Кулаковський
        ...

    Returns [] if not found.
    """
    # Look for '## Присутні' section
    m = re.search(
        r"^##\s+Присутні\s*$(.+?)^##\s+",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if not m:
        # Fallback: try up to the next '### ' topic
        m = re.search(
            r"^##\s+Присутні\s*$(.+?)^###\s+",
            text,
            re.MULTILINE | re.DOTALL,
        )
    if not m:
        return []
    section = m.group(1)
    attendees: list[str] = []
    for line in section.splitlines():
        line = line.strip()
        if line.startswith("- "):
            name = line[2:].strip()
            if name:
                attendees.append(name)
    return attendees


def _parse_topics_from_polished(text: str) -> list[Topic]:
    """
    Parse ### topic sections from polished.md.

    Body of each topic = everything between its ### heading and the next
    ### or ## heading (whichever comes first).

    Ignores HTML comments (e.g. '<!-- topic from chunks: ... -->').
    """
    lines = text.split("\n")
    topics: list[Topic] = []
    current_title: Optional[str] = None
    current_body_lines: list[str] = []
    order = 0

    def flush() -> None:
        nonlocal current_title, current_body_lines, order
        if current_title is not None:
            body = "\n".join(current_body_lines).strip()
            topics.append(Topic(
                title=current_title,
                body=body,
                order=order,
            ))
            order += 1

    for line in lines:
        # Stop the current topic at next ### or ## (not #### etc — full stop)
        if line.startswith("### "):
            flush()
            current_title = line[4:].strip()
            current_body_lines = []
        elif line.startswith("## ") and current_title is not None:
            # Section boundary (e.g. '## Дії' after topics)
            flush()
            current_title = None
            current_body_lines = []
        elif current_title is not None:
            # Skip HTML comments (topic-from-chunks markers)
            stripped = line.strip()
            if stripped.startswith("<!--") and stripped.endswith("-->"):
                continue
            current_body_lines.append(line)

    flush()
    return topics


def _count_action_items(text: str) -> int:
    """
    Count action items in polished.md.

    Format: '## Дії' followed by '- ' bullets.
    Returns 0 if section not found.
    """
    m = re.search(
        r"^##\s+Дії\s*$(.+?)(?=^##\s+|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if not m:
        return 0
    section = m.group(1)
    return sum(
        1 for line in section.splitlines()
        if line.strip().startswith("- ")
    )


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────

def load_summary(folder: Path) -> Optional[MeetingSummary]:
    """
    Load MeetingSummary for a single meeting folder.

    Returns None if the folder has no polished.md.
    """
    if not folder.is_dir():
        return None
    date_str = folder.name
    polished = folder / "polished.md"
    index_state = folder / "index_state.json"

    if not polished.exists():
        return None

    text = polished.read_text(encoding="utf-8")
    attendees = _parse_attendees_from_polished(text)
    topics = _parse_topics_from_polished(text)
    action_items = _count_action_items(text)

    return MeetingSummary(
        date=date_str,
        folder=folder,
        attendees=attendees,
        topic_count=len(topics),
        action_item_count=action_items,
        indexed=index_state.exists(),
    )


def list_all_summaries(meetings_dir: Path) -> list[MeetingSummary]:
    """
    Return summaries for one tenant's meetings, newest first.

    Skips folders without polished.md.
    """
    result: list[MeetingSummary] = []
    for folder in list_meeting_folders(meetings_dir):
        summary = load_summary(folder)
        if summary:
            result.append(summary)
    return result


# Transcript line: "[00:05 Павло Кулаковський]: текст".
# Speaker may contain spaces and even nested brackets — the pipeline emits
# placeholder speakers like "[немає мовця]" / "[нерозбірливо]", giving
# "[00:30 [немає мовця]]: текст". A lazy speaker group stops at the "]:" that
# closes the header, so these placeholder lines parse (and their timestamps
# become clickable) too.
_TURN_RE = re.compile(r"^\[(?P<ts>[^\]\s]+)\s+(?P<speaker>.+?)\]:\s*(?P<text>.*)$")


def _parse_transcript_from_annotated(text: str) -> list[TranscriptTurn]:
    """
    Parse annotated.md into speaker turns (the стенограма).

    Recognizes '[timestamp Speaker]: text' lines; any other non-empty,
    non-heading line is kept as a turn with no timestamp/speaker so nothing
    is silently dropped.
    """
    turns: list[TranscriptTurn] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        m = _TURN_RE.match(line)
        if m:
            turns.append(TranscriptTurn(
                timestamp=m.group("ts"),
                speaker=m.group("speaker").strip(),
                text=m.group("text").strip(),
            ))
        else:
            turns.append(TranscriptTurn(text=line))
    return turns


def _rttm_segments(rttm_path: Path) -> list[tuple[float, float, str]]:
    """Parse diarization.rttm → [(start, end, SPEAKER_XX)] sorted by start."""
    segs: list[tuple[float, float, str]] = []
    try:
        lines = rttm_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return segs
    for line in lines:
        p = line.split()
        if len(p) < 8 or p[0] != "SPEAKER":
            continue
        try:
            start = float(p[3]); dur = float(p[4])
        except ValueError:
            continue
        segs.append((start, start + dur, p[7]))
    segs.sort(key=lambda s: s[0])
    return segs


def _ts_to_seconds(ts: str) -> Optional[float]:
    """'4:12' → 252.0 ; '1:02:03' → 3723.0 ; invalid → None."""
    parts = ts.split(":")
    try:
        nums = [int(x) for x in parts]
    except ValueError:
        return None
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return None


def _label_at(segs: list[tuple[float, float, str]], starts: list[float], t: float) -> str:
    """SPEAKER_XX active at time t (covering segment, else nearest). '' if none."""
    if not segs:
        return ""
    i = bisect.bisect_right(starts, t) - 1
    if 0 <= i < len(segs) and segs[i][1] >= t:        # t falls inside segment i
        return segs[i][2]
    cands = []
    if 0 <= i < len(segs):
        cands.append(segs[i])
    if i + 1 < len(segs):
        cands.append(segs[i + 1])
    if not cands:
        return ""
    best = min(cands, key=lambda s: min(abs(s[0] - t), abs(s[1] - t)))
    return best[2]


def _strip_review(name: str) -> str:
    return name[: -len(" [REVIEW]")] if name.endswith(" [REVIEW]") else name


def _attach_speaker_labels(
    transcript: list[TranscriptTurn],
    rttm_path: Path,
    speaker_map: dict[str, str],
) -> None:
    """
    Fill each turn's speaker_label (diarization cluster) in place.

    The label MUST be consistent with the displayed name so that "reassign this
    speaker" targets the right cluster. Strategy per turn:
      1. Candidates = clusters whose speakers.json name == the displayed name.
         - exactly one → use it (the common case).
         - several (same name split across clusters) → pick the candidate whose
           RTTM segment is active at the turn's timestamp.
      2. No name match (placeholders, unmapped) → fall back to the RTTM cluster
         active at the timestamp.
    """
    segs = _rttm_segments(rttm_path)
    if not segs:
        return
    starts = [s[0] for s in segs]

    # reverse map: display-name → [labels]
    name_to_labels: dict[str, list[str]] = {}
    for label, raw in speaker_map.items():
        name_to_labels.setdefault(_strip_review(str(raw)), []).append(label)

    for turn in transcript:
        if not turn.timestamp:
            continue
        sec = _ts_to_seconds(turn.timestamp)
        if sec is None:
            continue
        candidates = name_to_labels.get(turn.speaker, [])
        if len(candidates) == 1:
            turn.speaker_label = candidates[0]
        elif len(candidates) > 1:
            sub = [s for s in segs if s[2] in candidates]
            turn.speaker_label = _label_at(sub, [s[0] for s in sub], sec) or candidates[0]
        else:
            turn.speaker_label = _label_at(segs, starts, sec)


def load_detail(meetings_dir: Path, meeting_date: str) -> Optional[MeetingDetail]:
    """
    Load full detail for a meeting by its date ('YYYY-MM-DD') within one tenant.

    Returns None if not found or no polished.md. The стенограма (annotated.md)
    is loaded when present; absent, transcript is an empty list. Each turn's
    speaker_label (diarization cluster) is derived from diarization.rttm so the
    UI can offer per-cluster speaker reassignment.

    The date is regex-checked before it is joined onto the path, so a crafted
    'date' can't walk out of the tenant's folder.
    """
    if not _DATE_RE.match(meeting_date):
        return None
    folder = meetings_dir / meeting_date
    if not folder.is_dir():
        return None
    polished = folder / "polished.md"
    if not polished.exists():
        return None

    text = polished.read_text(encoding="utf-8")

    annotated = folder / "annotated.md"
    transcript: list[TranscriptTurn] = []
    if annotated.exists():
        transcript = _parse_transcript_from_annotated(
            annotated.read_text(encoding="utf-8")
        )
        rttm = folder / "diarization.rttm"
        speakers_json = folder / "speakers.json"
        if rttm.exists() and speakers_json.exists():
            try:
                sp = json.loads(speakers_json.read_text(encoding="utf-8"))
                speaker_map = {k: v for k, v in sp.items() if not k.startswith("_")}
            except (OSError, json.JSONDecodeError):
                speaker_map = {}
            _attach_speaker_labels(transcript, rttm, speaker_map)

    return MeetingDetail(
        date=meeting_date,
        folder=folder,
        attendees=_parse_attendees_from_polished(text),
        topics=_parse_topics_from_polished(text),
        transcript=transcript,
        polished_md_path=polished,
        indexed=(folder / "index_state.json").exists(),
    )


# ─────────────────────────────────────────────────────────────
# Keyword search (across all meetings)
# ─────────────────────────────────────────────────────────────

@dataclass
class SearchMatch:
    """One keyword-search result."""
    meeting_date: str
    topic_title: str
    topic_order: int
    snippet: str = ""


def search_topics(
    meetings_dir: Path, keyword: str, limit: int = 50,
) -> list[SearchMatch]:
    """
    Case-insensitive substring search across ONE tenant's meeting topics.

    Returns matches ordered by meeting date DESC (newest first).
    """
    keyword = keyword.strip()
    if not keyword:
        return []

    results: list[SearchMatch] = []
    for summary in list_all_summaries(meetings_dir):
        detail = load_detail(meetings_dir, summary.date)
        if not detail:
            continue
        for topic in detail.topics:
            if topic.matches_keyword(keyword):
                # Snippet: the first line of body that contains keyword
                snippet = ""
                for line in topic.body.splitlines():
                    if keyword.lower() in line.lower():
                        snippet = line.strip()[:200]
                        break
                if not snippet:
                    snippet = topic.title
                results.append(SearchMatch(
                    meeting_date=summary.date,
                    topic_title=topic.title,
                    topic_order=topic.order,
                    snippet=snippet,
                ))
                if len(results) >= limit:
                    return results
    return results


# ─────────────────────────────────────────────────────────────
# CLI smoke test
# ─────────────────────────────────────────────────────────────

def _smoke_test() -> None:
    """Run against the legacy tenant's folder (the existing corpus)."""
    from church_assistant.shared import tenant_paths

    meetings_dir = tenant_paths.paths_for(
        tenant_paths.legacy_slug() or "default"
    ).meetings

    print("=" * 70)
    print("  meetings_index — smoke test")
    print("=" * 70)
    print(f"  meetings_dir: {meetings_dir}")
    print()

    # 1. List all summaries
    print("1. list_all_summaries()")
    print("-" * 70)
    summaries = list_all_summaries(meetings_dir)
    print(f"  Found {len(summaries)} meetings")
    for s in summaries[:5]:
        print(f"    {s.date}  ({len(s.attendees)} attendees, "
              f"{s.topic_count} topics, {s.action_item_count} action items, "
              f"indexed={s.indexed})")
    if len(summaries) > 5:
        print(f"    ... and {len(summaries) - 5} more")
    print()

    assert len(summaries) > 0, "Expected at least one meeting"

    # 2. Load detail for latest
    latest = summaries[0]
    print(f"2. load_detail({latest.date})")
    print("-" * 70)
    detail = load_detail(meetings_dir, latest.date)
    assert detail is not None
    print(f"  Attendees ({len(detail.attendees)}):")
    for a in detail.attendees[:10]:
        print(f"    - {a}")
    print(f"  Topics ({len(detail.topics)}):")
    for t in detail.topics[:5]:
        body_preview = t.body[:80].replace("\n", " ")
        print(f"    #{t.order}: {t.title!r}")
        if body_preview:
            print(f"      body: {body_preview}...")
    if len(detail.topics) > 5:
        print(f"    ... and {len(detail.topics) - 5} more")
    print()

    # 3. Keyword search
    keyword = "пастор"
    print(f"3. search_topics({keyword!r})")
    print("-" * 70)
    matches = search_topics(meetings_dir, keyword, limit=10)
    print(f"  Found {len(matches)} matches (limit=10)")
    for m in matches[:5]:
        print(f"    [{m.meeting_date}] {m.topic_title!r}")
        if m.snippet != m.topic_title:
            print(f"      → {m.snippet[:100]}")
    print()

    print("=" * 70)
    print("  ✓ MEETINGS_INDEX SMOKE TESTS PASSED")
    print("=" * 70)


if __name__ == "__main__":
    _smoke_test()
