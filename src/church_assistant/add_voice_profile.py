"""Add a single voice profile from a cached embeddings file.

Use case: someone new appears in a meeting, the matcher correctly reports
"no match", and you want to save their voice signature for future meetings.

Workflow:
    1. match_speakers.py runs pyannote on a new audio file, caches embeddings
       in data/{audio_stem}_embeddings.pkl.
    2. You manually identify which SPEAKER_XX is the new person (by listening
       to their RTTM segments, by speech time, or by ground truth knowledge).
    3. Run this script to extract that label's embedding and save it as
       data/voice_profiles/{canonical_name}.npy.
    4. On subsequent meetings, this person will be recognized as a strong match.

Usage:
    uv run python -m church_assistant.add_voice_profile \\
        --embeddings-cache data/audio1512988623_embeddings.pkl \\
        --speaker-label SPEAKER_00 \\
        --name "Володимир Вальченко"

    # Preview without saving
    uv run python -m church_assistant.add_voice_profile \\
        --embeddings-cache data/audio1512988623_embeddings.pkl \\
        --speaker-label SPEAKER_00 \\
        --name "Володимир Вальченко" \\
        --dry-run
"""

from __future__ import annotations

import argparse
import pickle
from pathlib import Path

import numpy as np


DEFAULT_PROFILES_DIR = Path("data/voice_profiles")
MIN_EMBEDDING_NORM = 0.01


def filename_safe(name: str) -> str:
    """Convert canonical name to filesystem-safe filename.

    Replaces path-reserved characters; keeps Ukrainian letters intact.
    """
    unsafe = '/\\:*?"<>|'
    result = name
    for ch in unsafe:
        result = result.replace(ch, "_")
    return result.strip()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add a voice profile from cached embeddings"
    )
    parser.add_argument(
        "--embeddings-cache",
        type=Path,
        required=True,
        help="Path to embeddings pickle (data/{audio_stem}_embeddings.pkl)",
    )
    parser.add_argument(
        "--speaker-label",
        type=str,
        required=True,
        help="SPEAKER_XX label to extract (e.g. SPEAKER_00)",
    )
    parser.add_argument(
        "--name",
        type=str,
        required=True,
        help="Canonical name to save as (e.g. 'Володимир Вальченко')",
    )
    parser.add_argument(
        "--profiles-dir",
        type=Path,
        default=DEFAULT_PROFILES_DIR,
        help=f"Voice profiles directory (default: {DEFAULT_PROFILES_DIR})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be saved, don't write file",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing profile if present",
    )
    args = parser.parse_args()

    # Validate inputs
    if not args.embeddings_cache.exists():
        print(f"❌ Embeddings cache not found: {args.embeddings_cache}")
        raise SystemExit(1)

    # Load cached embeddings
    print(f"Loading embeddings from {args.embeddings_cache}")
    with args.embeddings_cache.open("rb") as f:
        data = pickle.load(f)

    embeddings: dict[str, np.ndarray] = data["embeddings"]
    speech_seconds: dict[str, float] = data.get("speech_seconds", {})
    source_audio = data.get("audio_path", "unknown")

    print(f"  Source audio: {source_audio}")
    print(f"  Speakers in cache: {sorted(embeddings.keys())}")

    # Locate the requested label
    if args.speaker_label not in embeddings:
        print(f"\n❌ Speaker label '{args.speaker_label}' not in cache")
        print(f"   Available: {sorted(embeddings.keys())}")
        raise SystemExit(1)

    embedding = embeddings[args.speaker_label]
    speech = speech_seconds.get(args.speaker_label, 0.0)
    norm = float(np.linalg.norm(embedding))

    print(f"\nExtracting profile for {args.speaker_label}:")
    print(f"  Embedding shape: {embedding.shape}")
    print(f"  Norm:            {norm:.4f}")
    print(f"  Speech time:     {speech:.1f}s")
    print(f"  Canonical name:  {args.name}")

    # Validate
    if norm < MIN_EMBEDDING_NORM:
        print(f"\n❌ Embedding norm ({norm:.4f}) is below {MIN_EMBEDDING_NORM} —")
        print(f"   this looks like an artifact (zero-vector). Refusing to save.")
        raise SystemExit(1)

    if np.isnan(embedding).any() or np.isinf(embedding).any():
        print(f"\n❌ Embedding contains NaN/Inf — refusing to save.")
        raise SystemExit(1)

    # Determine output path
    args.profiles_dir.mkdir(parents=True, exist_ok=True)
    output_path = args.profiles_dir / f"{filename_safe(args.name)}.npy"

    print(f"\nOutput path: {output_path}")

    if output_path.exists() and not args.force:
        print(f"\n⚠ Profile already exists: {output_path}")
        print(f"  Use --force to overwrite.")
        raise SystemExit(1)

    if args.dry_run:
        print(f"\n✓ Dry-run: would save embedding ({embedding.nbytes} bytes)")
        return

    # Save
    np.save(output_path, embedding)
    print(f"\n✓ Saved profile: {output_path}")
    print(f"\nVerification:")
    loaded = np.load(output_path)
    if np.allclose(loaded, embedding):
        print(f"  ✓ File integrity check passed")
    else:
        print(f"  ❌ WARNING: file does not match in-memory embedding!")


if __name__ == "__main__":
    main()
