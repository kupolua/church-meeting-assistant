"""
CLI: manage web UI accounts (MT Phase 3).

The web login is what maps a person to a church, so this is also how you hand a
new church its first way in. `--tenant` accepts either the numeric id or the
slug, since after the first church you'll be thinking in slugs.

Usage:
    # Create the first account for the default church (prompts for a password):
    uv run python -m church_assistant.scripts.add_web_user \
        --tenant 1 --username pavlo --name "Павло Кулаковський" --role admin

    # Non-interactive (avoid: the password lands in your shell history):
    uv run python -m church_assistant.scripts.add_web_user \
        --tenant first-baptist --username roman --name "Роман В." \
        --password 'correct horse battery staple'

    # List / disable / re-enable / change password:
    uv run python -m church_assistant.scripts.add_web_user --tenant 1 --list
    uv run python -m church_assistant.scripts.add_web_user --tenant 1 \
        --deactivate --username roman
    uv run python -m church_assistant.scripts.add_web_user --tenant 1 \
        --reactivate --username roman
    uv run python -m church_assistant.scripts.add_web_user --tenant 1 \
        --set-password --username roman
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from typing import Optional

from church_assistant.db import tenants_repo, web_users_repo
from church_assistant.db.connection import close_pool, get_pool
from church_assistant.web import security


MIN_PASSWORD_LEN = 8


async def _resolve_tenant(pool, raw: str) -> Optional[dict]:
    """Accept a tenant id or slug → the tenant row (None if no such church)."""
    if raw.isdigit():
        return await tenants_repo.get_by_id(pool, int(raw))
    return await tenants_repo.get_by_slug(pool, raw)


def _read_password(args: argparse.Namespace) -> Optional[str]:
    """Password from --password, else prompted twice. None → abort."""
    if args.password:
        pw = args.password
    else:
        pw = getpass.getpass("Пароль: ")
        if pw != getpass.getpass("Повторіть пароль: "):
            print("❌ Паролі не збігаються", file=sys.stderr)
            return None
    if len(pw) < MIN_PASSWORD_LEN:
        print(f"❌ Пароль закороткий (мінімум {MIN_PASSWORD_LEN} символів)",
              file=sys.stderr)
        return None
    return pw


async def cmd_add(pool, tenant: dict, args: argparse.Namespace) -> int:
    pw = _read_password(args)
    if pw is None:
        return 2
    try:
        user_id = await web_users_repo.add_web_user(
            pool,
            tenant["id"],
            username=args.username,
            password_hash=security.hash_password(pw),
            full_name=args.name,
            role=args.role,
            notes=args.notes,
        )
    except web_users_repo.WebUserAlreadyExists as e:
        print(f"❌ {e}", file=sys.stderr)
        print("   Логіни глобально унікальні — одна людина належить одній церкві.",
              file=sys.stderr)
        return 2
    except ValueError as e:
        print(f"❌ Невірні дані: {e}", file=sys.stderr)
        return 2

    user = await web_users_repo.get_by_id(pool, tenant["id"], user_id)
    assert user is not None
    print("✓ Веб-акаунт створено:")
    print(f"    id         = {user['id']}")
    print(f"    tenant     = {tenant['id']} ({tenant['slug']} — {tenant['name']})")
    print(f"    username   = {user['username']}")
    print(f"    full_name  = {user['full_name']}")
    print(f"    role       = {user['role']}")
    return 0


async def cmd_list(pool, tenant: dict, args: argparse.Namespace) -> int:
    users = await web_users_repo.list_active(pool, tenant["id"])
    if not users:
        print(f"(у церкви {tenant['slug']} немає активних веб-акаунтів)")
        return 0
    print(f"Активні веб-акаунти церкви {tenant['slug']} ({len(users)}):")
    print()
    print(f"  {'ID':<5} {'Логін':<20} {'Роль':<8} {'Імʼя':<30} Останній вхід")
    print(f"  {'-'*5} {'-'*20} {'-'*8} {'-'*30} {'-'*20}")
    for u in users:
        last = u["last_login_at"].strftime("%Y-%m-%d %H:%M") if u["last_login_at"] else "—"
        print(f"  {u['id']:<5} {u['username']:<20} {u['role']:<8} "
              f"{u['full_name']:<30} {last}")
    return 0


async def _find_by_username(pool, tenant: dict, username: str) -> Optional[dict]:
    user = await web_users_repo.get_by_username(pool, tenant["id"], username)
    if user is None:
        print(f"❌ У церкві {tenant['slug']} немає акаунта {username!r}", file=sys.stderr)
    return user


async def cmd_deactivate(pool, tenant: dict, args: argparse.Namespace) -> int:
    user = await _find_by_username(pool, tenant, args.username)
    if user is None:
        return 3
    await web_users_repo.deactivate(pool, tenant["id"], user["id"])
    print(f"✓ Акаунт {args.username} деактивовано (сесія діє до закінчення TTL)")
    return 0


async def cmd_reactivate(pool, tenant: dict, args: argparse.Namespace) -> int:
    user = await _find_by_username(pool, tenant, args.username)
    if user is None:
        return 3
    await web_users_repo.reactivate(pool, tenant["id"], user["id"])
    print(f"✓ Акаунт {args.username} активовано")
    return 0


async def cmd_set_password(pool, tenant: dict, args: argparse.Namespace) -> int:
    user = await _find_by_username(pool, tenant, args.username)
    if user is None:
        return 3
    pw = _read_password(args)
    if pw is None:
        return 2
    await web_users_repo.set_password_hash(
        pool, tenant["id"], user["id"], security.hash_password(pw)
    )
    print(f"✓ Пароль для {args.username} змінено")
    return 0


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Manage web UI accounts for Church Meeting Assistant",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--list", action="store_true",
                      help="List this tenant's active web accounts and exit")
    mode.add_argument("--deactivate", action="store_true",
                      help="Disable the account with --username")
    mode.add_argument("--reactivate", action="store_true",
                      help="Re-enable the account with --username")
    mode.add_argument("--set-password", action="store_true",
                      help="Change the password of the account with --username")

    p.add_argument("--tenant", type=str, required=True,
                   help="Tenant id or slug (the church this account belongs to)")
    p.add_argument("--username", type=str,
                   help="Login name (lowercased; globally unique)")
    p.add_argument("--name", type=str, help='Full name (e.g. "Павло Кулаковський")')
    p.add_argument("--role", choices=list(web_users_repo.ROLES), default="member",
                   help="member (default) or admin")
    p.add_argument("--password", type=str, default=None,
                   help="Password (omit to be prompted — preferred)")
    p.add_argument("--notes", type=str, default=None, help="Free-form notes")

    return p


def validate_args(args: argparse.Namespace) -> Optional[str]:
    if args.list:
        return None
    if args.deactivate or args.reactivate or args.set_password:
        if not args.username:
            return "--deactivate/--reactivate/--set-password requires --username"
        return None
    if not args.username:
        return "Creating an account requires --username"
    if not args.name:
        return "Creating an account requires --name"
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

    pool = await get_pool()
    try:
        tenant = await _resolve_tenant(pool, args.tenant)
        if tenant is None:
            print(f"❌ Церкву {args.tenant!r} не знайдено в реєстрі tenants",
                  file=sys.stderr)
            return 3

        if args.list:
            return await cmd_list(pool, tenant, args)
        if args.deactivate:
            return await cmd_deactivate(pool, tenant, args)
        if args.reactivate:
            return await cmd_reactivate(pool, tenant, args)
        if args.set_password:
            return await cmd_set_password(pool, tenant, args)
        return await cmd_add(pool, tenant, args)
    finally:
        await close_pool()


def main() -> None:
    sys.exit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
