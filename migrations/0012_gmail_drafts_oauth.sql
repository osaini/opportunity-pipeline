-- Gmail drafts for approved outreach. A separate OAuth connection whose only
-- scope is gmail.compose; the app creates drafts and never sends. SQLite cannot
-- alter a CHECK constraint, so oauth_states (short-lived rows) is rebuilt.
CREATE TABLE oauth_states_rebuilt (
    state_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL CHECK (provider IN ('google', 'microsoft', 'gmail_drafts')),
    code_verifier TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    consumed_at TEXT,
    created_at TEXT NOT NULL
);
INSERT INTO oauth_states_rebuilt SELECT state_hash, user_id, provider, code_verifier, redirect_uri, expires_at, consumed_at, created_at FROM oauth_states;
DROP TABLE oauth_states;
ALTER TABLE oauth_states_rebuilt RENAME TO oauth_states;
