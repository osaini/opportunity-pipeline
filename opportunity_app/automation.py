"""Everything the app does on its own: the switches, the master pause, the ledger, and its health.

Switches. FEATURES is the one list of automation features. Each is off until
the student turns it on (a missing user_settings row means off). A feature
that needs something besides its switch (a threshold in the student's
profile, or a résumé variant set up) names it in REQUIREMENTS, and cannot be turned on without it. A feature
that can act on the student's records without asking may also run in
"shadow": it records what it would have done and changes nothing. It can be
turned on only after SHADOW_HOURS in shadow with at least SHADOW_MIN_ROWS
shadow actions, every one reviewed and none marked wrong (can_turn_on).

Pause. 'automation_paused' stops everything the app does on its own at
once: every feature switch (Jev inbox suggestions included), scheduled sends,
automatic contact-form submissions, and call prep the app starts without a
click. It never stops something the student does themselves, such as Send
now. It does not stop the app reading Gmail either: replies and bounces are
facts that have already happened, so they are still recorded (and move a
company to Replied or Bounced), and notices still appear, since they only
inform. An email already handed to Gmail, or a contact form whose button is
being pressed, cannot be stopped, so pausing reports it (in_flight) instead
of promising that nothing will go. A Gmail draft being saved is not a send
and is never reported.

Ledger. perform() is the single way an automatic change is made. The change
and its automation_actions row are written in one transaction, so neither
can exist without the other. Each action type has a handler (HANDLERS) that
reads the fields it changes, applies the change, and undoes it only if
nothing changed those fields since (a compare-and-swap). An undo that finds
them changed marks the action superseded and refuses, naming what changed.
Undoing or rejecting 2 of a feature's last 5 actions turns it off (the
circuit breaker) and leaves the student a notice.

Every ledger transaction writes before it reads. Python's sqlite3 opens a
transaction only at the first write, so a read that came first would run on
its own, and a pause or a student's edit could land between it and the
change. perform() starts with pause_guard; approve, reject, and undo start by
claiming the action (_claim). On SQLite that first write takes the database's
write lock; on PostgreSQL the handlers' reads also lock the rows they read
(_for_update), so what was read is still true when the change is written.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol
from uuid import uuid4

from . import actions
from .database import is_unique_violation
from .schema import PAUSE_NEVER_CHANGED, utc_now
from .user_time import user_timezone

OFF_ON = ("off", "on")
OFF_SHADOW_ON = ("off", "shadow", "on")
GROUPS = ("outreach", "applications", "discovery", "notifications")
RISKS = ("internal", "external")
PAUSED_KEY = "automation_paused"
STATUSES = ("shadow", "pending", "proposed", "applied", "undone", "rejected", "superseded", "failed", "expired")
VERDICTS = ("right", "wrong")
NOTICE_LEVELS = ("info", "warning", "problem")
SHADOW_HOURS = 48
SHADOW_MIN_ROWS = 5
# Undoing or rejecting this many of a feature's last BREAKER_WINDOW actions turns it off.
BREAKER_LIMIT = 2
BREAKER_WINDOW = 5
# A send claim older than this was left by a request that has ended; it is not in flight.
IN_FLIGHT_CLAIM_AGE = timedelta(minutes=10)
# The breaker's notices are keyed "breaker:<feature>:<action id>"; Health reads them back.
BREAKER_NOTICE_PREFIX = "breaker:"
BREAKER_OFF_DAYS = 30
MAX_ERROR_LENGTH = 300
DEFAULT_GMAIL_TOKEN_DAYS = 7


class AutomationGateError(ValueError):
    """A shadow-capable feature has not earned being turned on yet; the message says what is missing."""


class AutomationPaused(ValueError):
    """The student paused automation, so an automatic action stops before anything leaves the app."""

    def __init__(self, message: str = "Automation is paused") -> None:
        super().__init__(message)


class Superseded(Exception):
    """A later change touched this action's fields; the message names which, in a plain sentence."""


class NotApplicable(ValueError):
    """Raised by a handler's apply when what the action was for no longer holds (the student acted first).

    perform() lets it through, and its transaction writes nothing: neither the
    change nor a ledger row. The caller skips that subject.
    """


# --- The registry ------------------------------------------------------------------------


@dataclass(frozen=True)
class Feature:
    key: str
    label: str
    description: str
    group: str
    risk: str
    modes: tuple[str, ...] = OFF_ON

    def __post_init__(self) -> None:
        if self.group not in GROUPS:
            raise ValueError(f"Unknown automation group: {self.group}")
        if self.risk not in RISKS:
            raise ValueError(f"Unknown automation risk: {self.risk}")
        if self.modes not in (OFF_ON, OFF_SHADOW_ON):
            raise ValueError(f"Automation modes must be {OFF_ON} or {OFF_SHADOW_ON}")

    @property
    def shadow_capable(self) -> bool:
        return "shadow" in self.modes


# The descriptions of the five outreach switches are the words
# outreach_automation.SETTINGS has always used; app.js shows the longer help.
FEATURES: dict[str, Feature] = {
    feature.key: feature
    for feature in (
        Feature("auto_drafts", "Write drafts automatically",
                "Write a draft for every company with a contact and a location", "outreach", "internal"),
        Feature("bounce_recovery", "Find a new contact after a bounce",
                "After a bounce, find another contact and fix the greeting", "outreach", "internal"),
        # The student approved and scheduled every email this sends, so it has no shadow.
        Feature("scheduled_sending", "Send on their weekday morning",
                "Send approved emails on the recipient's next weekday morning", "outreach", "external"),
        Feature("follow_up_review", "Have a second model check each follow-up",
                "Have a second model check each follow-up before it goes out", "outreach", "internal"),
        # No shadow either: it pre-dates shadow, sends only a first message the student approved,
        # and keeping it off/on leaves unchanged a switch the student already uses.
        Feature("form_submission", "Send through contact forms",
                "Send approved first messages through the company's contact form when it has no email", "outreach", "external"),
        Feature("jev_inbox_suggestions", "Jev inbox suggestions",
                "Suggest what a pasted reply or an application email means with Jev, which sends the message text to TypeSafe. "
                "You still confirm each change", "applications", "internal"),
        Feature("desktop_notifications", "Show automation notices as desktop pop-ups",
                "Show each automation notice as a pop-up on this computer, without any email text or links", "notifications", "internal"),
        # Phase 2: changes that stay inside the app, each with an Undo (resume_variants.py,
        # internal_automation.py, auto_triage.py).
        Feature("outreach_auto_close", "Close companies that never answered",
                "Mark a company No response once 14 days have passed since its follow-up with no reply, after checking "
                "Gmail once more. A company Gmail can't search for, such as one you messaged on LinkedIn, is left for "
                "you. You can undo it, and a later reply reopens it", "outreach", "internal"),
        Feature("auto_follow_up_drafts", "Write follow-up drafts when they are due",
                "When a company's follow-up date arrives and nobody has replied, write the follow-up draft. "
                "It waits for your approval; nothing is sent", "outreach", "internal"),
        Feature("resume_variant_pick", "Pick the résumé variant for each saved role",
                "When you save a role, choose which of your résumé variants fits it, from the words you listed for each "
                "variant in your profile. It picks only for roles you save while it is on. The role shows the pick "
                "and you can change it", "applications", "internal"),
        Feature("application_silence", "Flag applications with no reply",
                "Show an Urgent row when an application is still at Applied a set number of days after you applied "
                "(21 days, unless your profile sets another number)", "applications", "internal"),
        Feature("archive_silent_applications", "Archive applications that never answered",
                "Move an application still at Applied to Archived after a set number of days "
                "(60 days, unless your profile sets another number). You can undo it", "applications", "internal"),
        Feature("auto_save", "Save new roles that score high",
                "After each daily sync, save new roles that score at or above the number in your profile "
                "(automation.auto_save_at). Scores are kept only for this computer's main account, so it works only there",
                "discovery", "internal"),
        Feature("auto_pass", "Pass on new roles that score low",
                "After each daily sync, pass on new roles that have a description and score below the number in your "
                "profile (automation.auto_pass_below). Review them under Auto-passed this week. Scores are kept only for "
                "this computer's main account, so it works only there", "discovery", "internal"),
    )
}


def _triage_requirement(key: str) -> Callable[[Any, str], str]:
    def check(conn: Any, user_id: str) -> str:
        from .auto_triage import requirement  # imported here: auto_triage imports this module

        return requirement(conn, user_id, key)

    return check


def _resume_variant_requirement(conn: Any, user_id: str) -> str:
    from .resume_variants import setup_requirement  # imported here: resume_variants imports this module

    return setup_requirement(conn, user_id)


# What a feature needs before it can act, beyond its switch: a function that
# returns "" when the need is met, or a plain sentence saying what is missing.
# A feature whose need is not met cannot be turned on (can_turn_on), and the
# Automation panel shows why.
REQUIREMENTS: dict[str, Callable[[Any, str], str]] = {
    "auto_save": _triage_requirement("auto_save"),
    "auto_pass": _triage_requirement("auto_pass"),
    "resume_variant_pick": _resume_variant_requirement,
}


def requirement(conn: sqlite3.Connection, user_id: str, key: str) -> str:
    """What ``key`` still needs before it can act, or "" when nothing."""
    check = REQUIREMENTS.get(key)
    return check(conn, user_id) if check is not None else ""


def register(feature: Feature) -> Feature:
    """Add a feature to the registry (later phases, and tests with a feature of their own)."""
    FEATURES[feature.key] = feature
    return feature


def _feature(key: str) -> Feature:
    try:
        return FEATURES[key]
    except KeyError:
        raise ValueError(f"Unknown automation feature: {key}") from None


# --- Settings ----------------------------------------------------------------------------


def _now(now: datetime | None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _stamp(now: datetime | None) -> str:
    return _now(now).isoformat(timespec="microseconds")


def _setting(conn: sqlite3.Connection, user_id: str, key: str) -> str | None:
    row = conn.execute("SELECT value FROM user_settings WHERE user_id=? AND key=?", (user_id, key)).fetchone()
    return None if row is None else str(row[0])


def _put_setting(conn: sqlite3.Connection, user_id: str, key: str, value: str, stamp: str) -> None:
    """Upsert one setting. Opens no transaction: the caller owns it."""
    conn.execute(
        """
        INSERT INTO user_settings(user_id, key, value, updated_at) VALUES(?, ?, ?, ?)
        ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (user_id, key, value, stamp),
    )


def modes(conn: sqlite3.Connection, user_id: str, keys: tuple[str, ...] | list[str] | None = None) -> dict[str, str]:
    """The mode of each feature in one read. A missing row, or a value the feature does not take, is off."""
    wanted = [_feature(key) for key in (keys if keys is not None else FEATURES)]
    if not wanted:
        return {}
    # Indexed rather than dict(rows): a PostgreSQL row is a mapping, and dict() would read its column names.
    rows = {row[0]: row[1] for row in conn.execute(
        f"SELECT key, value FROM user_settings WHERE user_id=? AND key IN ({', '.join('?' for _ in wanted)})",
        (user_id, *(feature.key for feature in wanted)),
    ).fetchall()}
    return {feature.key: rows[feature.key] if rows.get(feature.key) in feature.modes else "off" for feature in wanted}


def mode(conn: sqlite3.Connection, user_id: str, key: str) -> str:
    return modes(conn, user_id, [key])[key]


def paused(conn: sqlite3.Connection, user_id: str) -> bool:
    return _setting(conn, user_id, PAUSED_KEY) == "on"


def is_enabled(conn: sqlite3.Connection, user_id: str, key: str) -> bool:
    """Switched on and not paused: the feature may act."""
    return mode(conn, user_id, key) == "on" and not paused(conn, user_id)


def still_enabled(conn: sqlite3.Connection, user_id: str, key: str) -> bool:
    """is_enabled, asked inside the transaction of the write that finishes an automatic step.

    A step can run for minutes (a site search, a model call) after the worker
    first asked is_enabled. Asked here, the answer holds until the write
    commits (pause_guard), so a pause either lands first and stops the write,
    or lands after it.
    """
    return not pause_guard(conn, user_id) and mode(conn, user_id, key) == "on"


def on_since(conn: sqlite3.Connection, user_id: str, key: str) -> str | None:
    """When the switch was last turned on, while it is on; None when it is not on."""
    if mode(conn, user_id, key) != "on":
        return None
    return _setting(conn, user_id, f"{key}.on_since")


def is_shadow(conn: sqlite3.Connection, user_id: str, key: str) -> bool:
    """In shadow and not paused: the feature records what it would do."""
    return mode(conn, user_id, key) == "shadow" and not paused(conn, user_id)


def can_turn_on(conn: sqlite3.Connection, user_id: str, key: str, *, now: datetime | None = None) -> tuple[bool, str]:
    """Whether a feature may be turned on now, and if not, what it still needs.

    A feature with an unmet requirement (REQUIREMENTS) cannot be; a
    shadow-capable one must also have earned acting on its own in shadow.
    """
    feature = _feature(key)
    current = mode(conn, user_id, key)
    if current == "on":
        return True, ""
    missing = requirement(conn, user_id, key)
    if missing:
        return False, missing
    if not feature.shadow_capable:
        return True, ""
    since_text = _setting(conn, user_id, f"{key}.shadow_since")
    since = _parse(since_text)
    if current != "shadow" or since is None:
        return False, f"Run it in shadow first: it needs {SHADOW_HOURS} hours and {SHADOW_MIN_ROWS} reviewed actions there"
    row = conn.execute(
        """
        SELECT COUNT(*) AS total,
               COALESCE(SUM(CASE WHEN review='' THEN 1 ELSE 0 END), 0) AS unreviewed,
               COALESCE(SUM(CASE WHEN review='wrong' THEN 1 ELSE 0 END), 0) AS wrong
        FROM automation_actions WHERE user_id=? AND feature=? AND status='shadow' AND created_at>=?
        """,
        (user_id, key, since_text),
    ).fetchone()
    total, unreviewed, wrong = int(row["total"]), int(row["unreviewed"]), int(row["wrong"])
    if wrong:
        what = "one of its shadow actions" if wrong == 1 else f"{wrong} of its shadow actions"
        return False, f"You marked {what} wrong. Turn it off and back to shadow to start again"
    hours = int((_now(now) - since).total_seconds() // 3600)
    if hours < SHADOW_HOURS:
        return False, f"Needs {SHADOW_HOURS} hours in shadow first ({max(hours, 0)} hours so far)"
    if total < SHADOW_MIN_ROWS:
        return False, f"Needs at least {SHADOW_MIN_ROWS} actions in shadow first ({total} so far)"
    if unreviewed:
        return False, f"Review every shadow action first ({unreviewed} not reviewed yet)"
    return True, ""


def _plan_modes(
    conn: sqlite3.Connection, user_id: str, changes: dict[str, str], *, now: datetime | None = None,
) -> list[tuple[str, str, str]]:
    """Check every change, gates included, and return (key, value, current) for each. Writes nothing."""
    plans = []
    for key, value in changes.items():
        feature = _feature(key)
        if value not in feature.modes:
            raise ValueError(f"{feature.label} can be {' or '.join(feature.modes)}, not {value!r}")
        current = mode(conn, user_id, key)
        if value == "on" and current != "on":
            allowed, reason = can_turn_on(conn, user_id, key, now=now)
            if not allowed:
                raise AutomationGateError(reason)
        plans.append((key, value, current))
    return plans


def _write_modes(conn: sqlite3.Connection, user_id: str, plans: list[tuple[str, str, str]], stamp: str) -> None:
    """Write planned changes. Opens no transaction: the caller owns it."""
    for key, value, current in plans:
        _put_setting(conn, user_id, key, value, stamp)
        # The shadow clock starts when shadow starts, not on every save.
        if value == "shadow" and current != "shadow":
            _put_setting(conn, user_id, f"{key}.shadow_since", stamp, stamp)
        # Likewise when it was last turned on (auto_triage acts only on roles first seen since).
        if value == "on" and current != "on":
            _put_setting(conn, user_id, f"{key}.on_since", stamp, stamp)


def _set_modes(conn: sqlite3.Connection, user_id: str, changes: dict[str, str], *, now: datetime | None = None) -> None:
    """Validate every change, then write them all in one transaction (the legacy outreach switches use this)."""
    apply_settings(conn, user_id, modes=changes, paused=None, now=now)


def set_mode(conn: sqlite3.Connection, user_id: str, key: str, value: str, *, now: datetime | None = None) -> str:
    """Switch one feature. Turning a shadow-capable feature on raises AutomationGateError until can_turn_on allows it."""
    apply_settings(conn, user_id, modes={key: value}, paused=None, now=now)
    return mode(conn, user_id, key)


def apply_settings(
    conn: sqlite3.Connection, user_id: str, *, modes: dict[str, str] | None, paused: bool | None, now: datetime | None = None,
) -> dict[str, Any]:
    """Change the pause and any switches together: all of it, or none of it.

    Everything is checked (unknown features, modes a feature does not take,
    the shadow gates) inside the one transaction that then writes it all, so a
    refused switch never leaves a pause behind, and no other request can
    change what was checked before it is written: the transaction's first
    write (pause_guard) takes SQLite's write lock, and on PostgreSQL holds the
    pause row. Raises ValueError, or AutomationGateError for a gate.

    Returns the pause as it now stands and, when the pause was part of the
    request, what was already too far along to stop (in_flight, read after
    the commit, so a hand-over that won the race is in it); otherwise None.
    """
    changes = dict(modes or {})
    stamp = _stamp(now)
    with conn:
        pause_guard(conn, user_id)
        plans = _plan_modes(conn, user_id, changes, now=now)
        if paused is not None:
            _write_pause(conn, user_id, paused, utc_now())
        _write_modes(conn, user_id, plans, stamp)
    return {
        # ``paused`` is the request here, so the stored value is read directly.
        "paused": _setting(conn, user_id, PAUSED_KEY) == "on",
        "in_flight": in_flight(conn, user_id) if paused is not None else None,
    }


def pause_guard(conn: sqlite3.Connection, user_id: str) -> bool:
    """Inside the caller's transaction, whether automation is paused, held steady until that transaction ends.

    The row is made to exist first. On SQLite that insert also takes the write
    lock, so a pause cannot commit in between; on PostgreSQL the row is
    share-locked, which a pause (an UPDATE of it) waits for. Either the pause
    lands first and the caller sees it, or the caller's work lands first.
    A row made here was never paused, so it gets PAUSE_NEVER_CHANGED (see set_paused).
    """
    conn.execute(
        "INSERT INTO user_settings(user_id, key, value, updated_at) VALUES(?, ?, 'off', ?) ON CONFLICT(user_id, key) DO NOTHING",
        (user_id, PAUSED_KEY, PAUSE_NEVER_CHANGED),
    )
    lock = " FOR SHARE" if getattr(conn, "backend", "sqlite") == "postgresql" else ""
    row = conn.execute(f"SELECT value FROM user_settings WHERE user_id=? AND key=?{lock}", (user_id, PAUSED_KEY)).fetchone()
    return bool(row) and row[0] == "on"


def set_paused(conn: sqlite3.Connection, user_id: str, on: bool) -> dict[str, Any]:
    """Pause or resume everything automatic, and say what was already too far along to stop.

    The pause row's updated_at is when the pause last started or ended, and
    nothing else: outreach_schedule reads it to say a late send was held by a
    pause. So it moves only when the value flips, and a row made 'off' (here,
    by pause_guard, or by the 0037 seed) starts at PAUSE_NEVER_CHANGED.
    """
    with conn:
        _write_pause(conn, user_id, on, utc_now())
    # Read after the commit: a hand-over that won the race is visible now.
    return {"paused": bool(on), "in_flight": in_flight(conn, user_id)}


def _write_pause(conn: sqlite3.Connection, user_id: str, on: bool, now: str) -> None:
    """Set the pause row, moving its updated_at only when the value flips (see set_paused). Opens no transaction."""
    conn.execute(
        """
        INSERT INTO user_settings(user_id, key, value, updated_at) VALUES(?, ?, ?, ?)
        ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, updated_at=?
            WHERE user_settings.value <> excluded.value
        """,
        (user_id, PAUSED_KEY, "on" if on else "off", now if on else PAUSE_NEVER_CHANGED, now),
    )


# The claim state a contact form moves to as its send button is pressed
# (outreach_forms.submit_contact_form); from then on a pause cannot stop it.
FORM_HANDED_OVER = "clicking"


def in_flight(conn: sqlite3.Connection, user_id: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """What is past stopping: emails handed to Gmail, and contact forms whose button is being pressed.

    Each item's action is 'send' or 'form'. A Gmail draft being saved is not
    a send, and a form still being filled in can still be stopped by a pause
    (for an automatic one), so neither is listed.
    """
    items = []
    for row in conn.execute(
        """
        SELECT s.target_id, s.kind, s.label, s.updated_at, t.company
        FROM outreach_scheduled_sends s LEFT JOIN outreach_targets t ON t.id=s.target_id
        WHERE s.user_id=? AND s.state='transmitting' ORDER BY s.updated_at
        """,
        (user_id,),
    ).fetchall():
        items.append({
            "source": "scheduled_send", "target_id": row["target_id"], "company": row["company"] or "",
            "kind": row["kind"], "action": "send", "label": row["label"], "at": row["updated_at"],
        })
    listed = {(item["target_id"], item["kind"]) for item in items}
    cutoff = (_now(now) - IN_FLIGHT_CLAIM_AGE).isoformat(timespec="microseconds")
    # Only a Gmail send ('sending', action 'send') or a form being clicked: a
    # 'drafting' claim is a Gmail draft being saved, or a form still being filled in.
    for row in conn.execute(
        """
        SELECT c.target_id, c.kind, c.action, c.state, c.claimed_at, t.company
        FROM outreach_send_claims c LEFT JOIN outreach_targets t ON t.id=c.target_id
        WHERE c.user_id=? AND c.claimed_at>=?
          AND ((c.state='sending' AND c.action='send') OR c.state=?)
        ORDER BY c.claimed_at
        """,
        (user_id, cutoff, FORM_HANDED_OVER),
    ).fetchall():
        # A scheduled email being handed over holds a send claim too; it is one email, listed once.
        if (row["target_id"], row["kind"]) in listed:
            continue
        form = row["state"] == FORM_HANDED_OVER
        items.append({
            "source": "form_claim" if form else "send_claim", "target_id": row["target_id"], "company": row["company"] or "",
            "kind": row["kind"], "action": "form" if form else "send", "label": "", "at": row["claimed_at"],
        })
    return items


def unconfirmed(conn: sqlite3.Connection, user_id: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Sends and form submissions that may or may not have gone out, so the student must look.

    An 'unconfirmed' claim, and a form claim left mid-click longer than
    IN_FLIGHT_CLAIM_AGE (the app stopped while pressing the button). A first
    email is left out once the company is recorded as sent ("I sent it"), and
    a follow-up once the company has moved past Sent.
    """
    cutoff = (_now(now) - IN_FLIGHT_CLAIM_AGE).isoformat(timespec="microseconds")
    rows = conn.execute(
        """
        SELECT c.target_id, c.kind, c.action, c.claimed_at, t.company, t.sent_at, t.status
        FROM outreach_send_claims c LEFT JOIN outreach_targets t ON t.id=c.target_id
        WHERE c.user_id=? AND (c.state='unconfirmed' OR (c.state=? AND c.claimed_at<?))
        ORDER BY c.claimed_at
        """,
        (user_id, FORM_HANDED_OVER, cutoff),
    ).fetchall()
    items = []
    for row in rows:
        if row["kind"] == "initial" and row["sent_at"]:
            continue
        if row["kind"] == "follow_up" and row["status"] not in (None, "sent"):
            continue
        items.append({
            "target_id": row["target_id"], "company": row["company"] or "", "kind": row["kind"],
            "action": row["action"], "at": row["claimed_at"],
        })
    return items


def settings_payload(conn: sqlite3.Connection, user_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """The switches as the API shows them: each feature, its mode, and whether it can be turned on yet."""
    current = modes(conn, user_id)
    features = []
    for feature in FEATURES.values():
        allowed, reason = can_turn_on(conn, user_id, feature.key, now=now)
        features.append({
            "key": feature.key, "label": feature.label, "description": feature.description, "group": feature.group,
            "risk": feature.risk, "modes": list(feature.modes), "mode": current[feature.key],
            "shadow_since": _setting(conn, user_id, f"{feature.key}.shadow_since") if feature.shadow_capable else None,
            "can_turn_on": allowed, "can_turn_on_reason": reason,
            # What it still needs to act, whatever its mode: a switch left on can lose a requirement later.
            "requirement": requirement(conn, user_id, feature.key),
        })
    return {"paused": paused(conn, user_id), "features": features}


# --- Handlers ----------------------------------------------------------------------------


class Handler(Protocol):
    """How one action_type reads, makes, and takes back its change. None of these opens a transaction.

    ``read`` runs after the caller's transaction has made its first write, and
    on PostgreSQL locks what it reads (_for_update), so the values it returns
    still hold when ``apply`` or ``undo`` writes.

    A handler may also define ``effective(before, after, timestamp)``: what its
    fields will hold once ``after`` is applied, when that is not simply
    ``after`` (a rule decides a value). perform() compares it with ``before``
    to skip a change that is already in place, and records it as what undo
    must find.

    ``undo`` may return a dict of what else the student should know (such as
    ``undo_note``, a plain sentence); undo() adds it to its result.
    """

    fields: tuple[str, ...]

    def read(self, conn: sqlite3.Connection, user_id: str, subject_id: str) -> dict[str, Any]: ...

    def apply(
        self, conn: sqlite3.Connection, user_id: str, subject_id: str, after: dict[str, Any], *, source: str, timestamp: str,
    ) -> dict[str, Any]: ...

    def undo(
        self, conn: sqlite3.Connection, user_id: str, subject_id: str, before: dict[str, Any], after: dict[str, Any],
        *, source: str, timestamp: str,
    ) -> dict[str, Any] | None: ...


def _for_update(conn: sqlite3.Connection) -> str:
    """The row lock for a read that a later write in the same transaction relies on: PostgreSQL only.

    SQLite needs none: every ledger transaction writes first, and that holds
    the database's write lock until it ends.
    """
    return " FOR UPDATE" if getattr(conn, "backend", "sqlite") == "postgresql" else ""


def _same(conn: sqlite3.Connection, column: str) -> str:
    """A comparison that treats NULL as equal to NULL, in each backend's words."""
    if getattr(conn, "backend", "sqlite") == "postgresql":
        return f"{column} IS NOT DISTINCT FROM ?"
    return f"{column} IS ?"


FIELD_NAMES = {
    "stage": "the stage", "applied_at": "the applied date", "intent": "the saved or passed choice", "task": "the task",
    "status": "the status", "follow_up": "the follow-up draft", "resume_pick": "the résumé choice",
}


def _changed(expected: dict[str, Any], current: dict[str, Any]) -> str:
    """The fields that differ, in words: "the stage and the applied date"."""
    names = [FIELD_NAMES.get(key, key.replace("_", " ")) for key in expected if current.get(key) != expected[key]]
    names = names or ["the record"]
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _capitalized(text: str) -> str:
    return text[:1].upper() + text[1:]


class ApplicationStage:
    """application.stage: an application's stage, and when the student applied.

    Undo restores both, only while both are still what this action left.

    A move to a closed stage also cancels the application's follow-up
    reminder (actions._update_application_tx). apply records when it did, and
    undo schedules that reminder again, but only while it is still the one
    this move cancelled (unchanged since) and the follow-up date is still set
    and still ahead. Otherwise undo says the reminder was not restored, so the
    student is not left with a follow-up date that will never remind them.
    """

    fields = ("stage", "applied_at")

    def read(self, conn: sqlite3.Connection, user_id: str, subject_id: str) -> dict[str, Any]:
        row = conn.execute(
            f"SELECT stage, applied_at FROM applications WHERE id=? AND user_id=?{_for_update(conn)}", (subject_id, user_id),
        ).fetchone()
        if row is None:
            raise actions.ApplicationNotFoundError(subject_id)
        return {"stage": row["stage"], "applied_at": row["applied_at"]}

    def effective(self, before: dict[str, Any], after: dict[str, Any], timestamp: str) -> dict[str, Any]:
        """What the fields become, by the same rule _update_application_tx writes with.

        ``only_from`` makes the move conditional: when the stage is no longer
        that one (the student moved it on), nothing changes.
        """
        stage = after.get("stage")
        if stage not in actions.APPLICATION_STAGES:
            raise ValueError(f"Unsupported application stage: {stage}")
        if after.get("only_from") is not None and before["stage"] != after["only_from"]:
            return dict(before)
        return {"stage": stage, "applied_at": actions._next_applied_at(before["applied_at"], stage, after.get("applied_at"), timestamp)}

    @staticmethod
    def _reminder(conn: sqlite3.Connection, user_id: str, subject_id: str) -> Any:
        return conn.execute(
            "SELECT status, updated_at FROM reminders WHERE application_id=? AND user_id=? AND reminder_type='follow_up'",
            (subject_id, user_id),
        ).fetchone()

    def apply(
        self, conn: sqlite3.Connection, user_id: str, subject_id: str, after: dict[str, Any], *, source: str, timestamp: str,
    ) -> dict[str, Any]:
        reminder = self._reminder(conn, user_id, subject_id)
        actions._update_application_tx(
            conn, subject_id, stage=after["stage"], applied_at=after.get("applied_at"), user_id=user_id,
            source=source, timestamp=timestamp,
        )
        now = self._reminder(conn, user_id, subject_id)
        if reminder is not None and reminder["status"] == "scheduled" and now is not None and now["status"] == "cancelled":
            # What undo needs to put it back, and to know it is still the one this move cancelled.
            return {"reminder_cancelled_at": now["updated_at"]}
        return {}

    def undo(
        self, conn: sqlite3.Connection, user_id: str, subject_id: str, before: dict[str, Any], after: dict[str, Any],
        *, source: str, timestamp: str,
    ) -> dict[str, Any]:
        self._restore_stage(conn, user_id, subject_id, before, after, source=source, timestamp=timestamp)
        cancelled_at = (after.get("_result") or {}).get("reminder_cancelled_at")
        if not cancelled_at:
            return {}
        return self._restore_reminder(conn, user_id, subject_id, cancelled_at, timestamp)

    def _restore_reminder(
        self, conn: sqlite3.Connection, user_id: str, subject_id: str, cancelled_at: str, timestamp: str,
    ) -> dict[str, Any]:
        """Schedule again the follow-up reminder this move cancelled, or say why it stays cancelled."""
        row = conn.execute(
            f"SELECT follow_up_at FROM applications WHERE id=? AND user_id=?{_for_update(conn)}", (subject_id, user_id),
        ).fetchone()
        due = _parse(row["follow_up_at"]) if row is not None else None
        if due is None:
            return {"reminder_restored": False,
                    "undo_note": "The follow-up reminder this change cancelled was not restored, because no follow-up date is set."}
        if due <= _now(None):
            return {"reminder_restored": False,
                    "undo_note": "The follow-up reminder this change cancelled was not restored, because its date has passed. Set a new follow-up date if you want one."}
        # Due on the follow-up date as it stands now: the student may have moved it while the stage was closed.
        restored = conn.execute(
            """
            UPDATE reminders SET status='scheduled', due_at=?, updated_at=?
            WHERE application_id=? AND user_id=? AND reminder_type='follow_up' AND status='cancelled' AND updated_at=?
            """,
            (row["follow_up_at"], timestamp, subject_id, user_id, cancelled_at),
        ).rowcount
        if not restored:
            return {"reminder_restored": False,
                    "undo_note": "The follow-up reminder this change cancelled was changed since, so it was left as it is. Set the follow-up date again if you want a reminder."}
        return {"reminder_restored": True, "undo_note": "Your follow-up reminder is scheduled again."}

    def _restore_stage(
        self, conn: sqlite3.Connection, user_id: str, subject_id: str, before: dict[str, Any], after: dict[str, Any],
        *, source: str, timestamp: str,
    ) -> None:
        restored = conn.execute(
            f"UPDATE applications SET stage=?, applied_at=?, updated_at=? WHERE id=? AND user_id=? AND stage=? AND {_same(conn, 'applied_at')}",
            (before["stage"], before["applied_at"], timestamp, subject_id, user_id, after["stage"], after["applied_at"]),
        ).rowcount
        if not restored:
            try:
                current = self.read(conn, user_id, subject_id)
            except LookupError:
                raise Superseded("The application no longer exists, so there is nothing to undo") from None
            raise Superseded(f"{_capitalized(_changed({'stage': after['stage'], 'applied_at': after['applied_at']}, current))} "
                             "changed since, so it was left as it is")
        if before["stage"] != after["stage"]:
            conn.execute(
                """
                INSERT INTO application_events(application_id, event_type, from_stage, to_stage, detail_json, created_at)
                VALUES(?, 'stage_changed', ?, ?, ?, ?)
                """,
                (subject_id, after["stage"], before["stage"], json.dumps({"source": source}), timestamp),
            )
        else:
            conn.execute(
                """
                INSERT INTO application_events(application_id, event_type, from_stage, to_stage, detail_json, created_at)
                VALUES(?, 'application_updated', NULL, NULL, ?, ?)
                """,
                (subject_id, json.dumps({"source": source, "fields": ["applied_at"]}), timestamp),
            )


INTENTS = ("", "saved", "passed")


class OpportunityIntent:
    """opportunity.intent: whether the student saved or passed on an opportunity ('' for neither).

    Interactions are a history, so undo adds a row restoring the earlier
    choice, and only while the latest row is still the one this action added.

    On PostgreSQL the opportunity row is locked before the history is read:
    recording a save or pass checks that row's key (the foreign key) with a
    lock this one conflicts with, so a student's choice waits for this
    transaction instead of landing between the read and the write.
    """

    fields = ("intent",)

    def _lock(self, conn: sqlite3.Connection, subject_id: str) -> bool:
        return conn.execute(f"SELECT 1 FROM opportunities WHERE id=?{_for_update(conn)}", (subject_id,)).fetchone() is not None

    def read(self, conn: sqlite3.Connection, user_id: str, subject_id: str) -> dict[str, Any]:
        if not self._lock(conn, subject_id):
            raise actions.OpportunityNotFoundError(subject_id)
        return {"intent": actions._intent_state(conn, subject_id, user_id)}

    def effective(self, before: dict[str, Any], after: dict[str, Any], timestamp: str) -> dict[str, Any]:
        if after.get("intent") not in INTENTS:
            raise ValueError(f"Unsupported intent: {after.get('intent')!r}")
        return {"intent": after["intent"]}

    def apply(
        self, conn: sqlite3.Connection, user_id: str, subject_id: str, after: dict[str, Any], *, source: str, timestamp: str,
    ) -> dict[str, Any]:
        # ``only_if_untouched`` (auto_triage): act only on a role the student has never saved, passed,
        # or opened. Checked here, inside the write, so a choice they made meanwhile always stands.
        if after.get("only_if_untouched") and conn.execute(
            "SELECT 1 FROM opportunity_interactions WHERE opportunity_id=? AND user_id=? "
            "UNION ALL SELECT 1 FROM applications WHERE opportunity_id=? AND user_id=? LIMIT 1",
            (subject_id, user_id, subject_id, user_id),
        ).fetchone() is not None:
            raise NotApplicable("You already acted on this role, so it was left as it is")
        response = actions._record_intent_tx(
            conn, subject_id, after["intent"] or "undo", user_id=user_id, source=source, timestamp=timestamp,
        )
        if response["unchanged"]:
            return {}
        row = conn.execute(
            "SELECT id FROM opportunity_interactions WHERE opportunity_id=? AND user_id=? AND action=? AND created_at=?",
            (subject_id, user_id, after["intent"] or "undo", timestamp),
        ).fetchone()
        return {"interaction_id": int(row["id"])}

    def undo(
        self, conn: sqlite3.Connection, user_id: str, subject_id: str, before: dict[str, Any], after: dict[str, Any],
        *, source: str, timestamp: str,
    ) -> None:
        added = (after.get("_result") or {}).get("interaction_id")
        self._lock(conn, subject_id)
        latest = conn.execute(
            "SELECT id FROM opportunity_interactions WHERE opportunity_id=? AND user_id=? ORDER BY id DESC LIMIT 1",
            (subject_id, user_id),
        ).fetchone()
        if added is None or latest is None or int(latest["id"]) != int(added):
            raise Superseded("The saved or passed choice changed since, so it was left as it is")
        actions._record_intent_tx(conn, subject_id, before["intent"] or "undo", user_id=user_id, source=source, timestamp=timestamp)


class ApplicationTask:
    """application.task: a task added to an application (the subject).

    Undo deletes it only while it is still open with the title and due date it
    was created with; a task the student edited, finished, or deleted stays as they left it.
    """

    fields = ("task",)

    def read(self, conn: sqlite3.Connection, user_id: str, subject_id: str) -> dict[str, Any]:
        if conn.execute("SELECT 1 FROM applications WHERE id=? AND user_id=?", (subject_id, user_id)).fetchone() is None:
            raise actions.ApplicationNotFoundError(subject_id)
        # Each task is new; the idempotency key is what stops a second copy.
        return {"task": None}

    def effective(self, before: dict[str, Any], after: dict[str, Any], timestamp: str) -> dict[str, Any]:
        task = after.get("task")
        if not isinstance(task, dict) or not str(task.get("title") or "").strip():
            raise ValueError("Task title is required")
        return {"task": task}

    def apply(
        self, conn: sqlite3.Connection, user_id: str, subject_id: str, after: dict[str, Any], *, source: str, timestamp: str,
    ) -> dict[str, Any]:
        task = after["task"]
        due_at = user_timezone(conn, user_id).normalize_instant(task.get("due_at"), field="due_at")
        row = actions._add_application_task_tx(
            conn, subject_id, title=str(task["title"]), due_at=due_at, user_id=user_id,
            origin=str(task.get("origin") or "automation"), origin_ref=str(task.get("origin_ref") or ""),
            source=source, timestamp=timestamp,
        )
        return {"task_id": row["id"], "title": row["title"], "due_at": row["due_at"]}

    def undo(
        self, conn: sqlite3.Connection, user_id: str, subject_id: str, before: dict[str, Any], after: dict[str, Any],
        *, source: str, timestamp: str,
    ) -> None:
        created = after.get("_result") or {}
        deleted = conn.execute(
            f"DELETE FROM application_tasks WHERE id=? AND user_id=? AND application_id=? AND status='open' AND title=? AND {_same(conn, 'due_at')}",
            (created.get("task_id"), user_id, subject_id, created.get("title"), created.get("due_at")),
        ).rowcount
        if not deleted:
            row = conn.execute("SELECT status FROM application_tasks WHERE id=? AND user_id=?", (created.get("task_id"), user_id)).fetchone()
            if row is None:
                raise Superseded("The task was already deleted")
            if row["status"] != "open":
                raise Superseded("The task was marked done since, so it was left as it is")
            raise Superseded("The task was edited since, so it was left as it is")
        conn.execute(
            """
            INSERT INTO application_events(application_id, event_type, from_stage, to_stage, detail_json, created_at)
            VALUES(?, 'task_removed', NULL, NULL, ?, ?)
            """,
            (subject_id, json.dumps({"task_id": created.get("task_id"), "title": created.get("title"), "source": source}), timestamp),
        )


class OutreachStatus:
    """outreach.status: a cold-outreach company's status (outreach_auto_close closes one as no_response).

    The change runs through outreach._update_target_tx, so it has every side
    effect a status change made by hand has: a follow-up date that no longer
    applies is cleared, and the change is logged. Undo puts the status back only
    while it is still what this action left, and the follow-up date too, only
    while it is still what this action left.
    """

    fields = ("status",)

    def read(self, conn: sqlite3.Connection, user_id: str, subject_id: str) -> dict[str, Any]:
        row = conn.execute(
            f"SELECT status FROM outreach_targets WHERE id=? AND user_id=?{_for_update(conn)}", (subject_id, user_id),
        ).fetchone()
        if row is None:
            from .outreach import OutreachNotFoundError

            raise OutreachNotFoundError(subject_id)
        return {"status": row["status"]}

    def effective(self, before: dict[str, Any], after: dict[str, Any], timestamp: str) -> dict[str, Any]:
        from .outreach import OUTREACH_STATUSES

        if after.get("status") not in OUTREACH_STATUSES:
            raise ValueError(f"Unsupported outreach status: {after.get('status')!r}")
        # ``only_from``: change it only from that status, so a reply recorded meanwhile stands.
        if after.get("only_from") is not None and before["status"] != after["only_from"]:
            return dict(before)
        return {"status": after["status"]}

    def apply(
        self, conn: sqlite3.Connection, user_id: str, subject_id: str, after: dict[str, Any], *, source: str, timestamp: str,
    ) -> dict[str, Any]:
        from .outreach import _update_target_tx

        written = _update_target_tx(
            conn, subject_id, {"status": after["status"]}, user_id=user_id, status_detail="Changed automatically",
        )
        previous = written["previous"]
        return {
            "follow_up_at_before": previous.get("follow_up_at"),
            "follow_up_at_after": written["values"].get("follow_up_at", previous.get("follow_up_at")),
        }

    def undo(
        self, conn: sqlite3.Connection, user_id: str, subject_id: str, before: dict[str, Any], after: dict[str, Any],
        *, source: str, timestamp: str,
    ) -> dict[str, Any] | None:
        from .outreach import _log

        row = conn.execute(
            f"SELECT status, follow_up_at FROM outreach_targets WHERE id=? AND user_id=?{_for_update(conn)}", (subject_id, user_id),
        ).fetchone()
        if row is None:
            raise Superseded("The company is no longer in your outreach list, so there is nothing to undo")
        if row["status"] != after["status"]:
            raise Superseded("The status changed since, so it was left as it is")
        result = after.get("_result") or {}
        restore_date = row["follow_up_at"] == result.get("follow_up_at_after")
        follow_up_at = result.get("follow_up_at_before") if restore_date else row["follow_up_at"]
        conn.execute(
            f"UPDATE outreach_targets SET status=?, follow_up_at=?, updated_at=? WHERE id=? AND user_id=? AND status=? AND {_same(conn, 'follow_up_at')}",
            (before["status"], follow_up_at, timestamp, subject_id, user_id, after["status"], row["follow_up_at"]),
        )
        _log(conn, subject_id, user_id, "status", from_status=after["status"], to_status=before["status"], detail="Undone by you")
        if not restore_date and result.get("follow_up_at_before") != row["follow_up_at"]:
            return {"undo_note": "The follow-up date was changed since, so it was left as it is."}
        return None


class OutreachFollowUpDraft:
    """outreach.follow_up_draft: a follow-up draft auto_follow_up_drafts wrote for a company.

    ``after['draft']`` is what outreach_drafting.compose_draft wrote; apply stores
    it the way a draft written on request is stored (the history keeps it), and
    only while the company is still waiting on a follow-up, with none written.
    Undo discards the draft from the editor only while it is exactly as written
    (its fingerprint) and not approved; the draft stays in the history.
    """

    fields = ("follow_up",)

    @staticmethod
    def _row(conn: sqlite3.Connection, user_id: str, subject_id: str) -> Any:
        return conn.execute(
            "SELECT status, contact_email, contact_cc, follow_up_subject, follow_up_body, follow_up_claims_json, "
            f"follow_up_generated_by, follow_up_status FROM outreach_targets WHERE id=? AND user_id=?{_for_update(conn)}",
            (subject_id, user_id),
        ).fetchone()

    @staticmethod
    def _fingerprint(row: Any) -> str:
        from .outreach import _draft_fingerprint

        # The same inputs outreach._record fingerprints the follow-up with.
        return _draft_fingerprint(
            "follow_up", row["follow_up_subject"] or "", row["follow_up_body"] or "", row["contact_email"] or "",
            row["follow_up_claims_json"] or "[]", row["follow_up_generated_by"] or "", row["contact_cc"] or "",
        )

    def read(self, conn: sqlite3.Connection, user_id: str, subject_id: str) -> dict[str, Any]:
        row = self._row(conn, user_id, subject_id)
        if row is None:
            from .outreach import OutreachNotFoundError

            raise OutreachNotFoundError(subject_id)
        written = bool(str(row["follow_up_body"] or "").strip() or str(row["follow_up_subject"] or "").strip())
        return {"follow_up": self._fingerprint(row) if written else ""}

    def effective(self, before: dict[str, Any], after: dict[str, Any], timestamp: str) -> dict[str, Any]:
        draft = after.get("draft")
        if not isinstance(draft, dict) or draft.get("kind") != "follow_up" or not str(draft.get("body") or "").strip():
            raise ValueError("An automatic follow-up needs a written follow-up draft")
        if before["follow_up"]:
            # A follow-up is already written (the student's own, most likely): it stays.
            return dict(before)
        return {"follow_up": "generated"}

    def apply(
        self, conn: sqlite3.Connection, user_id: str, subject_id: str, after: dict[str, Any], *, source: str, timestamp: str,
    ) -> dict[str, Any]:
        from .outreach import get_target
        from .outreach_drafting import save_draft_tx

        target = get_target(conn, subject_id, user_id=user_id)
        if (
            target["status"] != "sent" or target["follow_up_body"] or target["reply_count"] or target["reply_suggestion"]
            or target["contact_bounced"] or target.get("bounced_at")
        ):
            raise NotApplicable("The company is no longer waiting on a follow-up, so no draft was saved")
        prepared = {**after["draft"], "target_status": target["status"], "target_draft_status": target["follow_up_status"]}
        version_id = save_draft_tx(conn, subject_id, user_id=user_id, prepared=prepared)
        return {"fingerprint": self._fingerprint(self._row(conn, user_id, subject_id)), "version_id": version_id}

    def undo(
        self, conn: sqlite3.Connection, user_id: str, subject_id: str, before: dict[str, Any], after: dict[str, Any],
        *, source: str, timestamp: str,
    ) -> dict[str, Any]:
        from .outreach import _log

        result = after.get("_result") or {}
        row = self._row(conn, user_id, subject_id)
        if row is None:
            raise Superseded("The company is no longer in your outreach list, so there is nothing to undo")
        if row["follow_up_status"] == "approved":
            raise Superseded("You approved the follow-up draft since, so it was left as it is")
        if not (row["follow_up_body"] or row["follow_up_subject"]):
            raise Superseded("The follow-up draft was already cleared")
        if row["follow_up_status"] != "generated" or self._fingerprint(row) != result.get("fingerprint"):
            raise Superseded("The follow-up draft or its recipient changed since, so it was left as it is")
        conn.execute(
            """
            UPDATE outreach_targets SET follow_up_subject='', follow_up_body='', follow_up_claims_json='[]',
                follow_up_generated_by='', follow_up_status='none', updated_at=?
            WHERE id=? AND user_id=? AND follow_up_status='generated'
            """,
            (timestamp, subject_id, user_id),
        )
        _log(conn, subject_id, user_id, "follow_up_discarded",
             detail="The automatic follow-up draft was undone. It stays in the follow-up history.")
        return {"undo_note": "The draft stays in the follow-up's history if you want it back."}


class ResumePick:
    """resume.pick: which résumé variant to use for a role (resume_variant_pick).

    A pick the student made (picked_by 'student') is never overwritten: the
    change counts as already in place. Undo clears the automatic pick, only
    while it is still the one this action made.
    """

    fields = ("resume_pick",)

    def read(self, conn: sqlite3.Connection, user_id: str, subject_id: str) -> dict[str, Any]:
        # No row lock on the role: a save's pick runs in the web request, and on
        # PostgreSQL FOR UPDATE here would wait for a running sync, which holds
        # every role's row until it commits. The insert in apply is what guards
        # against a student pick landing meanwhile.
        if conn.execute("SELECT 1 FROM opportunities WHERE id=?", (subject_id,)).fetchone() is None:
            raise actions.OpportunityNotFoundError(subject_id)
        row = conn.execute(
            f"SELECT resume_file_id, picked_by FROM opportunity_resume_picks WHERE user_id=? AND opportunity_id=?{_for_update(conn)}",
            (user_id, subject_id),
        ).fetchone()
        return {"resume_pick": None if row is None else {"resume_file_id": row["resume_file_id"], "picked_by": row["picked_by"]}}

    def effective(self, before: dict[str, Any], after: dict[str, Any], timestamp: str) -> dict[str, Any]:
        file_id = after.get("resume_file_id")
        if not isinstance(file_id, str) or not file_id:
            raise ValueError("A résumé pick needs a résumé")
        current = before.get("resume_pick")
        if current and current.get("picked_by") == "student":
            return dict(before)  # the student's choice sticks
        return {"resume_pick": {"resume_file_id": file_id, "picked_by": "automatic"}}

    def apply(
        self, conn: sqlite3.Connection, user_id: str, subject_id: str, after: dict[str, Any], *, source: str, timestamp: str,
    ) -> dict[str, Any]:
        if conn.execute(
            "SELECT 1 FROM resume_files WHERE id=? AND user_id=?", (after["resume_file_id"], user_id),
        ).fetchone() is None:
            raise NotApplicable("That résumé was deleted, so nothing was picked")
        written = conn.execute(
            """
            INSERT INTO opportunity_resume_picks(user_id, opportunity_id, resume_file_id, picked_by, matched_json, created_at, updated_at)
            VALUES(?, ?, ?, 'automatic', ?, ?, ?)
            ON CONFLICT(user_id, opportunity_id) DO UPDATE SET
                resume_file_id=excluded.resume_file_id, matched_json=excluded.matched_json, updated_at=excluded.updated_at
            WHERE opportunity_resume_picks.picked_by='automatic'
            """,
            (user_id, subject_id, after["resume_file_id"], _dumps(after.get("matched") or {}), timestamp, timestamp),
        ).rowcount
        if not written:
            # The student picked one for this role after it was read: theirs sticks, and no row claims a pick.
            raise NotApplicable("You picked a résumé for this role yourself, so it was left as it is")
        return {}

    def undo(
        self, conn: sqlite3.Connection, user_id: str, subject_id: str, before: dict[str, Any], after: dict[str, Any],
        *, source: str, timestamp: str,
    ) -> None:
        pick = after.get("resume_pick") or {}
        deleted = conn.execute(
            "DELETE FROM opportunity_resume_picks WHERE user_id=? AND opportunity_id=? AND resume_file_id=? AND picked_by='automatic'",
            (user_id, subject_id, pick.get("resume_file_id")),
        ).rowcount
        if not deleted:
            raise Superseded("The résumé choice changed since, so it was left as it is")


HANDLERS: dict[str, Handler] = {
    "application.stage": ApplicationStage(),
    "opportunity.intent": OpportunityIntent(),
    "application.task": ApplicationTask(),
    "outreach.status": OutreachStatus(),
    "outreach.follow_up_draft": OutreachFollowUpDraft(),
    "resume.pick": ResumePick(),
}


def _handler(action_type: str) -> Handler:
    try:
        return HANDLERS[action_type]
    except KeyError:
        raise ValueError(f"Unknown automation action type: {action_type}") from None


def _effective(handler: Handler, before: dict[str, Any], after: dict[str, Any], timestamp: str) -> dict[str, Any]:
    """What the handler's fields would hold once ``after`` is applied."""
    rule = getattr(handler, "effective", None)
    if rule is not None:
        return rule(before, after, timestamp)
    return {**before, **{key: value for key, value in after.items() if key in handler.fields}}


# --- The ledger --------------------------------------------------------------------------

_JSON_COLUMNS = {"fields_json": "fields", "before_json": "before", "after_json": "after", "evidence_json": "evidence"}


def _decode(row: Any) -> dict[str, Any]:
    item = dict(row)
    for column, key in _JSON_COLUMNS.items():
        item[key] = json.loads(item.pop(column) or ("[]" if column == "fields_json" else "{}"))
    return item


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _by_key(conn: sqlite3.Connection, user_id: str, idempotency_key: str) -> Any:
    return conn.execute(
        "SELECT * FROM automation_actions WHERE user_id=? AND idempotency_key=?", (user_id, idempotency_key),
    ).fetchone()


def _row(conn: sqlite3.Connection, action_id: str, user_id: str) -> Any:
    row = conn.execute("SELECT * FROM automation_actions WHERE id=? AND user_id=?", (action_id, user_id)).fetchone()
    if row is None:
        raise LookupError(action_id)
    return row


def _insert_action(conn: sqlite3.Connection, values: dict[str, Any]) -> None:
    """Write one ledger row. Its own function so a test can make it fail after the change was made."""
    columns = list(values)
    conn.execute(
        f"INSERT INTO automation_actions({', '.join(columns)}) VALUES({', '.join('?' for _ in columns)})",
        tuple(values[column] for column in columns),
    )


def _claim(conn: sqlite3.Connection, action_id: str, user_id: str, status: str, refusal: str, timestamp: str) -> Any:
    """Take hold of one action in the state a student's decision needs, as the transaction's first write.

    On SQLite this write takes the database's write lock before anything is
    read. On PostgreSQL it locks the row, so a second Approve, Reject, or Undo
    of the same action waits here and then finds it no longer in that state.
    Raises LookupError when there is no such action, and ValueError (refusal,
    then its current status) when it is in another state.
    """
    claimed = conn.execute(
        "UPDATE automation_actions SET decided_at=? WHERE id=? AND user_id=? AND status=?",
        (timestamp, action_id, user_id, status),
    ).rowcount
    row = _row(conn, action_id, user_id)
    if claimed != 1:
        raise ValueError(f"{refusal}; this one is {row['status']}")
    return row


def _settle(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...]) -> None:
    """Record the decision on a claimed action.

    Anything but exactly one row means it was decided somewhere else, and
    raising rolls back whatever this transaction changed along with it.
    """
    if conn.execute(sql, params).rowcount != 1:
        raise ValueError("This action was decided somewhere else at the same time, so nothing was changed")


def perform(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    feature: str,
    action_type: str,
    subject_kind: str,
    subject_id: str,
    after: dict[str, Any],
    evidence: dict[str, Any],
    summary: str,
    basis: str,
    confidence: float | None,
    idempotency_key: str,
    auto: bool,
    policy_version: str = "",
) -> dict[str, Any] | None:
    """Make, propose, or shadow one change, and record it, in one transaction.

    Returns the ledger row, or None when nothing was recorded: the feature is
    off, automation is paused, or the change is already in place. The same
    idempotency key returns the row it made the first time, whatever became
    of it. ``auto`` False proposes the change for the student to approve.
    """
    definition = _feature(feature)
    handler = _handler(action_type)
    if not idempotency_key:
        raise ValueError("An automatic action needs an idempotency key")
    # Made first, so the change itself can name the action that made it.
    action_id = f"auto-{uuid4().hex}"
    timestamp = utc_now()
    try:
        with conn:
            # Written first (see the module docstring): a pause either lands before this and is
            # seen here, or waits until the change and its row are in.
            is_paused = pause_guard(conn, user_id)
            existing = _by_key(conn, user_id, idempotency_key)
            if existing is not None:
                return _decode(existing)
            current_mode = mode(conn, user_id, definition.key)
            if current_mode == "off" or is_paused:
                return None
            current = handler.read(conn, user_id, subject_id)
            before = {name: current.get(name) for name in handler.fields}
            target = _effective(handler, before, after, timestamp)
            if target == before:
                return None
            recorded_after: dict[str, Any] = dict(after)
            applied_at = None
            decided_by = ""
            if not auto:
                status = "proposed"
            elif current_mode == "shadow":
                status = "shadow"
            else:
                result = handler.apply(conn, user_id, subject_id, after, source=f"automation:{action_id}", timestamp=timestamp)
                status, applied_at, decided_by = "applied", timestamp, "system"
                # What the fields now hold, so undo compares against exactly that.
                recorded_after = {**target, "_result": result or {}}
            _insert_action(conn, {
                "id": action_id, "user_id": user_id, "feature": definition.key, "action_type": action_type,
                "subject_kind": subject_kind, "subject_id": subject_id, "status": status,
                "fields_json": _dumps(list(handler.fields)), "before_json": _dumps(before),
                "after_json": _dumps(recorded_after), "evidence_json": _dumps(evidence or {}),
                "summary": summary, "basis": basis, "confidence": confidence, "policy_version": policy_version,
                "idempotency_key": idempotency_key, "created_at": timestamp, "applied_at": applied_at,
                "decided_by": decided_by,
            })
            return _decode(_row(conn, action_id, user_id))
    except Exception as exc:
        # Two passes raced on the same evidence: the other one's row stands.
        if not is_unique_violation(exc):
            raise
        existing = _by_key(conn, user_id, idempotency_key)
        if existing is None:
            raise
        return _decode(existing)


def approve(conn: sqlite3.Connection, action_id: str, user_id: str) -> dict[str, Any]:
    """Apply a proposed change the student approved, unless its fields changed since it was proposed."""
    timestamp = utc_now()
    superseded: Superseded | None = None
    with conn:
        action = _decode(_claim(conn, action_id, user_id, "proposed", "Only a proposed action can be approved", timestamp))
        handler = _handler(action["action_type"])
        try:
            current = handler.read(conn, user_id, action["subject_id"])
        except LookupError:
            current = None
        now_values = None if current is None else {name: current.get(name) for name in action["before"]}
        if now_values != action["before"]:
            what = "It no longer exists" if now_values is None else f"{_capitalized(_changed(action['before'], now_values))} changed"
            superseded = Superseded(f"{what} after this was proposed, so it was not applied")
            _settle(
                conn, "UPDATE automation_actions SET status='superseded', note=?, decided_at=? WHERE id=? AND user_id=? AND status='proposed'",
                (str(superseded), timestamp, action_id, user_id),
            )
        else:
            requested = {key: value for key, value in action["after"].items() if key != "_result"}
            target = _effective(handler, action["before"], requested, timestamp)
            result = handler.apply(conn, user_id, action["subject_id"], requested, source=f"automation:{action_id}", timestamp=timestamp)
            _settle(
                conn,
                """
                UPDATE automation_actions SET status='applied', after_json=?, applied_at=?, decided_at=?, decided_by='student'
                WHERE id=? AND user_id=? AND status='proposed'
                """,
                (_dumps({**target, "_result": result or {}}), timestamp, timestamp, action_id, user_id),
            )
    if superseded is not None:
        raise superseded
    return _decode(_row(conn, action_id, user_id))


def reject(conn: sqlite3.Connection, action_id: str, user_id: str) -> dict[str, Any]:
    """The student turned a proposal down.

    Returns the row, with feature_paused when that tripped the breaker and
    breaker_notice, the notice it left ({title, body}, or None).
    """
    timestamp = utc_now()
    with conn:
        row = _claim(conn, action_id, user_id, "proposed", "Only a proposed action can be rejected", timestamp)
        _settle(
            conn, "UPDATE automation_actions SET status='rejected', decided_at=?, decided_by='student' WHERE id=? AND user_id=? AND status='proposed'",
            (timestamp, action_id, user_id),
        )
        breaker = _trip_breaker(conn, user_id, row["feature"], action_id, timestamp)
    return {**_decode(_row(conn, action_id, user_id)), "feature_paused": breaker is not None, "breaker_notice": breaker}


def undo(conn: sqlite3.Connection, action_id: str, user_id: str) -> dict[str, Any]:
    """Take back an applied change, only while its fields still hold what it left.

    Raises Superseded, after recording the action as superseded, when a later
    change touched them; the message names what changed. Returns the row with
    feature_paused and breaker_notice (as reject does), plus whatever the
    handler adds, such as undo_note.
    """
    timestamp = utc_now()
    superseded: Superseded | None = None
    breaker: dict[str, str] | None = None
    extra: dict[str, Any] = {}
    with conn:
        action = _decode(_claim(conn, action_id, user_id, "applied", "Only an applied action can be undone", timestamp))
        handler = _handler(action["action_type"])
        try:
            extra = handler.undo(
                conn, user_id, action["subject_id"], action["before"], action["after"],
                source=f"automation-undo:{action_id}", timestamp=timestamp,
            ) or {}
        except Superseded as exc:
            superseded = exc
            _settle(
                conn, "UPDATE automation_actions SET status='superseded', note=?, decided_at=? WHERE id=? AND user_id=? AND status='applied'",
                (str(exc)[:500], timestamp, action_id, user_id),
            )
        else:
            _settle(
                conn, "UPDATE automation_actions SET status='undone', decided_at=?, decided_by='student' WHERE id=? AND user_id=? AND status='applied'",
                (timestamp, action_id, user_id),
            )
            breaker = _trip_breaker(conn, user_id, action["feature"], action_id, timestamp)
    if superseded is not None:
        raise superseded
    return {**_decode(_row(conn, action_id, user_id)), **extra, "feature_paused": breaker is not None, "breaker_notice": breaker}


def review(conn: sqlite3.Connection, action_id: str, user_id: str, verdict: str) -> dict[str, Any]:
    """The student's verdict on what a feature would have done in shadow. Never trips the breaker."""
    if verdict not in VERDICTS:
        raise ValueError("A shadow action is reviewed as right or wrong")
    with conn:
        row = _row(conn, action_id, user_id)
        if row["status"] != "shadow":
            raise ValueError(f"Only a shadow action can be reviewed; this one is {row['status']}")
        conn.execute(
            "UPDATE automation_actions SET review=?, reviewed_at=? WHERE id=? AND user_id=?",
            (verdict, utc_now(), action_id, user_id),
        )
    return _decode(_row(conn, action_id, user_id))


StatusFilter = str | list[str] | tuple[str, ...] | None


def parse_statuses(status: StatusFilter) -> list[str] | None:
    """The statuses asked for: one, several, or a comma-separated list ("applied,undone"). None for any.

    Raises ValueError naming an unknown status, or when nothing is left once blanks are dropped.
    """
    if status is None:
        return None
    given = [status] if isinstance(status, str) else list(status)
    wanted: list[str] = []
    for part in (piece.strip() for value in given for piece in str(value).split(",")):
        if not part:
            continue
        if part not in STATUSES:
            raise ValueError(f"Unknown automation status: {part}")
        if part not in wanted:
            wanted.append(part)
    if not wanted:
        raise ValueError("Name at least one automation status")
    return wanted


def _action_filter(user_id: str, status: StatusFilter, feature: str | None) -> tuple[str, list[Any]]:
    clauses, params = ["user_id=?"], [user_id]
    statuses = parse_statuses(status)
    if statuses is not None:
        clauses.append(f"status IN ({', '.join('?' for _ in statuses)})")
        params.extend(statuses)
    if feature is not None:
        clauses.append("feature=?")
        params.append(feature)
    return " AND ".join(clauses), params


def list_actions(
    conn: sqlite3.Connection, user_id: str, *, status: StatusFilter = None, feature: str | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    """Newest first. ``status`` takes one status or several (parse_statuses)."""
    where, params = _action_filter(user_id, status, feature)
    rows = conn.execute(
        f"SELECT * FROM automation_actions WHERE {where} ORDER BY created_at DESC, id DESC LIMIT ?",
        (*params, max(1, min(int(limit), 500))),
    ).fetchall()
    return [_decode(row) for row in rows]


def count_actions(conn: sqlite3.Connection, user_id: str, *, status: StatusFilter = None, feature: str | None = None) -> int:
    """How many actions list_actions would find with no limit."""
    where, params = _action_filter(user_id, status, feature)
    return int(conn.execute(f"SELECT COUNT(*) FROM automation_actions WHERE {where}", tuple(params)).fetchone()[0])


def _trip_breaker(conn: sqlite3.Connection, user_id: str, feature: str, action_id: str, timestamp: str) -> dict[str, str] | None:
    """After a student undo or reject: turn the feature off if they took back BREAKER_LIMIT of its last BREAKER_WINDOW actions.

    Runs inside the caller's transaction. Returns the notice it left
    ({title, body}) when it turned the feature off just now, else None.
    """
    definition = FEATURES.get(feature)
    if definition is None or mode(conn, user_id, feature) == "off":
        return None
    rows = conn.execute(
        """
        SELECT status, decided_by FROM automation_actions
        WHERE user_id=? AND feature=? AND status IN ('applied', 'undone', 'rejected', 'superseded')
        ORDER BY created_at DESC, id DESC LIMIT ?
        """,
        (user_id, feature, BREAKER_WINDOW),
    ).fetchall()
    taken_back = sum(1 for row in rows if row["status"] in {"undone", "rejected"} and row["decided_by"] == "student")
    if taken_back < BREAKER_LIMIT:
        return None
    _put_setting(conn, user_id, feature, "off", timestamp)
    notice = {
        "title": f"Turned off {definition.label}: you undid or rejected {taken_back} of its last {len(rows)} actions",
        "body": "Turn it back on under Automation when you want it again.",
    }
    _insert_notice(conn, user_id, event_key=f"{BREAKER_NOTICE_PREFIX}{feature}:{action_id}", level="warning",
                   title=notice["title"], body=notice["body"], timestamp=timestamp)
    return notice


# --- Notices -----------------------------------------------------------------------------


def _insert_notice(
    conn: sqlite3.Connection, user_id: str, *, event_key: str, level: str, title: str, body: str, timestamp: str,
) -> bool:
    if level not in NOTICE_LEVELS:
        raise ValueError(f"Unknown notice level: {level}")
    return bool(conn.execute(
        """
        INSERT INTO automation_notices(id, user_id, event_key, level, title, body, created_at)
        VALUES(?, ?, ?, ?, ?, ?, ?) ON CONFLICT(user_id, event_key) DO NOTHING
        """,
        (f"notice-{uuid4().hex}", user_id, event_key, level, title[:300], body[:1_000], timestamp),
    ).rowcount)


def notice(conn: sqlite3.Connection, user_id: str, *, event_key: str, level: str, title: str, body: str = "") -> bool:
    """Leave the student a notice, once per event_key. True when it is new.

    ``body`` is shown on the desktop too, so it never holds an email's text, a link, or an address.
    """
    with conn:
        return _insert_notice(conn, user_id, event_key=event_key, level=level, title=title, body=body, timestamp=utc_now())


def list_notices(conn: sqlite3.Connection, user_id: str, *, unread_only: bool = False, limit: int = 20) -> list[dict[str, Any]]:
    unread = " AND read_at IS NULL" if unread_only else ""
    return [dict(row) for row in conn.execute(
        f"SELECT * FROM automation_notices WHERE user_id=?{unread} ORDER BY created_at DESC, id DESC LIMIT ?",
        (user_id, max(1, min(int(limit), 200))),
    ).fetchall()]


def mark_notices_read(conn: sqlite3.Connection, user_id: str, ids: list[str] | None = None, *, all_unread: bool = False) -> int:
    """Mark these notices read, or with ``all_unread`` every unread one (not only those a page happened to show).

    Returns how many were unread and are now read.
    """
    if all_unread:
        with conn:
            return conn.execute(
                "UPDATE automation_notices SET read_at=? WHERE user_id=? AND read_at IS NULL", (utc_now(), user_id),
            ).rowcount
    ids = [str(notice_id) for notice_id in ids or []]
    if not ids:
        return 0
    with conn:
        return conn.execute(
            f"UPDATE automation_notices SET read_at=? WHERE user_id=? AND read_at IS NULL AND id IN ({', '.join('?' for _ in ids)})",
            (utc_now(), user_id, *ids),
        ).rowcount


# --- Health ------------------------------------------------------------------------------


def record_health(
    conn: sqlite3.Connection, user_id: str, component: str, *, ok: bool, error: str = "", detail: dict[str, Any] | None = None,
) -> None:
    """Record how a background step went. Opens its own transaction, so never call it inside one."""
    timestamp = utc_now()
    keep_detail = detail is None
    detail_json = _dumps(detail or {})
    if ok:
        values = (user_id, component, timestamp, None, "", detail_json, timestamp)
        update = "last_ok_at=excluded.last_ok_at"
    else:
        values = (user_id, component, None, timestamp, str(error)[:MAX_ERROR_LENGTH], detail_json, timestamp)
        update = "last_error_at=excluded.last_error_at, last_error=excluded.last_error"
    if not keep_detail:
        update += ", detail_json=excluded.detail_json"
    with conn:
        conn.execute(
            f"""
            INSERT INTO automation_health(user_id, component, last_ok_at, last_error_at, last_error, detail_json, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, component) DO UPDATE SET {update}, updated_at=excluded.updated_at
            """,
            values,
        )


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


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
    now = _now(now)
    row = conn.execute(
        "SELECT status, last_ok_at, last_error, token_granted_at, backoff_until FROM connector_accounts WHERE user_id=? AND provider='gmail_drafts'",
        (user_id,),
    ).fetchone()
    if row is None:
        return {"state": "not_connected", "last_ok_at": None, "last_error": "", "backoff_until": None,
                "token_granted_at": None, "likely_expires_at": None, "expiring_soon": False, "estimate_passed": False}
    backoff = _parse(row["backoff_until"])
    if row["status"] == "error":
        state = "needs_reconnect"
    elif row["status"] == "disconnected":
        state = "disconnected"
    elif backoff is not None and backoff > now:
        state = "throttled"
    else:
        state = "connected"
    granted = _parse(row["token_granted_at"])
    days = _token_days()
    expires = granted + timedelta(days=days) if granted is not None and days is not None else None
    estimate_passed = expires is not None and now >= expires
    if estimate_passed:
        last_ok = _parse(row["last_ok_at"])
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
    now = _now(now)
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
        local = zone.to_local(_parse(gmail["backoff_until"]))
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
        "banner": banner,
    }


PAUSED_BANNER = "Automation is paused. Nothing is sent and no switch acts on its own. Replies and bounces are still recorded."


def _counted(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def paused_text(flights: list[dict[str, Any]]) -> str:
    """The pause banner, and a second sentence for whatever was already too far along to stop."""
    emails = sum(1 for item in flights if item["action"] == "send")
    forms = sum(1 for item in flights if item["action"] == "form")
    parts = []
    if emails:
        parts.append(f"{_counted(emails, 'email')} {'was' if emails == 1 else 'were'} already handed to Gmail")
    if forms:
        parts.append(f"{_counted(forms, 'contact form')} {'was' if forms == 1 else 'were'} already being sent")
    if not parts:
        return PAUSED_BANNER
    stop = "can't be stopped" if len(parts) == 1 else "neither can be stopped"
    joined = parts[0] if len(parts) == 1 else f"{parts[0]} and {parts[1]}"
    return f"{PAUSED_BANNER} {joined[:1].upper()}{joined[1:]}{' and ' if len(parts) == 1 else ', and '}{stop}."


def breaker_off(conn: sqlite3.Connection, user_id: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Features the circuit breaker turned off in the last BREAKER_OFF_DAYS that are still off since.

    Read from the breaker's notices, so it holds after the student marks them
    read. A feature switched since the breaker wrote (turned back on, or set
    off again by the student) is no longer the breaker's doing, and is left out.
    """
    since = (_now(now) - timedelta(days=BREAKER_OFF_DAYS)).isoformat(timespec="microseconds")
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
