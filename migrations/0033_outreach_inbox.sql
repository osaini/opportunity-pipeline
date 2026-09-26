-- Replies to outreach read from Gmail (opportunity_app/outreach_inbox.py).
--
-- outreach_inbox_messages holds every Gmail message the app has read for a
-- target, so none is logged twice: a reply, an automatic reply (out of
-- office), or one it passed over. target_id is empty for a message that
-- matched no target.
--
-- reply_suggestion_json is what the latest captured reply suggests beyond
-- Replied (declined, a call, an offer), waiting for the student to apply or
-- dismiss it. It is cleared whenever the status changes.
CREATE TABLE IF NOT EXISTS outreach_inbox_messages (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    gmail_id TEXT NOT NULL,
    target_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    sender TEXT NOT NULL DEFAULT '',
    received_at TEXT,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (user_id, gmail_id)
);

ALTER TABLE outreach_targets ADD COLUMN reply_suggestion_json TEXT NOT NULL DEFAULT '';
