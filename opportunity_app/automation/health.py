"""What the Automation panel and the app-wide banner say about automation's health: read-only views of the ledger.

Gmail's connection state, how each background step last went, what is in flight or unconfirmed, what the circuit
breaker turned off, what the app just did on its own, and the paused banner, in one read (health_summary). All of it
reads the ledger, the notices and the settings that automation/ledger.py writes; nothing here changes how automation acts.
The writes that feed it (record_health, notice) stay in automation/ledger.py beside the ledger and the breaker.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from .ledger import BREAKER_NOTICE_PREFIX, FEATURES, in_flight, now_utc, paused, unconfirmed, undoable
from ..core.timestamps import parse_app_instant
from ..core.user_time import user_timezone

BREAKER_OFF_DAYS = 30
DEFAULT_GMAIL_TOKEN_DAYS = 7


def _token_days() -> int | None:
    """PIPELINE_GMAIL_TOKEN_DAYS: how long a Testing-mode Gmail grant lasts. 0 (production apps) or nonsense means no estimate."""
    try:
        days = int(os.environ.get("PIPELINE_GMAIL_TOKEN_DAYS", str(DEFAULT_GMAIL_TOKEN_DAYS)).strip())
    except ValueError:
        return None
    return days if days > 0 else None


def gmail_health(conn: sqlite3.Connection, user_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """The Gmail connection's state as the banner and health panel show it. Expiry is an estimate, and labelled so.

    The estimate is token_granted_at plus PIPELINE_GMAIL_TOKEN_DAYS. Once that
    date has passed, ``estimate_passed`` is True, and the date is no longer a
    "by <date>" promise:

    - if Gmail has answered since the date (last_ok_at is later), the estimate
      was wrong (a Google project in production has no 7-day limit), so it is
      retired: no date, and not expiring soon;
    - if not, the connection may still be asked to reconnect at any time, so
      it stays expiring soon, and the banner says "soon" without the old date.
    """
    now = now_utc(now)
    row = conn.execute(
        "SELECT status, last_ok_at, last_error, token_granted_at, backoff_until FROM connector_accounts WHERE user_id=? AND provider='gmail_drafts'",
        (user_id,),
    ).fetchone()
    if row is None:
        return {"state": "not_connected", "last_ok_at": None, "last_error": "", "backoff_until": None,
                "token_granted_at": None, "likely_expires_at": None, "expiring_soon": False, "estimate_passed": False}
    backoff = parse_app_instant(row["backoff_until"])
    if row["status"] == "error":
        state = "needs_reconnect"
    elif row["status"] == "disconnected":
        state = "disconnected"
    elif backoff is not None and backoff > now:
        state = "throttled"
    else:
        state = "connected"
    granted = parse_app_instant(row["token_granted_at"])
    days = _token_days()
    expires = granted + timedelta(days=days) if granted is not None and days is not None else None
    estimate_passed = expires is not None and now >= expires
    if estimate_passed:
        last_ok = parse_app_instant(row["last_ok_at"])
        if last_ok is not None and last_ok > expires:
            expires = None  # Gmail kept answering past the date: the estimate was wrong
    return {
        "state": state, "last_ok_at": row["last_ok_at"], "last_error": row["last_error"] or "",
        "backoff_until": row["backoff_until"], "token_granted_at": row["token_granted_at"],
        "likely_expires_at": expires.isoformat(timespec="seconds") if expires else None,
        "expiring_soon": bool(expires is not None and state == "connected" and expires - now <= timedelta(hours=24)),
        "estimate_passed": estimate_passed,
    }


def health_summary(conn: sqlite3.Connection, user_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Pause, component health, Gmail, what is in flight, and the app-wide banner, in one read."""
    now = now_utc(now)
    is_paused = paused(conn, user_id)
    components = []
    for row in conn.execute("SELECT * FROM automation_health WHERE user_id=? ORDER BY component", (user_id,)).fetchall():
        item = dict(row)
        item["detail"] = json.loads(item.pop("detail_json") or "{}")
        components.append(item)
    gmail = gmail_health(conn, user_id, now=now)
    since = (now - timedelta(hours=24)).isoformat(timespec="microseconds")
    counts = conn.execute(
        """
        SELECT COALESCE(SUM(CASE WHEN status='proposed' THEN 1 ELSE 0 END), 0) AS proposed,
               COALESCE(SUM(CASE WHEN status='shadow' AND review='' THEN 1 ELSE 0 END), 0) AS shadow_unreviewed,
               COALESCE(SUM(CASE WHEN applied_at IS NOT NULL AND applied_at>=? THEN 1 ELSE 0 END), 0) AS applied_last_24h
        FROM automation_actions WHERE user_id=?
        """,
        (since, user_id),
    ).fetchone()
    unread = conn.execute("SELECT COUNT(*) FROM automation_notices WHERE user_id=? AND read_at IS NULL", (user_id,)).fetchone()[0]
    zone = user_timezone(conn, user_id)
    flights = in_flight(conn, user_id, now=now)
    banner = []
    if is_paused:
        banner.append({"level": "warning", "key": "paused", "text": paused_text(flights)})
    if gmail["state"] == "needs_reconnect":
        banner.append({"level": "problem", "key": "gmail_needs_reconnect",
                       "text": "Gmail needs reconnecting. Reply and bounce checks have stopped."})
    if gmail["expiring_soon"]:
        if gmail["estimate_passed"]:
            # The estimated date is behind us: saying "by <that date>" would be false.
            text = "Gmail may ask you to reconnect soon."
        else:
            local = zone.to_local(datetime.fromisoformat(gmail["likely_expires_at"]))
            days = _token_days() or DEFAULT_GMAIL_TOKEN_DAYS
            text = (f"Gmail will likely ask you to reconnect by {local:%a, %b} {local.day}. "
                    f"Testing-mode connections last about {days} day{'s' if days != 1 else ''}.")
        banner.append({"level": "warning", "key": "gmail_expiring", "text": text})
    if gmail["state"] == "throttled":
        local = zone.to_local(parse_app_instant(gmail["backoff_until"]))
        banner.append({"level": "info", "key": "gmail_throttled",
                       "text": f"Gmail asked the app to slow down. Checks resume after {f'{local:%I:%M %p}'.lstrip('0')}."})
    return {
        "paused": is_paused,
        "components": components,
        "gmail": gmail,
        "in_flight": flights,
        "unconfirmed": unconfirmed(conn, user_id, now=now),
        "breaker_off": breaker_off(conn, user_id, now=now),
        "unread_notices": int(unread),
        "counts": {key: int(counts[key]) for key in ("proposed", "shadow_unreviewed", "applied_last_24h")},
        "recent_applied": recent_applied(conn, user_id, since=since),
        # How many there are in all, so the page can say "at least" when recent_applied was cut short.
        "recent_applied_total": recent_applied_count(conn, user_id, since=since),
        "banner": banner,
    }


RECENT_APPLIED_LIMIT = 5


def recent_applied(conn: sqlite3.Connection, user_id: str, *, since: str) -> list[dict[str, Any]]:
    """The newest changes the app made on its own since ``since``, still in place, for the page to announce with Undo.

    Only what the app applied itself (decided_by 'system'): a proposal the
    student approved is their own doing and is never announced back to them.
    """
    rows = conn.execute(
        """
        SELECT id, feature, action_type, summary, applied_at FROM automation_actions
        WHERE user_id=? AND status='applied' AND decided_by='system' AND applied_at IS NOT NULL AND applied_at>=?
        ORDER BY applied_at DESC, id DESC LIMIT ?
        """,
        (user_id, since, RECENT_APPLIED_LIMIT),
    ).fetchall()
    return [
        {"id": row["id"], "feature": row["feature"], "action_type": row["action_type"], "summary": row["summary"],
         "applied_at": row["applied_at"], "undoable": undoable(str(row["action_type"]))}
        for row in rows
    ]


def recent_applied_count(conn: sqlite3.Connection, user_id: str, *, since: str) -> int:
    """How many changes recent_applied would list with no limit."""
    return int(conn.execute(
        """
        SELECT COUNT(*) FROM automation_actions
        WHERE user_id=? AND status='applied' AND decided_by='system' AND applied_at IS NOT NULL AND applied_at>=?
        """,
        (user_id, since),
    ).fetchone()[0])


PAUSED_BANNER = "Automation is paused. Nothing is sent and no switch acts on its own. Replies and bounces are still recorded."


def _counted(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def paused_text(flights: list[dict[str, Any]]) -> str:
    """The pause banner, and a second sentence for whatever was already too far along to stop."""
    emails = sum(1 for item in flights if item["action"] == "send")
    forms = sum(1 for item in flights if item["action"] == "form")
    applications = sum(1 for item in flights if item["action"] == "application")
    parts = []
    if emails:
        parts.append(f"{_counted(emails, 'email')} {'was' if emails == 1 else 'were'} already handed to Gmail")
    if forms:
        parts.append(f"{_counted(forms, 'contact form')} {'was' if forms == 1 else 'were'} already being sent")
    if applications:
        parts.append(f"{_counted(applications, 'application')} {'was' if applications == 1 else 'were'} already being submitted")
    if not parts:
        return PAUSED_BANNER
    if len(parts) == 1:
        joined, stop, glue = parts[0], "can't be stopped", " and "
    elif len(parts) == 2:
        joined, stop, glue = f"{parts[0]} and {parts[1]}", "neither can be stopped", ", and "
    else:
        joined, stop, glue = f"{', '.join(parts[:-1])}, and {parts[-1]}", "none of them can be stopped", ", and "
    return f"{PAUSED_BANNER} {joined[:1].upper()}{joined[1:]}{glue}{stop}."


def breaker_off(conn: sqlite3.Connection, user_id: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Features the circuit breaker turned off in the last BREAKER_OFF_DAYS that are still off since.

    Read from the breaker's notices, so it holds after the student marks them
    read. A feature switched since the breaker wrote (turned back on, or set
    off again by the student) is no longer the breaker's doing, and is left out.
    """
    since = (now_utc(now) - timedelta(days=BREAKER_OFF_DAYS)).isoformat(timespec="microseconds")
    latest: dict[str, str] = {}
    for row in conn.execute(
        "SELECT event_key, created_at FROM automation_notices WHERE user_id=? AND event_key LIKE ? AND created_at>=? ORDER BY created_at",
        (user_id, f"{BREAKER_NOTICE_PREFIX}%", since),
    ).fetchall():
        feature = str(row["event_key"])[len(BREAKER_NOTICE_PREFIX):].split(":", 1)[0]
        latest[feature] = row["created_at"]
    items = []
    for feature, at in latest.items():
        definition = FEATURES.get(feature)
        if definition is None:
            continue
        setting = conn.execute(
            "SELECT value, updated_at FROM user_settings WHERE user_id=? AND key=?", (user_id, feature),
        ).fetchone()
        if setting is None or setting["value"] != "off" or setting["updated_at"] != at:
            continue
        items.append({"feature": feature, "label": definition.label, "at": at})
    return sorted(items, key=lambda item: item["at"])
