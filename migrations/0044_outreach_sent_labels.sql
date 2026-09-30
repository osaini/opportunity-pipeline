-- A Gmail label on every outreach email the student sent, not only on reply
-- threads (opportunity_app/outreach_labels.py). Portable SQL: no Python step.
--
-- outreach_label_threads: one row per Gmail thread that holds an outreach email
-- the student sent. There is no foreign key to outreach_targets, so a deleted
-- company's threads keep their label.
--   thread_id   the Gmail thread.
--   target_id   the company the thread belongs to, '' when not known.
--   source      how the app learned of the thread: 'sent' = the app sent it (a
--               gmail_sent or thank_you_sent event), 'search' = found by the
--               one-time search of the Sent folder for a company that has gone
--               out, 'sweep' = new sent mail whose recipients or subject match
--               an outreach company.
--   label_name  the label name this row was last settled under, '' = not yet. A
--               row whose label_name differs from the student's current label is
--               labelled again under the new name.
--   labeled_at  set = the app added that label to the thread's messages at that
--               time. label_name set with labeled_at NULL = settled without a
--               label: the thread is gone or the app could not label it.
--   label_note  why a row was settled without a label: 'gone' = Gmail no longer
--               has the thread, 'failed' = Gmail refused it. '' when labelled or
--               not yet settled.
--   found_at    when the app first recorded the thread.
CREATE TABLE IF NOT EXISTS outreach_label_threads (
    user_id TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    target_id TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    label_name TEXT NOT NULL DEFAULT '',
    labeled_at TEXT,
    label_note TEXT NOT NULL DEFAULT '',
    found_at TEXT NOT NULL,
    PRIMARY KEY (user_id, thread_id)
);

CREATE INDEX IF NOT EXISTS idx_outreach_label_threads_label
    ON outreach_label_threads(user_id, label_name);

-- outreach_label_searches: the companies whose sent mail the app has already
-- searched. searched_at is when, found how many threads it found, and query the
-- Gmail search it used. A company with no row here, or whose addresses, subjects
-- or dates now make a different query, still has its Sent folder to be searched.
CREATE TABLE IF NOT EXISTS outreach_label_searches (
    user_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    searched_at TEXT NOT NULL,
    found INTEGER NOT NULL DEFAULT 0,
    query TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (user_id, target_id)
);
