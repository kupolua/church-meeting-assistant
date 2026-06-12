# Phase 2B: RAG over historical meeting protocols

**Status:** planning (no code yet)
**Author:** Pavlo Kulakovskyi
**Created:** 2026-06-12

---

## 1. Context

### Where we are

Phase 2A delivered an automated audio→protocol pipeline:
`audio.m4a → diarization + transcription → speaker-attributed transcript → Gemma chunked analysis → polished.md`

Two real meetings (01/06/2026, 08/06/2026) processed end-to-end. Both protocols
approved by the product owner and shared with the team. Team feedback: "дуже
гарна стенографія розмови, допомагає зорієнтуватись у списку питань."

### The new gap

A working transcript is not enough. The team and the leadership need to
**search and reason over the history of protocols**, not just read the
latest one. Sample real questions Pavlo wants to answer:

- "Коли ми обговорювали Великдень минулого року?"
- "Які питання обговорювались більше 3-х разів, але не попали в протокол?"
- "Які питання забули?"
- "Які питання втратили актуальність?"
- "Хто говорив про проблеми музичного служіння?"
- "Хто сказав фразу: 'попереджаю, що це не моя позиція'?"
- "Що ми вирішували по молодіжному служінню за останні 6 місяців?"

These split into two distinct query families:

1. **Retrieval queries** — find passages where a topic / person / phrase
   appears. Classic RAG territory.
2. **Analytics queries** — aggregate over the corpus to surface patterns
   (e.g. recurring unfinished topics). Not classic RAG; needs agent-level
   reasoning over structured outputs.

### Corpus size — what we actually have

- 11 audio recordings of pastoral council meetings
- 3 already processed (baseline 2026-02-23, 2026-06-01, 2026-06-08)
- 9 to process (19 hours total audio): mostly Zoom, 3 are phone recordings
  with lower audio quality
- Date range: 2025-08-09 to 2026-06-08 (~10 months)
- Team composition stable across the period
- Occasional one-off guests possible (Pavlo can identify each)

Processing all 9 remaining recordings: ~19 hours wall clock if run
sequentially; ~10 hours if two `new_meeting.py` wrappers run in parallel
(experimental on M1 32GB). Decision: start with one at a time, parallelize
later if stable.

---

## 2. User stories

### US-1: Topic history
**As** a pastor preparing for a meeting,
**I want** to ask "що ми вирішували по [темі] раніше?"
**so that** I don't repeat past discussions or contradict prior decisions.

### US-2: Who said what
**As** a pastor or new team member,
**I want** to ask "хто говорив про [тему]?" or "хто сказав [фраза]?"
**so that** I know who to follow up with on a specific concern.

### US-3: Unresolved topics
**As** the team secretary (Pavlo) or chairman,
**I want** to identify "які теми обговорювалися декілька разів, але без
рішення?"
**so that** we can either resolve them or formally drop them.

### US-4: Stale topics
**As** the team,
**I want** to identify "які питання втратили актуальність?" — topics
that came up once or twice months ago and were never mentioned again,
**so that** we can close them or recognize that the team has lost
interest.

### US-5: Person-topic history
**As** Pavlo or any team member,
**I want** to ask "що Роман говорив про [тему] за останній рік?"
**so that** I understand a colleague's stance before bringing up the
topic again.

### US-6: New member onboarding
**As** a new team member,
**I want** to ask "розкажи мені історію [теми]"
**so that** I have context for ongoing discussions.

### US-7 (out of scope for Phase 2B but worth naming):
The team has also asked for action-item workflow (Web UI with Google auth,
self-assigned deadlines, Telegram reminders, integration with church
calendar). This is **Phase 3 — Church Assistant Web App**. It is a much
larger project; not part of this planning.

---

## 3. Architecture

### High level

```
                       ┌──────────────────────────────┐
                       │  Existing Phase 2A pipeline  │
                       │  (audio → polished.md)       │
                       └───────────────┬──────────────┘
                                       │
                                       ▼
              ┌────────────────────────────────────────────┐
              │  data/meetings/YYYY-MM-DD/                 │
              │   ├── polished.md                          │
              │   ├── annotated.md                         │
              │   ├── chunks/*.md                          │
              │   ├── transcript.json (whisper turns)      │
              │   └── diarization.rttm                     │
              └────────────────────────┬───────────────────┘
                                       │
                  ┌────────────────────┴────────────────────┐
                  │                                         │
                  ▼                                         ▼
        ┌─────────────────┐                       ┌──────────────────┐
        │  Indexing job   │                       │  Query interface │
        │  (one-off + on  │                       │  (CLI for now,   │
        │  new meeting)   │                       │  later Web)      │
        └────────┬────────┘                       └────────┬─────────┘
                 │                                         │
                 ▼                                         ▼
        ┌─────────────────┐                       ┌──────────────────┐
        │  Qdrant         │◀──── retrieval ──────│  Query router    │
        │  (vector store) │                       │  (RAG vs         │
        │                 │      ┌────────────────│   analytics      │
        │  Multiple       │      │                │   vs agent)      │
        │  collections    │      │                └──────────────────┘
        │  per granularity│      │                         │
        └─────────────────┘      │                         ▼
                                 │                ┌──────────────────┐
                                 └────────────────│  Gemma 4 26B     │
                                                  │  (answer/summary)│
                                                  └──────────────────┘
```

### Why two paths from query

Retrieval queries (US-1, US-2, US-5, US-6) → standard RAG: vector search,
rerank, LLM answer.

Analytics queries (US-3, US-4) → aggregation pipeline that may use vector
search as a sub-step but produces structured output (lists, counts, time
ranges) before the LLM frames the answer.

A query router (Gemma classifier, or a simpler intent matcher) decides
which path to use.

---

## 4. Tech stack

Choices follow Phase 1 wherever sensible — Pavlo has working code from
`llmops-roadmap/day08-observability/lf-client/`.

| Component         | Choice                          | Why                              |
| ----------------- | ------------------------------- | -------------------------------- |
| Vector DB         | Qdrant (local Docker)           | Reused from Phase 1; works offline |
| Embeddings        | Voyage `voyage-multilingual-2`  | Strong Ukrainian support; Phase 1 experience |
| Reranker          | Voyage `rerank-2`               | Phase 1 experience; +20% precision in practice |
| Hybrid retrieval  | BM25 + vector + RRF             | Phase 1 pattern; handles literal-phrase queries (US-2) |
| Multi-query       | Yes (3 query variants)          | Phase 1 pattern; broadens recall on vague queries |
| LLM (answer)      | Gemma 4 26B (Ollama, local)     | Already used in Phase 2A polish step |
| Observability     | Langfuse (optional)             | Phase 1 setup; can add later     |
| Query interface   | CLI first, Web later            | Match current Phase 2A interface |

### Costs

- Voyage embeddings: ~$0.13 per 1M tokens. ~10 hours of transcripts at
  ~30K chars each ≈ 1M tokens. **One-time indexing ~$0.13.**
- Voyage rerank: ~$0.05 per 1K queries. Negligible.
- Re-indexing on each new meeting: ~$0.01 (one meeting ~100K chars).

All other components free / local.

---

## 5. Indexing strategy

Four levels of granularity, indexed into separate Qdrant collections:

### Collection A: `protocols`
**Source:** `polished.md` (one document per meeting)
**Granularity:** one chunk per `### topic heading` inside the polished
protocol
**Use:** topic-level retrieval ("which meetings discussed Великдень?")
**Metadata:** meeting_date, attendees, topic_title, source_chunks (which
raw chunks merged into this topic)

### Collection B: `analyses`
**Source:** `chunks/*.md` (raw Gemma output per 15-min chunk)
**Granularity:** one chunk per `### topic heading` inside each chunk
**Use:** finer-grained topic retrieval before merging; useful for
US-3/US-4 (counting occurrences across the meeting timeline)
**Metadata:** meeting_date, chunk_id, time_range, topic_title

### Collection C: `turns`
**Source:** `annotated.md`
**Granularity:** one chunk per speaker turn (or merge consecutive same-
speaker turns up to ~300 chars)
**Use:** literal-text queries (US-2 "хто сказав X?"), person+topic queries
(US-5 "що Роман говорив про X?")
**Metadata:** meeting_date, speaker, timestamp, audio_seek_seconds

### Collection D: `protocol_full`
**Source:** `polished.md`
**Granularity:** the entire protocol as one document, with a summary
field generated by Gemma
**Use:** meeting-level retrieval ("show me the summary of the May meeting")
**Metadata:** meeting_date, attendees, n_topics, n_action_items

### Indexing job

A new module `church_assistant/index_meeting.py`:
- Input: a meeting folder (`data/meetings/YYYY-MM-DD/`)
- Reads polished.md, chunks/*.md, annotated.md
- Splits per the rules above
- Calls Voyage embeddings API
- Upserts to four Qdrant collections
- Writes `index_state.json` in the meeting folder so re-runs are idempotent

`new_meeting.py` wrapper grows an optional `--index` flag that calls
`index_meeting.py` as a final step.

---

## 6. Query types and routing

### Query family: retrieval (RAG-shaped)

User asks a natural-language question. Pipeline:

1. **Query analysis** (Gemma, fast call): classify into RAG or analytics
   bucket; if RAG, identify which collection(s) to hit (turns for literal
   quotes; protocols for topic-level; analyses for time-bounded).
2. **Multi-query expansion** (Gemma): generate 3 query variants to
   broaden recall.
3. **Retrieve**: BM25 + vector search in chosen collections, RRF merge.
4. **Rerank** (Voyage rerank-2): top 30 → top 8.
5. **Generate** (Gemma): synthesize answer with citations.

Sample mapping:

| Query                              | Collections used      |
| ---------------------------------- | --------------------- |
| "Хто сказав 'попереджаю'?"         | turns                 |
| "Що Роман говорив про музику?"     | turns (filter: speaker=Роман) |
| "Які рішення були по Великодню?"   | protocols             |
| "Розкажи історію [теми]"           | protocols + analyses (timeline) |

### Query family: analytics

User asks a question that requires aggregation, not just retrieval:

| Query                                              | Approach          |
| -------------------------------------------------- | ----------------- |
| "Які теми обговорювалися >3 разів?"                | scan analyses; group by topic_title (fuzzy match); count |
| "Які теми обговорювалися, але немає в протоколі?"  | diff: analyses topics minus protocols topics |
| "Які теми втратили актуальність?"                  | scan analyses; for each topic, find last_mention_date; rank by age |
| "Хто найчастіше пропонує рішення?"                 | scan all chunks; LLM extracts proposer; count |

These need a `church_assistant/analytics.py` with one function per query
type initially. Later — agent-style with Gemma choosing the right tool.

---

## 7. Implementation phases

### Phase 2B.0 — Process remaining audio (BEFORE building RAG)

- 9 meetings × ~2 hours processing each
- Run sequentially through `new_meeting.py`
- Manual review of speakers.json for each (likely some [REVIEW] tags and
  occasional one-off guests to name manually)
- Output: `data/meetings/*/polished.md` for 12 total meetings
- **Estimate:** 3-5 working days (mostly background)
- **Risk:** phone-recorded meetings may have poor speaker separation;
  expect more manual cleanup there

### Phase 2B.1 — Indexing infrastructure

- Set up Qdrant locally (Docker)
- Voyage API keys in `.env`
- Write `index_meeting.py` with 4 collections
- Test on 3 already-processed meetings
- Verify embeddings, metadata, retrieval round-trip
- **Estimate:** 1-2 days

### Phase 2B.2 — Retrieval queries (RAG)

- Write `query.py` CLI: `uv run python -m church_assistant.query "<question>"`
- Implement RAG pipeline: classify → multi-query → retrieve (hybrid) →
  rerank → answer
- Test against US-1, US-2, US-5, US-6
- **Estimate:** 2-3 days

### Phase 2B.3 — Analytics queries

- Write `analytics.py` with one function per analytic query type
- Wire into `query.py` router
- Test against US-3, US-4
- **Estimate:** 2 days

### Phase 2B.4 — Wrapper integration + indexing on new meeting

- `new_meeting.py` gains `--index` flag
- On a new meeting, after polish, run `index_meeting.py` automatically
- Validate end-to-end: new meeting → searchable in seconds
- **Estimate:** 0.5 day

### Phase 2B.5 — Evaluation

- Build a small eval set: 15-20 known answers across the 6 user stories
- Measure: precision@5, answer accuracy (manual grading)
- Iterate on tricky cases
- **Estimate:** 1-2 days

**Total Phase 2B:** ~8-12 working days after audio processing finishes.

---

## 8. Open questions

### OQ-1: Multi-meeting deduplication of topics

A topic like "Великдень" appears in multiple meetings. When the user asks
"коли ми обговорювали Великдень?", they want a timeline across meetings,
not one passage from one meeting.

**Question:** Do we merge topic chunks across meetings into a single
"thread" view, or keep them per-meeting and let the LLM stitch the
timeline in the answer?

**Tentative answer:** Keep per-meeting in storage. Let the LLM stitch
when generating the answer (post-retrieval). Adding a "thread" view as a
post-processing layer is possible but defers for now.

### OQ-2: Updating embeddings if Gemma reprocesses a meeting

If we tweak the chunked-analyze prompt and re-run on an old meeting,
the polished.md changes. Do we re-embed?

**Tentative answer:** Yes — `index_meeting.py` should detect content
changes (md5 of polished.md vs index_state.json) and re-upsert. This is
why we have `index_state.json` per meeting.

### OQ-3: Phone-recorded meeting quality

Three of the nine remaining recordings are phone audio. Voice
fingerprinting may degrade. Expected impact:

- Lower cosine similarities, more [REVIEW] flags
- Potentially more "no match" speakers (background noise misidentified)
- Manual review burden higher per meeting

**Tentative answer:** Process one phone recording first as a sanity
check. If quality is unacceptable, consider running it through audio
enhancement (e.g. RNNoise) as a preprocessing step. Not blocking the
RAG work itself.

### OQ-4: Ukrainian language tokenization for BM25

BM25 needs decent tokenization. Most stock BM25 libraries assume English.
For Ukrainian we may need:

- A library that handles Ukrainian word forms (lemmatization helps for
  recall)
- Or accept "okay-not-great" with default tokenization

**Tentative answer:** Start with default tokenizer; measure. If recall
on US-2 ("хто сказав X?") is poor, swap in `pymorphy3` for Ukrainian
lemmatization. Optional, not blocking.

### OQ-5: Authentication and privacy for RAG

Right now everything is local. If the RAG eventually goes online (web
UI for team), we need authentication, and the question "who can search
which meetings?" gets real.

**Tentative answer:** Out of scope for Phase 2B. Local-only.
Authentication is a Phase 3 concern.

### OQ-6: Query latency budget

Voyage embeddings + rerank API calls add latency: ~500 ms per query for
embeddings, ~300 ms for rerank, ~5-10 s for Gemma generation.

**Tentative answer:** ~10-15 s end-to-end is acceptable for an
exploratory CLI. Web UI would want streaming output for perceived
latency reduction; address in Phase 3.

### OQ-7: Cost monitoring

Voyage API has free tier (50M tokens/month initial), then $0.13/1M
tokens. Easy to fit our usage. But should be tracked.

**Tentative answer:** Add a thin wrapper around the Voyage SDK that
logs token counts to a local JSON file. Pavlo can review monthly.

---

## 9. Decisions deferred to implementation

- **Chunking strategy for turns:** simple max-300-char windows, or
  use the diarization turn boundaries? Decide when writing `index_meeting.py`.
- **BM25 library:** `rank-bm25` is well-known but no Ukrainian support
  out of the box. `bm25s` is newer and faster. Decide when writing the
  retrieval module.
- **CLI output format:** plain text, or markdown with citations, or JSON?
  Decide when writing `query.py`. Likely plain text for terminal,
  markdown when piped to a file.

---

## 10. Success criteria

Phase 2B is "done" when:

1. All 12 meetings (3 existing + 9 new) are processed and indexed.
2. CLI `query.py` can answer all 6 user stories on the eval set with
   manual grading ≥80% accuracy.
3. Adding a new meeting takes < 2 hours wall clock (current pipeline +
   indexing).
4. Pavlo uses the system for at least 3 real lookups in a real working
   week without falling back to grep.
