"""
Draft "Слухали" / "Вирішили" for a meeting whose audio was processed earlier.

    uv run python -m church_assistant.scripts.draft_protocol              # list
    uv run python -m church_assistant.scripts.draft_protocol --date 2026-08-17

WHY THIS EXISTS. Gemma drafts the protocol as the last step of ingestion, which
is right for a meeting recorded from now on — by the time the chair opens the
page, the draft is already there. It does nothing for the twenty meetings
processed before the protocol existed, and nothing for a meeting whose agenda
gets written after the recording was handled, which the "open a meeting" form
allows on purpose. Without this, those chairs retype "Слухали" out of the Теми
section by hand.

WHY A SCRIPT AND NOT A BUTTON. The web tier runs on the VPS, which has no
models at all — a route cannot call Gemma however long it is willing to wait.
A button therefore needs a queue and a worker to drain it (backlog 13's
neighbour), which is a real piece of design; this is one command on the machine
where Gemma already is, and it works today.

DEFAULT IS TO LIST, like purge_archived_tenants. Drafting overwrites nothing —
it only fills fields the chair has left empty — but a run that prints what it
is about to touch is a run that can be stopped.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from dotenv import load_dotenv

from church_assistant.db import protocols_repo, tenants_repo
from church_assistant.db.connection import close_pool, get_pool
from church_assistant.db.tenant_context import tenant_cursor
from church_assistant.ingestion import protocol_draft
from church_assistant.shared import tenant_paths

# Load .env here rather than relying on get_pool() to have done it first — the
# lesson from purge_archived_tenants, where QDRANT_URL resolved only by accident
# of import order and the fallback pointed at a machine that has no Qdrant.
load_dotenv()


async def _resolve_tenant(pool, slug: str) -> tuple[int, str]:
    tenant = await tenants_repo.get_by_slug(pool, slug)
    if tenant is None:
        raise SystemExit(f"✗ немає церкви «{slug}»")
    return int(tenant["id"]), str(tenant["slug"])


async def _list(tenant_slug: str) -> int:
    pool = await get_pool()
    tenant_id, slug = await _resolve_tenant(pool, tenant_slug)

    rows = await protocols_repo.list_all(pool, tenant_id)
    if not rows:
        print(f"У церкві «{slug}» немає жодного протоколу.")
        return 0

    print(f"{'Дата':<12} {'Номер':<16} {'Стан':<12} Питань  Без «Слухали»")
    print("-" * 70)
    for r in rows:
        items = await protocols_repo.list_items(pool, tenant_id, int(r["id"]))
        blank = sum(1 for i in items if not i["heard"].strip())
        print(f"{r['meeting_date']:%Y-%m-%d}   "
              f"{protocols_repo.number(r):<16} {r['status']:<12} "
              f"{len(items):>6}  {blank:>12}")
    print()
    print("Заповнити чернетку:  --date YYYY-MM-DD")
    return 0


async def _draft(tenant_slug: str, date: str, decisions: bool) -> int:
    pool = await get_pool()
    tenant_id, slug = await _resolve_tenant(pool, tenant_slug)

    protocol = await protocols_repo.get_by_date(pool, tenant_id, date)
    if protocol is None:
        print(f"✗ для {date} протокол не відкрито. Відкрийте його в панелі "
              f"«Зустрічі» — там же призначається ведучий.", file=sys.stderr)
        return 1
    if protocol["status"] == "approved":
        print(f"✗ протокол {protocols_repo.number(protocol)} затверджено — "
              f"його вже не змінити.", file=sys.stderr)
        return 1

    items = await protocols_repo.list_items(pool, tenant_id, int(protocol["id"]))
    if not items:
        print(f"✗ у протоколі {protocols_repo.number(protocol)} немає жодного "
              f"питання. Порядок денний пише ведучий — Gemma його не вигадує.",
              file=sys.stderr)
        return 1

    meetings_dir = tenant_paths.paths_for(slug).meetings
    if not (meetings_dir / date).exists():
        print(f"✗ немає теки зустрічі {meetings_dir / date} — розподіляти нічого. "
              f"Спершу обробка запису.", file=sys.stderr)
        return 1

    print(f"Протокол {protocols_repo.number(protocol)}, питань: {len(items)}")

    print("  · «Слухали» — Gemma читає теми зустрічі…", flush=True)
    heard = await protocol_draft.draft_heard(
        pool, tenant_id, date, meetings_dir / date)
    if heard is None:
        print("    · нічого не змінено (усі «Слухали» вже заповнені, "
              "або в зустрічі немає тем)")
    else:
        print(f"    ✓ {heard['items']} питань, розподілено "
              f"{heard['assigned']} із {heard['topics']} тем")

    if decisions:
        print("  · «Вирішили» та «Постановили» — по одному виклику на питання…",
              flush=True)
        made = await protocol_draft.draft_decisions(pool, tenant_id, date)
        if made is None:
            print("    · нічого не змінено")
        else:
            print(f"    ✓ «Вирішили» для {made['resolved']} питань, "
                  f"{made['rulings']} постанов")
            if made["resolved"] < len(items):
                # Silence here would read as "Gemma had nothing to say", and it
                # usually means she degenerated and the filter refused it.
                print("    ⚠ частина питань лишилась порожньою — Gemma або не "
                      "знайшла рішення, або видала нісенітницю, яку відсіяв "
                      "фільтр. Ці поля пише ведучий.")

    print("\nЧернетка готова. Ведучий бачить її на сторінці зустрічі й править.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--date", metavar="YYYY-MM-DD",
                   help="зустріч, для якої заповнити чернетку")
    p.add_argument("--tenant", default=tenant_paths.legacy_slug() or "default",
                   help="церква (ідентифікатор)")
    p.add_argument("--heard-only", action="store_true",
                   help="лише «Слухали», без «Вирішили» (швидше)")
    a = p.parse_args()

    async def run() -> int:
        try:
            if a.date:
                return await _draft(a.tenant, a.date, decisions=not a.heard_only)
            return await _list(a.tenant)
        finally:
            await close_pool()

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
