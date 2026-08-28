-- ═══════════════════════════════════════════════════════════════════════
-- Migration 018: the protocol — the first thing here that cannot be re-derived
-- ═══════════════════════════════════════════════════════════════════════
--
-- A meeting has had a transcript and a topics digest since June. Neither is
-- minutes. What the PDF calls "Протокол зустрічі від …" is a heading, an
-- attendee list and topics — a useful digest of what was said, and not a
-- document a council can sign.
--
-- ⚠️ EVERYTHING ELSE IN THIS PROJECT IS DERIVED FROM THE AUDIO. Lose it, re-run
-- the pipeline, get it back. A protocol holds facts the recording never
-- contained and never will: an agenda entered BEFORE the meeting, a vote count,
-- the chair's edits, a person made responsible, a deadline, an approval. None
-- of it can be regenerated from anything. That is why it lives in the database
-- rather than beside the audio, and why the nightly dump is now the only thing
-- standing between a church and losing its minutes.
--
-- SHAPE (decided with Pavlo, 28.08):
--   номер · дата · голова(ведучий) · секретар · присутні · кворум
--   питання N: текст · Слухали · Вирішили · Голосували · Постановили[]
--   підписи
--
-- WHO MAY TOUCH IT. The chair is a property of THIS MEETING, not a third church
-- role: an admin assigns one, and only that person edits this protocol. Two
-- meetings can therefore be written up by two people at the same time, which a
-- church-wide "chair" role could not express. The secretary is a formal office,
-- not a permission — a name on the page and a line to sign on paper.
--
-- WHY A PROTOCOL MAY EXIST WITH NO MEETING FOLDER. The agenda is entered before
-- the meeting happens; the audio arrives hours after it ends. So the row is
-- keyed on (tenant, date) and is created first, standing alone until artifacts
-- turn up under the same date. It is also, incidentally, the first real record
-- of a meeting in this database — until now a meeting was a folder plus an
-- ingestion_jobs row.
--
-- Applied: 2026-08-__ (schema_version 19)
-- ═══════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS meeting_protocols (
    id            BIGSERIAL PRIMARY KEY,
    tenant_id     BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    meeting_date  DATE   NOT NULL,

    -- Number is "ДД-ММ-РРРР/N" where N runs within the year. Only N is stored:
    -- the rest is the date, and a number that repeats a column is a number that
    -- can disagree with it.
    seq           INT    NOT NULL,
    protocol_year INT    GENERATED ALWAYS AS (EXTRACT(YEAR FROM meeting_date)::int) STORED,

    chair_id      BIGINT REFERENCES web_users(id) ON DELETE SET NULL,
    secretary     TEXT   NOT NULL DEFAULT '',

    -- Confirmed by hand, deliberately. Diarization identifies voices, which is
    -- evidence of who spoke and not a record of who attended — a member present
    -- and silent is absent to it. A quorum line signed by a chair cannot rest
    -- on that.
    attendees     JSONB  NOT NULL DEFAULT '[]'::jsonb,
    quorum        TEXT   NOT NULL DEFAULT '',

    -- draft → review (circulated to participants) → approved (frozen).
    -- Approval is one-way: 15 (signing as an audited action) may add more, but
    -- nothing may add a way back, because a protocol that can be edited after
    -- it was approved is not minutes.
    status        TEXT   NOT NULL DEFAULT 'draft'
                         CHECK (status IN ('draft', 'review', 'approved')),
    approved_at   TIMESTAMPTZ,
    approved_by   BIGINT REFERENCES web_users(id) ON DELETE SET NULL,

    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by    TEXT   NOT NULL,               -- 'web:pavlo' — audit actor form
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (tenant_id, meeting_date)
);

-- Two protocols in one church may not carry the same number in the same year.
-- A unique index rather than a constraint because the year is generated.
CREATE UNIQUE INDEX IF NOT EXISTS idx_protocols_number
    ON meeting_protocols (tenant_id, protocol_year, seq);

COMMENT ON TABLE meeting_protocols IS
    'Formal minutes. The only entity here that cannot be regenerated from audio.';


CREATE TABLE IF NOT EXISTS protocol_items (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    protocol_id BIGINT NOT NULL REFERENCES meeting_protocols(id) ON DELETE CASCADE,
    position    INT    NOT NULL,

    question    TEXT   NOT NULL,                 -- the agenda item itself
    heard       TEXT   NOT NULL DEFAULT '',      -- Слухали  ← topics, grouped by a person
    resolved    TEXT   NOT NULL DEFAULT '',      -- Вирішили ← Gemma drafts, chair edits

    -- Typed by the chair afterwards. Not derived, and not derivable: on the
    -- recording a vote is a murmur, not a tally, and this is the field with the
    -- most formal weight and the least support in the data.
    votes_for     INT,
    votes_against INT,
    votes_abstain INT,

    -- 'not_considered' is set by the chair for an item the meeting never
    -- reached, or reached without deciding. It is meant to flow into "Стан
    -- справ" (backlog 14), which does not exist yet — so for now the status is
    -- recorded and leads nowhere. That is a decision, not an oversight: the
    -- tracker has other sources nobody has described.
    status      TEXT   NOT NULL DEFAULT 'considered'
                       CHECK (status IN ('considered', 'not_considered')),

    UNIQUE (protocol_id, position)
);

CREATE INDEX IF NOT EXISTS idx_protocol_items_protocol
    ON protocol_items (protocol_id, position);


-- Постановили — several rulings may come out of one question, each with its own
-- person and date, which is why this is a table and not three columns above.
CREATE TABLE IF NOT EXISTS protocol_rulings (
    id          BIGSERIAL PRIMARY KEY,
    tenant_id   BIGINT NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    item_id     BIGINT NOT NULL REFERENCES protocol_items(id) ON DELETE CASCADE,
    position    INT    NOT NULL,
    text        TEXT   NOT NULL,
    responsible TEXT   NOT NULL DEFAULT '',
    -- Free text, not a date. "до наступної зустрічі" and "коли Роман
    -- повернеться" are what councils actually say, and a DATE column would
    -- force the chair to invent a precision the decision did not have.
    due         TEXT   NOT NULL DEFAULT '',

    UNIQUE (item_id, position)
);

CREATE INDEX IF NOT EXISTS idx_protocol_rulings_item
    ON protocol_rulings (item_id, position);


-- RLS on all three, like every other tenant-scoped table.
DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['meeting_protocols', 'protocol_items', 'protocol_rulings']
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
            EXECUTE format(
                'GRANT SELECT, INSERT, UPDATE, DELETE ON %I TO cma_app', t);
        END IF;
    END LOOP;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cma_app') THEN
        GRANT USAGE, SELECT ON SEQUENCE
            meeting_protocols_id_seq, protocol_items_id_seq, protocol_rulings_id_seq
            TO cma_app;
    END IF;
END $$;


-- ── The freeze, in the database ──────────────────────────────────────
--
-- "Approved means it can no longer be edited" is the one promise this document
-- makes that a church could be harmed by. A check in the route would hold for
-- the route — and 016 is a fresh reminder of what happens to a guard that lives
-- only where someone remembered to put it: purge's protection of the founding
-- church was a filesystem comparison, and it stopped meaning anything the day
-- the folders moved, silently.
--
-- So it lives here, where a stray UPDATE, a future route, a fix applied by hand
-- in psql at midnight, and a well-meant admin panel all meet the same refusal.
--
-- Approval itself is an UPDATE, so the transition INTO approved has to pass:
-- the rule is about a protocol that already IS approved.

CREATE OR REPLACE FUNCTION refuse_editing_approved_protocol()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $fn$
BEGIN
    IF OLD.status = 'approved' THEN
        RAISE EXCEPTION
            'protocol % (%) is approved and cannot be changed',
            OLD.id, OLD.meeting_date;
    END IF;
    RETURN NEW;
END
$fn$;

-- ⚠️ UPDATE ONLY — NOT DELETE, and that was found by trying it. A DELETE guard
-- also fires on the CASCADE from tenants, so a church holding one approved
-- protocol could never be purged: 016 had just made the purge work for the
-- first time, and this would have broken it again for exactly the churches
-- that used the product. The retention promise ("removed after a year") would
-- have become quietly impossible, discovered the day someone tried.
--
-- So the rule is: THE FREEZE FORBIDS EDITING, NOT DESTRUCTION. Destroying a
-- church is governed by 016 — archived first, slug typed by hand, founding
-- corpus refused outright. Residual risk, stated so the next person does not
-- have to rediscover it: a future route that DELETEs a single item from an
-- approved protocol would not be stopped here and must check for itself.
DROP TRIGGER IF EXISTS trg_protocol_frozen ON meeting_protocols;
CREATE TRIGGER trg_protocol_frozen
    BEFORE UPDATE ON meeting_protocols
    FOR EACH ROW EXECUTE FUNCTION refuse_editing_approved_protocol();


-- The items and rulings carry no status of their own — they inherit the
-- protocol's. SECURITY DEFINER for the same reason 016's guard needed it: the
-- lookup crosses into meeting_protocols, whose RLS policy casts
-- current_setting('app.current_tenant') to bigint, and a statement issued
-- outside any tenant context would die on the empty string instead of
-- refusing — a guard indistinguishable from a broken table.
CREATE OR REPLACE FUNCTION refuse_editing_approved_protocol_child()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $fn$
DECLARE
    v_protocol BIGINT;
    v_status   TEXT;
BEGIN
    v_protocol := NEW.protocol_id;
    SELECT status INTO v_status FROM meeting_protocols WHERE id = v_protocol;
    IF v_status = 'approved' THEN
        RAISE EXCEPTION 'protocol % is approved; its items cannot be changed',
            v_protocol;
    END IF;
    RETURN NEW;
END
$fn$;

DROP TRIGGER IF EXISTS trg_protocol_items_frozen ON protocol_items;
CREATE TRIGGER trg_protocol_items_frozen
    BEFORE INSERT OR UPDATE ON protocol_items   -- not DELETE: see above
    FOR EACH ROW EXECUTE FUNCTION refuse_editing_approved_protocol_child();


-- Rulings hang off an item, so the protocol is one join away. Same function
-- shape, different lookup — inlined rather than generalised, because a guard
-- that has to be read to be trusted is better read straight through.
CREATE OR REPLACE FUNCTION refuse_editing_approved_ruling()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $fn$
DECLARE
    v_status TEXT;
BEGIN
    SELECT p.status INTO v_status
      FROM protocol_items i JOIN meeting_protocols p ON p.id = i.protocol_id
     WHERE i.id = NEW.item_id;
    IF v_status = 'approved' THEN
        RAISE EXCEPTION 'protocol is approved; its rulings cannot be changed';
    END IF;
    RETURN NEW;
END
$fn$;

DROP TRIGGER IF EXISTS trg_protocol_rulings_frozen ON protocol_rulings;
CREATE TRIGGER trg_protocol_rulings_frozen
    BEFORE INSERT OR UPDATE ON protocol_rulings   -- not DELETE: see above
    FOR EACH ROW EXECUTE FUNCTION refuse_editing_approved_ruling();


INSERT INTO schema_version (version, description)
VALUES (19, 'Protocol: formal minutes a council can sign, and the first table audio cannot rebuild')
ON CONFLICT (version) DO NOTHING;
