-- One-time pairing challenges and revocable, extension-only device tokens.
-- Plaintext codes and tokens are returned once and never persisted.
CREATE TABLE IF NOT EXISTS extension_pairing_challenges (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    code_hash TEXT NOT NULL UNIQUE,
    expires_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'used', 'expired', 'locked')),
    created_at TEXT NOT NULL,
    consumed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_extension_pairings_user
    ON extension_pairing_challenges(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS extension_devices (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    extension_origin TEXT NOT NULL,
    device_name TEXT NOT NULL DEFAULT 'Chrome Apply Mode',
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_extension_devices_user
    ON extension_devices(user_id, revoked_at, created_at DESC);

-- Origin-level throttling for code guessing. No attempted code is retained.
CREATE TABLE IF NOT EXISTS extension_pairing_redemption_attempts (
    id TEXT PRIMARY KEY,
    extension_origin TEXT NOT NULL,
    succeeded INTEGER NOT NULL DEFAULT 0 CHECK (succeeded IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_extension_pairing_attempts_origin
    ON extension_pairing_redemption_attempts(extension_origin, created_at DESC);
