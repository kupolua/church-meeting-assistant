"""
Render a meeting's topics to PDF.

The protocol is written in Ukrainian, so the only real constraint here is a font
that covers Cyrillic: the PDF built-ins (Helvetica and friends) are Latin-1 and
would silently produce black boxes rather than an error. Hence the font search
below, and the explicit failure when nothing suitable is found — a protocol full
of tofu is worse than a message saying which package to install.

Timestamps are stripped (meetings_index.strip_timestamps): they exist to seek
the recording, which paper cannot do.

Layout mirrors what polished.md actually contains — a topic heading, an intro
paragraph, then one or two levels of bullets — rather than being a general
Markdown renderer, because that is the only shape the analysis produces.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Iterable, Optional, Sequence

from dotenv import load_dotenv
from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate, Frame, PageTemplate, Paragraph, Spacer,
)

from church_assistant.shared.meetings_index import Topic, strip_timestamps


FONT_NAME = "CmaSans"
FONT_NAME_BOLD = "CmaSans-Bold"

# Searched in order. macOS ships Arial; Linux distributions ship DejaVu or
# Liberation. All three cover Ukrainian, including the ї / є / ґ / ʼ that a
# "Cyrillic" font sometimes omits.
_FONT_CANDIDATES: Sequence[tuple[str, str]] = (
    ("/System/Library/Fonts/Supplemental/Arial.ttf",
     "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
     "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"),
)


class FontNotFound(RuntimeError):
    """No Cyrillic-capable TTF available — refuse rather than emit tofu."""


def _resolve_font() -> tuple[Path, Path]:
    """(regular, bold) TTF paths. PDF_FONT_PATH / PDF_FONT_BOLD_PATH override."""
    load_dotenv()
    override = os.getenv("PDF_FONT_PATH", "").strip()
    if override:
        regular = Path(override)
        bold_env = os.getenv("PDF_FONT_BOLD_PATH", "").strip()
        bold = Path(bold_env) if bold_env else regular
        if not regular.is_file():
            raise FontNotFound(f"PDF_FONT_PATH does not exist: {regular}")
        return regular, (bold if bold.is_file() else regular)

    for regular_s, bold_s in _FONT_CANDIDATES:
        regular = Path(regular_s)
        if regular.is_file():
            bold = Path(bold_s)
            return regular, (bold if bold.is_file() else regular)

    raise FontNotFound(
        "No Cyrillic-capable TTF found. Install one (Debian/Ubuntu: "
        "`apt install fonts-dejavu-core`) or set PDF_FONT_PATH in .env. "
        f"Looked in: {', '.join(c[0] for c in _FONT_CANDIDATES)}"
    )


_fonts_registered = False


def _register_fonts() -> None:
    """Register the TTFs with reportlab once per process."""
    global _fonts_registered
    if _fonts_registered:
        return
    regular, bold = _resolve_font()
    pdfmetrics.registerFont(TTFont(FONT_NAME, str(regular)))
    pdfmetrics.registerFont(TTFont(FONT_NAME_BOLD, str(bold)))
    pdfmetrics.registerFontFamily(FONT_NAME, normal=FONT_NAME, bold=FONT_NAME_BOLD)
    _fonts_registered = True


# ─────────────────────────────────────────────────────────────
# Content shaping
# ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _Line:
    """One rendered line: an intro paragraph, or a bullet at some depth."""
    text: str
    depth: int          # -1 = plain paragraph, 0 = bullet, 1 = sub-bullet


_BULLET_RE = re.compile(r"^(?P<indent>\s*)[-*]\s+(?P<text>.*)$")


def _shape_body(body: str) -> list[_Line]:
    """
    Turn a topic body into paragraphs and bullets.

    polished.md uses two-space indentation for sub-points; anything deeper is
    clamped, since a printed protocol with four levels of nesting helps nobody.
    """
    lines: list[_Line] = []
    for raw in body.splitlines():
        if not raw.strip():
            continue
        m = _BULLET_RE.match(raw)
        if m:
            depth = min(len(m.group("indent")) // 2, 1)
            text = m.group("text").strip()
            if text:
                lines.append(_Line(text, depth))
        else:
            lines.append(_Line(raw.strip(), -1))
    return lines


def _escape(text: str) -> str:
    """Escape for reportlab's mini-HTML paragraph markup."""
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def format_meeting_date(meeting_date: str) -> str:
    """'2026-07-30' → '30.07.2026'. Anything unexpected passes through."""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", meeting_date)
    return f"{m.group(3)}.{m.group(2)}.{m.group(1)}" if m else meeting_date


def document_title(meeting_date: str) -> str:
    return f"Пасторська зустріч {format_meeting_date(meeting_date)}"


# ─────────────────────────────────────────────────────────────
# Rendering
# ─────────────────────────────────────────────────────────────

def build_topics_pdf(meeting_date: str, topics: Iterable[Topic]) -> bytes:
    """
    Render the meeting's topics to PDF bytes.

    Raises FontNotFound if no Cyrillic font is available.
    """
    _register_fonts()

    title = document_title(meeting_date)
    topics = list(topics)

    base = ParagraphStyle(
        "cma-base", fontName=FONT_NAME, fontSize=10.5, leading=15,
        alignment=TA_JUSTIFY, spaceAfter=0,
        # bulletFontName defaults to Times-Roman, NOT the paragraph font. The
        # bullet then comes from a built-in Latin-1 font, and while it looks
        # right it extracts as "(cid:127)" — so copying text out of the archived
        # protocol yields mojibake at the start of every point.
        bulletFontName=FONT_NAME,
    )
    styles = {
        "title": ParagraphStyle(
            "cma-title", parent=base, fontName=FONT_NAME_BOLD, fontSize=17,
            leading=22, alignment=0, spaceAfter=2 * mm,
        ),
        "topic": ParagraphStyle(
            "cma-topic", parent=base, fontName=FONT_NAME_BOLD, fontSize=12.5,
            leading=17, alignment=0, spaceBefore=6 * mm, spaceAfter=1.5 * mm,
            textColor=colors.HexColor("#1f2a44"),
        ),
        "intro": ParagraphStyle("cma-intro", parent=base, spaceAfter=1.5 * mm),
        "bullet": ParagraphStyle(
            "cma-bullet", parent=base, leftIndent=6 * mm, bulletIndent=1.5 * mm,
            spaceAfter=1 * mm,
        ),
        "subbullet": ParagraphStyle(
            "cma-subbullet", parent=base, leftIndent=12 * mm,
            bulletIndent=7.5 * mm, fontSize=10, leading=14,
            spaceAfter=0.8 * mm, textColor=colors.HexColor("#333333"),
        ),
    }

    story: list = [Paragraph(_escape(title), styles["title"])]
    if not topics:
        story.append(Paragraph(
            "У протоколі цієї зустрічі немає розглянутих тем.", styles["intro"]
        ))

    for index, topic in enumerate(topics, 1):
        heading = f"{index}. {_escape(strip_timestamps(topic.title).strip())}"
        story.append(Paragraph(heading, styles["topic"]))

        for line in _shape_body(strip_timestamps(topic.body)):
            text = _escape(line.text)
            if line.depth < 0:
                story.append(Paragraph(text, styles["intro"]))
            elif line.depth == 0:
                story.append(Paragraph(text, styles["bullet"], bulletText="•"))
            else:
                story.append(Paragraph(text, styles["subbullet"], bulletText="–"))

    story.append(Spacer(1, 4 * mm))

    buffer = BytesIO()
    doc = BaseDocTemplate(
        buffer, pagesize=A4,
        leftMargin=20 * mm, rightMargin=18 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
        title=title, author="Church Meeting Assistant", subject=title,
    )
    frame = Frame(
        doc.leftMargin, doc.bottomMargin, doc.width, doc.height, id="body"
    )

    def draw_footer(canvas, document) -> None:
        """Page number, and the title repeated so a loose page is identifiable."""
        canvas.saveState()
        canvas.setFont(FONT_NAME, 8)
        canvas.setFillColor(colors.HexColor("#888888"))
        canvas.drawString(doc.leftMargin, 11 * mm, title)
        canvas.drawRightString(
            A4[0] - doc.rightMargin, 11 * mm, str(document.page)
        )
        canvas.restoreState()

    doc.addPageTemplates([
        PageTemplate(id="cma", frames=[frame], onPage=draw_footer)
    ])
    doc.build(story)
    return buffer.getvalue()


def pdf_filename(meeting_date: str) -> str:
    """Filename for the download, e.g. 'Пасторська зустріч 30.07.2026.pdf'."""
    return f"{document_title(meeting_date)}.pdf"


# ─────────────────────────────────────────────────────────────
# CLI smoke test
# ─────────────────────────────────────────────────────────────

def _smoke_test() -> None:
    from church_assistant.shared import meetings_index, tenant_paths

    print("=" * 66)
    print("  pdf_export — smoke test")
    print("=" * 66)

    regular, bold = _resolve_font()
    print(f"1. font: {regular.name} / {bold.name} ✓")

    # Timestamps go, and only timestamps go.
    cases = [
        ("Пункт про фінанси (01:51)", "Пункт про фінанси"),
        ("Кілька моментів (24:11, 28:16)", "Кілька моментів"),
        ("Крапка з комою (31:30; 33:52; 34:42)", "Крапка з комою"),
        ("З годинами (1:02:03)", "З годинами"),
        ("Псалом 84:6 лишається", "Псалом 84:6 лишається"),
        ("Дужки (не таймкод) лишаються", "Дужки (не таймкод) лишаються"),
        ("Текст (02:00), далі", "Текст, далі"),
    ]
    for src, want in cases:
        got = strip_timestamps(src)
        assert got == want, f"{src!r} -> {got!r}, expected {want!r}"
    print(f"2. strip_timestamps ✓ ({len(cases)} cases, Bible refs untouched)")

    meetings_dir = tenant_paths.paths_for(
        tenant_paths.legacy_slug() or "default"
    ).meetings
    summaries = meetings_index.list_all_summaries(meetings_dir)
    assert summaries, "no meetings to render"
    detail = meetings_index.load_detail(meetings_dir, summaries[0].date)
    assert detail is not None

    pdf = build_topics_pdf(detail.date, detail.topics)
    assert pdf.startswith(b"%PDF-"), "not a PDF"
    print(f"3. built {detail.date}: {len(detail.topics)} topics, "
          f"{len(pdf) / 1024:.0f} KB ✓")

    # An empty meeting must still produce a valid document, not a traceback.
    assert build_topics_pdf("2026-01-01", []).startswith(b"%PDF-")
    print("4. meeting with no topics still renders ✓")

    print(f"5. title: {document_title(detail.date)!r} ✓")
    print("=" * 66)
    print("  ✓ ALL PDF_EXPORT SMOKE TESTS PASSED")
    print("=" * 66)


if __name__ == "__main__":
    _smoke_test()
