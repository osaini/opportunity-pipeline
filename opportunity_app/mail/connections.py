"""Sandbox-first provider lifecycle, monitored event previews, and notifications."""

from __future__ import annotations

import hashlib
import hmac
import json
import base64
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from cryptography.fernet import Fernet

from ..applications.actions import ApplicationNotFoundError, add_application_task, update_application
from .classifiers import classify_email
from .monitored_classifier import classify_monitored_message
from ..outreach.config import sender_account
from ..core.timestamps import utc_now
from ..integrations.typesafe_decisions import DecisionClient


class ConnectionNotFoundError(LookupError):
    pass


OAUTH_PROVIDERS = {
    "google": {
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "scopes": ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/calendar.events.readonly"],
        "client_id_env": "GOOGLE_OAUTH_CLIENT_ID",
        "client_secret_env": "GOOGLE_OAUTH_CLIENT_SECRET",
    },
    # Approved outreach, as drafts or sent after a confirm click, with the
    # resume attached. gmail.compose drafts and sends. gmail.readonly lets the
    # app find and read the delivery failure notice for a send that bounced
    # (outreach/delivery.py). gmail.modify is used only to add the student's
    # label to outreach threads, sent mail and replies (outreach/labels.py: messages.batchModify
    # with addLabelIds, and labels.list and labels.create); nothing in the app
    # removes a label, trashes, archives or marks mail read. Not
    # gmail.metadata: with that granted, Gmail refuses to return a message's
    # text even alongside gmail.readonly.
    "gmail_drafts": {
        "authorize": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "scopes": [
            "https://www.googleapis.com/auth/gmail.compose",
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.modify",
        ],
        "client_id_env": "GOOGLE_OAUTH_CLIENT_ID",
        "client_secret_env": "GOOGLE_OAUTH_CLIENT_SECRET",
    },
    "microsoft": {
        "authorize": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "scopes": ["offline_access", "Mail.Read", "Calendars.Read"],
        "client_id_env": "MICROSOFT_OAUTH_CLIENT_ID",
        "client_secret_env": "MICROSOFT_OAUTH_CLIENT_SECRET",
    },
}


GMAIL_PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"


def begin_oauth(conn: sqlite3.Connection, provider: str, redirect_uri: str, *, user_id: str, login_hint: str = "") -> dict[str, Any]:
    config = OAUTH_PROVIDERS.get(provider)
    if not config:
        raise ValueError("Unsupported OAuth provider")
    client_id = os.environ.get(config["client_id_env"], "")
    if not client_id:
        raise ValueError(f"{config['client_id_env']} is not configured")
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    created = datetime.now(timezone.utc)
    with conn:
        conn.execute("INSERT INTO oauth_states(state_hash, user_id, provider, code_verifier, redirect_uri, expires_at, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                     (hashlib.sha256(state.encode()).hexdigest(), user_id, provider, verifier, redirect_uri, (created + timedelta(minutes=10)).isoformat(), created.isoformat()))
    from urllib.parse import urlencode
    params = {"client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": " ".join(config["scopes"]),
              "state": state, "code_challenge": challenge, "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent"}
    if login_hint:
        params["login_hint"] = login_hint
    query = urlencode(params)
    return {"provider": provider, "authorization_url": f"{config['authorize']}?{query}", "expires_at": (created + timedelta(minutes=10)).isoformat()}


def _google_reasons(response: httpx.Response) -> set[str]:
    """The reasons Google gave an error (status, errors[].reason, details[].reason); empty for anything unreadable."""
    reasons: set[str] = set()
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return reasons
    if not isinstance(error, dict):
        return reasons
    if isinstance(error.get("status"), str):
        reasons.add(error["status"])
    for key in ("errors", "details"):
        for item in error.get(key) or []:
            if isinstance(item, dict) and isinstance(item.get("reason"), str):
                reasons.add(item["reason"])
    return reasons


async def _gmail_account(client: httpx.AsyncClient, access_token: str) -> str:
    """The address a new Gmail connection signed into, or a ValueError that refuses the connection.

    Fails closed: a connection whose account cannot be confirmed is not saved.
    PIPELINE_OUTREACH_ACCOUNT is read through outreach_config.
    """
    try:
        profile = await client.get(GMAIL_PROFILE_URL, headers={"Authorization": f"Bearer {access_token}"})
    except httpx.HTTPError as exc:
        raise ValueError("Could not confirm which Gmail account connected; try connecting again") from exc
    if profile.status_code == 403:
        # A 403 is not always a missing permission: the Gmail API may be off in the project, or Google may be rate limiting.
        reasons = _google_reasons(profile)
        if reasons & {"accessNotConfigured", "SERVICE_DISABLED"}:
            raise ValueError("Enable the Gmail API in your Google Cloud project (README, Gmail drafts setup), then connect again")
        if reasons & {"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "RESOURCE_EXHAUSTED"}:
            raise ValueError("Could not confirm which Gmail account connected; try connecting again")
        raise ValueError("Google did not grant the Gmail permissions; connect again and tick every box on Google's screen")
    try:
        address = str(profile.json().get("emailAddress") or "").strip() if profile.status_code == 200 else ""
    except (ValueError, AttributeError):
        address = ""
    if not address:
        raise ValueError("Could not confirm which Gmail account connected; try connecting again")
    expected = sender_account()
    if expected and expected.casefold() != address.casefold():
        raise ValueError(f"Google signed in as {address}, but this pipeline's mailbox is {expected}. Connect again and choose {expected}.")
    return address


async def complete_oauth(conn: sqlite3.Connection, provider: str, state: str, code: str, encryption_key: str, *, user_id: str) -> dict[str, Any]:
    config = OAUTH_PROVIDERS.get(provider)
    state_hash = hashlib.sha256(state.encode()).hexdigest()
    row = conn.execute("SELECT * FROM oauth_states WHERE state_hash=? AND user_id=? AND provider=?", (state_hash, user_id, provider)).fetchone()
    if not config or not row or row["consumed_at"] or datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
        raise ValueError("OAuth state is invalid or expired")
    client_id = os.environ.get(config["client_id_env"], "")
    client_secret = os.environ.get(config["client_secret_env"], "")
    if not client_id or not client_secret or not encryption_key:
        raise ValueError("OAuth client credentials and PIPELINE_CONNECTION_KEY are required")
    try:
        fernet = Fernet(encryption_key.encode())
    except Exception as exc:
        raise ValueError("PIPELINE_CONNECTION_KEY must be a valid Fernet key") from exc
    account_email = ""
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        response = await client.post(config["token"], data={"grant_type": "authorization_code", "client_id": client_id,
            "client_secret": client_secret, "redirect_uri": row["redirect_uri"], "code": code, "code_verifier": row["code_verifier"]})
        if response.status_code == 200 and provider == "gmail_drafts":
            # Which mailbox this connection reads, before anything is written: a connection made
            # with the wrong Google account is refused here, not found out by a reply that never comes.
            try:
                access_token = str(response.json().get("access_token") or "")
            except ValueError:
                access_token = ""
            if access_token:
                account_email = await _gmail_account(client, access_token)
    if response.status_code != 200:
        raise ValueError("Provider rejected the OAuth exchange")
    tokens = response.json()
    if not tokens.get("access_token"):
        raise ValueError("Provider response did not contain an access token")
    # Google names the permissions it actually granted, and the student may leave a box unticked.
    granted = str(tokens.get("scope") or "").split() if provider == "gmail_drafts" else []
    scopes = sorted(set(granted)) if granted else config["scopes"]
    connector_id = f"connector-{provider}-{user_id}"
    timestamp = utc_now()
    existing_connector = conn.execute(
        "SELECT encrypted_refresh_token, token_granted_at FROM connector_accounts WHERE user_id=? AND provider=?",
        (user_id, provider),
    ).fetchone()
    refresh_token = tokens.get("refresh_token")
    # token_granted_at is when the refresh token in use was granted, from which
    # a Testing-mode Gmail grant's likely expiry is estimated. A reconnect that
    # kept the old refresh token keeps its grant time.
    if refresh_token:
        encrypted_refresh_token = fernet.encrypt(str(refresh_token).encode()).decode()
        token_granted_at = timestamp
    elif existing_connector:
        encrypted_refresh_token = str(existing_connector["encrypted_refresh_token"] or "")
        token_granted_at = existing_connector["token_granted_at"]
    else:
        encrypted_refresh_token = fernet.encrypt(b"").decode()
        token_granted_at = None
    with conn:
        # A fresh connection clears the error that asked for it.
        conn.execute("""INSERT INTO connector_accounts(id, user_id, provider, scopes_json, encrypted_access_token, encrypted_refresh_token, status, created_at, updated_at, token_granted_at, last_error, account_email)
            VALUES(?, ?, ?, ?, ?, ?, 'connected', ?, ?, ?, '', ?) ON CONFLICT(user_id, provider) DO UPDATE SET scopes_json=excluded.scopes_json,
            encrypted_access_token=excluded.encrypted_access_token, encrypted_refresh_token=excluded.encrypted_refresh_token,
            status='connected', updated_at=excluded.updated_at, disconnected_at=NULL,
            token_granted_at=excluded.token_granted_at, last_error='', account_email=excluded.account_email""",
            (connector_id, user_id, provider, json.dumps(scopes), fernet.encrypt(tokens["access_token"].encode()).decode(),
             encrypted_refresh_token, timestamp, timestamp, token_granted_at, account_email))
        conn.execute("UPDATE oauth_states SET consumed_at=? WHERE state_hash=?", (timestamp, state_hash))
    return connector_record(conn, connector_id, user_id=user_id)


def ensure_preferences(conn: sqlite3.Connection, *, user_id: str) -> dict[str, Any]:
    timestamp = utc_now()
    with conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO notification_preferences(user_id, updated_at)
            VALUES(?, ?)
            """,
            (user_id, timestamp),
        )
    row = conn.execute("SELECT * FROM notification_preferences WHERE user_id=?", (user_id,)).fetchone()
    return {key: bool(row[key]) if key.endswith("_enabled") or key == "phone_verified" else row[key] for key in row.keys()}


def update_preferences(conn: sqlite3.Connection, updates: dict[str, Any], *, user_id: str) -> dict[str, Any]:
    allowed = {"timezone", "quiet_start", "quiet_end", "digest_frequency", "in_app_enabled", "email_enabled", "push_enabled", "sms_enabled", "voice_enabled"}
    unknown = set(updates) - allowed
    if unknown:
        raise ValueError(f"Unsupported notification preferences: {', '.join(sorted(unknown))}")
    current = ensure_preferences(conn, user_id=user_id)
    merged = {**current, **updates}
    try:
        ZoneInfo(str(merged["timezone"]))
    except ZoneInfoNotFoundError as exc:
        raise ValueError("Unknown IANA timezone") from exc
    if merged["digest_frequency"] not in {"immediate", "daily", "weekly", "off"}:
        raise ValueError("Unsupported digest frequency")
    for field in ("quiet_start", "quiet_end"):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(merged[field])):
            raise ValueError(f"{field} must use 24-hour HH:MM")
    timestamp = utc_now()
    with conn:
        conn.execute(
            """
            UPDATE notification_preferences SET
                timezone=?, quiet_start=?, quiet_end=?, digest_frequency=?,
                in_app_enabled=?, email_enabled=?, push_enabled=?, sms_enabled=?,
                voice_enabled=?, timezone_explicit=?, updated_at=? WHERE user_id=?
            """,
            (merged["timezone"], merged["quiet_start"], merged["quiet_end"], merged["digest_frequency"], int(bool(merged["in_app_enabled"])), int(bool(merged["email_enabled"])), int(bool(merged["push_enabled"])), int(bool(merged["sms_enabled"])), int(bool(merged["voice_enabled"])), int("timezone" in updates or bool(current.get("timezone_explicit"))), timestamp, user_id),
        )
    return ensure_preferences(conn, user_id=user_id)


def connect_provider(conn: sqlite3.Connection, provider: str, *, user_id: str) -> dict[str, Any]:
    if provider != "sandbox":
        raise ValueError("Live Google/Microsoft OAuth is disabled until provider credentials are configured")
    connector_id = f"connector-{provider}-{user_id}"
    timestamp = utc_now()
    scopes = ["mail.metadata", "calendar.events.readonly"]
    with conn:
        conn.execute(
            """
            INSERT INTO connector_accounts(
                id, user_id, provider, scopes_json, status, created_at, updated_at
            ) VALUES(?, ?, ?, ?, 'connected', ?, ?)
            ON CONFLICT(user_id, provider) DO UPDATE SET
                scopes_json=excluded.scopes_json, status='connected',
                encrypted_access_token='', encrypted_refresh_token='',
                updated_at=excluded.updated_at, disconnected_at=NULL
            """,
            (connector_id, user_id, provider, json.dumps(scopes), timestamp, timestamp),
        )
    return connector_record(conn, connector_id, user_id=user_id)


def connector_record(conn: sqlite3.Connection, connector_id: str, *, user_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM connector_accounts WHERE id=? AND user_id=?", (connector_id, user_id)).fetchone()
    if not row:
        raise ConnectionNotFoundError(connector_id)
    result = dict(row)
    result["scopes"] = json.loads(row["scopes_json"] or "[]")
    for key in ("scopes_json", "encrypted_access_token", "encrypted_refresh_token"):
        result.pop(key, None)
    return result


def list_connectors(conn: sqlite3.Connection, *, user_id: str) -> list[dict[str, Any]]:
    ids = [row[0] for row in conn.execute("SELECT id FROM connector_accounts WHERE user_id=? ORDER BY created_at", (user_id,)).fetchall()]
    return [connector_record(conn, str(value), user_id=user_id) for value in ids]


def disconnect_provider(conn: sqlite3.Connection, connector_id: str, *, user_id: str) -> dict[str, Any]:
    timestamp = utc_now()
    with conn:
        cursor = conn.execute(
            """
            UPDATE connector_accounts SET status='disconnected', encrypted_access_token='',
                encrypted_refresh_token='', token_granted_at=NULL, updated_at=?, disconnected_at=?
            WHERE id=? AND user_id=?
            """,
            (timestamp, timestamp, connector_id, user_id),
        )
        if not cursor.rowcount:
            raise ConnectionNotFoundError(connector_id)
    return connector_record(conn, connector_id, user_id=user_id)


def ingest_message(
    conn: sqlite3.Connection,
    connector_id: str,
    external_id: str,
    subject: str,
    body: str,
    sender: str = "",
    *,
    user_id: str,
    decisions: DecisionClient | None = None,
) -> dict[str, Any]:
    """Record one delivered email as a pending tracker update the student confirms or ignores.

    With a decisions client its type comes from Jev when it is sure enough;
    without one, or when Jev cannot answer, from classify_monitored_message.
    """
    connector = connector_record(conn, connector_id, user_id=user_id)
    if connector["status"] != "connected":
        raise ValueError("Connector is disconnected")
    existing = conn.execute(
        "SELECT id FROM monitored_events WHERE user_id=? AND connector_id=? AND external_id=?",
        (user_id, connector_id, external_id),
    ).fetchone()
    if existing:
        return monitored_event(conn, str(existing[0]), user_id=user_id)
    event_type, confidence, classified_by = classify_email(subject, body, classify_monitored_message, decisions)
    event_id = f"event-{uuid4().hex}"
    timestamp = utc_now()
    payload = {
        "subject": subject[:1_000], "body_preview": body[:2_000], "sender": sender[:500], "classified_by": classified_by,
    }
    with conn:
        conn.execute(
            """
            INSERT INTO monitored_events(
                id, user_id, connector_id, external_id, event_type, confidence,
                payload_json, status, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (event_id, user_id, connector_id, external_id, event_type, confidence, json.dumps(payload), timestamp),
        )
    return monitored_event(conn, event_id, user_id=user_id)


def monitored_event(conn: sqlite3.Connection, event_id: str, *, user_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM monitored_events WHERE id=? AND user_id=?", (event_id, user_id)).fetchone()
    if not row:
        raise ConnectionNotFoundError(event_id)
    result = dict(row)
    result["payload"] = json.loads(row["payload_json"] or "{}")
    result.pop("payload_json", None)
    return result


def list_monitored_events(conn: sqlite3.Connection, *, user_id: str) -> list[dict[str, Any]]:
    ids = [row[0] for row in conn.execute("SELECT id FROM monitored_events WHERE user_id=? ORDER BY created_at DESC", (user_id,)).fetchall()]
    return [monitored_event(conn, str(value), user_id=user_id) for value in ids]


def queue_notification(conn: sqlite3.Connection, channel: str, event_key: str, payload: dict[str, Any], *, user_id: str) -> dict[str, Any]:
    preferences = ensure_preferences(conn, user_id=user_id)
    enabled = bool(preferences.get(f"{channel}_enabled", False))
    if channel in {"sms", "voice"} and not preferences["phone_verified"]:
        enabled = False
    notification_id = f"notification-{hashlib.sha256(f'{user_id}|{channel}|{event_key}'.encode()).hexdigest()[:24]}"
    timestamp = utc_now()
    # Development and test are deliberately non-delivery providers. Enabling a
    # channel records intent, but does not contact an external system.
    status = "sandbox_suppressed" if enabled else "cancelled"
    with conn:
        conn.execute(
            """
            INSERT INTO notification_outbox(id, user_id, channel, event_key, payload_json, status, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, channel, event_key) DO NOTHING
            """,
            (notification_id, user_id, channel, event_key, json.dumps(payload), status, timestamp),
        )
    row = conn.execute("SELECT * FROM notification_outbox WHERE user_id=? AND channel=? AND event_key=?", (user_id, channel, event_key)).fetchone()
    return {**dict(row), "payload": json.loads(row["payload_json"])}


def apply_channel_opt_out(conn: sqlite3.Connection, channel: str, keyword: str, *, user_id: str) -> dict[str, Any]:
    if channel not in {"email", "push", "sms", "voice"}:
        raise ValueError("Unsupported opt-out channel")
    if keyword.strip().upper() not in {"STOP", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}:
        raise ValueError("Unrecognized opt-out keyword")
    ensure_preferences(conn, user_id=user_id)
    field = f"{channel}_enabled"
    timestamp = utc_now()
    with conn:
        conn.execute(f"UPDATE notification_preferences SET {field}=0, updated_at=? WHERE user_id=?", (timestamp, user_id))
        conn.execute(
            """UPDATE notification_outbox SET status='cancelled'
               WHERE user_id=? AND channel=?
                 AND status IN ('queued', 'sandbox_suppressed')""",
            (user_id, channel),
        )
    return {"channel": channel, "opted_out": True, "updated_at": timestamp}


# What confirming an email does to the application the student picks.
# Assessment and scheduling emails add a task, never a stage.
EVENT_STAGES = {
    "application_confirmation": "applied", "interview": "interview", "offer": "offer", "rejected": "rejected",
    "assessment": None, "scheduling": None,
}
EVENT_TASKS = {"assessment": "Complete the assessment", "scheduling": "Schedule interview"}


def decide_event_directly(
    conn: sqlite3.Connection, event: dict[str, Any], decision: str, application_id: str | None, *, user_id: str,
) -> dict[str, Any]:
    """Confirm or ignore an email with no automation proposals behind it: the stage or task it points to, then the event."""
    event_id = event["id"]
    timestamp = utc_now()
    status = "ignored"
    if decision == "confirm":
        stage = EVENT_STAGES.get(event["event_type"])
        if stage:
            update_application(conn, application_id, stage=stage, user_id=user_id, source=f"monitored_event:{event_id}")
        task = EVENT_TASKS.get(event["event_type"])
        if task:
            add_application_task(
                conn, application_id, title=task, user_id=user_id, origin="monitored_event", origin_ref=event_id,
                source=f"monitored_event:{event_id}",
            )
        status = "confirmed"
        queue_notification(conn, "in_app", f"monitored:{event_id}", {"event_type": event["event_type"], "application_id": application_id}, user_id=user_id)
    with conn:
        conn.execute(
            "UPDATE monitored_events SET status=?, application_id=?, decided_at=?, decided_by='student' WHERE id=? AND user_id=?",
            (status, application_id, timestamp, event_id, user_id),
        )
    return monitored_event(conn, event_id, user_id=user_id)


def request_phone_verification(conn: sqlite3.Connection, phone: str, secret: str, *, user_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"\+[1-9]\d{7,14}", phone):
        raise ValueError("Phone number must use E.164 format")
    code = f"{secrets.randbelow(1_000_000):06d}"
    challenge_id = f"phone-{uuid4().hex}"
    created = datetime.now(timezone.utc)
    digest = hmac.new(secret.encode(), f"{challenge_id}:{code}".encode(), hashlib.sha256).hexdigest()
    with conn:
        conn.execute(
            "INSERT INTO phone_verifications(id, user_id, phone_e164, code_hash, expires_at, created_at) VALUES(?, ?, ?, ?, ?, ?)",
            (challenge_id, user_id, phone, digest, (created + timedelta(minutes=10)).isoformat(), created.isoformat()),
        )
    return {"id": challenge_id, "phone_e164": phone, "expires_at": (created + timedelta(minutes=10)).isoformat(), "sandbox_code": code, "delivery": "sandbox_suppressed"}


def confirm_phone(conn: sqlite3.Connection, challenge_id: str, code: str, secret: str, *, user_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM phone_verifications WHERE id=? AND user_id=?", (challenge_id, user_id)).fetchone()
    if not row or row["status"] != "pending":
        raise ConnectionNotFoundError(challenge_id)
    if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
        with conn:
            conn.execute("UPDATE phone_verifications SET status='expired' WHERE id=?", (challenge_id,))
        raise ValueError("Verification code expired")
    expected = hmac.new(secret.encode(), f"{challenge_id}:{code}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, row["code_hash"]):
        with conn:
            conn.execute("UPDATE phone_verifications SET attempts=attempts+1 WHERE id=?", (challenge_id,))
        raise ValueError("Invalid verification code")
    timestamp = utc_now()
    ensure_preferences(conn, user_id=user_id)
    with conn:
        conn.execute("UPDATE phone_verifications SET status='verified' WHERE id=?", (challenge_id,))
        conn.execute("UPDATE notification_preferences SET phone_e164=?, phone_verified=1, updated_at=? WHERE user_id=?", (row["phone_e164"], timestamp, user_id))
    return ensure_preferences(conn, user_id=user_id)


def connector_owner(conn: sqlite3.Connection, connector_id: str) -> str:
    """The user a connector account belongs to. A webhook authenticates by signature, not session, so its event belongs to
    whoever owns the connector it names. Raises ConnectionNotFoundError for an unknown connector."""
    owner = conn.execute(
        "SELECT user_id FROM connector_accounts WHERE id=?", (connector_id,)
    ).fetchone()
    if not owner:
        raise ConnectionNotFoundError(connector_id)
    return str(owner["user_id"])
