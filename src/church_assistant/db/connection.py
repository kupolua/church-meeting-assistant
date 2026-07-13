"""
Async PostgreSQL connection pool for Church Meeting Assistant.

Uses psycopg v3 with connection pooling for FastAPI, Telegram bot,
and background worker.

Usage:
    from church_assistant.db.connection import get_pool, close_pool

    # In app startup:
    pool = await get_pool()

    # In request handler:
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT ...")
            row = await cur.fetchone()

    # In app shutdown:
    await close_pool()

Configuration via .env:
    DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD

Pool sizing (defaults):
    min_size = 2      # Always-open connections
    max_size = 10     # Max simultaneous connections
    max_idle = 300s   # Close idle beyond this
"""

import os
from typing import Optional

from dotenv import load_dotenv
from psycopg_pool import AsyncConnectionPool


# ─────────────────────────────────────────────────────────────
# Module state (singleton pool)
# ─────────────────────────────────────────────────────────────

_pool: Optional[AsyncConnectionPool] = None


# ─────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────

def _build_conninfo() -> str:
    """Build PostgreSQL connection string from .env variables."""
    load_dotenv()

    host = os.getenv("DB_HOST", "127.0.0.1")
    port = os.getenv("DB_PORT", "5433")
    dbname = os.getenv("DB_NAME", "cma")
    user = os.getenv("DB_USER", "cma")
    password = os.getenv("DB_PASSWORD")

    if not password:
        raise RuntimeError(
            "DB_PASSWORD is not set. Copy .env.example → .env and fill it in."
        )

    # libpq keyword=value format (safe for special chars in password)
    return (
        f"host={host} port={port} dbname={dbname} "
        f"user={user} password={password}"
    )


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────

async def get_pool() -> AsyncConnectionPool:
    """
    Get or create the global connection pool.

    First call opens the pool (may take ~100ms).
    Subsequent calls return the same instance.

    Raises:
        RuntimeError: if DB_PASSWORD is missing.
        psycopg.OperationalError: if PostgreSQL is unreachable.
    """
    global _pool

    if _pool is None:
        conninfo = _build_conninfo()
        _pool = AsyncConnectionPool(
            conninfo=conninfo,
            min_size=2,
            max_size=10,
            max_idle=300,       # seconds
            timeout=10,         # wait for connection (seconds)
            open=False,         # explicit open below (avoids deprecation warning)
        )
        await _pool.open()
        await _pool.wait()      # ensure min_size connections are ready

    return _pool


async def close_pool() -> None:
    """
    Close the global pool. Called on app shutdown.

    Safe to call multiple times (idempotent).
    """
    global _pool

    if _pool is not None:
        await _pool.close()
        _pool = None


async def check_connection() -> bool:
    """
    Test that the pool can execute a trivial query.

    Returns True if healthy, False otherwise (does not raise).
    """
    try:
        pool = await get_pool()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1")
                row = await cur.fetchone()
                return row is not None and row[0] == 1
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────
# CLI smoke test (uv run python -m church_assistant.db.connection)
# ─────────────────────────────────────────────────────────────

async def _smoke_test() -> None:
    """Run a basic connectivity check."""
    print("Building conninfo...")
    conninfo = _build_conninfo()
    # Mask password in output
    safe = conninfo.replace(
        f"password={os.getenv('DB_PASSWORD')}", "password=***"
    )
    print(f"  {safe}")
    print()

    print("Opening pool...")
    pool = await get_pool()
    print(f"  ✓ Pool opened (min={pool.min_size}, max={pool.max_size})")
    print()

    print("Executing SELECT 1...")
    ok = await check_connection()
    print(f"  {'✓' if ok else '✗'} check_connection() = {ok}")
    print()

    print("Executing SELECT version()...")
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT version()")
            row = await cur.fetchone()
            if row:
                print(f"  {row[0]}")
    print()

    print("Executing SELECT * FROM v_queue_depth...")
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT * FROM v_queue_depth")
            row = await cur.fetchone()
            if row:
                print(f"  pending={row[0]}, processing={row[1]}, failed={row[2]}")
    print()

    print("Closing pool...")
    await close_pool()
    print("  ✓ Closed")


if __name__ == "__main__":
    import asyncio
    asyncio.run(_smoke_test())
