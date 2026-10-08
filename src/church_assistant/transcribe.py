"""Whisper transcription with caching.

Two engines behind one JSON contract — see WHISPER_BACKEND below. The M1 runs
faster-whisper on the CPU; the DL165 runs whisper.cpp on the RX 580 via Vulkan.

Mirrors the structure of diarization.py:
- Long pipeline call (~2h on M1 CPU)
- Save to disk BEFORE printing summary (so a crash at print time doesn't lose work)
- Cache by checking if output JSON exists
- CLI args matching diarization.py style

Usage:
    # First run: transcribes test_baseline.m4a (~2h on M1 CPU), saves JSON
    uv run python -m church_assistant.transcribe

    # Subsequent runs: instant — loads from cache
    uv run python -m church_assistant.transcribe

    # Different audio file
    uv run python -m church_assistant.transcribe data/other_audio.m4a

    # Force re-transcription (ignore cache)
    uv run python -m church_assistant.transcribe --no-cache

    # Show only first N segments in summary
    uv run python -m church_assistant.transcribe --limit 20
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# The stage runs as its own `uv run python -m …` process; when started by hand
# the worker's environment is not there, so read .env the way the worker does.
load_dotenv()

# Whisper parameters — these match what gave 4+/5 quality in earlier experiments
MODEL_NAME = "large-v3"
LANGUAGE = "uk"
BEAM_SIZE = 5
COMPUTE_TYPE = "int8"  # int8 quantization — best speed/quality on M1 CPU

# Which engine runs the model. Both produce the same TranscriptionResult JSON, so
# nothing downstream (merge_transcript, polish, the web) can tell them apart.
#
#   faster-whisper  (default) — CTranslate2 on the CPU. What the M1 has always run.
#   whisper-cpp     — the whisper.cpp CLI, used on the DL165 where the model runs on
#                     the RX 580 through Vulkan. faster-whisper has no path to that
#                     GPU (Polaris has no ROCm), and on the Opteron CPUs it is 3×
#                     slower than real time; whisper.cpp on the card is 3× faster.
WHISPER_BACKEND = os.getenv("WHISPER_BACKEND", "faster-whisper").strip().lower()
WHISPER_CPP_BIN = Path(
    os.getenv("WHISPER_CPP_BIN") or Path.home() / "whisper.cpp/build/bin/whisper-cli"
)
WHISPER_CPP_MODEL = Path(
    os.getenv("WHISPER_CPP_MODEL")
    or Path.home() / "whisper.cpp/models/ggml-large-v3-q8_0.bin"
)
# CPU threads for the parts whisper.cpp keeps on the host (sampling, feature
# extraction). 4 is plenty when the matmuls are on the GPU; more only steals cores
# from a diarization running alongside.
WHISPER_CPP_THREADS = int(os.getenv("WHISPER_CPP_THREADS", "4"))


@dataclass
class TranscriptSegment:
    """One Whisper segment with timing info."""

    start: float
    end: float
    text: str


@dataclass
class TranscriptionResult:
    """Full transcription output with metadata."""

    audio_path: str
    duration: float
    language: str
    language_probability: float
    processing_time_seconds: float
    model_name: str
    segments: list[TranscriptSegment]

    def to_json_dict(self) -> dict[str, Any]:
        """JSON-serializable dict."""
        return {
            "audio_path": self.audio_path,
            "duration": self.duration,
            "language": self.language,
            "language_probability": self.language_probability,
            "processing_time_seconds": self.processing_time_seconds,
            "model_name": self.model_name,
            "segments": [asdict(s) for s in self.segments],
        }

    @classmethod
    def from_json_dict(cls, data: dict[str, Any]) -> TranscriptionResult:
        """Reconstruct from JSON dict."""
        segments = [TranscriptSegment(**s) for s in data["segments"]]
        return cls(
            audio_path=data["audio_path"],
            duration=data["duration"],
            language=data["language"],
            language_probability=data["language_probability"],
            processing_time_seconds=data["processing_time_seconds"],
            model_name=data["model_name"],
            segments=segments,
        )


def transcribe_audio(audio_path: Path) -> TranscriptionResult:
    """Run Whisper on the audio file with whichever backend WHISPER_BACKEND names.

    This is the slow part (~2h on M1 CPU for 2h audio; ~40 min on the RX 580).
    """
    if WHISPER_BACKEND == "whisper-cpp":
        return _transcribe_whisper_cpp(audio_path)
    if WHISPER_BACKEND != "faster-whisper":
        raise SystemExit(
            f"WHISPER_BACKEND={WHISPER_BACKEND!r}: expected 'faster-whisper' or 'whisper-cpp'"
        )
    return _transcribe_faster_whisper(audio_path)


def _transcribe_faster_whisper(audio_path: Path) -> TranscriptionResult:
    """CTranslate2 on the CPU — the original path, unchanged."""
    from faster_whisper import WhisperModel  # heavy import; only when this backend runs

    print(f"Loading model {MODEL_NAME} (compute_type={COMPUTE_TYPE})...")
    model = WhisperModel(MODEL_NAME, device="cpu", compute_type=COMPUTE_TYPE)

    print(f"Transcribing {audio_path.name}...")
    print(
        f"Parameters: language={LANGUAGE}, beam_size={BEAM_SIZE}, "
        f"compute_type={COMPUTE_TYPE}"
    )
    print("(this will take ~1-2 hours on M1 CPU — be patient)")

    start_time = time.time()
    segments_iter, info = model.transcribe(
        str(audio_path),
        language=LANGUAGE,
        beam_size=BEAM_SIZE,
    )

    # segments_iter is a generator — must iterate to actually do the work
    segments: list[TranscriptSegment] = []
    for seg in segments_iter:
        segments.append(
            TranscriptSegment(
                start=float(seg.start),
                end=float(seg.end),
                text=seg.text.strip(),
            )
        )

    elapsed = time.time() - start_time

    return TranscriptionResult(
        audio_path=str(audio_path),
        duration=float(info.duration),
        language=info.language,
        language_probability=float(info.language_probability),
        processing_time_seconds=elapsed,
        model_name=MODEL_NAME,
        segments=segments,
    )


def _audio_duration_seconds(audio_path: Path) -> float | None:
    """Duration from ffprobe, or None when ffprobe is missing or unhappy."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    proc = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(audio_path)],
        capture_output=True, text=True,
    )
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return None


def _transcribe_whisper_cpp(audio_path: Path) -> TranscriptionResult:
    """whisper.cpp CLI → the same TranscriptionResult faster-whisper produces.

    whisper.cpp reads only 16 kHz mono WAV, so the recording goes through ffmpeg
    into a temp dir first; the JSON it writes (-oj) has per-segment offsets in ms.
    """
    for what, path in (("binary", WHISPER_CPP_BIN), ("model", WHISPER_CPP_MODEL)):
        if not path.exists():
            raise SystemExit(f"whisper.cpp {what} not found: {path} "
                             f"(set WHISPER_CPP_BIN / WHISPER_CPP_MODEL)")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("whisper-cpp backend needs ffmpeg on PATH to resample the audio")

    print(f"whisper.cpp: {WHISPER_CPP_BIN.name}, model {WHISPER_CPP_MODEL.name}, "
          f"language={LANGUAGE}, beam_size={BEAM_SIZE}, threads={WHISPER_CPP_THREADS}")
    start_time = time.time()
    with tempfile.TemporaryDirectory(prefix="whisper-cpp-") as tmp:
        wav = Path(tmp) / "audio.wav"
        out_prefix = Path(tmp) / "out"
        subprocess.run(
            [ffmpeg, "-v", "error", "-y", "-i", str(audio_path), "-ac", "1", "-ar", "16000", str(wav)],
            check=True,
        )
        # -mc 0: do not condition on the previous window's text. With the carry-over
        # on, large-v3 fell into a loop on a real 98-minute recording (the same
        # sentence every second from 01:10:30 to the end). faster-whisper has the
        # same knob (condition_on_previous_text); here it is simply off.
        cmd = [
            str(WHISPER_CPP_BIN), "-m", str(WHISPER_CPP_MODEL), "-f", str(wav),
            "-l", LANGUAGE, "-bs", str(BEAM_SIZE), "-t", str(WHISPER_CPP_THREADS),
            "-mc", "0", "-oj", "-of", str(out_prefix),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            tail = "\n".join((proc.stderr or proc.stdout).splitlines()[-15:])
            raise RuntimeError(f"whisper-cli exited {proc.returncode}:\n{tail}")
        with (out_prefix.with_suffix(".json")).open(encoding="utf-8") as f:
            raw = json.load(f)
    elapsed = time.time() - start_time

    segments: list[TranscriptSegment] = []
    for item in raw.get("transcription", []):
        text = item.get("text", "").strip()
        if not text:
            continue
        offsets = item["offsets"]
        segments.append(TranscriptSegment(
            start=offsets["from"] / 1000.0,
            end=offsets["to"] / 1000.0,
            text=text,
        ))

    duration = _audio_duration_seconds(audio_path)
    if duration is None:
        duration = segments[-1].end if segments else 0.0
    language = (raw.get("result") or {}).get("language") or LANGUAGE

    return TranscriptionResult(
        audio_path=str(audio_path),
        duration=float(duration),
        language=language,
        # The language is forced, so whisper.cpp does not score it. 1.0 keeps the
        # field meaningful for consumers that print or threshold it.
        language_probability=1.0,
        processing_time_seconds=elapsed,
        model_name=f"{MODEL_NAME} ({WHISPER_CPP_MODEL.stem}, whisper.cpp)",
        segments=segments,
    )


def save_transcription(result: TranscriptionResult, output_path: Path) -> None:
    """Save transcription to JSON file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(result.to_json_dict(), f, ensure_ascii=False, indent=2)


def load_transcription(input_path: Path) -> TranscriptionResult:
    """Load transcription from JSON file."""
    with input_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return TranscriptionResult.from_json_dict(data)


def get_transcription(
        audio_path: Path,
        output_path: Path,
        use_cache: bool = True,
) -> TranscriptionResult:
    """Get transcription — from cache if available, otherwise compute and cache.

    Crucially: saves to disk BEFORE any print/return, so a crash later
    doesn't lose the 2 hours of work.
    """
    if use_cache and output_path.exists():
        print(f"✓ Loading transcription from cache: {output_path}")
        return load_transcription(output_path)

    print("No cache — computing fresh transcription...")
    result = transcribe_audio(audio_path)

    # Save IMMEDIATELY, before any other operation that could fail
    save_transcription(result, output_path)
    print(f"✓ Saved transcription to: {output_path}")

    return result


def print_summary(result: TranscriptionResult, limit: int = 20) -> None:
    """Print summary statistics and first N segments."""
    print(f"\n{'=' * 70}")
    print(f"  Transcription summary")
    print(f"{'=' * 70}")
    print(f"Audio:               {result.audio_path}")
    print(f"Duration:            {result.duration:.1f}s ({result.duration/60:.1f} min)")
    print(f"Processing time:     {result.processing_time_seconds:.1f}s "
          f"({result.processing_time_seconds/60:.1f} min)")
    if result.processing_time_seconds > 0:
        speedup = result.duration / result.processing_time_seconds
        print(f"Speedup vs realtime: {speedup:.2f}x")
    print(f"Language:            {result.language} "
          f"(confidence: {result.language_probability:.2f})")
    print(f"Total segments:      {len(result.segments)}")

    total_chars = sum(len(s.text) for s in result.segments)
    print(f"Transcript length:   {total_chars} characters")
    print(f"{'=' * 70}")

    print(f"\nFirst {min(limit, len(result.segments))} segments:")
    for seg in result.segments[:limit]:
        # Truncate long text for readable display
        text_preview = seg.text[:80] + ("..." if len(seg.text) > 80 else "")
        print(f"  [{seg.start:7.2f}s -> {seg.end:7.2f}s] {text_preview}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Transcribe audio with Whisper large-v3 (Ukrainian); "
                    "engine chosen by WHISPER_BACKEND: faster-whisper (default) or whisper-cpp"
    )
    parser.add_argument(
        "audio",
        type=Path,
        nargs="?",
        default=Path("data/test_baseline.m4a"),
        help="Path to audio file (default: data/test_baseline.m4a)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Output JSON path (default: <audio_stem>_transcript.json in same dir)",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Force re-transcription, ignore existing cache",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="How many segments to show in summary (default: 20)",
    )
    args = parser.parse_args()

    audio_path: Path = args.audio
    if not audio_path.exists():
        print(f"❌ Audio file not found: {audio_path}")
        raise SystemExit(1)

    output_path: Path = args.output or audio_path.with_name(
        f"{audio_path.stem}_transcript.json"
    )

    result = get_transcription(
        audio_path=audio_path,
        output_path=output_path,
        use_cache=not args.no_cache,
    )

    print_summary(result, limit=args.limit)


if __name__ == "__main__":
    main()