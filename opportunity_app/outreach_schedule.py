"""Approved outreach emails sent on the recipient's next weekday morning.

With the scheduled_sending switch on, the student's confirmed Send queues the
approved draft instead of sending it at once. It goes out between 9:00 and
9:40 in the recipient's timezone on the next weekday, through the same
once-only path as Send (outreach_gmail.send_gmail_message), from the
AutomationWorker's background thread.

The student stays in charge of every email: only a draft they approved and
scheduled is sent, and only the words they scheduled. Editing the draft or
changing the recipient cancels the schedule (outreach._cancel_schedules), as
does Send now or Cancel. Anything that stops the send is shown on the card.

The recipient's timezone comes from the US state in the company's location;
without one it is the student's own, and the label says so.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .outreach import DraftChangedError, UNSENT_STATUSES, _city_state, _log, get_target
from .outreach_gmail import (
    ClientFactory,
    GmailAuthError,
    SendConflictError,
    SendNeedsCheckError,
    SendUnconfirmedError,
    _approved_for,
    gmail_drafts_status,
    send_gmail_message,
)
from .schema import utc_now
from .user_time import user_timezone

# Where a state spans zones, the zone most of its people live in.
STATE_ZONES = {
    "AL": "America/Chicago", "AK": "America/Anchorage", "AZ": "America/Phoenix", "AR": "America/Chicago",
    "CA": "America/Los_Angeles", "CO": "America/Denver", "CT": "America/New_York", "DE": "America/New_York",
    "DC": "America/New_York", "FL": "America/New_York", "GA": "America/New_York", "HI": "Pacific/Honolulu",
    "ID": "America/Boise", "IL": "America/Chicago", "IN": "America/Indiana/Indianapolis", "IA": "America/Chicago",
    "KS": "America/Chicago", "KY": "America/New_York", "LA": "America/Chicago", "ME": "America/New_York",
    "MD": "America/New_York", "MA": "America/New_York", "MI": "America/Detroit", "MN": "America/Chicago",
    "MS": "America/Chicago", "MO": "America/Chicago", "MT": "America/Denver", "NE": "America/Chicago",
    "NV": "America/Los_Angeles", "NH": "America/New_York", "NJ": "America/New_York", "NM": "America/Denver",
    "NY": "America/New_York", "NC": "America/New_York", "ND": "America/Chicago", "OH": "America/New_York",
    "OK": "America/Chicago", "OR": "America/Los_Angeles", "PA": "America/New_York", "RI": "America/New_York",
    "SC": "America/New_York", "SD": "America/Chicago", "TN": "America/Chicago", "TX": "America/Chicago",
    "UT": "America/Denver", "VT": "America/New_York", "VA": "America/New_York", "WA": "America/Los_Angeles",
    "WV": "America/New_York", "WI": "America/Chicago", "WY": "America/Denver",
}
# Cities on the other side of their state's line.
CITY_ZONES = {
    ("el paso", "TX"): "America/Denver", ("knoxville", "TN"): "America/New_York",
    ("chattanooga", "TN"): "America/New_York", ("pensacola", "FL"): "America/Chicago",
}
MORNING = time(9, 0)
# Spread across the first 40 minutes, so a batch does not all land at 9:00.
SPREAD_MINUTES = 40
RETRY_AFTER = timedelta(minutes=10)
MAX_ATTEMPTS = 3
STUCK_AFTER = timedelta(minutes=10)
LIVE_STATES = ("scheduled", "sending", "failed")


def recipient_zone(conn: sqlite3.Connection, target: dict[str, Any], *, user_id: str) -> tuple[Any, str]:
    """The zone to send in and a note on where it came from."""
    city, state = _city_state(target.get("location") or "")
    name = CITY_ZONES.get((city, state)) or STATE_ZONES.get(state)
    if name:
        return ZoneInfo(name), f"their time, from {target['location']}"
    own = user_timezone(conn, user_id)
    return (own.zone, "your time; their location names no US state")


def next_morning(now: datetime, zone: Any, seed: str) -> datetime:
    """The next weekday 9:00-9:40 in ``zone`` at least five minutes away, as UTC."""
    minutes = int(hashlib.sha256(seed.encode()).hexdigest(), 16) % SPREAD_MINUTES
    local = now.astimezone(zone) if zone else now.astimezone()
    day = local.date()
    while True:
        slot = datetime.combine(day, MORNING) + timedelta(minutes=minutes)
        slot = slot.replace(tzinfo=zone) if zone else slot.astimezone()
        if day.weekday() < 5 and slot > local + timedelta(minutes=5):
            return slot.astimezone(timezone.utc)
        day += timedelta(days=1)


def _label(send_at: datetime, zone: Any, basis: str) -> str:
    local = send_at.astimezone(zone) if zone else send_at.astimezone()
    return f"{local:%a, %b} {local.day}, {local:%I:%M %p %Z}".replace(" 0", " ").strip() + f" ({basis})"


def schedule_send(
    conn: sqlite3.Connection, target_id: str, *, user_id: str, kind: str, fingerprint: str, now: datetime | None = None,
) -> dict[str, Any]:
    """Queue the approved draft the student confirmed. Every check Send makes is made now, and again at send time."""
    now = now or datetime.now(timezone.utc)
    approved = _approved_for(conn, target_id, user_id, kind, sending=True, fingerprint=fingerprint)
    if not gmail_drafts_status(conn, user_id=user_id)["connected"]:
        raise ValueError("Connect Gmail before scheduling; scheduled emails go out from your Gmail")
    zone, basis = recipient_zone(conn, approved.target, user_id=user_id)
    send_at = next_morning(now, zone, f"{target_id}:{kind}")
    label = _label(send_at, zone, basis)
    stamp = utc_now()
    with conn:
        conn.execute(
            """
            INSERT INTO outreach_scheduled_sends(target_id, user_id, kind, fingerprint, send_at, timezone, label, state, error, attempts, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, 'scheduled', '', 0, ?, ?)
            ON CONFLICT(target_id, kind) DO UPDATE SET fingerprint=excluded.fingerprint, send_at=excluded.send_at,
                timezone=excluded.timezone, label=excluded.label, state='scheduled', error='', attempts=0, updated_at=excluded.updated_at
            """,
            (target_id, user_id, kind, fingerprint, send_at.isoformat(timespec="seconds"), getattr(zone, "key", "system-local"),
             label, stamp, stamp),
        )
        what = "follow-up" if kind == "follow_up" else "email"
        _log(conn, target_id, user_id, "send_scheduled", detail=f"The {what} to {approved.target['contact_email']} goes out {label}")
    return {"kind": kind, "send_at": send_at.isoformat(timespec="seconds"), "label": label, "state": "scheduled"}


def cancel_send(conn: sqlite3.Connection, target_id: str, *, user_id: str, kind: str, reason: str = "You cancelled it") -> bool:
    get_target(conn, target_id, user_id=user_id)
    with conn:
        cancelled = conn.execute(
            "UPDATE outreach_scheduled_sends SET state='cancelled', error=?, updated_at=? "
            "WHERE target_id=? AND user_id=? AND kind=? AND state IN ('scheduled', 'failed')",
            (reason, utc_now(), target_id, user_id, kind),
        ).rowcount
        if cancelled:
            _log(conn, target_id, user_id, "send_cancelled", detail=reason)
    return bool(cancelled)


def _finish(conn: sqlite3.Connection, row: sqlite3.Row, state: str, error: str = "") -> None:
    with conn:
        conn.execute(
            "UPDATE outreach_scheduled_sends SET state=?, error=?, updated_at=? WHERE target_id=? AND kind=?",
            (state, error[:500], utc_now(), row["target_id"], row["kind"]),
        )
        if state == "failed":
            _log(conn, row["target_id"], row["user_id"], "scheduled_send_failed", detail=error[:500])
        elif state == "cancelled":
            _log(conn, row["target_id"], row["user_id"], "send_cancelled", detail=error[:500])


def run_due_sends(conn: sqlite3.Connection, *, client_factory: ClientFactory, now: datetime | None = None) -> list[dict[str, Any]]:
    """Send every scheduled email that is due. Each outcome is recorded on its row and in the history."""
    now = now or datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    # A send cut off mid-way (the app stopped) may or may not have reached Gmail.
    for row in conn.execute(
        "SELECT * FROM outreach_scheduled_sends WHERE state='sending' AND updated_at<?",
        ((now - STUCK_AFTER).isoformat(timespec="microseconds"),),
    ).fetchall():
        _finish(conn, row, "failed", "The app stopped while sending this. Check your Gmail Sent folder before sending it again")
    results = []
    for row in conn.execute(
        "SELECT * FROM outreach_scheduled_sends WHERE state='scheduled' AND send_at<=? ORDER BY send_at", (stamp,),
    ).fetchall():
        with conn:
            claimed = conn.execute(
                "UPDATE outreach_scheduled_sends SET state='sending', updated_at=? WHERE target_id=? AND kind=? AND state='scheduled'",
                (utc_now(), row["target_id"], row["kind"]),
            ).rowcount
        if not claimed:
            continue
        outcome = {"target_id": row["target_id"], "kind": row["kind"]}
        try:
            sent = send_gmail_message(
                conn, row["target_id"], user_id=row["user_id"], kind=row["kind"], fingerprint=row["fingerprint"],
                client_factory=client_factory,
            )
        except DraftChangedError:
            _finish(conn, row, "cancelled", "The draft changed after you scheduled it. Approve it and schedule it again")
            outcome["state"] = "cancelled"
        except (SendNeedsCheckError, SendConflictError, SendUnconfirmedError, GmailAuthError) as exc:
            _finish(conn, row, "failed", str(exc))
            outcome["state"] = "failed"
        except ValueError as exc:
            target = get_target(conn, row["target_id"], user_id=row["user_id"])
            # Sent or answered some other way meanwhile: nothing is wrong, there is just nothing to send.
            moved_on = row["kind"] == "initial" and (target["sent_at"] or target["status"] not in UNSENT_STATUSES)
            _finish(conn, row, "cancelled" if moved_on else "failed", str(exc))
            outcome["state"] = "cancelled" if moved_on else "failed"
        except httpx.HTTPError as exc:
            # Never reached Gmail (a claim settled as nothing sent), so it is safe to try again shortly.
            attempts = row["attempts"] + 1
            if attempts >= MAX_ATTEMPTS:
                _finish(conn, row, "failed", f"Could not reach Gmail after {attempts} tries. Nothing was sent")
                outcome["state"] = "failed"
            else:
                with conn:
                    conn.execute(
                        "UPDATE outreach_scheduled_sends SET state='scheduled', attempts=?, send_at=?, error=?, updated_at=? "
                        "WHERE target_id=? AND kind=?",
                        (attempts, (now + RETRY_AFTER).isoformat(timespec="seconds"), f"Could not reach Gmail: {exc}"[:500],
                         utc_now(), row["target_id"], row["kind"]),
                    )
                outcome["state"] = "retrying"
        except RuntimeError as exc:
            _finish(conn, row, "failed", str(exc))
            outcome["state"] = "failed"
        else:
            _finish(conn, row, "sent")
            outcome.update(state="sent", to=sent.get("to"))
        results.append(outcome)
    return results
