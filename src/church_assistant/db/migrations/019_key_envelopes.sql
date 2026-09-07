-- ═══════════════════════════════════════════════════════════════════════
-- Migration 019: envelopes — where the server keeps keys it cannot open
-- ═══════════════════════════════════════════════════════════════════════
--
-- Storage for the scheme in docs/cloud_plan.md §5.1, implemented in
-- shared/crypto.py. Nothing here is encrypted BY these tables; they hold the
-- envelopes that make encryption elsewhere usable.
--
-- THE ONE PROPERTY THIS SCHEMA MUST NOT LOSE. Every secret stored here is
-- addressed to somebody else. A KEK arrives sealed to a device's X25519 public
-- key; a DEK arrives sealed under the KEK. The database holds both and holds no
-- private key at all, so a full dump — the one this project takes nightly onto
-- a laptop — is a pile of ciphertext and letters nobody in possession of it can
-- read. If a future migration ever adds a column that would let the server
-- unwrap anything, that is the moment the whole design stops being true.
--
-- WHY RECOVERY IS A DEVICE. The printed key in the parish safe is an X25519
-- pair like any other, so it is a row in church_devices with kind='recovery'
-- rather than a special column somewhere. One code path, exercised daily by
-- real devices, is the only kind of recovery path that still works on the day
-- it is needed — cloud_plan §5.3.
--
-- WHY THE PROCESSING NODE IS ALSO A DEVICE, AND SAID OUT LOUD. The M1 must see
-- plaintext: it diarises audio and runs Gemma, and neither works over
-- ciphertext. So it holds the KEK and appears here as kind='processor'. This is
-- recorded in the schema rather than left as folklore, because it is the
-- difference between "the VPS cannot read your minutes" (true) and "nobody
-- outside the church can" (not true while processing runs on a laptop) —
-- cloud_plan §5.0, point 3.
--
-- Applied: 2026-09-__ (schema_version 20)
-- ═══════════════════════════════════════════════════════════════════════


-- ─────────────────────────────────────────────────────────────
-- 1. Recipients
-- ─────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS church_devices (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    -- 32 bytes of X25519. The private half lives in Android Keystore, on paper
    -- in a safe, or in the processing node's config — never here.
    public_key  BYTEA  NOT NULL CHECK (octet_length(public_key) = 32),

    kind        TEXT   NOT NULL
                       CHECK (kind IN ('device', 'recovery', 'processor')),

    -- "Телефон Романа". Free text and deliberately not a name field: the point
    -- is that a person revoking a device can tell which one it is.
    label       TEXT   NOT NULL DEFAULT '',

    -- Who paired it. NULL for the first device of a church and for recovery,
    -- which exist before anyone could have approved them.
    added_by    BIGINT REFERENCES web_users(id) ON DELETE SET NULL,

    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Revocation is a timestamp, not a delete: the envelopes addressed to this
    -- key stay for audit, and rotation is what actually removes access.
    revoked_at  TIMESTAMPTZ,

    UNIQUE (tenant_id, public_key)
);

CREATE INDEX IF NOT EXISTS idx_devices_active
    ON church_devices (tenant_id) WHERE revoked_at IS NULL;

COMMENT ON TABLE church_devices IS
    'Recipients a KEK can be sealed to: phones, the printed recovery key, the processing node.';


-- ─────────────────────────────────────────────────────────────
-- 2. The church key, wrapped once per recipient
-- ─────────────────────────────────────────────────────────────
--
-- One row per (device, version). Rotating the KEK writes a new set of rows for
-- the devices that remain and simply never writes one for the device that left
-- — which is what revocation is, mechanically.

CREATE TABLE IF NOT EXISTS key_envelopes (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    device_id   BIGINT NOT NULL REFERENCES church_devices(id) ON DELETE CASCADE,

    kek_version INT    NOT NULL CHECK (kek_version > 0),
    wrapped_kek BYTEA  NOT NULL,          -- crypto_box_seal to device.public_key

    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (device_id, kek_version)
);

COMMENT ON TABLE key_envelopes IS
    'The church KEK sealed to each recipient. The server stores these and cannot open one.';


-- ─────────────────────────────────────────────────────────────
-- 3. Data keys, wrapped by the church key
-- ─────────────────────────────────────────────────────────────
--
-- GRANULARITY IS ONE DEK PER MEETING (cloud_plan §5.2). Per-row DEKs buy
-- nothing — there is no case where one item of a protocol is readable and
-- another is not — and would turn rotation into millions of rewrites. Per
-- meeting, a decade of councils is a few hundred small blobs.
--
-- `scope` is the meeting date as text, or 'church' for rows that belong to no
-- single meeting. Text rather than a date column because the second kind exists.

CREATE TABLE IF NOT EXISTS dek_envelopes (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,

    scope       TEXT   NOT NULL,
    kek_version INT    NOT NULL CHECK (kek_version > 0),

    -- XChaCha20-Poly1305 under the KEK, AAD = context(tenant, 'dek', scope).
    -- Symmetric here, asymmetric one layer up: the KEK is a key the recipients
    -- share, so sealing a DEK to each device separately would store the same
    -- secret N times for no gain.
    wrapped_dek BYTEA  NOT NULL,

    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (tenant_id, scope, kek_version)
);

COMMENT ON TABLE dek_envelopes IS
    'Per-meeting data keys wrapped under the church KEK. Rotation rewrites these, never the content.';


-- ─────────────────────────────────────────────────────────────
-- 4. Which version is current
-- ─────────────────────────────────────────────────────────────
--
-- 0 means this church is not encrypted — the state every existing church is in
-- today, and the one they stay in until someone deliberately starts. Encryption
-- is per church precisely because the first one to try it should not decide for
-- the others.

ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS kek_version INT NOT NULL DEFAULT 0;

COMMENT ON COLUMN tenants.kek_version IS
    '0 = not encrypted. Otherwise the current KEK generation for this church.';


-- ─────────────────────────────────────────────────────────────
-- 5. RLS, like every other tenant-scoped table
-- ─────────────────────────────────────────────────────────────

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['church_devices', 'key_envelopes', 'dek_envelopes']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %I '
            'USING (tenant_id = current_setting(''app.current_tenant'', true)::bigint) '
            'WITH CHECK (tenant_id = current_setting(''app.current_tenant'', true)::bigint)',
            t);
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cma_app') THEN
            EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO cma_app', t);
        END IF;
    END LOOP;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cma_app') THEN
        GRANT USAGE, SELECT ON SEQUENCE
            church_devices_id_seq, key_envelopes_id_seq, dek_envelopes_id_seq
            TO cma_app;
    END IF;
END $$;


INSERT INTO schema_version (version, description)
VALUES (20, 'Key envelopes: the server stores letters addressed to devices, and no key of its own')
ON CONFLICT (version) DO NOTHING;
