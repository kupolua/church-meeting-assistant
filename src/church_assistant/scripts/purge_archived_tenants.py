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
    3. database rows        ON DELETE CASCADE from tenants (migration 016)
Vectors first because they are the only part that can be regenerated; if the run
dies midway the church is still restorable from what is left. The tenant row goes
last, so an interrupted purge leaves a church that still appears in the archive
rather than orphaned files nobody can attribute.

⚠️ THE CASCADE ONLY EXISTS SINCE 016. Before it, every one of the nine foreign
keys into tenants was NO ACTION and step 3 died on the first audit_log row — so
this script had never once run to the end, for any church. Against a database
older than 016 it still cannot; that is now the loud failure it should always
have been.

PROTECTED, CHECKED BEFORE ANYTHING IS TOUCHED (see _protected_reason):
tenant 0 is the platform, tenant 1 is the founding corpus, and LEGACY_TENANT_SLUG
names the same church by another route. Migration 016 refuses all three in the
database too, but that backstop arrives at step 3 — after the vectors and the
recordings are already gone. The check that matters is the one up here.

⚠️ The refusal this file used to rely on — "is the artifact folder the shared
data root?" — was true only while `default` lived directly in data/. It moved to
data/tenants/default/ on 25.08 and the condition has been false, and therefore
unreachable, ever since. It is kept below for setups that never migrated, but it
is no longer what protects anything.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
from datetime import datetime, timezone

from church_assistant.db import tenants_repo
from church_assistant.db.connection import close_pool, get_pool
from church_assistant.ingestion.config import get_artifact_sync_remote
from church_assistant.shared import collections, tenant_paths
from church_assistant.shared.logger import Logger


_logger = Logger(process="purge")


def _days_left(purge_after: datetime) -> int:
    return (purge_after - datetime.now(timezone.utc)).days


def _protected_reason(tenant_id: int, slug: str) -> str | None:
    """
    Why this church must not be purged — or None if it may be.

    Identity, not filesystem shape. The old refusal asked where a church's
    artifacts happened to sit, which stopped being true about `default` the day
    the folders moved and took the protection with it, quietly. A tenant id
    cannot move.

    Migration 016 says the same three things in a BEFORE DELETE trigger. This is
    not a duplicate of it but the half that arrives in time: the trigger fires at
    step 3, and by then the vectors and the recordings are already deleted.
    """
    if int(tenant_id) == 0:
        return "це платформа (_system), а не церква"
    if int(tenant_id) == 1:
        return ("це засновницький корпус — уся історія до мультитенантності. "
                "Переносити його можна лише вручну, з бекапом на руках")
    legacy = os.getenv("LEGACY_TENANT_SLUG", "default").strip()
    if legacy and slug == legacy:
        return (f"це легасі-тенант (LEGACY_TENANT_SLUG={legacy!r}) — "
                f"той самий засновницький корпус, названий інакше")
    return None


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

    # BEFORE ANYTHING IS TOUCHED. Everything below this point destroys
    # something, and the database backstop in 016 only speaks at the very end.
    reason = _protected_reason(row["id"], slug)
    if reason is not None:
        print(f"✗ «{slug}» видаляти не можна: {reason}.", file=sys.stderr)
        return 1

    # A SECOND, NARROWER REFUSAL, for a setup that never migrated its folders:
    # there paths_for() still returns the SHARED data root, and the rmtree below
    # would take every church's artifacts with it. It no longer fires for
    # `default` (whose folder moved on 25.08) — _protected_reason above is what
    # catches that now — but it is exactly right for anyone still on the old
    # layout, and it costs one comparison.
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
    # Named before the slug is typed, not after: the operator is about to
    # confirm a permanent deletion, and what this run will NOT reach is part of
    # what they are agreeing to.
    _remote = get_artifact_sync_remote().rstrip("/")
    if _remote:
        print(f"    ⚠ на VPS:    {_remote}/tenants/{slug} — цей запуск не чіпає")
    print(f"    Qdrant:      t_{slug}_*")
    print(f"    база:        tenant #{row['id']} з усіма рядками")
    print("=" * 70)
    # Typed, not y/n. The answer to a y/n prompt is muscle memory by the second
    # time; the slug has to be read off the screen above.
    if input(f"Введіть «{slug}» для підтвердження: ").strip() != slug:
        print("Скасовано.")
        return 1

    # 1. Vectors — rebuildable, so they go first.
    #
    # ⚠️ THE IMPORT IS OUTSIDE THE try, and that is the whole lesson of this
    # step. It used to be inside, next to `from church_assistant.shared.config
    # import get_settings` — a module that has never existed. The except below
    # is written for a Qdrant that is briefly unreachable, and it dutifully
    # reported the ImportError in the same tone: "приберіть вручну". A service
    # that is down means "try later"; code that cannot be imported means this
    # script has never worked, and the two must not print the same sentence.
    from qdrant_client import QdrantClient

    qdrant_url = os.getenv("QDRANT_URL", "http://localhost:6333")
    try:
        client = QdrantClient(url=qdrant_url)
        # The names this tenant's own code writes to, asked of the module that
        # builds them — not a prefix guessed here that could drift from it or
        # match a neighbour whose slug starts the same way.
        wanted = set(collections.all_collections(slug).values())
        live = {c.name for c in client.get_collections().collections}
        dropped = sorted(wanted & live)
        for name in dropped:
            client.delete_collection(name)
        print(f"  ✓ Qdrant: {len(dropped)} колекц. {dropped or ''}")
        qdrant_left = []
    except Exception as e:
        # Not fatal: leftover collections are inert and findable by name, and
        # stopping here would leave the church half-removed.
        qdrant_left = sorted(collections.all_collections(slug).values())
        print(f"  ⚠ Qdrant не прибрано ({e} @ {qdrant_url}) — "
              f"приберіть вручну: {', '.join(qdrant_left)}")

    # 2. Artifacts — the ones on THIS machine.
    if root.exists():
        shutil.rmtree(root)
        print(f"  ✓ артефакти: {root}")
    else:
        print(f"  · артефактів не було: {root}")

    # ⚠️ SINCE THE PLANES SPLIT (24.08) THERE ARE TWO COPIES. This script deletes
    # DATA_ROOT/tenants/<slug> on whichever machine it runs, and the control
    # plane keeps its own mirror at <ARTIFACT_SYNC_REMOTE>/tenants/<slug>. Run
    # from the M1, the recordings on the VPS survive a purge that announced
    # itself as permanent — which is the one thing this script must never say
    # untruthfully.
    #
    # It is reported, not deleted: an `ssh … rm -rf` fired from a retention job
    # is a remote destructive path that would exist for the rest of the
    # project's life to save one line of typing, and vps_deploy.md keeps
    # destruction on that machine in human hands on purpose.
    remote_left = ""
    remote = get_artifact_sync_remote().rstrip("/")
    if remote:
        remote_left = f"{remote}/tenants/{slug}"
        print(f"  ⚠ віддалена копія НЕ чіпалась: {remote_left}")

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
        message=f"purged archived church {slug!r} (#{row['id']}, {size_mb} MB, "
                f"qdrant_left={len(qdrant_left)}, remote_left={bool(remote_left)})",
    )

    # "Назавжди" is a promise to a congregation, so it is only printed when it
    # is true. Anything still standing is named, with the command that finishes
    # the job, rather than folded into a success line nobody re-reads.
    if not qdrant_left and not remote_left:
        print(f"\n«{row['name']}» видалено назавжди.")
        return 0

    print(f"\n«{row['name']}» прибрано з бази, але НЕ повністю:")
    if qdrant_left:
        print(f"  · колекції Qdrant: {', '.join(qdrant_left)}")
    if remote_left:
        print(f"  · артефакти на контрольній площині:\n"
              f"      ssh {remote_left.split(':', 1)[0]} "
              f"'rm -rf {remote_left.split(':', 1)[-1]}'")
    return 2


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
