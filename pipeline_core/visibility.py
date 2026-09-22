"""Who may see an opportunity: the one rule every read path shares.

Standard library only (AGENTS.md rule 4). ``opportunity_app.urgent`` re-exports
these names, so the Urgent queue, the opportunity list and detail, and the
assistant all apply the same predicate.
"""

from __future__ import annotations

CAPTURE_SOURCE_KEY = "manual:capture"


def capture_visible_sql(alias: str = "o") -> str:
    """SQL that is true when ``alias`` is visible to the user bound as ``?``.

    Ordinary postings are shared inventory. A manual capture is visible only
    to the student who captured it, proven by provenance: the capture's own id
    is the source ``external_id`` and its application points at this
    opportunity. Having *an* application is not enough, since another student
    could open one on a capture they should never have seen. A capture with no
    owning row is visible to no one.

    The fragment binds exactly one parameter, the user id.
    """
    return f"""(
        NOT EXISTS (
            SELECT 1 FROM opportunity_sources cap_source
            WHERE cap_source.opportunity_id = {alias}.id
              AND cap_source.source_key = '{CAPTURE_SOURCE_KEY}'
        )
        OR EXISTS (
            SELECT 1 FROM opportunity_sources cap_source
            JOIN opportunity_captures cap ON cap.id = cap_source.external_id
            JOIN applications cap_app ON cap_app.id = cap.application_id
            WHERE cap_source.opportunity_id = {alias}.id
              AND cap_source.source_key = '{CAPTURE_SOURCE_KEY}'
              AND cap_app.opportunity_id = {alias}.id
              AND cap.user_id = ?
        )
    )"""
