-- Contact finding beyond the company's own pages, and what backs each guess.
--
-- Two new candidate methods:
--   site_person          a person the company's site names, with no address.
--                        These rows used to be stored as site_published with an
--                        empty email, which made "published on the site" count
--                        people as if they were addresses.
--   published_elsewhere  an address printed on a page other than the company's
--                        own site, which Python opened and checked. It stays
--                        unverified: the company did not publish it.
--
-- verification records what a mail server said about an address (never by
-- sending mail): accepted, rejected, accepts any address (catch_all), or no
-- usable answer. pattern_observed marks a guess built from a format the
-- company's own published addresses use. note explains the evidence in words.
-- None of these make a guess confirmed; confidence keeps its three values.
--
-- method carries a CHECK that neither engine can widen in place, so it is
-- rebuilt beside the old column and renamed over it, as in 0018.
ALTER TABLE outreach_contact_candidates ADD COLUMN method_v2 TEXT NOT NULL DEFAULT 'site_published'
    CHECK (method_v2 IN ('site_published', 'site_generic', 'site_person', 'pattern_guess',
                         'published_elsewhere', 'ai_research'));
UPDATE outreach_contact_candidates SET method_v2 = method;
UPDATE outreach_contact_candidates SET method_v2 = 'site_person' WHERE method = 'site_published' AND email = '';
ALTER TABLE outreach_contact_candidates DROP COLUMN method;
ALTER TABLE outreach_contact_candidates RENAME COLUMN method_v2 TO method;

ALTER TABLE outreach_contact_candidates ADD COLUMN verification TEXT NOT NULL DEFAULT ''
    CHECK (verification IN ('', 'smtp_accepted', 'smtp_rejected', 'catch_all', 'smtp_unknown'));
ALTER TABLE outreach_contact_candidates ADD COLUMN pattern_observed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE outreach_contact_candidates ADD COLUMN note TEXT NOT NULL DEFAULT '';

-- A second recipient, used when the To address is a guess: the company's
-- shared inbox in Cc, so a wrong guess still reaches the company once.
ALTER TABLE outreach_targets ADD COLUMN contact_cc TEXT NOT NULL DEFAULT '';
