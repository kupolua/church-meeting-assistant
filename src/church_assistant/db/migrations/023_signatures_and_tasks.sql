-- ═══════════════════════════════════════════════════════════════════════
-- Migration 023: signatures on a protocol, and the rulings that become work
-- ═══════════════════════════════════════════════════════════════════════
--
-- Step 4 of the app (24.10.2026) — the two asks from the October meeting
-- that depend on each other: participants sign the protocol from their
-- phones, and once it is approved every "Постановили" line becomes a task in
-- «Стан справ».
--
-- A SIGNATURE IS OVER CONTENT, NOT OVER A ROW. The phone signs the SHA-256 of
-- a canonical rendering of the protocol (shared/protocol_hash.py) with the
-- Ed25519 key it registered (022). The hash is stored beside the signature:
-- if the chair edits the protocol afterwards, the hash of the new text
-- differs, and every signature on the old hash is visibly for an older text
-- — stale, kept, never deleted. Approval freezes the text (018's trigger), so
-- an approved protocol's signatures stay current forever.
--
-- A TASK IS A RULING THAT LEFT THE MEETING. One row per ruling, created when
-- the protocol is approved (from the web or from the app), keyed on the
-- ruling so it is created once. The text, the responsible and the due are
-- copied, not referenced: the protocol is frozen, the task moves on — it gets
-- a status, a person who closed it, a note.
--
-- Applied: 2026-10-24 (schema_version 24)
-- ═══════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS protocol_signatures (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    protocol_id   BIGINT NOT NULL REFERENCES meeting_protocols(id) ON DELETE CASCADE,
    web_user_id   BIGINT NOT NULL REFERENCES web_users(id) ON DELETE CASCADE,
    key_id        BIGINT NOT NULL REFERENCES signing_keys(id) ON DELETE RESTRICT,
    content_hash  TEXT   NOT NULL,                       -- sha256 hex of the canonical protocol
    signature     BYTEA  NOT NULL CHECK (octet_length(signature) = 64),
    signed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- One signature per person per text. A re-signing after an edit is a new
    -- row with the new hash; the old one stays as history.
    UNIQUE (protocol_id, web_user_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_protocol_signatures_protocol
    ON protocol_signatures (protocol_id, signed_at);

COMMENT ON TABLE protocol_signatures IS
    'Ed25519 signatures of members over the canonical hash of a protocol. '
    'A signature whose hash is not the current one is stale, not deleted.';

CREATE TABLE IF NOT EXISTS tasks (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    protocol_id   BIGINT NOT NULL REFERENCES meeting_protocols(id) ON DELETE CASCADE,
    ruling_id     BIGINT UNIQUE REFERENCES protocol_rulings(id) ON DELETE SET NULL,
    meeting_date  DATE   NOT NULL,
    text          TEXT   NOT NULL,
    responsible   TEXT   NOT NULL DEFAULT '',
    due           TEXT   NOT NULL DEFAULT '',
    status        TEXT   NOT NULL DEFAULT 'new'
                         CHECK (status IN ('new', 'in_progress', 'done', 'cancelled')),
    note          TEXT   NOT NULL DEFAULT '',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by    BIGINT REFERENCES web_users(id) ON DELETE SET NULL,
    done_at       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_tasks_open
    ON tasks (tenant_id, status, meeting_date DESC) WHERE status IN ('new', 'in_progress');

COMMENT ON TABLE tasks IS
    '«Стан справ»: one row per ruling of an approved protocol. Text copied, '
    'status lives here. ruling_id is what makes creation idempotent.';

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['protocol_signatures', 'tasks']
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
            EXECUTE format('GRANT USAGE, SELECT ON SEQUENCE %I_id_seq TO cma_app', t);
        END IF;
    END LOOP;
END $$;

INSERT INTO schema_version (version, description)
VALUES (24, 'protocol_signatures (Ed25519 over the canonical hash) and tasks («Стан справ»)')
ON CONFLICT (version) DO NOTHING;
