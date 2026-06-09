"""Polish the chunked protocol output (v3 — with attendee detection from RTTM).

Adds to v2:
- Detects attendees from RTTM: anyone speaking >= MIN_SPEECH_SECONDS
- Excludes SPEAKER_05 (known artifact)
- Lists attendees in order of first appearance in the meeting
- Maps SPEAKER_XX → canonical name via speakers.json + aliases

v2 features (kept):
- Loads data/name_aliases.json: maps name variants to canonical forms
- Splits composite headings ('Павло та Євген') into separate entries per person
- Keeps group headings ('Команда декорування', 'Проповідники') as-is

Does NOT call Gemma. Pure post-processing.

Usage:
    uv run python -m church_assistant.polish_protocol --date 23/02/2026
    uv run python -m church_assistant.polish_protocol --audio-file data/test_baseline.m4a
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path


DEFAULT_CHUNKS_DIR = Path("data/chunks")
DEFAULT_OUTPUT = Path("data/test_baseline_polished.md")
DEFAULT_ALIASES = Path("data/name_aliases.json")
DEFAULT_RTTM = Path("data/test_baseline.rttm")
DEFAULT_SPEAKERS_MAP = Path("data/speakers.json")

# Attendee detection params
MIN_SPEECH_SECONDS = 60.0   # 1 minute — filters out crosstalk artifacts
EXCLUDE_SPEAKER_LABELS = frozenset({"SPEAKER_05"})  # known artifact label

GROUP_KEYWORDS = (
    "Команда", "Проповідники", "Брати", "Сестри",
    "Програма", "Організація", "Підготовка", "Усі", "Всі",
)

COMPOSITE_SEPARATORS = (" та ", " і ", " / ", ", ")

# Topic deduplication
TOPIC_SIMILARITY_THRESHOLD = 0.65  # fuzzy ratio for "same topic" detection
TOPIC_HEADING_RE = re.compile(r"^###\s+(.+?)\s*$")


@dataclass
class ActionItem:
    person: str
    text: str
    timestamps: list[str]
    raw_line: str

    @property
    def normalized_text(self) -> str:
        return re.sub(r"\s+", " ", self.text.lower()).strip()


@dataclass
class ChunkContent:
    chunk_id: str
    topics_section: str
    actions_by_person: dict[str, list[ActionItem]]


@dataclass
class Topic:
    """A single ### topic from a chunk's topics section.

    title: the ### heading text (without the '### ' prefix)
    body: all the lines AFTER the heading until next ### or end
    source_chunks: which chunks this topic appeared in
    """
    title: str
    body: str
    source_chunks: list[str] = field(default_factory=list)

    @property
    def normalized_title(self) -> str:
        """Lowercased title for fuzzy comparison."""
        return re.sub(r"\s+", " ", self.title.lower()).strip()


CHUNK_FILENAME_RE = re.compile(r"^chunk_(\d+[a-z]?)_(\d+)m-(\d+)m\.md$")
ACTION_LINE_RE = re.compile(r"^(\s*-\s+)(.+?)\s*\(([\d:,\s]+)\)\s*$")


def find_chunk_files(chunks_dir: Path) -> list[Path]:
    files = [
        p for p in chunks_dir.glob("chunk_*.md")
        if CHUNK_FILENAME_RE.match(p.name)
    ]

    def sort_key(p: Path) -> tuple[int, str]:
        m = CHUNK_FILENAME_RE.match(p.name)
        if not m:
            return (999, "")
        chunk_id = m.group(1)
        num_match = re.match(r"(\d+)([a-z]?)", chunk_id)
        if num_match:
            return (int(num_match.group(1)), num_match.group(2))
        return (999, "")

    return sorted(files, key=sort_key)


def split_chunk_sections(text: str) -> tuple[str, str]:
    actions_match = re.search(r"##\s+Наступні кроки", text, flags=re.MULTILINE)
    if actions_match:
        return text[:actions_match.start()].rstrip(), text[actions_match.start():].rstrip()
    return text.rstrip(), ""


def parse_action_items(actions_section: str) -> dict[str, list[ActionItem]]:
    items_by_person: dict[str, list[ActionItem]] = defaultdict(list)
    if not actions_section.strip():
        return items_by_person

    body = re.sub(r"^##\s+Наступні кроки\s*\n?", "", actions_section, count=1)
    current_person: str | None = None

    for line in body.split("\n"):
        person_match = re.match(r"^###\s+(.+?)\s*$", line)
        if person_match:
            current_person = person_match.group(1).strip()
            continue
        if current_person is None:
            continue

        item_match = ACTION_LINE_RE.match(line)
        if item_match:
            text = item_match.group(2).strip()
            ts_field = item_match.group(3).strip()
            timestamps = [t.strip() for t in ts_field.split(",") if t.strip()]
            items_by_person[current_person].append(
                ActionItem(
                    person=current_person,
                    text=text,
                    timestamps=timestamps,
                    raw_line=line.strip(),
                )
            )
    return items_by_person


def parse_chunk_file(path: Path) -> ChunkContent:
    text = path.read_text(encoding="utf-8")
    topics, actions = split_chunk_sections(text)
    actions_by_person = parse_action_items(actions)
    m = CHUNK_FILENAME_RE.match(path.name)
    chunk_id = m.group(1) if m else path.stem
    return ChunkContent(
        chunk_id=chunk_id,
        topics_section=topics,
        actions_by_person=actions_by_person,
    )


def is_group_heading(name: str) -> bool:
    name_lower = name.lower()
    return any(kw.lower() in name_lower for kw in GROUP_KEYWORDS)


def split_composite(name: str) -> list[str]:
    if is_group_heading(name):
        return [name]
    for sep in COMPOSITE_SEPARATORS:
        if sep in name:
            parts = [p.strip() for p in name.split(sep) if p.strip()]
            if len(parts) >= 2:
                return parts
    return [name]


def normalize_name(name: str, aliases: dict[str, str]) -> str:
    if is_group_heading(name):
        return name
    if name in aliases:
        return aliases[name]
    name_no_parens = re.sub(r"\s*\([^)]*\)\s*", " ", name).strip()
    if name_no_parens != name and name_no_parens in aliases:
        return aliases[name_no_parens]
    return name


def expand_and_normalize(raw_name: str, aliases: dict[str, str]) -> list[str]:
    parts = split_composite(raw_name)
    return [normalize_name(p, aliases) for p in parts]


def collect_normalized_actions(
    chunks: list[ChunkContent],
    aliases: dict[str, str],
) -> tuple[dict[str, list[ActionItem]], list[str]]:
    actions_map: dict[str, list[ActionItem]] = defaultdict(list)
    seen_order: list[str] = []

    for chunk in chunks:
        for raw_name, items in chunk.actions_by_person.items():
            canonical_names = expand_and_normalize(raw_name, aliases)
            for canonical in canonical_names:
                if canonical not in actions_map:
                    seen_order.append(canonical)
                for item in items:
                    actions_map[canonical].append(
                        ActionItem(
                            person=canonical,
                            text=item.text,
                            timestamps=list(item.timestamps),
                            raw_line=item.raw_line,
                        )
                    )
    return actions_map, seen_order


def merge_action_items_for_person(items: list[ActionItem]) -> list[ActionItem]:
    grouped: dict[str, list[ActionItem]] = defaultdict(list)
    order: list[str] = []
    for item in items:
        key = item.normalized_text
        if key not in grouped:
            order.append(key)
        grouped[key].append(item)

    merged: list[ActionItem] = []
    for key in order:
        group = grouped[key]
        first = group[0]
        all_ts: list[str] = []
        seen: set[str] = set()
        for item in group:
            for ts in item.timestamps:
                if ts not in seen:
                    seen.add(ts)
                    all_ts.append(ts)
        all_ts.sort(key=timestamp_to_seconds)
        merged.append(
            ActionItem(
                person=first.person,
                text=first.text,
                timestamps=all_ts,
                raw_line=first.raw_line,
            )
        )
    return merged


def timestamp_to_seconds(ts: str) -> float:
    parts = ts.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        elif len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        return float(ts)
    except ValueError:
        return 0.0


def format_action_item(item: ActionItem) -> str:
    ts_str = ", ".join(item.timestamps)
    return f"- {item.text} ({ts_str})" if ts_str else f"- {item.text}"


def get_date_string(cli_date: str | None, audio_file: Path | None) -> str:
    if cli_date:
        return cli_date
    if audio_file and audio_file.exists():
        stat = audio_file.stat()
        birth_time = getattr(stat, "st_birthtime", None)
        timestamp = birth_time if birth_time else stat.st_mtime
        return datetime.fromtimestamp(timestamp).strftime("%d/%m/%Y")
    return "[дата]"


def load_aliases(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return {k: v for k, v in data.items() if not k.startswith("_")}


def detect_attendees(
    rttm_path: Path,
    speakers_map_path: Path,
    aliases: dict[str, str],
    min_speech_seconds: float = MIN_SPEECH_SECONDS,
    exclude_labels: frozenset[str] = EXCLUDE_SPEAKER_LABELS,
) -> list[str]:
    """Detect who attended the meeting, in order of first appearance.

    Steps:
        1. Load RTTM, find all (speaker_label, start, duration) turns
        2. Sum speech time per speaker, exclude labels below min_speech_seconds
        3. Exclude known artifact labels (SPEAKER_05)
        4. Order speakers by their FIRST appearance in audio
        5. Map SPEAKER_XX → name via speakers.json
        6. Map raw name → canonical via aliases
    """
    if not rttm_path.exists():
        return []

    # Lazy import — avoid hard dep on pyannote at module top level
    from pyannote.database.util import load_rttm

    rttm_dict = load_rttm(str(rttm_path))
    diarization = next(iter(rttm_dict.values()))

    # Pass 1: total speech time per speaker label
    speech_total: dict[str, float] = defaultdict(float)
    first_seen: dict[str, float] = {}

    for turn, _, speaker in diarization.itertracks(yield_label=True):
        if speaker in exclude_labels:
            continue
        speech_total[speaker] += turn.duration
        # Record earliest appearance
        if speaker not in first_seen or turn.start < first_seen[speaker]:
            first_seen[speaker] = turn.start

    # Filter by min_speech threshold
    qualified = [
        speaker for speaker, total in speech_total.items()
        if total >= min_speech_seconds
    ]

    # Sort by first appearance
    qualified.sort(key=lambda s: first_seen[s])

    # Load SPEAKER_XX → name mapping
    if not speakers_map_path.exists():
        # Fallback: use raw SPEAKER_XX labels
        return qualified

    with speakers_map_path.open("r", encoding="utf-8") as f:
        speakers_map = json.load(f)

    # Map each qualified speaker through speakers.json, then through aliases
    attendees: list[str] = []
    seen_canonical: set[str] = set()

    for label in qualified:
        raw_name = speakers_map.get(label, label)
        canonical = normalize_name(raw_name, aliases)
        if canonical in seen_canonical:
            continue
        seen_canonical.add(canonical)
        attendees.append(canonical)

    return attendees


def parse_topics_from_chunk(chunk: ChunkContent) -> list[Topic]:
    """Parse a chunk's topics_section into individual Topic objects.

    Splits on ### headings. Body of each topic = everything between this
    ### and the next ###. Strips the chunk's "## Розглянуті питання" header.
    """
    if not chunk.topics_section.strip():
        return []

    # Strip the section header
    text = re.sub(
        r"^##\s+Розглянуті\s+питання\s*\n?",
        "",
        chunk.topics_section,
        count=1,
        flags=re.MULTILINE | re.IGNORECASE,
    )

    topics: list[Topic] = []
    current_title: str | None = None
    current_body_lines: list[str] = []

    for line in text.split("\n"):
        m = TOPIC_HEADING_RE.match(line)
        if m:
            # Flush the previous topic
            if current_title is not None:
                topics.append(
                    Topic(
                        title=current_title,
                        body="\n".join(current_body_lines).rstrip(),
                        source_chunks=[chunk.chunk_id],
                    )
                )
            current_title = m.group(1).strip()
            current_body_lines = []
        else:
            if current_title is not None:
                current_body_lines.append(line)
            # else: lines before any ### (typically blank or stray text) — skip

    # Flush the last topic
    if current_title is not None:
        topics.append(
            Topic(
                title=current_title,
                body="\n".join(current_body_lines).rstrip(),
                source_chunks=[chunk.chunk_id],
            )
        )

    return topics


def title_similarity(a: str, b: str) -> float:
    """Fuzzy similarity ratio between two topic titles, in [0, 1]."""
    return SequenceMatcher(None, a, b).ratio()


def deduplicate_topics(
    all_topics: list[Topic],
    threshold: float = TOPIC_SIMILARITY_THRESHOLD,
) -> tuple[list[Topic], list[tuple[str, str, float]]]:
    """Group topics by title similarity and merge similar ones.

    Greedy O(n^2) algorithm:
        For each new topic, compare to all already-kept groups.
        If similarity to any kept topic >= threshold → merge into that group.
        Otherwise → start a new group.

    The first occurrence's title is kept as canonical. Bodies are concatenated
    in order of appearance.

    Returns:
        merged_topics: deduplicated list of Topic objects
        merge_log: list of (kept_title, merged_title, similarity) tuples
                   for diagnostic printing
    """
    merged: list[Topic] = []
    merge_log: list[tuple[str, str, float]] = []

    for topic in all_topics:
        best_match_idx: int | None = None
        best_sim = 0.0

        for i, kept in enumerate(merged):
            sim = title_similarity(topic.normalized_title, kept.normalized_title)
            if sim >= threshold and sim > best_sim:
                best_sim = sim
                best_match_idx = i

        if best_match_idx is not None:
            # Merge into existing group
            kept = merged[best_match_idx]
            # Append body with chunk marker
            chunk_marker = f"\n\n<!-- continued in chunk {topic.source_chunks[0]} -->\n"
            kept.body = kept.body + chunk_marker + topic.body
            kept.source_chunks.extend(topic.source_chunks)
            merge_log.append((kept.title, topic.title, best_sim))
        else:
            # Start new group (make a copy to avoid mutating input)
            merged.append(
                Topic(
                    title=topic.title,
                    body=topic.body,
                    source_chunks=list(topic.source_chunks),
                )
            )

    return merged, merge_log


def build_polished_protocol(
    chunks: list[ChunkContent],
    date_str: str,
    aliases: dict[str, str],
    attendees: list[str],
    topic_similarity_threshold: float = TOPIC_SIMILARITY_THRESHOLD,
) -> tuple[str, dict[str, int], dict[str, int], list[tuple[str, str, float]]]:
    """Build the final polished markdown.

    Returns:
        (markdown_text, pre_dedup_counts, post_dedup_counts, topic_merge_log)
    """
    lines: list[str] = []
    lines.append(f"# Протокол зустрічі від {date_str}")
    lines.append("")
    lines.append("## Присутні")
    if attendees:
        for name in attendees:
            lines.append(f"- {name}")
    else:
        lines.append("- [не вдалося визначити — RTTM або speakers.json відсутні]")
    lines.append("")
    lines.append("## Розглянуті питання")
    lines.append("")

    # Collect topics from all chunks and dedup
    all_topics: list[Topic] = []
    for chunk in chunks:
        all_topics.extend(parse_topics_from_chunk(chunk))

    merged_topics, topic_merge_log = deduplicate_topics(
        all_topics, threshold=topic_similarity_threshold
    )

    # Render merged topics
    for topic in merged_topics:
        # Source chunks marker for transparency
        if len(topic.source_chunks) == 1:
            chunks_str = topic.source_chunks[0]
        else:
            chunks_str = ", ".join(topic.source_chunks)
        lines.append(f"<!-- topic from chunks: {chunks_str} -->")
        lines.append(f"### {topic.title}")
        if topic.body.strip():
            lines.append(topic.body.rstrip())
        lines.append("")

    lines.append("## Наступні кроки")
    lines.append("")

    actions_map, order = collect_normalized_actions(chunks, aliases)

    pre_dedup: dict[str, int] = {}
    post_dedup: dict[str, int] = {}

    people_names = [n for n in order if not is_group_heading(n)]
    group_names = [n for n in order if is_group_heading(n)]

    def render_section(canonical: str) -> None:
        items = actions_map[canonical]
        merged = merge_action_items_for_person(items)
        if not merged:
            return
        pre_dedup[canonical] = len(items)
        post_dedup[canonical] = len(merged)
        lines.append(f"### {canonical}")
        for item in merged:
            lines.append(format_action_item(item))
        lines.append("")

    for canonical in people_names:
        render_section(canonical)

    if group_names:
        lines.append("---")
        lines.append("")
        lines.append("**Збірні позиції** (групи / теми):")
        lines.append("")
        for canonical in group_names:
            render_section(canonical)

    return "\n".join(lines), pre_dedup, post_dedup, topic_merge_log


def print_summary(
    chunks: list[ChunkContent],
    pre_dedup: dict[str, int],
    post_dedup: dict[str, int],
    polished: str,
    aliases: dict[str, str],
    topic_merge_log: list[tuple[str, str, float]] | None = None,
) -> None:
    n_chunks = len(chunks)
    n_empty = sum(1 for c in chunks if not c.topics_section.strip())
    total_pre = sum(pre_dedup.values())
    total_post = sum(post_dedup.values())

    n_lines = len(polished.split("\n"))
    n_headers = sum(1 for l in polished.split("\n") if l.lstrip().startswith("#"))
    n_bullets = sum(1 for l in polished.split("\n") if l.lstrip().startswith("-"))
    n_timestamps = len(re.findall(r"\(\d+:\d+(?::\d+)?", polished))

    print(f"\n{'=' * 70}")
    print(f"  Polishing summary")
    print(f"{'=' * 70}")
    print(f"Chunks processed:           {n_chunks} (empty: {n_empty})")
    print(f"Name aliases loaded:        {len(aliases)}")
    print(f"")

    if topic_merge_log is not None:
        print(f"Topic deduplication:")
        print(f"  topics merged (similar titles): {len(topic_merge_log)}")
        if topic_merge_log:
            print(f"  merged pairs (kept ← merged, sim):")
            for kept, merged, sim in topic_merge_log:
                # Truncate long titles for display
                kept_disp = kept if len(kept) <= 50 else kept[:47] + "..."
                merged_disp = merged if len(merged) <= 50 else merged[:47] + "..."
                print(f"    '{kept_disp}'")
                print(f"      ← '{merged_disp}' (sim={sim:.3f})")
        print(f"")

    print(f"Action items:")
    print(f"  total (after composite split): {total_pre}")
    print(f"  after dedup:                   {total_post}")
    print(f"  duplicates removed:            {total_pre - total_post}")
    print(f"")

    people = sorted([n for n in post_dedup if not is_group_heading(n)])
    groups = sorted([n for n in post_dedup if is_group_heading(n)])

    print(f"PEOPLE ({len(people)}):")
    for name in people:
        pre = pre_dedup[name]
        post = post_dedup[name]
        note = f" ({pre - post} dedup'd)" if pre != post else ""
        print(f"  {name:<30} {post}{note}")

    if groups:
        print(f"\nGROUPS / TOPICS ({len(groups)}):")
        for name in groups:
            print(f"  {name:<40} {post_dedup[name]}")

    print(f"")
    print(f"Output: {n_lines} lines, {n_headers} headers, {n_bullets} bullets, {n_timestamps} timestamps")


def main() -> None:
    parser = argparse.ArgumentParser(description="Polish chunked protocol output")
    parser.add_argument("--chunks-dir", type=Path, default=DEFAULT_CHUNKS_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--aliases", type=Path, default=DEFAULT_ALIASES)
    parser.add_argument("--rttm", type=Path, default=DEFAULT_RTTM)
    parser.add_argument("--speakers-map", type=Path, default=DEFAULT_SPEAKERS_MAP)
    parser.add_argument(
        "--min-speech-seconds",
        type=float,
        default=MIN_SPEECH_SECONDS,
        help=f"Min speech seconds to count as attendee (default: {MIN_SPEECH_SECONDS})",
    )
    parser.add_argument(
        "--topic-similarity",
        type=float,
        default=TOPIC_SIMILARITY_THRESHOLD,
        help=f"Fuzzy ratio threshold for merging similar topics "
             f"(default: {TOPIC_SIMILARITY_THRESHOLD}). "
             f"Lower = more aggressive merging.",
    )
    parser.add_argument("--date", type=str, default=None)
    parser.add_argument("--audio-file", type=Path, default=None)
    args = parser.parse_args()

    if not args.chunks_dir.exists():
        print(f"❌ Chunks directory not found: {args.chunks_dir}")
        raise SystemExit(1)

    chunk_files = find_chunk_files(args.chunks_dir)
    if not chunk_files:
        print(f"❌ No chunk_*.md files in {args.chunks_dir}")
        raise SystemExit(1)

    print(f"Found {len(chunk_files)} chunk files:")
    for p in chunk_files:
        print(f"  {p.name}")

    chunks = [parse_chunk_file(p) for p in chunk_files]
    date_str = get_date_string(args.date, args.audio_file)
    print(f"\nMeeting date: {date_str}")
    if args.date:
        print(f"  source: --date argument")
    elif args.audio_file:
        print(f"  source: {args.audio_file} file metadata")

    aliases = load_aliases(args.aliases)
    print(f"\nLoaded {len(aliases)} name aliases from {args.aliases}")

    # Detect attendees from RTTM
    attendees = detect_attendees(
        rttm_path=args.rttm,
        speakers_map_path=args.speakers_map,
        aliases=aliases,
        min_speech_seconds=args.min_speech_seconds,
    )
    if attendees:
        print(f"\nDetected {len(attendees)} attendees from RTTM (min {args.min_speech_seconds:.0f}s):")
        for i, name in enumerate(attendees, 1):
            print(f"  {i}. {name}")
    else:
        print(f"\n⚠ No attendees detected (RTTM or speakers.json missing?)")

    polished, pre_dedup, post_dedup, topic_merge_log = build_polished_protocol(
        chunks, date_str, aliases, attendees,
        topic_similarity_threshold=args.topic_similarity,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(polished, encoding="utf-8")
    print(f"\n✓ Saved polished protocol: {args.output}")

    print_summary(
        chunks, pre_dedup, post_dedup, polished, aliases,
        topic_merge_log=topic_merge_log,
    )


if __name__ == "__main__":
    main()
