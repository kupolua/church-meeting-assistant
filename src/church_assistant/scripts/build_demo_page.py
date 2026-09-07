"""
Build the public mockup page from `design/screens/` — one file, no server code.

    uv run python -m church_assistant.scripts.build_demo_page
    uv run python -m church_assistant.scripts.build_demo_page --out /tmp/demo.html

Deployed at https://cma.rechurch.org.ua/demo, served by Caddy straight off disk
(`/srv/cma-demo/index.html`) and never through FastAPI. That is deliberate: the
page has to be showable to people who have no account, while the app behind `/`
still requires one, and a static file cannot accidentally grow a way into the
church's data.

    scp design/demo.html cma@10.10.0.1:/srv/cma-demo/index.html

That is the whole deploy. `/srv/cma-demo` is owned by `cma` (07.09), so updating
the page needs no root and no Caddy reload — the `handle_path /demo*` block is
already in the Caddyfile, and Caddy re-reads the file on every request.

THE SCREENS IN design/screens/ ARE ALREADY ANONYMISED, and that is the rule, not
a step this script performs. The mockups were drawn against the real corpus
because a layout judged on invented text is a layout judged on nothing — but
what lands in git and on a public URL carries invented names, invented topics
and invented decisions. Keeping the substitution here would have meant a table
of real surnames living in the repository forever, which is precisely what the
page is meant to avoid.

If a screen is redrawn against real data again, anonymise before copying it in.
The check at the bottom of this file will refuse the build if it spots a name
from the corpus, but it only knows the names it was told about — it is a
backstop, not the policy.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCREENS = ROOT / "design" / "screens"

# One row per menu item; derived screens flow right, in the order a person
# reaches them. This is the structure Pavlo asked for on 07.09 — the navigation
# is visible in the layout instead of being described beside it.
# The icon is the row label's inner SVG, verbatim — the same markup the screens
# use in their own navigation bars, so the two never drift apart. A single path
# was not enough: the search glyph is a circle and a line.
ROWS: list[tuple[str, str, list[tuple[str, str]]]] = [
    ("Головна", '<path d="M4 11l8-6 8 6M6 10v9h12v-9"></path>',
     [("Головна", "Home")]),
    ("Запис", '<rect x="9" y="3" width="6" height="11" rx="3"></rect>'
              '<path d="M6 11a6 6 0 0 0 12 0M12 17v4"></path>',
     [("Запис зустрічі", "Record")]),
    ("Зустрічі", '<path d="M4 7h16M4 12h16M4 17h10"></path>',
     [("Список зустрічей", "Meetings"), ("Нова зустріч", "NewMeeting"),
      ("Протокол — чернетка", "Draft"), ("Протокол і аудіо", "Main"),
      ("Теми", "Topics"), ("Стенограма", "Transcript")]),
    ("Пошук", '<circle cx="11" cy="11" r="7"></circle>'
              '<path d="M20 20l-3.5-3.5"></path>',
     [("Пошук", "Search"), ("Результати", "Results")]),
]

# Names that must never appear in a public build. A backstop for the rule in the
# docstring, not a substitute for it.
FORBIDDEN = (
    "Вечерківськ", "Кулаковськ", "Бутенко", "Коновалов", "Пономарьов",
    "Пташенко", "Фоменко", "Ковальчук", "Нове життя", "Леонід",
    "Грунтовськ", "Якубів", "Telegram",
)

PAGE = """<!doctype html>
<html lang="uk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Church Meeting Assistant — екрани застосунку</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Onest:wght@400;500;600;700&display=swap">
<style>
  :root{{--ink:#1f2a44;--muted:#6b7280;--rule:#dfe3ea;--ground:#eef0f4;--accent:#2563c9}}
  *{{box-sizing:border-box}}
  html,body{{margin:0;background:var(--ground);color:var(--ink);
    font-family:Onest,system-ui,sans-serif;-webkit-font-smoothing:antialiased}}
  header{{max-width:1100px;margin:0 auto;padding:38px 22px 6px}}
  h1{{margin:0;font-size:clamp(24px,4vw,32px);font-weight:700;letter-spacing:-.02em}}
  header p{{margin:12px 0 0;color:var(--muted);font-size:15.5px;line-height:1.6;max-width:64ch}}
  .note{{margin:18px 0 0;padding:12px 15px;background:#fff;border:1px solid var(--rule);
    border-radius:8px;font-size:14px;color:var(--muted);line-height:1.55;max-width:64ch}}
  .scroll{{overflow-x:auto;padding:26px 0 60px;-webkit-overflow-scrolling:touch}}
  .sheet{{display:flex;flex-direction:column;gap:40px;padding:0 22px;width:max-content}}
  .row{{display:flex;gap:40px;align-items:flex-start}}
  .rowlab{{width:180px;flex:none;display:flex;align-items:center;gap:9px;
    font-size:17px;font-weight:600;padding-top:2px}}
  .col{{display:flex;flex-direction:column;gap:10px;flex:none}}
  .cap{{font-size:15px;font-weight:600;color:var(--muted);height:21px;line-height:21px}}
  .phone{{width:412px;height:915px;overflow:hidden;border:1px solid var(--rule);
    border-radius:14px;background:#fff;box-shadow:0 10px 30px -22px rgba(31,42,68,.5)}}
  footer{{max-width:1100px;margin:0 auto;padding:0 22px 48px;color:var(--muted);font-size:13.5px}}
</style></head><body>
<header>
  <h1>Church Meeting Assistant — екрани застосунку</h1>
  <p>Макети мобільного застосунку: пошук по протоколах громади, читання зустрічі
  з перемоткою аудіо по таймкодах, ведення протоколу й завантаження записів.
  Гортайте вбік — праворуч від кожного пункту меню стоять похідні екрани.</p>
  <div class="note"><b>Дані на екранах вигадані.</b> Структура, кількості й вигляд — справжні,
  але імена, назви тем і тексти замінені: це макет для показу, а не чиїсь протоколи.</div>
</header>
<div class="scroll"><div class="sheet">
{sheet}
</div></div>
<footer>Ескіз, не робочий застосунок: кнопки не натискаються.</footer>
</body></html>
"""


def build() -> str:
    rows: list[str] = []
    for label, icon, screens in ROWS:
        cells = []
        for caption, stem in screens:
            path = SCREENS / f"{stem}.html"
            if not path.exists():
                raise SystemExit(f"✗ немає екрана {path}")
            body = path.read_text(encoding="utf-8").strip()
            cells.append(
                f'<div class="col"><div class="cap">{caption}</div>'
                f'<div class="phone">{body}</div></div>'
            )
        rows.append(
            '<div class="row"><div class="rowlab">'
            '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" '
            f'stroke="#2563c9" stroke-width="2" stroke-linecap="round">{icon}</svg>'
            f'<div>{label}</div></div>' + "".join(cells) + "</div>"
        )
    return PAGE.format(sheet="\n".join(rows))


def main() -> None:
    ap = argparse.ArgumentParser(description="Зібрати сторінку макетів для показу")
    ap.add_argument("--out", default=str(ROOT / "design" / "demo.html"),
                    help="куди покласти зібрану сторінку")
    args = ap.parse_args()

    html = build()
    leaked = [w for w in FORBIDDEN if w in html]
    if leaked:
        print(f"✗ у збірці знайдено реальні дані: {leaked}", file=sys.stderr)
        print("  Знеособте екран у design/screens/ і зберіть знову.", file=sys.stderr)
        sys.exit(1)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    screens = sum(len(s) for _, _, s in ROWS)
    print(f"✓ {out}  ({len(html):,} байт · {screens} екранів · {len(ROWS)} рядків)")
    print(f"  scp {out} cma@10.10.0.1:/srv/cma-demo/index.html")


if __name__ == "__main__":
    main()
