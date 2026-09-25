-- Tags for outreach companies, inferred from the student's own research.
--
-- An outreach company usually has no posting, so company_tags (built from
-- postings, shared by everyone) has nothing for it. These rows come from the
-- company's research summary instead, which belongs to one student, so they
-- are kept per student: one student's private research never tags a company
-- for another. company_tag_choices applies to both kinds alike.
--
-- outreach_tag_state records what the rows were built from, so the Outreach
-- list rebuilds them only when a company, summary, or research status changed
-- (opportunity_app/company_tags.py sync_outreach_tags).
CREATE TABLE IF NOT EXISTS outreach_company_tags (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    company_key TEXT NOT NULL,
    tag TEXT NOT NULL,
    score INTEGER NOT NULL,
    evidence TEXT NOT NULL DEFAULT '',
    generated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, company_key, tag)
);

CREATE INDEX IF NOT EXISTS outreach_company_tags_by_tag ON outreach_company_tags(user_id, tag, company_key);

CREATE TABLE IF NOT EXISTS outreach_tag_state (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    signature TEXT NOT NULL,
    generated_at TEXT NOT NULL
);
