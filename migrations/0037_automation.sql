-- The automation ledger, its health, and its notices (opportunity_app/automation/ledger.py).
--
-- The columns this migration adds to existing tables (opportunity_interactions,
-- application_tasks, connector_accounts), and the 'automation_paused' row every
-- student starts with, are added by a Python step (schema._apply_automation),
-- each guarded, so a crash before the migration is marked cannot make the next
-- start fail on a duplicate column.
--
-- automation_actions holds every change automation made, proposed, or would
-- have made, so each one can be seen, reviewed, and undone. status is 'shadow'
-- (would have acted; never applied), 'pending', 'proposed', 'applied', 'undone',
-- 'rejected', 'superseded' (a later change touched the same fields; note says
-- which), 'failed', or 'expired'. Values are checked in Python, so a new status
-- needs no table rebuild. before_json and after_json hold only the fields the
-- action changes (fields_json). idempotency_key makes a re-read of the same
-- evidence act once. review is the student's verdict on a shadow row ('right'
-- or 'wrong'). decided_by is 'system' or 'student'.
CREATE TABLE IF NOT EXISTS automation_actions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    feature TEXT NOT NULL,
    action_type TEXT NOT NULL,
    subject_kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    status TEXT NOT NULL,
    fields_json TEXT NOT NULL DEFAULT '[]',
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    summary TEXT NOT NULL DEFAULT '',
    basis TEXT NOT NULL DEFAULT '',
    confidence REAL,
    policy_version TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT NOT NULL,
    review TEXT NOT NULL DEFAULT '',
    note TEXT NOT NULL DEFAULT '',
    undo_of TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    applied_at TEXT,
    decided_at TEXT,
    reviewed_at TEXT,
    decided_by TEXT NOT NULL DEFAULT '',
    UNIQUE(user_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_automation_actions_created
    ON automation_actions(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_automation_actions_status
    ON automation_actions(user_id, status);
CREATE INDEX IF NOT EXISTS idx_automation_actions_feature
    ON automation_actions(user_id, feature, created_at);

-- The last outcome of each background step ('inbox.replies',
-- 'automation.worker', ...), so a step that stops working is visible rather
-- than silent.
CREATE TABLE IF NOT EXISTS automation_health (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    component TEXT NOT NULL,
    last_ok_at TEXT,
    last_error_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    detail_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, component)
);

-- Notices for the student ("Turned off ... after two undos"), shown in the app
-- and, when they ask for it, as desktop pop-ups. event_key makes each notice
-- once-only. body never holds an email's text, a link, or an address.
-- level is 'info', 'warning', or 'problem'.
CREATE TABLE IF NOT EXISTS automation_notices (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_key TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    read_at TEXT,
    desktop_at TEXT,
    UNIQUE(user_id, event_key)
);
