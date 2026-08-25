"""
Remove churches whose year in the archive has run out.

    uv run python -m church_assistant.scripts.purge_archived_tenants            # list
    uv run python -m church_assistant.scripts.purge_archived_tenants --purge first-baptist

DEFAULT IS TO LIST. Nothing is removed without both --purge and the slug spelled
out, and one run removes one church. That is deliberate friction: this is the
only code in the project that destroys a congregation's history, and it should
be impossible to do by accident or in bulk.

THERE IS NO CRON JOB FOR THIS, and adding one would be a mistake. A scheduled
task that erases churches works correctly for years and then meets a clock
change, a restored backup with stale timestamps, or a migration that sets
deleted_at on the wrong rows — and by the time anyone looks, the recordings are
gone. A person reading the list and typing a slug cannot make that class of
mistake. Retention is a promise about the earliest date data may be removed, not
a promise that it will be.

WHAT IT REMOVES, in the order that fails safest:
    1. Qdrant collections   t_<slug>_*     (rebuildable from artifacts)
    2. artifacts            tenants/<slug> (the recordings and protocols)
    3. database rows        ON DELETE CASCADE from tenants
Vectors first because they are the only part that can be regenerated; if the run
dies midway the church is still restorable from what is left. The tenant row goes
last, so an interrupted purge leaves a church that still appears in the archive
rather than orphaned files nobody can attribute.

It refuses the legacy tenant outright — its artifacts sit in the shared data root
rather than under tenants/<slug>/, so the same rmtree would take every church
with it.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from datetime import datetime, timezone

from church_assistant.db import tenants_repo
from church_assistant.db.connection import close_pool, get_pool
from church_assistant.shared import collections, tenant_paths
from church_assistant.shared.logger import Logger


_logger = Logger(process="purge")


def _days_left(purge_after: datetime) -> int:
    return (purge_after - datetime.now(timezone.utc)).days


async def _list() -> int:
    pool = await get_pool()
    rows = await tenants_repo.list_archived(pool)
    if not rows:
        print("Архів порожній.")
        return 0

    print(f"{'Ідентифікатор':<24} {'Архівовано':<12} {'Зберігати до':<14} Стан")
    print("-" * 70)
    for r in rows:
        left = _days_left(r["purge_after"])
        state = "МОЖНА ВИДАЛЯТИ" if r["overdue"] else f"ще {left} дн."
        print(f"{r['slug']:<24} {r['deleted_at']:%Y-%m-%d}   "
              f"{r['purge_after']:%Y-%m-%d}     {state}")
    print()
    print("Щоб видалити назавжди:  --purge <ідентифікатор>")
    return 0


async def _purge(slug: str, force: bool) -> int:
    pool = await get_pool()

    rows = await tenants_repo.list_archived(pool)
    row = next((r for r in rows if r["slug"] == slug), None)
    if row is None:
        print(f"✗ «{slug}» немає в архіві. Видаляти можна лише архівоване — "
              f"спершу архівуйте церкву в панелі /platform.", file=sys.stderr)
        return 1

    if not row["overdue"] and not force:
        left = _days_left(row["purge_after"])
        print(f"✗ «{slug}» ще під захистом: {left} дн. до "
              f"{row['purge_after']:%Y-%m-%d}.\n"
              f"  Якщо церква просить видалити раніше — --force.", file=sys.stderr)
        return 1

    # THE LEGACY TENANT IS NOT PURGEABLE HERE, and this is not a formality.
    # paths_for() returns the SHARED data root for it — not data/tenants/<slug> —
    # so rmtree below would take every church's artifacts with it, and its
    # collections are the unprefixed cma_* ones the whole original corpus lives
    # in. Removing that corpus is a migration, done deliberately and with a
    # backup in hand, not a retention job.
    paths = tenant_paths.paths_for(slug)
    root = paths.root
    if root == tenant_paths.data_root():
        print(f"✗ «{slug}» — легасі-тенант: його артефакти лежать просто в "
              f"{root}, спільно з усіма. Цей скрипт його не чіпає.\n"
              f"  Спершу перенесіть його у tenants/{slug}/ "
              f"(scripts/migrate_tenant_fs.py).", file=sys.stderr)
        return 1

    size_mb = 0
    if root.exists():
        size_mb = sum(f.stat().st_size for f in root.rglob("*") if f.is_file()) // (1 << 20)

    print("=" * 70)
    print(f"  НАЗАВЖДИ видалити «{row['name']}» ({slug})")
    print(f"    архівовано:  {row['deleted_at']:%Y-%m-%d}")
    print(f"    артефакти:   {root} ({size_mb} МБ)")
    print(f"    Qdrant:      t_{slug}_*")
    print(f"    база:        tenant #{row['id']} з усіма рядками")
    print("=" * 70)
    # Typed, not y/n. The answer to a y/n prompt is muscle memory by the second
    # time; the slug has to be read off the screen above.
    if input(f"Введіть «{slug}» для підтвердження: ").strip() != slug:
        print("Скасовано.")
        return 1

    # 1. Vectors — rebuildable, so they go first.
    try:
        from qdrant_client import QdrantClient

        from church_assistant.shared.config import get_settings

        client = QdrantClient(url=get_settings().qdrant_url)
        # The names this tenant's own code writes to, asked of the module that
        # builds them — not a prefix guessed here that could drift from it or
        # match a neighbour whose slug starts the same way.
        wanted = set(collections.all_collections(slug).values())
        live = {c.name for c in client.get_collections().collections}
        dropped = sorted(wanted & live)
        for name in dropped:
            client.delete_collection(name)
        print(f"  ✓ Qdrant: {len(dropped)} колекц. {dropped or ''}")
    except Exception as e:
        # Not fatal: leftover collections are inert and findable by name, and
        # stopping here would leave the church half-removed.
        names = ", ".join(sorted(collections.all_collections(slug).values()))
        print(f"  ⚠ Qdrant не прибрано ({e}) — приберіть вручну: {names}")

    # 2. Artifacts.
    if root.exists():
        shutil.rmtree(root)
        print(f"  ✓ артефакти: {root}")
    else:
        print(f"  · артефактів не було: {root}")

    # 3. The tenant row, and everything that cascades off it.
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM tenants WHERE id = %s AND deleted_at IS NOT NULL",
                (row["id"],),
            )
    print(f"  ✓ база: tenant #{row['id']}")

    await _logger.warn(
        "tenant.purged",
        message=f"purged archived church {slug!r} (#{row['id']}, {size_mb} MB)",
    )
    print(f"\n«{row['name']}» видалено назавжди.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--purge", metavar="SLUG",
                   help="видалити назавжди цю церкву (лише архівовану)")
    p.add_argument("--force", action="store_true",
                   help="видалити до кінця терміну (на прохання самої церкви)")
    a = p.parse_args()

    async def run() -> int:
        try:
            return await (_purge(a.purge, a.force) if a.purge else _list())
        finally:
            await close_pool()

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
