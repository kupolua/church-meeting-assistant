-- 026: topic_corrections — how the council rewords what the model wrote.
--
-- WHY. On 15.11.2026 the council read the first «Теми» in the app and said
-- the text sounds like an office report («Регулювання поведінки Валентина»
-- where they say «Оцінка поведінки Валентина»). The user's idea was to
-- fine-tune Gemma on the church's own language. Fine-tuning needs hundreds
-- of «model wrote → people say» pairs, and there were none. So: a glossary
-- in the prompt now (shared/style.py), and from now on every edit of a topic
-- title or body in the app is kept here as a pair. When there are enough,
-- they become examples in the prompt first, training data later.
--
-- What is stored is the text before and after, not a diff: a pair must be
-- readable on its own months later, by a person or a training script.
-- The file (polished.md) is still the source of truth for what the topic
-- says now; this table is the memory of how it got there.
--
-- Applied: dev 15.11.2026; VPS — див. docs/session_2026-11-15.md (schema_version 27)

CREATE TABLE IF NOT EXISTS topic_corrections (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    meeting_date  DATE   NOT NULL,
    topic_order   INT    NOT NULL,                         -- position of the ### section, 0-based
    field         TEXT   NOT NULL CHECK (field IN ('title', 'body')),
    before        TEXT   NOT NULL,
    after         TEXT   NOT NULL,
    web_user_id   BIGINT REFERENCES web_users(id) ON DELETE SET NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (before <> after)
);

CREATE INDEX IF NOT EXISTS idx_topic_corrections_tenant_time
    ON topic_corrections (tenant_id, created_at DESC);

COMMENT ON TABLE topic_corrections IS
    'Every edit of a topic title/body made in the app: the model''s text and the council''s. '
    'Raw material for prompt examples and, later, fine-tuning.';

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['topic_corrections']
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
VALUES (27, 'topic_corrections — the council''s rewording of model text, kept as pairs')
ON CONFLICT (version) DO NOTHING;
