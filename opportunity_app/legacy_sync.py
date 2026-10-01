"""One-way sync of the legacy pipeline database into the product database.

The target is intentionally a new database. The existing pipeline database is
opened read-only and is never mutated by this migration. Every run upserts the
postings, retires the ones the legacy pipeline no longer holds, and carries the
shortlist and application stages across.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from pipeline_core import OpportunityFilters, OpportunityRepository
from pipeline_core.identity import sort_key
from pipeline_core.read_model import RULESET_VERSION
from pipeline_core.regions import region_label

from . import DEFAULT_LEGACY_DB, DEFAULT_PLATFORM_DB, DEFAULT_PROFILE
from .company_tags import regenerate_company_tags
from .database import connect_legacy_read_only, connect_product, is_postgres_target
from .opportunity_metadata import extract_opportunity_metadata
from .schema import LOCAL_USER_ID, ensure_product_schema
from .timestamps import canonical_utc, utc_now

# The migration_runs key of the one-time import from the legacy database. Not the
# ruleset version (`pipeline_core.read_model.RULESET_VERSION`), though the string is the same.
LEGACY_MIGRATION_KEY = "legacy-v1"
APPLICATION_STATUSES = {"applying", "applied", "interview", "offer", "rejected", "withdrawn"}


def _load_profile(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class MigrationResult:
    source_count: int
    imported_count: int
    active_unique_source: int
    active_unique_target: int
    top_ids_match: bool
    source_path: str
    target_path: str
    retired_missing: int = 0


def _insert_user_and_profile(
    target: sqlite3.Connection,
    profile: dict[str, Any],
    timestamp: str,
    profile_changed_at: str | None = None,
) -> None:
    target.execute(
        """
        INSERT INTO users(id, email, display_name, role, created_at, updated_at)
        VALUES(?, NULL, ?, 'student', ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            display_name=excluded.display_name,
            updated_at=excluded.updated_at
        """,
        (LOCAL_USER_ID, str(profile.get("name", "")), timestamp, timestamp),
    )
    target.execute(
        """
        INSERT INTO profiles(user_id, profile_json, confirmed_at, created_at, updated_at)
        VALUES(?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO NOTHING
        """,
        (LOCAL_USER_ID, json.dumps(profile), timestamp, timestamp, timestamp),
    )
    stored = target.execute(
        "SELECT profile_json FROM profiles WHERE user_id=?",
        (LOCAL_USER_ID,),
    ).fetchone()
    fact_count = target.execute(
        "SELECT COUNT(*) FROM profile_facts WHERE user_id=?",
        (LOCAL_USER_ID,),
    ).fetchone()[0]
    stored_profile = json.loads(stored["profile_json"] or "{}") if stored else {}
    if stored and int(fact_count) == 0 and stored_profile == profile:
        for field, value in profile.items():
            target.execute(
                """
                INSERT OR IGNORE INTO profile_facts(
                    user_id, field_path, value_json, source, confirmed, created_at, updated_at
                ) VALUES(?, ?, ?, 'legacy_profile', 1, ?, ?)
                """,
                (LOCAL_USER_ID, field, json.dumps(value), timestamp, timestamp),
            )
        return
    # config/profile.json edited since the product database last changed the
    # profile (by hand, or by an agent following SETUP.md): the newer copy wins,
    # the same way a web edit is written back to the file (profile.py).
    updated_at = target.execute(
        "SELECT updated_at FROM profiles WHERE user_id=?", (LOCAL_USER_ID,)
    ).fetchone()
    if (
        stored
        and profile_changed_at
        and updated_at
        and profile_changed_at > str(updated_at[0])
        and stored_profile != profile
    ):
        merged = {**stored_profile, **profile}
        target.execute(
            "UPDATE profiles SET profile_json=?, updated_at=? WHERE user_id=?",
            (json.dumps(merged), timestamp, LOCAL_USER_ID),
        )
        for field, value in profile.items():
            if stored_profile.get(field) == value:
                continue
            target.execute(
                """
                INSERT INTO profile_facts(
                    user_id, field_path, value_json, source, confirmed, created_at, updated_at
                ) VALUES(?, ?, ?, 'profile_file', 1, ?, ?)
                ON CONFLICT(user_id, field_path) DO UPDATE SET
                    value_json=excluded.value_json,
                    source=excluded.source,
                    confirmed=1,
                    updated_at=excluded.updated_at
                """,
                (LOCAL_USER_ID, field, json.dumps(value), timestamp, timestamp),
            )


def _upsert_opportunity(
    target: sqlite3.Connection,
    job: sqlite3.Row,
    profile: dict[str, Any],
    timestamp: str,
    columns: set[str],
) -> None:
    content_fingerprint = job["content_fingerprint"] if "content_fingerprint" in columns else ""
    attributes = extract_opportunity_metadata(job["title"], job["location"], job["description"])
    target.execute(
        """
        INSERT INTO opportunities(
            id, company, title, company_sort_key, title_sort_key, location, region,
            role_type, url, description,
            posted_at, posted_at_utc, deadline_at, first_seen_at, last_seen_at, active,
            fingerprint, content_fingerprint, duplicate_of, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            company=excluded.company,
            title=excluded.title,
            company_sort_key=excluded.company_sort_key,
            title_sort_key=excluded.title_sort_key,
            location=excluded.location,
            region=excluded.region,
            role_type=excluded.role_type,
            url=excluded.url,
            description=excluded.description,
            posted_at=excluded.posted_at,
            posted_at_utc=excluded.posted_at_utc,
            deadline_at=excluded.deadline_at,
            first_seen_at=excluded.first_seen_at,
            last_seen_at=excluded.last_seen_at,
            active=excluded.active,
            fingerprint=excluded.fingerprint,
            content_fingerprint=excluded.content_fingerprint,
            updated_at=excluded.updated_at
        """,
        (
            job["id"],
            job["company"],
            job["title"],
            sort_key(job["company"]),
            sort_key(job["title"]),
            job["location"],
            region_label(job["location"], profile),
            job["role_type"],
            job["url"],
            job["description"],
            job["posted_at"],
            # Derived here, beside the raw value, so the two can never drift:
            # the sync replaces posted_at on every run, and a stale
            # posted_at_utc would rank the card by a date it no longer shows.
            canonical_utc(job["posted_at"]),
            attributes["deadline_at"],
            job["first_seen_at"],
            job["last_seen_at"],
            int(job["active"]),
            job["fingerprint"],
            content_fingerprint,
            job["first_seen_at"] or timestamp,
            timestamp,
        ),
    )
    target.execute(
        """
        INSERT INTO opportunity_attributes(
            opportunity_id, remote_mode, terms_json, graduation_years_json,
            pay_min, pay_max, pay_period, currency, extracted_json, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(opportunity_id) DO UPDATE SET
            remote_mode=excluded.remote_mode,
            terms_json=excluded.terms_json,
            graduation_years_json=excluded.graduation_years_json,
            pay_min=excluded.pay_min,
            pay_max=excluded.pay_max,
            pay_period=excluded.pay_period,
            currency=excluded.currency,
            extracted_json=excluded.extracted_json,
            updated_at=excluded.updated_at
        """,
        (
            job["id"],
            attributes["remote_mode"],
            json.dumps(attributes["terms"]),
            json.dumps(attributes["graduation_years"]),
            attributes["pay_min"],
            attributes["pay_max"],
            attributes["pay_period"],
            attributes["currency"],
            json.dumps(attributes),
            timestamp,
        ),
    )
    target.execute(
        """
        INSERT INTO opportunity_sources(
            opportunity_id, source_key, source_name, external_id, source_url,
            first_seen_at, last_seen_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(opportunity_id, source_key, external_id) DO UPDATE SET
            source_name=excluded.source_name,
            source_url=excluded.source_url,
            last_seen_at=excluded.last_seen_at
        """,
        (
            job["id"],
            job["source_key"],
            job["source_name"],
            job["external_id"],
            job["url"],
            job["first_seen_at"],
            job["last_seen_at"],
        ),
    )
    target.execute(
        """
        INSERT INTO fit_scores(
            opportunity_id, user_id, ruleset_version, score,
            explanation_json, created_at
        ) VALUES(?, ?, ?, ?, ?, ?)
        ON CONFLICT(opportunity_id, user_id, ruleset_version) DO UPDATE SET
            score=excluded.score,
            explanation_json=excluded.explanation_json,
            created_at=excluded.created_at
        """,
        (
            job["id"],
            LOCAL_USER_ID,
            RULESET_VERSION,
            int(job["score"]),
            job["score_explanation"],
            timestamp,
        ),
    )


def _migrate_status(target: sqlite3.Connection, job: sqlite3.Row, timestamp: str) -> None:
    status = str(job["status"])
    if status == "shortlisted":
        target.execute(
            """
            INSERT OR IGNORE INTO opportunity_interactions(
                opportunity_id, user_id, action, created_at
            ) VALUES(?, ?, 'saved', ?)
            """,
            (job["id"], LOCAL_USER_ID, job["first_seen_at"] or timestamp),
        )
        return
    if status not in APPLICATION_STATUSES:
        return
    application_id = f"app-{job['id']}"
    created_at = job["applied_at"] or job["first_seen_at"] or timestamp
    target.execute(
        """
        INSERT INTO applications(
            id, opportunity_id, user_id, stage, notes, applied_at,
            follow_up_at, created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(opportunity_id, user_id) DO UPDATE SET
            stage=excluded.stage,
            notes=excluded.notes,
            applied_at=excluded.applied_at,
            follow_up_at=excluded.follow_up_at,
            updated_at=excluded.updated_at
        """,
        (
            application_id,
            job["id"],
            LOCAL_USER_ID,
            status,
            job["notes"],
            job["applied_at"],
            job["follow_up_at"],
            created_at,
            timestamp,
        ),
    )
    target.execute(
        """
        INSERT OR IGNORE INTO application_events(
            application_id, event_type, from_stage, to_stage,
            detail_json, created_at
        ) VALUES(?, 'legacy_import', NULL, ?, '{}', ?)
        """,
        (application_id, status, created_at),
    )


def _target_legacy_ordered_ids(target: sqlite3.Connection, limit: int = 200) -> list[str]:
    """The target's top IDs under the *legacy* ordering, for the parity check.

    This deliberately does not go through `OpportunityRepository.list`. That
    orders by `posted_at_utc`, which is the corrected chronological order and
    therefore, for equal scores with dates written in different spellings, a
    different order from the legacy `COALESCE(posted_at, last_seen_at)` text
    sort -- by design.

    Parity asks "did every row survive the migration intact", so both sides of
    it must be ordered the same way. Ordering one side the new way and the
    other the old way would fail the check every day on a difference that is
    the improvement, not a defect. Product ranking is asserted separately.
    """

    return [
        str(row[0])
        for row in target.execute(
            """
            SELECT id FROM opportunity_read_model
            WHERE active=1 AND duplicate_of IS NULL
            ORDER BY score DESC, COALESCE(posted_at, last_seen_at) DESC, id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    ]


def _legacy_active_ids(source: sqlite3.Connection, limit: int = 200) -> list[str]:
    return [
        str(row[0])
        for row in source.execute(
            """
            SELECT id FROM jobs
            WHERE active=1 AND duplicate_of IS NULL
            ORDER BY score DESC, COALESCE(posted_at, last_seen_at) DESC, id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    ]


def _retire_missing_opportunities(target: sqlite3.Connection, legacy_ids: set[str], timestamp: str) -> int:
    """Retire product rows the legacy pipeline no longer holds.

    The legacy purge deletes postings outright, so without this a posting the
    employer took down stays active here forever. Manual captures never came
    from the legacy database and are left alone.
    """
    active_ids = [
        str(row[0])
        # Bind the pattern: psycopg treats a literal % in the SQL as a placeholder.
        for row in target.execute(
            "SELECT id FROM opportunities WHERE active=1 AND id NOT LIKE ?", ("manual-%",)
        )
    ]
    missing = [opportunity_id for opportunity_id in active_ids if opportunity_id not in legacy_ids]
    for start in range(0, len(missing), 500):
        chunk = missing[start : start + 500]
        placeholders = ",".join("?" for _ in chunk)
        target.execute(
            f"UPDATE opportunities SET active=0, updated_at=? WHERE id IN ({placeholders})",
            (timestamp, *chunk),
        )
    return len(missing)


def migrate_legacy_database(
    source_path: Path = DEFAULT_LEGACY_DB,
    target_path: Path | str = DEFAULT_PLATFORM_DB,
    profile_path: Path = DEFAULT_PROFILE,
    progress: Callable[[int, int], None] | None = None,
) -> MigrationResult:
    """Sync the product database from the legacy one.

    ``progress(done, total)`` is called as postings are written, for callers
    that show a progress bar.
    """
    source_path = source_path.expanduser().resolve()
    target_path = str(target_path) if is_postgres_target(target_path) else Path(target_path).expanduser().resolve()
    profile_path = profile_path.expanduser().resolve()
    profile = _load_profile(profile_path)
    started_at = utc_now()

    with closing(connect_legacy_read_only(source_path)) as source, closing(
        connect_product(target_path)
    ) as target:
        ensure_product_schema(target)
        jobs = source.execute("SELECT * FROM jobs ORDER BY first_seen_at, id").fetchall()
        columns = {str(row[1]) for row in source.execute("PRAGMA table_info(jobs)")}
        with target:
            _insert_user_and_profile(
                target,
                profile,
                started_at,
                datetime.fromtimestamp(profile_path.stat().st_mtime, timezone.utc).isoformat(
                    timespec="microseconds"
                ),
            )
            total = len(jobs) * 2
            for index, job in enumerate(jobs, start=1):
                _upsert_opportunity(target, job, profile, started_at, columns)
                if progress and (index % 25 == 0 or index == len(jobs)):
                    progress(index, total)
            # Add duplicate links only after every referenced row exists.
            for index, job in enumerate(jobs, start=len(jobs) + 1):
                target.execute(
                    "UPDATE opportunities SET duplicate_of=? WHERE id=?",
                    (job["duplicate_of"], job["id"]),
                )
                _migrate_status(target, job, started_at)
                if progress and index % 25 == 0:
                    progress(index, total)
            retired_missing = _retire_missing_opportunities(
                target, {str(job["id"]) for job in jobs}, started_at
            )
            # Tags follow the postings, so they are rebuilt in the same
            # transaction; a student's own tag edits are left alone.
            regenerate_company_tags(target)
            if progress:
                progress(total, total)

            finished_at = utc_now()
            target.execute(
                """
                INSERT INTO migration_runs(
                    migration_key, source_path, source_count, imported_count,
                    started_at, finished_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (LEGACY_MIGRATION_KEY, str(source_path), len(jobs), len(jobs), started_at, finished_at),
            )

        source_active_ids = _legacy_active_ids(source)
        target_ids = _target_legacy_ordered_ids(target)
        _, target_count = OpportunityRepository(target).list(OpportunityFilters(limit=200))

        return MigrationResult(
            source_count=len(jobs),
            imported_count=int(target.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]),
            active_unique_source=int(
                source.execute(
                    "SELECT COUNT(*) FROM jobs WHERE active=1 AND duplicate_of IS NULL"
                ).fetchone()[0]
            ),
            active_unique_target=target_count,
            top_ids_match=source_active_ids == target_ids,
            source_path=str(source_path),
            target_path=str(target_path),
            retired_missing=retired_missing,
        )


def result_dict(result: MigrationResult) -> dict[str, Any]:
    return asdict(result)
