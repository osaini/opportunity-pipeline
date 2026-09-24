-- One-word industry tags per company (opportunity_app/company_tags.py).
--
-- company_tags holds the automatic tags, rebuilt from the postings on every
-- sync; `evidence` says which words each one was inferred from, so the UI can
-- show that a tag is a classification rather than something the employer said.
-- Tags are keyed by the company's stored fold (opportunities.company_sort_key),
-- not by an opportunity, so every posting from one company shares them.
--
-- company_tag_choices holds one student's own edits, and survives every
-- rebuild: 'removed' hides an automatic tag, 'added' adds one of their own.
-- The Python step registered in schema.py backfills tags for existing data.
CREATE TABLE IF NOT EXISTS company_tags (
    company_key TEXT NOT NULL,
    tag TEXT NOT NULL,
    score INTEGER NOT NULL,
    evidence TEXT NOT NULL DEFAULT '',
    generated_at TEXT NOT NULL,
    PRIMARY KEY (company_key, tag)
);

CREATE INDEX IF NOT EXISTS company_tags_by_tag ON company_tags(tag, company_key);

CREATE TABLE IF NOT EXISTS company_tag_choices (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    company_key TEXT NOT NULL,
    tag TEXT NOT NULL,
    choice TEXT NOT NULL CHECK (choice IN ('added', 'removed')),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, company_key, tag)
);

CREATE INDEX IF NOT EXISTS company_tag_choices_by_tag ON company_tag_choices(user_id, tag, company_key);
