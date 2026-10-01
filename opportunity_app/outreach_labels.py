"""A Gmail label on every outreach thread: the emails the student sent, and the replies.

Two kinds of thread carry it, in the student's own mailbox, ``opportunities``
unless they chose another name. **Reply threads**: the replies captured in
outreach_inbox_messages (kind 'reply', whatever the via) with the whole thread,
the student's own emails in it included. **Sent threads**: every thread that
holds an outreach email the student sent, whether or not anyone answered,
recorded in outreach_label_threads. Those come from the app's own sends (the
threads its gmail_sent and thank_you_sent events name), from a one-time search
of Sent for each company that has gone out (its addresses and the subjects only
it uses, from 30 days before it was added, and again after the company changes;
the student often sent from Gmail after the app made a draft), and from the
sweep below. Threads found before the step existed are labelled too, 25 threads a
pass, and 10 companies are searched a pass. A message that joins a labelled
thread later (the thank-you the app sends, the student's own answer) gets the
label on a later pass, which is why a "sweep" lists mail from the last few
minutes and labels the labelled threads it finds; it also lists recent Sent mail
and labels the threads of any that went to an outreach address or carries an
outreach subject. What the sweep has read is kept in SWEEP_SETTING: the label it
is for, the time it has read up to ("after") and, when a listing was too long
for one pass, "recheck", the labelled thread it has re-read as far as, since the
messages it did not list may belong to any of them. A Sent listing too long for
one pass makes every company be searched again, and so does a different label name.

Only ever an add, inside the student's own mailbox: the app sends nothing,
deletes nothing, archives or moves nothing, marks nothing read, and never
removes a label (messages.batchModify is only ever sent ``addLabelIds``). Possible
replies, bounces, automatic replies and application mail are never labelled, and
neither is a delivery notice (a failure or a delay) inside a labelled thread. Labels live on
messages, not threads, so a thread's later messages do not inherit it; drafts
cannot carry one and are left out.

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
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses
from typing import Any
from urllib.parse import quote

import httpx

from . import automation
from .database import rollback_quietly
from .mail_trust import FREEMAIL, registrable_domain
from .outreach import UNSENT_STATUSES
from .outreach_delivery import _DAEMONS, _is_delivery_notice
from .outreach_drafting import sender_account
from .outreach_gmail import (
    DRAFT_EVENT,
    MODIFY_SCOPE,
    PROVIDER,
    SENT_EVENT,
    SERVER_ERRORS,
    THANK_YOU_SENT_EVENT,
    ClientFactory,
    GmailAuthError,
    GmailThrottled,
    _connector,
    _Gmail,
    _is_throttle,
    backoff_until,
)
from .timestamps import parse_app_instant, utc_now

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
# Companies whose Sent mail is searched in one pass, and what one search may read and keep: pages of results, pages
# per search and threads per company.
SEARCHES_PER_PASS = 10
SEARCH_RESULTS = 100
SEARCH_PAGES = 5
SEARCH_THREADS = 100
# A company's Sent search starts this long before the day it was added: the student sometimes writes to a company
# before adding it here.
HISTORY_LEAD_DAYS = 30
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
# A subject shorter than this is too generic to tell outreach from other mail.
MIN_SUBJECT = 12
# The longest subject phrase a Sent search carries.
SUBJECT_PHRASE = 100
# What the label step reads of a message's thread: enough to tell a delivery failure notice from mail.
_THREAD_HEADERS = ("From", "Subject", "Content-Type", "X-Failed-Recipients")
_ADDRESS_PART = r"[^\s@\"'(){}<>,;:\\]+"
_ADDRESS = re.compile(rf"^{_ADDRESS_PART}@{_ADDRESS_PART}\.{_ADDRESS_PART}$")
_REPLY_PREFIX = re.compile(r"^(?:re|fwd?)\s*:\s*", re.IGNORECASE)

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
    A different name in effect makes every company that has gone out be searched again: the Sent mail written since the
    last name's sweep last ran (labelling off, a rename) was never listed under this one.
    """
    before = label_name(conn, user_id)
    if value is None:
        with conn:
            conn.execute("DELETE FROM user_settings WHERE user_id=? AND key=?", (user_id, SETTING))
            _search_again(conn, user_id, before, DEFAULT_LABEL)
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
        automation._put_setting(conn, user_id, SETTING, name, utc_now())
        _search_again(conn, user_id, before, name)
    return name


def _search_again(conn: sqlite3.Connection, user_id: str, before: str, name: str) -> None:
    """Forget which companies' Sent mail was searched when labelling starts under a name other than the last one. Opens no transaction."""
    if name and name != before:
        conn.execute("DELETE FROM outreach_label_searches WHERE user_id=?", (user_id,))


# --- The watcher's step -----------------------------------------------------------------


class _Stop(Exception):
    """The pass ends with this state; what was settled before it stays settled."""

    def __init__(self, state: str):
        super().__init__(state)
        self.state = state


def _discard(conn: sqlite3.Connection) -> None:
    """Roll back a transaction left open, so no network call is made inside one and none is left behind."""
    rollback_quietly(conn, LOGGER, "labelling replies")


def _granted(row: sqlite3.Row) -> list[str]:
    try:
        granted = json.loads(row["scopes_json"] or "[]")
    except (TypeError, ValueError):
        return []
    return [str(scope) for scope in granted] if isinstance(granted, list) else []


def label_replies(
    conn: sqlite3.Connection, *, user_id: str, client_factory: ClientFactory, now: datetime | None = None,
) -> dict[str, Any]:
    """Label every outreach thread: what the student sent, the confirmed replies, and what joined them since.

    See the module docstring; the name is the one the watcher already calls.

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
        _record_sent_threads(conn, user_id, now)
        pending = [dict(item) for item in conn.execute(
            "SELECT gmail_id, thread_id, label_name FROM outreach_inbox_messages "
            "WHERE user_id=? AND kind='reply' AND label_name<>? ORDER BY received_at, gmail_id LIMIT ?",
            (user_id, name, PER_PASS + 1),
        ).fetchall()]
        marks = _Marks(conn, user_id)
        work = bool(pending) or bool(_sent_rows(conn, user_id, name, 1)) or bool(_search_candidates(conn, user_id, 1, marks=marks))
        if not work and not _labelled_threads(conn, user_id, name) and not _sweeping_sent(conn, user_id, name, known, marks):
            return {"state": "ok", "detail": {"labelled": 0}}
        if MODIFY_SCOPE not in granted:
            return {"state": "needs_label_permission"} if work else {"state": "ok", "detail": {"labelled": 0}}
        _discard(conn)
        return _label(conn, connection(), user_id, known, name, pending, now, marks)


def _labelled_threads(conn: sqlite3.Connection, user_id: str, name: str) -> list[str]:
    """The threads the app has already labelled under this name (replies and sent mail), in a stable order."""
    return [str(item[0]) for item in conn.execute(
        "SELECT thread_id FROM outreach_inbox_messages "
        "WHERE user_id=? AND kind='reply' AND label_name=? AND labeled_at IS NOT NULL AND thread_id<>'' "
        "UNION SELECT thread_id FROM outreach_label_threads WHERE user_id=? AND label_name=? AND labeled_at IS NOT NULL "
        "ORDER BY thread_id",
        (user_id, name, user_id, name),
    ).fetchall()]


# --- Sent threads: which they are, and what marks an email as outreach ---------------------


def _record_sent_threads(conn: sqlite3.Connection, user_id: str, now: datetime) -> None:
    """Note the thread of every email the app itself sent (a first email, a follow-up, a thank-you). No Gmail call."""
    found: dict[str, str] = {}
    for row in conn.execute(
        "SELECT target_id, detail FROM outreach_events WHERE user_id=? AND event_type IN (?, ?) ORDER BY created_at, id",
        (user_id, SENT_EVENT, THANK_YOU_SENT_EVENT),
    ).fetchall():
        thread_id = str(_json_dict(row["detail"]).get("thread_id") or "").strip()
        if thread_id:
            found.setdefault(thread_id, str(row["target_id"]))
    if not found:
        return
    known = {str(item[0]) for item in conn.execute(
        "SELECT thread_id FROM outreach_label_threads WHERE user_id=?", (user_id,),
    ).fetchall()}
    fresh = [(thread_id, target_id) for thread_id, target_id in found.items() if thread_id not in known]
    if fresh:
        with conn:
            for thread_id, target_id in fresh:
                _add_thread(conn, user_id, thread_id, target_id, "sent", now)


def _add_thread(conn: sqlite3.Connection, user_id: str, thread_id: str, target_id: str, source: str, now: datetime) -> None:
    """Note a thread that holds outreach the student sent, unless it is noted already. Opens no transaction."""
    conn.execute(
        "INSERT INTO outreach_label_threads(user_id, thread_id, target_id, source, found_at) VALUES(?, ?, ?, ?, ?) "
        "ON CONFLICT(user_id, thread_id) DO NOTHING",
        (user_id, thread_id, target_id, source, now.isoformat(timespec="microseconds")),
    )


def _sent_rows(conn: sqlite3.Connection, user_id: str, name: str, limit: int) -> list[dict[str, Any]]:
    """Sent threads not yet settled under this name, oldest found first."""
    return [dict(item) for item in conn.execute(
        "SELECT thread_id, target_id FROM outreach_label_threads WHERE user_id=? AND label_name<>? "
        "ORDER BY found_at, thread_id LIMIT ?",
        (user_id, name, limit),
    ).fetchall()]


def _json_dict(text: Any) -> dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _addresses(*values: Any) -> list[str]:
    """Plausible, lowercased, de-duplicated email addresses out of strings (or lists of strings) that may hold several."""
    found: list[str] = []
    for value in values:
        for text in (value if isinstance(value, list) else [value]):
            if not isinstance(text, str) or not text:
                continue
            for _display, address in getaddresses([text]):
                address = address.strip().casefold()
                if _ADDRESS.match(address) and address not in found:
                    found.append(address)
    return found


def _subject_key(subject: Any) -> str:
    """A subject as it compares: reply and forward prefixes and repeated spaces gone, in one case."""
    text = " ".join(str(subject or "").split())
    while True:
        bare = _REPLY_PREFIX.sub("", text, count=1).strip()
        if bare == text:
            return text.casefold()
        text = bare


def _own_addresses(account: str) -> set[str]:
    return {address.casefold() for address in (account, sender_account()) if address}


def _outreach_marks(conn: sqlite3.Connection, user_id: str, own: set[str]) -> dict[str, dict[str, Any]]:
    """Per company: when it was added and sent, the addresses mail to it went to, and the subjects its emails carry.

    Never the student's own address. An address only copied on the emails (a referrer, a mentor) is the company's
    only when it is at one of the company's own mail domains, as the reply watcher decides. A subject shorter
    than MIN_SUBJECT is left out: it would match other mail, and so is one that another company of the student's has too.
    """
    marks: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT id, created_at, sent_at, contact_email, contact_cc, bounced_addresses_json, email_subject, follow_up_subject "
        "FROM outreach_targets WHERE user_id=? ORDER BY created_at, id",
        (user_id,),
    ).fetchall():
        try:
            bounced = json.loads(row["bounced_addresses_json"] or "[]")
        except (TypeError, ValueError):
            bounced = []
        subjects: list[dict[str, str]] = []
        for subject in (row["email_subject"], row["follow_up_subject"]):
            key = _subject_key(subject)
            phrase = " ".join(re.sub(r'["{}()]', "", str(subject or "")).split())
            if len(phrase) > SUBJECT_PHRASE:
                # Gmail's phrase search matches whole words, so a cut inside a word would find nothing.
                cut = phrase[:SUBJECT_PHRASE + 1]
                phrase = cut.rsplit(" ", 1)[0] if " " in cut else phrase[:SUBJECT_PHRASE]
            if len(key) >= MIN_SUBJECT and phrase and all(item["key"] != key for item in subjects):
                subjects.append({"key": key, "phrase": phrase})
        marks[str(row["id"])] = {
            "created_at": str(row["created_at"] or ""),
            "sent_at": str(row["sent_at"] or ""),
            # (address, whether it was only copied), in the order they are found; settled below.
            "found": [(address, False) for address in _addresses(row["contact_email"])]
            + [(address, True) for address in _addresses(row["contact_cc"])]
            + [(address, False) for address in _addresses(bounced if isinstance(bounced, list) else [])],
            "subjects": subjects,
        }
    # A subject that two companies share cannot say which of them mail belongs to; it marks neither.
    held: dict[str, int] = {}
    for mark in marks.values():
        for subject in mark["subjects"]:
            held[subject["key"]] = held.get(subject["key"], 0) + 1
    for mark in marks.values():
        mark["subjects"] = [subject for subject in mark["subjects"] if held[subject["key"]] == 1]
    for row in conn.execute(
        "SELECT target_id, detail FROM outreach_events WHERE user_id=? AND event_type IN (?, ?, ?) ORDER BY created_at, id",
        (user_id, SENT_EVENT, DRAFT_EVENT, THANK_YOU_SENT_EVENT),
    ).fetchall():
        mark = marks.get(str(row["target_id"]))
        if mark is None:
            continue
        detail = _json_dict(row["detail"])
        mark["found"] += [(address, False) for address in _addresses(detail.get("to"))]
        mark["found"] += [(address, True) for address in _addresses(detail.get("cc"))]
    shared = {host for address in own for host in (_host(address), registrable_domain(address) or "") if host}
    for mark in marks.values():
        found = [(address, copied) for address, copied in mark.pop("found") if address not in own]
        # The company's mail domains: those of the addresses written to, never a free mail service or the student's own.
        hosts = {_host(address) for address, copied in found if not copied}
        hosts = {host for host in hosts if host not in FREEMAIL and (registrable_domain(host) or host) not in FREEMAIL
                 and host not in shared and (registrable_domain(host) or host) not in shared}
        primary = {address for address, copied in found if not copied}
        addresses: list[str] = []
        for address, _copied in found:
            if address not in addresses and (address in primary or any(
                    _host(address) == host or _host(address).endswith(f".{host}") for host in hosts)):
                addresses.append(address)
        mark["addresses"] = addresses
    return marks


class _Marks:
    """_outreach_marks for steps of one label pass that run back to back: built the first time a set of own addresses asks, then shared.

    A pass writes only outreach_label_threads, outreach_label_searches and the label columns, never the targets or
    events the marks read. But the student can change those at any moment, and a pass waits on Gmail between its steps
    with no transaction open, so marks built before a wait may name a company deleted or edited during it. A step that
    follows a wait calls ``forget`` first and builds its own, as it did before the marks were shared; only steps with
    nothing between them but reads of this database share one build. Each caller still names its own addresses and
    gets the marks for exactly that set; a caller that names none builds nothing until it asks. The marks are shared,
    so callers only read them.
    """

    def __init__(self, conn: sqlite3.Connection, user_id: str):
        self.conn, self.user_id = conn, user_id
        self._built: dict[frozenset[str], dict[str, dict[str, Any]]] = {}

    def forget(self) -> None:
        """Drop what was built, because Gmail was waited on since (the next ``get`` reads the database again)."""
        self._built.clear()

    def get(self, own: set[str]) -> dict[str, dict[str, Any]]:
        key = frozenset(own)
        if key not in self._built:
            self._built[key] = _outreach_marks(self.conn, self.user_id, own)
        return self._built[key]


def _host(address: str) -> str:
    return address.rsplit("@", 1)[-1]


def _index(marks: dict[str, dict[str, Any]]) -> tuple[dict[str, str], dict[str, str]]:
    """Address to company and normalised subject to company, over every company; the first one in a stable order wins."""
    addresses: dict[str, str] = {}
    subjects: dict[str, str] = {}
    for target_id in sorted(marks):
        for address in marks[target_id]["addresses"]:
            addresses.setdefault(address, target_id)
        for subject in marks[target_id]["subjects"]:
            subjects.setdefault(subject["key"], target_id)
    return addresses, subjects


def _sweeping_sent(conn: sqlite3.Connection, user_id: str, name: str, account: str, marks: _Marks | None = None) -> bool:
    """Whether the sweep has sent mail to look for: it has started under this name and some company has an address or subject."""
    if _kept(conn, user_id, name) is None:
        return False
    addresses, subjects = _index((marks or _Marks(conn, user_id)).get(_own_addresses(account)))
    return bool(addresses or subjects)


def _search_candidates(
    conn: sqlite3.Connection, user_id: str, limit: int, account: str = "", marks: _Marks | None = None,
) -> list[tuple[str, str]]:
    """(company, query) for each company whose Sent mail has not been searched with the query it has now.

    Only companies that have gone out, or that an app send names. A company is searched once, and once more only when
    what it would be searched by changes (an address added, a subject or a sent date edited): other edits to the
    company leave it alone. The query is '' when there is nothing to search by.
    """
    unsent = sorted(UNSENT_STATUSES)
    gone_out = [str(item[0]) for item in conn.execute(
        "SELECT t.id FROM outreach_targets t WHERE t.user_id=? "
        f"AND (t.status NOT IN ({', '.join('?' for _ in unsent)}) OR EXISTS ("
        "SELECT 1 FROM outreach_events e WHERE e.target_id=t.id AND e.user_id=t.user_id AND e.event_type=?)) "
        "ORDER BY t.created_at, t.id",
        (user_id, *unsent, SENT_EVENT),
    ).fetchall()]
    if not gone_out:
        return []
    searched = {str(item[0]): str(item[1]) for item in conn.execute(
        "SELECT target_id, query FROM outreach_label_searches WHERE user_id=?", (user_id,),
    ).fetchall()}
    built = (marks or _Marks(conn, user_id)).get(_own_addresses(account))
    wanted: list[tuple[str, str]] = []
    for target_id in gone_out:
        query = _history_query(built.get(target_id))
        if searched.get(target_id) != query:
            wanted.append((target_id, query))
            if len(wanted) >= limit:
                break
    return wanted


def _history_query(mark: dict[str, Any] | None) -> str:
    """The Sent search for one company's history, or '' when it has no address or subject to search by."""
    if not mark:
        return ""
    terms: list[str] = []
    for address in mark["addresses"]:
        terms.extend((f"to:{address}", f"cc:{address}", f"bcc:{address}"))
    terms.extend(f'subject:"{subject["phrase"]}"' for subject in mark["subjects"])
    if not terms:
        return ""
    # An email can predate the company's being added (the student may write first, or add it as already sent): the earlier of the two dates, less the lead.
    starts = [start for start in (_epoch(mark["created_at"]), _epoch(mark["sent_at"])) if start is not None]
    since = min(starts) - HISTORY_LEAD_DAYS * 86400 if starts else None
    return "in:sent " + (f"after:{since} " if since is not None else "") + "{" + " ".join(terms) + "}"


def _epoch(text: str) -> int | None:
    moment = parse_app_instant(text.strip())
    return int(moment.timestamp()) if moment else None


class _Labeller:
    """One pass: the label's id, and how many threads it has asked Gmail about."""

    def __init__(self, gmail: _Gmail, user_id: str, account: str, name: str):
        self.gmail, self.user_id, self.name, self.account = gmail, user_id, name, account
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
        """Add the label to every message of a thread that lacks it, delivery notices apart. (outcome, the thread's message ids as listed).

        The outcome is "labelled", "gone" (Gmail no longer has the thread) or
        "failed" (Gmail refused this thread). A rate limit or a refused
        permission raises _Stop.
        """
        self.threads += 1
        label_id = self.label_id()
        response = self.gmail.request("GET", f"/threads/{quote(thread_id, safe='')}", params={
            "format": "metadata", "metadataHeaders": list(_THREAD_HEADERS),
        })
        if response.status_code == 404:
            return "gone", []
        self._refuse(response)
        if response.status_code != 200:
            return "failed", []
        messages = [item for item in response.json().get("messages", []) if isinstance(item, dict) and item.get("id")]
        listed = [str(item["id"]) for item in messages]
        if not listed:
            return "gone", []
        # Drafts cannot carry a label, a message that has it needs nothing, and a delivery notice (failure or delay) is not outreach.
        targets = [
            str(item["id"]) for item in messages
            if "DRAFT" not in (item.get("labelIds") or []) and label_id not in (item.get("labelIds") or [])
            and not _is_delivery_notice(item)
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
    now: datetime, marks: _Marks | None = None,
) -> dict[str, Any]:
    labeller = _Labeller(gmail, user_id, account, name)
    marks = marks or _Marks(conn, user_id)
    counts = {"labelled": 0, "gone": 0, "failed": 0, "relabelled": 0, "sent_labelled": 0, "searched": 0, "found": 0}
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

    def settle_sent(thread_id: str, outcome: str) -> None:
        _settle_sent(conn, user_id, name, thread_id, outcome, stamp, counts)

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
        _discard(conn)
        marks.forget()  # the replies above were labelled by waiting on Gmail
        _search_history(conn, labeller, user_id, now, counts, detail, marks)
        sent = _sent_rows(conn, user_id, name, PER_PASS + 1)
        for item in sent:
            thread_id = str(item["thread_id"])
            if thread_id in done:
                settle_sent(thread_id, done[thread_id])
                continue
            if labeller.threads >= PER_PASS:
                more = True
                break
            _discard(conn)
            done[thread_id] = labeller.thread(thread_id)[0]
            settle_sent(thread_id, done[thread_id])
        more = more or len(sent) > PER_PASS
        marks.forget()  # the history searches and sent threads above waited on Gmail
        _sweep(conn, labeller, user_id, name, now, done, detail, fresh, counts, marks)
    except _Stop as stop:
        return result(stop.state)
    return result()


def _settle_sent(
    conn: sqlite3.Connection, user_id: str, name: str, thread_id: str, outcome: str, stamp: str, counts: dict[str, int],
) -> None:
    """A sent thread is settled under this name; the time is kept only when the label was added."""
    with conn:
        conn.execute(
            "UPDATE outreach_label_threads SET label_name=?, labeled_at=?, label_note=? WHERE user_id=? AND thread_id=?",
            (name, stamp if outcome == "labelled" else None, "" if outcome == "labelled" else outcome, user_id, thread_id),
        )
    counts["sent_labelled" if outcome == "labelled" else outcome] += 1


def _search_history(
    conn: sqlite3.Connection, labeller: _Labeller, user_id: str, now: datetime, counts: dict[str, int], detail: dict[str, Any],
    marks: _Marks | None = None,
) -> None:
    """Search Sent for each company that has gone out and not been searched since it last changed, for the threads the app did not send itself.

    The student often sends from Gmail after the app made a draft, and a draft's
    thread does not keep the sent mail. The search is bounded by the company's
    addresses and subjects and by HISTORY_LEAD_DAYS before the earlier of the day it was added and its sent date. A company is marked
    searched only when Gmail answered, so a failed search is tried again. It reads up to SEARCH_PAGES pages and keeps up to
    SEARCH_THREADS threads; a search cut short by either says so in ``search_truncated`` rather than passing as complete.
    """
    marks = marks or _Marks(conn, user_id)
    candidates = _search_candidates(conn, user_id, SEARCHES_PER_PASS, labeller.account, marks)
    if not candidates:
        return
    built = marks.get(_own_addresses(labeller.account))
    for target_id, query in candidates:
        mark = built.get(target_id) or {"created_at": "", "sent_at": "", "addresses": [], "subjects": []}
        threads: list[str] = []
        truncated = False
        if query:
            own_addresses = {address: target_id for address in mark["addresses"]}
            own_subjects = {subject["key"]: target_id for subject in mark["subjects"]}
            token = ""
            for _page in range(SEARCH_PAGES):
                params: dict[str, Any] = {"q": query, "maxResults": SEARCH_RESULTS}
                if token:
                    params["pageToken"] = token
                _discard(conn)
                response = labeller.gmail.request("GET", "/messages", params=params)
                labeller._refuse(response)
                if response.status_code != 200:
                    raise _Stop("unreachable")
                body = response.json()
                for message in body.get("messages", []) or []:
                    thread_id = str(message.get("threadId") or "") if isinstance(message, dict) else ""
                    if not thread_id or thread_id in threads:
                        continue
                    if len(threads) >= SEARCH_THREADS:
                        truncated = True
                        break
                    # Gmail's to: and subject: match words, not the whole address or subject (to:ann@ also finds
                    # jo.ann@), so every hit is checked as the sweep checks mail.
                    _discard(conn)
                    if not _outreach_target(labeller, str(message.get("id") or ""), own_addresses, own_subjects):
                        continue
                    threads.append(thread_id)
                token = str(body.get("nextPageToken") or "")
                if truncated or not token:
                    break
            truncated = truncated or bool(token)
            if truncated:
                detail["search_truncated"] = True
                LOGGER.warning("The Sent search for company %s was cut short at %d threads over %d pages", target_id, len(threads), SEARCH_PAGES)
        with conn:
            for thread_id in threads:
                _add_thread(conn, user_id, thread_id, target_id, "search", now)
            conn.execute(
                "INSERT INTO outreach_label_searches(user_id, target_id, searched_at, found, query) VALUES(?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id, target_id) DO UPDATE SET searched_at=excluded.searched_at, found=excluded.found, query=excluded.query",
                (user_id, target_id, now.isoformat(timespec="microseconds"), len(threads), query),
            )
        counts["searched"] += 1
        counts["found"] += len(threads)


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
    detail: dict[str, Any], fresh: bool = False, counts: dict[str, int] | None = None, marks: _Marks | None = None,
) -> None:
    """Label what joined an outreach thread since the last pass, and the sent mail that is outreach and not yet a thread.

    Reads Gmail's list of messages from a little before where the last sweep
    stopped and labels the threads in it that the app has labelled (the app's
    own thank-you, the student's answers). It then lists the Sent mail of the
    same span and labels the thread of any message that went to an address of an
    outreach company or carries one of its subjects. The first pass under a name
    only records where to start (before it labels anything), since the threads it
    labelled were labelled whole. Mail that already has the label is left out of
    the listings, so a pass cut short by its thread budget carries on where it
    stopped. A listing too long for one pass moves the start on; for the labelled
    threads' listing that asks for every one of them to be read again over the
    next passes, and for the Sent listing it asks for every company to be
    searched again, so what it did not list is not missed.
    """
    if fresh:
        return
    counts = counts if counts is not None else {"sent_labelled": 0, "gone": 0, "failed": 0}
    wanted = set(_labelled_threads(conn, user_id, name))
    addresses, subjects = _index((marks or _Marks(conn, user_id)).get(_own_addresses(labeller.account)))
    if not wanted and not addresses and not subjects:
        return
    start = int(now.timestamp())
    kept = _kept(conn, user_id, name)
    if kept is None:  # a setting that cannot be read: start over from here
        _keep_watermark(conn, user_id, name, start, now)
        return
    listing = "complete"
    if wanted:
        listing = _sweep_labelled(conn, labeller, user_id, name, kept, wanted, now, done, detail)
        if listing == "stopped":
            return
    if addresses or subjects:
        sent = _sweep_sent(conn, labeller, user_id, name, kept, wanted, now, done, detail, counts, addresses, subjects)
        if sent == "stopped":
            return
        if sent == "truncated":
            with conn:
                conn.execute("DELETE FROM outreach_label_searches WHERE user_id=?", (user_id,))
            detail["sent_sweep_truncated"] = True
    if listing == "truncated":
        detail["sweep_truncated"] = True
        _keep_watermark(conn, user_id, name, start, now, recheck="")
    else:
        _keep_watermark(conn, user_id, name, start, now)


def _sweep_labelled(
    conn: sqlite3.Connection, labeller: _Labeller, user_id: str, name: str, kept: dict[str, Any], wanted: set[str],
    now: datetime, done: dict[str, str], detail: dict[str, Any],
) -> str:
    """The labelled threads' new messages. "stopped" (the thread budget ran out; the start stays), "truncated" or "complete"."""
    if "recheck" in kept:
        cursor = str(kept["recheck"] or "")
        for thread_id in sorted(wanted):
            if thread_id <= cursor:
                continue
            if thread_id not in done:
                if labeller.threads >= PER_PASS:
                    detail["more"] = True
                    _keep_watermark(conn, user_id, name, int(kept["after"]), now, recheck=cursor)
                    return "stopped"
                _discard(conn)
                done[thread_id] = labeller.thread(thread_id)[0]
            cursor = thread_id
        _keep_watermark(conn, user_id, name, int(kept["after"]), now)
    _discard(conn)
    token = ""
    # A delivery notice is never labelled, so left in the listing it would be read again every pass and could hold the sweep back.
    query = (
        f"after:{int(kept['after']) - SWEEP_OVERLAP_SECONDS} -in:chats -in:drafts "
        f"-from:({' OR '.join(sorted(_DAEMONS))}) -label:{search_form(name)}"
    )
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
                return "stopped"  # the rest waits; the watermark stays where it was
            done[thread_id] = labeller.thread(thread_id)[0]
        token = str(body.get("nextPageToken") or "")
        if not token:
            return "complete"
    return "truncated"


def _sweep_sent(
    conn: sqlite3.Connection, labeller: _Labeller, user_id: str, name: str, kept: dict[str, Any], wanted: set[str],
    now: datetime, done: dict[str, str], detail: dict[str, Any], counts: dict[str, int],
    addresses: dict[str, str], subjects: dict[str, str],
) -> str:
    """Sent mail since the last pass that is outreach: labelled by thread. "stopped", "truncated" or "complete"."""
    known = wanted | {str(item[0]) for item in conn.execute(
        "SELECT thread_id FROM outreach_label_threads WHERE user_id=?", (user_id,),
    ).fetchall()}
    _discard(conn)  # the SELECT opens a transaction on PostgreSQL, and the listing below is a network call
    seen: set[str] = set()
    stamp = now.isoformat(timespec="seconds")
    token = ""
    query = f"in:sent after:{int(kept['after']) - SWEEP_OVERLAP_SECONDS} -label:{search_form(name)}"
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
            gmail_id = str(message.get("id") or "") if isinstance(message, dict) else ""
            thread_id = str(message.get("threadId") or "") if isinstance(message, dict) else ""
            if not gmail_id or not thread_id or gmail_id in seen or thread_id in known or thread_id in done:
                continue
            seen.add(gmail_id)
            target_id = _outreach_target(labeller, gmail_id, addresses, subjects)
            if not target_id:
                continue
            with conn:
                _add_thread(conn, user_id, thread_id, target_id, "sweep", now)
            known.add(thread_id)
            if labeller.threads >= PER_PASS:
                detail["more"] = True
                return "stopped"  # noted above; the next pass labels it
            _discard(conn)
            done[thread_id] = labeller.thread(thread_id)[0]
            _settle_sent(conn, user_id, name, thread_id, done[thread_id], stamp, counts)
        token = str(body.get("nextPageToken") or "")
        if not token:
            return "complete"
    return "truncated"


def _outreach_target(labeller: _Labeller, gmail_id: str, addresses: dict[str, str], subjects: dict[str, str]) -> str:
    """The company a sent message is outreach to, going by who it went to and its subject; '' when it is not outreach."""
    response = labeller.gmail.request("GET", f"/messages/{quote(gmail_id, safe='')}", params={
        "format": "metadata", "metadataHeaders": ["To", "Cc", "Bcc", "Subject"],
    })
    if response.status_code == 404:
        return ""
    labeller._refuse(response)
    if response.status_code != 200:
        raise _Stop("unreachable")
    headers = {
        str(item.get("name", "")).casefold(): str(item.get("value", ""))
        for item in (response.json().get("payload") or {}).get("headers") or [] if isinstance(item, dict)
    }
    for address in _addresses(headers.get("to"), headers.get("cc"), headers.get("bcc")):
        if address in addresses:
            return addresses[address]
    return subjects.get(_subject_key(headers.get("subject")), "")


def _keep_watermark(
    conn: sqlite3.Connection, user_id: str, name: str, after: int, now: datetime, recheck: str | None = None,
) -> None:
    """Store where the sweep starts; ``recheck`` (a thread id, '' for the first) is how far through the labelled threads it has re-read."""
    value: dict[str, Any] = {"label": name, "after": after}
    if recheck is not None:
        value["recheck"] = recheck
    with conn:
        automation._put_setting(conn, user_id, SWEEP_SETTING, json.dumps(value), now.isoformat(timespec="microseconds"))
