-- A deadline the student found and recorded for a role (for example on the
-- employer's careers page). It is the student's own claim, kept apart from the
-- deadline stated in posting text, and it never drives the expiry purge: it
-- cascades away with its posting, like a save does.
CREATE TABLE IF NOT EXISTS opportunity_deadlines (
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    opportunity_id TEXT NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
    deadline_on TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, opportunity_id)
);

CREATE INDEX IF NOT EXISTS idx_opportunity_deadlines_user_date
    ON opportunity_deadlines(user_id, deadline_on);
