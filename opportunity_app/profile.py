"""Confirmed student profile storage and onboarding completeness."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

from pipeline import score_job

from .schema import LOCAL_USER_ID, utc_now


ALLOWED_PROFILE_FIELDS = {
    "name",
    "school",
    "degree",
    "graduation_year",
    "degree_keywords",
    "preferred_role_types",
    "preferred_locations",
    "regions",
    "break_location",
    "out_of_region_penalty",
    "remote_ok",
    "willing_to_relocate",
    "skills",
    "interest_keywords",
    "deprioritize_title_keywords",
    "max_years_experience",
    "work_authorized_us",
    "us_citizen",
    "requires_sponsorship",
    "hours_per_week",
    "available_terms",
    "compensation_preferences",
    "contact",
    "summary",
    "education",
    "experience",
    "projects",
    "awards",
    "activities",
}

COMPLETENESS_FIELDS = (
    "name",
    "school",
    "degree",
    "graduation_year",
    "skills",
    "interest_keywords",
    "available_terms",
    "regions",
    "work_authorized_us",
    "requires_sponsorship",
    "compensation_preferences",
)


# Profile fields pipeline.score_job reads. With none set, every score is the
# unexplained base score, so the UI must say matches are not personalized yet.
SCORING_FIELDS = (
    "preferred_role_types",
    "degree_keywords",
    "interest_keywords",
    "deprioritize_title_keywords",
    "skills",
    "regions",
    "preferred_locations",
    "remote_ok",
    "willing_to_relocate",
    "available_terms",
    "max_years_experience",
)


def _has_value(value: Any) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return True


# Keys a form fills in on its own; their presence is not an answer.
_DEFAULTED_KEYS = {"currency"}


def is_answered(value: Any) -> bool:
    """Whether a value records something the student actually said.

    A saved form submits every field, so "Not answered" arrives as null, "",
    [] or a dict of nulls. Treating those as confirmed would present an
    unknown as a confirmed fact.
    """
    if isinstance(value, dict):
        return any(is_answered(item) for key, item in value.items() if key not in _DEFAULTED_KEYS)
    return _has_value(value)


def profile_completeness(profile: dict[str, Any]) -> dict[str, Any]:
    missing = [field for field in COMPLETENESS_FIELDS if not is_answered(profile.get(field))]
    completed = len(COMPLETENESS_FIELDS) - len(missing)
    return {
        "completed": completed,
        "total": len(COMPLETENESS_FIELDS),
        "percent": round(completed / len(COMPLETENESS_FIELDS) * 100),
        "missing": missing,
        "onboarding_complete": not missing,
    }


def is_personalized(conn: sqlite3.Connection, *, user_id: str) -> bool:
    """Whether any scoring input is set, without provisioning a profile row."""

    row = conn.execute("SELECT profile_json FROM profiles WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return False
    try:
        profile = json.loads(row[0] or "{}")
    except (TypeError, json.JSONDecodeError):
        return False
    return isinstance(profile, dict) and any(_has_value(profile.get(field)) for field in SCORING_FIELDS)


def _compute_scores(
    conn: sqlite3.Connection, profile: dict[str, Any]
) -> list[tuple[Any, int, list[str]]]:
    """Score every opportunity for ``profile`` in memory; writes nothing."""

    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT id, title, description, role_type, location, posted_at
           FROM opportunities"""
    ).fetchall()
    scores = []
    for row in rows:
        score, reasons = score_job(row, profile)
        scores.append((row["id"], score, reasons))
    return scores


def _write_scores(
    conn: sqlite3.Connection,
    scores: list[tuple[Any, int, list[str]]],
    *,
    user_id: str,
    timestamp: str,
) -> None:
    """Upsert computed scores. The caller owns the transaction."""

    for opportunity_id, score, reasons in scores:
        conn.execute(
            """INSERT INTO fit_scores(
                   opportunity_id, user_id, ruleset_version, score,
                   explanation_json, created_at
               ) VALUES(?, ?, 'legacy-v1', ?, ?, ?)
               ON CONFLICT(opportunity_id, user_id, ruleset_version) DO UPDATE SET
                   score=excluded.score,
                   explanation_json=excluded.explanation_json,
                   created_at=excluded.created_at""",
            (opportunity_id, user_id, score, json.dumps(reasons), timestamp),
        )


def rescore_profile(
    conn: sqlite3.Connection, profile: dict[str, Any], *, user_id: str
) -> int:
    """Persist the legacy deterministic score for one authenticated student."""

    scores = _compute_scores(conn, profile)
    timestamp = utc_now()
    with conn:
        _write_scores(conn, scores, user_id=user_id, timestamp=timestamp)
    return len(scores)


def get_profile(conn: sqlite3.Connection, *, user_id: str) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM profiles WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        # Newly registered users have no profile row yet; provision a default
        # instead of failing. Only known users may be provisioned.
        known = conn.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
        if not known:
            raise LookupError(user_id)
        timestamp = utc_now()
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO profiles(user_id, profile_json, created_at, updated_at) VALUES(?, '{}', ?, ?)",
                (user_id, timestamp, timestamp),
            )
        row = conn.execute("SELECT * FROM profiles WHERE user_id=?", (user_id,)).fetchone()
        if not row:
            raise LookupError(user_id)
        rescore_profile(conn, {}, user_id=user_id)
    profile = json.loads(row["profile_json"] or "{}")
    facts = conn.execute(
        """
        SELECT field_path, value_json, source, confirmed, updated_at
        FROM profile_facts WHERE user_id=? ORDER BY field_path
        """,
        (user_id,),
    ).fetchall()
    return {
        "profile": profile,
        "confirmed_fields": [fact["field_path"] for fact in facts if fact["confirmed"]],
        "facts": [
            {
                "field_path": fact["field_path"],
                "value": json.loads(fact["value_json"]),
                "source": fact["source"],
                "confirmed": bool(fact["confirmed"]),
                "updated_at": fact["updated_at"],
            }
            for fact in facts
        ],
        "completeness": profile_completeness(profile),
        "updated_at": row["updated_at"],
    }


# Every field a web save may set, and the JSON types it may hold. Scoring and
# the outreach tools read these, so a wrong type must be refused before it is
# stored rather than coerced into a value the student never gave.
_TEXT_FIELDS = ("name", "school", "degree", "break_location", "summary")
_TEXT_LIST_FIELDS = (
    "degree_keywords",
    "preferred_role_types",
    "preferred_locations",
    "skills",
    "interest_keywords",
    "deprioritize_title_keywords",
    "available_terms",
)
_INTEGER_FIELDS = ("max_years_experience", "out_of_region_penalty", "hours_per_week")
_BOOLEAN_FIELDS = (
    "work_authorized_us",
    "us_citizen",
    "requires_sponsorship",
    "remote_ok",
    "willing_to_relocate",
)
_STRUCTURED_FIELDS = ("education", "experience", "projects", "awards", "activities")
_REGION_TEXT_LISTS = ("state_markers", "places", "aliases")


def _is_integer(value: Any) -> bool:
    # bool is an int subclass; true/false is not a number of anything.
    return isinstance(value, int) and not isinstance(value, bool)


def _text_list_error(label: str, value: Any) -> str | None:
    if not isinstance(value, list):
        return f"{label} must be a list of text values"
    for index, item in enumerate(value):
        if not isinstance(item, str):
            return f"{label}[{index}] must be text"
    return None


def _region_errors(value: Any) -> list[str]:
    if not isinstance(value, list):
        return ["regions must be a list of region objects"]
    errors = []
    for index, region in enumerate(value):
        label = f"regions[{index}]"
        if not isinstance(region, dict):
            errors.append(f"{label} must be an object with a name")
            continue
        name = region.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{label}.name must be non-empty text")
        for key in _REGION_TEXT_LISTS:
            # Optional, and may be empty: the profile form creates regions
            # with no state markers.
            if key in region:
                error = _text_list_error(f"{label}.{key}", region[key])
                if error:
                    errors.append(error)
        if "bonus" in region and not _is_integer(region["bonus"]):
            errors.append(f"{label}.bonus must be a whole number")
        if region.get("phrase") is not None and not isinstance(region["phrase"], str):
            errors.append(f"{label}.phrase must be text")
    return errors


def _compensation_errors(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return ["compensation_preferences must be an object"]
    errors = []
    paid_only = value.get("paid_only")
    if paid_only is not None and not isinstance(paid_only, bool):
        errors.append("compensation_preferences.paid_only must be true, false, or not answered")
    minimum = value.get("minimum_hourly")
    if minimum is not None and (isinstance(minimum, bool) or not isinstance(minimum, (int, float))):
        errors.append("compensation_preferences.minimum_hourly must be a number")
    currency = value.get("currency")
    if currency is not None and not isinstance(currency, str):
        errors.append("compensation_preferences.currency must be text")
    return errors


def validate_profile_types(profile: dict[str, Any]) -> None:
    """Refuse a web profile save whose values have the wrong JSON types.

    Types only. Completeness is a separate question: the profile form
    legitimately saves regions with no state markers, and fields left
    unanswered arrive as null. A container that fails its own check is never
    traversed. Raises ValueError naming every problem found.
    """

    errors: list[str] = []
    for field, value in profile.items():
        if value is None:
            continue
        if field in _TEXT_FIELDS:
            if not isinstance(value, str):
                errors.append(f"{field} must be text")
        elif field in _TEXT_LIST_FIELDS:
            error = _text_list_error(field, value)
            if error:
                errors.append(error)
        elif field in _INTEGER_FIELDS:
            if not _is_integer(value):
                errors.append(f"{field} must be a whole number")
        elif field in _BOOLEAN_FIELDS:
            if not isinstance(value, bool):
                errors.append(f"{field} must be true, false, or not answered")
        elif field == "graduation_year":
            if not (_is_integer(value) or isinstance(value, str)):
                errors.append("graduation_year must be a year")
        elif field == "regions":
            errors.extend(_region_errors(value))
        elif field == "compensation_preferences":
            errors.extend(_compensation_errors(value))
        elif field == "contact":
            if not isinstance(value, dict):
                errors.append("contact must be an object")
        elif field in _STRUCTURED_FIELDS:
            if not isinstance(value, (list, dict)):
                errors.append(f"{field} must be a list or an object")
    if errors:
        raise ValueError("Invalid profile: " + "; ".join(errors))


def _stored_profile(conn: sqlite3.Connection, *, user_id: str) -> dict[str, Any]:
    """The saved profile, or {} when the user has no profile row yet.

    Unlike get_profile this never provisions a row, so a refused save leaves
    nothing behind.
    """

    row = conn.execute("SELECT profile_json FROM profiles WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        if not conn.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone():
            raise LookupError(user_id)
        return {}
    try:
        profile = json.loads(row[0] or "{}")
    except (TypeError, ValueError):
        return {}
    return profile if isinstance(profile, dict) else {}


def _restore_file(path: Path, previous: bytes | None) -> None:
    """Put the profile file back exactly as it was before a failed save."""
    if previous is None:
        path.unlink(missing_ok=True)
        return
    temp = path.with_suffix(".json.restore")
    temp.write_bytes(previous)
    temp.replace(path)


# Overlapping saves must not interleave one save's file write with another's
# rollback of it.
_PROFILE_SAVE_LOCK = threading.Lock()


def update_profile(
    conn: sqlite3.Connection,
    updates: dict[str, Any],
    confirmed_fields: list[str],
    *,
    source: str = "user",
    user_id: str,
    profile_file: Path | None = None,
) -> dict[str, Any]:
    """Validate, merge, and store confirmed updates to a student's profile.

    For the local owner, ``profile_file`` is config/profile.json: pipeline.py
    scores from that file, and each refresh copies its scores over these, so an
    edit kept only in the database would be undone by the next refresh. The
    file and the database therefore move together: an invalid save, a scoring
    failure, or a failed database write leaves both as they were.
    """
    unknown = sorted(set(updates) - ALLOWED_PROFILE_FIELDS)
    if unknown:
        raise ValueError(f"Unsupported profile fields: {', '.join(unknown)}")
    confirmed = set(confirmed_fields)
    if not confirmed.issubset(ALLOWED_PROFILE_FIELDS):
        raise ValueError("confirmed_fields contains an unsupported field")
    with _PROFILE_SAVE_LOCK:
        merged = {**_stored_profile(conn, user_id=user_id), **updates}
        validate_profile_types({key: value for key, value in merged.items() if key in ALLOWED_PROFILE_FIELDS})
        # Scoring is pure: a failure here has written nothing yet.
        scores = _compute_scores(conn, merged)
        timestamp = utc_now()

        mirror_file = profile_file if profile_file is not None and user_id == LOCAL_USER_ID else None
        previous: bytes | None = None
        if mirror_file is not None:
            previous = mirror_file.read_bytes() if mirror_file.exists() else None
            write_profile_file(mirror_file, merged)
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO profiles(user_id, profile_json, confirmed_at, created_at, updated_at)
                    VALUES(?, ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        profile_json=excluded.profile_json,
                        confirmed_at=excluded.confirmed_at,
                        updated_at=excluded.updated_at
                    """,
                    (
                        user_id,
                        json.dumps(merged),
                        timestamp if confirmed else None,
                        timestamp,
                        timestamp,
                    ),
                )
                for field in updates:
                    if field in confirmed and is_answered(merged[field]):
                        conn.execute(
                            """
                            INSERT INTO profile_facts(
                                user_id, field_path, value_json, source, confirmed,
                                created_at, updated_at
                            ) VALUES(?, ?, ?, ?, 1, ?, ?)
                            ON CONFLICT(user_id, field_path) DO UPDATE SET
                                value_json=excluded.value_json,
                                source=excluded.source,
                                confirmed=1,
                                updated_at=excluded.updated_at
                            """,
                            (
                                user_id,
                                field,
                                json.dumps(merged[field]),
                                source,
                                timestamp,
                                timestamp,
                            ),
                        )
                    else:
                        # Editing a field without confirming it, or clearing it
                        # to no answer, revokes any previous confirmation rather
                        # than leaving stale evidence behind.
                        conn.execute(
                            "DELETE FROM profile_facts WHERE user_id=? AND field_path=?",
                            (user_id, field),
                        )
                _write_scores(conn, scores, user_id=user_id, timestamp=timestamp)
        except BaseException:
            if mirror_file is not None:
                _restore_file(mirror_file, previous)
            raise
        return get_profile(conn, user_id=user_id)


def write_profile_file(path: Path, profile: dict[str, Any]) -> None:
    """Write the profile over the file, keeping any keys only the file has."""
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        existing = {}
    merged = {**(existing if isinstance(existing, dict) else {}), **profile}
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(merged, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temp.replace(path)
