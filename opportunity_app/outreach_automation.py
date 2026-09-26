"""Outreach work the app does on its own, each behind the student's own switch.

- auto_drafts: a company with a usable contact and location but no draft gets
  one written, whatever brought it in (added by hand, imported, a new contact
  found later, a deep search draft that failed). It waits for approval.
- bounce_recovery: after an email bounces, the company's site is searched
  again and the best other contact applied (choose_contact, never an address
  that bounced). The greeting follows (outreach._readdress_drafts) and the draft
  goes back for approval.
- scheduled_sending: the student's confirmed Send queues the approved email
  for the recipient's next weekday morning (outreach_schedule.py).

Only scheduled_sending leads to mail going out, and only an email the student
approved and scheduled. Every switch is off until the student turns it on
(user_settings). ``AutomationWorker`` runs the work on a background thread.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import ExitStack, closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import httpx

from .outreach import _log, get_target, list_targets
from .outreach_contacts import SafeFetcher, apply_choice, choose_contact, find_contacts, list_candidates
from .outreach_gmail import last_bounce
from .schema import connect_product, utc_now

LOGGER = logging.getLogger(__name__)

SETTINGS = {
    "auto_drafts": "Write a draft for every company with a contact and a location",
    "bounce_recovery": "After a bounce, find another contact and fix the greeting",
    "scheduled_sending": "Send approved emails on the recipient's next weekday morning",
    "follow_up_review": "Have a second model check each follow-up before it goes out",
}
RECOVERY_EVENT = "contact_recovery"
AUTO_DRAFT_FAILED = "auto_draft_failed"
# A draft that failed (the model was down, the profile was missing a fact) is
# tried again after this, not on every pass.
DRAFT_RETRY_AFTER = timedelta(hours=6)


def settings(conn: sqlite3.Connection, *, user_id: str) -> dict[str, bool]:
    rows = dict(conn.execute(
        f"SELECT key, value FROM user_settings WHERE user_id=? AND key IN ({', '.join('?' for _ in SETTINGS)})",
        (user_id, *SETTINGS),
    ).fetchall())
    return {key: rows.get(key) == "on" for key in SETTINGS}


def update_settings(conn: sqlite3.Connection, changes: dict[str, Any], *, user_id: str) -> dict[str, bool]:
    unknown = set(changes) - set(SETTINGS)
    if unknown:
        raise ValueError(f"Unknown automation settings: {', '.join(sorted(unknown))}")
    with conn:
        for key, value in changes.items():
            conn.execute(
                """
                INSERT INTO user_settings(user_id, key, value, updated_at) VALUES(?, ?, ?, ?)
                ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (user_id, key, "on" if value else "off", utc_now()),
            )
    return settings(conn, user_id=user_id)


# --- Bounce recovery ----------------------------------------------------------------


def _latest(conn: sqlite3.Connection, target_id: str, user_id: str, event_type: str) -> datetime | None:
    row = conn.execute(
        "SELECT MAX(created_at) FROM outreach_events WHERE target_id=? AND user_id=? AND event_type=?",
        (target_id, user_id, event_type),
    ).fetchone()
    return datetime.fromisoformat(row[0]) if row and row[0] else None


def recovery_due(conn: sqlite3.Connection, *, user_id: str) -> list[str]:
    """Companies whose contact bounced and that have not been searched again since."""
    due = []
    for item in list_targets(conn, user_id=user_id):
        if not item["contact_bounced"] or item["sent_at"] or item["status"] not in {"not_started", "drafted"}:
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
) -> dict[str, Any]:
    """Search the company's site again and apply the best contact that has not bounced.

    Recorded as a contact_recovery event either way, so each bounce is searched once.
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
    if not current["contact_bounced"]:
        # The student picked someone while the search ran; theirs stands.
        detail = "You chose a new contact while the app was looking."
        choice = None
    elif choice:
        apply_choice(conn, target_id, choice, user_id=user_id)
        cc = f", Cc {choice['cc']['email']}" if choice.get("cc") else ""
        detail = f"Chose {choice['to']['email']}{cc} ({choice['basis'].replace('_', ' ')}). Review the draft, then send it again."
    elif not target["website"]:
        detail = "No website on record, so there was nowhere to look. Add another contact by hand."
    else:
        detail = f"Found no other address on the company's site. Add one by hand, or try Find people.{note}"
    with conn:
        _log(conn, target_id, user_id, RECOVERY_EVENT, detail=detail)
    return {"target_id": target_id, "company": target["company"], "to": choice["to"]["email"] if choice else None, "detail": detail}


# --- Automatic drafts --------------------------------------------------------------------


def draft_due(conn: sqlite3.Connection, *, user_id: str, now: datetime | None = None) -> list[str]:
    """Companies ready for a first draft: a contact that has not bounced, a location, no draft, nothing sent."""
    now = now or datetime.now(timezone.utc)
    due = []
    for item in list_targets(conn, user_id=user_id):
        if item["email_body"] or item["sent_at"] or item["status"] not in {"not_started", "drafted"}:
            continue
        if not item["contact_email"] or item["contact_bounced"] or item["cc_bounced"] or not item["location_verified"]:
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
) -> dict[str, Any]:
    from .outreach_drafting import generate_draft  # imported here: drafting pulls in the model providers

    try:
        target = generate_draft(conn, target_id, user_id=user_id, provider_factory=provider_factory, provider=draft_provider)
    except (ValueError, RuntimeError) as exc:
        with conn:
            _log(conn, target_id, user_id, AUTO_DRAFT_FAILED, detail=f"{exc}"[:500])
        return {"target_id": target_id, "drafted": False, "error": str(exc)[:500]}
    return {"target_id": target_id, "company": target["company"], "drafted": True}


# --- In the background ---------------------------------------------------------------------


class AutomationWorker:
    """Runs each student's switched-on automation on one background thread.

    A pass first sends every scheduled email that is due (for any student:
    turning the switch off does not strand one already scheduled), then
    recovers every bounced contact that is due, then writes at most one draft,
    so a slow model call never holds the others up for long.
    """

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
        interval_seconds: float = 60.0,
    ) -> None:
        self.platform_target = platform_target
        self._gmail_client_factory = gmail_client_factory
        self._fetcher_factory = fetcher_factory
        self._renderer_factory = renderer_factory
        self._verifier_factory = verifier_factory
        self._provider_factory = provider_factory
        self._draft_provider = draft_provider
        self._contact_delay = contact_delay
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> dict[str, Any]:
        report: dict[str, Any] = {"sent": [], "recovered": [], "drafted": []}
        with closing(connect_product(self.platform_target)) as conn:
            if self._gmail_client_factory is not None:
                from .outreach_schedule import run_due_sends  # imported here: it pulls in the Gmail send path

                report["sent"] = run_due_sends(conn, client_factory=self._gmail_client_factory)
            users = [row[0] for row in conn.execute(
                f"SELECT DISTINCT user_id FROM user_settings WHERE value='on' AND key IN ({', '.join('?' for _ in SETTINGS)})",
                tuple(SETTINGS),
            ).fetchall()]
            for user_id in users:
                switched = settings(conn, user_id=user_id)
                if switched["bounce_recovery"]:
                    due = recovery_due(conn, user_id=user_id)
                    if due:
                        with ExitStack() as stack:
                            fetcher = stack.enter_context(self._fetcher_factory())
                            renderer = self._renderer_factory()
                            renderer = stack.enter_context(renderer) if renderer is not None else None
                            verifier = self._verifier_factory()
                            verifier = stack.enter_context(verifier) if verifier is not None else None
                            for target_id in due:
                                report["recovered"].append(recover_contact(
                                    conn, target_id, user_id=user_id, fetcher=fetcher, renderer=renderer,
                                    verifier=verifier, contact_delay=self._contact_delay,
                                ))
                if switched["auto_drafts"] and self._provider_factory is not None and not report["drafted"]:
                    due = draft_due(conn, user_id=user_id)
                    if due:
                        report["drafted"].append(auto_draft(
                            conn, due[0], user_id=user_id, provider_factory=self._provider_factory, draft_provider=self._draft_provider,
                        ))
        return report

    def wake(self) -> None:
        self._wake.set()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="outreach-automation", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:  # the thread must outlive any one bad pass
                LOGGER.exception("Outreach automation pass failed")
            self._wake.wait(self._interval)
            self._wake.clear()
