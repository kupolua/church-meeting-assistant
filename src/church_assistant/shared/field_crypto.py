"""
Encrypting single database columns — the protocol half of cloud_plan §3.1.

Artifacts became ciphertext at the sync boundary (ingestion/artifact_crypto).
This is the other half, and the more sensitive one: `protocol_items.resolved`
and `protocol_rulings.text` are what a council actually decided, in its own
words, and they live in Postgres rather than in a file.

CIPHERTEXT GOES INTO THE SAME TEXT COLUMN, MARKED. No migration, no parallel
`_enc` columns, no type change:

    plaintext                    "Проводити зустрічі раз на місяць"
    ciphertext    "enc.v1.<base64 of nonce||ct||tag>"

A church at kek_version = 0 keeps writing the left-hand form and never touches
this module. A church with a key writes the right-hand one. Both shapes live in
one column, which is what makes switching a church on a decision rather than a
migration — and what lets one church be encrypted while its neighbour is not.

WHY A MARKER AND NOT "TRY TO DECRYPT AND FALL BACK". Guessing would mean a
decrypt failure — a wrong key, a rotation the reader has not caught up with,
corruption — silently rendering as though the row were plaintext. The council
would read raw base64 where a decision should be, or worse, nothing at all. A
marker turns every one of those into a loud failure instead.

BINDING. The AAD carries church, table, column and row id, so a blob cannot be
moved to another field, another row, another church. The row id means callers
must have one before sealing: an INSERT writes the row, takes its id back, and
seals in the same transaction (see protocols_repo). That is a real cost, paid
because minutes are a document where "who said what about which question" is
the whole point, and swapping two `resolved` values between items would produce
a protocol that is internally plausible and false.
"""

from __future__ import annotations

import base64
import json
from typing import Any, Optional

from church_assistant.shared import crypto


PREFIX = "enc.v1."


class NotDecryptable(Exception):
    """A marked value that would not open.

    Deliberately not the same as crypto.DecryptionFailed: by the time a caller
    sees this, the question is not "was the key wrong" but "what should this
    page show", and the answer is an honest refusal rather than a blank field.
    """


def aad(tenant_id: int, table: str, column: str, row_id: int) -> bytes:
    """Pins a value to one column of one row of one church."""
    return crypto.context(tenant_id, table, column, row_id)


def is_sealed(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(PREFIX)


def seal(
    dek: Optional[bytes], value: Optional[str],
    tenant_id: int, table: str, column: str, row_id: int,
) -> Optional[str]:
    """Encrypt one field, or pass it through when the church has no key.

    `dek is None` is the ordinary state of every church today, so this is not a
    special case to be handled at each call site — it is the default path.
    """
    if dek is None or value is None or value == "":
        return value
    if is_sealed(value):
        return value          # already sealed: re-sealing would double-wrap
    blob = crypto.encrypt(dek, value.encode("utf-8"),
                          aad(tenant_id, table, column, row_id))
    return PREFIX + base64.b64encode(blob).decode("ascii")


def unseal(
    dek: Optional[bytes], value: Any,
    tenant_id: int, table: str, column: str, row_id: int,
) -> Any:
    """Decrypt one field. Plaintext passes through; a marked value must open.

    Raises NotDecryptable rather than returning the marker, because a page that
    renders `enc.v1.AAAA…` where a decision should be is worse than a page that
    says it cannot show this.
    """
    if not is_sealed(value):
        return value
    if dek is None:
        raise NotDecryptable(f"{table}.{column} is encrypted and no key is available")
    try:
        blob = base64.b64decode(value[len(PREFIX):], validate=True)
        return crypto.decrypt(dek, blob, aad(tenant_id, table, column, row_id)).decode("utf-8")
    except (crypto.DecryptionFailed, ValueError) as e:
        raise NotDecryptable(f"{table}.{column} would not open: {e}") from e


# Which columns hold what a person said, or points at a person — cloud_plan
# §3.1. Everything absent from this map is metadata the server's invariants
# stand on (seq, dates, status, position, ids, vote counts) and stays readable.
SENSITIVE: dict[str, tuple[str, ...]] = {
    "meeting_protocols": ("secretary", "quorum", "created_by"),
    "protocol_items": ("question", "heard", "resolved"),
    "protocol_rulings": ("text", "responsible", "due"),
}

# JSONB columns from the same list. Separate because the value arrives as a
# Python list, not a string: `attendees` is who sat in the room, which §3.1
# calls out as exactly what a leak would expose.
#
# THE WHOLE LIST IS ONE BLOB, not one blob per name. Sealing each name would
# leave the COUNT in the clear — "eleven people attended" is itself a fact about
# a council — and would make the column a set of equal-length ciphertexts that
# repeat across meetings whenever the same person attends twice. One blob has
# neither property. It costs the ability to query attendance in SQL, which
# §3.1 already gives up for every other sealed column.
SENSITIVE_JSON: dict[str, tuple[str, ...]] = {
    "meeting_protocols": ("attendees",),
}


def seal_json(
    dek: Optional[bytes], value: Any,
    tenant_id: int, table: str, column: str, row_id: int,
) -> Any:
    """Seal a JSONB value. Returns something json.dumps() can write back.

    A sealed JSONB column holds a JSON *string* (the marker) where it used to
    hold an array. Postgres is content either way, and `is_sealed` tells the
    two apart on the way back — the same one-column-two-shapes trick the text
    columns use, for the same reason: switching a church on stays a decision
    rather than a migration.
    """
    if dek is None or value is None:
        return value
    if is_sealed(value):
        return value
    return seal(dek, json.dumps(value, ensure_ascii=False),
                tenant_id, table, column, row_id)


def unseal_json(
    dek: Optional[bytes], value: Any,
    tenant_id: int, table: str, column: str, row_id: int,
) -> Any:
    """Inverse of seal_json(). A plaintext array passes through untouched."""
    if not is_sealed(value):
        return value
    return json.loads(unseal(dek, value, tenant_id, table, column, row_id))


def seal_row(
    dek: Optional[bytes], tenant_id: int, table: str, row: dict[str, Any],
) -> dict[str, Any]:
    """Seal every sensitive column present in `row`. Needs row['id']."""
    if dek is None:
        return row
    rid = int(row["id"])
    out = dict(row)
    for col in SENSITIVE.get(table, ()):
        if col in out:
            out[col] = seal(dek, out[col], tenant_id, table, col, rid)
    for col in SENSITIVE_JSON.get(table, ()):
        if col in out:
            out[col] = seal_json(dek, out[col], tenant_id, table, col, rid)
    return out


def open_row(
    dek: Optional[bytes], tenant_id: int, table: str, row: dict[str, Any],
) -> dict[str, Any]:
    """Inverse of seal_row(), for a row already read from the database."""
    rid = int(row["id"])
    out = dict(row)
    for col in SENSITIVE.get(table, ()):
        if col in out:
            out[col] = unseal(dek, out[col], tenant_id, table, col, rid)
    for col in SENSITIVE_JSON.get(table, ()):
        if col in out:
            out[col] = unseal_json(dek, out[col], tenant_id, table, col, rid)
    return out
