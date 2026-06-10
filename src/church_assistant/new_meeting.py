"""End-to-end pipeline wrapper for processing a new meeting.

Orchestrates:
    1. Create per-meeting folder: data/meetings/YYYY-MM-DD/
    2. Copy audio file into the folder as audio.m4a (or original extension)
    3. Run match_speakers.py and transcribe.py in PARALLEL (slow steps)
    4. PAUSE for manual review of speakers.json (you edit [REVIEW] / no-match)
    5. Run merge_transcript.py
    6. Run chunked_analyze.py (slow: 10-30 min for Gemma)
    7. Run polish_protocol.py
    8. Print path to final polished.md

Each step is skipped if its expected output file already exists (resumability).
Use --no-cache flags on individual steps if you want forced re-run.

Usage:
    uv run python -m church_assistant.new_meeting \\
        --audio /path/to/recording.m4a \\
        --date 2026-06-15

    # Resume after fixing speakers.json
    uv run python -m church_assistant.new_meeting \\
        --audio /path/to/recording.m4a \\
        --date 2026-06-15 \\
        --resume

    # Skip the manual edit pause (DANGEROUS — [REVIEW] tags stay in transcript)
    uv run python -m church_assistant.new_meeting \\
        --audio /path/to/recording.m4a \\
        --date 2026-06-15 \\
        --no-pause

    # Sequential (slower, useful if memory is tight)
    uv run python -m church_assistant.new_meeting \\
        --audio /path/to/recording.m4a \\
        --date 2026-06-15 \\
        --sequential
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(-\d+)?$")
MODULE_PREFIX = ["uv", "run", "python", "-m"]


@dataclass
class StepResult:
    """Outcome of a single pipeline step."""
    name: str
    skipped: bool          # True if output already existed
    success: bool
    duration_sec: float


def log(msg: str, color: str = "") -> None:
    """Print log message with optional ANSI color."""
    colors = {
        "blue":   "\033[94m",
        "green":  "\033[92m",
        "yellow": "\033[93m",
        "red":    "\033[91m",
        "bold":   "\033[1m",
        "reset":  "\033[0m",
    }
    if color and color in colors:
        print(f"{colors[color]}{msg}{colors['reset']}", flush=True)
    else:
        print(msg, flush=True)


def header(text: str) -> None:
    """Print a section header."""
    line = "=" * 70
    log("")
    log(line, "blue")
    log(f"  {text}", "blue")
    log(line, "blue")


def validate_date(date_str: str) -> str:
    """Ensure date is YYYY-MM-DD format (optional -N suffix)."""
    if not DATE_RE.match(date_str):
        log(f"❌ Date must be YYYY-MM-DD (got: {date_str})", "red")
        raise SystemExit(1)
    return date_str


def date_for_polish(date_str: str) -> str:
    """Convert 2026-06-15 → 15/06/2026 for polish_protocol --date arg."""
    # Strip any -N suffix
    base = date_str.split("-")
    if len(base) >= 3:
        y, m, d = base[0], base[1], base[2]
        return f"{d}/{m}/{y}"
    return date_str


def create_meeting_folder(date: str) -> Path:
    """Create data/meetings/YYYY-MM-DD/ if not exists."""
    folder = Path("data/meetings") / date
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def copy_audio(source: Path, dest_folder: Path) -> Path:
    """Copy audio file into the meeting folder.

    Names the file `audio.{ext}` where ext is the original extension.
    If already present, skip.
    """
    dest_path = dest_folder / f"audio{source.suffix}"
    if dest_path.exists():
        log(f"  ✓ Audio already at {dest_path} (skipping copy)", "green")
        return dest_path

    log(f"  Copying {source} → {dest_path}")
    shutil.copy2(source, dest_path)
    log(f"  ✓ Copied ({dest_path.stat().st_size / 1024 / 1024:.1f} MB)", "green")
    return dest_path


def run_command_blocking(
    cmd: list[str],
    description: str,
) -> bool:
    """Run a command, stream its output, return True on success."""
    log(f"\n→ {description}", "bold")
    log(f"  $ {' '.join(cmd)}")

    start = time.time()
    result = subprocess.run(cmd, check=False)
    elapsed = time.time() - start

    if result.returncode == 0:
        log(f"  ✓ Done in {elapsed:.1f}s ({elapsed/60:.1f} min)", "green")
        return True
    else:
        log(f"  ❌ FAILED with exit code {result.returncode} "
            f"after {elapsed:.1f}s", "red")
        return False


def run_parallel(
    commands: list[tuple[str, list[str]]],
) -> dict[str, bool]:
    """Launch multiple commands in parallel, stream outputs prefixed.

    `commands` is a list of (name, argv) pairs. Each gets its own process.
    Output is line-prefixed with [name].
    """
    log(f"\n→ Running {len(commands)} processes in parallel:", "bold")
    for name, argv in commands:
        log(f"  [{name}]  $ {' '.join(argv)}")

    start = time.time()

    procs: dict[str, subprocess.Popen] = {}
    for name, argv in commands:
        # Combine stderr and stdout, line-buffered
        procs[name] = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

    # Read interleaved (poll all, print as available)
    # Simple approach: read one process at a time after it finishes.
    # For true streaming, we'd use threads or select(), but this is enough.
    results: dict[str, bool] = {}
    for name, proc in procs.items():
        # Stream output for this process to completion
        log(f"\n  --- output from [{name}] starts ---", "yellow")
        if proc.stdout is not None:
            for line in proc.stdout:
                print(f"  [{name}] {line.rstrip()}", flush=True)
        proc.wait()
        log(f"  --- output from [{name}] ends (exit {proc.returncode}) ---",
            "yellow")
        results[name] = (proc.returncode == 0)

    elapsed = time.time() - start
    n_success = sum(1 for v in results.values() if v)
    log(f"\n  Parallel phase done in {elapsed:.1f}s ({elapsed/60:.1f} min). "
        f"{n_success}/{len(results)} succeeded.", "green" if n_success == len(results) else "red")
    return results


def run_pause(message: str) -> None:
    """Print message and wait for ENTER."""
    log("")
    log("=" * 70, "yellow")
    log("  PAUSED FOR MANUAL STEP", "yellow")
    log("=" * 70, "yellow")
    log(message, "yellow")
    log("")
    log("Press ENTER when ready to continue...", "yellow")
    try:
        input()
    except (KeyboardInterrupt, EOFError):
        log("\n❌ Aborted by user", "red")
        raise SystemExit(1)
    log("  ✓ Continuing", "green")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="End-to-end pipeline for a new meeting"
    )
    parser.add_argument(
        "--audio",
        type=Path,
        required=True,
        help="Path to source audio file (will be copied into meeting folder)",
    )
    parser.add_argument(
        "--date",
        type=str,
        required=True,
        help="Meeting date in YYYY-MM-DD format",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip steps whose output already exists (default: also skip them, "
             "but with --resume the script will not warn about it)",
    )
    parser.add_argument(
        "--sequential",
        action="store_true",
        help="Run pyannote and Whisper sequentially instead of in parallel",
    )
    parser.add_argument(
        "--no-pause",
        action="store_true",
        help="Don't pause for manual speakers.json edit (DANGEROUS — leaves "
             "[REVIEW] tags in the protocol)",
    )
    args = parser.parse_args()

    # Validate inputs
    if not args.audio.exists():
        log(f"❌ Audio not found: {args.audio}", "red")
        raise SystemExit(1)

    validate_date(args.date)

    # 1. Create meeting folder
    header(f"Pipeline for meeting {args.date}")
    log(f"Source audio: {args.audio}")
    log(f"Sequential:   {args.sequential}")
    log(f"Resume mode:  {args.resume}")

    meeting_dir = create_meeting_folder(args.date)
    log(f"\nMeeting folder: {meeting_dir}", "green")

    # 2. Copy audio
    header("Step 1: Copy audio")
    audio_path = copy_audio(args.audio, meeting_dir)

    # Resolve paths for all downstream artifacts
    audio_stem = audio_path.stem                    # "audio"

    # transcribe.py writes "{audio_stem}_transcript.json" by default.
    # For backward compatibility with manually-renamed files, also accept
    # a clean "transcript.json" if it exists.
    default_transcript = meeting_dir / f"{audio_stem}_transcript.json"
    clean_transcript = meeting_dir / "transcript.json"
    if clean_transcript.exists() and not default_transcript.exists():
        transcript_path = clean_transcript
    else:
        transcript_path = default_transcript

    embeddings_path = meeting_dir / f"{audio_stem}_embeddings.pkl"
    rttm_path = meeting_dir / "diarization.rttm"
    speakers_path = meeting_dir / "speakers.json"
    annotated_path = meeting_dir / "annotated.md"
    chunks_dir = meeting_dir / "chunks"
    chunked_path = meeting_dir / "chunked.md"
    polished_path = meeting_dir / "polished.md"

    # 3. Parallel (or sequential): match_speakers + transcribe
    header("Step 2: Diarization + Transcription")

    match_cmd = MODULE_PREFIX + [
        "church_assistant.match_speakers",
        "--audio", str(audio_path),
        "--output", str(speakers_path),
        "--rttm", str(rttm_path),
    ]
    transcribe_cmd = MODULE_PREFIX + [
        "church_assistant.transcribe",
        str(audio_path),
    ]

    # Resume: skip individual steps if outputs already exist
    match_needed = not (speakers_path.exists() and rttm_path.exists())
    transcribe_needed = not transcript_path.exists()

    if not match_needed:
        log(f"  ✓ speakers.json + diarization.rttm exist — skipping match_speakers",
            "green")
    if not transcribe_needed:
        log(f"  ✓ transcript.json exists — skipping transcribe", "green")

    if match_needed and transcribe_needed:
        if args.sequential:
            ok1 = run_command_blocking(match_cmd, "match_speakers (sequential)")
            if not ok1:
                raise SystemExit(1)
            ok2 = run_command_blocking(transcribe_cmd, "transcribe (sequential)")
            if not ok2:
                raise SystemExit(1)
        else:
            results = run_parallel([
                ("match_speakers", match_cmd),
                ("transcribe", transcribe_cmd),
            ])
            failed = [n for n, ok in results.items() if not ok]
            if failed:
                log(f"\n❌ Failed steps: {failed}", "red")
                raise SystemExit(1)
    elif match_needed:
        if not run_command_blocking(match_cmd, "match_speakers"):
            raise SystemExit(1)
    elif transcribe_needed:
        if not run_command_blocking(transcribe_cmd, "transcribe"):
            raise SystemExit(1)

    # Sanity check
    for p, label in [
        (transcript_path, "transcript.json"),
        (speakers_path, "speakers.json"),
        (rttm_path, "diarization.rttm"),
    ]:
        if not p.exists():
            log(f"❌ Expected output missing: {p} ({label})", "red")
            raise SystemExit(1)

    # 4. Manual review pause
    if not args.no_pause:
        header("Step 3: Manual review of speakers.json")
        run_pause(f"""
Review and edit:
    {speakers_path}

Things to check:
  - Remove any [REVIEW] suffixes if you're confident in the weak matches:
      sed -i '' 's/ \\[REVIEW\\]//g' {speakers_path}
  - For 'no_match' speakers (kept as SPEAKER_XX), assign real names if
    you recognize the voice. Listen to their RTTM segments if unsure:
      grep SPEAKER_XX {rttm_path} | head -5

After editing, save the file and come back here.
""")
    else:
        log("\n⚠ --no-pause: skipping manual review (any [REVIEW] tags stay)",
            "yellow")

    # 5. merge_transcript
    header("Step 4: Merge transcript with diarization")
    if annotated_path.exists():
        log(f"  ✓ {annotated_path.name} exists — skipping merge", "green")
    else:
        merge_cmd = MODULE_PREFIX + [
            "church_assistant.merge_transcript",
            "--transcript", str(transcript_path),
            "--rttm", str(rttm_path),
            "--speakers", str(speakers_path),
            "--output", str(annotated_path),
        ]
        if not run_command_blocking(merge_cmd, "merge_transcript"):
            raise SystemExit(1)

    # 6. chunked_analyze
    header("Step 5: Chunked analysis (Gemma)")
    if chunks_dir.exists() and any(chunks_dir.iterdir()) and chunked_path.exists():
        log(f"  ✓ chunks/ and chunked.md exist — skipping chunked_analyze", "green")
    else:
        chunked_cmd = MODULE_PREFIX + [
            "church_assistant.chunked_analyze",
            "--transcript", str(annotated_path),
            "--output-dir", str(chunks_dir),
            "--final-output", str(chunked_path),
        ]
        if not run_command_blocking(chunked_cmd, "chunked_analyze"):
            raise SystemExit(1)

    # 7. polish_protocol
    header("Step 6: Polish final protocol")
    if polished_path.exists():
        log(f"  ✓ {polished_path.name} exists — skipping polish (delete to redo)",
            "green")
    else:
        polish_cmd = MODULE_PREFIX + [
            "church_assistant.polish_protocol",
            "--chunks-dir", str(chunks_dir),
            "--output", str(polished_path),
            "--rttm", str(rttm_path),
            "--speakers-map", str(speakers_path),
            "--date", date_for_polish(args.date),
        ]
        if not run_command_blocking(polish_cmd, "polish_protocol"):
            raise SystemExit(1)

    # 8. Summary
    header("Pipeline complete")
    log(f"Polished protocol: {polished_path}", "green")
    log(f"Open in editor to review, then share with the team.")
    log("")
    log("All artifacts in this meeting folder:")
    for p in sorted(meeting_dir.iterdir()):
        if p.is_file():
            size_kb = p.stat().st_size / 1024
            log(f"  {p.name:<30} {size_kb:>10.1f} KB")
        elif p.is_dir():
            n_files = sum(1 for _ in p.iterdir())
            log(f"  {p.name + '/':<30} {n_files:>10} files")


if __name__ == "__main__":
    main()
