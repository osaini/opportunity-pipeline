-- A location found by a web search, on a page Python opened and checked, which
-- ranks below the company's own site and a Form D filing and above the deep
-- search's unchecked word.
--
-- location_basis carries a CHECK that neither SQLite nor PostgreSQL can widen
-- in place, so the column is rebuilt beside the old one and renamed over it.
-- Both engines support ALTER TABLE ... DROP COLUMN and RENAME COLUMN.
ALTER TABLE outreach_targets ADD COLUMN location_basis_v2 TEXT NOT NULL DEFAULT ''
    CHECK (location_basis_v2 IN ('', 'manual', 'company_site', 'sec_form_d', 'web_search', 'research'));
UPDATE outreach_targets SET location_basis_v2 = location_basis;
ALTER TABLE outreach_targets DROP COLUMN location_basis;
ALTER TABLE outreach_targets RENAME COLUMN location_basis_v2 TO location_basis;
