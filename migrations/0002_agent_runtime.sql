CREATE TABLE IF NOT EXISTS agent_turns (
    id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES agent_threads(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed', 'cancelled')),
    provider_request_id TEXT NOT NULL DEFAULT '',
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_turns_thread
    ON agent_turns(thread_id, created_at DESC);

ALTER TABLE agent_threads ADD COLUMN provider TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE agent_threads ADD COLUMN model TEXT NOT NULL DEFAULT 'deterministic-v1';
ALTER TABLE agent_messages ADD COLUMN turn_id TEXT REFERENCES agent_turns(id) ON DELETE SET NULL;
ALTER TABLE agent_tool_runs ADD COLUMN turn_id TEXT REFERENCES agent_turns(id) ON DELETE SET NULL;
