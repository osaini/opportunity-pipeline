-- Why the student set an outreach company aside: they applied through its own site.
--
-- outreach_targets.not_interested_at is the "set aside" time: set, the company is kept (never deleted, so a deep
-- search or an import still finds it tracked) and no automation acts on it. This column says why. '' is the original
-- reason, not interested; 'applied_directly' means the student filled in the company's application form themselves,
-- so nothing here should write to it (no first email, contact form, follow-up, or thank-you) as if it were a cold
-- contact. Companies already set aside keep '' and stay Not interested.
--
-- The column is added by a Python step (schema._apply_outreach_applied_directly), guarded, so a crash before the
-- migration is marked cannot make the next start fail on a duplicate column.

SELECT 1;
