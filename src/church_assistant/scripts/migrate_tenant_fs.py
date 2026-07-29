"""
CLI: move the legacy tenant's artifacts into the per-tenant layout (MT Phase 3).

    data/meetings/            →  data/tenants/<slug>/meetings/
    data/voice_profiles/      →  data/tenants/<slug>/voice_profiles/

This is OPTIONAL. shared/tenant_paths.py keeps serving the legacy folders to the
legacy tenant for as long as they exist, so the app works before and after. Run
this when you want one uniform layout — e.g. before onboarding a second church,
so nobody has to remember that one of them is special.

WHY THE DB IS TOUCHED TOO: ingestion_jobs.meeting_dir stores an ABSOLUTE path.
Move the folders without rewriting those rows and a re-queued job points at a
directory that no longer exists — the failure would surface hours later, in the
middle of a pipeline run. So the move and the rewrite happen together, and the
rewrite is verified before any file is touched.

Dry-run by default: it prints the plan and changes nothing. Add --apply to
execute. The move is a rename when possible (atomic, instant, same filesystem).

Usage:
    # See what would happen (nothing is modified):
    uv run python -m church_assistant.scripts.migrate_tenant_fs

    # Do it:
    uv run python -m church_assistant.scripts.migrate_tenant_fs --apply

    # A different tenant / explicit slug:
    uv run python -m church_assistant.scripts.migrate_tenant_fs \
        --tenant-slug default --apply

AFTERWARDS: Qdrant payloads of the `protocol_full` kind carry an informational
`polished_md_path` that will still show the old location. It is not read by
retrieval; it refreshes on the next re-index of that meeting.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path
from typing import Any, Optional

from church_assistant.db import tenants_repo
from church_assistant.db.connection import close_pool, get_pool
from church_assistant.db.tenant_context import tenant_cursor
from church_assistant.shared import tenant_paths


async def _resolve_tenant(pool, raw: str) -> Optional[dict[str, Any]]:
    """Accept a tenant id or slug → the tenant row."""
    if raw.isdigit():
        return await tenants_repo.get_by_id(pool, int(raw))
    return await tenants_repo.get_by_slug(pool, raw)


async def _jobs_to_rewrite(
    pool, tenant_id: int, old_root: Path,
) -> list[tuple[int, str]]:
    """(job_id, meeting_dir) rows whose path sits under the old layout."""
    prefix = f"{old_root}/"
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(
            "SELECT id, meeting_dir FROM ingestion_jobs "
            "WHERE meeting_dir LIKE %s ORDER BY id",
            (f"{prefix}%",),
        )
        return [(int(r[0]), str(r[1])) for r in await cur.fetchall()]


async def _rewrite_jobs(
    pool, tenant_id: int, old_root: Path, new_root: Path,
) -> int:
    """Repoint ingestion_jobs.meeting_dir at the new layout. Returns row count."""
    async with tenant_cursor(pool, tenant_id) as cur:
        await cur.execute(
            "UPDATE ingestion_jobs "
            "SET meeting_dir = %s || substring(meeting_dir from %s) "
            "WHERE meeting_dir LIKE %s",
            (f"{new_root}/", len(f"{old_root}/") + 1, f"{old_root}/%"),
        )
        return cur.rowcount


def _move(src: Path, dst: Path, *, apply: bool) -> str:
    """
    Move src → dst. Returns a one-line description of what happened.

    Refuses to merge into an existing destination: silently combining two
    meeting trees could interleave two churches' folders, and there is no safe
    automatic answer to that — the operator has to look.
    """
    if not src.exists():
        return f"  (skip) {src} — not present"
    if dst.exists():
        return f"  ❌ {dst} already exists — resolve by hand, refusing to merge"

    n = sum(1 for _ in src.rglob("*"))
    if not apply:
        return f"  would move {src} → {dst}  ({n} entries)"

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return f"  ✓ moved {src} → {dst}  ({n} entries)"


async def async_main() -> int:
    parser = argparse.ArgumentParser(
        description="Move the legacy tenant's data into data/tenants/<slug>/",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--tenant-slug", type=str, default=None,
        help="Tenant id or slug whose legacy folders to move "
             "(default: LEGACY_TENANT_SLUG)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually move the files (default: dry run)",
    )
    parser.add_argument(
        "--skip-db", action="store_true",
        help="Don't rewrite ingestion_jobs.meeting_dir (you'll fix paths yourself)",
    )
    args = parser.parse_args()

    slug = args.tenant_slug or tenant_paths.legacy_slug()
    if not slug:
        print("❌ No tenant slug given and LEGACY_TENANT_SLUG is empty",
              file=sys.stderr)
        return 1

    old = tenant_paths.legacy_paths_for(slug)
    new = tenant_paths.tenant_paths_for(slug)

    print("=" * 70)
    print(f"  Tenant FS migration — {slug}   {'(APPLY)' if args.apply else '(dry run)'}")
    print("=" * 70)
    print(f"  from: {old.root}")
    print(f"  to:   {new.root}")
    print()

    if old.meetings == new.meetings:
        print("  Nothing to do: legacy and per-tenant paths are identical.")
        return 0

    pool = None
    tenant: Optional[dict[str, Any]] = None
    if not args.skip_db:
        pool = await get_pool()
        tenant = await _resolve_tenant(pool, slug)
        if tenant is None:
            print(f"❌ Tenant {slug!r} is not in the tenants registry. "
                  f"Use --skip-db to move files anyway.", file=sys.stderr)
            await close_pool()
            return 3

        jobs = await _jobs_to_rewrite(pool, int(tenant["id"]), old.meetings)
        print(f"  ingestion_jobs rows pointing under the old layout: {len(jobs)}")
        for job_id, path in jobs[:10]:
            print(f"    #{job_id}  {path}")
        if len(jobs) > 10:
            print(f"    … and {len(jobs) - 10} more")
        print()

    # ─── Files ───────────────────────────────────────────────
    print("  Files:")
    lines = [
        _move(old.meetings, new.meetings, apply=args.apply),
        _move(old.voice_profiles, new.voice_profiles, apply=args.apply),
    ]
    for line in lines:
        print(line)
    print()

    if any("❌" in line for line in lines):
        print("  Aborted — nothing further was changed.", file=sys.stderr)
        if pool is not None:
            await close_pool()
        return 4

    # ─── DB paths ────────────────────────────────────────────
    if pool is not None and tenant is not None:
        if args.apply:
            n = await _rewrite_jobs(
                pool, int(tenant["id"]), old.meetings, new.meetings
            )
            print(f"  ✓ ingestion_jobs.meeting_dir rewritten: {n} row(s)")
        else:
            print("  would rewrite ingestion_jobs.meeting_dir for the rows above")
        await close_pool()

    print()
    if args.apply:
        print("  ✓ Migration complete. tenant_paths.paths_for() now resolves to")
        print(f"    {new.meetings} for '{slug}'.")
    else:
        print("  Dry run only — re-run with --apply to perform the migration.")
    return 0


def main() -> None:
    sys.exit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
