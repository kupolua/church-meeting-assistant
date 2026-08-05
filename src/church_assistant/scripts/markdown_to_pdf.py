"""
CLI: render a Markdown document to PDF with the same layout as the protocols.

For documents the church writes by hand — requirement lists, role descriptions —
as opposed to the generated meeting protocols. Keeping the source as Markdown
means the text stays editable and diffable; the PDF is only the printable form,
regenerated whenever the source changes.

Expected shape (the same subset shared/pdf_export renders):

    # Document title
    > optional note line, shown under the title

    ## Section heading
    Intro paragraph.

    - point
      - sub-point

Anything deeper than one level of nesting is flattened — a printed list with
four levels helps nobody.

Usage:
    uv run python -m church_assistant.scripts.markdown_to_pdf \
        data/tenants/default/documents/vymohy-do-buhhaltera.md

    # explicit output path
    uv run python -m church_assistant.scripts.markdown_to_pdf INPUT.md -o OUT.pdf
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from church_assistant.shared import pdf_export
from church_assistant.shared.meetings_index import Topic


def parse_markdown(text: str) -> tuple[str, str | None, list[Topic]]:
    """
    Split the document into (title, note, sections).

    Deliberately not a Markdown library: this recognises exactly the four
    constructs above, so an unexpected one shows up as plain text rather than
    being silently reinterpreted.
    """
    title = ""
    note: str | None = None
    sections: list[Topic] = []

    current_title: str | None = None
    body: list[str] = []

    def flush() -> None:
        if current_title is not None:
            sections.append(
                Topic(title=current_title, body="\n".join(body).strip(),
                      order=len(sections))
            )

    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("# ") and not title:
            title = line[2:].strip()
        elif line.startswith("## "):
            flush()
            current_title = line[3:].strip()
            body = []
        elif line.startswith("> ") and current_title is None:
            note = line[2:].strip()
        elif current_title is not None:
            body.append(raw)

    flush()
    return title, note, sections


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render a Markdown document to PDF (protocol styling)",
    )
    parser.add_argument("source", type=Path, help="Markdown file to render")
    parser.add_argument(
        "-o", "--output", type=Path, default=None,
        help="Output PDF (default: alongside the source, same name)",
    )
    args = parser.parse_args()

    if not args.source.is_file():
        print(f"❌ Not found: {args.source}", file=sys.stderr)
        raise SystemExit(1)

    title, note, sections = parse_markdown(args.source.read_text(encoding="utf-8"))
    if not title:
        print("❌ No '# Title' line in the source", file=sys.stderr)
        raise SystemExit(2)
    if not sections:
        print("❌ No '## Section' headings in the source", file=sys.stderr)
        raise SystemExit(2)

    try:
        pdf = pdf_export.build_document_pdf(
            title, sections,
            header_note=pdf_export._inline_markup(note) if note else None,
        )
    except pdf_export.FontNotFound as e:
        print(f"❌ {e}", file=sys.stderr)
        raise SystemExit(3)

    out = args.output or args.source.with_suffix(".pdf")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(pdf)

    print(f"✓ {out}")
    print(f"  {title}")
    print(f"  {len(sections)} sections, {len(pdf) / 1024:.0f} KB")


if __name__ == "__main__":
    main()
