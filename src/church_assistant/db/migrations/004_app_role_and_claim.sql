-- =================================================================
-- Migration 004 — app role + cross-tenant queue claim (MT Phase 1, step 1-2)
-- =================================================================
-- Two things the rewired app needs:
--
-- (1) A NON-superuser DB role. RLS is bypassed by superusers/BYPASSRLS roles;
--     the container's `cma` role is superuser, so isolation only works if the
--     app connects as `cma_app`. Password is set at deploy (not in git).
--
-- (2) Shared background workers must scan the queue ACROSS tenants, but the
--     tables are RLS-gated (a tenant-less session sees nothing). These
--     SECURITY DEFINER functions atomically claim the next item across all
--     tenants (bypassing RLS) and return it WITH its tenant_id, so the worker
--     can then process inside that tenant's context.
--
-- Run as superuser (init_db / provisioning). Idempotent.
-- =================================================================


-- ─────────────────────────────────────────────────────────────
-- 1. Application role (non-superuser → subject to RLS)
-- ─────────────────────────────────────────────────────────────
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cma_app') THEN
        CREATE ROLE cma_app LOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
END $$;

GRANT USAGE ON SCHEMA public TO cma_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO cma_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO cma_app;
-- audit_log is append-only for the app
REVOKE UPDATE, DELETE ON audit_log FROM cma_app;
-- tenant registry is admin-managed: app reads, doesn't write
REVOKE INSERT, UPDATE, DELETE ON tenants FROM cma_app;
-- resolver + claim functions
GRANT EXECUTE ON FUNCTION resolve_tenant_for_telegram(BIGINT) TO cma_app;
-- future tables/sequences default grants
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO cma_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO cma_app;


-- ─────────────────────────────────────────────────────────────
-- 2a. Claim the next pending query across all tenants
-- ─────────────────────────────────────────────────────────────
-- Atomic: pick oldest pending (FOR UPDATE SKIP LOCKED) → mark processing →
-- return the full row incl tenant_id. Bypasses RLS (SECURITY DEFINER).
CREATE OR REPLACE FUNCTION claim_next_query()
RETURNS queries
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $fn$
DECLARE
    picked_id BIGINT;
    result queries;
BEGIN
    SELECT id INTO picked_id
    FROM queries
    WHERE status = 'pending'
    ORDER BY asked_at ASC
    LIMIT 1
    FOR UPDATE SKIP LOCKED;

    IF picked_id IS NULL THEN
        RETURN NULL;
    END IF;

    UPDATE queries
    SET status = 'processing', started_at = NOW()
    WHERE id = picked_id
    RETURNING * INTO result;

    RETURN result;
END
$fn$;
REVOKE ALL ON FUNCTION claim_next_query() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION claim_next_query() TO cma_app;


-- ─────────────────────────────────────────────────────────────
-- 2b. Claim the next runnable ingestion job across all tenants
-- ─────────────────────────────────────────────────────────────
-- allowed: subset of runnable statuses ('pending','queued_analysis').
-- Transitions pending→transcribing, queued_analysis→analyzing.
CREATE OR REPLACE FUNCTION claim_next_ingestion_job(allowed TEXT[])
RETURNS ingestion_jobs
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $fn$
DECLARE
    picked_id BIGINT;
    picked_status TEXT;
    next_status TEXT;
    result ingestion_jobs;
BEGIN
    SELECT id, status INTO picked_id, picked_status
    FROM ingestion_jobs
    WHERE status = ANY(allowed)
    ORDER BY created_at ASC
    LIMIT 1
    FOR UPDATE SKIP LOCKED;

    IF picked_id IS NULL THEN
        RETURN NULL;
    END IF;

    next_status := CASE picked_status
        WHEN 'pending' THEN 'transcribing'
        WHEN 'queued_analysis' THEN 'analyzing'
    END;

    UPDATE ingestion_jobs
    SET status = next_status,
        started_at = COALESCE(started_at, NOW()),
        error_message = NULL,
        error_traceback = NULL
    WHERE id = picked_id
    RETURNING * INTO result;

    RETURN result;
END
$fn$;
REVOKE ALL ON FUNCTION claim_next_ingestion_job(TEXT[]) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION claim_next_ingestion_job(TEXT[]) TO cma_app;


INSERT INTO schema_version (version, description)
VALUES (5, 'MT Phase 1: cma_app role + cross-tenant claim functions')
ON CONFLICT (version) DO NOTHING;
