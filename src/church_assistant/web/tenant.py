"""
Web request → tenant resolution (MT Phase 1).

The web UI has no auth yet (single trusted operator), so every web request maps
to the DEFAULT tenant. This is the ONE place to change when per-user auth lands
(Phase 3): resolve the tenant from the session/user instead of the constant.
"""

from __future__ import annotations

import os

from fastapi import Request

DEFAULT_TENANT_ID = int(os.getenv("WEB_DEFAULT_TENANT_ID", "1"))


def current_tenant(request: Request) -> int:
    """The tenant this web request belongs to (constant until auth exists)."""
    return DEFAULT_TENANT_ID
