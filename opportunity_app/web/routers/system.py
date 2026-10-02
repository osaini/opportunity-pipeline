"""Health, manual refresh, scheduler status and job-board sources."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, HTTPException, Request, status

from ..overrides import shared_router
from ...opportunities.refresh import RefreshBusy, fresh_steps
from ...core.database import connect_product, is_postgres_target
from ...opportunities.boards import BoardLookupExpired, BoardTracker
from ..context import AppContext
from ..dependencies import get_ctx, require_owner
from ..models.system import BoardAddRequest, BoardLookupRequest, RefreshStatusResponse


health_router = shared_router()
router = shared_router()


def refresh_status_payload(ctx: AppContext) -> RefreshStatusResponse:
    if ctx.services.refresh_manager is None:
        return RefreshStatusResponse(available=False, state="idle", steps=fresh_steps())
    return RefreshStatusResponse(available=True, **ctx.services.refresh_manager.status())


def require_board_tracker(ctx: AppContext) -> BoardTracker:
    if ctx.services.board_tracker is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Adding job boards is only available for the main database")
    return ctx.services.board_tracker


@health_router.get("/api/v1/health")
def health(request: Request, ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    if not is_postgres_target(ctx.config.database_target) and not Path(ctx.config.database_target).exists():
        return {"ok": False, "database": "missing", "version": request.app.version}
    try:
        with closing(connect_product(ctx.config.database_target, read_only=True)) as conn:
            conn.execute("SELECT 1 FROM opportunity_read_model LIMIT 1").fetchone()
    except sqlite3.Error:
        return {"ok": False, "database": "invalid", "version": request.app.version}
    return {"ok": True, "database": "ready", "version": request.app.version}


@router.get("/api/v1/refresh", response_model=RefreshStatusResponse)
def refresh_status(_owner: str = Depends(require_owner), ctx: AppContext = Depends(get_ctx)) -> RefreshStatusResponse:
    return refresh_status_payload(ctx)


@router.post("/api/v1/refresh", response_model=RefreshStatusResponse, status_code=status.HTTP_202_ACCEPTED)
def start_refresh(_owner: str = Depends(require_owner), ctx: AppContext = Depends(get_ctx)) -> RefreshStatusResponse:
    if ctx.services.refresh_manager is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Manual refresh is only available for the main database",
        )
    try:
        ctx.services.refresh_manager.start()
    except RefreshBusy as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return refresh_status_payload(ctx)


@router.get("/api/v1/system/status")
def system_status_report(_owner: str = Depends(require_owner), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    if ctx.services.system_status is None:
        return {"available": False, "jobs": [], "daily": None, "sources": None, "problems": [],
                "can_add_boards": ctx.services.board_tracker is not None}
    return {**ctx.services.system_status.status(), "can_add_boards": ctx.services.board_tracker is not None}


@router.post("/api/v1/system/schedules/{job}/install")
def install_scheduled_job(job: Literal["daily", "outreach"], _owner: str = Depends(require_owner), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    if ctx.services.system_status is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Scheduling is only available for the main database")
    try:
        return {**ctx.services.system_status.install(job), "can_add_boards": ctx.services.board_tracker is not None}
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.post("/api/v1/sources/lookup")
def look_up_job_board(payload: BoardLookupRequest, _owner: str = Depends(require_owner), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Probe Greenhouse, Ashby and Lever for a company's board. Writes nothing."""
    try:
        return require_board_tracker(ctx).look_up(payload.company)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.post("/api/v1/sources", status_code=status.HTTP_201_CREATED)
def add_job_board(payload: BoardAddRequest, _owner: str = Depends(require_owner), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """Track the board a lookup found, in config/sources.local.json."""
    tracker = require_board_tracker(ctx)
    try:
        return tracker.add(payload.lookup_id, student_confirmed=payload.student_confirmed)
    except BoardLookupExpired as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
