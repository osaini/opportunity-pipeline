"""Gmail drafts for approved outreach, with the student's attachment.

A compose URL cannot carry an attachment, so once the student connects Gmail
the app writes the approved draft into their Drafts folder through the Gmail
API instead. The student can also send an approved draft from the app, but
only by pressing Send and then confirming the recipient; nothing goes out
without both. The OAuth connection is the separate "gmail_drafts" connector, so
its gmail.compose scope (which covers drafts and sending) is never mixed with
the read-only monitoring connection. Its read scope, gmail.readonly, is for
finding bounces (outreach_delivery.py).

Rate limits. Gmail answering "slow down" (a 429, or a 403 naming a rate limit
or quota) is not a broken connection, so it never asks the student to
reconnect. The student's reads are held back for a while (60 s, doubling up to
30 minutes, or Gmail's own Retry-After) and a read in that time raises
GmailThrottled without asking Google; every caller treats it as "could not
reach Gmail" and tries again later. A send or a draft is never held back: the
student asked for it, and Gmail's answer decides.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import math
import mimetypes
import os
import re
import sqlite3
import threading
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator
from urllib.parse import quote
from uuid import uuid4

import httpx
from cryptography.fernet import Fernet, InvalidToken

from . import ROOT, automation
from .connections import OAUTH_PROVIDERS
from .outreach import (
    DRAFT_KINDS,
    UNSENT_STATUSES,
    DraftChangedError,
    _is_unique_violation,
    _log,
    get_target,
    missing_location_message,
    update_target,
)
from .outreach_drafting import sender_account
from .schema import utc_now
from .user_time import user_timezone

LOGGER = logging.getLogger(__name__)

PROVIDER = "gmail_drafts"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
READ_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
DRAFT_EVENT = "gmail_draft_created"
SENT_EVENT = "gmail_sent"
# Logged by outreach_delivery.record_bounce. Every send and draft before the
# latest bounce went to an address that failed, so none of them counts
# against sending the email again to a new contact.
BOUNCE_EVENT = "bounced"
# The status a successful send moves the target to, as "I sent it" would.
SENT_STATUS = {"initial": "sent", "follow_up": "followed_up"}
# The signature links are written as bare URLs in the plain text body. The HTML
# alternative turns each one into an anchor so Gmail shows it as a live link in
# the compose window instead of flat text. Trailing sentence punctuation is left
# outside the link.
_URL = re.compile(r"""https?://[^\s<>"']+""")
_URL_TAIL = ".,;:!?)]}'\""

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


def default_client_factory() -> httpx.Client:
    return httpx.Client(timeout=30, follow_redirects=False)


def attachment_path() -> Path | None:
    """PIPELINE_OUTREACH_ATTACHMENT, resolved against the project root when relative."""
    value = os.environ.get("PIPELINE_OUTREACH_ATTACHMENT", "").strip().strip('"')
    if not value:
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else ROOT / path


def attachment_problem(path: Path | None) -> str:
    if path is None:
        return ""
    if not path.is_file():
        return f"The attachment {path.name} was not found at {path}"
    if path.stat().st_size > MAX_ATTACHMENT_BYTES:
        return f"The attachment {path.name} is larger than 10 MB"
    return ""


def _connector(conn: sqlite3.Connection, user_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM connector_accounts WHERE user_id=? AND provider=?", (user_id, PROVIDER)).fetchone()


def gmail_drafts_status(conn: sqlite3.Connection, *, user_id: str) -> dict[str, Any]:
    """What the Outreach tab needs to offer Connect Gmail or Create Gmail draft."""
    config = OAUTH_PROVIDERS[PROVIDER]
    configured = all(os.environ.get(name, "").strip() for name in (config["client_id_env"], config["client_secret_env"], "PIPELINE_CONNECTION_KEY"))
    row = _connector(conn, user_id)
    path = attachment_path()
    try:
        granted = json.loads(row["scopes_json"] or "[]") if row else []
    except (TypeError, ValueError):
        granted = []
    return {
        "configured": configured,
        "connected": bool(configured and row and row["status"] == "connected"),
        "needs_reconnect": bool(row and row["status"] == "error"),
        # A connection made before the app asked to read mail sends fine but
        # cannot see bounces until it is reconnected.
        "bounce_check": bool(configured and row and row["status"] == "connected" and READ_SCOPE in granted),
        "account": sender_account(),
        "attachment": path.name if path else "",
        "attachment_problem": attachment_problem(path),
    }


def _fernet() -> Fernet:
    try:
        return Fernet(os.environ.get("PIPELINE_CONNECTION_KEY", "").encode())
    except Exception as exc:
        raise GmailAuthError("PIPELINE_CONNECTION_KEY must be a valid Fernet key") from exc


RENEW_REFUSED = "Google refused to renew the connection"


def _mark_error(conn: sqlite3.Connection, user_id: str) -> None:
    """The connection needs the student to reconnect. Called only after a 401."""
    with conn:
        conn.execute(
            "UPDATE connector_accounts SET status='error', last_error=?, updated_at=? WHERE user_id=? AND provider=?",
            (RENEW_REFUSED, utc_now(), user_id, PROVIDER),
        )


# --- Rate limits ---------------------------------------------------------------
#
# Memory is the source of truth within the process: _BACKOFF says, per student,
# until when reads are held back and how many throttles came in a row (reset to
# 0 by any success). connector_accounts.backoff_until mirrors it for the health
# banner and for the next start, written only when no transaction is open, so a
# health write never commits someone else's half-done work.

THROTTLE_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded", "RESOURCE_EXHAUSTED"})
BACKOFF_FIRST = timedelta(seconds=60)
BACKOFF_CAP = timedelta(minutes=30)
# A read that works records last_ok_at at most this often, so polling does not write constantly.
OK_WRITE_EVERY = timedelta(minutes=5)
_NEVER = datetime(1970, 1, 1, tzinfo=timezone.utc)
_BACKOFF: dict[str, tuple[datetime, int]] = {}
_OK_WRITTEN: dict[str, datetime] = {}
_BACKOFF_LOCK = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _in_transaction(conn: sqlite3.Connection) -> bool:
    return bool(getattr(conn, "in_transaction", False))


def _stamp_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def backoff_until(user_id: str, *, now: datetime | None = None) -> datetime | None:
    """Until when this student's Gmail reads are held back, or None when they are not."""
    now = now or _now()
    with _BACKOFF_LOCK:
        until, _level = _BACKOFF.get(user_id, (_NEVER, 0))
    return until if until > now else None


def _is_throttle(response: httpx.Response) -> bool:
    """A 429, or a 403 whose error names a rate limit or quota. An answer that is not JSON is not one."""
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return False
    if not isinstance(error, dict):
        return False
    named = [error.get("status")]
    if isinstance(error.get("errors"), list):
        named += [item.get("reason") for item in error["errors"] if isinstance(item, dict)]
    return any(isinstance(value, str) and value in THROTTLE_REASONS for value in named)


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


def _note_throttle(user_id: str, response: httpx.Response, now: datetime) -> datetime:
    """Hold this student's reads back: Retry-After when Gmail gave one, else 60 s doubling per throttle in a row."""
    with _BACKOFF_LOCK:
        _until, level = _BACKOFF.get(user_id, (_NEVER, 0))
        level += 1
        wait = _retry_after(response, now)
        if wait is None:
            wait = min(BACKOFF_FIRST * (2 ** min(level - 1, 16)), BACKOFF_CAP)
        until = now + wait
        _BACKOFF[user_id] = (until, level)
    return until


def _store_backoff(conn: sqlite3.Connection, user_id: str, until: datetime) -> bool:
    """Mirror the hold in connector_accounts, only when no transaction is open. True when written."""
    if _in_transaction(conn):
        return False
    try:
        with conn:
            conn.execute(
                "UPDATE connector_accounts SET backoff_until=? WHERE user_id=? AND provider=?",
                (until.isoformat(timespec="seconds"), user_id, PROVIDER),
            )
    except Exception:  # noqa: BLE001 - a health record must never turn a Gmail answer into an error
        LOGGER.warning("Could not record Gmail's rate limit", exc_info=True)
        return False
    return True


def _throttled(until: datetime) -> GmailThrottled:
    return GmailThrottled(f"Gmail asked the app to slow down; reads resume after {until.isoformat(timespec='seconds')}", until)


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
    if response.status_code == 429:
        # Too many renewals is Google asking to wait, not refusing the grant.
        until = _note_throttle(user_id, response, _now())
        _store_backoff(conn, user_id, until)
        raise _throttled(until)
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


class _Gmail:
    """Authorized Gmail calls that renew the access token once on a 401, and that slow down when Gmail asks.

    ``wait_out_backoff`` False lets reads through while the student's reads are
    held back: the checks inside a send or draft the student asked for.
    """

    def __init__(self, conn: sqlite3.Connection, client: httpx.Client, user_id: str, *, wait_out_backoff: bool = True):
        row = _connector(conn, user_id)
        if not row or row["status"] != "connected":
            raise GmailAuthError("Connect Gmail before creating a draft" if not row or row["status"] == "disconnected" else "Reconnect Gmail before creating a draft")
        self.conn, self.client, self.user_id, self.row = conn, client, user_id, row
        self.wait_out_backoff = wait_out_backoff
        self.fernet = _fernet()
        try:
            self.token = self.fernet.decrypt(row["encrypted_access_token"].encode()).decode()
        except InvalidToken as exc:
            raise GmailAuthError("The stored Gmail connection cannot be decrypted; reconnect Gmail") from exc
        stored = row["backoff_until"] if "backoff_until" in row.keys() else None
        # Whether the row still carries a hold that a success must clear.
        self.backoff_stored = bool(stored)
        until = _stamp_time(stored)
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
        response = self._send(method, path, **kwargs)
        if response.status_code == 401:
            self.token = _refresh_access_token(self.conn, self.client, self.fernet, self.row, self.user_id)
            response = self._send(method, path, **kwargs)
        if response.status_code == 401:
            _mark_error(self.conn, self.user_id)
            raise GmailAuthError("Gmail rejected the connection; reconnect Gmail")
        if _is_throttle(response):
            until = _note_throttle(self.user_id, response, _now())
            if _store_backoff(self.conn, self.user_id, until):
                self.backoff_stored = True
            if reading:
                raise _throttled(until)
            # A send or a draft: the caller's claim logic reads Gmail's refusal as it always has.
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
            # A hold on the row is cleared at once, whenever it was last written.
            clearing = self.backoff_stored or level > 0
            last = _OK_WRITTEN.get(self.user_id)
            if not (clearing or last is None or now - last >= OK_WRITE_EVERY) or _in_transaction(self.conn):
                return
            _OK_WRITTEN[self.user_id] = now
        try:
            with self.conn:
                self.conn.execute(
                    "UPDATE connector_accounts SET last_ok_at=?, last_error='', backoff_until=NULL WHERE user_id=? AND provider=?",
                    (now.isoformat(timespec="microseconds"), self.user_id, PROVIDER),
                )
            self.backoff_stored = False
        except Exception:  # noqa: BLE001 - a health record must never turn a Gmail answer into an error
            LOGGER.warning("Could not record that Gmail answered", exc_info=True)


def html_body(body: str) -> str:
    """The plain text draft as HTML, with every URL in it a real link.

    Nothing is added or reworded: the approved words are escaped, line breaks
    are kept, and only the URLs already in the body become anchors.
    """
    def anchor(match: re.Match[str]) -> str:
        url = match.group(0)
        tail = ""
        while url and url[-1] in _URL_TAIL:
            url, tail = url[:-1], url[-1] + tail
        if not url:
            return match.group(0)
        return f'<a href="{url}">{url}</a>{tail}'

    lines = [_URL.sub(anchor, html.escape(line)) for line in body.splitlines()]
    return "<html><body><div>" + "<br>".join(lines) + "</div></body></html>"


def _mime(account: str, to: str, subject: str, body: str, attachment: Path | None, cc: str = "") -> str:
    message = EmailMessage()
    if account:
        message["From"] = account
    message["To"] = to
    if cc:
        message["Cc"] = cc
    message["Subject"] = subject
    message.set_content(body)
    message.add_alternative(html_body(body), subtype="html")
    if attachment is not None:
        kind = mimetypes.guess_type(attachment.name)[0] or "application/octet-stream"
        maintype, subtype = kind.split("/", 1)
        message.add_attachment(attachment.read_bytes(), maintype=maintype, subtype=subtype, filename=attachment.name)
    return base64.urlsafe_b64encode(message.as_bytes()).decode()


def draft_url(account: str, message_id: str) -> str:
    authuser = quote(account) if account else "0"
    return f"https://mail.google.com/mail/?authuser={authuser}#drafts?compose={quote(message_id)}"


def _previous_draft(
    conn: sqlite3.Connection,
    target_id: str,
    user_id: str,
    kind: str,
    fingerprint: str,
    attachment: str,
    attachment_sha256: str,
) -> dict[str, Any] | None:
    rows = conn.execute(
        "SELECT detail FROM outreach_events WHERE target_id=? AND user_id=? AND event_type=? ORDER BY created_at DESC",
        (target_id, user_id, DRAFT_EVENT),
    ).fetchall()
    for row in rows:
        try:
            detail = json.loads(row["detail"])
        except (TypeError, ValueError):
            continue
        if (
            detail.get("kind") == kind
            and detail.get("fingerprint") == fingerprint
            and detail.get("attachment", "") == attachment
            and detail.get("attachment_sha256", "") == attachment_sha256
        ):
            return detail
    return None


class _Approved:
    """An approved draft that has passed every check for leaving the app."""

    def __init__(self, conn: sqlite3.Connection, target_id: str, user_id: str, kind: str, action: str):
        if kind not in DRAFT_KINDS:
            raise ValueError("kind must be initial or follow_up")
        subject_field, body_field, status_field = DRAFT_KINDS[kind]
        target = get_target(conn, target_id, user_id=user_id)
        if target[status_field] != "approved":
            raise ValueError(f"Approve this draft before {action}")
        # Approval can predate the check that placed the company near the student.
        if kind == "initial" and target["draft_location"]["missing"]:
            raise ValueError(missing_location_message(target))
        if not target["contact_email"]:
            raise ValueError(f"Add a contact email before {action}")
        path = attachment_path()
        problem = attachment_problem(path)
        if problem:
            raise ValueError(problem)
        self.target, self.kind, self.path = target, kind, path
        self.subject, self.body = target[subject_field], target[body_field]
        self.fingerprint = target["draft_fingerprint"] if kind == "initial" else target["follow_up_fingerprint"]
        self.attachment = path.name if path else ""
        self.attachment_sha256 = hashlib.sha256(path.read_bytes()).hexdigest() if path else ""

    def raw(self, account: str) -> str:
        return _mime(account, self.target["contact_email"], self.subject, self.body, self.path, cc=self.target["contact_cc"])

    def live_draft(self, conn: sqlite3.Connection, gmail: _Gmail, user_id: str) -> dict[str, Any] | None:
        """The Gmail draft already made of these exact words, if it is still in Drafts."""
        previous = _previous_draft(
            conn, self.target["id"], user_id, self.kind, self.fingerprint, self.attachment, self.attachment_sha256
        )
        if not previous:
            return None
        return previous if _draft_still_there(gmail, previous["draft_id"]) else None


def _require_account(gmail: _Gmail, account: str) -> None:
    if not account:
        return
    profile = gmail.request("GET", "/profile")
    connected_as = str(profile.json().get("emailAddress", "")) if profile.status_code == 200 else ""
    if connected_as.lower() != account.lower():
        raise GmailAuthError(f"Gmail is connected as {connected_as or 'an unknown account'}, not {account}; reconnect with {account}")


def _draft_still_there(gmail: _Gmail, draft_id: str) -> bool:
    """Whether a recorded draft is still in Drafts. Gone means sent or deleted in Gmail.

    Any other answer is not read as gone: that would let a second copy be made
    or sent while the first may still be sitting in Drafts.
    """
    response = gmail.request("GET", f"/drafts/{quote(draft_id, safe='')}", params={"format": "minimal"})
    if response.status_code == 200:
        return True
    if response.status_code == 404:
        return False
    raise RuntimeError(f"Could not check your Gmail drafts (HTTP {response.status_code}). Nothing was sent")


# --- Each email goes out at most once -----------------------------------------
#
# A send or a draft first inserts a row in outreach_send_claims for its target
# and kind; the primary key makes that the lock. The row is released only when
# Gmail certainly did nothing, kept as 'sent' when Gmail confirmed the send, and
# kept as 'unconfirmed' when Gmail may or may not have acted. An unconfirmed
# row, like a draft that vanished from Drafts, asks the student to look in
# Gmail before anything else is sent, since gmail.compose cannot read Sent.

# The server runs as one process, so a claim from another instance was left by
# a process that has since died or been replaced. Its request may still have
# been finishing its one Gmail call, hence the grace period before it counts as
# stale.
SERVER_INSTANCE = uuid4().hex
FOREIGN_CLAIM_GRACE = timedelta(minutes=5)
IN_PROGRESS = "This email is already being sent or written to Gmail. Wait a moment, then reload"
_SEND_UNCERTAIN = (
    "Gmail may already have sent this email. Check your Gmail Sent folder: "
    "if it went out, use \"I sent it\"; if not, press Send again."
)
_DRAFT_UNCERTAIN = (
    "Gmail may have made a draft of this email that the app did not record. Check your Gmail Drafts and Sent "
    "folders: delete any draft of it, or send it from Gmail and use \"I sent it\". If nothing went out, press Send again."
)
_DRAFT_VANISHED = (
    "A Gmail draft of this email is no longer in your Drafts, so it may have been sent from Gmail. "
    "Check your Sent folder: if it went out, use \"I sent it\"; if not, press Send again."
)
# Failures that certainly never reached Gmail, or that Gmail refused outright.
# GmailThrottled is raised only where Gmail did nothing (a renewal it asked to wait on).
_NOTHING_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, GmailAuthError, GmailThrottled)


class SendConflictError(Exception):
    """Another request holds this email, or a Gmail draft of it is still live."""


class SendNeedsCheckError(Exception):
    """Gmail may already have this email; the student must look before it is sent.

    ``check`` names exactly what the student is vouching for. Sending again
    with it acknowledges these reasons and no others.
    """

    def __init__(self, message: str, check: str):
        super().__init__(message)
        self.check = check


class SendUnconfirmedError(RuntimeError):
    """Gmail did not confirm a send it may have carried out."""


def _claim(conn: sqlite3.Connection, target_id: str, user_id: str, kind: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM outreach_send_claims WHERE target_id=? AND user_id=? AND kind=?", (target_id, user_id, kind)
    ).fetchone()


def _claim_held(row: sqlite3.Row) -> bool:
    """Whether a request may still be working under this claim."""
    if row["state"] not in {"drafting", "sending"}:
        return False
    if row["instance"] == SERVER_INSTANCE:
        # A claim this process left behind (its request ended without being
        # able to settle it) is as uncertain as one from a dead process.
        return row["token"] in _RUNNING
    age = datetime.now(timezone.utc) - datetime.fromisoformat(row["claimed_at"])
    return age < FOREIGN_CLAIM_GRACE


def _superseded(conn: sqlite3.Connection, row: sqlite3.Row, target_id: str, user_id: str) -> bool:
    """Whether a bounce came after this claim, so what it guarded went to an address that failed."""
    bounce = last_bounce(conn, target_id, user_id)
    return bounce is not None and datetime.fromisoformat(row["claimed_at"]) < bounce


def _claim_reason(row: sqlite3.Row) -> str:
    return _DRAFT_UNCERTAIN if row["action"] == "draft" else _SEND_UNCERTAIN


# Tokens of the claims whose requests are running in this process.
_RUNNING: set[str] = set()


@contextmanager
def _claimed(
    conn: sqlite3.Connection, target_id: str, user_id: str, kind: str, action: str,
    revalidate: Callable[[], Any], *, stale_token: str = "",
) -> Iterator[tuple[str, Any]]:
    """Hold the claim for the body; the checks are re-run on a fresh read as it is taken.

    The insert comes first so SQLite holds the write lock while the target is
    re-read, and the checks raising rolls the claim back. The body settles the
    claim; one it could not settle is treated as uncertain once the body ends.
    """
    token = uuid4().hex
    _RUNNING.add(token)
    try:
        try:
            with conn:
                if stale_token and not conn.execute(
                    "DELETE FROM outreach_send_claims WHERE target_id=? AND kind=? AND token=?", (target_id, kind, stale_token)
                ).rowcount:
                    raise SendConflictError(IN_PROGRESS)
                conn.execute(
                    """
                    INSERT INTO outreach_send_claims(target_id, user_id, kind, token, state, action, instance, claimed_at)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (target_id, user_id, kind, token, "sending" if action == "send" else "drafting", action, SERVER_INSTANCE, utc_now()),
                )
                result = revalidate()
        except Exception as exc:
            if _is_unique_violation(exc):
                raise SendConflictError(IN_PROGRESS) from exc
            raise
        yield token, result
    finally:
        _RUNNING.discard(token)


def _settle_claim(conn: sqlite3.Connection, target_id: str, kind: str, token: str, state: str | None) -> None:
    """Move our own claim to ``state``, or drop it when ``state`` is None. Never raises."""
    try:
        with conn:
            if state is None:
                conn.execute("DELETE FROM outreach_send_claims WHERE target_id=? AND kind=? AND token=?", (target_id, kind, token))
            else:
                conn.execute("UPDATE outreach_send_claims SET state=? WHERE target_id=? AND kind=? AND token=?", (state, target_id, kind, token))
    except Exception:
        # Left as it was, the claim still blocks another send, which is the safe side.
        pass


def last_bounce(conn: sqlite3.Connection, target_id: str, user_id: str) -> datetime | None:
    """When this target's email last bounced, or None."""
    row = conn.execute(
        "SELECT MAX(created_at) FROM outreach_events WHERE target_id=? AND user_id=? AND event_type=?",
        (target_id, user_id, BOUNCE_EVENT),
    ).fetchone()
    return datetime.fromisoformat(row[0]) if row and row[0] else None


def _since(stamp: str, bounce: datetime | None) -> bool:
    return bounce is None or datetime.fromisoformat(stamp) > bounce


def _already_sent(conn: sqlite3.Connection, target_id: str, user_id: str, kind: str) -> bool:
    bounce = last_bounce(conn, target_id, user_id)
    rows = conn.execute(
        "SELECT detail, created_at FROM outreach_events WHERE target_id=? AND user_id=? AND event_type=?",
        (target_id, user_id, SENT_EVENT),
    ).fetchall()
    for row in rows:
        if not _since(row["created_at"], bounce):
            continue
        try:
            if json.loads(row["detail"]).get("kind") == kind:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _draft_events(conn: sqlite3.Connection, target_id: str, user_id: str, kind: str) -> list[tuple[str, dict[str, Any]]]:
    """Every Gmail draft the app made of this kind since the last bounce, whatever version of the words it held."""
    bounce = last_bounce(conn, target_id, user_id)
    rows = conn.execute(
        "SELECT id, detail, created_at FROM outreach_events WHERE target_id=? AND user_id=? AND event_type=? ORDER BY created_at DESC",
        (target_id, user_id, DRAFT_EVENT),
    ).fetchall()
    events = []
    for row in rows:
        if not _since(row["created_at"], bounce):
            continue
        try:
            detail = json.loads(row["detail"])
        except (TypeError, ValueError):
            continue
        if isinstance(detail, dict) and detail.get("kind") == kind and detail.get("draft_id"):
            events.append((str(row["id"]), detail))
    return events


def _refuse_sent(target: dict[str, Any], kind: str, sending: bool) -> None:
    """The status checks shared by sending and drafting, so "I sent it" also stops a new copy."""
    if kind == "initial" and (target["sent_at"] or target["status"] not in UNSENT_STATUSES):
        what = "the first email is not sent again" if sending else "no new draft of the first email is made"
        raise ValueError(f"{target['company']} is already marked {target['status'].replace('_', ' ')}, so {what}")
    if kind == "follow_up" and sending and target["status"] != "sent":
        raise ValueError("A follow-up goes out only after the first email, while the company is marked sent")
    if kind == "follow_up" and not sending and target["status"] == "followed_up":
        raise ValueError(f"{target['company']} is already marked followed up, so no new draft of the follow-up is made")


def _approved_for(
    conn: sqlite3.Connection, target_id: str, user_id: str, kind: str, *, sending: bool, fingerprint: str | None = None,
) -> _Approved:
    approved = _Approved(conn, target_id, user_id, kind, "sending it" if sending else "creating it in Gmail")
    if approved.target["contact_bounced"]:
        raise ValueError(f"Email to {approved.target['contact_email']} bounced, so nothing more goes there. Choose another contact first")
    if approved.target["cc_bounced"]:
        raise ValueError(f"Email to the Cc {approved.target['contact_cc']} bounced. Remove it or choose another first")
    if fingerprint is not None and fingerprint != approved.fingerprint:
        raise DraftChangedError("This draft changed after you confirmed it. Review it, then send again")
    if _already_sent(conn, target_id, user_id, kind):
        raise ValueError("This email was already sent from Gmail")
    _refuse_sent(approved.target, kind, sending)
    return approved


def _post_under_claim(
    conn: sqlite3.Connection, gmail: _Gmail, target_id: str, kind: str, token: str,
    path: str, payload: dict[str, Any], *, refused: str, uncertain: str,
) -> dict[str, Any]:
    """The one Gmail call that acts, with our claim settled by what Gmail may have done.

    A refusal, or a call that never reached Gmail, releases the claim. A 5xx or
    a call that got no answer leaves Gmail's outcome unknown, so the claim stays
    as 'unconfirmed' and the student is asked to look before anything else goes out.
    """
    try:
        response = gmail.request("POST", path, json=payload)
    except _NOTHING_SENT:
        _settle_claim(conn, target_id, kind, token, None)
        raise
    except httpx.HTTPError as exc:
        _settle_claim(conn, target_id, kind, token, "unconfirmed")
        raise SendUnconfirmedError(f"Gmail did not answer, so {uncertain}") from exc
    except BaseException:
        _settle_claim(conn, target_id, kind, token, "unconfirmed")
        raise
    if response.status_code == 200:
        try:
            return response.json()
        except BaseException:
            _settle_claim(conn, target_id, kind, token, "unconfirmed")
            raise
    if 400 <= response.status_code < 500:
        _settle_claim(conn, target_id, kind, token, None)
        raise RuntimeError(f"{refused} (HTTP {response.status_code}). Nothing was sent")
    _settle_claim(conn, target_id, kind, token, "unconfirmed")
    raise SendUnconfirmedError(f"Gmail did not confirm it (HTTP {response.status_code}), so {uncertain}")


def create_gmail_draft(
    conn: sqlite3.Connection,
    target_id: str,
    *,
    user_id: str,
    kind: str = "initial",
    client_factory: ClientFactory = default_client_factory,
) -> dict[str, Any]:
    """Create a Gmail draft of an approved outreach email, with the configured attachment.

    Clicking again for the same approved words reopens the draft already made,
    unless it has since been sent or deleted in Gmail. Once the email was sent,
    or while a send of it is under way or unconfirmed, no draft is made: a new
    copy in Drafts could be sent a second time.
    """
    _approved_for(conn, target_id, user_id, kind, sending=False)
    stale_token = ""
    existing = _claim(conn, target_id, user_id, kind)
    if existing is not None:
        if _claim_held(existing):
            raise SendConflictError(IN_PROGRESS)
        if _superseded(conn, existing, target_id, user_id):
            stale_token = existing["token"]
        elif existing["state"] == "sent":
            raise ValueError("This email was already sent from Gmail")
        else:
            raise SendConflictError(f"{_claim_reason(existing)} Until then no new draft is made.")
    account = sender_account()
    revalidate = lambda: _approved_for(conn, target_id, user_id, kind, sending=False)  # noqa: E731
    with _claimed(conn, target_id, user_id, kind, "draft", revalidate, stale_token=stale_token) as (token, approved):
        try:
            client = client_factory()
        except BaseException:
            _settle_claim(conn, target_id, kind, token, None)
            raise
        with client:
            try:
                # The student asked for this draft, so its checks go to Gmail even while background reads wait.
                gmail = _Gmail(conn, client, user_id, wait_out_backoff=False)
                _require_account(gmail, account)
                previous = approved.live_draft(conn, gmail, user_id)
                raw = None if previous else approved.raw(account)
            except BaseException:
                _settle_claim(conn, target_id, kind, token, None)
                raise
            if previous:
                _settle_claim(conn, target_id, kind, token, None)
                public = {key: value for key, value in previous.items() if key != "attachment_sha256"}
                return {**public, "url": draft_url(account, previous["message_id"]), "reused": True}
            created = _post_under_claim(
                conn, gmail, target_id, kind, token, "/drafts", {"message": {"raw": raw}},
                refused="Gmail did not create the draft",
                uncertain="the draft may be in your Gmail Drafts. Check Drafts before trying again",
            )
            try:
                detail = {
                    "kind": kind, "fingerprint": approved.fingerprint, "attachment": approved.attachment,
                    "attachment_sha256": approved.attachment_sha256,
                    "draft_id": str(created["id"]), "message_id": str(created["message"]["id"]),
                    # Lets a send made in Gmail itself be matched to this draft (outreach_gmail_sends).
                    "thread_id": str(created["message"].get("threadId", "")),
                }
                with conn:
                    _log(conn, target_id, user_id, DRAFT_EVENT, detail=json.dumps(detail, sort_keys=True))
                    conn.execute("DELETE FROM outreach_send_claims WHERE target_id=? AND kind=? AND token=?", (target_id, kind, token))
            except BaseException:
                # The draft exists but is not recorded, so the next send asks first.
                _settle_claim(conn, target_id, kind, token, "unconfirmed")
                raise
    public = {key: value for key, value in detail.items() if key != "attachment_sha256"}
    return {**public, "url": draft_url(account, detail["message_id"]), "reused": False}


def send_gmail_message(
    conn: sqlite3.Connection,
    target_id: str,
    *,
    user_id: str,
    kind: str = "initial",
    fingerprint: str,
    sent_folder_check: str | None = None,
    client_factory: ClientFactory = default_client_factory,
) -> dict[str, Any]:
    """Send an approved draft from the student's Gmail after they confirm it.

    The fingerprint is the approved draft the student confirmed; if the words
    changed since, nothing is sent. Each draft kind goes out at most once, and a
    successful send moves the target to Sent (or Followed up) exactly as
    "I sent it" would.

    Only the app's own approved message is sent, never a Gmail draft: a draft
    can be edited in Gmail at any moment. While a draft of this email is in
    Drafts the student sends that one from Gmail, or deletes it first. When
    Gmail may already have the email (an unconfirmed earlier send, or a draft
    that has left Drafts) nothing is sent until the student has looked and
    sends again with ``sent_folder_check``.
    """
    _approved_for(conn, target_id, user_id, kind, sending=True, fingerprint=fingerprint)
    reasons: dict[str, str] = {}
    stale_token = ""
    existing = _claim(conn, target_id, user_id, kind)
    if existing is not None:
        if _claim_held(existing):
            raise SendConflictError(IN_PROGRESS)
        stale_token = existing["token"]
        if _superseded(conn, existing, target_id, user_id):
            pass
        elif existing["state"] == "sent":
            raise ValueError("This email was already sent from Gmail")
        else:
            reasons[f"claim:{stale_token}"] = _claim_reason(existing)
    account = sender_account()

    with client_factory() as client:
        # The student asked for this send (or scheduled it, and the scheduler's fresh look
        # waits out a slowdown first), so its checks go to Gmail even while background reads wait.
        gmail = _Gmail(conn, client, user_id, wait_out_backoff=False)
        # Everything read from Gmail is read before the claim, so the claim is
        # held only across the one call that sends.
        _require_account(gmail, account)
        drafts = _draft_events(conn, target_id, user_id, kind)
        for _event_id, detail in drafts:
            if _draft_still_there(gmail, detail["draft_id"]):
                raise SendConflictError(
                    "This email is in your Gmail Drafts. Send it from Gmail and use \"I sent it\", "
                    "or delete that draft to send from here"
                )
            reasons[f"draft:{detail['draft_id']}"] = _DRAFT_VANISHED
        if reasons:
            check = hashlib.sha256(json.dumps(sorted(reasons)).encode()).hexdigest()[:32]
            if sent_folder_check != check:
                raise SendNeedsCheckError(" ".join(dict.fromkeys(reasons.values())), check)
        seen = [event_id for event_id, _detail in drafts]

        def revalidate() -> tuple[_Approved, str]:
            fresh = _approved_for(conn, target_id, user_id, kind, sending=True, fingerprint=fingerprint)
            if [event_id for event_id, _detail in _draft_events(conn, target_id, user_id, kind)] != seen:
                raise SendConflictError("A Gmail draft of this email was just made. Send it from Gmail, or delete it and send again")
            return fresh, fresh.raw(account)

        with _claimed(conn, target_id, user_id, kind, "send", revalidate, stale_token=stale_token) as (token, (approved, raw)):
            sent = _post_under_claim(
                conn, gmail, target_id, kind, token, "/messages/send", {"raw": raw},
                refused="Gmail did not send the email",
                uncertain="it may have gone out. Check your Gmail Sent folder before sending again",
            )
            # What Gmail sent is recorded before anything else, so it is never lost.
            try:
                detail = {
                    "kind": kind, "fingerprint": approved.fingerprint, "attachment": approved.attachment,
                    "to": approved.target["contact_email"], "cc": approved.target["contact_cc"],
                    "message_id": str(sent.get("id", "")), "thread_id": str(sent.get("threadId", "")),
                }
                with conn:
                    conn.execute("UPDATE outreach_send_claims SET state='sent' WHERE target_id=? AND kind=? AND token=?", (target_id, kind, token))
                    _log(conn, target_id, user_id, SENT_EVENT, detail=json.dumps(detail, sort_keys=True))
            except BaseException:
                _settle_claim(conn, target_id, kind, token, "sent")
                raise
    try:
        updated = update_target(conn, target_id, {"status": SENT_STATUS[kind]}, user_id=user_id)
    except Exception:
        # The email went out and is recorded; only the status is behind, and "I sent it" catches it up.
        current = get_target(conn, target_id, user_id=user_id)
        return {**detail, "account": account, "status": current["status"], "follow_up_at": current["follow_up_at"], "marked": False}
    return {**detail, "account": account, "status": updated["status"], "follow_up_at": updated["follow_up_at"], "marked": True}


# --- Notices about the connection ---------------------------------------------------


def gmail_notices(conn: sqlite3.Connection, user_id: str, *, now: datetime | None = None) -> list[str]:
    """Leave the student a notice when Gmail will likely need reconnecting soon, or already does.

    Each is left once: the expiry notice once per grant, the reconnect notice
    once per time the connection broke. Returns the event keys of the notices
    that are new. automation.notice opens its own transaction, so this is never
    called inside one. The expiry is an estimate (automation.gmail_health), and
    the notice says "likely".
    """
    health = automation.gmail_health(conn, user_id, now=now)
    new = []
    if health["expiring_soon"]:
        local = user_timezone(conn, user_id).to_local(datetime.fromisoformat(health["likely_expires_at"]))
        when = f"{local:%a, %b} {local.day} at {f'{local:%I:%M %p}'.lstrip('0')}"
        key = f"gmail-expiring:{health['token_granted_at']}"
        if automation.notice(
            conn, user_id, event_key=key, level="warning", title="Gmail will likely need reconnecting soon",
            body=f"Open Outreach and click Reconnect Gmail before {when} so reply and bounce checks keep running.",
        ):
            new.append(key)
    row = _connector(conn, user_id)
    if row is not None and row["status"] == "error":
        key = f"gmail-expired:{row['updated_at']}"
        if automation.notice(
            conn, user_id, event_key=key, level="problem", title="Gmail needs reconnecting",
            body="Reply and bounce checks have stopped until you reconnect Gmail in Outreach.",
        ):
            new.append(key)
    return new
