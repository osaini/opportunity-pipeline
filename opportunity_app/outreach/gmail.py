"""Gmail drafts for approved outreach, with the student's attachment.

A compose URL cannot carry an attachment, so once the student connects Gmail
the app writes the approved draft into their Drafts folder through the Gmail
API instead. The student can also send an approved draft from the app, but
only by pressing Send and then confirming the recipient; nothing goes out
without both. The OAuth connection is the separate "gmail_drafts" connector, so
its gmail.compose scope (which covers drafts and sending) is never mixed with
the read-only monitoring connection. Its read scope, gmail.readonly, is for
finding bounces (outreach_delivery.py). Its gmail.modify scope is only for
adding the student's label to outreach threads, sent mail and replies (outreach_labels.py); the
app never uses it to remove a label, trash, archive or mark mail read.

The authorized REST client, its rate limits and its health are in gmail_connection; the once-only claim ledger is in
send_claims. This module is the outreach send workflow on top of both: drafts, sends, thank-yous and notices.
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
from email.utils import formataddr
from pathlib import Path
from datetime import datetime
from typing import Any
from urllib.parse import quote

import httpx

from .. import ROOT
from . import callbacks as outreach_callbacks
from ..automation import ledger as automation, health as automation_health
from ..mail.connections import OAUTH_PROVIDERS
from ..integrations.gmail_client import (
    MODIFY_SCOPE,
    PROVIDER,
    ClientFactory,
    GmailAuthError,
    GmailThrottled,
    can_read_mail,
    default_client_factory,
    granted_scopes,
)
from ..mail.gmail_connection import GmailClient, connector_row
from ..mail.message import URL_TAIL
from .targets import (
    DRAFT_KINDS,
    UNSENT_STATUSES,
    DraftChangedError,
    log_event,
    get_target,
    latest_event_stamp,
    update_target,
)
from .location import missing_location_message
from .config import ATTACHMENT_ENV, gmail_web_url, sender_account
from .label_name import label_name
from .send_claims import (
    IN_PROGRESS,
    SendConflictError,
    claim_reason,
    claimed_send,
    send_claim_held,
    send_claim_row,
    settle_send_claim,
)
from ..core.timestamps import utc_now
from ..core.user_time import user_timezone

MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
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

def attachment_path() -> Path | None:
    """PIPELINE_OUTREACH_ATTACHMENT, resolved against the project root when relative."""
    value = os.environ.get(ATTACHMENT_ENV, "").strip().strip('"')
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


def gmail_drafts_status(conn: sqlite3.Connection, *, user_id: str, now: datetime | None = None) -> dict[str, Any]:
    """What the Outreach tab needs to offer Connect Gmail, Reconnect Gmail, or Create Gmail draft.

    ``expiring_soon`` and ``likely_expires_at`` are automation_health.gmail_health's
    estimate of when Google will ask for the grant again, so the tab can offer
    Reconnect Gmail before reply and bounce checks stop rather than after.
    """
    config = OAUTH_PROVIDERS[PROVIDER]
    configured = all(os.environ.get(name, "").strip() for name in (config["client_id_env"], config["client_secret_env"], "PIPELINE_CONNECTION_KEY"))
    row = connector_row(conn, user_id)
    path = attachment_path()
    granted = granted_scopes(row["scopes_json"]) if row else []
    health = automation_health.gmail_health(conn, user_id, now=now)
    connected = bool(configured and row and row["status"] == "connected")
    account = sender_account()
    connected_as = str(row["account_email"] or "") if connected and "account_email" in row.keys() else ""
    return {
        "configured": configured,
        "connected": connected,
        "needs_reconnect": bool(row and row["status"] == "error"),
        # A connection made before the app asked to read mail sends fine but
        # cannot see bounces until it is reconnected. gmail.modify reads mail too.
        "bounce_check": bool(connected and can_read_mail(granted)),
        # Likewise a connection made before the reply label cannot label until it is reconnected.
        "label_check": bool(connected and MODIFY_SCOPE in granted),
        "label": label_name(conn, user_id),
        # The address the connection signed into, once known, and whether it is not the outreach address.
        "connected_as": connected_as,
        "wrong_account": bool(connected_as and account and connected_as.casefold() != account.casefold()),
        "account": account,
        "attachment": path.name if path else "",
        "attachment_problem": attachment_problem(path),
        # Only while connected (gmail_health), and only when Reconnect Gmail can work (configured).
        "expiring_soon": bool(configured and health.get("expiring_soon")),
        "likely_expires_at": health.get("likely_expires_at"),
    }


def html_body(body: str) -> str:
    """The plain text draft as HTML, with every URL in it a real link.

    Nothing is added or reworded: the approved words are escaped, line breaks
    are kept, and only the URLs already in the body become anchors.
    """
    def anchor(match: re.Match[str]) -> str:
        url = match.group(0)
        tail = ""
        while url and url[-1] in URL_TAIL:
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


def _sent_folder_check(reasons: dict[str, str]) -> str:
    """What the student vouches for by checking their Sent folder: a hash of exactly these reasons, echoed back to send again.

    The client keeps it between the two clicks, so the formula must never change.
    """
    return hashlib.sha256(json.dumps(sorted(reasons)).encode()).hexdigest()[:32]


def _public_draft(detail: dict[str, Any]) -> dict[str, Any]:
    """A recorded draft as the client sees it: without the attachment's hash."""
    return {key: value for key, value in detail.items() if key != "attachment_sha256"}


def draft_url(account: str, message_id: str) -> str:
    return gmail_web_url(f"drafts?compose={quote(message_id)}", account)


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

    def live_draft(self, conn: sqlite3.Connection, gmail: GmailClient, user_id: str) -> dict[str, Any] | None:
        """The Gmail draft already made of these exact words, if it is still in Drafts."""
        previous = _previous_draft(
            conn, self.target["id"], user_id, self.kind, self.fingerprint, self.attachment, self.attachment_sha256
        )
        if not previous:
            return None
        return previous if _draft_still_there(gmail, previous["draft_id"]) else None


def _require_account(gmail: GmailClient, account: str) -> None:
    if not account:
        return
    profile = gmail.request("GET", "/profile")
    connected_as = str(profile.json().get("emailAddress", "")) if profile.status_code == 200 else ""
    if connected_as.lower() != account.lower():
        raise GmailAuthError(f"Gmail is connected as {connected_as or 'an unknown account'}, not {account}; reconnect with {account}")


def _draft_still_there(gmail: GmailClient, draft_id: str) -> bool:
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


# --- Drafting and sending under a claim (send_claims.py holds the ledger) -----------

_DRAFT_VANISHED = (
    "A Gmail draft of this email is no longer in your Drafts, so it may have been sent from Gmail. "
    "Check your Sent folder: if it went out, use \"I sent it\"; if not, press Send again."
)
# Failures that certainly never reached Gmail, or that Gmail refused outright.
# GmailThrottled is raised only where Gmail did nothing (a renewal it asked to wait on).
_NOTHING_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout, GmailAuthError, GmailThrottled)


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


def _superseded(conn: sqlite3.Connection, row: sqlite3.Row, target_id: str, user_id: str) -> bool:
    """Whether a bounce came after this claim, so what it guarded went to an address that failed."""
    bounce = last_bounce(conn, target_id, user_id)
    return bounce is not None and datetime.fromisoformat(row["claimed_at"]) < bounce


def event_tie_order(conn: sqlite3.Connection, alias: str = "e") -> str:
    """The ORDER BY tail that settles events sharing a created_at, for a query that sorts the student's events ascending.

    Such a query used to sort in a temporary b-tree, which hands tied rows back newest-inserted first; reading
    through an index now would hand them back oldest-inserted first and change which of two tied drafts or sends a
    caller keeps. Naming the old order here keeps the answer the same whatever plan SQLite picks. PostgreSQL has no
    row id and never promised an order for ties, so it gets none.
    """
    return "" if getattr(conn, "backend", "sqlite") == "postgresql" else f", {alias}.rowid DESC"


def last_bounce(conn: sqlite3.Connection, target_id: str, user_id: str) -> datetime | None:
    """When this target's email last bounced, or None."""
    stamp = latest_event_stamp(conn, target_id, user_id, BOUNCE_EVENT)
    return datetime.fromisoformat(stamp) if stamp else None


def _since(stamp: str, bounce: datetime | None) -> bool:
    return bounce is None or datetime.fromisoformat(stamp) > bounce


_LOOK_UP = object()


def last_bounces(conn: sqlite3.Connection, user_id: str) -> dict[str, datetime]:
    """When each target's email last bounced, for every target that has bounced: last_bounce for all of them in one query."""
    return {
        str(row[0]): datetime.fromisoformat(row[1])
        for row in conn.execute(
            "SELECT target_id, MAX(created_at) FROM outreach_events WHERE user_id=? AND event_type=? GROUP BY target_id",
            (user_id, BOUNCE_EVENT),
        ).fetchall()
        if row[1]
    }


def _already_sent(conn: sqlite3.Connection, target_id: str, user_id: str, kind: str, *, bounce: Any = _LOOK_UP) -> bool:
    """Whether an email of this kind went out since the last bounce. ``bounce`` is that bounce (None for none) when the caller has it already."""
    if bounce is _LOOK_UP:
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
    conn: sqlite3.Connection, gmail: GmailClient, target_id: str, kind: str, token: str,
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
        settle_send_claim(conn, target_id, kind, token, None)
        raise
    except httpx.HTTPError as exc:
        settle_send_claim(conn, target_id, kind, token, "unconfirmed")
        raise SendUnconfirmedError(f"Gmail did not answer, so {uncertain}") from exc
    except BaseException:
        settle_send_claim(conn, target_id, kind, token, "unconfirmed")
        raise
    if response.status_code == 200:
        try:
            return response.json()
        except BaseException:
            settle_send_claim(conn, target_id, kind, token, "unconfirmed")
            raise
    if 400 <= response.status_code < 500:
        settle_send_claim(conn, target_id, kind, token, None)
        raise RuntimeError(f"{refused} (HTTP {response.status_code}). Nothing was sent")
    settle_send_claim(conn, target_id, kind, token, "unconfirmed")
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
    existing = send_claim_row(conn, target_id, user_id, kind)
    if existing is not None:
        if send_claim_held(existing):
            raise SendConflictError(IN_PROGRESS)
        if _superseded(conn, existing, target_id, user_id):
            stale_token = existing["token"]
        elif existing["state"] == "sent":
            raise ValueError("This email was already sent from Gmail")
        else:
            raise SendConflictError(f"{claim_reason(existing)} Until then no new draft is made.")
    account = sender_account()
    revalidate = lambda: _approved_for(conn, target_id, user_id, kind, sending=False)  # noqa: E731
    with claimed_send(conn, target_id, user_id, kind, "draft", revalidate, stale_token=stale_token) as (token, approved):
        try:
            client = client_factory()
        except BaseException:
            settle_send_claim(conn, target_id, kind, token, None)
            raise
        with client:
            try:
                # The student asked for this draft, so its checks go to Gmail even while background reads wait.
                gmail = GmailClient(conn, client, user_id, wait_out_backoff=False)
                _require_account(gmail, account)
                previous = approved.live_draft(conn, gmail, user_id)
                raw = None if previous else approved.raw(account)
            except BaseException:
                settle_send_claim(conn, target_id, kind, token, None)
                raise
            if previous:
                settle_send_claim(conn, target_id, kind, token, None)
                public = _public_draft(previous)
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
                    log_event(conn, target_id, user_id, DRAFT_EVENT, detail=json.dumps(detail, sort_keys=True))
                    conn.execute("DELETE FROM outreach_send_claims WHERE target_id=? AND kind=? AND token=?", (target_id, kind, token))
            except BaseException:
                # The draft exists but is not recorded, so the next send asks first.
                settle_send_claim(conn, target_id, kind, token, "unconfirmed")
                raise
    public = _public_draft(detail)
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
    existing = send_claim_row(conn, target_id, user_id, kind)
    if existing is not None:
        if send_claim_held(existing):
            raise SendConflictError(IN_PROGRESS)
        stale_token = existing["token"]
        if _superseded(conn, existing, target_id, user_id):
            pass
        elif existing["state"] == "sent":
            raise ValueError("This email was already sent from Gmail")
        else:
            reasons[f"claim:{stale_token}"] = claim_reason(existing)
    account = sender_account()

    with client_factory() as client:
        # The student asked for this send (or scheduled it, and the scheduler's fresh look
        # waits out a slowdown first), so its checks go to Gmail even while background reads wait.
        gmail = GmailClient(conn, client, user_id, wait_out_backoff=False)
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
            check = _sent_folder_check(reasons)
            if sent_folder_check != check:
                raise SendNeedsCheckError(" ".join(dict.fromkeys(reasons.values())), check)
        seen = [event_id for event_id, _detail in drafts]

        def revalidate() -> tuple[_Approved, str]:
            fresh = _approved_for(conn, target_id, user_id, kind, sending=True, fingerprint=fingerprint)
            if [event_id for event_id, _detail in _draft_events(conn, target_id, user_id, kind)] != seen:
                raise SendConflictError("A Gmail draft of this email was just made. Send it from Gmail, or delete it and send again")
            return fresh, fresh.raw(account)

        with claimed_send(conn, target_id, user_id, kind, "send", revalidate, stale_token=stale_token) as (token, (approved, raw)):
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
                    log_event(conn, target_id, user_id, SENT_EVENT, detail=json.dumps(detail, sort_keys=True))
            except BaseException:
                settle_send_claim(conn, target_id, kind, token, "sent")
                raise
    try:
        updated = update_target(conn, target_id, {"status": SENT_STATUS[kind]}, user_id=user_id)
    except Exception:
        # The email went out and is recorded; only the status is behind, and "I sent it" catches it up.
        current = get_target(conn, target_id, user_id=user_id)
        return {**detail, "account": account, "status": current["status"], "follow_up_at": current["follow_up_at"], "marked": False}
    return {**detail, "account": account, "status": updated["status"], "follow_up_at": updated["follow_up_at"], "marked": True}


# --- The thank-you after a decline -------------------------------------------------
#
# The one email the app writes and sends on its own (outreach_thank_you.py). It
# answers the person who declined, in their thread: Gmail's threadId, and
# In-Reply-To and References set to their Message-ID. Plain text with its HTML
# twin, never an attachment. It goes out once, under the same claim as every
# send (kind 'thank_you'), settled the same way: released when Gmail certainly
# did nothing, kept 'unconfirmed' when it may have sent it.

THANK_YOU_KIND = "thank_you"
THANK_YOU_SENT_EVENT = "thank_you_sent"
THANK_YOU_DRAFT_EVENT = "thank_you_draft_created"


class ThankYouChanged(ValueError):
    """The thank-you is no longer the one shown or scheduled: its words, recipient, or state moved."""


def thank_you_fingerprint(to_email: str, to_name: str, subject: str, body: str, reply_message_id: str, thread_id: str) -> str:
    """What a thank-you is: who it goes to, its words, and the message and thread it answers."""
    fields = ["thank_you", str(to_email or "").casefold(), to_name or "", subject or "", body or "", reply_message_id or "", thread_id or ""]
    canonical = json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def thank_you_row(conn: sqlite3.Connection, target_id: str, user_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM outreach_thank_yous WHERE target_id=? AND user_id=?", (target_id, user_id)).fetchone()
    return dict(row) if row is not None else None


def _thank_you_ready(
    conn: sqlite3.Connection, target_id: str, user_id: str, fingerprint: str, states: tuple[str, ...],
) -> dict[str, Any]:
    """The stored thank-you, once every check for leaving the app has passed on a fresh read."""
    target = get_target(conn, target_id, user_id=user_id)
    row = thank_you_row(conn, target_id, user_id)
    if row is None:
        raise ThankYouChanged("There is no thank-you for this company")
    if row["state"] not in states:
        raise ThankYouChanged(f"The thank-you is {row['state']}, so it was not sent")
    expected = thank_you_fingerprint(
        row["to_email"], row["to_name"], row["subject"], row["body"], row["reply_message_id"], row["thread_id"],
    )
    if fingerprint != row["fingerprint"] or expected != row["fingerprint"]:
        raise ThankYouChanged("The thank-you changed after it was shown. Reload and check it before sending")
    if not row["to_email"] or not row["thread_id"] or not row["body"].strip():
        raise ValueError("The thank-you has no recipient, words, or thread to answer, so it was not sent")
    if row["to_email"].casefold() in target["bounced_addresses"]:
        raise ValueError(f"Email to {row['to_email']} bounced, so the thank-you was not sent")
    if conn.execute(
        "SELECT 1 FROM outreach_events WHERE target_id=? AND user_id=? AND event_type=?", (target_id, user_id, THANK_YOU_SENT_EVENT),
    ).fetchone() is not None:
        raise ValueError("The thank-you was already sent")
    # Read under the claim's write lock: a reply, or a send of the student's, logged since the last check
    # (while the reviewer ran, say) still stops it. The student's own Send it anyway is not stopped by the
    # company's status, only by their newer message or the student's.
    stop = outreach_callbacks.thank_you_problem_now(conn, target_id, user_id, row, manual=states != ("transmitting",))
    if stop is not None:
        raise ThankYouChanged(stop[1])
    return {**row, "target": target}


def _thank_you_claim_reason(row: sqlite3.Row) -> str:
    if row["action"] == "draft":
        return ("Gmail may have put this thank-you in your Drafts without the app hearing back. Check your Gmail Drafts "
                "and Sent folders, and delete any copy there, before sending it from here.")
    return "Gmail may already have sent this thank-you. Check your Gmail Sent folder before sending it again."


def thank_you_mime(account: str, row: dict[str, Any]) -> str:
    """The reply as Gmail sends it: in their thread by its headers, plain text and HTML, no attachment."""
    message = EmailMessage()
    if account:
        message["From"] = account
    message["To"] = formataddr((row["to_name"], row["to_email"])) if row.get("to_name") else row["to_email"]
    message["Subject"] = row["subject"]
    if row.get("reply_message_id"):
        message["In-Reply-To"] = row["reply_message_id"]
        message["References"] = row["reply_message_id"]
    message.set_content(row["body"])
    message.add_alternative(html_body(row["body"]), subtype="html")
    return base64.urlsafe_b64encode(message.as_bytes()).decode()


def thread_url(account: str, thread_id: str) -> str:
    return gmail_web_url(f"all/{quote(thread_id)}", account)


def send_thank_you(
    conn: sqlite3.Connection,
    target_id: str,
    *,
    user_id: str,
    fingerprint: str,
    client_factory: ClientFactory = default_client_factory,
    sent_folder_check: str | None = None,
    automatic: bool = True,
) -> dict[str, Any]:
    """Send the stored thank-you once, in the thread it answers, and record it.

    ``automatic`` is the scheduler's hand-over (the row is 'transmitting');
    otherwise it is the student's own confirmed Send it anyway (the row is
    'sending'). Like send_gmail_message: the fingerprint must still match what
    was stored, the claim is taken before the one call that sends, and an
    earlier send Gmail may have carried out (an unconfirmed claim) stops it
    until the student has looked and sends again with ``sent_folder_check``.
    The scheduler never vouches for that, so for it this always stops.
    """
    states = ("transmitting",) if automatic else ("sending",)
    _thank_you_ready(conn, target_id, user_id, fingerprint, states)
    reasons: dict[str, str] = {}
    stale_token = ""
    existing = send_claim_row(conn, target_id, user_id, THANK_YOU_KIND)
    if existing is not None:
        if send_claim_held(existing):
            raise SendConflictError(IN_PROGRESS)
        stale_token = existing["token"]
        if existing["state"] == "sent":
            raise ValueError("The thank-you was already sent from Gmail")
        reasons[f"claim:{stale_token}"] = _thank_you_claim_reason(existing)
    if reasons:
        check = _sent_folder_check(reasons)
        if automatic or sent_folder_check != check:
            raise SendNeedsCheckError(" ".join(dict.fromkeys(reasons.values())), check)
    account = sender_account()
    with client_factory() as client:
        gmail = GmailClient(conn, client, user_id, wait_out_backoff=False)
        _require_account(gmail, account)

        def revalidate() -> tuple[dict[str, Any], str]:
            fresh = _thank_you_ready(conn, target_id, user_id, fingerprint, states)
            return fresh, thank_you_mime(account, fresh)

        with claimed_send(conn, target_id, user_id, THANK_YOU_KIND, "send", revalidate, stale_token=stale_token) as (token, (row, raw)):
            sent = _post_under_claim(
                conn, gmail, target_id, THANK_YOU_KIND, token, "/messages/send", {"raw": raw, "threadId": row["thread_id"]},
                refused="Gmail did not send the thank-you",
                uncertain="it may have gone out. Check your Gmail Sent folder before sending it again",
            )
            # What Gmail sent is recorded before anything else, so it is never lost.
            try:
                detail = {
                    "to": row["to_email"], "message_id": str(sent.get("id", "")),
                    "thread_id": str(sent.get("threadId", "") or row["thread_id"]),
                    "reply_gmail_id": row["reply_gmail_id"], "fingerprint": row["fingerprint"],
                }
                stamp = utc_now()
                with conn:
                    conn.execute("UPDATE outreach_send_claims SET state='sent' WHERE target_id=? AND kind=? AND token=?",
                                 (target_id, THANK_YOU_KIND, token))
                    log_event(conn, target_id, user_id, THANK_YOU_SENT_EVENT, detail=json.dumps(detail, sort_keys=True))
                    conn.execute(
                        "UPDATE outreach_thank_yous SET state='sent', note='', updated_at=? WHERE target_id=? AND user_id=?",
                        (stamp, target_id, user_id),
                    )
            except BaseException:
                settle_send_claim(conn, target_id, THANK_YOU_KIND, token, "sent")
                raise
    return {**detail, "account": account, "sent_at": stamp, "url": thread_url(account, detail["thread_id"])}


def create_thank_you_draft(
    conn: sqlite3.Connection, target_id: str, *, user_id: str, client_factory: ClientFactory = default_client_factory,
) -> dict[str, Any]:
    """Write the thank-you into the student's Gmail Drafts, in their thread, for the student to edit and send.

    Nothing is sent. The caller has already stopped the automatic send. The
    one call that makes the draft runs under the thank-you's claim (action
    'draft'), as a first email's draft does: a call that got no clear answer
    leaves it 'unconfirmed', so a later Send it anyway asks the student to
    look in Drafts first. An earlier try Gmail may have carried out stops a
    draft being made at all, since a copy in Drafts could then go twice.
    """
    row = thank_you_row(conn, target_id, user_id)
    if row is None:
        raise ThankYouChanged("There is no thank-you for this company")
    existing = send_claim_row(conn, target_id, user_id, THANK_YOU_KIND)
    if existing is not None:
        if send_claim_held(existing):
            raise SendConflictError(IN_PROGRESS)
        if existing["state"] == "sent":
            raise ValueError("The thank-you was already sent from Gmail")
        raise SendConflictError(f"{_thank_you_claim_reason(existing)} Until then no draft of it is made.")
    account = sender_account()

    def revalidate() -> dict[str, Any]:
        fresh = thank_you_row(conn, target_id, user_id)
        if fresh is None or fresh["fingerprint"] != row["fingerprint"]:
            raise ThankYouChanged("The thank-you changed while its draft was being made. Reload and try again")
        return fresh

    with claimed_send(conn, target_id, user_id, THANK_YOU_KIND, "draft", revalidate) as (token, fresh):
        try:
            client = client_factory()
        except BaseException:
            settle_send_claim(conn, target_id, THANK_YOU_KIND, token, None)
            raise
        with client:
            try:
                # The student asked for this draft, so its checks go to Gmail even while background reads wait.
                gmail = GmailClient(conn, client, user_id, wait_out_backoff=False)
                _require_account(gmail, account)
                raw = thank_you_mime(account, fresh)
            except BaseException:
                settle_send_claim(conn, target_id, THANK_YOU_KIND, token, None)
                raise
            created = _post_under_claim(
                conn, gmail, target_id, THANK_YOU_KIND, token, "/drafts", {"message": {"raw": raw, "threadId": fresh["thread_id"]}},
                refused="Gmail did not create the draft",
                uncertain="the draft may be in your Gmail Drafts. Check Drafts before trying again",
            )
            try:
                message = created.get("message") or {}
                detail = {
                    "draft_id": str(created.get("id", "")), "message_id": str(message.get("id", "")),
                    "thread_id": str(message.get("threadId", "") or fresh["thread_id"]),
                }
                with conn:
                    log_event(conn, target_id, user_id, THANK_YOU_DRAFT_EVENT, detail=json.dumps(detail, sort_keys=True))
                    conn.execute("DELETE FROM outreach_send_claims WHERE target_id=? AND kind=? AND token=?", (target_id, THANK_YOU_KIND, token))
            except BaseException:
                # The draft exists but is not recorded, so the next send asks first.
                settle_send_claim(conn, target_id, THANK_YOU_KIND, token, "unconfirmed")
                raise
    return {**detail, "url": draft_url(account, detail["message_id"])}


# --- Notices about the connection ---------------------------------------------------


def gmail_notices(conn: sqlite3.Connection, user_id: str, *, now: datetime | None = None) -> list[str]:
    """Leave the student a notice when Gmail will likely need reconnecting soon, or already does.

    Each is left once: the expiry notice once per grant, the reconnect notice
    once per time the connection broke. Returns the event keys of the notices
    that are new. automation.notice opens its own transaction, so this is never
    called inside one. The expiry is an estimate (automation_health.gmail_health), and
    the notice says "likely". Once the estimated date has passed
    (``estimate_passed``) there is no date left to name: "before <that date>"
    would point the student at a time already behind them, so the notice says
    "soon" instead, as the banner does.
    """
    health = automation_health.gmail_health(conn, user_id, now=now)
    new = []
    if health["expiring_soon"]:
        if health.get("estimate_passed"):
            body = "Open Outreach and click Reconnect Gmail soon so reply and bounce checks keep running."
        else:
            local = user_timezone(conn, user_id).to_local(datetime.fromisoformat(health["likely_expires_at"]))
            when = f"{local:%a, %b} {local.day} at {f'{local:%I:%M %p}'.lstrip('0')}"
            body = f"Open Outreach and click Reconnect Gmail before {when} so reply and bounce checks keep running."
        key = f"gmail-expiring:{health['token_granted_at']}"
        if automation.notice(
            conn, user_id, event_key=key, level="warning", title="Gmail will likely need reconnecting soon", body=body,
        ):
            new.append(key)
    row = connector_row(conn, user_id)
    if row is not None and row["status"] == "error":
        key = f"gmail-expired:{row['updated_at']}"
        if automation.notice(
            conn, user_id, event_key=key, level="problem", title="Gmail needs reconnecting",
            body="Reply and bounce checks have stopped until you reconnect Gmail in Outreach.",
        ):
            new.append(key)
    return new
