-- Outreach provenance and local-calendar preferences.
ALTER TABLE notification_preferences ADD COLUMN timezone_explicit INTEGER NOT NULL DEFAULT 0;
UPDATE notification_preferences SET timezone_explicit=1 WHERE timezone <> 'UTC';

ALTER TABLE outreach_targets ADD COLUMN research_confidence TEXT NOT NULL DEFAULT 'confirmed'
    CHECK (research_confidence IN ('confirmed', 'unverified'));
UPDATE outreach_targets SET research_confidence='unverified' WHERE origin='discovery';

ALTER TABLE outreach_targets ADD COLUMN follow_up_claims_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE outreach_targets ADD COLUMN follow_up_generated_by TEXT NOT NULL DEFAULT '';
