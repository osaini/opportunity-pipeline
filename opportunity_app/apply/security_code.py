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
- A code is handed out once per claim. A second request, even after a restart, is a fallback.
- Handing the code out is not typing it. The claim records "handed" and says so to nobody; only the child's
  acknowledgement ({"op": "security_code_typed"}, see below) records "typed", counts as typed in the statistics and
  posts the notice. A repeat request for a code that was handed out and never acknowledged falls back to D10 A with a
  reason that tells the student to type it, because the app cannot know what became of it.
- A silent Gmail is not "no email": when the last look could not read the mailbox, the window's end says the app could
  not reach Gmail, not that no security code email arrived.
- The code is never logged, stored, or put in a notice, a progress line, an event, a run or a claim: only the pipe reply
  carries it, and ``CodeAnswer.__repr__`` leaves it out. What is recorded is that a prompt happened, which way it went,
  and when (``detail.security_code_reader`` on the claim; ``detail.security_code`` stays the boolean 6.14 settles with,
  and watch.ats_statistics counts either).

The pipe interface (the browser driver is another milestone's; this is the contract it calls):

  child  -> parent  {"op": "security_code", "token": "<claim token>"}   when the security-code field first appears
  parent -> child   {"op": "security_code", "status": "waiting" | "found" | "fallback", "reason": "<key>", "code": "..."}
  child  -> parent  {"op": "security_code_typed", "token": "<claim token>"}   after it typed the code into the fields

``code`` is present only when the status is "found". The child repeats the request every POLL_EVERY while the status
is "waiting", waits at most REPLY_TIMEOUT_S for each reply (no reply counts as waiting), and keeps heartbeating the
claim. On "found" it types the code once into the same window's empty fields, sends the acknowledgement (the parent's
``confirm_typed``) and presses Greenhouse's second Submit; on "fallback" it brings the window to the front and waits for
the student (D10 A). The runner must not log, persist or forward the reply of this op. Its waits add CODE_WINDOW to the
student's own wait.
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
    """The one reader of a server process: it keeps, per claim token, when it last looked and which codes were handed out."""

    def __init__(self, client_factory: ClientFactory = _reader_client, *, poll_every: timedelta = POLL_EVERY, window: timedelta = CODE_WINDOW):
        self.client_factory = client_factory
        self.poll_every = poll_every
        self.window = window
        self._looked: dict[str, datetime] = {}
        self._handed: set[str] = set()
        self._lock = threading.Lock()

    def forget(self, token: str) -> None:
        """The claim is settled: its memory can go."""
        with self._lock:
            self._looked.pop(token, None)
            self._handed.discard(token)

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
        if record.get("reader") == "handed" or token in self._handed:
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
            with self._lock:
                self._handed.add(token)
            _record(conn, user_id, token, {"reader": "handed", "handed_at": apply_watch.iso_utc(moment)}, now=now)
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

    def _give_up(self, conn: sqlite3.Connection, user_id: str, claim: Any, reason: str, moment: datetime, now: datetime | None) -> CodeAnswer:
        """D10 A: record the fallback once and tell the student, with the reason in words and never a value."""
        token = claim["token"]
        _record(conn, user_id, token, {"reader": "fallback", "reason": reason, "fell_back_at": apply_watch.iso_utc(moment)}, now=now)
        sentence = FALLBACK_REASONS.get(reason, "")
        if sentence:
            automation.notice(
                conn, user_id, event_key=f"apply-security-code:{token}", level="warning",
                title=f"{_company(conn, claim['opportunity_id'])}: Greenhouse emailed you a security code. Type it into the Chromium window.",
                body=f"The app couldn't read it from your email: {sentence}.",
            )
        return _fallback(reason)


def _company(conn: sqlite3.Connection, opportunity_id: str) -> str:
    row = conn.execute("SELECT company FROM opportunities WHERE id=?", (opportunity_id,)).fetchone()
    return (row["company"] if row else "") or "the company"


def _record(
    conn: sqlite3.Connection, user_id: str, token: str, changes: dict[str, Any], *, waiting: bool = False, now: datetime | None = None,
    only_if_reader: str = "",
) -> bool:
    """Merge into the claim's detail.security_code_reader, in one transaction, while the claim is still 'clicking'. Never a code.

    ``only_if_reader`` makes it conditional on the record being in that reader state (the acknowledgement's rule).
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
        detail[RECORD_KEY] = {**current, **changes}
        if waiting:
            detail["waiting"] = "security_code"
        return bool(conn.execute(
            "UPDATE application_submit_claims SET detail_json=?, updated_at=? WHERE token=? AND user_id=? AND state='clicking'",
            (json.dumps(detail, sort_keys=True), apply_watch.stamp_now(now), token, user_id),
        ).rowcount)


READER = SecurityCodeReader()
