"""Apply for me, the data layer: claims, the two locks, runs, limits, the rehearsal gate, recovery, retention.

This is the module every write about an application attempt goes through
(docs/phase5-apply-agent-spec.md, sections 4.6 and 5). It opens no browser and
reaches no network: the agent that fills a Greenhouse form is another module,
and only ever asks this one for a claim, a hand-over, a heartbeat and a result.

Claims. One row of application_submit_claims per submit or Finish in browser
attempt. Two partial unique indexes are the locks: one live attempt per
application, one live attempt per Greenhouse job (job_ref), so a second saved
copy of the same posting cannot be submitted too. Every transaction that writes
claims starts with lock_user, so two threads or two server processes take turns
for one student. The claim is taken before the browser opens; the hand-over is
one transaction just before the button is pressed (hand_over), and from then on
the attempt counts toward the limits whatever becomes of it.

An attempt that reached Greenhouse and did not end in a seen confirmation page
is uncertain ('unconfirmed', or 'needs_you' or 'failed' with after_click set).
Nothing here retries one: only the confirmation email or the student settles it
(resolve_uncertain). Nothing here writes a field value; notes, reasons and
events say what happened in words.

Limits. limits_block reads every claim that was handed over, in any state,
released ones included. rehearsal_block and gate read apply_runs.

Recovery and retention. recover_stale finds what a stopped server left behind;
run_worker_step is the AutomationWorker's pass over it (a student who turned
Apply for me off still has their open claims finished); purge_evidence deletes
old screenshots and keeps their hashes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from time import monotonic
from typing import Any, Iterator
from uuid import uuid4

from pipeline_core.identity import normalized

from . import SERVER_INSTANCE, actions, automation
from .background import step_error
from .database import is_unique_violation
from .json_values import json_as
from .profile_store import read_stored_profile
from .settings_store import get_setting, put_setting, setting_updated_at
from .timestamps import parse_app_instant, utc_now
from .user_time import UserTimezone, user_timezone

LOGGER = logging.getLogger(__name__)

ATS_GREENHOUSE = "greenhouse"
# The adapter's version (docs/phase5-apply-agent-spec.md 4.4). A rehearsal counts toward the gate only for
# the version the adapter has now, so a change to its selectors or rules means rehearsing again.
ADAPTER_VERSION = "greenhouse-1"
MODES = ("one_click", "handoff", "unattended")
CLAIM_STATES = ("claimed", "clicking", "submitted", "unconfirmed", "needs_you", "failed", "released")
RUN_KINDS = ("lookup", "rehearsal", "submit", "handoff")
# What a screenshot of a filled form is kept for, in days (PIPELINE_APPLY_EVIDENCE_DAYS).
DEFAULT_EVIDENCE_DAYS = 90
# Screenshots live under APPLY_ROOT (opportunity_app/__init__.py): <user folder>/<opportunity id>/<run id>-<step>.png.
# The folder is per student, so deleting an account removes one folder. Not in output/: everything under data/ is ignored.
# A claim, or a run, that another server process holds is held while its heartbeat is this fresh.
HELD_HEARTBEAT = timedelta(minutes=2)
# The one confirm clock: how old the rehearsal a one-click submit confirmed may be at hand-over.
CONFIRM_MAX_AGE = timedelta(minutes=15)
# After a submission, how long the app looks for Greenhouse's confirmation email.
WATCH_WINDOW = timedelta(hours=24)
# How long an attempt that reached Greenhouse keeps being looked at (watched, offered for the student to settle).
WATCH_DAYS = 14
# A run's step-by-step progress is trimmed to its last step once the run is this old.
PROGRESS_KEEP_DAYS = 30
# The last time this student's evidence was purged, so the worker does it once a local day.
PURGE_LAST_RUN_KEY = "apply_purge.last_run"
GATE_RESET_KEY = "apply_gate_reset_at"
# The breaker: this many wrong marks among a student's last BREAKER_WINDOW reviewed runs on one ATS.
BREAKER_LIMIT = 2
BREAKER_WINDOW = 5
RUNNER_COMPONENT = "apply_agent.runner"
# The daily evidence purge's own health row, so a failed purge is not mixed with the runner's status.
RETENTION_COMPONENT = "apply_agent.retention"
# Greenhouse's own senders (data/application_senders.json), for a confirmation the reader could not match to a role.
GREENHOUSE_SENDER_DOMAINS = ("greenhouse.io", "greenhouse-mail.io")

# Per-student limits: defaults here, overridable in the profile under "apply_agent", read the way
# internal_automation.follow_up_days reads its day count. A value that is not an integer in range is the default.
DEFAULT_LIMITS = {
    "spacing_minutes": 10,
    "daily_cap": 5,
    "company_days": 30,
    "rehearsals_per_day": 20,
    "rehearsals_before_submit": 3,
    # Unattended mode only (not built yet): at most this many an hour and a day.
    "unattended_per_hour": 1,
    "unattended_daily_cap": 3,
}
LIMIT_MAXIMUM = {
    "spacing_minutes": 1440, "daily_cap": 50, "company_days": 365, "rehearsals_per_day": 200,
    "rehearsals_before_submit": 20, "unattended_per_hour": 10, "unattended_daily_cap": 50,
}

LIVE_APPLICATION = "This application is already being submitted, or was submitted."
STOPPED_EARLIER = "An earlier attempt stopped before anything was sent. Retry it."
# A claim that stopped for the student before the hand-over: nothing left the app, so it blocks nothing.
_STOPPED_UNSENT = "(c.state IN ('needs_you', 'failed') AND c.after_click=0)"
# The acknowledgments a student can tick (the "ask" refusals): each is a code a start request may carry.
ASK_COMPANY_LIMIT = "company_limit"
ASK_RELEASED_JOB = "released_job"
ASK_UNMATCHED_CONFIRMATION = "unmatched_confirmation"
ASK_APPLYING_OLD = "applying_old"

# Tokens of the claims whose runs are working in this process. A claim is added by claim() and leaves when its
# result is recorded (settle, record_result) or the runner calls forget(); a claim this process made that is not
# in here was left by a run that ended without settling it.
RUNNING: set[str] = set()
RUNNING_RUNS: set[str] = set()


class ClaimRefused(Exception):
    """An attempt that may not start. ``ask`` means a tick from the student (``code``) would allow it."""

    def __init__(self, message: str, *, code: str, ask: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.ask = ask


class ClaimHeldError(Exception):
    """The claim is being worked right now, so the student cannot settle it yet."""


class ClaimNotFoundError(LookupError):
    pass


# --- Small helpers ------------------------------------------------------------------------


def _at(now: datetime | None) -> datetime:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _stamp(now: datetime | None) -> str:
    """The stamp a write records: utc_now's (never repeated in this process) unless the caller gave a time."""
    return utc_now() if now is None else _at(now).isoformat(timespec="microseconds")


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def lock_user(conn: sqlite3.Connection, user_id: str) -> None:
    """Start a transaction that takes turns with every other writer of this student's claims (rule 0).

    On SQLite the write takes the database's write lock; on PostgreSQL it locks the student's users row.
    Either way threads and server processes queue here, so the checks that follow are still true at the insert.
    """
    conn.execute("UPDATE users SET id=id WHERE id=?", (user_id,))


def limits(conn: sqlite3.Connection, user_id: str) -> dict[str, int]:
    """This student's limits: the profile's apply_agent values where they are sound, else the defaults."""
    stored = read_stored_profile(conn, user_id).get("apply_agent")
    stored = stored if isinstance(stored, dict) else {}
    result = {}
    for key, default in DEFAULT_LIMITS.items():
        value = stored.get(key)
        ok = isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= LIMIT_MAXIMUM[key]
        result[key] = value if ok else default
    return result


def evidence_days() -> int:
    try:
        days = int(os.environ.get("PIPELINE_APPLY_EVIDENCE_DAYS", str(DEFAULT_EVIDENCE_DAYS)).strip())
    except ValueError:
        return DEFAULT_EVIDENCE_DAYS
    return days if days > 0 else DEFAULT_EVIDENCE_DAYS


def user_folder(user_id: str) -> str:
    """Where this student's screenshots live under APPLY_ROOT: 16 hex characters of the id's SHA-256."""
    return hashlib.sha256(user_id.encode()).hexdigest()[:16]


def _local_day_start(zone: UserTimezone, now: datetime) -> str:
    """The start of today in the student's timezone, as the UTC stamp claims and runs are compared with."""
    return _iso(zone.localize(datetime.combine(zone.today(now), time.min)))


def _when_text(zone: UserTimezone, moment: datetime, now: datetime) -> str:
    local = zone.to_local(moment)
    clock = f"{local:%I:%M %p}".lstrip("0")
    if local.date() == zone.today(now):
        return clock
    return f"{local:%a, %b} {local.day} at {clock}"


def _day_text(zone: UserTimezone, value: Any) -> str:
    moment = parse_app_instant(value)
    if moment is None:
        return "an earlier day"
    local = zone.to_local(moment)
    return f"{local:%B} {local.day}"


def _title_of(conn: sqlite3.Connection, opportunity_id: str) -> tuple[str, str]:
    row = conn.execute("SELECT title, company FROM opportunities WHERE id=?", (opportunity_id,)).fetchone()
    return (row["title"] or "", row["company"] or "") if row else ("", "")


# --- Which claims are held ----------------------------------------------------------------


def claim_held(row: Any, *, now: datetime | None = None) -> bool:
    """Whether a run may still be working under this claim (any row with token, instance and heartbeat_at).

    One this process made is held while its token is in RUNNING: when it is not, the run ended without settling
    it. One another server process made is held while its heartbeat is fresh, so a claim that process left when it
    died is recovered, and one it is still working is not.
    """
    if row["instance"] == SERVER_INSTANCE:
        return row["token"] in RUNNING
    beat = parse_app_instant(row["heartbeat_at"])
    return beat is not None and _at(now) - beat < HELD_HEARTBEAT


def forget(token: str) -> None:
    """The run under this claim has ended in this process (the runner's finally)."""
    RUNNING.discard(token)


@contextmanager
def running_run(run_id: str) -> Iterator[None]:
    """Mark a run as working in this process, so recover_stale does not call it orphaned."""
    RUNNING_RUNS.add(run_id)
    try:
        yield
    finally:
        RUNNING_RUNS.discard(run_id)


# --- Limits -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Block:
    """Why an attempt may not start. kind 'failed' cannot be overridden; 'ask' can, with the tick named by code."""

    kind: str
    code: str
    message: str


def _limit_check(
    conn: sqlite3.Connection, user_id: str, company: str, board_token: str, mode: str, now: datetime,
) -> Block | None:
    """9.1: the first limit that blocks a hand-over now, or None. Every handed-over claim counts, released ones too."""
    zone = user_timezone(conn, user_id)
    values = limits(conn, user_id)
    latest = conn.execute(
        "SELECT MAX(handed_over_at) FROM application_submit_claims WHERE user_id=? AND handed_over_at IS NOT NULL", (user_id,),
    ).fetchone()
    last = parse_app_instant(latest[0]) if latest else None
    spacing = timedelta(minutes=values["spacing_minutes"])
    if last is not None and now - last < spacing:
        return Block("failed", "spacing", f"The next agent submission is allowed at {_when_text(zone, last + spacing, now)}")
    if mode in ("one_click", "unattended"):
        day_start = _local_day_start(zone, now)
        today = conn.execute(
            "SELECT COUNT(*) FROM application_submit_claims WHERE user_id=? AND handed_over_at>=? AND mode IN ('one_click', 'unattended')",
            (user_id, day_start),
        ).fetchone()[0]
        if today >= values["daily_cap"]:
            return Block("failed", "daily_cap", f"You have reached today's limit of {values['daily_cap']} agent submissions. It starts over tomorrow")
    if mode == "unattended":
        hour = conn.execute(
            "SELECT COUNT(*) FROM application_submit_claims WHERE user_id=? AND mode='unattended' AND handed_over_at>=?",
            (user_id, _iso(now - timedelta(hours=1))),
        ).fetchone()[0]
        if hour >= values["unattended_per_hour"]:
            return Block("failed", "unattended_hour", "Unattended mode sends at most one application an hour")
        day = conn.execute(
            "SELECT COUNT(*) FROM application_submit_claims WHERE user_id=? AND mode='unattended' AND handed_over_at>=?",
            (user_id, _local_day_start(zone, now)),
        ).fetchone()[0]
        if day >= values["unattended_daily_cap"]:
            return Block("failed", "unattended_day", f"Unattended mode sends at most {values['unattended_daily_cap']} applications a day")
    # The same company under another spelling is still one company: the name's key or the Greenhouse board matches.
    recent = conn.execute(
        """
        SELECT c.handed_over_at, o.company FROM application_submit_claims c LEFT JOIN opportunities o ON o.id=c.opportunity_id
        WHERE c.user_id=? AND c.handed_over_at IS NOT NULL AND ((c.company_key<>'' AND c.company_key=?) OR (c.board_token<>'' AND c.board_token=?))
        ORDER BY c.handed_over_at DESC LIMIT 1
        """,
        (user_id, company, board_token),
    ).fetchone()
    if recent is not None:
        handed = parse_app_instant(recent["handed_over_at"])
        if handed is not None and now - handed < timedelta(days=values["company_days"]):
            days = max(0, (now - handed).days)
            name = recent["company"] or "this company"
            return Block("ask", ASK_COMPANY_LIMIT, f"You applied to {name} with Apply for me {days} days ago" if days != 1
                         else f"You applied to {name} with Apply for me 1 day ago")
    return None


def limit_check(
    conn: sqlite3.Connection, user_id: str, company: str, board_token: str, mode: str, now: datetime | None = None,
) -> Block | None:
    """The first limit that blocks a hand-over now, with its kind ('ask' can be ticked past, 'failed' cannot), or None."""
    return _limit_check(conn, user_id, company, board_token, mode, _at(now))


def limits_block(
    conn: sqlite3.Connection, user_id: str, company: str, board_token: str, mode: str, now: datetime | None = None,
) -> str | None:
    """The sentence for the first limit that stops a submit or Finish in browser now, or None (9.1).

    ``company`` is employer_key(name). Finish in browser (handoff) counts toward the spacing and the company limit
    but not the daily cap, because the student presses Submit.
    """
    block = _limit_check(conn, user_id, company, board_token, mode, _at(now))
    return None if block is None else block.message


def rehearsal_block(conn: sqlite3.Connection, user_id: str, now: datetime | None = None) -> str | None:
    """Whether today's rehearsals and option lookups (in the student's timezone) have reached their limit."""
    moment = _at(now)
    cap = limits(conn, user_id)["rehearsals_per_day"]
    used = conn.execute(
        "SELECT COUNT(*) FROM apply_runs WHERE user_id=? AND kind IN ('lookup', 'rehearsal') AND started_at>=?",
        (user_id, _local_day_start(user_timezone(conn, user_id), moment)),
    ).fetchone()[0]
    if used >= cap:
        return f"You have reached today's limit of {cap} rehearsals and option lookups. It starts over tomorrow"
    return None


# --- Duplicate checks (6.0 step 4) --------------------------------------------------------------


def duplicate_block(
    conn: sqlite3.Connection, user_id: str, *, opportunity_id: str, ats: str, job_ref: str, company: str,
    application_id: str | None = None, acknowledged: tuple[str, ...] | list[str] = (), now: datetime | None = None,
) -> Block | None:
    """The first reason this posting should not be submitted for the student, from what the app already holds.

    Reads only, and never creates the application (looking changes nothing). A 'failed' block cannot be
    overridden; an 'ask' one is skipped when its code is in ``acknowledged``.
    """
    moment = _at(now)
    zone = user_timezone(conn, user_id)
    application = conn.execute(
        "SELECT id, stage, created_at FROM applications WHERE opportunity_id=? AND user_id=?", (opportunity_id, user_id),
    ).fetchone()
    if application is not None:
        application_id = str(application["id"])
        if application["stage"] != "applying":
            return Block("failed", "stage", f"This application is already {application['stage']}")
        confirmation = conn.execute(
            "SELECT received_at FROM application_mail_messages WHERE user_id=? AND application_id=? AND kind='application_confirmation' "
            "ORDER BY received_at LIMIT 1", (user_id, application_id),
        ).fetchone()
        if confirmation is not None:
            return Block("failed", "confirmed_by_email",
                         f"Greenhouse already confirmed an application from you on {_day_text(zone, confirmation['received_at'])}")
        session = conn.execute(
            "SELECT updated_at FROM application_form_sessions WHERE user_id=? AND application_id=? AND status='completed' "
            "ORDER BY updated_at LIMIT 1", (user_id, application_id),
        ).fetchone()
        if session is not None:
            return Block("failed", "marked_submitted", f"You marked this application submitted on {_day_text(zone, session['updated_at'])}")
    # An attempt that is live, submitted, or may have reached Greenhouse blocks; one that sent nothing does not
    # (the next attempt releases it, rule 2).
    for row in conn.execute(
        """
        SELECT c.application_id, c.note, c.opportunity_id FROM application_submit_claims c
        WHERE c.user_id=? AND (c.application_id=? OR (c.ats=? AND c.job_ref=?))
          AND (c.state IN ('claimed', 'clicking', 'submitted', 'unconfirmed') OR (c.state IN ('needs_you', 'failed') AND c.after_click=1))
        ORDER BY c.created_at
        """,
        (user_id, application_id or "", ats, job_ref),
    ).fetchall():
        if application_id and row["application_id"] == application_id:
            return Block("failed", "application", row["note"] or LIVE_APPLICATION)
        return Block("failed", "job", _other_copy(conn, row["opportunity_id"]))
    released = conn.execute(
        "SELECT handed_over_at FROM application_submit_claims WHERE user_id=? AND ats=? AND job_ref=? AND state='released' "
        "AND handed_over_at IS NOT NULL ORDER BY handed_over_at DESC LIMIT 1", (user_id, ats, job_ref),
    ).fetchone()
    if released is not None and ASK_RELEASED_JOB not in acknowledged:
        return Block("ask", ASK_RELEASED_JOB, f"You said the attempt on {_day_text(zone, released['handed_over_at'])} didn't go through. "
                     "Greenhouse may still have it. Send it again anyway.")
    unmatched = _unmatched_confirmation(conn, user_id, opportunity_id, company)
    if unmatched is not None and ASK_UNMATCHED_CONFIRMATION not in acknowledged:
        name = _title_of(conn, opportunity_id)[1] or "this company"
        return Block("ask", ASK_UNMATCHED_CONFIRMATION,
                     f"An application confirmation from {name} arrived on {_day_text(zone, unmatched)} that the app couldn't match to a role. "
                     "I haven't applied to this role.")
    created = parse_app_instant(application["created_at"]) if application is not None else None
    if created is not None and moment - created > timedelta(days=1) and ASK_APPLYING_OLD not in acknowledged:
        return Block("ask", ASK_APPLYING_OLD, "Did you already apply to this by hand? I haven't applied yet.")
    return None


def _other_copy(conn: sqlite3.Connection, opportunity_id: str) -> str:
    title = _title_of(conn, opportunity_id)[0] or "this role"
    return f"This Greenhouse job already has an attempt from another saved copy of the role ({title}). Finish or release that one first."


def _unmatched_confirmation(conn: sqlite3.Connection, user_id: str, opportunity_id: str, company: str) -> str | None:
    """When a Greenhouse confirmation the mail reader could not match to a role arrived for this company, since the role was saved."""
    tokens = company.split()
    saved = conn.execute(
        "SELECT MAX(created_at) FROM opportunity_interactions WHERE user_id=? AND opportunity_id=? AND action='saved'",
        (user_id, opportunity_id),
    ).fetchone()
    if not tokens or not saved or not saved[0]:
        return None
    for row in conn.execute(
        "SELECT subject, sender_domain, received_at FROM application_mail_messages "
        "WHERE user_id=? AND kind='application_confirmation' AND application_id='' AND received_at>=? ORDER BY received_at",
        (user_id, saved[0]),
    ).fetchall():
        domain = str(row["sender_domain"] or "").lower()
        if not any(domain == known or domain.endswith(f".{known}") for known in GREENHOUSE_SENDER_DOMAINS):
            continue
        if set(tokens) <= set(normalized(str(row["subject"] or "")).split()):
            return str(row["received_at"])
    return None


# --- Claims -------------------------------------------------------------------------------


def stage_policy_for(mode: str, *, one_click_approved: bool = False) -> str:
    """5.2 rule 8, fixed when the claim is made so a later change cannot reinterpret it.

    'record': the stage moves to applied when the confirmation page is seen (one-click; Finish in browser too once
    the student has approved one-click, D1 A). 'ask': it never moves by itself; the card asks (Finish in browser
    while the student always presses Submit, D1 B). 'ledger': unattended, through automation.perform.
    """
    if mode == "unattended":
        return "ledger"
    if mode == "one_click" or one_click_approved:
        return "record"
    return "ask"


def claim(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    opportunity_id: str,
    mode: str,
    ats: str,
    board_token: str,
    job_ref: str,
    company: str,
    plan_hash: str = "",
    run_id: str = "",
    confirmed_at: str | None = None,
    rehearsal_run_id: str = "",
    stage_policy: str | None = None,
    acknowledged: tuple[str, ...] | list[str] = (),
    retry: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Take the claim for one attempt, before any browser opens (5.2 rules 0 to 2, 6.1). Raises ClaimRefused.

    In one transaction: the lock; the application, made at 'applying' if there is none; for a student-started claim
    (any mode but 'unattended') or a ``retry``, the release of every stopped attempt that sent nothing (5.2 rule 2:
    only the worker's own unattended attempt may not release one); the duplicate and limit checks (so a pause, a stage edit or a second
    server process either lands first and is seen, or waits); then the insert. A unique-index conflict, which the
    checks should already have caught, is refused with the same sentences. Nothing is created when it is refused.

    ``company`` is employer_key(name). ``acknowledged`` holds the codes of the "ask" refusals the student ticked
    (ASK_*); they are recorded on the claim. ``rehearsal_run_id`` is the rehearsal a one-click confirm approved.
    """
    if mode not in MODES:
        raise ValueError(f"Unsupported mode: {mode}")
    stamp = _stamp(now)
    moment = _at(now)
    token = uuid4().hex
    acknowledged = tuple(dict.fromkeys(acknowledged))
    # Held from before the insert, so no recovery pass can call the new claim stale in between.
    RUNNING.add(token)
    try:
        with conn:
            lock_user(conn, user_id)
            if mode == "unattended" and automation.pause_guard(conn, user_id):
                raise automation.AutomationPaused()
            application_id = actions.ensure_application_tx(
                conn, opportunity_id, user_id, event_type="apply_agent_started", detail={"mode": mode, "run_id": run_id},
                timestamp=stamp,
            )
            if retry or mode != "unattended":
                conn.execute(
                    "UPDATE application_submit_claims SET state='released', resolved_by='student', updated_at=? "
                    "WHERE user_id=? AND (application_id=? OR (ats=? AND job_ref=?)) AND after_click=0 AND state IN ('failed', 'needs_you')",
                    (stamp, user_id, application_id, ats, job_ref),
                )
            block = duplicate_block(
                conn, user_id, opportunity_id=opportunity_id, ats=ats, job_ref=job_ref, company=company,
                application_id=application_id, acknowledged=acknowledged, now=moment,
            )
            if block is None:
                block = _limit_check(conn, user_id, company, board_token, mode, moment)
                # The company tick is for a student-started attempt; unattended mode cannot tick past it (9.1).
                if block is not None and block.code == ASK_COMPANY_LIMIT and ASK_COMPANY_LIMIT in acknowledged and mode != "unattended":
                    block = None
            if block is not None:
                raise ClaimRefused(block.message, code=block.code, ask=block.kind == "ask")
            detail = {"acknowledged": list(acknowledged)} if acknowledged else {}
            if rehearsal_run_id:
                detail["rehearsal_run_id"] = rehearsal_run_id
            conn.execute(
                """
                INSERT INTO application_submit_claims(
                    token, application_id, user_id, opportunity_id, instance, mode, state, after_click, ats, board_token,
                    job_ref, company_key, stage_policy, plan_hash, run_id, confirmed_at, heartbeat_at, detail_json,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, 'claimed', 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    token, application_id, user_id, opportunity_id, SERVER_INSTANCE, mode, ats, board_token, job_ref, company,
                    stage_policy or stage_policy_for(mode), plan_hash, run_id, confirmed_at, stamp, _dumps(detail), stamp, stamp,
                ),
            )
    except ClaimRefused:
        RUNNING.discard(token)
        raise
    except Exception as exc:
        RUNNING.discard(token)
        if not is_unique_violation(exc):
            raise
        raise _conflict(conn, user_id, opportunity_id, ats, job_ref) from exc
    return {"token": token, "application_id": application_id, "state": "claimed", "stage_policy": stage_policy or stage_policy_for(mode)}


def _conflict(conn: sqlite3.Connection, user_id: str, opportunity_id: str, ats: str, job_ref: str) -> ClaimRefused:
    """The refusal for a unique-index conflict, named for the lock it hit.

    An attempt that stopped before anything was sent (needs_you or failed, never handed over) is not called live or
    submitted: a student-started claim releases it, so only an unattended one meets it here.
    """
    mine = conn.execute(
        "SELECT 1 FROM application_submit_claims c JOIN applications a ON a.id=c.application_id "
        f"WHERE c.user_id=? AND a.opportunity_id=? AND c.state<>'released' AND NOT ({_STOPPED_UNSENT})",
        (user_id, opportunity_id),
    ).fetchone()
    if mine is not None:
        return ClaimRefused(LIVE_APPLICATION, code="application")
    other = conn.execute(
        f"SELECT opportunity_id FROM application_submit_claims c WHERE user_id=? AND ats=? AND job_ref=? AND state<>'released' AND NOT ({_STOPPED_UNSENT})",
        (user_id, ats, job_ref),
    ).fetchone()
    if other is None:
        stopped = conn.execute(
            f"SELECT 1 FROM application_submit_claims c WHERE user_id=? AND state<>'released' AND {_STOPPED_UNSENT} "
            "AND (application_id IN (SELECT id FROM applications WHERE opportunity_id=? AND user_id=?) OR (ats=? AND job_ref=?))",
            (user_id, opportunity_id, user_id, ats, job_ref),
        ).fetchone()
        if stopped is not None:
            return ClaimRefused(STOPPED_EARLIER, code="stopped_earlier")
    return ClaimRefused(_other_copy(conn, other["opportunity_id"]) if other else LIVE_APPLICATION, code="job" if other else "application")


def _claim_row(conn: sqlite3.Connection, token: str, user_id: str, *, lock: bool = False) -> Any:
    suffix = automation.for_update_clause(conn) if lock else ""
    return conn.execute(
        f"SELECT * FROM application_submit_claims WHERE token=? AND user_id=?{suffix}", (token, user_id),
    ).fetchone()


def get_claim(conn: sqlite3.Connection, token: str, *, user_id: str) -> dict[str, Any] | None:
    row = _claim_row(conn, token, user_id)
    return None if row is None else dict(row)


def request_cancel(conn: sqlite3.Connection, token: str, *, user_id: str) -> bool:
    """Ask a claim that has not been handed over to stop. True if it was still ours to stop."""
    with conn:
        lock_user(conn, user_id)
        return bool(conn.execute(
            "UPDATE application_submit_claims SET cancel_requested=1, updated_at=? WHERE token=? AND user_id=? AND state='claimed'",
            (utc_now(), token, user_id),
        ).rowcount)


def hand_over(conn: sqlite3.Connection, token: str, *, user_id: str, now: datetime | None = None) -> bool:
    """Just before the click (submit) or inside the route that sees the student's POST (handoff): 5.2 rule 3.

    One transaction, after the lock and pause_guard. False, with nothing changed, when the claim is not ours as it
    was, when it was cancelled, or when a rule says wait: an unattended claim while automation is paused; a
    one-click claim when a pause began after the student confirmed (D6 B: the newer intent wins; a pause already on
    at the confirm does not stop it), or when the rehearsal it confirmed is 15 minutes old. Finish in browser is
    not refused by a pause: the student's own press of Submit is the confirm. When unsure, nothing is sent.
    """
    moment = _at(now)
    stamp = _stamp(now)
    with conn:
        lock_user(conn, user_id)
        paused = automation.pause_guard(conn, user_id)
        row = _claim_row(conn, token, user_id, lock=True)
        if row is None or row["state"] != "claimed" or row["cancel_requested"]:
            return False
        mode = row["mode"]
        if mode == "unattended" and paused:
            return False
        if mode == "one_click":
            confirmed = parse_app_instant(row["confirmed_at"])
            if confirmed is None:
                return False
            if paused:
                changed = parse_app_instant(setting_updated_at(conn, user_id, automation.PAUSED_KEY))
                if changed is None or changed > confirmed:
                    return False
            rehearsal = conn.execute(
                "SELECT finished_at, started_at FROM apply_runs WHERE id=? AND user_id=?",
                (json_as(row["detail_json"], {}).get("rehearsal_run_id", ""), user_id),
            ).fetchone()
            rehearsed = parse_app_instant(rehearsal["finished_at"] or rehearsal["started_at"]) if rehearsal is not None else None
            if rehearsed is None or moment - rehearsed >= CONFIRM_MAX_AGE:
                return False
        return bool(conn.execute(
            "UPDATE application_submit_claims SET state='clicking', after_click=1, handed_over_at=?, heartbeat_at=?, updated_at=? "
            "WHERE token=? AND user_id=? AND state='claimed'",
            (stamp, stamp, stamp, token, user_id),
        ).rowcount == 1)


def heartbeat(conn: sqlite3.Connection, token: str, *, now: datetime | None = None) -> bool:
    """From every wait loop, at least every 30 seconds: this claim's run is alive (5.2 rule 4)."""
    stamp = _stamp(now)
    with conn:
        return bool(conn.execute(
            "UPDATE application_submit_claims SET heartbeat_at=? WHERE token=? AND state IN ('claimed', 'clicking')", (stamp, token),
        ).rowcount)


def _watch_fields(stamp: str, watch: bool) -> tuple[str, str | None]:
    if not watch:
        return "not_watched", None
    until = parse_app_instant(stamp)
    return "awaiting_email", _iso(until + WATCH_WINDOW) if until else None


def _settle_tx(
    conn: sqlite3.Connection, token: str, user_id: str, *, state: str, note: str, after_click: bool | None,
    submitted_at: str | None, resolved_by: str, detail: dict[str, Any] | None, confirmation_seen: bool, watch: bool, stamp: str,
) -> bool:
    """5.2 rule 5's conditional update, inside a transaction the caller owns."""
    if state not in ("submitted", "unconfirmed", "needs_you", "failed"):
        raise ValueError(f"A claim is settled to submitted, unconfirmed, needs_you or failed, not {state}")
    # The confirmation page is stronger evidence than a crash recovery, so it may move an unconfirmed row.
    sources = ("claimed", "clicking", "unconfirmed") if state == "submitted" and confirmation_seen else ("claimed", "clicking")
    sets = ["state=?", "note=?", "updated_at=?"]
    params: list[Any] = [state, note, stamp]
    if after_click is not None:
        sets.append("after_click=?")
        params.append(1 if after_click else 0)
    if resolved_by:
        sets.append("resolved_by=?")
        params.append(resolved_by)
    if detail:
        sets.append("detail_json=?")
        current = conn.execute("SELECT detail_json FROM application_submit_claims WHERE token=?", (token,)).fetchone()
        params.append(_dumps({**json_as(current[0] if current else "", {}), **detail}))
    if state == "submitted":
        submitted = submitted_at or stamp
        verification, until = _watch_fields(submitted, watch)
        sets += ["submitted_at=?", "verification=?", "watch_until=?", "verified_at=?"]
        params += [submitted, verification, until, stamp]
    marks = ", ".join("?" for _ in sources)
    return bool(conn.execute(
        f"UPDATE application_submit_claims SET {', '.join(sets)} WHERE token=? AND user_id=? AND state IN ({marks})",
        (*params, token, user_id, *sources),
    ).rowcount)


def _submitted_event(
    conn: sqlite3.Connection, application_id: str, detail: dict[str, Any] | None, stamp: str,
) -> None:
    actions.log_application_event(conn, application_id, "apply_agent_submitted", None, stamp, encoded=_dumps(detail or {}))


def _after_missed_settle(conn: sqlite3.Connection, token: str, user_id: str, detail: dict[str, Any] | None, stamp: str) -> None:
    """A confirmation page was seen for a claim that is no longer ours to settle: the event, and a notice. Never raises."""
    try:
        row = _claim_row(conn, token, user_id)
        if row is None:
            return
        company = _title_of(conn, row["opportunity_id"])[1] or "the company"
        with conn:
            _submitted_event(conn, row["application_id"], detail, stamp)
            automation.insert_notice(
                conn, user_id, event_key=f"apply-late-confirmation:{token}", level="warning",
                title=f"Greenhouse showed its confirmation page for {company}, after this attempt was marked as not sent. Check it.",
                body="", timestamp=stamp,
            )
    except Exception:  # noqa: BLE001 - like send_claims.settle_send_claim: a failed report never hides the result
        LOGGER.exception("A late confirmation for an apply claim was not recorded")


def settle(
    conn: sqlite3.Connection,
    token: str,
    *,
    user_id: str,
    state: str,
    note: str = "",
    after_click: bool | None = None,
    submitted_at: str | None = None,
    resolved_by: str = "",
    detail: dict[str, Any] | None = None,
    confirmation_seen: bool = False,
    watch: bool = True,
    event_detail: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> bool:
    """Settle our own claim (5.2 rule 5); False when it was no longer ours. Never overwrites a newer attempt.

    Only 'claimed' and 'clicking' rows are settled, plus 'unconfirmed' when ``confirmation_seen``. A row that
    matches no longer (the student released it meanwhile) after a seen confirmation page still gets the
    apply_agent_submitted event and a notice, and this never raises. A submitted claim starts its confirmation
    email watch ('awaiting_email', for 24 hours), or is 'not_watched' when ``watch`` is False (D12).
    """
    stamp = _stamp(now)
    settled = False
    try:
        with conn:
            settled = _settle_tx(
                conn, token, user_id, state=state, note=note, after_click=after_click, submitted_at=submitted_at,
                resolved_by=resolved_by, detail=detail, confirmation_seen=confirmation_seen, watch=watch, stamp=stamp,
            )
            if settled and state == "submitted":
                _submitted_event(conn, _claim_row(conn, token, user_id)["application_id"], event_detail, stamp)
    finally:
        forget(token)
    if not settled and state == "submitted" and confirmation_seen:
        _after_missed_settle(conn, token, user_id, event_detail, stamp)
    return settled


def resolve_uncertain(
    conn: sqlite3.Connection, token: str, *, user_id: str, went_through: bool, watch: bool = True, now: datetime | None = None,
) -> dict[str, Any]:
    """The student's answer for an attempt that may have reached Greenhouse (5.2 rule 6).

    "It went through" makes it 'submitted' (resolved_by 'student'). "It didn't go through" releases it into a
    tombstone, which still counts toward the limits and asks before the same job is tried again. Refused with
    ClaimHeldError while the claim is held: its run may still be working.
    """
    stamp = _stamp(now)
    with conn:
        lock_user(conn, user_id)
        row = _claim_row(conn, token, user_id, lock=True)
        if row is None:
            raise ClaimNotFoundError(token)
        uncertain = row["state"] == "unconfirmed" or (row["state"] in ("needs_you", "failed") and row["after_click"])
        clicking = row["state"] == "clicking"
        if clicking and claim_held(row, now=now):
            raise ClaimHeldError("This application is still being submitted")
        if not (uncertain or clicking):
            raise ValueError("This attempt does not need an answer")
        if went_through:
            verification, until = _watch_fields(stamp, watch)
            conn.execute(
                "UPDATE application_submit_claims SET state='submitted', resolved_by='student', submitted_at=?, verification=?, "
                "watch_until=?, verified_at=?, updated_at=? WHERE token=? AND user_id=?",
                (stamp, verification, until, stamp, stamp, token, user_id),
            )
            _submitted_event(conn, row["application_id"], {"run_id": row["run_id"], "mode": row["mode"], "resolved_by": "student"}, stamp)
        else:
            conn.execute(
                "UPDATE application_submit_claims SET state='released', resolved_by='student', updated_at=? WHERE token=? AND user_id=?",
                (stamp, token, user_id),
            )
    forget(token)
    if went_through:
        record_stage(conn, token, user_id=user_id, now=now)
    return dict(_claim_row(conn, token, user_id))


# What settled a submission, said in the stage event and the ledger row: (event source, ledger basis, sentence).
_SETTLED_BY = {
    "page": ("apply_agent:confirmation_page", "confirmation_page", "Greenhouse showed its confirmation page"),
    "email": ("apply_agent:confirmation_email", "confirmation_email", "Greenhouse's confirmation email arrived"),
    "student": ("apply_agent:student_confirmed", "student_confirmed", "you said it went through"),
}


def _settled_by(row: Any) -> tuple[str, str, str]:
    """How this claim's submission was established, from resolved_by. A claim settled without one had the page."""
    return _SETTLED_BY.get(row["resolved_by"], _SETTLED_BY["page"])


def record_stage(
    conn: sqlite3.Connection, token: str, *, user_id: str, source: str | None = None, now: datetime | None = None,
) -> bool:
    """The forward-only stage write of 6.15 step 3, for a submitted claim whose stage is not recorded yet.

    'record' moves an application that is still 'applying' to 'applied' (applied_at = when it was submitted): a
    stage the student changed meanwhile wins. 'ledger' goes through automation.perform and waits while paused.
    'ask' never moves by itself; the student's "Mark as applied" passes ``source`` to make this same write. The
    event's source says what settled the submission (the page, the email, or the student) unless one is passed.
    True when the stage was settled one way or the other (stage_recorded is now 1).
    """
    stamp = _stamp(now)
    row = _claim_row(conn, token, user_id)
    if row is None or row["state"] != "submitted" or row["stage_recorded"]:
        return False
    if row["stage_policy"] == "ask" and source is None:
        return False
    if row["stage_policy"] == "ledger" and source is None:
        return _ledger_stage(conn, row)
    with conn:
        conn.execute("UPDATE applications SET updated_at=updated_at WHERE id=? AND user_id=?", (row["application_id"], user_id))
        stage = conn.execute(
            f"SELECT stage FROM applications WHERE id=? AND user_id=?{automation.for_update_clause(conn)}", (row["application_id"], user_id),
        ).fetchone()
        if stage is not None and stage["stage"] == "applying":
            actions.update_application_tx(
                conn, row["application_id"], stage="applied", applied_at=row["submitted_at"], user_id=user_id,
                source=source or _settled_by(row)[0], timestamp=stamp,
            )
        conn.execute(
            "UPDATE application_submit_claims SET stage_recorded=1 WHERE token=? AND user_id=? AND state='submitted'", (token, user_id),
        )
    return True


def _ledger_stage(conn: sqlite3.Connection, row: Any) -> bool:
    """Unattended mode's stage write, through the ledger. Waits while paused; needs the auto_apply switch (not built yet).

    The claim is marked recorded only when the ledger holds the decision (a row, whatever its status) or the stage
    had already moved on; perform returning None with the stage still 'applying' leaves it to the next pass (6.15).
    """
    user_id = row["user_id"]
    if "auto_apply" not in automation.FEATURES or automation.mode(conn, user_id, "auto_apply") == "off" or automation.paused(conn, user_id):
        return False
    title, company = _title_of(conn, row["opportunity_id"])
    _, basis, how = _settled_by(row)
    recorded = automation.perform(
        conn, user_id=user_id, feature="auto_apply", action_type="application.stage", subject_kind="application",
        subject_id=row["application_id"],
        after={"stage": "applied", "only_from": "applying", "applied_at": row["submitted_at"]},
        evidence={"claim": row["token"], "run_id": row["run_id"], "mode": row["mode"]},
        summary=f"Applied to {title} at {company} ({how})", basis=basis,
        confidence=None, idempotency_key=f"apply:{row['token']}", auto=True,
    )
    # None means nothing was written: a pause landed after the check above, or the stage was already past
    # 'applying' (a stage the student set wins). Only the second is settled; the first waits for the resume.
    if recorded is None:
        stage = conn.execute("SELECT stage FROM applications WHERE id=? AND user_id=?", (row["application_id"], user_id)).fetchone()
        if stage is None or stage["stage"] == "applying":
            return False
    with conn:
        conn.execute("UPDATE application_submit_claims SET stage_recorded=1 WHERE token=? AND user_id=?", (row["token"], user_id))
    return True


# --- Runs ---------------------------------------------------------------------------------


def create_run(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    opportunity_id: str,
    kind: str,
    started_by: str,
    ats: str,
    board_token: str,
    page_url: str,
    company: str,
    deadline_seconds: int,
    adapter_version: str = ADAPTER_VERSION,
    application_id: str | None = None,
    claim_token: str = "",
    now: datetime | None = None,
) -> str:
    """The row of a run that has started (lookups and rehearsals have no application: they are keyed by opportunity)."""
    if kind not in RUN_KINDS:
        raise ValueError(f"Unsupported run kind: {kind}")
    stamp = _stamp(now)
    run_id = f"run-{uuid4().hex}"
    with conn:
        conn.execute(
            """
            INSERT INTO apply_runs(id, user_id, opportunity_id, application_id, claim_token, kind, started_by, ats, adapter_version,
                                   company_key, board_token, page_url, status, heartbeat_at, deadline_at, started_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)
            """,
            (run_id, user_id, opportunity_id, application_id, claim_token, kind, started_by, ats, adapter_version, company, board_token,
             page_url, stamp, _iso(parse_app_instant(stamp) + timedelta(seconds=deadline_seconds)), stamp),
        )
    return run_id


def link_run(conn: sqlite3.Connection, *, user_id: str, run_id: str, token: str) -> bool:
    """Tie a run and its claim to each other, whichever was made first (claim() makes its token, create_run its id).

    Crash recovery reads the link from either side, so a run made before its claim and a claim made before its run
    both find each other. True when both rows were the student's and are now linked.
    """
    with conn:
        run = conn.execute(
            "UPDATE apply_runs SET claim_token=? WHERE id=? AND user_id=?", (token, run_id, user_id),
        ).rowcount
        claim = conn.execute(
            "UPDATE application_submit_claims SET run_id=? WHERE token=? AND user_id=?", (run_id, token, user_id),
        ).rowcount
    return bool(run and claim)


def heartbeat_run(conn: sqlite3.Connection, run_id: str, *, now: datetime | None = None) -> bool:
    with conn:
        return bool(conn.execute(
            "UPDATE apply_runs SET heartbeat_at=? WHERE id=? AND status='running'", (_stamp(now), run_id),
        ).rowcount)


_RUN_JSON = (
    ("reasons", "reasons_json"), ("plan", "plan_json"), ("options", "options_json"), ("screenshots", "screenshots_json"),
    ("refused", "refused_json"), ("requests", "requests_json"), ("evidence", "evidence_json"),
)


def _finish_run_tx(
    conn: sqlite3.Connection, run_id: str, *, outcome: str, clean: bool, plan_hash: str | None, stamp: str, **documents: Any,
) -> bool:
    sets = ["status='finished'", "outcome=?", "clean=?", "finished_at=?", "heartbeat_at=?"]
    params: list[Any] = [outcome, 1 if clean else 0, stamp, stamp]
    for name, column in _RUN_JSON:
        if documents.get(name) is not None:
            sets.append(f"{column}=?")
            params.append(_dumps(documents[name]))
    if plan_hash is not None:
        sets.append("plan_hash=?")
        params.append(plan_hash)
    return bool(conn.execute(f"UPDATE apply_runs SET {', '.join(sets)} WHERE id=? AND status='running'", (*params, run_id)).rowcount)


def finish_run(
    conn: sqlite3.Connection, run_id: str, *, outcome: str, clean: bool = False, plan_hash: str | None = None,
    now: datetime | None = None, **documents: Any,
) -> bool:
    """Finish a running run: outcome, whether the rehearsal was clean, and any of reasons, plan, options, screenshots,
    refused, requests, evidence (JSON documents; never a field value). False if it had already finished."""
    with conn:
        return _finish_run_tx(conn, run_id, outcome=outcome, clean=clean, plan_hash=plan_hash, stamp=_stamp(now), **documents)


def get_run(conn: sqlite3.Connection, run_id: str, *, user_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM apply_runs WHERE id=? AND user_id=?", (run_id, user_id)).fetchone()
    return None if row is None else dict(row)


def record_result(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    token: str,
    run_id: str,
    state: str,
    outcome: str,
    note: str = "",
    reasons: list[str] | None = None,
    after_click: bool | None = None,
    submitted_at: str | None = None,
    confirmation_seen: bool = False,
    watch: bool = True,
    evidence: dict[str, Any] | None = None,
    screenshots: list[dict[str, Any]] | None = None,
    requests: list[dict[str, Any]] | None = None,
    refused: list[dict[str, Any]] | None = None,
    event_detail: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """6.15: settle the claim and finish the run in one transaction, then the stage change and the notice.

    Returns {"settled", "stage_recorded"}. The stage moves only on 'submitted', by the claim's stage_policy, and
    only forward (record_stage). A result that arrives for a claim no longer ours (released meanwhile) is not
    applied to it; after a seen confirmation page it is still reported (settle). Notices carry no field values.
    """
    stamp = _stamp(now)
    settled = False
    # Only a submission whose confirmation page was seen was settled by the page (5.2, 6.14). A stop for the
    # student, a failure or an uncertain attempt is settled by no one yet: only the email or the student resolves it.
    resolved_by = "page" if state == "submitted" and confirmation_seen else ""
    try:
        with conn:
            lock_user(conn, user_id)
            settled = _settle_tx(
                conn, token, user_id, state=state, note=note, after_click=after_click, submitted_at=submitted_at,
                resolved_by=resolved_by, detail=None, confirmation_seen=confirmation_seen, watch=watch, stamp=stamp,
            )
            if run_id:
                _finish_run_tx(
                    conn, run_id, outcome=outcome, clean=False, plan_hash=None, stamp=stamp, reasons=reasons or [],
                    evidence=evidence, screenshots=screenshots, requests=requests, refused=refused,
                )
            row = _claim_row(conn, token, user_id)
            if settled and state == "submitted":
                _submitted_event(conn, row["application_id"], event_detail, stamp)
    finally:
        forget(token)
    if not settled:
        if state == "submitted" and confirmation_seen:
            _after_missed_settle(conn, token, user_id, event_detail, stamp)
        return {"settled": False, "stage_recorded": False}
    stage_recorded = record_stage(conn, token, user_id=user_id, now=now) if state == "submitted" else False
    title, company = _title_of(conn, row["opportunity_id"])
    if state == "submitted":
        text = f"Greenhouse showed its confirmation page for your application to {title} at {company}"
    elif state == "unconfirmed":
        text = f"{company}: your application may or may not have gone through"
    else:
        text = f"{company}: your application needs you"
    automation.notice(conn, user_id, event_key=f"apply-result:{token}:{state}", level="info" if state == "submitted" else "warning", title=text)
    return {"settled": True, "stage_recorded": stage_recorded}


# --- Reviews, the rehearsal gate and its breaker ------------------------------------------


def gate_reset_at(conn: sqlite3.Connection, user_id: str, ats: str) -> str:
    return get_setting(conn, user_id, f"{GATE_RESET_KEY}:{ats}") or ""


def gate(conn: sqlite3.Connection, user_id: str, ats: str, *, adapter_version: str = ADAPTER_VERSION) -> tuple[bool, int, int]:
    """9.2: (met, count, needed). count is the number of distinct companies with a clean rehearsal the student marked right."""
    reset = gate_reset_at(conn, user_id, ats)
    rows = conn.execute(
        "SELECT DISTINCT company_key FROM apply_runs WHERE user_id=? AND ats=? AND kind='rehearsal' AND outcome='rehearsed' "
        "AND clean=1 AND review='right' AND adapter_version=? AND started_at>?",
        (user_id, ats, adapter_version, reset),
    ).fetchall()
    needed = limits(conn, user_id)["rehearsals_before_submit"]
    return len(rows) >= needed, len(rows), needed


def mark_review(
    conn: sqlite3.Connection, run_id: str, *, user_id: str, verdict: str, note: str = "", now: datetime | None = None,
) -> dict[str, Any]:
    """The student marks a finished run right or wrong (this opens the gate), and the breaker looks at the marks (9.2).

    If 2 of the student's last 5 reviewed runs on the ATS are wrong (counting only marks made since the gate was
    last reset), the gate resets: apply_gate_reset_at:<ats> is set, older rehearsals stop counting, and a notice
    says why. Returns {"review", "breaker_tripped"}.
    """
    if verdict not in ("right", "wrong"):
        raise ValueError("A run is marked right or wrong")
    stamp = _stamp(now)
    tripped = False
    with conn:
        lock_user(conn, user_id)
        run = conn.execute("SELECT ats, kind, status FROM apply_runs WHERE id=? AND user_id=?", (run_id, user_id)).fetchone()
        if run is None:
            raise LookupError(run_id)
        if run["status"] != "finished" or run["kind"] == "lookup":
            raise ValueError("Only a finished rehearsal, submission or handoff can be marked")
        conn.execute(
            "UPDATE apply_runs SET review=?, review_note=?, reviewed_at=? WHERE id=? AND user_id=?",
            (verdict, note[:500], stamp, run_id, user_id),
        )
        ats = run["ats"]
        reset = gate_reset_at(conn, user_id, ats)
        recent = conn.execute(
            "SELECT review FROM apply_runs WHERE user_id=? AND ats=? AND review<>'' AND reviewed_at>? ORDER BY reviewed_at DESC LIMIT ?",
            (user_id, ats, reset, BREAKER_WINDOW),
        ).fetchall()
        if sum(1 for item in recent if item["review"] == "wrong") >= BREAKER_LIMIT:
            put_setting(conn, user_id, f"{GATE_RESET_KEY}:{ats}", stamp, stamp)
            tripped = True
            needed = limits(conn, user_id)["rehearsals_before_submit"]
            automation.insert_notice(
                conn, user_id, event_key=f"apply-breaker:{ats}:{stamp}", level="warning",
                title=f"Apply for me: {BREAKER_LIMIT} of your last {BREAKER_WINDOW} reviews were wrong",
                body=f"It needs {needed} new clean rehearsals that you mark right before it offers to submit again.", timestamp=stamp,
            )
    return {"review": verdict, "breaker_tripped": tripped}


# --- Crash recovery -----------------------------------------------------------------------

_STOPPED_BEFORE = "The app stopped before handing your application to Greenhouse. Nothing was sent."
_STOPPED_DURING = "The app stopped while submitting. Check whether it arrived."


def recover_stale(conn: sqlite3.Connection, now: datetime | None = None, *, user_id: str | None = None) -> dict[str, int]:
    """5.2 rule 7: settle what a stopped server left behind, for every student (or one). Runs at start and every worker pass.

    A claim that is not held: 'claimed' becomes 'failed' (nothing could have left: routing aborts every request that
    could carry the application before hand-over); 'clicking' becomes 'unconfirmed'. A 'submitted' claim whose stage
    was not recorded gets that write retried, for 'record' and 'ledger' only, never 'ask'. A running run whose
    heartbeat is more than two minutes old and is not working in this process is finished (as 'failed', or as
    'unconfirmed' when its claim was handed over) with a notice. Never touches an attempt that is already uncertain:
    only the confirmation email or the student settles those.
    """
    moment = _at(now)
    scope, args = ("AND user_id=?", (user_id,)) if user_id else ("", ())
    counts = {"failed": 0, "unconfirmed": 0, "stage_retried": 0, "runs_failed": 0}
    for row in conn.execute(
        f"SELECT * FROM application_submit_claims WHERE state IN ('claimed', 'clicking') {scope} ORDER BY created_at", args,
    ).fetchall():
        if claim_held(row, now=moment):
            continue
        stopped = row["state"] == "claimed"
        with conn:
            lock_user(conn, row["user_id"])
            changed = conn.execute(
                "UPDATE application_submit_claims SET state=?, note=?, updated_at=? WHERE token=? AND state=?",
                ("failed" if stopped else "unconfirmed", _STOPPED_BEFORE if stopped else _STOPPED_DURING, _stamp(now), row["token"], row["state"]),
            ).rowcount
        if not changed:
            continue
        counts["failed" if stopped else "unconfirmed"] += 1
        if not stopped:
            company = _title_of(conn, row["opportunity_id"])[1] or "the company"
            automation.notice(
                conn, row["user_id"], event_key=f"apply-stopped:{row['token']}", level="warning",
                title=f"{company}: the app stopped while submitting. Check whether your application arrived.",
            )
    for row in conn.execute(
        f"SELECT * FROM application_submit_claims WHERE state='submitted' AND stage_recorded=0 AND stage_policy IN ('record', 'ledger') {scope}",
        args,
    ).fetchall():
        if record_stage(conn, row["token"], user_id=row["user_id"], now=now):
            counts["stage_retried"] += 1
    stale = _iso(moment - HELD_HEARTBEAT)
    for run in conn.execute(
        f"SELECT * FROM apply_runs WHERE status='running' AND heartbeat_at<? {scope} ORDER BY started_at", (stale, *args),
    ).fetchall():
        if run["id"] in RUNNING_RUNS:
            continue
        # The claim is found from either side: the run's claim_token, or the claim's run_id (a run made before its claim).
        claims = conn.execute(
            "SELECT * FROM application_submit_claims WHERE user_id=? AND ((?<>'' AND token=?) OR run_id=?)",
            (run["user_id"], run["claim_token"], run["claim_token"], run["id"]),
        ).fetchall()
        if any(item["state"] in ("claimed", "clicking") and claim_held(item, now=moment) for item in claims):
            continue  # its attempt is still being worked: only the run's own heartbeat went quiet
        outcome, body = _stopped_run(run["kind"], claims)
        with conn:
            done = _finish_run_tx(conn, run["id"], outcome=outcome, clean=False, plan_hash=None, stamp=_stamp(now),
                                  reasons=["The app stopped during this run"])
        if done:
            counts["runs_failed"] += 1
            automation.notice(
                conn, run["user_id"], event_key=f"apply-run-stopped:{run['id']}", level="warning",
                title="The app stopped during an Apply for me run", body=body,
            )
    return counts


def _stopped_run(kind: str, claims: list[Any]) -> tuple[str, str]:
    """The outcome and notice body for an orphaned run: it says nothing was sent only when the app saw that nothing left.

    A lookup or a rehearsal never submits, but it does send what was typed into a field to Greenhouse's lookup
    service (5.5), so it says no application was sent and stops there. A submit or handoff run says 'Nothing was
    sent.' only when its claim was found and was never handed over; with no claim found, or one handed over, it may
    have reached Greenhouse.
    """
    if kind in ("lookup", "rehearsal"):
        return "failed", "No application was sent."
    if claims and not any(item["after_click"] for item in claims):
        return "failed", "Nothing was sent."
    return "unconfirmed", "Check whether your application arrived."


def students_to_watch(conn: sqlite3.Connection, now: datetime | None = None) -> list[str]:
    """The students the worker's apply step works for, whether or not Apply for me is on (5.6).

    A claim in 'claimed' or 'clicking'; an uncertain attempt or a tombstone handed over in the last 14 days; a
    submitted claim still being watched (or whose stage is not recorded yet); or a run still 'running'.
    """
    since = _iso(_at(now) - timedelta(days=WATCH_DAYS))
    rows = conn.execute(
        """
        SELECT user_id FROM application_submit_claims WHERE state IN ('claimed', 'clicking')
           OR (handed_over_at>=? AND (state IN ('unconfirmed', 'released') OR (state IN ('needs_you', 'failed') AND after_click=1)))
           OR (state='submitted' AND ((verification IN ('awaiting_email', 'no_email_24h') AND submitted_at>=?)
                                      OR (stage_recorded=0 AND stage_policy IN ('record', 'ledger'))))
        UNION SELECT user_id FROM apply_runs WHERE status='running'
        ORDER BY user_id
        """,
        (since, since),
    ).fetchall()
    return [str(row[0]) for row in rows]


def run_worker_step(conn: sqlite3.Connection, *, apply_root: Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    """The AutomationWorker's apply step (5.6): independent of every switch.

    For each student students_to_watch returns, recover_stale (a student who turned Apply for me off still gets their
    open claims finished). Once per local day, for every student, purge_evidence when an ``apply_root`` is given.
    One student's failure is recorded (apply_agent.runner) and never stops the next.
    """
    report: dict[str, Any] = {"recovered": {}, "purged": []}
    for user_id in students_to_watch(conn, now):
        try:
            counts = recover_stale(conn, now, user_id=user_id)
        except Exception as exc:  # noqa: BLE001 - recorded, and the next student still runs
            LOGGER.exception("Apply for me recovery failed for one student")
            _rollback(conn)
            _record_runner(conn, user_id, ok=False, error=exc)
            continue
        if any(counts.values()):
            report["recovered"][user_id] = counts
        _record_runner(conn, user_id, ok=True)
    if apply_root is not None:
        for row in conn.execute("SELECT id FROM users ORDER BY id").fetchall():
            user_id = str(row[0])
            try:
                if _purged_today(conn, user_id, _at(now)):
                    continue
                purge_evidence(conn, now=now, apply_root=apply_root, user_id=user_id)
                with conn:
                    put_setting(conn, user_id, PURGE_LAST_RUN_KEY, _iso(_at(now)), _stamp(now))
                report["purged"].append(user_id)
                _record_runner(conn, user_id, ok=True, component=RETENTION_COMPONENT)
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("Apply for me evidence retention failed for one student")
                _rollback(conn)
                _record_runner(conn, user_id, ok=False, error=exc, component=RETENTION_COMPONENT)
    return report


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.rollback()
    except Exception:  # noqa: BLE001
        pass


def _record_runner(
    conn: sqlite3.Connection, user_id: str, *, ok: bool, error: Exception | None = None, component: str = RUNNER_COMPONENT,
) -> None:
    try:
        automation.record_health(conn, user_id, component, ok=ok, error=step_error(error) if error else "")
    except Exception:  # noqa: BLE001 - a health row never stops the pass
        _rollback(conn)


def _purged_today(conn: sqlite3.Connection, user_id: str, moment: datetime) -> bool:
    last = parse_app_instant(get_setting(conn, user_id, PURGE_LAST_RUN_KEY))
    if last is None:
        return False
    zone = user_timezone(conn, user_id)
    return zone.to_local(last).date() == zone.today(moment)


# --- Retention and deletion (11) ----------------------------------------------------------

_RUN_FILE = re.compile(r"^(run-[0-9a-f]{32})-")
_USER_FOLDER = re.compile(r"^[0-9a-f]{16}$")


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _screenshot_file(root: Path, stored: str) -> Path:
    path = Path(stored)
    return path if path.is_absolute() else root / path


def purge_evidence(
    conn: sqlite3.Connection, *, apply_root: Path, now: datetime | None = None, user_id: str | None = None,
) -> dict[str, int]:
    """Retention (11): screenshots older than PIPELINE_APPLY_EVIDENCE_DAYS (90) go, their hashes stay.

    Deletes each old screenshot file and clears its ``path`` in screenshots_json, keeping the sha256 for good;
    deletes files under a student's folder that no run row references (left by a crash mid-screenshot; the files of
    a run that is still running are left alone); and trims progress_json to its last step once a run is 30 days
    old. Only files inside ``apply_root`` are ever deleted, and never anything directly in it (the hash key lives
    there). ``user_id`` limits it to one student's rows and folder.
    """
    moment = _at(now)
    root = apply_root.resolve()
    scope, args = ("AND user_id=?", (user_id,)) if user_id else ("", ())
    counts = {"apply_screenshots_removed": 0, "apply_orphan_files_removed": 0, "apply_progress_trimmed": 0}
    old = _iso(moment - timedelta(days=evidence_days()))
    for row in conn.execute(
        f"SELECT id, screenshots_json FROM apply_runs WHERE started_at<? AND screenshots_json<>'[]' {scope}", (old, *args),
    ).fetchall():
        shots = json_as(row["screenshots_json"], [])
        removed = 0
        for shot in shots:
            stored = str(shot.get("path") or "") if isinstance(shot, dict) else ""
            if not stored:
                continue
            path = _screenshot_file(root, stored)
            if _inside(root, path):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    continue
            shot["path"] = ""
            removed += 1
        if removed:
            with conn:
                conn.execute("UPDATE apply_runs SET screenshots_json=? WHERE id=?", (_dumps(shots), row["id"]))
            counts["apply_screenshots_removed"] += removed
    counts["apply_orphan_files_removed"] = _remove_orphans(conn, root, user_id, now)
    trim = _iso(moment - timedelta(days=PROGRESS_KEEP_DAYS))
    for row in conn.execute(
        f"SELECT id, progress_json FROM apply_runs WHERE status='finished' AND started_at<? AND progress_json<>'[]' {scope}", (trim, *args),
    ).fetchall():
        steps = json_as(row["progress_json"], [])
        if len(steps) > 1:
            with conn:
                conn.execute("UPDATE apply_runs SET progress_json=? WHERE id=?", (_dumps(steps[-1:]), row["id"]))
            counts["apply_progress_trimmed"] += 1
    return counts


def _remove_orphans(conn: sqlite3.Connection, root: Path, user_id: str | None, now: datetime | None = None) -> int:
    """Files under a student's folder that no run row references, whose run is not still working, and that are not new."""
    if not root.is_dir():
        return 0
    folders = [root / user_folder(user_id)] if user_id else [child for child in root.iterdir() if child.is_dir() and _USER_FOLDER.match(child.name)]
    # Read the working runs first: one that records its screenshots and finishes between the two reads is then in
    # 'referenced' (read second) and cannot fall in neither set. A file made moments ago is left as well; a crash
    # orphan is old by definition.
    working = {str(row[0]) for row in conn.execute("SELECT id FROM apply_runs WHERE status='running'").fetchall()}
    referenced: set[Path] = set()
    for row in conn.execute("SELECT screenshots_json FROM apply_runs WHERE screenshots_json<>'[]'").fetchall():
        for shot in json_as(row["screenshots_json"], []):
            stored = str(shot.get("path") or "") if isinstance(shot, dict) else ""
            if stored:
                referenced.add(_screenshot_file(root, stored).resolve())
    fresh = (_at(now) - HELD_HEARTBEAT).timestamp()
    removed = 0
    for folder in folders:
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*"), reverse=True):
            if path.is_dir():
                try:
                    path.rmdir()  # only if it is empty now
                except OSError:
                    pass
                continue
            named = _RUN_FILE.match(path.name)
            if path.resolve() in referenced or (named and named.group(1) in working):
                continue
            try:
                if path.stat().st_mtime > fresh:
                    continue
                path.unlink()
            except OSError:
                continue
            removed += 1
    return removed


def delete_apply_folder(apply_root: Path, user_id: str) -> int:
    """Account deletion (5.7): remove this student's screenshot folder in full. Returns how many files it held."""
    root = apply_root.resolve()
    folder = (root / user_folder(user_id)).resolve()
    if folder.parent != root or not folder.is_dir():
        return 0
    count = sum(1 for path in folder.rglob("*") if path.is_file())
    shutil.rmtree(folder, ignore_errors=True)
    return count


# --- Settings: the requirement, the agent probe, and the student's exact option labels (5.5, 5.6) -------------

INSTALL_PLAYWRIGHT = "Install Playwright and Chromium: python -m playwright install chromium"
NOT_HERE = "Apply for me runs only in your own app, with Playwright installed."
LINUX_DISPLAY = (
    "Apply for me needs to open a window. Run: systemctl --user import-environment DISPLAY WAYLAND_DISPLAY, then restart the dashboard"
)
NEEDS_NAME = "Add your first and last name for applications in your profile"
NEEDS_EMAIL = "Add your email to your profile"
NEEDS_RESUME = "Confirm a résumé on your Profile page first"

# The agent factory this app was built with (create_app wires it; None for a test or the fuzz sandbox). The
# apply_agent switch's requirement runs from automation.REQUIREMENTS, which is given only a connection and a
# student, so the factory's probe is kept here. Apps in one process share it: the last one built wins.
_AGENT_FACTORY: Any = None
_PROBE_CACHE: dict[str, Any] = {"at": 0.0, "answer": ""}
_PROBE_SECONDS = 300.0


def configure_agent_factory(factory: Any) -> None:
    """Record which agent factory this app has (None means Apply for me cannot run here)."""
    global _AGENT_FACTORY
    _AGENT_FACTORY = factory


class PlaywrightProbe:
    """The agent factory the real app has until the agent itself is built: it can say whether a window could open.

    ``available()`` returns "" when Playwright and its Chromium are present and, on Linux, a display is set,
    else the sentence saying what is missing. It reads the installed package and never the network, and
    answers the Playwright half from a five minute cache because the requirement runs on every settings
    render. The display is asked here, not by the requirement, so a fake factory answers it too (12.6).
    """

    def available(self) -> str:
        installed = self._installed()
        if installed:
            return installed
        if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            return LINUX_DISPLAY
        return ""

    def _installed(self) -> str:
        now = monotonic()
        if _PROBE_CACHE["at"] and now - _PROBE_CACHE["at"] < _PROBE_SECONDS:
            return str(_PROBE_CACHE["answer"])
        # Playwright's sync API refuses to start on a thread that has an asyncio loop, so it is asked from a thread of its own.
        found: list[str] = []
        thread = threading.Thread(target=lambda: found.append(_probe_playwright()), name="apply-agent-probe", daemon=True)
        thread.start()
        thread.join(20)
        answer = found[0] if found else INSTALL_PLAYWRIGHT
        _PROBE_CACHE.update(at=now, answer=answer)
        return answer


def _probe_playwright() -> str:
    """"" when Playwright and its Chromium build are installed, else the sentence saying what to install."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            path = playwright.chromium.executable_path
        return "" if path and Path(path).exists() else INSTALL_PLAYWRIGHT
    except Exception:  # noqa: BLE001 - no package, no browser build, or a broken driver: all mean "install it"
        return INSTALL_PLAYWRIGHT


def setup_requirement(conn: sqlite3.Connection, user_id: str) -> str:
    """What Apply for me still needs before it can be turned on, or "" (automation.REQUIREMENTS). Reads only, no network.

    The first that applies: the agent factory's own probe (Playwright and Chromium missing, or a Linux server
    with no display), no first and last name for applications, no confirmed email, no confirmed résumé. The confirmation-email checks (D12)
    belong to a one-click submit, not to the switch.
    """
    from . import apply_policy, preparation

    if _AGENT_FACTORY is None:
        return NOT_HERE
    probe = getattr(_AGENT_FACTORY, "available", None)
    missing = str(probe() or "") if callable(probe) else ""
    if missing:
        return missing
    facts = preparation.confirmed_facts(conn, user_id)
    first, last, _preferred = apply_policy.name_parts(facts)
    if not (first and last):
        return NEEDS_NAME
    contact = facts.get("contact")
    if not (isinstance(contact, dict) and isinstance(contact.get("email"), str) and contact["email"].strip()):
        return NEEDS_EMAIL
    if conn.execute("SELECT 1 FROM resume_versions WHERE user_id=? AND status='confirmed' LIMIT 1", (user_id,)).fetchone() is None:
        return NEEDS_RESUME
    return ""


def list_ats_labels(conn: sqlite3.Connection, user_id: str, ats: str = ATS_GREENHOUSE) -> dict[str, dict[str, str]]:
    """The exact option labels the student confirmed, by field: {field: {label, confirmed_at}}."""
    rows = conn.execute("SELECT field, label, confirmed_at FROM apply_ats_labels WHERE user_id=? AND ats=? ORDER BY field", (user_id, ats)).fetchall()
    return {str(row["field"]): {"label": str(row["label"]), "confirmed_at": str(row["confirmed_at"])} for row in rows}


def set_ats_label(
    conn: sqlite3.Connection, user_id: str, field: str, label: str, *, ats: str = ATS_GREENHOUSE, now: datetime | None = None,
) -> dict[str, str]:
    """Save the exact text of the option the student picked for a typeahead list. Replaces an earlier one."""
    from .apply_policy import ALLOWED_ATS_LABEL_FIELDS

    if field not in ALLOWED_ATS_LABEL_FIELDS:
        raise ValueError(f"Unknown option list: {field}")
    text = " ".join(str(label or "").split())
    if not text or len(text) > 200:
        raise ValueError("An option label is 1 to 200 characters")
    stamp = _stamp(now)
    with conn:
        conn.execute(
            "INSERT INTO apply_ats_labels(user_id, ats, field, label, confirmed_at) VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, ats, field) DO UPDATE SET label=excluded.label, confirmed_at=excluded.confirmed_at",
            (user_id, ats, field, text, stamp),
        )
    return {"field": field, "label": text, "confirmed_at": stamp}


def delete_ats_label(conn: sqlite3.Connection, user_id: str, field: str, *, ats: str = ATS_GREENHOUSE) -> bool:
    with conn:
        cursor = conn.execute("DELETE FROM apply_ats_labels WHERE user_id=? AND ats=? AND field=?", (user_id, ats, field))
    return bool(cursor.rowcount)
