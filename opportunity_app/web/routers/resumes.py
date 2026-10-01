"""Resume upload, versions, confirmation and download."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import Depends, File, HTTPException, Response, UploadFile, status
from fastapi.responses import FileResponse

from ..overrides import shared_router
from ... import resume_variants
from ...resumes import (
    MAX_RESUME_BYTES,
    ResumeNotFoundError,
    ResumeValidationError,
    confirm_resume,
    confirm_variant,
    delete_resume,
    list_resumes,
    resume_file_path,
    resume_record,
    store_resume,
)
from ..context import AppContext
from ..dependencies import get_ctx, require_auth, writable_connection
from ..models.resumes import ResumeConfirmRequest, ResumeVariantRequest


router = shared_router()


@router.get("/api/v1/resumes")
def resumes(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    items = list_resumes(conn, user_id=user_id)
    # Which labels the profile lists and which of them a confirmed résumé carries, so the page counts only those.
    return {"items": items, "total": len(items), "variants": resume_variants.variant_setup(conn, user_id)}


@router.post("/api/v1/resumes", status_code=status.HTTP_201_CREATED)
async def upload_resume(
    resume: UploadFile = File(...),
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    original_name = resume.filename or "resume"
    data = await resume.read(MAX_RESUME_BYTES + 1)
    await resume.close()
    try:
        return store_resume(
            conn,
            data=data,
            original_name=original_name,
            storage_root=ctx.config.resume_storage, user_id=user_id)
    except ResumeValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.get("/api/v1/resumes/{version_id}")
def get_resume(
    version_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return resume_record(conn, version_id, user_id=user_id)
    except ResumeNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found") from exc


@router.post("/api/v1/resumes/{version_id}/confirm")
def confirm_resume_version(
    version_id: str,
    payload: ResumeConfirmRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    try:
        return confirm_resume(
            conn,
            version_id,
            payload.confirmed_data,
            payload.profile_updates,
            payload.confirmed_profile_fields, user_id=user_id, profile_file=ctx.config.profile_file)
    except ResumeNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.post("/api/v1/resumes/{version_id}/variant")
def confirm_resume_variant(
    version_id: str,
    payload: ResumeVariantRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    """Use as a variant: confirm the résumé as a document to send, with a label, without changing profile facts."""
    try:
        return confirm_variant(conn, version_id, payload.variant_label, user_id=user_id)
    except ResumeNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found") from exc
    except ResumeValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.get("/api/v1/resumes/{version_id}/file")
def download_resume(
    version_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> FileResponse:
    try:
        path, filename, media_type = resume_file_path(conn, version_id, ctx.config.resume_storage, user_id=user_id)
    except ResumeNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found") from exc
    return FileResponse(path, media_type=media_type, filename=filename)


@router.delete("/api/v1/resumes/{version_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_resume(
    version_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> Response:
    try:
        delete_resume(conn, version_id, ctx.config.resume_storage, user_id=user_id)
    except ResumeNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
