-- ═══════════════════════════════════════════════════════════════════════
-- Migration 020: the aggregate views must see the world as the reader, not as
-- their owner — otherwise a new church's dashboard counts another church's work
-- ═══════════════════════════════════════════════════════════════════════
--
-- Found 2026-10-15, the first time a second church was created: its empty
-- /ingest page showed «Готово: 6» — ReChurch's six finished meetings.
--
-- WHY. v_ingestion_depth, v_queue_depth and v_stats_today are plain views, and
-- a plain view runs with the privileges of its OWNER. They are owned by `cma`,
-- the superuser the schema is applied as, and a superuser is exempt from row
-- level security — FORCE ROW LEVEL SECURITY included. So the tenant context the
-- web sets for every request (app.current_tenant) never reached these three
-- counts: the comment above the repo functions, "RLS scopes the views
-- automatically", was an assumption nobody had been able to test with one
-- church.
--
-- Every other read in the application goes straight to the tables, where the
-- policies do apply; only the aggregates took this detour.
--
-- FIX. security_invoker (PostgreSQL 15+): the view body runs as the caller,
-- cma_app, for whom the policies hold. All three callers already open a tenant
-- cursor, so nothing in Python changes. A caller without tenant context — the
-- connection smoke test — now gets zero rows instead of everybody's, which is
-- the correct answer to a question asked outside any church.
--
-- v_latest_health is left alone: health_checks is platform-wide by design.
--
-- Applied: 2026-10-15 (schema_version 21)
-- ═══════════════════════════════════════════════════════════════════════

ALTER VIEW v_ingestion_depth SET (security_invoker = true);
ALTER VIEW v_queue_depth     SET (security_invoker = true);
ALTER VIEW v_stats_today     SET (security_invoker = true);

INSERT INTO schema_version (version, description)
VALUES (21, 'Aggregate views run as the invoker so RLS scopes them per church')
ON CONFLICT (version) DO NOTHING;
