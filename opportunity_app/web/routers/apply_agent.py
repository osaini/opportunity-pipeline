"""Apply for me: the check, the answers it asks for, its settings and the sensitive-answers store."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import Depends, HTTPException, Response, status

from ..overrides import shared_router
from ...automation import ledger as automation_core
from ...applications.actions import OpportunityNotFoundError
from ...apply import (
    classify as apply_classify,
    policy as apply_policy,
    preflight as apply_preflight,
    runs as apply_runs,
    sensitive as apply_sensitive,
    watch as apply_watch,
)
from ...apply.schema_client import SchemaClient
from ..context import AppContext
from ..dependencies import get_ctx, require_auth, require_browser_session, writable_connection
from ..models.apply_agent import (
    ApplyAnswerRequest,
    ApplyClaimResolveRequest,
    ApplyLabelRequest,
    ApplySensitiveAnswerRequest,
    ApplySensitiveCategoriesRequest,
    ApplySensitiveEntryRequest,
)


router = shared_router()


def apply_schema_client(ctx: AppContext) -> SchemaClient:
    """The client the read-only Apply for me check uses, or 503 in an app that has none (a test, the fuzz sandbox)."""
    if ctx.services.apply_schema_client_factory is None or ctx.services.apply_agent_factory is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=apply_runs.NOT_HERE)
    return ctx.services.apply_schema_client_factory()


def require_apply_agent(conn: sqlite3.Connection, user_id: str) -> None:
    """6.0 step 1: Apply for me must be on. The answer is what it still needs, or how to turn it on."""
    if automation_core.mode(conn, user_id, "apply_agent") != "on":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=apply_runs.setup_requirement(conn, user_id) or "Apply for me is off. Turn it on under Automation",
        )


def sensitive_answers_payload(conn: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    allowed = apply_sensitive.allowed_categories(conn, user_id)
    return {
        "groups": [
            {"key": key, "label": label, "categories": list(categories), "on": all(category in allowed for category in categories)}
            for key, label, categories in apply_sensitive.CATEGORY_GROUPS
        ],
        "categories": [
            {"category": category, "label": apply_sensitive.LABELS[category], "statement": category in apply_classify.STATEMENT_CATEGORIES,
             "decline_only": category in apply_sensitive.EEO_CATEGORIES,
             "tickable": category in apply_classify.TICKABLE}
            for category in apply_sensitive.STORABLE
        ],
        "entries": apply_sensitive.list_entries(conn, user_id),
        "consent_text": apply_sensitive.CONSENT_TEXT,
        "decline_examples": list(apply_sensitive.DECLINE_EXAMPLES),
    }


@router.get("/api/v1/apply-agent/opportunities/{opportunity_id}/check")
def apply_agent_check(
    opportunity_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """What Apply for me would fill for this Greenhouse role, and what it still needs from the student.

    Read-only: it opens no browser and writes nothing (no application, no event, no run). It asks Greenhouse's
    public listing once an hour per role and keeps the answer in memory.
    """
    client = apply_schema_client(ctx)
    require_apply_agent(conn, user_id)
    try:
        return apply_preflight.check(
            conn, user_id, opportunity_id, client=client, cache=ctx.runtime.apply_schema_cache, resume_root=ctx.config.resume_storage,
        )
    except OpportunityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found") from exc


@router.post("/api/v1/apply-agent/opportunities/{opportunity_id}/answers")
def apply_agent_answer(
    opportunity_id: str,
    payload: ApplyAnswerRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Answer one question the check listed as missing. Saved for this company, without field ids, so it carries over."""
    client = apply_schema_client(ctx)
    require_apply_agent(conn, user_id)
    try:
        return apply_preflight.answer_missing(
            conn, user_id, opportunity_id, key=payload.key, answer=payload.answer, reusable=payload.reusable, posting_confirmed=payload.posting_confirmed,
            client=client, cache=ctx.runtime.apply_schema_cache, resume_root=ctx.config.resume_storage,
        )
    except OpportunityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found") from exc
    except apply_preflight.AnswerRefused as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.get("/api/v1/apply-agent/settings")
def apply_agent_settings(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    """The Apply for me settings in force: the switch and what it needs, the limits, and the option labels confirmed."""
    values = apply_runs.limits(conn, user_id)
    return {
        "mode": automation_core.mode(conn, user_id, "apply_agent"),
        "requirement": apply_runs.setup_requirement(conn, user_id),
        "limits": [
            {"key": key, "value": values[key], "default": default, "overridden": values[key] != default}
            for key, default in apply_runs.DEFAULT_LIMITS.items()
        ],
        "ats_labels": apply_runs.list_ats_labels(conn, user_id),
        "label_fields": list(apply_policy.ALLOWED_ATS_LABEL_FIELDS),
        "evidence_days": apply_runs.evidence_days(),
        "ats_statistics": [apply_watch.ats_statistics(conn, user_id)],
    }


@router.put("/api/v1/apply-agent/ats-labels/{field}")
def put_apply_ats_label(
    field: str,
    payload: ApplyLabelRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, str]:
    """Save the exact text of an option the student picked in a typeahead list (school, location, degree)."""
    try:
        return apply_runs.set_ats_label(conn, user_id, field, payload.label)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.delete("/api/v1/apply-agent/ats-labels/{field}", status_code=status.HTTP_204_NO_CONTENT)
def delete_apply_ats_label(
    field: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> Response:
    if not apply_runs.delete_ats_label(conn, user_id, field):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No confirmed option for that list")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# The sensitive-answers store. Every route here needs the student's own browser session (require_browser_session): an
# access token, which scripts hold, is refused. Reading is refused too, since an entry is the student's own answer.
@router.get("/api/v1/apply-agent/sensitive-answers")
def list_stored_sensitive_answers(
    user_id: str = Depends(require_browser_session),
    conn: sqlite3.Connection = Depends(writable_connection),
) -> dict[str, Any]:
    """The kinds of sensitive answer the student switched on, and the answers they stored, each with its consent."""
    return sensitive_answers_payload(conn, user_id)


@router.put("/api/v1/apply-agent/sensitive-categories")
def put_apply_sensitive_categories(
    payload: ApplySensitiveCategoriesRequest,
    user_id: str = Depends(require_browser_session),
    conn: sqlite3.Connection = Depends(writable_connection),
) -> dict[str, Any]:
    """Switch kinds of sensitive answer on or off. Export control, citizenship, clearance and salary cannot be switched on."""
    try:
        apply_sensitive.set_allowed_categories(conn, user_id, payload.categories)
    except apply_sensitive.StoreRefused as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    return sensitive_answers_payload(conn, user_id)


@router.post("/api/v1/apply-agent/sensitive-answers")
def add_apply_sensitive_answer(
    payload: ApplySensitiveEntryRequest,
    user_id: str = Depends(require_browser_session),
    conn: sqlite3.Connection = Depends(writable_connection),
) -> dict[str, Any]:
    """Store an answer the app may type into a form, with the consent ticked. The same question for the same company is replaced."""
    try:
        return apply_sensitive.add_entry(
            conn, user_id, category=payload.category, question=payload.question, answer=payload.answer, answer_kind=payload.answer_kind,
            company=payload.company, links=payload.links, consent=payload.consent,
        )
    except apply_sensitive.StoreRefused as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.delete("/api/v1/apply-agent/sensitive-answers/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_apply_sensitive_answer(
    entry_id: str,
    user_id: str = Depends(require_browser_session),
    conn: sqlite3.Connection = Depends(writable_connection),
) -> Response:
    """Remove a stored answer at once. A plan that used it no longer matches."""
    if not apply_sensitive.delete_entry(conn, user_id, entry_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No stored answer with that id")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/api/v1/apply-agent/opportunities/{opportunity_id}/sensitive-answers")
def apply_agent_sensitive_answer(
    opportunity_id: str,
    payload: ApplySensitiveAnswerRequest,
    user_id: str = Depends(require_browser_session),
    conn: sqlite3.Connection = Depends(writable_connection),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Needs you: store the student's answer to one sensitive question the check listed, with the consent ticked."""
    client = apply_schema_client(ctx)
    require_apply_agent(conn, user_id)
    try:
        return apply_preflight.answer_sensitive(
            conn, user_id, opportunity_id, key=payload.key, answer=payload.answer, consent=payload.consent, any_company=payload.any_company,
            posting_confirmed=payload.posting_confirmed, client=client, cache=ctx.runtime.apply_schema_cache, resume_root=ctx.config.resume_storage,
        )
    except OpportunityNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found") from exc
    except apply_preflight.AnswerRefused as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


# The card's two answers and its Mark as applied? button. They need the student's own browser session, like the sensitive
# answers: an attempt that may have reached Greenhouse is settled by the student looking, never by a script. They do not need
# Apply for me to be on: a student who turned it off still has attempts to settle.
@router.post("/api/v1/apply-agent/claims/{token}/resolve")
def resolve_apply_claim(
    token: str,
    payload: ApplyClaimResolveRequest,
    user_id: str = Depends(require_browser_session),
    conn: sqlite3.Connection = Depends(writable_connection),
) -> dict[str, Any]:
    """It went through / It didn't go through, for an attempt that may have reached Greenhouse."""
    try:
        return {"claim": apply_watch.resolve_by_student(conn, token, user_id=user_id, went_through=payload.went_through)}
    except apply_runs.ClaimNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This attempt was not found") from exc
    except apply_runs.ClaimHeldError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This application is still being submitted") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This attempt does not need an answer") from exc


@router.post("/api/v1/apply-agent/claims/{token}/mark-applied")
def mark_apply_claim_applied(
    token: str,
    user_id: str = Depends(require_browser_session),
    conn: sqlite3.Connection = Depends(writable_connection),
) -> dict[str, Any]:
    """Mark as applied?: move the application forward after a Finish in browser submission, which never moves it by itself."""
    try:
        return {"claim": apply_watch.mark_applied(conn, token, user_id=user_id)}
    except apply_runs.ClaimNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This attempt was not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This attempt has nothing to mark") from exc
