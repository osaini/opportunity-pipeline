"""The employer surface: organizations, requisitions, candidates, messages and interviews."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import Depends, HTTPException, status

from ..overrides import shared_router
from ...accounts.dossier import DossierNotFoundError
from ...accounts.employer import (
    EmployerNotFoundError,
    add_candidate_from_share,
    approve_candidate_message,
    candidate_record,
    confirm_interview,
    create_organization,
    create_requisition,
    decide_candidate,
    draft_candidate_message,
    import_requisitions,
    list_candidates,
    list_requisitions,
    propose_interview,
    requisition_record,
)
from ...automation.notifications import build_provider as build_notification_provider
from ..dependencies import employer_connection
from ..models.employer import (
    CandidateDecisionRequest,
    CandidateShareRequest,
    EmployerInterviewRequest,
    EmployerMessageRequest,
    OrganizationRequest,
    RequisitionImportRequest,
    RequisitionRequest,
)


router = shared_router()


@router.post("/api/v1/employer/organizations", status_code=status.HTTP_201_CREATED)
def employer_create_organization(
    payload: OrganizationRequest,
    context: tuple[sqlite3.Connection, str] = Depends(employer_connection),
) -> dict[str, Any]:
    conn, actor = context
    try:
        return create_organization(conn, payload.name, payload.organization_type, actor)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.post("/api/v1/employer/organizations/{organization_id}/requisitions", status_code=status.HTTP_201_CREATED)
def employer_create_requisition(
    organization_id: str,
    payload: RequisitionRequest,
    context: tuple[sqlite3.Connection, str] = Depends(employer_connection),
) -> dict[str, Any]:
    conn, actor = context
    try:
        return create_requisition(conn, organization_id, payload.title, payload.description, payload.rubric, actor)
    except EmployerNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.get("/api/v1/employer/requisitions")
def employer_requisitions(
    context: tuple[sqlite3.Connection, str] = Depends(employer_connection),
) -> dict[str, Any]:
    conn, actor = context
    items = list_requisitions(conn, actor)
    return {"items": items, "total": len(items)}


@router.post("/api/v1/employer/requisitions/import", status_code=status.HTTP_201_CREATED)
def employer_import_requisitions(
    payload: RequisitionImportRequest,
    context: tuple[sqlite3.Connection, str] = Depends(employer_connection),
) -> dict[str, Any]:
    conn, actor = context
    try:
        items = import_requisitions(conn, payload.organization_id, payload.requisitions, actor)
        return {"items": items, "total": len(items), "format": "ats-json-v1"}
    except EmployerNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.get("/api/v1/employer/requisitions/{requisition_id}")
def employer_requisition(
    requisition_id: str,
    context: tuple[sqlite3.Connection, str] = Depends(employer_connection),
) -> dict[str, Any]:
    conn, actor = context
    try:
        return requisition_record(conn, requisition_id, actor)
    except EmployerNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Requisition not found") from exc


@router.get("/api/v1/employer/requisitions/{requisition_id}/export")
def employer_export_requisition(
    requisition_id: str,
    context: tuple[sqlite3.Connection, str] = Depends(employer_connection),
) -> dict[str, Any]:
    conn, actor = context
    try:
        return {"format": "ats-json-v1", "requisition": requisition_record(conn, requisition_id, actor)}
    except EmployerNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Requisition not found") from exc


@router.get("/api/v1/employer/requisitions/{requisition_id}/candidates")
def employer_candidates(
    requisition_id: str,
    context: tuple[sqlite3.Connection, str] = Depends(employer_connection),
) -> dict[str, Any]:
    conn, actor = context
    try:
        items = list_candidates(conn, requisition_id, actor)
        return {"items": items, "total": len(items)}
    except EmployerNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Requisition not found") from exc


@router.post("/api/v1/employer/requisitions/{requisition_id}/candidates", status_code=status.HTTP_201_CREATED)
def employer_add_candidate(
    requisition_id: str,
    payload: CandidateShareRequest,
    context: tuple[sqlite3.Connection, str] = Depends(employer_connection),
) -> dict[str, Any]:
    conn, actor = context
    try:
        return add_candidate_from_share(conn, requisition_id, payload.share_token, actor)
    except (EmployerNotFoundError, DossierNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Requisition or active consent share not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.get("/api/v1/employer/candidates/{candidate_id}")
def employer_candidate(
    candidate_id: str,
    context: tuple[sqlite3.Connection, str] = Depends(employer_connection),
) -> dict[str, Any]:
    conn, actor = context
    try:
        return candidate_record(conn, candidate_id, actor)
    except EmployerNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate or active consent not found") from exc


@router.post("/api/v1/employer/candidates/{candidate_id}/decision")
def employer_decide_candidate(
    candidate_id: str,
    payload: CandidateDecisionRequest,
    context: tuple[sqlite3.Connection, str] = Depends(employer_connection),
) -> dict[str, Any]:
    conn, actor = context
    try:
        return decide_candidate(conn, candidate_id, payload.status, payload.reason, actor)
    except EmployerNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate or active consent not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.get("/api/v1/employer/candidates/{candidate_id}/agent-summary")
def employer_agent_summary(
    candidate_id: str,
    context: tuple[sqlite3.Connection, str] = Depends(employer_connection),
) -> dict[str, Any]:
    conn, actor = context
    try:
        candidate = candidate_record(conn, candidate_id, actor)
    except EmployerNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate or active consent not found") from exc
    return {
        "candidate_id": candidate_id,
        "evidence_count": len(candidate["evidence"]),
        "rubric_score": candidate["score"],
        "summary": "This evidence-only summary does not make or recommend a hiring decision.",
        "human_decision_required": True,
    }


@router.post("/api/v1/employer/candidates/{candidate_id}/messages", status_code=status.HTTP_201_CREATED)
def employer_draft_message(
    candidate_id: str,
    payload: EmployerMessageRequest,
    context: tuple[sqlite3.Connection, str] = Depends(employer_connection),
) -> dict[str, Any]:
    conn, actor = context
    try:
        return draft_candidate_message(conn, candidate_id, payload.body, actor)
    except EmployerNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found") from exc


@router.post("/api/v1/employer/messages/{message_id}/approve")
def employer_approve_message(
    message_id: str,
    context: tuple[sqlite3.Connection, str] = Depends(employer_connection),
) -> dict[str, Any]:
    conn, actor = context
    provider = build_notification_provider()
    deliver = None
    if provider.live:
        def deliver(message: dict[str, Any]) -> dict[str, Any]:
            return provider.deliver("email", "candidate@relay", "A recruiter message awaits your review", str(message.get("body", "")))
    try:
        return approve_candidate_message(conn, message_id, actor, deliver=deliver)
    except EmployerNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/api/v1/employer/candidates/{candidate_id}/interviews", status_code=status.HTTP_201_CREATED)
def employer_propose_interview(
    candidate_id: str,
    payload: EmployerInterviewRequest,
    context: tuple[sqlite3.Connection, str] = Depends(employer_connection),
) -> dict[str, Any]:
    conn, actor = context
    try:
        return propose_interview(conn, candidate_id, payload.starts_at, payload.timezone, payload.location, actor)
    except EmployerNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Candidate not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.post("/api/v1/employer/interviews/{interview_id}/confirm")
def employer_confirm_interview(
    interview_id: str,
    context: tuple[sqlite3.Connection, str] = Depends(employer_connection),
) -> dict[str, Any]:
    conn, actor = context
    try:
        return confirm_interview(conn, interview_id, actor)
    except EmployerNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Interview not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
