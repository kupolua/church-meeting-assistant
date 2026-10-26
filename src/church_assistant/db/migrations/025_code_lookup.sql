-- 025: a sign-in code can be presented on its own, without the address.
--
-- THE PHONE ASKS FOR THE CODE ONLY (decision of 26.10.2026): the person
-- taps «Увійти» and types the six digits from the invitation mail — no
-- address field. The hash in code_hash folds the address in (021), so a
-- code alone could not be found; this second hash is of the code alone and
-- exists only to find the row and learn the address. code_hash is still
-- what the code is checked against.
--
-- What this costs: a live code is now a key by itself, so the route that
-- takes it throttles guesses per IP (api.verify). Six digits, a handful of
-- live codes, ten guesses a quarter hour — that is enough for a council's
-- minutes, and the address is still shown nowhere.

ALTER TABLE email_codes
    ADD COLUMN IF NOT EXISTS code_lookup TEXT;

CREATE INDEX IF NOT EXISTS idx_email_codes_lookup
    ON email_codes (code_lookup) WHERE consumed_at IS NULL;

COMMENT ON COLUMN email_codes.code_lookup IS
    'sha256 of the code alone, to find the row when the phone sends no address; NULL for codes issued before 025.';

INSERT INTO schema_version (version, description)
VALUES (26, 'email_codes.code_lookup — sign in with the code alone')
ON CONFLICT (version) DO NOTHING;
