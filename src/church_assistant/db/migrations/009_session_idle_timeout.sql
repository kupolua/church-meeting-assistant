-- =================================================================
-- Migration 009 — session idle timeout (MT Phase 3, follow-up)
-- =================================================================
-- web_sessions already has an ABSOLUTE cap: expires_at is set at sign-in and
-- never extended, so no session outlives WEB_SESSION_TTL however busy it is.
-- What that cannot express is the case it was never meant to: a session left
-- open and untouched on a machine someone else can reach. For that the clock
-- has to run from LAST USE, not from sign-in.
--
-- So resolve_web_session() gains an idle window. Both limits now live in the
-- one function that decides whether a session may act — which is the whole
-- reason that function exists: adding a rule here needs no change anywhere
-- else, and there is no second copy to drift out of sync.
--
-- The window is a PARAMETER rather than a constant in the SQL: it is an
-- operational knob (WEB_SESSION_IDLE_TIMEOUT), and baking it in would mean a
-- migration every time an operator wanted a different value.
--
-- CAUTION for whoever tunes it: last_seen_at is refreshed at most once per
-- web_sessions_repo.TOUCH_INTERVAL_SECONDS (60 s by default), so an idle window
-- anywhere near that would expire sessions that are actively in use. Keep it in
-- hours. p_idle_seconds <= 0 disables the check.
--
-- Replaces the 008 signature by ADDING a defaulted argument, so any caller that
-- still passes one argument keeps the old behaviour (no idle limit).
--
-- Run as superuser. Idempotent.
-- =================================================================

CREATE OR REPLACE FUNCTION resolve_web_session(
    p_token_hash TEXT,
    p_idle_seconds INT DEFAULT 0
)
RETURNS TABLE (
    session_id   BIGINT,
    tenant_id    BIGINT,
    tenant_slug  TEXT,
    web_user_id  BIGINT,
    username     TEXT,
    full_name    TEXT,
    role         TEXT,
    expires_at   TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $fn$
    SELECT s.id, s.tenant_id, t.slug, u.id, u.username, u.full_name, u.role,
           s.expires_at, s.last_seen_at
    FROM web_sessions s
    JOIN web_users u ON u.id = s.web_user_id
    JOIN tenants   t ON t.id = s.tenant_id
    WHERE s.token_hash = p_token_hash
      AND s.revoked_at IS NULL
      AND s.expires_at > NOW()                    -- absolute cap (from sign-in)
      AND (p_idle_seconds <= 0                    -- idle cap (from last use)
           OR s.last_seen_at > NOW() - make_interval(secs => p_idle_seconds))
      AND u.is_active                             -- account disabled → cut off
      AND t.is_active                             -- church suspended → same
    LIMIT 1
$fn$;

REVOKE ALL ON FUNCTION resolve_web_session(TEXT, INT) FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cma_app') THEN
        GRANT EXECUTE ON FUNCTION resolve_web_session(TEXT, INT) TO cma_app;
    END IF;
END $$;

-- The one-argument form from 008 would otherwise linger and keep resolving
-- sessions with no idle check, depending on how PostgreSQL picked an overload.
DROP FUNCTION IF EXISTS resolve_web_session(TEXT);


INSERT INTO schema_version (version, description)
VALUES (10, 'MT: session idle timeout in resolve_web_session')
ON CONFLICT (version) DO NOTHING;
