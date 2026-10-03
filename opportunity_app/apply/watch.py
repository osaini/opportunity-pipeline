"""Apply for me, the confirmation watch: does Greenhouse's email arrive, and what the application card says about it.

docs/phase5-apply-agent-spec.md, 6.16 (the watch), 5.6 (D12, when the watch is available), 8 and 8.8 (what is
counted per ATS) and 10.5 (the card). It opens no browser and reaches no network: ``watch`` is a database query over
what the Phase 1 mail reader (applications/inbox.py) already recorded in application_mail_messages. The
AutomationWorker passes it to apply_runs.run_worker_step on every pass (run_worker_step cannot import it, because
this module imports apply_runs).

What confirms. An email confirms an attempt only as a strong match: a confirmation the reader matched to this
application by the job id or by company and role, whose sender Gmail vouched for (``sender_verified``), received no
earlier than five minutes before the hand-over, and whose subject is not Greenhouse's security-code email. Anything
weaker (``company_single``, an unverified sender, an unmatched email from a Greenhouse sender that names the company)
only sets ``detail.possible_email_at``: it never confirms, never stops the clock and never counts in the statistics.
One email confirms one attempt (``detail.email_gmail_id``), the newest attempt first.

The clock. A submission is watched for 24 hours, and the 24 hours run only while the reader works. A stalled reader
(switch off, Gmail not connected or needing a reconnect, a pass that has not finished lately, a backlog, a recovery
search, an error, an email set aside unread, a Gmail account that is not the application's address) pauses the
watch; when it recovers, the stall is added to the deadline. ``no_email_24h`` is written only when a pass that began
after the deadline finished cleanly, so it always means "the reader looked and no email came". A watch whose reader
stays stalled for almost the whole 14 days, or whose extended deadline would pass the 13th day, becomes ``not_watched``
and never counts; so does one still waiting when its 14 days are over (``watch_stopped``: reader_stalled or window_ended).

Nothing here writes a field value, a subject, a Gmail id into a notice, or a link. Every write is one transaction
after apply_runs.lock_user that names the state it read, so the student's own answer meanwhile wins.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from pipeline_core.identity import normalized

from . import runs as apply_runs
from ..applications import actions, inbox as application_inbox, mail_rules
from ..automation import ledger as automation
from ..core.database import is_unique_violation, rollback_quietly
from ..core.json_values import json_as
from ..core.timestamps import parse_app_instant, utc_now
from ..integrations.gmail_client import connection_state
from ..mail.gmail_connection import connector_row
from ..student import preparation
from .claims import UNCONFIRMED_UNWRITTEN, claim_held
from .greenhouse import ATS_GREENHOUSE, is_greenhouse_sender

LOGGER = logging.getLogger(__name__)

# 6.16: an email counts when it was received no earlier than this long before the hand-over.
EMAIL_SLACK = timedelta(minutes=5)
# A reader whose last finished pass is older than this is stalled (passes are PASS_EVERY apart).
READER_STALE = 3 * application_inbox.PASS_EVERY
# A watch whose reader stays stalled this long after the submission gives up, so the 14 day window never ends "paused".
GIVE_UP_AFTER = timedelta(days=apply_runs.WATCH_DAYS - 1)
STRONG_TIERS = ("job_id", "company_title")
# 8.8: the statistics' "recent" window is the last this-many submissions whose watch finished.
RECENT_WINDOW = 10
ATS_NAMES = {ATS_GREENHOUSE: "Greenhouse"}
# The same strings runs._SETTLED_BY and the timeline words use.
WATCH_SOURCE_EMAIL = "apply_agent:confirmation_email"
WATCH_SOURCE_APP = "apply_agent:watch"
WATCH_SOURCE_STUDENT = "apply_agent:student_confirmed"
WATCH_COUNTS = (
    "email_confirmed", "resolved_by_email", "no_email_24h", "possible_email", "paused", "extended", "stopped", "held_back",
)

# Why the reader is not working, in the card's "Looking for its email: paused, {reason}".
READER_OFF = "“Update applications from job emails” is off"
READER_NOT_CONNECTED = "Gmail isn't connected"
READER_RECONNECT = "Gmail needs reconnecting"
READER_NOT_STARTED = "the job-email check hasn't started yet"
READER_RECOVERING = "the job-email check is catching up on missed mail"
READER_SLOW_DOWN = "Gmail asked the app to slow down"
READER_FAILING = "the job-email check couldn't reach Gmail"
READER_BEHIND = "the job-email check is still reading new mail"
READER_IDLE = "the job-email check hasn't run recently"
READER_SET_ASIDE = "the job-email check couldn't read some emails"
READER_OTHER_ADDRESS = "the email in your profile isn't the Gmail account the app reads"
READER_UNKNOWN_ADDRESS = "Gmail needs reconnecting once so the app knows which address it reads"

# D12 (5.6): what the student is told when the watch cannot run. The two requirements of the setting, and the address.
WATCH_NEEDS_SWITCH = "Turn on Update applications from job emails, in shadow is enough, so the app can look for each confirmation"
WATCH_NEEDS_ADDRESS = "Reconnect Gmail once so the app knows which address it reads"
WATCH_NEEDS_GMAIL = "Connect Gmail so the app can look for each confirmation"
WATCH_OTHER_ADDRESS = "The email in your profile isn't the Gmail account the app reads, so it can't look for each confirmation"

NO_EMAIL_BODY = "Some employers don't send one. If you want to be sure, check the employer's portal or your spam folder."


# --- Small helpers ------------------------------------------------------------------------


def _at(now: datetime | None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def stamp_now(now: datetime | None) -> str:
    """The stamp a write records: utc_now's unless the caller gave a time."""
    return utc_now() if now is None else _at(now).isoformat(timespec="microseconds")


def iso_utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _detail(row: Any) -> dict[str, Any]:
    return json_as(row["detail_json"], {})


# --- D12: can the watch run at all --------------------------------------------------------


def mailbox_reason(conn: sqlite3.Connection, user_id: str) -> str:
    """'' when the Gmail account the app reads is the email the application carries; else the sentence that says what is wrong.

    Database reads only: requirements run on every settings render.
    """
    row = connector_row(conn, user_id)
    if connection_state(row) != "connected":
        return WATCH_NEEDS_GMAIL
    account = str(row["account_email"] or "").strip()
    if not account:
        return WATCH_NEEDS_ADDRESS
    contact = preparation.confirmed_facts(conn, user_id).get("contact")
    email = contact.get("email") if isinstance(contact, dict) else None
    if not isinstance(email, str) or not email.strip() or account.casefold() != email.strip().casefold():
        return WATCH_OTHER_ADDRESS
    return ""


def watch_available(conn: sqlite3.Connection, user_id: str) -> str:
    """D12: '' when the confirmation watch can run for this student, else the first sentence that applies."""
    if automation.mode(conn, user_id, application_inbox.FEATURE) == "off":
        return WATCH_NEEDS_SWITCH
    return mailbox_reason(conn, user_id)


def watch_for(conn: sqlite3.Connection, user_id: str, mode: str) -> bool:
    """The ``watch`` argument for settle, record_result and resolve_uncertain (6.15, D12 C).

    One-click and unattended always watch (their preflight requires it); Finish in browser watches only when the
    watch is available, and is otherwise recorded as not watched.
    """
    return mode in ("one_click", "unattended") or not watch_available(conn, user_id)


# --- The reader's health ------------------------------------------------------------------


def reader_health(
    conn: sqlite3.Connection, user_id: str, now: datetime | None = None, *, since: datetime | None = None,
) -> tuple[str, datetime | None]:
    """(stall reason, when the reader last finished a pass). Reason '' means the mail reader is working now.

    ``since`` is the earliest time a watched email could have arrived: an email the reader set aside unread (state
    'error') at or after it stalls the watch, because the email that failed may be the confirmation. The Gmail account
    must also still be the application's address (D12): an account switched since the submission reads a mailbox where
    the confirmation never lands, so that is a stall too (the watch pauses; it never ends as "no email").
    """
    moment = _at(now)
    row = connector_row(conn, user_id)
    sync = conn.execute("SELECT * FROM application_mail_sync WHERE user_id=?", (user_id,)).fetchone()
    last_ok = parse_app_instant(sync["last_ok_at"]) if sync is not None else None
    if automation.mode(conn, user_id, application_inbox.FEATURE) == "off":
        return READER_OFF, last_ok
    state = connection_state(row)
    if state == "not_connected":
        return READER_NOT_CONNECTED, last_ok
    if state == "needs_reconnect":
        return READER_RECONNECT, last_ok
    problem = mailbox_reason(conn, user_id)
    if problem:
        return (READER_UNKNOWN_ADDRESS if problem == WATCH_NEEDS_ADDRESS else READER_OTHER_ADDRESS), last_ok
    if sync is None or not sync["history_id"]:
        return READER_NOT_STARTED, last_ok
    if sync["recovery_state"]:
        return READER_RECOVERING, last_ok
    error = str(sync["last_error"] or "")
    if error:
        lowered = error.casefold()
        if "reconnect" in lowered:
            return READER_RECONNECT, last_ok
        return (READER_SLOW_DOWN if "slow down" in lowered else READER_FAILING), last_ok
    if json_as(sync["pending_ids_json"], []):
        return READER_BEHIND, last_ok
    if since is not None:
        for aside in conn.execute("SELECT received_at FROM application_mail_messages WHERE user_id=? AND state='error'", (user_id,)).fetchall():
            received = parse_app_instant(aside["received_at"])
            if received is None or received >= since:
                return READER_SET_ASIDE, last_ok
    if last_ok is None or moment - last_ok > READER_STALE:
        return READER_IDLE, last_ok
    return "", last_ok


# --- The watch ----------------------------------------------------------------------------

_WATCHED = """
SELECT c.*, o.company AS company_name, o.title AS role_title, a.stage AS application_stage
FROM application_submit_claims c
JOIN opportunities o ON o.id = c.opportunity_id
LEFT JOIN applications a ON a.id = c.application_id
WHERE c.user_id = ? AND (
      (c.state = 'submitted' AND c.verification IN ('awaiting_email', 'no_email_24h') AND c.submitted_at >= ?)
   OR (c.handed_over_at >= ? AND (c.state IN ('unconfirmed', 'released')
                                  OR (c.state IN ('needs_you', 'failed') AND c.after_click = 1))))
ORDER BY c.handed_over_at DESC, c.created_at DESC
"""

_MAIL = """
SELECT gmail_id, application_id, matched_by, subject, sender_domain, sender_verified, received_at
FROM application_mail_messages
WHERE user_id = ? AND kind = 'application_confirmation' AND state IN ('done', 'awaiting_resume')
  AND (application_id = ? OR application_id = '')
ORDER BY received_at, gmail_id
"""


def _used_ids(conn: sqlite3.Connection, user_id: str) -> set[str]:
    """The Gmail ids that already confirmed an attempt of this student: one email confirms one attempt."""
    used: set[str] = set()
    for row in conn.execute("SELECT detail_json FROM application_submit_claims WHERE user_id=?", (user_id,)).fetchall():
        found = _detail(row).get("email_gmail_id")
        if isinstance(found, str) and found:
            used.add(found)
    return used


def _evidence(conn: sqlite3.Connection, user_id: str, claim: dict[str, Any], used: set[str]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(strong, weak): the earliest email that confirms this attempt, and the earliest that only might be for it."""
    handed = parse_app_instant(claim["handed_over_at"])
    if handed is None:
        return None, None
    # Phase 1 stores received_at to the second, so the floor is compared at that precision (AGENTS.md rule 15).
    floor = (handed - EMAIL_SLACK).replace(microsecond=0)
    tokens = set(str(claim["company_key"] or "").split())
    strong: dict[str, Any] | None = None
    weak: dict[str, Any] | None = None
    for row in conn.execute(_MAIL, (user_id, claim["application_id"])).fetchall():
        received = parse_app_instant(row["received_at"])
        subject = str(row["subject"] or "")
        if received is None or received < floor or row["gmail_id"] in used or mail_rules.SECURITY_CODE_SUBJECT.search(subject):
            continue
        item = {"gmail_id": row["gmail_id"], "received_at": received, "matched_by": row["matched_by"] or ""}
        if row["application_id"] == claim["application_id"]:
            if row["matched_by"] in STRONG_TIERS and row["sender_verified"] == 1:
                strong = strong or item
            elif row["matched_by"] == "company_single" or row["matched_by"] in STRONG_TIERS:
                weak = weak or item
        elif (row["application_id"] or "") == "" and tokens and is_greenhouse_sender(str(row["sender_domain"] or "")) \
                and tokens <= set(normalized(subject).split()):
            weak = weak or item
        if strong is not None:
            break
    return strong, weak


def watch(conn: sqlite3.Connection, user_id: str, now: datetime | None = None) -> dict[str, int]:
    """6.16 for one student: a database query only. Returns the changes it made, by kind (all zero when nothing changed).

    The kinds are WATCH_COUNTS: email_confirmed (a submitted claim confirmed), resolved_by_email (an uncertain attempt
    or a tombstone settled by its email), no_email_24h, possible_email (weak evidence noted), paused, extended
    (the deadline moved by a stall), stopped (a stalled watch given up) and held_back (an email for a tombstone whose
    job has a newer attempt: the student is asked to check).
    """
    moment = _at(now)
    counts = dict.fromkeys(WATCH_COUNTS, 0)
    since = iso_utc(moment - timedelta(days=apply_runs.WATCH_DAYS))
    counts["stopped"] += _end_window(conn, user_id, since, now)
    rows = [dict(row) for row in conn.execute(_WATCHED, (user_id, since, since)).fetchall()]
    if not rows:
        return counts
    used = _used_ids(conn, user_id)
    health: list[tuple[str, datetime | None]] = []
    for claim in rows:
        strong, weak = _evidence(conn, user_id, claim, used)
        if strong is not None:
            if claim["state"] == "submitted":
                changed = _confirm_submitted(conn, user_id, claim, strong, now)
                kind = "email_confirmed"
            else:
                changed = _resolve_by_email(conn, user_id, claim, strong, now, counts)
                kind = "resolved_by_email"
            if changed:
                counts[kind] += 1
                used.add(strong["gmail_id"])
            continue
        if weak is not None and claim["state"] != "released" and "possible_email_at" not in _detail(claim):
            if _merge(conn, user_id, claim, {"possible_email_at": iso_utc(weak["received_at"])}):
                counts["possible_email"] += 1
        if claim["state"] == "submitted" and claim["verification"] == "awaiting_email":
            if not health:
                handed = [parse_app_instant(item["handed_over_at"]) for item in rows if item["state"] == "submitted" and item["verification"] == "awaiting_email"]
                oldest = min((value for value in handed if value is not None), default=None)
                floor = None if oldest is None else (oldest - EMAIL_SLACK).replace(microsecond=0)
                health.append(reader_health(conn, user_id, moment, since=floor))
            _clock(conn, user_id, claim, health[0], moment, now, counts)
    return counts


def _stop_watch(conn: sqlite3.Connection, user_id: str, token: str, stamp: str, why: str, reason: str = "") -> bool:
    """End a watch that cannot finish: not_watched, so it never counts. False when the claim is no longer awaiting its email."""
    with conn:
        apply_runs.lock_user(conn, user_id)
        fresh = conn.execute(
            "SELECT detail_json FROM application_submit_claims WHERE token=? AND user_id=? AND state='submitted' "
            "AND verification='awaiting_email'", (token, user_id),
        ).fetchone()
        if fresh is None:
            return False
        kept = {key: value for key, value in _detail(fresh).items() if key not in ("watch_paused_since", "watch_paused")}
        kept["watch_stopped"] = why
        if reason:
            kept["watch_stopped_reason"] = reason
        return bool(conn.execute(
            "UPDATE application_submit_claims SET verification='not_watched', verified_at=?, updated_at=?, detail_json=? "
            "WHERE token=? AND user_id=? AND state='submitted' AND verification='awaiting_email'",
            (stamp, stamp, json.dumps(kept, sort_keys=True), token, user_id),
        ).rowcount)


def _end_window(conn: sqlite3.Connection, user_id: str, since: str, now: datetime | None) -> int:
    """A submission still awaiting its email once its 14 days are over is never visited again: end it as not watched."""
    stale = conn.execute(
        "SELECT token FROM application_submit_claims WHERE user_id=? AND state='submitted' AND verification='awaiting_email' "
        "AND submitted_at < ?", (user_id, since),
    ).fetchall()
    stamp = stamp_now(now)
    return sum(1 for row in stale if _stop_watch(conn, user_id, row["token"], stamp, "window_ended"))


def _merge(
    conn: sqlite3.Connection, user_id: str, claim: dict[str, Any], add: dict[str, Any], drop: tuple[str, ...] = (),
) -> bool:
    """Merge into this claim's detail while it is still in the state this pass read. No other column changes."""
    with conn:
        apply_runs.lock_user(conn, user_id)
        fresh = conn.execute(
            "SELECT state, verification, detail_json FROM application_submit_claims WHERE token=? AND user_id=?", (claim["token"], user_id),
        ).fetchone()
        if fresh is None or fresh["state"] != claim["state"] or fresh["verification"] != claim["verification"]:
            return False
        detail = {key: value for key, value in _detail(fresh).items() if key not in drop}
        detail.update(add)
        return bool(conn.execute(
            "UPDATE application_submit_claims SET detail_json=? WHERE token=? AND user_id=? AND state=? AND verification=?",
            (json.dumps(detail, sort_keys=True), claim["token"], user_id, claim["state"], claim["verification"]),
        ).rowcount)


def _email_detail(strong: dict[str, Any]) -> dict[str, Any]:
    return {
        "email_gmail_id": strong["gmail_id"], "email_received_at": iso_utc(strong["received_at"]), "email_matched_by": strong["matched_by"],
    }


def _confirm_submitted(
    conn: sqlite3.Connection, user_id: str, claim: dict[str, Any], strong: dict[str, Any], now: datetime | None,
) -> bool:
    """A strong match on a submitted claim: email_confirmed, a timeline event, and the stage write if it is still owed."""
    stamp = stamp_now(now)
    with conn:
        apply_runs.lock_user(conn, user_id)
        fresh = conn.execute(
            "SELECT state, verification, detail_json FROM application_submit_claims WHERE token=? AND user_id=?", (claim["token"], user_id),
        ).fetchone()
        if fresh is None or fresh["state"] != "submitted" or fresh["verification"] not in ("awaiting_email", "no_email_24h"):
            return False
        detail = {key: value for key, value in _detail(fresh).items() if key not in ("watch_paused_since", "watch_paused")}
        detail.update(_email_detail(strong))
        changed = conn.execute(
            "UPDATE application_submit_claims SET verification='email_confirmed', verified_at=?, updated_at=?, detail_json=? "
            "WHERE token=? AND user_id=? AND state='submitted' AND verification IN ('awaiting_email', 'no_email_24h')",
            (stamp, stamp, json.dumps(detail, sort_keys=True), claim["token"], user_id),
        ).rowcount
        if not changed:
            return False
        actions.log_application_event(
            conn, claim["application_id"], "apply_agent_verification", None, stamp,
            encoded=json.dumps({
                "run_id": claim["run_id"], "mode": claim["mode"], "verification": "email_confirmed",
                "received_at": iso_utc(strong["received_at"]), "matched_by": strong["matched_by"], "source": WATCH_SOURCE_EMAIL,
            }, sort_keys=True),
        )
    if claim["stage_policy"] in ("record", "ledger") and not claim["stage_recorded"]:
        apply_runs.record_stage(conn, claim["token"], user_id=user_id, now=now)
    return True


def _newer_live_attempts(conn: sqlite3.Connection, user_id: str, claim: dict[str, Any]) -> list[str]:
    """The states of the other attempts that hold the application or the Greenhouse job this tombstone would need again."""
    return [str(row[0]) for row in conn.execute(
        "SELECT state FROM application_submit_claims WHERE user_id=? AND token<>? AND state<>'released' "
        "AND (application_id=? OR (ats=? AND job_ref=?))",
        (user_id, claim["token"], claim["application_id"], claim["ats"], claim["job_ref"]),
    ).fetchall()]


def _held_back(conn: sqlite3.Connection, user_id: str, claim: dict[str, Any], strong: dict[str, Any], counts: dict[str, int]) -> bool:
    """A tombstone's email, with a newer live attempt in the way: say so once and ask the student to check."""
    if "email_after_release_at" in _detail(claim):
        return False
    if _merge(conn, user_id, claim, {"email_after_release_at": iso_utc(strong["received_at"])}):
        counts["held_back"] += 1
        company = claim["company_name"] or "the company"
        automation.notice(
            conn, user_id, event_key=f"apply-email-after-release:{claim['token']}", level="warning",
            title=f"Greenhouse confirmed an application to {company} by email, after an attempt was marked as not sent. Check it.",
        )
    return False


def _resolve_by_email(
    conn: sqlite3.Connection, user_id: str, claim: dict[str, Any], strong: dict[str, Any], now: datetime | None, counts: dict[str, int],
) -> bool:
    """A strong match on an uncertain attempt or a tombstone: it was sent. Forward-only stage write by stage_policy."""
    if claim["state"] == "released":
        newer = _newer_live_attempts(conn, user_id, claim)
        if {"claimed", "clicking"} & set(newer):
            # The retry is still being sent: the email is most likely its own, so wait for it to settle (the next pass).
            return False
        if newer:
            return _held_back(conn, user_id, claim, strong, counts)
    stamp = stamp_now(now)
    old_state = claim["state"]
    try:
        with conn:
            apply_runs.lock_user(conn, user_id)
            fresh = conn.execute(
                "SELECT state, detail_json FROM application_submit_claims WHERE token=? AND user_id=?", (claim["token"], user_id),
            ).fetchone()
            if fresh is None or fresh["state"] != old_state:
                return False  # the student's answer, or a newer pass, came first
            detail = {key: value for key, value in _detail(fresh).items() if key not in ("watch_paused_since", "watch_paused", "waiting")}
            detail.update(_email_detail(strong))
            changed = conn.execute(
                "UPDATE application_submit_claims SET state='submitted', verification='email_confirmed', resolved_by='email', "
                "submitted_at=?, verified_at=?, watch_until=NULL, note='', updated_at=?, detail_json=? "
                "WHERE token=? AND user_id=? AND state=?",
                (iso_utc(strong["received_at"]), stamp, stamp, json.dumps(detail, sort_keys=True), claim["token"], user_id, old_state),
            ).rowcount
            if not changed:
                return False
            actions.log_application_event(
                conn, claim["application_id"], "apply_agent_resolved", None, stamp,
                encoded=json.dumps({
                    "run_id": claim["run_id"], "mode": claim["mode"], "resolved_by": "email", "verification": "email_confirmed",
                    "received_at": iso_utc(strong["received_at"]), "matched_by": strong["matched_by"], "from_state": old_state,
                    "source": WATCH_SOURCE_EMAIL,
                }, sort_keys=True),
            )
    except Exception as exc:  # noqa: BLE001 - only a unique-index clash is expected; anything else is raised again
        if not is_unique_violation(exc):
            raise
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return _held_back(conn, user_id, claim, strong, counts)
    company = claim["company_name"] or "the company"
    automation.notice(
        conn, user_id, event_key=f"apply-email-confirmed:{claim['token']}", level="info",
        title=f"Greenhouse confirmed your application to {company} by email",
    )
    apply_runs.record_stage(conn, claim["token"], user_id=user_id, now=now)
    return True


def _clock(
    conn: sqlite3.Connection, user_id: str, claim: dict[str, Any], health: tuple[str, datetime | None], moment: datetime,
    now: datetime | None, counts: dict[str, int],
) -> None:
    """The 24 hour clock of a submitted, awaiting_email claim: pause, extend, give up, or end it with no_email_24h."""
    reason, last_ok = health
    detail = _detail(claim)
    until = parse_app_instant(claim["watch_until"])
    submitted = parse_app_instant(claim["submitted_at"])
    paused_since = parse_app_instant(detail.get("watch_paused_since"))
    stamp = stamp_now(now)
    if reason:
        if submitted is not None and moment - submitted >= GIVE_UP_AFTER:
            if _stop_watch(conn, user_id, claim["token"], stamp, "reader_stalled", reason):
                counts["stopped"] += 1
            return
        if paused_since is None:
            if _merge(conn, user_id, claim, {"watch_paused_since": stamp, "watch_paused": reason}):
                counts["paused"] += 1
        elif detail.get("watch_paused") != reason:
            _merge(conn, user_id, claim, {"watch_paused": reason})
        return
    if until is None:
        return
    if paused_since is not None:
        # The reader is back: the clock was stopped for the whole stall, so the deadline moves by it.
        extended = until + max(timedelta(0), moment - paused_since)
        if submitted is not None and extended - submitted >= GIVE_UP_AFTER:
            # The extended deadline would leave no time to look before the 14 days end, and the reader was not working for
            # the 24 hours: the watch is over, and never counts.
            if _stop_watch(conn, user_id, claim["token"], stamp, "reader_stalled", str(detail.get("watch_paused") or READER_IDLE)):
                counts["stopped"] += 1
            return
        with conn:
            apply_runs.lock_user(conn, user_id)
            fresh = conn.execute(
                "SELECT detail_json FROM application_submit_claims WHERE token=? AND user_id=? AND state='submitted' "
                "AND verification='awaiting_email'", (claim["token"], user_id),
            ).fetchone()
            if fresh is None:
                return
            kept = {key: value for key, value in _detail(fresh).items() if key not in ("watch_paused_since", "watch_paused")}
            if not conn.execute(
                "UPDATE application_submit_claims SET watch_until=?, detail_json=? WHERE token=? AND user_id=? "
                "AND state='submitted' AND verification='awaiting_email'",
                (iso_utc(extended), json.dumps(kept, sort_keys=True), claim["token"], user_id),
            ).rowcount:
                return
        counts["extended"] += 1
        until = extended
    if moment <= until or last_ok is None or last_ok < until:
        # Not due yet, or the reader is healthy but has not finished a pass that began after the deadline (at most one
        # pass away): wait. This is also what ends an extension loop.
        return
    with conn:
        apply_runs.lock_user(conn, user_id)
        changed = conn.execute(
            "UPDATE application_submit_claims SET verification='no_email_24h', verified_at=?, updated_at=? "
            "WHERE token=? AND user_id=? AND state='submitted' AND verification='awaiting_email'",
            (stamp, stamp, claim["token"], user_id),
        ).rowcount
        if not changed:
            return
        actions.log_application_event(
            conn, claim["application_id"], "apply_agent_verification", None, stamp,
            encoded=json.dumps({
                "run_id": claim["run_id"], "mode": claim["mode"], "verification": "no_email_24h", "watched_until": iso_utc(until),
                "source": WATCH_SOURCE_APP,
            }, sort_keys=True),
        )
    counts["no_email_24h"] += 1
    automation.notice(
        conn, user_id, event_key=f"apply-no-email:{claim['token']}", level="warning",
        title=f"No confirmation email yet for your application to {claim['role_title'] or 'this role'} at {claim['company_name'] or 'the company'}",
        body=NO_EMAIL_BODY,
    )


# --- What the application card shows (10.5) -----------------------------------------------

_CARD = """
SELECT c.*, a.stage AS application_stage, o.company AS company_name
FROM application_submit_claims c
JOIN applications a ON a.id = c.application_id
JOIN opportunities o ON o.id = c.opportunity_id
WHERE c.user_id = ? AND c.state <> 'released'
"""


def card_state(row: Any, now: datetime | None = None) -> dict[str, Any]:
    """One claim as its application card shows it. ``row`` carries application_stage and company_name besides the claim's columns."""
    detail = _detail(row)
    state, verification = row["state"], row["verification"]
    status = "stopped"
    can_resolve = False
    note = row["note"] or ""
    if state == "claimed":
        # Finish in browser spends its fill and the student's turn here (up to 20 minutes). A claim nobody holds any more is
        # a run that ended without settling: recover_stale finishes it, and until then it is a stopped attempt.
        if claim_held(row, now=now):
            status = "your_turn" if detail.get("waiting") == "student" else "filling"
        elif row["token"] in UNCONFIRMED_UNWRITTEN:
            # The runner decided the application may have been sent and could not write it (a busy database). Recovery will settle it
            # that way; until then the card must not say nothing was sent. Read without taking it: recovery takes it.
            status, note = "may_have_been_sent", UNCONFIRMED_UNWRITTEN.get(row["token"]) or note
    elif state == "clicking":
        if claim_held(row, now=now):
            # After the student's Submit Greenhouse may ask for the emailed code: the reader sets waiting once it has been asked about it
            # (and it stays, through a typed code, a fallback and an abandoned ask), and from then the window is the student's again.
            status = "security_code" if row["mode"] == "handoff" and detail.get("waiting") == "security_code" else "submitting"
        else:
            status, can_resolve = "may_have_been_sent", True
    elif state == "unconfirmed" or (state in ("needs_you", "failed") and row["after_click"]):
        status, can_resolve = "may_have_been_sent", True
    elif state == "submitted":
        if verification == "email_confirmed":
            status = "email_confirmed"
        elif verification == "no_email_24h":
            status = "no_email"
        elif verification == "not_watched":
            status = "not_watched"
        else:
            status = "watch_paused" if detail.get("watch_paused_since") else "watching"
    ats = row["ats"]
    return {
        "token": row["token"], "mode": row["mode"], "state": state, "verification": verification, "run_id": str(row["run_id"] or ""),
        "stage_policy": row["stage_policy"], "stage_recorded": bool(row["stage_recorded"]), "resolved_by": row["resolved_by"],
        "ats": ats, "ats_name": ATS_NAMES.get(ats, ats.title() if isinstance(ats, str) else ""),
        "company": row["company_name"] or "", "application_stage": row["application_stage"], "note": note,
        "status": status, "submitted_at": row["submitted_at"], "watch_until": row["watch_until"],
        "email_received_at": detail.get("email_received_at") or (row["verified_at"] if verification == "email_confirmed" else None),
        "possible_email_at": detail.get("possible_email_at"),
        "paused_reason": detail.get("watch_paused") if status == "watch_paused" else None,
        "ask_mark_applied": bool(
            state == "submitted" and row["stage_policy"] == "ask" and not row["stage_recorded"] and row["application_stage"] == "applying"
        ),
        "can_resolve": can_resolve,
        # Whether the claim was ever handed over (the student pressed Submit in the window). A released claim that was not had its
        # release written by a later start, not by the student's own word, so no page says the student said anything about it.
        "handed_over": bool(row["handed_over_at"]),
    }


def card_states(conn: sqlite3.Connection, user_id: str, *, now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """application_id -> the Apply for me state its card shows, for the one live claim of each application."""
    return {str(row["application_id"]): card_state(row, now) for row in conn.execute(_CARD, (user_id,)).fetchall()}


def claim_card(conn: sqlite3.Connection, user_id: str, token: str, now: datetime | None = None) -> dict[str, Any] | None:
    """One claim of any state as its card shows it, with the run that made it in ``run_id`` (the run view embeds this), or None."""
    row = _card_row(conn, user_id, token)
    if row is None:
        return None
    return card_state(row, now)


def _card_row(conn: sqlite3.Connection, user_id: str, token: str) -> Any:
    return conn.execute(
        "SELECT c.*, a.stage AS application_stage, o.company AS company_name FROM application_submit_claims c "
        "JOIN applications a ON a.id = c.application_id JOIN opportunities o ON o.id = c.opportunity_id "
        "WHERE c.token=? AND c.user_id=?", (token, user_id),
    ).fetchone()


def resolve_by_student(
    conn: sqlite3.Connection, token: str, *, user_id: str, went_through: bool, now: datetime | None = None,
) -> dict[str, Any]:
    """The card's It went through / It didn't go through, for an attempt that may have reached Greenhouse.

    apply_runs.resolve_uncertain does the write, watching for the email when the mode needs it (or the watch is
    available). "It didn't go through" also leaves an apply_agent_resolved event; "It went through" already left
    apply_agent_submitted. Raises apply_runs.ClaimNotFoundError, apply_runs.ClaimHeldError or ValueError.
    """
    claim = apply_runs.get_claim(conn, token, user_id=user_id)
    if claim is None:
        raise apply_runs.ClaimNotFoundError(token)
    apply_runs.resolve_uncertain(
        conn, token, user_id=user_id, went_through=went_through, watch=watch_for(conn, user_id, claim["mode"]), now=now,
    )
    if not went_through:
        try:
            with conn:
                actions.log_application_event(
                    conn, claim["application_id"], "apply_agent_resolved", None, stamp_now(now),
                    encoded=json.dumps({
                        "run_id": claim["run_id"], "mode": claim["mode"], "resolved_by": "student", "went_through": False,
                        "from_state": claim["state"], "source": WATCH_SOURCE_STUDENT,
                    }, sort_keys=True),
                )
        except Exception:  # noqa: BLE001 - the answer is already recorded; a missing timeline line must not turn it into an error
            LOGGER.exception("The timeline event for a student's answer could not be written")
            rollback_quietly(conn, LOGGER, "the timeline event for a student answer")
    return card_state(_card_row(conn, user_id, token), now)


def mark_applied(conn: sqlite3.Connection, token: str, *, user_id: str, now: datetime | None = None) -> dict[str, Any]:
    """The card's Mark as applied?: the forward-only stage write of 6.15 for a claim whose policy is 'ask'.

    Raises apply_runs.ClaimNotFoundError, or ValueError("This attempt has nothing to mark") unless the claim is
    submitted, 'ask', and not yet recorded.
    """
    claim = apply_runs.get_claim(conn, token, user_id=user_id)
    if claim is None:
        raise apply_runs.ClaimNotFoundError(token)
    if claim["state"] != "submitted" or claim["stage_policy"] != "ask" or claim["stage_recorded"]:
        raise ValueError("This attempt has nothing to mark")
    apply_runs.record_stage(conn, token, user_id=user_id, source=WATCH_SOURCE_STUDENT, now=now)
    return card_state(_card_row(conn, user_id, token), now)


# --- Per-ATS statistics (8, 8.8) ----------------------------------------------------------


def ats_statistics(conn: sqlite3.Connection, user_id: str, ats: str = ATS_GREENHOUSE) -> dict[str, Any]:
    """What 8 and 8.8 count for one ATS. The threshold itself is M6's; stalled or unfinished watches never count."""
    rows = conn.execute(
        "SELECT state, verification, detail_json, handed_over_at FROM application_submit_claims "
        "WHERE user_id=? AND ats=? AND handed_over_at IS NOT NULL ORDER BY handed_over_at DESC",
        (user_id, ats),
    ).fetchall()
    name = ATS_NAMES.get(ats, ats.title())
    total = {"handed_over": len(rows), "submitted": 0, "email_confirmed": 0, "no_email_24h": 0, "watching": 0, "watch_paused": 0,
             "not_watched": 0, "security_code_prompts": 0, "security_code_typed": 0}
    recent = {"window": RECENT_WINDOW, "finished": 0, "no_email_24h": 0, "security_code_prompts": 0}
    for row in rows:
        detail = _detail(row)
        # The reader's record has its own key; detail.security_code is the boolean 6.14 settles a prompted claim with.
        code = detail.get("security_code_reader")
        code = code if isinstance(code, dict) else {}
        prompted = bool(code.get("prompted_at")) or detail.get("security_code") is True
        if prompted:
            total["security_code_prompts"] += 1
        if code.get("reader") == "typed":
            total["security_code_typed"] += 1
        if row["state"] != "submitted":
            continue
        total["submitted"] += 1
        verification = row["verification"]
        if verification == "email_confirmed":
            total["email_confirmed"] += 1
        elif verification == "no_email_24h":
            total["no_email_24h"] += 1
        elif verification == "not_watched":
            total["not_watched"] += 1
        elif verification == "awaiting_email":
            total["watch_paused" if detail.get("watch_paused_since") else "watching"] += 1
        # 8.8's recent window: only a watch that finished with the reader working counts, never a stalled or unwatched one.
        if verification in ("email_confirmed", "no_email_24h") and recent["finished"] < RECENT_WINDOW:
            recent["finished"] += 1
            recent["no_email_24h"] += verification == "no_email_24h"
            recent["security_code_prompts"] += prompted
    return {"ats": ats, "ats_name": name, **total, "recent": recent, "lines": _lines(name, total, recent)}


def _lines(name: str, total: dict[str, int], recent: dict[str, int]) -> list[str]:
    if not total["handed_over"]:
        return [f"No applications submitted with Apply for me on {name} yet."]
    still = total["watching"] + total["watch_paused"]
    lines = [
        f"{name}: {_plural(total['handed_over'], 'application', 'applications')} handed over, {total['submitted']} submitted.",
        f"Confirmation emails: {total['email_confirmed']} arrived, {total['no_email_24h']} didn't come within 24 hours, "
        f"{still} still being looked for.",
        f"Security codes: {name} asked for a code {_plural(total['security_code_prompts'], 'time', 'times')}; "
        f"the app typed it {_plural(total['security_code_typed'], 'time', 'times')}.",
    ]
    if recent["finished"]:
        lines.append(
            f"Of your last {_plural(recent['finished'], 'submission', 'submissions')} whose email watch finished, "
            f"{recent['no_email_24h']} got no confirmation email and {recent['security_code_prompts']} asked for a security code."
        )
    return lines
