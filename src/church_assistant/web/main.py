"""
Church Meeting Assistant — Web UI entry point.

Serves at http://localhost:8000/ (localhost-only, single user).

Run with:
    uv run uvicorn church_assistant.web.main:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from church_assistant.db.connection import close_pool, get_pool
from church_assistant.web.auth import AuthMiddleware
from church_assistant.web.headers import SecurityHeadersMiddleware
from church_assistant.web.security import check_session_config, get_secret_key


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
    # Fail fast rather than at the first login: without a secret key every
    # session cookie would be forgeable, and a forged cookie means a forged
    # tenant_id — i.e. one church reading another's protocols.
    get_secret_key()
    for problem in check_session_config():
        print(f"[config] WARNING: {problem}", file=sys.stderr, flush=True)
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

# Deny-by-default: every route except /login and /static needs a valid session,
# because a request without a session has no tenant and must not reach a repo.
app.add_middleware(AuthMiddleware)

# Added last → runs OUTERMOST, so the headers land on everything, including the
# redirects and 401s AuthMiddleware returns without calling a handler.
app.add_middleware(SecurityHeadersMiddleware)


# ─────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────

from church_assistant.web.routes import (  # noqa: E402
    account,
    admin,
    auth,
    home,
    meetings,
    query,
    search,
    history,
    dashboard,
    ingest,
)

app.include_router(auth.router)
app.include_router(account.router)
app.include_router(admin.router)
app.include_router(home.router)
app.include_router(meetings.router)
app.include_router(query.router)
app.include_router(search.router)
app.include_router(history.router)
app.include_router(dashboard.router)
app.include_router(ingest.router)
