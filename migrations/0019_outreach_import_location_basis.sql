-- An imported location stops claiming the student typed it.
--
-- create_target used to stamp location_basis = 'manual' for every origin that
-- was not 'discovery', so a location that arrived in an import file was
-- recorded as the student's own entry: shown as "your entry", counted as
-- verified, relied on by drafts, and — because 'manual' outranks every source —
-- never correctable by the company's own site or its Form D. Nobody typed it.
--
-- Those rows are demoted to no basis at all, which is what an import file's
-- word is worth. The only affirmative evidence the schema holds that a student
-- really did vouch for one is a location_confirmed event, so a row carrying one
-- is left alone. A row demoted in error keeps its location text and gains a
-- Confirm button; a row wrongly left as 'manual' would stay fabricated and
-- invisible, with no button to fix it.
--
-- updated_at is deliberately not touched: it drives the "Recently updated"
-- sort, and correcting the app's own past mistake is not activity by the
-- student. The migration file and its schema_migrations row are the audit
-- record, and the change shows itself in the UI immediately — every affected
-- row gains "not yet checked".
UPDATE outreach_targets
    SET location_basis = '', location_source_url = ''
    WHERE origin = 'import'
      AND location_basis = 'manual'
      AND id NOT IN (
          SELECT target_id FROM outreach_events WHERE event_type = 'location_confirmed'
      );
