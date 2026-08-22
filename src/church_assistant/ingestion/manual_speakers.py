"""
Manually added speakers — a participant diarization never clustered.

Someone who says two sentences in a two-hour meeting can be missed by pyannote
entirely, or folded into a neighbour's cluster. There is no fingerprint to fix
and nothing to rename: the voice simply is not in speakers.json. The human,
however, knows WHEN they spoke.

So the editor takes a timestamp and a name, and this module turns that into the
one thing the whole pipeline already agrees on — a diarization segment:

    speakers.json   _meta.manual_speakers = [{label, name_hint, spec, windows}]
                    SPEAKER_07 = "Ім'я"          (a normal mapping entry)
    diarization.rttm  = pyannote's segments MINUS the manual windows,
                        PLUS one line per manual window

Everything downstream (merge_transcript, polish_protocol's attendee detection,
the transcript's per-turn labels, the talk-time hints) reads the RTTM, so
nothing else has to learn about manual speakers. That is the reason for writing
into the RTTM rather than carrying an overlay: an overlay would have to be
threaded through six independent readers, and the one that got missed would
quietly disagree about who spoke.

Two consequences of that choice, both handled here:

  - pyannote's own output is preserved once, as diarization.pyannote.rttm, and
    the RTTM is REBUILT from it on every edit. Manual entries stay editable and
    removable, and repeated edits cannot compound into a drifting file.
  - the manual window is subtracted from the other speakers' segments. Attribution
    is by dominant overlap (merge_transcript.find_dominant_speaker), so a manual
    segment merely inserted alongside a longer one would lose the vote and change
    nothing — the edit would appear to save and silently do nothing.

Windows snap to the transcript, not to the typed number: attribution happens per
Whisper segment, so half a segment cannot be reassigned. A bare timestamp claims
the segment spoken at that moment; a range claims the segments it mostly covers.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

META_KEY = "manual_speakers"
PRISTINE_NAME = "diarization.pyannote.rttm"

#: A timestamp that lands in silence has no Whisper segment to claim; give it a
#: short window anyway, so the person still counts as present.
GAP_WINDOW_S = 5.0
#: A range claims a Whisper segment only if it covers most of it — a range that
#: merely clips a neighbouring phrase must not steal it.
RANGE_CLAIM_RATIO = 0.5
#: Trimming can leave slivers; a segment shorter than this is noise, not speech.
MIN_PIECE_S = 0.05


class TimeSpecError(ValueError):
    """The typed timestamp could not be understood (message is user-facing)."""


# ─────────────────────────────────────────────────────────────
# Timestamps
# ─────────────────────────────────────────────────────────────

_PART_RE = re.compile(r"^(?:(\d+):)?(?:(\d+):)?(\d+(?:\.\d+)?)$")
_RANGE_SPLIT_RE = re.compile(r"\s*[-–—]\s*")


def format_hms(seconds: float) -> str:
    """Seconds → 'M:SS' (or 'H:MM:SS' past an hour)."""
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def parse_timestamp(text: str) -> float:
    """
    'SS' | 'M:SS' | 'H:MM:SS' (fractional seconds allowed) → seconds.

    Raises TimeSpecError with a message meant for the person who typed it.
    """
    raw = text.strip()
    m = _PART_RE.match(raw)
    if not m:
        raise TimeSpecError(f"не зрозумів час «{raw}» — очікую 12:34 або 1:23:45")
    g1, g2, g3 = m.groups()
    secs = float(g3)
    if g1 and g2:
        hours, minutes = int(g1), int(g2)
    elif g1:
        hours, minutes = 0, int(g1)
    else:
        hours, minutes = 0, 0
    if (g1 or g2) and (secs >= 60 or minutes >= 60):
        raise TimeSpecError(f"хвилини й секунди мають бути < 60: «{raw}»")
    return hours * 3600 + minutes * 60 + secs


def parse_spec(spec: str) -> list[tuple[float, Optional[float]]]:
    """
    Parse the typed "Говорив" field → [(start, end|None), …].

    Accepts several moments separated by ',' or ';', each either a single
    timestamp ('1:23:45') or a range ('1:23:45-1:24:10').
    """
    parts = [p.strip() for p in re.split(r"[,;]", spec) if p.strip()]
    if not parts:
        raise TimeSpecError("порожній час — вкажи, коли людина говорила")

    out: list[tuple[float, Optional[float]]] = []
    for part in parts:
        halves = _RANGE_SPLIT_RE.split(part)
        if len(halves) == 1:
            out.append((parse_timestamp(halves[0]), None))
        elif len(halves) == 2:
            start, end = parse_timestamp(halves[0]), parse_timestamp(halves[1])
            if end <= start:
                raise TimeSpecError(
                    f"кінець раніше за початок у «{part}» "
                    f"({format_hms(start)} → {format_hms(end)})"
                )
            out.append((start, end))
        else:
            raise TimeSpecError(f"не зрозумів діапазон «{part}»")
    return out


def format_spec(windows: Sequence[tuple[float, float]]) -> str:
    """Resolved windows → the text shown back in the editor's time field."""
    return ", ".join(f"{format_hms(s)}-{format_hms(e)}" for s, e in windows)


# ─────────────────────────────────────────────────────────────
# Snapping to the transcript
# ─────────────────────────────────────────────────────────────

def load_whisper_segments(transcript_path: Path) -> list[tuple[float, float]]:
    """
    audio_transcript.json → [(start, end)] sorted. Missing/unreadable → [].

    Optional on purpose: without the transcript the windows fall back to the
    typed times, which still records attendance even if no phrase moves.
    """
    try:
        data = json.loads(Path(transcript_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    segments = data.get("segments") if isinstance(data, dict) else data
    if not isinstance(segments, list):
        return []
    out: list[tuple[float, float]] = []
    for seg in segments:
        try:
            start, end = float(seg["start"]), float(seg["end"])
        except (KeyError, TypeError, ValueError):
            continue
        if end > start:
            out.append((start, end))
    out.sort()
    return out


def _merge_windows(windows: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    """Sort and coalesce overlapping/touching windows."""
    ordered = sorted(windows)
    merged: list[tuple[float, float]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def resolve_windows(
    specs: Sequence[tuple[float, Optional[float]]],
    segments: Sequence[tuple[float, float]],
) -> list[tuple[float, float]]:
    """
    Typed moments + Whisper segments → the windows to hand the manual speaker.

    A bare timestamp takes the segment spoken at that moment (or a short window
    if it fell in silence). A range takes itself plus every segment it covers by
    more than RANGE_CLAIM_RATIO — a phrase is reassigned whole or not at all.
    """
    windows: list[tuple[float, float]] = []
    for start, end in specs:
        if end is None:
            hit = next(
                ((s, e) for s, e in segments if s <= start < e),
                None,
            )
            windows.append(hit if hit is not None else (start, start + GAP_WINDOW_S))
            continue

        window_start, window_end = start, end
        for s, e in segments:
            overlap = min(e, end) - max(s, start)
            if overlap > 0 and overlap >= RANGE_CLAIM_RATIO * (e - s):
                window_start = min(window_start, s)
                window_end = max(window_end, e)
        windows.append((window_start, window_end))

    return _merge_windows(windows)


# ─────────────────────────────────────────────────────────────
# The _meta.manual_speakers list
# ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ManualEntry:
    """One human-added speaker: a label, the typed time, the resolved windows."""
    label: str
    name: str
    spec: str
    windows: list[tuple[float, float]]

    def to_json(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "name": self.name,
            "spec": self.spec,
            "windows": [[round(s, 3), round(e, 3)] for s, e in self.windows],
        }


def load_entries(meta: dict[str, Any]) -> list[ManualEntry]:
    """Read _meta.manual_speakers; anything malformed is skipped, not fatal."""
    raw = meta.get(META_KEY)
    if not isinstance(raw, list):
        return []
    entries: list[ManualEntry] = []
    for item in raw:
        if not isinstance(item, dict) or not item.get("label"):
            continue
        windows: list[tuple[float, float]] = []
        for w in item.get("windows", []) or []:
            try:
                start, end = float(w[0]), float(w[1])
            except (TypeError, ValueError, IndexError):
                continue
            if end > start:
                windows.append((start, end))
        entries.append(ManualEntry(
            label=str(item["label"]),
            name=str(item.get("name", "")),
            spec=str(item.get("spec", "")),
            windows=windows,
        ))
    return entries


def store_entries(meta: dict[str, Any], entries: Sequence[ManualEntry]) -> dict[str, Any]:
    """Write the list back into _meta (dropping the key when empty)."""
    out = dict(meta)
    if entries:
        out[META_KEY] = [e.to_json() for e in entries]
    else:
        out.pop(META_KEY, None)
    return out


def manual_labels(meta: dict[str, Any]) -> set[str]:
    """Labels a human added by hand (used to bypass machine-only heuristics)."""
    return {e.label for e in load_entries(meta)}


_LABEL_RE = re.compile(r"^SPEAKER_(\d+)$")


def next_free_label(mapping: dict[str, str], entries: Sequence[ManualEntry] = ()) -> str:
    """
    First unused SPEAKER_NN, two digits.

    Computed server-side on every save: the number rendered in the form is a
    hint for the eye, never an instruction the browser can pin down.
    """
    used = set(mapping) | {e.label for e in entries}
    taken = {int(m.group(1)) for label in used if (m := _LABEL_RE.match(label))}
    n = 0
    while n in taken:
        n += 1
    return f"SPEAKER_{n:02d}"


# ─────────────────────────────────────────────────────────────
# Rebuilding diarization.rttm
# ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _Segment:
    start: float
    duration: float
    label: str

    @property
    def end(self) -> float:
        return self.start + self.duration


def _parse_rttm(path: Path) -> tuple[str, list[_Segment]]:
    """RTTM file → (uri, segments). Unparsable lines are skipped."""
    uri = "audio"
    segments: list[_Segment] = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return uri, segments
    for line in lines:
        parts = line.split()
        if len(parts) < 8 or parts[0] != "SPEAKER":
            continue
        try:
            start, duration = float(parts[3]), float(parts[4])
        except ValueError:
            continue
        uri = parts[1]
        segments.append(_Segment(start=start, duration=duration, label=parts[7]))
    return uri, segments


def _rttm_line(uri: str, seg: _Segment) -> str:
    return (
        f"SPEAKER {uri} 1 {seg.start:.3f} {seg.duration:.3f} "
        f"<NA> <NA> {seg.label} <NA> <NA>"
    )


def _subtract(seg: _Segment, windows: Sequence[tuple[float, float]]) -> list[_Segment]:
    """seg minus the manual windows → 0, 1 or 2 pieces (slivers dropped)."""
    pieces = [(seg.start, seg.end)]
    for w_start, w_end in windows:
        nxt: list[tuple[float, float]] = []
        for start, end in pieces:
            if w_end <= start or w_start >= end:
                nxt.append((start, end))
                continue
            if start < w_start:
                nxt.append((start, w_start))
            if w_end < end:
                nxt.append((w_end, end))
        pieces = nxt
    return [
        _Segment(start=s, duration=e - s, label=seg.label)
        for s, e in pieces
        if e - s >= MIN_PIECE_S
    ]


def ensure_pristine(rttm_path: Path) -> Path:
    """
    Copy pyannote's RTTM aside once, before the first manual edit.

    The rebuild always starts from this file, so an edit never builds on a
    previous edit and removing every manual speaker restores the original.
    """
    pristine = Path(rttm_path).with_name(PRISTINE_NAME)
    if not pristine.exists() and Path(rttm_path).exists():
        shutil.copy2(rttm_path, pristine)
    return pristine


def rebuild_rttm(rttm_path: Path, entries: Sequence[ManualEntry]) -> int:
    """
    Rewrite diarization.rttm as pyannote's segments + the manual ones.

    Returns the number of manual lines written. Without a pristine copy (no
    manual edit was ever made and the RTTM is missing) this is a no-op.
    """
    rttm_path = Path(rttm_path)
    pristine = ensure_pristine(rttm_path)
    if not pristine.exists():
        return 0

    uri, base = _parse_rttm(pristine)
    windows = _merge_windows(w for e in entries for w in e.windows)

    kept: list[_Segment] = []
    for seg in base:
        kept.extend(_subtract(seg, windows))

    manual = [
        _Segment(start=s, duration=e - s, label=entry.label)
        for entry in entries
        for s, e in entry.windows
    ]
    everything = sorted(kept + manual, key=lambda s: (s.start, s.label))
    rttm_path.write_text(
        "\n".join(_rttm_line(uri, s) for s in everything) + "\n", encoding="utf-8"
    )
    return len(manual)


# ─────────────────────────────────────────────────────────────
# Applying an editor submission
# ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ManualInput:
    """One manual row as submitted: existing entries carry a label, new ones don't."""
    spec: str
    name: str
    label: str = ""
    remove: bool = False


@dataclass
class ApplyResult:
    """Outcome of applying the manual rows — never partially applied."""
    meta: dict[str, Any]
    mapping: dict[str, str]
    entries: list[ManualEntry]
    notes: list[str]
    error: Optional[str] = None

    @property
    def changed(self) -> bool:
        return bool(self.notes)


def apply_edits(
    transcript_path: Path,
    meta: dict[str, Any],
    mapping: dict[str, str],
    inputs: Sequence[ManualInput],
) -> ApplyResult:
    """
    Fold the editor's manual rows into (meta, mapping) — in memory only.

    The caller writes speakers.json and calls rebuild_rttm(); this stays pure so
    a bad timestamp costs nothing. The first error aborts everything: half-applying
    a submission and queueing a several-hour re-run on it is worse than refusing.
    """
    entries = {e.label: e for e in load_entries(meta)}
    mapping = dict(mapping)
    notes: list[str] = []
    segments: Optional[list[tuple[float, float]]] = None

    def resolve(spec: str) -> list[tuple[float, float]]:
        nonlocal segments
        if segments is None:
            segments = load_whisper_segments(transcript_path)
        return resolve_windows(parse_spec(spec), segments)

    try:
        for item in inputs:
            spec, name = item.spec.strip(), item.name.strip()

            if item.remove and item.label:
                if entries.pop(item.label, None) is not None:
                    mapping.pop(item.label, None)
                    notes.append(f"прибрано {item.label}")
                continue

            if item.label:                                  # editing an existing one
                current = entries.get(item.label)
                if current is None:
                    continue
                if not spec:
                    raise TimeSpecError(
                        f"{item.label}: час не може бути порожнім — "
                        f"познач «прибрати», якщо цей спікер зайвий"
                    )
                windows = resolve(spec) if spec != current.spec else current.windows
                if windows != current.windows:
                    notes.append(f"{item.label}: час → {format_spec(windows)}")
                if name != current.name:
                    notes.append(f"{item.label}: імʼя → {name or '(порожнє)'}")
                entries[item.label] = ManualEntry(
                    label=item.label, name=name, spec=spec, windows=windows,
                )
                # Blanking the field blanks the name, exactly as for a clustered
                # speaker — the editor never quietly restores a value.
                mapping[item.label] = name or item.label
                continue

            if not spec and not name:                       # empty row — nothing typed
                continue
            if not spec:
                raise TimeSpecError(
                    f"«{name}»: вкажи час, коли ця людина говорила (напр. 1:23:45)"
                )

            windows = resolve(spec)
            label = next_free_label(mapping, list(entries.values()))
            entries[label] = ManualEntry(
                label=label, name=name, spec=spec, windows=windows,
            )
            mapping[label] = name or label
            notes.append(
                f"додано {label} ({name or 'без імені'}) — {format_spec(windows)}"
            )
    except TimeSpecError as e:
        return ApplyResult(meta=meta, mapping=mapping, entries=[], notes=[], error=str(e))

    ordered = [entries[k] for k in sorted(entries)]
    return ApplyResult(
        meta=store_entries(meta, ordered), mapping=mapping, entries=ordered, notes=notes,
    )


NEW_TIME_FIELD = "manual_new_time"
NEW_NAME_FIELD = "manual_new_name"


def inputs_from_form(form: Any, labels: Iterable[str]) -> list[ManualInput]:
    """
    Editor form → ManualInput list (both speaker editors submit the same fields).

    `labels` are the manual entries already on file; every other row in the form
    is an ordinary cluster and is left to the caller's name handling.
    """
    inputs = [
        ManualInput(
            label=label,
            spec=str(form.get(f"time_{label}", "")),
            name=str(form.get(f"name_{label}", "")),
            remove=bool(form.get(f"remove_{label}")),
        )
        for label in sorted(labels)
    ]
    new_spec = str(form.get(NEW_TIME_FIELD, ""))
    new_name = str(form.get(NEW_NAME_FIELD, ""))
    if new_spec.strip() or new_name.strip():
        inputs.append(ManualInput(spec=new_spec, name=new_name))
    return inputs


# ─────────────────────────────────────────────────────────────
# CLI smoke test
# ─────────────────────────────────────────────────────────────

def _smoke_test() -> None:  # pragma: no cover - run by hand / by the suite
    import tempfile

    print("=" * 70)
    print("  ingestion.manual_speakers — smoke test")
    print("=" * 70)

    assert parse_timestamp("45") == 45.0
    assert parse_timestamp("12:30") == 750.0
    assert parse_timestamp("1:23:45") == 5025.0
    assert parse_timestamp("2:03.5") == 123.5
    for bad in ("", "abc", "12:99", "1:2:3:4", "12:60"):
        try:
            parse_timestamp(bad)
            raise AssertionError(f"{bad!r} should not parse")
        except TimeSpecError:
            pass
    print("1. parse_timestamp ✓ (SS / M:SS / H:MM:SS, junk refused)")

    assert parse_spec("12:30") == [(750.0, None)]
    assert parse_spec("12:30, 1:00:00") == [(750.0, None), (3600.0, None)]
    assert parse_spec("12:30-12:50") == [(750.0, 770.0)]
    assert parse_spec("12:30 – 12:50") == [(750.0, 770.0)]
    try:
        parse_spec("12:50-12:30")
        raise AssertionError("backwards range should be refused")
    except TimeSpecError:
        pass
    print("2. parse_spec ✓ (lists, ranges, backwards range refused)")

    segments = [(0.0, 10.0), (10.0, 20.0), (20.0, 26.0), (100.0, 110.0)]
    # A bare timestamp claims the phrase spoken at that moment, whole.
    assert resolve_windows([(12.0, None)], segments) == [(10.0, 20.0)]
    # In silence there is no phrase to claim — a short window still records it.
    assert resolve_windows([(50.0, None)], segments) == [(50.0, 50.0 + GAP_WINDOW_S)]
    # A range takes the segments it mostly covers, whole; the one it barely
    # clips (20.0–26.0, 1s of 6s) stays with its original speaker.
    assert resolve_windows([(11.0, 21.0)], segments) == [(10.0, 21.0)]
    assert resolve_windows([(11.0, 25.0)], segments) == [(10.0, 26.0)]
    # Overlapping claims coalesce instead of producing two RTTM lines.
    assert resolve_windows([(2.0, None), (5.0, None)], segments) == [(0.0, 10.0)]
    print("3. resolve_windows ✓ (snap to phrase, gap fallback, ratio, merge)")

    assert next_free_label({"SPEAKER_00": "a", "SPEAKER_01": "b"}) == "SPEAKER_02"
    assert next_free_label({"SPEAKER_00": "a", "SPEAKER_02": "b"}) == "SPEAKER_01"
    assert next_free_label({}) == "SPEAKER_00"
    print("4. next_free_label ✓ (fills gaps, never collides)")

    with tempfile.TemporaryDirectory() as d:
        base = Path(d)
        rttm = base / "diarization.rttm"
        transcript = base / "audio_transcript.json"
        rttm.write_text(
            "SPEAKER audio 1 0.000 10.000 <NA> <NA> SPEAKER_00 <NA> <NA>\n"
            "SPEAKER audio 1 10.000 10.000 <NA> <NA> SPEAKER_01 <NA> <NA>\n"
            "SPEAKER audio 1 20.000 6.000 <NA> <NA> SPEAKER_00 <NA> <NA>\n",
            encoding="utf-8",
        )
        transcript.write_text(json.dumps({"segments": [
            {"start": s, "end": e, "text": "x"} for s, e in segments[:3]
        ]}), encoding="utf-8")

        meta = {"needs_review": ["SPEAKER_01"]}
        mapping = {"SPEAKER_00": "Богдан", "SPEAKER_01": "Вячеслав"}

        res = apply_edits(transcript, meta, mapping,
                          [ManualInput(spec="0:12", name="Гість Іван")])
        assert res.error is None, res.error
        assert res.mapping["SPEAKER_02"] == "Гість Іван"
        assert res.meta["needs_review"] == ["SPEAKER_01"], "other _meta keys survive"
        assert [e.windows for e in res.entries] == [[(10.0, 20.0)]]
        print(f"5. apply_edits ✓ ({res.notes[0]})")

        written = rebuild_rttm(rttm, res.entries)
        assert written == 1
        assert (base / PRISTINE_NAME).exists(), "pyannote's own output must be kept"
        _, segs = _parse_rttm(rttm)
        by_label = {s.label: s for s in segs}
        assert by_label["SPEAKER_02"].start == 10.0 and by_label["SPEAKER_02"].duration == 10.0
        # The window was TAKEN from SPEAKER_01, not merely added alongside it:
        # otherwise the dominant-overlap vote would still go to SPEAKER_01.
        assert "SPEAKER_01" not in by_label, "manual window must be subtracted"
        assert sum(1 for s in segs if s.label == "SPEAKER_00") == 2
        print("6. rebuild_rttm ✓ (pristine kept, window subtracted from the loser)")

        # A bad time aborts the whole submission — nothing half-applied.
        bad = apply_edits(transcript, res.meta, res.mapping,
                          [ManualInput(spec="хтозна", name="Ще один")])
        assert bad.error and "не зрозумів" in bad.error
        assert not bad.notes
        print(f"7. bad timestamp refuses the whole edit ✓ ({bad.error})")

        # Removing the manual speaker restores pyannote's file byte for byte.
        gone = apply_edits(transcript, res.meta, res.mapping,
                           [ManualInput(label="SPEAKER_02", spec="0:12",
                                        name="Гість Іван", remove=True)])
        assert gone.error is None and "SPEAKER_02" not in gone.mapping
        assert META_KEY not in gone.meta
        rebuild_rttm(rttm, gone.entries)
        assert rttm.read_text() == (base / PRISTINE_NAME).read_text()
        print("8. removal restores the original diarization ✓")

    print("=" * 70)
    print("  ✓ ALL SMOKE TESTS PASSED")
    print("=" * 70)


if __name__ == "__main__":
    _smoke_test()
