"""The student agent: providers, threads, messages and proposals."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import Depends, HTTPException, status

from ..overrides import shared_router
from ...student_agent import (
    AgentNotFoundError,
    activity_feed,
    cancel_thread,
    create_thread,
    decide_proposal,
    list_threads,
    post_message,
    thread_record,
)
from ...agent_providers import default_provider, provider_catalog
from ..context import AppContext
from ..dependencies import get_ctx, require_auth, writable_connection
from ..models.agent import AgentDecisionRequest, AgentMessageRequest, AgentThreadRequest


router = shared_router()


@router.get("/api/v1/agent/providers")
def agent_providers(
    _authenticated_user: str = Depends(require_auth),
) -> dict[str, Any]:
    items = provider_catalog()
    return {"items": items, "total": len(items)}


@router.get("/api/v1/agent/threads")
def agent_threads(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    items = list_threads(conn, user_id=user_id)
    return {"items": items, "total": len(items)}


@router.post("/api/v1/agent/threads", status_code=status.HTTP_201_CREATED)
def start_agent_thread(
    payload: AgentThreadRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        provider = payload.provider or default_provider()
        return create_thread(conn, payload.title, provider, user_id=user_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.get("/api/v1/agent/activity")
def agent_activity(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    items = activity_feed(conn, user_id=user_id)
    return {"items": items, "total": len(items)}


@router.get("/api/v1/agent/threads/{thread_id}")
def get_agent_thread(
    thread_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return thread_record(conn, thread_id, user_id=user_id)
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent thread not found") from exc


@router.post("/api/v1/agent/threads/{thread_id}/messages", status_code=status.HTTP_201_CREATED)
def send_agent_message(
    thread_id: str,
    payload: AgentMessageRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    try:
        return post_message(
            conn,
            thread_id,
            payload.content,
            provider_factory=ctx.services.agent_provider_factory, user_id=user_id)
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent thread not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.post("/api/v1/agent/threads/{thread_id}/cancel")
def cancel_agent_thread(
    thread_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return cancel_thread(conn, thread_id, user_id=user_id)
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Active agent thread not found") from exc


@router.post("/api/v1/agent/proposals/{proposal_id}/decision")
def decide_agent_action(
    proposal_id: str,
    payload: AgentDecisionRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return decide_proposal(conn, proposal_id, payload.decision, user_id=user_id)
    except AgentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Proposed action not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
