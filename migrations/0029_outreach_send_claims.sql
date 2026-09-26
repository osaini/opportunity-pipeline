-- One row per outreach email the app is sending, has sent, or is writing to
-- Gmail Drafts, so each email kind can go out at most once.
--
-- The primary key is the lock: a second request for the same target and kind
-- fails to insert, on SQLite and PostgreSQL alike, before it can reach Gmail.
-- state is 'drafting' or 'sending' while a request holds the row, 'sent' once
-- Gmail confirmed the send (kept for good), and 'unconfirmed' when Gmail may or
-- may not have acted (a timeout or a 5xx), which asks the student to check
-- their Sent (and Drafts) folders before the app sends anything else.
-- instance is the server process that holds the row, so a row left behind by
-- a process that died is told apart from one whose request is still running
-- (opportunity_app/outreach_gmail.py).
CREATE TABLE IF NOT EXISTS outreach_send_claims (
    target_id TEXT NOT NULL REFERENCES outreach_targets(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    token TEXT NOT NULL,
    state TEXT NOT NULL,
    action TEXT NOT NULL,
    instance TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    PRIMARY KEY (target_id, kind)
);
