"""
Per-tenant filesystem layout (MT Phase 3).

Meeting artifacts (audio, transcripts, protocols) and voice profiles are the
most sensitive things this system holds, and before multi-tenancy they lived in
two shared folders:

    data/meetings/<date>/…
    data/voice_profiles/<name>.npy

Two churches would then collide on `data/meetings/2026-06-15/`, and one church's
voice fingerprints would be offered as name suggestions in another's speaker
review. So every tenant gets its own subtree, keyed by slug:

    data/tenants/<slug>/meetings/<date>/…
    data/tenants/<slug>/voice_profiles/<name>.npy

LEGACY TENANT. The church that predates multi-tenancy keeps the original shared
folders — moving ~14 meetings of audio is a real, avoidable risk, and its slug
('default') is the only one that can mean the old layout. `scripts/migrate_tenant_fs.py`
moves it into the new layout when you want to; until then both work, because
paths_for() checks the legacy location for that one slug. Set LEGACY_TENANT_SLUG=''
to turn the special case off entirely.

Slugs are validated here rather than trusted: a slug reaches this module from a
session cookie and from the DB, and one containing '..' or '/' would escape the
data root. The tenants table has no such constraint, so this is the enforcement
point.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


# Repo root: this file lives at src/church_assistant/shared/tenant_paths.py
REPO_ROOT = Path(__file__).resolve().parents[3]

# A slug is url/id-safe by construction (see tenants.slug); enforce it before it
# is ever concatenated into a path.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")

# The platform tenant (migration 007). It owns log rows, never artifacts — it is
# not a church and has no recordings, protocols or voice profiles. Reaching here
# with it means a system code path picked up a tenant it should have skipped, so
# say that plainly instead of failing on the leading-underscore regex.
SYSTEM_SLUG = "_system"


class InvalidTenantSlug(ValueError):
    """A tenant slug that must not be turned into a filesystem path."""


class SystemTenantHasNoArtifacts(InvalidTenantSlug):
    """Asked for the platform tenant's data directory — it has none."""


def data_root() -> Path:
    """Root of all artifact storage (DATA_ROOT, default <repo>/data)."""
    load_dotenv()
    raw = os.getenv("DATA_ROOT", "").strip()
    return Path(raw).expanduser().resolve() if raw else REPO_ROOT / "data"


def legacy_slug() -> str:
    """
    Slug of the pre-multi-tenancy church, whose data stays in the old folders.

    Empty string disables the legacy layout entirely (every tenant, including
    'default', then uses data/tenants/<slug>/).
    """
    load_dotenv()
    return os.getenv("LEGACY_TENANT_SLUG", "default").strip()


def validate_slug(slug: str) -> str:
    """Return the slug if it is path-safe, else raise."""
    slug = (slug or "").strip()
    if slug == SYSTEM_SLUG:
        raise SystemTenantHasNoArtifacts(
            f"Tenant {SYSTEM_SLUG!r} is the platform, not a church — it has no "
            f"meetings or voice profiles. A caller reached artifact storage with "
            f"a system tenant; skip system tenants there instead."
        )
    if not _SLUG_RE.match(slug) or slug in (".", ".."):
        raise InvalidTenantSlug(
            f"Unsafe tenant slug {slug!r} — expected [a-z0-9][a-z0-9._-]*"
        )
    return slug


@dataclass(frozen=True)
class TenantPaths:
    """Where one tenant's artifacts live."""
    slug: str
    root: Path                       # data/tenants/<slug>  (or data/ for legacy)
    meetings: Path                   # …/meetings
    voice_profiles: Path             # …/voice_profiles

    def meeting_dir(self, meeting_date: str) -> Path:
        """Folder for one meeting. The date is a caller-validated 'YYYY-MM-DD'."""
        return self.meetings / meeting_date

    def ensure(self) -> "TenantPaths":
        """Create the tenant's folders if missing (idempotent)."""
        self.meetings.mkdir(parents=True, exist_ok=True)
        self.voice_profiles.mkdir(parents=True, exist_ok=True)
        return self


def _legacy_paths(slug: str, root: Path) -> TenantPaths:
    return TenantPaths(
        slug=slug,
        root=root,
        meetings=root / "meetings",
        voice_profiles=root / "voice_profiles",
    )


def paths_for(slug: str) -> TenantPaths:
    """
    Resolve one tenant's artifact folders.

    For the legacy tenant this returns the original shared folders as long as
    they still exist and the new subtree hasn't been created — i.e. the moment
    migrate_tenant_fs.py runs, the same call starts returning the new location
    with no code change and no config flip.
    """
    slug = validate_slug(slug)
    root = data_root()
    tenant_root = root / "tenants" / slug

    if slug == legacy_slug() and not tenant_root.exists():
        legacy = _legacy_paths(slug, root)
        if legacy.meetings.exists() or legacy.voice_profiles.exists():
            return legacy

    return TenantPaths(
        slug=slug,
        root=tenant_root,
        meetings=tenant_root / "meetings",
        voice_profiles=tenant_root / "voice_profiles",
    )


def legacy_paths_for(slug: str) -> TenantPaths:
    """The pre-multi-tenancy folders for a slug (used by the migration script)."""
    return _legacy_paths(validate_slug(slug), data_root())


def tenant_paths_for(slug: str) -> TenantPaths:
    """The post-migration folders for a slug, ignoring the legacy fallback."""
    slug = validate_slug(slug)
    tenant_root = data_root() / "tenants" / slug
    return TenantPaths(
        slug=slug,
        root=tenant_root,
        meetings=tenant_root / "meetings",
        voice_profiles=tenant_root / "voice_profiles",
    )


# ─────────────────────────────────────────────────────────────
# CLI smoke test
# ─────────────────────────────────────────────────────────────

def _smoke_test() -> None:
    import tempfile

    print("=" * 66)
    print("  tenant_paths — smoke test")
    print("=" * 66)

    with tempfile.TemporaryDirectory() as d:
        os.environ["DATA_ROOT"] = d
        os.environ["LEGACY_TENANT_SLUG"] = "default"
        root = Path(d).resolve()   # macOS /var → /private/var; data_root() resolves too

        # A brand-new tenant always gets the per-tenant subtree.
        p = paths_for("first-baptist")
        assert p.meetings == root / "tenants" / "first-baptist" / "meetings", p.meetings
        assert p.voice_profiles == root / "tenants" / "first-baptist" / "voice_profiles"
        print(f"1. new tenant → {p.meetings.relative_to(root)} ✓")

        # No legacy folders on disk → even 'default' uses the new layout.
        assert paths_for("default").meetings == root / "tenants" / "default" / "meetings"
        print("2. 'default' with no legacy folders → new layout ✓")

        # Legacy folders present → 'default' keeps them (no forced migration).
        (root / "meetings").mkdir()
        assert paths_for("default").meetings == root / "meetings"
        assert paths_for("first-baptist").meetings != root / "meetings"
        print("3. legacy data/meetings present → 'default' stays put ✓")

        # Once migrated, the fallback stops applying.
        (root / "tenants" / "default" / "meetings").mkdir(parents=True)
        assert paths_for("default").meetings == root / "tenants" / "default" / "meetings"
        print("4. after migration → 'default' follows the new layout ✓")

        # Two tenants never share a meeting folder for the same date.
        a = paths_for("church-a").meeting_dir("2026-06-15")
        b = paths_for("church-b").meeting_dir("2026-06-15")
        assert a != b
        print("5. same date, two churches → separate folders ✓")

        # Path traversal is refused, not sanitized.
        for bad in ("../../etc", "a/b", "", ".", "..", "UPPER", "sl ug"):
            try:
                paths_for(bad)
            except InvalidTenantSlug:
                continue
            raise AssertionError(f"slug {bad!r} should have been rejected")
        print("6. traversal / malformed slugs rejected ✓")

        # The platform tenant owns log rows, never artifacts.
        try:
            paths_for(SYSTEM_SLUG)
            raise AssertionError("_system should have no artifact directory")
        except SystemTenantHasNoArtifacts:
            pass
        print("7. '_system' has no data directory (distinct, explicit error) ✓")

        p = paths_for("church-a").ensure()
        assert p.meetings.is_dir() and p.voice_profiles.is_dir()
        print("8. ensure() creates both folders ✓")

    print("=" * 66)
    print("  ✓ ALL TENANT_PATHS SMOKE TESTS PASSED")
    print("=" * 66)


if __name__ == "__main__":
    _smoke_test()
