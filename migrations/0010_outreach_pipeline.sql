-- Outreach pipeline: grounded drafts that wait for approval, contact candidates
-- with their evidence, and the twice-weekly deep search runs that add targets.
-- Nothing here sends mail; an approved draft only unlocks a compose link.
ALTER TABLE outreach_targets ADD COLUMN origin TEXT NOT NULL DEFAULT 'manual'
    CHECK (origin IN ('manual', 'import', 'discovery'));
ALTER TABLE outreach_targets ADD COLUMN discovery_run_id TEXT;
ALTER TABLE outreach_targets ADD COLUMN draft_status TEXT NOT NULL DEFAULT 'none'
    CHECK (draft_status IN ('none', 'generated', 'approved'));
ALTER TABLE outreach_targets ADD COLUMN draft_generated_by TEXT NOT NULL DEFAULT '';
ALTER TABLE outreach_targets ADD COLUMN draft_generated_at TEXT;
ALTER TABLE outreach_targets ADD COLUMN draft_approved_at TEXT;
ALTER TABLE outreach_targets ADD COLUMN draft_claims_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE outreach_targets ADD COLUMN follow_up_subject TEXT NOT NULL DEFAULT '';
ALTER TABLE outreach_targets ADD COLUMN follow_up_body TEXT NOT NULL DEFAULT '';
ALTER TABLE outreach_targets ADD COLUMN follow_up_status TEXT NOT NULL DEFAULT 'none'
    CHECK (follow_up_status IN ('none', 'generated', 'approved'));
ALTER TABLE outreach_targets ADD COLUMN contact_evidence_url TEXT NOT NULL DEFAULT '';
ALTER TABLE outreach_targets ADD COLUMN mail_domain_ok INTEGER;

CREATE TABLE IF NOT EXISTS outreach_contact_candidates (
    id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL REFERENCES outreach_targets(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL DEFAULT '',
    role TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    method TEXT NOT NULL
        CHECK (method IN ('site_published', 'site_generic', 'pattern_guess', 'ai_research')),
    confidence TEXT NOT NULL CHECK (confidence IN ('confirmed', 'unverified', 'unknown')),
    evidence_url TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(target_id, email, name)
);

CREATE INDEX IF NOT EXISTS idx_outreach_candidates_target
    ON outreach_contact_candidates(target_id, confidence);

CREATE TABLE IF NOT EXISTS outreach_discovery_runs (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    run_trigger TEXT NOT NULL DEFAULT 'manual',
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
    scopes_json TEXT NOT NULL DEFAULT '[]',
    proposed INTEGER NOT NULL DEFAULT 0,
    imported INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    rejected_json TEXT NOT NULL DEFAULT '[]',
    report_path TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_outreach_discovery_runs_user
    ON outreach_discovery_runs(user_id, started_at DESC);
