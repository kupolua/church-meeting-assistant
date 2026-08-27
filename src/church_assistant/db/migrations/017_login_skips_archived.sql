-- ═══════════════════════════════════════════════════════════════════════
-- Migration 017: a church in the archive stops holding a login name
-- ═══════════════════════════════════════════════════════════════════════
--
-- Found by hand, running the namesake test plan on 27.08. Archive a church, then
-- sign in as a namesake who still has an account elsewhere: the question "which
-- church?" still appears, and one of the answers is a church that no longer
-- exists. Answer it, and the reply is "Доступ до цієї церкви призупинено.
-- Зверніться до адміністратора" — a sentence in which both halves are false. The
-- church is not suspended, it is retired; there is no administrator to contact.
--
-- The cause is that 015 asks only about the ACCOUNT (u.is_active) and never
-- about the church. tenants_repo.archive() sets deleted_at and is_active on the
-- tenant row and deliberately leaves the accounts alone — so an archived
-- church's people stay "active" forever and go on occupying their names.
--
-- Three costs, and the third is the one that decided it:
--   1. A member of a live church is asked to identify a dead one.
--   2. `_finish_login` cannot tell archived from suspended, so it says the
--      wrong thing to whoever answers.
--   3. The failure budget in routes/auth.py counts password CHECKS, so a live
--      member spends attempts verifying against accounts in churches nobody
--      administers any more — and whose passwords will never be rotated,
--      because there is no one left to rotate them. Since 015 the weakest
--      password among namesakes sets the strength of the name; this quietly
--      admitted the abandoned ones into that set.
--
-- ⚠️ THE CONDITION IS deleted_at, NOT is_active, and the difference is the whole
-- migration. Three things have to stay true at once:
--
--   `_system` (tenant 0) is is_active = FALSE **by design** since 007, so that
--   nobody signs in "as the platform" for free. Filtering on is_active would
--   drop it from the candidate list and take the platform login with it — the
--   panel would become unreachable, and the failure would look like a wrong
--   password.
--
--   A SUSPENDED church must stay a candidate. Its members are meant to reach
--   `_finish_login` and be told, in as many words, that access is paused and to
--   contact their administrator — an administrator who exists, in a church that
--   is coming back. That message is the point of suspension, and the test plan
--   checks it (4.1, 4.2).
--
--   An ARCHIVED church must not. It is gone; its member now gets the ordinary
--   "Невірний логін або пароль", which is the same answer a church that never
--   held the name gives. That is deliberate and matches the rule the rest of
--   this function already follows: naming a church that does not hold your
--   login has to be indistinguishable from getting the password wrong,
--   otherwise the field becomes a way to ask "is this name in THAT church?".
--   Adding a third, distinguishable outcome for "archived" would reopen exactly
--   that.
--
-- Applied: 2026-08-__ (schema_version 18)
-- ═══════════════════════════════════════════════════════════════════════

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
    -- No church named: every active account in a church that still exists.
    -- The join is new in 017 — this branch had none at all, which is how an
    -- archived church went on being a candidate.
    IF v_church IS NULL THEN
        RETURN QUERY
            SELECT u.tenant_id
              FROM web_users u
              JOIN tenants t ON t.id = u.tenant_id
             WHERE u.username = p_username AND u.is_active
               AND t.deleted_at IS NULL
             ORDER BY u.tenant_id;
        RETURN;
    END IF;

    -- The identifier first: unique, and the one thing about a church that
    -- never changes (renaming is a supported action).
    RETURN QUERY
        SELECT u.tenant_id
          FROM web_users u JOIN tenants t ON t.id = u.tenant_id
         WHERE u.username = p_username AND u.is_active
           AND t.deleted_at IS NULL
           AND t.slug = v_church;
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
           AND t.deleted_at IS NULL
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
VALUES (18, 'MT: an archived church no longer holds a login name')
ON CONFLICT (version) DO NOTHING;
