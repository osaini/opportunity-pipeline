"""The application tracker: list, export, import, detail, contacts and tasks."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile, status

from ... import application_inbox
from ...actions import (
    APPLICATION_STAGES,
    ApplicationNotFoundError,
    add_application_contact,
    add_application_task,
    application_analytics,
    application_detail,
    import_applications,
    list_applications,
    update_application,
    update_application_task,
)
from ..dependencies import require_auth, writable_connection
from ..models.applications import ApplicationUpdateRequest, ContactCreateRequest, TaskCreateRequest, TaskUpdateRequest


router = APIRouter()


@router.get("/api/v1/applications")
def applications(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    items = list_applications(conn, user_id=user_id)
    return {"items": items, "total": len(items), "stages": sorted(APPLICATION_STAGES)}


@router.get("/api/v1/applications/export")
def export_applications(
    export_format: Literal["json", "csv"] = Query(default="json", alias="format"),
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> Response:
    items = list_applications(conn, user_id=user_id)
    if export_format == "json":
        return Response(
            content=json.dumps(items, indent=2, sort_keys=True),
            media_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="applications.json"'},
        )
    output = io.StringIO(newline="")
    fields = [
        "id", "opportunity_id", "company", "title", "stage", "notes",
        "applied_at", "follow_up_at", "location", "region", "url", "created_at", "updated_at",
    ]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(items)
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="applications.csv"'},
    )


@router.get("/api/v1/applications/analytics")
def tracker_analytics(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    return application_analytics(conn, user_id=user_id)


@router.post("/api/v1/applications/import")
async def import_application_file(
    upload: UploadFile = File(...),
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    data = await upload.read(2 * 1024 * 1024 + 1)
    original_name = (upload.filename or "applications.json").lower()
    await upload.close()
    if len(data) > 2 * 1024 * 1024:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Application imports are limited to 2 MB",
        )
    try:
        text_data = data.decode("utf-8-sig")
        if original_name.endswith(".csv"):
            records = [dict(row) for row in csv.DictReader(io.StringIO(text_data))]
        else:
            parsed = json.loads(text_data)
            if isinstance(parsed, list):
                records = parsed
            elif isinstance(parsed, dict):
                records = parsed.get("items", [])
            else:
                raise ValueError("Import must be a JSON list or object with an items list")
        if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
            raise ValueError("Import must contain a list of application objects")
        return import_applications(conn, records, user_id=user_id)
    except (UnicodeDecodeError, json.JSONDecodeError, csv.Error, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.get("/api/v1/applications/{application_id}")
def get_application_detail(
    application_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        detail = application_detail(conn, application_id, user_id=user_id)
    except ApplicationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found") from exc
    # The job emails linked to it (application_inbox), each with a link to its Gmail thread.
    return {**detail, "emails": application_inbox.application_emails(conn, user_id, application_id)}


@router.post("/api/v1/applications/{application_id}/contacts", status_code=status.HTTP_201_CREATED)
def create_contact(
    application_id: str,
    payload: ContactCreateRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return add_application_contact(conn, application_id, **payload.model_dump(), user_id=user_id)
    except ApplicationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.post("/api/v1/applications/{application_id}/tasks", status_code=status.HTTP_201_CREATED)
def create_task(
    application_id: str,
    payload: TaskCreateRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return add_application_task(
            conn,
            application_id,
            title=payload.title,
            due_at=payload.due_at,
            timezone_name=payload.timezone,
            user_id=user_id,
        )
    except ApplicationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.patch("/api/v1/application-tasks/{task_id}")
def patch_task(
    task_id: str,
    payload: TaskUpdateRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return update_application_task(conn, task_id, status=payload.status, user_id=user_id)
    except ApplicationNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.patch("/api/v1/applications/{application_id}")
def patch_application(
    application_id: str,
    payload: ApplicationUpdateRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    if payload.stage is None and payload.notes is None and payload.follow_up_at is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Provide at least one field to update",
        )
    try:
        return update_application(
            conn,
            application_id,
            stage=payload.stage,
            notes=payload.notes,
            follow_up_at=payload.follow_up_at,
            timezone_name=payload.timezone, user_id=user_id)
    except ApplicationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Application not found",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
