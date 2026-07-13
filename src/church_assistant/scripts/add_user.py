"""
CLI: Add a user to the Telegram whitelist.

Usage:
    uv run python -m church_assistant.scripts.add_user \
        --telegram-id 123456789 \
        --name "Pavlo Kulakovskyi" \
        --role admin

    uv run python -m church_assistant.scripts.add_user \
        --telegram-id 987654321 \
        --name "Роман Вечерківський" \
        --username roman_v \
        --role pastor \
        --notes "Голова ради"

    # List existing users:
    uv run python -m church_assistant.scripts.add_user --list

    # Deactivate a user (soft delete):
    uv run python -m church_assistant.scripts.add_user \
        --deactivate --telegram-id 987654321
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from church_assistant.db.connection import get_pool, close_pool
from church_assistant.db import users_repo


async def cmd_add(args: argparse.Namespace) -> int:
    """Add a new user to whitelist."""
    pool = await get_pool()
    try:
        try:
            user_id = await users_repo.add_user(
                pool,
                telegram_user_id=args.telegram_id,
                full_name=args.name,
                role=args.role,
                telegram_username=args.username,
                notes=args.notes,
            )
        except users_repo.UserAlreadyExists as e:
            print(f"❌ {e}", file=sys.stderr)
            print(f"   Use --deactivate to remove or fetch existing entry.", file=sys.stderr)
            return 2
        except ValueError as e:
            print(f"❌ Invalid input: {e}", file=sys.stderr)
            return 2

        user = await users_repo.get_by_id(pool, user_id)
        assert user is not None

        print(f"✓ User added:")
        print(f"    id                 = {user['id']}")
        print(f"    telegram_user_id   = {user['telegram_user_id']}")
        print(f"    telegram_username  = @{user['telegram_username'] or '(none)'}")
        print(f"    full_name          = {user['full_name']}")
        print(f"    role               = {user['role']}")
        print(f"    is_active          = {user['is_active']}")
        print(f"    added_at           = {user['added_at']}")
        if user['notes']:
            print(f"    notes              = {user['notes']}")
        return 0
    finally:
        await close_pool()


async def cmd_list(args: argparse.Namespace) -> int:
    """List all active users."""
    pool = await get_pool()
    try:
        users = await users_repo.list_active(pool)

        if not users:
            print("(no active users)")
            return 0

        print(f"Active users ({len(users)}):")
        print()
        print(f"  {'ID':<5} {'TG ID':<15} {'Role':<8} {'Full name':<30} @username")
        print(f"  {'-'*5} {'-'*15} {'-'*8} {'-'*30} {'-'*20}")
        for u in users:
            uname = f"@{u['telegram_username']}" if u['telegram_username'] else "-"
            print(f"  {u['id']:<5} {u['telegram_user_id']:<15} "
                  f"{u['role']:<8} {u['full_name']:<30} {uname}")
        return 0
    finally:
        await close_pool()


async def cmd_deactivate(args: argparse.Namespace) -> int:
    """Deactivate a user (soft delete)."""
    pool = await get_pool()
    try:
        updated = await users_repo.deactivate(pool, args.telegram_id)
        if not updated:
            print(f"❌ No user found with telegram_user_id={args.telegram_id}",
                  file=sys.stderr)
            return 3
        print(f"✓ User telegram_user_id={args.telegram_id} deactivated")
        return 0
    finally:
        await close_pool()


async def cmd_reactivate(args: argparse.Namespace) -> int:
    """Re-activate a previously deactivated user."""
    pool = await get_pool()
    try:
        updated = await users_repo.reactivate(pool, args.telegram_id)
        if not updated:
            print(f"❌ No user found with telegram_user_id={args.telegram_id}",
                  file=sys.stderr)
            return 3
        print(f"✓ User telegram_user_id={args.telegram_id} reactivated")
        return 0
    finally:
        await close_pool()


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Manage Telegram whitelist for Church Meeting Assistant",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # Mutually exclusive: --list, --deactivate, --reactivate, or add (default)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--list", action="store_true",
        help="List all active users and exit",
    )
    mode.add_argument(
        "--deactivate", action="store_true",
        help="Deactivate the user with --telegram-id",
    )
    mode.add_argument(
        "--reactivate", action="store_true",
        help="Re-activate the user with --telegram-id",
    )

    # Common
    p.add_argument(
        "--telegram-id", type=int,
        help="Numeric Telegram user ID (from message.from.id)",
    )

    # Add-specific
    p.add_argument(
        "--name", type=str,
        help='Full name (e.g. "Pavlo Kulakovskyi")',
    )
    p.add_argument(
        "--username", type=str, default=None,
        help="Telegram @username without '@' (optional)",
    )
    p.add_argument(
        "--role", choices=["pastor", "admin"], default="pastor",
        help="Role: pastor (default) or admin",
    )
    p.add_argument(
        "--notes", type=str, default=None,
        help="Free-form notes (optional)",
    )

    return p


def validate_args(args: argparse.Namespace) -> Optional[str]:
    """Return None if valid, or error message string."""
    # --list needs nothing else
    if args.list:
        return None

    # --deactivate/--reactivate need --telegram-id
    if args.deactivate or args.reactivate:
        if args.telegram_id is None:
            return "--deactivate/--reactivate requires --telegram-id"
        return None

    # Default (add) needs --telegram-id AND --name
    if args.telegram_id is None:
        return "Adding a user requires --telegram-id"
    if not args.name:
        return "Adding a user requires --name"
    return None


async def async_main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    error = validate_args(args)
    if error:
        print(f"❌ {error}", file=sys.stderr)
        print(file=sys.stderr)
        parser.print_help(sys.stderr)
        return 1

    if args.list:
        return await cmd_list(args)
    if args.deactivate:
        return await cmd_deactivate(args)
    if args.reactivate:
        return await cmd_reactivate(args)
    return await cmd_add(args)


def main() -> None:
    # Import here to avoid unused-import warning at module top
    from typing import Optional  # noqa: F401
    exit_code = asyncio.run(async_main())
    sys.exit(exit_code)


# Fix forward reference for validate_args signature (import Optional in outer scope)
from typing import Optional  # noqa: E402


if __name__ == "__main__":
    main()
