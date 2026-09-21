-- Where an outreach target's location came from, and what the SEC says it raised.
--
-- location_basis, most authoritative first: manual (the student typed it),
-- company_site (the company's own site states it), sec_form_d (the issuer
-- address on its latest Form D), research (the deep search said so and nothing
-- has checked it). Locations recorded before this migration keep the only
-- basis that can be known: a deep search target's came from the search.
ALTER TABLE outreach_targets ADD COLUMN location_basis TEXT NOT NULL DEFAULT ''
    CHECK (location_basis IN ('', 'manual', 'company_site', 'sec_form_d', 'research'));
ALTER TABLE outreach_targets ADD COLUMN location_source_url TEXT NOT NULL DEFAULT '';
UPDATE outreach_targets
    SET location_basis = CASE WHEN origin = 'discovery' THEN 'research' ELSE 'manual' END
    WHERE location <> '';

-- The latest matching Form D, or the outcome of the last lookup, as JSON.
ALTER TABLE outreach_targets ADD COLUMN sec_form_d_json TEXT NOT NULL DEFAULT '';
-- When the company's site and SEC filings were last checked for this target.
ALTER TABLE outreach_targets ADD COLUMN profile_checked_at TEXT;

-- Companies the student deleted from outreach. The deep search never proposes
-- them again; adding one back by hand removes it from this list.
CREATE TABLE IF NOT EXISTS outreach_dismissed (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    company_key TEXT NOT NULL,
    company TEXT NOT NULL,
    domain TEXT NOT NULL DEFAULT '',
    dismissed_at TEXT NOT NULL,
    PRIMARY KEY (user_id, company_key)
);
