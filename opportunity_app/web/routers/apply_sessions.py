"""Apply sessions recorded for an application."""

from __future__ import annotations

import json
import sqlite3
import urllib.parse
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from ...actions import log_application_event
from ...timestamps import utc_now
from ..dependencies import require_auth, writable_connection
from ..models.extension import ApplySessionRequest


router = APIRouter()


@router.get("/api/v1/apply-sessions")
def apply_sessions(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT * FROM application_form_sessions WHERE user_id=? ORDER BY updated_at DESC",
        (user_id,),
    ).fetchall()
    items = [{**dict(row), "fields": json.loads(row["fields_json"])} for row in rows]
    for item in items:
        item.pop("fields_json", None)
    return {"items": items, "total": len(items)}


@router.post("/api/v1/apply-sessions")
def sync_apply_session(
    payload: ApplySessionRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    parsed = urllib.parse.urlsplit(payload.page_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Apply sessions require an HTTP or HTTPS page URL",
        )
    if payload.application_id:
        owned = conn.execute(
            "SELECT 1 FROM applications WHERE id=? AND user_id=?",
            (payload.application_id, user_id),
        ).fetchone()
        if not owned:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found")
    safe_fields: list[dict[str, Any]] = []
    allowed = {
        "key", "label", "type", "provenance", "confidence",
        "requires_review", "filled", "reason",
    }
    for field in payload.fields:
        field_type = str(field.get("type", "")).lower()
        label = str(field.get("label", ""))[:500]
        if field_type in {"submit", "button", "image"} or "submit application" in label.lower():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Final-submit controls are prohibited from Apply Mode sessions",
            )
        safe_fields.append({key: value for key, value in field.items() if key in allowed})
    timestamp = utc_now()
    existing_session = conn.execute(
        "SELECT user_id FROM application_form_sessions WHERE id=?", (payload.session_id,)
    ).fetchone()
    if existing_session and existing_session["user_id"] != user_id:
        # Session ids are client-generated; never let one user overwrite
        # or read another user's session by replaying its id.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Apply session belongs to another user")
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
            (payload.session_id, user_id, payload.application_id, payload.page_url, payload.ats_type, json.dumps(safe_fields), payload.status, timestamp, timestamp),
        )
        if payload.application_id:
            log_application_event(
                conn, payload.application_id, "apply_session_synced",
                {"session_id": payload.session_id, "status": payload.status}, timestamp,
            )
    return {
        "id": payload.session_id,
        "application_id": payload.application_id,
        "page_url": payload.page_url,
        "ats_type": payload.ats_type,
        "fields": safe_fields,
        "status": payload.status,
        "updated_at": timestamp,
        "final_submit_available": False,
    }
