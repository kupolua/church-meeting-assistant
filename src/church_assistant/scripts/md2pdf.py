"""
Markdown → A4 PDF with the project's Cyrillic font.

    uv run python -m church_assistant.scripts.md2pdf in.md out.pdf

WHY. Analyses written for the council (the first: the secretary analysis of
22 protocols, 18.11.2026) are handed around as files, and the user's rule
since that day is that any work ending in conclusions goes to PDF at once.
The site's own exporter (shared/pdf_export.py) renders meetings, not free
text, so this takes the markdown subset those documents use — #/##/###
headings, paragraphs, - and 1. lists with indented continuation lines,
| tables |, **bold**, `code` — and nothing more. Fonts come from
pdf_export._register_fonts (Arial on macOS, DejaVu/Liberation on Linux),
which refuses to run without a font that has ї/є/ґ.
"""
import re, sys
from pathlib import Path
from xml.sax.saxutils import escape
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, ListFlowable, ListItem, Table, TableStyle, KeepTogether
from church_assistant.shared import pdf_export as pe

pe._register_fonts()
F, FB = pe.FONT_NAME, pe.FONT_NAME_BOLD
base = ParagraphStyle("b", fontName=F, fontSize=10.5, leading=14.5, spaceAfter=5)
h1 = ParagraphStyle("h1", parent=base, fontName=FB, fontSize=17, leading=21, spaceBefore=4, spaceAfter=10)
h2 = ParagraphStyle("h2", parent=base, fontName=FB, fontSize=13.5, leading=17, spaceBefore=14, spaceAfter=6, textColor=colors.HexColor("#1f3a6e"))
h3 = ParagraphStyle("h3", parent=base, fontName=FB, fontSize=11.5, leading=15, spaceBefore=10, spaceAfter=4)
small = ParagraphStyle("s", parent=base, fontSize=9, leading=12, textColor=colors.HexColor("#555555"))
cell = ParagraphStyle("c", parent=base, fontSize=9, leading=12, spaceAfter=0)

def inline(t):
    t = escape(t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"`([^`]+)`", r"<font face='Courier'>\1</font>", t)
    t = re.sub(r"~~(.+?)~~", r"<strike>\1</strike>", t)
    return t

def build(md: str):
    story, lines, i = [], md.splitlines(), 0
    para = []
    def flush():
        nonlocal para
        if para:
            story.append(Paragraph(inline(" ".join(para)), base)); para = []
    while i < len(lines):
        l = lines[i]
        if l.startswith("# "):
            flush(); story.append(Paragraph(inline(l[2:]), h1))
        elif l.startswith("## "):
            flush(); story.append(Paragraph(inline(l[3:]), h2))
        elif l.startswith("### "):
            flush(); story.append(Paragraph(inline(l[4:]), h3))
        elif l.startswith("|"):
            flush(); rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r"-+", c) for c in cells):
                    rows.append([Paragraph(inline(c), cell) for c in cells])
                i += 1
            if rows:
                n = len(rows[0]); w = (A4[0] - 40 * mm) / n
                t = Table(rows, colWidths=[w] * n, repeatRows=1)
                t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#bbbbbb")),
                                       ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eef2f8")),
                                       ("VALIGN", (0, 0), (-1, -1), "TOP")]))
                story.append(t); story.append(Spacer(1, 6))
            continue
        elif re.match(r"^(\d+[а-яa-z]?)\. ", l) or l.startswith("- "):
            flush(); items = []; ordered = not l.startswith("- ")
            while i < len(lines) and (re.match(r"^(\d+[а-яa-z]?)\. ", lines[i]) or lines[i].startswith("- ") or (lines[i].startswith("   ") and lines[i].strip())):
                if lines[i].startswith("   ") and items:
                    items[-1] += " " + lines[i].strip()
                else:
                    items.append(re.sub(r"^(\d+[а-яa-z]?\. |- )", "", lines[i]))
                i += 1
            story.append(ListFlowable([ListItem(Paragraph(inline(x), base), leftIndent=12) for x in items],
                                      bulletType="1" if ordered else "bullet", bulletFontName=F, bulletFontSize=9.5, leftIndent=14))
            continue
        elif not l.strip():
            flush()
        else:
            para.append(l.strip())
        i += 1
    flush()
    return story

src, out = Path(sys.argv[1]), Path(sys.argv[2])
md = src.read_text(encoding="utf-8")
doc = SimpleDocTemplate(str(out), pagesize=A4, leftMargin=20*mm, rightMargin=20*mm, topMargin=18*mm, bottomMargin=18*mm,
                        title=re.search(r"^# (.+)$", md, re.M).group(1) if re.search(r"^# (.+)$", md, re.M) else src.stem, author="CMA")
def footer(c, d):
    c.saveState(); c.setFont(F, 8); c.setFillColor(colors.HexColor("#777777"))
    c.drawRightString(A4[0] - 20*mm, 10*mm, f"стор. {d.page}"); c.drawString(20*mm, 10*mm, src.stem); c.restoreState()
doc.build(build(md), onFirstPage=footer, onLaterPages=footer)
print(out, out.stat().st_size, "bytes")
