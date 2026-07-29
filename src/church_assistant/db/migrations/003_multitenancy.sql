-- =================================================================
-- Migration 003 — Multi-tenancy foundation (Phase 1)
-- =================================================================
-- Adds tenant isolation so one shared server can serve many churches:
--   - tenants registry
--   - tenant_id on every tenant-scoped table (backfilled to a default tenant)
--   - audit_log (append-only) — the technical backbone for the наглядова рада
--   - Row-Level Security (RLS): isolation enforced by the DB ENGINE, not by
--     app-level WHERE filters (a forgotten filter can't leak another church).
--
-- SAFETY / CUTOVER NOTES:
--   RLS is FAIL-CLOSED: a session that does NOT set `app.current_tenant`
--   sees NO rows and cannot INSERT. Therefore this migration must be deployed
--   TOGETHER with app code that sets the tenant per operation (connection
--   helper set_tenant()). Do NOT apply to the live `cma` DB until that code is
--   wired and services are restarted. During the transition, tenant_id columns
--   keep DEFAULT 1 so any not-yet-wired path still lands in the default tenant.
--
-- Idempotent where PostgreSQL allows (IF NOT EXISTS / ON CONFLICT / OR REPLACE).
-- =================================================================


-- ─────────────────────────────────────────────────────────────
-- 1. Tenants registry
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tenants (
    id BIGSERIAL PRIMARY KEY,
    slug TEXT UNIQUE NOT NULL,                    -- url/id-safe key, e.g. 'first-baptist'
    name TEXT NOT NULL,                           -- human name of the church/council
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    telegram_bot_token TEXT,                      -- optional per-tenant bot (else shared)
    settings JSONB NOT NULL DEFAULT '{}'::jsonb,  -- per-tenant config (model overrides, quotas)
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE tenants IS
    'One row per church/council. Existing single-tenant data lives under id=1.';

-- Default tenant: all pre-migration data belongs here.
INSERT INTO tenants (id, slug, name)
VALUES (1, 'default', 'Default (existing corpus)')
ON CONFLICT (id) DO NOTHING;

-- Keep the sequence ahead of the manually-inserted id=1.
SELECT setval(
    pg_get_serial_sequence('tenants', 'id'),
    GREATEST((SELECT MAX(id) FROM tenants), 1)
);


-- ─────────────────────────────────────────────────────────────
-- 2. tenant_id on every tenant-scoped table (backfilled → 1)
-- ─────────────────────────────────────────────────────────────
-- DEFAULT 1 during transition keeps not-yet-wired inserts working.
-- (health_checks is global infra — intentionally NOT tenant-scoped.)

ALTER TABLE users          ADD COLUMN IF NOT EXISTS tenant_id BIGINT NOT NULL DEFAULT 1 REFERENCES tenants(id);
ALTER TABLE queries        ADD COLUMN IF NOT EXISTS tenant_id BIGINT NOT NULL DEFAULT 1 REFERENCES tenants(id);
ALTER TABLE logs           ADD COLUMN IF NOT EXISTS tenant_id BIGINT NOT NULL DEFAULT 1 REFERENCES tenants(id);
ALTER TABLE errors         ADD COLUMN IF NOT EXISTS tenant_id BIGINT NOT NULL DEFAULT 1 REFERENCES tenants(id);
ALTER TABLE ingestion_jobs ADD COLUMN IF NOT EXISTS tenant_id BIGINT NOT NULL DEFAULT 1 REFERENCES tenants(id);

CREATE INDEX IF NOT EXISTS idx_users_tenant          ON users(tenant_id);
CREATE INDEX IF NOT EXISTS idx_queries_tenant        ON queries(tenant_id, asked_at DESC);
CREATE INDEX IF NOT EXISTS idx_logs_tenant           ON logs(tenant_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_errors_tenant         ON errors(tenant_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_ingestion_jobs_tenant ON ingestion_jobs(tenant_id, created_at DESC);


-- ─────────────────────────────────────────────────────────────
-- 3. Audit log (append-only) — backbone for the supervisory board
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_log (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    tenant_id BIGINT REFERENCES tenants(id),
    actor TEXT,                          -- 'web:<user>' | 'bot:<tg_id>' | 'worker' | 'system'
    action TEXT NOT NULL,                -- 'data.read' | 'query.answer' | 'admin.access' | ...
    resource TEXT,                       -- 'queries/123' | 'meeting/2026-05-18' | ...
    detail JSONB
);

CREATE INDEX IF NOT EXISTS idx_audit_tenant_time ON audit_log(tenant_id, timestamp DESC);

COMMENT ON TABLE audit_log IS
    'Append-only record of data access. Inspected by the supervisory board.';


-- ─────────────────────────────────────────────────────────────
-- 4. Row-Level Security — DB-enforced tenant isolation
-- ─────────────────────────────────────────────────────────────
-- Session sets `app.current_tenant`; policies filter every row by it.
-- current_setting(..., true) → NULL if unset → fail-closed (no rows / no insert).
-- FORCE so the table OWNER (the app role) is subject to RLS too.

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['users','queries','logs','errors','ingestion_jobs','audit_log']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format($f$
            CREATE POLICY tenant_isolation ON %I
            USING (tenant_id = current_setting('app.current_tenant', true)::bigint)
            WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::bigint)
        $f$, t);
    END LOOP;
END $$;

-- audit_log is append-only: no UPDATE/DELETE for the app role.
-- (Owner can still be restricted; adjust role name if not 'cma'.)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cma') THEN
        REVOKE UPDATE, DELETE ON audit_log FROM cma;
    END IF;
END $$;


-- ─────────────────────────────────────────────────────────────
-- 5. Tenant-resolution bootstrap (SECURITY DEFINER — bypasses RLS)
-- ─────────────────────────────────────────────────────────────
-- Chicken-and-egg: to set app.current_tenant we must first know which tenant a
-- Telegram user belongs to — but `users` is RLS-gated. This function runs as its
-- owner (a privileged role) so the app can resolve the tenant BEFORE gating.
-- Returns NULL for unknown/inactive users (→ request rejected upstream).
CREATE OR REPLACE FUNCTION resolve_tenant_for_telegram(p_tg_id BIGINT)
RETURNS BIGINT
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $fn$
    SELECT tenant_id FROM users
    WHERE telegram_user_id = p_tg_id AND is_active = TRUE
    LIMIT 1
$fn$;

REVOKE ALL ON FUNCTION resolve_tenant_for_telegram(BIGINT) FROM PUBLIC;
-- app role gets EXECUTE (grant explicitly at deploy: GRANT EXECUTE ... TO cma_app)


-- ─────────────────────────────────────────────────────────────
-- Version marker
-- ─────────────────────────────────────────────────────────────
INSERT INTO schema_version (version, description)
VALUES (4, 'Multi-tenancy Phase 1: tenants, tenant_id, audit_log, RLS')
ON CONFLICT (version) DO NOTHING;
