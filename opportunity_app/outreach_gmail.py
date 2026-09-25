"""Gmail drafts for approved outreach, with the student's attachment.

A compose URL cannot carry an attachment, so once the student connects Gmail
the app writes the approved draft into their Drafts folder through the Gmail
API instead. The student can also send an approved draft from the app, but
only by pressing Send and then confirming the recipient; nothing goes out
without both. The OAuth connection is the separate "gmail_drafts" connector, so
its gmail.compose scope (which covers drafts and sending) is never mixed with
the read-only monitoring connection.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import mimetypes
import os
import re
import sqlite3
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import httpx
from cryptography.fernet import Fernet, InvalidToken

from . import ROOT
from .connections import OAUTH_PROVIDERS
from .outreach import DRAFT_KINDS, DraftChangedError, _log, get_target, missing_location_message, update_target
from .outreach_drafting import sender_account
from .schema import utc_now

PROVIDER = "gmail_drafts"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
DRAFT_EVENT = "gmail_draft_created"
SENT_EVENT = "gmail_sent"
# The status a successful send moves the target to, as "I sent it" would.
SENT_STATUS = {"initial": "sent", "follow_up": "followed_up"}
# Statuses the first email can still go out from.
UNSENT_STATUSES = {"not_started", "drafted", "paused"}
# The signature links are written as bare URLs in the plain text body. The HTML
# alternative turns each one into an anchor so Gmail shows it as a live link in
# the compose window instead of flat text. Trailing sentence punctuation is left
# outside the link.
_URL = re.compile(r"""https?://[^\s<>"']+""")
_URL_TAIL = ".,;:!?)]}'\""

ClientFactory = Callable[[], httpx.Client]


class GmailAuthError(RuntimeError):
    """The Gmail connection is missing, revoked, or for the wrong account."""


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
    return {
        "configured": configured,
        "connected": bool(configured and row and row["status"] == "connected"),
        "needs_reconnect": bool(row and row["status"] == "error"),
        "account": sender_account(),
        "attachment": path.name if path else "",
        "attachment_problem": attachment_problem(path),
    }


def _fernet() -> Fernet:
    try:
        return Fernet(os.environ.get("PIPELINE_CONNECTION_KEY", "").encode())
    except Exception as exc:
        raise GmailAuthError("PIPELINE_CONNECTION_KEY must be a valid Fernet key") from exc


def _mark_error(conn: sqlite3.Connection, user_id: str) -> None:
    with conn:
        conn.execute("UPDATE connector_accounts SET status='error', updated_at=? WHERE user_id=? AND provider=?", (utc_now(), user_id, PROVIDER))


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
    """Authorized Gmail calls that renew the access token once on a 401."""

    def __init__(self, conn: sqlite3.Connection, client: httpx.Client, user_id: str):
        row = _connector(conn, user_id)
        if not row or row["status"] != "connected":
            raise GmailAuthError("Connect Gmail before creating a draft" if not row or row["status"] == "disconnected" else "Reconnect Gmail before creating a draft")
        self.conn, self.client, self.user_id, self.row = conn, client, user_id, row
        self.fernet = _fernet()
        try:
            self.token = self.fernet.decrypt(row["encrypted_access_token"].encode()).decode()
        except InvalidToken as exc:
            raise GmailAuthError("The stored Gmail connection cannot be decrypted; reconnect Gmail") from exc

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        response = self.client.request(method, f"{GMAIL_API}{path}", headers={"Authorization": f"Bearer {self.token}"}, **kwargs)
        if response.status_code == 401:
            self.token = _refresh_access_token(self.conn, self.client, self.fernet, self.row, self.user_id)
            response = self.client.request(method, f"{GMAIL_API}{path}", headers={"Authorization": f"Bearer {self.token}"}, **kwargs)
        if response.status_code == 401:
            _mark_error(self.conn, self.user_id)
            raise GmailAuthError("Gmail rejected the connection; reconnect Gmail")
        return response


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
        existing = gmail.request("GET", f"/drafts/{quote(previous['draft_id'], safe='')}", params={"format": "minimal"})
        return previous if existing.status_code == 200 else None


def _require_account(gmail: _Gmail, account: str) -> None:
    if not account:
        return
    profile = gmail.request("GET", "/profile")
    connected_as = str(profile.json().get("emailAddress", "")) if profile.status_code == 200 else ""
    if connected_as.lower() != account.lower():
        raise GmailAuthError(f"Gmail is connected as {connected_as or 'an unknown account'}, not {account}; reconnect with {account}")


def create_gmail_draft(
    conn: sqlite3.Connection,
    target_id: str,
    *,
    user_id: str,
    kind: str = "initial",
    client_factory: ClientFactory = default_client_factory,
) -> dict[str, Any]:
    """Put an approved draft, with the configured attachment, in the student's Gmail Drafts.

    Clicking again for the same approved words reopens the draft already made,
    unless it has since been sent or deleted in Gmail.
    """
    approved = _Approved(conn, target_id, user_id, kind, "creating it in Gmail")
    account = sender_account()

    with client_factory() as client:
        gmail = _Gmail(conn, client, user_id)
        _require_account(gmail, account)
        previous = approved.live_draft(conn, gmail, user_id)
        if previous:
            public = {key: value for key, value in previous.items() if key != "attachment_sha256"}
            return {**public, "url": draft_url(account, previous["message_id"]), "reused": True}
        response = gmail.request("POST", "/drafts", json={"message": {"raw": approved.raw(account)}})
        if response.status_code != 200:
            raise RuntimeError(f"Gmail did not create the draft (HTTP {response.status_code})")
        created = response.json()
    detail = {
        "kind": kind, "fingerprint": approved.fingerprint, "attachment": approved.attachment,
        "attachment_sha256": approved.attachment_sha256,
        "draft_id": str(created["id"]), "message_id": str(created["message"]["id"]),
    }
    with conn:
        _log(conn, target_id, user_id, DRAFT_EVENT, detail=json.dumps(detail, sort_keys=True))
    public = {key: value for key, value in detail.items() if key != "attachment_sha256"}
    return {**public, "url": draft_url(account, detail["message_id"]), "reused": False}


def _already_sent(conn: sqlite3.Connection, target_id: str, user_id: str, kind: str) -> bool:
    rows = conn.execute(
        "SELECT detail FROM outreach_events WHERE target_id=? AND user_id=? AND event_type=?",
        (target_id, user_id, SENT_EVENT),
    ).fetchall()
    for row in rows:
        try:
            if json.loads(row["detail"]).get("kind") == kind:
                return True
        except (TypeError, ValueError):
            continue
    return False


def send_gmail_message(
    conn: sqlite3.Connection,
    target_id: str,
    *,
    user_id: str,
    kind: str = "initial",
    fingerprint: str,
    client_factory: ClientFactory = default_client_factory,
) -> dict[str, Any]:
    """Send an approved draft from the student's Gmail after they confirm it.

    The fingerprint is the approved draft the student confirmed; if the words
    changed since, nothing is sent. Each draft kind goes out at most once, and a
    successful send moves the target to Sent (or Followed up) exactly as
    "I sent it" would. If a Gmail draft of these approved words is still in
    Drafts, that draft is the one sent, so no stale copy is left behind to be
    sent a second time.
    """
    approved = _Approved(conn, target_id, user_id, kind, "sending it")
    target = approved.target
    if fingerprint != approved.fingerprint:
        raise DraftChangedError("This draft changed after you confirmed it. Review it, then send again")
    if _already_sent(conn, target_id, user_id, kind):
        raise ValueError("This email was already sent from Gmail")
    if kind == "initial" and (target["sent_at"] or target["status"] not in UNSENT_STATUSES):
        raise ValueError(f"{target['company']} is already marked {target['status'].replace('_', ' ')}, so the first email is not sent again")
    if kind == "follow_up" and target["status"] != "sent":
        raise ValueError("A follow-up goes out only after the first email, while the company is marked sent")
    account = sender_account()

    with client_factory() as client:
        gmail = _Gmail(conn, client, user_id)
        _require_account(gmail, account)
        previous = approved.live_draft(conn, gmail, user_id)
        if previous:
            response = gmail.request("POST", "/drafts/send", json={"id": previous["draft_id"]})
        else:
            response = gmail.request("POST", "/messages/send", json={"raw": approved.raw(account)})
        if response.status_code != 200:
            raise RuntimeError(f"Gmail did not send the email (HTTP {response.status_code}). Nothing was sent")
        sent = response.json()
    detail = {
        "kind": kind, "fingerprint": approved.fingerprint, "attachment": approved.attachment,
        "to": target["contact_email"], "cc": target["contact_cc"],
        "message_id": str(sent.get("id", "")), "thread_id": str(sent.get("threadId", "")),
    }
    with conn:
        _log(conn, target_id, user_id, SENT_EVENT, detail=json.dumps(detail, sort_keys=True))
    updated = update_target(conn, target_id, {"status": SENT_STATUS[kind]}, user_id=user_id)
    return {**detail, "account": account, "status": updated["status"], "follow_up_at": updated["follow_up_at"]}
