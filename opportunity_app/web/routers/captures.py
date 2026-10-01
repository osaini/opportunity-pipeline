"""Opportunity captures from a URL or a file."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import Depends, File, HTTPException, UploadFile, status

from ..overrides import shared_router
from ...captures import (
    MAX_CAPTURE_BYTES,
    CaptureNotFoundError,
    CaptureValidationError,
    capture_file as store_capture_file,
    capture_url,
    confirm_capture,
    get_capture,
)
from ..context import AppContext
from ..dependencies import get_ctx, require_auth, writable_connection
from ..models.captures import CaptureConfirmRequest, CaptureUrlRequest


router = shared_router()


@router.post("/api/v1/opportunity-captures/url", status_code=status.HTTP_201_CREATED)
def create_url_capture(
    payload: CaptureUrlRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return capture_url(conn, payload.url, user_id=user_id)
    except CaptureValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.post("/api/v1/opportunity-captures/file", status_code=status.HTTP_201_CREATED)
async def create_file_capture(
    capture: UploadFile = File(...),
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    original_name = capture.filename or "capture"
    data = await capture.read(MAX_CAPTURE_BYTES + 1)
    await capture.close()
    try:
        return store_capture_file(
            conn,
            data,
            original_name,
            ctx.config.capture_storage, user_id=user_id)
    except CaptureValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.get("/api/v1/opportunity-captures/{capture_id}")
def capture_detail(
    capture_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return get_capture(conn, capture_id, user_id=user_id)
    except CaptureNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Capture not found") from exc


@router.post("/api/v1/opportunity-captures/{capture_id}/confirm")
def confirm_capture_draft(
    capture_id: str,
    payload: CaptureConfirmRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return confirm_capture(conn, capture_id, payload.model_dump(), user_id=user_id)
    except CaptureNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Capture not found") from exc
    except CaptureValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
