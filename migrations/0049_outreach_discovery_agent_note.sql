-- Which agent ran a deep search when it was not the one the student chose.
--
-- outreach_discovery_runs.agent_note is '' for a search that ran as chosen. When Codex was chosen but is not
-- allowed to read the web (no .env opt-in), Claude Code runs the search and this holds the sentence that says so.
-- The note used to live only in the manager's in-memory result and on the command line's stderr, so the deep
-- search panel (which shows the stored runs) and the scheduled Monday and Thursday search (which runs through
-- the command line) never showed it. Stored with the run, every reader sees it.
--
-- The column is added by a Python step (schema._apply_outreach_discovery_agent_note), guarded, so a crash
-- before the migration is marked cannot make the next start fail on a duplicate column. No index: the runs
-- are read newest first through the existing per-student index.

SELECT 1;
