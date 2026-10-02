"""The career dossier: items, settings and shares."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from typing import Any

from fastapi import Depends, HTTPException, Response, status

from ..overrides import shared_router
from ...dossier import (
    DossierNotFoundError,
    create_share,
    delete_all as delete_dossier_all,
    delete_item as delete_dossier_item,
    dossier,
    read_share,
    revoke_share,
    save_item as save_dossier_item,
    share_preview,
    update_settings as update_dossier_settings,
)
from ...core.database import connect_product
from ..context import AppContext
from ..dependencies import get_ctx, require_auth, writable_connection
from ..models.dossier import DossierItemRequest, DossierPreviewRequest, DossierSettingsRequest, DossierShareRequest


router = shared_router()


@router.get("/api/v1/dossier")
def career_dossier(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    return dossier(conn, user_id=user_id)


@router.get("/api/v1/dossier/export")
def export_dossier(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> Response:
    return Response(
        content=json.dumps(dossier(conn, user_id=user_id), indent=2, sort_keys=True),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="career-dossier.json"'},
    )


@router.put("/api/v1/dossier/settings")
def put_dossier_settings(
    payload: DossierSettingsRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return update_dossier_settings(conn, payload.paused, payload.retention_days, user_id=user_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.post("/api/v1/dossier/items", status_code=status.HTTP_201_CREATED)
def create_dossier_item(
    payload: DossierItemRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return save_dossier_item(conn, **payload.model_dump(), user_id=user_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.delete("/api/v1/dossier/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_dossier_item(
    item_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> Response:
    try:
        delete_dossier_item(conn, item_id, user_id=user_id)
    except DossierNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dossier item not found") from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/api/v1/dossier", status_code=status.HTTP_204_NO_CONTENT)
def remove_all_dossier_data(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> Response:
    delete_dossier_all(conn, user_id=user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/api/v1/dossier/shares/preview")
def preview_dossier_share(
    payload: DossierPreviewRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        items = share_preview(conn, payload.item_ids, user_id=user_id)
        return {"items": items, "total": len(items), "employer_visible_before_approval": False}
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.post("/api/v1/dossier/shares", status_code=status.HTTP_201_CREATED)
def create_dossier_share(
    payload: DossierShareRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return create_share(conn, **payload.model_dump(), user_id=user_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.delete("/api/v1/dossier/shares/{grant_id}")
def revoke_dossier_share(
    grant_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return revoke_share(conn, grant_id, user_id=user_id)
    except DossierNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Active share not found") from exc


@router.get("/api/v1/public/dossier-shares/{share_token}")
def public_dossier_share(share_token: str, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    try:
        with closing(connect_product(ctx.config.database_target)) as conn:
            return read_share(conn, share_token)
    except DossierNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Share not found, revoked, or expired") from exc
