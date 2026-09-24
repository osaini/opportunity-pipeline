"""Read-only opportunity contracts for CLI/API parity.

This module deliberately uses sqlite3 and plain dictionaries.  Keeping the
contract framework-neutral makes it usable from FastAPI, migration tests, and
the dependency-free legacy CLI.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from .visibility import capture_visible_sql


# Date sorts order by `posted_at_utc`, the derived fixed-width UTC column, not
# by the raw `posted_at` the source sent. Sources disagree on spelling -- `Z`,
# `.000Z`, and offsets like `-04:00` all occur -- and as text `...17Z` sorts
# after `...17.999999+00:00` despite being nearly a second earlier. The raw
# value is still what the card displays; only the ordering key is derived.
#
# `posted_at_utc` is NULL when the source gave no timezone, so the fallback to
# an observation timestamp is expressed as COALESCE exactly as before.
SORT_SQL = {
    "score": "o.score DESC, COALESCE(o.posted_at_utc, o.last_seen_at) DESC, o.id ASC",
    "newest": "COALESCE(o.posted_at_utc, o.first_seen_at) DESC, o.score DESC, o.id ASC",
    "discovered": "o.first_seen_at DESC, o.score DESC, o.id ASC",
    # The stored fold, not COLLATE NOCASE: see migration 0021. NOCASE is
    # ASCII-only and differs from the casefold the tenant path uses, and
    # PostgreSQL drops NOCASE entirely for its own collation.
    "company": "o.company_sort_key ASC, o.title_sort_key ASC, o.id ASC",
    "deadline": "CASE WHEN o.deadline_at IS NULL THEN 1 ELSE 0 END, o.deadline_at ASC, o.score DESC, o.id ASC",
}


@dataclass(frozen=True)
class OpportunityFilters:
    """Validated filters supported by the first API/read-model slice."""

    query: str = ""
    role_type: str = ""
    status: str = ""
    intent_state: str = ""
    exclude_passed: bool = False
    region: str = ""
    source: str = ""
    term: str = ""
    graduation_year: int | None = None
    remote_mode: str = ""
    min_hourly_pay: float | None = None
    posted_since: str = ""
    deadline_before: str = ""
    tag: str = ""
    sort: str = "score"
    active_only: bool = True
    unique_only: bool = True
    limit: int = 50
    offset: int = 0

    def normalized(self) -> "OpportunityFilters":
        return OpportunityFilters(
            query=self.query.strip(),
            role_type=self.role_type.strip(),
            status=self.status.strip(),
            intent_state=self.intent_state.strip(),
            exclude_passed=bool(self.exclude_passed),
            region=self.region.strip(),
            source=self.source.strip(),
            term=self.term.strip().lower(),
            graduation_year=int(self.graduation_year) if self.graduation_year else None,
            remote_mode=self.remote_mode.strip().lower(),
            min_hourly_pay=max(0.0, float(self.min_hourly_pay)) if self.min_hourly_pay is not None else None,
            posted_since=self.posted_since.strip(),
            deadline_before=self.deadline_before.strip(),
            tag=self.tag.strip().lstrip("#").lower(),
            sort=self.sort if self.sort in SORT_SQL else "score",
            active_only=bool(self.active_only),
            unique_only=bool(self.unique_only),
            limit=max(1, min(int(self.limit), 200)),
            offset=max(0, int(self.offset)),
        )


def _decode_reasons(value: str | None) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if not _BASE_REASON.fullmatch(str(item))][:8]


# The scorer records its starting points as a reason ("35 base"). It is kept out
# of the reason list so a card's top reason is a real match, and exposed on its
# own so an explanation can still account for the whole score.
_BASE_REASON = re.compile(r"(\d+) base")


def _decode_base_score(value: str | None) -> int | None:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return None
    for item in parsed if isinstance(parsed, list) else []:
        match = _BASE_REASON.fullmatch(str(item))
        if match:
            return int(match.group(1))
    return None


def _decode_list(value: str | None) -> list[Any]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _evidence_for_reason(reason: str) -> dict[str, Any]:
    lowered = reason.lower()
    if "region" in lowered or "location" in lowered:
        profile_field, opportunity_fields = "regions", ["location"]
    elif "skill" in lowered or "tool" in lowered:
        profile_field, opportunity_fields = "skills", ["title", "description"]
    elif "degree" in lowered or "major" in lowered:
        profile_field, opportunity_fields = "degree_keywords", ["title", "description"]
    elif "interest" in lowered:
        profile_field, opportunity_fields = "interest_keywords", ["title", "description"]
    elif "remote" in lowered:
        profile_field, opportunity_fields = "remote_ok", ["location", "description"]
    elif "term" in lowered or "summer" in lowered or "spring" in lowered or "fall" in lowered:
        profile_field, opportunity_fields = "available_terms", ["title", "description"]
    elif "experience" in lowered or "senior" in lowered:
        profile_field, opportunity_fields = "max_years_experience", ["title", "description"]
    elif "sponsor" in lowered or "authorization" in lowered:
        profile_field, opportunity_fields = "requires_sponsorship", ["description"]
    else:
        profile_field, opportunity_fields = "scoring_rules", ["title", "description"]
    return {
        "reason": reason,
        "profile_field": profile_field,
        "opportunity_fields": opportunity_fields,
    }


_ASCII_UPPER = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_NOCASE_FOLD = str.maketrans(_ASCII_UPPER, _ASCII_UPPER.lower())


def _nocase_key(value: str) -> tuple[str, str]:
    """Sort key matching SQLite's ``COLLATE NOCASE``.

    NOCASE folds A-Z and nothing else, so it is *not* `str.casefold()`:
    casefold expands 'ß' to 'ss', which moves it before 'Test' where NOCASE
    leaves it after, by code point. These columns carry company and source
    names, so non-ASCII is realistic and the ordering difference is visible.

    The raw value is a second key so values that NOCASE considers equal still
    come out in a stable order rather than whatever a set happened to yield.
    """

    return value.translate(_NOCASE_FOLD), value


def _row_to_opportunity(row: sqlite3.Row) -> dict[str, Any]:
    reasons = _decode_reasons(row["score_explanation"])
    negative_markers = ("penalty", "outside", "senior", "requires", "mismatch", "not eligible")
    return {
        "id": row["id"],
        "company": row["company"],
        "title": row["title"],
        "location": row["location"],
        "region": row["region"],
        "role_type": row["role_type"],
        "url": row["url"],
        "description": row["description"],
        "company_sort_key": row["company_sort_key"],
        "title_sort_key": row["title_sort_key"],
        "posted_at": row["posted_at"],
        # The raw string above is what the card shows; this is the
        # derived UTC key it is ordered by, exposed so an explanation of
        # "why is this first" can account for the difference. NULL when
        # the source stated no timezone.
        "posted_at_utc": row["posted_at_utc"],
        "deadline_at": row["deadline_at"],
        "first_seen_at": row["first_seen_at"],
        "last_seen_at": row["last_seen_at"],
        "remote_mode": row["remote_mode"],
        "terms": [str(item) for item in _decode_list(row["terms_json"])],
        "graduation_years": [int(item) for item in _decode_list(row["graduation_years_json"])],
        "compensation": {
            "minimum": row["pay_min"],
            "maximum": row["pay_max"],
            "period": row["pay_period"],
            "currency": row["currency"],
            "known": row["pay_min"] is not None,
        },
        "active": bool(row["active"]),
        "duplicate_of": row["duplicate_of"],
        "score": int(row["score"]),
        "score_base": _decode_base_score(row["score_explanation"]),
        "reasons": reasons,
        "gaps": [reason for reason in reasons if any(marker in reason.lower() for marker in negative_markers)],
        "score_version": row["ruleset_version"],
        "score_created_at": row["score_created_at"],
        "score_evidence": [_evidence_for_reason(reason) for reason in reasons],
        "status": row["status"],
        "intent_state": row["intent_state"],
        "notes": row["notes"],
        "applied_at": row["applied_at"],
        "follow_up_at": row["follow_up_at"],
        "source_key": row["source_key"],
        "source_name": row["source_name"],
        "external_id": row["external_id"],
    }


# The view's user-owned columns belong to this account; see _where.
_INVENTORY_USER_ID = "local-user"


def _where(
    filters: OpportunityFilters,
    *,
    include_user_state: bool = True,
    alias: str = "o",
    user_id: str = _INVENTORY_USER_ID,
) -> tuple[str, list[Any]]:
    """Build the WHERE clause for either the inventory view or a tenant CTE.

    `alias` exists so both paths share one definition of what each filter
    means. `include_user_state` stays because the view's status and intent
    columns belong to `local-user`; a tenant query passes its own alias and
    filters on the computed expressions instead.

    `posted_since` deliberately still compares the raw `posted_at`: it is a
    user-supplied date filter matched against what the card displays, not the
    ordering key.
    """

    clauses: list[str] = []
    params: list[Any] = []
    if filters.active_only:
        clauses.append(f"{alias}.active = 1")
    if filters.unique_only:
        clauses.append(f"{alias}.duplicate_of IS NULL")
    if filters.query:
        clauses.append(
            f"({alias}.title LIKE ? ESCAPE '\\' OR {alias}.company LIKE ? ESCAPE '\\' "
            f"OR {alias}.location LIKE ? ESCAPE '\\' OR {alias}.description LIKE ? ESCAPE '\\')"
        )
        escaped = filters.query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        term = f"%{escaped}%"
        params.extend([term, term, term, term])
    for column, value in (
        (f"{alias}.role_type", filters.role_type),
        (f"{alias}.region", filters.region),
        (f"{alias}.source_name", filters.source),
    ):
        if value:
            clauses.append(f"{column} = ?")
            params.append(value)
    if include_user_state and filters.status:
        clauses.append(f"{alias}.status = ?")
        params.append(filters.status)
    if include_user_state and filters.intent_state:
        if filters.intent_state == "undecided":
            clauses.append(f"{alias}.intent_state = ''")
        else:
            clauses.append(f"{alias}.intent_state = ?")
            params.append(filters.intent_state)
    if include_user_state and filters.exclude_passed:
        clauses.append(f"{alias}.intent_state <> 'passed'")
    if filters.term:
        clauses.append(f"LOWER({alias}.terms_json) LIKE ? ESCAPE '\\'")
        escaped = filters.term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.append(f'%"%{escaped}%"%')
    if filters.graduation_year:
        clauses.append(f"{alias}.graduation_years_json LIKE ?")
        params.append(f"%{filters.graduation_year}%")
    if filters.remote_mode:
        clauses.append(f"{alias}.remote_mode = ?")
        params.append(filters.remote_mode)
    if filters.min_hourly_pay is not None:
        clauses.append(f"{alias}.pay_period = 'hour' AND {alias}.pay_max >= ?")
        params.append(filters.min_hourly_pay)
    if filters.posted_since:
        clauses.append(f"COALESCE({alias}.posted_at, {alias}.first_seen_at) >= ?")
        params.append(filters.posted_since)
    if filters.deadline_before:
        # A deadline is a calendar date stored as midnight UTC
        # ("2026-06-01T00:00:00+00:00"), and the filter is a date the student
        # picked. Comparing whole strings would drop the chosen day itself, so
        # both sides compare their date part. substr works on both backends.
        clauses.append(
            f"{alias}.deadline_at IS NOT NULL AND substr({alias}.deadline_at, 1, 10) <= substr(?, 1, 10)"
        )
        params.append(filters.deadline_before)
    if filters.tag:
        # A company carries a tag when it was generated and this user has not
        # removed it, or when this user added it (opportunity_app/company_tags.py).
        clauses.append(
            f"""(
            EXISTS (
                SELECT 1 FROM company_tags auto_tag
                WHERE auto_tag.company_key = {alias}.company_sort_key AND auto_tag.tag = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM company_tag_choices removed
                      WHERE removed.user_id = ? AND removed.company_key = auto_tag.company_key
                        AND removed.tag = auto_tag.tag AND removed.choice = 'removed'
                  )
            )
            OR EXISTS (
                SELECT 1 FROM company_tag_choices added
                WHERE added.user_id = ? AND added.company_key = {alias}.company_sort_key
                  AND added.tag = ? AND added.choice = 'added'
            )
        )"""
        )
        params.extend([filters.tag, user_id, user_id, filters.tag])
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


class OpportunityRepository:
    """Query the migrated product read model.

    A connection is supplied per operation so FastAPI requests do not share a
    sqlite connection across threads.
    """

    def __init__(self, connection: sqlite3.Connection, user_id: str | None = None):
        self.connection = connection
        self.connection.row_factory = sqlite3.Row
        self.user_id = user_id

    # Columns describing the opportunity itself. The view also carries
    # `local-user`'s score, status, notes and intent, and their absence here is
    # deliberate: a tenant query computes those from its own rows.
    #
    # Explicit rather than `o.*`, for a reason that is not style.
    # `SELECT o.*, <tenant score> AS score` puts two columns named `score` in
    # one result, and sqlite3.Row resolves a duplicated name to the *first*
    # occurrence -- verified: `SELECT t.*, 7 AS score` returns 99. That would
    # serve one account's score, status and intent to every authenticated
    # student, silently, as a side effect of a performance change.
    _INVENTORY_COLUMNS = (
        "id", "company", "title", "company_sort_key", "title_sort_key",
        "location", "region", "role_type", "url", "description",
        "posted_at", "posted_at_utc", "deadline_at", "first_seen_at",
        "last_seen_at", "remote_mode", "terms_json", "graduation_years_json",
        "pay_min", "pay_max", "pay_period", "currency", "active",
        "duplicate_of", "source_key", "source_name", "external_id",
    )

    def _tenant_cte(self) -> tuple[str, list[Any]]:
        """One single-tenant view of the inventory, as SQL.

        Every user-owned value is an expression over this caller's own rows.
        The rules live here, once:

          * latest interaction means MAX(id), not MAX(created_at)
          * no interaction row means undecided, not excluded
          * only 'saved' and 'passed' express intent; 'seen', 'apply_opened'
            and 'undo' leave it empty
          * an application's stage outranks intent, so saving something again
            never demotes it out of 'interview'
          * a missing score is 0, and the score join is pinned to one ruleset
            version so it cannot return two rows for one opportunity

        Filtering and sorting downstream use these computed columns, never the
        raw join columns, so a row with no interaction is never dropped by a
        comparison against NULL and a missing score never sorts first.
        """

        return (
            """
            WITH latest_action AS (
                SELECT interaction.opportunity_id AS opportunity_id,
                       interaction.action AS action
                FROM opportunity_interactions interaction
                WHERE interaction.user_id = ?
                  AND interaction.id = (
                      SELECT MAX(candidate.id)
                      FROM opportunity_interactions candidate
                      WHERE candidate.opportunity_id = interaction.opportunity_id
                        AND candidate.user_id = interaction.user_id
                  )
            ),
            tenant AS (
                SELECT """
            + ", ".join(f"o.{column}" for column in self._INVENTORY_COLUMNS)
            + """,
                    COALESCE(fit.score, 0) AS score,
                    COALESCE(fit.explanation_json, '[]') AS score_explanation,
                    COALESCE(fit.ruleset_version, '') AS ruleset_version,
                    fit.created_at AS score_created_at,
                    CASE
                        WHEN latest.action IN ('saved', 'passed') THEN latest.action
                        ELSE ''
                    END AS intent_state,
                    CASE
                        WHEN app.stage IS NOT NULL THEN app.stage
                        WHEN latest.action = 'saved' THEN 'shortlisted'
                        ELSE 'discovered'
                    END AS status,
                    COALESCE(app.notes, '') AS notes,
                    app.applied_at AS applied_at,
                    app.follow_up_at AS follow_up_at
                FROM opportunity_read_model o
                LEFT JOIN fit_scores fit
                    ON fit.opportunity_id = o.id
                    AND fit.user_id = ?
                    AND fit.ruleset_version = 'legacy-v1'
                LEFT JOIN applications app
                    ON app.opportunity_id = o.id
                    AND app.user_id = ?
                LEFT JOIN latest_action latest ON latest.opportunity_id = o.id
            ) """,
            [self.user_id, self.user_id, self.user_id],
        )

    def _tenant_sql(
        self,
        filters: OpportunityFilters,
        projection: str,
        *,
        opportunity_id: str | None = None,
        suffix: str = "",
    ) -> tuple[str, list[Any]]:
        cte, params = self._tenant_cte()
        where_sql, where_params = _where(filters, alias="tenant", user_id=self.user_id)
        if opportunity_id is not None:
            joiner = " AND " if where_sql else " WHERE "
            where_sql += f"{joiner}tenant.id = ?"
            where_params = [*where_params, opportunity_id]
        # A manual capture belongs to the student who captured it; every other
        # tenant sees shared inventory only. Same rule as the Urgent queue.
        joiner = " AND " if where_sql else " WHERE "
        where_sql += f"{joiner}{capture_visible_sql('tenant')}"
        where_params = [*where_params, self.user_id]
        return (
            f"{cte}SELECT {projection} FROM tenant{where_sql}{suffix}",
            [*params, *where_params],
        )

    def _order_by(self, sort: str, alias: str = "o") -> str:
        """SORT_SQL for one alias, with a collation the backend agrees on.

        Storing the casefold unified the *transformation*, but not the
        comparison: SQLite compares TEXT byte-wise while PostgreSQL uses its
        database locale, where 'ørsted' can order against 'zebra' differently
        and locale-equal strings can bypass the title and id tie-breakers.
        `COLLATE "C"` asks PostgreSQL for byte order, which is what SQLite
        already does, so the two produce the same list. SQLite has no such
        collation name, hence the dialect check rather than one literal.
        """

        clause = SORT_SQL[sort]
        if alias != "o":
            clause = clause.replace("o.", f"{alias}.")
        if getattr(self.connection, "backend", "sqlite") == "postgresql":
            for column in ("company_sort_key", "title_sort_key"):
                clause = clause.replace(f"{alias}.{column} ASC", f'{alias}.{column} COLLATE "C" ASC')
        return clause

    def list(self, filters: OpportunityFilters) -> tuple[list[dict[str, Any]], int]:
        filters = filters.normalized()
        if self.user_id is not None:
            total = int(
                self.connection.execute(
                    *self._tenant_sql(filters, "COUNT(*)")
                ).fetchone()[0]
            )
            sql, params = self._tenant_sql(
                filters,
                "*",
                suffix=" ORDER BY " + self._order_by(filters.sort, "tenant") + " LIMIT ? OFFSET ?",
            )
            rows = self.connection.execute(
                sql, [*params, filters.limit, filters.offset]
            ).fetchall()
            return [_row_to_opportunity(row) for row in rows], total
        where_sql, params = _where(filters)
        total = int(
            self.connection.execute(
                "SELECT COUNT(*) FROM opportunity_read_model o" + where_sql,
                params,
            ).fetchone()[0]
        )
        rows = self.connection.execute(
            "SELECT o.* FROM opportunity_read_model o"
            + where_sql
            + " ORDER BY "
            + self._order_by(filters.sort)
            + " LIMIT ? OFFSET ?",
            [*params, filters.limit, filters.offset],
        ).fetchall()
        return [_row_to_opportunity(row) for row in rows], total

    def get(self, opportunity_id: str) -> dict[str, Any] | None:
        """Read one opportunity, with the caller's own state overlaid.

        Both paths go through `opportunity_read_model`, which is what enforces
        source eligibility: it hides opportunities whose only source is
        disabled or blocked, and picks exactly one primary source per
        opportunity. Querying `opportunities` directly would expose
        blocked-only records; joining `opportunity_sources` would duplicate
        the row.

        Inactive rows and duplicates stay reachable by id, because a deep link
        to one must resolve rather than 404.
        """

        if self.user_id is None:
            row = self.connection.execute(
                "SELECT * FROM opportunity_read_model WHERE id = ?",
                (opportunity_id,),
            ).fetchone()
            return _row_to_opportunity(row) if row else None
        sql, params = self._tenant_sql(
            OpportunityFilters(active_only=False, unique_only=False),
            "*",
            opportunity_id=opportunity_id,
            suffix=" LIMIT 1",
        )
        row = self.connection.execute(sql, params).fetchone()
        return _row_to_opportunity(row) if row else None

    def facets(self) -> dict[str, list[str]]:
        # `opportunity_read_model` is an expensive view: a window function over
        # every source plus a correlated latest-interaction subquery. This used
        # to run five SELECT DISTINCTs and a terms scan against it, evaluating
        # the whole view six times for one request. One scan answers all of
        # them, and the DISTINCT sets are small enough to build in Python.
        columns = {
            "role_types": "role_type",
            "statuses": "status",
            "regions": "region",
            "sources": "source_name",
            "remote_modes": "remote_mode",
        }
        collected: dict[str, set[str]] = {key: set() for key in columns}
        terms: set[str] = set()
        if self.user_id is None:
            cursor = self.connection.execute(
                "SELECT role_type, status, region, source_name, remote_mode, terms_json "
                "FROM opportunity_read_model WHERE active=1 AND duplicate_of IS NULL"
            )
        else:
            # The tenant's own statuses, from the same single scan -- the view's
            # `status` column belongs to local-user.
            cursor = self.connection.execute(
                *self._tenant_sql(
                    OpportunityFilters(),
                    "tenant.role_type, tenant.status, tenant.region, "
                    "tenant.source_name, tenant.remote_mode, tenant.terms_json",
                )
            )
        for row in cursor:
            for key, column in columns.items():
                value = row[column]
                # The previous per-column query filtered `<> ''`; NULL never
                # satisfied that comparison either, so both are dropped here.
                if value is not None and str(value) != "":
                    collected[key].add(str(value))
            terms.update(str(term) for term in _decode_list(row["terms_json"]))
        # These five were ordered by SQL with COLLATE NOCASE, so they keep that
        # ordering exactly -- see _nocase_key for why casefold is not a
        # substitute. `terms` was already sorted in Python with casefold.
        result: dict[str, list[str]] = {
            key: sorted(values, key=_nocase_key) for key, values in collected.items()
        }
        result["terms"] = sorted(terms, key=str.casefold)
        return result

    def stats(self) -> dict[str, int]:
        # Deliberately unpaged: these count the whole inventory, which is why
        # they get their own query rather than sharing the listing one.
        if self.user_id is not None:
            sql, params = self._tenant_sql(
                OpportunityFilters(active_only=False, unique_only=False),
                """
                COUNT(*) AS total,
                SUM(CASE WHEN tenant.active=1 AND tenant.duplicate_of IS NULL THEN 1 ELSE 0 END) AS active_unique,
                SUM(CASE WHEN tenant.active=1 AND tenant.duplicate_of IS NULL AND tenant.status='discovered' THEN 1 ELSE 0 END) AS discovered,
                SUM(CASE WHEN tenant.active=1 AND tenant.duplicate_of IS NULL AND tenant.status<>'discovered' THEN 1 ELSE 0 END) AS tracked,
                MAX(CASE WHEN tenant.active=1 AND tenant.duplicate_of IS NULL THEN tenant.score ELSE NULL END) AS top_score
                """,
            )
            row = self.connection.execute(sql, params).fetchone()
            return {key: int(row[key] or 0) for key in row.keys()}
        row = self.connection.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN active=1 AND duplicate_of IS NULL THEN 1 ELSE 0 END) AS active_unique,
                SUM(CASE WHEN active=1 AND duplicate_of IS NULL AND status='discovered' THEN 1 ELSE 0 END) AS discovered,
                SUM(CASE WHEN active=1 AND duplicate_of IS NULL AND status<>'discovered' THEN 1 ELSE 0 END) AS tracked,
                MAX(CASE WHEN active=1 AND duplicate_of IS NULL THEN score ELSE NULL END) AS top_score
            FROM opportunity_read_model
            """
        ).fetchone()
        return {key: int(row[key] or 0) for key in row.keys()}


def ids(items: Iterable[dict[str, Any]]) -> list[str]:
    """Return IDs for concise parity assertions."""

    return [str(item["id"]) for item in items]
