-- The durable job writing a target's call prep in the background. The job's
-- state lives in job_queue, so a server restart or a sleeping laptop picks up
-- where it left off instead of losing the request.
ALTER TABLE outreach_targets ADD COLUMN call_prep_job_id TEXT;
