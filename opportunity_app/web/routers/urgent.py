"""Dated things due soon and the researched early-program list."""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import Depends, HTTPException, Query, status

from pipeline_core import OpportunityRepository
from ..overrides import shared_router
from ...opportunities.early_programs import EarlyProgramNotFoundError, early_programs, set_program_status
from ...applications.urgent import urgent_queue
from ...core.schema import LOCAL_USER_ID
from ..context import AppContext
from ..dependencies import get_ctx, repository, require_auth, writable_connection
from ..models.opportunities import EarlyProgramStatusRequest


router = shared_router()


@router.get("/api/v1/urgent")
def get_urgent(
    days: int = Query(default=14, ge=1, le=60),
    repo: OpportunityRepository = Depends(repository),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    """Dated things due in the next ``days`` days, plus the last 60 days overdue."""
    return urgent_queue(
        repo.connection, user_id=repo.user_id or LOCAL_USER_ID, days=days, programs_path=ctx.config.early_programs_file,
    )


@router.get("/api/v1/early-programs")
def get_early_programs(repo: OpportunityRepository = Depends(repository), ctx: AppContext = Depends(get_ctx)) -> dict[str, Any]:
    """The student's researched early-program list, with the caller's progress."""
    return early_programs(repo.connection, user_id=repo.user_id or LOCAL_USER_ID, path=ctx.config.early_programs_file)


@router.put("/api/v1/early-programs/{program_id}/status")
def put_early_program_status(
    program_id: str,
    payload: EarlyProgramStatusRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    try:
        return set_program_status(
            conn, program_id, user_id=user_id, status=payload.status, path=ctx.config.early_programs_file
        )
    except EarlyProgramNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Program not found") from exc
