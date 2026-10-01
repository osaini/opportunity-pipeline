-- Three indexes for queries that scanned a whole table. Index-only: no column, row or result changes.
-- Portable SQL (SQLite and PostgreSQL run the same text), idempotent, so no Python step is needed.
--
-- outreach_events: the only index was (target_id, created_at), so every query that asks for one kind of
-- event across all of a student's companies (the reply, send, delivery and label steps of the inbox
-- watcher, every few minutes, plus the sent-draft look) read the whole table, reply text included.
-- (user_id, event_type, created_at) serves 'WHERE user_id=? AND event_type=?' with or without a
-- created_at bound or ordering.
-- (target_id, user_id, event_type, created_at DESC) serves the per-company queries ('WHERE target_id=? AND
-- user_id=? AND event_type=? ...'). Without it SQLite, which is never given ANALYZE here, prefers the
-- two-equality student-wide index above for them and reads every event of that kind for the student.
-- It is DESC like idx_outreach_events_target, the index those queries used before: rows that share a
-- created_at come back in the same order from either index, with or without an ORDER BY, so which of
-- two tied events a query picks does not change. (An ascending index would reverse every such tie.)
--
-- opportunities.company_sort_key: the read model, the company tags and the tag joins filter and join on
-- it (company_sort_key = ?, IN (...), company_key = o.company_sort_key) and nothing indexed it.
-- The older idx_opportunities_company_title is left in place.

CREATE INDEX IF NOT EXISTS idx_outreach_events_user_type
    ON outreach_events(user_id, event_type, created_at);

CREATE INDEX IF NOT EXISTS idx_outreach_events_target_type
    ON outreach_events(target_id, user_id, event_type, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_opportunities_company_sort_key
    ON opportunities(company_sort_key);
