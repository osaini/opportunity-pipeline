-- Cold outreach to companies that have no posting in the opportunity feed.
-- Kept apart from `opportunities`/`applications` on purpose: those rows must
-- trace back to a real posting source, and a cold lead has none.
CREATE TABLE IF NOT EXISTS outreach_targets (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    company TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT '',
    priority TEXT NOT NULL DEFAULT 'P2' CHECK (priority IN ('P1', 'P2', 'P3')),
    website TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    fit_rationale TEXT NOT NULL DEFAULT '',
    activity_signal TEXT NOT NULL DEFAULT '',
    contact_name TEXT NOT NULL DEFAULT '',
    contact_role TEXT NOT NULL DEFAULT '',
    contact_email TEXT NOT NULL DEFAULT '',
    contact_linkedin TEXT NOT NULL DEFAULT '',
    contact_route TEXT NOT NULL DEFAULT '',
    contact_confidence TEXT NOT NULL DEFAULT 'unknown'
        CHECK (contact_confidence IN ('confirmed', 'unverified', 'unknown')),
    status TEXT NOT NULL DEFAULT 'not_started'
        CHECK (status IN ('not_started', 'drafted', 'sent', 'followed_up', 'replied',
                          'call_scheduled', 'offer', 'declined', 'no_response', 'paused')),
    deadline_label TEXT NOT NULL DEFAULT '',
    deadline_date TEXT,
    email_subject TEXT NOT NULL DEFAULT '',
    email_body TEXT NOT NULL DEFAULT '',
    sent_at TEXT,
    follow_up_at TEXT,
    notes TEXT NOT NULL DEFAULT '',
    source_urls_json TEXT NOT NULL DEFAULT '[]',
    researched_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, company)
);

CREATE INDEX IF NOT EXISTS idx_outreach_targets_user
    ON outreach_targets(user_id, status, follow_up_at);

CREATE TABLE IF NOT EXISTS outreach_events (
    id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL REFERENCES outreach_targets(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outreach_events_target
    ON outreach_events(target_id, created_at DESC);
