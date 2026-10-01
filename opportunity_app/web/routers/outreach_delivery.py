"""Outreach mail: Gmail drafts and sends, contact forms, bounces, delivery and inbox checks, scheduling, thank-yous."""

from __future__ import annotations

import sqlite3
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, status

from ...inbox_classifiers import client_for as inbox_client_for
from ...outreach import (
    DraftChangedError,
    OutreachNotFoundError,
    dismiss_reply_suggestion,
    get_target as get_outreach_target,
)
from ...outreach_delivery import bounce_from_text, check_deliveries
from ...outreach_inbox import PossibleReplyNotFound, PossibleReplySettled, capture_replies, decide_possible_reply
from ...outreach_forms import set_contact_form, submit_contact_form
from ...outreach_schedule import cancel_send, schedule_send
from ... import outreach_thank_you
from ...gmail_client import GmailAuthError
from ...outreach_gmail import (
    SendNeedsCheckError,
    ThankYouChanged,
    create_gmail_draft,
    send_gmail_message,
)
from ...send_claims import SendConflictError
from ..context import AppContext
from ..dependencies import get_ctx, require_auth, writable_connection
from ..errors import outreach_not_found, send_needs_check
from ..models.outreach import (
    OutreachBounceRequest,
    OutreachContactFormRequest,
    OutreachDraftRequest,
    OutreachFormSubmitRequest,
    OutreachScheduleRequest,
    OutreachSendRequest,
    OutreachThankYouSendRequest,
    PossibleReplyDecisionRequest,
)


router = APIRouter()


@router.post("/api/v1/outreach/{target_id}/gmail-draft")
def gmail_draft_for_outreach(
    target_id: str,
    payload: OutreachDraftRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Create the approved draft in the student's Gmail Drafts. Nothing is sent."""
    try:
        return create_gmail_draft(
            conn, target_id, user_id=user_id, kind=payload.kind,
            client_factory=ctx.services.gmail_client_factory,
        )
    except OutreachNotFoundError as exc:
        raise outreach_not_found() from exc
    except (GmailAuthError, SendConflictError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except (RuntimeError, httpx.HTTPError) as exc:
        detail = str(exc) if isinstance(exc, RuntimeError) else "Could not reach Gmail"
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail) from exc


@router.post("/api/v1/outreach/{target_id}/gmail-send")
def gmail_send_for_outreach(
    target_id: str,
    payload: OutreachSendRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Send the approved draft the student confirmed from their Gmail, and mark it sent."""
    try:
        sent = send_gmail_message(
            conn, target_id, user_id=user_id, kind=payload.kind, fingerprint=payload.fingerprint,
            sent_folder_check=payload.sent_folder_check,
            client_factory=ctx.services.gmail_client_factory,
        )
        # Sent now instead of at the scheduled time.
        cancel_send(conn, target_id, user_id=user_id, kind=payload.kind, reason="You sent it now instead")
        return sent
    except OutreachNotFoundError as exc:
        raise outreach_not_found() from exc
    except SendNeedsCheckError as exc:
        # 428: the same request succeeds once it carries the named check.
        raise send_needs_check(exc) from exc
    except (GmailAuthError, DraftChangedError, SendConflictError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except (RuntimeError, httpx.HTTPError) as exc:
        detail = str(exc) if isinstance(exc, RuntimeError) else "Could not reach Gmail. Nothing was sent"
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail) from exc


@router.post("/api/v1/outreach/{target_id}/form-submit")
def form_submit_for_outreach(
    target_id: str,
    payload: OutreachFormSubmitRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Send the approved first message the student confirmed through the company's contact form."""
    if ctx.services.form_submitter_factory is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="This copy of the app does not open a browser to send contact forms")
    try:
        return submit_contact_form(
            conn, target_id, user_id=user_id, submitter_factory=ctx.services.form_submitter_factory,
            fingerprint=payload.fingerprint, retry_unconfirmed=payload.retry_unconfirmed, in_browser=payload.in_browser,
        )
    except OutreachNotFoundError as exc:
        raise outreach_not_found() from exc
    except SendNeedsCheckError as exc:
        raise send_needs_check(exc) from exc
    except (DraftChangedError, SendConflictError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.put("/api/v1/outreach/{target_id}/contact-form")
def set_outreach_contact_form(
    target_id: str,
    payload: OutreachContactFormRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    """The student names the page holding the company's contact form."""
    try:
        return set_contact_form(conn, target_id, payload.page_url, user_id=user_id)
    except OutreachNotFoundError as exc:
        raise outreach_not_found() from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.post("/api/v1/outreach/{target_id}/bounce")
def mark_outreach_bounced(
    target_id: str,
    payload: OutreachBounceRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    """Record that the email bounced: back to Drafted, and nothing more to the failed address."""
    try:
        return bounce_from_text(conn, target_id, payload.text, user_id=user_id)
    except OutreachNotFoundError as exc:
        raise outreach_not_found() from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.post("/api/v1/outreach/delivery-check")
def check_outreach_deliveries(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Look in Gmail for bounces of recent sends. Reads only failure notices, and never raises for Gmail trouble."""
    return check_deliveries(
        conn, user_id=user_id, client_factory=ctx.services.gmail_client_factory,
    )


@router.post("/api/v1/outreach/inbox-check")
def check_outreach_inbox(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Look in Gmail for drafts sent or scheduled there, bounces, and replies. Never raises for Gmail trouble."""
    from ...outreach_gmail_sends import capture_gmail_sends

    gmail_sends = capture_gmail_sends(conn, user_id=user_id, client_factory=ctx.services.gmail_client_factory)
    delivery = check_deliveries(conn, user_id=user_id, client_factory=ctx.services.gmail_client_factory)
    replies = capture_replies(
        conn, user_id=user_id, client_factory=ctx.services.gmail_client_factory,
        decisions=inbox_client_for(conn, ctx.services.inbox_client_factory, user_id=user_id), on_reply=ctx.services.prep_after_reply,
    )
    if delivery["bounced"]:
        ctx.services.automation_worker.wake()  # a bounced contact may be recovered right away
    state = next((value for value in (gmail_sends["state"], delivery["state"], replies["state"]) if value != "ok"), "ok")
    return {
        "state": state, "bounced": delivery["bounced"], "replies": replies["replies"], "automatic": replies["automatic"],
        "possible": replies["possible"], "sent_in_gmail": gmail_sends["sent"], "scheduled_in_gmail": gmail_sends["scheduled"],
    }


@router.post("/api/v1/outreach/{target_id}/possible-replies/{gmail_id}")
def decide_outreach_possible_reply(
    target_id: str,
    gmail_id: str,
    payload: PossibleReplyDecisionRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """The student says whether an email from the company that may be a reply is one."""
    try:
        return decide_possible_reply(
            conn, target_id, gmail_id, payload.decision, user_id=user_id,
            decisions=inbox_client_for(conn, ctx.services.inbox_client_factory, user_id=user_id), on_reply=ctx.services.prep_after_reply,
        )
    except (OutreachNotFoundError, PossibleReplyNotFound) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such possible reply") from exc
    except PossibleReplySettled as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.delete("/api/v1/outreach/{target_id}/reply-suggestion")
def dismiss_outreach_reply_suggestion(
    target_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return dismiss_reply_suggestion(conn, target_id, user_id=user_id)
    except OutreachNotFoundError as exc:
        raise outreach_not_found() from exc


@router.post("/api/v1/outreach/{target_id}/schedule")
def schedule_outreach_send(
    target_id: str,
    payload: OutreachScheduleRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Queue the approved draft the student confirmed for the recipient's next weekday morning."""
    try:
        scheduled = schedule_send(conn, target_id, user_id=user_id, kind=payload.kind, fingerprint=payload.fingerprint)
    except OutreachNotFoundError as exc:
        raise outreach_not_found() from exc
    except (DraftChangedError, SendConflictError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    ctx.services.automation_worker.wake()
    return scheduled


@router.delete("/api/v1/outreach/{target_id}/schedule")
def cancel_outreach_send(
    target_id: str,
    kind: Literal["initial", "follow_up"] = "initial",
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        cancelled = cancel_send(conn, target_id, user_id=user_id, kind=kind)
        # False: nothing was left to stop, for example it had already gone out.
        return {**get_outreach_target(conn, target_id, user_id=user_id), "cancelled": cancelled}
    except OutreachNotFoundError as exc:
        raise outreach_not_found() from exc


@router.delete("/api/v1/outreach/{target_id}/thank-you")
def cancel_outreach_thank_you(
    target_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    """Cancel the thank-you waiting to go, or dismiss one that was held. False when nothing was left to stop."""
    try:
        cancelled = outreach_thank_you.cancel(conn, target_id, user_id=user_id)
        return {**get_outreach_target(conn, target_id, user_id=user_id), "cancelled": cancelled}
    except OutreachNotFoundError as exc:
        raise outreach_not_found() from exc


@router.post("/api/v1/outreach/{target_id}/thank-you/edit")
def edit_outreach_thank_you(
    target_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Stop the automatic thank-you and put it in the student's Gmail Drafts, in the thread, to edit and send."""
    try:
        draft = outreach_thank_you.edit_in_gmail(conn, target_id, user_id=user_id, client_factory=ctx.services.gmail_client_factory)
    except OutreachNotFoundError as exc:
        raise outreach_not_found() from exc
    except (ThankYouChanged, SendConflictError, GmailAuthError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (RuntimeError, httpx.HTTPError) as exc:
        detail = str(exc) if isinstance(exc, RuntimeError) else "Could not reach Gmail. Nothing was sent"
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail) from exc
    return {"draft": draft, "target": get_outreach_target(conn, target_id, user_id=user_id)}


@router.post("/api/v1/outreach/{target_id}/thank-you/send")
def send_outreach_thank_you(
    target_id: str,
    payload: OutreachThankYouSendRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Send it anyway: the student's own confirmed send of a thank-you that was held or stopped."""
    try:
        sent = outreach_thank_you.send_anyway(
            conn, target_id, user_id=user_id, fingerprint=payload.fingerprint,
            sent_folder_check=payload.sent_folder_check, client_factory=ctx.services.gmail_client_factory,
        )
    except OutreachNotFoundError as exc:
        raise outreach_not_found() from exc
    except SendNeedsCheckError as exc:
        raise send_needs_check(exc) from exc
    except (ThankYouChanged, SendConflictError, GmailAuthError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except (RuntimeError, httpx.HTTPError) as exc:
        detail = str(exc) if isinstance(exc, RuntimeError) else "Could not reach Gmail. Nothing was sent"
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail) from exc
    return {**sent, "target": get_outreach_target(conn, target_id, user_id=user_id)}
