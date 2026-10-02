"""The student's Gmail connection: authorized calls, token renewal, rate limits and the connection's health.

``GmailClient`` makes the REST calls. It renews the access token once on a 401, holds the student's reads back while
Gmail asks the app to slow down, and keeps what this process knows of the connection (the hold, the last good call, the
last failure) in memory and mirrored to ``connector_accounts``. The send workflow (outreach_gmail), the reply, bounce,
label and application-mail watchers and the agent mailbox reader all call Gmail through it.

Rate limits. Gmail answering "slow down" (a 429, or a 403 naming a rate limit or quota) is not a broken connection, so it
never asks the student to reconnect. Neither is a passing server error (500, 502, 503, 504), from the Gmail API or from
Google's token endpoint. The student's reads are held back for a while (60 s, doubling up to 30 minutes, or Gmail's own
Retry-After) and a read in that time raises GmailThrottled without asking Google; every caller treats it as "could not
reach Gmail" and tries again later. A send or a draft is never held back: the student asked for it, and Gmail's answer
decides.
"""

from __future__ import annotations

import logging
import math
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from cryptography.fernet import Fernet, InvalidToken

from .connections import OAUTH_PROVIDERS
from .integrations.gmail_client import (
    GMAIL_API,
    PROVIDER,
    SERVER_ERRORS,
    GmailAuthError,
    GmailThrottled,
    connection_state,
    is_throttle,
)
from .core.timestamps import parse_app_instant, utc_now

LOGGER = logging.getLogger(__name__)


def connector_row(conn: sqlite3.Connection, user_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM connector_accounts WHERE user_id=? AND provider=?", (user_id, PROVIDER)).fetchone()


def _fernet() -> Fernet:
    try:
        return Fernet(os.environ.get("PIPELINE_CONNECTION_KEY", "").encode())
    except Exception as exc:
        raise GmailAuthError("PIPELINE_CONNECTION_KEY must be a valid Fernet key") from exc


RENEW_REFUSED = "Google refused to renew the connection"
# What connector_accounts.last_error says after a call that got no answer at
# all. Like every last_error, never an address or a message's words.
UNREACHABLE = "Gmail could not be reached"


def _mark_error(conn: sqlite3.Connection, user_id: str) -> None:
    """The connection needs the student to reconnect. Called only after a 401 or a refused renewal.

    The health columns are written with the status, from memory, so a later
    persist_gmail_health has nothing older to put back over RENEW_REFUSED.
    """
    _note_error(user_id, RENEW_REFUSED)
    version, values = _unsaved(user_id, force=True)
    with conn:
        conn.execute(
            "UPDATE connector_accounts SET status='error', updated_at=? WHERE user_id=? AND provider=?",
            (utc_now(), user_id, PROVIDER),
        )
        conn.execute(_MIRROR_SQL, (*values, user_id, PROVIDER))
    _saved(user_id, version, values)


# --- Rate limits, and the connection's health ------------------------------------
#
# Memory is the source of truth within the process. _BACKOFF says, per student,
# until when reads are held back and how many throttles came in a row (reset to
# 0 by any success). _HEALTH says when a call last worked and why the last one
# failed ('' once one works again).
#
# connector_accounts mirrors both (backoff_until, last_ok_at, last_error) for
# the health panel, the banner, and the next start. A change is written at once
# only when no transaction is open, so a health write never commits someone
# else's half-done work. Otherwise it waits for persist_gmail_health, which the
# inbox watcher calls between its steps, where nothing of its own is pending.
# That second path is what makes the mirror work on PostgreSQL: psycopg opens a
# transaction on the first statement, a read included, so in_transaction is True
# after any query and the write at once almost never happens there.

BACKOFF_FIRST = timedelta(seconds=60)
BACKOFF_CAP = timedelta(minutes=30)
# A read that works records last_ok_at at most this often, so polling does not write constantly.
OK_WRITE_EVERY = timedelta(minutes=5)
_NEVER = datetime(1970, 1, 1, tzinfo=timezone.utc)
_BACKOFF: dict[str, tuple[datetime, int]] = {}
_BACKOFF_LOCK = threading.Lock()


@dataclass
class _Health:
    """What this process knows of one student's Gmail connection; ahead of the row while ``saved < changed``."""

    ok_at: datetime | None = None
    error: str = ""
    # Bumped by every change the row should show; saved is the change it last showed.
    changed: int = 0
    saved: int = 0
    # The last last_ok_at written, so a working read writes at most every OK_WRITE_EVERY.
    ok_written: datetime | None = None


_HEALTH: dict[str, _Health] = {}
_MIRROR_SQL = (
    "UPDATE connector_accounts SET backoff_until=?, last_ok_at=COALESCE(?, last_ok_at), last_error=? "
    "WHERE user_id=? AND provider=?"
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _in_transaction(conn: sqlite3.Connection) -> bool:
    return bool(getattr(conn, "in_transaction", False))


def backoff_until(user_id: str, *, now: datetime | None = None) -> datetime | None:
    """Until when this student's Gmail reads are held back, or None when they are not."""
    now = now or _now()
    with _BACKOFF_LOCK:
        until, _level = _BACKOFF.get(user_id, (_NEVER, 0))
    return until if until > now else None


def _retry_after(response: httpx.Response, now: datetime) -> timedelta | None:
    """Gmail's Retry-After (seconds or a date), capped at BACKOFF_CAP, or None when it gave none that reads."""
    value = response.headers.get("Retry-After", "").strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return None
        if when is None:
            return None
        seconds = ((when if when.tzinfo else when.replace(tzinfo=timezone.utc)) - now).total_seconds()
    if not math.isfinite(seconds):
        return None
    return timedelta(seconds=min(max(seconds, 0.0), BACKOFF_CAP.total_seconds()))


def _note_error(user_id: str, why: str) -> None:
    """Remember why the last Gmail call failed, for the row's last_error."""
    with _BACKOFF_LOCK:
        health = _HEALTH.setdefault(user_id, _Health())
        health.error = why[:200]
        health.changed += 1


def _note_throttle(user_id: str, response: httpx.Response, now: datetime, why: str = "") -> datetime:
    """Hold this student's reads back: Retry-After when Gmail gave one, else 60 s doubling per throttle in a row."""
    with _BACKOFF_LOCK:
        _until, level = _BACKOFF.get(user_id, (_NEVER, 0))
        level += 1
        wait = _retry_after(response, now)
        if wait is None:
            wait = min(BACKOFF_FIRST * (2 ** min(level - 1, 16)), BACKOFF_CAP)
        until = now + wait
        _BACKOFF[user_id] = (until, level)
        health = _HEALTH.setdefault(user_id, _Health())
        health.error = (why or f"Gmail asked the app to slow down (HTTP {response.status_code})")[:200]
        health.changed += 1
    return until


def _unsaved(user_id: str, *, force: bool = False) -> tuple[int, tuple[Any, ...]]:
    """What memory holds for the row: (version, (backoff_until, last_ok_at, last_error)).

    Version 0 when the row already shows everything memory knows, unless ``force``.
    """
    now = _now()
    with _BACKOFF_LOCK:
        health = _HEALTH.setdefault(user_id, _Health())
        if health.saved >= health.changed and not force:
            return 0, ()
        until, _level = _BACKOFF.get(user_id, (_NEVER, 0))
        return health.changed, (
            until.isoformat(timespec="seconds") if until > now else None,
            health.ok_at.isoformat(timespec="microseconds") if health.ok_at else None,
            health.error,
        )


def _saved(user_id: str, version: int, values: tuple[Any, ...]) -> None:
    """The row now shows memory as of ``version``."""
    with _BACKOFF_LOCK:
        health = _HEALTH.setdefault(user_id, _Health())
        health.saved = max(health.saved, version)
        written = parse_app_instant(values[1]) if values else None
        if written is not None and (health.ok_written is None or written > health.ok_written):
            health.ok_written = written


def persist_gmail_health(conn: sqlite3.Connection, user_id: str) -> bool:
    """Write what memory knows of this student's Gmail connection to connector_accounts, in its own transaction.

    Only for a caller at a point where nothing of its own is uncommitted: the
    transaction this opens commits whatever is open on ``conn``. Writes nothing
    when the row already shows it all. True when written; a failure is logged,
    and the same change is written next time.
    """
    version, values = _unsaved(user_id)
    if not version:
        return False
    try:
        with conn:
            conn.execute(_MIRROR_SQL, (*values, user_id, PROVIDER))
    except Exception:  # noqa: BLE001 - a health record must never turn a Gmail answer into an error
        LOGGER.warning("Could not record the Gmail connection's health", exc_info=True)
        return False
    _saved(user_id, version, values)
    return True


def _save_now(conn: sqlite3.Connection, user_id: str) -> bool:
    """persist_gmail_health, only when no transaction is open on ``conn``. True when written."""
    return False if _in_transaction(conn) else persist_gmail_health(conn, user_id)


def _throttled(until: datetime, why: str = "Gmail asked the app to slow down") -> GmailThrottled:
    return GmailThrottled(f"{why}; reads resume after {until.isoformat(timespec='seconds')}", until)


def _refresh_access_token(conn: sqlite3.Connection, client: httpx.Client, fernet: Fernet, row: sqlite3.Row, user_id: str) -> str:
    config = OAUTH_PROVIDERS[PROVIDER]
    try:
        refresh_token = fernet.decrypt(row["encrypted_refresh_token"].encode()).decode() if row["encrypted_refresh_token"] else ""
    except InvalidToken as exc:
        raise GmailAuthError("The stored Gmail connection cannot be decrypted; reconnect Gmail") from exc
    if not refresh_token:
        _mark_error(conn, user_id)
        raise GmailAuthError("Google did not grant offline access; reconnect Gmail")
    response = client.post(config["token"], data={
        "grant_type": "refresh_token", "refresh_token": refresh_token,
        "client_id": os.environ.get(config["client_id_env"], ""), "client_secret": os.environ.get(config["client_secret_env"], ""),
    })
    if response.status_code == 429 or response.status_code >= 500:
        # Too many renewals is Google asking to wait, and a 5xx is its token
        # service failing for a moment. Neither refuses the grant, so neither
        # asks the student to reconnect: reads wait, as for a rate limit.
        why = ("Google asked the app to wait before renewing the connection" if response.status_code == 429
               else "Google could not renew the connection just now")
        until = _note_throttle(user_id, response, _now(), f"{why} (HTTP {response.status_code})")
        _save_now(conn, user_id)
        raise _throttled(until, why)
    access_token = response.json().get("access_token") if response.status_code == 200 else None
    if not access_token:
        _mark_error(conn, user_id)
        raise GmailAuthError("Google refused to renew the Gmail connection; reconnect Gmail")
    with conn:
        conn.execute(
            "UPDATE connector_accounts SET encrypted_access_token=?, updated_at=? WHERE user_id=? AND provider=?",
            (fernet.encrypt(access_token.encode()).decode(), utc_now(), user_id, PROVIDER),
        )
    return str(access_token)


class GmailClient:
    """Authorized Gmail calls that renew the access token once on a 401, and that slow down when Gmail asks.

    ``wait_out_backoff`` False lets reads through while the student's reads are
    held back: the checks inside a send or draft the student asked for.
    """

    def __init__(self, conn: sqlite3.Connection, client: httpx.Client, user_id: str, *, wait_out_backoff: bool = True):
        row = connector_row(conn, user_id)
        state = connection_state(row)
        if state != "connected":
            raise GmailAuthError("Connect Gmail before creating a draft" if state == "not_connected" else "Reconnect Gmail before creating a draft")
        self.conn, self.client, self.user_id, self.row = conn, client, user_id, row
        self.wait_out_backoff = wait_out_backoff
        self.fernet = _fernet()
        try:
            self.token = self.fernet.decrypt(row["encrypted_access_token"].encode()).decode()
        except InvalidToken as exc:
            raise GmailAuthError("The stored Gmail connection cannot be decrypted; reconnect Gmail") from exc
        columns = row.keys()
        stored = row["backoff_until"] if "backoff_until" in columns else None
        # Whether the row still carries a hold or an error that a success must clear.
        self.row_flagged = bool(stored) or bool(row["last_error"] if "last_error" in columns else "")
        until = parse_app_instant(stored)
        if until is not None and until > _now():
            with _BACKOFF_LOCK:
                # After a restart memory is empty; a hold this process already knows about stands.
                _BACKOFF.setdefault(user_id, (until, 1))

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return self.client.request(method, f"{GMAIL_API}{path}", headers={"Authorization": f"Bearer {self.token}"}, **kwargs)

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        reading = method.upper() == "GET"
        if reading and self.wait_out_backoff:
            until = backoff_until(self.user_id)
            if until is not None:
                raise _throttled(until)
        try:
            response = self._send(method, path, **kwargs)
            if response.status_code == 401:
                self.token = _refresh_access_token(self.conn, self.client, self.fernet, self.row, self.user_id)
                response = self._send(method, path, **kwargs)
        except GmailThrottled:
            raise  # a renewal Google asked to wait on, already noted
        except httpx.TransportError:
            _note_error(self.user_id, UNREACHABLE)
            _save_now(self.conn, self.user_id)
            raise
        if response.status_code == 401:
            _mark_error(self.conn, self.user_id)
            raise GmailAuthError("Gmail rejected the connection; reconnect Gmail")
        throttle = is_throttle(response)
        if throttle or response.status_code in SERVER_ERRORS:
            why = "Gmail asked the app to slow down" if throttle else "Gmail had a temporary problem"
            until = _note_throttle(self.user_id, response, _now(), f"{why} (HTTP {response.status_code})")
            if _save_now(self.conn, self.user_id):
                self.row_flagged = True
            if reading:
                raise _throttled(until, why)
            # A send or a draft: the caller's claim logic reads Gmail's answer as it always has
            # (a refusal releases the claim; a 5xx leaves the email unconfirmed).
            return response
        if 200 <= response.status_code < 300:
            self._note_ok()
        return response

    def _note_ok(self) -> None:
        """A call worked: reads are no longer held back, and the connection's health says so (at most every 5 minutes)."""
        now = _now()
        with _BACKOFF_LOCK:
            _until, level = _BACKOFF.get(self.user_id, (_NEVER, 0))
            _BACKOFF[self.user_id] = (_NEVER, 0)
            health = _HEALTH.setdefault(self.user_id, _Health())
            # A hold or an error on the row, or a change not written yet, is cleared at once.
            clearing = self.row_flagged or level > 0 or bool(health.error) or health.saved < health.changed
            health.ok_at, health.error = now, ""
            if not (clearing or health.ok_written is None or now - health.ok_written >= OK_WRITE_EVERY):
                return
            health.changed += 1
        if _save_now(self.conn, self.user_id):
            self.row_flagged = False
