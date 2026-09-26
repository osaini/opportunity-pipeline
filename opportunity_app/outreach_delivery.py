"""Outreach email that bounced: recording it, and spotting it in Gmail.

Gmail accepting a send does not mean it arrived. The recipient's server can
refuse it seconds later, and a delivery failure notice lands in the sender's
inbox, usually threaded with the sent email. Until the bounce is recorded the
target looks sent, a follow-up is scheduled to an address that cannot receive
mail, and the first email is locked against going out again.

``record_bounce`` puts the target back to Drafted and keeps the failed address
on record, so nothing more goes there and the first email can go out again to
a new contact. ``check_deliveries`` finds failure notices for recent sends: in
each sent email's thread, and by searching the inbox for notices a server sent
outside it. A notice is read in full (gmail.readonly), because its delivery
report names exactly which recipient failed and why. Only notices are read.
"""

from __future__ import annotations

import base64
import email
import html
import json
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from email import policy
from email.utils import getaddresses, parseaddr
from typing import Any, Iterable
from urllib.parse import quote

import httpx

from .outreach import AWAITING_REPLY, _log, get_target
from .outreach_drafting import _keep_current_draft
from .outreach_gmail import (
    BOUNCE_EVENT,
    SENT_EVENT,
    ClientFactory,
    GmailAuthError,
    _connector,
    _Gmail,
    last_bounce,
)
from .schema import utc_now

# Some recipients failed and the rest were reached (a bad guess with the shared
# inbox in Cc): the email did arrive, so nothing is reopened.
PARTIAL_BOUNCE_EVENT = "partly_bounced"
# Given up on for silence, but a bounce says the silence was an address that failed.
REOPENED_BY_BOUNCE = {*AWAITING_REPLY, "no_response"}
# Most bounces arrive within a minute; a server that keeps retrying gives up
# within a few days.
WATCH_FOR = timedelta(days=3)
# Failure notices anywhere in the inbox, for servers whose notice Gmail does
# not thread with the sent email. One day longer than WATCH_FOR, so a notice
# for the oldest watched send is still found.
NOTICE_SEARCH = "from:(mailer-daemon OR postmaster) newer_than:4d"
_HEADERS = ("From", "Subject", "Content-Type", "X-Failed-Recipients")
_DAEMONS = {"mailer-daemon", "mailerdaemon", "mail-daemon", "postmaster"}
_DELAY = re.compile(r"\(delay\)|\bdelayed\b|\bwarning\b|\bwill (retry|keep trying)\b|\btemporar(y|ily)\b", re.IGNORECASE)
# Wording that makes a notice a failure even when it also mentions retrying.
_FAILED_WORDING = re.compile(r"\(failure\)|\bpermanent(ly)?\b|\bgave up\b|\bgiving up\b|\bcould ?n[o']t be delivered\b|\b5\d\d[ -]5\.\d\.\d+", re.IGNORECASE)
_ADDRESS = re.compile(r"[^@\s<>\"'(),;:]+@[^@\s<>\"'(),;:]+\.[^@\s<>\"'(),;:]+")
_SALUTATION = re.compile(r"^(hello|hi)\s+\S+,\s*", re.IGNORECASE)

# When each sent email was last looked at, so a page that loads often does not
# ask Gmail every time, and which searched notices were already read. Kept in
# memory: a restart only means one early look.
_LAST_LOOK: dict[tuple[str, str], datetime] = {}
_READ_NOTICES: set[tuple[str, str]] = set()
_LOOK_LOCK = threading.Lock()


class _NeedsReconnect(Exception):
    """Gmail refused a read: the connection predates the read scope."""


class _Unreadable(Exception):
    """Gmail answered a read with an error, so what it would have shown is unknown."""


def _interval(age: timedelta) -> timedelta:
    """How long to wait between looks at one sent email: often while it is fresh."""
    if age < timedelta(minutes=15):
        return timedelta(seconds=15)
    if age < timedelta(hours=6):
        return timedelta(minutes=10)
    return timedelta(hours=1)


def _recorded(conn: sqlite3.Connection, target_id: str, user_id: str, notice_id: str) -> bool:
    rows = conn.execute(
        "SELECT detail FROM outreach_events WHERE target_id=? AND user_id=? AND event_type IN (?, ?)",
        (target_id, user_id, BOUNCE_EVENT, PARTIAL_BOUNCE_EVENT),
    ).fetchall()
    for row in rows:
        try:
            if json.loads(row["detail"]).get("notice_id") == notice_id:
                return True
        except (TypeError, ValueError, AttributeError):
            continue
    return False


def record_bounce(
    conn: sqlite3.Connection,
    target_id: str,
    *,
    user_id: str,
    reason: str = "",
    addresses: Iterable[str] = (),
    source: str = "you",
    sent: dict[str, Any] | None = None,
    notice_id: str = "",
) -> dict[str, Any]:
    """Record that an outreach email bounced.

    ``addresses`` are the ones the notice says failed; with none named, the
    address the email went to is taken as the one that failed. Only when every
    recipient failed did the email reach nobody: then a company still waiting
    to hear back goes back to Drafted with no sent date or follow-up. When a
    Cc (or the To) was still reached, only the failed addresses are marked.
    Either way the failed addresses are kept for good.
    """
    target = get_target(conn, target_id, user_id=user_id)
    if notice_id and _recorded(conn, target_id, user_id, notice_id):
        return target
    source_of = sent or {"to": target["contact_email"], "cc": target["contact_cc"]}
    sent_to = str(source_of.get("to") or "").strip().casefold()
    recipients = {str(source_of.get(field) or "").strip().casefold() for field in ("to", "cc")} - {""}
    failed = sorted({address.strip().casefold() for address in addresses if address.strip()})
    if not failed:
        if not sent_to:
            raise ValueError("There is no contact address to mark bounced")
        failed = [sent_to]
    whole = recipients <= set(failed)
    reason = " ".join(str(reason or "").split())[:500]
    timestamp = utc_now()
    assignments: dict[str, Any] = {"bounced_addresses_json": json.dumps(sorted({*target["bounced_addresses"], *failed}))}
    reverted = whole and target["status"] in REOPENED_BY_BOUNCE
    if whole:
        assignments.update(bounced_at=timestamp, bounce_reason=reason)
    if reverted:
        assignments.update(status="drafted", sent_at=None, follow_up_at=None)
    detail = {
        "addresses": failed, "reason": reason, "source": source, "notice_id": notice_id,
        "message_id": str((sent or {}).get("message_id", "")), "kind": str((sent or {}).get("kind", "")),
    }
    with conn:
        # The words that bounced stay in the draft history once a new contact changes them.
        _keep_current_draft(conn, target_id, user_id, "initial")
        conn.execute(
            f"UPDATE outreach_targets SET {', '.join(f'{column}=?' for column in assignments)}, updated_at=? WHERE id=? AND user_id=?",
            [*assignments.values(), timestamp, target_id, user_id],
        )
        if reverted:
            _log(conn, target_id, user_id, "status", from_status=target["status"], to_status="drafted")
        _log(conn, target_id, user_id, BOUNCE_EVENT if whole else PARTIAL_BOUNCE_EVENT, detail=json.dumps(detail, sort_keys=True))
    return get_target(conn, target_id, user_id=user_id)


def bounce_from_text(conn: sqlite3.Connection, target_id: str, text: str, *, user_id: str) -> dict[str, Any]:
    """Record a bounce the student pasted. The addresses it names that this target used are the failed ones."""
    target = get_target(conn, target_id, user_id=user_id)
    body = str(text or "").strip()
    if len(body) > 20_000:
        raise ValueError("The notice is too long")
    ours = {target["contact_email"].casefold(), target["contact_cc"].casefold()} - {""}
    named = {address.casefold().rstrip(".") for address in _ADDRESS.findall(body)} & ours
    first_line = next((line.strip() for line in body.splitlines() if line.strip()), "")
    return record_bounce(conn, target_id, user_id=user_id, reason=first_line, addresses=named, source="you")


# --- Reading a notice ---------------------------------------------------------


def read_notice(raw: bytes) -> dict[str, Any] | None:
    """The failed addresses and the reason a failure notice gives, from its own text.

    A standard notice carries a delivery report naming each recipient with
    its outcome; those marked failed are the failed addresses. One that
    reports only delays is not a bounce, so None. A notice without a report
    (a mailing list's refusal, say) names no address, and the reason is its
    first paragraph of plain text.
    """
    message = email.message_from_bytes(raw, policy=policy.default)
    failed: list[str] = []
    delayed = False
    diagnostic = ""
    text = ""
    report = False
    for part in message.walk():
        kind = part.get_content_type()
        if kind == "message/delivery-status" and isinstance(part.get_payload(), list):
            report = True
            for block in part.get_payload():
                action = str(block.get("Action", "")).strip().casefold()
                recipient = str(block.get("Final-Recipient") or block.get("Original-Recipient") or "")
                address = recipient.split(";", 1)[-1].strip().strip("<>").casefold()
                if action == "failed" and "@" in address:
                    failed.append(address)
                    diagnostic = diagnostic or " ".join(str(block.get("Diagnostic-Code", "")).split())
                elif action == "delayed":
                    delayed = True
        elif kind == "text/plain" and not text:
            try:
                text = str(part.get_content())
            except (LookupError, ValueError):
                text = ""
    if delayed and not failed:
        return None
    subject = str(message.get("Subject", ""))
    if not report:
        # No report: the notice's own words say whether it is only a delay, and
        # an Exim-style notice names the failed address in a header.
        wording = f"{subject}\n{text}"
        if _DELAY.search(wording) and not _FAILED_WORDING.search(wording):
            return None
        failed = [address.casefold() for _name, address in getaddresses([str(message.get("X-Failed-Recipients", ""))]) if "@" in address]
    reason = _SALUTATION.sub("", " ".join(text.split()))[:300] or diagnostic
    return {"failed": sorted(set(failed)), "reason": reason}


def _headers_say_failure(message: dict[str, Any]) -> dict[str, Any] | None:
    """What a message's headers alone say, if it looks like a delivery failure notice."""
    payload = message.get("payload") or {}
    headers = {str(item.get("name", "")).lower(): str(item.get("value", "")) for item in payload.get("headers") or []}
    sender = parseaddr(headers.get("from", ""))[1].casefold()
    content_type = f"{headers.get('content-type', '')} {payload.get('mimeType', '')}".casefold()
    from_daemon = sender.split("@", 1)[0] in _DAEMONS
    delivery_report = "multipart/report" in content_type and "delivery-status" in content_type
    if not (from_daemon or delivery_report):
        return None
    subject = headers.get("subject", "")
    if _DELAY.search(subject) and "failure" not in subject.casefold():
        return None
    failed = [address.casefold() for _name, address in getaddresses([headers.get("x-failed-recipients", "")]) if "@" in address]
    return {"failed": failed, "reason": html.unescape(str(message.get("snippet") or subject))}


def _raw(gmail: _Gmail, message_id: str) -> tuple[bytes, int] | None:
    """A message's full text and when Gmail received it (ms), or None when Gmail will not give it."""
    response = gmail.request("GET", f"/messages/{quote(message_id, safe='')}", params={"format": "raw"})
    if response.status_code == 403:
        raise _NeedsReconnect
    if response.status_code != 200:
        return None
    data = response.json()
    raw = str(data.get("raw", ""))
    try:
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)), int(data.get("internalDate") or 0)
    except (ValueError, TypeError):
        return None


def _notice_in_thread(gmail: _Gmail, thread: dict[str, Any], message_id: str) -> dict[str, Any] | None:
    """A failure notice in a sent email's thread that arrived after it, read in full when Gmail allows."""
    messages = thread.get("messages") or []
    sent_at = next((int(item.get("internalDate") or 0) for item in messages if item.get("id") == message_id), 0)
    for message in messages:
        if message.get("id") == message_id or set(message.get("labelIds") or []) & {"SENT", "DRAFT"}:
            continue
        if int(message.get("internalDate") or 0) < sent_at:
            continue
        hint = _headers_say_failure(message)
        if not hint:
            continue
        notice_id = str(message.get("id", ""))
        fetched = _raw(gmail, notice_id)
        if fetched is None:
            return {**hint, "notice_id": notice_id}
        read = read_notice(fetched[0])
        if read is None:
            continue
        return {"failed": read["failed"] or hint["failed"], "reason": read["reason"] or hint["reason"], "notice_id": notice_id}
    return None


# --- Watching recent sends ------------------------------------------------------


def _watched(conn: sqlite3.Connection, user_id: str, now: datetime, every_send_of: str | None = None) -> list[dict[str, Any]]:
    """Recent sends from the app that have not bounced yet, oldest first.

    ``every_send_of`` adds that company's older sends too: a server can give up
    after days, and a follow-up a week later must still see that bounce.
    """
    cutoff = (now - WATCH_FOR).isoformat(timespec="microseconds")
    rows = conn.execute(
        """
        SELECT e.target_id, e.detail, e.created_at FROM outreach_events e
        JOIN outreach_targets t ON t.id=e.target_id AND t.user_id=e.user_id
        WHERE e.user_id=? AND e.event_type=? AND (e.created_at>=? OR e.target_id=?) ORDER BY e.created_at
        """,
        (user_id, SENT_EVENT, cutoff, every_send_of or ""),
    ).fetchall()
    watched = []
    for row in rows:
        try:
            detail = json.loads(row["detail"])
        except (TypeError, ValueError):
            continue
        if not isinstance(detail, dict) or not detail.get("message_id") or not detail.get("thread_id"):
            continue
        item = {"target_id": row["target_id"], "sent_at": datetime.fromisoformat(row["created_at"]), "detail": detail}
        if not _bounced_since(conn, user_id, item):
            watched.append(item)
    return watched


def _bounced_since(conn: sqlite3.Connection, user_id: str, item: dict[str, Any]) -> bool:
    bounce = last_bounce(conn, item["target_id"], user_id)
    return bounce is not None and bounce > item["sent_at"]


def _take_due(user_id: str, watched: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    due = []
    with _LOOK_LOCK:
        for item in watched:
            key = (user_id, item["detail"]["message_id"])
            last = _LAST_LOOK.get(key)
            if last is None or now - last >= _interval(now - item["sent_at"]):
                _LAST_LOOK[key] = now
                due.append(item)
    return due


def _forget(user_id: str, items: list[dict[str, Any]]) -> None:
    with _LOOK_LOCK:
        for item in items:
            _LAST_LOOK.pop((user_id, item["detail"]["message_id"]), None)


def _addressed(item: dict[str, Any]) -> set[str]:
    return {str(item["detail"].get(field) or "").strip().casefold() for field in ("to", "cc")} - {""}


def _match(conn: sqlite3.Connection, user_id: str, watched: list[dict[str, Any]], failed: list[str], received_ms: int) -> dict[str, Any] | None:
    """The latest unbounced send to a failed address that went out before the notice arrived."""
    # A few seconds for the send being recorded just after Gmail accepted it.
    received = datetime.fromtimestamp(received_ms / 1000, tz=timezone.utc) + timedelta(seconds=5)
    candidates = [
        item for item in watched
        if item["sent_at"] <= received and _addressed(item) & set(failed) and not _bounced_since(conn, user_id, item)
    ]
    return max(candidates, key=lambda item: item["sent_at"]) if candidates else None


def _searched_notices(gmail: _Gmail, conn: sqlite3.Connection, user_id: str, watched: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Failure notices outside the sent threads, each with the send it is about."""
    response = gmail.request("GET", "/messages", params={"q": NOTICE_SEARCH, "maxResults": 25})
    if response.status_code == 403:
        raise _NeedsReconnect
    if response.status_code != 200:
        raise _Unreadable
    found = []
    for reference in response.json().get("messages") or []:
        notice_id = str(reference.get("id", ""))
        key = (user_id, notice_id)
        with _LOOK_LOCK:
            if not notice_id or key in _READ_NOTICES:
                continue
            _READ_NOTICES.add(key)
        fetched = _raw(gmail, notice_id)
        if fetched is None:
            with _LOOK_LOCK:
                _READ_NOTICES.discard(key)
            raise _Unreadable
        read = read_notice(fetched[0])
        # Without a named address a notice outside the thread cannot be tied to a send.
        if not read or not read["failed"]:
            continue
        item = _match(conn, user_id, watched, read["failed"], fetched[1])
        if item:
            found.append((item, {**read, "notice_id": notice_id}))
    return found


def check_deliveries(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    client_factory: ClientFactory,
    now: datetime | None = None,
    force_target: str | None = None,
) -> dict[str, Any]:
    """Look in Gmail for bounces of the app's recent sends, and record each one found.

    ``force_target`` looks at that company's sends now, however recently they
    were looked at: the check made just before an automatic send.

    ``state`` is "ok", "not_connected", "needs_reconnect" (the connection
    predates the read scope, or was revoked), or "unreachable". Nothing is
    asked of Gmail when no send is due for a look.
    """
    now = now or datetime.now(timezone.utc)
    result: dict[str, Any] = {"state": "ok", "checked": 0, "bounced": []}
    watched = _watched(conn, user_id, now, every_send_of=force_target)
    due = _take_due(user_id, [item for item in watched if item["target_id"] != force_target], now)
    due += [item for item in watched if item["target_id"] == force_target]
    if not due:
        return result
    row = _connector(conn, user_id)
    if not row or row["status"] != "connected":
        _forget(user_id, due)
        return {**result, "state": "not_connected" if not row or row["status"] == "disconnected" else "needs_reconnect"}

    reported: set[str] = set()

    def record(item: dict[str, Any], notice: dict[str, Any]) -> None:
        detail = item["detail"]
        if notice["notice_id"] in reported or _recorded(conn, item["target_id"], user_id, notice["notice_id"]):
            return
        reported.add(notice["notice_id"])
        target = record_bounce(
            conn, item["target_id"], user_id=user_id, reason=notice["reason"], addresses=notice["failed"],
            source="gmail", sent=detail, notice_id=notice["notice_id"],
        )
        result["bounced"].append({
            "target_id": item["target_id"], "company": target["company"],
            "addresses": notice["failed"] or [str(detail.get("to", ""))], "reason": notice["reason"],
        })

    try:
        with client_factory() as client:
            gmail = _Gmail(conn, client, user_id)
            for item in due:
                detail = item["detail"]
                response = gmail.request(
                    "GET", f"/threads/{quote(str(detail['thread_id']), safe='')}",
                    params=[("format", "metadata"), *(("metadataHeaders", name) for name in _HEADERS)],
                )
                if response.status_code == 403:
                    raise _NeedsReconnect
                if response.status_code == 404:
                    continue  # the thread was deleted: nothing left to find in it
                if response.status_code != 200:
                    # Not read is not "no bounce": look again next time, and say so.
                    _forget(user_id, [item])
                    result["state"] = "unreachable"
                    continue
                result["checked"] += 1
                notice = _notice_in_thread(gmail, response.json(), str(detail["message_id"]))
                if notice:
                    record(item, notice)
            try:
                for item, notice in _searched_notices(gmail, conn, user_id, watched):
                    record(item, notice)
            except _Unreadable:
                result["state"] = "unreachable"
    except _NeedsReconnect:
        # Granted before the app asked to read mail: it can send but not see bounces.
        _forget(user_id, due)
        return {**result, "state": "needs_reconnect"}
    except GmailAuthError:
        return {**result, "state": "needs_reconnect"}
    except (httpx.HTTPError, ValueError):
        # ValueError: an answer that was not JSON.
        return {**result, "state": "unreachable"}
    return result
