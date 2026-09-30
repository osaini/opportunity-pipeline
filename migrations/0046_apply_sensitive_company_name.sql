-- The company name as the student typed it, beside the matching key.
--
-- apply_sensitive_answers.company_key is the words that identify an employer,
-- sorted and stripped of suffixes ("Zeta Alpha Labs, Inc." is "alpha labs zeta"),
-- which is right for matching and wrong to show back as the company a consent
-- covers. company_name keeps what the student typed (or what the role names) so
-- the list shows that. '' on a row with no company (any company).
--
-- The column is added by a Python step (schema._apply_apply_sensitive_company_name),
-- guarded, so a crash before the migration is marked cannot make the next start
-- fail on a duplicate column.

SELECT 1;
