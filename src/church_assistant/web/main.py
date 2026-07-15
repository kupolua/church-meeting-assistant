"""
Church Meeting Assistant — Web UI entry point.

Serves at http://localhost:8000/ (localhost-only, single user).

Run with:
    uv run uvicorn church_assistant.web.main:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from church_assistant.db.connection import close_pool, get_pool


# ─────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


# ─────────────────────────────────────────────────────────────
# Templates
# ─────────────────────────────────────────────────────────────

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ─────────────────────────────────────────────────────────────
# Lifespan
# ─────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await get_pool()
    yield
    await close_pool()


# ─────────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────────

app = FastAPI(
    title="Church Meeting Assistant",
    description="Personal RAG interface for pastoral council meeting protocols",
    version="0.1.0",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ─────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────

from church_assistant.web.routes import (  # noqa: E402
    home,
    meetings,
    query,
    search,
    history,
)

app.include_router(home.router)
app.include_router(meetings.router)
app.include_router(query.router)
app.include_router(search.router)
app.include_router(history.router)
