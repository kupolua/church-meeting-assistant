-- 024: where a protocol item and a ruling came from in the recording.
--
-- THE AGENDA STOPPED BEING THE SKELETON (decision of 24.10.2026). Small
-- churches have no agenda — it lives in the pastor's head — and larger ones
-- do not follow theirs. So the protocol is now assembled AFTER the meeting
-- from the topics the analysis found in the recording: a topic becomes an
-- item, a line of it becomes a ruling. Two pointers make that round trip
-- possible:
--
--   protocol_items.topic_order   which topic of polished.md this item came
--                                from (0-based, as meetings_index numbers
--                                them). NULL = typed by hand, the old way.
--                                One item per topic per protocol, so adding
--                                the same topic twice finds the first.
--   protocol_rulings.tc          the timecode in the recording where this was
--                                said ("1:09:59"); '' when nobody knows.
--
-- Neither is content. A timecode is a pointer into the audio and the topic
-- number is bookkeeping, so neither is sealed for an encrypted church and
-- neither enters the canonical hash (shared/protocol_hash.py): re-pointing a
-- ruling at the right minute is not a changed decision, and must not make
-- signatures stale.
--
-- «Вирішили» vs «Обговорювали» is NOT a column, on purpose: an item with a
-- resolution or a ruling was decided, one without was discussed. Derived on
-- read, so it cannot disagree with the text.

ALTER TABLE protocol_items
    ADD COLUMN IF NOT EXISTS topic_order INT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_protocol_items_topic
    ON protocol_items (protocol_id, topic_order)
    WHERE topic_order IS NOT NULL;

ALTER TABLE protocol_rulings
    ADD COLUMN IF NOT EXISTS tc TEXT NOT NULL DEFAULT '';

COMMENT ON COLUMN protocol_items.topic_order IS
    'Topic of polished.md this item was made from (0-based); NULL = typed by hand.';
COMMENT ON COLUMN protocol_rulings.tc IS
    'Timecode in the recording where the decision was said; not content, not hashed.';

INSERT INTO schema_version (version, description)
VALUES (25, 'protocol_items.topic_order and protocol_rulings.tc — the protocol assembled from topics')
ON CONFLICT (version) DO NOTHING;
