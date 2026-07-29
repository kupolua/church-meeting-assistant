"""
Baseline response hardening headers.

Small, boring, and worth having once the app is reachable by more than one
person on more than one machine. None of these replace TLS; they narrow what a
browser will do with our pages if something else goes wrong.

    X-Content-Type-Options   don't let the browser re-guess a response's type
                             (a protocol .md served as text must not become HTML)
    Referrer-Policy          meeting URLs contain dates; don't leak them to any
                             third party a user navigates to
    X-Frame-Options          nothing here should ever be framed — the whole UI is
                             one-click destructive actions behind hx-confirm
    Strict-Transport-Security  only over https, and only when the deployment says
                             it is really https: sending HSTS from a plain-HTTP
                             LAN box would pin browsers to a scheme it cannot
                             serve, locking users out until the header expires.

Content-Security-Policy is deliberately absent: the templates load htmx from a
CDN (base.html), so any honest policy would either allow arbitrary external
script or break the UI. Worth adding together with vendoring htmx locally.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from church_assistant.web import security


# One year, the usual value — long enough to matter, and only ever sent when the
# request itself arrived over https.
HSTS_VALUE = "max-age=31536000; includeSubDomains"


def hsts_enabled() -> bool:
    """
    Send HSTS at all? Off unless the deployment opts in.

    Tied to WEB_COOKIE_SECURE rather than a flag of its own: both answer the
    same question — "is this deployment really behind TLS?" — and two knobs that
    must agree are one knob too many.
    """
    load_dotenv()
    return os.getenv("WEB_COOKIE_SECURE", "auto").strip().lower() not in (
        "0", "false", "no", "off"
    )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Attach the baseline headers to every response, including error pages."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")

        if request.url.scheme == "https" and hsts_enabled():
            response.headers.setdefault("Strict-Transport-Security", HSTS_VALUE)

        return response


def cookie_secure(request: Request) -> bool:
    """Whether this request's session cookie should be marked Secure."""
    return security.cookie_secure_for(request.url.scheme)
