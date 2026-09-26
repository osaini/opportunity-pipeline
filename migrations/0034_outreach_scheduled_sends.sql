-- Approved outreach emails queued to go out on the recipient's next weekday
-- morning (opportunity_app/outreach_schedule.py). One row per target and kind;
-- scheduling again replaces it.
--
-- fingerprint is the approved draft the student scheduled: if the words or the
-- recipient change, the send is cancelled rather than sending other words.
-- state is 'scheduled', 'sending' while the worker checks it (it can still be
-- cancelled), 'transmitting' once it is handed to Gmail (too late to cancel),
-- 'sent', 'cancelled', or 'failed' with the reason in error. label is the send time as
-- the student was shown it ("Tue, Sep 29, 9:12 AM CDT").
CREATE TABLE IF NOT EXISTS outreach_scheduled_sends (
    target_id TEXT NOT NULL REFERENCES outreach_targets(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    send_at TEXT NOT NULL,
    timezone TEXT NOT NULL,
    label TEXT NOT NULL,
    state TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (target_id, kind)
);
