-- =================================================================
-- Migration 010 — platform admin (create a church from the web)
-- =================================================================
-- `role` is scoped to one church: an admin manages that church's people and
-- nothing beyond it. Creating a CHURCH is a different kind of act — it happens
-- outside every tenant, and letting a church admin do it would hand anyone who
-- runs one church the ability to mint more. The whole isolation model rests on
-- that line, so the permission gets its own flag rather than being folded into
-- the role it is not.
--
-- Why a column on web_users and not a row in the `_system` tenant: `_system` is
-- is_active = FALSE precisely so nobody can sign in as the platform (007), and
-- resolve_web_session refuses inactive tenants. A platform admin is an ordinary
-- member of an ordinary church who additionally may create others — which is
-- exactly what a boolean on their account says.
--
-- Default FALSE, so every existing account keeps precisely the rights it had.
-- Only the accounts named at the bottom are raised, and that list is explicit
-- rather than "whoever is admin of tenant 1" — a rule like that would quietly
-- promote the next admin somebody adds to the founding church.
--
-- Run as superuser. Idempotent.
-- =================================================================

ALTER TABLE web_users
    ADD COLUMN IF NOT EXISTS is_platform_admin BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN web_users.is_platform_admin IS
    'May create new churches. Platform-level, deliberately not part of `role`.';


-- resolve_web_session is the ONLY place that answers "who is this session"
-- (invariant 6 in docs/mt_handoff.md). The flag has to come back from here, or
-- a second source of truth appears the first time some route looks it up on its
-- own — and the two will disagree the day an account is demoted mid-session.
-- Adding a column to the return type means REPLACE will not do — PostgreSQL
-- refuses to change a function's OUT parameters in place. DROP + CREATE inside
-- one transaction so there is never an instant where the function is missing:
-- outside a transaction, every live session would fail to resolve in that gap,
-- which on a running server means logging everybody out at random.
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

COMMIT;


-- Creating a church writes a `tenants` row, which no tenant policy covers —
-- tenants is the registry, not tenant data. Grant it narrowly and explicitly.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cma_app') THEN
        GRANT INSERT ON TABLE tenants TO cma_app;
        GRANT USAGE, SELECT ON SEQUENCE tenants_id_seq TO cma_app;
        -- DELETE is for ONE case: undoing a church whose first admin could not
        -- be created. The login has to be globally unique, and RLS deliberately
        -- hides other churches' logins, so the clash can only surface on INSERT
        -- — by which point the tenant row exists and its slug is taken. Without
        -- this the operator would have to pick a new slug because of a mistyped
        -- username. tenants_repo.delete_if_empty refuses any tenant that has
        -- users, so this cannot become a way to remove a live church.
        GRANT DELETE ON TABLE tenants TO cma_app;
    END IF;
END $$;


-- The founding operator. Named, not derived: "every admin of tenant 1" would
-- promote whoever is added to that church next, which is not the same thing.
UPDATE web_users SET is_platform_admin = TRUE WHERE username = 'pavlo';


INSERT INTO schema_version (version, description)
VALUES (11, 'MT: is_platform_admin — creating a church is not a tenant role')
ON CONFLICT (version) DO NOTHING;
