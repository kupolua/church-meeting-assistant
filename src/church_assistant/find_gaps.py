"""Find gaps in Whisper transcript timeline.

Loads the cached Whisper JSON (data/test_baseline_transcript.json) and looks
for time gaps between consecutive segments — places where Whisper may have
silently dropped audio content.

For a 2-hour pastoral meeting recording, we expect:
- Most gaps < 1s (natural speech pauses)
- Some gaps 1-5s (turn-taking, brief silence)
- Few gaps 5-15s (real pauses, transitions between topics)
- Almost no gaps > 15s — these are suspect

The script outputs:
- All gaps > GAP_THRESHOLD seconds, sorted by size
- A histogram of gap durations
- Total "missing" time in suspicious gaps
- Summary statistics

Usage:
    uv run python -m church_assistant.find_gaps

    # Custom threshold
    uv run python -m church_assistant.find_gaps --threshold 5.0

    # Different transcript
    uv run python -m church_assistant.find_gaps --transcript data/other_transcript.json
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path


DEFAULT_TRANSCRIPT = Path("data/test_baseline_transcript.json")
DEFAULT_GAP_THRESHOLD = 5.0  # seconds


@dataclass
class Gap:
    """A time gap between two Whisper segments."""

    segment_index: int       # index of the segment AFTER the gap
    gap_start: float         # end time of previous segment
    gap_end: float           # start time of next segment
    duration: float          # gap_end - gap_start
    prev_text: str           # last words before the gap (truncated)
    next_text: str           # first words after the gap (truncated)


def format_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds - h * 3600 - m * 60
    if h > 0:
        return f"{h:d}:{m:02d}:{s:05.2f}"
    return f"{m:02d}:{s:05.2f}"


def truncate(text: str, max_chars: int = 60) -> str:
    """Truncate text for display."""
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3] + "..."


def find_gaps(
        segments: list[dict],
        threshold: float = DEFAULT_GAP_THRESHOLD,
) -> list[Gap]:
    """Find gaps > threshold between consecutive segments."""
    gaps: list[Gap] = []
    for i in range(1, len(segments)):
        prev_end = segments[i - 1]["end"]
        curr_start = segments[i]["start"]
        duration = curr_start - prev_end
        if duration > threshold:
            gaps.append(
                Gap(
                    segment_index=i,
                    gap_start=prev_end,
                    gap_end=curr_start,
                    duration=duration,
                    prev_text=truncate(segments[i - 1]["text"]),
                    next_text=truncate(segments[i]["text"]),
                )
            )
    return gaps


def print_gaps_table(gaps: list[Gap]) -> None:
    """Print all gaps sorted by duration (longest first)."""
    if not gaps:
        print("  (no gaps above threshold)")
        return

    gaps_sorted = sorted(gaps, key=lambda g: g.duration, reverse=True)

    print(f"\n{'#':>3} {'Start':>12} {'End':>12} {'Duration':>10}  Context")
    print("-" * 100)
    for i, gap in enumerate(gaps_sorted, 1):
        start_str = format_timestamp(gap.gap_start)
        end_str = format_timestamp(gap.gap_end)
        print(
            f"{i:>3} {start_str:>12} {end_str:>12} "
            f"{gap.duration:>8.2f}s  "
            f"'{gap.prev_text}' → '{gap.next_text}'"
        )


def print_chronological(gaps: list[Gap]) -> None:
    """Print gaps in chronological order (for context with the meeting)."""
    if not gaps:
        return

    print(f"\nGaps in chronological order:")
    print(f"{'Time range':>30} {'Dur':>8}")
    print("-" * 40)
    for gap in gaps:
        time_range = (
            f"{format_timestamp(gap.gap_start)} → "
            f"{format_timestamp(gap.gap_end)}"
        )
        print(f"{time_range:>30} {gap.duration:>6.2f}s")


def print_histogram(segments: list[dict]) -> None:
    """Print a histogram of all gap durations (including small ones)."""
    if len(segments) < 2:
        return

    all_gaps: list[float] = []
    for i in range(1, len(segments)):
        gap = segments[i]["start"] - segments[i - 1]["end"]
        if gap > 0:
            all_gaps.append(gap)

    # Bucket boundaries (seconds)
    buckets = [
        (0.0, 0.5, "0.0–0.5s"),
        (0.5, 1.0, "0.5–1.0s"),
        (1.0, 2.0, "1–2s    "),
        (2.0, 5.0, "2–5s    "),
        (5.0, 10.0, "5–10s   "),
        (10.0, 30.0, "10–30s  "),
        (30.0, 60.0, "30–60s  "),
        (60.0, float("inf"), "60s+    "),
    ]

    print(f"\nGap distribution (total {len(all_gaps)} gaps):")
    print(f"{'Range':<12} {'Count':>8} {'Total dur':>12}")
    print("-" * 36)
    for low, high, label in buckets:
        count = sum(1 for g in all_gaps if low <= g < high)
        total = sum(g for g in all_gaps if low <= g < high)
        if count > 0:
            print(f"{label:<12} {count:>8} {total:>10.1f}s")


def chunk_4_specific_analysis(gaps: list[Gap]) -> None:
    """Check whether chunk 4 (55-80 min main zone) has unusual gaps."""
    chunk4_start = 55 * 60   # 3300s
    chunk4_end = 80 * 60     # 4800s

    chunk4_gaps = [
        g for g in gaps
        if g.gap_start >= chunk4_start and g.gap_end <= chunk4_end
    ]

    if not chunk4_gaps:
        print(
            f"\n  No gaps > threshold in chunk 4 zone "
            f"(55:00 – 80:00). Chunk 4 transcript is clean."
        )
        return

    total_missing = sum(g.duration for g in chunk4_gaps)
    print(f"\nChunk 4 zone (55:00–80:00):")
    print(f"  {len(chunk4_gaps)} gap(s) above threshold")
    print(
        f"  Total 'missing' time in this zone: {total_missing:.1f}s "
        f"({total_missing/60:.1f} min)"
    )
    for gap in chunk4_gaps:
        print(
            f"    {format_timestamp(gap.gap_start)} → "
            f"{format_timestamp(gap.gap_end)} "
            f"({gap.duration:.1f}s)"
        )
        print(f"      before: '{gap.prev_text}'")
        print(f"      after:  '{gap.next_text}'")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Find gaps in Whisper transcript timeline"
    )
    parser.add_argument(
        "--transcript",
        type=Path,
        default=DEFAULT_TRANSCRIPT,
        help=f"Whisper JSON transcript (default: {DEFAULT_TRANSCRIPT})",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_GAP_THRESHOLD,
        help=f"Minimum gap duration to report, in seconds "
             f"(default: {DEFAULT_GAP_THRESHOLD})",
    )
    args = parser.parse_args()

    if not args.transcript.exists():
        print(f"❌ Transcript not found: {args.transcript}")
        raise SystemExit(1)

    print(f"Loading transcript: {args.transcript}")
    with args.transcript.open("r", encoding="utf-8") as f:
        data = json.load(f)
    segments = data["segments"]
    audio_duration = data.get("duration", 0)

    print(f"  ✓ {len(segments)} segments")
    print(f"  ✓ Audio duration: {format_timestamp(audio_duration)}")

    # Compute total speaking time and overall "missing" time
    total_speaking = sum(s["end"] - s["start"] for s in segments)
    missing = audio_duration - total_speaking
    print(
        f"  ✓ Total speaking time: {format_timestamp(total_speaking)} "
        f"({total_speaking/audio_duration*100:.1f}%)"
    )
    print(
        f"  ✓ Total silence/missing: {format_timestamp(missing)} "
        f"({missing/audio_duration*100:.1f}%)"
    )

    # Find suspicious gaps
    gaps = find_gaps(segments, threshold=args.threshold)
    print(f"\n{'=' * 70}")
    print(f"  Gaps > {args.threshold:.1f}s: found {len(gaps)}")
    print(f"{'=' * 70}")

    print_gaps_table(gaps)
    print_histogram(segments)
    print_chronological(gaps)
    chunk_4_specific_analysis(gaps)

    # Verdict
    print(f"\n{'=' * 70}")
    print(f"  Diagnosis")
    print(f"{'=' * 70}")

    total_gap_time = sum(g.duration for g in gaps)
    print(
        f"\nTotal 'suspicious' missing time (gaps > {args.threshold:.1f}s): "
        f"{total_gap_time:.1f}s ({total_gap_time/60:.1f} min)"
    )

    if gaps:
        biggest = max(gaps, key=lambda g: g.duration)
        print(f"\nLargest gap: {biggest.duration:.1f}s at "
              f"{format_timestamp(biggest.gap_start)}")
        print(f"  Before: '{biggest.prev_text}'")
        print(f"  After:  '{biggest.next_text}'")
        print(
            f"\n→ Listen to the audio around these timestamps to verify "
            f"whether Whisper dropped real content."
        )


if __name__ == "__main__":
    main()