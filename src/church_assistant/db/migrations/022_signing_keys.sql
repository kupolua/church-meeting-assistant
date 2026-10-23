-- ═══════════════════════════════════════════════════════════════════════
-- Migration 022: a person's signing key — the phone that will sign protocols
-- ═══════════════════════════════════════════════════════════════════════
--
-- Step 2 of the app (23.10.2026). A protocol is approved when the people who
-- were in the room sign it from their phones. For that signature to mean
-- anything later, the server has to know, before any protocol exists, which
-- public key belongs to which member — registered at a moment when the person
-- has just proven their address, from the device they will sign with.
--
-- NOT church_devices (019). That table holds X25519 keys that RECEIVE sealed
-- data (device, recovery, processor) and belongs to no person. This one holds
-- Ed25519 keys that SIGN, and each belongs to exactly one web_user. Same shape
-- of row, different question — "who may read" versus "who said yes" — and
-- folding them would make revoking a phone's reading right also strike its
-- past signatures, or the reverse.
--
-- The private half never leaves the phone: generated there, kept in the
-- device's secure storage behind the screen lock, used only after the person
-- passes the fingerprint or face prompt. The server stores 32 bytes it can
-- verify with and cannot sign with.
--
-- Revocation is a timestamp: a signature made while the key was live stays
-- valid, which is the whole point of writing down WHEN.
--
-- Applied: 2026-10-23 (schema_version 23)
-- ═══════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS signing_keys (
    id           BIGSERIAL PRIMARY KEY,
    tenant_id    BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    web_user_id  BIGINT NOT NULL REFERENCES web_users(id) ON DELETE CASCADE,

    algorithm    TEXT   NOT NULL DEFAULT 'ed25519' CHECK (algorithm IN ('ed25519')),
    public_key   BYTEA  NOT NULL CHECK (octet_length(public_key) = 32),

    -- "Pixel 8 · Android 15" — so a person with two phones can tell which one
    -- they are revoking. Free text from the device.
    label        TEXT   NOT NULL DEFAULT '',

    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ
);

-- The same key may not be registered twice in a church (a re-install sends a
-- new one; a replay of the old registration must not create a second row).
CREATE UNIQUE INDEX IF NOT EXISTS idx_signing_keys_unique
    ON signing_keys (tenant_id, public_key);
CREATE INDEX IF NOT EXISTS idx_signing_keys_user
    ON signing_keys (web_user_id) WHERE revoked_at IS NULL;

COMMENT ON TABLE signing_keys IS
    'Ed25519 public keys of members'' phones, one per device, for signing protocols. '
    'Private halves live on the phones. Revoked by timestamp, never deleted.';

ALTER TABLE signing_keys ENABLE ROW LEVEL SECURITY;
ALTER TABLE signing_keys FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON signing_keys;
CREATE POLICY tenant_isolation ON signing_keys
    USING (tenant_id = current_setting('app.current_tenant', true)::bigint)
    WITH CHECK (tenant_id = current_setting('app.current_tenant', true)::bigint);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cma_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON signing_keys TO cma_app;
        GRANT USAGE, SELECT ON SEQUENCE signing_keys_id_seq TO cma_app;
    END IF;
END $$;

INSERT INTO schema_version (version, description)
VALUES (23, 'signing_keys: members'' phone keys for signing protocols')
ON CONFLICT (version) DO NOTHING;
