"""Test voice profile matching on extracted test segments.

For each test segment:
1. Load the audio
2. Compute its embedding via pyannote's standalone embedding model
3. Compare with all baseline profiles via cosine similarity
4. Print best match + runner-up + ground-truth comparison

This is the empirical test that decides whether voice fingerprints
work reliably across recordings ~3 months apart.

Usage:
    uv run python -m church_assistant.test_segment_matching
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np


DEFAULT_SEGMENTS_DIR = Path("data/test_segments")
DEFAULT_PROFILES_DIR = Path("data/voice_profiles")


# Ground-truth: filename → expected person
# (Filenames match what extract_test_segments.py writes.)
GROUND_TRUTH: dict[str, str] = {
    "Роман_Вечерківський_000108.wav":  "Роман Вечерківський",
    "Павло_Кулаковський_000000.wav":   "Павло Кулаковський",
    "Євген_Бутенко_005405.wav":         "Євген Бутенко",
    "Богдан_Терещенко_013740.wav":     "Богдан Терещенко",
    "Вячеслав_Коновалов_002438.wav":   "Вячеслав Коновалов",
}


@dataclass
class MatchResult:
    """Result of matching one segment against all profiles."""

    segment_file: str
    expected: str
    best_match: str
    best_sim: float
    runner_up: str
    runner_up_sim: float
    correct: bool


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
    """Load voice profiles."""
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


def compute_segment_embedding(audio_path: Path, embedding_model) -> np.ndarray:
    """Compute speaker embedding for the entire audio segment.

    Uses pyannote's Inference wrapper, which handles loading, padding, etc.
    Returns a 1-D numpy array.
    """
    # Inference returns a SlidingWindowFeature; we average the embeddings
    # (the segment is short enough that one window covers it).
    from pyannote.audio import Inference

    if not isinstance(embedding_model, Inference):
        # First call — wrap in Inference helper
        inference = Inference(embedding_model, window="whole")
    else:
        inference = embedding_model

    output = inference(str(audio_path))

    # `output` can be:
    # - np.ndarray (1D, when window="whole")
    # - SlidingWindowFeature (when sliding window)
    if isinstance(output, np.ndarray):
        emb = output
    elif hasattr(output, "data"):
        emb = np.mean(output.data, axis=0)
    else:
        emb = np.asarray(output)

    return emb


def match_segment(
    embedding: np.ndarray,
    profiles: dict[str, np.ndarray],
) -> tuple[str, float, str, float]:
    """Find best and runner-up match for an embedding.

    Returns (best_name, best_sim, runner_up_name, runner_up_sim).
    """
    sims: list[tuple[str, float]] = []
    for name, profile in profiles.items():
        sims.append((name, cosine_similarity(embedding, profile)))

    sims.sort(key=lambda x: x[1], reverse=True)
    best_name, best_sim = sims[0]
    runner_name, runner_sim = sims[1] if len(sims) > 1 else ("(none)", 0.0)
    return best_name, best_sim, runner_name, runner_sim


def print_result_row(result: MatchResult) -> None:
    """One-line summary for a single segment."""
    status = "✓" if result.correct else "✗"
    gap = result.best_sim - result.runner_up_sim
    print(
        f"  {status}  {result.expected:<25} → "
        f"{result.best_match:<25} "
        f"sim={result.best_sim:+.3f}  "
        f"(2nd: {result.runner_up:<22} {result.runner_up_sim:+.3f}, gap={gap:+.3f})"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test voice profile matching on extracted segments",
    )
    parser.add_argument(
        "--segments-dir",
        type=Path,
        default=DEFAULT_SEGMENTS_DIR,
    )
    parser.add_argument(
        "--profiles-dir",
        type=Path,
        default=DEFAULT_PROFILES_DIR,
    )
    args = parser.parse_args()

    if not args.segments_dir.exists():
        print(f"❌ Segments dir not found: {args.segments_dir}")
        print(f"Run extract_test_segments.py first.")
        raise SystemExit(1)

    if not args.profiles_dir.exists():
        print(f"❌ Profiles dir not found: {args.profiles_dir}")
        raise SystemExit(1)

    load_env()
    token = os.environ.get("HUGGINGFACE_TOKEN") or os.environ.get("HF_TOKEN")
    if not token:
        print("❌ HUGGINGFACE_TOKEN missing")
        raise SystemExit(1)

    print("Loading voice profiles...")
    profiles = load_profiles(args.profiles_dir)
    print(f"  ✓ {len(profiles)} profiles loaded")

    print("\nLoading pyannote embedding model (this may take ~30s)...")
    from pyannote.audio import Model, Inference

    # model = Model.from_pretrained("pyannote/embedding", token=token)
    model = Model.from_pretrained("pyannote/wespeaker-voxceleb-resnet34-LM", token=token)
    inference = Inference(model, window="whole")
    print("  ✓ Model ready")

    # Find segment files
    segment_files = sorted(args.segments_dir.glob("*.wav"))
    if not segment_files:
        print(f"❌ No .wav files in {args.segments_dir}")
        raise SystemExit(1)

    print(f"\n{'=' * 95}")
    print(f"  Matching {len(segment_files)} segments against {len(profiles)} profiles")
    print(f"{'=' * 95}\n")

    results: list[MatchResult] = []

    for seg_path in segment_files:
        expected = GROUND_TRUTH.get(seg_path.name)
        if expected is None:
            print(f"  ⚠ {seg_path.name}: no ground-truth entry, skipping")
            continue

        print(f"Processing {seg_path.name}...", flush=True)
        embedding = compute_segment_embedding(seg_path, inference)

        best_name, best_sim, runner_name, runner_sim = match_segment(
            embedding, profiles
        )

        result = MatchResult(
            segment_file=seg_path.name,
            expected=expected,
            best_match=best_name,
            best_sim=best_sim,
            runner_up=runner_name,
            runner_up_sim=runner_sim,
            correct=(best_name == expected),
        )
        results.append(result)

    # Results table
    print(f"\n{'=' * 95}")
    print(f"  Results")
    print(f"{'=' * 95}\n")
    for result in results:
        print_result_row(result)

    # Summary
    n_correct = sum(1 for r in results if r.correct)
    n_total = len(results)
    print(f"\n{'=' * 95}")
    print(f"  Summary")
    print(f"{'=' * 95}")
    print(f"  Accuracy: {n_correct}/{n_total} = {100 * n_correct / n_total:.0f}%")
    print()

    # Per-result detail with full similarity ranking
    print(f"{'=' * 95}")
    print(f"  Per-segment full ranking (all 9 profiles, sorted)")
    print(f"{'=' * 95}\n")

    for seg_path in segment_files:
        expected = GROUND_TRUTH.get(seg_path.name)
        if expected is None:
            continue
        print(f"  Segment: {seg_path.name}  (expected: {expected})")
        embedding = compute_segment_embedding(seg_path, inference)
        sims = sorted(
            ((name, cosine_similarity(embedding, p))
             for name, p in profiles.items()),
            key=lambda x: x[1],
            reverse=True,
        )
        for rank, (name, sim) in enumerate(sims, 1):
            marker = " ← expected" if name == expected else ""
            print(f"    {rank}. {name:<30} sim={sim:+.4f}{marker}")
        print()

    # Suggested threshold based on this real data
    if results:
        correct_sims = [r.best_sim for r in results if r.correct]
        wrong_sims = [r.best_sim for r in results if not r.correct]
        runner_sims = [r.runner_up_sim for r in results]
        print(f"{'=' * 95}")
        print(f"  Threshold suggestions based on real cross-meeting data")
        print(f"{'=' * 95}")
        if correct_sims:
            print(f"  Correct-match similarities: "
                  f"min={min(correct_sims):.3f}, mean={np.mean(correct_sims):.3f}, "
                  f"max={max(correct_sims):.3f}")
        if runner_sims:
            print(f"  Runner-up similarities:      "
                  f"min={min(runner_sims):.3f}, mean={np.mean(runner_sims):.3f}, "
                  f"max={max(runner_sims):.3f}")
        if correct_sims and runner_sims:
            min_correct = min(correct_sims)
            max_runner = max(runner_sims)
            if min_correct > max_runner:
                suggested = (min_correct + max_runner) / 2
                print(
                    f"\n  ✓ Separation: correct min ({min_correct:.3f}) > "
                    f"runner-up max ({max_runner:.3f})"
                )
                print(f"  Suggested threshold: {suggested:.2f}")
            else:
                print(
                    f"\n  ⚠ Overlap: correct min ({min_correct:.3f}) ≤ "
                    f"runner-up max ({max_runner:.3f})"
                )
                print(f"  Reliable threshold cannot be cleanly chosen — "
                      f"some matches will be ambiguous")


if __name__ == "__main__":
    main()
