-- =================================================================
-- Church Meeting Assistant — Database Schema
-- Version: 1.0
-- Date: 2026-06-25
-- =================================================================
--
-- This schema supports:
--   - User whitelist (Telegram bot authorization)
--   - Query queue + history (web + telegram, async processing)
--   - Structured logging (T1+T2+T3+T4)
--   - Error tracking (with Telegram alerts via MVP-B)
--   - Health check snapshots (operational monitoring)
--
-- Constraints:
--   - PostgreSQL 16+
--   - Single database: cma
--   - Single schema: public (default)
-- =================================================================


-- =================================================================
-- TABLE: users
-- =================================================================
-- Whitelist of authorized Telegram users.
-- Pavlo (admin) + ~10 pastors.
--
CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    telegram_user_id BIGINT UNIQUE NOT NULL,
    telegram_username TEXT,                     -- @username, can be NULL
    full_name TEXT NOT NULL,                    -- "Іван Іванов"
    role TEXT NOT NULL DEFAULT 'pastor'
        CHECK (role IN ('pastor', 'admin')),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    added_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_users_telegram_id
    ON users(telegram_user_id);

CREATE INDEX IF NOT EXISTS idx_users_active
    ON users(is_active) WHERE is_active = TRUE;

COMMENT ON TABLE users IS
    'Whitelist of authorized Telegram bot users (pastoral council)';
COMMENT ON COLUMN users.telegram_user_id IS
    'Numeric Telegram ID (from /api/getMe or message.from.id)';
COMMENT ON COLUMN users.role IS
    'pastor = can query; admin = + /stats, /errors commands';


-- =================================================================
-- TABLE: queries
-- =================================================================
-- All RAG queries: web + telegram.
-- Also serves as queue (status='pending') and history (status='completed').
--
CREATE TABLE IF NOT EXISTS queries (
    id BIGSERIAL PRIMARY KEY,

    -- Identification
    source TEXT NOT NULL
        CHECK (source IN ('web', 'telegram')),
    user_id BIGINT REFERENCES users(id),        -- NULL for web (Pavlo)
    telegram_chat_id BIGINT,                    -- For bot delivery (NULL for web)
    telegram_message_id BIGINT,                 -- Reply-to message (NULL for web)

    -- Query content
    question TEXT NOT NULL,
    collection TEXT NOT NULL DEFAULT 'protocols'
        CHECK (collection IN ('protocols', 'analyses', 'turns', 'protocol_full')),
    verbose_mode BOOLEAN NOT NULL DEFAULT FALSE,     -- /verbose flag from bot

    -- Status workflow
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'completed', 'failed', 'cancelled')),
    asked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,                     -- When worker picked it up
    completed_at TIMESTAMPTZ,                   -- When done

    -- Results (filled when status='completed')
    hits JSONB,                                 -- List of Hit objects with scores
    synthesis TEXT,                             -- Gemma output
    sources TEXT[],                             -- List of meeting dates referenced

    -- Performance metrics (milliseconds)
    embed_time_ms INTEGER,
    qdrant_time_ms INTEGER,
    rerank_time_ms INTEGER,
    gemma_time_ms INTEGER,
    total_time_ms INTEGER,

    -- Error info (if status='failed')
    error_message TEXT,
    error_traceback TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0
);

-- Index for queue scanning (worker fetches pending)
CREATE INDEX IF NOT EXISTS idx_queries_status_pending
    ON queries(asked_at) WHERE status = 'pending';

-- Index for history view (newest first)
CREATE INDEX IF NOT EXISTS idx_queries_asked_at
    ON queries(asked_at DESC);

-- Index for user history
CREATE INDEX IF NOT EXISTS idx_queries_user
    ON queries(user_id, asked_at DESC) WHERE user_id IS NOT NULL;

-- Index for source filtering (web vs telegram analytics)
CREATE INDEX IF NOT EXISTS idx_queries_source
    ON queries(source, asked_at DESC);

-- Index for /verbose: find user's last completed query
CREATE INDEX IF NOT EXISTS idx_queries_telegram_completed
    ON queries(telegram_chat_id, completed_at DESC)
    WHERE status = 'completed' AND source = 'telegram';

COMMENT ON TABLE queries IS
    'RAG queries from web (Pavlo) and Telegram bot (team). Queue + history.';
COMMENT ON COLUMN queries.status IS
    'pending → processing → completed | failed (retry x3) | cancelled (manual)';
COMMENT ON COLUMN queries.hits IS
    'JSONB: [{point_id, meeting_date, topic_title, body, vector_score, rerank_score}]';


-- =================================================================
-- TABLE: logs
-- =================================================================
-- Structured application logs (T1+T2+T3+T4).
-- One row per significant event.
-- Forever retention (manual cleanup if needed).
--
CREATE TABLE IF NOT EXISTS logs (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    process TEXT NOT NULL
        CHECK (process IN ('web', 'bot', 'worker', 'cli')),
    level TEXT NOT NULL
        CHECK (level IN ('DEBUG', 'INFO', 'WARN', 'ERROR')),
    event TEXT NOT NULL,                        -- e.g. 'query.started', 'ollama.timeout'
    message TEXT,                               -- Human-readable
    metadata JSONB,                             -- Arbitrary structured data
    query_id BIGINT REFERENCES queries(id),     -- Link to query (if applicable)
    user_id BIGINT REFERENCES users(id)         -- Link to user (if applicable)
);

CREATE INDEX IF NOT EXISTS idx_logs_timestamp
    ON logs(timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_logs_level_event
    ON logs(level, event, timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_logs_query
    ON logs(query_id) WHERE query_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_logs_process_level
    ON logs(process, level, timestamp DESC);

COMMENT ON TABLE logs IS
    'Structured application logs. T1+T2+T3+T4 monitoring data.';
COMMENT ON COLUMN logs.event IS
    'Dot-separated namespace: query.started, ollama.down, bot.unauthorized, etc.';


-- =================================================================
-- TABLE: errors
-- =================================================================
-- Errors (caught exceptions) — separate from logs for fast alert lookup.
-- Triggers Telegram alerts to Pavlo (MVP-B).
--
CREATE TABLE IF NOT EXISTS errors (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    process TEXT NOT NULL
        CHECK (process IN ('web', 'bot', 'worker', 'cli')),
    error_type TEXT NOT NULL,                   -- 'OllamaTimeout', 'QdrantConnectionError', etc.
    error_message TEXT NOT NULL,
    traceback TEXT NOT NULL,
    query_id BIGINT REFERENCES queries(id),
    user_id BIGINT REFERENCES users(id),
    alerted_at TIMESTAMPTZ,                     -- When Telegram alert sent
    resolved_at TIMESTAMPTZ,                    -- When marked as resolved (dashboard)
    metadata JSONB
);

-- Fast lookup: unalerted errors (worker alerts loop polls this)
CREATE INDEX IF NOT EXISTS idx_errors_unalerted
    ON errors(timestamp) WHERE alerted_at IS NULL;

-- Fast lookup: unresolved errors (dashboard view)
CREATE INDEX IF NOT EXISTS idx_errors_unresolved
    ON errors(timestamp DESC) WHERE resolved_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_errors_timestamp
    ON errors(timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_errors_type
    ON errors(error_type, timestamp DESC);

COMMENT ON TABLE errors IS
    'Caught exceptions. Source for Telegram alerts and Tier 3/4 dashboard.';


-- =================================================================
-- TABLE: health_checks
-- =================================================================
-- Snapshots of system health (Tier 1).
-- Written by worker every 60s.
--
CREATE TABLE IF NOT EXISTS health_checks (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ollama_up BOOLEAN NOT NULL,
    qdrant_up BOOLEAN NOT NULL,
    postgres_up BOOLEAN NOT NULL DEFAULT TRUE,  -- Trivially TRUE (we're inserting)
    ollama_response_time_ms INTEGER,
    qdrant_response_time_ms INTEGER,
    notes TEXT                                  -- Optional: which collection, model, etc.
);

CREATE INDEX IF NOT EXISTS idx_health_timestamp
    ON health_checks(timestamp DESC);

-- Index for "show current status" — latest row
CREATE INDEX IF NOT EXISTS idx_health_latest
    ON health_checks(id DESC);

COMMENT ON TABLE health_checks IS
    'System health snapshots (Ollama/Qdrant up-down, response times). Written every 60s by worker.';


-- =================================================================
-- TABLE: ingestion_jobs
-- =================================================================
-- Async meeting-ingestion pipeline (MVP-C): upload audio → protocol.
-- One row per meeting being processed. Mirrors the `queries` queue model,
-- but the pipeline is long (diarization ~2h) and has a human-in-the-loop
-- pause (edit speakers.json) between transcription and analysis.
--
-- Status machine (runnable states = 'pending', 'queued_analysis'):
--   pending          → worker runs diarization + transcription (slow)
--   transcribing     → (in-flight) match_speakers + transcribe
--   awaiting_review  → paused: user edits speakers.json in web editor
--   queued_analysis  → review submitted, worker resumes
--   analyzing        → (in-flight) merge → chunked_analyze → polish
--   indexing         → (in-flight) index_meeting into Qdrant
--   completed        → polished.md written + indexed
--   failed           → error (retry_count tracked)
--   cancelled        → manual stop (dashboard)
--
CREATE TABLE IF NOT EXISTS ingestion_jobs (
    id BIGSERIAL PRIMARY KEY,

    -- Identity / filesystem
    meeting_date TEXT NOT NULL,                 -- 'YYYY-MM-DD' (folder name)
    meeting_dir TEXT NOT NULL,                  -- abs path to data/meetings/<date>/
    original_filename TEXT,                     -- uploaded file's name (audit)
    audio_filename TEXT,                        -- copied-in name (e.g. audio.m4a)

    -- Status workflow
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN (
            'pending', 'transcribing', 'awaiting_review',
            'queued_analysis', 'analyzing', 'indexing',
            'completed', 'failed', 'cancelled'
        )),
    stage TEXT,                                 -- fine-grained: 'diarization', 'whisper', 'merge', 'analyze', 'polish', 'index'
    progress_note TEXT,                         -- human-readable current-step note

    -- Timestamps
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,                     -- worker first picked it up
    transcribed_at TIMESTAMPTZ,                 -- transcription done (→ awaiting_review)
    reviewed_at TIMESTAMPTZ,                    -- speakers.json submitted
    completed_at TIMESTAMPTZ,                   -- fully done (or failed/cancelled)

    -- Results / metadata
    speaker_count INTEGER,                      -- # speakers detected (from speakers.json)
    indexed BOOLEAN NOT NULL DEFAULT FALSE,     -- did index_meeting run
    index_points INTEGER,                       -- points upserted (optional)

    -- Error info (if status='failed')
    error_message TEXT,
    error_traceback TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,

    notes TEXT
);

-- One job per meeting_date (re-uploading resumes the same folder).
CREATE UNIQUE INDEX IF NOT EXISTS idx_ingestion_meeting_date
    ON ingestion_jobs(meeting_date);

-- Queue scan: runnable jobs oldest-first (pending + queued_analysis).
CREATE INDEX IF NOT EXISTS idx_ingestion_runnable
    ON ingestion_jobs(created_at)
    WHERE status IN ('pending', 'queued_analysis');

-- Active-jobs view (newest first) for the ingestion dashboard.
CREATE INDEX IF NOT EXISTS idx_ingestion_created_at
    ON ingestion_jobs(created_at DESC);

COMMENT ON TABLE ingestion_jobs IS
    'Async meeting-ingestion pipeline (MVP-C). Queue + history for audio→protocol.';
COMMENT ON COLUMN ingestion_jobs.status IS
    'pending → transcribing → awaiting_review → queued_analysis → analyzing → indexing → completed | failed | cancelled';
COMMENT ON COLUMN ingestion_jobs.stage IS
    'Fine-grained progress within a status (diarization/whisper/merge/analyze/polish/index)';


-- =================================================================
-- VIEWS for common queries
-- =================================================================

-- Latest health status
CREATE OR REPLACE VIEW v_latest_health AS
SELECT *
FROM health_checks
ORDER BY id DESC
LIMIT 1;

-- Queue depth (for dashboard widget)
CREATE OR REPLACE VIEW v_queue_depth AS
SELECT
    count(*) FILTER (WHERE status = 'pending') AS pending,
    count(*) FILTER (WHERE status = 'processing') AS processing,
    count(*) FILTER (WHERE status = 'failed') AS failed
FROM queries;

-- Today's stats
CREATE OR REPLACE VIEW v_stats_today AS
SELECT
    count(*) AS total,
    count(*) FILTER (WHERE status = 'completed') AS completed,
    count(*) FILTER (WHERE status = 'failed') AS failed,
    count(*) FILTER (WHERE source = 'web') AS from_web,
    count(*) FILTER (WHERE source = 'telegram') AS from_telegram,
    avg(total_time_ms) FILTER (WHERE status = 'completed') AS avg_time_ms
FROM queries
WHERE asked_at > NOW() - INTERVAL '24 hours';

-- Ingestion queue depth (for the ingestion dashboard widget)
CREATE OR REPLACE VIEW v_ingestion_depth AS
SELECT
    count(*) FILTER (WHERE status = 'pending')          AS pending,
    count(*) FILTER (WHERE status = 'transcribing')     AS transcribing,
    count(*) FILTER (WHERE status = 'awaiting_review')  AS awaiting_review,
    count(*) FILTER (WHERE status = 'queued_analysis')  AS queued_analysis,
    count(*) FILTER (WHERE status = 'analyzing')        AS analyzing,
    count(*) FILTER (WHERE status = 'indexing')         AS indexing,
    count(*) FILTER (WHERE status = 'completed')        AS completed,
    count(*) FILTER (WHERE status = 'failed')           AS failed
FROM ingestion_jobs;


-- =================================================================
-- INITIAL DATA
-- =================================================================
-- Note: No INSERT statements here.
-- Pavlo is added separately via scripts/add_user.py with admin role.
-- This is intentional: schema.sql should be idempotent and not require
-- environment-specific data.


-- =================================================================
-- SCHEMA VERSION TRACKING
-- =================================================================
-- Used by future migration scripts.
--
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    description TEXT NOT NULL
);

INSERT INTO schema_version (version, description)
VALUES (1, 'Initial schema: users, queries, logs, errors, health_checks')
ON CONFLICT (version) DO NOTHING;

INSERT INTO schema_version (version, description)
VALUES (2, 'MVP-C: ingestion_jobs table + v_ingestion_depth view')
ON CONFLICT (version) DO NOTHING;
