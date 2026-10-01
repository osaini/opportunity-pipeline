"""The public market snapshot archive and issues."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from ...market import (
    MarketNotFoundError,
    create_issue,
    create_snapshot,
    issue_record,
    public_issues,
    publish_issue,
    verify_snapshot,
)
from ...database import connect_product
from ..context import AppContext
from ..dependencies import get_ctx, require_auth, writable_connection
from ..models.market import MarketIssueRequest, MarketSnapshotRequest


router = APIRouter()


@router.post("/api/v1/market/snapshots", status_code=status.HTTP_201_CREATED)
def build_market_snapshot(
    payload: MarketSnapshotRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
) -> dict[str, Any]:
    try:
        return create_snapshot(conn, payload.as_of)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.get("/api/v1/market/snapshots/{snapshot_id}/verify")
def verify_market_snapshot(
    snapshot_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
) -> dict[str, Any]:
    try:
        return verify_snapshot(conn, snapshot_id)
    except MarketNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Snapshot not found") from exc


@router.post("/api/v1/market/issues", status_code=status.HTTP_201_CREATED)
def draft_market_issue(
    payload: MarketIssueRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return create_issue(conn, **payload.model_dump(), user_id=user_id)
    except MarketNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Snapshot not found") from exc


@router.get("/api/v1/market/issues/{issue_id}")
def get_market_issue(
    issue_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
) -> dict[str, Any]:
    try:
        return issue_record(conn, issue_id)
    except MarketNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found") from exc


@router.post("/api/v1/market/issues/{issue_id}/publish")
def publish_market_issue(
    issue_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
) -> dict[str, Any]:
    try:
        return publish_issue(conn, issue_id)
    except MarketNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Issue not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/api/v1/public/market")
def public_market_archive(ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    with closing(connect_product(ctx.config.database_target)) as conn:
        items = public_issues(conn)
    return {"items": items, "total": len(items)}
