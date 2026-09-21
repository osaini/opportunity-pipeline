-- Every draft the student has had for a target, so a regenerated draft that
-- reads worse never costs them the earlier one. A version keeps the claims and
-- provenance it was written with, and the comments that asked for it.
-- source 'generated' is a draft as the drafter wrote it; 'saved' is the text
-- that was in the editor when a new draft replaced it, which may be hand edited.
CREATE TABLE IF NOT EXISTS outreach_draft_versions (
    id TEXT PRIMARY KEY,
    target_id TEXT NOT NULL REFERENCES outreach_targets(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('initial', 'follow_up')),
    source TEXT NOT NULL DEFAULT 'generated' CHECK (source IN ('generated', 'saved')),
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    claims_json TEXT NOT NULL DEFAULT '[]',
    generated_by TEXT NOT NULL DEFAULT '',
    comments TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outreach_draft_versions_target
    ON outreach_draft_versions(target_id, kind, created_at);
