"""The administrator surface: operations overview, jobs, retention, verification, flags, sources and moderation."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import Depends, HTTPException, status

from ..overrides import shared_router
from ...accounts.employer import (
    EmployerNotFoundError,
    admin_overview,
    audit,
    create_moderation_item,
    resolve_moderation_item,
    school_aggregate,
    set_feature_flag,
    set_source_control,
    verify_organization,
)
from ...accounts.operations import OperationsError, enqueue_job, retry_dead_job, run_retention, service_overview
from ...notifications import connector_health
from ..context import AppContext
from ..dependencies import admin_connection, get_ctx
from ..models.admin import (
    FeatureFlagRequest,
    JobCreateRequest,
    ModerationCreateRequest,
    ModerationResolveRequest,
    OrganizationVerificationRequest,
    SourceControlRequest,
)


router = shared_router()


@router.get("/api/v1/admin/overview")
def operations_overview(
    context: tuple[sqlite3.Connection, str] = Depends(admin_connection),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    conn, _actor = context
    overview = admin_overview(conn)
    return service_overview(conn, overview, ctx.runtime.metrics, ctx.runtime.traces)


@router.post("/api/v1/admin/jobs", status_code=status.HTTP_201_CREATED)
def admin_enqueue_job(
    payload: JobCreateRequest,
    context: tuple[sqlite3.Connection, str] = Depends(admin_connection),
) -> dict[str, Any]:
    conn, actor = context
    record = enqueue_job(conn, payload.job_type, payload.payload, payload.idempotency_key, max_attempts=payload.max_attempts)
    with conn:
        audit(conn, actor, "job_enqueued", "job", record["id"], {"job_type": payload.job_type})
    return record


@router.post("/api/v1/admin/jobs/{job_id}/retry")
def admin_retry_job(
    job_id: str,
    context: tuple[sqlite3.Connection, str] = Depends(admin_connection),
) -> dict[str, Any]:
    conn, _actor = context
    try:
        return retry_dead_job(conn, job_id)
    except OperationsError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


@router.post("/api/v1/admin/retention/run")
def admin_run_retention(
    context: tuple[sqlite3.Connection, str] = Depends(admin_connection),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, int]:
    conn, _actor = context
    return run_retention(conn, apply_root=ctx.config.apply_storage)


@router.post("/api/v1/admin/organizations/{organization_id}/verify")
def admin_verify_organization(
    organization_id: str,
    payload: OrganizationVerificationRequest,
    context: tuple[sqlite3.Connection, str] = Depends(admin_connection),
) -> dict[str, Any]:
    conn, actor = context
    try:
        return verify_organization(conn, organization_id, payload.approved, actor)
    except EmployerNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found") from exc


@router.put("/api/v1/admin/feature-flags/{key}")
def admin_put_feature_flag(
    key: str,
    payload: FeatureFlagRequest,
    context: tuple[sqlite3.Connection, str] = Depends(admin_connection),
) -> dict[str, Any]:
    conn, actor = context
    return set_feature_flag(conn, key, payload.enabled, payload.description, actor)


@router.get("/api/v1/admin/connector-health")
def admin_connector_health(
    context: tuple[sqlite3.Connection, str] = Depends(admin_connection),
) -> dict[str, Any]:
    conn, _actor = context
    return connector_health(conn)


@router.put("/api/v1/admin/sources/{source_key:path}")
def admin_source_control(
    source_key: str,
    payload: SourceControlRequest,
    context: tuple[sqlite3.Connection, str] = Depends(admin_connection),
) -> dict[str, Any]:
    conn, actor = context
    return set_source_control(conn, source_key, payload.enabled, payload.moderation_status, payload.note, actor)


@router.post("/api/v1/admin/moderation", status_code=status.HTTP_201_CREATED)
def admin_create_moderation(
    payload: ModerationCreateRequest,
    context: tuple[sqlite3.Connection, str] = Depends(admin_connection),
) -> dict[str, Any]:
    conn, actor = context
    return create_moderation_item(conn, payload.target_type, payload.target_id, payload.reason, actor)


@router.post("/api/v1/admin/moderation/{item_id}/resolve")
def admin_resolve_moderation(
    item_id: str,
    payload: ModerationResolveRequest,
    context: tuple[sqlite3.Connection, str] = Depends(admin_connection),
) -> dict[str, Any]:
    conn, actor = context
    try:
        return resolve_moderation_item(conn, item_id, payload.status, payload.resolution, actor)
    except EmployerNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Moderation item not found") from exc


@router.get("/api/v1/admin/school-report")
def admin_school_report(
    context: tuple[sqlite3.Connection, str] = Depends(admin_connection),
) -> dict[str, Any]:
    conn, _actor = context
    return school_aggregate(conn)
