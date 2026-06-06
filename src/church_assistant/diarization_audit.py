"""Audit pyannote diarization quality against manual ground truth.

Reads the cached RTTM file and answers two questions:
1. Does pyannote attribute Roman's moments (4 out of 5 ground truth points)
   to the same SPEAKER_XX, or does it split him into multiple virtual speakers?
2. How does filtering microsegments (<0.5s) affect the unique speaker count?

Usage:
    uv run python -m church_assistant.diarization_audit
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from pyannote.core import Annotation
from pyannote.database.util import load_rttm


# Ground truth points from manual listening to test_baseline.m4a
GROUND_TRUTH: dict[float, str] = {
    # 00:00 — first 1-2s is Roman, then Pasha takes over for FireFiles discussion
    # We use 0:03 (just after the Roman→Pasha transition) to capture Pasha
    3.0: "Pasha",
    # 16:00-16:16 — Roman about Easter dates
    970.0: "Roman",  # 16:10 — middle of Roman's segment
    # 28:30 — Roman about 5000 UAH for the skit
    1710.0: "Roman",
    # 01:14:00 — Chad about Anya Filatova going to Argentina
    4440.0: "Chad",
    # 01:50:00 — Roman about FOP and payments to ministers
    6600.0: "Roman",
}

# Tolerance window (seconds) — look for speakers active around the timestamp
WINDOW_SECONDS = 5.0

# Microsegment threshold (seconds) — segments shorter than this are likely noise
MICROSEGMENT_THRESHOLD = 0.5


@dataclass
class SpeakerAtTimestamp:
    """One speaker active at or near a target timestamp."""

    speaker: str
    start: float
    end: float
    overlap_with_target: float  # how much of this segment is within the ±window


def speakers_at_timestamp(
        diarization: Annotation,
        target_t: float,
        window: float = WINDOW_SECONDS,
) -> list[SpeakerAtTimestamp]:
    """Find all speakers active within ±window seconds of target_t.

    Returns a list sorted by overlap (most overlap first).
    """
    window_start = target_t - window
    window_end = target_t + window

    found: list[SpeakerAtTimestamp] = []
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        # No overlap with our window — skip
        if turn.end < window_start or turn.start > window_end:
            continue

        # Calculate actual overlap with the ±window region
        overlap_start = max(turn.start, window_start)
        overlap_end = min(turn.end, window_end)
        overlap = overlap_end - overlap_start

        found.append(
            SpeakerAtTimestamp(
                speaker=speaker,
                start=turn.start,
                end=turn.end,
                overlap_with_target=overlap,
            )
        )

    # Most overlap first
    found.sort(key=lambda s: s.overlap_with_target, reverse=True)
    return found


def dominant_speaker(
        diarization: Annotation,
        target_t: float,
        window: float = WINDOW_SECONDS,
) -> str | None:
    """Which SPEAKER_XX has the most overlap at this timestamp?"""
    speakers = speakers_at_timestamp(diarization, target_t, window)
    if not speakers:
        return None
    # Group by speaker and sum overlaps
    totals: Counter[str] = Counter()
    for s in speakers:
        totals[s.speaker] += s.overlap_with_target
    return totals.most_common(1)[0][0]


def filter_microsegments(
        diarization: Annotation,
        threshold: float = MICROSEGMENT_THRESHOLD,
) -> Annotation:
    """Return a new Annotation with segments shorter than threshold removed."""
    cleaned = Annotation(uri=diarization.uri)
    for turn, track, speaker in diarization.itertracks(yield_label=True):
        if turn.duration >= threshold:
            cleaned[turn, track] = speaker
    return cleaned


def speaker_statistics(diarization: Annotation) -> dict[str, float]:
    """Total speaking time per SPEAKER_XX label, sorted descending."""
    totals: Counter[str] = Counter()
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        totals[speaker] += turn.duration
    return dict(totals.most_common())


def print_section(title: str) -> None:
    print(f"\n{'=' * 70}")
    print(f"  {title}")
    print(f"{'=' * 70}")


def format_timestamp(seconds: float) -> str:
    """Format seconds as HH:MM:SS for readability."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def main() -> None:
    # ============ Load RTTM from cache ============
    rttm_path = Path("data/test_baseline.rttm")
    if not rttm_path.exists():
        print(f"❌ RTTM not found: {rttm_path}")
        print("   Run `uv run python -m church_assistant.diarization` first.")
        return

    print(f"Loading diarization from {rttm_path}...")
    rttm_dict = load_rttm(str(rttm_path))
    # load_rttm returns {uri: Annotation}; we take the first value
    diarization = next(iter(rttm_dict.values()))
    print(f"✓ Loaded {len(list(diarization.itertracks()))} segments")

    # ============ Section 1: Raw vs Filtered statistics ============
    print_section("1. Speaker statistics — raw vs filtered")

    raw_stats = speaker_statistics(diarization)
    print(f"\nRaw diarization: {len(raw_stats)} unique speakers")
    print(f"{'Speaker':<15} {'Total time':>12} {'% of speech':>12}")
    print("-" * 45)
    total_speech = sum(raw_stats.values())
    for speaker, duration in raw_stats.items():
        pct = duration / total_speech * 100
        print(f"{speaker:<15} {duration:>10.1f}s {pct:>11.1f}%")

    filtered = filter_microsegments(diarization)
    filtered_stats = speaker_statistics(filtered)
    print(
        f"\nFiltered (segments >= {MICROSEGMENT_THRESHOLD}s): "
        f"{len(filtered_stats)} unique speakers"
    )
    print(f"{'Speaker':<15} {'Total time':>12} {'% of speech':>12}")
    print("-" * 45)
    total_speech_f = sum(filtered_stats.values())
    for speaker, duration in filtered_stats.items():
        pct = duration / total_speech_f * 100 if total_speech_f > 0 else 0
        print(f"{speaker:<15} {duration:>10.1f}s {pct:>11.1f}%")

    # ============ Section 2: Ground truth spot-checks ============
    print_section("2. Ground truth spot-checks")

    print(
        f"\nFor each of the {len(GROUND_TRUTH)} known points, find dominant "
        f"SPEAKER_XX (±{WINDOW_SECONDS}s window):"
    )
    print()
    print(
        f"{'Timestamp':<12} {'Real speaker':<15} {'pyannote says':<18} "
        f"{'All speakers in window'}"
    )
    print("-" * 90)

    real_to_pyannote: dict[str, list[str]] = {}

    for timestamp_seconds, real_speaker in GROUND_TRUTH.items():
        ts_str = format_timestamp(timestamp_seconds)
        speakers = speakers_at_timestamp(diarization, timestamp_seconds)
        dominant = dominant_speaker(diarization, timestamp_seconds)

        all_speakers = ", ".join(
            f"{s.speaker}({s.overlap_with_target:.1f}s)" for s in speakers[:5]
        )
        if not speakers:
            all_speakers = "(no speakers in window)"

        print(
            f"{ts_str:<12} {real_speaker:<15} "
            f"{dominant or '?':<18} {all_speakers}"
        )

        if dominant:
            real_to_pyannote.setdefault(real_speaker, []).append(dominant)

    # ============ Section 3: Diagnosis ============
    print_section("3. Diagnosis")

    print("\nMapping real speaker → pyannote labels:")
    for real_speaker, pyannote_labels in real_to_pyannote.items():
        unique_labels = set(pyannote_labels)
        n_points = len(pyannote_labels)
        if len(unique_labels) == 1:
            verdict = "✓ CONSISTENT"
        else:
            verdict = (
                f"⚠ INCONSISTENT (split into {len(unique_labels)} labels)"
            )
        print(
            f"  {real_speaker:<10} → {pyannote_labels} ({n_points} points) "
            f"{verdict}"
        )

    # Specific Roman analysis — he appears 4 times in ground truth
    roman_labels = real_to_pyannote.get("Roman", [])
    if roman_labels:
        unique_roman = set(roman_labels)
        print(f"\nRoman analysis ({len(roman_labels)} ground truth points):")
        if len(unique_roman) == 1:
            print(
                f"  ✓ pyannote consistently attributes Roman to {unique_roman}"
            )
            print("    → diarization is WORKING (at least for the dominant speaker)")
        else:
            print(
                f"  ⚠ pyannote SPLITS Roman across {len(unique_roman)} labels: "
                f"{unique_roman}"
            )
            print(
                f"    → this explains over-segmentation "
                f"(10 pyannote labels vs 8 real participants)"
            )
            print("    → consider re-running with num_speakers=8")

    # Microsegment impact
    n_raw = len(raw_stats)
    n_filtered = len(filtered_stats)
    print("\nMicrosegment impact:")
    print(f"  Raw labels:       {n_raw}")
    print(f"  Filtered labels:  {n_filtered}")
    if n_filtered < n_raw:
        print(
            f"  → filtering eliminated {n_raw - n_filtered} labels "
            f"that only had microsegments"
        )

    # ============ Section 4: Verdict ============
    print_section("4. Verdict")

    n_real_speakers = 8  # from golden_protocol
    print(f"\nReal participants:        {n_real_speakers}")
    print(f"pyannote raw:             {n_raw}")
    print(f"pyannote filtered:        {n_filtered}")

    if abs(n_filtered - n_real_speakers) <= 1:
        print("\n✓ Speaker count is REASONABLE after filtering.")
    else:
        print(
            f"\n⚠ Speaker count is OFF by {abs(n_filtered - n_real_speakers)}. "
            f"Likely over- or under-segmentation."
        )

    if roman_labels and len(set(roman_labels)) == 1:
        print("✓ Roman (dominant speaker) attributed consistently.")
    elif roman_labels:
        print(
            f"⚠ Roman split into {len(set(roman_labels))} labels — "
            f"diarization quality is questionable."
        )


if __name__ == "__main__":
    main()