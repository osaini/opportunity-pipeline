-- Reversible internal automation: résumé variants and the pick for each saved
-- role (opportunity_app/student/resume_variants.py).
--
-- The column this migration adds to an existing table (resume_files.variant_label,
-- the student's own name for a résumé they keep for one kind of role, '' for
-- none) is added by a Python step (schema._apply_internal_automation), guarded,
-- so a crash before the migration is marked cannot make the next start fail on
-- a duplicate column.
--
-- opportunity_resume_picks holds, per student and role, the résumé variant to
-- use. picked_by is 'automatic' (the resume_variant_pick switch chose it, and
-- the ledger can undo it) or 'student' (the student chose it; automation never
-- overwrites that). matched_json holds why: the variant's words the posting
-- matched, the points, and whether the choice was unsure.
CREATE TABLE IF NOT EXISTS opportunity_resume_picks (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    opportunity_id TEXT NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    resume_file_id TEXT NOT NULL REFERENCES resume_files(id) ON DELETE CASCADE,
    picked_by TEXT NOT NULL CHECK (picked_by IN ('automatic', 'student')),
    matched_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, opportunity_id)
);
CREATE INDEX IF NOT EXISTS idx_opportunity_resume_picks_file
    ON opportunity_resume_picks(resume_file_id);
