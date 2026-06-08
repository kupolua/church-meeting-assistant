"""Diagnostic: check whether voice profiles are distinct enough for matching.

For each pair of profiles in data/voice_profiles/, computes cosine similarity.
Outputs:
- Full pairwise similarity matrix
- Lowest and highest pairs
- Suggested threshold for Phase 2 matching

This is a fast local computation (seconds, no pyannote calls).

Expected results:
- Different people: similarity < 0.6 (ideally < 0.4)
- Same person (across recordings): similarity > 0.7
- Related people (relatives, same family): similarity may be elevated
  due to genetic vocal similarity — interesting data point

Usage:
    uv run python -m church_assistant.test_profile_uniqueness
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np


DEFAULT_PROFILES_DIR = Path("data/voice_profiles")


def load_profiles(profiles_dir: Path) -> dict[str, np.ndarray]:
    """Load all .npy profiles from the directory."""
    profiles: dict[str, np.ndarray] = {}
    for path in sorted(profiles_dir.glob("*.npy")):
        name = path.stem
        embedding = np.load(path)
        profiles[name] = embedding
    return profiles


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity in [-1, 1]. Higher = more similar."""
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a, b) / (norm_a * norm_b))


def compute_pairwise_matrix(
    profiles: dict[str, np.ndarray],
) -> tuple[list[str], np.ndarray]:
    """Compute full pairwise cosine similarity matrix."""
    names = list(profiles.keys())
    n = len(names)
    matrix = np.zeros((n, n))
    for i in range(n):
        for j in range(n):
            matrix[i, j] = cosine_similarity(profiles[names[i]], profiles[names[j]])
    return names, matrix


def print_matrix(names: list[str], matrix: np.ndarray) -> None:
    """Print the full similarity matrix with truncated names."""
    # Truncate names to first 12 characters for display
    short_names = [n[:12] for n in names]

    print(f"\n{'=' * 100}")
    print(f"  Pairwise cosine similarity matrix")
    print(f"{'=' * 100}\n")

    # Header
    print(f"{'':<14}", end="")
    for short in short_names:
        print(f"{short:>10}", end="")
    print()

    # Rows
    for i, name in enumerate(short_names):
        print(f"{name:<14}", end="")
        for j in range(len(names)):
            val = matrix[i, j]
            if i == j:
                # Diagonal — should always be 1.0
                print(f"{'':>10}", end="")
            else:
                print(f"{val:>10.3f}", end="")
        print()


def analyze_pairs(
    names: list[str],
    matrix: np.ndarray,
) -> tuple[list[tuple[str, str, float]], list[tuple[str, str, float]]]:
    """Extract sorted off-diagonal pairs.

    Returns (sorted_ascending, sorted_descending) — same pairs, two orderings.
    """
    pairs: list[tuple[str, str, float]] = []
    n = len(names)
    for i in range(n):
        for j in range(i + 1, n):
            pairs.append((names[i], names[j], float(matrix[i, j])))

    asc = sorted(pairs, key=lambda x: x[2])
    desc = sorted(pairs, key=lambda x: x[2], reverse=True)
    return asc, desc


def suggest_threshold(pairs: list[tuple[str, str, float]]) -> tuple[float, str]:
    """Suggest a threshold based on the gap between distinct-people pairs.

    Strategy:
    - Take all off-diagonal pairs (all are different-people pairs)
    - Find the MAX similarity among them
    - Suggested threshold = max + safety margin

    A new meeting's embedding should match its own profile with sim > threshold,
    and any other profile with sim < threshold.
    """
    if not pairs:
        return 0.7, "no pairs (only one profile)"

    max_other = max(pair[2] for pair in pairs)

    # Suggested threshold: midpoint between max_other and 1.0,
    # but not below 0.5 (sanity floor)
    suggested = max(0.5, (max_other + 1.0) / 2)

    if max_other < 0.4:
        rationale = (
            f"max different-person sim is {max_other:.3f} — profiles are very distinct. "
            f"Use ~0.7 for strict matching with comfortable margin."
        )
        return 0.7, rationale
    elif max_other < 0.6:
        rationale = (
            f"max different-person sim is {max_other:.3f} — comfortable separation. "
            f"Use ~{suggested:.2f} (midpoint to 1.0)."
        )
        return suggested, rationale
    else:
        rationale = (
            f"⚠ max different-person sim is {max_other:.3f} — limited separation. "
            f"Threshold {suggested:.2f} suggested, but expect some ambiguous matches."
        )
        return suggested, rationale


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test voice profile uniqueness"
    )
    parser.add_argument(
        "--profiles-dir",
        type=Path,
        default=DEFAULT_PROFILES_DIR,
    )
    args = parser.parse_args()

    if not args.profiles_dir.exists():
        print(f"❌ Profiles directory not found: {args.profiles_dir}")
        raise SystemExit(1)

    profiles = load_profiles(args.profiles_dir)
    if not profiles:
        print(f"❌ No .npy profiles found in {args.profiles_dir}")
        raise SystemExit(1)

    print(f"Loaded {len(profiles)} voice profiles:")
    for name, emb in profiles.items():
        print(f"  {name:<30} shape={emb.shape}, norm={np.linalg.norm(emb):.3f}")

    # Compute matrix
    names, matrix = compute_pairwise_matrix(profiles)

    # Print full matrix
    print_matrix(names, matrix)

    # Sorted pairs
    asc, desc = analyze_pairs(names, matrix)

    print(f"\n{'=' * 70}")
    print(f"  Most similar pairs (top 5 — POTENTIAL CONFUSION)")
    print(f"{'=' * 70}")
    for a, b, sim in desc[:5]:
        marker = "⚠ " if sim > 0.5 else "  "
        print(f"{marker}{a:<28} ↔ {b:<28} sim = {sim:+.4f}")

    print(f"\n{'=' * 70}")
    print(f"  Least similar pairs (bottom 5 — best separation)")
    print(f"{'=' * 70}")
    for a, b, sim in asc[:5]:
        print(f"  {a:<28} ↔ {b:<28} sim = {sim:+.4f}")

    # Statistics
    all_sims = [pair[2] for pair in asc]
    print(f"\n{'=' * 70}")
    print(f"  Statistics (across all {len(all_sims)} off-diagonal pairs)")
    print(f"{'=' * 70}")
    print(f"  min:    {min(all_sims):+.4f}")
    print(f"  max:    {max(all_sims):+.4f}")
    print(f"  mean:   {np.mean(all_sims):+.4f}")
    print(f"  median: {np.median(all_sims):+.4f}")
    print(f"  stdev:  {np.std(all_sims):+.4f}")

    # Threshold suggestion
    threshold, rationale = suggest_threshold(asc)
    print(f"\n{'=' * 70}")
    print(f"  Suggested threshold for Phase 2 matching")
    print(f"{'=' * 70}")
    print(f"  Threshold: {threshold:.2f}")
    print(f"  Rationale: {rationale}")

    # Same-family heads-up
    surnames: dict[str, list[str]] = {}
    for name in names:
        parts = name.split()
        if len(parts) >= 2:
            surname = parts[-1]
            surnames.setdefault(surname, []).append(name)
    families = {s: ms for s, ms in surnames.items() if len(ms) >= 2}
    if families:
        print(f"\n{'=' * 70}")
        print(f"  Same-surname check (potential genetic vocal similarity)")
        print(f"{'=' * 70}")
        for surname, members in families.items():
            print(f"  '{surname}' family: {', '.join(members)}")
            for i, m1 in enumerate(members):
                for m2 in members[i + 1 :]:
                    idx1 = names.index(m1)
                    idx2 = names.index(m2)
                    sim = matrix[idx1, idx2]
                    flag = "⚠ " if sim > 0.4 else "  "
                    print(f"    {flag}{m1} ↔ {m2}: {sim:+.4f}")


if __name__ == "__main__":
    main()
