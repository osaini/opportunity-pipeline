"""Outreach discovery, the bulk contact search, call prep and company research."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from ...schema import LOCAL_USER_ID
from ...outreach import OutreachNotFoundError
from ...outreach_call_prep import NotReplied, ReplyRequired, queue_call_prep
from ...outreach_research import queue_research as queue_company_research
from ...outreach_discovery import DiscoveryBusy
from ...outreach_recontact import RecontactBusy, RecontactManager
from ..context import AppContext
from ..dependencies import get_ctx, require_auth, writable_connection
from ..models.outreach import OutreachDiscoveryRequest, RecontactApplyRequest
from ..payloads import outreach_discovery_payload, outreach_recontact_payload


discovery_router = APIRouter()
recontact_router = APIRouter()
router = APIRouter()


def require_recontact_owner(ctx: AppContext, user_id: str) -> RecontactManager:
    if user_id != LOCAL_USER_ID:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the owner can search for contacts in bulk")
    if ctx.services.outreach_recontact_manager is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="The bulk contact search is only available for the main database")
    return ctx.services.outreach_recontact_manager


@discovery_router.get("/api/v1/outreach/discovery")
def outreach_discovery_status(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    return outreach_discovery_payload(ctx, conn, user_id)


@discovery_router.post("/api/v1/outreach/discovery", status_code=status.HTTP_202_ACCEPTED)
def start_outreach_discovery(
    payload: OutreachDiscoveryRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    if user_id != LOCAL_USER_ID:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only the owner can run the deep search")
    if ctx.services.outreach_discovery_manager is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="The deep search is only available for the main database")
    try:
        ctx.services.outreach_discovery_manager.start(user_id=user_id, scopes=list(payload.scopes) if payload.scopes else None)
    except DiscoveryBusy as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return outreach_discovery_payload(ctx, conn, user_id)


@recontact_router.get("/api/v1/outreach/recontact")
def outreach_recontact_status(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    return outreach_recontact_payload(ctx, conn, user_id)


@recontact_router.post("/api/v1/outreach/recontact", status_code=status.HTTP_202_ACCEPTED)
def start_outreach_recontact(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Search again for people at shared-inbox targets. Reports only; changes no contact."""
    manager = require_recontact_owner(ctx, user_id)
    try:
        manager.start_report(user_id=user_id)
    except RecontactBusy as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return outreach_recontact_payload(ctx, conn, user_id)


@recontact_router.post("/api/v1/outreach/recontact/apply", status_code=status.HTTP_202_ACCEPTED)
def apply_outreach_recontact(
    payload: RecontactApplyRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Apply the upgrades the student ticked in the last report, without searching again."""
    manager = require_recontact_owner(ctx, user_id)
    choices = {choice.target_id: choice.to.strip() for choice in payload.choices}
    try:
        manager.start_apply(user_id=user_id, choices=choices, redraft=payload.redraft)
    except RecontactBusy as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return outreach_recontact_payload(ctx, conn, user_id)


@router.post("/api/v1/outreach/{target_id}/call-prep", status_code=status.HTTP_202_ACCEPTED)
def call_prep_for_outreach(
    target_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Queue new call-prep notes, written in the background. Replaced notes stay in the history.

    409 when no reply is logged: the notes are written from it.
    """
    try:
        target = queue_call_prep(conn, target_id, user_id=user_id, replace=True, reason="You asked for new call prep")
    except OutreachNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Outreach target not found") from exc
    except ReplyRequired as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (NotReplied, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    ctx.services.call_prep_worker.wake()
    return target


@router.post("/api/v1/outreach/{target_id}/research", status_code=status.HTTP_202_ACCEPTED)
def research_outreach_company(
    target_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Queue research on the company from the web, run in the background.

    Every fact kept has its quote found on the page it cites (outreach_research.py).
    409 when this app has no research agent wired up (it runs only against
    the student's own database), or when none is installed on this computer.
    """
    if not ctx.services.call_prep_worker.can_research:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Company research is not available in this app. It runs in the app on your own database.",
        )
    if problem := ctx.services.research_problem():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=problem)
    try:
        target = queue_company_research(conn, target_id, user_id=user_id, reason="You asked for research")
    except OutreachNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Outreach target not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    ctx.services.call_prep_worker.wake()
    return target
