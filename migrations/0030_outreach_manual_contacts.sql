-- An address the student found and typed in themselves, kept beside the ones
-- the site crawl and the web search found.
--
--   manual   the student added it. It is confirmed only when they say they
--            confirmed it (the person gave it to them, or it is published);
--            otherwise it stays unverified, like any other unchecked address.
--
-- method carries a CHECK that neither engine can widen in place, so it is
-- rebuilt beside the old column and renamed over it, as in 0018 and 0022.
ALTER TABLE outreach_contact_candidates ADD COLUMN method_v3 TEXT NOT NULL DEFAULT 'site_published'
    CHECK (method_v3 IN ('site_published', 'site_generic', 'site_person', 'pattern_guess',
                         'published_elsewhere', 'ai_research', 'manual'));
UPDATE outreach_contact_candidates SET method_v3 = method;
ALTER TABLE outreach_contact_candidates DROP COLUMN method;
ALTER TABLE outreach_contact_candidates RENAME COLUMN method_v3 TO method;
