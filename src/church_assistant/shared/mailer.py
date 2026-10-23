"""
Outgoing email — the first and only place this system talks to a mailbox.

There was deliberately no email here for a year: the web signs people in with
a name and a password, and access travels as a link handed over by a person.
The app changes that. A phone proves who holds an address by receiving a code,
and the operator learns about a new church from a message rather than from a
glance at a panel.

Plain SMTP with STARTTLS, stdlib only, in a worker thread so the event loop
does not wait on a mail server. Configured by five variables:

    SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASSWORD, SMTP_FROM

With SMTP_HOST unset nothing is sent and send() returns False. The caller
decides what that means — the sign-in route, for one, then shows the code in
the server log so a developer can finish the flow without a provider.
"""

from __future__ import annotations

import asyncio
import os
import smtplib
import sys
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from dotenv import load_dotenv

load_dotenv()

SMTP_HOST = os.getenv("SMTP_HOST", "").strip()
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "").strip()
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", "").strip() or SMTP_USER
# Where the operator reads about new churches. Optional: without it the panel
# is the only place they appear.
PLATFORM_NOTIFY_EMAIL = os.getenv("PLATFORM_NOTIFY_EMAIL", "").strip()

SEND_TIMEOUT_SECONDS = 20


def configured() -> bool:
    return bool(SMTP_HOST and SMTP_FROM)


def _send_sync(to: str, subject: str, text: str) -> None:
    msg = EmailMessage()
    msg["From"] = SMTP_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg.set_content(text)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SEND_TIMEOUT_SECONDS) as s:
        s.ehlo()
        if s.has_extn("starttls"):
            s.starttls()
            s.ehlo()
        if SMTP_USER:
            s.login(SMTP_USER, SMTP_PASSWORD)
        s.send_message(msg)


async def send(to: str, subject: str, text: str) -> bool:
    """
    Deliver one plain-text message. True if the server accepted it.

    Never raises: a mail server that is down must not take a sign-in route down
    with it. The failure is printed to stderr (journal on the VPS) because this
    module sits below the logger and must not import it.
    """
    if not configured():
        return False
    try:
        await asyncio.to_thread(_send_sync, to, subject, text)
        return True
    except Exception as e:  # noqa: BLE001 — anything from DNS to auth
        print(f"[mailer] send to {to!r} failed: {type(e).__name__}: {e}",
              file=sys.stderr, flush=True)
        return False
