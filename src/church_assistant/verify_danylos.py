"""Verify the last two SPEAKER_XX mappings: Danik vs Danylo Ponomariov.

Quick standalone check before we commit to the full mapping table.

Usage:
    uv run python -m church_assistant.verify_danylos
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from pyannote.core import Annotation
from pyannote.database.util import load_rttm


# New ground truth points (timestamps in seconds)
NEW_POINTS: dict[float, str] = {
    # 0:31:58 — Danik fixed his microphone here
    1918.0: "Danik (Kulakovskyi)",
    # 0:59:19 — Danylo Ponomariov speaks
    3559.0: "Danylo (Ponomariov)",
}

# Use a generous window because Danik fixing the mic might have some setup time
WINDOW_SECONDS = 8.0


def format_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def main() -> None:
    rttm_path = Path("data/test_baseline.rttm")
    if not rttm_path.exists():
        print(f"❌ RTTM not found: {rttm_path}")
        return

    rttm_dict = load_rttm(str(rttm_path))
    diarization: Annotation = next(iter(rttm_dict.values()))

    print("=" * 70)
    print("Verifying mappings for Danik and Danylo Ponomariov")
    print("=" * 70)

    for ts_seconds, real_name in sorted(NEW_POINTS.items()):
        ts_str = format_timestamp(ts_seconds)
        window_start = ts_seconds - WINDOW_SECONDS
        window_end = ts_seconds + WINDOW_SECONDS

        # Aggregate overlap per speaker in this window
        totals: Counter[str] = Counter()
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            if turn.end < window_start or turn.start > window_end:
                continue
            overlap_start = max(turn.start, window_start)
            overlap_end = min(turn.end, window_end)
            overlap = overlap_end - overlap_start
            totals[speaker] += overlap

        print(f"\n{ts_str}  →  {real_name}")
        print(f"  ±{WINDOW_SECONDS:.0f}s window: {format_timestamp(window_start)} to {format_timestamp(window_end)}")
        print(f"  Speakers active (sorted by total overlap):")
        if not totals:
            print("    (no speakers in window!)")
            continue
        for speaker, overlap in totals.most_common():
            print(f"    {speaker}: {overlap:.1f}s")

        dominant = totals.most_common(1)[0][0]
        print(f"  → Dominant: {dominant}")

    print("\n" + "=" * 70)
    print("Expected (per our hypothesis):")
    print("  Danik (Kulakovskyi)     should be SPEAKER_09")
    print("  Danylo (Ponomariov)     should be SPEAKER_07")
    print("=" * 70)


if __name__ == "__main__":
    main()