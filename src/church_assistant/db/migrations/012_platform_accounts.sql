-- =================================================================
-- Migration 012 — the platform admin stops being somebody's church member
-- =================================================================
-- 010 gave a church member a flag. That worked, but it put "run the fleet" and
-- "run this congregation" in one account and one screen, and the seam showed:
-- registering a church from inside a church's own admin page, then having
-- nowhere to see the result.
--
-- A platform account now lives in `_system` (tenant 0) — the row that has
-- existed since 007 precisely to mean "the platform, which is not a church".
-- It owns no meetings, no voice profiles, no Qdrant collections; tenant_paths
-- and collections already refuse it by name.
--
-- WHAT THIS RELAXES, AND WHY THAT IS STILL SAFE. 007 set `_system.is_active =
-- FALSE` so that "sign in as the platform" was impossible for free, and
-- resolve_web_session enforces it by refusing inactive tenants. That guard was
-- blunt on purpose because there was no legitimate platform account. There is
-- one now, so the guard narrows rather than disappears: tenant 0 is accepted
-- ONLY for an account that carries is_platform_admin. A member row placed in
-- `_system` by accident still cannot sign in, and `_system` stays inactive for
-- every other purpose (it is not offered in listings and owns no data).
--
-- The two panels do not intersect, and that is enforced in the application
-- (web/tenant.py): a platform session is refused by every church route, and a
-- church session by every platform route. This migration only makes the session
-- possible; it grants no reach into any church's rows, which RLS still governs
-- by app.current_tenant exactly as before.
--
-- Run as superuser. Idempotent.
-- =================================================================

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
      AND s.expires_at > NOW()                    -- absolute cap (from sign-in)
      AND (p_idle_seconds <= 0                    -- idle cap (from last use)
           OR s.last_seen_at > NOW() - make_interval(secs => p_idle_seconds))
      AND u.is_active                             -- account disabled → cut off
      AND (
            t.is_active                           -- a church, still running
            OR (t.id = 0 AND u.is_platform_admin) -- or the platform, and only
          )                                       -- for an account allowed there
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


-- Invites have to reach the platform too, for the same reason they exist at
-- all: whoever sets up the first platform account should not be handed its
-- password. Same narrowing as the session resolver — tenant 0 is accepted only
-- for an account that is a platform admin.
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
      AND (t.is_active OR (t.id = 0 AND u.is_platform_admin))
    FOR UPDATE OF i;

    IF v_invite_id IS NULL THEN
        RETURN NULL;
    END IF;

    UPDATE web_invites SET used_at = NOW() WHERE id = v_invite_id;
    UPDATE web_users
       SET password_hash = p_password_hash,
           is_active = TRUE
     WHERE id = v_user_id;

    RETURN v_user_id;
END
$fn$;

REVOKE ALL ON FUNCTION redeem_web_invite(TEXT, TEXT) FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cma_app') THEN
        GRANT EXECUTE ON FUNCTION resolve_web_invite(TEXT) TO cma_app;
        GRANT EXECUTE ON FUNCTION redeem_web_invite(TEXT, TEXT) TO cma_app;
    END IF;
END $$;


-- The flag moves off every church account. `pavlo` administers `default` and
-- nothing more from here on; running the fleet becomes a separate login. This
-- leaves NO platform admin for a moment — scripts/add_platform_admin.py makes
-- the first one, and it issues an invite rather than a password.
UPDATE web_users SET is_platform_admin = FALSE WHERE tenant_id <> 0;

-- Enforced in the schema, not in a form: a rule that lives only in a route
-- holds until somebody adds a second route.
ALTER TABLE web_users DROP CONSTRAINT IF EXISTS web_users_platform_in_system;
ALTER TABLE web_users ADD CONSTRAINT web_users_platform_in_system
    CHECK (NOT is_platform_admin OR tenant_id = 0);


-- Numbers, and only numbers. The platform panel needs to show how big each
-- church is without being able to look inside one, so this returns counts and
-- nothing that could be read as content — no names of people, no titles, no
-- dates of meetings. SECURITY DEFINER because a platform session carries
-- tenant 0 and RLS would otherwise (correctly) show it nothing.
--
-- Deliberately NOT done by setting app.current_tenant per church in the
-- application: that would put "become that church for a moment" into the
-- codebase, where the next person can reuse it for something less innocent.
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
        GRANT EXECUTE ON FUNCTION platform_church_counts() TO cma_app;
        -- Suspending a church flips tenants.is_active, which resolve_web_session
        -- already honours. tenants_repo.set_active has existed since Phase 1 and
        -- would have failed at runtime: the grant was never there.
        GRANT UPDATE ON TABLE tenants TO cma_app;
    END IF;
END $$;


INSERT INTO schema_version (version, description)
VALUES (13, 'MT: platform accounts live in _system, not inside a church')
ON CONFLICT (version) DO NOTHING;
