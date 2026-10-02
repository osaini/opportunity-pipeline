"""Outreach targets: list, create, export, import, detail, update and delete."""

from __future__ import annotations

import csv
import json
import os
import sqlite3
from typing import Any, Literal

from fastapi import Depends, File, HTTPException, Query, Response, UploadFile, status

from ..overrides import shared_router
from ...core.company_tags import decorate_outreach_with_tags, sync_outreach_tags
from ...outreach.targets import (
    CONTACT_CONFIDENCE,
    LocationConflictError,
    OUTREACH_PRIORITIES,
    OUTREACH_STATUSES,
    OutreachNotFoundError,
    SetAsideError as OutreachSetAsideError,
    create_target as create_outreach_target,
    delete_target as delete_outreach_target,
    export_csv as export_outreach_csv,
    export_json as export_outreach_json,
    filtered_target_ids as filtered_outreach_target_ids,
    get_target as get_outreach_target,
    import_targets as import_outreach_targets,
    list_targets as list_outreach_targets,
    outreach_summary,
    parse_import as parse_outreach_import,
    update_target as update_outreach_target,
)
from ...outreach.call_prep import auto_queue_call_prep
from ...outreach.config import sender_account
from ...outreach.automation import listing_switches
from ...outreach.gmail import gmail_drafts_status
from ..context import AppContext
from ..dependencies import get_ctx, require_auth, writable_connection
from ..errors import outreach_not_found
from ..models.outreach import OutreachTargetRequest
from ..payloads import outreach_discovery_payload, outreach_recontact_payload


router = shared_router()
detail_router = shared_router()


def outreach_compose_settings() -> dict[str, str]:
    """Where an approved draft opens when Gmail is not connected to send it."""
    provider = os.environ.get("PIPELINE_OUTREACH_COMPOSE", "mailto").strip().lower()
    account = sender_account()
    if provider not in {"gmail", "mailto"} or (provider == "gmail" and not account):
        provider = "mailto"
    return {"provider": provider, "account": account}


@router.get("/api/v1/outreach")
def outreach_targets(
    status_filter: str = Query(default="", alias="status", max_length=40),
    channel: str = Query(default="", max_length=100),
    q: str = Query(default="", max_length=200),
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    if status_filter and status_filter not in OUTREACH_STATUSES:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Unknown outreach status")
    sync_outreach_tags(conn, user_id)
    everything = list_outreach_targets(conn, user_id=user_id)
    if status_filter or channel or q.strip():
        # The filter and its order come from SQL (LIKE's rules included); the records are the ones already built.
        by_id = {item["id"]: item for item in everything}
        wanted = filtered_outreach_target_ids(conn, user_id=user_id, status=status_filter, channel=channel, query=q)
        items = [by_id[target_id] for target_id in wanted if target_id in by_id]
    else:
        items = everything
    items, tags = decorate_outreach_with_tags(conn, items, user_id=user_id)
    # Not available when no agent is installed here, and why, so the pane can say so.
    company_research: dict[str, Any] = {"available": ctx.services.call_prep_worker.can_research}
    if ctx.services.call_prep_worker.can_research and (problem := ctx.services.research_problem()):
        company_research = {"available": False, "reason": problem}
    return {
        "items": items,
        "total": len(items),
        "tags": tags,
        "summary": outreach_summary(everything),
        "statuses": list(OUTREACH_STATUSES),
        "priorities": list(OUTREACH_PRIORITIES),
        "contact_confidence": list(CONTACT_CONFIDENCE),
        "compose": outreach_compose_settings(),
        "gmail_drafts": gmail_drafts_status(conn, user_id=user_id),
        "automation": listing_switches(conn, user_id=user_id),
        "discovery": outreach_discovery_payload(ctx, conn, user_id),
        "recontact": outreach_recontact_payload(ctx, conn, user_id),
        "company_research": company_research,
    }


@router.post("/api/v1/outreach", status_code=status.HTTP_201_CREATED)
def create_outreach(
    payload: OutreachTargetRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return create_outreach_target(conn, payload.model_dump(exclude_unset=True), user_id=user_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.get("/api/v1/outreach/export")
def export_outreach(
    export_format: Literal["json", "csv"] = Query(default="json", alias="format"),
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> Response:
    items = list_outreach_targets(conn, user_id=user_id)
    if export_format == "json":
        return Response(
            content=export_outreach_json(items),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="outreach.json"'},
        )
    return Response(
        content=export_outreach_csv(items),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="outreach.csv"'},
    )


@router.post("/api/v1/outreach/import")
async def import_outreach_file(
    upload: UploadFile = File(...),
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    data = await upload.read(2 * 1024 * 1024 + 1)
    original_name = upload.filename or "outreach.json"
    await upload.close()
    if len(data) > 2 * 1024 * 1024:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail="Outreach imports are limited to 2 MB")
    try:
        records = parse_outreach_import(data, original_name)
        return import_outreach_targets(conn, records, user_id=user_id)
    except (UnicodeDecodeError, json.JSONDecodeError, csv.Error, ValueError, AttributeError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc) or "Unreadable import") from exc


@detail_router.get("/api/v1/outreach/{target_id}")
def outreach_detail(
    target_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return get_outreach_target(conn, target_id, user_id=user_id, include_events=True)
    except OutreachNotFoundError as exc:
        raise outreach_not_found() from exc


@detail_router.patch("/api/v1/outreach/{target_id}")
def update_outreach(
    target_id: str,
    payload: OutreachTargetRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    try:
        before = get_outreach_target(conn, target_id, user_id=user_id)
        updated = update_outreach_target(conn, target_id, payload.model_dump(exclude_unset=True), user_id=user_id)
        # Reaching a reply status starts call prep on its own, when there is a reply to write it from.
        if updated["status"] != before["status"] and auto_queue_call_prep(
            conn, target_id, user_id=user_id, reason=f"Status moved to {updated['status'].replace('_', ' ')}",
        ):
            ctx.services.call_prep_worker.wake()
            updated = get_outreach_target(conn, target_id, user_id=user_id)
        return updated
    except OutreachNotFoundError as exc:
        raise outreach_not_found() from exc
    # LocationConflictError subclasses ValueError, so this ordering is what
    # makes it a 409 rather than being swallowed as an invalid request.
    except LocationConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@detail_router.delete("/api/v1/outreach/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_outreach(
    target_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> Response:
    try:
        deleted = delete_outreach_target(conn, target_id, user_id=user_id)
    except OutreachSetAsideError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if not deleted:
        raise outreach_not_found()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
