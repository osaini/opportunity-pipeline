"""Automatic steps that never leave the app, each behind its own switch and each with an Undo.

- application_silence: an application still at Applied N days after the
  student applied gets an Urgent row ("No reply yet"). N is the profile's
  application_follow_up_days (default 21). The row is derived each time the
  queue is read (urgent.py); nothing is written, so there is nothing to undo.
- archive_silent_applications: at M days (archive_after_days, default 60) the
  application moves to Archived through the ledger, at most once a day per
  student. automation_archived says whether an application's stage was last
  set by that switch, so a later email can reopen it (Phase 1 calls it).
- outreach_auto_close: a company that never answered its follow-up
  (outreach.lifecycle_suggestion says no_response) is closed as No response,
  after outreach_review.fresh_look has read Gmail once more. It fails closed:
  when Gmail cannot be read, the company is left for the next pass, and a
  company Gmail is never searched for (outreach_inbox.watched_ids: no
  address, or first written to too long ago) is left for the student. A
  close tried once (undone, say) is not tried again for the same follow-up
  date.
- auto_follow_up_drafts: when a company's follow-up date arrives with no
  reply and no bounce, the follow-up draft is written (outreach_drafting). It
  waits for the student's approval; nothing is sent.

Every change goes through automation.perform, whose transaction checks the
pause and the switch again before it writes, so a pause that lands during a
Gmail read or a model call still stops the write.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from . import automation
from .schema import utc_now
from .user_time import user_timezone

LOGGER = logging.getLogger(__name__)

DEFAULT_FOLLOW_UP_DAYS = 21
DEFAULT_ARCHIVE_DAYS = 60
MAX_DAYS = 365
AUTO_CLOSE_PER_PASS = 5
AUTO_FOLLOW_UP_DRAFT_FAILED = "auto_follow_up_draft_failed"
# The same wait as a failed first draft (outreach_automation.DRAFT_RETRY_AFTER).
FOLLOW_UP_RETRY_AFTER = timedelta(hours=6)
ARCHIVE_LAST_RUN_KEY = "archive_silent_applications.last_run"
ARCHIVE_EVERY = timedelta(days=1)
AUTO_CLOSE_BASIS = "lifecycle:no_reply_14d"


# --- Per-student settings -------------------------------------------------------------------


def _profile(conn: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    """The stored profile, read without creating one (profile.get_profile would)."""
    row = conn.execute("SELECT profile_json FROM profiles WHERE user_id=?", (user_id,)).fetchone()
    try:
        profile = json.loads(row[0] or "{}") if row else {}
    except (TypeError, ValueError):
        return {}
    return profile if isinstance(profile, dict) else {}


def _days(profile: dict[str, Any], key: str, default: int) -> int:
    value = profile.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_DAYS:
        return default
    return value


def follow_up_days(conn: sqlite3.Connection, user_id: str) -> int:
    """Days after applying with no reply before Urgent says so: the profile's application_follow_up_days, else 21."""
    return _days(_profile(conn, user_id), "application_follow_up_days", DEFAULT_FOLLOW_UP_DAYS)


def archive_days(conn: sqlite3.Connection, user_id: str) -> int:
    """Days after applying before a silent application is archived: archive_after_days, else 60."""
    return _days(_profile(conn, user_id), "archive_after_days", DEFAULT_ARCHIVE_DAYS)


# --- Application silence (Urgent rows) ----------------------------------------------------


def silence_rows(conn: sqlite3.Connection, user_id: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Applications still at Applied N days after the student applied, as Urgent rows dated applied + N.

    Only with the application_silence switch on. The switch, not the pause,
    decides: a row only informs, like a notice, so it still shows while
    automation is paused. A row appears on day N, not before.
    """
    if automation.mode(conn, user_id, "application_silence") != "on":
        return []
    days = follow_up_days(conn, user_id)
    zone = user_timezone(conn, user_id)
    today = zone.today(now)
    rows = conn.execute(
        """
        SELECT a.id AS application_id, a.applied_at, a.stage, o.id AS opportunity_id, o.company, o.title
        FROM applications a JOIN opportunities o ON o.id = a.opportunity_id
        WHERE a.user_id = ? AND a.stage = 'applied' AND a.applied_at IS NOT NULL AND a.applied_at <> ''
        """,
        (user_id,),
    ).fetchall()
    found = []
    for row in rows:
        try:
            applied_on = zone.calendar_date(row["applied_at"])
        except ValueError:
            continue
        if applied_on is None:
            continue
        due = applied_on + timedelta(days=days)
        if due > today:
            continue
        found.append({
            "kind": "application_silence",
            "record_id": str(row["application_id"]),
            "raw_date": due.isoformat(),
            "date_only": True,
            "title": row["title"],
            "company": row["company"],
            "subtitle": f"No reply {days} days after you applied",
            "opportunity_id": str(row["opportunity_id"]),
            "application_id": row["application_id"],
            "stage": row["stage"],
        })
    return found


# --- Archive silent applications (Could) ---------------------------------------------------


def automation_archived(conn: sqlite3.Connection, application_id: str) -> bool:
    """Whether the application's latest stage change was an archive archive_silent_applications made and still stands.

    A later matched email may reopen such an application; one the student
    archived is never reopened.
    """
    row = conn.execute(
        """
        SELECT detail_json FROM application_events
        WHERE application_id=? AND event_type='stage_changed' ORDER BY created_at DESC, id DESC LIMIT 1
        """,
        (application_id,),
    ).fetchone()
    if row is None:
        return False
    try:
        source = str(json.loads(row["detail_json"] or "{}").get("source") or "")
    except (TypeError, ValueError, AttributeError):
        return False
    if not source.startswith("automation:"):
        return False
    action = conn.execute(
        "SELECT feature, status, action_type FROM automation_actions WHERE id=?", (source.split(":", 1)[1],),
    ).fetchone()
    if not (action and action["feature"] == "archive_silent_applications" and action["status"] == "applied"
            and action["action_type"] == "application.stage"):
        return False
    # The daily sync can move an imported application back without a stage_changed
    # event (schema._migrate_status), so the archive stands only while the stage is still archived.
    stage = conn.execute("SELECT stage FROM applications WHERE id=?", (application_id,)).fetchone()
    return stage is not None and stage["stage"] == "archived"


def archive_due(conn: sqlite3.Connection, user_id: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Applications still at Applied at least M days after the student applied."""
    days = archive_days(conn, user_id)
    zone = user_timezone(conn, user_id)
    today = zone.today(now)
    due = []
    for row in conn.execute(
        """
        SELECT a.id, a.applied_at, o.company, o.title FROM applications a JOIN opportunities o ON o.id=a.opportunity_id
        WHERE a.user_id=? AND a.stage='applied' AND a.applied_at IS NOT NULL AND a.applied_at<>''
        ORDER BY a.applied_at, a.id
        """,
        (user_id,),
    ).fetchall():
        try:
            applied_on = zone.calendar_date(row["applied_at"])
        except ValueError:
            continue
        if applied_on is not None and (today - applied_on).days >= days:
            due.append({**dict(row), "days": (today - applied_on).days, "applied_on": applied_on.isoformat()})
    return due


def _archive_ran_recently(conn: sqlite3.Connection, user_id: str, now: datetime) -> bool:
    row = conn.execute("SELECT value FROM user_settings WHERE user_id=? AND key=?", (user_id, ARCHIVE_LAST_RUN_KEY)).fetchone()
    last = automation._parse(row[0]) if row else None
    return last is not None and now - last < ARCHIVE_EVERY


def archive_silent_applications(conn: sqlite3.Connection, user_id: str, *, now: datetime | None = None, force: bool = False) -> list[dict[str, Any]]:
    """Archive every application that has been silent M days, at most once a day per student. Returns what was archived."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not automation.is_enabled(conn, user_id, "archive_silent_applications"):
        return []
    if not force and _archive_ran_recently(conn, user_id, now):
        return []
    archived = []
    for item in archive_due(conn, user_id, now=now):
        if not automation.is_enabled(conn, user_id, "archive_silent_applications"):
            break
        try:
            row = automation.perform(
                conn, user_id=user_id, feature="archive_silent_applications", action_type="application.stage",
                subject_kind="application", subject_id=item["id"], after={"stage": "archived", "only_from": "applied"},
                evidence={"subject": f"{item['title']} at {item['company']}", "applied_on": item["applied_on"], "days": item["days"]},
                summary=f"Archived {item['title']} at {item['company']}: no reply {item['days']} days after you applied",
                basis=f"silence:{archive_days(conn, user_id)}d", confidence=None,
                idempotency_key=f"archive-silent:{item['id']}:{item['applied_at']}", auto=True,
            )
        except automation.NotApplicable:
            continue
        if row is not None and row.get("status") == "applied" and row.get("created_at") == row.get("applied_at"):
            archived.append({"application_id": item["id"], "action_id": row["id"]})
    with conn:
        automation._put_setting(conn, user_id, ARCHIVE_LAST_RUN_KEY, now.isoformat(timespec="seconds"), utc_now())
    return archived


# --- Outreach auto-close -------------------------------------------------------------------


def _auto_close_key(target: dict[str, Any]) -> str:
    return f"auto-close:{target['id']}:{target['follow_up_at']}"


def auto_close_due(conn: sqlite3.Connection, user_id: str, *, today: date | None = None) -> list[dict[str, Any]]:
    """Companies whose follow-up went unanswered long enough that lifecycle_suggestion says no_response.

    A company with any reply on record (logged, or waiting as a suggestion) is
    left alone: someone wrote back, so closing it as No response would be wrong.
    One tried before (its ledger row exists, whatever became of it, such as an
    undo) is not tried again for the same follow-up date, so an undo sticks and
    costs no Gmail read on later passes.
    """
    from .outreach import lifecycle_suggestion, list_targets, local_today

    today = today or local_today(conn, user_id)
    due = []
    for item in list_targets(conn, user_id=user_id, status="followed_up", today=today):
        suggestion = lifecycle_suggestion(item, today)
        if not suggestion or suggestion["status"] != "no_response":
            continue
        if item["reply_count"] or item["reply_suggestion"]:
            continue
        if automation._by_key(conn, user_id, _auto_close_key(item)) is not None:
            continue
        due.append(item)
    return due


def auto_close(
    conn: sqlite3.Connection, user_id: str, *, client_factory: Callable[[], Any] | None, today: date | None = None,
    limit: int = AUTO_CLOSE_PER_PASS, decisions_for: Callable[[Any, str], Any] | None = None,
    on_reply: Callable[[Any, str, str], None] | None = None, now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Close up to ``limit`` unanswered companies as No response. Each one Gmail is read for first; it fails closed.

    Only a company whose replies Gmail is searched for (outreach_inbox.watched_ids),
    with Gmail connected, is closed; any other is held and takes none of the
    ``limit``. ``decisions_for`` and ``on_reply`` are the InboxWatcher's, so a
    reply the fresh look finds is classified and starts call prep as it would there.

    Returns one entry per company it looked at: closed, or held with the reason.
    """
    from .outreach import NO_RESPONSE_AFTER_DAYS, get_target, lifecycle_suggestion, local_today
    from .outreach_gmail import _connector
    from .outreach_inbox import REPLY_WINDOW, watched_ids
    from .outreach_review import FRESH_LOOK_REASONS, fresh_look

    results: list[dict[str, Any]] = []
    if not automation.is_enabled(conn, user_id, "outreach_auto_close"):
        return results
    today = today or local_today(conn, user_id)
    due = auto_close_due(conn, user_id, today=today)
    if not due:
        return results
    searched = watched_ids(conn, user_id, now)
    not_searched = (
        f"Gmail is never searched for this company's replies (no email address, or it was first written to over "
        f"{REPLY_WINDOW.days} days ago), so it is left for you to close"
    )
    checkable = []
    for item in due:
        if item["id"] in searched:
            checkable.append(item)
        else:
            results.append({"target_id": item["id"], "company": item["company"], "closed": False, "reason": not_searched})
    connector = _connector(conn, user_id)
    if client_factory is None:
        gmail_problem = "Gmail is not set up, so replies could not be checked"
    elif connector is None or connector["status"] == "disconnected":
        gmail_problem = FRESH_LOOK_REASONS["not_connected"]
    elif connector["status"] != "connected":
        gmail_problem = FRESH_LOOK_REASONS["needs_reconnect"]
    else:
        gmail_problem = ""
    decisions: Any = None
    asked_for_decisions = False
    for item in checkable[:limit]:
        if not automation.is_enabled(conn, user_id, "outreach_auto_close"):
            break
        target_id = item["id"]
        if gmail_problem:
            results.append({"target_id": target_id, "company": item["company"], "closed": False, "reason": gmail_problem})
            continue
        if decisions_for is not None and not asked_for_decisions:
            asked_for_decisions = True
            try:
                decisions = decisions_for(conn, user_id)
            except Exception:  # noqa: BLE001 - a reply is then classified by its words alone
                LOGGER.warning("The reply classifier could not be set up for auto-close", exc_info=True)
        look = fresh_look(conn, target_id, user_id=user_id, client_factory=client_factory, decisions=decisions, on_reply=on_reply)
        if not look.get("ok"):
            results.append({"target_id": target_id, "closed": False, "reason": look.get("reason") or "Gmail could not be read"})
            continue
        # Read again: the fresh look may have found a reply, which reopens the company.
        target = get_target(conn, target_id, user_id=user_id, today=today)
        suggestion = lifecycle_suggestion(target, today)
        if target["status"] != "followed_up" or not suggestion or suggestion["status"] != "no_response" \
                or target["reply_count"] or target["reply_suggestion"]:
            results.append({"target_id": target_id, "closed": False, "reason": "It is no longer waiting on a reply"})
            continue
        days = (today - date.fromisoformat(target["follow_up_at"])).days
        row = automation.perform(
            conn, user_id=user_id, feature="outreach_auto_close", action_type="outreach.status",
            subject_kind="outreach_target", subject_id=target_id, after={"status": "no_response", "only_from": "followed_up"},
            evidence={
                "subject": f"No reply from {target['company']} {days} days after the follow-up",
                "company": target["company"], "follow_up_at": target["follow_up_at"], "days_since": days,
                "threshold_days": NO_RESPONSE_AFTER_DAYS, "gmail_checked": True,
            },
            summary=f"Closed {target['company']} as No response: no reply {days} days after the follow-up",
            basis=AUTO_CLOSE_BASIS, confidence=None,
            idempotency_key=_auto_close_key(target), auto=True,
        )
        closed = row is not None and row.get("status") == "applied" and row.get("created_at") == row.get("applied_at")
        results.append({"target_id": target_id, "company": target["company"], "closed": closed,
                        "reason": "" if closed else "Nothing was changed"})
    return results


# --- Automatic follow-up drafts ------------------------------------------------------------


def _follow_up_key(target: dict[str, Any]) -> str:
    return f"follow-up-draft:{target['id']}:{target['follow_up_at']}"


def _latest_event(conn: sqlite3.Connection, target_id: str, user_id: str, event_type: str) -> datetime | None:
    row = conn.execute(
        "SELECT MAX(created_at) FROM outreach_events WHERE target_id=? AND user_id=? AND event_type=?",
        (target_id, user_id, event_type),
    ).fetchone()
    return automation._parse(row[0]) if row and row[0] else None


def follow_up_draft_due(conn: sqlite3.Connection, user_id: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """Companies whose follow-up is due in the student's timezone, with no follow-up draft, no reply, and no bounce.

    One tried before (its ledger row exists, whatever became of it, such as an
    undo) is not written again for the same follow-up date, and one that failed
    waits FOLLOW_UP_RETRY_AFTER.
    """
    from .outreach import list_targets, local_today

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    today = local_today(conn, user_id, now)
    due = []
    for item in list_targets(conn, user_id=user_id, status="sent", today=today):
        if not item["follow_up_due"] or not item["email_body"]:
            continue
        if item["follow_up_body"] or item["follow_up_subject"] or item["follow_up_status"] != "none":
            continue
        if item["reply_count"] or item["reply_suggestion"] or item["contact_bounced"] or item.get("bounced_at"):
            continue
        if automation._by_key(conn, user_id, _follow_up_key(item)) is not None:
            continue
        failed = _latest_event(conn, item["id"], user_id, AUTO_FOLLOW_UP_DRAFT_FAILED)
        if failed is not None and now - failed < FOLLOW_UP_RETRY_AFTER:
            continue
        due.append(item)
    return due


def auto_follow_up_draft(
    conn: sqlite3.Connection, target: dict[str, Any], *, user_id: str, provider_factory: Callable[[str, str], Any],
    draft_provider: str | None = None,
) -> dict[str, Any]:
    """Write one company's follow-up draft and record it in the ledger. It waits for approval; nothing is sent.

    A failure (the model was down, the draft was not grounded) is logged as
    auto_follow_up_draft_failed and tried again after FOLLOW_UP_RETRY_AFTER. A
    pause or the switch turned off during the model call saves nothing and
    is not a failure: it is tried again on resume.
    """
    from .outreach import _log
    from .outreach_drafting import compose_draft

    target_id = target["id"]
    try:
        prepared = compose_draft(
            conn, target_id, user_id=user_id, provider_factory=provider_factory, kind="follow_up", provider=draft_provider,
        )
    except (ValueError, RuntimeError, LookupError) as exc:
        with conn:
            _log(conn, target_id, user_id, AUTO_FOLLOW_UP_DRAFT_FAILED, detail=f"{exc}"[:500])
        return {"target_id": target_id, "drafted": False, "error": str(exc)[:500]}
    try:
        row = automation.perform(
            conn, user_id=user_id, feature="auto_follow_up_drafts", action_type="outreach.follow_up_draft",
            subject_kind="outreach_target", subject_id=target_id, after={"draft": prepared},
            evidence={
                "subject": f"Follow-up for {target['company']} was due {target['follow_up_at']}",
                "company": target["company"], "follow_up_at": target["follow_up_at"], "generated_by": prepared["generated_by"],
            },
            summary=f"Wrote a follow-up draft for {target['company']}. It waits for your approval",
            basis="follow_up_due", confidence=None, idempotency_key=_follow_up_key(target), auto=True,
        )
    except automation.NotApplicable as exc:
        return {"target_id": target_id, "drafted": False, "skipped": True, "error": str(exc)}
    if row is None:
        # Nothing saved, and not a failure: paused or switched off during the model call
        # (tried again on resume), or a follow-up was written meanwhile (it stays).
        if not automation.is_enabled(conn, user_id, "auto_follow_up_drafts"):
            return {"target_id": target_id, "drafted": False, "paused": True}
        return {"target_id": target_id, "drafted": False, "skipped": True, "error": "A follow-up was written meanwhile"}
    return {"target_id": target_id, "company": target["company"], "drafted": row.get("status") == "applied", "action_id": row["id"]}


# --- The worker's share ---------------------------------------------------------------------


# The switches the automation worker runs these steps for.
WORKER_FEATURES = ("outreach_auto_close", "auto_follow_up_drafts", "archive_silent_applications")


def run_for_user(
    conn: sqlite3.Connection, user_id: str, report: dict[str, Any], *,
    gmail_client_factory: Callable[[], Any] | None, provider_factory: Callable[[str, str], Any] | None,
    draft_provider: str | None = None, now: datetime | None = None,
    decisions_for: Callable[[Any, str], Any] | None = None, on_reply: Callable[[Any, str, str], None] | None = None,
) -> None:
    """One student's internal steps for one worker pass: auto-close, one follow-up draft, and the daily archive.

    Each step stands alone; the first error is raised after the others ran.
    ``decisions_for`` and ``on_reply`` are the InboxWatcher's (auto_close).
    """
    first_error: Exception | None = None
    if automation.is_enabled(conn, user_id, "outreach_auto_close"):
        try:
            report.setdefault("closed", []).extend(auto_close(
                conn, user_id, client_factory=gmail_client_factory, decisions_for=decisions_for, on_reply=on_reply,
            ))
        except Exception as exc:  # noqa: BLE001 - the next step still runs
            LOGGER.exception("Closing unanswered companies failed")
            _rollback(conn)
            first_error = first_error or exc
    if provider_factory is not None and not report.get("follow_up_drafts") and automation.is_enabled(conn, user_id, "auto_follow_up_drafts"):
        try:
            due = follow_up_draft_due(conn, user_id, now=now)
            if due:
                report.setdefault("follow_up_drafts", []).append(
                    auto_follow_up_draft(conn, due[0], user_id=user_id, provider_factory=provider_factory, draft_provider=draft_provider)
                )
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Writing a follow-up draft failed")
            _rollback(conn)
            first_error = first_error or exc
    if automation.is_enabled(conn, user_id, "archive_silent_applications"):
        try:
            report.setdefault("archived", []).extend(archive_silent_applications(conn, user_id, now=now))
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Archiving silent applications failed")
            _rollback(conn)
            first_error = first_error or exc
    if first_error is not None:
        raise first_error


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        if getattr(conn, "in_transaction", False):
            conn.rollback()
    except Exception:  # noqa: BLE001
        LOGGER.warning("Could not roll back after an automatic step failed", exc_info=True)
