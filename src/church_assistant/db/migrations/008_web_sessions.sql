-- =================================================================
-- Migration 008 — server-side web sessions (MT Phase 3, follow-up)
-- =================================================================
-- Sessions used to live entirely in a signed cookie: self-contained, zero DB
-- reads, and impossible to revoke. Disabling an account left the person signed
-- in until their cookie expired (up to 12 h), and "sign this person out" had
-- exactly one lever — rotating WEB_SECRET_KEY, which signs everyone out. For a
-- system whose trust model is a supervisory board, "access removed" has to mean
-- removed now.
--
-- So the cookie becomes a pointer and the DB becomes the authority.
--
-- WHAT IS STORED. Only the SHA-256 of the session token, never the token
-- itself — same reasoning as password hashes: a leaked backup or an over-broad
-- SELECT then yields nothing a browser can present. Plain SHA-256 (not scrypt)
-- is right here because the token is 256 bits of CSPRNG output, so there is no
-- guessable input to slow an attacker down over.
--
-- ONE PLACE FOR LIVENESS. resolve_web_session() checks every condition that
-- makes a session usable — not revoked, not expired, the account still active,
-- the church still active. Anything that reads sessions goes through it, so
-- there is no second copy of the rules to forget to update. That is also what
-- makes deactivation take effect on the very next request without any code
-- hunting down that user's sessions.
--
-- Same SECURITY DEFINER bootstrap as the login and the bot: web_sessions is
-- RLS-gated, but a request arrives knowing only a cookie — the tenant is what
-- we are trying to learn.
--
-- Run as superuser. Idempotent.
-- =================================================================


-- ─────────────────────────────────────────────────────────────
-- 1. web_sessions
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS web_sessions (
    id BIGSERIAL PRIMARY KEY,
    tenant_id BIGINT NOT NULL REFERENCES tenants(id),
    web_user_id BIGINT NOT NULL REFERENCES web_users(id) ON DELETE CASCADE,
    token_hash TEXT UNIQUE NOT NULL,            -- sha256 hex, never the token
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,                     -- set = signed out / cut off
    user_agent TEXT,
    ip TEXT
);

CREATE INDEX IF NOT EXISTS idx_web_sessions_tenant ON web_sessions(tenant_id);
CREATE INDEX IF NOT EXISTS idx_web_sessions_user
    ON web_sessions(web_user_id, revoked_at, expires_at DESC);
-- The hot path is one equality lookup on token_hash per request; UNIQUE already
-- provides that index.

COMMENT ON TABLE web_sessions IS
    'Server-side web sessions. The cookie carries an opaque token; only its '
    'SHA-256 is stored here. Revoking a row cuts access on the next request.';
COMMENT ON COLUMN web_sessions.revoked_at IS
    'Non-NULL = unusable. Sessions are never deleted on sign-out, so the '
    'supervisory board can still see that the session existed.';


-- ─────────────────────────────────────────────────────────────
-- 2. RLS — same fail-closed isolation as every tenant table
-- ─────────────────────────────────────────────────────────────
ALTER TABLE web_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE web_sessions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON web_sessions;
CREATE POLICY tenant_isolation ON web_sessions
    USING (tenant_id = current_setting('app.current_tenant', true)::bigint)
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::bigint);


-- ─────────────────────────────────────────────────────────────
-- 3. Request bootstrap (SECURITY DEFINER — bypasses RLS)
-- ─────────────────────────────────────────────────────────────
-- The single definition of "this session may act right now". Returns the
-- identity the request will run as, or no rows.
--
-- It deliberately returns the user's CURRENT role and name rather than letting
-- the caller cache them in the cookie: a demotion or a rename then applies on
-- the next request instead of at the next login.
CREATE OR REPLACE FUNCTION resolve_web_session(p_token_hash TEXT)
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
      AND s.expires_at > NOW()
      AND u.is_active                    -- account disabled → cut off at once
      AND t.is_active                    -- church suspended → same
    LIMIT 1
$fn$;

REVOKE ALL ON FUNCTION resolve_web_session(TEXT) FROM PUBLIC;


-- ─────────────────────────────────────────────────────────────
-- 4. Housekeeping (SECURITY DEFINER — deliberately cross-tenant)
-- ─────────────────────────────────────────────────────────────
-- Expired rows are dead weight, and no single church's request context can see
-- across the table to clear them. Kept separate from resolve() so the hot path
-- stays a pure read.
CREATE OR REPLACE FUNCTION purge_expired_web_sessions(p_keep_days INT DEFAULT 30)
RETURNS INTEGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $fn$
DECLARE
    n INTEGER;
BEGIN
    -- Keep recently-expired rows for a while: "when did this session end" is a
    -- question the board may ask, and deleting on expiry destroys the answer.
    DELETE FROM web_sessions
    WHERE expires_at < NOW() - make_interval(days => p_keep_days);
    GET DIAGNOSTICS n = ROW_COUNT;
    RETURN n;
END
$fn$;

REVOKE ALL ON FUNCTION purge_expired_web_sessions(INT) FROM PUBLIC;


-- ─────────────────────────────────────────────────────────────
-- 5. Grants for the app role
-- ─────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cma_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON web_sessions TO cma_app;
        GRANT USAGE, SELECT ON SEQUENCE web_sessions_id_seq TO cma_app;
        GRANT EXECUTE ON FUNCTION resolve_web_session(TEXT) TO cma_app;
        GRANT EXECUTE ON FUNCTION purge_expired_web_sessions(INT) TO cma_app;
    END IF;
END $$;


INSERT INTO schema_version (version, description)
VALUES (9, 'MT: server-side web_sessions (revocable) + resolver')
ON CONFLICT (version) DO NOTHING;
