"""
Per-tenant Qdrant collection naming (MT Phase 3).

Before multi-tenancy there were four fixed collections shared by everyone:

    cma_protocols   cma_analyses   cma_turns   cma_protocol_full

That's the one isolation hole RLS can't close — Qdrant has no row-level
security, so a single shared collection means every church's protocol chunks sit
in one index and one forgotten filter leaks them. Filtering by a tenant_id
payload field would work only as long as every query remembers the filter;
separate collections make a leak impossible rather than unlikely:

    t_<slug>_protocols   t_<slug>_analyses   t_<slug>_turns   t_<slug>_protocol_full

LEGACY TENANT. The church that predates multi-tenancy keeps the original
`cma_*` names, so its ~14 indexed meetings need no re-embedding (hours of local
bge-m3 work). Set LEGACY_TENANT_SLUG='' to drop the special case once that
corpus has been re-indexed under the new names.

KIND vs NAME. Code branches on WHAT a hit is (a protocol topic? a speaker turn?)
— that's the *kind*. The physical collection name now varies per tenant, so
comparisons must use kind, not name. kind_of() maps either naming scheme back,
which also keeps query rows stored before this change readable.
"""

from __future__ import annotations

import os
import re
from typing import Optional

from dotenv import load_dotenv


# ─────────────────────────────────────────────────────────────
# Kinds — the stable, tenant-independent identity of a collection
# ─────────────────────────────────────────────────────────────

KIND_PROTOCOLS = "protocols"
KIND_ANALYSES = "analyses"
KIND_TURNS = "turns"
KIND_PROTOCOL_FULL = "protocol_full"

ALL_KINDS = (KIND_PROTOCOLS, KIND_ANALYSES, KIND_TURNS, KIND_PROTOCOL_FULL)

# Accepted spellings on the CLI / in form fields → kind.
KIND_ALIASES = {
    **{k: k for k in ALL_KINDS},
    **{f"cma_{k}": k for k in ALL_KINDS},
}

TENANT_PREFIX = "t_"
LEGACY_PREFIX = "cma_"

# t_<slug>_<kind>. Slugs may contain '_' and kinds may too (protocol_full), so
# the kind is anchored to the end of the string rather than split on '_'.
_TENANT_NAME_RE = re.compile(
    rf"^{TENANT_PREFIX}(?P<slug>.+)_(?P<kind>{'|'.join(ALL_KINDS)})$"
)


class UnknownCollectionKind(ValueError):
    """A collection alias that doesn't name one of the four kinds."""


def legacy_slug() -> str:
    """Slug whose collections keep the pre-multi-tenancy `cma_*` names."""
    load_dotenv()
    return os.getenv("LEGACY_TENANT_SLUG", "default").strip()


def resolve_kind(alias: str) -> str:
    """
    Normalize a user-supplied collection alias to a kind.

    Accepts 'protocols' and 'cma_protocols' (what the CLI, the bot config and
    the web form have always sent) — but not a physical per-tenant name, since
    a request must never choose which tenant's index it reads.
    """
    kind = KIND_ALIASES.get((alias or "").strip())
    if kind is None:
        raise UnknownCollectionKind(
            f"Unknown collection: {alias!r} (expected one of {', '.join(ALL_KINDS)})"
        )
    return kind


def collection_name(tenant_slug: str, kind: str) -> str:
    """
    Physical Qdrant collection for one tenant + kind.

    The slug is validated as a filesystem path would be — it is concatenated
    into a name that selects which church's vectors are searched.
    """
    from church_assistant.shared.tenant_paths import validate_slug

    if kind not in ALL_KINDS:
        raise UnknownCollectionKind(f"Unknown collection kind: {kind!r}")
    slug = validate_slug(tenant_slug)
    if slug == legacy_slug():
        return f"{LEGACY_PREFIX}{kind}"
    return f"{TENANT_PREFIX}{slug}_{kind}"


def all_collections(tenant_slug: str) -> dict[str, str]:
    """{kind: physical collection name} for one tenant."""
    return {kind: collection_name(tenant_slug, kind) for kind in ALL_KINDS}


def kind_of(collection: str) -> Optional[str]:
    """
    Physical collection name → kind. None if it isn't ours.

    Handles both schemes so hits stored in queries.hits before this change (with
    'cma_turns' in them) still render correctly.
    """
    name = (collection or "").strip()
    if name.startswith(LEGACY_PREFIX):
        return KIND_ALIASES.get(name)
    m = _TENANT_NAME_RE.match(name)
    return m.group("kind") if m else None


def slug_of(collection: str) -> Optional[str]:
    """Physical collection name → tenant slug (legacy names → the legacy slug)."""
    name = (collection or "").strip()
    if name.startswith(LEGACY_PREFIX) and name in KIND_ALIASES:
        return legacy_slug()
    m = _TENANT_NAME_RE.match(name)
    return m.group("slug") if m else None


# ─────────────────────────────────────────────────────────────
# CLI smoke test
# ─────────────────────────────────────────────────────────────

def _smoke_test() -> None:
    print("=" * 66)
    print("  collections — smoke test")
    print("=" * 66)

    os.environ["LEGACY_TENANT_SLUG"] = "default"

    # Legacy tenant keeps its names → no re-index needed.
    assert collection_name("default", KIND_PROTOCOLS) == "cma_protocols"
    assert collection_name("default", KIND_PROTOCOL_FULL) == "cma_protocol_full"
    print("1. legacy tenant → cma_* (existing index untouched) ✓")

    # Every other tenant gets its own namespace.
    assert collection_name("first-baptist", KIND_TURNS) == "t_first-baptist_turns"
    assert all_collections("first-baptist") == {
        "protocols": "t_first-baptist_protocols",
        "analyses": "t_first-baptist_analyses",
        "turns": "t_first-baptist_turns",
        "protocol_full": "t_first-baptist_protocol_full",
    }
    print("2. new tenant → t_<slug>_* (all four) ✓")

    # No two tenants can ever land on the same collection.
    a = set(all_collections("church-a").values())
    b = set(all_collections("church-b").values())
    assert not (a & b)
    print("3. two churches share no collection ✓")

    # Round-trip: name → kind/slug, both schemes.
    assert kind_of("cma_turns") == KIND_TURNS
    assert kind_of("t_first-baptist_protocol_full") == KIND_PROTOCOL_FULL
    assert kind_of("t_a_b_c_turns") == KIND_TURNS          # slug containing '_'
    assert slug_of("t_a_b_c_turns") == "a_b_c"
    assert slug_of("cma_protocols") == "default"
    assert kind_of("something_else") is None
    print("4. kind_of / slug_of round-trip (both schemes) ✓")

    # Aliases the CLI / bot / web form send.
    assert resolve_kind("protocols") == KIND_PROTOCOLS
    assert resolve_kind("cma_protocol_full") == KIND_PROTOCOL_FULL
    for bad in ("t_other-church_turns", "", "nope"):
        try:
            resolve_kind(bad)
        except UnknownCollectionKind:
            continue
        raise AssertionError(f"alias {bad!r} should have been rejected")
    print("5. aliases resolve; a physical name is NOT a valid alias ✓")

    # A malformed slug can't be smuggled into a collection name. The platform
    # tenant is refused by the same guard — it has log rows, never vectors.
    from church_assistant.shared.tenant_paths import (
        InvalidTenantSlug, SYSTEM_SLUG, SystemTenantHasNoArtifacts,
    )
    for bad in ("../etc", "UPPER", "a/b"):
        try:
            collection_name(bad, KIND_TURNS)
        except InvalidTenantSlug:
            continue
        raise AssertionError(f"slug {bad!r} should have been rejected")
    print("6. malformed slugs rejected ✓")

    try:
        collection_name(SYSTEM_SLUG, KIND_TURNS)
        raise AssertionError("_system should have no collections")
    except SystemTenantHasNoArtifacts:
        pass
    print("7. '_system' has no collections (same single guard as paths) ✓")

    print("=" * 66)
    print("  ✓ ALL COLLECTIONS SMOKE TESTS PASSED")
    print("=" * 66)


if __name__ == "__main__":
    _smoke_test()
