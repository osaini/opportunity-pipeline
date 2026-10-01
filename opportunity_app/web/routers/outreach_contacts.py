"""Outreach contacts, found-contact candidates, confirmed research and logged replies."""

from __future__ import annotations

import sqlite3
from contextlib import ExitStack
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from ...inbox_classifiers import client_for as inbox_client_for
from ...outreach import (
    OutreachNotFoundError,
    confirm_research as confirm_outreach_research,
    get_target as get_outreach_target,
    log_reply as log_outreach_reply,
)
from ...outreach_contacts import (
    add_manual_contact as add_manual_outreach_contact,
    apply_candidate as apply_outreach_candidate,
    find_contacts as find_outreach_contacts,
    list_candidates as list_outreach_candidates,
)
from ...outreach_call_prep import auto_queue_call_prep
from ..context import AppContext
from ..dependencies import get_ctx, require_auth, writable_connection
from ..errors import outreach_not_found
from ..models.outreach import OutreachManualContactRequest, OutreachReplyRequest


router = APIRouter()


@router.post("/api/v1/outreach/{target_id}/confirm-research")
def confirm_research_for_outreach(
    target_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return confirm_outreach_research(conn, target_id, user_id=user_id)
    except OutreachNotFoundError as exc:
        raise outreach_not_found() from exc


@router.get("/api/v1/outreach/{target_id}/contacts")
def outreach_contacts(
    target_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return {"candidates": list_outreach_candidates(conn, target_id, user_id=user_id)}
    except OutreachNotFoundError as exc:
        raise outreach_not_found() from exc


@router.post("/api/v1/outreach/{target_id}/contacts", status_code=status.HTTP_201_CREATED)
def add_outreach_contact(
    target_id: str,
    payload: OutreachManualContactRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return add_manual_outreach_contact(conn, target_id, user_id=user_id, **payload.model_dump())
    except OutreachNotFoundError as exc:
        raise outreach_not_found() from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.post("/api/v1/outreach/{target_id}/find-contacts")
def find_contacts_for_outreach(
    target_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    try:
        with ExitStack() as stack:
            fetcher = stack.enter_context(ctx.services.contact_client_factory())
            verifier = ctx.services.smtp_verifier_factory()
            renderer = ctx.services.renderer_factory()
            return find_outreach_contacts(
                conn, target_id, user_id=user_id, fetcher=fetcher, delay=ctx.config.outreach_contact_delay,
                verifier=stack.enter_context(verifier) if verifier is not None else None,
                renderer=stack.enter_context(renderer) if renderer is not None else None,
            )
    except OutreachNotFoundError as exc:
        raise outreach_not_found() from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.post("/api/v1/outreach/{target_id}/contacts/{candidate_id}/apply")
def apply_outreach_contact(
    target_id: str,
    candidate_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return apply_outreach_candidate(conn, target_id, candidate_id, user_id=user_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contact candidate not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.post("/api/v1/outreach/{target_id}/reply")
def log_outreach_reply_route(
    target_id: str,
    payload: OutreachReplyRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    try:
        logged = log_outreach_reply(
            conn, target_id, payload.text, user_id=user_id,
            decisions=inbox_client_for(conn, ctx.services.inbox_client_factory, user_id=user_id), as_reply=payload.as_reply,
        )
        # A reply logged on a company already at a reply status starts its call prep.
        # A bounce notice is not a reply, so it is not logged and starts nothing.
        if logged["logged"] and auto_queue_call_prep(conn, target_id, user_id=user_id, reason="Reply logged"):
            ctx.services.call_prep_worker.wake()
            logged["target"] = get_outreach_target(conn, target_id, user_id=user_id, include_events=True)
        return logged
    except OutreachNotFoundError as exc:
        raise outreach_not_found() from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
