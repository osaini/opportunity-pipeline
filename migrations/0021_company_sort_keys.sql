-- Deterministic company/title ordering that both repository paths share.
--
-- The tenant path sorted in Python with str.casefold(); the CLI path sorted in
-- SQL with COLLATE NOCASE. Those are not the same function. casefold expands
-- U+00DF, so 'Straße' and 'Strasse' compare equal and the id breaks the tie;
-- NOCASE folds A-Z only, leaves U+00DF alone, and orders it after 's'. The two
-- paths therefore returned different orders for the same data, and which one a
-- caller saw depended on whether it passed a user_id.
--
-- PostgreSQL makes it three-way: database.py strips COLLATE NOCASE entirely
-- and the server applies its own database collation.
--
-- So the fold is done once, in Python, and stored. Both paths then order by the
-- same bytes on every backend. The displayed company and title are untouched.
--
-- Columns are added and backfilled by the Python step registered for this
-- migration in opportunity_app/schema.py, because ALTER TABLE ADD COLUMN is not
-- idempotent and casefold is not a SQL function. Only the view is recreated
-- here, to project the new columns.

DROP VIEW IF EXISTS opportunity_read_model;

CREATE VIEW opportunity_read_model AS
WITH primary_source AS (
    SELECT ranked.opportunity_id, ranked.source_key, ranked.source_name,
           ranked.external_id, ranked.source_url, ranked.first_seen_at, ranked.last_seen_at
    FROM (
        SELECT source.*, ROW_NUMBER() OVER (
            PARTITION BY source.opportunity_id ORDER BY source.source_key, source.external_id
        ) AS source_rank
        FROM opportunity_sources source
        LEFT JOIN source_controls control ON control.source_key=source.source_key
        WHERE COALESCE(control.enabled, 1)=1
          AND COALESCE(control.moderation_status, 'approved')!='blocked'
    ) ranked
    WHERE ranked.source_rank=1
),
latest_interaction AS (
    SELECT interaction.*
    FROM opportunity_interactions interaction
    WHERE interaction.user_id = 'local-user'
      AND interaction.id = (
          SELECT MAX(candidate.id)
          FROM opportunity_interactions candidate
          WHERE candidate.opportunity_id = interaction.opportunity_id
            AND candidate.user_id = interaction.user_id
      )
)
SELECT
    o.id,
    o.company,
    o.title,
    o.company_sort_key,
    o.title_sort_key,
    o.location,
    o.region,
    o.role_type,
    o.url,
    o.description,
    o.posted_at,
    o.posted_at_utc,
    o.deadline_at,
    o.first_seen_at,
    o.last_seen_at,
    COALESCE(oa.remote_mode, 'unknown') AS remote_mode,
    COALESCE(oa.terms_json, '[]') AS terms_json,
    COALESCE(oa.graduation_years_json, '[]') AS graduation_years_json,
    oa.pay_min,
    oa.pay_max,
    COALESCE(oa.pay_period, '') AS pay_period,
    COALESCE(oa.currency, '') AS currency,
    o.active,
    o.duplicate_of,
    COALESCE(fs.score, 0) AS score,
    COALESCE(fs.explanation_json, '[]') AS score_explanation,
    COALESCE(fs.ruleset_version, '') AS ruleset_version,
    fs.created_at AS score_created_at,
    CASE
        WHEN latest.action IN ('saved', 'passed') THEN latest.action
        ELSE ''
    END AS intent_state,
    CASE
        WHEN a.stage IS NOT NULL THEN a.stage
        WHEN latest.action = 'saved' THEN 'shortlisted'
        ELSE 'discovered'
    END AS status,
    COALESCE(a.notes, '') AS notes,
    a.applied_at,
    a.follow_up_at,
    os.source_key,
    os.source_name,
    os.external_id
FROM opportunities o
JOIN primary_source os ON os.opportunity_id = o.id
LEFT JOIN fit_scores fs
    ON fs.opportunity_id = o.id
    AND fs.user_id = 'local-user'
    AND fs.ruleset_version = 'legacy-v1'
LEFT JOIN opportunity_attributes oa ON oa.opportunity_id = o.id
LEFT JOIN applications a
    ON a.opportunity_id = o.id
    AND a.user_id = 'local-user'
LEFT JOIN latest_interaction latest ON latest.opportunity_id = o.id;
