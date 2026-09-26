"""Replies to outreach, read from Gmail so the student never pastes them.

For every company the student has written to, the app searches Gmail for
mail from the contact, the Cc, any address an email went to, and anyone at
the company's own domain. Each new message is read once
(outreach_inbox_messages) and sorted:

- A reply is logged exactly as a pasted one would be, so call prep and the
  history see it. A company still waiting on a reply (or given up on as No
  response) moves to Replied: that someone wrote back is a fact. What the
  reply means (declined, a call, an offer) stays a suggestion the student
  applies or dismisses, from Jev when it is on and sure, else the rules.
- An automatic reply (out of office) is noted and changes nothing.
- A delivery failure notice is left to outreach_delivery.

``InboxWatcher`` runs both checks on a background thread, so a reply or a
bounce is caught even when the page is closed.
"""

from __future__ import annotations

import base64
import email
import html
import json
import logging
import re
import sqlite3
import threading
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import httpx

from .inbox_classifiers import classify_reply
from .outreach import (
    BOUNCED,
    _log,
    get_target,
    suggest_reply_status,
    update_target,
    website_domain,
)
from .outreach_delivery import check_deliveries
from .outreach_gmail import (
    PROVIDER,
    SENT_EVENT,
    ClientFactory,
    GmailAuthError,
    _connector,
    _Gmail,
)
from .outreach_drafting import sender_account
from .schema import connect_product, utc_now
from .typesafe_decisions import DecisionClient

LOGGER = logging.getLogger(__name__)

# Statuses of a company the student has written to.
WATCHED_STATUSES = ("sent", "followed_up", "replied", "call_scheduled", "offer", "paused", "no_response", "declined")
# Waiting on them, or given up on: a reply moves these to Replied.
REOPENED_BY_REPLY = {"sent", "followed_up", "no_response"}
# Replies can come months later; past this a company is not searched.
REPLY_WINDOW = timedelta(days=180)
CAPTURE_EVERY = timedelta(seconds=60)
# A domain shared by millions of senders says nothing about who wrote.
FREEMAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "outlook.com", "hotmail.com", "live.com", "msn.com", "icloud.com",
    "me.com", "aol.com", "proton.me", "protonmail.com", "gmx.com", "mail.com", "yandex.com", "zoho.com",
}
_DAEMONS = {"mailer-daemon", "mailerdaemon", "mail-daemon", "postmaster"}
_AUTO_SUBJECT = re.compile(r"^\s*(automatic reply|auto(matic)?[- ]?reply|auto:|out of (the )?office|ooo\b|away from)", re.IGNORECASE)
# Where the quoted email starts in a reply: "On Fri, ... wrote:", "-----Original Message-----",
# Outlook's "From: ... Sent: ..." header block, or a "> " line.
_ON_WROTE = re.compile(r"^\s*On .{0,300}wrote:\s*$", re.IGNORECASE | re.DOTALL)
_ORIGINAL = re.compile(r"^\s*-{2,}\s*(original message|forwarded message)\s*-{2,}\s*$", re.IGNORECASE)

_LAST_CAPTURE: dict[str, datetime] = {}
_CAPTURE_LOCK = threading.Lock()

OnReply = Callable[[sqlite3.Connection, str, str], None]


# --- Reading a message ----------------------------------------------------------


def _html_text(markup: str) -> str:
    markup = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h\d)>", "\n", markup)
    markup = re.sub(r"(?is)<blockquote\b.*", "", markup)
    return html.unescape(re.sub(r"<[^>]+>", "", markup))


def strip_quoted(text: str) -> str:
    """The reply above the email it quotes."""
    lines = text.replace("\r\n", "\n").split("\n")
    for index, line in enumerate(lines):
        pair = f"{line} {lines[index + 1]}" if index + 1 < len(lines) else line
        header_block = line.startswith("From:") and any(
            following.startswith(("Sent:", "Date:")) for following in lines[index + 1:index + 3]
        )
        if line.lstrip().startswith(">") or _ORIGINAL.match(line) or header_block or _ON_WROTE.match(line) or (
            line.lstrip().startswith("On ") and _ON_WROTE.match(pair)
        ):
            lines = lines[:index]
            break
    return "\n".join(lines).strip()


def reply_text(message: EmailMessage) -> str:
    body = message.get_body(preferencelist=("plain", "html"))
    if body is None:
        return ""
    try:
        text = str(body.get_content())
    except (LookupError, ValueError):
        return ""
    if body.get_content_type() == "text/html":
        text = _html_text(text)
    return strip_quoted(text)[:20_000]


def is_bulk(message: EmailMessage) -> bool:
    """Mailing-list mail, such as a company newsletter: never a reply."""
    return bool(message.get("List-Unsubscribe") or message.get("List-Id"))


def answers_something(message: EmailMessage) -> bool:
    """Whether a message is written as a reply, not a fresh email."""
    return bool(message.get("In-Reply-To") or message.get("References")) or bool(
        re.match(r"^\s*(re|aw|sv|antw)\s*:", str(message.get("Subject", "")), re.IGNORECASE)
    )


def is_automatic(message: EmailMessage) -> bool:
    """An out-of-office or other automatic reply, by its headers or subject."""
    auto = str(message.get("Auto-Submitted", "")).strip().casefold()
    if auto and auto != "no":
        return True
    if message.get("X-Autoreply") or message.get("X-Autorespond"):
        return True
    if str(message.get("Precedence", "")).strip().casefold() in {"auto_reply", "bulk", "junk"}:
        return True
    return bool(_AUTO_SUBJECT.search(str(message.get("Subject", ""))))


# --- Who each company is ------------------------------------------------------------


def _domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].casefold() if "@" in address else ""


def _watched(conn: sqlite3.Connection, user_id: str, now: datetime) -> list[dict[str, Any]]:
    """Companies written to within the reply window, with every address that speaks for them."""
    rows = conn.execute(
        f"""
        SELECT id, company, status, contact_email, contact_cc, website, sent_at FROM outreach_targets
        WHERE user_id=? AND status IN ({', '.join('?' for _ in WATCHED_STATUSES)})
        """,
        (user_id, *WATCHED_STATUSES),
    ).fetchall()
    sends: dict[str, list[tuple[datetime, dict[str, Any]]]] = {}
    for event in conn.execute(
        "SELECT target_id, detail, created_at FROM outreach_events WHERE user_id=? AND event_type=?", (user_id, SENT_EVENT)
    ).fetchall():
        try:
            detail = json.loads(event["detail"])
        except (TypeError, ValueError):
            continue
        if isinstance(detail, dict):
            sends.setdefault(event["target_id"], []).append((datetime.fromisoformat(event["created_at"]), detail))
    watched = []
    for row in rows:
        own = sends.get(row["id"], [])
        addresses = {str(row[field] or "").strip().casefold() for field in ("contact_email", "contact_cc")}
        addresses |= {str(detail.get(field) or "").strip().casefold() for _at, detail in own for field in ("to", "cc")}
        addresses -= {""}
        # The app's own sends say to the second when the email went; a send marked
        # by hand has only a date, so the day before it is the earliest a reply counts.
        starts = [at for at, _detail in own]
        if not starts and row["sent_at"]:
            starts.append(datetime.combine(date.fromisoformat(row["sent_at"]), datetime.min.time(), tzinfo=timezone.utc) - timedelta(days=1))
        if not addresses or not starts:
            continue
        since = min(starts)
        if now - since > REPLY_WINDOW:
            continue
        # Anyone at the company's own domain may answer, whatever address the contact uses.
        site = website_domain(row["website"] or "")
        domains = {site} if site and "." in site and site not in FREEMAIL else set()
        watched.append({
            "id": row["id"], "company": row["company"], "status": row["status"],
            "addresses": addresses, "domains": domains, "since": since,
        })
    return watched


def _owner(watched: list[dict[str, Any]], sender: str) -> dict[str, Any] | None:
    """The one company a sender speaks for: by address, else by the company's own domain."""
    by_address = [target for target in watched if sender in target["addresses"]]
    if len(by_address) == 1:
        return by_address[0]
    if by_address:
        return None
    domain = _domain(sender)
    by_domain = [
        target for target in watched
        if any(domain == own or domain.endswith(f".{own}") for own in target["domains"])
    ]
    return {**by_domain[0], "by_domain": True} if len(by_domain) == 1 else None


def _query(chunk: list[dict[str, Any]]) -> str:
    terms = sorted({term for target in chunk for term in (*target["addresses"], *target["domains"])})
    since = min(target["since"] for target in chunk) - timedelta(days=1)
    return f"from:({' OR '.join(terms)}) after:{since:%Y/%m/%d} -in:sent -in:drafts"


# --- Capturing ------------------------------------------------------------------------

# Pages of search results read per check. A backlog longer than this is read
# over the next checks, since every message read is remembered.
MAX_PAGES = 10


def _search(gmail: _Gmail, query: str) -> tuple[list[dict[str, Any]], int]:
    """Every message a search finds, following Gmail's pages, and the status of a failed page (0 if none)."""
    found: list[dict[str, Any]] = []
    token = ""
    for _page in range(MAX_PAGES):
        params: dict[str, Any] = {"q": query, "maxResults": 100}
        if token:
            params["pageToken"] = token
        listing = gmail.request("GET", "/messages", params=params)
        if listing.status_code != 200:
            return found, listing.status_code
        data = listing.json()
        found.extend(data.get("messages") or [])
        token = str(data.get("nextPageToken") or "")
        if not token:
            break
    return found, 0


def _seen(conn: sqlite3.Connection, user_id: str, gmail_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM outreach_inbox_messages WHERE user_id=? AND gmail_id=?", (user_id, gmail_id)
    ).fetchone() is not None


def _remember(conn: sqlite3.Connection, user_id: str, gmail_id: str, target_id: str, kind: str, sender: str, received: str) -> bool:
    """Record a message as read. False when another check already recorded it."""
    return bool(conn.execute(
        """
        INSERT INTO outreach_inbox_messages(user_id, gmail_id, target_id, kind, sender, received_at, recorded_at)
        VALUES(?, ?, ?, ?, ?, ?, ?) ON CONFLICT(user_id, gmail_id) DO NOTHING
        """,
        (user_id, gmail_id, target_id, kind, sender, received, utc_now()),
    ).rowcount)


def _record_reply(
    conn: sqlite3.Connection, target: dict[str, Any], *, user_id: str, gmail_id: str, sender: str,
    received: str, text: str, decisions: DecisionClient | None,
) -> dict[str, Any] | None:
    """Log one reply, or None when another check (the background one, say) got to it first."""
    suggestion = classify_reply(text, suggest_reply_status, decisions)
    if suggestion["status"] == BOUNCED:
        # A person wrote this, so it is a reply even if it talks about a failed delivery.
        suggestion = {**suggestion, "status": "replied"}
    with conn:
        if not _remember(conn, user_id, gmail_id, target["id"], "reply", sender, received):
            return None
        _log(conn, target["id"], user_id, "reply_logged", detail=text)
    if target["status"] in REOPENED_BY_REPLY:
        update_target(conn, target["id"], {"status": "replied"}, user_id=user_id)
    current = get_target(conn, target["id"], user_id=user_id)
    stored = ""
    if suggestion["status"] not in {current["status"], "replied"}:
        stored = json.dumps({**suggestion, "from": sender, "received_at": received}, sort_keys=True)
    with conn:
        conn.execute(
            "UPDATE outreach_targets SET reply_suggestion_json=?, updated_at=? WHERE id=? AND user_id=?",
            (stored, utc_now(), target["id"], user_id),
        )
    return {"target_id": target["id"], "company": target["company"], "from": sender, "suggestion": suggestion}


def capture_replies(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    client_factory: ClientFactory,
    decisions: DecisionClient | None = None,
    on_reply: OnReply | None = None,
    now: datetime | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Find new replies in Gmail and log each one. See the module docstring for what each does.

    ``state`` is as for check_deliveries. Runs at most once a minute per
    student unless ``force``; nothing is asked of Gmail when no company is
    waiting to hear back.
    """
    now = now or datetime.now(timezone.utc)
    result: dict[str, Any] = {"state": "ok", "replies": [], "automatic": []}
    with _CAPTURE_LOCK:
        last = _LAST_CAPTURE.get(user_id)
        if not force and last is not None and now - last < CAPTURE_EVERY:
            return result
        _LAST_CAPTURE[user_id] = now
    watched = _watched(conn, user_id, now)
    if not watched:
        return result
    row = _connector(conn, user_id)
    if not row or row["status"] != "connected":
        return {**result, "state": "not_connected" if not row or row["status"] == "disconnected" else "needs_reconnect"}
    account = sender_account().casefold()
    try:
        with client_factory() as client:
            gmail = _Gmail(conn, client, user_id)
            for start in range(0, len(watched), 20):
                chunk = watched[start:start + 20]
                references, failed = _search(gmail, _query(chunk))
                if failed == 403:
                    return {**result, "state": "needs_reconnect"}
                if failed:
                    # A search that failed found nothing yet; say so, so a send waiting on it holds.
                    result["state"] = "unreachable"
                for reference in references:
                    gmail_id = str(reference.get("id", ""))
                    if not gmail_id or _seen(conn, user_id, gmail_id):
                        continue
                    fetched = gmail.request("GET", f"/messages/{quote(gmail_id, safe='')}", params={"format": "raw"})
                    if fetched.status_code == 404:
                        continue  # deleted since the search
                    if fetched.status_code != 200:
                        result["state"] = "unreachable"
                        continue
                    data = fetched.json()
                    raw = str(data.get("raw", ""))
                    message = email.message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)), policy=policy.default)
                    received_at = datetime.fromtimestamp(int(data.get("internalDate") or 0) / 1000, tz=timezone.utc)
                    received = received_at.isoformat(timespec="seconds")
                    sender = parseaddr(str(message.get("From", "")))[1].casefold()
                    target = _owner(chunk, sender)
                    text = reply_text(message)
                    # A colleague at the company counts only when answering, never a newsletter.
                    if (
                        target is None or sender == account or sender.split("@", 1)[0] in _DAEMONS
                        or received_at < target["since"] or "SENT" in (data.get("labelIds") or [])
                        or is_bulk(message) or (target.get("by_domain") and not answers_something(message))
                    ):
                        with conn:
                            _remember(conn, user_id, gmail_id, target["id"] if target else "", "ignored", sender, received)
                        continue
                    if is_automatic(message):
                        with conn:
                            _remember(conn, user_id, gmail_id, target["id"], "automatic", sender, received)
                            _log(conn, target["id"], user_id, "auto_reply", detail=f"{sender}: {' '.join(text.split())[:300]}")
                        result["automatic"].append({"target_id": target["id"], "company": target["company"], "from": sender})
                        continue
                    if not text:
                        text = f"(A reply from {sender} with no text, subject: {message.get('Subject', '')})"
                    captured = _record_reply(
                        conn, get_target(conn, target["id"], user_id=user_id), user_id=user_id, gmail_id=gmail_id,
                        sender=sender, received=received, text=text, decisions=decisions,
                    )
                    if captured is None:
                        continue
                    result["replies"].append(captured)
                    for listed in chunk:
                        if listed["id"] == target["id"]:
                            listed["status"] = get_target(conn, target["id"], user_id=user_id)["status"]
                    if on_reply:
                        on_reply(conn, target["id"], user_id)
    except GmailAuthError:
        return {**result, "state": "needs_reconnect"}
    except (httpx.HTTPError, ValueError):
        return {**result, "state": "unreachable"}
    return result


# --- In the background ----------------------------------------------------------------


class InboxWatcher:
    """Checks every connected student's Gmail for bounces and replies on a background thread."""

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
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> None:
        with closing(connect_product(self.platform_target)) as conn:
            users = [row[0] for row in conn.execute(
                "SELECT user_id FROM connector_accounts WHERE provider=? AND status='connected'", (PROVIDER,)
            ).fetchall()]
            for user_id in users:
                check_deliveries(conn, user_id=user_id, client_factory=self._client_factory)
                capture_replies(
                    conn, user_id=user_id, client_factory=self._client_factory,
                    decisions=self._decisions_for(conn, user_id), on_reply=self._on_reply,
                )

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="inbox-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        # The first pass soon after start, to catch what arrived while the app was off.
        wait = min(20.0, self._interval)
        while not self._stop.wait(wait):
            try:
                self.run_once()
            except Exception:  # the thread must outlive any one bad pass
                LOGGER.exception("Inbox watcher pass failed")
            wait = self._interval
