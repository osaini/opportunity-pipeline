-- Which version of the tagging rules produced the rows in company_tags.
--
-- One row. ensure_product_schema compares its fingerprint with the rules in
-- opportunity_app/company_tags.py on every startup and rebuilds every
-- automatic tag when they differ, so a rule edit (a new tag, a keyword fix)
-- reaches existing data without waiting for the next refresh. A student's own
-- tag choices are separate and survive the rebuild.
CREATE TABLE IF NOT EXISTS company_tag_rules (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    fingerprint TEXT NOT NULL,
    generated_at TEXT NOT NULL
);
