-- Apply for me (opportunity_app/apply_runs.py): the tables behind the Greenhouse apply agent.
--
-- The columns this migration adds to existing tables
-- (application_mail_messages.sender_verified,
-- generated_document_artifacts.content_sha256) are added by a Python step
-- (schema._apply_apply_agent), each guarded, so a crash before the migration
-- is marked cannot make the next start fail on a duplicate column.

-- One row per submit or Finish in browser attempt. Rows are never deleted
-- (except with the account): an attempt that is abandoned becomes a 'released'
-- tombstone, so the limits and the duplicate checks can still see it.
--
-- The two partial unique indexes are the locks, in the spirit of
-- outreach_send_claims: at most one live attempt per application, and at most
-- one live attempt per Greenhouse job (job_ref), so two saved copies of one
-- posting cannot both be submitted. A 'submitted' row keeps both for good.
-- state describes the click; verification describes the confirmation email
-- afterwards. instance is the server process that holds the row, so a claim a
-- dead process left is told apart from one still being worked (heartbeat_at).
-- handed_over_at is set in the hand-over transaction and never cleared: every
-- attempt that reached Greenhouse keeps counting toward the limits.
-- stage_policy is fixed at claim time so no later change can reinterpret it.
-- note and detail_json never hold a field value.
CREATE TABLE IF NOT EXISTS application_submit_claims (
    token TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    opportunity_id TEXT NOT NULL,
    instance TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('one_click', 'handoff', 'unattended')),
    state TEXT NOT NULL CHECK (state IN
        ('claimed', 'clicking', 'submitted', 'unconfirmed', 'needs_you', 'failed', 'released')),
    after_click INTEGER NOT NULL DEFAULT 0 CHECK (after_click IN (0, 1)),
    ats TEXT NOT NULL,
    board_token TEXT NOT NULL,
    job_ref TEXT NOT NULL,
    company_key TEXT NOT NULL,
    stage_policy TEXT NOT NULL CHECK (stage_policy IN ('record', 'ask', 'ledger')),
    plan_hash TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    confirmed_at TEXT,
    handed_over_at TEXT,
    heartbeat_at TEXT NOT NULL,
    cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
    verification TEXT NOT NULL DEFAULT '' CHECK (verification IN
        ('', 'awaiting_email', 'email_confirmed', 'no_email_24h', 'not_watched')),
    watch_until TEXT,
    submitted_at TEXT,
    verified_at TEXT,
    stage_recorded INTEGER NOT NULL DEFAULT 0 CHECK (stage_recorded IN (0, 1)),
    resolved_by TEXT NOT NULL DEFAULT '' CHECK (resolved_by IN ('', 'page', 'email', 'student')),
    note TEXT NOT NULL DEFAULT '',
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_submit_claims_live_application
    ON application_submit_claims(application_id) WHERE state <> 'released';
CREATE UNIQUE INDEX IF NOT EXISTS ux_submit_claims_live_job
    ON application_submit_claims(user_id, ats, job_ref) WHERE state <> 'released';
CREATE INDEX IF NOT EXISTS idx_submit_claims_user_state ON application_submit_claims(user_id, state);
CREATE INDEX IF NOT EXISTS idx_submit_claims_handed_over ON application_submit_claims(user_id, handed_over_at);
CREATE INDEX IF NOT EXISTS idx_submit_claims_company ON application_submit_claims(user_id, company_key, handed_over_at);
CREATE INDEX IF NOT EXISTS idx_submit_claims_board ON application_submit_claims(user_id, ats, board_token, handed_over_at);
CREATE INDEX IF NOT EXISTS idx_submit_claims_verification ON application_submit_claims(user_id, verification, watch_until);

-- Every option lookup, rehearsal, submit and Finish in browser run. A lookup
-- or rehearsal has no application (looking changes nothing in the tracker),
-- so it is keyed by opportunity_id. plan_json holds the plan without values:
-- each field's value is kept only as an HMAC (value_mac), and screenshots_json
-- keeps a file's path only until retention deletes the file (its sha256 stays).
-- refused_json and requests_json hold a method, host, path and status, never a
-- query string or a body.
CREATE TABLE IF NOT EXISTS apply_runs (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    opportunity_id TEXT NOT NULL,
    application_id TEXT REFERENCES applications(id) ON DELETE CASCADE,
    claim_token TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL CHECK (kind IN ('lookup', 'rehearsal', 'submit', 'handoff')),
    started_by TEXT NOT NULL CHECK (started_by IN ('student', 'worker')),
    ats TEXT NOT NULL,
    adapter_version TEXT NOT NULL,
    company_key TEXT NOT NULL,
    board_token TEXT NOT NULL,
    page_url TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'finished')),
    outcome TEXT NOT NULL DEFAULT '' CHECK (outcome IN
        ('', 'looked_up', 'rehearsed', 'submitted', 'unconfirmed', 'needs_you', 'failed')),
    clean INTEGER NOT NULL DEFAULT 0 CHECK (clean IN (0, 1)),
    reasons_json TEXT NOT NULL DEFAULT '[]',
    plan_json TEXT NOT NULL DEFAULT '[]',
    plan_hash TEXT NOT NULL DEFAULT '',
    options_json TEXT NOT NULL DEFAULT '{}',
    progress_json TEXT NOT NULL DEFAULT '[]',
    screenshots_json TEXT NOT NULL DEFAULT '[]',
    refused_json TEXT NOT NULL DEFAULT '[]',
    requests_json TEXT NOT NULL DEFAULT '[]',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    confirm_nonce_sha256 TEXT NOT NULL DEFAULT '',
    nonce_used_at TEXT,
    review TEXT NOT NULL DEFAULT '' CHECK (review IN ('', 'right', 'wrong')),
    review_note TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT,
    heartbeat_at TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_apply_runs_opportunity ON apply_runs(user_id, opportunity_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_apply_runs_ats ON apply_runs(user_id, ats, kind, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_apply_runs_status ON apply_runs(status, heartbeat_at);

-- What the student allowed the app to answer on a sensitive question
-- (work authorization, sponsorship, 18 or older, EEO stored only as a decline,
-- acknowledgments, consents), each with the exact question, the answer, and
-- the consent it was given under. Read only by the apply policy code, never by
-- the extension, employer views or any report. The unique key uses
-- question_hash because a privacy statement can be longer than a btree index
-- row may be on PostgreSQL. company_key '' means any company, which is not
-- allowed for a statement that cites a document. salary is in the CHECK so the
-- table never needs a migration if it is allowed later.
CREATE TABLE IF NOT EXISTS apply_sensitive_answers (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    category TEXT NOT NULL CHECK (category IN ('work_authorization', 'sponsorship', 'age_18',
        'export_control', 'eeo_gender', 'eeo_hispanic', 'eeo_race', 'eeo_veteran', 'eeo_disability',
        'acknowledgment', 'consent', 'salary')),
    question_text TEXT NOT NULL,
    question_key TEXT NOT NULL,
    question_hash TEXT NOT NULL,
    answer_kind TEXT NOT NULL CHECK (answer_kind IN ('option', 'options', 'text', 'checkbox')),
    answer TEXT NOT NULL,
    company_key TEXT NOT NULL DEFAULT '',
    statement_links_json TEXT NOT NULL DEFAULT '[]',
    consent_scope TEXT NOT NULL CHECK (consent_scope IN ('confirmed', 'unattended')),
    consented_at TEXT NOT NULL,
    last_used_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, question_hash, company_key)
);

-- The exact option label the student picked once for a typeahead list (school,
-- location, degree, ...). Kept apart from profile_facts, whose flat map other
-- features read: an ATS's spelling of a school must not leak into them.
CREATE TABLE IF NOT EXISTS apply_ats_labels (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ats TEXT NOT NULL,
    field TEXT NOT NULL,
    label TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    PRIMARY KEY (user_id, ats, field)
);
