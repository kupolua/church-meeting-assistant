-- =================================================================
-- Migration 007 — dedicated `_system` tenant (MT Phase 3, follow-up)
-- =================================================================
-- Platform events (worker.started, health warnings, "Ollama unreachable")
-- belong to no church, but every tenant-scoped table is RLS-gated and needs a
-- tenant_id. Until now they were filed under tenant 1 — the first real church —
-- which put the platform's operational noise into a congregation's log and,
-- worse, made it visible in that church's dashboard.
--
-- WHY id = 0. The id has to be a compile-time constant: shared/logger.py must
-- pick it before any tenant context exists, and it is explicitly a component
-- that "must never crash" — it cannot do an async registry lookup, and a lazy
-- cached lookup would add a failure mode to the one thing that has to keep
-- working while other things break. BIGSERIAL would hand out a different id in
-- every deployment, so the row is inserted with an explicit reserved id
-- instead. 0 is a natural sentinel: no real church can be tenant zero.
--
-- is_active = FALSE is not "suspended" here, it is load-bearing: the web login
-- already refuses inactive tenants (web/routes/auth.py), so nobody can ever log
-- in "as the platform" — for free, with no extra check to remember. For the
-- same reason it never appears in tenants_repo.list_active().
--
-- NOT BACKFILLED. Pre-existing system events under tenant 1 are left where they
-- are: nothing distinguishes "worker.started" from a church's own INFO rows
-- reliably enough to move them automatically, and audit/log history is meant to
-- be append-only. This takes effect for events written from now on.
--
-- Run as superuser. Idempotent.
-- =================================================================


INSERT INTO tenants (id, slug, name, is_active)
VALUES (0, '_system', 'System (platform events)', FALSE)
ON CONFLICT (id) DO NOTHING;

COMMENT ON TABLE tenants IS
    'One row per church/council. id=0 is the reserved platform tenant '
    '(slug _system) that owns cross-church system events; id=1 is the '
    'pre-multi-tenancy corpus.';

-- The sequence must stay ahead of the real churches; id=0 is below every
-- generated value, so re-assert rather than assume.
SELECT setval(
    pg_get_serial_sequence('tenants', 'id'),
    GREATEST((SELECT MAX(id) FROM tenants), 1)
);


INSERT INTO schema_version (version, description)
VALUES (8, 'MT: dedicated _system tenant (id 0) for platform events')
ON CONFLICT (version) DO NOTHING;
