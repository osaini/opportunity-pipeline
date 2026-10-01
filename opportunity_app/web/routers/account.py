"""Profile and account export and deletion."""

from __future__ import annotations

import json
import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Header, Response, status

from ...profile import get_profile, update_profile
from ...operations import delete_account, export_account
from ..context import AppContext
from ..dependencies import get_ctx, require_auth, writable_connection
from ..models.account import ProfileUpdateRequest


router = APIRouter()


@router.get("/api/v1/profile")
def profile(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return get_profile(conn, user_id=user_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found") from exc


@router.put("/api/v1/profile")
def put_profile(
    payload: ProfileUpdateRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    try:
        return update_profile(
            conn, payload.updates, payload.confirmed_fields, user_id=user_id, profile_file=ctx.config.profile_file
        )
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.get("/api/v1/profile/export")
def export_profile(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> Response:
    try:
        profile_data = get_profile(conn, user_id=user_id)["profile"]
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Profile not found") from exc
    return Response(
        content=json.dumps(profile_data, indent=2, sort_keys=True),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="profile.json"'},
    )


@router.get("/api/v1/account/export")
def export_full_account(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> Response:
    return Response(
        content=json.dumps(export_account(conn, user_id=user_id), indent=2, sort_keys=True),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="opportunity-account.json"'},
    )


@router.delete("/api/v1/account")
def permanently_delete_account(
    confirmation: Annotated[str | None, Header(alias="X-Confirm-Delete")] = None,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    if confirmation != "DELETE":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Send X-Confirm-Delete: DELETE to confirm permanent account deletion")
    return delete_account(conn, [ctx.config.resume_storage, ctx.config.capture_storage, ctx.config.interview_storage], user_id=user_id, apply_root=ctx.config.apply_storage)
