"""The Chrome extension's pairing, devices, apply context and sessions."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Any

from fastapi import Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse

from ..overrides import shared_router
from ...actions import ApplicationNotFoundError
from ...student.artifacts import backfill_approved_artifacts
from ...extension_apply import (
    ExtensionApplyError,
    ExtensionAuthError,
    answer_is_sensitive,
    application_candidates,
    apply_context,
    artifact_path as extension_artifact_path,
    confirm_submitted as confirm_extension_submitted,
    create_pairing,
    list_devices as list_extension_devices,
    redeem_pairing,
    revoke_device as revoke_extension_device,
    sync_session as sync_extension_session,
    sync_step as sync_extension_step,
)
from ...student.preparation import save_answer
from ...core.database import connect_product
from ... import apply_classify
from ..context import AppContext
from ..dependencies import extension_connection, get_ctx, require_auth, writable_connection
from ..models.extension import (
    ExtensionAnswerRequest,
    ExtensionPairingRedeemRequest,
    ExtensionSessionRequest,
    ExtensionStepRequest,
)


router = shared_router()


@router.post("/api/v1/extension/pairings", status_code=status.HTTP_201_CREATED)
def create_extension_pairing(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    return create_pairing(conn, user_id=user_id)


@router.post("/api/v1/extension/pairings/redeem")
def redeem_extension_pairing(
    payload: ExtensionPairingRedeemRequest,
    request: Request,
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    origin = request.headers.get("Origin", "")
    if not ctx.config.database_present():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Product database unavailable")
    try:
        with closing(connect_product(ctx.config.database_target)) as conn:
            return redeem_pairing(conn, payload.code, origin, payload.device_name)
    except ExtensionAuthError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc


@router.get("/api/v1/extension/devices")
def extension_devices(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    items = list_extension_devices(conn, user_id=user_id)
    return {"items": items, "total": len(items)}


@router.delete("/api/v1/extension/devices/{device_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_extension_device(
    device_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> Response:
    if not revoke_extension_device(conn, device_id, user_id=user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Extension device not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/api/v1/extension/application-candidates")
def extension_application_candidates(
    page_url: str = Query(min_length=8, max_length=2_000),
    context: tuple[sqlite3.Connection, dict[str, str]] = Depends(extension_connection),
) -> dict[str, Any]:
    conn, device = context
    try:
        return application_candidates(conn, page_url, user_id=device["user_id"])
    except ExtensionApplyError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.get("/api/v1/extension/apply-context")
def extension_apply_context(
    application_id: str = Query(min_length=1, max_length=500),
    context: tuple[sqlite3.Connection, dict[str, str]] = Depends(extension_connection),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    conn, device = context
    warnings = backfill_approved_artifacts(conn, ctx.config.resume_storage, user_id=device["user_id"])
    try:
        result = apply_context(conn, application_id, user_id=device["user_id"])
    except ApplicationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found") from exc
    result["artifact_warnings"] = sorted(set(warnings))
    return result


@router.get("/api/v1/extension/artifacts/{artifact_id}/file")
def download_extension_artifact(
    artifact_id: str,
    application_id: str = Query(min_length=1, max_length=500),
    context: tuple[sqlite3.Connection, dict[str, str]] = Depends(extension_connection),
    ctx: AppContext = Depends(get_ctx),
) -> FileResponse:
    conn, device = context
    try:
        path, filename, media_type, sha256 = extension_artifact_path(
            conn,
            artifact_id,
            application_id,
            ctx.config.resume_storage,
            user_id=device["user_id"],
        )
    except ApplicationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found") from exc
    except ExtensionApplyError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    return FileResponse(
        path,
        media_type=media_type,
        filename=filename,
        headers={"X-Artifact-SHA256": sha256, "Cache-Control": "no-store"},
    )


@router.put("/api/v1/extension/sessions/{session_id}")
def put_extension_session(
    session_id: str,
    payload: ExtensionSessionRequest,
    context: tuple[sqlite3.Connection, dict[str, str]] = Depends(extension_connection),
) -> dict[str, Any]:
    conn, device = context
    try:
        return sync_extension_session(
            conn, session_id, payload.model_dump(), user_id=device["user_id"]
        )
    except ApplicationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found") from exc
    except ExtensionAuthError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ExtensionApplyError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.put("/api/v1/extension/sessions/{session_id}/steps/{step_key}")
def put_extension_step(
    session_id: str,
    step_key: str,
    payload: ExtensionStepRequest,
    context: tuple[sqlite3.Connection, dict[str, str]] = Depends(extension_connection),
) -> dict[str, Any]:
    conn, device = context
    try:
        return sync_extension_step(
            conn,
            session_id,
            step_key,
            payload.model_dump(),
            user_id=device["user_id"],
        )
    except ExtensionApplyError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.post("/api/v1/extension/sessions/{session_id}/confirm-submitted")
def confirm_extension_submission(
    session_id: str,
    context: tuple[sqlite3.Connection, dict[str, str]] = Depends(extension_connection),
) -> dict[str, Any]:
    conn, device = context
    try:
        return confirm_extension_submitted(conn, session_id, user_id=device["user_id"])
    except ApplicationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Apply session not found") from exc


@router.post("/api/v1/extension/answers", status_code=status.HTTP_201_CREATED)
def save_extension_answer(
    payload: ExtensionAnswerRequest,
    context: tuple[sqlite3.Connection, dict[str, str]] = Depends(extension_connection),
) -> dict[str, Any]:
    conn, device = context
    # The precise rule, and the broad net's never-storable topics (criminal history, personal details, pay, security): a wording
    # the precise rule misses is still never offered for saving here (spec 7.3 "As built").
    if answer_is_sensitive(payload.question) or apply_classify.never_storable(payload.question):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Sensitive or consequential answers cannot enter the reusable library",
        )
    try:
        return save_answer(conn, **payload.model_dump(), user_id=device["user_id"])
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
