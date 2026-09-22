-- The student's own progress on a first-year program from
-- config/early_programs.local.json (see opportunity_app/early_programs.py).
-- The program list itself is a private file, not a table: it is researched by
-- hand, and a row here only records what the student did about one entry. No
-- row means "not started", so a program removed from the file leaves nothing
-- that pretends it still exists.
CREATE TABLE IF NOT EXISTS early_program_status (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    program_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('applied', 'skipped')),
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, program_id)
);
