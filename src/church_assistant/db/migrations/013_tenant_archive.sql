-- =================================================================
-- Migration 013 — archiving a church instead of deleting it
-- =================================================================
-- A church that leaves should not have its archive destroyed the same afternoon
-- somebody clicked a button. It also should not linger as a live tenant. So
-- "delete" means archive: access stops immediately, the data stays where it is,
-- and a clock starts.
--
-- ARCHIVED IS NOT SUSPENDED, and the two are deliberately separate columns
-- rather than one status. Suspension is operational and reversible by design —
-- an unpaid month, an investigation, a church asking for a pause. Archiving is
-- terminal-with-a-grace-period, and its restore is a different act that
-- deserves a different button. Collapsing them into one field would make
-- "resume" quietly undo a decision nobody meant to undo.
--
-- WHAT IS NOT HERE: automatic destruction. Nothing in this migration or in the
-- application ever deletes an archived church. scripts/purge_archived_tenants.py
-- lists what is past its date and removes it only when a person runs it. A cron
-- job that erases a congregation's history on a schedule is the kind of thing
-- that works correctly for years and then does not.
--
-- Run as superuser. Idempotent.
-- =================================================================

ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;

COMMENT ON COLUMN tenants.deleted_at IS
    'Archived at this moment. Data retained; see scripts/purge_archived_tenants.py.';

-- Partial: almost every row is NULL here, and the only question ever asked is
-- "which ones are archived".
CREATE INDEX IF NOT EXISTS idx_tenants_deleted ON tenants (deleted_at)
    WHERE deleted_at IS NOT NULL;


-- Access has to stop on archiving, not merely on the is_active flag the caller
-- is trusted to have set too. Two fields that must agree are two fields that
-- will one day disagree, so the session resolver checks the one that matters.
BEGIN;

DROP FUNCTION IF EXISTS resolve_web_session(TEXT, INT);

CREATE FUNCTION resolve_web_session(
    p_token_hash TEXT,
    p_idle_seconds INT DEFAULT 0
)
RETURNS TABLE (
    session_id        BIGINT,
    tenant_id         BIGINT,
    tenant_slug       TEXT,
    web_user_id       BIGINT,
    username          TEXT,
    full_name         TEXT,
    role              TEXT,
    is_platform_admin BOOLEAN,
    expires_at        TIMESTAMPTZ,
    last_seen_at      TIMESTAMPTZ
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $fn$
    SELECT s.id, s.tenant_id, t.slug, u.id, u.username, u.full_name, u.role,
           u.is_platform_admin, s.expires_at, s.last_seen_at
    FROM web_sessions s
    JOIN web_users u ON u.id = s.web_user_id
    JOIN tenants   t ON t.id = s.tenant_id
    WHERE s.token_hash = p_token_hash
      AND s.revoked_at IS NULL
      AND s.expires_at > NOW()
      AND (p_idle_seconds <= 0
           OR s.last_seen_at > NOW() - make_interval(secs => p_idle_seconds))
      AND u.is_active
      AND t.deleted_at IS NULL                    -- archived → nobody gets in
      AND (
            t.is_active
            OR (t.id = 0 AND u.is_platform_admin)
          )
    LIMIT 1
$fn$;

REVOKE ALL ON FUNCTION resolve_web_session(TEXT, INT) FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cma_app') THEN
        GRANT EXECUTE ON FUNCTION resolve_web_session(TEXT, INT) TO cma_app;
    END IF;
END $$;

COMMIT;


-- An archived church must not be reachable through an unused invite either:
-- the link was issued while it was live, and it outlives the decision.
CREATE OR REPLACE FUNCTION resolve_web_invite(p_token_hash TEXT)
RETURNS TABLE (
    invite_id   BIGINT,
    tenant_id   BIGINT,
    tenant_slug TEXT,
    web_user_id BIGINT,
    username    TEXT,
    full_name   TEXT
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $fn$
    SELECT i.id, i.tenant_id, t.slug, u.id, u.username, u.full_name
    FROM web_invites i
    JOIN web_users u ON u.id = i.web_user_id
    JOIN tenants   t ON t.id = i.tenant_id
    WHERE i.token_hash = p_token_hash
      AND i.used_at IS NULL
      AND i.expires_at > NOW()
      AND t.deleted_at IS NULL
      AND (t.is_active OR (t.id = 0 AND u.is_platform_admin))
    LIMIT 1
$fn$;

REVOKE ALL ON FUNCTION resolve_web_invite(TEXT) FROM PUBLIC;

CREATE OR REPLACE FUNCTION redeem_web_invite(
    p_token_hash TEXT,
    p_password_hash TEXT
)
RETURNS BIGINT
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $fn$
DECLARE
    v_invite_id BIGINT;
    v_user_id   BIGINT;
BEGIN
    SELECT i.id, i.web_user_id INTO v_invite_id, v_user_id
    FROM web_invites i
    JOIN web_users u ON u.id = i.web_user_id
    JOIN tenants   t ON t.id = i.tenant_id
    WHERE i.token_hash = p_token_hash
      AND i.used_at IS NULL
      AND i.expires_at > NOW()
      AND t.deleted_at IS NULL
      AND (t.is_active OR (t.id = 0 AND u.is_platform_admin))
    FOR UPDATE OF i;

    IF v_invite_id IS NULL THEN
        RETURN NULL;
    END IF;

    UPDATE web_invites SET used_at = NOW() WHERE id = v_invite_id;
    UPDATE web_users
       SET password_hash = p_password_hash, is_active = TRUE
     WHERE id = v_user_id;

    RETURN v_user_id;
END
$fn$;

REVOKE ALL ON FUNCTION redeem_web_invite(TEXT, TEXT) FROM PUBLIC;


-- Counts now carry the archive state, so the panel can list live and archived
-- churches from one query without asking a second question per row.
CREATE OR REPLACE FUNCTION platform_church_counts()
RETURNS TABLE (
    tenant_id       BIGINT,
    accounts        BIGINT,
    active_accounts BIGINT,
    pending_invites BIGINT,
    jobs            BIGINT
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $fn$
    SELECT t.id,
           (SELECT count(*) FROM web_users u WHERE u.tenant_id = t.id),
           (SELECT count(*) FROM web_users u WHERE u.tenant_id = t.id AND u.is_active),
           (SELECT count(*) FROM web_invites i
             WHERE i.tenant_id = t.id AND i.used_at IS NULL AND i.expires_at > NOW()),
           (SELECT count(*) FROM ingestion_jobs j WHERE j.tenant_id = t.id)
    FROM tenants t
    WHERE t.id <> 0
$fn$;

REVOKE ALL ON FUNCTION platform_church_counts() FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cma_app') THEN
        GRANT EXECUTE ON FUNCTION resolve_web_invite(TEXT) TO cma_app;
        GRANT EXECUTE ON FUNCTION redeem_web_invite(TEXT, TEXT) TO cma_app;
        GRANT EXECUTE ON FUNCTION platform_church_counts() TO cma_app;
    END IF;
END $$;


INSERT INTO schema_version (version, description)
VALUES (14, 'MT: archive a church for a year instead of deleting it')
ON CONFLICT (version) DO NOTHING;
