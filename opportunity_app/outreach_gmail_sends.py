"""Emails the student sends or schedules in Gmail itself, noticed by the app.

Gmail's own Schedule send works with the student's computer off, which the
app's scheduler cannot, and the Gmail API cannot schedule a send. So the app
writes the approved draft into Gmail Drafts (Open in Gmail), the student picks
a time there, and this module notices when the email actually goes: the
draft leaves Drafts and the message shows up in Sent. It is then recorded
exactly as a send from the app is (SENT_EVENT, the once-only claim, the
company moved to Sent), so bounce and reply watching take over.

Finding the sent message, most certain first: the draft's own message id,
now labelled SENT; a sent message in the draft's thread; a sent message to the
contact with the draft's subject, after the draft was made. A draft that left
Drafts but is not in Sent yet may be waiting in Gmail's Scheduled folder,
which is noted once so the card can say so.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx

from . import SERVER_INSTANCE
from .integrations.gmail_client import (
    ClientFactory,
    GmailAuthError,
    GmailNeedsReadScope,
    GmailThrottled,
    GmailUnreadable,
    LookSchedule,
    connection_state,
)
from .mail.message import header_map, received_or_epoch
from .outreach import DRAFT_KINDS, UNSENT_STATUSES, OutreachNotFoundError, log_event, get_target, update_target
from .outreach_gmail import DRAFT_EVENT, SENT_EVENT, SENT_STATUS, _already_sent, event_tie_order, last_bounces
from .mail.gmail_connection import connector_row, GmailClient
from .core.timestamps import utc_now
from .core.user_time import user_timezone

SCHEDULED_EVENT = "gmail_scheduled"
# A draft can be scheduled days ahead; past this it is no longer watched.
WATCH_DRAFTS_FOR = timedelta(days=30)
_HEADERS = ("To", "Cc", "Subject")
_LAST_LOOK: dict[tuple[str, str], datetime] = {}
_LOOK_LOCK = threading.Lock()
_IN_CHUNKS = 500


def _interval(age: timedelta) -> timedelta:
    """Often while the draft is new (a quick send), then less and less."""
    if age < timedelta(hours=1):
        return timedelta(minutes=2)
    if age < timedelta(days=1):
        return timedelta(minutes=15)
    return timedelta(hours=1)


# take_due marks the drafts due for a look as looked at now; forget gives the mark back for a look that failed: not read
# is not "not sent", so those drafts are looked at again on the next check, not an interval later.
_LOOKS = LookSchedule(
    _LAST_LOOK, _LOOK_LOCK, interval=_interval, key=lambda item: item["detail"]["draft_id"], started=lambda item: item["made"],
    forget_key=str,
)


def _pending(conn: sqlite3.Connection, user_id: str, now: datetime) -> list[dict[str, Any]]:
    """The newest Gmail draft of each unsent email, oldest first.

    Each item is light (target_id, made, detail): the target itself is read
    only for the drafts that are due for a look, by _with_targets.
    """
    cutoff = (now - WATCH_DRAFTS_FOR).isoformat(timespec="microseconds")
    rows = conn.execute(
        f"""
        SELECT e.target_id, e.detail, e.created_at FROM outreach_events e
        JOIN outreach_targets t ON t.id=e.target_id AND t.user_id=e.user_id
        WHERE e.user_id=? AND e.event_type=? AND e.created_at>=? ORDER BY e.created_at{event_tie_order(conn)}
        """,
        (user_id, DRAFT_EVENT, cutoff),
    ).fetchall()
    bounces = last_bounces(conn, user_id)
    newest: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        try:
            detail = json.loads(row["detail"])
        except (TypeError, ValueError):
            continue
        if not isinstance(detail, dict) or detail.get("kind") not in DRAFT_KINDS or not detail.get("draft_id"):
            continue
        made = datetime.fromisoformat(row["created_at"])
        bounce = bounces.get(row["target_id"])
        if bounce is not None and bounce > made:
            continue
        newest[(row["target_id"], detail["kind"])] = {"target_id": row["target_id"], "made": made, "detail": detail}
    unsent = {
        key: item for key, item in newest.items()
        if not _already_sent(conn, key[0], user_id, key[1], bounce=bounces.get(key[0]))
    }
    states = _target_states(conn, user_id, {target_id for target_id, _kind in unsent})
    pending = []
    for (target_id, kind), item in unsent.items():
        state = states.get(target_id)
        if state is None:
            continue  # deleted since the drafts were read
        status, sent_at = state
        waiting = not sent_at and status in UNSENT_STATUSES if kind == "initial" else status == "sent"
        if waiting:
            pending.append(item)
    return sorted(pending, key=lambda item: item["made"])


def _target_states(conn: sqlite3.Connection, user_id: str, target_ids: set[str]) -> dict[str, tuple[str, Any]]:
    """(status, sent_at) of each of these targets: all that deciding whether a draft is waiting needs of a target."""
    states: dict[str, tuple[str, Any]] = {}
    ids = sorted(target_ids)
    for begin in range(0, len(ids), _IN_CHUNKS):
        chunk = ids[begin:begin + _IN_CHUNKS]
        for row in conn.execute(
            f"SELECT id, status, sent_at FROM outreach_targets WHERE user_id=? AND id IN ({', '.join('?' for _ in chunk)})",
            (user_id, *chunk),
        ).fetchall():
            states[str(row["id"])] = (row["status"], row["sent_at"])
    return states


def _with_targets(conn: sqlite3.Connection, user_id: str, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The items, each with its target read in full (what _find_sent and _record_sent need).

    A target deleted since _pending is dropped, and its draft is forgotten so the next check looks at it again.
    """
    full = []
    for item in items:
        try:
            target = get_target(conn, item["target_id"], user_id=user_id)
        except OutreachNotFoundError:
            _LOOKS.forget(user_id, [item])
            continue
        full.append({**item, "target": target})
    return full


def _get(gmail: GmailClient, path: str, **params: Any) -> dict[str, Any] | None:
    """A Gmail read: the answer, or None when the thing is gone. Any other error is raised."""
    response = gmail.request("GET", path, params=params or None)
    if response.status_code == 404:
        return None
    if response.status_code == 403:
        raise GmailNeedsReadScope
    if response.status_code != 200:
        raise GmailUnreadable
    return response.json()


def _metadata(gmail: GmailClient, message_id: str) -> dict[str, Any] | None:
    return _get(gmail, f"/messages/{quote(message_id, safe='')}",
                format="metadata", metadataHeaders=list(_HEADERS))


def _addresses(value: str) -> set[str]:
    return {address.casefold() for _name, address in getaddresses([value]) if "@" in address}


def _same_subject(one: str, other: str) -> bool:
    def plain(text: str) -> str:
        text = " ".join(text.split()).casefold()
        while text.startswith("re:"):
            text = text[3:].strip()
        return text

    return bool(one.strip()) and plain(one) == plain(other)


def _find_sent(gmail: GmailClient, item: dict[str, Any], seen: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The message the student sent from this draft, if Gmail has sent it.

    ``seen`` receives the draft's own message as read here ("own"), so _scheduled need not read it again.
    """
    detail, target = item["detail"], item["target"]
    subject_field = DRAFT_KINDS[detail["kind"]][0]
    subject, contact = target[subject_field], target["contact_email"].casefold()
    made_ms = int(item["made"].timestamp() * 1000)
    own = _metadata(gmail, str(detail.get("message_id", ""))) if detail.get("message_id") else None
    if seen is not None:
        seen["own"] = own
    if own and "SENT" in (own.get("labelIds") or []) and "DRAFT" not in (own.get("labelIds") or []):
        return own
    since = (item["made"] - timedelta(days=1)).strftime("%Y/%m/%d")
    listing = _get(gmail, "/messages", q=f"in:sent to:{contact} after:{since}", maxResults=20)
    for reference in (listing or {}).get("messages") or []:
        message = _metadata(gmail, str(reference.get("id", "")))
        if not message or int(message.get("internalDate") or 0) < made_ms:
            continue
        headers = header_map(message)
        same_thread = bool(detail.get("thread_id")) and message.get("threadId") == detail.get("thread_id")
        to_contact = contact in _addresses(headers.get("to", ""))
        if same_thread or (to_contact and _same_subject(headers.get("subject", ""), subject)):
            return message
    return None


def _scheduled(gmail: GmailClient, item: dict[str, Any], seen: dict[str, Any] | None = None) -> bool:
    """Whether the draft is waiting in Gmail's Scheduled folder. False when Gmail cannot say.

    ``seen`` is what _find_sent read of the draft's own message a moment ago; it is read again only when absent.
    """
    detail = item["detail"]
    if seen is not None and "own" in seen:
        own = seen["own"]
    else:
        own = _metadata(gmail, str(detail.get("message_id", ""))) if detail.get("message_id") else None
    return bool(own) and "SCHEDULED" in (own.get("labelIds") or [])


def _record_sent(conn: sqlite3.Connection, item: dict[str, Any], message: dict[str, Any], *, user_id: str) -> dict[str, Any]:
    """Record a send made in Gmail exactly as a send from the app, and move the company on."""
    detail, target = item["detail"], item["target"]
    kind, target_id = detail["kind"], target["id"]
    headers = header_map(message)
    sent_at = received_or_epoch(message)
    record = {
        "kind": kind, "fingerprint": detail.get("fingerprint", ""), "attachment": detail.get("attachment", ""),
        "to": target["contact_email"], "cc": target["contact_cc"],
        "message_id": str(message.get("id", "")), "thread_id": str(message.get("threadId", "")),
        "sent_from": "gmail", "sent_to_header": headers.get("to", "")[:500],
        # When Gmail sent it, which can be hours before the app noticed: replies count from then (outreach_inbox).
        "sent_ms": int(message.get("internalDate") or 0),
    }
    stamp = utc_now()
    with conn:
        # The same once-only record a send from the app leaves.
        conn.execute(
            """
            INSERT INTO outreach_send_claims(target_id, user_id, kind, token, state, action, instance, claimed_at)
            VALUES(?, ?, ?, ?, 'sent', 'send', ?, ?)
            ON CONFLICT(target_id, kind) DO UPDATE SET state='sent', token=excluded.token, action='send',
                instance=excluded.instance, claimed_at=excluded.claimed_at
            """,
            (target_id, user_id, kind, uuid4().hex, SERVER_INSTANCE, stamp),
        )
        log_event(conn, target_id, user_id, SENT_EVENT, detail=json.dumps(record, sort_keys=True))
    from .outreach_schedule import cancel_send  # imported here: scheduling imports the Gmail send path

    cancel_send(conn, target_id, user_id=user_id, kind=kind, reason="You sent it from Gmail instead")
    local_day = user_timezone(conn, user_id).to_local(sent_at).date().isoformat()
    changes: dict[str, Any] = {"status": SENT_STATUS[kind]}
    if kind == "initial":
        changes["sent_at"] = local_day
    update_target(conn, target_id, changes, user_id=user_id)
    return {"target_id": target_id, "company": target["company"], "kind": kind, "sent_at": sent_at.isoformat(timespec="seconds")}


def _scheduled_noted(conn: sqlite3.Connection, target_id: str, user_id: str, draft_id: str) -> bool:
    for row in conn.execute(
        "SELECT detail FROM outreach_events WHERE target_id=? AND user_id=? AND event_type=?", (target_id, user_id, SCHEDULED_EVENT),
    ).fetchall():
        try:
            if json.loads(row["detail"]).get("draft_id") == draft_id:
                return True
        except (TypeError, ValueError, AttributeError):
            continue
    return False


def capture_gmail_sends(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    client_factory: ClientFactory,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Notice the app's Gmail drafts that the student sent, or scheduled, in Gmail.

    ``state`` is as for check_deliveries. Nothing is asked of Gmail when no
    draft is waiting or none is due for a look.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    result: dict[str, Any] = {"state": "ok", "sent": [], "scheduled": []}
    due = _LOOKS.take_due(user_id, _pending(conn, user_id, now), now)
    if not due:
        return result
    state = connection_state(connector_row(conn, user_id))
    if state != "connected":
        return {**result, "state": state}
    try:
        due = _with_targets(conn, user_id, due)
    except BaseException:
        _LOOKS.forget(user_id, due)  # a look that fails is forgotten
        raise
    try:
        with client_factory() as client:
            gmail = GmailClient(conn, client, user_id)
            for item in due:
                detail = item["detail"]
                try:
                    if _get(gmail, f"/drafts/{quote(str(detail['draft_id']), safe='')}", format="minimal") is not None:
                        continue  # still a draft: not sent or scheduled yet
                    seen: dict[str, Any] = {}
                    message = _find_sent(gmail, item, seen)
                    if message is not None:
                        result["sent"].append(_record_sent(conn, item, message, user_id=user_id))
                        continue
                    target_id = item["target"]["id"]
                    if _scheduled(gmail, item, seen) and not _scheduled_noted(conn, target_id, user_id, str(detail["draft_id"])):
                        with conn:
                            log_event(conn, target_id, user_id, SCHEDULED_EVENT, detail=json.dumps(
                                {"draft_id": str(detail["draft_id"]), "kind": detail["kind"]}, sort_keys=True,
                            ))
                        result["scheduled"].append({"target_id": target_id, "company": item["target"]["company"]})
                except GmailUnreadable:
                    # Not read is not "not sent": look again next time.
                    _LOOKS.forget(user_id, [item])
                    result["state"] = "unreachable"
    except GmailNeedsReadScope:
        _LOOKS.forget(user_id, due)
        return {**result, "state": "needs_reconnect"}
    except GmailAuthError:
        _LOOKS.forget(user_id, due)
        return {**result, "state": "needs_reconnect"}
    except GmailThrottled:
        # Not read is not "not sent": look again as soon as Gmail allows.
        _LOOKS.forget(user_id, due)
        return {**result, "state": "throttled"}
    except (httpx.HTTPError, ValueError):
        _LOOKS.forget(user_id, due)
        return {**result, "state": "unreachable"}
    except BaseException:
        _LOOKS.forget(user_id, due)
        raise
    return result
