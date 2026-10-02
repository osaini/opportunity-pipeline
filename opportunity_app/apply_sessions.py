"""Apply sessions: the form sessions the student's browser reports for an application, kept per user.

A session records which page a form is on and what each field is (its label, type and where its value came from), never the
value itself. Final-submit controls are refused: nothing here ever submits an application (AGENTS.md, "Never auto-apply").
"""

from __future__ import annotations

import json
import sqlite3
import urllib.parse
from typing import Any

from .applications.actions import log_application_event
from .core.timestamps import utc_now

# The only keys of a field the session keeps; anything else the browser sent (a proposed value, say) is dropped.
ALLOWED_FIELD_KEYS = {
    "key", "label", "type", "provenance", "confidence",
    "requires_review", "filled", "reason",
}


class ApplySessionRefused(ValueError):
    """The session cannot be recorded. The message says why and is safe to show."""


class ApplicationNotOwned(LookupError):
    """The application the session names is not the caller's, or does not exist."""


class ApplySessionForeign(Exception):
    """The session id already belongs to another user. Ids are client-generated, so they never grant access."""


def list_sessions(conn: sqlite3.Connection, user_id: str) -> list[dict[str, Any]]:
    """The caller's sessions, most recently updated first, each with its decoded fields."""
    rows = conn.execute(
        "SELECT * FROM application_form_sessions WHERE user_id=? ORDER BY updated_at DESC",
        (user_id,),
    ).fetchall()
    items = [{**dict(row), "fields": json.loads(row["fields_json"])} for row in rows]
    for item in items:
        item.pop("fields_json", None)
    return items


def sync_session(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    session_id: str,
    application_id: str | None,
    page_url: str,
    ats_type: str,
    fields: list[dict[str, Any]],
    status: str,
) -> dict[str, Any]:
    """Create or update a session. Checks run in this order: the page URL, the application's owner, the fields, the session id's owner.

    Raises ApplySessionRefused (bad URL, or a final-submit control among the fields), ApplicationNotOwned or ApplySessionForeign.
    """
    parsed = urllib.parse.urlsplit(page_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ApplySessionRefused("Apply sessions require an HTTP or HTTPS page URL")
    if application_id:
        owned = conn.execute(
            "SELECT 1 FROM applications WHERE id=? AND user_id=?",
            (application_id, user_id),
        ).fetchone()
        if not owned:
            raise ApplicationNotOwned(application_id)
    safe_fields: list[dict[str, Any]] = []
    for field in fields:
        field_type = str(field.get("type", "")).lower()
        label = str(field.get("label", ""))[:500]
        if field_type in {"submit", "button", "image"} or "submit application" in label.lower():
            raise ApplySessionRefused("Final-submit controls are prohibited from Apply Mode sessions")
        safe_fields.append({key: value for key, value in field.items() if key in ALLOWED_FIELD_KEYS})
    timestamp = utc_now()
    existing_session = conn.execute(
        "SELECT user_id FROM application_form_sessions WHERE id=?", (session_id,)
    ).fetchone()
    if existing_session and existing_session["user_id"] != user_id:
        # Session ids are client-generated; never let one user overwrite
        # or read another user's session by replaying its id.
        raise ApplySessionForeign(session_id)
    with conn:
        conn.execute(
            """
                INSERT INTO application_form_sessions(
                    id, user_id, application_id, page_url, ats_type,
                    fields_json, status, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    application_id=excluded.application_id,
                    page_url=excluded.page_url,
                    ats_type=excluded.ats_type,
                    fields_json=excluded.fields_json,
                    status=excluded.status,
                    updated_at=excluded.updated_at
                """,
            (session_id, user_id, application_id, page_url, ats_type, json.dumps(safe_fields), status, timestamp, timestamp),
        )
        if application_id:
            log_application_event(
                conn, application_id, "apply_session_synced",
                {"session_id": session_id, "status": status}, timestamp,
            )
    return {
        "id": session_id,
        "application_id": application_id,
        "page_url": page_url,
        "ats_type": ats_type,
        "fields": safe_fields,
        "status": status,
        "updated_at": timestamp,
        "final_submit_available": False,
    }
