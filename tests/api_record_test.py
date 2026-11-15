"""Joining the phone's pieces into one audio file — the part that touches disk."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from church_assistant.web.routes.api_record import (
    AUDIO_NAME, PARTS_DIR, AssembleError, assemble, list_parts, part_name,
)


def _parts(meeting: Path, n: int) -> Path:
    d = meeting / PARTS_DIR
    d.mkdir(parents=True)
    for i in range(n):
        (d / part_name(i)).write_bytes(bytes([i]) * 10)
    return d


def test_part_names_sort_as_numbers():
    assert part_name(0) == "000.m4a" and part_name(12) == "012.m4a"
    assert part_name(9) < part_name(10) < part_name(100)


def test_list_parts_ignores_half_written_uploads(tmp_path: Path):
    d = _parts(tmp_path, 2)
    (d / "002.m4a.part").write_bytes(b"x")
    (d / "concat.txt").write_text("file 'x'\n")
    assert [p["n"] for p in list_parts(d)] == [0, 1]
    assert list_parts(tmp_path / "nope") == []


def test_one_part_is_renamed_without_ffmpeg(tmp_path: Path):
    _parts(tmp_path, 1)
    out = asyncio.run(assemble(tmp_path, 1, ffmpeg=None))
    assert out == tmp_path / AUDIO_NAME and out.read_bytes() == bytes([0]) * 10
    assert not (tmp_path / PARTS_DIR / part_name(0)).exists()


def test_missing_part_is_refused_and_nothing_is_touched(tmp_path: Path):
    d = _parts(tmp_path, 3)
    (d / part_name(1)).unlink()
    with pytest.raises(AssembleError, match="бракує шматків: 1"):
        asyncio.run(assemble(tmp_path, 3, ffmpeg="/usr/bin/true"))
    assert (d / part_name(0)).exists() and (d / part_name(2)).exists()
    assert not (tmp_path / AUDIO_NAME).exists()


def test_several_parts_without_ffmpeg_keep_the_pieces(tmp_path: Path):
    d = _parts(tmp_path, 2)
    with pytest.raises(AssembleError, match="ffmpeg"):
        asyncio.run(assemble(tmp_path, 2, ffmpeg=None))
    assert len(list_parts(d)) == 2


def test_ffmpeg_failure_leaves_the_pieces(tmp_path: Path):
    d = _parts(tmp_path, 2)
    with pytest.raises(AssembleError, match="ffmpeg"):
        asyncio.run(assemble(tmp_path, 2, ffmpeg="/usr/bin/false"))
    assert len(list_parts(d)) == 2 and not (tmp_path / AUDIO_NAME).exists()


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed here")
def test_real_ffmpeg_joins_two_aac_parts(tmp_path: Path):
    """Two half-second tones, encoded like the phone encodes, come out as one
    file of about a second with the parts folder gone."""
    import subprocess
    d = tmp_path / PARTS_DIR
    d.mkdir()
    for i, freq in enumerate((440, 880)):
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=0.5",
            "-ac", "1", "-ar", "44100", "-c:a", "aac", "-b:a", "64k", str(d / part_name(i)),
        ], check=True)
    out = asyncio.run(assemble(tmp_path, 2, ffmpeg=shutil.which("ffmpeg")))
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, check=True,
    )
    assert 0.9 < float(probe.stdout.strip()) < 1.3
    assert not d.exists()


# ─── the streaming receiver behind parts and whole-file uploads ──────────────

class _FakeRequest:
    """Just what _receive reads: the headers and the body stream."""

    def __init__(self, chunks: list[bytes], declared: str | None = None):
        self.headers = {"content-length": declared} if declared is not None else {}
        self._chunks = chunks

    async def stream(self):
        for c in self._chunks:
            yield c


def _recv(req: _FakeRequest, tmp: Path, limit: int = 100) -> int:
    from church_assistant.web.routes.api_record import _receive
    return asyncio.run(_receive(req, tmp, limit=limit, too_big="big", empty="empty"))


def test_receive_writes_the_body_and_returns_its_size(tmp_path: Path):
    tmp = tmp_path / "audio.mp3.part"
    assert _recv(_FakeRequest([b"ab", b"cde"]), tmp) == 5
    assert tmp.read_bytes() == b"abcde"


def test_receive_refuses_a_declared_oversize_before_reading(tmp_path: Path):
    from fastapi import HTTPException
    tmp = tmp_path / "x.part"
    with pytest.raises(HTTPException) as e:
        _recv(_FakeRequest([b"x"], declared="101"), tmp)
    assert e.value.status_code == 413 and not tmp.exists()


def test_receive_stops_an_undeclared_oversize_midway_and_cleans_up(tmp_path: Path):
    from fastapi import HTTPException
    tmp = tmp_path / "x.part"
    with pytest.raises(HTTPException) as e:
        _recv(_FakeRequest([b"a" * 60, b"b" * 60]), tmp)
    assert e.value.status_code == 413 and not tmp.exists()


def test_receive_rejects_an_empty_body(tmp_path: Path):
    from fastapi import HTTPException
    tmp = tmp_path / "x.part"
    with pytest.raises(HTTPException) as e:
        _recv(_FakeRequest([]), tmp)
    assert e.value.status_code == 400 and not tmp.exists()

