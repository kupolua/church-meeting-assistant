"""Generate structured protocol from annotated transcript using Gemma 4.

Updated version (v2) with:
- Save raw response BEFORE parsing (so a parse failure doesn't lose 14 min of Gemma work)
- Cleanup of common LLM output wrappers (markdown code blocks, YAML separators)
- Explicit num_predict to avoid Ollama default truncation
- --limit-minutes flag to test on a small transcript slice first
- More verbose logging

Architectural decisions (unchanged):
- One-shot prompt with golden_protocol as few-shot example
- Structured JSON output (Ollama format=json)
- num_ctx=65536 to fit example + transcript + output

Usage:
    # Quick test on first 30 minutes of transcript (~3-5 min Gemma time)
    uv run python -m church_assistant.analyze_transcript --limit-minutes 30

    # Full transcript
    uv run python -m church_assistant.analyze_transcript

    # Force re-analysis, ignore cache
    uv run python -m church_assistant.analyze_transcript --no-cache
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

import ollama


# Default paths
DEFAULT_TRANSCRIPT = Path("data/test_baseline_annotated.md")
DEFAULT_EXAMPLE = Path("data/golden_protocol_clean.md")
DEFAULT_OUTPUT = Path("data/test_baseline_protocol.json")
DEFAULT_MARKDOWN_OUTPUT = Path("data/test_baseline_protocol.md")

# Model parameters
MODEL_NAME = "gemma4:26b"
NUM_CTX = 65536          # fits example + transcript + output
NUM_PREDICT = 8192       # max output tokens — explicit to override Ollama defaults
TEMPERATURE = 0.0        # deterministic for protocol generation


SYSTEM_PROMPT = """Ти професійний аналітик церковних зустрічей.

Твоя задача — створити структурований протокол на основі стенограми зустрічі команди пасторів.

ВАЖЛИВІ ПРАВИЛА:
1. Відповідай ВИКЛЮЧНО валідним JSON у форматі, який зазначено нижче. БЕЗ markdown-обгорток. БЕЗ роздільників ---. ПОЧИНАЙ ВІДПОВІДЬ ЗАРАЗ ЖЕ З СИМВОЛУ {.
2. Використовуй ВИКЛЮЧНО інформацію зі стенограми — не вигадуй фактів.
3. Часові мітки (timestamps) бери ЗІ стенограми у форматі HH:MM:SS або MM:SS.
4. Імена учасників атрибутуй ТОЧНО так, як у стенограмі (Роман, Павло, Чед, тощо).
5. Якщо у стенограмі є сегменти з міткою [нерозбірливо] — пропускай їх або позначай у details як "часткова інформація".

JSON СХЕМА:
{
  "title": "string — назва протоколу",
  "date": "string — дата у форматі DD/MM/YYYY",
  "format": "string — формат зустрічі (Zoom / офлайн)",
  "presence": ["string", ...] — список присутніх,
  "topics_overview": [
    {
      "title": "string — назва теми",
      "summary": "string — короткий опис (1-2 речення)"
    }
  ],
  "detailed_topics": [
    {
      "title": "string — назва теми",
      "intro": "string — вступний параграф про важливість теми",
      "discussion_points": [
        {
          "main_point": "string — головна теза",
          "timestamp": "string — час у форматі HH:MM:SS або MM:SS",
          "details": [
            "string — деталь 1",
            "string — деталь 2"
          ]
        }
      ]
    }
  ],
  "action_items": [
    {
      "person": "string — ім'я відповідального",
      "tasks": [
        {
          "description": "string — опис завдання",
          "timestamp": "string — час у форматі HH:MM:SS або MM:SS"
        }
      ]
    }
  ]
}

Структуруй детальні теми у порядку, у якому вони обговорювались. Action items згрупуй за людиною."""


USER_PROMPT_TEMPLATE = """Ось приклад протоколу хорошої якості (для зразка структури і стилю):

<example_protocol>
{example}
</example_protocol>

Тепер створи такий самий за структурою протокол для цієї стенограми:

<transcript>
{transcript}
</transcript>

Поверни валідний JSON, який відповідає схемі. Почни відповідь ЗАРАЗ ЖЕ з символу {{."""


def load_text(path: Path) -> str:
    """Load text from file."""
    with path.open("r", encoding="utf-8") as f:
        return f.read()


def truncate_transcript_by_minutes(
        transcript_text: str,
        limit_minutes: int,
) -> str:
    """Keep only segments before `limit_minutes` minute mark.

    Annotated transcript lines look like:
        [00:00 Роман]: ...
        [01:14:00 Чед]: ...

    We extract the timestamp from each line and keep only those <= limit_minutes.
    """
    limit_seconds = limit_minutes * 60
    kept_lines: list[str] = []

    for line in transcript_text.split("\n"):
        # Extract timestamp from format [MM:SS Name]: or [H:MM:SS Name]:
        match = re.match(r"^\[(\d+):(\d+)(?::(\d+))?\s+", line)
        if not match:
            # Header lines, empty lines — keep them
            kept_lines.append(line)
            continue

        parts = match.groups()
        if parts[2] is not None:
            # H:MM:SS format
            h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
            total_seconds = h * 3600 + m * 60 + s
        else:
            # MM:SS format
            m, s = int(parts[0]), int(parts[1])
            total_seconds = m * 60 + s

        if total_seconds <= limit_seconds:
            kept_lines.append(line)
        else:
            break  # transcript is sorted by time

    return "\n".join(kept_lines)


def cleanup_llm_response(content: str) -> str:
    """Strip common LLM output wrappers before JSON parsing.

    Handles:
    - YAML separator at start: `---\\n`
    - Markdown code blocks: ```json ... ```
    - Leading/trailing whitespace
    - Text before/after the JSON object
    """
    content = content.strip()

    # Remove YAML separator at start
    content = re.sub(r"^---\s*\n", "", content)

    # Remove markdown code block fences
    content = re.sub(r"^```(?:json)?\s*\n", "", content)
    content = re.sub(r"\n```\s*$", "", content)

    # If the response still has text before {, try to find the JSON
    first_brace = content.find("{")
    last_brace = content.rfind("}")
    if first_brace != -1 and last_brace != -1 and first_brace < last_brace:
        content = content[first_brace : last_brace + 1]

    return content.strip()


def call_gemma(
        system_prompt: str,
        user_prompt: str,
        raw_output_path: Path,
        model: str = MODEL_NAME,
        num_ctx: int = NUM_CTX,
        num_predict: int = NUM_PREDICT,
        temperature: float = TEMPERATURE,
) -> dict:
    """Call Gemma via Ollama with structured JSON output.

    Saves raw response BEFORE attempting to parse JSON, so a parse failure
    doesn't lose the Gemma work (which can take 10+ minutes).

    Returns the parsed JSON dict.
    """
    print(
        f"Calling {model} (num_ctx={num_ctx}, num_predict={num_predict}, "
        f"temperature={temperature})..."
    )
    print(f"This may take 1-15 minutes depending on transcript length.")

    start = time.time()

    response = ollama.chat(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        options={
            "num_ctx": num_ctx,
            "num_predict": num_predict,
            "temperature": temperature,
        },
        format="json",
    )

    elapsed = time.time() - start
    print(f"✓ Gemma response in {elapsed:.1f}s ({elapsed/60:.1f} min)")

    # Get raw content
    content = response["message"]["content"]
    print(f"Raw response length: {len(content)} characters")

    # CRITICAL: Save raw response FIRST, before any parsing that could fail.
    # If we lose 14 min of Gemma work because of a parse error, we cry.
    raw_output_path.parent.mkdir(parents=True, exist_ok=True)
    raw_output_path.write_text(content, encoding="utf-8")
    print(f"✓ Raw response saved to: {raw_output_path}")

    # Now try to clean and parse
    cleaned = cleanup_llm_response(content)
    if cleaned != content:
        print(
            f"  (cleaned: removed wrappers, "
            f"{len(content) - len(cleaned)} chars stripped)"
        )

    try:
        parsed = json.loads(cleaned)
        print(f"✓ JSON parsed successfully")
        return parsed
    except json.JSONDecodeError as e:
        print(f"\n❌ JSON parse failed: {e}")
        print(f"   Raw response preserved at: {raw_output_path}")
        print(f"   Cleaned response preview (first 500 chars):")
        print(f"   {cleaned[:500]}")
        raise


def render_markdown(protocol: dict) -> str:
    """Convert parsed JSON protocol to Markdown format."""
    lines: list[str] = []

    # Title
    title = protocol.get("title", "Протокол зустрічі")
    lines.append(f"# {title}")
    lines.append("")

    # Metadata
    if "date" in protocol:
        lines.append(f"**Дата:** {protocol['date']}")
    if "format" in protocol:
        lines.append(f"**Формат:** {protocol['format']}")
    lines.append("")

    # Presence
    if "presence" in protocol and protocol["presence"]:
        lines.append("## Присутні")
        for person in protocol["presence"]:
            lines.append(f"- {person}")
        lines.append("")

    # Topics overview
    if "topics_overview" in protocol and protocol["topics_overview"]:
        lines.append("## Перелік розглянутих питань")
        for topic in protocol["topics_overview"]:
            t_title = topic.get("title", "")
            t_summary = topic.get("summary", "")
            lines.append(f"- **{t_title}:** {t_summary}")
        lines.append("")

    # Detailed topics
    if "detailed_topics" in protocol and protocol["detailed_topics"]:
        lines.append("## Розглянуті питання")
        lines.append("")
        for topic in protocol["detailed_topics"]:
            t_title = topic.get("title", "")
            t_intro = topic.get("intro", "")
            lines.append(f"### {t_title}")
            if t_intro:
                lines.append(t_intro)
            lines.append("")
            for point in topic.get("discussion_points", []):
                main = point.get("main_point", "")
                ts = point.get("timestamp", "")
                ts_part = f" ({ts})" if ts else ""
                lines.append(f"- {main}{ts_part}")
                for detail in point.get("details", []):
                    lines.append(f"  - {detail}")
            lines.append("")

    # Action items
    if "action_items" in protocol and protocol["action_items"]:
        lines.append("## Наступні кроки")
        lines.append("")
        for entry in protocol["action_items"]:
            person = entry.get("person", "")
            lines.append(f"### {person}")
            for task in entry.get("tasks", []):
                desc = task.get("description", "")
                ts = task.get("timestamp", "")
                ts_part = f" ({ts})" if ts else ""
                lines.append(f"- {desc}{ts_part}")
            lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate structured protocol from annotated transcript"
    )
    parser.add_argument(
        "--transcript",
        type=Path,
        default=DEFAULT_TRANSCRIPT,
        help=f"Annotated transcript Markdown (default: {DEFAULT_TRANSCRIPT})",
    )
    parser.add_argument(
        "--example",
        type=Path,
        default=DEFAULT_EXAMPLE,
        help=f"Golden protocol example (default: {DEFAULT_EXAMPLE})",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Output JSON path (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--markdown",
        type=Path,
        default=DEFAULT_MARKDOWN_OUTPUT,
        help=f"Output Markdown path (default: {DEFAULT_MARKDOWN_OUTPUT})",
    )
    parser.add_argument(
        "--limit-minutes",
        type=int,
        default=None,
        help="Use only the first N minutes of transcript (for quick testing)",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Force re-analysis, ignore cached JSON",
    )
    args = parser.parse_args()

    # Validate inputs
    for path, name in [
        (args.transcript, "transcript"),
        (args.example, "example"),
    ]:
        if not path.exists():
            print(f"❌ {name} not found: {path}")
            raise SystemExit(1)

    # Determine output paths — different for limited vs full runs
    if args.limit_minutes:
        suffix = f"_first_{args.limit_minutes}min"
        output_path = args.output.with_stem(args.output.stem + suffix)
        markdown_path = args.markdown.with_stem(args.markdown.stem + suffix)
    else:
        output_path = args.output
        markdown_path = args.markdown

    raw_output_path = output_path.with_suffix(".raw.txt")

    # Cache check
    if not args.no_cache and output_path.exists():
        print(f"✓ Loading cached protocol from: {output_path}")
        with output_path.open("r", encoding="utf-8") as f:
            protocol = json.load(f)
    else:
        # Load inputs
        print(f"Loading transcript: {args.transcript}")
        transcript_text = load_text(args.transcript)
        print(f"  ✓ {len(transcript_text)} characters (full)")

        # Truncate if requested
        if args.limit_minutes:
            transcript_text = truncate_transcript_by_minutes(
                transcript_text, args.limit_minutes
            )
            print(
                f"  ✓ {len(transcript_text)} characters "
                f"(limited to first {args.limit_minutes} minutes)"
            )

        print(f"Loading example: {args.example}")
        example_text = load_text(args.example)
        print(f"  ✓ {len(example_text)} characters")

        # Build user prompt
        user_prompt = USER_PROMPT_TEMPLATE.format(
            example=example_text,
            transcript=transcript_text,
        )

        total_input_chars = len(SYSTEM_PROMPT) + len(user_prompt)
        print(
            f"\nTotal input size: ~{total_input_chars} characters "
            f"(~{total_input_chars // 4} tokens approx)"
        )

        # Call Gemma — raw response saved inside
        protocol = call_gemma(
            SYSTEM_PROMPT,
            user_prompt,
            raw_output_path=raw_output_path,
        )

        # Save parsed JSON
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as f:
            json.dump(protocol, f, ensure_ascii=False, indent=2)
        print(f"✓ Saved JSON: {output_path}")

    # Render to Markdown
    markdown = render_markdown(protocol)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    with markdown_path.open("w", encoding="utf-8") as f:
        f.write(markdown)
    print(f"✓ Saved Markdown: {markdown_path}")

    # Quick summary
    print(f"\n{'=' * 70}")
    print(f"  Protocol summary")
    print(f"{'=' * 70}")
    print(f"Title:              {protocol.get('title', '?')}")
    print(
        f"Topics overview:    {len(protocol.get('topics_overview', []))} items"
    )
    print(
        f"Detailed topics:    {len(protocol.get('detailed_topics', []))} topics"
    )
    n_action_items = sum(
        len(p.get("tasks", []))
        for p in protocol.get("action_items", [])
    )
    print(
        f"Action items:       {n_action_items} tasks across "
        f"{len(protocol.get('action_items', []))} people"
    )
    print()
    print(f"Open the result: cat {markdown_path}")


if __name__ == "__main__":
    main()