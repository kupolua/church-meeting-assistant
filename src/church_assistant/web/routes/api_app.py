"""
The app updates itself from here — no Google Play, no store account.

WHY: the council members install the APK by hand (decided 26.10.2026: a Play
account takes days of verification plus a 14-day closed test, and the app is
for a dozen people). Without this the admin would have to send every new APK
by messenger and everyone would tap through the installer; with it the phone
asks `/app/latest` on launch, shows a strip «Є версія X — завантажити», and
installs over itself. One tap.

HOW: the published builds are plain files in <DATA_ROOT>/app/ named exactly as
`build_apk.sh` names them — `cma-app-<major>.<minor>.<patch>+<build>.apk`.
The highest <build> (Android's versionCode) is the latest; nothing is stored
in the database and publishing is one `scp` (deploy/publish_apk.sh). Both
routes are unauthenticated: the version is not a secret and the check has to
work before sign-in, and the filename pattern is the only path allowed, so
nothing else under DATA_ROOT can be read through here.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from church_assistant.shared import tenant_paths

router = APIRouter(prefix="/api/v1/app")

APK_NAME = re.compile(r"^cma-app-(\d+)\.(\d+)\.(\d+)\+(\d+)\.apk$")
APK_MEDIA_TYPE = "application/vnd.android.package-archive"


def apk_dir() -> Path:
    return tenant_paths.data_root() / "app"


def latest_apk(directory: Path) -> Path | None:
    """The published file with the highest build number, or None."""
    best: tuple[int, Path] | None = None
    if not directory.is_dir():
        return None
    for p in directory.iterdir():
        m = APK_NAME.match(p.name)
        if m and p.is_file() and (best is None or int(m[4]) > best[0]):
            best = (int(m[4]), p)
    return best[1] if best else None


@router.get("/latest")
async def latest() -> dict:
    p = latest_apk(apk_dir())
    if p is None:
        raise HTTPException(404, "no build published")
    m = APK_NAME.match(p.name)
    assert m
    return {
        "version": f"{m[1]}.{m[2]}.{m[3]}",
        "build": int(m[4]),
        "url": f"/api/v1/app/{p.name}",
        "size": p.stat().st_size,
    }


@router.get("/{name}")
async def download(name: str) -> FileResponse:
    if not APK_NAME.match(name):
        raise HTTPException(404, "no such build")
    p = apk_dir() / name
    if not p.is_file():
        raise HTTPException(404, "no such build")
    return FileResponse(p, media_type=APK_MEDIA_TYPE, filename=name)
