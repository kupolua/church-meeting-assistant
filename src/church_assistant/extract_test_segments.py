"""Extract short audio segments from a meeting recording for embedding tests.

Reads ground-truth ranges (who spoke when), uses ffmpeg to extract each
range into a separate .wav file in data/test_segments/.

These small files (seconds-to-minutes long) are then fed into
test_segment_matching.py to verify voice profile matching.

Usage:
    uv run python -m church_assistant.extract_test_segments
"""

from __future__ import annotations

import argparse
import subprocess
from dataclasses import dataclass
from pathlib import Path


DEFAULT_AUDIO = Path("data/audio1438994435.m4a")
DEFAULT_OUTPUT_DIR = Path("data/test_segments")


@dataclass(frozen=True)
class TestSegment:
    """One labeled segment of audio to extract."""

    expected_person: str         # canonical name (matches voice_profiles/{name}.npy)
    start: str                   # HH:MM:SS
    end: str                     # HH:MM:SS

    @property
    def safe_filename(self) -> str:
        """Filesystem-safe slug for output file."""
        person_slug = self.expected_person.replace(" ", "_")
        start_slug = self.start.replace(":", "")
        return f"{person_slug}_{start_slug}.wav"


# Ground-truth segments for data/audio1438994435.m4a (meeting from 01/06/2026)
# These are moments where Pavlo confirmed who is speaking.
GROUND_TRUTH = [
    TestSegment("Роман Вечерківський",  "00:01:08", "00:07:25"),
    TestSegment("Павло Кулаковський",   "00:00:00", "00:01:01"),
    TestSegment("Євген Бутенко",        "00:54:05", "00:54:28"),
    TestSegment("Богдан Терещенко",     "01:37:40", "01:38:01"),
    TestSegment("Вячеслав Коновалов",   "00:24:38", "00:25:54"),
]


def time_to_seconds(t: str) -> int:
    """Convert HH:MM:SS to total seconds."""
    parts = t.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    raise ValueError(f"Bad time format: {t}")


def extract_segment(
    source_audio: Path,
    segment: TestSegment,
    output_dir: Path,
) -> Path:
    """Extract one segment with ffmpeg. Returns the output path."""
    duration_sec = time_to_seconds(segment.end) - time_to_seconds(segment.start)
    output_path = output_dir / segment.safe_filename

    # ffmpeg command:
    # -ss start position, -t duration
    # -ac 1: mono (pyannote prefers mono)
    # -ar 16000: 16kHz sample rate (pyannote standard)
    # -y: overwrite without asking
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(source_audio),
        "-ss", segment.start,
        "-t", str(duration_sec),
        "-ac", "1",
        "-ar", "16000",
        "-vn",                # no video stream
        str(output_path),
    ]

    print(f"  Extracting {segment.expected_person:<30} "
          f"({segment.start}–{segment.end}, {duration_sec}s)...")

    result = subprocess.run(
        cmd, capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        print(f"  ❌ ffmpeg failed:\n{result.stderr[:500]}")
        raise RuntimeError(f"ffmpeg failed for {segment.expected_person}")

    size_kb = output_path.stat().st_size / 1024
    print(f"  ✓ Saved {output_path.name} ({size_kb:.1f} KB)")
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract test segments for voice profile matching",
    )
    parser.add_argument(
        "--audio",
        type=Path,
        default=DEFAULT_AUDIO,
        help=f"Source meeting audio (default: {DEFAULT_AUDIO})",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output directory for segments (default: {DEFAULT_OUTPUT_DIR})",
    )
    args = parser.parse_args()

    if not args.audio.exists():
        print(f"❌ Audio not found: {args.audio}")
        raise SystemExit(1)

    args.output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Source audio: {args.audio}")
    print(f"Output dir:   {args.output_dir}")
    print(f"\nExtracting {len(GROUND_TRUTH)} segments:\n")

    for segment in GROUND_TRUTH:
        extract_segment(args.audio, segment, args.output_dir)

    print(f"\n{'=' * 70}")
    print(f"  Done. {len(GROUND_TRUTH)} segments saved in {args.output_dir}/")
    print(f"{'=' * 70}")
    print(f"\nNext: uv run python -m church_assistant.test_segment_matching")


if __name__ == "__main__":
    main()
