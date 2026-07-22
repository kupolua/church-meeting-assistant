"""
Pipeline stages — async subprocess wrappers around the ingestion CLI modules.

Each stage shells out to the SAME `uv run python -m church_assistant.<module>`
command that new_meeting.py uses (verbatim args), so the web-driven pipeline and
the CLI produce identical artifacts and can resume each other's folders.

Stages are resumable: a step is skipped when its output already exists (mirrors
new_meeting.py). A non-zero exit raises StageError, which the processor turns
into a job failure (+ retry).

Phases:
    run_transcription_phase  — diarization (match_speakers) + transcription
                               (transcribe), parallel or sequential. Slow (~2h).
    run_analysis_phase       — merge_transcript → chunked_analyze → polish_protocol.
    run_index                — index_meeting into Qdrant.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Awaitable, Callable, Optional

from church_assistant.ingestion.paths import MeetingPaths


_std = logging.getLogger("church_assistant.ingestion.stages")

MODULE_PREFIX = ["uv", "run", "python", "-m"]

# Project root (…/church-meeting-assistant) — cwd for the subprocesses so that
# `uv run` resolves the project and relative data paths behave like the CLI.
PROJECT_ROOT = Path(__file__).resolve().parents[3]


class StageError(RuntimeError):
    """A pipeline stage exited non-zero (or produced no expected output)."""


# Optional callback invoked with a short human-readable note when a stage starts
# (used by the processor to update ingestion_jobs.progress_note live).
ProgressFn = Callable[[str, str], Awaitable[None]]  # (stage, note) -> None


async def _noop_progress(stage: str, note: str) -> None:  # pragma: no cover
    return None


# ─────────────────────────────────────────────────────────────
# Subprocess runner
# ─────────────────────────────────────────────────────────────

async def _run(cmd: list[str], *, label: str) -> None:
    """
    Run a command, stream its combined output to the logger, raise on failure.

    Uses asyncio subprocess so the worker's event loop stays responsive to
    shutdown signals during long steps.
    """
    _std.info("→ [%s] $ %s", label, " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(PROJECT_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None
    async for raw in proc.stdout:
        line = raw.decode("utf-8", errors="replace").rstrip()
        if line:
            _std.info("[%s] %s", label, line)
    rc = await proc.wait()
    if rc != 0:
        raise StageError(f"stage '{label}' failed (exit {rc})")
    _std.info("✓ [%s] done", label)


# ─────────────────────────────────────────────────────────────
# Individual steps
# ─────────────────────────────────────────────────────────────

async def _diarize(paths: MeetingPaths) -> None:
    if paths.speakers.exists() and paths.rttm.exists():
        _std.info("✓ speakers.json + diarization.rttm exist — skipping match_speakers")
        return
    await _run(
        MODULE_PREFIX + [
            "church_assistant.match_speakers",
            "--audio", str(paths.audio),
            "--output", str(paths.speakers),
            "--rttm", str(paths.rttm),
        ],
        label="diarization",
    )


async def _transcribe(paths: MeetingPaths) -> None:
    if paths.transcript.exists():
        _std.info("✓ %s exists — skipping transcribe", paths.transcript.name)
        return
    await _run(
        MODULE_PREFIX + ["church_assistant.transcribe", str(paths.audio)],
        label="whisper",
    )


async def _merge(paths: MeetingPaths) -> None:
    if paths.annotated.exists():
        _std.info("✓ %s exists — skipping merge", paths.annotated.name)
        return
    await _run(
        MODULE_PREFIX + [
            "church_assistant.merge_transcript",
            "--transcript", str(paths.transcript),
            "--rttm", str(paths.rttm),
            "--speakers", str(paths.speakers),
            "--output", str(paths.annotated),
        ],
        label="merge",
    )


async def _analyze(paths: MeetingPaths) -> None:
    if paths.chunks_dir.exists() and any(paths.chunks_dir.iterdir()) and paths.chunked.exists():
        _std.info("✓ chunks/ and chunked.md exist — skipping chunked_analyze")
        return
    await _run(
        MODULE_PREFIX + [
            "church_assistant.chunked_analyze",
            "--transcript", str(paths.annotated),
            "--output-dir", str(paths.chunks_dir),
            "--final-output", str(paths.chunked),
        ],
        label="analyze",
    )


async def _polish(paths: MeetingPaths, *, polish_date: str) -> None:
    if paths.polished.exists():
        _std.info("✓ %s exists — skipping polish", paths.polished.name)
        return
    await _run(
        MODULE_PREFIX + [
            "church_assistant.polish_protocol",
            "--chunks-dir", str(paths.chunks_dir),
            "--output", str(paths.polished),
            "--rttm", str(paths.rttm),
            "--speakers-map", str(paths.speakers),
            "--date", polish_date,
        ],
        label="polish",
    )


# ─────────────────────────────────────────────────────────────
# Phases
# ─────────────────────────────────────────────────────────────

async def run_transcription_phase(
    paths: MeetingPaths,
    *,
    sequential: bool = False,
    progress: ProgressFn = _noop_progress,
) -> None:
    """
    Diarization + transcription (the slow ~2h phase).

    Both steps are independent; run them concurrently unless sequential=True
    (lower peak memory). Each self-skips if its output already exists.
    Validates the required outputs before returning.
    """
    need_diar = not (paths.speakers.exists() and paths.rttm.exists())
    need_tx = not paths.transcript.exists()

    if sequential or not (need_diar and need_tx):
        # Nothing to parallelize (or user forced sequential): run in order.
        await progress("diarization", "Діаризація (pyannote)…")
        await _diarize(paths)
        await progress("whisper", "Транскрипція (Whisper)…")
        await _transcribe(paths)
    else:
        await progress("diarization", "Діаризація + транскрипція (паралельно)…")
        results = await asyncio.gather(
            _diarize(paths), _transcribe(paths), return_exceptions=True
        )
        errors = [r for r in results if isinstance(r, BaseException)]
        if errors:
            # Surface the first error (both are logged by _run already).
            raise StageError(f"transcription phase failed: {errors[0]}")

    # Sanity: required outputs must exist before we pause for review.
    missing = [
        p.name for p in (paths.transcript, paths.speakers, paths.rttm)
        if not p.exists()
    ]
    if missing:
        raise StageError(f"transcription phase produced no {missing}")


async def run_analysis_phase(
    paths: MeetingPaths,
    *,
    polish_date: str,
    progress: ProgressFn = _noop_progress,
) -> None:
    """
    merge_transcript → chunked_analyze (Gemma) → polish_protocol.

    Runs after the human speakers.json review. Each step self-skips if done.
    """
    await progress("merge", "Обʼєднання транскрипту з діаризацією…")
    await _merge(paths)

    await progress("analyze", "Аналіз чанків (Gemma)…")
    await _analyze(paths)

    await progress("polish", "Фінальне полірування протоколу…")
    await _polish(paths, polish_date=polish_date)

    if not paths.polished.exists():
        raise StageError("analysis phase produced no polished.md")


async def run_index(meeting_dir: Path, *, progress: ProgressFn = _noop_progress) -> None:
    """Index the finished meeting into Qdrant (auto-index step)."""
    await progress("index", "Індексація у Qdrant…")
    await _run(
        MODULE_PREFIX + [
            "church_assistant.index_meeting",
            "--meeting-dir", str(meeting_dir),
        ],
        label="index",
    )


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def count_speakers(speakers_path: Path) -> Optional[int]:
    """Number of speaker entries in speakers.json (None if unreadable)."""
    try:
        data = json.loads(speakers_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return len(data)
    except (OSError, json.JSONDecodeError):
        pass
    return None
