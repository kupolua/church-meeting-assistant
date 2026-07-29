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
    Content-Security-Policy  see CSP below.
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

# Everything the UI needs is served from this origin (see static/VENDOR.md), so
# the policy can be strict with no escape hatches. What each directive buys:
#
#   default-src 'self'   the catch-all; anything not named below is same-origin
#   script-src 'self'    no inline handlers, no CDN, no eval. This is the one
#                        that matters: it means an injected <script> cannot run
#                        even if something else fails and gets HTML into a page
#                        holding an authenticated session.
#   style-src 'self'     no 'unsafe-inline' — which is only possible because the
#                        templates' <style> blocks and style= attributes moved
#                        into app.css. It stays honest only if they stay there.
#   img-src 'self' data: Pico.css inlines 14 SVG icons as data: URIs
#                        (background-image counts as img-src, not style-src).
#   connect-src 'self'   htmx's XHRs; nothing here talks to another origin.
#   frame-ancestors      the modern X-Frame-Options; both are sent because old
#                        browsers only understand the latter.
#   form-action 'self'   a login form that could POST elsewhere is a credential
#                        leak waiting for an HTML-injection bug.
#
# No 'unsafe-eval': htmx only needs it for the js: prefix on hx-vals/hx-headers
# and for hx-on, none of which this UI uses. If a future template reaches for
# them, the browser console will say so — prefer changing the template.
CSP_VALUE = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    "img-src 'self' data:",
    "font-src 'self'",
    "connect-src 'self'",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
])


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
        response.headers.setdefault("Content-Security-Policy", CSP_VALUE)

        if request.url.scheme == "https" and hsts_enabled():
            response.headers.setdefault("Strict-Transport-Security", HSTS_VALUE)

        return response


def cookie_secure(request: Request) -> bool:
    """Whether this request's session cookie should be marked Secure."""
    return security.cookie_secure_for(request.url.scheme)
