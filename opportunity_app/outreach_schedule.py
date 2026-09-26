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
import re
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
LIVE_STATES = ("scheduled", "sending", "transmitting", "failed")


# A state code at the end, with or without a comma or ZIP: "Denver CO", "Boston, MA 02110".
_TRAILING_STATE = re.compile(r"^(?P<city>.*?)[,\s]+(?P<state>[A-Za-z]{2})(?:\s+\d{5}(?:-\d{4})?)?\s*$")


def _place(location: str) -> tuple[str, str]:
    """(city, state code) from a location as students type it; the state is "" when none is named."""
    text = re.sub(r"\s+\d{5}(?:-\d{4})?\s*$", "", location.strip())
    city, state = _city_state(text)
    if state:
        return city, state
    match = _TRAILING_STATE.match(text)
    if match and match["state"].upper() in STATE_ZONES:
        return " ".join(match["city"].casefold().split()), match["state"].upper()
    return city, ""


def recipient_zone(conn: sqlite3.Connection, target: dict[str, Any], *, user_id: str) -> tuple[Any, str]:
    """The zone to send in and a note on where it came from."""
    location = str(target.get("location") or "").strip()
    city, state = _place(location)
    name = CITY_ZONES.get((city, state)) or STATE_ZONES.get(state)
    if name:
        return ZoneInfo(name), f"their time, from {location}"
    own = user_timezone(conn, user_id)
    if not location:
        return own.zone, "your time; no location on file for them"
    return own.zone, "your time; their location names no US state"


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
    from .outreach_automation import settings as automation_settings  # imported here: automation imports this module

    now = now or datetime.now(timezone.utc)
    if not automation_settings(conn, user_id=user_id)["scheduled_sending"]:
        raise ValueError("Turn on Send on their weekday morning under Outreach settings first")
    current = conn.execute(
        "SELECT state FROM outreach_scheduled_sends WHERE target_id=? AND user_id=? AND kind=?", (target_id, user_id, kind),
    ).fetchone()
    if current and current[0] in {"sending", "transmitting"}:
        raise SendConflictError("This email is being sent right now. Wait a moment, then reload")
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
                WHERE outreach_scheduled_sends.state NOT IN ('sending', 'transmitting')
            """,
            (target_id, user_id, kind, fingerprint, send_at.isoformat(timespec="seconds"), getattr(zone, "key", "system-local"),
             label, stamp, stamp),
        )
        what = "follow-up" if kind == "follow_up" else "email"
        _log(conn, target_id, user_id, "send_scheduled", detail=f"The {what} to {approved.target['contact_email']} goes out {label}")
    return {"kind": kind, "send_at": send_at.isoformat(timespec="seconds"), "label": label, "state": "scheduled"}


def cancel_send(conn: sqlite3.Connection, target_id: str, *, user_id: str, kind: str, reason: str = "You cancelled it") -> bool:
    """Stop a scheduled send. False when there was nothing left to stop.

    A send the worker is still checking is stopped too: the worker hands it to
    Gmail only if the row is still its own ('sending' to 'transmitting' in one
    step). One already handed over ('transmitting') cannot be stopped, and
    this says so by returning False.
    """
    get_target(conn, target_id, user_id=user_id)
    with conn:
        cancelled = conn.execute(
            "UPDATE outreach_scheduled_sends SET state='cancelled', error=?, updated_at=? "
            "WHERE target_id=? AND user_id=? AND kind=? AND state IN ('scheduled', 'sending', 'failed')",
            (reason, utc_now(), target_id, user_id, kind),
        ).rowcount
        if cancelled:
            _log(conn, target_id, user_id, "send_cancelled", detail=reason)
    return bool(cancelled)


def _finish(conn: sqlite3.Connection, row: sqlite3.Row, state: str, error: str = "") -> None:
    """Settle a row the worker holds. A send that went out is recorded even if it was cancelled meanwhile."""
    held = "" if state == "sent" else " AND state IN ('sending', 'transmitting')"
    with conn:
        if not conn.execute(
            f"UPDATE outreach_scheduled_sends SET state=?, error=?, updated_at=? WHERE target_id=? AND kind=?{held}",
            (state, error[:500], utc_now(), row["target_id"], row["kind"]),
        ).rowcount:
            return
        if state == "failed":
            _log(conn, row["target_id"], row["user_id"], "scheduled_send_failed", detail=error[:500])
        elif state == "cancelled":
            _log(conn, row["target_id"], row["user_id"], "send_cancelled", detail=error[:500])


def run_due_sends(conn: sqlite3.Connection, *, client_factory: ClientFactory, now: datetime | None = None) -> list[dict[str, Any]]:
    """Send every scheduled email that is due. Each outcome is recorded on its row and in the history.

    One email going wrong never stops the others.
    """
    # Due times are stored in UTC and compared as text, so ``now`` must be UTC too.
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    # A send cut off mid-way (the app stopped). Still being checked, it never
    # reached Gmail and goes back in line; handed over, it may have gone out.
    stuck = (now - STUCK_AFTER).isoformat(timespec="microseconds")
    for row in conn.execute(
        "SELECT * FROM outreach_scheduled_sends WHERE state IN ('sending', 'transmitting') AND updated_at<?", (stuck,),
    ).fetchall():
        if row["state"] == "sending":
            _hold_for_retry(conn, row, now, "The app stopped before sending this")
        else:
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
            outcome["state"] = _send_one(conn, row, client_factory=client_factory, now=now)
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the rest
            # Raised before Gmail was asked to send, so nothing went out.
            _finish(conn, row, "failed", f"Something went wrong before sending: {exc}. Nothing was sent"[:500])
            outcome["state"] = "failed"
        results.append(outcome)
    return results


def _still_held(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    found = conn.execute(
        "SELECT state FROM outreach_scheduled_sends WHERE target_id=? AND kind=?", (row["target_id"], row["kind"]),
    ).fetchone()
    return bool(found) and found[0] == "sending"


def _hand_over(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """Mark the row as handed to Gmail, in one step with checking it was not cancelled. False if it was."""
    with conn:
        return bool(conn.execute(
            "UPDATE outreach_scheduled_sends SET state='transmitting', updated_at=? WHERE target_id=? AND kind=? AND state='sending'",
            (utc_now(), row["target_id"], row["kind"]),
        ).rowcount)


def _send_one(conn: sqlite3.Connection, row: sqlite3.Row, *, client_factory: ClientFactory, now: datetime) -> str:
    """Send one claimed row and return its outcome.

    An error this raises was raised before Gmail was asked to send. One raised
    by the send itself is settled here, since Gmail may have acted on it.
    """
    if not _hand_over(conn, row):
        return "cancelled"  # cancelled while it waited
    try:
        sent = send_gmail_message(
            conn, row["target_id"], user_id=row["user_id"], kind=row["kind"], fingerprint=row["fingerprint"],
            client_factory=client_factory,
        )
    except DraftChangedError:
        _finish(conn, row, "cancelled", "The draft changed after you scheduled it. Approve it and schedule it again")
        return "cancelled"
    except (SendNeedsCheckError, SendConflictError, SendUnconfirmedError, GmailAuthError) as exc:
        _finish(conn, row, "failed", str(exc))
        return "failed"
    except ValueError as exc:
        target = get_target(conn, row["target_id"], user_id=row["user_id"])
        # Sent or answered some other way meanwhile: nothing is wrong, there is just nothing to send.
        moved_on = row["kind"] == "initial" and (target["sent_at"] or target["status"] not in UNSENT_STATUSES)
        _finish(conn, row, "cancelled" if moved_on else "failed", str(exc))
        return "cancelled" if moved_on else "failed"
    except httpx.HTTPError:
        # Never reached Gmail (a claim settled as nothing sent), so it is safe to try again shortly.
        return _hold_for_retry(conn, row, now, "Could not reach Gmail")
    except Exception as exc:  # noqa: BLE001 - Gmail may have acted, so the student looks before anything else
        _finish(conn, row, "failed", f"Sending stopped with an error ({exc}). Check your Gmail Sent folder before sending it again"[:500])
        return "failed"
    _finish(conn, row, "sent")
    return "sent"


def _hold_for_retry(conn: sqlite3.Connection, row: sqlite3.Row, now: datetime, reason: str) -> str:
    """Put a send the worker holds back in line for a little later, or give up after MAX_ATTEMPTS."""
    attempts = row["attempts"] + 1
    if attempts >= MAX_ATTEMPTS:
        _finish(conn, row, "failed", f"{reason} after {attempts} tries. Nothing was sent")
        return "failed"
    with conn:
        conn.execute(
            "UPDATE outreach_scheduled_sends SET state='scheduled', attempts=?, send_at=?, error=?, updated_at=? "
            "WHERE target_id=? AND kind=? AND state IN ('sending', 'transmitting')",
            (attempts, (now + RETRY_AFTER).isoformat(timespec="seconds"), reason[:500], utc_now(), row["target_id"], row["kind"]),
        )
    return "retrying"
