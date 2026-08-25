"""
Create a platform account and print its invitation link.

    uv run python -m church_assistant.scripts.add_platform_admin \\
        --username root --name "Павло Кулаковський" --base-url https://cma.rechurch.org.ua

CHICKEN AND EGG. Platform accounts are made from the platform panel, which needs
a platform account to reach. The first one has to come from outside the web, and
this is that outside — a script somebody with shell access runs once.

It still does not create a password. The account is inactive with a hash of
something nobody holds, and what comes back is a single-use link, exactly as for
a church's founding admin. Whoever runs this cannot sign in as the account they
just made, which is the point: an operator setting up an account for someone
else should not be able to become them.

The account lives in `_system` (tenant 0) — the platform, which is not a church.
A CHECK constraint added in migration 012 refuses is_platform_admin anywhere
else, so this cannot quietly create a church member with fleet powers.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from church_assistant.db import tenants_repo, web_invites_repo, web_users_repo
from church_assistant.db.connection import close_pool, get_pool
from church_assistant.web import security


SYSTEM_TENANT_ID = 0


async def _create(username: str, full_name: str, base_url: str) -> int:
    pool = await get_pool()
    try:
        system = await tenants_repo.get_by_id(pool, SYSTEM_TENANT_ID)
        if system is None:
            print("✗ немає тенанта `_system` — не застосована міграція 007",
                  file=sys.stderr)
            return 1

        try:
            user_id = await web_users_repo.add_web_user(
                pool,
                SYSTEM_TENANT_ID,
                username=username,
                password_hash=security.hash_password(security.new_session_token()),
                full_name=full_name,
                role="admin",
            )
        except web_users_repo.WebUserAlreadyExists:
            # Globally unique, and RLS hides other tenants' — so this is the
            # first moment the clash can be seen, wherever the other account is.
            print(f"✗ логін «{username}» уже зайнятий", file=sys.stderr)
            return 1

        # The flag and the deactivation are separate statements from the insert
        # because add_web_user knows nothing about the platform, and should not.
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT set_config('app.current_tenant', %s, true)",
                    (str(SYSTEM_TENANT_ID),),
                )
                await cur.execute(
                    "UPDATE web_users SET is_platform_admin = TRUE, is_active = FALSE "
                    "WHERE id = %s", (user_id,),
                )

        token = security.new_session_token()
        await web_invites_repo.create(
            pool,
            SYSTEM_TENANT_ID,
            web_user_id=user_id,
            token_hash=security.hash_token(token),
            created_by="script:add_platform_admin",
        )
        hours = web_invites_repo.DEFAULT_TTL_SECONDS // 3600

        print("=" * 70)
        print(f"  Платформовий акаунт «{username}» створено (id {user_id}).")
        print(f"  Пароля не існує — його задасть той, хто перейде за посиланням.")
        print()
        print(f"  {base_url.rstrip('/')}/invite/{token}")
        print()
        print(f"  Одноразове, діє {hours} год. У базі лише хеш — показане один раз.")
        print("=" * 70)
        return 0
    finally:
        await close_pool()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    p.add_argument("--username", required=True)
    p.add_argument("--name", required=True, help="Імʼя Прізвище")
    p.add_argument("--base-url", default="http://127.0.0.1:8000",
                   help="звідки будується посилання (https://cma.example.org)")
    a = p.parse_args()
    return asyncio.run(_create(a.username.strip().lower(), a.name.strip(), a.base_url))


if __name__ == "__main__":
    sys.exit(main())
