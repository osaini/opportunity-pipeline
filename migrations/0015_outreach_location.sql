-- Where an outreach company is based, as its research states it ("San Francisco, CA").
-- Empty means no source said; the drafter never guesses a location.
ALTER TABLE outreach_targets ADD COLUMN location TEXT NOT NULL DEFAULT '';
