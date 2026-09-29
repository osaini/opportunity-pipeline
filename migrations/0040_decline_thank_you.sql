-- A thank-you after a plain decline (opportunity_app/outreach_thank_you.py).
--
-- The column this migration adds to an existing table (outreach_events.detail_json,
-- what an event records beside its text: for a reply read from Gmail, its ids,
-- who wrote it, and both readings of it, the rules' and Jev's) is added by a
-- Python step (schema._apply_decline_thank_you), guarded, so a crash before the
-- migration is marked cannot make the next start fail on a duplicate column.
--
-- outreach_thank_yous holds, per company, the one thank-you the app planned
-- after a decline: at most one per company, ever (the primary key), whatever
-- became of it. It answers the person who wrote the decline (to_email,
-- to_name), in their thread (thread_id; reply_message_id is their RFC
-- Message-ID, for In-Reply-To and References). generated_by is the model that
-- wrote it, or 'template'. fingerprint covers the recipient, the words and the
-- thread, so a send checks it is sending exactly what was shown. state is
-- 'planned', 'scheduled', 'sending' (the student's own Send it anyway),
-- 'transmitting' (handed to Gmail), 'sent', 'cancelled', 'held' (a check before
-- sending stopped it; note says why), or 'failed'. Values are checked in
-- Python, so a new state needs no table rebuild. send_at and label are when it
-- was planned to go, as outreach_scheduled_sends words it.
CREATE TABLE IF NOT EXISTS outreach_thank_yous (
    target_id TEXT PRIMARY KEY REFERENCES outreach_targets(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    reply_gmail_id TEXT NOT NULL DEFAULT '',
    reply_message_id TEXT NOT NULL DEFAULT '',
    thread_id TEXT NOT NULL DEFAULT '',
    to_email TEXT NOT NULL DEFAULT '',
    to_name TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    generated_by TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'planned',
    note TEXT NOT NULL DEFAULT '',
    send_at TEXT,
    label TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outreach_thank_yous_state
    ON outreach_thank_yous(user_id, state);
