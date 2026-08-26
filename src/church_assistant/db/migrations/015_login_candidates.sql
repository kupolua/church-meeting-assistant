-- ═══════════════════════════════════════════════════════════════════════
-- Migration 015: the password answers first, the person only if it cannot
-- ═══════════════════════════════════════════════════════════════════════
--
-- 014 asked "which church?" whenever a login name was shared, before checking
-- anything. Correct, and in practice tiresome: two people who happen to share a
-- name almost never share a password, so the password already knew the answer
-- and we asked anyway. A member — who has no reason to have ever seen a church
-- identifier — was made to produce one for nothing.
--
-- So the resolver stops picking a church and starts reporting the candidates.
-- The route checks the password against every one of them, without stopping at
-- the first match, and asks only when TWO accounts answer to the same name and
-- the same password. That is the only case where the password genuinely cannot
-- say which person is signing in.
--
-- What this costs, stated plainly: one submitted password is now checked
-- against every account carrying that name, so the weakest password among
-- namesakes sets the strength of the name. It is paid for in the brake — the
-- failure budget in routes/auth.py counts password CHECKS, not submissions, so
-- N candidates spend the allowance N times as fast.
--
-- Still not returned from here: password hashes. The function reports which
-- churches to look in; each hash is read afterwards inside its own tenant's
-- context, exactly as before, so this call still leaks at most "such a username
-- exists".
--
-- Applied: 2026-08-__ (schema_version 16)
-- ═══════════════════════════════════════════════════════════════════════

DROP FUNCTION IF EXISTS resolve_login_tenant(TEXT, TEXT);

CREATE OR REPLACE FUNCTION login_tenants(
    p_username TEXT,
    p_church   TEXT DEFAULT NULL
)
RETURNS TABLE (tenant_id BIGINT)
LANGUAGE plpgsql
STABLE
SECURITY DEFINER
SET search_path = public
AS $fn$
DECLARE
    v_church TEXT := NULLIF(lower(trim(coalesce(p_church, ''))), '');
BEGIN
    -- No church named: every active account with this name is a candidate.
    -- Ordered so that two identical logins are answered identically, whatever
    -- order the rows happen to sit in.
    IF v_church IS NULL THEN
        RETURN QUERY
            SELECT u.tenant_id FROM web_users u
             WHERE u.username = p_username AND u.is_active
             ORDER BY u.tenant_id;
        RETURN;
    END IF;

    -- The identifier first: unique, and the one thing about a church that
    -- never changes (renaming is a supported action).
    RETURN QUERY
        SELECT u.tenant_id
          FROM web_users u JOIN tenants t ON t.id = u.tenant_id
         WHERE u.username = p_username AND u.is_active AND t.slug = v_church;
    IF FOUND THEN
        RETURN;
    END IF;

    -- Failing that, the display name — which is what members actually know.
    -- The aggregate with no GROUP BY yields a row only when HAVING holds, so
    -- two churches sharing a display name return nothing and the caller treats
    -- it as any other wrong answer.
    RETURN QUERY
        SELECT max(u.tenant_id)
          FROM web_users u JOIN tenants t ON t.id = u.tenant_id
         WHERE u.username = p_username AND u.is_active
           AND lower(t.name) = v_church
        HAVING count(DISTINCT u.tenant_id) = 1;
END
$fn$;

REVOKE ALL ON FUNCTION login_tenants(TEXT, TEXT) FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cma_app') THEN
        GRANT EXECUTE ON FUNCTION login_tenants(TEXT, TEXT) TO cma_app;
    END IF;
END $$;


INSERT INTO schema_version (version, description)
VALUES (16, 'MT: login checks every namesake; the church is asked only on a tie')
ON CONFLICT (version) DO NOTHING;
