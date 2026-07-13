"""
Health checks for external services.

Used by:
    - Worker: periodic snapshots (every 60s) → health_checks table
    - Web UI: dashboard T1 widget
    - Bot: /stats admin command

Design:
    - Every check returns HealthResult (never raises).
    - Includes response_time_ms — useful for spotting slowness.
    - Timeouts intentionally short (5s each) — user is waiting.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import httpx


# ─────────────────────────────────────────────────────────────
# Config (from .env, with sensible defaults)
# ─────────────────────────────────────────────────────────────

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4:26b")

HEALTH_CHECK_TIMEOUT = 5.0   # seconds


# ─────────────────────────────────────────────────────────────
# Data types
# ─────────────────────────────────────────────────────────────

@dataclass
class HealthResult:
    """
    Result of one health check. Never raises — errors are captured as
    up=False + notes.
    """
    up: bool
    response_time_ms: Optional[int] = None
    notes: Optional[str] = None                # error message OR extra info


# ─────────────────────────────────────────────────────────────
# Ollama
# ─────────────────────────────────────────────────────────────

async def check_ollama(url: Optional[str] = None) -> HealthResult:
    """
    Ping Ollama /api/tags to verify:
        1. Ollama is reachable
        2. The configured model (OLLAMA_MODEL) is installed

    Does not send a generation request (that would take seconds).
    """
    ollama_url = url or OLLAMA_URL
    t = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=HEALTH_CHECK_TIMEOUT) as client:
            response = await client.get(f"{ollama_url}/api/tags")
            elapsed_ms = int((time.perf_counter() - t) * 1000)
            response.raise_for_status()
            data = response.json()
            models = data.get("models", [])
            model_names = {m.get("name", "") for m in models}

            if OLLAMA_MODEL not in model_names:
                return HealthResult(
                    up=False,
                    response_time_ms=elapsed_ms,
                    notes=(
                        f"Ollama up but model '{OLLAMA_MODEL}' not installed. "
                        f"Have: {sorted(model_names)}"
                    ),
                )

            return HealthResult(
                up=True,
                response_time_ms=elapsed_ms,
                notes=f"Model {OLLAMA_MODEL} available",
            )
    except httpx.TimeoutException:
        return HealthResult(
            up=False,
            response_time_ms=int((time.perf_counter() - t) * 1000),
            notes=f"Timeout after {HEALTH_CHECK_TIMEOUT}s",
        )
    except httpx.HTTPError as e:
        return HealthResult(up=False, notes=f"HTTP error: {e}")
    except Exception as e:
        return HealthResult(up=False, notes=f"Unexpected: {type(e).__name__}: {e}")


# ─────────────────────────────────────────────────────────────
# Qdrant
# ─────────────────────────────────────────────────────────────

async def check_qdrant(url: Optional[str] = None) -> HealthResult:
    """
    Ping Qdrant /collections to verify:
        1. Qdrant is reachable
        2. All 4 cma_* collections exist
    """
    qdrant_url = url or QDRANT_URL
    expected = {
        "cma_protocols",
        "cma_analyses",
        "cma_turns",
        "cma_protocol_full",
    }
    t = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=HEALTH_CHECK_TIMEOUT) as client:
            response = await client.get(f"{qdrant_url}/collections")
            elapsed_ms = int((time.perf_counter() - t) * 1000)
            response.raise_for_status()
            data = response.json()

            collections = {
                c.get("name") for c in data.get("result", {}).get("collections", [])
            }
            missing = expected - collections

            if missing:
                return HealthResult(
                    up=False,
                    response_time_ms=elapsed_ms,
                    notes=f"Qdrant up but missing collections: {sorted(missing)}",
                )

            return HealthResult(
                up=True,
                response_time_ms=elapsed_ms,
                notes=f"All 4 collections present",
            )
    except httpx.TimeoutException:
        return HealthResult(
            up=False,
            response_time_ms=int((time.perf_counter() - t) * 1000),
            notes=f"Timeout after {HEALTH_CHECK_TIMEOUT}s",
        )
    except httpx.HTTPError as e:
        return HealthResult(up=False, notes=f"HTTP error: {e}")
    except Exception as e:
        return HealthResult(up=False, notes=f"Unexpected: {type(e).__name__}: {e}")


# ─────────────────────────────────────────────────────────────
# PostgreSQL (via existing pool)
# ─────────────────────────────────────────────────────────────

async def check_postgres() -> HealthResult:
    """
    Test the DB pool by executing SELECT 1.

    Uses the shared connection pool (via db.connection).
    """
    t = time.perf_counter()
    try:
        # Import inside function to avoid circular deps at module load
        from church_assistant.db.connection import get_pool

        pool = await get_pool()
        async with pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1")
                row = await cur.fetchone()
                if row is None or row[0] != 1:
                    return HealthResult(
                        up=False,
                        notes="Unexpected SELECT 1 result",
                    )

        elapsed_ms = int((time.perf_counter() - t) * 1000)
        return HealthResult(
            up=True,
            response_time_ms=elapsed_ms,
        )
    except Exception as e:
        elapsed_ms = int((time.perf_counter() - t) * 1000)
        return HealthResult(
            up=False,
            response_time_ms=elapsed_ms,
            notes=f"{type(e).__name__}: {e}",
        )


# ─────────────────────────────────────────────────────────────
# Combined
# ─────────────────────────────────────────────────────────────

@dataclass
class SystemHealth:
    """All three services rolled up."""
    ollama: HealthResult
    qdrant: HealthResult
    postgres: HealthResult

    @property
    def all_up(self) -> bool:
        return self.ollama.up and self.qdrant.up and self.postgres.up

    @property
    def can_process_queries(self) -> bool:
        """Worker uses this to decide whether to fetch pending."""
        return self.ollama.up and self.qdrant.up


async def check_all() -> SystemHealth:
    """
    Check all three services in sequence (not parallel to keep logs orderly).
    """
    ollama = await check_ollama()
    qdrant = await check_qdrant()
    postgres = await check_postgres()
    return SystemHealth(ollama=ollama, qdrant=qdrant, postgres=postgres)


# ─────────────────────────────────────────────────────────────
# CLI smoke test
# ─────────────────────────────────────────────────────────────

async def _smoke_test() -> None:
    print("=" * 70)
    print("  health — smoke test")
    print("=" * 70)
    print()

    print("Checking all services...")
    print()

    result = await check_all()

    def _show(name: str, r: HealthResult) -> None:
        status = "✓ UP" if r.up else "✗ DOWN"
        rt = f"{r.response_time_ms}ms" if r.response_time_ms is not None else "-"
        print(f"  {name:<12} {status:<8} response={rt:<8} notes={r.notes}")

    _show("Ollama", result.ollama)
    _show("Qdrant", result.qdrant)
    _show("PostgreSQL", result.postgres)
    print()

    print(f"  all_up:              {result.all_up}")
    print(f"  can_process_queries: {result.can_process_queries}")

    # Clean up DB pool
    from church_assistant.db.connection import close_pool
    await close_pool()

    print()
    print("=" * 70)
    if result.all_up:
        print("  ✓ ALL SERVICES HEALTHY")
    else:
        print("  ⚠ Some services down — see notes above")
    print("=" * 70)


if __name__ == "__main__":
    import asyncio
    asyncio.run(_smoke_test())
