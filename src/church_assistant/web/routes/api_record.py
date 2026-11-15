"""
The JSON API — a meeting recorded on the phone, sent in pieces.

    POST   /api/v1/recordings                    {date} → reserve the date, {date, parts}
    PUT    /api/v1/recordings/{date}/parts/{n}   one closed 10-minute file (audio/mp4 body)
    GET    /api/v1/recordings/{date}             which parts the server holds
    POST   /api/v1/recordings/{date}/finish      {parts, seconds} → audio.m4a + a job
    DELETE /api/v1/recordings/{date}             throw the pieces away (nothing assembled yet)
    PUT    /api/v1/uploads/{date}?name=<file>    a whole file recorded elsewhere → audio.<ext> + a job

WHY PIECES. A council meeting runs two hours; a phone in a church basement
drops off the network and the app may be killed by Android at any moment. One
upload at the end would lose the whole evening to any of that. So the app
closes a file every ten minutes and sends it at once; whatever reached the
server is safe, and a part that failed is retried while the recording goes on
(decided 24.10.2026, docs/session_2026-10-24.md «Що далі» п.2).

WHAT COMES OUT IS THE SAME AS A WEB UPLOAD. `finish` joins the parts into
<meetings>/<date>/audio.m4a with ffmpeg (`-c copy`: a re-mux, no re-encode, a
second for an hour of audio) and inserts the same pending ingestion job that
routes/ingest.py inserts for a file picked in the browser. From there the
pipeline on the processing node neither knows nor cares where the audio came
from. The parts sit in <meetings>/<date>/parts/ until the join succeeds, so a
server without ffmpeg loses nothing: it keeps the pieces and says so.

The date is the whole identity, as everywhere else in this codebase (one
meeting per church per day). Reserving a date that already has a job or an
audio file answers 409, and the app lets the person pick another day.

A FILE MADE ELSEWHERE (Zoom, a dictaphone, a file on Google Drive) takes the
short road: one PUT with the whole file, streamed to <meetings>/<date>/audio.<ext>
exactly as routes/ingest.py stores a browser upload, then the same job. The
suffix is kept because the pipeline resolves `audio.*` by name
(ingestion/paths.py); the original file name goes into the job for the list.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from psycopg.errors import UniqueViolation
from pydantic import BaseModel, Field

from church_assistant.db import audit_repo
from church_assistant.db import ingestion_jobs_repo as jobs_repo
from church_assistant.db.connection import get_pool
from church_assistant.shared import tenant_paths
from church_assistant.shared.logger import Logger
from church_assistant.web import encrypted
from church_assistant.web.routes.api import ApiUser, api_user


router = APIRouter(prefix="/api/v1")
_logger = Logger(process="web")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PARTS_DIR = "parts"
META_FILE = "recording.json"
AUDIO_NAME = "audio.m4a"
MAX_PARTS = 1000
# Ten minutes of mono AAC at 64 kbit/s is ~5 MB; a hundred times that is a
# bug or an attack, not a recording.
MAX_PART_BYTES = 64 * 1024 * 1024
FFMPEG_TIMEOUT_S = 600
# What routes/ingest.py takes from the browser — the same list, so a file the
# site accepts the phone accepts too.
UPLOAD_SUFFIXES = {".m4a", ".mp3", ".wav", ".aac", ".ogg", ".flac", ".opus", ".mp4", ".webm"}
# A three-hour Zoom recording as video is ~1 GB; a WAV of the same is ~2 GB.
MAX_UPLOAD_BYTES = 3 * 1024 * 1024 * 1024


class StartRecording(BaseModel):
    date: str = Field(min_length=10, max_length=10)
    device: Optional[str] = Field(default=None, max_length=120)


class FinishRecording(BaseModel):
    parts: int = Field(ge=1, le=MAX_PARTS)
    seconds: int = Field(ge=0, le=24 * 3600)


def _meetings_dir(user: ApiUser) -> Path:
    return tenant_paths.paths_for(user.tenant_slug).meetings


def _find_audio(mdir: Path, date: str) -> Optional[Path]:
    # routes.meetings pulls in web.main and, through it, every router; imported
    # at call time so this module (and its tests) can load on their own.
    from church_assistant.web.routes.meetings import _find_audio as find
    return find(mdir, date)


def _check_date(date: str) -> None:
    if not _DATE_RE.match(date):
        raise HTTPException(status_code=404, detail="Немає такої дати.")
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=404, detail="Немає такої дати.")


def part_name(n: int) -> str:
    return f"{n:03d}.m4a"


def list_parts(parts_dir: Path) -> list[dict[str, int]]:
    """[{n, bytes}] for every closed part on disk, in order. Half-written
    uploads (`.part` suffix) are not parts."""
    out: list[dict[str, int]] = []
    if not parts_dir.is_dir():
        return out
    for p in sorted(parts_dir.glob("[0-9][0-9][0-9].m4a")):
        out.append({"n": int(p.stem), "bytes": p.stat().st_size})
    return out


def _status(meeting_dir: Path) -> dict[str, Any]:
    audio = meeting_dir / AUDIO_NAME
    return {
        "date": meeting_dir.name,
        "parts": list_parts(meeting_dir / PARTS_DIR),
        "finished": audio.exists(),
    }


async def _busy_reason(pool: Any, user: ApiUser, mdir: Path, date: str) -> Optional[str]:
    """Why this date cannot take a new recording — or None."""
    if encrypted.folder_is_sealed(mdir / date):
        return "Ця зустріч зашифрована — запис із телефона поки неможливий."
    if _find_audio(mdir, date) is not None:
        return "За цю дату вже є запис. Оберіть іншу дату."
    job = await jobs_repo.get_by_date(pool, user.tenant_id, date)
    if job is not None and job["status"] != "cancelled":
        return "За цю дату вже є запис в обробці. Оберіть іншу дату."
    return None


class AssembleError(RuntimeError):
    """The parts are intact on disk; only the join did not happen."""


def ffmpeg_path() -> Optional[str]:
    return shutil.which("ffmpeg")


async def assemble(meeting_dir: Path, n_parts: int, *, ffmpeg: Optional[str]) -> Path:
    """
    Join parts/000.m4a … parts/{n-1}.m4a into <meeting_dir>/audio.m4a.

    One part is renamed, nothing else needed. Several are re-muxed by ffmpeg's
    concat demuxer with `-c copy` — the AAC frames are carried over untouched,
    so the join costs seconds and the sound is exactly what the phone heard.
    `+faststart` moves the index to the front so the app's player starts on
    the first bytes rather than after the whole file.

    Raises AssembleError and leaves the parts where they are on any failure.
    """
    parts_dir = meeting_dir / PARTS_DIR
    have = {p["n"] for p in list_parts(parts_dir)}
    missing = [n for n in range(n_parts) if n not in have]
    if missing:
        raise AssembleError(
            f"Серверу бракує шматків: {', '.join(str(m) for m in missing[:5])}"
            + ("…" if len(missing) > 5 else "")
        )
    out = meeting_dir / AUDIO_NAME
    tmp = meeting_dir / (AUDIO_NAME + ".part")
    if tmp.exists():
        tmp.unlink()

    if n_parts == 1:
        shutil.move(str(parts_dir / part_name(0)), str(tmp))
        tmp.rename(out)
        return out

    if ffmpeg is None:
        raise AssembleError("На сервері немає ffmpeg — шматки збережено, завершіть пізніше.")

    listing = parts_dir / "concat.txt"
    # The concat demuxer takes one `file '<path>'` per line; a single quote in
    # the path would need escaping, but these names are ours (digits only).
    listing.write_text(
        "".join(f"file '{(parts_dir / part_name(n)).resolve()}'\n" for n in range(n_parts)),
        encoding="utf-8",
    )
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "concat", "-safe", "0", "-i", str(listing),
        "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(tmp),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, err = await asyncio.wait_for(proc.communicate(), timeout=FFMPEG_TIMEOUT_S)
        except asyncio.TimeoutError:
            proc.kill()
            raise AssembleError("ffmpeg не вклався в час — шматки збережено.")
    except OSError as e:
        raise AssembleError(f"Не вдалося запустити ffmpeg: {e}")
    if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        if tmp.exists():
            tmp.unlink()
        tail = (err or b"").decode("utf-8", "replace").strip().splitlines()[-1:] or ["?"]
        raise AssembleError(f"ffmpeg відмовив: {tail[0][:200]}")
    tmp.rename(out)
    shutil.rmtree(parts_dir, ignore_errors=True)
    return out


# ─────────────────────────────────────────────────────────────

async def _open_job(pool: Any, user: ApiUser, date: str, meeting_dir: Path, *, original: str, audio_filename: str) -> int:
    """Insert the pending job for a date — or start over the cancelled one.
    A date keeps its row forever (unique per tenant), so a date that was
    recorded, cancelled and recorded again revives the old row."""
    existing = await jobs_repo.get_by_date(pool, user.tenant_id, date)
    if existing is None:
        try:
            return int(await jobs_repo.insert_job(
                pool, user.tenant_id, meeting_date=date, meeting_dir=str(meeting_dir),
                original_filename=original, audio_filename=audio_filename,
            ))
        except UniqueViolation:
            raise HTTPException(status_code=409, detail="За цю дату вже є запис в обробці.")
    if existing["status"] == "cancelled":
        job_id = int(existing["id"])
        await jobs_repo.revive(
            pool, user.tenant_id, job_id, meeting_dir=str(meeting_dir),
            original_filename=original, audio_filename=audio_filename,
        )
        return job_id
    raise HTTPException(status_code=409, detail="За цю дату вже є запис в обробці.")


async def _receive(request: Request, tmp: Path, *, limit: int, too_big: str, empty: str) -> int:
    """Stream the request body into `tmp`; on any failure the file is gone.
    Returns the byte count."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(status_code=413, detail=too_big)
    size = 0
    try:
        with tmp.open("wb") as out:
            async for chunk in request.stream():
                size += len(chunk)
                if size > limit:
                    raise HTTPException(status_code=413, detail=too_big)
                out.write(chunk)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if size == 0:
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=empty)
    return size


@router.post("/recordings", status_code=201)
async def start_recording(request: Request, body: StartRecording):
    user = await api_user(request)
    _check_date(body.date)
    pool = await get_pool()
    mdir = _meetings_dir(user)
    reason = await _busy_reason(pool, user, mdir, body.date)
    if reason:
        raise HTTPException(status_code=409, detail=reason)

    meeting_dir = mdir / body.date
    (meeting_dir / PARTS_DIR).mkdir(parents=True, exist_ok=True)
    meta_path = meeting_dir / META_FILE
    if not meta_path.exists():
        meta_path.write_text(json.dumps({
            "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "by": user.username,
            "device": body.device or "",
        }, ensure_ascii=False), encoding="utf-8")
        await _logger.info(
            "recording.started",
            message=f"{body.date}: recording from the phone by {user.username}",
            metadata={"date": body.date, "device": body.device or ""},
            tenant_id=user.tenant_id, user_id=user.user_id,
        )
    return _status(meeting_dir)


@router.get("/recordings/{date}")
async def recording_status(request: Request, date: str):
    user = await api_user(request)
    _check_date(date)
    meeting_dir = _meetings_dir(user) / date
    if not (meeting_dir / PARTS_DIR).is_dir() and not (meeting_dir / META_FILE).exists():
        raise HTTPException(status_code=404, detail="Запис не починали.")
    return _status(meeting_dir)


@router.put("/recordings/{date}/parts/{n}")
async def upload_part(request: Request, date: str, n: int):
    """One closed piece. Written to a `.part` file and renamed when complete, so
    a connection that drops halfway never leaves a truncated part behind; a
    repeated PUT of the same number simply replaces it."""
    user = await api_user(request)
    _check_date(date)
    if not 0 <= n < MAX_PARTS:
        raise HTTPException(status_code=404, detail="Немає такого шматка.")
    meeting_dir = _meetings_dir(user) / date
    parts_dir = meeting_dir / PARTS_DIR
    if not parts_dir.is_dir():
        raise HTTPException(status_code=409, detail="Запис не починали або вже завершено.")
    tmp = parts_dir / (part_name(n) + ".part")
    await _receive(request, tmp, limit=MAX_PART_BYTES, too_big="Шматок надто великий.", empty="Порожній шматок.")
    os.replace(tmp, parts_dir / part_name(n))
    return _status(meeting_dir)


@router.post("/recordings/{date}/finish")
async def finish_recording(request: Request, date: str, body: FinishRecording):
    user = await api_user(request)
    _check_date(date)
    pool = await get_pool()
    mdir = _meetings_dir(user)
    meeting_dir = mdir / date

    # Already done (a retry after the reply was lost): say so, do nothing twice.
    existing = await jobs_repo.get_by_date(pool, user.tenant_id, date)
    audio = meeting_dir / AUDIO_NAME
    if existing is not None and existing["status"] != "cancelled" and audio.exists():
        return {"job_id": int(existing["id"]), "bytes": audio.stat().st_size, "repeated": True}

    # Join — unless an earlier call joined and then failed to open the job.
    if not audio.exists():
        if not (meeting_dir / PARTS_DIR).is_dir():
            raise HTTPException(status_code=409, detail="Запис не починали або вже завершено.")
        if _find_audio(mdir, date) is not None:
            raise HTTPException(status_code=409, detail="За цю дату вже є запис.")
        try:
            audio = await assemble(meeting_dir, body.parts, ffmpeg=ffmpeg_path())
        except AssembleError as e:
            await _logger.warn(
                "recording.assemble_failed", message=f"{date}: {e}",
                metadata={"date": date, "parts": body.parts}, tenant_id=user.tenant_id,
            )
            raise HTTPException(status_code=503, detail=str(e))

    original = f"phone-recording-{date}.m4a"
    job_id = await _open_job(pool, user, date, meeting_dir, original=original, audio_filename=AUDIO_NAME)

    (meeting_dir / META_FILE).unlink(missing_ok=True)
    size_mb = audio.stat().st_size / 1024 / 1024
    await _logger.info(
        "ingestion.uploaded",
        message=f"job #{job_id} ({date}) recorded on the phone ({size_mb:.1f} MB, {body.parts} parts, {body.seconds} s)",
        metadata={"job_id": job_id, "size_mb": round(size_mb, 1), "parts": body.parts, "seconds": body.seconds, "via": "app"},
        tenant_id=user.tenant_id, user_id=user.user_id,
    )
    await audit_repo.record(
        pool, tenant_id=user.tenant_id, action="recording.finished", actor=user.actor,
        resource=f"meeting/{date}",
        detail={"job_id": job_id, "parts": body.parts, "seconds": body.seconds, "size_mb": round(size_mb, 1), "via": "app"},
    )
    return {"job_id": job_id, "bytes": audio.stat().st_size}


@router.put("/uploads/{date}")
async def upload_file(request: Request, date: str, name: str = Query(min_length=1, max_length=200)):
    """A whole recording made elsewhere, in one request. `name` is the file's
    own name — its suffix decides audio.<ext>, and it is kept on the job the
    way the browser upload keeps it. Streams to a `.part` file and renames at
    the end, so a dropped connection leaves nothing behind."""
    user = await api_user(request)
    _check_date(date)
    suffix = Path(name).suffix.lower()
    if suffix not in UPLOAD_SUFFIXES:
        raise HTTPException(
            status_code=415,
            detail=f"Непідтримуваний формат ({suffix or 'без розширення'}). "
                   "Підійдуть: " + ", ".join(sorted(x.lstrip(".") for x in UPLOAD_SUFFIXES)) + ".",
        )
    pool = await get_pool()
    mdir = _meetings_dir(user)
    meeting_dir = mdir / date
    reason = await _busy_reason(pool, user, mdir, date)
    if reason is not None:
        raise HTTPException(status_code=409, detail=reason)
    if (meeting_dir / PARTS_DIR).is_dir():
        raise HTTPException(status_code=409, detail="За цю дату йде запис із телефона. Оберіть іншу дату.")

    meeting_dir.mkdir(parents=True, exist_ok=True)
    audio_filename = f"audio{suffix}"
    tmp = meeting_dir / (audio_filename + ".part")
    size = await _receive(request, tmp, limit=MAX_UPLOAD_BYTES, too_big="Файл надто великий (понад 3 ГБ).", empty="Порожній файл.")
    audio = meeting_dir / audio_filename
    os.replace(tmp, audio)

    try:
        job_id = await _open_job(pool, user, date, meeting_dir, original=name, audio_filename=audio_filename)
    except HTTPException:
        # Lost the race for the date: the file must not sit there as a ghost
        # that makes the date «busy» for everyone.
        audio.unlink(missing_ok=True)
        raise

    size_mb = size / 1024 / 1024
    await _logger.info(
        "ingestion.uploaded",
        message=f"job #{job_id} ({date}) file sent from the phone ({size_mb:.1f} MB): {name}",
        metadata={"job_id": job_id, "size_mb": round(size_mb, 1), "via": "app-file", "name": name},
        tenant_id=user.tenant_id, user_id=user.user_id,
    )
    await audit_repo.record(
        pool, tenant_id=user.tenant_id, action="recording.uploaded", actor=user.actor,
        resource=f"meeting/{date}",
        detail={"job_id": job_id, "size_mb": round(size_mb, 1), "name": name, "via": "app-file"},
    )
    return {"job_id": job_id, "bytes": size}


@router.delete("/recordings/{date}")
async def discard_recording(request: Request, date: str):
    """Only the pieces of an unfinished recording. Once audio.m4a exists the
    meeting is a meeting, and deleting those is not the app's business."""
    user = await api_user(request)
    _check_date(date)
    mdir = _meetings_dir(user)
    meeting_dir = mdir / date
    if _find_audio(mdir, date) is not None:
        raise HTTPException(status_code=409, detail="Запис уже зібрано — видалити його звідси не можна.")
    shutil.rmtree(meeting_dir / PARTS_DIR, ignore_errors=True)
    (meeting_dir / META_FILE).unlink(missing_ok=True)
    try:
        meeting_dir.rmdir()          # only if nothing else ever lived here
    except OSError:
        pass
    await _logger.info("recording.discarded", message=f"{date}: pieces discarded by {user.username}",
                       metadata={"date": date}, tenant_id=user.tenant_id, user_id=user.user_id)
    return {"ok": True}
