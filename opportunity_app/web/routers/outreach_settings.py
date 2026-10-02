"""Outreach automation switches, settings and the Gmail label."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import Depends, HTTPException, status

from ..overrides import shared_router
from ...outreach.automation import settings as automation_settings, update_settings as update_automation_settings
from ...outreach import label_name as outreach_label_name, labels as outreach_labels
from ...outreach.gmail import gmail_drafts_status
from ..context import AppContext
from ..dependencies import get_ctx, require_auth, require_owner, writable_connection
from ..models.outreach import GmailLabelRequest, OutreachAutomationRequest, OutreachSettingsRequest


automation_router = shared_router()
router = shared_router()


def gmail_label_view(conn: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    value = outreach_label_name.label_name(conn, user_id)
    gmail = gmail_drafts_status(conn, user_id=user_id)
    return {
        "value": value,
        "default": outreach_label_name.DEFAULT_LABEL,
        "search": outreach_labels.search_form(value),
        "mailbox": {"connected": gmail["connected"], "connected_as": gmail["connected_as"], "expected": gmail["account"]},
        "permission": gmail["label_check"],
    }


@automation_router.get("/api/v1/outreach/automation")
def get_outreach_automation(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    return automation_settings(conn, user_id=user_id)


@automation_router.put("/api/v1/outreach/automation")
def put_outreach_automation(
    payload: OutreachAutomationRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    changes = {key: value for key, value in payload.model_dump().items() if value is not None}
    updated = update_automation_settings(conn, changes, user_id=user_id)
    ctx.services.automation_worker.wake()
    return updated


@router.get("/api/v1/outreach/settings")
def get_outreach_settings(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_owner),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    if ctx.services.outreach_settings is None:
        return {"available": False}
    return {"available": True, **ctx.services.outreach_settings.view(conn, user_id=user_id)}


@router.put("/api/v1/outreach/settings")
def put_outreach_settings(
    payload: OutreachSettingsRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_owner),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    if ctx.services.outreach_settings is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Settings can only be changed for the main database")
    try:
        view = ctx.services.outreach_settings.update(conn, payload.model_dump(exclude_unset=True), user_id=user_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    return {"available": True, **view}


@router.get("/api/v1/outreach/gmail-label")
def get_gmail_label(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    """The Gmail label put on every outreach thread (the emails the student sent and the replies), and whether Gmail lets the app add it."""
    return gmail_label_view(conn, user_id)


@router.put("/api/v1/outreach/gmail-label")
def put_gmail_label(
    payload: GmailLabelRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        outreach_labels.set_label_name(conn, user_id, payload.value)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    return gmail_label_view(conn, user_id)
