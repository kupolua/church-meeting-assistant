"""
What the web says about a church whose own data it cannot read.

WHY THIS EXISTS AT ALL. cloud_plan §2.1 puts the key on the church's devices
and nowhere else, and §3.1 seals the columns and the artifacts that follow from
that. The VPS therefore serves a church whose protocols it holds and cannot
open — by design, and permanently. Web is not broken for that church; it is
correct and half-blind, and the page has to say so.

THREE FAILURES, ONE SENTENCE. A sealed folder (only `*.enc` on disk), a sealed
column that will not open (`field_crypto.NotDecryptable`) and a write that has
no key to seal with (`keyring.NoProcessorKey`) are three different mechanisms
and one fact for the reader: this content lives in the app. Spelling that fact
three ways would invite three half-answers.

A HANDLER AND NOT EIGHT try/excepts. Every write route in `meetings.py` reaches
protocols_repo, and each would need the same three lines — which means the next
route added needs them too, and will not have them, and will show a stack trace
to a pastor. The handlers below are the floor: they catch what no route thought
about. Where a page can do better than the floor — the meeting page still shows
its date, its number and its neighbours around the notice — it handles the
exception itself and never reaches here.

⚠️ NOT A SECURITY BOUNDARY. Nothing here withholds anything: the server could
not show this content if it wanted to. This is the message, not the lock.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from church_assistant.ingestion.artifact_crypto import ENC_SUFFIX  # noqa: F401  (re-exported)
from church_assistant.shared.field_crypto import NotDecryptable
from church_assistant.shared.keyring import NoProcessorKey


READ_MESSAGE = (
    "Цей вміст зашифрований ключем церкви. Сервер його не читає — "
    "відкрийте зустріч у застосунку."
)
WRITE_MESSAGE = (
    "Цей запис зашифрований ключем церкви. Сервер не може його змінити — "
    "редагуйте у застосунку."
)


def folder_is_sealed(folder: Path) -> bool:
    """True when a meeting folder holds ciphertext instead of artifacts.

    Checked by the presence of `.enc` rather than the absence of `polished.md`:
    a folder that is merely empty (audio uploaded, pipeline not run yet) is a
    normal, temporary state and must not be reported as encrypted.
    """
    if not folder.is_dir():
        return False
    return any(p.suffix == ENC_SUFFIX for p in folder.iterdir() if p.is_file())


def _page(request: Request, message: str, status: int) -> HTMLResponse:
    from church_assistant.web.main import templates
    return templates.TemplateResponse(
        request, "encrypted.html", {"message": message}, status_code=status,
    )


def register(app: FastAPI) -> None:
    """Install the two handlers. Called once, from main."""

    async def _handle(request: Request, message: str, status: int) -> HTMLResponse:
        return _page(request, message, status)

    async def _cannot_read(request: Request, exc: Exception) -> HTMLResponse:
        # A GET answered with this page is not a failure: the server did what
        # was asked and this IS the answer, so 200. The same exception raised
        # while SERVING A FORM POST is a different story — something the person
        # asked for did not happen, and a 200 would let a form report success
        # and lose their edit. Hence the method, not the exception, decides.
        if request.method == "GET":
            return await _handle(request, READ_MESSAGE, 200)
        return await _handle(request, WRITE_MESSAGE, 409)

    async def _cannot_write(request: Request, exc: Exception) -> HTMLResponse:
        return await _handle(request, WRITE_MESSAGE, 409)

    app.add_exception_handler(NotDecryptable, _cannot_read)
    app.add_exception_handler(NoProcessorKey, _cannot_write)
