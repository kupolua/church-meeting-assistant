"""
Users repository: CRUD for the `users` table (Telegram whitelist).

Handles:
    - Add new user (admin or pastor role)
    - Fetch by Telegram user ID (auth check in bot middleware)
    - List active users (dashboard, /stats command)
    - Deactivate user (soft delete — keep audit trail)

Design:
    - Repository functions are stateless — take pool as parameter.
    - All functions return dicts (not ORM objects).
    - Soft delete (is_active=false) — never DELETE rows.
"""

from __future__ import annotations

from typing import Any, Optional

from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool


# ─────────────────────────────────────────────────────────────
# Types (documented shape of dicts)
# ─────────────────────────────────────────────────────────────
#
# User row = {
#     "id": int,
#     "telegram_user_id": int,
#     "telegram_username": str | None,
#     "full_name": str,
#     "role": "pastor" | "admin",
#     "is_active": bool,
#     "added_at": datetime,
#     "notes": str | None,
# }


# ─────────────────────────────────────────────────────────────
# Exceptions
# ─────────────────────────────────────────────────────────────

class UserAlreadyExists(Exception):
    """Raised when trying to insert a user with an existing telegram_user_id."""
    pass


class UserNotFound(Exception):
    """Raised when a lookup expects a user but none matches."""
    pass


# ─────────────────────────────────────────────────────────────
# CREATE
# ─────────────────────────────────────────────────────────────

async def add_user(
    pool: AsyncConnectionPool,
    *,
    telegram_user_id: int,
    full_name: str,
    role: str = "pastor",
    telegram_username: Optional[str] = None,
    notes: Optional[str] = None,
) -> int:
    """
    Add a new user to the whitelist.

    Args:
        telegram_user_id: numeric Telegram ID (from BotFather / message.from.id)
        full_name: display name ("Іван Іванов")
        role: 'pastor' (default) or 'admin'
        telegram_username: @handle without '@' (optional, can be NULL)
        notes: free-form comment (optional)

    Returns:
        Newly assigned user ID.

    Raises:
        ValueError: if role not in {'pastor', 'admin'} or telegram_user_id invalid
        UserAlreadyExists: if telegram_user_id already registered
    """
    if role not in ("pastor", "admin"):
        raise ValueError(f"Invalid role: {role!r} (expected 'pastor' or 'admin')")

    if telegram_user_id <= 0:
        raise ValueError(f"Invalid telegram_user_id: {telegram_user_id}")

    if not full_name.strip():
        raise ValueError("full_name cannot be empty")

    sql = """
        INSERT INTO users (
            telegram_user_id, telegram_username, full_name, role, notes
        ) VALUES (
            %s, %s, %s, %s, %s
        )
        RETURNING id
    """
    try:
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(sql, (
                    telegram_user_id, telegram_username,
                    full_name.strip(), role, notes,
                ))
                row = await cur.fetchone()
                if row is None:
                    raise RuntimeError("INSERT RETURNING did not return an id")
                return int(row[0])
    except UniqueViolation as e:
        raise UserAlreadyExists(
            f"User with telegram_user_id={telegram_user_id} already exists"
        ) from e


# ─────────────────────────────────────────────────────────────
# READ
# ─────────────────────────────────────────────────────────────

async def get_by_telegram_id(
    pool: AsyncConnectionPool,
    telegram_user_id: int,
) -> Optional[dict[str, Any]]:
    """
    Look up user by Telegram user ID.

    Returns None if not found (does not raise).

    Used by bot middleware to check authorization on every message.
    """
    sql = "SELECT * FROM users WHERE telegram_user_id = %s"
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (telegram_user_id,))
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_by_id(
    pool: AsyncConnectionPool,
    user_id: int,
) -> Optional[dict[str, Any]]:
    """Look up user by internal ID. Returns None if not found."""
    sql = "SELECT * FROM users WHERE id = %s"
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, (user_id,))
            row = await cur.fetchone()
            return dict(row) if row else None


async def is_authorized(
    pool: AsyncConnectionPool,
    telegram_user_id: int,
) -> bool:
    """
    Check if a Telegram user ID is on the active whitelist.

    True → user exists AND is_active=TRUE.
    False → not found OR is_active=FALSE.

    Fast check for bot middleware (no full row fetch).
    """
    sql = """
        SELECT 1 FROM users
        WHERE telegram_user_id = %s AND is_active = TRUE
        LIMIT 1
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (telegram_user_id,))
            row = await cur.fetchone()
            return row is not None


async def is_admin(
    pool: AsyncConnectionPool,
    telegram_user_id: int,
) -> bool:
    """
    Check if a Telegram user has admin role AND is active.

    Used for admin-only commands: /stats, /errors, etc.
    """
    sql = """
        SELECT 1 FROM users
        WHERE telegram_user_id = %s
          AND is_active = TRUE
          AND role = 'admin'
        LIMIT 1
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (telegram_user_id,))
            row = await cur.fetchone()
            return row is not None


async def list_active(
    pool: AsyncConnectionPool,
    *,
    role: Optional[str] = None,
) -> list[dict[str, Any]]:
    """
    List all active users, ordered by added_at ASC (oldest first).

    Args:
        role: optional filter — 'pastor' or 'admin'
    """
    if role is None:
        sql = """
            SELECT * FROM users
            WHERE is_active = TRUE
            ORDER BY added_at ASC
        """
        params: tuple = ()
    else:
        if role not in ("pastor", "admin"):
            raise ValueError(f"Invalid role: {role!r}")
        sql = """
            SELECT * FROM users
            WHERE is_active = TRUE AND role = %s
            ORDER BY added_at ASC
        """
        params = (role,)

    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(sql, params)
            rows = await cur.fetchall()
            return [dict(r) for r in rows]


async def count_active(pool: AsyncConnectionPool) -> int:
    """Count active users."""
    sql = "SELECT count(*) FROM users WHERE is_active = TRUE"
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql)
            row = await cur.fetchone()
            return int(row[0]) if row else 0


# ─────────────────────────────────────────────────────────────
# UPDATE
# ─────────────────────────────────────────────────────────────

async def deactivate(
    pool: AsyncConnectionPool,
    telegram_user_id: int,
) -> bool:
    """
    Soft-delete: set is_active=FALSE.

    Returns True if updated, False if user not found.

    Preserves audit trail — user's past queries still resolve.
    """
    sql = """
        UPDATE users
        SET is_active = FALSE
        WHERE telegram_user_id = %s
        RETURNING 1
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (telegram_user_id,))
            row = await cur.fetchone()
            return row is not None


async def reactivate(
    pool: AsyncConnectionPool,
    telegram_user_id: int,
) -> bool:
    """Set is_active=TRUE (undo deactivate). Returns True if updated."""
    sql = """
        UPDATE users
        SET is_active = TRUE
        WHERE telegram_user_id = %s
        RETURNING 1
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (telegram_user_id,))
            row = await cur.fetchone()
            return row is not None


async def update_notes(
    pool: AsyncConnectionPool,
    telegram_user_id: int,
    notes: Optional[str],
) -> bool:
    """Update the notes field. Returns True if updated."""
    sql = """
        UPDATE users
        SET notes = %s
        WHERE telegram_user_id = %s
        RETURNING 1
    """
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(sql, (notes, telegram_user_id))
            row = await cur.fetchone()
            return row is not None


# ─────────────────────────────────────────────────────────────
# CLI smoke test
# ─────────────────────────────────────────────────────────────

async def _smoke_test() -> None:
    """
    Test full lifecycle: add → check → list → deactivate → reactivate → cleanup.
    """
    from church_assistant.db.connection import get_pool, close_pool

    print("=" * 70)
    print("  users_repo — smoke test")
    print("=" * 70)
    print()

    pool = await get_pool()

    # Use a distinctive test ID (unlikely real Telegram ID)
    test_tg_id = 999_999_999_999

    # Cleanup any prior test data
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM users WHERE telegram_user_id = %s",
                (test_tg_id,)
            )

    # 1. Add
    print("1. Adding test user (pastor role)...")
    user_id = await add_user(
        pool,
        telegram_user_id=test_tg_id,
        full_name="[SMOKE TEST] Тестовий Пастор",
        telegram_username="test_smoke",
        role="pastor",
        notes="Automated smoke test",
    )
    print(f"   ✓ Added, id={user_id}")

    # 2. Read back
    print()
    print("2. Fetching by telegram_user_id...")
    user = await get_by_telegram_id(pool, test_tg_id)
    assert user is not None
    assert user["role"] == "pastor"
    assert user["is_active"] is True
    assert user["full_name"] == "[SMOKE TEST] Тестовий Пастор"
    print(f"   ✓ Name={user['full_name']}, role={user['role']}, active={user['is_active']}")

    # 3. is_authorized
    print()
    print("3. Checking is_authorized...")
    auth = await is_authorized(pool, test_tg_id)
    admin = await is_admin(pool, test_tg_id)
    print(f"   is_authorized = {auth}")
    print(f"   is_admin      = {admin}")
    assert auth is True
    assert admin is False

    # 4. Try duplicate insert
    print()
    print("4. Attempting duplicate insert (should raise)...")
    try:
        await add_user(
            pool,
            telegram_user_id=test_tg_id,
            full_name="Duplicate attempt",
        )
        raise AssertionError("Expected UserAlreadyExists, got no error")
    except UserAlreadyExists as e:
        print(f"   ✓ UserAlreadyExists raised: {e}")

    # 5. Deactivate
    print()
    print("5. Deactivating...")
    updated = await deactivate(pool, test_tg_id)
    assert updated is True
    auth_after = await is_authorized(pool, test_tg_id)
    print(f"   ✓ Deactivated, is_authorized = {auth_after}")
    assert auth_after is False

    # 6. Reactivate
    print()
    print("6. Reactivating...")
    updated = await reactivate(pool, test_tg_id)
    assert updated is True
    auth_final = await is_authorized(pool, test_tg_id)
    print(f"   ✓ Reactivated, is_authorized = {auth_final}")
    assert auth_final is True

    # 7. Count active (includes our test row)
    print()
    print("7. Counting active users...")
    n = await count_active(pool)
    print(f"   Active users total: {n}")

    # 8. Cleanup
    print()
    print("8. Cleanup — deleting test row...")
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM users WHERE telegram_user_id = %s",
                (test_tg_id,)
            )
    print(f"   ✓ Deleted test user telegram_user_id={test_tg_id}")

    await close_pool()

    print()
    print("=" * 70)
    print("  ✓ ALL USERS_REPO SMOKE TESTS PASSED")
    print("=" * 70)


if __name__ == "__main__":
    import asyncio
    asyncio.run(_smoke_test())
