"""Jev availability and the inbox-suggestion switch."""

from __future__ import annotations

import os
import sqlite3
from typing import Any

from fastapi import APIRouter, Depends

from ...inbox_classifiers import set_enabled as set_inbox_suggestions, status as inbox_suggestions_status
from ...typesafe_decisions import DEFAULT_MODEL as TYPESAFE_DEFAULT_MODEL, QUESTION_SET_VERSION, TypeSafeError
from ..context import AppContext
from ..dependencies import get_ctx, require_auth, writable_connection
from ..models.opportunities import InboxSuggestionsRequest


router = APIRouter()


@router.get("/api/v1/typesafe")
def typesafe_status(_authenticated_user: str = Depends(require_auth), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Report optional Jev availability without exposing credentials."""
    try:
        client = ctx.services.typesafe_client_factory()
    except TypeSafeError as exc:
        return {
            "configured": False,
            "model": os.environ.get("TYPESAFE_MODEL", TYPESAFE_DEFAULT_MODEL),
            "question_set_version": QUESTION_SET_VERSION,
            "external_processing": True,
            "automatic_actions": False,
            "setup_hint": str(exc),
        }
    return {
        "configured": client.configured,
        "model": client.model,
        "question_set_version": QUESTION_SET_VERSION,
        "external_processing": True,
        "automatic_actions": False,
        "setup_hint": "" if client.configured else "Set TYPESAFE_API_KEY to enable Jev reviews",
    }


@router.get("/api/v1/typesafe/inbox-suggestions")
def get_inbox_suggestions(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Whether Jev can classify replies and connector emails here, and whether this student turned it on."""
    return inbox_suggestions_status(conn, ctx.services.inbox_client_factory, user_id=user_id)


@router.put("/api/v1/typesafe/inbox-suggestions")
def put_inbox_suggestions(
    payload: InboxSuggestionsRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    # Turning it on without a key is allowed: nothing is sent until a key is set,
    # and the rules answer in the meantime.
    set_inbox_suggestions(conn, payload.enabled, user_id=user_id)
    return inbox_suggestions_status(conn, ctx.services.inbox_client_factory, user_id=user_id)
