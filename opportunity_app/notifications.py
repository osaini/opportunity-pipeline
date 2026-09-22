"""Notification delivery engine: provider adapters, digest, reminders, health.

Sandbox remains the default delivery provider. A live SMTP provider is built
only when both PIPELINE_NOTIFICATIONS_LIVE=1 and PIPELINE_SMTP_HOST are set,
so development and tests can never contact an external system by accident.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Any, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .connections import ensure_preferences
from .schema import utc_now
from .user_time import UserTimezone, user_timezone

DIGEST_FREQUENCIES = {"immediate", "daily", "weekly", "off"}
OUTBOX_PENDING_STATES = ("sandbox_suppressed", "queued")


class NotificationProvider(Protocol):
    live: bool

    def deliver(self, channel: str, recipient: str, subject: str, body: str) -> dict[str, Any]:
        ...


class SandboxProvider:
    """Non-delivery provider: records intent without contacting any system."""

    live = False

    def deliver(self, channel: str, recipient: str, subject: str, body: str) -> dict[str, Any]:
        return {"delivered": False, "detail": "sandbox provider does not deliver", "recipient": recipient}


class SmtpProvider:
    """Minimal RFC 5321 email adapter for the live delivery path."""

    live = True

    def __init__(self, host: str, port: int, sender: str, username: str = "", password: str = "", use_tls: bool = True):
        self.host = host
        self.port = port
        self.sender = sender
        self.username = username
        self.password = password
        self.use_tls = use_tls

    def deliver(self, channel: str, recipient: str, subject: str, body: str) -> dict[str, Any]:
        if channel != "email" or not recipient:
            return {"delivered": False, "detail": f"channel {channel!r} has no transport"}
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = recipient
        message["Subject"] = subject
        message.set_content(body)
        try:
            with smtplib.SMTP(self.host, self.port, timeout=15) as client:
                if self.use_tls:
                    client.starttls()
                if self.username:
                    client.login(self.username, self.password)
                client.send_message(message)
        except OSError as exc:
            return {"delivered": False, "detail": f"smtp failure: {exc}"}
        return {"delivered": True, "detail": "smtp accepted"}


def build_provider() -> NotificationProvider:
    if os.environ.get("PIPELINE_NOTIFICATIONS_LIVE") == "1":
        host = os.environ.get("PIPELINE_SMTP_HOST", "")
        if host:
            return SmtpProvider(
                host=host,
                port=int(os.environ.get("PIPELINE_SMTP_PORT", "587")),
                sender=os.environ.get("PIPELINE_NOTIFY_SENDER") or "no-reply@localhost",
                username=os.environ.get("PIPELINE_SMTP_USERNAME", ""),
                password=os.environ.get("PIPELINE_SMTP_PASSWORD", ""),
                use_tls=os.environ.get("PIPELINE_SMTP_TLS", "1") == "1",
            )
    return SandboxProvider()


def _parse_hhmm(value: str) -> int:
    hours, minutes = str(value).split(":")
    return int(hours) * 60 + int(minutes)


def in_quiet_hours(
    preferences: dict[str, Any],
    now: datetime | None = None,
    *,
    zone: UserTimezone | None = None,
) -> bool:
    """Whether ``now`` falls inside the user's local quiet-hours window.

    Callers that know the user pass ``zone`` from ``user_time.user_timezone``,
    the same resolver the Urgent queue uses, so an unset preference falls back
    to ``PIPELINE_TIMEZONE`` or machine-local time rather than the column's
    ``'UTC'`` default. Without ``zone`` the preference's own name is used.
    """
    now = now or datetime.now(timezone.utc)
    if zone is not None:
        local_now = zone.to_local(now)
    else:
        try:
            local_now = now.astimezone(ZoneInfo(str(preferences.get("timezone", "UTC"))))
        except ZoneInfoNotFoundError:
            local_now = now
    current = local_now.hour * 60 + local_now.minute
    start = _parse_hhmm(preferences["quiet_start"])
    end = _parse_hhmm(preferences["quiet_end"])
    if start == end:
        return False
    if start < end:
        return start <= current < end
    return current >= start or current < end


def _user_email(conn: sqlite3.Connection, user_id: str) -> str:
    row = conn.execute("SELECT email FROM users WHERE id=?", (user_id,)).fetchone()
    return str(row["email"]) if row and row["email"] else ""


def _recipient_for_channel(
    conn: sqlite3.Connection,
    user_id: str,
    channel: str,
    preferences: dict[str, Any],
) -> str:
    """Resolve only a channel-appropriate, verified delivery destination.

    Web-push subscriptions are intentionally not inferred from another contact
    field. Until the product has an explicit subscription record, push remains
    suppressed even if a live provider supports other channels.
    """
    if channel == "email":
        return _user_email(conn, user_id)
    if channel in {"sms", "voice"} and preferences.get("phone_verified"):
        return str(preferences.get("phone_e164") or "")
    return ""


def run_notification_digest(
    conn: sqlite3.Connection,
    payload: dict[str, Any] | None = None,
    *,
    provider: NotificationProvider | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Deliver pending outbox entries per-user honoring digest preferences.

    Rows are only marked 'sent' when a live provider actually accepted the
    message; under the sandbox provider rows keep their suppressed state.
    """
    payload = payload or {}
    provider = provider or build_provider()
    frequency_filter = payload.get("frequency")
    stats = {"users_processed": 0, "messages_delivered": 0, "held_quiet_hours": 0, "cancelled_off": 0, "left_suppressed": 0}
    now = now or datetime.now(timezone.utc)
    users = [
        row["user_id"]
        for row in conn.execute(
            "SELECT DISTINCT user_id FROM notification_outbox WHERE status IN ('sandbox_suppressed', 'queued')"
        ).fetchall()
    ]
    for user_id in users:
        pending = conn.execute(
            "SELECT * FROM notification_outbox WHERE user_id=? AND status IN ('sandbox_suppressed', 'queued') ORDER BY created_at",
            (user_id,),
        ).fetchall()
        if not pending:
            continue
        preferences = ensure_preferences(conn, user_id=user_id)
        frequency = str(preferences["digest_frequency"])
        if frequency_filter and frequency != frequency_filter:
            continue
        stats["users_processed"] += 1
        if frequency == "off":
            with conn:
                conn.execute(
                    "UPDATE notification_outbox SET status='cancelled' WHERE user_id=? AND status IN ('sandbox_suppressed', 'queued')",
                    (user_id,),
                )
            stats["cancelled_off"] += len(pending)
            continue
        if in_quiet_hours(preferences, now, zone=user_timezone(conn, user_id)):
            stats["held_quiet_hours"] += len(pending)
            continue
        by_channel: dict[str, list] = {}
        for row in pending:
            by_channel.setdefault(str(row["channel"]), []).append(row)
        for channel, rows in by_channel.items():
            enabled = bool(preferences.get(f"{channel}_enabled"))
            if not enabled:
                with conn:
                    conn.execute(
                        f"UPDATE notification_outbox SET status='cancelled' WHERE id IN ({','.join('?' * len(rows))})",
                        tuple(row["id"] for row in rows),
                    )
                stats["cancelled_off"] += len(rows)
                continue
            if channel == "in_app":
                # In-app delivery is the record itself becoming visible as sent.
                with conn:
                    conn.execute(
                        f"UPDATE notification_outbox SET status='sent', delivered_at=? WHERE id IN ({','.join('?' * len(rows))})",
                        (now.isoformat(), *[row["id"] for row in rows]),
                    )
                stats["messages_delivered"] += len(rows)
                continue
            if not provider.live:
                stats["left_suppressed"] += len(rows)
                continue
            recipient = _recipient_for_channel(conn, user_id, channel, preferences)
            if not recipient:
                stats["left_suppressed"] += len(rows)
                continue
            lines = [f"- [{row['event_key']}] {json.loads(row['payload_json']).get('subject', '')}" for row in rows]
            result = provider.deliver(channel, recipient, f"You have {len(rows)} updates", "\n".join(lines))
            if result.get("delivered"):
                with conn:
                    conn.execute(
                        f"UPDATE notification_outbox SET status='sent', delivered_at=? WHERE id IN ({','.join('?' * len(rows))})",
                        (now.isoformat(), *[row["id"] for row in rows]),
                    )
                stats["messages_delivered"] += len(rows)
            else:
                stats["left_suppressed"] += len(rows)
    return stats


def _due_instant(value: Any) -> datetime | None:
    """Parse a stored ``due_at`` into an aware UTC instant.

    A naive value is treated as UTC (the legacy storage convention before
    reminders carried an offset); a bare date means midnight UTC that day.
    Unparseable values return ``None`` and are never fired.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def send_due_reminders(conn: sqlite3.Connection, *, provider: NotificationProvider | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Record due reminders in the outbox and attempt immediate delivery."""
    provider = provider or build_provider()
    now = now or datetime.now(timezone.utc)
    # Stored due_at values carry the student's own offset (e.g. -05:00), so a
    # string comparison against UTC "now" is wrong by the offset. Fetch every
    # scheduled candidate and compare parsed instants instead.
    candidates = conn.execute(
        """
        SELECT r.id, r.application_id, r.user_id, r.reminder_type, r.due_at, a.opportunity_id
        FROM reminders r JOIN applications a ON a.id=r.application_id
        WHERE r.status='scheduled'
        """
    ).fetchall()
    due_pairs = []
    for candidate in candidates:
        instant = _due_instant(candidate["due_at"])
        if instant is not None and instant <= now:
            due_pairs.append((instant, candidate))
    due_pairs.sort(key=lambda pair: pair[0])
    due = [candidate for _, candidate in due_pairs]
    stats = {"reminders_fired": 0, "messages_delivered": 0, "held_quiet_hours": 0}
    for reminder in due:
        user_id = str(reminder["user_id"])
        preferences = ensure_preferences(conn, user_id=user_id)
        event_key = f"reminder:{reminder['id']}"
        timestamp = utc_now()
        notification_id = f"notification-{hashlib.sha256(f'{user_id}|in_app|{event_key}'.encode()).hexdigest()[:24]}"
        with conn:
            conn.execute(
                """
                INSERT INTO notification_outbox(id, user_id, channel, event_key, payload_json, status, created_at)
                VALUES(?, ?, 'in_app', ?, ?, 'queued', ?)
                ON CONFLICT(user_id, channel, event_key) DO NOTHING
                """,
                (notification_id, user_id, event_key, json.dumps({
                    "subject": f"Reminder: {reminder['reminder_type']}",
                    "application_id": reminder["application_id"],
                    "opportunity_id": reminder["opportunity_id"],
                    "due_at": reminder["due_at"],
                }), timestamp),
            )
        if not in_quiet_hours(preferences, now, zone=user_timezone(conn, user_id)):
            with conn:
                conn.execute(
                    "UPDATE notification_outbox SET status='sent', delivered_at=? WHERE user_id=? AND event_key=? AND status='queued'",
                    (now.isoformat(), user_id, event_key),
                )
            stats["messages_delivered"] += 1
            recipient = _user_email(conn, user_id) if preferences["email_enabled"] and provider.live else ""
            if recipient:
                provider.deliver(
                    "email",
                    recipient,
                    f"Reminder: {reminder['reminder_type']}",
                    f"Your {reminder['reminder_type']} reminder for application {reminder['application_id']} was due at {reminder['due_at']}.",
                )
        else:
            stats["held_quiet_hours"] += 1
        with conn:
            conn.execute(
                "UPDATE reminders SET status='completed', updated_at=? WHERE id=? AND status='scheduled'",
                (timestamp, reminder["id"]),
            )
        stats["reminders_fired"] += 1
    return stats


def connector_health(conn: sqlite3.Connection) -> dict[str, Any]:
    """Report connection state and staleness for every registered connector."""
    items = []
    for row in conn.execute(
        """
        SELECT c.id, c.user_id, c.provider, c.status, c.disconnected_at, c.updated_at,
               LENGTH(c.encrypted_access_token) AS token_length
        FROM connector_accounts c ORDER BY c.user_id, c.provider
        """
    ).fetchall():
        if row["disconnected_at"]:
            state = "disconnected"
            detail = f"disconnected at {row['disconnected_at']}"
        elif row["status"] != "connected":
            state = str(row["status"])
            detail = "provider-reported non-connected status"
        elif not row["token_length"]:
            state = "unverified_sandbox"
            detail = "sandbox connector without OAuth tokens; no live mailbox is monitored"
        else:
            state = "connected"
            detail = f"last updated {row['updated_at']}"
        items.append({
            "connector_id": row["id"],
            "user_id": row["user_id"],
            "provider": row["provider"],
            "state": state,
            "detail": detail,
            "updated_at": row["updated_at"],
        })
    return {"items": items, "total": len(items), "checked_at": utc_now()}
