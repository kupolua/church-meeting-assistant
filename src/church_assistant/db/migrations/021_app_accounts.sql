-- ═══════════════════════════════════════════════════════════════════════
-- Migration 021: accounts that begin on a phone — an email, a code, and a
-- church that waits for the operator's yes
-- ═══════════════════════════════════════════════════════════════════════
--
-- Until now a church was founded from the platform panel and its first
-- administrator received a link by hand. The mobile app turns that around: a
-- person installs it, names their church, and proves they own an email address
-- by typing a six-digit code. Decided with Pavlo 23.10.2026:
--
--   1. the code goes out AT ONCE and only proves the address. It opens nothing;
--   2. the church appears with approval = 'pending'. Its founder can sign in
--      and sees «Запит розглядається»; every other screen stays shut;
--   3. the operator approves from the platform panel — or, one day, a paid
--      subscription flips the same column without anyone's hand.
--
-- The point of the order: what reaches the operator's list is a LIVE address
-- with a few words from a real person, not a typo or a joke.
--
-- WHAT STAYS. `is_active` keeps meaning what it meant — the operator's
-- suspension lever, checked by resolve_web_session. A pending church is active
-- in that sense (its founder must be able to sign in to read the answer), and
-- the app gate reads `approval` instead. One column, two switches, and the
-- payment path and the approval path end in the same UPDATE.
--
-- The app signs in by email, so web_users gains one. The old web login (name +
-- password) is untouched; a founder who wants the browser later gets a link
-- from the panel, exactly as before.
--
-- Sessions are the same table. An app token is a web_sessions row with
-- client = 'app' and a year-long cap: the account page already lists where a
-- person is signed in, and revoking a phone should be the same gesture as
-- revoking a browser.
--
-- Applied: 2026-10-23 (schema_version 22)
-- ═══════════════════════════════════════════════════════════════════════

-- ─── 1. A church may wait ────────────────────────────────────
ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS approval TEXT NOT NULL DEFAULT 'active'
        CHECK (approval IN ('pending', 'active', 'rejected')),
    ADD COLUMN IF NOT EXISTS requested_by TEXT,         -- founder's email
    ADD COLUMN IF NOT EXISTS request_note TEXT,         -- their few words
    ADD COLUMN IF NOT EXISTS approved_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS approved_by  TEXT;         -- 'web:pavlo' | 'payment'

COMMENT ON COLUMN tenants.approval IS
    'pending = asked from the app, waiting for the operator (or a payment); '
    'active = may be used; rejected = refused, kept for the record. '
    'Orthogonal to is_active, which is suspension.';

CREATE INDEX IF NOT EXISTS idx_tenants_pending
    ON tenants (created_at) WHERE approval = 'pending';

-- ─── 2. Accounts have an address ─────────────────────────────
ALTER TABLE web_users ADD COLUMN IF NOT EXISTS email TEXT;

-- One address per church — a person may well sit on two councils.
CREATE UNIQUE INDEX IF NOT EXISTS idx_web_users_tenant_email
    ON web_users (tenant_id, lower(email)) WHERE email IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_web_users_email
    ON web_users (lower(email)) WHERE email IS NOT NULL;

-- ─── 3. Which client holds a session ─────────────────────────
ALTER TABLE web_sessions
    ADD COLUMN IF NOT EXISTS client TEXT NOT NULL DEFAULT 'web'
        CHECK (client IN ('web', 'app'));

-- ─── 4. The codes ────────────────────────────────────────────
-- Not tenant-scoped: at the moment a code is issued there may be no church yet.
-- The code itself is never stored — its hash is — and a row dies after a few
-- minutes or a few wrong guesses, whichever comes first. The founding form
-- travels in `payload` so that nothing is created until the address is proven.
CREATE TABLE IF NOT EXISTS email_codes (
    id          BIGSERIAL PRIMARY KEY,
    email       TEXT NOT NULL,                      -- lower-cased
    purpose     TEXT NOT NULL CHECK (purpose IN ('signup', 'login')),
    code_hash   TEXT NOT NULL,
    payload     JSONB NOT NULL DEFAULT '{}'::jsonb, -- church_name, full_name, note
    attempts    INT  NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at  TIMESTAMPTZ NOT NULL,
    consumed_at TIMESTAMPTZ,
    ip          TEXT
);

CREATE INDEX IF NOT EXISTS idx_email_codes_live
    ON email_codes (email, created_at DESC) WHERE consumed_at IS NULL;

COMMENT ON TABLE email_codes IS
    'Six-digit email codes for the app. Hash only; short-lived; a few attempts.';

-- ─── 5. Who answers to this address? ─────────────────────────
-- The app has no tenant when a person types their email, so this is the same
-- SECURITY DEFINER bootstrap as login_tenants: candidates, not a decision.
-- Archived churches are not candidates; pending and suspended ones ARE, because
-- the app has to be able to say why the door is shut.
CREATE OR REPLACE FUNCTION app_accounts_by_email(p_email TEXT)
RETURNS TABLE (
    tenant_id     BIGINT,
    tenant_slug   TEXT,
    tenant_name   TEXT,
    approval      TEXT,
    tenant_active BOOLEAN,
    web_user_id   BIGINT,
    full_name     TEXT,
    role          TEXT
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public
AS $fn$
    SELECT t.id, t.slug, t.name, t.approval, t.is_active,
           u.id, u.full_name, u.role
      FROM web_users u
      JOIN tenants t ON t.id = u.tenant_id
     WHERE lower(u.email) = lower(trim(p_email))
       AND u.is_active
       AND t.deleted_at IS NULL
       AND t.id <> 0
     ORDER BY t.id
$fn$;

REVOKE ALL ON FUNCTION app_accounts_by_email(TEXT) FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cma_app') THEN
        GRANT EXECUTE ON FUNCTION app_accounts_by_email(TEXT) TO cma_app;
        -- email_codes is created by the migrating superuser; the default
        -- privileges from 004 cover it, this is belt and braces.
        GRANT SELECT, INSERT, UPDATE, DELETE ON email_codes TO cma_app;
        GRANT USAGE, SELECT ON SEQUENCE email_codes_id_seq TO cma_app;
    END IF;
END $$;

INSERT INTO schema_version (version, description)
VALUES (22, 'App accounts: email on web_users, email codes, church approval, session client')
ON CONFLICT (version) DO NOTHING;
