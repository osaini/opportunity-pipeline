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

One email is queued without the student's click: a first email that bounced,
resent to the new contact with only its greeting changed (send_soon, from
outreach_automation with the bounce_auto_resend switch on). It is due at once
rather than on a weekday morning, and goes the same way from there.

Just before sending, Gmail is read again for a bounce or a reply
(outreach_review.fresh_look), and a follow-up is not sent to a company that
replied or whose first email bounced. With the follow_up_review switch on, a
second model reads each follow-up first (outreach_review.review_follow_up).
Both fail closed: a check that cannot be made holds the email. One that
cannot be made because Gmail asked the app to slow down waits for that hold
to end, without using up one of the email's tries.

The recipient's timezone comes from the US state in the company's location;
without one it is the student's own, and the label says so.

The student's pause (automation.set_paused) holds every scheduled email: the
worker claims none of theirs, and the hand-over to Gmail checks it again in
the transaction that marks the row transmitting, so either the pause lands
first and nothing goes, or the hand-over lands first and the pause reports the
email as already on its way. A held email that missed its morning moves to the
next one when the student resumes. Send now is the student's own act, and a
pause never stops it.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, time, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import httpx

from . import automation
from .outreach import NOT_INTERESTED, DraftChangedError, UNSENT_STATUSES, _city_state, _log, get_target, heard_back
from .outreach_gmail import (
    SENT_EVENT,
    THANK_YOU_KIND,
    ClientFactory,
    GmailAuthError,
    SendConflictError,
    SendNeedsCheckError,
    SendUnconfirmedError,
    ThankYouChanged,
    _approved_for,
    backoff_until,
    gmail_drafts_status,
    send_gmail_message,
    send_thank_you,
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
# Past this, a send has missed its morning (the computer was asleep or off) and
# waits for the next one rather than landing at an odd hour.
LATE_AFTER = timedelta(hours=2)
MAX_ATTEMPTS = 3
STUCK_AFTER = timedelta(minutes=10)
LIVE_STATES = ("scheduled", "sending", "transmitting", "failed")
PAUSED_NOTE = "Paused: this goes out when automation is resumed"
GMAIL_WAIT_NOTE = (
    "Could not check Gmail for replies or bounces first: Gmail asked the app to slow down. "
    "Waiting for Gmail's rate limit to pass; this does not use up a try"
)
# A send held by Gmail's rate limit is tried again this long after the hold ends.
GMAIL_HOLD_MARGIN = timedelta(minutes=1)
MISSED_NOTE = "Missed its morning while this computer was asleep or off"
HELD_NOTE = "Held while automation was paused"
# A thank-you goes only 9:00 to 17:00 on a weekday in their zone (outreach_thank_you.in_window).
AFTER_HOURS_NOTE = "Its time came outside 9 to 5 on a weekday in their time zone"
# The row belongs to a student who paused automation (for the worker's SQL).
_PAUSED = (
    "EXISTS (SELECT 1 FROM user_settings p WHERE p.user_id=outreach_scheduled_sends.user_id "
    "AND p.key='automation_paused' AND p.value='on')"
)


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
    approved = _ready_to_queue(conn, target_id, user_id=user_id, kind=kind, fingerprint=fingerprint)
    zone, basis = recipient_zone(conn, approved.target, user_id=user_id)
    send_at = next_morning(now, zone, f"{target_id}:{kind}")
    label = _label(send_at, zone, basis)
    what = "follow-up" if kind == "follow_up" else "email"
    _queue(conn, target_id, user_id=user_id, kind=kind, fingerprint=fingerprint, send_at=send_at,
           zone_key=getattr(zone, "key", "system-local"), label=label,
           detail=f"The {what} to {approved.target['contact_email']} goes out {label}")
    return {"kind": kind, "send_at": send_at.isoformat(timespec="seconds"), "label": label, "state": "scheduled"}


RESEND_LABEL = "Right away, to the new contact after the bounce"


def send_soon(
    conn: sqlite3.Connection, target_id: str, *, user_id: str, fingerprint: str, detail: str, now: datetime | None = None,
) -> dict[str, Any]:
    """Queue an approved first email for the worker's next pass, not a weekday morning (outreach_automation's resend).

    It takes the same path as a scheduled send, so the check for a reply or a
    bounce, the pause, Cancel, and the once-only send all apply to it.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    _ready_to_queue(conn, target_id, user_id=user_id, kind="initial", fingerprint=fingerprint)
    _queue(conn, target_id, user_id=user_id, kind="initial", fingerprint=fingerprint, send_at=now,
           zone_key="UTC", label=RESEND_LABEL, detail=detail)
    return {"kind": "initial", "send_at": now.isoformat(timespec="seconds"), "label": RESEND_LABEL, "state": "scheduled"}


def _ready_to_queue(conn: sqlite3.Connection, target_id: str, *, user_id: str, kind: str, fingerprint: str) -> Any:
    """Every check Send makes, made at queueing time as well as at send time. Returns the approved draft."""
    current = conn.execute(
        "SELECT state FROM outreach_scheduled_sends WHERE target_id=? AND user_id=? AND kind=?", (target_id, user_id, kind),
    ).fetchone()
    if current and current[0] in {"sending", "transmitting"}:
        raise SendConflictError("This email is being sent right now. Wait a moment, then reload")
    approved = _approved_for(conn, target_id, user_id, kind, sending=True, fingerprint=fingerprint)
    gmail = gmail_drafts_status(conn, user_id=user_id)
    if not gmail["connected"]:
        raise ValueError("Connect Gmail before scheduling; scheduled emails go out from your Gmail")
    if not gmail["bounce_check"]:
        # The check just before sending reads Gmail for replies and bounces.
        raise ValueError("Reconnect Gmail once before scheduling, so the app can check for replies and bounces before it sends")
    return approved


def _queue(
    conn: sqlite3.Connection, target_id: str, *, user_id: str, kind: str, fingerprint: str, send_at: datetime,
    zone_key: str, label: str, detail: str,
) -> None:
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
            (target_id, user_id, kind, fingerprint, send_at.isoformat(timespec="seconds"), zone_key, label, stamp, stamp),
        )
        _log(conn, target_id, user_id, "send_scheduled", detail=detail)


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
    """Settle a row the worker holds. A send that went out is recorded even if it was cancelled meanwhile.

    A thank-you after a decline (kind 'thank_you') is settled on its own row
    too (outreach_thank_you.settle_in), which says why in its history and
    leaves a notice when it is held or failed. Only a thank-you can be
    'held': the check before sending stopped it for the student to decide,
    and its scheduled row is cancelled.
    """
    held = "" if state == "sent" else " AND state IN ('sending', 'transmitting')"
    with conn:
        if not conn.execute(
            f"UPDATE outreach_scheduled_sends SET state=?, error=?, updated_at=? WHERE target_id=? AND kind=?{held}",
            ("cancelled" if state == "held" else state, error[:500], utc_now(), row["target_id"], row["kind"]),
        ).rowcount:
            return
        if row["kind"] == THANK_YOU_KIND:
            from .outreach_thank_you import settle_in  # imported here: it imports this module

            settle_in(conn, row["target_id"], row["user_id"], state, error)
            return
        if state == "failed":
            _log(conn, row["target_id"], row["user_id"], "scheduled_send_failed", detail=error[:500])
        elif state == "cancelled":
            _log(conn, row["target_id"], row["user_id"], "send_cancelled", detail=error[:500])


Reviewer = Callable[[], tuple[str, Callable[[str], str]]]


Decisions = Callable[[sqlite3.Connection, str], Any]
OnReply = Callable[[sqlite3.Connection, str, str], None]


def _gate(
    conn: sqlite3.Connection, row: sqlite3.Row, *, client_factory: ClientFactory, now: datetime, reviewer: Reviewer | None,
    decisions: Any = None, on_reply: OnReply | None = None,
) -> str | None:
    """The checks just before an automatic send. None to send; otherwise the outcome it was stopped with.

    ``decisions`` and ``on_reply`` are the InboxWatcher's: the fresh look reads
    every watched company's replies, and a reply it finds for another company
    is read (by Jev too) and handled as the InboxWatcher would have.
    """
    from .outreach_review import FRESH_LOOK_REASONS, fresh_look, review_follow_up, review_runner

    target_id, user_id = row["target_id"], row["user_id"]
    # Marking a company not interested stops what it has queued; this catches a send that was already being checked.
    if get_target(conn, target_id, user_id=user_id).get("not_interested_at"):
        _finish(conn, row, "cancelled", f"{NOT_INTERESTED}, so it was not sent")
        return "cancelled"
    if row["kind"] == "follow_up":
        stopped = _answered(conn, row, get_target(conn, target_id, user_id=user_id), now)
        if stopped:
            return stopped
    look = fresh_look(conn, target_id, user_id=user_id, client_factory=client_factory, decisions=decisions, on_reply=on_reply)
    if not look["ok"]:
        hold = backoff_until(user_id)
        if look["reason"] == FRESH_LOOK_REASONS["throttled"] and hold is not None:
            # Gmail asked this student's reads to wait: the email waits with
            # them, without using up a try. Any other failed check still counts.
            return _wait_for_gmail(conn, row, hold + GMAIL_HOLD_MARGIN)
        return _hold_for_retry(conn, row, now, f"Could not check Gmail for replies or bounces first: {look['reason']}")
    if row["kind"] == THANK_YOU_KIND:
        from .outreach_thank_you import gate  # imported here: it imports this module

        return gate(conn, row, client_factory=client_factory, now=now, reviewer=reviewer)
    target = get_target(conn, target_id, user_id=user_id)
    if row["kind"] != "follow_up":
        # A first email going out again (after a bounce) stops if anyone may have answered the earlier one.
        if heard_back(target) and _sent_before(conn, target_id, user_id):
            _finish(conn, row, "cancelled", "They may have answered your earlier email, so it was not sent again. "
                                            "Check the company's card")
            if row["label"] == RESEND_LABEL:
                # The app approved it for the new contact on its own; nothing stays approved that the student did not.
                with conn:
                    if conn.execute(
                        "UPDATE outreach_targets SET draft_status='generated', updated_at=? WHERE id=? AND user_id=? AND draft_status='approved'",
                        (utc_now(), target_id, user_id),
                    ).rowcount:
                        _log(conn, target_id, user_id, "approval_withdrawn",
                             detail="They may have answered the earlier email, so the automatic resend was not sent")
            return "cancelled"
        return None
    stopped = _answered(conn, row, target, now)
    if stopped:
        return stopped
    if target["contact_bounced"]:
        _finish(conn, row, "cancelled", f"Email to {target['contact_email']} bounced, so the follow-up was not sent")
        return "cancelled"
    # The stored switch alone: pause is enforced at the hand-over, just after this.
    if automation.mode(conn, user_id, "follow_up_review") != "on":
        return None
    zone, basis = recipient_zone(conn, target, user_id=user_id)
    try:
        name, run = (reviewer or review_runner)()
        verdict = review_follow_up(
            conn, target_id, user_id=user_id, runner=run, reviewer=name,
            today=(now.astimezone(zone) if zone else now.astimezone()).date(),
        )
    except Exception as exc:  # noqa: BLE001 - any reviewer failure holds the follow-up
        _finish(conn, row, "failed", f"The follow-up reviewer could not run: {exc}. Nothing was sent"[:500])
        return "failed"
    with conn:
        _log(conn, target_id, user_id, "follow_up_reviewed", detail=(
            f"Passed by {name}" if verdict["send"] else f"Held by {name}: " + "; ".join(verdict["problems"])
        )[:1_000])
    if verdict["send"]:
        # The review can take minutes: a reply, or an email that may be one, found meanwhile still stops it.
        return _answered(conn, row, get_target(conn, target_id, user_id=user_id), now)
    if verdict["away_until"]:
        back = datetime.combine(verdict["away_until"], time(0, 0))
        back = back.replace(tzinfo=zone) if zone else back.astimezone()
        send_at = next_morning(back, zone, f"{target_id}:follow_up")
        label = _label(send_at, zone, basis)
        with conn:
            if conn.execute(
                "UPDATE outreach_scheduled_sends SET state='scheduled', send_at=?, label=?, error=?, updated_at=? "
                "WHERE target_id=? AND kind=? AND state='sending'",
                (send_at.isoformat(timespec="seconds"), label, f"Held until they are back ({verdict['away_until'].isoformat()})",
                 utc_now(), target_id, row["kind"]),
            ).rowcount:
                _log(conn, target_id, user_id, "follow_up_held", detail=f"They are away; the follow-up now goes out {label}")
        return "held"
    _finish(conn, row, "failed", "The reviewer held this follow-up: " + "; ".join(verdict["problems"]))
    return "failed"


def _clock() -> datetime:
    """The real time, to measure how long a pass has run (a model review can take minutes)."""
    return datetime.now(timezone.utc)


def _sent_before(conn: sqlite3.Connection, target_id: str, user_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM outreach_events WHERE target_id=? AND user_id=? AND event_type=? LIMIT 1", (target_id, user_id, SENT_EVENT),
    ).fetchone() is not None


def _answered(conn: sqlite3.Connection, row: sqlite3.Row, target: dict[str, Any], now: datetime) -> str | None:
    """Stop a follow-up to a company that answered: cancelled for a reply, held (not a try) while an email may be one.

    None when nothing from them is on record and it may go.
    """
    if target["reply_count"] or target["status"] != "sent":
        _finish(conn, row, "cancelled", "They replied, or the company moved on, so the follow-up was not sent")
        return "cancelled"
    if not heard_back(target):
        return None
    # An email from them may be a reply: wait for the student to say, then go the next morning after.
    zone, basis = recipient_zone(conn, target, user_id=row["user_id"])
    send_at = next_morning(now, zone, f"{row['target_id']}:follow_up")
    label = _label(send_at, zone, basis)
    with conn:
        if conn.execute(
            "UPDATE outreach_scheduled_sends SET state='scheduled', send_at=?, label=?, error=?, attempts=0, updated_at=? "
            "WHERE target_id=? AND kind=? AND state='sending'",
            (send_at.isoformat(timespec="seconds"), label,
             f"Held: {target['company']} may have replied. Say whether it is a reply on the company's card",
             utc_now(), row["target_id"], row["kind"]),
        ).rowcount:
            _log(conn, row["target_id"], row["user_id"], "follow_up_held",
                 detail=f"An email from them may be a reply; the follow-up waits for you, and is looked at again {label}")
    return "held"


def run_due_sends(
    conn: sqlite3.Connection,
    *,
    client_factory: ClientFactory,
    now: datetime | None = None,
    reviewer: Reviewer | None = None,
    decisions_for: Decisions | None = None,
    on_reply: OnReply | None = None,
) -> list[dict[str, Any]]:
    """Send every scheduled email that is due, each after the checks in _gate.

    Each outcome is recorded on its row and in the history, and one email going
    wrong never stops the others. ``reviewer`` returns the follow-up reviewer's
    name and runner (outreach_review.review_runner by default). A student who
    paused automation has their rows left scheduled, untouched, until they resume.
    ``decisions_for`` (a student's Jev client, as the InboxWatcher asks for it)
    and ``on_reply`` are passed to the fresh look before each send.

    A thank-you after a decline goes only 9:00 to 17:00 on a weekday in their
    zone: one due outside that (a retry, a wait on Gmail, a pause, a sleeping
    computer) waits for their next weekday morning, and the hand-over looks at
    the time again after the checks, which can take minutes.
    """
    # Due times are stored in UTC and compared as text, so ``now`` must be UTC too.
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    started = _clock()
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
    from .outreach_thank_you import recover_stuck  # imported here: it imports this module

    # A thank-you the student's Send it anyway left 'sending' when the app stopped.
    recover_stuck(conn, now, STUCK_AFTER)
    decisions: dict[str, Any] = {}

    def decisions_of(user_id: str) -> Any:
        if decisions_for is None:
            return None
        if user_id not in decisions:
            try:
                decisions[user_id] = decisions_for(conn, user_id)
            except Exception:  # noqa: BLE001 - a reply is then read by its words alone, and says Jev was not asked
                decisions[user_id] = None
        return decisions[user_id]

    results = []
    for row in conn.execute(
        f"SELECT * FROM outreach_scheduled_sends WHERE state='scheduled' AND send_at<=? AND NOT {_PAUSED} ORDER BY send_at",
        (stamp,),
    ).fetchall():
        if now - datetime.fromisoformat(row["send_at"]) > LATE_AFTER:
            results.append({"target_id": row["target_id"], "kind": row["kind"], "state": _move_to_next_morning(conn, row, now)})
            continue
        if row["kind"] == THANK_YOU_KIND and _after_hours(conn, row, now + (_clock() - started)):
            results.append({"target_id": row["target_id"], "kind": row["kind"],
                            "state": _move_to_next_morning(conn, row, now, reason=AFTER_HOURS_NOTE)})
            continue
        with conn:
            claimed = conn.execute(
                "UPDATE outreach_scheduled_sends SET state='sending', updated_at=? "
                f"WHERE target_id=? AND kind=? AND state='scheduled' AND NOT {_PAUSED}",
                (utc_now(), row["target_id"], row["kind"]),
            ).rowcount
        if not claimed:
            continue
        outcome = {"target_id": row["target_id"], "kind": row["kind"]}
        try:
            outcome["state"] = _send_one(
                conn, row, client_factory=client_factory, now=now, reviewer=reviewer, decisions=decisions_of(row["user_id"]),
                on_reply=on_reply, clock=lambda: now + (_clock() - started),
            )
        except Exception as exc:  # noqa: BLE001 - one bad row must not stop the rest
            # Raised before Gmail was asked to send, so nothing went out.
            _finish(conn, row, "failed", f"Something went wrong before sending: {exc}. Nothing was sent"[:500])
            outcome["state"] = "failed"
        results.append(outcome)
    return results


def _held_by_pause(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    """Whether the student's pause changed after this send was due, so it was held rather than missed."""
    found = conn.execute(
        "SELECT updated_at FROM user_settings WHERE user_id=? AND key='automation_paused'", (row["user_id"],),
    ).fetchone()
    try:
        return bool(found) and datetime.fromisoformat(found[0]) > datetime.fromisoformat(row["send_at"])
    except (TypeError, ValueError):
        return False


def _after_hours(conn: sqlite3.Connection, row: sqlite3.Row, moment: datetime) -> bool:
    """Whether ``moment`` is outside a thank-you's window (9:00 to 17:00 on a weekday) in the recipient's zone."""
    from .outreach import OutreachNotFoundError
    from .outreach_thank_you import in_window  # imported here: it imports this module

    try:
        target = get_target(conn, row["target_id"], user_id=row["user_id"])
    except OutreachNotFoundError:
        return False  # the check before sending cancels it
    zone, _basis = recipient_zone(conn, target, user_id=row["user_id"])
    return not in_window(moment, zone)


def _to_next_morning_in(conn: sqlite3.Connection, row: sqlite3.Row, now: datetime, reason: str, *, states: tuple[str, ...]) -> bool:
    """Give a send the recipient's next weekday morning, and say why. Inside the caller's transaction."""
    target = get_target(conn, row["target_id"], user_id=row["user_id"])
    zone, basis = recipient_zone(conn, target, user_id=row["user_id"])
    send_at = next_morning(now, zone, f"{row['target_id']}:{row['kind']}")
    label = _label(send_at, zone, basis)
    moved = conn.execute(
        f"UPDATE outreach_scheduled_sends SET state='scheduled', send_at=?, label=?, error=?, updated_at=? "
        f"WHERE target_id=? AND kind=? AND state IN ({', '.join('?' for _ in states)})",
        (send_at.isoformat(timespec="seconds"), label, reason, utc_now(), row["target_id"], row["kind"], *states),
    ).rowcount
    if moved:
        _log(conn, row["target_id"], row["user_id"], "send_moved", detail=f"{reason}; now goes out {label}")
        if row["kind"] == THANK_YOU_KIND:
            conn.execute(
                "UPDATE outreach_thank_yous SET send_at=?, label=?, updated_at=? WHERE target_id=? AND user_id=? AND state='scheduled'",
                (send_at.isoformat(timespec="seconds"), label, utc_now(), row["target_id"], row["user_id"]),
            )
    return bool(moved)


def _move_to_next_morning(conn: sqlite3.Connection, row: sqlite3.Row, now: datetime, *, reason: str = MISSED_NOTE) -> str:
    """Give a send that missed its morning (or, for a thank-you, its window) the recipient's next one, and say why."""
    reason = HELD_NOTE if _held_by_pause(conn, row) else reason
    with conn:
        moved = _to_next_morning_in(conn, row, now, reason, states=("scheduled",))
    return "moved" if moved else "cancelled"


def _still_held(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
    found = conn.execute(
        "SELECT state FROM outreach_scheduled_sends WHERE target_id=? AND kind=?", (row["target_id"], row["kind"]),
    ).fetchone()
    return bool(found) and found[0] == "sending"


def _hand_over(conn: sqlite3.Connection, row: sqlite3.Row, *, now: datetime | None = None) -> str:
    """Mark the row as handed to Gmail, in one step with checking it was neither cancelled nor paused.

    Returns 'handed_over', 'cancelled', or 'paused'. The pause row is held first
    (automation.pause_guard), so a pause cannot land between the check and the
    mark. A paused row goes back in line as it was, without counting a try.

    A thank-you is checked once more in the same transaction
    (outreach_thank_you.hand_over_stop): 'held' when its switch or Jev inbox
    suggestions was turned off, 'cancelled' when they or the student wrote
    since, and 'moved' to their next weekday morning when the checks ran past
    its window. ``now`` is the pass's clock.
    """
    stamp = utc_now()
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    unanswered, params = "", ()
    if row["kind"] == "follow_up":
        unanswered = (
            " AND NOT EXISTS (SELECT 1 FROM outreach_events e WHERE e.target_id=? AND e.event_type='reply_logged')"
            " AND NOT EXISTS (SELECT 1 FROM outreach_inbox_messages m WHERE m.user_id=? AND m.kind='possible'"
            " AND (m.target_id=? OR m.candidates_json LIKE ?))"
        )
        params = (row["target_id"], row["user_id"], row["target_id"], f'%"{row["target_id"]}"%')
    with conn:
        is_paused = automation.pause_guard(conn, row["user_id"])
        if row["kind"] == THANK_YOU_KIND and not is_paused:
            from .outreach_thank_you import hand_over_stop, settle_in  # imported here: it imports this module

            stop = hand_over_stop(conn, row, now)
            if stop is not None:
                state, note = stop
                if state == "later":
                    return "moved" if _to_next_morning_in(conn, row, now, AFTER_HOURS_NOTE, states=("sending",)) else "cancelled"
                if conn.execute(
                    "UPDATE outreach_scheduled_sends SET state='cancelled', error=?, updated_at=? WHERE target_id=? AND kind=? AND state='sending'",
                    (note[:500], stamp, row["target_id"], row["kind"]),
                ).rowcount:
                    settle_in(conn, row["target_id"], row["user_id"], state, note)
                    return state
                return "cancelled"
        if conn.execute(
            "UPDATE outreach_scheduled_sends SET state='transmitting', updated_at=? WHERE target_id=? AND kind=? AND state='sending' "
            f"AND NOT EXISTS (SELECT 1 FROM user_settings WHERE user_id=? AND key='automation_paused' AND value='on'){unanswered}",
            (stamp, row["target_id"], row["kind"], row["user_id"], *params),
        ).rowcount:
            if row["kind"] == THANK_YOU_KIND:
                conn.execute(
                    "UPDATE outreach_thank_yous SET state='transmitting', updated_at=? WHERE target_id=? AND user_id=? AND state='scheduled'",
                    (stamp, row["target_id"], row["user_id"]),
                )
            return "handed_over"
        if not _still_held(conn, row):
            return "cancelled"
        if is_paused:
            conn.execute(
                "UPDATE outreach_scheduled_sends SET state='scheduled', error=?, updated_at=? WHERE target_id=? AND kind=? AND state='sending'",
                (PAUSED_NOTE, stamp, row["target_id"], row["kind"]),
            )
            return "paused"
    # Still ours and not paused: they answered, or may have, while the checks ran.
    return _answered(conn, row, get_target(conn, row["target_id"], user_id=row["user_id"]), now) or "cancelled"


def _send_one(
    conn: sqlite3.Connection, row: sqlite3.Row, *, client_factory: ClientFactory, now: datetime, reviewer: Reviewer | None = None,
    decisions: Any = None, on_reply: OnReply | None = None, clock: Callable[[], datetime] | None = None,
) -> str:
    """Check, then send, one claimed row and return its outcome.

    An error this raises was raised before Gmail was asked to send. One raised
    by the send itself is settled here, since Gmail may have acted on it.
    ``clock`` is the pass's time as the checks go on (``now`` plus how long the
    pass has run), for the hand-over's look at a thank-you's window.
    """
    if not _still_held(conn, row):
        return "cancelled"  # cancelled while it waited
    stopped = _gate(conn, row, client_factory=client_factory, now=now, reviewer=reviewer, decisions=decisions, on_reply=on_reply)
    if stopped:
        return stopped
    # The checks can take a while, and Cancel and pause still work during them; from here on neither can.
    handed = _hand_over(conn, row, now=clock() if clock else now)
    if handed != "handed_over":
        return handed
    if row["kind"] == THANK_YOU_KIND:
        return _send_thank_you(conn, row, client_factory=client_factory, now=now)
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


def _send_thank_you(conn: sqlite3.Connection, row: sqlite3.Row, *, client_factory: ClientFactory, now: datetime) -> str:
    """Hand a thank-you to Gmail (outreach_gmail.send_thank_you) and settle its outcome as _send_one does an email's."""
    from .outreach import OutreachNotFoundError

    try:
        send_thank_you(
            conn, row["target_id"], user_id=row["user_id"], fingerprint=row["fingerprint"], client_factory=client_factory,
            automatic=True,
        )
    except (ThankYouChanged, OutreachNotFoundError) as exc:
        _finish(conn, row, "cancelled", str(exc) or "The company is no longer in your outreach list")
        return "cancelled"
    except (SendNeedsCheckError, SendConflictError, SendUnconfirmedError, GmailAuthError) as exc:
        _finish(conn, row, "failed", str(exc))
        return "failed"
    except ValueError as exc:
        _finish(conn, row, "failed", str(exc))
        return "failed"
    except httpx.HTTPError:
        # Never reached Gmail (a claim settled as nothing sent), so it is safe to try again shortly.
        return _hold_for_retry(conn, row, now, "Could not reach Gmail")
    except Exception as exc:  # noqa: BLE001 - Gmail may have acted, so the student looks before anything else
        _finish(conn, row, "failed", f"Sending stopped with an error ({exc}). Check your Gmail Sent folder before sending it again"[:500])
        return "failed"
    _finish(conn, row, "sent")
    return "sent"


def _back_in_line(conn: sqlite3.Connection, row: sqlite3.Row) -> None:
    """A thank-you going back in line waits again as scheduled (it may have been handed over). Inside the caller's transaction."""
    if row["kind"] == THANK_YOU_KIND:
        from .outreach_thank_you import back_in_line  # imported here: it imports this module

        back_in_line(conn, row["target_id"], row["user_id"])


def _wait_for_gmail(conn: sqlite3.Connection, row: sqlite3.Row, send_at: datetime) -> str:
    """Put a send back in line for when Gmail's hold on this student's reads ends, without counting a try.

    Gmail asked the app to slow down, perhaps after another thread's read (the
    inbox check), so the check before sending could not run. That is not this
    email failing, and must not use up its tries and fail it unasked.
    """
    with conn:
        conn.execute(
            "UPDATE outreach_scheduled_sends SET state='scheduled', send_at=?, error=?, updated_at=? "
            "WHERE target_id=? AND kind=? AND state IN ('sending', 'transmitting')",
            (send_at.astimezone(timezone.utc).isoformat(timespec="seconds"), GMAIL_WAIT_NOTE, utc_now(), row["target_id"], row["kind"]),
        )
        _back_in_line(conn, row)
    return "retrying"


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
        _back_in_line(conn, row)
    return "retrying"
