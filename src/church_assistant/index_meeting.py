"""Index a single meeting into Qdrant collections for RAG retrieval.

Indexes 4 levels of granularity, each into its own Qdrant collection:

    cma_protocols      — one chunk per ### topic heading in polished.md
                         (meeting-level decisions, deduplicated topics)

    cma_analyses       — one chunk per ### topic heading in each raw chunk
                         (per-15-minute-segment topics, before merging)

    cma_turns          — one chunk per speaker turn in annotated.md
                         (literal text by speaker; for "who said X?" queries)

    cma_protocol_full  — one entry per meeting (Gemma-generated summary)
                         (for meeting-level "summarize the May meeting")

All embeddings use Voyage multilingual-2 (1024-dim, Cosine distance) to
match the team's existing Qdrant setup (Phase 1 collections also 1024d).

Idempotent: writes data/meetings/YYYY-MM-DD/index_state.json with content
hashes. Re-running on unchanged content is a no-op. Changed content
(re-polished, re-analyzed) triggers delete-and-reupsert.

Usage:
    uv run python -m church_assistant.index_meeting \\
        --meeting-dir data/meetings/2026-06-08

    # Dry-run to see what would be indexed
    uv run python -m church_assistant.index_meeting \\
        --meeting-dir data/meetings/2026-06-08 \\
        --dry-run

    # Force reindex even if content hashes match
    uv run python -m church_assistant.index_meeting \\
        --meeting-dir data/meetings/2026-06-08 \\
        --force
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Constants
COLLECTION_PROTOCOLS = "cma_protocols"
COLLECTION_ANALYSES = "cma_analyses"
COLLECTION_TURNS = "cma_turns"
COLLECTION_PROTOCOL_FULL = "cma_protocol_full"

ALL_COLLECTIONS = [
    COLLECTION_PROTOCOLS,
    COLLECTION_ANALYSES,
    COLLECTION_TURNS,
    COLLECTION_PROTOCOL_FULL,
]

EMBEDDING_MODEL = "voyage-multilingual-2"
EMBEDDING_DIM = 1024
EMBEDDING_DISTANCE = "Cosine"
EMBEDDING_BATCH_SIZE = 128

# Turn-level chunking: merge consecutive same-speaker turns up to this size
TURN_CHUNK_MAX_CHARS = 300

# Patterns
DATE_FROM_DIR_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
TOPIC_HEADING_RE = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)
TURN_LINE_RE = re.compile(
    r"^\[(\d+:\d+(?::\d+)?)\s+([^\]]+?)\]:\s*(.+)$"
)
CHUNK_FILENAME_RE = re.compile(r"^chunk_(\d+[a-z]?)_(\d+)m-(\d+)m\.md$")
CHUNK_SOURCE_COMMENT_RE = re.compile(
    r"<!--\s*topic from chunks:\s*(.+?)\s*-->"
)


@dataclass
class IndexPoint:
    """One vector to be upserted into Qdrant."""

    text: str                          # text that will be embedded
    payload: dict[str, Any]            # metadata stored alongside
    point_id: str = field(default_factory=lambda: str(uuid.uuid4()))


def log(msg: str, color: str = "") -> None:
    """Log with optional color."""
    codes = {
        "blue":   "\033[94m",
        "green":  "\033[92m",
        "yellow": "\033[93m",
        "red":    "\033[91m",
        "bold":   "\033[1m",
        "reset":  "\033[0m",
    }
    if color in codes:
        print(f"{codes[color]}{msg}{codes['reset']}", flush=True)
    else:
        print(msg, flush=True)


def load_env() -> None:
    """Read .env into os.environ (without overwriting existing values)."""
    env_file = Path(".env")
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(
            key.strip(),
            value.strip().strip('"').strip("'"),
        )


def md5_of_file(path: Path) -> str:
    """Compute MD5 of a file, or 'missing' if not found."""
    if not path.exists():
        return "missing"
    return hashlib.md5(path.read_bytes()).hexdigest()


def md5_of_dir(dir_path: Path, glob: str = "*.md") -> str:
    """Compute combined MD5 of all files in a directory matching glob.

    Sorted to be deterministic.
    """
    if not dir_path.exists():
        return "missing"
    files = sorted(dir_path.glob(glob))
    h = hashlib.md5()
    for f in files:
        h.update(f.name.encode())
        h.update(f.read_bytes())
    return h.hexdigest()


def parse_meeting_date(meeting_dir: Path) -> str:
    """Extract YYYY-MM-DD from the directory name."""
    m = DATE_FROM_DIR_RE.search(meeting_dir.name)
    if m is None:
        raise ValueError(
            f"Cannot extract YYYY-MM-DD from directory name: {meeting_dir.name}"
        )
    return m.group(1)


# ----- Source parsing -----


def parse_polished_topics(
    polished_md: Path,
) -> list[tuple[str, str, list[str]]]:
    """Parse polished.md into a list of (topic_title, body, source_chunks).

    A topic is everything between one '### heading' and the next.
    The 'source_chunks' list comes from the `<!-- topic from chunks: X, Y -->`
    comments that polish_protocol.py emits above each topic.
    """
    if not polished_md.exists():
        return []

    text = polished_md.read_text(encoding="utf-8")

    # Find topic blocks: each starts with optional source-chunk comment,
    # then the ### heading. Capture both pieces.
    topics: list[tuple[str, str, list[str]]] = []

    # Split the file into segments at each '### ' (preserve everything else)
    # We iterate line-by-line for robustness.
    lines = text.split("\n")
    current_title: str | None = None
    current_body_lines: list[str] = []
    current_source_chunks: list[str] = []
    pending_source_chunks: list[str] = []

    def flush() -> None:
        nonlocal current_title, current_body_lines, current_source_chunks
        if current_title is not None:
            topics.append(
                (
                    current_title,
                    "\n".join(current_body_lines).strip(),
                    list(current_source_chunks),
                )
            )
        current_title = None
        current_body_lines = []
        current_source_chunks = []

    for line in lines:
        # Catch source-chunks comment so we can attach it to the NEXT topic
        m = CHUNK_SOURCE_COMMENT_RE.match(line.strip())
        if m:
            raw = m.group(1)
            pending_source_chunks = [s.strip() for s in raw.split(",")]
            continue

        # Catch topic heading
        m = TOPIC_HEADING_RE.match(line)
        if m:
            flush()
            current_title = m.group(1).strip()
            current_source_chunks = pending_source_chunks
            pending_source_chunks = []
            continue

        if current_title is not None:
            current_body_lines.append(line)

    flush()

    return topics


def parse_chunks_topics(chunks_dir: Path) -> list[dict[str, Any]]:
    """Parse each chunk_NN.md in chunks_dir.

    Returns one entry per ### topic in each chunk:
        {chunk_id, time_range, topic_title, body}
    """
    if not chunks_dir.exists():
        return []

    entries: list[dict[str, Any]] = []
    for chunk_file in sorted(chunks_dir.glob("chunk_*.md")):
        m = CHUNK_FILENAME_RE.match(chunk_file.name)
        if not m:
            continue
        chunk_id = m.group(1)
        start_min = int(m.group(2))
        end_min = int(m.group(3))
        time_range = f"{start_min:02d}:00-{end_min:02d}:00"

        text = chunk_file.read_text(encoding="utf-8")

        # Parse topics inside this chunk (### headings)
        lines = text.split("\n")
        current_title: str | None = None
        current_body: list[str] = []

        def flush_local() -> None:
            nonlocal current_title, current_body
            if current_title is not None:
                entries.append({
                    "chunk_id": chunk_id,
                    "time_range": time_range,
                    "topic_title": current_title,
                    "body": "\n".join(current_body).strip(),
                })
            current_title = None
            current_body = []

        for line in lines:
            mh = TOPIC_HEADING_RE.match(line)
            if mh:
                flush_local()
                current_title = mh.group(1).strip()
                continue
            if current_title is not None:
                current_body.append(line)

        flush_local()

    return entries


def parse_annotated_turns(annotated_md: Path) -> list[dict[str, Any]]:
    """Parse annotated.md into a list of merged speaker turns.

    Consecutive same-speaker lines are merged up to TURN_CHUNK_MAX_CHARS,
    then a new chunk starts (still same speaker).
    """
    if not annotated_md.exists():
        return []

    turns: list[dict[str, Any]] = []
    current_speaker: str | None = None
    current_start: str | None = None
    current_text_parts: list[str] = []

    def flush_turn() -> None:
        nonlocal current_speaker, current_start, current_text_parts
        if current_speaker is not None and current_text_parts:
            turns.append({
                "speaker": current_speaker,
                "start_timestamp": current_start,
                "start_seconds": timestamp_to_seconds(current_start),
                "text": " ".join(current_text_parts).strip(),
            })
        current_speaker = None
        current_start = None
        current_text_parts = []

    for line in annotated_md.read_text(encoding="utf-8").split("\n"):
        m = TURN_LINE_RE.match(line.strip())
        if not m:
            continue
        ts, speaker, content = m.group(1), m.group(2), m.group(3)

        # New speaker → flush
        if speaker != current_speaker:
            flush_turn()
            current_speaker = speaker
            current_start = ts
            current_text_parts = [content]
            continue

        # Same speaker — check if we'd exceed cap
        combined_len = (
            sum(len(p) + 1 for p in current_text_parts) + len(content)
        )
        if combined_len > TURN_CHUNK_MAX_CHARS:
            flush_turn()
            current_speaker = speaker
            current_start = ts
            current_text_parts = [content]
        else:
            current_text_parts.append(content)

    flush_turn()
    return turns


def timestamp_to_seconds(ts: str | None) -> int | None:
    """Convert HH:MM:SS or MM:SS to seconds."""
    if ts is None:
        return None
    parts = ts.split(":")
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    return None


def load_attendees(polished_md: Path) -> list[str]:
    """Parse the '## Присутні' section from polished.md."""
    if not polished_md.exists():
        return []
    text = polished_md.read_text(encoding="utf-8")
    # Find the section between "## Присутні" and the next "##"
    m = re.search(
        r"##\s+Присутні\s*\n(.*?)(?=^##\s)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if not m:
        return []
    body = m.group(1)
    names: list[str] = []
    for line in body.split("\n"):
        line = line.strip()
        if line.startswith("- "):
            names.append(line[2:].strip())
    return names


# ----- Build IndexPoints from parsed sources -----


def build_protocol_points(
    polished_md: Path,
    meeting_date: str,
    attendees: list[str],
) -> list[IndexPoint]:
    """Build cma_protocols points from polished.md (one per ### topic)."""
    topics = parse_polished_topics(polished_md)
    points: list[IndexPoint] = []
    for topic_title, body, source_chunks in topics:
        text = f"{topic_title}\n\n{body}".strip()
        payload = {
            "meeting_date": meeting_date,
            "topic_title": topic_title,
            "source_chunks": source_chunks,
            "attendees": attendees,
            "collection": COLLECTION_PROTOCOLS,
        }
        points.append(IndexPoint(text=text, payload=payload))
    return points


def build_analysis_points(
    chunks_dir: Path,
    meeting_date: str,
) -> list[IndexPoint]:
    """Build cma_analyses points from chunks/*.md."""
    entries = parse_chunks_topics(chunks_dir)
    points: list[IndexPoint] = []
    for e in entries:
        text = f"{e['topic_title']}\n\n{e['body']}".strip()
        payload = {
            "meeting_date": meeting_date,
            "chunk_id": e["chunk_id"],
            "time_range": e["time_range"],
            "topic_title": e["topic_title"],
            "collection": COLLECTION_ANALYSES,
        }
        points.append(IndexPoint(text=text, payload=payload))
    return points


def build_turn_points(
    annotated_md: Path,
    meeting_date: str,
) -> list[IndexPoint]:
    """Build cma_turns points from annotated.md."""
    turns = parse_annotated_turns(annotated_md)
    points: list[IndexPoint] = []
    for t in turns:
        text = t["text"]
        if not text:
            continue
        payload = {
            "meeting_date": meeting_date,
            "speaker": t["speaker"],
            "start_timestamp": t["start_timestamp"],
            "start_seconds": t["start_seconds"],
            "text": t["text"],
            "collection": COLLECTION_TURNS,
        }
        points.append(IndexPoint(text=text, payload=payload))
    return points


def build_protocol_full_points(
    polished_md: Path,
    meeting_date: str,
    attendees: list[str],
    topic_count: int,
) -> list[IndexPoint]:
    """Build one cma_protocol_full point per meeting.

    For now, the embedded text is the FIRST 1500 chars of polished.md
    (intro + first few topics). Later we can replace this with a
    Gemma-generated summary.
    """
    if not polished_md.exists():
        return []
    text = polished_md.read_text(encoding="utf-8")[:1500].strip()
    payload = {
        "meeting_date": meeting_date,
        "attendees": attendees,
        "n_topics": topic_count,
        "collection": COLLECTION_PROTOCOL_FULL,
        "polished_md_path": str(polished_md),
    }
    return [IndexPoint(text=text, payload=payload)]


# ----- Voyage embeddings -----


def embed_batch(texts: list[str], api_key: str) -> list[list[float]]:
    """Call Voyage embeddings API. Returns list of vectors in input order."""
    if not texts:
        return []

    import voyageai  # lazy import
    client = voyageai.Client(api_key=api_key)
    result = client.embed(
        texts=texts,
        model=EMBEDDING_MODEL,
        input_type="document",
    )
    return result.embeddings


def embed_points(
    points: list[IndexPoint],
    api_key: str,
    batch_size: int = EMBEDDING_BATCH_SIZE,
) -> list[list[float]]:
    """Embed all points in batches, preserving order."""
    embeddings: list[list[float]] = []
    n = len(points)
    for i in range(0, n, batch_size):
        batch = points[i:i + batch_size]
        log(f"    Embedding batch {i // batch_size + 1}/{(n + batch_size - 1) // batch_size} "
            f"({len(batch)} items)...")
        vectors = embed_batch([p.text for p in batch], api_key)
        embeddings.extend(vectors)
    return embeddings


# ----- Qdrant -----


def ensure_collection(client, name: str) -> None:
    """Create collection if it does not exist."""
    from qdrant_client.http import models as qmodels

    existing = [c.name for c in client.get_collections().collections]
    if name in existing:
        return
    log(f"  Creating collection '{name}'...")
    client.create_collection(
        collection_name=name,
        vectors_config=qmodels.VectorParams(
            size=EMBEDDING_DIM,
            distance=qmodels.Distance.COSINE,
        ),
    )


def delete_points_for_meeting(client, collection: str, meeting_date: str) -> int:
    """Delete all points in a collection matching meeting_date.

    Returns the number deleted (best-effort).
    """
    from qdrant_client.http import models as qmodels

    filter_ = qmodels.Filter(
        must=[
            qmodels.FieldCondition(
                key="meeting_date",
                match=qmodels.MatchValue(value=meeting_date),
            )
        ]
    )

    # Count before delete
    count_result = client.count(
        collection_name=collection,
        count_filter=filter_,
        exact=True,
    )
    n = count_result.count

    if n > 0:
        client.delete(
            collection_name=collection,
            points_selector=qmodels.FilterSelector(filter=filter_),
        )

    return n


def upsert_points(
    client,
    collection: str,
    points: list[IndexPoint],
    vectors: list[list[float]],
) -> None:
    """Upsert (point_id, vector, payload) triples into Qdrant."""
    from qdrant_client.http import models as qmodels

    qpoints = [
        qmodels.PointStruct(
            id=p.point_id,
            vector=v,
            payload=p.payload,
        )
        for p, v in zip(points, vectors)
    ]
    client.upsert(collection_name=collection, points=qpoints)


# ----- State management -----


def load_index_state(state_path: Path) -> dict[str, Any]:
    if not state_path.exists():
        return {}
    return json.loads(state_path.read_text(encoding="utf-8"))


def save_index_state(state_path: Path, state: dict[str, Any]) -> None:
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def compute_content_hashes(meeting_dir: Path) -> dict[str, str]:
    """Compute md5 hashes of source files; used to detect changes."""
    return {
        "polished_md": md5_of_file(meeting_dir / "polished.md"),
        "annotated_md": md5_of_file(meeting_dir / "annotated.md"),
        "chunks": md5_of_dir(meeting_dir / "chunks", "*.md"),
    }


# ----- Main pipeline for one meeting -----


def index_one_meeting(
    meeting_dir: Path,
    force: bool,
    dry_run: bool,
) -> dict[str, Any]:
    """Index all four collections for one meeting. Returns the new state."""
    meeting_date = parse_meeting_date(meeting_dir)
    log(f"\n  Meeting date: {meeting_date}", "bold")

    polished_md = meeting_dir / "polished.md"
    annotated_md = meeting_dir / "annotated.md"
    chunks_dir = meeting_dir / "chunks"
    state_path = meeting_dir / "index_state.json"

    # Sanity check input
    missing = [
        str(p) for p in [polished_md, annotated_md, chunks_dir]
        if not p.exists()
    ]
    if missing:
        log(f"  ❌ Missing required files: {missing}", "red")
        raise SystemExit(1)

    # Compute content hashes
    hashes = compute_content_hashes(meeting_dir)
    log(f"  Content hashes:")
    for k, v in hashes.items():
        log(f"    {k:<15} {v}")

    # Decide whether to skip
    prev_state = load_index_state(state_path)
    prev_hashes = prev_state.get("content_hashes", {})
    if not force and prev_hashes == hashes:
        log(f"  ✓ Content unchanged — skipping (use --force to override)",
            "green")
        return prev_state

    # Load attendees from polished.md
    attendees = load_attendees(polished_md)
    log(f"  Attendees: {len(attendees)} ({', '.join(attendees) if attendees else '(none)'})")

    # Build all points
    log(f"\n  Building points to index...")
    protocol_points = build_protocol_points(polished_md, meeting_date, attendees)
    analysis_points = build_analysis_points(chunks_dir, meeting_date)
    turn_points = build_turn_points(annotated_md, meeting_date)
    full_points = build_protocol_full_points(
        polished_md, meeting_date, attendees, len(protocol_points)
    )

    log(f"    {COLLECTION_PROTOCOLS:<22} {len(protocol_points):>4} points")
    log(f"    {COLLECTION_ANALYSES:<22} {len(analysis_points):>4} points")
    log(f"    {COLLECTION_TURNS:<22} {len(turn_points):>4} points")
    log(f"    {COLLECTION_PROTOCOL_FULL:<22} {len(full_points):>4} points")

    total = (len(protocol_points) + len(analysis_points)
             + len(turn_points) + len(full_points))
    log(f"    {'TOTAL':<22} {total:>4} points")

    if dry_run:
        log(f"\n  ✓ Dry run — no embeddings or upserts performed", "yellow")
        return prev_state

    # Embed
    log(f"\n  Embedding points (Voyage {EMBEDDING_MODEL})...")
    api_key = os.environ.get("VOYAGE_API_KEY")
    if not api_key:
        log(f"  ❌ VOYAGE_API_KEY missing from .env", "red")
        raise SystemExit(1)

    t0 = time.time()
    protocol_vecs = embed_points(protocol_points, api_key)
    analysis_vecs = embed_points(analysis_points, api_key)
    turn_vecs = embed_points(turn_points, api_key)
    full_vecs = embed_points(full_points, api_key)
    log(f"  ✓ Embeddings done in {time.time() - t0:.1f}s", "green")

    # Connect Qdrant
    log(f"\n  Connecting to Qdrant...")
    from qdrant_client import QdrantClient
    qdrant = QdrantClient(host="localhost", port=6333)

    for c in ALL_COLLECTIONS:
        ensure_collection(qdrant, c)

    # Delete old points for this meeting (in case of re-index)
    log(f"\n  Deleting previous points for {meeting_date} (if any)...")
    deleted = {}
    for c in ALL_COLLECTIONS:
        n = delete_points_for_meeting(qdrant, c, meeting_date)
        deleted[c] = n
        if n > 0:
            log(f"    {c:<22} deleted {n}")
    if not any(deleted.values()):
        log(f"    (nothing previous — first index for this meeting)")

    # Upsert
    log(f"\n  Upserting points...")
    upsert_points(qdrant, COLLECTION_PROTOCOLS, protocol_points, protocol_vecs)
    upsert_points(qdrant, COLLECTION_ANALYSES, analysis_points, analysis_vecs)
    upsert_points(qdrant, COLLECTION_TURNS, turn_points, turn_vecs)
    upsert_points(qdrant, COLLECTION_PROTOCOL_FULL, full_points, full_vecs)
    log(f"  ✓ Upserts complete", "green")

    # Save state
    new_state = {
        "meeting_date": meeting_date,
        "indexed_at": datetime.now(timezone.utc).isoformat(),
        "content_hashes": hashes,
        "embedding_model": EMBEDDING_MODEL,
        "collection_counts": {
            COLLECTION_PROTOCOLS: len(protocol_points),
            COLLECTION_ANALYSES: len(analysis_points),
            COLLECTION_TURNS: len(turn_points),
            COLLECTION_PROTOCOL_FULL: len(full_points),
        },
    }
    save_index_state(state_path, new_state)
    log(f"  ✓ State saved to {state_path}", "green")

    return new_state


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Index one meeting into Qdrant collections"
    )
    parser.add_argument(
        "--meeting-dir",
        type=Path,
        required=True,
        help="Path to data/meetings/YYYY-MM-DD/ folder",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and count points, but don't embed or upsert",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-index even if content hashes match",
    )
    args = parser.parse_args()

    if not args.meeting_dir.exists():
        log(f"❌ Not found: {args.meeting_dir}", "red")
        raise SystemExit(1)

    load_env()

    log("=" * 70, "blue")
    log(f"  Index meeting: {args.meeting_dir}", "blue")
    log("=" * 70, "blue")

    state = index_one_meeting(
        meeting_dir=args.meeting_dir,
        force=args.force,
        dry_run=args.dry_run,
    )

    log(f"\n{'=' * 70}", "blue")
    log(f"  Final state", "blue")
    log(f"{'=' * 70}", "blue")
    log(json.dumps(state, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
