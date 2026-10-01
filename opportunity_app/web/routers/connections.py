"""Connected accounts, monitored events, notification preferences and phone verification."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
from contextlib import closing
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Header, Request, status

from ... import automation as automation_core
from ...actions import ApplicationNotFoundError
from ...auth import constant_time_equal
from ...connections import (
    ConnectionNotFoundError,
    connector_owner,
    begin_oauth,
    complete_oauth,
    apply_channel_opt_out,
    confirm_phone,
    connect_provider,
    decide_monitored_event,
    disconnect_provider,
    ensure_preferences,
    ingest_message,
    list_connectors,
    list_monitored_events,
    queue_notification,
    request_phone_verification,
    update_preferences,
)
from ...schema import connect_product
from ...timestamps import utc_now
from ...inbox_classifiers import client_for as inbox_client_for
from ...outreach_config import sender_account
from ..context import AppContext
from ..dependencies import get_ctx, require_auth, writable_connection
from ..models.connections import (
    ConnectorRequest,
    MonitoredDecisionRequest,
    MonitoredMessageRequest,
    NotificationOptOutRequest,
    NotificationPreferencesRequest,
    OAuthCompleteRequest,
    PhoneConfirmRequest,
    PhoneRequest,
)


router = APIRouter()


@router.get("/api/v1/connections")
def connections(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    items = list_connectors(conn, user_id=user_id)
    return {"items": items, "total": len(items)}


@router.post("/api/v1/connections", status_code=status.HTTP_201_CREATED)
def connect_account(
    payload: ConnectorRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return connect_provider(conn, payload.provider, user_id=user_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.get("/api/v1/connections/oauth/{provider}/start")
def start_oauth_connection(
    provider: Literal["google", "microsoft", "gmail_drafts"],
    request: Request,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    origin = os.environ.get("PIPELINE_PUBLIC_ORIGIN") or str(request.base_url).rstrip("/")
    redirect_uri = f"{origin}/connections/oauth/{provider}/callback"
    try:
        return begin_oauth(conn, provider, redirect_uri, user_id=user_id, login_hint=sender_account() if provider == "gmail_drafts" else "")
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.post("/api/v1/connections/oauth/{provider}/complete")
async def finish_oauth_connection(
    provider: Literal["google", "microsoft", "gmail_drafts"],
    payload: OAuthCompleteRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return await complete_oauth(conn, provider, payload.state, payload.code, os.environ.get("PIPELINE_CONNECTION_KEY", ""), user_id=user_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.delete("/api/v1/connections/{connector_id}")
def disconnect_account(
    connector_id: str,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return disconnect_provider(conn, connector_id, user_id=user_id)
    except ConnectionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connector not found") from exc


@router.get("/api/v1/monitored-events")
def monitored_events(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    items = list_monitored_events(conn, user_id=user_id)
    return {"items": items, "total": len(items)}


@router.post("/api/v1/monitored-events", status_code=status.HTTP_201_CREATED)
def ingest_monitored_message(
    payload: MonitoredMessageRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    try:
        return ingest_message(
            conn, **payload.model_dump(), user_id=user_id,
            decisions=inbox_client_for(conn, ctx.services.inbox_client_factory, user_id=user_id),
        )
    except ConnectionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connector not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.post("/api/v1/connections/webhook", status_code=status.HTTP_201_CREATED)
async def verified_connector_webhook(
    request: Request,
    signature: Annotated[str | None, Header(alias="X-Webhook-Signature")] = None,
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    secret = os.environ.get("PIPELINE_WEBHOOK_SECRET", "")
    if not secret:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Webhook receiver is not configured")
    body = await request.body()
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not signature or not constant_time_equal(signature, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature")
    try:
        payload = MonitoredMessageRequest.model_validate_json(body)
        with closing(connect_product(ctx.config.database_target)) as conn:
            # Webhook callers authenticate by HMAC, not session; the event
            # belongs to whichever user owns the referenced connector.
            owner_id = connector_owner(conn, payload.connector_id)
            return ingest_message(
                conn, **payload.model_dump(), user_id=owner_id,
                decisions=inbox_client_for(conn, ctx.services.inbox_client_factory, user_id=owner_id),
            )
    except ConnectionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Connector not found") from exc
    except (ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.post("/api/v1/monitored-events/{event_id}/decision")
def decide_monitored_update(
    event_id: str,
    payload: MonitoredDecisionRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return decide_monitored_event(conn, event_id, payload.decision, payload.application_id, user_id=user_id)
    except (ConnectionNotFoundError, ApplicationNotFoundError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Event or application not found") from exc
    except automation_core.CorrectionRefused as exc:
        # The application picked cannot take what the email says; nothing was decided, so pick another.
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except automation_core.Superseded as exc:
        # The application changed after the email's proposals were made: the card is settled, nothing applied.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/api/v1/notification-preferences")
def notification_preferences(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    return ensure_preferences(conn, user_id=user_id)


@router.put("/api/v1/notification-preferences")
def put_notification_preferences(
    payload: NotificationPreferencesRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return update_preferences(conn, payload.updates, user_id=user_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.post("/api/v1/notifications/opt-out")
def notification_opt_out(
    payload: NotificationOptOutRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    try:
        return apply_channel_opt_out(conn, payload.channel, payload.keyword, user_id=user_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.post("/api/v1/phone-verifications", status_code=status.HTTP_201_CREATED)
def create_phone_verification(
    payload: PhoneRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    try:
        return request_phone_verification(conn, payload.phone_e164, ctx.config.access_token, user_id=user_id)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.post("/api/v1/phone-verifications/confirm")
def verify_phone(
    payload: PhoneConfirmRequest,
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
    ctx: AppContext = Depends(get_ctx),
) -> dict[str, Any]:
    try:
        return confirm_phone(conn, payload.challenge_id, payload.code, ctx.config.access_token, user_id=user_id)
    except ConnectionNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Verification challenge not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc


@router.post("/api/v1/notifications/voice-check-in")
def request_voice_check_in(
    conn: sqlite3.Connection = Depends(writable_connection),
    user_id: str = Depends(require_auth),
) -> dict[str, Any]:
    return queue_notification(
        conn,
        "voice",
        f"requested-check-in:{utc_now()[:16]}",
        {"kind": "requested_check_in", "requested_by": "user"}, user_id=user_id)
