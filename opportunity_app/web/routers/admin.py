"""The administrator surface: operations overview, jobs, retention, verification, flags, sources and moderation."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from ...employer import (
    EmployerNotFoundError,
    admin_overview,
    create_moderation_item,
    resolve_moderation_item,
    school_aggregate,
    set_feature_flag,
    set_source_control,
    verify_organization,
)
from ...operations import OperationsError, enqueue_job, queue_status, retry_dead_job, run_retention
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


router = APIRouter()


@router.get("/api/v1/admin/overview")
def operations_overview(
    context: tuple[sqlite3.Connection, str] = Depends(admin_connection),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    conn, _actor = context
    overview = admin_overview(conn)
    requests = int(ctx.runtime.metrics["requests"])
    read_latencies = sorted(ctx.runtime.metrics["read_latency_ms"])
    write_latencies = sorted(ctx.runtime.metrics["write_latency_ms"])

    def p95(values: list[float]) -> float | None:
        if not values:
            return None
        index = max(0, (len(values) * 95 + 99) // 100 - 1)
        return round(float(values[index]), 3)

    read_p95 = p95(read_latencies)
    write_p95 = p95(write_latencies)
    error_rate = round(int(ctx.runtime.metrics["errors"]) / requests, 4) if requests else 0.0
    overview["service"] = {
        "requests": requests,
        "errors": int(ctx.runtime.metrics["errors"]),
        "error_rate": error_rate,
        "rate_limited": int(ctx.runtime.metrics["rate_limited"]),
        "average_latency_ms": round(float(ctx.runtime.metrics["latency_ms_total"]) / requests, 3) if requests else 0,
        "read_p95_ms": read_p95,
        "write_p95_ms": write_p95,
    }
    queue = queue_status(conn)
    overview["queue"] = queue
    alerts = []
    if error_rate > 0.02:
        alerts.append({"key": "api_error_rate", "severity": "critical", "value": error_rate, "threshold": 0.02})
    if read_p95 is not None and read_p95 > 750:
        alerts.append({"key": "read_p95_ms", "severity": "warning", "value": read_p95, "threshold": 750})
    if write_p95 is not None and write_p95 > 1_500:
        alerts.append({"key": "write_p95_ms", "severity": "warning", "value": write_p95, "threshold": 1_500})
    if queue["states"]["dead"]:
        alerts.append({"key": "dead_letter_jobs", "severity": "critical", "value": queue["states"]["dead"], "threshold": 0})
    if queue["backpressure"]:
        alerts.append({"key": "queue_backpressure", "severity": "critical", "value": True, "threshold": False})
    overview["slo"] = {
        "availability_target": 0.995,
        "read_p95_target_ms": 750,
        "write_p95_target_ms": 1_500,
        "queue_age_target_seconds": 600,
        "status": "alerting" if alerts else "within_observed_thresholds",
        "scope": "current process window; external durable telemetry required for monthly SLOs",
    }
    overview["alerts"] = alerts
    overview["recent_traces"] = list(ctx.runtime.traces)[-50:]
    overview["product_analytics"] = {
        "application_events": int(conn.execute("SELECT COUNT(*) FROM application_events").fetchone()[0]),
        "apply_sessions": int(conn.execute("SELECT COUNT(*) FROM application_form_sessions").fetchone()[0]),
        "agent_turns": int(conn.execute("SELECT COUNT(*) FROM agent_turns").fetchone()[0]),
        "active_dossier_shares": int(conn.execute("SELECT COUNT(*) FROM dossier_consent_grants WHERE status='active'").fetchone()[0]),
        "contains_user_identifiers": False,
    }
    return overview


@router.post("/api/v1/admin/jobs", status_code=status.HTTP_201_CREATED)
def admin_enqueue_job(
    payload: JobCreateRequest,
    context: tuple[sqlite3.Connection, str] = Depends(admin_connection),
) -> dict[str, Any]:
    conn, actor = context
    record = enqueue_job(conn, payload.job_type, payload.payload, payload.idempotency_key, max_attempts=payload.max_attempts)
    from ...employer import audit
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
