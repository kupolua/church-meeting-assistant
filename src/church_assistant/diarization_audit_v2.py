"""Audit pyannote diarization quality against expanded ground truth.

Now with 9 ground truth points covering more speakers, plus investigation
of SPEAKER_05 (which we suspect is an artifact, only 32s of speech total).

Usage:
    uv run python -m church_assistant.diarization_audit_v2
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from pyannote.core import Annotation
from pyannote.database.util import load_rttm


# Expanded ground truth — manual listening to test_baseline.m4a
# Format: timestamp_seconds → real_speaker_name
GROUND_TRUTH: dict[float, str] = {
    # Original 5 points (with Pasha point moved away from transition)
    8.0: "Pasha",          # 0:08 — Pasha continues after Roman's brief intro
    970.0: "Roman",        # 16:10 — Roman about Easter dates
    1710.0: "Roman",       # 28:30 — Roman about 5000 UAH for skit
    4440.0: "Chad",        # 1:14:00 — Chad about Anya Filatova in Argentina
    6600.0: "Roman",       # 1:50:00 — Roman about FOP and payments

    # New 4 points
    2430.0: "Bohdan",      # 0:40:30 — Tereshchenko Bohdan
    3483.0: "Yevhen",      # 0:58:03 — Butenko Yevhen
    3904.0: "Veniamin",    # 1:05:04 — Konovalov Veniamin (joined later!)
    4267.0: "Vyacheslav",  # 1:11:07 — Konovalov Vyacheslav
}

# Window size for finding speakers around a timestamp
WINDOW_SECONDS = 5.0

# Microsegment threshold
MICROSEGMENT_THRESHOLD = 0.5

# Suspect label to investigate
SUSPECT_LABEL = "SPEAKER_05"


@dataclass
class SpeakerAtTimestamp:
    speaker: str
    start: float
    end: float
    overlap_with_target: float


def speakers_at_timestamp(
        diarization: Annotation,
        target_t: float,
        window: float = WINDOW_SECONDS,
) -> list[SpeakerAtTimestamp]:
    """All speakers active within ±window seconds, sorted by overlap (desc)."""
    window_start = target_t - window
    window_end = target_t + window

    found: list[SpeakerAtTimestamp] = []
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        if turn.end < window_start or turn.start > window_end:
            continue
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
    found.sort(key=lambda s: s.overlap_with_target, reverse=True)
    return found


def dominant_speaker_by_total_overlap(
        diarization: Annotation,
        target_t: float,
        window: float = WINDOW_SECONDS,
) -> str | None:
    """Sum overlap by speaker, return the one with the highest total."""
    speakers = speakers_at_timestamp(diarization, target_t, window)
    if not speakers:
        return None
    totals: Counter[str] = Counter()
    for s in speakers:
        totals[s.speaker] += s.overlap_with_target
    return totals.most_common(1)[0][0]


def find_speaker_segments(
        diarization: Annotation,
        target_speaker: str,
) -> list[tuple[float, float, float]]:
    """All segments where target_speaker is active.

    Returns: list of (start, end, duration), sorted by start time.
    """
    segments = []
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        if speaker == target_speaker:
            segments.append((turn.start, turn.end, turn.duration))
    segments.sort(key=lambda s: s[0])
    return segments


def format_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def print_section(title: str) -> None:
    print(f"\n{'=' * 75}")
    print(f"  {title}")
    print(f"{'=' * 75}")


def main() -> None:
    # Load RTTM cache
    rttm_path = Path("data/test_baseline.rttm")
    if not rttm_path.exists():
        print(f"❌ RTTM not found: {rttm_path}")
        return

    print(f"Loading diarization from {rttm_path}...")
    rttm_dict = load_rttm(str(rttm_path))
    diarization = next(iter(rttm_dict.values()))
    print(f"✓ Loaded {len(list(diarization.itertracks()))} segments")

    # ============ Section 1: Ground truth → SPEAKER_XX mapping ============
    print_section("1. Expanded ground truth check (9 points, 6 speakers)")

    print(
        f"\n{'Timestamp':<12} {'Real speaker':<14} {'pyannote dominant':<22} "
        f"{'Top speakers in window (sec)'}"
    )
    print("-" * 100)

    real_to_pyannote: dict[str, list[str]] = {}

    for timestamp_s, real_speaker in sorted(GROUND_TRUTH.items()):
        ts_str = format_timestamp(timestamp_s)
        speakers = speakers_at_timestamp(diarization, timestamp_s)
        dominant = dominant_speaker_by_total_overlap(diarization, timestamp_s)

        # Aggregate by speaker for readable output
        totals: Counter[str] = Counter()
        for s in speakers:
            totals[s.speaker] += s.overlap_with_target
        top_summary = ", ".join(
            f"{spk}({dur:.1f}s)" for spk, dur in totals.most_common(4)
        )
        if not totals:
            top_summary = "(no speakers in window)"

        print(
            f"{ts_str:<12} {real_speaker:<14} {dominant or '?':<22} {top_summary}"
        )

        if dominant:
            real_to_pyannote.setdefault(real_speaker, []).append(dominant)

    # ============ Section 2: Mapping summary ============
    print_section("2. SPEAKER_XX → real name mapping")

    print()
    confirmed_mappings: dict[str, str] = {}
    for real_speaker, labels in sorted(real_to_pyannote.items()):
        unique = set(labels)
        n = len(labels)
        if len(unique) == 1:
            label = labels[0]
            mark = "✓"
            note = f"({n} point{'s' if n > 1 else ''})"
            confirmed_mappings[label] = real_speaker
        else:
            label = " or ".join(sorted(unique))
            mark = "⚠"
            note = f"({n} points, split across labels)"
        print(f"  {mark}  {real_speaker:<14} → {label:<25} {note}")

    # Unidentified labels
    print("\nLabels NOT yet identified (still mystery):")
    all_labels_in_diar = set()
    for _, _, speaker in diarization.itertracks(yield_label=True):
        all_labels_in_diar.add(speaker)
    unidentified = all_labels_in_diar - set(confirmed_mappings.keys())
    for label in sorted(unidentified):
        # Calculate this speaker's total time
        total = sum(
            d for _, _, d in find_speaker_segments(diarization, label)
        )
        print(f"  ?  {label:<25} ({total:.1f}s total, {total/60:.1f} min)")

    # ============ Section 3: SPEAKER_05 mystery ============
    print_section(f"3. {SUSPECT_LABEL} mystery — when does it appear?")

    suspect_segments = find_speaker_segments(diarization, SUSPECT_LABEL)
    total_duration = sum(s[2] for s in suspect_segments)

    print(f"\n{SUSPECT_LABEL}: {len(suspect_segments)} segments, "
          f"{total_duration:.1f}s total ({total_duration/60:.2f} min)")
    print(f"\nAll occurrences of {SUSPECT_LABEL} (sorted by time):")
    print(f"{'#':>3} {'Start':<10} {'End':<10} {'Duration':>10}")
    print("-" * 40)

    for i, (start, end, dur) in enumerate(suspect_segments, 1):
        print(
            f"{i:>3} {format_timestamp(start):<10} "
            f"{format_timestamp(end):<10} {dur:>8.2f}s"
        )

    # Group by adjacency — find clusters
    print(f"\n{SUSPECT_LABEL} clusters (segments within 30s of each other):")
    if suspect_segments:
        clusters = [[suspect_segments[0]]]
        for seg in suspect_segments[1:]:
            last_end = clusters[-1][-1][1]
            if seg[0] - last_end < 30:
                clusters[-1].append(seg)
            else:
                clusters.append([seg])

        for i, cluster in enumerate(clusters, 1):
            cluster_start = cluster[0][0]
            cluster_end = cluster[-1][1]
            cluster_total = sum(s[2] for s in cluster)
            print(
                f"  Cluster {i}: {format_timestamp(cluster_start)} – "
                f"{format_timestamp(cluster_end)}  "
                f"({len(cluster)} segments, {cluster_total:.1f}s total)"
            )

    # ============ Section 4: Final assessment ============
    print_section("4. Updated verdict")

    n_real = 9  # corrected: Veniamin joined later
    n_pyannote = len(all_labels_in_diar)
    n_confirmed = len(confirmed_mappings)
    n_remaining_mystery = len(unidentified)

    print(f"\nReal participants:                  {n_real}")
    print(f"pyannote labels (total):            {n_pyannote}")
    print(f"  - confirmed via ground truth:     {n_confirmed}")
    print(f"  - still unidentified:             {n_remaining_mystery}")
    print(f"\nDifference (pyannote - real):       {n_pyannote - n_real:+d}")

    if abs(n_pyannote - n_real) <= 1:
        print("\n✓ Speaker count is REASONABLE.")
    elif n_pyannote - n_real > 0:
        print(
            f"\n⚠ pyannote produced {n_pyannote - n_real} EXTRA labels — "
            f"likely over-segmentation."
        )
    else:
        print(
            f"\n⚠ pyannote MISSED {n_real - n_pyannote} speakers — "
            f"likely under-segmentation."
        )

    # Roman consistency check
    roman_labels = real_to_pyannote.get("Roman", [])
    if roman_labels and len(set(roman_labels)) == 1:
        print("✓ Roman (dominant speaker) attributed consistently.")

    # Recommendation
    print("\nNext steps to investigate:")
    if SUSPECT_LABEL in unidentified:
        print(
            f"  1. Listen to {SUSPECT_LABEL} clusters above — confirm if "
            f"artifact or real person (possibly Veniamin?)"
        )
    if n_remaining_mystery > 1:
        print(
            f"  2. Add ground truth for {n_remaining_mystery - 1} more "
            f"timestamps to map remaining labels"
        )


if __name__ == "__main__":
    main()