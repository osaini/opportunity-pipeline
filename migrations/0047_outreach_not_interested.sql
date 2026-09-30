-- When the student marked an outreach company not interested.
--
-- outreach_targets.not_interested_at is NULL for a company still in play. Set,
-- the company is kept (never deleted, so a deep search or an import still
-- finds it tracked and does not bring it back) but shows only under Not
-- interested, and no automation acts on it: no drafts, sends, follow-ups,
-- thank-yous, reminders, contact searches, or research.
--
-- The column is added by a Python step (schema._apply_outreach_not_interested),
-- guarded, so a crash before the migration is marked cannot make the next start
-- fail on a duplicate column.

SELECT 1;
