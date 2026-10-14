-- The student's answer to a company's reply, drafted for them to approve (opportunity_app/outreach/reply_drafts.py).
--
-- One row per company: the answer it is waiting on now. purpose is 'answer' (to the reply logged most recently) or
-- 'revisit' (the note to send when the revisit date the student set arrives). draft_id names one written draft for one
-- message: it changes only when a draft answers a different message or a different revisit date, never when the
-- student edits or regenerates the words, so the send claim (outreach_send_claims, kind 'reply:<draft_id>') still holds
-- an earlier try Gmail may have carried out. It answers the person who wrote (to_email, to_name, and the reply's other
-- recipients in cc), in their thread (thread_id; reply_message_id is their RFC Message-ID, for In-Reply-To and
-- References). reply_gmail_id is the Gmail id of the reply it answers (a 'revisit' draft: their latest reply when it
-- was written), or that reply's event id when it was pasted in rather than read from Gmail. reading is what their
-- reply was read as (paused, declined, call_scheduled, replied). claims_json is what each statement rests on, as a
-- first email's are. revisit_on and revisit_said are the date their reply names for
-- getting back in touch and their own words for it, only when their words name one; revisit_for is the revisit date a
-- 'revisit' draft was written for. state is 'generated', 'approved', 'sending' (the student's confirmed Send),
-- 'sent', 'in_gmail' (moved to the student's Gmail Drafts to finish there), 'dismissed', 'stale' (they wrote again, or
-- the student answered in Gmail) or 'skipped' (an automatic pass found it already answered; note says why). Values are
-- checked in Python, so a new state needs no table rebuild.
CREATE TABLE IF NOT EXISTS outreach_reply_drafts (
    target_id TEXT PRIMARY KEY REFERENCES outreach_targets(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    draft_id TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT 'answer',
    reading TEXT NOT NULL DEFAULT '',
    reply_event_id TEXT NOT NULL DEFAULT '',
    reply_gmail_id TEXT NOT NULL DEFAULT '',
    reply_message_id TEXT NOT NULL DEFAULT '',
    thread_id TEXT NOT NULL DEFAULT '',
    to_email TEXT NOT NULL DEFAULT '',
    to_name TEXT NOT NULL DEFAULT '',
    cc TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    claims_json TEXT NOT NULL DEFAULT '[]',
    generated_by TEXT NOT NULL DEFAULT '',
    comments TEXT NOT NULL DEFAULT '',
    revisit_on TEXT,
    revisit_said TEXT NOT NULL DEFAULT '',
    revisit_for TEXT,
    state TEXT NOT NULL DEFAULT 'generated',
    note TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL DEFAULT '',
    approved_at TEXT,
    sent_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outreach_reply_drafts_state
    ON outreach_reply_drafts(user_id, state);
