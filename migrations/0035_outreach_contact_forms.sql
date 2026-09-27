-- The contact form on a company's own site, for companies that publish no
-- email address (opportunity_app/outreach_forms.py). One row per target.
--
-- page_url is the company page that holds the form, found by the same crawl
-- that looks for addresses. fields_json is what the plain HTML showed (labels,
-- types, which are required); the submitter reads the live page again, since
-- many forms are built by scripts. captcha names the widget seen, if any.
--
-- state is 'found' until a submission is tried, then 'submitted' (the page
-- confirmed it), 'unconfirmed' (the form was sent but the page did not say it
-- arrived: never sent again automatically), 'needs_you' (nothing was sent: a
-- CAPTCHA challenge or a field the app cannot answer; note says which), or
-- 'failed' (nothing was sent; note says why).
CREATE TABLE IF NOT EXISTS outreach_contact_forms (
    target_id TEXT PRIMARY KEY REFERENCES outreach_targets(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    page_url TEXT NOT NULL,
    fields_json TEXT NOT NULL DEFAULT '[]',
    captcha TEXT NOT NULL DEFAULT '',
    accepts_file INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'found',
    note TEXT NOT NULL DEFAULT '',
    found_at TEXT NOT NULL,
    attempted_at TEXT,
    updated_at TEXT NOT NULL
);
