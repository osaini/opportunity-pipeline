-- Notes for the call once a company writes back: what they do, talking points,
-- what the student could bring, questions to ask, and blanks to fill in on the
-- call. call_prep is the editable text; the claims record the basis of every
-- generated line, as a draft's do, so a note never passes off a guess as fact.
ALTER TABLE outreach_targets ADD COLUMN call_prep TEXT NOT NULL DEFAULT '';
ALTER TABLE outreach_targets ADD COLUMN call_prep_claims_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE outreach_targets ADD COLUMN call_prep_generated_by TEXT NOT NULL DEFAULT '';
ALTER TABLE outreach_targets ADD COLUMN call_prep_generated_at TEXT;
