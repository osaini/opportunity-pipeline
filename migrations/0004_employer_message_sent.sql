-- Widen employer message lifecycle to allow real delivery ('sent') once a
-- live notification provider is configured. SQLite requires a table rebuild
-- to change a CHECK constraint.
CREATE TABLE IF NOT EXISTS employer_messages_new (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES employer_candidates(id) ON DELETE CASCADE,
    actor_user_id TEXT NOT NULL REFERENCES users(id),
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'sandbox_suppressed', 'cancelled', 'sent')),
    created_at TEXT NOT NULL,
    approved_at TEXT
);

INSERT INTO employer_messages_new(id, candidate_id, actor_user_id, body, status, created_at, approved_at)
    SELECT id, candidate_id, actor_user_id, body, status, created_at, approved_at FROM employer_messages;

DROP TABLE employer_messages;
ALTER TABLE employer_messages_new RENAME TO employer_messages;
