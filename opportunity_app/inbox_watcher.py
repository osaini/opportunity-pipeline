"""The inbox watcher: Gmail checked on a background thread, so a reply or a bounce is caught with the page closed.

Each step stands alone: one that fails is recorded in automation_health and the
next still runs. In order: sends, deliveries, replies, then outreach_labels (a
Gmail label on the thread of every confirmed reply), then application mail when
the student turned it on. The checks themselves live in outreach_inbox,
outreach_delivery, outreach_gmail_sends, outreach_labels and application_inbox.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import application_inbox, automation, outreach_labels
from .background import PollingWorker, discard_open_transaction, record_health_quietly, step_error
from .outreach_delivery import check_deliveries
from .outreach_gmail import PROVIDER, ClientFactory, _connector, gmail_notices, persist_gmail_health
from .outreach_inbox import OnReply, capture_replies
from .schema import connect_product, utc_now
from .typesafe_decisions import DecisionClient
from .user_time import user_timezone

LOGGER = logging.getLogger(__name__)

# What a check's state means, as automation_health records it. Never an address or a message's words.
STEP_ERRORS = {
    "needs_reconnect": "Gmail needs reconnecting",
    "not_connected": "Gmail is not connected",
    "unreachable": "Gmail could not be reached",
    "throttled": "Gmail asked the app to slow down",
    "needs_label_permission": (
        "Reconnect Gmail and tick the permission Google lists as reading, composing and sending, "
        "so the app can label your outreach threads (the emails you send and their replies)"
    ),
    "wrong_account": "Gmail is connected as a different account from your outreach address; reconnect with that address",
    "label_refused": "Gmail would not create a label with that name; choose another in Outreach settings",
    "message_errors": "Some job emails could not be read and were set aside",
    "database_busy": "The database was busy, so the job-email check stopped; it tries again next time",
}
RECONNECT_ERROR = "Gmail needs reconnecting"
CONNECTION = "inbox.connection"


def _save_gmail_health(conn: sqlite3.Connection, user_id: str) -> None:
    """Write the Gmail connection's health that memory holds (outreach_gmail.persist_gmail_health).

    Called only between the watcher's steps, where nothing of the watcher's is
    pending: every step commits its own writes, and a step that failed was
    rolled back (discard_open_transaction). What can still be open is the
    read-only transaction psycopg starts on PostgreSQL for a step's first
    SELECT, which makes every Gmail health write inside the steps wait. Ending
    it here commits nothing but reads; on SQLite no transaction is open here.
    """
    try:
        if getattr(conn, "in_transaction", False):
            conn.commit()
        persist_gmail_health(conn, user_id)
    except Exception:  # noqa: BLE001 - the pass goes on to the next step and the next student
        discard_open_transaction(conn)
        LOGGER.warning("Could not save the Gmail connection's health", exc_info=True)


def _moment(stamp: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _local(conn: sqlite3.Connection, user_id: str, stamp: str) -> str:
    """A stored UTC time as the student reads it: "Sat, Sep 26 at 3:14 PM"."""
    moment = _moment(stamp)
    if moment is None:
        return str(stamp)
    local = user_timezone(conn, user_id).to_local(moment)
    return f"{local:%a, %b} {local.day} at {f'{local:%I:%M %p}'.lstrip('0')}"


def _record_connection(conn: sqlite3.Connection, user_id: str, status: str, updated_at: str) -> None:
    """The inbox.connection health row, from the connector's status.

    Connected is ok. A connection the student disconnected on purpose is not a
    problem, so it is recorded as ok with the state "disconnected", once, and
    never as "needs reconnecting". Only a connection in 'error' is an error,
    and it says since when: the earliest break seen while it stays broken, so a
    pass every few minutes neither moves "since" nor the error's own time.
    """
    try:
        existing = conn.execute(
            "SELECT last_error, detail_json FROM automation_health WHERE user_id=? AND component=?", (user_id, CONNECTION),
        ).fetchone()
        before = json.loads(existing["detail_json"] or "{}") if existing else {}
    except Exception:  # noqa: BLE001 - unread, the row is simply written again
        LOGGER.warning("Could not read the inbox connection's health", exc_info=True)
        discard_open_transaction(conn)
        existing, before = None, {}
    if not isinstance(before, dict):
        before = {}
    if status == "connected":
        record_health_quietly(conn, user_id, CONNECTION, ok=True, detail={"state": "connected"})
        return
    if status == "disconnected":
        if before.get("state") != "disconnected":
            record_health_quietly(conn, user_id, CONNECTION, ok=True, detail={"state": "disconnected"})
        return
    since = str(updated_at or utc_now())
    earlier, now_seen = _moment(before.get("since")), _moment(since)
    if before.get("state") == "error" and earlier is not None and (now_seen is None or earlier < now_seen):
        since = str(before["since"])
    error = f"{RECONNECT_ERROR} (since {_local(conn, user_id, since)})"
    if existing is not None and before.get("state") == "error" and before.get("since") == since and existing["last_error"] == error:
        return  # already recorded: last_error_at stays when it was first seen
    record_health_quietly(conn, user_id, CONNECTION, ok=False, error=error, detail={"state": "error", "since": since})


class InboxWatcher(PollingWorker):
    """Checks every connected student's Gmail for bounces and replies on a background thread."""

    thread_name = "inbox-watcher"
    failure_message = "Inbox watcher pass failed"
    logger = LOGGER

    def __init__(
        self,
        platform_target: Path | str,
        *,
        client_factory: ClientFactory,
        decisions_for: Callable[[sqlite3.Connection, str], DecisionClient | None],
        on_reply: OnReply | None = None,
        interval_seconds: float = 180.0,
    ) -> None:
        self.platform_target = platform_target
        self._client_factory = client_factory
        self._decisions_for = decisions_for
        self._on_reply = on_reply
        super().__init__(interval_seconds)

    def run_once(self) -> None:
        """One pass over every student's Gmail connection, whatever its state.

        A broken or disconnected connection is only noted (a health row, and a
        notice when it is broken); nothing is asked of Gmail. A connected one
        runs each check on its own: a check that fails or raises is recorded and
        the next one runs. Between the steps, and at the end of each student,
        the Gmail health that memory holds is saved (_save_gmail_health), so a
        rate limit or a success seen inside a step reaches the row on
        PostgreSQL too, where it could not be written mid-step.
        """
        with closing(connect_product(self.platform_target)) as conn:
            rows = [(row[0], row[1], row[2]) for row in conn.execute(
                "SELECT user_id, status, updated_at FROM connector_accounts WHERE provider=? ORDER BY user_id", (PROVIDER,)
            ).fetchall()]
            for user_id, status, updated_at in rows:
                if status == "connected":
                    self._check(conn, user_id)
                else:
                    _record_connection(conn, user_id, status, updated_at)
                self._notices(conn, user_id)
                _save_gmail_health(conn, user_id)

    def _check(self, conn: sqlite3.Connection, user_id: str) -> None:
        # Looked up on every pass, not bound at import: a test patches outreach_gmail_sends.capture_gmail_sends, and
        # importing it here also keeps the scheduler out of the watcher's import.
        from .outreach_gmail_sends import capture_gmail_sends

        factory = self._client_factory
        steps: list[tuple[str, Callable[[], dict[str, Any]]]] = [
            # A draft sent from Gmail first, so its bounce and replies are watched in the same pass.
            ("inbox.sends", lambda: capture_gmail_sends(conn, user_id=user_id, client_factory=factory)),
            ("inbox.deliveries", lambda: check_deliveries(conn, user_id=user_id, client_factory=factory)),
            ("inbox.replies", lambda: capture_replies(
                conn, user_id=user_id, client_factory=factory,
                decisions=self._decisions_for(conn, user_id), on_reply=self._on_reply,
            )),
            # The label goes on a confirmed reply's thread as soon as the reply is captured (a no-op when off or paused).
            (outreach_labels.HEALTH_COMPONENT, lambda: outreach_labels.label_replies(
                conn, user_id=user_id, client_factory=factory,
            )),
        ]
        # Job-system mail after the replies, so a reply outreach owns is never read as one.
        # Only when the student turned it on (or into shadow); off, its cursor is forgotten.
        try:
            switch = automation.mode(conn, user_id, application_inbox.FEATURE)
        except Exception:  # noqa: BLE001 - the other steps still run
            discard_open_transaction(conn)
            LOGGER.warning("Could not read the application mail switch", exc_info=True)
            # Not knowing is not "off": the cursor is left alone, and the next pass asks again.
            switch = None
        if switch not in (None, "off"):
            steps.append((application_inbox.HEALTH_COMPONENT, lambda: application_inbox.run_pass(
                conn, user_id=user_id, client_factory=factory, decisions=self._decisions_for(conn, user_id),
            )))
        elif switch == "off":
            application_inbox.note_off(conn, user_id)
        for component, step in steps:
            try:
                outcome = step() or {}
                state = str(outcome.get("state", "ok"))
            except Exception as exc:  # noqa: BLE001 - one step failing never stops the others
                LOGGER.warning("Inbox step %s failed", component, exc_info=True)
                discard_open_transaction(conn)
                record_health_quietly(conn, user_id, component, ok=False, error=step_error(exc))
            else:
                detail = outcome.get("detail") if isinstance(outcome.get("detail"), dict) else None
                if outcome.get("skipped"):
                    pass  # it ran less than its interval ago; its last outcome stands
                elif state == "ok":
                    record_health_quietly(conn, user_id, component, ok=True, detail=detail)
                else:
                    error = STEP_ERRORS.get(state, f"Gmail check stopped: {state}")
                    record_health_quietly(conn, user_id, component, ok=False, error=error, detail=detail)
            _save_gmail_health(conn, user_id)
        # A 401 during the checks can leave the connection broken (or the student may have disconnected meanwhile).
        row = _connector(conn, user_id)
        if row is None:
            return
        _record_connection(conn, user_id, row["status"], row["updated_at"])

    def _notices(self, conn: sqlite3.Connection, user_id: str) -> None:
        try:
            gmail_notices(conn, user_id)
        except Exception:  # noqa: BLE001 - the next student is still checked
            discard_open_transaction(conn)
            LOGGER.warning("Could not leave Gmail connection notices", exc_info=True)

    def _run_pass(self) -> None:
        self.run_once()

    def _loop(self) -> None:
        # The first pass soon after start, to catch what arrived while the app was off. Unlike the other workers it
        # waits first, and on the stop flag, so stop() ends the wait; there is no wake.
        wait = min(20.0, self._interval)
        while not self._stop.wait(wait):
            try:
                self.run_once()
            except Exception:  # the thread must outlive any one bad pass
                LOGGER.exception(self.failure_message)
            wait = self._interval
