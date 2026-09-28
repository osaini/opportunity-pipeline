-- Application mail (opportunity_app/application_inbox.py): job-system and
-- assessment emails read from Gmail, matched to applications, and acted on or
-- proposed through the automation ledger.
--
-- The columns this migration adds to existing tables (application_tasks.link,
-- monitored_events.decided_by) are added by a Python step
-- (schema._apply_application_mail), each guarded, so a crash before the
-- migration is marked cannot make the next start fail on a duplicate column.

-- One row per student: where live reading stands in Gmail.
--
-- history_id is the users.history.list cursor. It only moves in the same
-- transaction that stores the ids it covers in pending_ids_json, and an id
-- leaves pending_ids_json only in the transaction that records its
-- application_mail_messages row, so a pass cut short loses nothing.
-- recovery_* is the paginated messages.list that refills pending_ids_json
-- after Gmail forgets an old cursor (HTTP 404); recovery_history_id is the
-- cursor taken before it started, which live reading resumes from once it is
-- done. backfill_* is the separate, proposal-only look at the 60 days before
-- enabled_at: its own query, page token, and queue, never shared with live
-- reading or recovery. last_pass_at spaces passes at least ten minutes apart.
CREATE TABLE IF NOT EXISTS application_mail_sync (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    history_id TEXT NOT NULL DEFAULT '',
    pending_ids_json TEXT NOT NULL DEFAULT '[]',
    recovery_state TEXT NOT NULL DEFAULT '',
    recovery_after TEXT NOT NULL DEFAULT '',
    recovery_page_token TEXT NOT NULL DEFAULT '',
    recovery_history_id TEXT NOT NULL DEFAULT '',
    backfill_state TEXT NOT NULL DEFAULT '',
    backfill_query TEXT NOT NULL DEFAULT '',
    backfill_page_token TEXT NOT NULL DEFAULT '',
    backfill_ids_json TEXT NOT NULL DEFAULT '[]',
    enabled_at TEXT,
    last_ok_at TEXT,
    last_pass_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

-- Every message this feature read, once. state says what became of it:
-- 'done' (decided: acted on, proposed, or nothing to do), 'skipped' (not a
-- job-system email; nothing else about it is kept), 'outreach' (outreach owns
-- it), 'awaiting_resume' (read while automation was paused; decided again
-- after resume), or 'gone' (deleted before it could be read).
-- application_id is '' when no application matched. subject and
-- sender_domain are kept for the application's Emails list; never a body.
-- Kept apart from outreach_inbox_messages, whose 'ignored' rows would hide
-- job-system mail from this reader.
CREATE TABLE IF NOT EXISTS application_mail_messages (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    gmail_id TEXT NOT NULL,
    thread_id TEXT NOT NULL DEFAULT '',
    application_id TEXT NOT NULL DEFAULT '',
    event_id TEXT NOT NULL DEFAULT '',
    action_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '',
    matched_by TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'done',
    origin TEXT NOT NULL DEFAULT 'live',
    subject TEXT NOT NULL DEFAULT '',
    sender_domain TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (user_id, gmail_id)
);
CREATE INDEX IF NOT EXISTS idx_application_mail_messages_application
    ON application_mail_messages(user_id, application_id, received_at);
CREATE INDEX IF NOT EXISTS idx_application_mail_messages_state
    ON application_mail_messages(user_id, state);

-- A deadline an email states for an application ("complete the assessment
-- by October 3"). Its own table: opportunity_deadlines holds the one deadline
-- the student entered per role, and an email never overwrites it. quote is the
-- sentence the date came from, at most 160 characters.
CREATE TABLE IF NOT EXISTS email_deadlines (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    application_id TEXT NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    gmail_id TEXT NOT NULL,
    deadline_on TEXT NOT NULL,
    quote TEXT NOT NULL DEFAULT '',
    sender_domain TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(user_id, gmail_id, application_id)
);
CREATE INDEX IF NOT EXISTS idx_email_deadlines_user
    ON email_deadlines(user_id, deadline_on);

-- Mail domains a company uses, as far as the student has confirmed.
-- company_key is pipeline.identity_tokens(company), sorted and joined by
-- spaces. status is 'suggested' (never authorizes anything), 'trusted' (the
-- student said yes: mail from it may act on that company's applications), or
-- 'dismissed' (the student said no, so it is not suggested again). evidence is
-- a plain sentence saying where the suggestion came from.
CREATE TABLE IF NOT EXISTS employer_domains (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    company_key TEXT NOT NULL,
    company TEXT NOT NULL DEFAULT '',
    domain TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'suggested',
    source TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    UNIQUE(user_id, company_key, domain)
);
