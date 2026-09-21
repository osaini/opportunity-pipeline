"""Confirmed student profile storage and onboarding completeness."""

from __future__ import annotations

import json
import sqlite3
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


def rescore_profile(
    conn: sqlite3.Connection, profile: dict[str, Any], *, user_id: str
) -> int:
    """Persist the legacy deterministic score for one authenticated student."""

    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT id, title, description, role_type, location, posted_at
           FROM opportunities"""
    ).fetchall()
    timestamp = utc_now()
    with conn:
        for row in rows:
            score, reasons = score_job(row, profile)
            conn.execute(
                """INSERT INTO fit_scores(
                       opportunity_id, user_id, ruleset_version, score,
                       explanation_json, created_at
                   ) VALUES(?, ?, 'legacy-v1', ?, ?, ?)
                   ON CONFLICT(opportunity_id, user_id, ruleset_version) DO UPDATE SET
                       score=excluded.score,
                       explanation_json=excluded.explanation_json,
                       created_at=excluded.created_at""",
                (row["id"], user_id, score, json.dumps(reasons), timestamp),
            )
    return len(rows)


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


def update_profile(
    conn: sqlite3.Connection,
    updates: dict[str, Any],
    confirmed_fields: list[str],
    *,
    source: str = "user",
    user_id: str,
    profile_file: Path | None = None,
) -> dict[str, Any]:
    """Merge confirmed updates into a student's profile and rescore.

    For the local owner, ``profile_file`` is config/profile.json: pipeline.py
    scores from that file, and each refresh copies its scores over these, so an
    edit kept only in the database would be undone by the next refresh.
    """
    unknown = sorted(set(updates) - ALLOWED_PROFILE_FIELDS)
    if unknown:
        raise ValueError(f"Unsupported profile fields: {', '.join(unknown)}")
    current = get_profile(conn, user_id=user_id)["profile"]
    merged = {**current, **updates}
    timestamp = utc_now()
    confirmed = set(confirmed_fields)
    if not confirmed.issubset(ALLOWED_PROFILE_FIELDS):
        raise ValueError("confirmed_fields contains an unsupported field")
    with conn:
        conn.execute(
            "UPDATE profiles SET profile_json=?, confirmed_at=?, updated_at=? WHERE user_id=?",
            (
                json.dumps(merged),
                timestamp if confirmed else None,
                timestamp,
                user_id,
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
                # Editing a field without confirming it, or clearing it to no
                # answer, revokes any previous confirmation rather than leaving
                # stale evidence behind.
                conn.execute(
                    "DELETE FROM profile_facts WHERE user_id=? AND field_path=?",
                    (user_id, field),
                )
    rescore_profile(conn, merged, user_id=user_id)
    if profile_file is not None and user_id == LOCAL_USER_ID:
        write_profile_file(profile_file, merged)
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
