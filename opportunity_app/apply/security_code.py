"""Apply for me, Greenhouse's emailed security code (D10 B), the parent side.

docs/phase5-apply-agent-spec.md, section 3 (D10 B and "What the three departures change") and 6.14. After the first
Submit, Greenhouse may show a security-code field and email an eight character code to the address on the application.
The browser agent cannot read the student's mail and must never be given a way to. This module lives in the server
process: the agent asks it, over the runner's pipe, whether the code has arrived, and types what it is told.

The reader rules, as built (each is a test in tests/test_apply_security_code.py):

- It reads only through the Phase 1 Gmail connection (GmailClient), with GET requests only: messages.list and
  messages.get in the raw format. Never a harness mailbox, never a second login. It needs the read permission.
- Only when the Gmail account the app reads is the address on the application (watch.mailbox_reason is empty). With any
  other address the code goes to a mailbox the app cannot see, so it falls back to D10 A (the window comes to the front
  for the student, who types it).
- A message counts only when Gmail vouched for its sender (mail_trust.authenticate, the ``sender_verified`` rule), the
  authenticated domain is Greenhouse's (greenhouse.io or greenhouse-mail.io), it was received after the hand-over the
  request belongs to, its subject is the security-code subject and names the same company (every company_key word).
- The code is the one eight character token after the word "code" that has a digit or stands alone on its line. Two
  candidate messages, or a message with no single such token, mean no code: the reader never guesses. Nothing within
  ten minutes of the prompt means no code either. In each of those cases the answer is a fallback to D10 A.
- A code is handed out once per claim, and handing it out is not typing it. Finding the code records only
  ``reader: handed`` (the code itself is never kept, not even in memory). Only the child's acknowledgement that it typed
  the code (``confirm_typed``, or ``confirm`` with ``typed=True``) records "typed", counts as typed in the statistics and
  posts the notice; ``confirm`` with ``typed=False`` records the fallback and its reason (the notice says the app read the
  code and could not type it). A repeat request for a code that was handed out and never acknowledged falls back to D10 A
  with a reason that tells the student to type it (``not_confirmed``), because the app cannot know what became of it. A
  request after "typed" is a fallback (``already_used``). An ask the agent stopped waiting for (``abandoned``) drops
  whatever a look still running finds.
- A silent Gmail is not "no email": when the last look could not read the mailbox, the window's end says the app could
  not reach Gmail, not that no security code email arrived.
- The code is never logged, stored, or put in a notice, a progress line, an event, a run or a claim: only the pipe reply
  carries it, and ``CodeAnswer.__repr__`` leaves it out. What is recorded is that a prompt happened, which way it went,
  and when (``detail.security_code_reader`` on the claim; ``detail.security_code`` stays the boolean 6.14 settles with,
  and watch.ats_statistics counts either).

The pipe interface (the runner carries it; apply/agent_types.py names the ops):

  child  -> parent  {"op": "security_code", "id": n}      when the security-code field appears (no token: the parent
                                                          answers for its own run's claim, never one the child names)
  parent -> child   {"op": "security_code_reply", "id": n, "status": "waiting" | "found" | "fallback", "reason": "<key>",
                     "code": "..."}
  child  -> parent  {"op": "security_code_result", "id": n, "typed": bool, "reason": "<why not>"}   once per "found"

``code`` is present only when the status is "found". The child asks without blocking, never asks again while a reply is
outstanding, and asks every POLL_EVERY while the status is "waiting" (a reply later than REPLY_TIMEOUT_S is shown as
waiting; the same id stays pending). On "found" it types the code once into the same window's empty boxes and **never
presses Submit**: the student presses it (D1 B, "Never auto-apply"). On "fallback" it brings the window to the front and
waits for the student (D10 A). The runner must not log, persist or forward the reply of this op, and answers on a thread
of its own so a slow Gmail never holds the pipe or the heartbeats. Its waits add CODE_WINDOW to the student's own wait.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

import httpx
from pipeline_core.identity import normalized

from . import runs as apply_runs, watch as apply_watch
from .. import SERVER_INSTANCE
from ..applications import mail_rules
from ..automation import ledger as automation
from ..core.json_values import json_as
from ..core.timestamps import parse_app_instant
from ..integrations.gmail_client import ClientFactory, GmailAuthError, GmailThrottled, can_read_mail, connection_state, granted_scopes
from ..mail import trust as mail_trust
from ..mail.gmail_connection import GmailClient, connector_row
from .greenhouse import GREENHOUSE_SENDER_DOMAINS, is_greenhouse_sender

LOGGER = logging.getLogger(__name__)

# Where the reader's record lives in the claim's detail. Not "security_code": 6.14 settles a prompted claim with
# detail.security_code=true, and a shallow merge of that would erase a dict stored there.
RECORD_KEY = "security_code_reader"

# How long after the prompt the app keeps looking for the email before it asks the student to type the code.
CODE_WINDOW = timedelta(minutes=10)
# The least time between two looks in Gmail for one claim (the child asks more often than this).
POLL_EVERY = timedelta(seconds=15)
# How long the child waits for one reply before asking again.
REPLY_TIMEOUT_S = 45
SEARCH_LIMIT = 5

# Why the app could not read the code, as the notice says it; "" means a fallback that needs no notice.
FALLBACK_REASONS = {
    "not_current": "",
    "already_used": "",
    "not_confirmed": "the app found the code but could not tell that it was entered",
    "gmail_unreachable": "the app couldn't reach Gmail in time, so it doesn't know whether the email arrived",
    "no_gmail": "Gmail isn't connected",
    "needs_reconnect": "Gmail needs reconnecting",
    "no_read_permission": "Gmail was connected without permission to read mail",
    "other_address": "the email on your application isn't the Gmail account the app reads",
    "unknown_address": "Gmail needs reconnecting once so the app knows which address it reads",
    "two_candidates": "more than one security code email arrived",
    "unclear_code": "the code in the email couldn't be read with certainty",
    "timed_out": "no security code email arrived within 10 minutes",
    "abandoned": "",       # the agent stopped waiting for its ask: not a reason to tell the student, and not "no email arrived"
}
# The child read the code and could not put it in (confirm with typed=False): said as "couldn't type it", not "couldn't read it".
TYPING_REASONS = {
    "inputs_not_empty": "the code boxes in the window already had something in them",
    "inputs_missing": "the window no longer showed the eight code boxes",
    "bad_code": "the code in the email wasn't eight letters and digits",
    "page_closed": "the window had left the application form",
    "already_typed": "the app had already typed a code into this window",
    "auto_submit_blocked": "the form tried to send the code by itself, which the app blocks",
    "typing_failed": "it could not be typed into the window",
    "press_unseen": "the app could not watch for your press of Submit in the window",
}

_CODE_WORD = re.compile(r"code", re.IGNORECASE)
_TOKEN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{8}(?![A-Za-z0-9])")


@dataclass(frozen=True)
class CodeAnswer:
    """What the reader says to one request: wait, here is the code, or give up (``reason`` is a FALLBACK_REASONS key)."""

    status: str
    code: str = field(default="", repr=False)
    reason: str = ""
    # For "waiting": whether the look read the mailbox to its end (an empty answer) or could not.
    reached: bool = field(default=False, repr=False)

    def message(self) -> dict[str, Any]:
        """The pipe reply. It carries the code only when it was found."""
        reply: dict[str, Any] = {"op": "security_code", "status": self.status, "reason": self.reason}
        if self.status == "found":
            reply["code"] = self.code
        return reply


def _waiting(reached: bool = False) -> CodeAnswer:
    return CodeAnswer("waiting", reached=reached)


def _fallback(reason: str) -> CodeAnswer:
    return CodeAnswer("fallback", reason=reason)


def extract_code(text: str) -> str | None:
    """The one eight character code after the first "code" in an email's text, or None when there is not exactly one.

    A candidate has a digit, or stands alone on its line, so a word of eight letters in a sentence is not one.
    """
    found = _CODE_WORD.search(text or "")
    if found is None:
        return None
    rest = text[found.end():]
    alone = {line.strip() for line in rest.splitlines()}
    codes = {token for token in _TOKEN.findall(rest) if any(char.isdigit() for char in token) or token in alone}
    return codes.pop() if len(codes) == 1 else None


def find_code(
    conn: sqlite3.Connection, user_id: str, *, handed_over_at: datetime, company_key: str, client_factory: ClientFactory,
) -> CodeAnswer:
    """One look in Gmail for the code that belongs to this hand-over. Writes nothing here; "waiting" when nothing qualifies yet."""
    tokens = set(str(company_key or "").split())
    if not tokens:
        return _fallback("unclear_code")
    query = f'from:({" OR ".join(GREENHOUSE_SENDER_DOMAINS)}) subject:"security code" after:{int(handed_over_at.timestamp()) - 60}'
    candidates: list[str | None] = []
    try:
        with client_factory() as client:
            gmail = GmailClient(conn, client, user_id)
            listed = gmail.request("GET", "/messages", params={"q": query, "maxResults": SEARCH_LIMIT})
            if listed.status_code != 200:
                return _waiting()
            ids = [str(item.get("id", "")) for item in (listed.json().get("messages") or []) if item.get("id")][:SEARCH_LIMIT]
            for gmail_id in ids:
                response = gmail.request("GET", f"/messages/{quote(gmail_id, safe='')}", params={"format": "raw"})
                if response.status_code == 404:
                    continue
                if response.status_code != 200:
                    return _waiting()
                mail = mail_rules.parse_message(response.json())
                if {"SENT", "DRAFT"} & set(mail.labels) or mail.received_at < handed_over_at:
                    continue
                if not mail_rules.SECURITY_CODE_SUBJECT.search(mail.subject) or not tokens <= set(normalized(mail.subject).split()):
                    continue
                auth = mail_trust.authenticate(mail.message)
                if not auth.ok or not is_greenhouse_sender(auth.from_domain):
                    continue
                candidates.append(extract_code(mail.text))
    except GmailAuthError:
        return _fallback("needs_reconnect")
    except (GmailThrottled, httpx.HTTPError, ValueError):
        # Gmail asked to wait or did not answer: the next look tries again, and the window still ends on time.
        return _waiting()
    if not candidates:
        return _waiting(reached=True)
    if len(candidates) > 1:
        return _fallback("two_candidates")
    return CodeAnswer("found", code=candidates[0]) if candidates[0] else _fallback("unclear_code")


def _reader_client() -> httpx.Client:
    return httpx.Client(timeout=15, follow_redirects=False)


class SecurityCodeReader:
    """The one reader of a server process: it keeps, per claim token, when it last looked and whether it handed a code out."""

    def __init__(self, client_factory: ClientFactory = _reader_client, *, poll_every: timedelta = POLL_EVERY, window: timedelta = CODE_WINDOW):
        self.client_factory = client_factory
        self.poll_every = poll_every
        self.window = window
        self._looked: dict[str, datetime] = {}
        self._handed: set[str] = set()
        # Tokens whose ask the agent stopped waiting for. A look in Gmail that was still running then finds a code nobody waits
        # for: it is dropped, never recorded as handed out (the marker goes with forget()).
        self._abandoned: set[str] = set()
        self._lock = threading.Lock()

    def forget(self, token: str) -> None:
        """The claim is settled: its memory can go."""
        with self._lock:
            self._looked.pop(token, None)
            self._handed.discard(token)
            self._abandoned.discard(token)

    def answer(self, conn: sqlite3.Connection, *, user_id: str, token: str, now: datetime | None = None) -> CodeAnswer:
        """One request from the child for the claim ``token``. Never raises for what Gmail or an email contains."""
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        claim = conn.execute("SELECT * FROM application_submit_claims WHERE token=? AND user_id=?", (token, user_id)).fetchone()
        handed = parse_app_instant(claim["handed_over_at"]) if claim is not None else None
        if claim is None or claim["state"] != "clicking" or handed is None or claim["instance"] != SERVER_INSTANCE:
            return _fallback("not_current")
        detail = json_as(claim["detail_json"], {})
        record = detail.get(RECORD_KEY) if isinstance(detail.get(RECORD_KEY), dict) else {}
        if not record.get("prompted_at"):
            # The first request is the prompt: counted for 8.8 (R1) whatever becomes of it.
            _record(conn, user_id, token, {"prompted_at": apply_watch.iso_utc(moment), "reader": "waiting"}, waiting=True, now=now)
            record = {"prompted_at": apply_watch.iso_utc(moment), "reader": "waiting"}
        if record.get("reader") == "typed":
            return _fallback("already_used")
        with self._lock:
            abandoned, handed_here = token in self._abandoned, token in self._handed
        if abandoned:
            return _fallback("abandoned")
        if record.get("reader") == "handed" or handed_here:
            # Handed out and never acknowledged: it may not have been typed (a reply lost, a window gone). The student is told.
            return self._give_up(conn, user_id, claim, "not_confirmed", moment, now)
        if record.get("reader") == "fallback":
            return _fallback(str(record.get("reason") or "timed_out"))
        reason = self._unable(conn, user_id)
        if not reason:
            prompted = parse_app_instant(record.get("prompted_at")) or moment
            if moment - prompted >= self.window:
                reason = "timed_out" if record.get("last_look_ok") else "gmail_unreachable"
        if reason:
            return self._give_up(conn, user_id, claim, reason, moment, now)
        with self._lock:
            last = self._looked.get(token)
            if last is not None and moment - last < self.poll_every:
                return _waiting()
            self._looked[token] = moment
        found = find_code(conn, user_id, handed_over_at=handed, company_key=claim["company_key"], client_factory=self.client_factory)
        if found.status == "fallback":
            return self._give_up(conn, user_id, claim, found.reason, moment, now)
        if found.status == "found":
            # Handed out is not typed: nothing is counted and no notice is written until the child says it typed it. The run may
            # have ended while Gmail was being read (forget() has run): then the claim is no longer 'clicking' and the child is
            # told nothing is current. The agent may have stopped waiting for this ask while Gmail was read (confirm(...,
            # abandoned) ran): nobody is waiting for the code, so it is not recorded as handed out over the fallback.
            with self._lock:
                if token in self._abandoned:
                    return _fallback("abandoned")
            if not _record(
                conn, user_id, token, {"reader": "handed", "handed_at": apply_watch.iso_utc(moment)}, now=now,
                not_reader=("fallback", "typed"),
            ):
                return _fallback("not_current")
            with self._lock:
                if token in self._abandoned:        # abandoned between the record and here: the code goes no further
                    return _fallback("abandoned")
                self._handed.add(token)
        elif bool(record.get("last_look_ok")) != found.reached:
            _record(conn, user_id, token, {"last_look_ok": found.reached}, now=now)
        return found

    def confirm_typed(self, conn: sqlite3.Connection, *, user_id: str, token: str, now: datetime | None = None) -> bool:
        """The child's acknowledgement that it typed the code it was handed: the only thing that records "typed".

        True when this call recorded it. A claim that is not current, was never handed a code, or already fell back to
        the student (D10 A) records nothing, so the statistics never count a typing nobody saw.
        """
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        claim = conn.execute("SELECT * FROM application_submit_claims WHERE token=? AND user_id=?", (token, user_id)).fetchone()
        if claim is None or claim["state"] != "clicking" or claim["instance"] != SERVER_INSTANCE:
            return False
        if not _record(conn, user_id, token, {"reader": "typed", "typed_at": apply_watch.iso_utc(moment)}, now=now, only_if_reader="handed"):
            return False
        with self._lock:
            self._handed.discard(token)
        automation.notice(
            conn, user_id, event_key=f"apply-security-code-typed:{token}", level="info",
            title=f"Apply for me entered the security code Greenhouse emailed you for {_company(conn, claim['opportunity_id'])}",
        )
        return True

    def confirm(
        self, conn: sqlite3.Connection, *, user_id: str, token: str, typed: bool, reason: str = "", now: datetime | None = None,
    ) -> None:
        """The child's word on the code it was handed (the pipe's security_code_result): typed, or not and why.

        Typed is ``confirm_typed``. Not typed records the fallback with its reason and tells the student to type the code
        themselves; ``abandoned`` (the agent stopped waiting for its ask) records only that. Nothing here ever holds the code,
        and only a claim that is still 'clicking' is touched.
        """
        if typed:
            self.confirm_typed(conn, user_id=user_id, token=token, now=now)
            return
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        with self._lock:
            self._handed.discard(token)
            if reason == "abandoned":
                self._abandoned.add(token)
        claim = conn.execute("SELECT * FROM application_submit_claims WHERE token=? AND user_id=?", (token, user_id)).fetchone()
        if claim is None or claim["state"] != "clicking":
            return
        if reason == "abandoned":
            # The agent stopped waiting for its ask (the student took the code over, the read window ended, the page moved on): whatever
            # the reader found is dropped, and the claim says the agent stopped waiting. It says "abandoned" and nothing more:
            # not "no email arrived", which may not be what happened. No notice: the window was already brought forward for them.
            _record(
                conn, user_id, token, {"reader": "fallback", "reason": "abandoned", "fell_back_at": apply_watch.iso_utc(moment)}, now=now,
                not_reader=("fallback", "typed"),
            )
            return
        key = reason if reason in TYPING_REASONS else "typing_failed"
        self._give_up(conn, user_id, claim, key, moment, now, typing=True)

    def _unable(self, conn: sqlite3.Connection, user_id: str) -> str:
        """The fallback reason when the app cannot read this student's mail at all, else ''. Database reads only."""
        row = connector_row(conn, user_id)
        state = connection_state(row)
        if state == "not_connected":
            return "no_gmail"
        if state == "needs_reconnect":
            return "needs_reconnect"
        if not can_read_mail(granted_scopes(row["scopes_json"])):
            return "no_read_permission"
        problem = apply_watch.mailbox_reason(conn, user_id)
        if not problem:
            return ""
        return "unknown_address" if problem == apply_watch.WATCH_NEEDS_ADDRESS else "other_address"

    def _give_up(
        self, conn: sqlite3.Connection, user_id: str, claim: Any, reason: str, moment: datetime, now: datetime | None,
        *, typing: bool = False,
    ) -> CodeAnswer:
        """D10 A: record the fallback once and tell the student, with the reason in words and never a value.

        ``typing`` is a fallback after the code was read but could not be put in (the notice says so, not "couldn't read it").
        """
        token = claim["token"]
        _record(conn, user_id, token, {"reader": "fallback", "reason": reason, "fell_back_at": apply_watch.iso_utc(moment)}, now=now)
        if typing:
            sentence = TYPING_REASONS.get(reason, TYPING_REASONS["typing_failed"])
            lead = "The app read the code but couldn't type it"
        else:
            sentence = FALLBACK_REASONS.get(reason, "")
            lead = "The app couldn't read it from your email"
        if sentence:
            automation.notice(
                conn, user_id, event_key=f"apply-security-code:{token}", level="warning",
                title=f"{_company(conn, claim['opportunity_id'])}: Greenhouse emailed you a security code. Type it into the Chromium window.",
                body=f"{lead}: {sentence}.",
            )
        return _fallback(reason)


def _company(conn: sqlite3.Connection, opportunity_id: str) -> str:
    row = conn.execute("SELECT company FROM opportunities WHERE id=?", (opportunity_id,)).fetchone()
    return (row["company"] if row else "") or "the company"


def _record(
    conn: sqlite3.Connection, user_id: str, token: str, changes: dict[str, Any], *, waiting: bool = False, now: datetime | None = None,
    only_if_reader: str = "",
    not_reader: tuple[str, ...] = (),
) -> bool:
    """Merge into the claim's detail.security_code_reader, in one transaction, while the claim is still 'clicking'. Never a code.

    ``only_if_reader`` makes it conditional on the record being in that reader state (the acknowledgement's rule);
    ``not_reader`` names readers the record must not overwrite (a late "handed" never replaces a fallback or a typed code).
    """
    with conn:
        apply_runs.lock_user(conn, user_id)
        row = conn.execute(
            "SELECT detail_json FROM application_submit_claims WHERE token=? AND user_id=? AND state='clicking'", (token, user_id),
        ).fetchone()
        if row is None:
            return False
        detail = json_as(row["detail_json"], {})
        current = detail.get(RECORD_KEY)
        current = current if isinstance(current, dict) else {}
        if only_if_reader and current.get("reader") != only_if_reader:
            return False
        if current.get("reader") in not_reader:
            return False
        detail[RECORD_KEY] = {**current, **changes}
        if waiting:
            detail["waiting"] = "security_code"
        return bool(conn.execute(
            "UPDATE application_submit_claims SET detail_json=?, updated_at=? WHERE token=? AND user_id=? AND state='clicking'",
            (json.dumps(detail, sort_keys=True), apply_watch.stamp_now(now), token, user_id),
        ).rowcount)


READER = SecurityCodeReader()
