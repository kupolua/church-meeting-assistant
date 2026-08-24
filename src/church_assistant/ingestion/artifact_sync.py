"""
Move a meeting folder between the processing node and the control plane.

After the split (docs/vps_deploy.md) the artifacts live on the VPS — the web has
to serve protocols, transcripts and the audio player around the clock — while
the pipeline runs on the M1, which is the only machine with the models. So the
two halves need the same folder, and something has to carry it.

WHY NOT A NETWORK MOUNT. That was the first attempt: export /srv/cma/data over
NFS and point DATA_ROOT at the mount. It needs no code, and it fails in ways
that are hard to see — uid squashing, NFSv4 string idmapping, and a `soft` mount
that turns a dropped tunnel into a half-written artifact while `hard` turns it
into a process nobody can kill. A three-hour transcription is a long time to
hold that.

Copying instead makes the failure boring. The state machine already serialises
who owns a folder:

    pending / transcribing  → the worker writes    (transcript, rttm, speakers)
    awaiting_review         → the web writes       (speakers.json, profiles)
    analyzing / indexing    → the worker writes    (annotated, chunks, polished)
    completed               → the web writes       (a speakers re-edit)

so a pull when a phase starts and a push when it ends can never race: nobody
else is writing during the phase. A dropped tunnel breaks the copy, the copy
raises, and the job requeues through the retry path that already exists.

The traffic is lopsided and that is the point. Pulling brings the audio — 68 MB,
read once at the start of transcription and then decoded into memory. Pushing
sends what the pipeline produced: about 660 KB per meeting, a thousandth of the
folder.

DISABLED BY DEFAULT. With ARTIFACT_SYNC_REMOTE unset every call is a no-op, so a
single machine holding everything behaves exactly as it always did.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

from church_assistant.ingestion.config import get_artifact_sync_remote, get_artifact_sync_ssh
from church_assistant.shared import tenant_paths


_std = logging.getLogger("church_assistant.ingestion.artifact_sync")


class SyncError(RuntimeError):
    """rsync exited non-zero — treated like a failed stage, so the job requeues."""


def enabled() -> bool:
    """True when a remote is configured (i.e. this machine is a worker node)."""
    return bool(get_artifact_sync_remote())


def _relative(path: Path) -> str:
    """
    Where `path` sits under the local DATA_ROOT.

    The remote mirrors the local layout rather than being told about tenants
    separately, so `data/tenants/first-baptist/meetings/2026-06-15` here is
    `<remote>/tenants/first-baptist/meetings/2026-06-15` there. Deriving it
    instead of rebuilding it means a layout change (the legacy-tenant fallback,
    say) cannot make the two sides disagree about where a meeting lives.
    """
    root = tenant_paths.data_root()
    try:
        return str(path.resolve().relative_to(root))
    except ValueError as e:
        raise SyncError(
            f"{path} is outside DATA_ROOT ({root}) — refusing to sync a path "
            f"whose remote location cannot be derived"
        ) from e


async def _rsync(src: str, dst: str, *, label: str) -> None:
    """Run one rsync, stream its output, raise on failure."""
    ssh = get_artifact_sync_ssh()
    cmd = [
        "rsync", "-az", "--partial",
        # No --delete, in either direction. The pipeline only ever adds or
        # rewrites files, and a mis-scoped --delete against the folder holding
        # a church's only copy of a recording is not a mistake worth risking.
        "-e", ssh,
        src, dst,
    ]
    _std.info("→ [%s] $ %s", label, " ".join(cmd))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
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
        raise SyncError(f"{label} failed (rsync exit {rc})")
    _std.info("✓ [%s] done", label)


async def pull_meeting(meeting_dir: Path) -> None:
    """
    Bring the meeting folder here before a phase runs.

    Also the step that fetches the audio, and — before analysis — the
    speakers.json the reviewer just edited on the web.
    """
    if not enabled():
        return
    rel = _relative(meeting_dir)
    remote = get_artifact_sync_remote().rstrip("/")
    meeting_dir.parent.mkdir(parents=True, exist_ok=True)
    # Trailing slash on the source, none on the destination's parent: rsync then
    # writes INTO <parent>/<date>/ rather than creating <date>/<date>/.
    await _rsync(f"{remote}/{rel}/", str(meeting_dir), label=f"pull {rel}")


async def push_meeting(meeting_dir: Path) -> None:
    """Send back what the phase produced (~660 KB; the audio is already there)."""
    if not enabled():
        return
    rel = _relative(meeting_dir)
    remote = get_artifact_sync_remote().rstrip("/")
    await _rsync(f"{str(meeting_dir).rstrip('/')}/", f"{remote}/{rel}", label=f"push {rel}")


async def pull_voice_profiles(profiles_dir: Path) -> None:
    """
    Fetch the church's voice fingerprints before diarization matches against them.

    Small (~56 KB for the whole library) but not optional: the web writes a new
    profile whenever someone names an unknown speaker, and diarization that ran
    against a stale copy would fail to recognise the person who was just taught
    to it.
    """
    if not enabled():
        return
    rel = _relative(profiles_dir)
    remote = get_artifact_sync_remote().rstrip("/")
    profiles_dir.mkdir(parents=True, exist_ok=True)
    await _rsync(f"{remote}/{rel}/", str(profiles_dir), label=f"pull {rel}")


# ─────────────────────────────────────────────────────────────
# CLI smoke test
# ─────────────────────────────────────────────────────────────

def _smoke_test() -> None:
    import os
    import tempfile

    print("=" * 66)
    print("  artifact_sync — smoke test")
    print("=" * 66)

    with tempfile.TemporaryDirectory() as d:
        os.environ["DATA_ROOT"] = d
        os.environ.pop("ARTIFACT_SYNC_REMOTE", None)
        root = Path(d).resolve()

        assert not enabled()
        print("1. no remote configured → disabled ✓")

        # Disabled is a no-op, not an error: one machine holding everything must
        # keep working with no configuration at all.
        meeting = root / "tenants" / "default" / "meetings" / "2026-06-15"
        asyncio.run(pull_meeting(meeting))
        asyncio.run(push_meeting(meeting))
        assert not meeting.exists(), "a disabled pull must not create anything"
        print("2. disabled → pull/push are no-ops ✓")

        os.environ["ARTIFACT_SYNC_REMOTE"] = "cma@10.10.0.1:/srv/cma/data"
        assert enabled()
        print("3. remote configured → enabled ✓")

        rel = _relative(meeting)
        assert rel == "tenants/default/meetings/2026-06-15", rel
        print(f"4. local path → remote-relative: {rel} ✓")

        # A path outside DATA_ROOT has no derivable remote location; saying so is
        # better than inventing one and copying a church's folder somewhere else.
        try:
            _relative(Path("/etc/passwd"))
            raise AssertionError("a path outside DATA_ROOT should be refused")
        except SyncError:
            pass
        print("5. path outside DATA_ROOT refused ✓")

        os.environ.pop("ARTIFACT_SYNC_REMOTE", None)

    print("=" * 66)
    print("  ✓ ALL ARTIFACT_SYNC SMOKE TESTS PASSED")
    print("=" * 66)


if __name__ == "__main__":
    _smoke_test()
