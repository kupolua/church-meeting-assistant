-- =================================================================
-- Migration 006 — Web authentication (MT Phase 3)
-- =================================================================
-- Until now the web UI had no auth: every request mapped to the DEFAULT tenant
-- (web/tenant.py returned the constant 1). With many churches on one server the
-- tenant must come from WHO is logged in, so we need web accounts.
--
-- Design mirrors the Telegram side exactly (see 003_multitenancy.sql):
--   - web_users is tenant-scoped and RLS-gated like every other tenant table;
--   - `username` is GLOBALLY unique — one person belongs to exactly one church,
--     which is what makes login-time tenant routing unambiguous;
--   - login is a chicken-and-egg problem (we must read web_users to learn the
--     tenant, but web_users is RLS-gated) → a SECURITY DEFINER resolver, the
--     same bootstrap trick as resolve_tenant_for_telegram().
--
-- Passwords are stored as scrypt hashes produced by web/security.py
-- ('scrypt$n$r$p$<salt_b64>$<hash_b64>'); this migration never sees plaintext.
--
-- Run as superuser (init_db / provisioning). Idempotent.
-- =================================================================


-- ─────────────────────────────────────────────────────────────
-- 1. web_users — per-tenant web accounts
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS web_users (
    id BIGSERIAL PRIMARY KEY,
    tenant_id BIGINT NOT NULL DEFAULT 1 REFERENCES tenants(id),
    username TEXT UNIQUE NOT NULL,              -- global: one person → one church
    password_hash TEXT NOT NULL,                -- scrypt$... (web/security.py)
    full_name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member'
        CHECK (role IN ('member', 'admin')),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_login_at TIMESTAMPTZ,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_web_users_tenant ON web_users(tenant_id);
CREATE INDEX IF NOT EXISTS idx_web_users_active
    ON web_users(is_active) WHERE is_active = TRUE;

COMMENT ON TABLE web_users IS
    'Web UI accounts. username is globally unique so login resolves the tenant.';
COMMENT ON COLUMN web_users.password_hash IS
    'scrypt$n$r$p$<salt_b64>$<hash_b64> — produced/verified by web/security.py';
COMMENT ON COLUMN web_users.role IS
    'member = read + query + ingest; admin = + user management';


-- ─────────────────────────────────────────────────────────────
-- 2. RLS — same fail-closed isolation as every tenant table
-- ─────────────────────────────────────────────────────────────
ALTER TABLE web_users ENABLE ROW LEVEL SECURITY;
ALTER TABLE web_users FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON web_users;
CREATE POLICY tenant_isolation ON web_users
    USING (tenant_id = current_setting('app.current_tenant', true)::bigint)
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::bigint);


-- ─────────────────────────────────────────────────────────────
-- 3. Login bootstrap (SECURITY DEFINER — bypasses RLS)
-- ─────────────────────────────────────────────────────────────
-- Returns the tenant of an ACTIVE web user, or NULL (unknown/disabled → the
-- caller rejects the login). Deliberately returns nothing but the tenant id:
-- the password hash is then read inside that tenant's RLS context, so a bug
-- here can leak at most "this username exists".
CREATE OR REPLACE FUNCTION resolve_tenant_for_web_user(p_username TEXT)
RETURNS BIGINT
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $fn$
    SELECT tenant_id FROM web_users
    WHERE username = p_username AND is_active = TRUE
    LIMIT 1
$fn$;

REVOKE ALL ON FUNCTION resolve_tenant_for_web_user(TEXT) FROM PUBLIC;


-- ─────────────────────────────────────────────────────────────
-- 4. Grants for the app role (no-op if cma_app doesn't exist yet;
--    004's ALTER DEFAULT PRIVILEGES covers the table itself)
-- ─────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cma_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON web_users TO cma_app;
        GRANT USAGE, SELECT ON SEQUENCE web_users_id_seq TO cma_app;
        GRANT EXECUTE ON FUNCTION resolve_tenant_for_web_user(TEXT) TO cma_app;
    END IF;
END $$;


INSERT INTO schema_version (version, description)
VALUES (7, 'MT Phase 3: web_users + RLS + login tenant resolver')
ON CONFLICT (version) DO NOTHING;
