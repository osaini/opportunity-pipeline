-- Value-free progress for multi-page application forms.
CREATE TABLE IF NOT EXISTS application_form_session_steps (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES application_form_sessions(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    step_key TEXT NOT NULL,
    page_url TEXT NOT NULL,
    ats_type TEXT NOT NULL,
    fields_json TEXT NOT NULL DEFAULT '[]',
    summary_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'scanned'
        CHECK (status IN ('scanned', 'reviewed', 'filled', 'manual', 'completed')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(session_id, step_key)
);

CREATE INDEX IF NOT EXISTS idx_apply_steps_session
    ON application_form_session_steps(session_id, updated_at DESC);
