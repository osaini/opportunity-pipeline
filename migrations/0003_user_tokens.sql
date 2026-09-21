-- Per-user API tokens enable real multi-user tenancy. Only SHA-256 hashes
-- are stored; plaintext tokens are shown once at issuance and never persisted.
CREATE TABLE IF NOT EXISTS user_api_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    last_used_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_user_api_tokens_user
    ON user_api_tokens(user_id);
