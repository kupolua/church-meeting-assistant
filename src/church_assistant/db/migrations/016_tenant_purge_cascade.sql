-- ═══════════════════════════════════════════════════════════════════════
-- Migration 016: make purging a church possible, and make it impossible
--                to purge the wrong one
-- ═══════════════════════════════════════════════════════════════════════
--
-- scripts/purge_archived_tenants.py says it removes "database rows ON DELETE
-- CASCADE from tenants". No such cascade was ever created: all nine foreign
-- keys pointing at tenants were plain NO ACTION. So the purge has never once
-- run to completion — every church has audit_log and logs rows from its first
-- login, and the DELETE died on the first of them. It was found the only way
-- something like this is found: by someone finally trying to remove a church.
--
-- The obvious fix is CASCADE on all nine. On its own that fix is dangerous,
-- because those NO ACTION keys are currently the only thing standing between
-- `DELETE FROM tenants` and the entire corpus of the founding church.
--
-- ⚠️ THE GUARD THAT WAS SUPPOSED TO PROTECT IT NO LONGER FIRES. purge's legacy
-- check asks whether the church's artifact folder IS the shared data root —
-- true while `default` still lived directly in data/. Migration to
-- data/tenants/<slug>/ (e2a72e7, 25.08) moved it, so the condition is now false
-- for `default` and the refusal it guards has been unreachable since. A check
-- written against a filesystem layout stopped meaning anything when the layout
-- moved, silently, and nothing failed.
--
-- So the cascade does not ship alone. A trigger takes over the job the foreign
-- keys were doing by accident, and states it directly instead:
--
--   tenant 0   the platform. Deleting it takes every cross-church event and
--              the operator's own accounts.
--   tenant 1   the pre-multi-tenancy corpus — the founding church, and the
--              only one whose history predates this system's ability to
--              archive anything. Named by id, not by slug: renaming a church
--              is a supported action and must not be able to unprotect it.
--   live ones  a church that is not archived and still has people in it is not
--              deletable at all. Archiving is deliberate, reversible for a
--              year, and visible in the panel; deletion should only ever be
--              reachable from the far side of it.
--
-- What stays possible: deleting an ARCHIVED church (the purge), and deleting a
-- brand-new tenant that has no accounts (tenants_repo.delete_if_empty, which
-- undoes a half-finished registration and is the reason the guard is written
-- as "not archived AND has people" rather than a blanket "must be archived").
--
-- ⚠️ THE TRIGGER PROTECTS THE DATABASE, NOT THE FILES. purge removes Qdrant
-- collections and artifacts BEFORE it touches a row, so a refusal here arrives
-- after the irreplaceable half is already gone. The script therefore refuses
-- protected tenants itself, before step 1. This is the backstop, not the lock.
--
-- Applied: 2026-08-__ (schema_version 17)
-- ═══════════════════════════════════════════════════════════════════════

-- ── 1. The cascade the purge always assumed it had ───────────────────
--
-- Dropped and recreated by name: ALTER CONSTRAINT cannot change a delete
-- action. Names are the ones Postgres generated (<table>_tenant_id_fkey),
-- verified against the live database rather than guessed.

ALTER TABLE users          DROP CONSTRAINT IF EXISTS users_tenant_id_fkey;
ALTER TABLE users          ADD  CONSTRAINT users_tenant_id_fkey
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE;

ALTER TABLE queries        DROP CONSTRAINT IF EXISTS queries_tenant_id_fkey;
ALTER TABLE queries        ADD  CONSTRAINT queries_tenant_id_fkey
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE;

ALTER TABLE logs           DROP CONSTRAINT IF EXISTS logs_tenant_id_fkey;
ALTER TABLE logs           ADD  CONSTRAINT logs_tenant_id_fkey
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE;

ALTER TABLE errors         DROP CONSTRAINT IF EXISTS errors_tenant_id_fkey;
ALTER TABLE errors         ADD  CONSTRAINT errors_tenant_id_fkey
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE;

ALTER TABLE ingestion_jobs DROP CONSTRAINT IF EXISTS ingestion_jobs_tenant_id_fkey;
ALTER TABLE ingestion_jobs ADD  CONSTRAINT ingestion_jobs_tenant_id_fkey
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE;

-- The audit log cascades too, and that is not a hole in its append-only rule.
-- `cma_app` holds only INSERT and SELECT here, which is what stops the
-- application rewriting the history of a LIVE church — the thing the rule is
-- for. A purge is the deliberate erasure of a church we promised to erase, and
-- keeping records ABOUT a congregation whose data we undertook to remove would
-- contradict the promise rather than honour it. (Referential actions run
-- outside RLS and outside table privileges, which is also the only reason the
-- purge can reach these rows as `cma_app` at all.)
ALTER TABLE audit_log      DROP CONSTRAINT IF EXISTS audit_log_tenant_id_fkey;
ALTER TABLE audit_log      ADD  CONSTRAINT audit_log_tenant_id_fkey
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE;

ALTER TABLE web_users      DROP CONSTRAINT IF EXISTS web_users_tenant_id_fkey;
ALTER TABLE web_users      ADD  CONSTRAINT web_users_tenant_id_fkey
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE;

ALTER TABLE web_sessions   DROP CONSTRAINT IF EXISTS web_sessions_tenant_id_fkey;
ALTER TABLE web_sessions   ADD  CONSTRAINT web_sessions_tenant_id_fkey
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE;

ALTER TABLE web_invites    DROP CONSTRAINT IF EXISTS web_invites_tenant_id_fkey;
ALTER TABLE web_invites    ADD  CONSTRAINT web_invites_tenant_id_fkey
    FOREIGN KEY (tenant_id) REFERENCES tenants(id) ON DELETE CASCADE;


-- ── 2. The guard that replaces them ──────────────────────────────────

-- SECURITY DEFINER, like every other function here that has to see across
-- tenants. Without it the two counts below are evaluated under RLS, whose
-- policy casts current_setting('app.current_tenant') to bigint — and a DELETE
-- issued outside any tenant context (which is every purge, and any hand-written
-- psql) has that setting empty. The result was not a refusal but
-- `invalid input syntax for type bigint: ""`: a guard that fails with a type
-- error is a guard nobody can tell apart from a broken table.
CREATE OR REPLACE FUNCTION refuse_deleting_protected_tenant()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $fn$
DECLARE
    v_people BIGINT;
BEGIN
    IF OLD.id = 0 THEN
        RAISE EXCEPTION
            'tenant 0 (_system) is the platform itself and cannot be deleted';
    END IF;

    IF OLD.id = 1 THEN
        RAISE EXCEPTION
            'tenant 1 (%) is the founding corpus and cannot be deleted here; '
            'moving it is a migration done by hand, with a backup in reach',
            OLD.slug;
    END IF;

    IF OLD.deleted_at IS NULL THEN
        SELECT (SELECT count(*) FROM web_users WHERE tenant_id = OLD.id)
             + (SELECT count(*) FROM users     WHERE tenant_id = OLD.id)
          INTO v_people;
        IF v_people > 0 THEN
            RAISE EXCEPTION
                'church % is not archived and still has % account(s); '
                'archive it first — deletion is only reachable from there',
                OLD.slug, v_people;
        END IF;
    END IF;

    RETURN OLD;
END
$fn$;

DROP TRIGGER IF EXISTS trg_refuse_deleting_protected_tenant ON tenants;
CREATE TRIGGER trg_refuse_deleting_protected_tenant
    BEFORE DELETE ON tenants
    FOR EACH ROW
    EXECUTE FUNCTION refuse_deleting_protected_tenant();


INSERT INTO schema_version (version, description)
VALUES (17, 'MT: tenant delete cascades, and the founding church cannot be deleted')
ON CONFLICT (version) DO NOTHING;
