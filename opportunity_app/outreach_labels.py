"""A Gmail label on every thread where someone at a company replied.

The student's replies are captured in outreach_inbox_messages. This step gives
each confirmed reply's whole thread (kind 'reply', whatever the via, the
student's own emails in it included) one label in the student's own mailbox,
``opportunities`` unless they chose another name. Replies captured before the
step existed are labelled too, 25 threads a pass. A message that joins a
labelled thread later (the thank-you the app sends, the student's own answer)
gets the label on a later pass, which is why a "sweep" lists mail from the last
few minutes and labels the labelled threads it finds. What the sweep has read is kept
in SWEEP_SETTING: the label it is for, the time it has read up to ("after") and,
when a listing was too long for one pass, "recheck", the labelled thread it has
re-read as far as, since the messages it did not list may belong to any of them.

Only ever an add, inside the student's own mailbox: the app sends nothing,
deletes nothing, archives or moves nothing, marks nothing read, and never
removes a label (messages.batchModify is only ever sent ``addLabelIds``). Possible
replies, bounces, automatic replies and application mail are never labelled.
Labels live on messages, not threads, so a thread's later messages do not
inherit it; drafts cannot carry one and are left out.

The label needs gmail.modify, which a connection made before this step lacks:
until the student reconnects Gmail the state is "needs_label_permission" and no
Gmail call is made for labelling. Turned off (an empty name) or paused with
"Pause all automation", it does nothing. Independent of both, one call per
connection asks Gmail which account it signed into, so a connection made with
the wrong Google account is noticed (state "wrong_account", nothing labelled).

``label_replies`` is a step of outreach_inbox.InboxWatcher and never imports it.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import unicodedata
from contextlib import ExitStack
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx

from . import automation
from .outreach_drafting import sender_account
from .outreach_gmail import (
    MODIFY_SCOPE,
    PROVIDER,
    SERVER_ERRORS,
    ClientFactory,
    GmailAuthError,
    GmailThrottled,
    _connector,
    _Gmail,
    _is_throttle,
    backoff_until,
)

LOGGER = logging.getLogger(__name__)

DEFAULT_LABEL = "opportunities"
# user_settings: no row means DEFAULT_LABEL, and '' means labelling is off.
SETTING = "outreach_gmail_label"
# user_settings, JSON {"label": name, "after": epoch seconds[, "recheck": thread id]}: the label the sweep is for, how
# far back it has read, and, when a listing was too long for one pass, how far through the labelled threads it has
# re-read since ('' = not yet started; the key is absent when no re-read is owed).
SWEEP_SETTING = "outreach_gmail_label_sweep"
HEALTH_COMPONENT = "inbox.labels"
# Threads asked of Gmail in one pass, so a backlog of replies takes a few passes, not one long one.
PER_PASS = 25
SWEEP_PAGES = 5
# Gmail's limit for one messages.batchModify.
BATCH_LIMIT = 1000
# The sweep reads from a little before where it stopped, so a message dated as the listing ran is not missed.
SWEEP_OVERLAP_SECONDS = 120
MAX_NAME_LENGTH = 100
# Names Gmail keeps for itself, in any case (labels.create refuses them).
_SYSTEM_NAMES = frozenset({
    "INBOX", "SPAM", "TRASH", "UNREAD", "STARRED", "IMPORTANT", "SENT", "DRAFT", "DRAFTS", "CHAT", "CHATS",
    "SCHEDULED", "SNOOZED", "ALL MAIL", "OUTBOX",
})
_IN_CHUNKS = 500

# The label's Gmail id, by (student, mailbox, name), so a pass asks Gmail for it once. A label id belongs to one
# mailbox and can be another label's in a different one, so a reconnect to another account never reuses it.
# Dropped when Gmail calls it invalid.
_IDS: dict[tuple[str, str, str], str] = {}
_IDS_LOCK = threading.Lock()


# --- The label's name -------------------------------------------------------------------


def label_name(conn: sqlite3.Connection, user_id: str) -> str:
    """The name replies are labelled with; '' when the student turned labelling off."""
    value = automation._setting(conn, user_id, SETTING)
    return DEFAULT_LABEL if value is None else value


def search_form(name: str) -> str:
    """How Gmail's search writes a label name: lowercase, with spaces and slashes as dashes."""
    return re.sub(r"[ /]+", "-", name.lower())


def set_label_name(conn: sqlite3.Connection, user_id: str, value: str | None) -> str:
    """Save the student's label name, in its own transaction, and return the name now in effect.

    None goes back to the default, '' turns labelling off. A ValueError says in a sentence what Gmail would refuse.
    """
    if value is None:
        with conn:
            conn.execute("DELETE FROM user_settings WHERE user_id=? AND key=?", (user_id, SETTING))
        return DEFAULT_LABEL
    name = " ".join(value.split())
    if name:
        if len(name) > MAX_NAME_LENGTH:
            raise ValueError(f"A Gmail label name can be at most {MAX_NAME_LENGTH} characters")
        if any(unicodedata.category(char).startswith("C") for char in name):
            raise ValueError("A Gmail label name cannot contain control characters")
        # The sweep writes the name into a Gmail search, where quotes, brackets, colons and the like are syntax.
        if any(char not in " -_/" and unicodedata.category(char)[0] not in "LMN" for char in name):
            raise ValueError("A Gmail label name can use only letters, digits, spaces, hyphens, underscores and slashes")
        if name.startswith("/") or name.endswith("/") or "//" in name:
            raise ValueError("A Gmail label name cannot start or end with a slash or have two slashes in a row")
        if name.upper() in _SYSTEM_NAMES or name.upper().startswith("CATEGORY_"):
            raise ValueError(f"Gmail keeps the name {name} for itself; choose another label name")
    with conn:
        automation._put_setting(conn, user_id, SETTING, name, utc_stamp())
    return name


def utc_stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# --- The watcher's step -----------------------------------------------------------------


class _Stop(Exception):
    """The pass ends with this state; what was settled before it stays settled."""

    def __init__(self, state: str):
        super().__init__(state)
        self.state = state


def _discard(conn: sqlite3.Connection) -> None:
    """Roll back a transaction left open, so no network call is made inside one and none is left behind."""
    try:
        if getattr(conn, "in_transaction", False):
            conn.rollback()
    except Exception:  # noqa: BLE001
        LOGGER.warning("Could not roll back after labelling replies", exc_info=True)


def _granted(row: sqlite3.Row) -> list[str]:
    try:
        granted = json.loads(row["scopes_json"] or "[]")
    except (TypeError, ValueError):
        return []
    return [str(scope) for scope in granted] if isinstance(granted, list) else []


def label_replies(
    conn: sqlite3.Connection, *, user_id: str, client_factory: ClientFactory, now: datetime | None = None,
) -> dict[str, Any]:
    """Label the threads of confirmed replies, and of the messages that joined them since. See the module docstring.

    ``state`` is "ok", "not_connected", "needs_reconnect", "throttled",
    "unreachable", "wrong_account", "needs_label_permission" (the connection
    predates the label, or Gmail refused it), or "label_refused" (Gmail would
    not create a label with that name).
    """
    now = now or datetime.now(timezone.utc)
    try:
        return _run(conn, user_id, client_factory, now)
    except _Stop as stop:
        return {"state": stop.state}
    except GmailAuthError:
        return {"state": "needs_reconnect"}
    except GmailThrottled:  # before HTTPError: it is one
        return {"state": "throttled"}
    except (httpx.HTTPError, ValueError):
        return {"state": "unreachable"}
    finally:
        _discard(conn)


def _run(conn: sqlite3.Connection, user_id: str, client_factory: ClientFactory, now: datetime) -> dict[str, Any]:
    row = _connector(conn, user_id)
    if row is None or row["status"] == "disconnected":
        return {"state": "not_connected"}
    if row["status"] != "connected":
        return {"state": "needs_reconnect"}
    if backoff_until(user_id) is not None:
        return {"state": "throttled"}
    known = str(row["account_email"] or "") if "account_email" in row.keys() else ""
    granted = _granted(row)
    _discard(conn)
    with ExitStack() as stack:
        gmail: list[_Gmail] = []

        def connection() -> _Gmail:
            if not gmail:
                gmail.append(_Gmail(conn, stack.enter_context(client_factory()), user_id))
                _discard(conn)  # the constructor's SELECT opens a transaction on PostgreSQL; no call is made inside one
            return gmail[0]

        if not known:
            profile = connection().request("GET", "/profile")
            address = str(profile.json().get("emailAddress") or "") if profile.status_code == 200 else ""
            if not address:
                return {"state": "unreachable"}
            with conn:
                conn.execute(
                    "UPDATE connector_accounts SET account_email=? WHERE user_id=? AND provider=? AND account_email=''",
                    (address, user_id, PROVIDER),
                )
            known = address
        expected = sender_account()
        if expected and known.casefold() != expected.casefold():
            return {"state": "wrong_account"}
        name = label_name(conn, user_id)
        if not name:
            return {"state": "ok", "detail": {"labels": "off"}}
        if automation.paused(conn, user_id):
            return {"state": "ok", "detail": {"labels": "paused"}}
        pending = [dict(item) for item in conn.execute(
            "SELECT gmail_id, thread_id, label_name FROM outreach_inbox_messages "
            "WHERE user_id=? AND kind='reply' AND label_name<>? ORDER BY received_at, gmail_id LIMIT ?",
            (user_id, name, PER_PASS + 1),
        ).fetchall()]
        if not pending and not _labelled_threads(conn, user_id, name):
            return {"state": "ok", "detail": {"labelled": 0}}
        if MODIFY_SCOPE not in granted:
            return {"state": "needs_label_permission"} if pending else {"state": "ok", "detail": {"labelled": 0}}
        _discard(conn)
        return _label(conn, connection(), user_id, known, name, pending, now)


def _labelled_threads(conn: sqlite3.Connection, user_id: str, name: str) -> list[str]:
    """The threads the app has already labelled under this name, in a stable order."""
    return [str(item[0]) for item in conn.execute(
        "SELECT DISTINCT thread_id FROM outreach_inbox_messages "
        "WHERE user_id=? AND kind='reply' AND label_name=? AND labeled_at IS NOT NULL AND thread_id<>'' ORDER BY thread_id",
        (user_id, name),
    ).fetchall()]


class _Labeller:
    """One pass: the label's id, and how many threads it has asked Gmail about."""

    def __init__(self, gmail: _Gmail, user_id: str, account: str, name: str):
        self.gmail, self.user_id, self.name = gmail, user_id, name
        self.key = (user_id, account.casefold(), name)
        self.threads = 0

    # -- The label ---------------------------------------------------------------------

    def label_id(self) -> str:
        with _IDS_LOCK:
            cached = _IDS.get(self.key)
        if cached:
            return cached
        found = self._listed() or self._created()
        with _IDS_LOCK:
            _IDS[self.key] = found
        return found

    def forget(self) -> None:
        with _IDS_LOCK:
            _IDS.pop(self.key, None)

    def _refuse(self, response: httpx.Response) -> None:
        """Stop the pass when Gmail's answer to a label call was a rate limit, a server error or a refused permission."""
        if _is_throttle(response) or response.status_code in SERVER_ERRORS:
            raise _Stop("throttled")
        if response.status_code == 403:
            raise _Stop("needs_label_permission")

    def _listed(self) -> str:
        """The id of the student's own label of this name, or ''. An exact name wins over one that differs in case or spacing."""
        response = self.gmail.request("GET", "/labels")
        self._refuse(response)
        if response.status_code != 200:
            raise _Stop("unreachable")
        mine = [item for item in response.json().get("labels", []) if isinstance(item, dict) and item.get("type") == "user"]
        for match in (lambda text: text == self.name, lambda text: " ".join(text.split()).casefold() == self.name.casefold()):
            for item in mine:
                if match(str(item.get("name", ""))) and item.get("id"):
                    return str(item["id"])
        return ""

    def _created(self) -> str:
        self._ready()
        response = self.gmail.request("POST", "/labels", json={
            "name": self.name, "labelListVisibility": "labelShow", "messageListVisibility": "show",
        })
        self._refuse(response)
        if 200 <= response.status_code < 300:
            found = str(response.json().get("id") or "")
            if found:
                return found
            raise _Stop("unreachable")
        if response.status_code == 409:
            # Another name that Gmail counts as this one (case, spacing) or a label made since the list.
            found = self._listed()
            if found:
                return found
        raise _Stop("label_refused")

    # -- One thread --------------------------------------------------------------------

    def _ready(self) -> None:
        """Before a call that changes something: not while Gmail has asked the app to slow down."""
        if backoff_until(self.user_id) is not None:
            raise _Stop("throttled")

    def thread(self, thread_id: str) -> tuple[str, list[str]]:
        """Add the label to every message of a thread that lacks it. (outcome, the thread's message ids as listed).

        The outcome is "labelled", "gone" (Gmail no longer has the thread) or
        "failed" (Gmail refused this thread). A rate limit or a refused
        permission raises _Stop.
        """
        self.threads += 1
        label_id = self.label_id()
        response = self.gmail.request("GET", f"/threads/{quote(thread_id, safe='')}", params={"format": "minimal"})
        if response.status_code == 404:
            return "gone", []
        self._refuse(response)
        if response.status_code != 200:
            return "failed", []
        messages = [item for item in response.json().get("messages", []) if isinstance(item, dict) and item.get("id")]
        listed = [str(item["id"]) for item in messages]
        if not listed:
            return "gone", []
        # Drafts cannot carry a label, and a message that has it needs nothing.
        targets = [
            str(item["id"]) for item in messages
            if "DRAFT" not in (item.get("labelIds") or []) and label_id not in (item.get("labelIds") or [])
        ]
        for start in range(0, len(targets), BATCH_LIMIT):
            if self._add(targets[start:start + BATCH_LIMIT], label_id) != "labelled":
                return "failed", listed
        return "labelled", listed

    def _add(self, ids: list[str], label_id: str) -> str:
        """messages.batchModify, adding the label and nothing else; retried once after Gmail called the label invalid."""
        for attempt in range(2):
            self._ready()
            response = self.gmail.request("POST", "/messages/batchModify", json={"ids": ids, "addLabelIds": [label_id]})
            self._refuse(response)
            if 200 <= response.status_code < 300:
                return "labelled"
            if attempt == 0 and response.status_code == 400 and _invalid_label(response):
                # The label was deleted or replaced in Gmail since its id was kept.
                self.forget()
                label_id = self.label_id()
                continue
            break
        return "failed"


def _invalid_label(response: httpx.Response) -> bool:
    try:
        error = response.json().get("error")
    except (ValueError, AttributeError):
        return False
    return isinstance(error, dict) and str(error.get("message", "")).startswith("Invalid label")


def _label(
    conn: sqlite3.Connection, gmail: _Gmail, user_id: str, account: str, name: str, pending: list[dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    labeller = _Labeller(gmail, user_id, account, name)
    counts = {"labelled": 0, "gone": 0, "failed": 0, "relabelled": 0}
    detail: dict[str, Any] = {"label": name}
    stamp = now.isoformat(timespec="seconds")
    done: dict[str, str] = {}
    settled: set[str] = set()
    more = False
    # The sweep's start point is written before anything is labelled, so a pass that stops early still leaves one
    # that predates what it labelled; the pass that writes it labelled every thread whole and has no sweep to do.
    fresh = _kept(conn, user_id, name) is None
    if fresh:
        _keep_watermark(conn, user_id, name, int(now.timestamp()), now)
    _discard(conn)

    def result(state: str = "ok") -> dict[str, Any]:
        return {"state": state, "detail": {**counts, "more": more, **detail}}

    def settle(gmail_ids: list[str], thread_id: str, outcome: str) -> None:
        """Mark these reply rows as settled under this name; the time is kept only when the label was added."""
        for begin in range(0, len(gmail_ids), _IN_CHUNKS):
            chunk = gmail_ids[begin:begin + _IN_CHUNKS]
            marks = ", ".join("?" for _ in chunk)
            with conn:
                rows = conn.execute(
                    f"SELECT gmail_id, label_name FROM outreach_inbox_messages "
                    f"WHERE user_id=? AND kind='reply' AND label_name<>? AND gmail_id IN ({marks})",
                    (user_id, name, *chunk),
                ).fetchall()
                conn.execute(
                    f"UPDATE outreach_inbox_messages SET label_name=?, labeled_at=?, label_note=?, "
                    f"thread_id=CASE WHEN thread_id='' THEN ? ELSE thread_id END "
                    f"WHERE user_id=? AND kind='reply' AND label_name<>? AND gmail_id IN ({marks})",
                    (name, stamp if outcome == "labelled" else None, "" if outcome == "labelled" else outcome, thread_id, user_id, name, *chunk),
                )
            settled.update(str(item[0]) for item in rows)
            counts[outcome] += len(rows)
            counts["relabelled"] += sum(1 for item in rows if item[1]) if outcome == "labelled" else 0

    def unlisted(gmail_id: str, thread_id: str) -> None:
        """A row whose message its thread's listing did not show: Gmail no longer has it (gone), or it is not one
        the thread shows (failed). Either way it leaves the queue; left pending it would be read again every pass
        and, being among the oldest, would crowd the newer replies out of the pass."""
        _discard(conn)
        settle([gmail_id], thread_id, "failed" if _thread_of(labeller, gmail_id) else "gone")

    try:
        for item in pending:
            gmail_id, thread_id = str(item["gmail_id"]), str(item["thread_id"] or "")
            if gmail_id in settled:
                continue
            if not thread_id:
                _discard(conn)
                thread_id = _thread_of(labeller, gmail_id)
                if not thread_id:
                    settle([gmail_id], "", "gone")
                    continue
                with conn:
                    conn.execute(
                        "UPDATE outreach_inbox_messages SET thread_id=? WHERE user_id=? AND gmail_id=? AND thread_id=''",
                        (thread_id, user_id, gmail_id),
                    )
            if thread_id in done:
                # Read already this pass: a row that was in the listing is settled, so this one was not listed.
                if done[thread_id] == "labelled":
                    unlisted(gmail_id, thread_id)
                else:
                    settle([gmail_id], thread_id, done[thread_id])
                continue
            if labeller.threads >= PER_PASS:
                more = True
                break
            _discard(conn)
            outcome, listed = labeller.thread(thread_id)
            done[thread_id] = outcome
            if outcome == "labelled":
                # Only rows whose message Gmail listed are marked labelled; one it did not list is looked up.
                settle(listed, thread_id, "labelled")
                if gmail_id not in listed:
                    unlisted(gmail_id, thread_id)
            else:
                settle([gmail_id], thread_id, outcome)
        more = more or len(pending) > PER_PASS  # the window was the oldest rows only
        _sweep(conn, labeller, user_id, name, now, done, detail, fresh)
    except _Stop as stop:
        return result(stop.state)
    return result()


def _thread_of(labeller: _Labeller, gmail_id: str) -> str:
    """The thread a captured message is in, read from Gmail; '' when Gmail no longer has the message."""
    response = labeller.gmail.request("GET", f"/messages/{quote(gmail_id, safe='')}", params={"format": "minimal"})
    if response.status_code == 404:
        return ""
    labeller._refuse(response)
    if response.status_code != 200:
        raise _Stop("unreachable")
    return str(response.json().get("threadId") or "")


def _kept(conn: sqlite3.Connection, user_id: str, name: str) -> dict[str, Any] | None:
    """The sweep's stored start for this label, or None when there is none for it (missing, unreadable, another label's)."""
    try:
        kept = json.loads(automation._setting(conn, user_id, SWEEP_SETTING) or "{}")
    except ValueError:
        return None
    if not isinstance(kept, dict) or kept.get("label") != name or not isinstance(kept.get("after"), (int, float)):
        return None
    return kept


def _sweep(
    conn: sqlite3.Connection, labeller: _Labeller, user_id: str, name: str, now: datetime, done: dict[str, str],
    detail: dict[str, Any], fresh: bool = False,
) -> None:
    """Label what joined a labelled thread since the last pass: the app's own thank-you, the student's answers.

    Reads Gmail's list of messages from a little before where the last sweep
    stopped and labels the threads in it that the app has labelled. The first
    pass under a name only records where to start (before it labels anything),
    since the threads it labelled were labelled whole. Mail that already has the
    label is left out of the listing, so a pass cut short by its thread budget
    carries on where it stopped. A listing too long for one pass moves the start
    on and asks for every labelled thread to be read again over the next passes,
    so what it did not list is not missed.
    """
    if fresh or not _labelled_threads(conn, user_id, name):
        return
    start = int(now.timestamp())
    kept = _kept(conn, user_id, name)
    if kept is None:  # a setting that cannot be read: start over from here
        _keep_watermark(conn, user_id, name, start, now)
        return
    wanted = set(_labelled_threads(conn, user_id, name))
    if "recheck" in kept:
        cursor = str(kept["recheck"] or "")
        for thread_id in sorted(wanted):
            if thread_id <= cursor:
                continue
            if thread_id not in done:
                if labeller.threads >= PER_PASS:
                    detail["more"] = True
                    _keep_watermark(conn, user_id, name, int(kept["after"]), now, recheck=cursor)
                    return
                _discard(conn)
                done[thread_id] = labeller.thread(thread_id)[0]
            cursor = thread_id
        _keep_watermark(conn, user_id, name, int(kept["after"]), now)
    _discard(conn)
    token = ""
    complete = False
    query = f"after:{int(kept['after']) - SWEEP_OVERLAP_SECONDS} -in:chats -in:drafts -label:{search_form(name)}"
    for _page in range(SWEEP_PAGES):
        params: dict[str, Any] = {"q": query, "maxResults": 100}
        if token:
            params["pageToken"] = token
        response = labeller.gmail.request("GET", "/messages", params=params)
        labeller._refuse(response)
        if response.status_code != 200:
            raise _Stop("unreachable")
        body = response.json()
        for message in body.get("messages", []) or []:
            thread_id = str(message.get("threadId") or "") if isinstance(message, dict) else ""
            if thread_id not in wanted or thread_id in done:
                continue
            if labeller.threads >= PER_PASS:
                detail["more"] = True
                return  # the rest waits; the watermark stays where it was
            done[thread_id] = labeller.thread(thread_id)[0]
        token = str(body.get("nextPageToken") or "")
        if not token:
            complete = True
            break
    if complete:
        _keep_watermark(conn, user_id, name, start, now)
    else:
        detail["sweep_truncated"] = True
        _keep_watermark(conn, user_id, name, start, now, recheck="")


def _keep_watermark(
    conn: sqlite3.Connection, user_id: str, name: str, after: int, now: datetime, recheck: str | None = None,
) -> None:
    """Store where the sweep starts; ``recheck`` (a thread id, '' for the first) is how far through the labelled threads it has re-read."""
    value: dict[str, Any] = {"label": name, "after": after}
    if recheck is not None:
        value["recheck"] = recheck
    with conn:
        automation._put_setting(conn, user_id, SWEEP_SETTING, json.dumps(value), now.isoformat(timespec="microseconds"))
