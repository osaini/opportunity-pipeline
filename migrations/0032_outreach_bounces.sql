-- An outreach email that bounced never reached anyone, so the target goes back
-- to Drafted (outreach_delivery.record_bounce) with the bounce kept here and in
-- its history. Columns rather than a new status: the status CHECK constraint
-- would need outreach_targets rebuilt, and dropping that table cascades into
-- its events, draft versions, and send claims.
--
-- bounced_at is the bounce still waiting on a new contact, cleared once an
-- email is sent again. bounced_addresses_json lists every address that ever
-- bounced for this target, and is kept for good, so an address that bounced
-- is never sent to again.
ALTER TABLE outreach_targets ADD COLUMN bounced_at TEXT;
ALTER TABLE outreach_targets ADD COLUMN bounce_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE outreach_targets ADD COLUMN bounced_addresses_json TEXT NOT NULL DEFAULT '[]';
