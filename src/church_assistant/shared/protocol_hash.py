"""
The canonical text a signature is over.

A protocol is a header row, items and rulings in three tables. What a
member signs on their phone has to be one byte string that both sides can
build identically from those rows, today and in five years. So: a dict with
only the fields a council would read aloud, sorted keys, no whitespace,
UTF-8, SHA-256. Nothing that is not content — no ids, no timestamps, no
chair_id — because a re-numbered row is not a changed decision.

The phone never builds this itself: the server sends the hex digest with
the protocol and the phone signs those 32 bytes. The server recomputes on
every signature and on every read, so a stale signature is visible without
anyone comparing texts.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical(protocol: dict[str, Any], items: list[dict[str, Any]]) -> bytes:
    doc = {
        "number": f"{protocol['meeting_date']:%d-%m-%Y}/{protocol['seq']}",
        "date": f"{protocol['meeting_date']:%Y-%m-%d}",
        "chair": (protocol.get("chair_name") or protocol.get("chair_username") or "").strip(),
        "secretary": (protocol.get("secretary") or "").strip(),
        "attendees": [str(a).strip() for a in (protocol.get("attendees") or [])],
        "quorum": (protocol.get("quorum") or "").strip(),
        "items": [{
            "position": int(i["position"]),
            "question": (i.get("question") or "").strip(),
            "heard": (i.get("heard") or "").strip(),
            "resolved": (i.get("resolved") or "").strip(),
            "status": i.get("status") or "considered",
            "votes": [i.get("votes_for"), i.get("votes_against"), i.get("votes_abstain")],
            "rulings": [{
                "position": int(r["position"]),
                "text": (r.get("text") or "").strip(),
                "responsible": (r.get("responsible") or "").strip(),
                "due": (r.get("due") or "").strip(),
            } for r in i.get("rulings", [])],
        } for i in items],
    }
    return json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def content_hash(protocol: dict[str, Any], items: list[dict[str, Any]]) -> str:
    """Hex SHA-256 of the canonical text."""
    return hashlib.sha256(canonical(protocol, items)).hexdigest()
