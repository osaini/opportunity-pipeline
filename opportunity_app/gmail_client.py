"""What every Gmail reader and writer shares and that needs no database: constants, errors, answers read, look spacing.

A leaf module (standard library and httpx, nothing else of this package): the
reply, delivery, send, label and job-mail watchers and the agent mailbox reader
all import from here, and none of them has to load the others, or the token and
health machinery of ``outreach_gmail``, to know what a Gmail answer means.

The errors. ``GmailAuthError``: the connection is missing, revoked or for the
wrong account. ``GmailThrottled``: Gmail asked the app to slow down; a
TransportError, so a caller that reads httpx.HTTPError as "could not reach
Gmail" holds and tries again, and none asks for a reconnect (test it before
httpx.HTTPError in an except ladder). ``GmailNeedsReadScope``: Gmail refused a
read because the connection predates the read scope. ``GmailUnreadable``: Gmail
answered a read with an error, so what it would have shown is unknown. Each
module keeps its own except ladder and its own side effects (forgetting a look,
recording a failure), since they differ on purpose.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable

import httpx

PROVIDER = "gmail_drafts"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
THROTTLE_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "RESOURCE_EXHAUSTED"})
# Gmail's own servers failing for a moment: held back like a rate limit, never taken as a refusal.
SERVER_ERRORS = frozenset({500, 502, 503, 504})

ClientFactory = Callable[[], httpx.Client]


class GmailAuthError(RuntimeError):
    """The Gmail connection is missing, revoked, or for the wrong account."""


class GmailThrottled(httpx.TransportError):
    """Gmail asked the app to slow down, so a read waits until ``until``; Gmail did nothing.

    A TransportError, so every caller that reads httpx.HTTPError as "could not
    reach Gmail" holds and tries again later, and none asks for a reconnect.
    """

    def __init__(self, message: str, until: datetime | None = None):
        super().__init__(message)
        self.until = until


class GmailNeedsReadScope(Exception):
    """Gmail refused a read: the connection predates the read scope."""


class GmailUnreadable(Exception):
    """Gmail answered a read with an error, so what it would have shown is unknown."""


def default_client_factory() -> httpx.Client:
    return httpx.Client(timeout=30, follow_redirects=False)


def connection_state(row: Any) -> str:
    """"connected", "not_connected" (no connection, or the student disconnected it) or "needs_reconnect" (any other status).

    ``row`` is a connector_accounts row, or None. What a watcher reports, and
    what the student is told to do, when it finds the connection not working.
    """
    if not row or row["status"] == "disconnected":
        return "not_connected"
    return "connected" if row["status"] == "connected" else "needs_reconnect"


def granted_scopes(scopes_json: Any) -> list[str]:
    """The scopes a connection was granted, from its scopes_json column: [] when the column cannot be read or is not a list."""
    try:
        granted = json.loads(scopes_json or "[]")
    except (TypeError, ValueError):
        return []
    return [str(scope) for scope in granted] if isinstance(granted, list) else []


def can_read_mail(scopes: Iterable[str]) -> bool:
    """Whether granted scopes let the app read mail: gmail.readonly, or gmail.modify, which reads too."""
    return READ_SCOPE in scopes or MODIFY_SCOPE in scopes


def error_reasons(response: httpx.Response) -> list[Any]:
    """What a Gmail error answer names: its error.status, then each error.errors[].reason. [] when it is not that shape."""
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return []
    if not isinstance(error, dict):
        return []
    named = [error.get("status")]
    if isinstance(error.get("errors"), list):
        named += [item.get("reason") for item in error["errors"] if isinstance(item, dict)]
    return named


def is_throttle(response: httpx.Response) -> bool:
    """A 429, or a 403 whose error names a rate limit or quota. An answer that is not JSON is not one."""
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    return any(isinstance(value, str) and value in THROTTLE_REASONS for value in error_reasons(response))


class LookSchedule:
    """When each item was last looked at in Gmail, so a page that loads often does not ask Gmail every time.

    Kept in memory (a restart means one early look), per student and item. An
    item is due when it has not been looked at, or when ``interval`` for how long
    it has been waiting has passed since the last look; ``take_due`` marks what
    it returns as looked at now, and ``forget`` takes the mark back, so a look
    that failed is made again on the next check rather than an interval later.

    ``last`` and ``lock`` are the owning module's own dict and lock, so the same
    objects stay reachable under its name (tests clear them). ``key`` reads an
    item's identity (a Gmail message or draft id), ``started`` the time its wait
    began, and ``forget_key`` is applied to the key on ``forget`` only: the send
    watcher's drafts are remembered under the id as stored and forgotten under
    str() of it, a mismatch that is harmless while the id is always a string and
    that is kept as it was.
    """

    def __init__(
        self,
        last: dict[tuple[str, Any], datetime],
        lock: threading.Lock,
        *,
        interval: Callable[[timedelta], timedelta],
        key: Callable[[dict[str, Any]], Any],
        started: Callable[[dict[str, Any]], datetime],
        forget_key: Callable[[Any], Any] = lambda key: key,
    ):
        self.last, self.lock = last, lock
        self.interval, self.key, self.started, self.forget_key = interval, key, started, forget_key

    def take_due(self, user_id: str, items: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
        due = []
        with self.lock:
            for item in items:
                key = (user_id, self.key(item))
                last = self.last.get(key)
                if last is None or now - last >= self.interval(now - self.started(item)):
                    self.last[key] = now
                    due.append(item)
        return due

    def forget(self, user_id: str, items: list[dict[str, Any]]) -> None:
        with self.lock:
            for item in items:
                self.last.pop((user_id, self.forget_key(self.key(item))), None)
