"""Outreach drafts: generate, history, restore, the location line and approve."""

from __future__ import annotations

import sqlite3
from typing import Any, Literal

from fastapi import Depends, HTTPException, Query, status

from ..overrides import shared_router
from ...outreach.targets import (
    DraftChangedError,
    OutreachNotFoundError,
    approve_draft as approve_outreach_draft,
    get_target as get_outreach_target,
)
from ...outreach.drafting import generate_draft as generate_outreach_draft
from ...outreach.draft_location import sync_location_line
from ...outreach.versions import (
    DraftVersionNotFoundError,
    draft_versions as outreach_draft_versions,
    restore_draft_version as restore_outreach_draft_version,
)
from ..context import AppContext
from ..dependencies import get_ctx, require_auth, writable_connection
from ..errors import outreach_not_found
from ..models.outreach import OutreachApprovalRequest, OutreachDraftRequest


router = shared_router()
history_router = shared_router()


@router.post("/api/v1/outreach/{target_id}/draft")
def draft_outreach(
    target_id: str,
    payload: OutreachDraftRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    try:
        return generate_outreach_draft(
            conn, target_id, user_id=user_id, kind=payload.kind,
            provider_factory=ctx.services.outreach_provider_factory, provider=ctx.config.outreach_draft_provider,
            comments=payload.comments,
        )
    except OutreachNotFoundError as exc:
        raise outreach_not_found() from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"The draft model failed: {exc}") from exc


@history_router.get("/api/v1/outreach/{target_id}/drafts")
def outreach_draft_history(
    target_id: str,
    kind: Literal["initial", "follow_up"] = Query(default="initial"),
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    """Every stored draft of one kind, oldest first, so a worse regeneration can be undone."""
    try:
        return {"items": outreach_draft_versions(conn, target_id, user_id=user_id, kind=kind)}
    except OutreachNotFoundError as exc:
        raise outreach_not_found() from exc


@history_router.post("/api/v1/outreach/{target_id}/drafts/{version_id}/restore")
def restore_outreach_draft(
    target_id: str,
    version_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return restore_outreach_draft_version(conn, target_id, version_id, user_id=user_id)
    except OutreachNotFoundError as exc:
        raise outreach_not_found() from exc
    except DraftVersionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="That earlier draft was not found") from exc


@history_router.post("/api/v1/outreach/{target_id}/location-line")
def add_outreach_location_line(
    target_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    """Put the "(live in ...)" line into the draft after the school's name, approved or not; an approval is taken back."""
    try:
        sync_location_line(conn, target_id, user_id=user_id, approved=True)
        target = get_outreach_target(conn, target_id, user_id=user_id)
    except OutreachNotFoundError as exc:
        raise outreach_not_found() from exc
    if target["draft_location"]["missing"] and not target["sent_at"]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "The opening never names your school as your profile has it, so the line has nowhere exact to go. "
                f"Add '(live in {target['draft_location']['phrase']})' after your school yourself, or regenerate the draft."
            ),
        )
    return target


@history_router.post("/api/v1/outreach/{target_id}/approve")
def approve_outreach(
    target_id: str,
    payload: OutreachApprovalRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return approve_outreach_draft(
            conn, target_id, user_id=user_id, kind=payload.kind, fingerprint=payload.fingerprint,
            acknowledge_warnings=payload.acknowledge_warnings,
        )
    except OutreachNotFoundError as exc:
        raise outreach_not_found() from exc
    except DraftChangedError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
