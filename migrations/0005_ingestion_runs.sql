-- Audit trail for scheduled pipeline stages executed by the web worker.
CREATE TABLE IF NOT EXISTS ingestion_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stage TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('success', 'failed')),
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
