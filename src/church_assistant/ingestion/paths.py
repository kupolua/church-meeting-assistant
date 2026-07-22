"""
Per-meeting artifact paths.

Resolves every file the pipeline reads/writes inside a meeting folder, matching
the layout produced by new_meeting.py so the web-driven ingestion and the CLI
stay interoperable (either can resume the other's folder).

Folder layout (data/meetings/YYYY-MM-DD/):
    audio.<ext>              — copied-in recording
    audio_transcript.json    — transcribe.py output   (fallback: transcript.json)
    audio_embeddings.pkl     — cached speaker embeddings
    diarization.rttm         — pyannote output
    speakers.json            — SPEAKER_XX → name map (edited during review)
    annotated.md             — merge_transcript output
    chunks/                  — chunked_analyze per-chunk output
    chunked.md               — merged analysis
    polished.md              — final protocol
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class MeetingPaths:
    """All artifact paths for one meeting folder."""
    meeting_dir: Path
    audio: Path
    transcript: Path
    embeddings: Path
    rttm: Path
    speakers: Path
    annotated: Path
    chunks_dir: Path
    chunked: Path
    polished: Path


def _find_audio(meeting_dir: Path, audio_filename: Optional[str]) -> Path:
    """
    Locate the copied-in audio file.

    Prefers the recorded audio_filename; otherwise the first `audio.*` file;
    otherwise defaults to `audio.m4a` (path may not exist yet).
    """
    if audio_filename:
        p = meeting_dir / audio_filename
        if p.exists() or not any(meeting_dir.glob("audio.*")):
            return p
    matches = sorted(meeting_dir.glob("audio.*"))
    if matches:
        return matches[0]
    return meeting_dir / (audio_filename or "audio.m4a")


def resolve(meeting_dir: Path, audio_filename: Optional[str] = None) -> MeetingPaths:
    """
    Resolve all artifact paths for a meeting folder.

    `audio_filename` is the name recorded on the ingestion job (e.g. 'audio.m4a').
    The transcript path mirrors transcribe.py's default (`<stem>_transcript.json`)
    with a fallback to a clean `transcript.json` if that exists instead.
    """
    meeting_dir = Path(meeting_dir)
    audio = _find_audio(meeting_dir, audio_filename)
    stem = audio.stem  # "audio"

    default_transcript = meeting_dir / f"{stem}_transcript.json"
    clean_transcript = meeting_dir / "transcript.json"
    if clean_transcript.exists() and not default_transcript.exists():
        transcript = clean_transcript
    else:
        transcript = default_transcript

    return MeetingPaths(
        meeting_dir=meeting_dir,
        audio=audio,
        transcript=transcript,
        embeddings=meeting_dir / f"{stem}_embeddings.pkl",
        rttm=meeting_dir / "diarization.rttm",
        speakers=meeting_dir / "speakers.json",
        annotated=meeting_dir / "annotated.md",
        chunks_dir=meeting_dir / "chunks",
        chunked=meeting_dir / "chunked.md",
        polished=meeting_dir / "polished.md",
    )
