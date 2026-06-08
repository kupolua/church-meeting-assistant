"""Build voice profiles for team members from a baseline meeting recording.

Phase 1 of voice fingerprinting:
1. Run pyannote diarization on baseline audio (with embeddings)
2. Map each SPEAKER_XX → canonical name via speakers.json + name_aliases.json
3. Filter out artifacts (SPEAKER_05) and zero-vector embeddings
4. Save per-person profile as data/voice_profiles/{canonical_name}.npy

Each profile is a single 256-dimensional numpy array.

The output is cached. On rerun, only missing profiles are recomputed.

Usage:
    # Build all profiles from baseline
    uv run python -m church_assistant.build_voice_profiles

    # Custom audio
    uv run python -m church_assistant.build_voice_profiles --audio data/other.m4a

    # Force rebuild (ignore cached embeddings)
    uv run python -m church_assistant.build_voice_profiles --no-cache
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import time
from pathlib import Path

import numpy as np


# Defaults
DEFAULT_AUDIO = Path("data/test_baseline.m4a")
DEFAULT_SPEAKERS_MAP = Path("data/speakers.json")
DEFAULT_ALIASES = Path("data/name_aliases.json")
DEFAULT_PROFILES_DIR = Path("data/voice_profiles")
DEFAULT_EMBEDDINGS_CACHE = Path("data/test_baseline_embeddings.pkl")

# Labels to exclude (known artifacts)
EXCLUDE_SPEAKER_LABELS = frozenset({"SPEAKER_05"})

# Minimum norm for an embedding to be considered "real" (not zero vector)
MIN_EMBEDDING_NORM = 0.01


def load_env() -> None:
    """Load .env file (HUGGINGFACE_TOKEN expected there)."""
    env_file = Path(".env")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(
                key.strip(),
                value.strip().strip('"').strip("'"),
            )


def load_aliases(path: Path) -> dict[str, str]:
    """Load name aliases, ignoring underscore-prefixed comment keys."""
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return {k: v for k, v in data.items() if not k.startswith("_")}


def canonical_name(raw_name: str, aliases: dict[str, str]) -> str:
    """Map a raw name to its canonical form via aliases."""
    if raw_name in aliases:
        return aliases[raw_name]
    return raw_name


def is_valid_embedding(emb: np.ndarray) -> bool:
    """Check whether embedding is a real vector (not zero / NaN)."""
    if emb is None or emb.size == 0:
        return False
    if np.isnan(emb).any():
        return False
    norm = np.linalg.norm(emb)
    return norm >= MIN_EMBEDDING_NORM


def run_diarization_with_embeddings(
        audio_path: Path,
        token: str,
) -> tuple[dict[str, np.ndarray], dict[str, float]]:
    """Run pyannote on the audio. Return (label→embedding, label→speech_seconds).

    This is slow (~realtime on M1 CPU for full meetings). Cache the result
    via run_or_load_cached().
    """
    print(f"Loading pyannote pipeline (this may take ~30s)...")
    from pyannote.audio import Pipeline

    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1",
        token=token,
    )

    print(f"Running diarization on {audio_path}...")
    print(f"(this may take 1-2 hours on M1 CPU for 2-hour audio)")
    start = time.time()

    output = pipeline(str(audio_path))

    elapsed = time.time() - start
    print(f"✓ Diarization complete in {elapsed:.1f}s ({elapsed/60:.1f} min)")

    diarization = output.speaker_diarization
    embeddings_array = output.speaker_embeddings  # shape: (N, 256)

    # pyannote returns embeddings ordered by sorted speaker labels
    labels = sorted(diarization.labels())
    print(f"\nDiarization found {len(labels)} speakers: {labels}")
    print(f"Embeddings shape: {embeddings_array.shape}")

    if len(labels) != embeddings_array.shape[0]:
        raise RuntimeError(
            f"Mismatch: {len(labels)} labels vs {embeddings_array.shape[0]} embeddings"
        )

    # Build label → embedding map
    label_to_embedding: dict[str, np.ndarray] = {}
    for i, label in enumerate(labels):
        label_to_embedding[label] = embeddings_array[i].copy()

    # Compute speech time per label
    label_to_speech_sec: dict[str, float] = {}
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        label_to_speech_sec[speaker] = (
                label_to_speech_sec.get(speaker, 0.0) + turn.duration
        )

    return label_to_embedding, label_to_speech_sec


def run_or_load_cached(
        audio_path: Path,
        cache_path: Path,
        token: str,
        use_cache: bool = True,
) -> tuple[dict[str, np.ndarray], dict[str, float]]:
    """Cached wrapper around run_diarization_with_embeddings."""
    if use_cache and cache_path.exists():
        print(f"✓ Loading cached embeddings from {cache_path}")
        with cache_path.open("rb") as f:
            data = pickle.load(f)
        return data["embeddings"], data["speech_seconds"]

    embeddings, speech_seconds = run_diarization_with_embeddings(
        audio_path, token
    )

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with cache_path.open("wb") as f:
        pickle.dump(
            {
                "embeddings": embeddings,
                "speech_seconds": speech_seconds,
                "audio_path": str(audio_path),
            },
            f,
        )
    print(f"✓ Cached embeddings to {cache_path}")

    return embeddings, speech_seconds


def filename_safe(name: str) -> str:
    """Convert canonical name to filesystem-safe filename.

    'Роман Вечерківський' → 'Роман Вечерківський.npy'
    'Данило Кулаковський' → 'Данило Кулаковський.npy'

    Replaces only filesystem-reserved characters; keeps Ukrainian letters intact.
    """
    # Replace path separators and other unsafe chars with underscore
    unsafe = '/\\:*?"<>|'
    result = name
    for ch in unsafe:
        result = result.replace(ch, "_")
    return result.strip()


def build_profiles(
        label_to_embedding: dict[str, np.ndarray],
        label_to_speech_sec: dict[str, float],
        speakers_map: dict[str, str],
        aliases: dict[str, str],
        profiles_dir: Path,
        exclude_labels: frozenset[str] = EXCLUDE_SPEAKER_LABELS,
) -> dict[str, tuple[str, float, bool]]:
    """Build voice profiles and write them to disk.

    Returns:
        per-canonical summary: {canonical → (source_label, speech_sec, saved)}
    """
    profiles_dir.mkdir(parents=True, exist_ok=True)

    summary: dict[str, tuple[str, float, bool]] = {}

    for label, embedding in label_to_embedding.items():
        speech_sec = label_to_speech_sec.get(label, 0.0)

        # Excluded labels (artifacts)
        if label in exclude_labels:
            print(f"  ⊘ {label}: excluded (artifact label)")
            continue

        # Empty / zero embeddings
        if not is_valid_embedding(embedding):
            print(
                f"  ⊘ {label}: skipped (invalid embedding, "
                f"norm={np.linalg.norm(embedding):.4f})"
            )
            continue

        # Map to raw name
        raw_name = speakers_map.get(label)
        if raw_name is None:
            print(f"  ⚠ {label}: no entry in speakers.json — skipping")
            continue

        # Normalize to canonical name
        canonical = canonical_name(raw_name, aliases)

        # If we already saw this canonical (e.g., two speakers mapped to same person),
        # keep the one with more speech time
        if canonical in summary:
            prev_label, prev_speech, _ = summary[canonical]
            if speech_sec <= prev_speech:
                print(
                    f"  ⊘ {label} → {canonical}: already have "
                    f"{prev_label} with more speech ({prev_speech:.1f}s vs {speech_sec:.1f}s)"
                )
                continue
            else:
                print(
                    f"  ↻ {label} → {canonical}: replacing {prev_label} "
                    f"({prev_speech:.1f}s) with this one ({speech_sec:.1f}s)"
                )

        # Save
        profile_path = profiles_dir / f"{filename_safe(canonical)}.npy"
        np.save(profile_path, embedding)
        summary[canonical] = (label, speech_sec, True)
        print(
            f"  ✓ {label} → {canonical}: saved "
            f"({speech_sec:.1f}s speech, norm={np.linalg.norm(embedding):.3f})"
        )

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build voice profiles from baseline meeting audio"
    )
    parser.add_argument(
        "--audio",
        type=Path,
        default=DEFAULT_AUDIO,
        help=f"Audio file (default: {DEFAULT_AUDIO})",
    )
    parser.add_argument(
        "--speakers-map",
        type=Path,
        default=DEFAULT_SPEAKERS_MAP,
        help=f"SPEAKER_XX → name map (default: {DEFAULT_SPEAKERS_MAP})",
    )
    parser.add_argument(
        "--aliases",
        type=Path,
        default=DEFAULT_ALIASES,
        help=f"Name aliases (default: {DEFAULT_ALIASES})",
    )
    parser.add_argument(
        "--profiles-dir",
        type=Path,
        default=DEFAULT_PROFILES_DIR,
        help=f"Where to save profiles (default: {DEFAULT_PROFILES_DIR})",
    )
    parser.add_argument(
        "--embeddings-cache",
        type=Path,
        default=DEFAULT_EMBEDDINGS_CACHE,
        help=f"Cache file for embeddings (default: {DEFAULT_EMBEDDINGS_CACHE})",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Force re-running pyannote (slow!)",
    )
    args = parser.parse_args()

    # Validation
    if not args.audio.exists():
        print(f"❌ Audio not found: {args.audio}")
        raise SystemExit(1)

    if not args.speakers_map.exists():
        print(f"❌ Speakers map not found: {args.speakers_map}")
        raise SystemExit(1)

    # Load .env and grab token
    load_env()
    token = os.environ.get("HUGGINGFACE_TOKEN") or os.environ.get("HF_TOKEN")
    if not token:
        print("❌ HUGGINGFACE_TOKEN missing from .env / environment")
        raise SystemExit(1)
    print(f"✓ HF token loaded: {token[:8]}...")

    # Load mappings
    with args.speakers_map.open("r", encoding="utf-8") as f:
        speakers_map = json.load(f)
    print(f"✓ Speakers map: {len(speakers_map)} entries")

    aliases = load_aliases(args.aliases)
    print(f"✓ Aliases: {len(aliases)} entries")

    # Run (or load cached) diarization with embeddings
    embeddings, speech_seconds = run_or_load_cached(
        audio_path=args.audio,
        cache_path=args.embeddings_cache,
        token=token,
        use_cache=not args.no_cache,
    )

    # Build profiles
    print(f"\n{'=' * 70}")
    print(f"  Building voice profiles")
    print(f"{'=' * 70}")
    summary = build_profiles(
        label_to_embedding=embeddings,
        label_to_speech_sec=speech_seconds,
        speakers_map=speakers_map,
        aliases=aliases,
        profiles_dir=args.profiles_dir,
    )

    # Final summary
    print(f"\n{'=' * 70}")
    print(f"  Built {len(summary)} voice profiles")
    print(f"{'=' * 70}")
    for canonical, (label, speech, _) in sorted(summary.items()):
        print(f"  {canonical:<30} ← {label} ({speech:.1f}s speech)")

    print(f"\nProfiles saved in: {args.profiles_dir}/")
    print(f"Each profile is one .npy file with a 256-dim embedding.")


if __name__ == "__main__":
    main()