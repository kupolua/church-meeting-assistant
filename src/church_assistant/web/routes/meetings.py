"""
Meetings routes:
    GET /meetings/{date}         — meeting detail page (attendees + topics + стенограма)
    GET /meetings/{date}/audio   — stream the meeting recording (HTTP Range support,
                                    so clickable timestamps can seek the audio player)
"""

from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse

from church_assistant.shared import meetings_index
from church_assistant.web.main import templates


router = APIRouter(prefix="/meetings")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Browser-friendly media types (mimetypes guesses e.g. 'audio/mp4a-latm' for
# .m4a, which several browsers refuse to play in <audio>).
_AUDIO_MEDIA_TYPES = {
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
    ".webm": "audio/webm",
}


def _audio_media_type(path: Path) -> str:
    """Pick a browser-friendly media type for an audio file."""
    return _AUDIO_MEDIA_TYPES.get(
        path.suffix.lower(),
        mimetypes.guess_type(path.name)[0] or "application/octet-stream",
    )


def _find_audio(date: str) -> Optional[Path]:
    """Locate data/meetings/<date>/audio.* (None if the date is bad or no file)."""
    if not _DATE_RE.match(date):
        return None
    folder = meetings_index.DATA_MEETINGS / date
    if not folder.is_dir():
        return None
    matches = sorted(folder.glob("audio.*"))
    return matches[0] if matches else None


@router.get("/{date}", response_class=HTMLResponse)
async def meeting_detail(request: Request, date: str):
    """Render meeting detail page for a given date (YYYY-MM-DD)."""
    detail = meetings_index.load_detail(date)
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail=f"Meeting {date!r} not found",
        )

    summaries = meetings_index.list_all_summaries()

    return templates.TemplateResponse(
        request,
        "meeting_detail.html",
        {
            "detail": detail,
            "meetings": summaries,
            "current_date": date,
            "has_audio": _find_audio(date) is not None,
        },
    )


@router.get("/{date}/audio")
async def meeting_audio(date: str):
    """
    Serve the meeting recording.

    Starlette's FileResponse handles HTTP Range natively (async file I/O, proper
    client-disconnect handling), returning 206 for range requests. That lets the
    browser's <audio> element seek to any timestamp — the basis for the clickable
    timestamps in topics and the стенограма — without a hand-rolled streamer that
    would tie up threadpool workers on every seek.
    """
    audio_path = _find_audio(date)
    if audio_path is None or not audio_path.exists():
        raise HTTPException(status_code=404, detail="Audio not found")

    return FileResponse(audio_path, media_type=_audio_media_type(audio_path))
