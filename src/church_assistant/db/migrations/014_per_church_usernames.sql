-- ═══════════════════════════════════════════════════════════════════════
-- Migration 014: a login name belongs to a church, not to the server
-- ═══════════════════════════════════════════════════════════════════════
--
-- `web_users.username` was globally UNIQUE, and that was not an accident: the
-- login form carries nothing but a name and a password, so the name had to be
-- what told us which church to check the password against. With one church it
-- was invisible. With two it means churches compete for names — the second
-- church to want `pavlo` has to invent `pavlo2`, and a name is a thing that
-- belongs to a congregation, not to a server.
--
-- So the constraint moves down a level, and the question "which church?" gets
-- a new answer: the name when it is unambiguous, and the person themselves
-- when it is not. resolve_login_tenant below reports BOTH the tenant (when it
-- can be known) and how many accounts share the name, so the route can ask.
--
-- Asking happens BEFORE any password is verified. The tempting alternative —
-- check the password against every candidate and ask only if two match — pays
-- N password hashes for one guess, tests that guess against N different
-- people's secrets, and makes the question itself proof to whoever sees it
-- that the password was right. Ambiguity of the NAME is not a secret; validity
-- of a password is.
--
-- Applied: 2026-08-__ (schema_version 15)
-- ═══════════════════════════════════════════════════════════════════════


-- ─────────────────────────────────────────────────────────────
-- 1. The constraint moves from (username) to (tenant_id, username)
-- ─────────────────────────────────────────────────────────────
-- Safe in one direction by construction: everything that satisfied the global
-- constraint satisfies the composite one, so this cannot fail on live data.
-- Going BACK is a different matter — once two churches share a name, the old
-- constraint can no longer be recreated. That is the real point of no return
-- in this migration, and it is worth knowing before running it, not after.
ALTER TABLE web_users DROP CONSTRAINT IF EXISTS web_users_username_key;

CREATE UNIQUE INDEX IF NOT EXISTS idx_web_users_tenant_username
    ON web_users (tenant_id, username);

-- The dropped constraint was also the index login looked names up by. Without
-- a replacement every sign-in becomes a sequential scan of web_users — slow
-- later, and slow in a way nobody connects to this migration.
CREATE INDEX IF NOT EXISTS idx_web_users_username
    ON web_users (username);

COMMENT ON TABLE web_users IS
    'Web UI accounts. username is unique WITHIN a church; login asks which '
    'church when a name is shared (resolve_login_tenant).';


-- ─────────────────────────────────────────────────────────────
-- 2. The login resolver, replacing resolve_tenant_for_web_user
-- ─────────────────────────────────────────────────────────────
-- Same bootstrap problem as before (web_users is RLS-gated and login has no
-- tenant context yet), so the same answer: one SECURITY DEFINER function that
-- answers one narrow question. It still never returns a password hash — that
-- is read afterwards, inside the resolved tenant's context.
--
-- Returns (tenant_id, n_candidates):
--   n_candidates = active accounts carrying this name, anywhere
--   tenant_id    = the church to check against, or NULL when the caller must
--                  ask (ambiguous name, no church given / no church matched)
--
-- What is deliberately NOT filtered here: whether the church is active. A
-- suspended church's member must keep getting "this church is suspended"
-- rather than "no such account", and that decision lives in the route, where
-- it always has. It does mean a suspended church still makes its names
-- ambiguous — a simpler rule ("two accounts share this name") with fewer
-- surprises than one that changes with a church's status.
DROP FUNCTION IF EXISTS resolve_tenant_for_web_user(TEXT);

CREATE OR REPLACE FUNCTION resolve_login_tenant(
    p_username TEXT,
    p_church   TEXT DEFAULT NULL
)
RETURNS TABLE (tenant_id BIGINT, n_candidates INT)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public
AS $fn$
DECLARE
    v_church TEXT := NULLIF(lower(trim(coalesce(p_church, ''))), '');
    v_n      INT;
    v_tid    BIGINT;
BEGIN
    SELECT count(*) INTO v_n
      FROM web_users u
     WHERE u.username = p_username AND u.is_active;

    IF v_n = 0 THEN
        RETURN QUERY SELECT NULL::BIGINT, 0;
        RETURN;
    END IF;

    IF v_church IS NULL THEN
        -- One account, one church, nothing to ask: the overwhelmingly common
        -- case, and the one that must stay a single form and a single hash.
        IF v_n = 1 THEN
            SELECT u.tenant_id INTO v_tid
              FROM web_users u
             WHERE u.username = p_username AND u.is_active;
        END IF;

    ELSE
        -- The identifier first. It is unique, and it is the one thing about a
        -- church that never changes — renaming is a supported action, so a
        -- display name is a label, not an address.
        SELECT u.tenant_id INTO v_tid
          FROM web_users u
          JOIN tenants t ON t.id = u.tenant_id
         WHERE u.username = p_username AND u.is_active AND t.slug = v_church;

        -- Failing that, the display name — but only if it points at exactly
        -- one of the candidates. The aggregate with no GROUP BY returns a row
        -- only when HAVING holds, so two churches sharing a name leave v_tid
        -- NULL and the caller treats it as "no match", like any wrong answer.
        IF v_tid IS NULL THEN
            SELECT max(u.tenant_id) INTO v_tid
              FROM web_users u
              JOIN tenants t ON t.id = u.tenant_id
             WHERE u.username = p_username AND u.is_active
               AND lower(t.name) = v_church
            HAVING count(DISTINCT u.tenant_id) = 1;
        END IF;
    END IF;

    RETURN QUERY SELECT v_tid, v_n;
END
$fn$;

REVOKE ALL ON FUNCTION resolve_login_tenant(TEXT, TEXT) FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cma_app') THEN
        GRANT EXECUTE ON FUNCTION resolve_login_tenant(TEXT, TEXT) TO cma_app;
    END IF;
END $$;


INSERT INTO schema_version (version, description)
VALUES (15, 'MT: usernames unique per church; login asks which one when shared')
ON CONFLICT (version) DO NOTHING;
