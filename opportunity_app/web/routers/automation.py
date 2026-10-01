"""The automation ledger: switches, actions, notices, trusted employer domains and application mail."""

from __future__ import annotations

import sqlite3
from typing import Any, Callable

from fastapi import Depends, HTTPException, Query, status

from ..overrides import shared_router
from ... import application_inbox
from ... import automation as automation_core
from ... import automation_health
from ... import auto_triage, mail_trust
from ...actions import ApplicationNotFoundError
from ...inbox_classifiers import client_for as inbox_client_for
from ..context import AppContext
from ..dependencies import get_ctx, require_auth, writable_connection
from ..models.automation import (
    AutomationApproveRequest,
    AutomationNoticesReadRequest,
    AutomationReviewRequest,
    AutomationSettingsRequest,
)


router = shared_router()


# Everything the app does on its own (automation.py): the switches, the
# master pause, the ledger of what it did, its notices, and its health.
def automation_view(conn: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    return {
        "settings": automation_core.settings_payload(conn, user_id),
        "health": automation_health.health_summary(conn, user_id),
        "application_mail": application_inbox.status(conn, user_id),
    }


def automation_decision(decide: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Run a student's decision on one action and map what it refuses to an HTTP status."""
    try:
        return decide()
    except ApplicationNotFoundError as exc:
        # The application a student chose for a proposal is not theirs, or is gone.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found") from exc
    except automation_core.Superseded as exc:
        # Recorded as superseded before this was raised; the message names what changed.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except automation_core.CorrectionRefused as exc:
        # The application the student chose cannot take this change. Nothing was decided, so they
        # can choose another (422, not 409: the action is still open).
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except LookupError as exc:
        if isinstance(exc, KeyError):
            raise  # a bug, not a missing action
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such automation action") from exc
    except ValueError as exc:
        # The wrong status, or decided somewhere else at the same time.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


def decide_employer_domain(conn: sqlite3.Connection, user_id: str, domain_id: str, decision: str) -> dict[str, Any]:
    try:
        return mail_trust.decide(conn, user_id, domain_id, decision)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such domain") from exc


@router.get("/api/v1/automation")
def get_automation(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    return {
        **automation_view(conn, user_id),
        "notices": automation_core.list_notices(conn, user_id, unread_only=True, limit=20),
    }


@router.put("/api/v1/automation/settings")
def put_automation_settings(
    payload: AutomationSettingsRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    modes = payload.modes or {}
    try:
        # One transaction checks and writes the pause and every switch, so a
        # refused switch leaves the pause and the other switches as they were.
        applied = automation_core.apply_settings(conn, user_id, modes=modes, paused=payload.paused)
    except automation_core.AutomationGateError as exc:
        # A subclass of ValueError, so it is caught first.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    if payload.paused is not None or modes:
        ctx.services.automation_worker.wake()
    if modes.get(application_inbox.FEATURE) == "off":
        # Off forgets where reading stood, so turning it on again starts afresh.
        application_inbox.note_off(conn, user_id)
    response = automation_view(conn, user_id)
    if applied["in_flight"] is not None:
        response["in_flight"] = applied["in_flight"]
    return response


@router.get("/api/v1/automation/actions")
def get_automation_actions(
    # One status or several, comma-separated ("applied,undone"); each must be one of automation.STATUSES.
    status_filter: str | None = Query(default=None, alias="status", min_length=1, max_length=200),
    feature: str | None = Query(default=None, min_length=1, max_length=64),
    limit: int = Query(default=50, ge=1, le=200),
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        statuses = automation_core.parse_statuses(status_filter)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    items = automation_core.list_actions(conn, user_id, status=statuses, feature=feature, limit=limit)
    total = automation_core.count_actions(conn, user_id, status=statuses, feature=feature)
    return {"items": items, "total": total}


@router.get("/api/v1/automation/actions/{action_id}")
def get_automation_action(
    action_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    # One action, for the application timeline's Undo on an automatic change.
    try:
        row = automation_core.action_row(conn, action_id, user_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No such automation action") from exc
    return automation_core._decode(row)


@router.post("/api/v1/automation/actions/{action_id}/undo")
def undo_automation_action(
    action_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    return automation_decision(lambda: automation_core.undo(conn, action_id, user_id))


@router.post("/api/v1/automation/actions/{action_id}/approve")
def approve_automation_action(
    action_id: str,
    payload: AutomationApproveRequest | None = None,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    # subject_id: the student picked another application than the one proposed (automation.approve records it).
    subject_id = payload.subject_id if payload is not None else None

    def approve() -> dict[str, Any]:
        try:
            return automation_core.approve(conn, action_id, user_id, subject_id=subject_id)
        except automation_core.Superseded:
            # Nothing is left to approve on its email card either, so it is settled before the 409.
            application_inbox.after_superseded(conn, user_id, action_id)
            raise

    result = automation_decision(approve)
    application_inbox.after_decision(conn, user_id, result)
    return result


@router.post("/api/v1/automation/actions/{action_id}/reject")
def reject_automation_action(
    action_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    result = automation_decision(lambda: automation_core.reject(conn, action_id, user_id))
    application_inbox.after_decision(conn, user_id, result)
    return result


@router.post("/api/v1/automation/actions/{action_id}/review")
def review_automation_action(
    action_id: str,
    payload: AutomationReviewRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    # The verdict is checked by the request model (422); a ValueError here is the wrong status (409).
    return automation_decision(lambda: automation_core.review(conn, action_id, user_id, payload.verdict))


@router.get("/api/v1/automation/auto-passed")
def get_auto_passed(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    """Roles auto_pass passed on in the last 7 days that still stand; Restore on each is the ledger's undo."""
    items = auto_triage.auto_passed_this_week(conn, user_id)
    return {"items": items, "total": len(items), "days": auto_triage.REVIEW_DAYS}


# Company mail domains the student trusts for application mail (mail_trust.py).
@router.get("/api/v1/automation/employer-domains")
def get_employer_domains(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    mail_trust.refresh_suggestions(conn, user_id)
    items = mail_trust.list_domains(conn, user_id)
    return {"items": items, "total": len(items), "domain_check": mail_trust.psl_available()}


@router.post("/api/v1/automation/employer-domains/{domain_id}/trust")
def trust_employer_domain(
    domain_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    return decide_employer_domain(conn, user_id, domain_id, "trusted")


@router.post("/api/v1/automation/employer-domains/{domain_id}/untrust")
def untrust_employer_domain(
    domain_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    # Back to a suggestion: its mail is still read, and only ever proposes.
    return decide_employer_domain(conn, user_id, domain_id, "suggested")


@router.post("/api/v1/automation/employer-domains/{domain_id}/dismiss")
def dismiss_employer_domain(
    domain_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    return decide_employer_domain(conn, user_id, domain_id, "dismissed")


# Update applications from job emails (application_inbox.py).
@router.get("/api/v1/automation/application-mail")
def get_application_mail(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    return application_inbox.status(conn, user_id)


@router.post("/api/v1/automation/application-mail/check")
def check_application_mail(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Read job emails now instead of at the next pass. Does nothing while the switch is off; never raises for Gmail trouble."""
    result = application_inbox.run_pass(
        conn, user_id=user_id, client_factory=ctx.services.gmail_client_factory,
        decisions=inbox_client_for(conn, ctx.services.inbox_client_factory, user_id=user_id), force=True,
    )
    return {**result, "status": application_inbox.status(conn, user_id)}


@router.post("/api/v1/automation/application-mail/backfill/approve-all")
def approve_application_mail_backfill(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    """Approve the updates found in the 60 days before the switch was on that waited only for that; the rest stay one by one."""
    counts = application_inbox.approve_backfill(conn, user_id)
    return {**counts, **automation_view(conn, user_id)}


@router.post("/api/v1/automation/notices/read")
def read_automation_notices(
    payload: AutomationNoticesReadRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    if payload.all:
        return {"marked": automation_core.mark_notices_read(conn, user_id, all_unread=True)}
    return {"marked": automation_core.mark_notices_read(conn, user_id, list(payload.ids or []))}
