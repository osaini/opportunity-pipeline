"""Outreach work the app does on its own, each behind the student's own switch.

- auto_drafts: a company with a usable contact and location but no draft gets
  one written, whatever brought it in (added by hand, imported, a new contact
  found later, a deep search draft that failed). It waits for approval.
- bounce_recovery: after an email bounces, the company's site is searched
  again and the best other contact applied (choose_contact, never an address
  that bounced). The greeting follows (outreach._readdress_drafts) and the draft
  goes back for approval.
- bounce_auto_resend: with bounce_recovery, the bounced first email goes again
  at once to the new contact, without a click, when only its greeting changed
  and a guessed address has an inbox from the company's site in Cc
  (resend_refusal). Once per company.
- scheduled_sending: the student's confirmed Send queues the approved email
  for the recipient's next weekday morning (outreach_schedule.py).
- form_submission: a company with no email but a contact form on its site
  gets its approved first message sent through that form (outreach_forms.py),
  once. A form that asks for a picture CAPTCHA or a field the app cannot
  answer waits for the student.

Only scheduled_sending, bounce_auto_resend and form_submission lead to
anything going out, and only a first message the student approved (for a
resend, approved before the bounce, with only the greeting changed since).
Every switch is off until the student turns it on (user_settings). The switches are features in automation.FEATURES, and the
student's pause stops all of them (automation.is_enabled). A pause that
lands while a step is running (a site search or a model call can take
minutes) stops it too: the write that would finish it checks again first
(_unless_stopped), and the worker checks before each company.
``AutomationWorker`` runs the work on a background thread.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import ExitStack, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from . import apply_runs, automation, internal_automation, outreach_thank_you
from .background import PollingWorker, record_health_quietly, step_error
from .database import rollback_quietly
from .outreach import (
    get_target, greeting_style, greets_contact, heard_back, latest_event_stamp, list_targets, log_event, without_greeting,
    withdraw_auto_approval,
)
from .outreach_contacts import apply_choice, choose_contact, find_contacts, list_candidates
from .outreach_forms import form_due
from .outreach_gmail import last_bounce
from .schema import connect_product
from .timestamps import utc_now
from .web_fetch import SafeFetcher

LOGGER = logging.getLogger(__name__)

# The automation_health component each worker pass records for every student it worked for.
WORKER_COMPONENT = "automation.worker"

# The outreach switches, as they have always been shown here: a view of the registry.
SETTINGS = {
    key: automation.FEATURES[key].description
    for key in ("auto_drafts", "bounce_recovery", "bounce_auto_resend", "scheduled_sending", "follow_up_review", "form_submission")
}
RECOVERY_EVENT = "contact_recovery"
RESEND_EVENT = "resent_after_bounce"
AUTO_DRAFT_FAILED = "auto_draft_failed"
# A draft that failed (the model was down, the profile was missing a fact) is
# tried again after this, not on every pass.
DRAFT_RETRY_AFTER = timedelta(hours=6)


def _unless_stopped(conn: sqlite3.Connection, user_id: str, key: str) -> Callable[[], None]:
    """What an automatic step's final write runs first, inside its transaction.

    It raises AutomationPaused, so nothing is written, when the student paused
    automation or turned the feature off while the step ran.
    """
    def check() -> None:
        if not automation.still_enabled(conn, user_id, key):
            raise automation.AutomationPaused("Automation was paused, or this switch turned off, before it finished, so nothing was changed")

    return check


def settings(conn: sqlite3.Connection, *, user_id: str) -> dict[str, bool]:
    """Each outreach switch as the student set it. Pause is not folded in: the worker checks it where it acts."""
    current = automation.modes(conn, user_id, list(SETTINGS))
    return {key: current[key] == "on" for key in SETTINGS}


def update_settings(conn: sqlite3.Connection, changes: dict[str, Any], *, user_id: str) -> dict[str, bool]:
    unknown = set(changes) - set(SETTINGS)
    if unknown:
        raise ValueError(f"Unknown automation settings: {', '.join(sorted(unknown))}")
    automation.set_modes(conn, user_id, {key: "on" if value else "off" for key, value in changes.items()})
    return settings(conn, user_id=user_id)


# --- Bounce recovery ----------------------------------------------------------------


def _latest(conn: sqlite3.Connection, target_id: str, user_id: str, event_type: str) -> datetime | None:
    stamp = latest_event_stamp(conn, target_id, user_id, event_type)
    return datetime.fromisoformat(stamp) if stamp else None


def recovery_due(conn: sqlite3.Connection, *, user_id: str) -> list[str]:
    """Companies whose contact bounced and that have not been searched again since."""
    due = []
    for item in list_targets(conn, user_id=user_id, interested_only=True, statuses=("not_started", "drafted")):
        if not item["contact_bounced"] or item["sent_at"] or item["status"] not in {"not_started", "drafted"} or heard_back(item):
            continue
        bounced = last_bounce(conn, item["id"], user_id)
        tried = _latest(conn, item["id"], user_id, RECOVERY_EVENT)
        if bounced is not None and (tried is None or tried < bounced):
            due.append(item["id"])
    return due


def recover_contact(
    conn: sqlite3.Connection,
    target_id: str,
    *,
    user_id: str,
    fetcher: SafeFetcher,
    renderer: Any = None,
    verifier: Any = None,
    contact_delay: float = 1.0,
    automatic: bool = False,
) -> dict[str, Any]:
    """Search the company's site again and apply the best contact that has not bounced.

    Recorded as a contact_recovery event either way, so each bounce is searched once.
    ``automatic`` (the worker) applies nothing, and records nothing, when the
    student paused automation or turned bounce recovery off during the
    search: the result says paused, and the bounce is searched again on resume.
    """
    target = get_target(conn, target_id, user_id=user_id)
    failed = set(target["bounced_addresses"])
    note = ""
    if target["website"]:
        try:
            find_contacts(conn, target_id, user_id=user_id, fetcher=fetcher, delay=contact_delay, renderer=renderer, verifier=verifier)
        except (ValueError, LookupError, OSError, httpx.HTTPError) as exc:
            note = f" The site search failed: {exc}"[:300]
    candidates = [candidate for candidate in list_candidates(conn, target_id, user_id=user_id)
                  if candidate.get("email") and candidate["email"].casefold() not in failed]
    choice = choose_contact(candidates)
    current = get_target(conn, target_id, user_id=user_id)
    resend = None
    if not current["contact_bounced"]:
        # The student picked someone while the search ran; theirs stands.
        detail = "You chose a new contact while the app was looking."
        choice = None
    elif choice:
        try:
            apply_choice(
                conn, target_id, choice, user_id=user_id,
                before_write=_unless_stopped(conn, user_id, "bounce_recovery") if automatic else None,
            )
        except automation.AutomationPaused as exc:
            return {"target_id": target_id, "company": target["company"], "to": None, "detail": str(exc), "paused": True}
        cc = f", Cc {choice['cc']['email']}" if choice.get("cc") else ""
        detail = f"Chose {choice['to']['email']}{cc} ({choice['basis'].replace('_', ' ')})."
        if automatic and automation.is_enabled(conn, user_id, "bounce_auto_resend"):
            resend = resend_after_bounce(conn, target_id, user_id=user_id, before=current, choice=choice)
        detail += f" {resend['detail']}" if resend else " Review the draft, then send it again."
    elif not target["website"]:
        detail = "No website on record, so there was nowhere to look. Add another contact by hand."
    else:
        detail = f"Found no other address on the company's site. Add one by hand, or try Find people.{note}"
    with conn:
        log_event(conn, target_id, user_id, RECOVERY_EVENT, detail=detail)
    return {
        "target_id": target_id, "company": target["company"], "to": choice["to"]["email"] if choice else None, "detail": detail,
        "resent": bool(resend and resend["queued"]),
    }


# --- Resend after a bounce ---------------------------------------------------------------


def resend_refusal(
    before: dict[str, Any], after: dict[str, Any], choice: dict[str, Any], *, resent_before: bool, style: dict[str, str],
) -> str:
    """Why the bounced first email may not go again to the new contact on its own, or "" when it may.

    ``before`` is the target just before the new contact was applied, ``after``
    just after. The rules are the student's (2026-09-28): the words are the
    ones they approved, with only the greeting changed for the new contact; a
    guessed address goes only with an inbox their site lists in Cc, so a wrong
    guess still reaches the company; and it happens once per company. The
    greeting must fit the new contact (outreach.greets_contact): one the
    student wrote to someone else is left as it was when the contact changes.
    ``style`` is the student's greeting style (outreach.greeting_style).
    """
    if resent_before:
        return "It was already resent once automatically, so this time it waits for you"
    if heard_back(before) or heard_back(after):
        return "They may have answered the earlier email (a reply, or an email that may be one), so it waits for you"
    if before["draft_status"] != "approved":
        return "The email that bounced was not an approved draft"
    if choice["basis"] in {"strong_guess", "weak_guess"} and not choice.get("cc"):
        return f"{choice['to']['email']} is a guess and their site lists no inbox to Cc"
    if choice["basis"] not in {"confirmed", "strong_guess", "weak_guess", "shared_inbox"}:
        return "The new contact's basis is not one that may be sent to automatically"
    if after["contact_bounced"] or after["cc_bounced"]:
        return "An address on it bounced before"
    if after["email_subject"] != before["email_subject"] or without_greeting(after["email_body"]) != without_greeting(before["email_body"]):
        return "More than the greeting changed"
    if not greets_contact(after["email_body"], after["contact_name"], after["company"], style):
        return "Its greeting is not to the new contact"
    return ""


def resend_after_bounce(
    conn: sqlite3.Connection, target_id: str, *, user_id: str, before: dict[str, Any], choice: dict[str, Any],
) -> dict[str, Any]:
    """Approve the readdressed first email again and queue it to go at once, when resend_refusal allows it.

    The approval is recorded as automatic (RESEND_EVENT). If it cannot be
    queued, the approval is taken back, so nothing stays approved that the
    student did not approve. Returns {"queued", "detail"}.
    """
    from .outreach import approve_draft
    from .outreach_schedule import send_soon  # imported here: it imports this module

    after = get_target(conn, target_id, user_id=user_id)
    resent_before = _latest(conn, target_id, user_id, RESEND_EVENT) is not None
    refusal = resend_refusal(before, after, choice, resent_before=resent_before, style=greeting_style(conn, user_id))
    if refusal:
        return {"queued": False, "detail": f"Not resent automatically: {refusal}. Review the draft, then send it again."}
    to = after["contact_email"] + (f" (Cc {after['contact_cc']})" if after["contact_cc"] else "")
    try:
        approved = approve_draft(conn, target_id, user_id=user_id, fingerprint=after["draft_fingerprint"], acknowledge_warnings=True)
        send_soon(conn, target_id, user_id=user_id, fingerprint=approved["draft_fingerprint"],
                  detail=f"The email to {to} goes out again now, after the bounce")
        with conn:
            log_event(conn, target_id, user_id, RESEND_EVENT, detail=(
                f"Approved again automatically for {to}: only the greeting changed from the email you approved"
            ))
    except Exception as exc:  # noqa: BLE001 - whatever stopped it, the student reviews the draft instead
        LOGGER.warning("The resend after a bounce was not queued: %s", exc)
        with conn:
            withdraw_auto_approval(conn, target_id, user_id, "The automatic resend could not be queued")
        return {"queued": False, "detail": f"Not resent automatically: {exc}. Review the draft, then send it again."[:500]}
    return {"queued": True, "detail": f"Sending it again now to {to} (Resend after a bounce is on)."}


# --- Automatic drafts --------------------------------------------------------------------


def draft_due(conn: sqlite3.Connection, *, user_id: str, now: datetime | None = None) -> list[str]:
    """Companies ready for a first draft: a contact (an address that has not bounced, or a contact form), a location, no draft, nothing sent."""
    now = now or datetime.now(timezone.utc)
    due = []
    for item in list_targets(conn, user_id=user_id, interested_only=True, statuses=("not_started", "drafted")):
        if item["email_body"] or item["sent_at"] or item["status"] not in {"not_started", "drafted"}:
            continue
        reachable = item["contact_email"] or item["contact_form"]
        if not reachable or item["contact_bounced"] or item["cc_bounced"] or not item["location_verified"]:
            continue
        failed = _latest(conn, item["id"], user_id, AUTO_DRAFT_FAILED)
        if failed is not None and now - failed < DRAFT_RETRY_AFTER:
            continue
        due.append(item["id"])
    return due


def auto_draft(
    conn: sqlite3.Connection,
    target_id: str,
    *,
    user_id: str,
    provider_factory: Callable[[str, str], Any],
    draft_provider: str | None = None,
    automatic: bool = False,
) -> dict[str, Any]:
    """Write a first draft for one company. ``automatic`` (the worker) saves nothing when the student
    paused automation or turned automatic drafts off during the model call; it is tried again on resume."""
    from .outreach_drafting import generate_draft  # imported here: drafting pulls in the model providers

    try:
        target = generate_draft(
            conn, target_id, user_id=user_id, provider_factory=provider_factory, provider=draft_provider,
            before_write=_unless_stopped(conn, user_id, "auto_drafts") if automatic else None,
        )
    except automation.AutomationPaused as exc:
        # Not a failure: nothing is recorded, so it is not held back DRAFT_RETRY_AFTER.
        return {"target_id": target_id, "drafted": False, "paused": True, "error": str(exc)}
    except (ValueError, RuntimeError) as exc:
        with conn:
            log_event(conn, target_id, user_id, AUTO_DRAFT_FAILED, detail=f"{exc}"[:500])
        return {"target_id": target_id, "drafted": False, "error": str(exc)[:500]}
    return {"target_id": target_id, "company": target["company"], "drafted": True}


# --- Contact forms ---------------------------------------------------------------------


def send_form(conn: sqlite3.Connection, target_id: str, *, user_id: str, submitter_factory: Callable[..., Any]) -> dict[str, Any]:
    """Send one approved first message through its contact form; a refusal is reported, not raised.

    A pause that lands first leaves the form waiting (found), so it goes once
    the student resumes; it is not parked as a refusal.
    """
    from .outreach_forms import submit_contact_form

    try:
        result = submit_contact_form(conn, target_id, user_id=user_id, submitter_factory=submitter_factory, automatic=True)
    except automation.AutomationPaused as exc:
        return {"target_id": target_id, "outcome": "paused", "note": str(exc)}
    except Exception as exc:  # noqa: BLE001 - the next pass tries the next company
        LOGGER.warning("Contact form for %s was not sent: %s", target_id, exc)
        # Parked for the student, so the same refusal is not tried every pass.
        with conn:
            conn.execute(
                "UPDATE outreach_contact_forms SET state='needs_you', note=?, updated_at=? WHERE target_id=? AND user_id=? AND state='found'",
                (str(exc)[:500], utc_now(), target_id, user_id),
            )
        return {"target_id": target_id, "outcome": "refused", "note": str(exc)[:300]}
    return {"target_id": target_id, "company": result["target"]["company"], "outcome": result["outcome"], "note": result["note"]}


# --- In the background ---------------------------------------------------------------------


def _step_failed(
    conn: sqlite3.Connection, errors: dict[str, str], users: list[str], exc: BaseException, log: str | None = None,
) -> None:
    """One worker step raised: log it when given a message, undo what it left open, and record it for these students.

    Only the first error of a pass is kept for a student (setdefault), as the pass's health row says.
    """
    if log is not None:
        LOGGER.exception(log)
    rollback_quietly(conn, LOGGER, "an inbox step failed")
    for user_id in users:
        errors.setdefault(user_id, step_error(exc))


class AutomationWorker(PollingWorker):
    """Runs each student's switched-on automation on one background thread.

    A pass first shows waiting automation notices as desktop pop-ups (for
    students who turned them on, paused or not: a notice only informs;
    desktop_notify), then sends every scheduled email that is due (for any
    student: turning the switch off does not strand one already scheduled,
    and a student who paused automation has theirs held), then recovers
    every bounced contact that is due (sending at once, in the same pass, any
    bounced email bounce_auto_resend may send again), then writes at most one draft and
    sends at most one contact form, so a slow model call or page never holds
    the others up for long. Then the changes that stay inside the app
    (internal_automation): up to five unanswered companies closed, at most
    one follow-up draft, and the daily archive of silent applications. Then
    the thank-yous after a plain decline (outreach_thank_you): up to three
    written and scheduled; the scheduled sends above send them when due.
    Then Apply for me's upkeep (apply_runs.run_worker_step), which needs no
    switch: a student who turned it off still has an application a stopped
    server left mid-submit finished, and once a local day their old
    screenshots are deleted (apply_root).
    Nothing that acts runs for a paused student.
    Each pass records how it went in automation_health (automation.worker).
    """

    thread_name = "outreach-automation"
    failure_message = "Outreach automation pass failed"
    logger = LOGGER

    def __init__(
        self,
        platform_target: Path | str,
        *,
        fetcher_factory: Callable[[], SafeFetcher],
        renderer_factory: Callable[[], Any] = lambda: None,
        verifier_factory: Callable[[], Any] = lambda: None,
        provider_factory: Callable[[str, str], Any] | None = None,
        draft_provider: str | None = None,
        contact_delay: float = 1.0,
        gmail_client_factory: Callable[[], Any] | None = None,
        form_submitter_factory: Callable[..., Any] | None = None,
        interval_seconds: float = 60.0,
        decisions_for: Callable[[sqlite3.Connection, str], Any] | None = None,
        on_reply: Callable[[sqlite3.Connection, str, str], None] | None = None,
        apply_root: Path | None = None,
    ) -> None:
        self.platform_target = platform_target
        # Where Apply for me keeps screenshots; only given for the real database, so nothing else is ever purged.
        self._apply_root = apply_root
        # The InboxWatcher's reply classifier and reply hook, for a reply auto-close's fresh look finds.
        self._decisions_for = decisions_for
        self._on_reply = on_reply
        self._gmail_client_factory = gmail_client_factory
        self._form_submitter_factory = form_submitter_factory
        self._fetcher_factory = fetcher_factory
        self._renderer_factory = renderer_factory
        self._verifier_factory = verifier_factory
        self._provider_factory = provider_factory
        self._draft_provider = draft_provider
        self._contact_delay = contact_delay
        super().__init__(interval_seconds)

    def run_once(self) -> dict[str, Any]:
        """One pass. Each step stands alone: one that raises is logged and recorded, and the next still runs.

        The pass records its outcome as the automation.worker health component,
        once for every student it worked for: those with an outreach switch or
        desktop pop-ups on, and those with a scheduled email due. That is ok,
        or the first error of the pass with any address taken out. A student
        with nothing on and nothing due gets no row.
        """
        # internal_automation adds "closed", "follow_up_drafts", and "archived" when those switches ran.
        report: dict[str, Any] = {"sent": [], "recovered": [], "drafted": [], "forms": []}
        with closing(connect_product(self.platform_target)) as conn:
            errors: dict[str, str] = {}
            desktop_users = self._users_with(conn, ("desktop_notifications",))
            try:
                from .desktop_notify import deliver_desktop_notices  # imported here: only the worker shows pop-ups

                deliver_desktop_notices(conn)
            except Exception as exc:  # noqa: BLE001 - a pop-up never holds up a send
                _step_failed(conn, errors, desktop_users, exc, "Desktop notices were not shown")
            due_users: list[str] = []
            if self._gmail_client_factory is not None:
                from .outreach_schedule import run_due_sends  # imported here: it pulls in the Gmail send path

                due_users = self._users_with_due_sends(conn)
                try:
                    # A reply the check before each send finds is read, by Jev too, as the InboxWatcher reads one.
                    report["sent"] = run_due_sends(
                        conn, client_factory=self._gmail_client_factory, decisions_for=self._decisions_for, on_reply=self._on_reply,
                    )
                except Exception as exc:  # noqa: BLE001 - the other steps still run, and the failure is recorded
                    _step_failed(conn, errors, due_users, exc, "Scheduled emails were not sent")
            users = self._users_with(conn, (*SETTINGS, *internal_automation.WORKER_FEATURES, *outreach_thank_you.WORKER_FEATURES))
            for user_id in users:
                try:
                    self._run_for(conn, user_id, report)
                except Exception as exc:  # noqa: BLE001 - one student's failure never stops the next student
                    _step_failed(conn, errors, [user_id], exc, "Outreach automation failed for one student")
                try:
                    # Changes that stay inside the app: auto-close, follow-up drafts, the daily archive.
                    internal_automation.run_for_user(
                        conn, user_id, report, gmail_client_factory=self._gmail_client_factory,
                        provider_factory=self._provider_factory, draft_provider=self._draft_provider,
                        decisions_for=self._decisions_for, on_reply=self._on_reply,
                    )
                except Exception as exc:  # noqa: BLE001 - recorded like any other step's failure
                    _step_failed(conn, errors, [user_id], exc)
                try:
                    # A thank-you after a plain decline, written and scheduled; sent above when due.
                    outreach_thank_you.run_for_user(conn, user_id, report, provider_factory=self._provider_factory)
                except Exception as exc:  # noqa: BLE001 - recorded like any other step's failure
                    _step_failed(conn, errors, [user_id], exc)
            try:
                # Independent of every switch; it records its own health (apply_agent.runner) per student.
                upkeep = apply_runs.run_worker_step(conn, apply_root=self._apply_root)
                if upkeep["recovered"] or upkeep["purged"]:
                    report["apply"] = upkeep
            except Exception:  # noqa: BLE001 - the other students' passes are done; the next pass tries again
                LOGGER.exception("Apply for me upkeep failed")
                rollback_quietly(conn, LOGGER, "an inbox step failed")
            for user_id in sorted({*desktop_users, *due_users, *users}):
                record_health_quietly(conn, user_id, WORKER_COMPONENT, ok=user_id not in errors, error=errors.get(user_id, ""))
        return report

    @staticmethod
    def _users_with(conn: sqlite3.Connection, keys: tuple[str, ...]) -> list[str]:
        """Students with any of these switches on, whether or not automation is paused."""
        return [row[0] for row in conn.execute(
            f"SELECT DISTINCT user_id FROM user_settings WHERE value='on' AND key IN ({', '.join('?' for _ in keys)}) ORDER BY user_id",
            keys,
        ).fetchall()]

    @staticmethod
    def _users_with_due_sends(conn: sqlite3.Connection) -> list[str]:
        """Students with a scheduled email this pass works on: one that is due, or one cut off mid-send."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return [row[0] for row in conn.execute(
            "SELECT DISTINCT user_id FROM outreach_scheduled_sends "
            "WHERE (state='scheduled' AND send_at<=?) OR state IN ('sending', 'transmitting') ORDER BY user_id",
            (now,),
        ).fetchall()]

    def _run_for(self, conn: sqlite3.Connection, user_id: str, report: dict[str, Any]) -> None:
        """One student's switched-on steps: bounce recovery, then at most one draft and one contact form per pass."""
        if automation.is_enabled(conn, user_id, "bounce_recovery"):
            due = recovery_due(conn, user_id=user_id)
            if due:
                with ExitStack() as stack:
                    fetcher = stack.enter_context(self._fetcher_factory())
                    renderer = self._renderer_factory()
                    renderer = stack.enter_context(renderer) if renderer is not None else None
                    verifier = self._verifier_factory()
                    verifier = stack.enter_context(verifier) if verifier is not None else None
                    for target_id in due:
                        # Each company can take minutes, so a pause stops the pass before the next one.
                        if not automation.is_enabled(conn, user_id, "bounce_recovery"):
                            break
                        recovered = recover_contact(
                            conn, target_id, user_id=user_id, fetcher=fetcher, renderer=renderer,
                            verifier=verifier, contact_delay=self._contact_delay, automatic=True,
                        )
                        if recovered.get("paused"):
                            break
                        report["recovered"].append(recovered)
                if self._gmail_client_factory is not None and any(item.get("resent") for item in report["recovered"]):
                    from .outreach_schedule import run_due_sends  # imported here: it pulls in the Gmail send path

                    # Right away, as the student asked: not on the next pass, after drafts and forms.
                    report["sent"].extend(run_due_sends(
                        conn, client_factory=self._gmail_client_factory, decisions_for=self._decisions_for, on_reply=self._on_reply,
                    ))
        if self._provider_factory is not None and not report["drafted"] and automation.is_enabled(conn, user_id, "auto_drafts"):
            due = draft_due(conn, user_id=user_id)
            if due:
                drafted = auto_draft(
                    conn, due[0], user_id=user_id, provider_factory=self._provider_factory, draft_provider=self._draft_provider,
                    automatic=True,
                )
                if not drafted.get("paused"):
                    report["drafted"].append(drafted)
        if self._form_submitter_factory is not None and not report["forms"] and automation.is_enabled(conn, user_id, "form_submission"):
            due = form_due(conn, user_id=user_id)
            if due:
                report["forms"].append(send_form(conn, due[0], user_id=user_id, submitter_factory=self._form_submitter_factory))

    def _run_pass(self) -> None:
        self.run_once()
