"""Apply sessions recorded for an application."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from ...apply_sessions import (
    ApplicationNotOwned,
    ApplySessionForeign,
    ApplySessionRefused,
    list_sessions as list_apply_sessions,
    sync_session as store_apply_session,
)
from ..dependencies import require_auth, writable_connection
from ..models.extension import ApplySessionRequest


router = APIRouter()


@router.get("/api/v1/apply-sessions")
def apply_sessions(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    items = list_apply_sessions(conn, user_id)
    return {"items": items, "total": len(items)}


@router.post("/api/v1/apply-sessions")
def sync_apply_session(
    payload: ApplySessionRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return store_apply_session(
            conn, user_id, session_id=payload.session_id, application_id=payload.application_id,
            page_url=payload.page_url, ats_type=payload.ats_type, fields=payload.fields, status=payload.status,
        )
    except ApplySessionRefused as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except ApplicationNotOwned as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found") from exc
    except ApplySessionForeign as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Apply session belongs to another user") from exc
