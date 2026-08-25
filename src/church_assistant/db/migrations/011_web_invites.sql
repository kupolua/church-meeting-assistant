-- =================================================================
-- Migration 011 — one-time invitation links
-- =================================================================
-- Registering a church used to end with a generated password shown on the
-- operator's screen, which they then had to send to the church somehow. That
-- makes the operator hold the church's credentials, and puts a standing secret
-- into a chat log where it stays readable forever.
--
-- An invite inverts it. The account is created with NO usable password and
-- inactive; the link is a short-lived, single-use secret that lets exactly one
-- person set that password once. After it is used the operator has no way in,
-- because there is nothing they ever knew.
--
-- The link still travels over some channel, so it is not magic — what changes
-- is its lifetime and its detectability. A password in a chat is valid until
-- somebody notices; an invite expires on a clock and, once used, cannot be used
-- again. If it IS intercepted, the legitimate recipient finds it consumed,
-- which is a signal a leaked password never gives.
--
-- Same shape as web_sessions (008), and for the same reason: the table holds
-- the token's HASH, never the token. A row that can be revoked beats a string
-- that promises to expire.
--
-- Run as superuser. Idempotent.
-- =================================================================

CREATE TABLE IF NOT EXISTS web_invites (
    id           BIGSERIAL PRIMARY KEY,
    tenant_id    BIGINT      NOT NULL REFERENCES tenants(id),
    web_user_id  BIGINT      NOT NULL REFERENCES web_users(id) ON DELETE CASCADE,
    -- sha256 of the token. The token itself exists once, in the response that
    -- created it, and is never stored anywhere.
    token_hash   TEXT        NOT NULL UNIQUE,
    created_by   TEXT        NOT NULL,          -- 'web:pavlo' — audit actor form
    expires_at   TIMESTAMPTZ NOT NULL,
    used_at      TIMESTAMPTZ,                   -- non-null = spent, refuse reuse
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_web_invites_user ON web_invites (web_user_id);

COMMENT ON TABLE web_invites IS
    'Single-use links that let an invited person set their own first password.';


-- RLS like every other tenant table. It buys less here than elsewhere — the
-- redeem path has no session and therefore no tenant context, so it goes
-- through the SECURITY DEFINER resolver below — but an invite is still one
-- church''s data, and the admin UI reads it under the usual policy.
ALTER TABLE web_invites ENABLE ROW LEVEL SECURITY;
ALTER TABLE web_invites FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON web_invites;
CREATE POLICY tenant_isolation ON web_invites
    USING (tenant_id = current_setting('app.current_tenant', true)::bigint)
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::bigint);


-- Redeeming happens BEFORE anyone is signed in: there is no session, so no
-- tenant context, so RLS cannot be satisfied. Same problem the login path has,
-- solved the same way — one SECURITY DEFINER function that answers a single
-- narrow question and hands back the tenant the caller may then set.
--
-- It deliberately returns nothing for an invite that is expired, already used,
-- or attached to a church that has been suspended. Every reason collapses into
-- "no row", so the caller cannot tell them apart and neither can a prober.
CREATE OR REPLACE FUNCTION resolve_web_invite(p_token_hash TEXT)
RETURNS TABLE (
    invite_id   BIGINT,
    tenant_id   BIGINT,
    tenant_slug TEXT,
    web_user_id BIGINT,
    username    TEXT,
    full_name   TEXT
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $fn$
    SELECT i.id, i.tenant_id, t.slug, u.id, u.username, u.full_name
    FROM web_invites i
    JOIN web_users u ON u.id = i.web_user_id
    JOIN tenants   t ON t.id = i.tenant_id
    WHERE i.token_hash = p_token_hash
      AND i.used_at IS NULL
      AND i.expires_at > NOW()
      AND t.is_active
    LIMIT 1
$fn$;

REVOKE ALL ON FUNCTION resolve_web_invite(TEXT) FROM PUBLIC;


-- Spending an invite has to do three things together — mark it used, set the
-- password, activate the account — or a crash between them leaves an account
-- nobody can reach and an invite nobody can spend again. One function, one
-- transaction, and the used_at check inside it so two simultaneous redeems
-- cannot both win.
CREATE OR REPLACE FUNCTION redeem_web_invite(
    p_token_hash TEXT,
    p_password_hash TEXT
)
RETURNS BIGINT                                  -- web_user_id, or NULL
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $fn$
DECLARE
    v_invite_id BIGINT;
    v_user_id   BIGINT;
BEGIN
    SELECT i.id, i.web_user_id INTO v_invite_id, v_user_id
    FROM web_invites i
    JOIN tenants t ON t.id = i.tenant_id
    WHERE i.token_hash = p_token_hash
      AND i.used_at IS NULL
      AND i.expires_at > NOW()
      AND t.is_active
    FOR UPDATE OF i;                            -- second redeemer waits, then finds used_at set

    IF v_invite_id IS NULL THEN
        RETURN NULL;
    END IF;

    UPDATE web_invites SET used_at = NOW() WHERE id = v_invite_id;
    UPDATE web_users
       SET password_hash = p_password_hash,
           is_active = TRUE
     WHERE id = v_user_id;

    RETURN v_user_id;
END
$fn$;

REVOKE ALL ON FUNCTION redeem_web_invite(TEXT, TEXT) FROM PUBLIC;


DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cma_app') THEN
        GRANT SELECT, INSERT, UPDATE ON TABLE web_invites TO cma_app;
        GRANT USAGE, SELECT ON SEQUENCE web_invites_id_seq TO cma_app;
        GRANT EXECUTE ON FUNCTION resolve_web_invite(TEXT) TO cma_app;
        GRANT EXECUTE ON FUNCTION redeem_web_invite(TEXT, TEXT) TO cma_app;
    END IF;
END $$;


INSERT INTO schema_version (version, description)
VALUES (12, 'MT: single-use invite links — the operator never holds a password')
ON CONFLICT (version) DO NOTHING;
