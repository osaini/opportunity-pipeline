-- Per-student switches that are not profile facts. No row means the default,
-- so a setting added later is off for everyone until each student turns it on.
-- The first key is 'jev_inbox_suggestions' (opportunity_app/inbox_classifiers.py):
-- whether pasted replies and connector emails are sent to TypeSafe to classify.
CREATE TABLE IF NOT EXISTS user_settings (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);
