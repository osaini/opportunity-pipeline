PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT,
    display_name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT 'student',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profiles (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    profile_json TEXT NOT NULL DEFAULT '{}',
    confirmed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_credentials (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    password_hash TEXT NOT NULL,
    password_salt TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recovery_challenges (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'used', 'expired')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS profile_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    field_path TEXT NOT NULL,
    value_json TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'user',
    confirmed INTEGER NOT NULL DEFAULT 0 CHECK (confirmed IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, field_path)
);

CREATE TABLE IF NOT EXISTS resume_files (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    original_name TEXT NOT NULL,
    media_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    storage_path TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, sha256)
);

CREATE TABLE IF NOT EXISTS resume_versions (
    id TEXT PRIMARY KEY,
    resume_file_id TEXT NOT NULL REFERENCES resume_files(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    extracted_text TEXT NOT NULL,
    parsed_json TEXT NOT NULL DEFAULT '{}',
    confirmed_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'confirmed')),
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    UNIQUE(resume_file_id, user_id)
);

CREATE TABLE IF NOT EXISTS opportunities (
    id TEXT PRIMARY KEY,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    region TEXT NOT NULL DEFAULT 'Unknown',
    role_type TEXT NOT NULL DEFAULT 'other',
    url TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    posted_at TEXT,
    deadline_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    fingerprint TEXT NOT NULL DEFAULT '',
    content_fingerprint TEXT NOT NULL DEFAULT '',
    duplicate_of TEXT REFERENCES opportunities(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_opportunities_active_score
    ON opportunities(active, duplicate_of, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_opportunities_company_title
    ON opportunities(company, title);

CREATE TABLE IF NOT EXISTS opportunity_attributes (
    opportunity_id TEXT PRIMARY KEY REFERENCES opportunities(id) ON DELETE CASCADE,
    remote_mode TEXT NOT NULL DEFAULT 'unknown' CHECK (remote_mode IN ('remote', 'hybrid', 'onsite', 'unknown')),
    terms_json TEXT NOT NULL DEFAULT '[]',
    graduation_years_json TEXT NOT NULL DEFAULT '[]',
    pay_min REAL,
    pay_max REAL,
    pay_period TEXT NOT NULL DEFAULT '',
    currency TEXT NOT NULL DEFAULT '',
    extracted_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_opportunity_attributes_filters
    ON opportunity_attributes(remote_mode, pay_period, pay_max);

CREATE TABLE IF NOT EXISTS opportunity_sources (
    opportunity_id TEXT NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    source_key TEXT NOT NULL,
    source_name TEXT NOT NULL,
    external_id TEXT NOT NULL,
    source_url TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (opportunity_id, source_key, external_id)
);

CREATE TABLE IF NOT EXISTS fit_scores (
    opportunity_id TEXT NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ruleset_version TEXT NOT NULL,
    score INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
    explanation_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    PRIMARY KEY (opportunity_id, user_id, ruleset_version)
);

CREATE TABLE IF NOT EXISTS opportunity_interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id TEXT NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    action TEXT NOT NULL CHECK (action IN ('seen', 'saved', 'passed', 'apply_opened', 'undo')),
    created_at TEXT NOT NULL,
    UNIQUE(opportunity_id, user_id, action, created_at)
);

CREATE TABLE IF NOT EXISTS action_requests (
    idempotency_key TEXT NOT NULL,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    opportunity_id TEXT NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS applications (
    id TEXT PRIMARY KEY,
    opportunity_id TEXT NOT NULL REFERENCES opportunities(id),
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    stage TEXT NOT NULL CHECK (stage IN ('applying', 'applied', 'interview', 'offer', 'rejected', 'withdrawn', 'archived')),
    notes TEXT NOT NULL DEFAULT '',
    applied_at TEXT,
    follow_up_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(opportunity_id, user_id)
);

CREATE TABLE IF NOT EXISTS application_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id TEXT NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    from_stage TEXT,
    to_stage TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(application_id, event_type, to_stage, created_at)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_application_events_legacy_idempotency
    ON application_events(application_id, event_type, to_stage, created_at);

CREATE TABLE IF NOT EXISTS application_contacts (
    id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS application_tasks (
    id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    due_at TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'done')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_application_tasks_due
    ON application_tasks(user_id, status, due_at);

CREATE TABLE IF NOT EXISTS reminders (
    id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    reminder_type TEXT NOT NULL DEFAULT 'follow_up',
    due_at TEXT NOT NULL,
    timezone TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled', 'cancelled', 'completed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(application_id, user_id, reminder_type)
);
CREATE INDEX IF NOT EXISTS idx_reminders_due
    ON reminders(user_id, status, due_at);

CREATE TABLE IF NOT EXISTS opportunity_captures (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL CHECK (source_type IN ('url', 'pdf', 'screenshot')),
    source_url TEXT NOT NULL DEFAULT '',
    original_name TEXT NOT NULL DEFAULT '',
    media_type TEXT NOT NULL DEFAULT '',
    storage_path TEXT NOT NULL DEFAULT '',
    extracted_text TEXT NOT NULL DEFAULT '',
    parsed_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'confirmed')),
    application_id TEXT REFERENCES applications(id),
    created_at TEXT NOT NULL,
    confirmed_at TEXT
);

CREATE TABLE IF NOT EXISTS generated_documents (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    opportunity_id TEXT REFERENCES opportunities(id) ON DELETE SET NULL,
    document_type TEXT NOT NULL CHECK (document_type IN ('resume', 'cover_letter')),
    version INTEGER NOT NULL,
    parent_id TEXT REFERENCES generated_documents(id),
    content TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'approved')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    approved_at TEXT,
    UNIQUE(user_id, opportunity_id, document_type, version)
);

CREATE TABLE IF NOT EXISTS answer_library (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    company TEXT NOT NULL DEFAULT '',
    tags_json TEXT NOT NULL DEFAULT '[]',
    usage_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_answer_library_user_updated
    ON answer_library(user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS mock_interviews (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    opportunity_id TEXT NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'completed')),
    created_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS mock_questions (
    id TEXT PRIMARY KEY,
    interview_id TEXT NOT NULL REFERENCES mock_interviews(id) ON DELETE CASCADE,
    prompt TEXT NOT NULL,
    rubric_json TEXT NOT NULL,
    position INTEGER NOT NULL,
    UNIQUE(interview_id, position)
);

CREATE TABLE IF NOT EXISTS mock_answers (
    id TEXT PRIMARY KEY,
    question_id TEXT NOT NULL REFERENCES mock_questions(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    answer_text TEXT NOT NULL DEFAULT '',
    transcript TEXT NOT NULL DEFAULT '',
    audio_path TEXT NOT NULL DEFAULT '',
    score INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
    feedback_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_threads (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'cancelled', 'completed')),
    message_budget INTEGER NOT NULL DEFAULT 100,
    tool_budget INTEGER NOT NULL DEFAULT 50,
    messages_used INTEGER NOT NULL DEFAULT 0,
    tools_used INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_messages (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES agent_threads(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    citations_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agent_tool_runs (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES agent_threads(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tool_name TEXT NOT NULL,
    input_json TEXT NOT NULL DEFAULT '{}',
    output_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed', 'cancelled')),
    error TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS agent_proposed_actions (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES agent_threads(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    action_type TEXT NOT NULL,
    scope TEXT NOT NULL,
    input_json TEXT NOT NULL,
    expected_effect TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'failed')),
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    decided_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_actions_pending
    ON agent_proposed_actions(user_id, status, created_at DESC);

CREATE TABLE IF NOT EXISTS application_form_sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    application_id TEXT REFERENCES applications(id) ON DELETE SET NULL,
    page_url TEXT NOT NULL,
    ats_type TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'reviewed', 'completed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS connector_accounts (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    scopes_json TEXT NOT NULL DEFAULT '[]',
    encrypted_access_token TEXT NOT NULL DEFAULT '',
    encrypted_refresh_token TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'connected' CHECK (status IN ('connected', 'disconnected', 'error')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    disconnected_at TEXT,
    UNIQUE(user_id, provider)
);

CREATE TABLE IF NOT EXISTS oauth_states (
    state_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL CHECK (provider IN ('google', 'microsoft')),
    code_verifier TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS monitored_events (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    connector_id TEXT REFERENCES connector_accounts(id) ON DELETE SET NULL,
    external_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    confidence REAL NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'confirmed', 'ignored')),
    application_id TEXT REFERENCES applications(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    UNIQUE(user_id, connector_id, external_id)
);

CREATE TABLE IF NOT EXISTS notification_preferences (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    timezone TEXT NOT NULL DEFAULT 'UTC',
    quiet_start TEXT NOT NULL DEFAULT '22:00',
    quiet_end TEXT NOT NULL DEFAULT '08:00',
    digest_frequency TEXT NOT NULL DEFAULT 'daily' CHECK (digest_frequency IN ('immediate', 'daily', 'weekly', 'off')),
    in_app_enabled INTEGER NOT NULL DEFAULT 1,
    email_enabled INTEGER NOT NULL DEFAULT 0,
    push_enabled INTEGER NOT NULL DEFAULT 0,
    sms_enabled INTEGER NOT NULL DEFAULT 0,
    voice_enabled INTEGER NOT NULL DEFAULT 0,
    phone_e164 TEXT NOT NULL DEFAULT '',
    phone_verified INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notification_outbox (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    channel TEXT NOT NULL CHECK (channel IN ('in_app', 'email', 'push', 'sms', 'voice')),
    event_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('sandbox_suppressed', 'queued', 'sent', 'cancelled')),
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    UNIQUE(user_id, channel, event_key)
);

CREATE TABLE IF NOT EXISTS phone_verifications (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    phone_e164 TEXT NOT NULL,
    code_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'verified', 'expired')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dossier_settings (
    user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    paused INTEGER NOT NULL DEFAULT 0,
    retention_days INTEGER NOT NULL DEFAULT 365,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dossier_items (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    item_type TEXT NOT NULL CHECK (item_type IN ('confirmed_fact', 'user_opinion', 'deterministic_analysis', 'ai_suggestion')),
    field_path TEXT NOT NULL,
    value_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'deleted')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, item_type, field_path)
);

CREATE TABLE IF NOT EXISTS dossier_consent_grants (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    recipient TEXT NOT NULL,
    item_ids_json TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'revoked', 'expired')),
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS dossier_access_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    grant_id TEXT NOT NULL REFERENCES dossier_consent_grants(id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    accessed_at TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS market_snapshots (
    id TEXT PRIMARY KEY,
    as_of TEXT NOT NULL,
    data_json TEXT NOT NULL,
    data_hash TEXT NOT NULL,
    opportunity_ids_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS market_issues (
    id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL REFERENCES market_snapshots(id),
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    market_json TEXT NOT NULL,
    personalized_json TEXT NOT NULL,
    methodology TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published', 'archived')),
    created_at TEXT NOT NULL,
    published_at TEXT
);

CREATE TABLE IF NOT EXISTS organizations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    organization_type TEXT NOT NULL CHECK (organization_type IN ('employer', 'school')),
    verification_status TEXT NOT NULL DEFAULT 'pending' CHECK (verification_status IN ('pending', 'verified', 'rejected')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS organization_memberships (
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    membership_role TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (organization_id, user_id)
);

CREATE TABLE IF NOT EXISTS requisitions (
    id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    rubric_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'open', 'closed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS employer_candidates (
    id TEXT PRIMARY KEY,
    requisition_id TEXT NOT NULL REFERENCES requisitions(id) ON DELETE CASCADE,
    consent_grant_id TEXT NOT NULL REFERENCES dossier_consent_grants(id),
    evidence_json TEXT NOT NULL,
    score INTEGER NOT NULL CHECK (score BETWEEN 0 AND 100),
    status TEXT NOT NULL DEFAULT 'review' CHECK (status IN ('review', 'shortlisted', 'interview', 'offer', 'rejected')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(requisition_id, consent_grant_id)
);

CREATE TABLE IF NOT EXISTS employer_decisions (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES employer_candidates(id) ON DELETE CASCADE,
    actor_user_id TEXT NOT NULL REFERENCES users(id),
    from_status TEXT,
    to_status TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS employer_messages (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES employer_candidates(id) ON DELETE CASCADE,
    actor_user_id TEXT NOT NULL REFERENCES users(id),
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'sandbox_suppressed', 'cancelled')),
    created_at TEXT NOT NULL,
    approved_at TEXT
);

CREATE TABLE IF NOT EXISTS employer_interviews (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES employer_candidates(id) ON DELETE CASCADE,
    actor_user_id TEXT NOT NULL REFERENCES users(id),
    starts_at TEXT NOT NULL,
    timezone TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'proposed' CHECK (status IN ('proposed', 'confirmed', 'cancelled')),
    created_at TEXT NOT NULL,
    confirmed_at TEXT
);

CREATE TABLE IF NOT EXISTS source_controls (
    source_key TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    moderation_status TEXT NOT NULL DEFAULT 'approved' CHECK (moderation_status IN ('approved', 'review', 'blocked')),
    note TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS moderation_items (
    id TEXT PRIMARY KEY,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'dismissed')),
    resolution TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    resolved_by TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS job_queue (
    id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    idempotency_key TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL DEFAULT 'queued' CHECK (state IN ('queued', 'running', 'succeeded', 'retry', 'dead', 'cancelled')),
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    next_attempt_at TEXT NOT NULL,
    locked_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_job_queue_claim
ON job_queue(state, next_attempt_at, created_at);

CREATE TABLE IF NOT EXISTS service_metrics (
    metric_key TEXT PRIMARY KEY,
    metric_value REAL NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS account_deletion_log (
    id TEXT PRIMARY KEY,
    user_id_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('completed', 'failed')),
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feature_flags (
    key TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 0,
    description TEXT NOT NULL DEFAULT '',
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operational_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS migration_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    migration_key TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_count INTEGER NOT NULL,
    imported_count INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    UNIQUE(migration_key, source_path, finished_at)
);

DROP VIEW IF EXISTS opportunity_read_model;
CREATE VIEW opportunity_read_model AS
WITH primary_source AS (
    SELECT ranked.opportunity_id, ranked.source_key, ranked.source_name,
           ranked.external_id, ranked.source_url, ranked.first_seen_at, ranked.last_seen_at
    FROM (
        SELECT source.*, ROW_NUMBER() OVER (
            PARTITION BY source.opportunity_id ORDER BY source.source_key, source.external_id
        ) AS source_rank
        FROM opportunity_sources source
        LEFT JOIN source_controls control ON control.source_key=source.source_key
        WHERE COALESCE(control.enabled, 1)=1
          AND COALESCE(control.moderation_status, 'approved')!='blocked'
    ) ranked
    WHERE ranked.source_rank=1
),
latest_interaction AS (
    SELECT interaction.*
    FROM opportunity_interactions interaction
    WHERE interaction.user_id = 'local-user'
      AND interaction.id = (
          SELECT MAX(candidate.id)
          FROM opportunity_interactions candidate
          WHERE candidate.opportunity_id = interaction.opportunity_id
            AND candidate.user_id = interaction.user_id
      )
)
SELECT
    o.id,
    o.company,
    o.title,
    o.location,
    o.region,
    o.role_type,
    o.url,
    o.description,
    o.posted_at,
    o.deadline_at,
    o.first_seen_at,
    o.last_seen_at,
    COALESCE(oa.remote_mode, 'unknown') AS remote_mode,
    COALESCE(oa.terms_json, '[]') AS terms_json,
    COALESCE(oa.graduation_years_json, '[]') AS graduation_years_json,
    oa.pay_min,
    oa.pay_max,
    COALESCE(oa.pay_period, '') AS pay_period,
    COALESCE(oa.currency, '') AS currency,
    o.active,
    o.duplicate_of,
    COALESCE(fs.score, 0) AS score,
    COALESCE(fs.explanation_json, '[]') AS score_explanation,
    COALESCE(fs.ruleset_version, '') AS ruleset_version,
    fs.created_at AS score_created_at,
    CASE
        WHEN latest.action IN ('saved', 'passed') THEN latest.action
        ELSE ''
    END AS intent_state,
    CASE
        WHEN a.stage IS NOT NULL THEN a.stage
        WHEN latest.action = 'saved' THEN 'shortlisted'
        ELSE 'discovered'
    END AS status,
    COALESCE(a.notes, '') AS notes,
    a.applied_at,
    a.follow_up_at,
    os.source_key,
    os.source_name,
    os.external_id
FROM opportunities o
JOIN primary_source os ON os.opportunity_id = o.id
LEFT JOIN fit_scores fs
    ON fs.opportunity_id = o.id
    AND fs.user_id = 'local-user'
    AND fs.ruleset_version = 'legacy-v1'
LEFT JOIN opportunity_attributes oa ON oa.opportunity_id = o.id
LEFT JOIN applications a
    ON a.opportunity_id = o.id
    AND a.user_id = 'local-user'
LEFT JOIN latest_interaction latest ON latest.opportunity_id = o.id;
