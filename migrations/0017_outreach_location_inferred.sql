-- A location read from the only place a company's site names, rather than an
-- address or headquarters the site states. It is shown as not yet checked, and
-- a draft does not rely on it until the student confirms it.
ALTER TABLE outreach_targets ADD COLUMN location_inferred INTEGER NOT NULL DEFAULT 0
    CHECK (location_inferred IN (0, 1));
