"""Merge Whisper transcript with pyannote diarization.

Takes the cached outputs from transcribe.py and diarization.py, plus the
speakers.json mapping, and produces an annotated transcript like:

    [00:00 Павло]: Ти ж можеш записати повністю зум, правильно?
    [00:04 Роман]: Так, так, оце ви тільки що чули...
    [01:10:36 [нерозбірливо]]: ...crosstalk segment...

Architectural rules (decided in conversation):
- A2: SPEAKER_05 segments → "[нерозбірливо]" prefix (we keep the text but drop the name)
- B1: Take dominant speaker (max overlap) when a Whisper segment overlaps multiple speakers
- C:  Speaker mapping comes from data/speakers.json (NOT hardcoded)

Usage:
    uv run python -m church_assistant.merge_transcript

    # Custom paths
    uv run python -m church_assistant.merge_transcript \\
        --transcript data/other_transcript.json \\
        --rttm data/other.rttm \\
        --output data/other_annotated.md

    # Show first N segments only
    uv run python -m church_assistant.merge_transcript --limit 30
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from pyannote.core import Annotation
from pyannote.database.util import load_rttm


# Default paths — match conventions from diarization.py and transcribe.py
DEFAULT_TRANSCRIPT = Path("data/test_baseline_transcript.json")
DEFAULT_RTTM = Path("data/test_baseline.rttm")
DEFAULT_SPEAKERS = Path("data/speakers.json")
DEFAULT_OUTPUT = Path("data/test_baseline_annotated.md")

# Label that signals "uncertain / crosstalk / bad audio" (rule A2)
UNCERTAIN_LABEL = "SPEAKER_05"


@dataclass
class WhisperSegment:
    """One Whisper segment loaded from JSON."""

    start: float
    end: float
    text: str


@dataclass
class AnnotatedSegment:
    """A Whisper segment after merging with diarization."""

    start: float
    end: float
    text: str
    speaker_label: str       # raw pyannote label, e.g. "SPEAKER_06"
    speaker_name: str        # resolved name, e.g. "Роман" or "[нерозбірливо]"
    overlap_seconds: float   # how much this speaker overlapped with the segment
    is_uncertain: bool       # True if speaker is SPEAKER_05


def load_whisper_transcript(path: Path) -> list[WhisperSegment]:
    """Load Whisper segments from JSON file produced by transcribe.py."""
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return [
        WhisperSegment(start=s["start"], end=s["end"], text=s["text"])
        for s in data["segments"]
    ]


def load_diarization(path: Path) -> Annotation:
    """Load diarization from RTTM file."""
    rttm_dict = load_rttm(str(path))
    return next(iter(rttm_dict.values()))


def load_speaker_map(path: Path) -> dict[str, str]:
    """Load SPEAKER_XX → name mapping from JSON file."""
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def find_dominant_speaker(
        diarization: Annotation,
        segment_start: float,
        segment_end: float,
) -> tuple[str | None, float]:
    """Find which speaker has the most overlap with [segment_start, segment_end].

    Rule B1: take the dominant speaker by total overlap.

    Returns:
        (speaker_label, overlap_seconds), or (None, 0.0) if no overlap.
    """
    totals: Counter[str] = Counter()
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        # Skip turns that don't overlap with this segment
        if turn.end < segment_start or turn.start > segment_end:
            continue
        overlap_start = max(turn.start, segment_start)
        overlap_end = min(turn.end, segment_end)
        overlap = overlap_end - overlap_start
        if overlap > 0:
            totals[speaker] += overlap

    if not totals:
        return None, 0.0

    label, total_overlap = totals.most_common(1)[0]
    return label, total_overlap


def merge_transcript_with_diarization(
        transcript: list[WhisperSegment],
        diarization: Annotation,
        speaker_map: dict[str, str],
) -> list[AnnotatedSegment]:
    """Annotate each Whisper segment with the dominant speaker.

    Rule A2: SPEAKER_05 → "[нерозбірливо]" (keep text, drop name attribution).
    Rule B1: dominant speaker by max overlap.
    Rule C:  resolve labels via speaker_map.
    """
    annotated: list[AnnotatedSegment] = []

    for seg in transcript:
        speaker_label, overlap = find_dominant_speaker(
            diarization, seg.start, seg.end
        )

        # No speaker detected in this time window — rare, but possible
        if speaker_label is None:
            annotated.append(
                AnnotatedSegment(
                    start=seg.start,
                    end=seg.end,
                    text=seg.text,
                    speaker_label="UNKNOWN",
                    speaker_name="[немає мовця]",
                    overlap_seconds=0.0,
                    is_uncertain=True,
                )
            )
            continue

        # Resolve label → name via mapping
        speaker_name = speaker_map.get(speaker_label, speaker_label)
        is_uncertain = speaker_label == UNCERTAIN_LABEL

        annotated.append(
            AnnotatedSegment(
                start=seg.start,
                end=seg.end,
                text=seg.text,
                speaker_label=speaker_label,
                speaker_name=speaker_name,
                overlap_seconds=overlap,
                is_uncertain=is_uncertain,
            )
        )

    return annotated


def format_timestamp(seconds: float) -> str:
    """Format seconds as MM:SS or H:MM:SS."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def format_segment(seg: AnnotatedSegment) -> str:
    """Format one segment as: [timestamp speaker]: text"""
    timestamp = format_timestamp(seg.start)
    return f"[{timestamp} {seg.speaker_name}]: {seg.text}"


def save_annotated_transcript(
        segments: list[AnnotatedSegment],
        output_path: Path,
) -> None:
    """Save annotated transcript as Markdown."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        f.write("# Annotated Transcript\n\n")
        for seg in segments:
            f.write(format_segment(seg) + "\n")


def print_statistics(segments: list[AnnotatedSegment]) -> None:
    """Print summary statistics about the merge result."""
    total = len(segments)
    uncertain = sum(1 for s in segments if s.is_uncertain)
    no_speaker = sum(1 for s in segments if s.speaker_label == "UNKNOWN")

    # Per-speaker statistics
    speaker_segment_counts: Counter[str] = Counter()
    speaker_time: Counter[str] = Counter()
    for s in segments:
        speaker_segment_counts[s.speaker_name] += 1
        speaker_time[s.speaker_name] += (s.end - s.start)

    total_time = sum(speaker_time.values())

    print(f"\n{'=' * 70}")
    print(f"  Merge statistics")
    print(f"{'=' * 70}")
    print(f"Total segments:              {total}")
    print(f"Uncertain (SPEAKER_05):      {uncertain} ({uncertain/total*100:.1f}%)")
    print(f"No speaker detected:         {no_speaker} ({no_speaker/total*100:.1f}%)")
    print(f"\nPer-speaker breakdown (by segment count):")
    print(f"{'Speaker':<30} {'Segments':>10} {'Time':>10} {'%':>8}")
    print("-" * 60)
    for name, count in speaker_segment_counts.most_common():
        time_s = speaker_time[name]
        pct = (time_s / total_time * 100) if total_time > 0 else 0
        print(f"{name:<30} {count:>10} {time_s:>9.1f}s {pct:>7.1f}%")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Merge Whisper transcript with pyannote diarization"
    )
    parser.add_argument(
        "--transcript",
        type=Path,
        default=DEFAULT_TRANSCRIPT,
        help=f"Whisper transcript JSON (default: {DEFAULT_TRANSCRIPT})",
    )
    parser.add_argument(
        "--rttm",
        type=Path,
        default=DEFAULT_RTTM,
        help=f"Diarization RTTM file (default: {DEFAULT_RTTM})",
    )
    parser.add_argument(
        "--speakers",
        type=Path,
        default=DEFAULT_SPEAKERS,
        help=f"Speaker mapping JSON (default: {DEFAULT_SPEAKERS})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output annotated transcript (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=30,
        help="How many segments to print preview (default: 30)",
    )
    args = parser.parse_args()

    # Validate inputs
    for path, name in [
        (args.transcript, "transcript"),
        (args.rttm, "RTTM"),
        (args.speakers, "speakers"),
    ]:
        if not path.exists():
            print(f"❌ {name} not found: {path}")
            raise SystemExit(1)

    # Load all three inputs
    print(f"Loading transcript: {args.transcript}")
    transcript = load_whisper_transcript(args.transcript)
    print(f"  ✓ {len(transcript)} Whisper segments")

    print(f"Loading diarization: {args.rttm}")
    diarization = load_diarization(args.rttm)
    n_turns = len(list(diarization.itertracks()))
    print(f"  ✓ {n_turns} diarization turns")

    print(f"Loading speaker map: {args.speakers}")
    speaker_map = load_speaker_map(args.speakers)
    print(f"  ✓ {len(speaker_map)} speakers mapped")

    # Merge
    print(f"\nMerging...")
    annotated = merge_transcript_with_diarization(
        transcript, diarization, speaker_map
    )

    # Save before showing anything — same crash-safety principle as earlier scripts
    save_annotated_transcript(annotated, args.output)
    print(f"✓ Saved annotated transcript to: {args.output}")

    # Statistics
    print_statistics(annotated)

    # Preview
    print(f"\n{'=' * 70}")
    print(f"  First {min(args.limit, len(annotated))} segments")
    print(f"{'=' * 70}")
    for seg in annotated[:args.limit]:
        print(format_segment(seg))


if __name__ == "__main__":
    main()