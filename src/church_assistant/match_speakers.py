"""Auto-generate speakers.json for a new meeting via voice fingerprint matching.

Pipeline:
1. Load all baseline voice profiles from data/voice_profiles/*.npy
2. Run pyannote on new audio (or load cached embeddings)
3. For each SPEAKER_XX in the new meeting:
   - Compute cosine similarity with all 9 baseline profiles
   - Strong match (sim > STRONG_THRESHOLD): assign canonical name directly
   - Weak match (WEAK_THRESHOLD < sim ≤ STRONG): assign with [WEAK: sim=...,
     runner-up=...] flag
   - No match (sim ≤ WEAK_THRESHOLD): keep as SPEAKER_XX (new participant)
4. Skip SPEAKER labels with invalid embeddings (zero vectors)
5. Write JSON output ready for merge_transcript.py

Empirically validated thresholds (from test_segment_matching.py on 23/02 baseline
vs 01/06 meeting, 5/5 accuracy):
- STRONG_THRESHOLD = 0.7 (correct matches were 0.76-0.83)
- WEAK_THRESHOLD = 0.5 (runner-ups were 0.41-0.59)

Usage:
    # First run on a new audio — does full pyannote pass (~2 hours)
    uv run python -m church_assistant.match_speakers \\
        --audio data/new_meeting.m4a \\
        --output data/new_meeting_speakers.json

    # Subsequent runs reuse cached embeddings (instant)
    uv run python -m church_assistant.match_speakers \\
        --audio data/new_meeting.m4a \\
        --output data/new_meeting_speakers.json

    # Force re-running pyannote
    uv run python -m church_assistant.match_speakers \\
        --audio data/new_meeting.m4a \\
        --output data/new_meeting_speakers.json \\
        --no-cache

    # Custom thresholds
    uv run python -m church_assistant.match_speakers \\
        --audio data/new_meeting.m4a \\
        --output data/new_meeting_speakers.json \\
        --strong-threshold 0.75 --weak-threshold 0.55
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np


# Defaults
DEFAULT_PROFILES_DIR = Path("data/voice_profiles")

# Empirically validated thresholds (see test_segment_matching.py results)
STRONG_THRESHOLD = 0.7
WEAK_THRESHOLD = 0.5

# Minimum L2 norm for an embedding to be considered "real"
MIN_EMBEDDING_NORM = 0.01


@dataclass
class MatchOutcome:
    """Result of matching one SPEAKER_XX against baseline profiles."""

    speaker_label: str           # e.g. "SPEAKER_03"
    assigned_name: str           # canonical name OR "SPEAKER_03" if no match
    status: str                  # "strong" / "weak" / "no_match" / "invalid_embedding"
    best_match: str | None = None
    best_sim: float | None = None
    runner_up: str | None = None
    runner_up_sim: float | None = None


def load_env() -> None:
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


def load_profiles(profiles_dir: Path) -> dict[str, np.ndarray]:
    """Load voice profiles. Returns {canonical_name → embedding}."""
    profiles: dict[str, np.ndarray] = {}
    for path in sorted(profiles_dir.glob("*.npy")):
        profiles[path.stem] = np.load(path)
    return profiles


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def is_valid_embedding(emb: np.ndarray) -> bool:
    """Check that embedding is non-zero and finite."""
    if emb is None or emb.size == 0:
        return False
    if np.isnan(emb).any() or np.isinf(emb).any():
        return False
    return np.linalg.norm(emb) >= MIN_EMBEDDING_NORM


def run_diarization_with_embeddings(
    audio_path: Path,
    token: str,
    rttm_output_path: Path | None = None,
) -> tuple[dict[str, np.ndarray], dict[str, float]]:
    """Run pyannote on audio. Returns (label→embedding, label→speech_seconds).

    Slow (~realtime on M1 CPU). Cache via run_or_load_cached().

    If rttm_output_path is provided, also writes the diarization as RTTM
    so downstream tools (merge_transcript.py, etc.) can use it.
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
    embeddings_array = output.speaker_embeddings

    # Save RTTM if requested
    if rttm_output_path is not None:
        rttm_output_path.parent.mkdir(parents=True, exist_ok=True)
        with rttm_output_path.open("w", encoding="utf-8") as f:
            diarization.write_rttm(f)
        print(f"✓ Saved RTTM: {rttm_output_path}")

    labels = sorted(diarization.labels())
    if len(labels) != embeddings_array.shape[0]:
        raise RuntimeError(
            f"Mismatch: {len(labels)} labels vs {embeddings_array.shape[0]} embeddings"
        )

    label_to_embedding: dict[str, np.ndarray] = {}
    for i, label in enumerate(labels):
        label_to_embedding[label] = embeddings_array[i].copy()

    label_to_speech_sec: dict[str, float] = {}
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        label_to_speech_sec[speaker] = (
            label_to_speech_sec.get(speaker, 0.0) + turn.duration
        )

    return label_to_embedding, label_to_speech_sec


def embeddings_cache_path(audio_path: Path) -> Path:
    """Derive cache path from audio file path.

    data/some_audio.m4a → data/some_audio_embeddings.pkl
    """
    stem = audio_path.stem
    return audio_path.parent / f"{stem}_embeddings.pkl"


def run_or_load_cached(
    audio_path: Path,
    token: str,
    use_cache: bool = True,
    rttm_output_path: Path | None = None,
) -> tuple[dict[str, np.ndarray], dict[str, float]]:
    """Cached wrapper around diarization.

    If rttm_output_path is provided AND the RTTM file does not exist yet,
    a fresh diarization will be triggered even if the embeddings cache exists.
    This avoids re-running pyannote when only embeddings are needed.
    """
    cache_path = embeddings_cache_path(audio_path)

    # If we have embeddings cache AND either we don't need RTTM, or RTTM
    # already exists, use the cache.
    rttm_already_exists = (
        rttm_output_path is None or rttm_output_path.exists()
    )

    if use_cache and cache_path.exists() and rttm_already_exists:
        print(f"✓ Loading cached embeddings from {cache_path}")
        with cache_path.open("rb") as f:
            data = pickle.load(f)
        return data["embeddings"], data["speech_seconds"]

    if use_cache and cache_path.exists() and not rttm_already_exists:
        print(
            f"⚠ Embeddings cache exists at {cache_path}, but RTTM "
            f"({rttm_output_path}) is missing. Will re-run pyannote to generate RTTM."
        )

    embeddings, speech_seconds = run_diarization_with_embeddings(
        audio_path, token, rttm_output_path=rttm_output_path,
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


def match_one_speaker(
    label: str,
    embedding: np.ndarray,
    profiles: dict[str, np.ndarray],
    strong_threshold: float,
    weak_threshold: float,
) -> MatchOutcome:
    """Match a single SPEAKER_XX label to a canonical name (or no match)."""
    if not is_valid_embedding(embedding):
        return MatchOutcome(
            speaker_label=label,
            assigned_name=label,
            status="invalid_embedding",
        )

    sims: list[tuple[str, float]] = []
    for name, profile in profiles.items():
        sims.append((name, cosine_similarity(embedding, profile)))
    sims.sort(key=lambda x: x[1], reverse=True)

    best_name, best_sim = sims[0]
    runner_name, runner_sim = sims[1] if len(sims) > 1 else (None, None)

    if best_sim >= strong_threshold:
        return MatchOutcome(
            speaker_label=label,
            assigned_name=best_name,
            status="strong",
            best_match=best_name,
            best_sim=best_sim,
            runner_up=runner_name,
            runner_up_sim=runner_sim,
        )
    elif best_sim >= weak_threshold:
        return MatchOutcome(
            speaker_label=label,
            assigned_name=best_name,
            status="weak",
            best_match=best_name,
            best_sim=best_sim,
            runner_up=runner_name,
            runner_up_sim=runner_sim,
        )
    else:
        return MatchOutcome(
            speaker_label=label,
            assigned_name=label,
            status="no_match",
            best_match=best_name,
            best_sim=best_sim,
            runner_up=runner_name,
            runner_up_sim=runner_sim,
        )


def build_speakers_json(
    outcomes: list[MatchOutcome],
    audio_path: Path,
    strong_threshold: float,
    weak_threshold: float,
) -> dict:
    """Build the JSON document for speakers mapping with metadata."""
    result: dict = {}

    # Meta header (sorted to come first when serialized)
    result["_meta"] = {
        "audio_file": str(audio_path),
        "strong_threshold": strong_threshold,
        "weak_threshold": weak_threshold,
        "needs_review": [
            o.speaker_label for o in outcomes if o.status == "weak"
        ],
        "no_match": [
            o.speaker_label for o in outcomes if o.status == "no_match"
        ],
        "invalid_embedding": [
            o.speaker_label for o in outcomes if o.status == "invalid_embedding"
        ],
    }

    # Strong matches: clean assignment
    # Weak matches: assigned with [REVIEW] marker
    # No match / invalid: keep SPEAKER_XX
    for o in outcomes:
        if o.status == "strong":
            result[o.speaker_label] = o.assigned_name
        elif o.status == "weak":
            # Option C from our discussion: simple [REVIEW] marker
            result[o.speaker_label] = f"{o.assigned_name} [REVIEW]"
        else:
            result[o.speaker_label] = o.speaker_label

    return result


def print_summary(
    outcomes: list[MatchOutcome],
    speech_seconds: dict[str, float],
) -> None:
    """Print a human-readable summary of matching results."""
    print(f"\n{'=' * 95}")
    print(f"  Matching results")
    print(f"{'=' * 95}\n")

    # Sort by speech time descending — most important speakers first
    outcomes_sorted = sorted(
        outcomes,
        key=lambda o: speech_seconds.get(o.speaker_label, 0.0),
        reverse=True,
    )

    for o in outcomes_sorted:
        speech_sec = speech_seconds.get(o.speaker_label, 0.0)

        if o.status == "strong":
            marker = "✓"
            sim_info = f"sim={o.best_sim:.3f}"
            detail = f"{marker} {o.speaker_label} → {o.assigned_name:<28} {sim_info}"
        elif o.status == "weak":
            marker = "⚠"
            sim_info = f"sim={o.best_sim:.3f}, runner={o.runner_up} {o.runner_up_sim:.3f}"
            detail = (
                f"{marker} {o.speaker_label} → {o.assigned_name + ' [REVIEW]':<35} {sim_info}"
            )
        elif o.status == "no_match":
            marker = "✗"
            detail = (
                f"{marker} {o.speaker_label} → (no match)"
                f"                                 best={o.best_match} {o.best_sim:.3f}"
            )
        else:  # invalid_embedding
            marker = "○"
            detail = f"{marker} {o.speaker_label} → (invalid embedding — likely artifact)"

        speech_str = f"{speech_sec:.1f}s speech"
        print(f"  {detail}  ({speech_str})")

    # Counts
    n_strong = sum(1 for o in outcomes if o.status == "strong")
    n_weak = sum(1 for o in outcomes if o.status == "weak")
    n_no_match = sum(1 for o in outcomes if o.status == "no_match")
    n_invalid = sum(1 for o in outcomes if o.status == "invalid_embedding")

    print(f"\n  Summary: {n_strong} strong, {n_weak} weak, "
          f"{n_no_match} no-match, {n_invalid} invalid")

    if n_weak > 0:
        print(f"\n  ⚠ {n_weak} weak matches need review.")
        print(f"  These are tagged with [REVIEW] in the output JSON.")
    if n_no_match > 0:
        print(f"\n  ✗ {n_no_match} speakers have no match — possibly new participants.")
        print(f"  Add them manually to the output JSON if you recognize them.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Auto-match speakers in a new meeting to baseline voice profiles"
    )
    parser.add_argument(
        "--audio",
        type=Path,
        required=True,
        help="Path to new meeting audio file",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output speakers.json path",
    )
    parser.add_argument(
        "--profiles-dir",
        type=Path,
        default=DEFAULT_PROFILES_DIR,
        help=f"Voice profiles directory (default: {DEFAULT_PROFILES_DIR})",
    )
    parser.add_argument(
        "--strong-threshold",
        type=float,
        default=STRONG_THRESHOLD,
        help=f"Cosine sim threshold for strong match (default: {STRONG_THRESHOLD})",
    )
    parser.add_argument(
        "--weak-threshold",
        type=float,
        default=WEAK_THRESHOLD,
        help=f"Cosine sim threshold for weak match (default: {WEAK_THRESHOLD})",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Force re-running pyannote (slow!)",
    )
    parser.add_argument(
        "--rttm",
        type=Path,
        default=None,
        help="Output RTTM path. If omitted, defaults to data/{audio_stem}.rttm. "
             "If RTTM does not exist but embeddings cache does, pyannote will rerun.",
    )
    args = parser.parse_args()

    if not args.audio.exists():
        print(f"❌ Audio not found: {args.audio}")
        raise SystemExit(1)

    if not args.profiles_dir.exists():
        print(f"❌ Profiles dir not found: {args.profiles_dir}")
        print(f"Run build_voice_profiles.py first.")
        raise SystemExit(1)

    # Validate thresholds
    if args.weak_threshold >= args.strong_threshold:
        print(
            f"❌ weak threshold ({args.weak_threshold}) must be < "
            f"strong threshold ({args.strong_threshold})"
        )
        raise SystemExit(1)

    # Load profiles
    profiles = load_profiles(args.profiles_dir)
    if not profiles:
        print(f"❌ No profiles in {args.profiles_dir}")
        raise SystemExit(1)
    print(f"✓ Loaded {len(profiles)} voice profiles")
    for name in sorted(profiles.keys()):
        print(f"    - {name}")

    # Load env and token
    load_env()
    token = os.environ.get("HUGGINGFACE_TOKEN") or os.environ.get("HF_TOKEN")
    if not token:
        print("❌ HUGGINGFACE_TOKEN missing")
        raise SystemExit(1)

    # Get embeddings (cached or fresh)
    # Determine RTTM output path
    if args.rttm is not None:
        rttm_path = args.rttm
    else:
        # Default: data/{audio_stem}.rttm
        rttm_path = args.audio.parent / f"{args.audio.stem}.rttm"

    print(f"\nProcessing audio: {args.audio}")
    print(f"Embeddings cache: {embeddings_cache_path(args.audio)}")
    print(f"RTTM output:      {rttm_path}")
    embeddings, speech_seconds = run_or_load_cached(
        audio_path=args.audio,
        token=token,
        use_cache=not args.no_cache,
        rttm_output_path=rttm_path,
    )
    print(f"\n✓ {len(embeddings)} speaker labels in this audio")

    # Match each label
    outcomes: list[MatchOutcome] = []
    for label, emb in embeddings.items():
        outcome = match_one_speaker(
            label=label,
            embedding=emb,
            profiles=profiles,
            strong_threshold=args.strong_threshold,
            weak_threshold=args.weak_threshold,
        )
        outcomes.append(outcome)

    # Print summary
    print_summary(outcomes, speech_seconds)

    # Build and write JSON
    output_json = build_speakers_json(
        outcomes=outcomes,
        audio_path=args.audio,
        strong_threshold=args.strong_threshold,
        weak_threshold=args.weak_threshold,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        json.dump(output_json, f, ensure_ascii=False, indent=2)

    print(f"\n✓ Saved speakers map: {args.output}")
    print(f"\nNext steps:")
    print(f"  1. Review [REVIEW]-tagged entries in {args.output}")
    print(f"  2. Run merge_transcript.py with --speakers-map {args.output}")


if __name__ == "__main__":
    main()
