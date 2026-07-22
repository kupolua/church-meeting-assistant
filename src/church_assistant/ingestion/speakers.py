"""
speakers.json helpers for the web review editor (MVP-C, Phase 4).

match_speakers.py writes a speakers.json shaped like:

    {
        "_meta": {
            "needs_review": ["SPEAKER_02"],   # weak matches to confirm
            "no_match": ["SPEAKER_00"],       # unknown voice — assign a name
            "invalid_embedding": [],          # too little speech to fingerprint
            ...thresholds...
        },
        "SPEAKER_00": "Богдан Терещенко",
        "SPEAKER_02": "Веніамін Коновалов [REVIEW]",   # weak match marker
        ...
    }

The review editor lets the human confirm/fix each SPEAKER_XX → name mapping
before merge → analyze → polish. This module:
    - loads/saves the file while PRESERVING the _meta block,
    - strips the " [REVIEW]" marker for display (the human's saved value is final),
    - derives per-speaker talk-time hints from diarization.rttm to aid recognition,
    - builds ready-to-render review rows (label, name, flag, stats).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

META_KEY = "_meta"
REVIEW_SUFFIX = " [REVIEW]"


# ─────────────────────────────────────────────────────────────
# Load / save (preserving _meta)
# ─────────────────────────────────────────────────────────────

def load_speakers(path: Path) -> tuple[dict[str, Any], dict[str, str]]:
    """
    Read speakers.json → (meta, mapping).

    meta    = the _meta block (or {} if absent)
    mapping = {SPEAKER_XX: name} entries (underscore-prefixed keys excluded)
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("speakers.json is not a JSON object")
    meta = data.get(META_KEY, {}) if isinstance(data.get(META_KEY), dict) else {}
    mapping = {
        k: str(v) for k, v in data.items() if not k.startswith("_")
    }
    return meta, mapping


def save_speakers(
    path: Path,
    meta: dict[str, Any],
    mapping: dict[str, str],
) -> None:
    """
    Write speakers.json back, _meta first, then speaker entries sorted by label.

    Values are written verbatim (the editor already produced the final names).
    """
    out: dict[str, Any] = {}
    if meta:
        out[META_KEY] = meta
    for label in sorted(mapping):
        out[label] = mapping[label]
    Path(path).write_text(
        json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def strip_review(name: str) -> tuple[str, bool]:
    """Return (name_without_marker, had_review_marker)."""
    if name.endswith(REVIEW_SUFFIX):
        return name[: -len(REVIEW_SUFFIX)], True
    return name, False


# ─────────────────────────────────────────────────────────────
# RTTM talk-time hints
# ─────────────────────────────────────────────────────────────

def _fmt_mmss(seconds: float) -> str:
    """Seconds → 'M:SS' (or 'H:MM:SS' past an hour)."""
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def rttm_speaker_stats(rttm_path: Path) -> dict[str, dict[str, Any]]:
    """
    Parse diarization.rttm → per-speaker {total_s, segments, starts[:3]}.

    RTTM line: `SPEAKER <file> 1 <start> <dur> <NA> <NA> SPEAKER_XX <NA> <NA>`.
    Missing/unreadable file → {} (hints are optional).
    """
    stats: dict[str, dict[str, Any]] = {}
    try:
        lines = Path(rttm_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return stats

    for line in lines:
        parts = line.split()
        if len(parts) < 8 or parts[0] != "SPEAKER":
            continue
        try:
            start = float(parts[3])
            dur = float(parts[4])
        except ValueError:
            continue
        label = parts[7]
        s = stats.setdefault(label, {"total_s": 0.0, "segments": 0, "starts": []})
        s["total_s"] += dur
        s["segments"] += 1
        if len(s["starts"]) < 3:
            s["starts"].append(start)
    return stats


# ─────────────────────────────────────────────────────────────
# Review rows (for the template)
# ─────────────────────────────────────────────────────────────

def build_review_rows(
    meta: dict[str, Any],
    mapping: dict[str, str],
    stats: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Combine mapping + _meta flags + RTTM stats into render-ready rows.

    Row flag (most severe first): 'invalid' | 'no_match' | 'review' | None.
    Rows are ordered by talk time DESC (loudest speakers first — easier to place).
    """
    needs_review = set(meta.get("needs_review", []))
    no_match = set(meta.get("no_match", []))
    invalid = set(meta.get("invalid_embedding", []))

    rows: list[dict[str, Any]] = []
    for label in mapping:
        raw = mapping[label]
        name, had_review = strip_review(raw)

        if label in invalid:
            flag = "invalid"
        elif label in no_match or name == label:
            flag = "no_match"
        elif label in needs_review or had_review:
            flag = "review"
        else:
            flag = None

        st = stats.get(label, {})
        total_s = float(st.get("total_s", 0.0))
        rows.append({
            "label": label,
            # For no_match rows the "name" is often the label itself — show blank
            # so the human types a real name into an empty field.
            "name": "" if name == label else name,
            "flag": flag,
            "total_s": total_s,
            "total_hms": _fmt_mmss(total_s),
            "segments": int(st.get("segments", 0)),
            "sample_starts": [_fmt_mmss(x) for x in st.get("starts", [])],
        })

    rows.sort(key=lambda r: r["total_s"], reverse=True)
    return rows


# ─────────────────────────────────────────────────────────────
# CLI smoke test
# ─────────────────────────────────────────────────────────────

def _smoke_test() -> None:
    import tempfile

    print("=" * 70)
    print("  ingestion.speakers — smoke test")
    print("=" * 70)

    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        sp = base / "speakers.json"
        rttm = base / "diarization.rttm"

        sp.write_text(json.dumps({
            "_meta": {"needs_review": ["SPEAKER_01"], "no_match": ["SPEAKER_02"],
                      "invalid_embedding": []},
            "SPEAKER_00": "Богдан",
            "SPEAKER_01": "Вячеслав [REVIEW]",
            "SPEAKER_02": "SPEAKER_02",
        }, ensure_ascii=False), encoding="utf-8")

        rttm.write_text(
            "SPEAKER audio 1 0.0 30.0 <NA> <NA> SPEAKER_00 <NA> <NA>\n"
            "SPEAKER audio 1 30.0 10.0 <NA> <NA> SPEAKER_01 <NA> <NA>\n"
            "SPEAKER audio 1 40.0 5.0 <NA> <NA> SPEAKER_02 <NA> <NA>\n"
            "SPEAKER audio 1 45.0 25.0 <NA> <NA> SPEAKER_00 <NA> <NA>\n",
            encoding="utf-8",
        )

        meta, mapping = load_speakers(sp)
        assert meta["needs_review"] == ["SPEAKER_01"]
        assert mapping["SPEAKER_01"] == "Вячеслав [REVIEW]"
        print("1. load_speakers ✓ (meta preserved, 3 speakers)")

        stats = rttm_speaker_stats(rttm)
        assert stats["SPEAKER_00"]["total_s"] == 55.0
        assert stats["SPEAKER_00"]["segments"] == 2
        print(f"2. rttm_speaker_stats ✓ (SPEAKER_00 = {stats['SPEAKER_00']['total_s']}s)")

        rows = build_review_rows(meta, mapping, stats)
        assert rows[0]["label"] == "SPEAKER_00"  # most talk time first
        by_label = {r["label"]: r for r in rows}
        assert by_label["SPEAKER_01"]["flag"] == "review"
        assert by_label["SPEAKER_01"]["name"] == "Вячеслав"       # marker stripped
        assert by_label["SPEAKER_02"]["flag"] == "no_match"
        assert by_label["SPEAKER_02"]["name"] == ""               # blank for retype
        print("3. build_review_rows ✓ (flags + stripping + ordering)")

        # Round-trip save with edited names, _meta preserved.
        edited = {"SPEAKER_00": "Богдан Терещенко", "SPEAKER_01": "Вячеслав Коновалов",
                  "SPEAKER_02": "Гість Іван"}
        save_speakers(sp, meta, edited)
        meta2, mapping2 = load_speakers(sp)
        assert meta2 == meta and mapping2 == edited
        print("4. save_speakers round-trip ✓ (_meta kept, names updated)")

    print("=" * 70)
    print("  ✓ ALL SMOKE TESTS PASSED")
    print("=" * 70)


if __name__ == "__main__":
    _smoke_test()
