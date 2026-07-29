"""
Per-cluster speaker review for a processed meeting (transcript "change speaker").

The user reassigns a diarization cluster (SPEAKER_XX) to a correct or NEW name
from the стенограма. Changes accumulate as a draft; a separate "run analysis"
button then, for each NEW participant, saves a voice profile (.npy) from that
cluster's cached embedding — so future meetings recognize them — writes the
names into speakers.json, and queues a full re-run.

Voice model (see match_speakers.py / add_voice_profile.py):
    - <voice_profiles>/<name>.npy      — one baseline embedding per known person
    - <audio>_embeddings.pkl           — {SPEAKER_XX → embedding} for this meeting

This module owns:
    - the draft store (<meeting_dir>/speaker_review.json)
    - the list of known names (profiles + current speakers) for the picker
    - saving a new voice profile from a meeting cluster

The voice-profile directory is passed in, never assumed: a voice fingerprint
identifies a real person, so one church's profiles must never surface as name
suggestions in another's speaker review. Callers resolve it per tenant via
shared/tenant_paths.py.
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path
from typing import Any, Optional

import numpy as np

from church_assistant.ingestion.paths import resolve as resolve_paths


DRAFT_NAME = "speaker_review.json"
MIN_EMBEDDING_NORM = 0.01


# ─────────────────────────────────────────────────────────────
# Names
# ─────────────────────────────────────────────────────────────

def filename_safe(name: str) -> str:
    """Filesystem-safe profile filename (keeps Ukrainian letters)."""
    result = name.strip()
    for ch in '/\\:*?"<>|':
        result = result.replace(ch, "_")
    return result


def has_profile(profiles_dir: Path, name: str) -> bool:
    """True if a baseline voice profile already exists for this name."""
    return (profiles_dir / f"{filename_safe(name)}.npy").exists()


def _is_real_name(value: str) -> bool:
    """A real person's name (not a placeholder like [нерозбірливо] or a raw label)."""
    v = value.strip()
    return bool(v) and not v.startswith("[") and not v.startswith("SPEAKER_")


def list_known_names(profiles_dir: Path, speaker_map: dict[str, str]) -> list[str]:
    """
    Sorted unique names for the picker: this tenant's voice profiles ∪ this
    meeting's current speaker names (real names only).
    """
    names: set[str] = set()
    if profiles_dir.is_dir():
        for p in profiles_dir.glob("*.npy"):
            names.add(p.stem)
    for raw in speaker_map.values():
        v = str(raw)
        v = v[: -len(" [REVIEW]")] if v.endswith(" [REVIEW]") else v
        if _is_real_name(v):
            names.add(v)
    return sorted(names, key=lambda s: s.lower())


# ─────────────────────────────────────────────────────────────
# Draft store  (data/meetings/<date>/speaker_review.json)
# ─────────────────────────────────────────────────────────────
#
# {"changes": [{"label": "SPEAKER_05", "new_name": "Андрій Гість", "is_new": true}]}
#

def draft_path(meeting_dir: Path) -> Path:
    return Path(meeting_dir) / DRAFT_NAME


def load_changes(meeting_dir: Path) -> list[dict[str, Any]]:
    """Return the pending changes (empty list if no draft / unreadable)."""
    p = draft_path(meeting_dir)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    changes = data.get("changes") if isinstance(data, dict) else None
    return changes if isinstance(changes, list) else []


def _save_changes(meeting_dir: Path, changes: list[dict[str, Any]]) -> None:
    draft_path(meeting_dir).write_text(
        json.dumps({"changes": changes}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def upsert_change(
    meeting_dir: Path, *, label: str, new_name: str, is_new: bool
) -> list[dict[str, Any]]:
    """Add or replace the pending change for a cluster (one per label)."""
    changes = [c for c in load_changes(meeting_dir) if c.get("label") != label]
    changes.append({"label": label, "new_name": new_name, "is_new": is_new})
    changes.sort(key=lambda c: c.get("label", ""))
    _save_changes(meeting_dir, changes)
    return changes


def remove_change(meeting_dir: Path, label: str) -> list[dict[str, Any]]:
    changes = [c for c in load_changes(meeting_dir) if c.get("label") != label]
    _save_changes(meeting_dir, changes)
    return changes


def clear_draft(meeting_dir: Path) -> None:
    draft_path(meeting_dir).unlink(missing_ok=True)


# ─────────────────────────────────────────────────────────────
# Voice profile from a meeting cluster
# ─────────────────────────────────────────────────────────────

def save_voice_profile_from_cluster(
    meeting_dir: Path,
    profiles_dir: Path,
    *,
    label: str,
    name: str,
    audio_filename: Optional[str] = None,
) -> tuple[bool, str]:
    """
    Save <profiles_dir>/<name>.npy from this meeting's cached embedding for
    cluster `label`. Returns (ok, message). Mirrors add_voice_profile.py's checks.
    """
    paths = resolve_paths(Path(meeting_dir), audio_filename)
    pkl = paths.embeddings
    if not pkl.exists():
        return False, f"немає кешу ембедингів ({pkl.name}) — фінгерпринт неможливий"

    try:
        with pkl.open("rb") as f:
            data = pickle.load(f)
        embeddings: dict[str, np.ndarray] = data["embeddings"]
    except (OSError, KeyError, pickle.UnpicklingError) as e:
        return False, f"не вдалося прочитати ембединги: {e}"

    if label not in embeddings:
        return False, f"кластер {label} відсутній у кеші ембедингів"

    emb = embeddings[label]
    norm = float(np.linalg.norm(emb))
    if norm < MIN_EMBEDDING_NORM:
        return False, f"ембединг {label} нульовий (norm={norm:.4f}) — це артефакт, профіль не збережено"
    if np.isnan(emb).any() or np.isinf(emb).any():
        return False, f"ембединг {label} містить NaN/Inf — профіль не збережено"

    profiles_dir.mkdir(parents=True, exist_ok=True)
    out = profiles_dir / f"{filename_safe(name)}.npy"
    np.save(out, emb)
    return True, f"голосовий профіль збережено: {out.name}"


# ─────────────────────────────────────────────────────────────
# CLI smoke test
# ─────────────────────────────────────────────────────────────

def _smoke_test() -> None:
    import tempfile

    print("=" * 60)
    print("  speaker_review — smoke test")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as d:
        md = Path(d)

        # draft round-trip
        assert load_changes(md) == []
        upsert_change(md, label="SPEAKER_05", new_name="Андрій Гість", is_new=True)
        upsert_change(md, label="SPEAKER_02", new_name="Роман Вечерківський", is_new=False)
        ch = load_changes(md)
        assert len(ch) == 2 and ch[0]["label"] == "SPEAKER_02"
        print("1. draft upsert/load ✓ (2 changes, sorted)")

        # upsert replaces same label
        upsert_change(md, label="SPEAKER_05", new_name="Андрій Новий", is_new=True)
        ch = load_changes(md)
        assert len(ch) == 2
        assert next(c for c in ch if c["label"] == "SPEAKER_05")["new_name"] == "Андрій Новий"
        print("2. upsert replaces same label ✓")

        remove_change(md, "SPEAKER_02")
        assert len(load_changes(md)) == 1
        clear_draft(md)
        assert load_changes(md) == [] and not draft_path(md).exists()
        print("3. remove + clear ✓")

    # known names = this tenant's profiles ∪ the meeting's own speakers
    from church_assistant.shared import tenant_paths
    profiles_dir = tenant_paths.paths_for(
        tenant_paths.legacy_slug() or "default"
    ).voice_profiles
    names = list_known_names(
        profiles_dir,
        {"SPEAKER_00": "Богдан Терещенко", "SPEAKER_01": "[нерозбірливо]"},
    )
    assert "Богдан Терещенко" in names
    assert not any(n.startswith("[") for n in names)
    print(f"4. list_known_names ✓ ({len(names)} names, placeholders excluded)")

    # A tenant with no profiles directory sees only its own meeting's names —
    # never another church's fingerprints.
    isolated = list_known_names(Path("/nonexistent/tenant/voice_profiles"),
                                {"SPEAKER_00": "Богдан Терещенко"})
    assert isolated == ["Богдан Терещенко"]
    print("5. profiles are per-tenant (empty dir → no cross-church names) ✓")

    print("=" * 60)
    print("  ✓ ALL SMOKE TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    _smoke_test()
