-- =================================================================
-- Migration 005 — MT fix-ups for the remaining repos
-- =================================================================
-- (a) ingestion_jobs.meeting_date must be unique PER TENANT, not globally —
--     two churches can meet on the same date.
-- (b) SECURITY DEFINER helpers for the PLATFORM error-alert loop, which is
--     cross-tenant (alerts go to the platform owner): it must read unalerted
--     errors across all tenants and mark them, bypassing RLS.
--
-- Run as superuser. Idempotent.
-- =================================================================


-- ─────────────────────────────────────────────────────────────
-- (a) Per-tenant uniqueness for ingestion_jobs
-- ─────────────────────────────────────────────────────────────
DROP INDEX IF EXISTS idx_ingestion_meeting_date;
CREATE UNIQUE INDEX IF NOT EXISTS idx_ingestion_tenant_date
    ON ingestion_jobs(tenant_id, meeting_date);


-- ─────────────────────────────────────────────────────────────
-- (b) Cross-tenant error-alert loop (platform-level)
-- ─────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION list_unalerted_errors_all(p_limit INT DEFAULT 20)
RETURNS SETOF errors
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $fn$
    SELECT * FROM errors
    WHERE alerted_at IS NULL
    ORDER BY timestamp ASC
    LIMIT p_limit
$fn$;

CREATE OR REPLACE FUNCTION mark_error_alerted_any(p_id BIGINT)
RETURNS void
LANGUAGE sql
SECURITY DEFINER
SET search_path = public
AS $fn$
    UPDATE errors SET alerted_at = NOW() WHERE id = p_id
$fn$;

REVOKE ALL ON FUNCTION list_unalerted_errors_all(INT) FROM PUBLIC;
REVOKE ALL ON FUNCTION mark_error_alerted_any(BIGINT) FROM PUBLIC;
-- grant at deploy: GRANT EXECUTE ... TO cma_app;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cma_app') THEN
        GRANT EXECUTE ON FUNCTION list_unalerted_errors_all(INT) TO cma_app;
        GRANT EXECUTE ON FUNCTION mark_error_alerted_any(BIGINT) TO cma_app;
    END IF;
END $$;


INSERT INTO schema_version (version, description)
VALUES (6, 'MT fix-ups: per-tenant ingestion uniqueness + cross-tenant alert helpers')
ON CONFLICT (version) DO NOTHING;
