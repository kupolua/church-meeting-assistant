"""Chunked protocol generation with retry + sub-chunk fallback.

Strategy:
    1. Split transcript into 25-min chunks with 5-min overlap (A2)
    2. For each chunk:
       a. First attempt — full chunk
       b. If empty (<100 chars): wait 5s, retry once
       c. If still empty: split in half, process each half as sub-chunk
       d. If sub-chunks also fail: mark as FAILED in final document
    3. Simple concatenation merge with failure markers

Cache structure:
    data/chunks/chunk_NN_XXXm-YYYm.md       — main chunks
    data/chunks/chunk_NNa_XXXm-YYYm.md      — first half sub-chunk (if needed)
    data/chunks/chunk_NNb_XXXm-YYYm.md      — second half sub-chunk (if needed)

Usage:
    uv run python -m church_assistant.chunked_analyze
    uv run python -m church_assistant.chunked_analyze --no-cache
"""

from __future__ import annotations

import argparse
import re
import time
from dataclasses import dataclass
from pathlib import Path

import ollama


DEFAULT_TRANSCRIPT = Path("data/test_baseline_annotated.md")
DEFAULT_OUTPUT_DIR = Path("data/chunks")
DEFAULT_FINAL_OUTPUT = Path("data/test_baseline_chunked.md")

DEFAULT_CHUNK_MINUTES = 25
DEFAULT_OVERLAP_MINUTES = 5

MODEL_NAME = "gemma4:26b"
NUM_CTX = 32768
NUM_PREDICT = 8192
TEMPERATURE = 0.0

# Fail criteria — anything less than this many characters is considered an empty response
MIN_VALID_OUTPUT_CHARS = 100

# Retry pause between first attempt and retry
RETRY_PAUSE_SECONDS = 5

SYSTEM_PROMPT = """Ти професійний аналітик церковних зустрічей.

Твоя задача — створити структурований протокол на основі стенограми зустрічі команди пасторів.

ВАЖЛИВІ ПРАВИЛА:
1. Використовуй ВИКЛЮЧНО інформацію зі стенограми — не вигадуй фактів.
2. Часові мітки бери ЗІ стенограми у форматі (HH:MM:SS) або (MM:SS).
3. Імена учасників атрибутуй ТОЧНО так, як у стенограмі (Роман, Павло, Чед, тощо).
4. Сегменти з міткою [нерозбірливо] пропускай — не атрибутуй їх жодному учаснику.
5. Пиши коротко і по суті."""


USER_PROMPT_TEMPLATE = """Ось стенограма частини зустрічі команди пасторів:

<transcript>
{transcript}
</transcript>

Створи протокол у наступній структурі Markdown:

## Розглянуті питання

### [Назва теми 1]
[1-2 речення про важливість теми]

- [Головна теза обговорення] (HH:MM)
  - [Деталь обговорення]
  - [Деталь обговорення]

### [Назва теми 2]
...

## Наступні кроки

### [Ім'я відповідального]
- [Опис завдання] (HH:MM)
- [Опис завдання] (HH:MM)

Створи протокол лише для тем, що обговорюються у цій частині стенограми."""


@dataclass
class TranscriptLine:
    text: str
    timestamp_seconds: float | None


@dataclass
class Chunk:
    """A chunk of transcript ready to be sent to Gemma.

    sub_id is None for main chunks, "a" or "b" for sub-chunks.
    """
    index: int
    sub_id: str | None
    start_seconds: float
    end_seconds: float
    main_start_seconds: float
    main_end_seconds: float
    transcript_text: str

    @property
    def label(self) -> str:
        ms = int(self.main_start_seconds // 60)
        me = int(self.main_end_seconds // 60)
        idx_str = f"{self.index:02d}" + (self.sub_id or "")
        return f"chunk_{idx_str}_{ms:03d}m-{me:03d}m"

    @property
    def main_range_label(self) -> str:
        """Human-readable range for messages."""
        ms = int(self.main_start_seconds // 60)
        me = int(self.main_end_seconds // 60)
        return f"{ms}-{me}min"


def parse_transcript_lines(transcript_text: str) -> list[TranscriptLine]:
    """Parse transcript and extract timestamp per line."""
    lines: list[TranscriptLine] = []
    for raw_line in transcript_text.split("\n"):
        match = re.match(r"^\[(\d+):(\d+)(?::(\d+))?\s+", raw_line)
        if match:
            parts = match.groups()
            if parts[2] is not None:
                h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
                ts = h * 3600 + m * 60 + s
            else:
                m, s = int(parts[0]), int(parts[1])
                ts = m * 60 + s
            lines.append(TranscriptLine(text=raw_line, timestamp_seconds=ts))
        else:
            lines.append(TranscriptLine(text=raw_line, timestamp_seconds=None))
    return lines


def make_chunks(
        lines: list[TranscriptLine],
        chunk_minutes: int,
        overlap_minutes: int,
) -> list[Chunk]:
    """Build overlapping chunks at the top level (no sub-chunks here)."""
    if not lines:
        return []

    timestamps = [l.timestamp_seconds for l in lines if l.timestamp_seconds is not None]
    if not timestamps:
        return []
    last_seconds = max(timestamps)

    chunk_seconds = chunk_minutes * 60
    overlap_seconds = overlap_minutes * 60
    step_seconds = chunk_seconds - 2 * overlap_seconds

    chunks: list[Chunk] = []
    chunk_idx = 0
    main_start = 0.0

    while main_start < last_seconds:
        main_end = main_start + step_seconds
        chunk_start = max(0.0, main_start - overlap_seconds)
        chunk_end = main_end + overlap_seconds

        if chunk_idx == 0:
            chunk_start = 0.0
        chunk_end = min(chunk_end, last_seconds + 1)

        chunk_lines: list[str] = []
        for l in lines:
            if l.timestamp_seconds is None:
                if chunk_idx == 0 and not chunk_lines:
                    chunk_lines.append(l.text)
                continue
            if chunk_start <= l.timestamp_seconds < chunk_end:
                chunk_lines.append(l.text)

        if chunk_lines:
            chunks.append(
                Chunk(
                    index=chunk_idx,
                    sub_id=None,
                    start_seconds=chunk_start,
                    end_seconds=chunk_end,
                    main_start_seconds=main_start,
                    main_end_seconds=main_end,
                    transcript_text="\n".join(chunk_lines),
                )
            )

        chunk_idx += 1
        main_start = main_end
        if main_start > last_seconds:
            break

    return chunks


def split_chunk_in_half(
        parent: Chunk,
        lines: list[TranscriptLine],
) -> tuple[Chunk, Chunk]:
    """Split a failed chunk's MAIN zone in half. Each half keeps its own overlap."""
    midpoint = (parent.main_start_seconds + parent.main_end_seconds) / 2
    half_overlap = 60.0  # 1-min overlap for sub-chunks (smaller than main)

    # Sub-chunk A: parent's main_start → midpoint
    a_main_start = parent.main_start_seconds
    a_main_end = midpoint
    a_start = max(0.0, a_main_start - half_overlap)
    a_end = a_main_end + half_overlap

    # Sub-chunk B: midpoint → parent's main_end
    b_main_start = midpoint
    b_main_end = parent.main_end_seconds
    b_start = b_main_start - half_overlap
    b_end = b_main_end + half_overlap

    def lines_in_range(start: float, end: float) -> list[str]:
        result: list[str] = []
        for l in lines:
            if l.timestamp_seconds is None:
                continue
            if start <= l.timestamp_seconds < end:
                result.append(l.text)
        return result

    sub_a = Chunk(
        index=parent.index,
        sub_id="a",
        start_seconds=a_start,
        end_seconds=a_end,
        main_start_seconds=a_main_start,
        main_end_seconds=a_main_end,
        transcript_text="\n".join(lines_in_range(a_start, a_end)),
    )
    sub_b = Chunk(
        index=parent.index,
        sub_id="b",
        start_seconds=b_start,
        end_seconds=b_end,
        main_start_seconds=b_main_start,
        main_end_seconds=b_main_end,
        transcript_text="\n".join(lines_in_range(b_start, b_end)),
    )
    return sub_a, sub_b


def call_gemma_once(chunk: Chunk) -> tuple[str, float]:
    """One call to Gemma. Returns (content, elapsed_seconds)."""
    user_prompt = USER_PROMPT_TEMPLATE.format(transcript=chunk.transcript_text)

    start = time.time()
    response = ollama.chat(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        options={
            "num_ctx": NUM_CTX,
            "num_predict": NUM_PREDICT,
            "temperature": TEMPERATURE,
        },
    )
    elapsed = time.time() - start
    content = response["message"]["content"]
    return content, elapsed


def is_valid_output(content: str) -> bool:
    """Check whether Gemma's output is non-empty and useful."""
    return len(content.strip()) >= MIN_VALID_OUTPUT_CHARS


def process_chunk_with_fallback(
        chunk: Chunk,
        output_dir: Path,
        lines: list[TranscriptLine],
        use_cache: bool = True,
) -> tuple[str, str]:
    """Process a chunk with retry + sub-chunk fallback.

    Returns (content, status):
        status ∈ {"cached", "first_try", "retry", "sub_chunked", "failed"}
    """
    md_path = output_dir / f"{chunk.label}.md"
    raw_path = output_dir / f"{chunk.label}.raw.txt"

    # ---- Cache check ----
    if use_cache and md_path.exists():
        cached = md_path.read_text(encoding="utf-8")
        if is_valid_output(cached):
            print(f"  ✓ Cached: {md_path}")
            return cached, "cached"
        # Cached but empty — fall through to retry+sub-chunk

    # ---- First attempt ----
    input_chars = len(SYSTEM_PROMPT) + len(USER_PROMPT_TEMPLATE) + len(chunk.transcript_text)
    print(
        f"  Attempt 1: input ~{input_chars} chars, "
        f"transcript {len(chunk.transcript_text)} chars"
    )

    content, elapsed = call_gemma_once(chunk)
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(content, encoding="utf-8")
    print(f"    Done in {elapsed:.1f}s ({elapsed/60:.1f} min) — {len(content)} chars")

    if is_valid_output(content):
        md_path.write_text(content, encoding="utf-8")
        return content, "first_try"

    # ---- Retry once ----
    print(f"  ⚠ Empty output. Waiting {RETRY_PAUSE_SECONDS}s and retrying...")
    time.sleep(RETRY_PAUSE_SECONDS)

    content, elapsed = call_gemma_once(chunk)
    raw_path.write_text(content, encoding="utf-8")  # overwrite raw with retry result
    print(f"    Retry done in {elapsed:.1f}s — {len(content)} chars")

    if is_valid_output(content):
        md_path.write_text(content, encoding="utf-8")
        return content, "retry"

    # ---- Sub-chunk fallback ----
    print(f"  ⚠ Retry also empty. Falling back to sub-chunking...")

    # If this is already a sub-chunk, don't recurse — give up
    if chunk.sub_id is not None:
        print(f"  ❌ Sub-chunk also failed. Giving up.")
        md_path.write_text("", encoding="utf-8")  # mark as processed but empty
        return "", "failed"

    sub_a, sub_b = split_chunk_in_half(chunk, lines)
    print(
        f"  Split into sub-chunks: "
        f"{sub_a.main_range_label} + {sub_b.main_range_label}"
    )

    sub_a_content, sub_a_status = process_chunk_with_fallback(
        sub_a, output_dir, lines, use_cache=use_cache
    )
    sub_b_content, sub_b_status = process_chunk_with_fallback(
        sub_b, output_dir, lines, use_cache=use_cache
    )

    print(
        f"  Sub-chunk results: a={sub_a_status} ({len(sub_a_content)} chars), "
        f"b={sub_b_status} ({len(sub_b_content)} chars)"
    )

    # Build merged content from sub-chunks
    merged_parts: list[str] = []

    if is_valid_output(sub_a_content):
        merged_parts.append(sub_a_content.strip())
    else:
        merged_parts.append(failure_placeholder(sub_a))

    if is_valid_output(sub_b_content):
        merged_parts.append(sub_b_content.strip())
    else:
        merged_parts.append(failure_placeholder(sub_b))

    merged_content = "\n\n".join(merged_parts)
    md_path.write_text(merged_content, encoding="utf-8")

    return merged_content, "sub_chunked"


def failure_placeholder(chunk: Chunk) -> str:
    """Markdown placeholder for a failed chunk."""
    return (
        f"## ⚠️ Не вдалося обробити фрагмент {chunk.main_range_label}\n\n"
        f"Система не змогла автоматично проаналізувати цю частину запису.\n"
        f"Перегляньте відповідний фрагмент стенограми вручну."
    )


def split_protocol_sections(output: str) -> tuple[str, str]:
    """Split output into (topics_part, actions_part)."""
    actions_match = re.search(r"##\s+Наступні кроки", output, flags=re.MULTILINE)
    if actions_match:
        topics_part = output[: actions_match.start()].rstrip()
        actions_part = output[actions_match.start():].rstrip()
        return topics_part, actions_part
    return output.rstrip(), ""


def merge_chunks(
        chunk_outputs: list[tuple[str, str]],
        chunks: list[Chunk],
) -> str:
    """Concatenate chunk outputs with status awareness."""
    lines: list[str] = []
    lines.append("# Протокол зустрічі від [дата]")
    lines.append("")
    lines.append("## Присутні")
    lines.append("- [список заповнюється вручну або з speakers.json]")
    lines.append("")
    lines.append("## Розглянуті питання")
    lines.append("")

    action_items_blocks: list[str] = []

    for chunk, (output, status) in zip(chunks, chunk_outputs):
        lines.append(
            f"<!-- Chunk {chunk.index}: {chunk.main_range_label} "
            f"(status: {status}) -->"
        )

        if not is_valid_output(output) and status == "failed":
            lines.append(failure_placeholder(chunk))
            lines.append("")
            continue

        topics_part, actions_part = split_protocol_sections(output)

        if topics_part:
            cleaned = re.sub(
                r"^##\s+Розглянуті питання\s*\n",
                "",
                topics_part,
                count=1,
                flags=re.MULTILINE,
            )
            lines.append(cleaned.rstrip())
            lines.append("")

        if actions_part:
            action_items_blocks.append(actions_part)

    if action_items_blocks:
        lines.append("## Наступні кроки")
        lines.append("")
        for block in action_items_blocks:
            cleaned = re.sub(
                r"^##\s+Наступні кроки\s*\n",
                "",
                block,
                count=1,
                flags=re.MULTILINE,
            )
            lines.append(cleaned.rstrip())
            lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Chunked transcript analysis with retry + sub-chunk fallback"
    )
    parser.add_argument("--transcript", type=Path, default=DEFAULT_TRANSCRIPT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--final-output", type=Path, default=DEFAULT_FINAL_OUTPUT)
    parser.add_argument("--chunk-minutes", type=int, default=DEFAULT_CHUNK_MINUTES)
    parser.add_argument("--overlap-minutes", type=int, default=DEFAULT_OVERLAP_MINUTES)
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()

    if not args.transcript.exists():
        print(f"❌ Transcript not found: {args.transcript}")
        raise SystemExit(1)

    print(f"Loading transcript: {args.transcript}")
    transcript_text = args.transcript.read_text(encoding="utf-8")
    print(f"  ✓ {len(transcript_text)} characters")

    lines = parse_transcript_lines(transcript_text)
    n_timed = sum(1 for l in lines if l.timestamp_seconds is not None)
    print(f"  ✓ {len(lines)} lines, {n_timed} with timestamps")

    chunks = make_chunks(lines, args.chunk_minutes, args.overlap_minutes)
    if not chunks:
        print("❌ No chunks built")
        raise SystemExit(1)

    print(f"\n{'=' * 70}")
    print(f"  Chunking plan: {len(chunks)} chunks")
    print(f"{'=' * 70}")
    for c in chunks:
        ms = int(c.main_start_seconds // 60)
        me = int(c.main_end_seconds // 60)
        cs = int(c.start_seconds // 60)
        ce = int(c.end_seconds // 60)
        print(
            f"  Chunk {c.index}: main {ms:3d}-{me:3d}min, "
            f"with context {cs:3d}-{ce:3d}min, "
            f"{len(c.transcript_text)} chars"
        )

    print(f"\n{'=' * 70}")
    print(f"  Processing chunks")
    print(f"{'=' * 70}")

    chunk_outputs: list[tuple[str, str]] = []
    total_start = time.time()

    for chunk in chunks:
        print(f"\nChunk {chunk.index}: {chunk.main_range_label}")
        try:
            output, status = process_chunk_with_fallback(
                chunk,
                output_dir=args.output_dir,
                lines=lines,
                use_cache=not args.no_cache,
            )
            chunk_outputs.append((output, status))
        except Exception as e:
            print(f"  ❌ Unexpected error: {e}")
            chunk_outputs.append(("", "exception"))

    total_elapsed = time.time() - total_start
    print(f"\n{'=' * 70}")
    print(f"  All chunks processed in {total_elapsed:.1f}s ({total_elapsed/60:.1f} min)")
    print(f"{'=' * 70}")

    # Status summary
    status_counts: dict[str, int] = {}
    for _, status in chunk_outputs:
        status_counts[status] = status_counts.get(status, 0) + 1
    print("\nStatus breakdown:")
    for status, count in sorted(status_counts.items()):
        print(f"  {status:<15} {count}")

    print("\nMerging chunks into final protocol...")
    merged = merge_chunks(chunk_outputs, chunks)

    args.final_output.parent.mkdir(parents=True, exist_ok=True)
    args.final_output.write_text(merged, encoding="utf-8")

    n_lines = len(merged.split("\n"))
    n_headers = sum(1 for line in merged.split("\n") if line.lstrip().startswith("#"))
    n_bullets = sum(1 for line in merged.split("\n") if line.lstrip().startswith("-"))
    n_timestamps = len(re.findall(r"\(\d+:\d+(?::\d+)?\)", merged))
    n_failure_markers = merged.count("⚠️")

    print(f"\n{'=' * 70}")
    print(f"  Final protocol stats")
    print(f"{'=' * 70}")
    print(f"Chunks merged:        {len(chunks)}")
    print(f"Lines:                {n_lines}")
    print(f"Headers (#):          {n_headers}")
    print(f"Bullets (-):          {n_bullets}")
    print(f"Timestamps:           {n_timestamps}")
    print(f"Failure markers (⚠️): {n_failure_markers}")
    print(f"Total size:           {len(merged)} chars")
    print(f"\nFinal output:    {args.final_output}")
    print(f"Per-chunk files: {args.output_dir}/")


if __name__ == "__main__":
    main()