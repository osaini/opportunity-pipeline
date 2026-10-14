"""Outreach: the student's answer to a company's reply, and the note for a revisit date (outreach/reply_drafts.py)."""

from __future__ import annotations

import sqlite3
from typing import Any

import httpx
from fastapi import Depends, HTTPException, status

from ..overrides import shared_router
from ...integrations.gmail_client import GmailAuthError
from ...outreach import reply_drafts as outreach_reply_drafts
from ...outreach.gmail import SendNeedsCheckError, SendUnconfirmedError
from ...outreach.send_claims import SendConflictError
from ...outreach.targets import OutreachNotFoundError
from ..context import AppContext
from ..dependencies import get_ctx, require_auth, writable_connection
from ..errors import outreach_not_found, send_needs_check
from ..models.outreach import (
    OutreachReplyApproveRequest,
    OutreachReplyDraftRequest,
    OutreachReplyEditRequest,
    OutreachThankYouSendRequest,
)


router = shared_router()


def _gmail_failure(exc: Exception) -> HTTPException:
    detail = str(exc) if isinstance(exc, RuntimeError) else "Could not reach Gmail. Nothing was sent"
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)


@router.post("/api/v1/outreach/{target_id}/reply-draft")
def write_outreach_reply(
    target_id: str,
    payload: OutreachReplyDraftRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Write the student's answer to the company's latest reply, or the note for their revisit date. Nothing is sent."""
    try:
        return outreach_reply_drafts.generate(
            conn, target_id, user_id=user_id, provider_factory=ctx.services.outreach_provider_factory,
            provider=ctx.config.outreach_draft_provider, comments=payload.comments, purpose=payload.purpose,
        )
    except OutreachNotFoundError as exc:
        raise outreach_not_found() from exc
    except outreach_reply_drafts.ReplyDraftChanged as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"The reply model failed: {exc}") from exc


@router.patch("/api/v1/outreach/{target_id}/reply-draft")
def edit_outreach_reply(
    target_id: str,
    payload: OutreachReplyEditRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    """Save the student's own words. An approved reply needs approving again."""
    try:
        return outreach_reply_drafts.edit(
            conn, target_id, user_id=user_id, subject=payload.subject, body=payload.body, fingerprint=payload.fingerprint,
        )
    except OutreachNotFoundError as exc:
        raise outreach_not_found() from exc
    except outreach_reply_drafts.ReplyDraftChanged as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.delete("/api/v1/outreach/{target_id}/reply-draft")
def dismiss_outreach_reply(
    target_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    """Put the reply draft away. It is not written again for the same message."""
    try:
        return outreach_reply_drafts.dismiss(conn, target_id, user_id=user_id)
    except OutreachNotFoundError as exc:
        raise outreach_not_found() from exc


@router.post("/api/v1/outreach/{target_id}/reply-draft/approve")
def approve_outreach_reply(
    target_id: str,
    payload: OutreachReplyApproveRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    """Approve the reply as shown. Refused while a bracket is left to fill in, or when they wrote again since."""
    try:
        return outreach_reply_drafts.approve(conn, target_id, user_id=user_id, fingerprint=payload.fingerprint)
    except OutreachNotFoundError as exc:
        raise outreach_not_found() from exc
    except outreach_reply_drafts.ReplyDraftChanged as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.post("/api/v1/outreach/{target_id}/reply-draft/send")
def send_outreach_reply(
    target_id: str,
    payload: OutreachThankYouSendRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """The student's confirmed Send of an approved reply, in their thread."""
    try:
        return outreach_reply_drafts.send(
            conn, target_id, user_id=user_id, fingerprint=payload.fingerprint, sent_folder_check=payload.sent_folder_check,
            client_factory=ctx.services.gmail_client_factory,
        )
    except OutreachNotFoundError as exc:
        raise outreach_not_found() from exc
    except SendNeedsCheckError as exc:
        raise send_needs_check(exc) from exc
    except (outreach_reply_drafts.ReplyDraftChanged, SendConflictError, GmailAuthError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except (RuntimeError, SendUnconfirmedError, httpx.HTTPError) as exc:
        raise _gmail_failure(exc) from exc


@router.post("/api/v1/outreach/{target_id}/reply-draft/gmail-draft")
def outreach_reply_to_gmail(
    target_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Put the reply in the student's Gmail Drafts, in their thread, to finish and send there. Nothing is sent."""
    try:
        return outreach_reply_drafts.to_gmail(conn, target_id, user_id=user_id, client_factory=ctx.services.gmail_client_factory)
    except OutreachNotFoundError as exc:
        raise outreach_not_found() from exc
    except (outreach_reply_drafts.ReplyDraftChanged, SendConflictError, GmailAuthError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except (RuntimeError, httpx.HTTPError) as exc:
        raise _gmail_failure(exc) from exc


@router.post("/api/v1/outreach/{target_id}/reply-draft/revisit")
def use_outreach_reply_revisit(
    target_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    """Make the date their reply named the company's revisit date."""
    try:
        return outreach_reply_drafts.use_revisit_date(conn, target_id, user_id=user_id)
    except OutreachNotFoundError as exc:
        raise outreach_not_found() from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
