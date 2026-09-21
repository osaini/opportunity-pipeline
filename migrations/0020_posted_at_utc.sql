-- Canonical UTC for posted_at, so ranking by date orders by time.
--
-- posted_at keeps whatever each source API sent, because the source's own
-- words are what this product promises to preserve. Those words disagree:
-- `2026-07-18T09:14:00Z`, `2026-05-01T00:00:00.000Z`, and Greenhouse offsets
-- like `-04:00` all appear. Sorted as text, `...17Z` lands after
-- `...17.999999+00:00` despite being nearly a second earlier, so the `newest`
-- sort ordered cards by spelling rather than by date.
--
-- posted_at_utc is derived, fixed-width (`.ffffff+00:00`), and NULL whenever
-- the source did not state a timezone -- reading a naive timestamp as UTC
-- would invent an offset the source never gave.
--
-- The column is added and backfilled by the Python step registered for this
-- migration in opportunity_app/schema.py; ALTER TABLE ADD COLUMN is not
-- idempotent, and the backfill has to parse timestamps, which SQL cannot do
-- portably across SQLite and PostgreSQL. Only the view is recreated here.

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
