"""Minimal test: can Gemma 4 produce a usable Markdown protocol?

Stripped-down version of analyze_transcript.py:
- NO few-shot example (golden_protocol)
- NO format=json mode (it caused token repetition collapse)
- NO structured schema in the prompt
- Markdown output, saved directly without parsing
- Always saves raw response BEFORE anything else

This is the smallest possible experiment to learn:
1. Can Gemma 4 produce coherent Markdown for our transcript?
2. Without format=json pressure, does the repetition collapse go away?
3. What's the baseline quality before we add few-shot?

Usage:
    # Quick test on first 30 minutes
    uv run python -m church_assistant.minimal_test --limit-minutes 30

    # Full transcript (don't run until 30-min works)
    uv run python -m church_assistant.minimal_test
"""

from __future__ import annotations

import argparse
import re
import time
from pathlib import Path

import ollama


DEFAULT_TRANSCRIPT = Path("data/test_baseline_annotated.md")
DEFAULT_OUTPUT = Path("data/test_baseline_minimal.md")

MODEL_NAME = "gemma4:26b"
NUM_CTX = 32768          # smaller than before — no few-shot example to fit
NUM_PREDICT = 8192
TEMPERATURE = 0.0


SYSTEM_PROMPT = """Ти професійний аналітик церковних зустрічей.

Твоя задача — створити структурований протокол на основі стенограми зустрічі команди пасторів.

ВАЖЛИВІ ПРАВИЛА:
1. Використовуй ВИКЛЮЧНО інформацію зі стенограми — не вигадуй фактів.
2. Часові мітки бери ЗІ стенограми у форматі (HH:MM:SS) або (MM:SS).
3. Імена учасників атрибутуй ТОЧНО так, як у стенограмі (Роман, Павло, Чед, тощо).
4. Сегменти з міткою [нерозбірливо] пропускай — не атрибутуй їх жодному учаснику.
5. Пиши коротко і по суті."""


USER_PROMPT_TEMPLATE = """Ось стенограма зустрічі команди пасторів:

<transcript>
{transcript}
</transcript>

Створи протокол у наступній структурі Markdown:

# Протокол зустрічі від [дата зустрічі]

## Присутні
- [Ім'я учасника 1]
- [Ім'я учасника 2]
...

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

### [Ім'я іншого відповідального]
...

Створи такий протокол зараз, використовуючи лише інформацію зі стенограми вище."""


def load_text(path: Path) -> str:
    with path.open("r", encoding="utf-8") as f:
        return f.read()


def truncate_transcript_by_minutes(
        transcript_text: str,
        limit_minutes: int,
) -> str:
    """Keep only segments before the N-minute mark."""
    limit_seconds = limit_minutes * 60
    kept_lines: list[str] = []

    for line in transcript_text.split("\n"):
        match = re.match(r"^\[(\d+):(\d+)(?::(\d+))?\s+", line)
        if not match:
            kept_lines.append(line)
            continue

        parts = match.groups()
        if parts[2] is not None:
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
            total_seconds = h * 3600 + m * 60 + s
        else:
            m, s = int(parts[0]), int(parts[1])
            total_seconds = m * 60 + s

        if total_seconds <= limit_seconds:
            kept_lines.append(line)
        else:
            break

    return "\n".join(kept_lines)


def call_gemma_markdown(
        system_prompt: str,
        user_prompt: str,
        raw_output_path: Path,
) -> str:
    """Call Gemma WITHOUT format=json. Save raw response before returning."""
    print(
        f"Calling {MODEL_NAME} (num_ctx={NUM_CTX}, num_predict={NUM_PREDICT}, "
        f"temperature={TEMPERATURE})..."
    )
    print(f"NOTE: NOT using format=json this time — Markdown only.")

    start = time.time()

    response = ollama.chat(
        model=MODEL_NAME,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        options={
            "num_ctx": NUM_CTX,
            "num_predict": NUM_PREDICT,
            "temperature": TEMPERATURE,
        },
        # NOTE: no format="json" — let Gemma produce free-form Markdown
    )

    elapsed = time.time() - start
    print(f"✓ Gemma response in {elapsed:.1f}s ({elapsed/60:.1f} min)")

    content = response["message"]["content"]
    print(f"Response length: {len(content)} characters")

    # Save raw BEFORE anything else
    raw_output_path.parent.mkdir(parents=True, exist_ok=True)
    raw_output_path.write_text(content, encoding="utf-8")
    print(f"✓ Raw response saved to: {raw_output_path}")

    return content


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Minimal Gemma test — Markdown protocol without few-shot"
    )
    parser.add_argument(
        "--transcript",
        type=Path,
        default=DEFAULT_TRANSCRIPT,
        help=f"Annotated transcript (default: {DEFAULT_TRANSCRIPT})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output Markdown path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--limit-minutes",
        type=int,
        default=None,
        help="Use only first N minutes of transcript (for fast testing)",
    )
    args = parser.parse_args()

    if not args.transcript.exists():
        print(f"❌ Transcript not found: {args.transcript}")
        raise SystemExit(1)

    # Output paths differ for limited vs full runs
    if args.limit_minutes:
        suffix = f"_first_{args.limit_minutes}min"
        output_path = args.output.with_stem(args.output.stem + suffix)
    else:
        output_path = args.output

    raw_output_path = output_path.with_suffix(".raw.txt")

    # Load transcript
    print(f"Loading transcript: {args.transcript}")
    transcript_text = load_text(args.transcript)
    print(f"  ✓ {len(transcript_text)} characters (full)")

    if args.limit_minutes:
        transcript_text = truncate_transcript_by_minutes(
            transcript_text, args.limit_minutes
        )
        print(
            f"  ✓ {len(transcript_text)} characters "
            f"(limited to first {args.limit_minutes} minutes)"
        )

    # Build user prompt
    user_prompt = USER_PROMPT_TEMPLATE.format(transcript=transcript_text)
    total_input_chars = len(SYSTEM_PROMPT) + len(user_prompt)
    print(
        f"\nTotal input size: ~{total_input_chars} characters "
        f"(~{total_input_chars // 4} tokens approx)"
    )

    # Call Gemma
    markdown = call_gemma_markdown(
        SYSTEM_PROMPT, user_prompt, raw_output_path
    )

    # Save as the actual Markdown output too (cleaning could go here, but
    # for minimal experiment we save raw and let user inspect)
    output_path.write_text(markdown, encoding="utf-8")
    print(f"✓ Saved Markdown: {output_path}")

    # Brief summary
    n_lines = len(markdown.split("\n"))
    n_headers = sum(
        1 for line in markdown.split("\n") if line.lstrip().startswith("#")
    )
    n_bullets = sum(
        1 for line in markdown.split("\n") if line.lstrip().startswith("-")
    )
    n_timestamps = len(re.findall(r"\(\d+:\d+(?::\d+)?\)", markdown))

    print(f"\n{'=' * 60}")
    print(f"  Quick stats")
    print(f"{'=' * 60}")
    print(f"Lines:       {n_lines}")
    print(f"Headers (#): {n_headers}")
    print(f"Bullets (-): {n_bullets}")
    print(f"Timestamps:  {n_timestamps}")
    print()
    print(f"Inspect with: cat {output_path}")
    print(f"  or:         less {output_path}")


if __name__ == "__main__":
    main()