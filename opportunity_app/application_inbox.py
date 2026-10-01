"""Job-system and assessment emails read from Gmail, matched to applications, then acted on or proposed.

"Update applications from job emails" (the ``application_mail`` switch) is
off for every student until they turn it on, and runs in shadow first. It is
the fifth step of the inbox watcher (inbox_watcher.InboxWatcher), with the
same Gmail connection and transport, at most one pass every ten minutes.

Reading. Live mail comes from users.history.list (2 quota units a call,
against 5 for a search). The cursor (application_mail_sync.history_id) only
moves in the transaction that stores the ids it covers in pending_ids_json,
and an id leaves that list only in the transaction that records its
application_mail_messages row, so a pass cut short (a crash, Gmail asking to
slow down) loses nothing. When Gmail has forgotten the cursor (HTTP 404), a
paginated messages.list since the last good pass (plus a day, never before
enabled_at) refills the list, resuming across passes, and live reading goes on
from a cursor taken before it began. Separately, the first time the switch is
on, a paginated backfill looks at the 60 days before: its own query, page
token, and queue, sharing the 50-message budget per pass after live mail. A
message received before enabled_at is only ever proposed.

What is read. A message is read in full when its sender's domain, or the host
of any link in it, is on the shipped list (data/application_senders.json), or
its sender is at a domain suggested or trusted for a company the student has
applied to. Anything else is recorded as skipped, with nothing about it kept.
A message outreach holds (outreach_inbox.owned_sql: a company's reply or
automatic reply matched by its thread or an address the student wrote to, or a
possible reply waiting for the student) is left to outreach. One outreach only
set aside, matched to a company by its domain alone, or the student dismissed
is read here too; _reclaim takes back any such message this reader had once
left to outreach.

What it means. The keyword rules (connections.classify_monitored_message) plus
what the sender says: mail from an assessment platform is an assessment unless
it is an offer, a rejection, or thanks for a finished test; a scheduling-tool
link or sender is scheduling unless the text says more. Jev answers when the
student turned Jev inbox suggestions on and has not paused automation. Jev
cannot be measured here (no key, and .env is never read), so a Jev answer can
make an automatic change only when it agrees with the rules; otherwise it
proposes, and when only one of the two finds a change in the email (Jev says
"unknown", the rules say "rejected"), that change is proposed, never dropped.
The rules' confidence must reach AUTO_ACT_MIN_CONFIDENCE, which is the lowest
value that makes no false automatic rejection or interview on the 60
synthetic emails in tests/fixtures/application_mail_eval.json (labelled
before the rules ran on them; tests/test_application_mail_eval.py pins the
per-label precision), nor on the confirmations review later found the rules
misreading (kept apart in the same file). The rules were written alongside that set, so its
numbers are an optimistic description of it, not a promise about real mail.

Which application. match_application tries, strongest first: an ATS job id or
job URL in a link that equals an application's source (job_id); the company
named in the sender, subject or opening of the email, equal by
identity.identity_tokens, plus at least 0.8 of the role's words (company_title);
the company alone, when exactly one open application has it (company_single;
one the app archived after no reply counts as open, counts_as_open).
Otherwise ambiguous (the candidates are ranked for the student's picker) or
none.

What may change on its own. Every one of these, or the change is proposed
with the missing ones named:
- Gmail vouches for the sender (mail_trust.authenticate: the topmost
  Authentication-Results is mx.google.com's and shows dmarc=pass or aligned
  dkim=pass), and the sender is a job system or assessment platform on the
  list, or at a domain the student trusted for that company. A link alone,
  a forward, or a newsletter never authorizes anything;
- the match is strong enough: job_id or company_title for a rejection or an
  interview; company_single too for a confirmation, a task or a deadline;
- the classification is sure enough, as above;
- the student did not change the stage after the email arrived (manual wins);
- it arrived after enabled_at, and its Date line is within 48 hours of when
  Gmail received it (internalDate, which orders everything);
- it is not an offer: an offer is always proposed, with a notice (whatever
  the application's stage, and when no application matched).
Stages only move forward (applying, applied, interview, offer; a rejection
closes anything before an offer), and the check is made inside the change's
own transaction. The same rules hold when the student approves a proposal for
another application than the one proposed (_correct).

Reopening. Nothing leaves a closed stage, with one exception: an application
the app archived itself after no reply (archive_silent_applications;
internal_automation.automatic_archive) counts as still at the stage it was
archived from, Applied, both here and when matching (counts_as_open), so an
email naming only the company is as unclear about which application it
means as it was before the archive. An interview, a rejection or an offer
(proposed, as always) moves it out of Archived whenever the email came; a
confirmation, an assessment, a scheduling link or a deadline reopens it to
Applied only when the archive did not know of the email: received on a
later day than the one it counted the silence from, even if it is read only
after the archive (news_to_archive). A task or deadline is never added to an
application still archived, on its own or by a correction. Each move out of
Archived checks again, inside its own transaction, that the archive is still
the app's (_reopen_guard): an application the student archived is never
reopened. A move out of it puts back the follow-up reminder the archive
cancelled (automation.ApplicationStage).

A stated date counts as a deadline only right after its cue ("by October 3",
"due on October 8", "before it expires on October 5"); "sent by the team on
September 28" is not one. A date written with numbers only, whose day and
month could be swapped (10/11/2026), or an email stating more than one date,
only ever proposes.

Sender wording. An email Gmail did not vouch for is described as "claiming to
be from" its domain, in the proposal, the timeline, the email card, and
Urgent, since anyone can write any From line.

Privacy. Evidence keeps the sender, subject, Date line, Gmail ids, a hash of
the text, and at most 500 characters of it with every link cut to its host.
The assessment or scheduling link itself is stored only on the task, shown in
the app. Notices carry no email text or links. run_retention drops the
excerpts after PIPELINE_MAIL_EVIDENCE_DAYS (180 by default).

Pause. A pass still reads and records mail while automation is paused, since
reading changes nothing; a message it would have acted on is recorded as
awaiting_resume, and decided again (with the same idempotency keys, so never
twice) once the student resumes.

Switching off. Every write a pass makes to the cursor, its queues, or a
message's record checks, in its own transaction, that the switch is still on
and was not reset since the pass began (note_off); a pass that finds it was
stops there, so it never writes back a cursor that turning the switch on again
would take for its own. A database that is busy (locked, a deadlock) stops the
pass with the message still queued, to be decided on the next one; only a
message that cannot be read or decided is set aside.

The breaker. One email can make a stage change, a task and a deadline; taking
all of them back counts once (automation.register_breaker_group), and turning
down a role that is not tracked never counts.
"""

from __future__ import annotations

import email
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlsplit, urlunsplit
from uuid import uuid4

import httpx

from pipeline_core.identity import identity_tokens, normalized

from . import automation, internal_automation, mail_trust
from .actions import log_application_event
from .connections import classify_monitored_message
from .database import is_transient_error
from .extension_apply import split_canonical_url
from .inbox_classifiers import classify_email
from .gmail_client import ClientFactory, GmailAuthError, GmailThrottled, connection_state
from .mail_message import (
    URL,
    clean_url,
    decode_base64url,
    has_list_headers,
    host_of,
    html_text_spaced,
    received_or_epoch,
    strip_queries,
)
from .outreach_config import sender_account
from .gmail_connection import connector_row, GmailClient
from .outreach_inbox import RULES, owned_sql
from .settings_store import setting_updated_at
from .timestamps import parse_app_instant, utc_now
from .typesafe_decisions import DecisionClient
from .user_time import user_timezone

LOGGER = logging.getLogger(__name__)

FEATURE = "application_mail"
# The origin of a message outreach held and then let go (_reclaim): only ever proposed.
RECLAIMED = "reclaimed"
HEALTH_COMPONENT = "inbox.applications"
POLICY_VERSION = "application-mail-v1"
# The lowest rules confidence that made no false automatic rejection or
# interview on tests/fixtures/application_mail_eval.json, and never below the
# 0.85 the plan set as the floor. On 2026-09-27 no confidence made a false one
# (the two wrong rejected/interview answers were a newsletter and a forward,
# which never act), so the floor stands. tests/test_application_mail_eval.py
# recomputes it and pins the per-label precision.
AUTO_ACT_MIN_CONFIDENCE = 0.85
AUTO_ACT_FLOOR = 0.85
MAX_GETS_PER_PASS = 50
MAX_LIST_PAGES = 10
PASS_EVERY = timedelta(minutes=10)
BACKFILL_DAYS = 60
RECOVERY_OVERLAP = timedelta(days=1)
DATE_GAP_LIMIT = timedelta(hours=48)
EXCERPT_LIMIT = 500
QUOTE_LIMIT = 160
SCHEDULE_TASK_DAYS = 2
DEFAULT_EVIDENCE_DAYS = 180
# How far ahead a date with no year may be read as this year's (or next year's) date.
YEARLESS_WINDOW_DAYS = 180
TEXT_LIMIT = 20_000

STAGE_RANK = {"applying": 0, "applied": 1, "interview": 2, "offer": 3}
CLOSED_STAGES = {"rejected", "withdrawn", "archived"}
# Which match tiers may act on their own, per kind of change (1.3a).
STRONG_TIERS = {"job_id", "company_title"}
ANY_TIER = {"job_id", "company_title", "company_single"}
# The kinds of email that lead to a change, and so to a monitored_events row.
ACTIONABLE = {"application_confirmation", "interview", "scheduling", "rejected", "offer", "assessment", "deadline"}
BACKFILL_BASIS = "window:before_enabled"
BEFORE_ENABLED = "it arrived before you turned this on"
# The backfill's search ends this long after the live cursor was taken, so nothing falls between the two.
BACKFILL_UNTIL_MARGIN = timedelta(minutes=1)
PLATFORM_NAMES = {
    "hackerrank.com": "HackerRank", "hackerrankforwork.com": "HackerRank", "codesignal.com": "CodeSignal",
    "codility.com": "Codility", "hirevue.com": "HireVue",
}

_PASS_LOCKS: dict[str, threading.Lock] = {}
_PASS_LOCKS_GUARD = threading.Lock()


# --- Reading one message ---------------------------------------------------------------

_FORWARD = re.compile(r"^\s*-{2,}\s*forwarded message\s*-{2,}\s*$|^\s*begin forwarded message:", re.IGNORECASE | re.MULTILINE)
_FORWARD_SUBJECT = re.compile(r"^\s*(fwd?|fw)\s*:", re.IGNORECASE)


@dataclass
class Mail:
    """One message as this module reads it. ``text`` is the plain text, cut to TEXT_LIMIT."""

    gmail_id: str
    thread_id: str
    labels: list[str]
    received_at: datetime
    date_header: str
    sent_at: datetime | None
    sender: str
    sender_name: str
    sender_domain: str
    subject: str
    text: str
    links: list[str]
    bulk: bool
    forwarded: bool
    message: EmailMessage = field(repr=False)

    @property
    def link_hosts(self) -> list[str]:
        hosts: list[str] = []
        for link in self.links:
            host = host_of(link)
            if host and host not in hosts:
                hosts.append(host)
        return hosts


def parse_message(data: dict[str, Any]) -> Mail:
    """A Gmail messages.get answer in the raw format, read."""
    message = email.message_from_bytes(decode_base64url(str(data.get("raw", ""))), policy=policy.default)
    received_at = received_or_epoch(data)
    name, address = parseaddr(str(message.get("From", "")))
    address = address.strip().lower()
    date_header = str(message.get("Date", "") or "")
    try:
        sent_at = parsedate_to_datetime(date_header) if date_header else None
        if sent_at is not None and sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        sent_at = None
    texts: list[str] = []
    links: list[str] = []
    plain = ""
    for part in message.walk():
        if part.is_multipart() or part.get_content_maintype() != "text":
            continue
        try:
            content = str(part.get_content())
        except (LookupError, ValueError):
            continue
        for url in URL.findall(content):
            cleaned = clean_url(url)
            if cleaned not in links:
                links.append(cleaned)
        if part.get_content_type() == "text/plain" and not plain:
            plain = content
        elif part.get_content_type() == "text/html":
            texts.append(html_text_spaced(content))
    text = plain or (texts[0] if texts else "")
    subject = " ".join(str(message.get("Subject", "") or "").split())
    return Mail(
        gmail_id=str(data.get("id", "")), thread_id=str(data.get("threadId", "")),
        labels=[str(label) for label in data.get("labelIds") or []], received_at=received_at,
        date_header=date_header[:200], sent_at=sent_at, sender=address, sender_name=" ".join(str(name).split()),
        sender_domain=address.rsplit("@", 1)[1] if "@" in address else "", subject=subject,
        text=text.replace("\r\n", "\n")[:TEXT_LIMIT], links=links[:200],
        bulk=has_list_headers(message),
        forwarded=bool(_FORWARD_SUBJECT.match(subject) or _FORWARD.search(text)),
        message=message,
    )


def redact(text: str) -> str:
    """Every link cut to its host, so no token or tracking id in a query string is ever kept."""
    return URL.sub(lambda match: host_of(clean_url(match.group(0))) or "[link]", str(text or ""))


def excerpt(text: str) -> str:
    return " ".join(redact(text).split())[:EXCERPT_LIMIT]


# --- What it means ---------------------------------------------------------------------

_COMPLETED = re.compile(
    r"\b(thank you for (completing|taking|submitting)|thanks for (completing|taking|submitting)|you (have )?completed"
    r"|(test|assessment|challenge) (was |has been )?(submitted|completed)|results have been shared)\b"
)


@dataclass(frozen=True)
class Classification:
    label: str
    confidence: float
    # What the rules said (with the sender's priors), which decides whether this may act on its own.
    rules_label: str
    rules_confidence: float
    prior: str
    classified_by: dict[str, Any]

    @property
    def agrees(self) -> bool:
        return self.label == self.rules_label


# A deadline the text states, when the rules found nothing more specific: never sure enough to act alone.
STATED_DATE_CONFIDENCE = 0.65


def classify_rules(
    subject: str, text: str, sender_domain: str, link_hosts: list[str], received: datetime | None = None,
) -> tuple[str, float, str]:
    """(label, confidence, prior): the keyword rules, then what the sender, the links, and a stated date say."""
    label, confidence = classify_monitored_message(subject, text)
    lowered = f"{subject}\n{text}".lower()
    sender_category = mail_trust.listed(sender_domain)
    link_categories = {mail_trust.listed(host) for host in link_hosts} - {None}
    completed = bool(_COMPLETED.search(lowered))
    if sender_category == "assessment":
        if completed:
            return "unknown", 0.3, "assessment_sender"
        if label not in ("offer", "rejected"):
            return "assessment", 0.9, "assessment_sender"
    if "assessment" in link_categories and label in ("unknown", "deadline", "recruiter_reply", "application_confirmation", "scheduling") and not completed:
        return "assessment", 0.88, "assessment_link"
    if (sender_category == "scheduling" or "scheduling" in link_categories) and label not in ("offer", "rejected", "interview", "assessment"):
        return "scheduling", 0.88, "scheduling_link"
    if label in ("unknown", "recruiter_reply") and stated_deadline(text, received or datetime.now(timezone.utc)):
        return "deadline", STATED_DATE_CONFIDENCE, "stated_date"
    return label, confidence, ""


def classify(mail: Mail, decisions: DecisionClient | None) -> Classification:
    rules_label, rules_confidence, prior = classify_rules(mail.subject, mail.text, mail.sender_domain, mail.link_hosts, mail.received_at)
    label, confidence, how = classify_email(mail.subject, mail.text, lambda _s, _b: (rules_label, rules_confidence), decisions)
    return Classification(
        label=label, confidence=float(confidence), rules_label=rules_label, rules_confidence=float(rules_confidence),
        prior=prior, classified_by={**how, "rules_label": rules_label, "rules_confidence": rules_confidence, "prior": prior},
    )


# --- Which application ------------------------------------------------------------------

# Words that sit next to a company's name without being part of it, so "Acme
# Hiring Team" names Acme but "Acme Robotics" does not.
_BESIDE_NAMES = {
    "your", "the", "a", "an", "at", "from", "with", "to", "for", "thank", "thanks", "you", "update", "re", "fwd", "fw",
    "interview", "interviews", "application", "applications", "invitation", "next", "steps", "step", "hi", "hello",
    "dear", "welcome", "congratulations", "regarding", "about", "our", "team", "hiring", "recruiting", "recruitment",
    "careers", "career", "talent", "acquisition", "people", "hr", "jobs", "job", "inc", "llc", "ltd", "corp", "co",
    "company", "group", "status", "confirmation", "received", "new", "message", "reminder", "important", "action",
    "required", "assessment", "invite", "offer", "and", "of", "in", "on", "is", "we", "i", "this", "that", "it", "via",
    "university", "campus", "program", "internship", "intern", "position", "role", "opportunity", "opportunities",
    "has", "have", "invited", "wants", "would", "like", "test", "coding", "challenge", "submitted", "applied", "notice",
    "s", "global", "usa", "us", "america", "north", "technologies",
}
_TITLE_NOISE = {
    "intern", "internship", "interns", "co", "op", "coop", "the", "a", "an", "and", "of", "for", "in", "to", "program",
    "spring", "summer", "fall", "autumn", "winter", "student", "students", "early", "career",
}
_WORD = re.compile(r"[A-Za-z0-9]+")
_GH_PATH = re.compile(r"/jobs/(\d{4,})")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
_LONG_ID = re.compile(r"(?<![A-Za-z0-9])(\d{6,}|R\d{4,}|JR\d{4,})(?![A-Za-z0-9])")


@dataclass
class Match:
    tier: str  # job_id, company_title, company_single, ambiguous or none
    application_id: str
    candidates: list[str]
    company: str
    company_key: str
    title: str
    by_domain: bool = False

    def as_evidence(self) -> dict[str, Any]:
        return {"tier": self.tier, "candidates": self.candidates, "company": self.company, "title": self.title}


def _job_ids(url: str) -> set[str]:
    """The ATS job ids a URL carries: a Greenhouse gh_jid or /jobs/<id>, a Lever or Ashby posting UUID, a requisition id."""
    ids: set[str] = set()
    try:
        parts = urlsplit(url)
    except ValueError:
        return ids
    for value in parse_qs(parts.query).get("gh_jid", []):
        if value.isdigit():
            ids.add(value)
    ids.update(_GH_PATH.findall(parts.path))
    ids.update(item.lower() for item in _UUID.findall(parts.path))
    return ids


def _url_key(url: str) -> tuple[str, str] | None:
    canonical, host, path = split_canonical_url(url)
    if not canonical or path in ("", "/"):
        return None
    return host, path


def _words(text: str) -> list[re.Match[str]]:
    return list(_WORD.finditer(text or ""))


def _mentions(text: str, name: str) -> bool:
    """Whether ``name`` appears in ``text`` as a whole company name, not as part of a longer capitalized one."""
    target = [piece.lower() for piece in _WORD.findall(name)]
    while target and target[-1] in {"inc", "llc", "ltd", "corp", "co", "corporation", "company", "incorporated"}:
        target.pop()
    if not target:
        return False
    words = _words(text)
    lowered = [word.group(0).lower() for word in words]
    size = len(target)
    for start in range(len(words) - size + 1):
        if lowered[start:start + size] != target:
            continue
        if not words[start].group(0)[:1].isupper() and not words[start].group(0)[:1].isdigit():
            continue
        before = words[start - 1] if start else None
        after = words[start + size] if start + size < len(words) else None
        if before is not None and _joined(text, before, words[start]) and _is_name_word(before.group(0)):
            continue
        if after is not None and _joined(text, words[start + size - 1], after) and _is_name_word(after.group(0)):
            continue
        return True
    return False


def _joined(text: str, first: re.Match[str], second: re.Match[str]) -> bool:
    """Two words with only spaces (or an ampersand) between them: one phrase, not two."""
    gap = text[first.end():second.start()]
    return bool(gap) and not gap.strip(" &") and "\n" not in gap


def _is_name_word(word: str) -> bool:
    return (word[:1].isupper() or word.isupper()) and word.lower() not in _BESIDE_NAMES


def _title_overlap(title: str, text_words: set[str]) -> float:
    wanted = {word for word in normalized(title).split() if word not in _TITLE_NOISE and not re.fullmatch(r"20\d{2}", word)}
    if not wanted:
        return 0.0
    return len(wanted & text_words) / len(wanted)


_DISPLAY_NOISE = re.compile(
    r"\s+(via\s+\S+.*|hiring team|recruiting( team)?|recruitment|talent( acquisition)?( team)?|careers?|jobs|"
    r"university recruiting|campus recruiting|people team|hr)$",
    re.IGNORECASE,
)
_COMPANY_CUES = (
    re.compile(r"(?:applying|application|applied|interest)\s+(?:to|at|with|in)\s+(?:the\s+)?(?P<c>[A-Z0-9][\w&'’.-]*(?:\s+(?:[A-Z0-9][\w&'’.-]*|&)){0,4})"),
    re.compile(r"(?:position|role|job|opening|internship|interview|opportunity)\s+(?:at|with)\s+(?P<c>[A-Z0-9][\w&'’.-]*(?:\s+(?:[A-Z0-9][\w&'’.-]*|&)){0,4})"),
)
# "Acme Robotics - Application received": a subject that leads with the company.
_SUBJECT_LEAD = re.compile(r"^(?P<c>[A-Z0-9][\w&'’.-]*(?:\s+(?:[A-Z0-9][\w&'’.-]*|&)){0,4})\s*[-–—|:]\s")
# Names that are the job system's, or a mailbox's, never the employer's.
GENERIC_NAMES = {
    "no", "reply", "noreply", "notifications", "notification", "support", "greenhouse", "lever", "workday", "ashby",
    "smartrecruiters", "icims", "workable", "hackerrank", "codesignal", "codility", "hirevue", "calendly", "goodtime",
    "modernloop", "team", "careers", "jobs", "recruiting", "linkedin", "candidate", "home", "talent", "hiring", "update",
    "application", "applications", "interview", "invitation", "thank", "you", "your", "action", "needed", "reminder",
}
_TITLE_CUES = (
    re.compile(r"(?:for|to) the (?P<t>[A-Z][\w/&,'’().-]*(?:\s+[A-Z0-9(][\w/&,'’().-]*){0,8}) (?:role|position|opening|job|internship|program)\b"),
    re.compile(r"application for (?:the )?(?P<t>[A-Z][\w/&'’().-]*(?:\s+[A-Z0-9(][\w/&'’().-]*){0,8})"),
    re.compile(r"(?:applying|applied) for (?:the )?(?P<t>[A-Z][\w/&'’().-]*(?:\s+[A-Z0-9(][\w/&'’().-]*){0,8})"),
)


def _trim_name(phrase: str) -> str:
    words = phrase.split()
    while words and words[-1].lower().strip(".,!:;") in _BESIDE_NAMES:
        words.pop()
    return " ".join(words).strip(" .,!:;-")


def _usable_company_name(name: str) -> str:
    name = _trim_name(name)
    tokens = identity_tokens(name)
    return name[:120] if name and tokens and not tokens <= GENERIC_NAMES else ""


def named_company(mail: Mail) -> str:
    """The company an email names, for proposing to capture a role that is not tracked. '' when none is clear."""
    for text in (mail.subject, mail.text[:2000]):
        for cue in _COMPANY_CUES:
            found = cue.search(text)
            if found and _usable_company_name(found.group("c")):
                return _usable_company_name(found.group("c"))
    lead = _SUBJECT_LEAD.search(mail.subject)
    if lead and _usable_company_name(lead.group("c")):
        return _usable_company_name(lead.group("c"))
    display = _DISPLAY_NOISE.sub("", mail.sender_name).strip(" -,")
    return _usable_company_name(display) if "@" not in display else ""


def named_title(mail: Mail) -> str:
    for text in (mail.subject, mail.text[:2000]):
        for cue in _TITLE_CUES:
            found = cue.search(text)
            if found:
                title = found.group("t").strip(" .,!:;-")
                title = re.split(r"\s+(?:at|with|position|role)\s+", title)[0]
                if 2 <= len(title) <= 120:
                    return title
    return ""


def _applications(conn: sqlite3.Connection, user_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT a.id, a.stage, a.updated_at, a.opportunity_id, o.company, o.title, o.url
        FROM applications a JOIN opportunities o ON o.id=a.opportunity_id WHERE a.user_id=?
        """,
        (user_id,),
    ).fetchall()
    apps = [dict(row) for row in rows]
    if not apps:
        return apps
    ids = [app["opportunity_id"] for app in apps]
    sources: dict[str, list[dict[str, Any]]] = {}
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        for row in conn.execute(
            f"SELECT opportunity_id, external_id, source_url FROM opportunity_sources WHERE opportunity_id IN ({', '.join('?' for _ in chunk)})",
            tuple(chunk),
        ).fetchall():
            sources.setdefault(row["opportunity_id"], []).append(dict(row))
    for app in apps:
        urls = [app["url"], *(source["source_url"] for source in sources.get(app["opportunity_id"], []))]
        app["job_ids"] = {str(source["external_id"]).lower() for source in sources.get(app["opportunity_id"], []) if len(str(source["external_id"])) >= 4}
        for url in urls:
            app["job_ids"] |= _job_ids(str(url or ""))
        app["url_keys"] = {key for key in (_url_key(str(url or "")) for url in urls) if key}
        app["company_key"] = mail_trust.company_key(app["company"])
    return apps


def counts_as_open(conn: sqlite3.Connection, application_id: str, stage: str) -> bool:
    """Open for matching an email: not closed, or archived only by the app after no reply.

    An application archive_silent_applications archived counts as still at
    Applied (plan() may reopen it), so the matcher counts it as open too: an
    email naming only the company is then no clearer about the company's
    other open application than it was before the archive.
    """
    if stage not in CLOSED_STAGES:
        return True
    return stage == "archived" and internal_automation.automation_archived(conn, application_id)


def _rank(apps: list[dict[str, Any]], overlap: dict[str, float], open_ids: set[str]) -> list[dict[str, Any]]:
    """Best first: the most of the role's words, then open (counts_as_open) before closed, then the most recently updated."""
    newest = sorted(apps, key=lambda app: str(app["updated_at"] or ""), reverse=True)
    return sorted(newest, key=lambda app: (-overlap.get(app["id"], 0.0), app["id"] not in open_ids))


def _open_ids(conn: sqlite3.Connection, apps: list[dict[str, Any]]) -> set[str]:
    return {app["id"] for app in apps if counts_as_open(conn, app["id"], str(app["stage"]))}


def match_application(conn: sqlite3.Connection, user_id: str, mail: Mail) -> Match:
    """The application an email is about, and how sure that is (the module docstring lists the tiers)."""
    apps = _applications(conn, user_id)
    company, title = named_company(mail), named_title(mail)
    if not apps:
        return Match("none", "", [], company, mail_trust.company_key(company), title)
    link_ids: set[str] = set()
    link_keys: set[tuple[str, str]] = set()
    for link in mail.links:
        link_ids |= _job_ids(link)
        key = _url_key(link)
        if key:
            link_keys.add(key)
    link_ids |= {item.lower() for item in _LONG_ID.findall(f"{mail.subject}\n{mail.text[:3000]}")}
    by_job = [app for app in apps if (link_ids & app["job_ids"]) or (link_keys & app["url_keys"])]
    text_words = set(normalized(f"{mail.subject}\n{mail.text[:5000]}").split())
    overlap = {app["id"]: _title_overlap(app["title"], text_words) for app in apps}
    if len(by_job) == 1:
        app = by_job[0]
        return Match("job_id", app["id"], [app["id"]], app["company"], app["company_key"], app["title"])
    if len(by_job) > 1:
        ranked = _rank(by_job, overlap, _open_ids(conn, by_job))
        return Match("ambiguous", ranked[0]["id"], [app["id"] for app in ranked], ranked[0]["company"], ranked[0]["company_key"], title)
    display = _DISPLAY_NOISE.sub("", mail.sender_name)
    places = (display, mail.subject, mail.text[:3000])
    sender_keys = mail_trust.known_domains(conn, user_id).get(mail_trust.registrable_domain(mail.sender_domain) or "", set())
    by_company = [
        app for app in apps
        if app["company_key"] and (any(_mentions(place, app["company"]) for place in places) or app["company_key"] in sender_keys)
    ]
    by_domain = bool(sender_keys) and all(app["company_key"] in sender_keys for app in by_company)
    if not by_company:
        return Match("none", "", [], company, mail_trust.company_key(company), title)
    open_ids = _open_ids(conn, by_company)
    ranked = _rank(by_company, overlap, open_ids)
    candidates = [app["id"] for app in ranked]
    titled = [app for app in by_company if overlap[app["id"]] >= 0.8]
    if len(titled) == 1:
        app = titled[0]
        return Match("company_title", app["id"], candidates, app["company"], app["company_key"], app["title"], by_domain)
    # An application the app archived after no reply counts as open here, as in plan(): otherwise its
    # archive would turn an email about either of two applications into a guess acted on for the other.
    open_ones = [app for app in by_company if app["id"] in open_ids]
    if not titled and len(open_ones) == 1:
        app = open_ones[0]
        return Match("company_single", app["id"], candidates, app["company"], app["company_key"], app["title"], by_domain)
    top = ranked[0]
    return Match("ambiguous", top["id"], candidates, top["company"], top["company_key"], title or top["title"], by_domain)


# --- Dates in the text ------------------------------------------------------------------

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4, "may": 5,
    "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_DATE = re.compile(
    r"\b(?:(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?"
    r"|nov(?:ember)?|dec(?:ember)?)\.?\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(?P<year>20\d{2}))?"
    r"|(?P<iso>20\d{2}-\d{2}-\d{2})|(?P<m>\d{1,2})/(?P<d>\d{1,2})(?:/(?P<y>20\d{2}))?)\b",
    re.IGNORECASE,
)
# A deadline's cue comes right before its date, with at most three small words between:
# "by October 3", "due by Friday, October 2", "by 11:59 PM PT on October 3", "before it expires on October 5".
_FILLER = (r"(?:on|the|of|end|midnight|noon|[a-z]+day|it|\d{1,2}(?::\d{2})?\s*(?:am|pm|a\.m\.|p\.m\.)?"
           r"|pt|pst|pdt|et|est|edt|ct|cst|cdt|mt|mst|mdt|utc|gmt)")
_CUE_BEFORE = (
    re.compile(
        r"\b(?P<cue>by|before|no later than|until|due(?: on| by| date)?|expires?(?: on)?|expiring(?: on)?|closes?(?: on)?"
        r"|deadline(?: is| of)?)[\s,:]+(?:" + _FILLER + r"[\s,]+){0,3}$",
        re.IGNORECASE,
    ),
    # "The deadline to return it is October 14"
    re.compile(r"\bdeadline\b[^.!?\n]{0,40}?\b(?:is|:)\s*(?:on\s+)?$", re.IGNORECASE),
)
# "Sent by", "posted by": who did something, not when anything is due.
_NOT_A_CUE = re.compile(
    r"\b(sent|posted|powered|provided|reviewed|signed|delivered|written|created|hosted|shared|managed|operated|made|built"
    r"|submitted|approved|contacted|emailed|called)\s+by\b",
    re.IGNORECASE,
)
TWO_READINGS = "the email's date could be read two ways (month/day or day/month)"
MANY_DATES = "the email states more than one date"


@dataclass(frozen=True)
class StatedDate:
    on: date
    quote: str
    year_stated: bool
    # Why this date should only be proposed, beyond a missing year (TWO_READINGS, MANY_DATES).
    doubts: tuple[str, ...] = ()


def _cued(sentence: str, found: re.Match[str]) -> bool:
    """Whether a date comes right after a deadline's cue in its sentence."""
    before = sentence[:found.start()]
    for cue in _CUE_BEFORE:
        hit = cue.search(before)
        if hit is None:
            continue
        if hit.groupdict().get("cue", "").lower() == "by" and _NOT_A_CUE.search(before[:hit.end("cue")]):
            continue
        return True
    return False


def stated_deadline(text: str, received: datetime) -> StatedDate | None:
    """A deadline the text states ("by October 3, 2026"), with the sentence it came from. None when there is none.

    Only a date right after its cue counts (_CUE_BEFORE). A date with no year
    is read as the first such date on or after the day the email arrived,
    within YEARLESS_WINDOW_DAYS; the caller only proposes those, and those
    with doubts. A date before the email arrived is not a deadline for it.
    """
    received_day = received.date()
    first: StatedDate | None = None
    seen: set[date] = set()
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text[:TEXT_LIMIT]):
        for found in _DATE.finditer(sentence):
            if not _cued(sentence, found):
                continue
            parsed, stated, doubt = _read_date(found, received_day)
            if parsed is None or parsed < received_day or parsed > received_day + timedelta(days=400):
                continue
            seen.add(parsed)
            if first is not None:
                continue
            quote_text = " ".join(redact(sentence).split())
            if len(quote_text) > QUOTE_LIMIT:
                middle = max(0, min(found.start(), len(quote_text)) - QUOTE_LIMIT // 2)
                quote_text = quote_text[middle:middle + QUOTE_LIMIT - 1].strip() + "…"
            first = StatedDate(parsed, quote_text, stated, (doubt,) if doubt else ())
    if first is not None and len(seen) > 1:
        first = StatedDate(first.on, first.quote, first.year_stated, (*first.doubts, MANY_DATES))
    return first


def _read_date(found: re.Match[str], received: date) -> tuple[date | None, bool, str]:
    """(the date, whether its year was stated, a doubt about it or '')."""
    doubt = ""
    try:
        if found.group("iso"):
            return date.fromisoformat(found.group("iso")), True, ""
        if found.group("month"):
            month, day, year = _MONTHS[found.group("month").lower().rstrip(".")], int(found.group("day")), found.group("year")
        else:
            first, second, year = int(found.group("m")), int(found.group("d")), found.group("y")
            if first > 12 >= second:
                month, day = second, first  # 13/10: only day/month reads
            else:
                month, day = first, second
                if first <= 12 and second <= 12 and first != second:
                    doubt = TWO_READINGS
        if year:
            return date(int(year), month, day), True, doubt
        for candidate_year in (received.year, received.year + 1):
            candidate = date(candidate_year, month, day)
            if received <= candidate <= received + timedelta(days=YEARLESS_WINDOW_DAYS):
                return candidate, False, doubt
        return None, False, doubt
    except (KeyError, ValueError):
        return None, False, doubt


# --- Deciding one message ----------------------------------------------------------------


@dataclass
class Planned:
    action_type: str
    subject_kind: str
    subject_id: str
    after: dict[str, Any]
    summary: str
    blockers: list[str]
    guard: Callable[[sqlite3.Connection, dict[str, Any]], bool] | None = None


@dataclass
class Outcome:
    state: str  # done, skipped, awaiting_resume
    kind: str = ""
    application_id: str = ""
    matched_by: str = ""
    event_id: str = ""
    action_id: str = ""
    counts: dict[str, int] = field(default_factory=dict)


def _is_candidate(conn: sqlite3.Connection, user_id: str, mail: Mail) -> bool:
    if mail_trust.listed(mail.sender_domain):
        return True
    if any(mail_trust.listed(host) for host in mail.link_hosts):
        return True
    domain = mail_trust.registrable_domain(mail.sender_domain)
    return bool(domain and domain in mail_trust.known_domains(conn, user_id))


def _manual_after(conn: sqlite3.Connection, application_id: str, received: datetime) -> bool:
    """Whether the student changed this application's stage after the email arrived (manual wins)."""
    row = conn.execute(
        """
        SELECT detail_json, created_at FROM application_events
        WHERE application_id=? AND event_type='stage_changed' ORDER BY created_at DESC, id DESC LIMIT 1
        """,
        (application_id,),
    ).fetchone()
    if row is None:
        return False
    try:
        source = str(json.loads(row["detail_json"] or "{}").get("source") or "user")
    except (TypeError, ValueError):
        source = "user"
    if source.startswith("automation:"):
        return False
    changed = parse_app_instant(row["created_at"])
    return changed is not None and changed > received


def _stage_guard(expected: str) -> Callable[[sqlite3.Connection, dict[str, Any]], bool]:
    """Inside the change's transaction: the stage is still the one the change was planned from."""
    return lambda _conn, before: before.get("stage") == expected


def _reopen_guard(application_id: str) -> Callable[[sqlite3.Connection, dict[str, Any]], bool]:
    """Inside the change's transaction: still archived, and still by the app's own archive for silence.

    internal_automation.automation_archived reads the latest stage change, so an
    archive the student made meanwhile (or an undo of the app's) stops the move:
    a student's archive is never reopened.
    """
    return lambda conn, before: before.get("stage") == "archived" and internal_automation.automation_archived(conn, application_id)


def news_to_archive(conn: sqlite3.Connection, user_id: str, archive: dict[str, Any], received: datetime) -> bool:
    """Whether an email tells an automatic archive (internal_automation.automatic_archive) something it did not know.

    The archive counted the silence from a calendar day (the company's last
    email it knew of, else the day the student applied) and knew of no email
    after it. An email received on a later day than that is news, even when
    it is read after the archive but was received before it (Gmail needed
    reconnecting, a pass ran out of reads, the look back when the switch was
    turned on): the silence the archive was made for never happened. One
    received on that day or before it is not. An archive that did not record
    its day falls back to when it was made.
    """
    archived_at = parse_app_instant(archive.get("archived_at"))
    if archived_at is not None and received > archived_at:
        return True
    since = str(archive.get("silent_since") or "")
    if not since:
        return archived_at is None
    try:
        received_on = user_timezone(conn, user_id).calendar_date(received.isoformat())
        return received_on is not None and received_on > date.fromisoformat(since)
    except ValueError:
        return False


def _open_guard(application_id: str) -> Callable[[sqlite3.Connection, dict[str, Any]], bool]:
    """For a task or deadline planned with a reopen: the application is open now, or still archived only by the app."""
    def check(conn: sqlite3.Connection, _before: dict[str, Any]) -> bool:
        row = conn.execute("SELECT stage FROM applications WHERE id=?", (application_id,)).fetchone()
        return row is not None and counts_as_open(conn, application_id, str(row["stage"]))
    return check


def _platform_name(mail: Mail) -> str:
    for host in [mail.sender_domain, *mail.link_hosts]:
        for domain, name in PLATFORM_NAMES.items():
            if host == domain or host.endswith(f".{domain}"):
                return name
    return ""


def _first_link(mail: Mail, category: str) -> str:
    for link in mail.links:
        if mail_trust.listed(host_of(link), (category,)) and link.lower().startswith(("https://", "http://")):
            return link[:2_000]
    return ""


def _counted(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _tier_blocker(match: Match, needed: set[str], apps_open: int) -> str:
    if match.tier in needed:
        return ""
    if match.tier == "ambiguous":
        if apps_open > 1 and match.company:
            return f"{_counted(apps_open, 'open ' + match.company + ' application')}, so it could be any of them"
        return "no single application clearly matches"
    if match.tier == "company_single":
        return "only the company matched, not the role"
    return "no application matched"


def origin_words(domain: str, verified: bool) -> str:
    """How a change names the email behind it: never as from a domain Gmail did not vouch for."""
    if not domain:
        return "from an email"
    return f"from an email by {domain}" if verified else f"from an email claiming to be from {domain} (sender not verified)"


def _date_blockers(found: StatedDate) -> list[str]:
    return ([] if found.year_stated else ["the email did not state the year of the deadline"]) + list(found.doubts)


def plan(
    conn: sqlite3.Connection, user_id: str, mail: Mail, classification: Classification, match: Match,
    auth: mail_trust.Authentication, *, enabled_at: datetime | None, now: datetime, label: str | None = None,
) -> tuple[list[Planned], list[str]]:
    """What this email should change, each with what stops it acting on its own; and the reasons common to all of it.

    ``label`` is the kind of email acted on (decide's acting label); by default the classification's.
    """
    common: list[str] = []
    domain = mail_trust.registrable_domain(mail.sender_domain) or mail.sender_domain
    sender_category = mail_trust.listed(mail.sender_domain, mail_trust.AUTHORIZING_CATEGORIES)
    trusted = domain in mail_trust.trusted_for(conn, user_id, match.company_key) if match.company_key else False
    if not auth.ok:
        common.append(auth.reason)
    if not sender_category and not trusted:
        if match.by_domain or (match.company_key and domain in mail_trust.known_domains(conn, user_id)):
            common.append(f"mail from {domain} is not trusted for {match.company or 'this company'} yet")
        else:
            common.append("the sender is not a job system or a company domain you trusted")
    if mail.forwarded:
        common.append("it is a forwarded email")
    if mail.bulk:
        common.append("it is a newsletter or mailing-list email")
    if not classification.agrees:
        common.append(f"Jev and the keyword rules disagree about what it means ({classification.label} or {classification.rules_label})")
    elif classification.rules_confidence < AUTO_ACT_MIN_CONFIDENCE:
        common.append(f"the app is not sure enough what it means ({round(classification.rules_confidence * 100)}%)")
    if enabled_at is None or mail.received_at < enabled_at:
        common.append(BEFORE_ENABLED)
    if mail.sent_at is not None and abs(mail.sent_at - mail.received_at) > DATE_GAP_LIMIT:
        common.append("its Date line and when Gmail received it are more than 48 hours apart")

    label = label or classification.label
    origin = origin_words(domain, auth.ok)
    planned: list[Planned] = []
    if not match.application_id:
        if label == "application_confirmation" and match.company:
            capture = {"company": match.company, "title": match.title or named_title(mail), "url": _job_link(mail),
                       "received_at": mail.received_at.isoformat(timespec="seconds")}
            role = f"{capture['title']} at {match.company}" if capture["title"] else f"a role at {match.company}"
            planned.append(Planned(
                "application.capture_proposal", "gmail_message", mail.gmail_id, {"capture": capture},
                f"Looks like you applied to {role}. Add it?", ["a role that is not in your tracker is always yours to add"],
            ))
        return planned, common

    app = conn.execute(
        "SELECT a.stage, o.company, o.title FROM applications a JOIN opportunities o ON o.id=a.opportunity_id WHERE a.id=? AND a.user_id=?",
        (match.application_id, user_id),
    ).fetchone()
    if app is None:
        return planned, common
    stage, company = str(app["stage"]), str(app["company"])
    # An application the app archived itself for no reply (archive_silent_applications) counts as
    # still at the stage it was archived from, so a later email can reopen it; each move out of
    # archived re-checks that inside its own transaction (_reopen_guard). A student's archive stays.
    archive = internal_automation.automatic_archive(conn, match.application_id) if stage == "archived" else None
    current = str(archive["from_stage"]) if archive else stage
    # An email that only confirms, or adds a task or a deadline, reopens it only when the archive did
    # not know of it (news_to_archive): one it had already counted the silence from says nothing new.
    news_since_archive = archive is None or news_to_archive(conn, user_id, archive, mail.received_at)
    reopen_waits: list[str] = []
    who = f"{company} ({app['title']})"
    apps_open = sum(
        1 for row in conn.execute(
            "SELECT a.id, a.stage, o.company FROM applications a JOIN opportunities o ON o.id=a.opportunity_id WHERE a.user_id=?", (user_id,),
        ).fetchall() if mail_trust.company_key(row["company"]) == match.company_key and counts_as_open(conn, str(row["id"]), str(row["stage"]))
    )
    manual = _manual_after(conn, match.application_id, mail.received_at)
    received_iso = mail.received_at.isoformat(timespec="seconds")

    def stage_change(target: str, tiers: set[str], extra: list[str] | None = None) -> None:
        blockers = [blocker for blocker in [_tier_blocker(match, tiers, apps_open), *(extra or [])] if blocker]
        if manual:
            blockers.append("you changed this application's stage after the email arrived")
        after = {"stage": target}
        words = f"{who}: move to {target}, {origin}"
        if target == "applied" or (label == "application_confirmation" and target == current):
            after["applied_at"] = received_iso
            if target == stage:
                words = f"{who}: set when you applied to {mail.received_at:%b} {mail.received_at.day}, {origin}"
        guard = _stage_guard(stage)
        if archive:
            words = f"{who}: reopen (archived automatically after no reply) and move to {target}, {origin}"
            guard = _reopen_guard(match.application_id)
            # What stops the reopen stops the task or deadline that comes with it too (reopening).
            reopen_waits[:] = blockers
        planned.append(Planned("application.stage", "application", match.application_id, after, words, blockers, guard))

    def reopening(blockers: list[str]) -> Callable[[sqlite3.Connection, dict[str, Any]], bool] | None:
        """For a task or deadline on an application the app archived: never added on its own to one still archived."""
        if not archive:
            return None
        if reopen_waits:
            blockers.append("reopening the application, which the app archived after no reply, waits for you")
        return _open_guard(match.application_id)

    def task(title: str, due_at: str | None, link: str, tiers: set[str], extra: list[str] | None = None) -> None:
        blockers = [blocker for blocker in [_tier_blocker(match, tiers, apps_open), *(extra or [])] if blocker]
        guard = reopening(blockers)
        spec = {"title": title, "due_at": due_at, "origin": "email", "origin_ref": mail.gmail_id, "link": link}
        planned.append(Planned("application.task", "application", match.application_id, {"task": spec},
                               f"{who}: add the task “{title}”, {origin}", blockers, guard))

    def deadline(found: StatedDate) -> None:
        blockers = [blocker for blocker in [_tier_blocker(match, ANY_TIER, apps_open)] if blocker]
        blockers.extend(_date_blockers(found))
        guard = reopening(blockers)
        # Urgent's note reads "From <sender_domain>, received ...": never a domain Gmail did not vouch for, unmarked.
        spec = {"deadline_on": found.on.isoformat(), "quote": found.quote,
                "sender_domain": domain if auth.ok or not domain else f"{domain} (sender not verified)",
                "gmail_id": mail.gmail_id, "received_at": received_iso}
        planned.append(Planned("application.deadline", "application", match.application_id, {"deadline": spec},
                               f"{who}: add the deadline {found.on:%b} {found.on.day}, {origin}", blockers, guard))

    # An interview, a rejection or an offer reopens an application the app archived whenever it came;
    # a confirmation, a task or a deadline only when the archive did not know of it (news_since_archive).
    closed = stage in CLOSED_STAGES and not (archive and (news_since_archive or label in ("interview", "rejected", "offer")))
    if label == "application_confirmation" and not closed:
        stage_change("applied" if current == "applying" else current, ANY_TIER)
    elif label == "interview" and not closed and current != "offer":
        if current in ("applying", "applied"):
            stage_change("interview", STRONG_TIERS)
        due = (now + timedelta(days=SCHEDULE_TASK_DAYS)).isoformat(timespec="minutes")
        task("Schedule interview", due, _first_link(mail, "scheduling"), ANY_TIER)
    elif label == "scheduling" and not closed:
        if archive:
            stage_change(current, ANY_TIER)
        due = (now + timedelta(days=SCHEDULE_TASK_DAYS)).isoformat(timespec="minutes")
        task("Schedule interview", due, _first_link(mail, "scheduling"), ANY_TIER)
    elif label == "rejected" and current in ("applying", "applied", "interview"):
        stage_change("rejected", STRONG_TIERS)
    elif label == "offer" and current != "offer":
        # Proposed whatever the stage: an offer for an application the student closed is theirs to judge.
        stage_change("offer", ANY_TIER, ["an offer is always yours to confirm", *([f"the application is {stage}"] if closed else [])])
    elif label in ("assessment", "deadline") and not closed:
        found = stated_deadline(mail.text, mail.received_at)
        if archive and (label == "assessment" or found):
            stage_change(current, ANY_TIER)
        if label == "assessment":
            platform = _platform_name(mail)
            title = f"Complete the {platform} assessment" if platform else "Complete the assessment"
            due = f"{found.on.isoformat()}T23:59:00" if found else None
            task(title, due, _first_link(mail, "assessment"), ANY_TIER, [] if found is None else _date_blockers(found))
        if found:
            deadline(found)
    return planned, common


def _job_link(mail: Mail) -> str:
    """A job posting link from the email for the capture form: host and path only, and a Greenhouse job id."""
    for link in mail.links:
        ids = _job_ids(link)
        if not ids:
            continue
        try:
            parts = urlsplit(link)
        except ValueError:
            continue
        jid = parse_qs(parts.query).get("gh_jid", [""])[0]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, f"gh_jid={jid}" if jid.isdigit() else "", ""))[:500]
    return ""


def _enabled_at(sync: dict[str, Any]) -> datetime | None:
    return parse_app_instant(sync.get("enabled_at"))


def _evidence(mail: Mail, classification: Classification, match: Match, auth: mail_trust.Authentication, *,
              event_id: str, blockers: list[str], origin: str) -> dict[str, Any]:
    return {
        "source": "gmail", "gmail_id": mail.gmail_id, "thread_id": mail.thread_id, "sender": mail.sender,
        "sender_domain": mail_trust.registrable_domain(mail.sender_domain) or mail.sender_domain,
        "subject": redact(mail.subject)[:300], "date_header": mail.date_header,
        "received_at": mail.received_at.isoformat(timespec="seconds"), "excerpt": excerpt(mail.text),
        "text_sha256": hashlib.sha256(mail.text.encode("utf-8")).hexdigest(), "link_hosts": mail.link_hosts[:20],
        "event_id": event_id, "label": classification.label, "classified_by": classification.classified_by,
        "match": match.as_evidence(), "auth": auth.as_evidence(), "why_proposal": blockers, "origin": origin,
    }


def _event_id(conn: sqlite3.Connection, user_id: str, gmail_id: str) -> str | None:
    row = conn.execute(
        "SELECT id FROM monitored_events WHERE user_id=? AND external_id=? ORDER BY created_at LIMIT 1", (user_id, f"gmail:{gmail_id}"),
    ).fetchone()
    return str(row["id"]) if row else None


def _insert_event(
    conn: sqlite3.Connection, user_id: str, event_id: str, mail: Mail, classification: Classification, match: Match,
    *, label: str, verified: bool,
) -> str:
    """The monitored_events row for this email (made once), which the email card and the ledger both point to."""
    connector = connector_row(conn, user_id)
    connector_id = connector["id"] if connector is not None else None
    external_id = f"gmail:{mail.gmail_id}"
    payload = {
        "source": "application_mail", "subject": redact(mail.subject)[:300], "body_preview": excerpt(mail.text),
        "sender": mail.sender[:300], "sender_domain": mail_trust.registrable_domain(mail.sender_domain) or mail.sender_domain,
        "classified_by": classification.classified_by, "gmail_id": mail.gmail_id, "thread_id": mail.thread_id,
        "candidates": match.candidates, "matched_by": match.tier, "received_at": mail.received_at.isoformat(timespec="seconds"),
        # Whether Gmail vouched for the sender, so the card never names an unverified domain as fact.
        "sender_verified": verified,
    }
    with conn:
        conn.execute(
            """
            INSERT INTO monitored_events(id, user_id, connector_id, external_id, event_type, confidence, payload_json, status, application_id, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?) ON CONFLICT(user_id, connector_id, external_id) DO NOTHING
            """,
            (event_id, user_id, connector_id, external_id, label, classification.confidence,
             json.dumps(payload, sort_keys=True), match.application_id or None, utc_now()),
        )
    return _event_id(conn, user_id, mail.gmail_id) or event_id


def decide(
    conn: sqlite3.Connection, user_id: str, mail: Mail, *, decisions: DecisionClient | None, sync: dict[str, Any],
    origin: str, now: datetime,
) -> Outcome:
    """Classify, match, and act on or propose what one email says. Never raises for what the email contains."""
    account = sender_account().lower()
    if (account and mail.sender == account) or {"SENT", "DRAFT"} & set(mail.labels) or not _is_candidate(conn, user_id, mail):
        return Outcome("skipped")
    classification = classify(mail, decisions)
    match = match_application(conn, user_id, mail)
    # Linked to an application only when the match says which one; an ambiguous match is a guess
    # until the student picks (after_decision, decide_event).
    linked = match.application_id if match.tier in ANY_TIER else ""
    kind = acting_label(classification)
    if kind not in ACTIONABLE:
        return Outcome("done", classification.label, linked, match.tier)
    if kind == "offer":
        # Always, whatever the match and the stage: the most consequential email never passes quietly.
        automation.notice(
            conn, user_id, event_key=f"offer:{mail.gmail_id}", level="info",
            title=f"{match.company or named_company(mail) or 'A company'} may have sent an offer", body="Open Automation to review it.",
        )
    if automation.paused(conn, user_id):
        return Outcome("awaiting_resume", kind, linked, match.tier)
    auth = mail_trust.authenticate(mail.message)
    enabled_at = _enabled_at(sync)
    planned, common = plan(conn, user_id, mail, classification, match, auth, enabled_at=enabled_at, now=now, label=kind)
    if origin == RECLAIMED:
        common.append("it was read late, after outreach stopped holding it as a reply")
    # Made only when there is something for the card: a change, or an email no application matched.
    existing_event = _event_id(conn, user_id, mail.gmail_id)
    event_id = existing_event or f"event-{uuid4().hex}"
    outcome = Outcome("done", kind, linked, match.tier, "", counts={"applied": 0, "proposed": 0, "shadow": 0})
    backfill = enabled_at is None or mail.received_at < enabled_at
    basis_parts = [f"classify:{classification.classified_by.get('source', 'rules')}", f"match:{match.tier}"]
    if classification.prior:
        basis_parts.append(f"prior:{classification.prior}")
    if backfill:
        basis_parts.append(BACKFILL_BASIS)
    applied_any = False
    for item in planned:
        blockers = [*common, *item.blockers]
        row = automation.perform(
            conn, user_id=user_id, feature=FEATURE, action_type=item.action_type, subject_kind=item.subject_kind,
            subject_id=item.subject_id, after=item.after,
            evidence=_evidence(mail, classification, match, auth, event_id=event_id, blockers=blockers, origin=origin),
            summary=item.summary, basis=";".join(basis_parts), confidence=classification.confidence,
            idempotency_key=f"gmail:{mail.gmail_id}:{item.subject_id}:{item.action_type}", auto=not blockers,
            policy_version=POLICY_VERSION, guard=item.guard,
        )
        if row is None:
            continue
        outcome.action_id = outcome.action_id or str(row["id"])
        status = str(row["status"])
        if status == "applied" and row.get("decided_by") == "system":
            applied_any = True
            outcome.counts["applied"] += 1
        elif status in ("proposed", "shadow"):
            outcome.counts[status] += 1
    # A card only for something to decide or already done: in shadow nothing was done, and its
    # rows are reviewed under Would have done instead. An offer always gets one.
    if applied_any or outcome.counts["proposed"] or not match.application_id or existing_event or kind == "offer":
        outcome.event_id = existing_event or _insert_event(conn, user_id, event_id, mail, classification, match,
                                                           label=kind, verified=auth.ok)
    if automation.paused(conn, user_id):
        # The pause landed while this email was being decided: decide it again after resume (same keys, never twice).
        outcome.state = "awaiting_resume"
        return outcome
    _suggest_from_email(conn, user_id, mail, match, auth)
    # Decided by the app only when nothing is left for the student; otherwise the card waits for them.
    if applied_any and not outcome.counts["proposed"] and outcome.event_id:
        with conn:
            conn.execute(
                """
                UPDATE monitored_events SET status='confirmed', decided_at=?, decided_by='system', application_id=?
                WHERE id=? AND user_id=? AND status='pending'
                """,
                (utc_now(), match.application_id or None, outcome.event_id, user_id),
            )
    return outcome


def acting_label(classification: Classification) -> str:
    """The kind of email to act on: Jev's, or when Jev found nothing to act on but the rules did, the rules'.

    Either way a disagreement is one of plan()'s blockers, so it only proposes.
    """
    if classification.label in ACTIONABLE or classification.rules_label not in ACTIONABLE:
        return classification.label
    return classification.rules_label


def _suggest_from_email(conn: sqlite3.Connection, user_id: str, mail: Mail, match: Match, auth: mail_trust.Authentication) -> None:
    """An authenticated email from a company's own domain about one of its applications suggests trusting that domain."""
    if not auth.ok or not match.company or match.tier not in ANY_TIER or mail_trust.not_an_employer(mail.sender_domain):
        return
    domain = mail_trust.registrable_domain(mail.sender_domain)
    if not domain:
        return
    with conn:
        mail_trust.suggest(
            conn, user_id, company=match.company, host=domain, source="email",
            evidence=f"An email from {domain} about your {match.company} application passed Gmail's sender check.",
        )


# --- The durable cursor ----------------------------------------------------------------


def _ensure_sync(conn: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    with conn:
        conn.execute(
            "INSERT INTO application_mail_sync(user_id, updated_at) VALUES(?, ?) ON CONFLICT(user_id) DO NOTHING",
            (user_id, utc_now()),
        )
    return dict(conn.execute("SELECT * FROM application_mail_sync WHERE user_id=?", (user_id,)).fetchone())


def _locked_sync(conn: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    """Inside the caller's transaction: the sync row, held until it commits (the write comes first; see automation)."""
    conn.execute("UPDATE application_mail_sync SET updated_at=updated_at WHERE user_id=?", (user_id,))
    lock = " FOR UPDATE" if getattr(conn, "backend", "sqlite") == "postgresql" else ""
    return dict(conn.execute(f"SELECT * FROM application_mail_sync WHERE user_id=?{lock}", (user_id,)).fetchone())


def _update_sync(conn: sqlite3.Connection, user_id: str, **values: Any) -> None:
    """Set sync columns. Opens no transaction."""
    columns = ", ".join(f"{name}=?" for name in values)
    conn.execute(f"UPDATE application_mail_sync SET {columns}, updated_at=? WHERE user_id=?", (*values.values(), utc_now(), user_id))


def _ids(value: Any) -> list[str]:
    try:
        items = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return [str(item) for item in items if str(item)] if isinstance(items, list) else []


class _Reset(Exception):
    """The switch was turned off (and its cursor forgotten) while this pass ran: it stops, writing nothing back."""


_ANY = object()


def _pass_sync(conn: sqlite3.Connection, user_id: str, expect: Any) -> dict[str, Any]:
    """_locked_sync, for a write a pass makes: raises _Reset when the switch is off or was reset since the pass began.

    ``expect`` is the enabled_at the pass started from (_ANY skips the check).
    Inside the caller's transaction, so note_off either lands first and is seen
    here, or waits until this write commits and then clears it.
    """
    sync = _locked_sync(conn, user_id)
    if expect is not _ANY and (sync.get("enabled_at") != expect or automation.mode(conn, user_id, FEATURE) == "off"):
        raise _Reset()
    return sync


def _queue_ids(conn: sqlite3.Connection, user_id: str, column: str, ids: list[str], *, expect: Any = _ANY, **values: Any) -> int:
    """Append ids to a queue and set other sync columns (the cursor) in the same transaction. Returns how many were new."""
    with conn:
        sync = _pass_sync(conn, user_id, expect)
        queued = _ids(sync[column])
        seen = set(queued)
        added = [item for item in ids if item and item not in seen and not seen.add(item)]
        _update_sync(conn, user_id, **{column: json.dumps(queued + added)}, **values)
    return len(added)


def _record_outcome(conn: sqlite3.Connection, user_id: str, mail: Mail | None, gmail_id: str, outcome: Outcome, *,
            queue: str | None, origin: str, received_at: str = "", expect: Any = _ANY) -> None:
    """Record one message's outcome and take it off its queue, in one transaction."""
    with conn:
        sync = _pass_sync(conn, user_id, expect)
        keep = outcome.state not in ("skipped", "gone", "outreach", "error") and mail is not None
        conn.execute(
            """
            INSERT INTO application_mail_messages(user_id, gmail_id, thread_id, application_id, event_id, action_id, kind,
                matched_by, state, origin, subject, sender_domain, received_at, recorded_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, gmail_id) DO UPDATE SET application_id=excluded.application_id, event_id=excluded.event_id,
                action_id=excluded.action_id, kind=excluded.kind, matched_by=excluded.matched_by, state=excluded.state,
                recorded_at=excluded.recorded_at,
                thread_id=CASE WHEN ? THEN excluded.thread_id ELSE application_mail_messages.thread_id END,
                subject=CASE WHEN ? THEN excluded.subject ELSE application_mail_messages.subject END,
                sender_domain=CASE WHEN ? THEN excluded.sender_domain ELSE application_mail_messages.sender_domain END,
                received_at=CASE WHEN ? THEN excluded.received_at ELSE application_mail_messages.received_at END
            WHERE application_mail_messages.state='awaiting_resume'
            """,
            (
                user_id, gmail_id, mail.thread_id if mail and keep else "", outcome.application_id if keep else "",
                outcome.event_id, outcome.action_id, outcome.kind if keep else "", outcome.matched_by if keep else "",
                outcome.state, origin, redact(mail.subject)[:300] if mail and keep else "",
                (mail_trust.registrable_domain(mail.sender_domain) or mail.sender_domain) if mail and keep else "",
                mail.received_at.isoformat(timespec="seconds") if mail else (received_at or utc_now()), utc_now(),
                # A row set aside unread (left to outreach, read while paused) gets what reading it found.
                bool(mail and keep), bool(mail and keep), bool(mail and keep), mail is not None,
            ),
        )
        if queue:
            remaining = [item for item in _ids(sync[queue]) if item != gmail_id]
            _update_sync(conn, user_id, **{queue: json.dumps(remaining)})


def _known(conn: sqlite3.Connection, user_id: str, gmail_id: str) -> Any:
    return conn.execute(
        "SELECT state FROM application_mail_messages WHERE user_id=? AND gmail_id=?", (user_id, gmail_id),
    ).fetchone()


def _outreach_owns(conn: sqlite3.Connection, user_id: str, gmail_id: str) -> bool:
    """Whether outreach holds this message as a company's reply, automatic reply, or possible reply (outreach_inbox.owned_sql).

    A message outreach only set aside, or matched to a company by its domain
    alone, is still this reader's to judge: a recruiter at a company the
    student also wrote to may be writing about the application.
    """
    return conn.execute(
        f"SELECT 1 FROM outreach_inbox_messages WHERE user_id=? AND gmail_id=? AND target_id<>'' AND {owned_sql()}",
        (user_id, gmail_id),
    ).fetchone() is not None


def _reclaim(conn: sqlite3.Connection, user_id: str, *, expect: Any) -> int:
    """Take back what this reader left to outreach that outreach no longer holds, to read it on its own rules.

    Outreach once set aside mail from a company's domain while still owning it,
    and a student can dismiss a possible reply or confirm one matched only by
    domain. Once outreach has judged the message under its current rules and
    does not hold it, its 'outreach' record here becomes one waiting to be
    decided again (as mail read while paused is, _rescan), with the origin
    'reclaimed': read late, so what it says is only ever proposed (decide).
    One transaction, with the sync row locked. Returns how many.
    """
    with conn:
        _pass_sync(conn, user_id, expect)
        ids = [str(row[0]) for row in conn.execute(
            f"""
            SELECT a.gmail_id FROM application_mail_messages a
            JOIN outreach_inbox_messages o ON o.user_id=a.user_id AND o.gmail_id=a.gmail_id
            WHERE a.user_id=? AND a.state='outreach' AND o.rules >= ? AND NOT {owned_sql('o')}
            ORDER BY a.gmail_id
            """,
            (user_id, RULES),
        ).fetchall()]
        if not ids:
            return 0
        conn.executemany(
            "UPDATE application_mail_messages SET state='awaiting_resume', origin=? WHERE user_id=? AND gmail_id=? AND state='outreach'",
            [(RECLAIMED, user_id, gmail_id) for gmail_id in ids],
        )
    return len(ids)


class _Stop(Exception):
    """Gmail could not answer a read; the pass stops and the message stays queued."""


class _Budget:
    def __init__(self, gets: int) -> None:
        self.left = gets

    def take(self) -> bool:
        if self.left <= 0:
            return False
        self.left -= 1
        return True


def _fetch(gmail: GmailClient, gmail_id: str) -> dict[str, Any] | None:
    """messages.get in the raw format. None when the message is gone; raises _Stop when Gmail could not answer."""
    response = gmail.request("GET", f"/messages/{quote(gmail_id, safe='')}", params={"format": "raw"})
    if response.status_code == 404:
        return None
    if response.status_code != 200:
        raise _Stop(f"Gmail answered HTTP {response.status_code} for a message")
    return response.json()


def _drain(
    conn: sqlite3.Connection, gmail: GmailClient, user_id: str, queue: str, origin: str, budget: _Budget,
    decisions: DecisionClient | None, now: datetime, totals: dict[str, int], *, expect: Any = _ANY,
) -> None:
    """Read and decide queued ids until the queue or the budget runs out."""
    sync = dict(conn.execute("SELECT * FROM application_mail_sync WHERE user_id=?", (user_id,)).fetchone())
    for gmail_id in _ids(sync[queue]):
        if _known(conn, user_id, gmail_id) is not None:
            _dequeue(conn, user_id, queue, gmail_id)  # read already (by another pass, or found twice)
            continue
        if _outreach_owns(conn, user_id, gmail_id):
            _record_outcome(conn, user_id, None, gmail_id, Outcome("outreach"), queue=queue, origin=origin, expect=expect)
            continue
        if not budget.take():
            break
        data = _fetch(gmail, gmail_id)
        if data is None:
            _record_outcome(conn, user_id, None, gmail_id, Outcome("gone"), queue=queue, origin=origin, expect=expect)
            continue
        mail, outcome = _decide_safely(conn, user_id, data, decisions=decisions, sync=sync, origin=origin, now=now)
        _record_outcome(conn, user_id, mail, gmail_id, outcome, queue=queue, origin=origin, expect=expect)
        _tally(totals, outcome)


def _rescan(
    conn: sqlite3.Connection, gmail: GmailClient, user_id: str, budget: _Budget, decisions: DecisionClient | None,
    now: datetime, totals: dict[str, int], *, expect: Any = _ANY,
) -> None:
    """Decide again the messages read while automation was paused."""
    if automation.paused(conn, user_id):
        return
    sync = dict(conn.execute("SELECT * FROM application_mail_sync WHERE user_id=?", (user_id,)).fetchone())
    rows = conn.execute(
        "SELECT gmail_id, origin FROM application_mail_messages WHERE user_id=? AND state='awaiting_resume' ORDER BY received_at LIMIT ?",
        (user_id, max(budget.left, 0)),
    ).fetchall()
    for row in rows:
        if not budget.take():
            break
        data = _fetch(gmail, row["gmail_id"])
        if data is None:
            _record_outcome(conn, user_id, None, row["gmail_id"], Outcome("gone"), queue=None, origin=row["origin"], expect=expect)
            continue
        mail, outcome = _decide_safely(conn, user_id, data, decisions=decisions, sync=sync, origin=row["origin"], now=now)
        _record_outcome(conn, user_id, mail, row["gmail_id"], outcome, queue=None, origin=row["origin"], expect=expect)
        _tally(totals, outcome)


def _decide_safely(
    conn: sqlite3.Connection, user_id: str, data: dict[str, Any], *, decisions: DecisionClient | None, sync: dict[str, Any],
    origin: str, now: datetime,
) -> tuple[Mail | None, Outcome]:
    """decide(), with one message that cannot be read or decided set aside instead of holding up every pass after it.

    A busy database (locked, a deadlock) is not the message's fault: it is
    raised, so the pass stops and the message stays queued for the next one.
    Its changes made so far stand, and deciding it again finishes the rest
    (the same idempotency keys).
    """
    try:
        mail = parse_message(data)
        return mail, decide(conn, user_id, mail, decisions=decisions, sync=sync, origin=origin, now=now)
    except Exception as exc:  # noqa: BLE001 - one bad message never blocks the queue
        if getattr(conn, "in_transaction", False):
            conn.rollback()
        if is_transient_error(exc):
            raise
        LOGGER.warning("An application email could not be decided; it was set aside", exc_info=True)
        return None, Outcome("error")


def _dequeue(conn: sqlite3.Connection, user_id: str, queue: str, gmail_id: str) -> None:
    with conn:
        sync = _locked_sync(conn, user_id)
        _update_sync(conn, user_id, **{queue: json.dumps([item for item in _ids(sync[queue]) if item != gmail_id])})


def _tally(totals: dict[str, int], outcome: Outcome) -> None:
    totals["read"] = totals.get("read", 0) + 1
    if outcome.state in ("awaiting_resume", "error"):
        totals[outcome.state] = totals.get(outcome.state, 0) + 1
    for key, value in outcome.counts.items():
        totals[key] = totals.get(key, 0) + value


def _epoch(moment: datetime) -> int:
    return int(moment.timestamp())


def _list_ids(gmail: GmailClient, query: str, page_token: str, pages: int) -> tuple[list[str], str]:
    """Up to ``pages`` pages of a messages.list search from ``page_token``: the ids, and the token to go on from ('' when done)."""
    found: list[str] = []
    token = page_token
    for _page in range(pages):
        params: dict[str, Any] = {"q": query, "maxResults": 100}
        if token:
            params["pageToken"] = token
        response = gmail.request("GET", "/messages", params=params)
        if response.status_code != 200:
            raise _Stop(f"Gmail answered HTTP {response.status_code} for a search")
        data = response.json()
        found.extend(str(item.get("id", "")) for item in data.get("messages") or [] if item.get("id"))
        token = str(data.get("nextPageToken") or "")
        if not token:
            break
    return found, token


def _profile_history(gmail: GmailClient) -> str:
    response = gmail.request("GET", "/profile")
    if response.status_code != 200:
        raise _Stop(f"Gmail answered HTTP {response.status_code} for the profile")
    history_id = str(response.json().get("historyId") or "")
    if not history_id:
        raise _Stop("Gmail's profile had no history id")
    return history_id


def _start(conn: sqlite3.Connection, gmail: GmailClient, user_id: str, now: datetime) -> str:
    """The first pass with the switch on: when it was turned on, the live cursor, and the backfill to run.

    Returns the enabled_at it recorded. The backfill's search ends a minute
    after the live cursor was taken (not when the pass began), so a message
    that arrived while the pass was getting ready is in one or the other.
    """
    history_id = _profile_history(gmail)
    until = datetime.now(timezone.utc) + BACKFILL_UNTIL_MARGIN
    turned_on = parse_app_instant(setting_updated_at(conn, user_id, FEATURE))
    enabled = min(turned_on, now) if turned_on is not None else now
    query = backfill_query(conn, user_id, enabled, until)
    enabled_text = enabled.isoformat(timespec="seconds")
    with conn:
        _locked_sync(conn, user_id)
        if automation.mode(conn, user_id, FEATURE) == "off":
            raise _Reset()  # switched off while this pass was getting ready
        _update_sync(
            conn, user_id, history_id=history_id, enabled_at=enabled_text,
            backfill_state="running", backfill_query=query, backfill_page_token="",
            backfill_ids_json="[]", pending_ids_json="[]", recovery_state="", recovery_page_token="", recovery_history_id="",
        )
    return enabled_text


def _collect_history(conn: sqlite3.Connection, gmail: GmailClient, user_id: str, sync: dict[str, Any], now: datetime, *, expect: Any = _ANY) -> None:
    """Page through history.list from the cursor; store the new ids and the new cursor together. Starts recovery on a 404."""
    token = ""
    ids: list[str] = []
    newest = ""
    last_record = ""
    for _page in range(MAX_LIST_PAGES):
        params: dict[str, Any] = {"startHistoryId": sync["history_id"], "historyTypes": "messageAdded", "labelId": "INBOX", "maxResults": 500}
        if token:
            params["pageToken"] = token
        response = gmail.request("GET", "/history", params=params)
        if response.status_code == 404:
            _begin_recovery(conn, gmail, user_id, sync, now, expect=expect)
            return
        if response.status_code != 200:
            raise _Stop(f"Gmail answered HTTP {response.status_code} for history")
        data = response.json()
        newest = str(data.get("historyId") or newest)
        for record in data.get("history") or []:
            last_record = str(record.get("id") or last_record)
            for added in record.get("messagesAdded") or []:
                message = added.get("message") or {}
                if message.get("id") and "INBOX" in (message.get("labelIds") or ["INBOX"]):
                    ids.append(str(message["id"]))
        token = str(data.get("nextPageToken") or "")
        if not token:
            break
    # Cut short with pages left: resume from the last record read, never skipping the rest.
    cursor = last_record if token and last_record else (newest or sync["history_id"])
    _queue_ids(conn, user_id, "pending_ids_json", ids, expect=expect, history_id=cursor)


def _begin_recovery(conn: sqlite3.Connection, gmail: GmailClient, user_id: str, sync: dict[str, Any], now: datetime, *, expect: Any = _ANY) -> None:
    """Gmail forgot the cursor: take a fresh one first, then search from the last good pass (a day before, never before enabled_at)."""
    fresh = _profile_history(gmail)
    last_ok = parse_app_instant(sync.get("last_ok_at")) or parse_app_instant(sync.get("enabled_at")) or now
    enabled = parse_app_instant(sync.get("enabled_at"))
    after = last_ok - RECOVERY_OVERLAP
    if enabled is not None and after < enabled:
        after = enabled
    with conn:
        _pass_sync(conn, user_id, expect)
        _update_sync(conn, user_id, recovery_state="running", recovery_after=str(_epoch(after)), recovery_page_token="",
                     recovery_history_id=fresh)
    _recover(conn, gmail, user_id, now, expect=expect)


def _recover(conn: sqlite3.Connection, gmail: GmailClient, user_id: str, now: datetime, *, expect: Any = _ANY) -> None:
    """Go on with a recovery search; when it is done, live reading resumes from the cursor taken before it began."""
    sync = dict(conn.execute("SELECT * FROM application_mail_sync WHERE user_id=?", (user_id,)).fetchone())
    query = f"in:inbox after:{sync['recovery_after']}"
    ids, token = _list_ids(gmail, query, sync["recovery_page_token"], MAX_LIST_PAGES)
    if token:
        _queue_ids(conn, user_id, "pending_ids_json", ids, expect=expect, recovery_page_token=token)
    else:
        _queue_ids(conn, user_id, "pending_ids_json", ids, expect=expect, recovery_state="", recovery_page_token="",
                   history_id=sync["recovery_history_id"] or sync["history_id"], recovery_history_id="")


def _backfill(conn: sqlite3.Connection, gmail: GmailClient, user_id: str, budget: _Budget, decisions: DecisionClient | None,
              now: datetime, totals: dict[str, int], *, expect: Any = _ANY) -> None:
    """The first-run look at the 60 days before enabled_at: one page at a time into its own queue, proposal-only."""
    sync = dict(conn.execute("SELECT * FROM application_mail_sync WHERE user_id=?", (user_id,)).fetchone())
    if sync["backfill_state"] != "running" or budget.left <= 0:
        return
    if not _ids(sync["backfill_ids_json"]):
        if sync["backfill_page_token"] == "done":
            with conn:
                _pass_sync(conn, user_id, expect)
                _update_sync(conn, user_id, backfill_state="done", backfill_page_token="")
            return
        ids, token = _list_ids(gmail, sync["backfill_query"], sync["backfill_page_token"], 1)
        _queue_ids(conn, user_id, "backfill_ids_json", ids, expect=expect, backfill_page_token=token or "done")
    _drain(conn, gmail, user_id, "backfill_ids_json", "backfill", budget, decisions, now, totals, expect=expect)


def backfill_query(conn: sqlite3.Connection, user_id: str, enabled_at: datetime, until: datetime) -> str:
    """The backfill's Gmail search: a term for every live-filter criterion, over the 60 days before enabled_at.

    from: terms for every listed sender domain and every domain suggested or
    trusted for a company, and quoted host terms for the listed domains, which
    Gmail's full-text search finds in links. It ends at ``until``, which the
    first pass takes a minute after the live cursor, so nothing between turning
    the switch on and the first pass falls between the two (a message found by
    both is read once).
    """
    lists = mail_trust.sender_lists()
    listed = sorted({domain for category in mail_trust.READ_CATEGORIES for domain in lists.get(category, ())})
    mail_trust.refresh_suggestions(conn, user_id)
    companies = sorted(mail_trust.known_domains(conn, user_id))
    senders = " OR ".join([*listed, *companies])
    hosts = " OR ".join(f'"{domain}"' for domain in listed)
    start = enabled_at - timedelta(days=BACKFILL_DAYS)
    return f"(from:({senders}) OR {hosts}) after:{_epoch(start)} before:{_epoch(max(until, enabled_at))} -in:sent -in:drafts -in:chats"


# --- A pass ----------------------------------------------------------------------------


def _lock(user_id: str) -> threading.Lock:
    with _PASS_LOCKS_GUARD:
        return _PASS_LOCKS.setdefault(user_id, threading.Lock())


def note_off(conn: sqlite3.Connection, user_id: str) -> None:
    """The switch is off: forget the cursor, so turning it on again starts afresh (a new enabled_at and backfill).

    Never raises. What was read stays recorded, so nothing is decided twice.
    """
    try:
        row = conn.execute("SELECT history_id FROM application_mail_sync WHERE user_id=?", (user_id,)).fetchone()
        if row is None or not row["history_id"]:
            return
        with conn:
            _locked_sync(conn, user_id)
            _update_sync(conn, user_id, history_id="", pending_ids_json="[]", recovery_state="", recovery_page_token="",
                         recovery_history_id="", backfill_state="", backfill_page_token="", backfill_ids_json="[]",
                         enabled_at=None, last_pass_at=None)
    except Exception:  # noqa: BLE001 - a background step
        if getattr(conn, "in_transaction", False):
            conn.rollback()
        LOGGER.warning("Could not reset application mail after it was switched off", exc_info=True)


def run_pass(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    client_factory: ClientFactory,
    decisions: DecisionClient | None = None,
    now: datetime | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """One pass: read new mail (or recover, or start), decide what is queued, then the backfill.

    ``state`` is ok, message_errors, off, not_connected, needs_reconnect,
    throttled, unreachable, or database_busy, as the inbox watcher records
    it. ``skipped`` is True when a pass ran less than ten minutes ago (unless
    ``force``), when another pass holds the reader, or (with state off) when
    the switch was turned off while this one ran. ``detail`` holds counts
    only, never an address or a message's words.
    """
    now = now or datetime.now(timezone.utc)
    result: dict[str, Any] = {"state": "ok", "skipped": False, "detail": {}}
    if automation.mode(conn, user_id, FEATURE) == "off":
        return {**result, "state": "off"}
    lock = _lock(user_id)
    if not lock.acquire(blocking=False):
        return {**result, "skipped": True}
    try:
        sync = _ensure_sync(conn, user_id)
        last = parse_app_instant(sync.get("last_pass_at"))
        if not force and last is not None and now - last < PASS_EVERY:
            return {**result, "skipped": True}
        state = connection_state(connector_row(conn, user_id))
        if state != "connected":
            return {**result, "state": state}
        totals: dict[str, int] = {}
        try:
            with conn:
                _locked_sync(conn, user_id)
                _update_sync(conn, user_id, last_pass_at=now.isoformat(timespec="seconds"))
            # A company's domain is looked out for from the pass after its application is added,
            # not only once the student opens the Automation panel.
            mail_trust.refresh_suggestions(conn, user_id)
            with client_factory() as client:
                gmail = GmailClient(conn, client, user_id)
                budget = _Budget(MAX_GETS_PER_PASS)
                expect = sync["enabled_at"]
                if not sync["history_id"]:
                    expect = _start(conn, gmail, user_id, now)
                elif sync["recovery_state"] == "running":
                    _recover(conn, gmail, user_id, now, expect=expect)
                else:
                    _collect_history(conn, gmail, user_id, sync, now, expect=expect)
                _reclaim(conn, user_id, expect=expect)
                _drain(conn, gmail, user_id, "pending_ids_json", "live", budget, decisions, now, totals, expect=expect)
                _rescan(conn, gmail, user_id, budget, decisions, now, totals, expect=expect)
                _backfill(conn, gmail, user_id, budget, decisions, now, totals, expect=expect)
        except _Reset:
            if getattr(conn, "in_transaction", False):
                conn.rollback()
            # Switched off while it ran: nothing more is written, and its last outcome stands.
            return {**result, "state": "off", "skipped": True, "detail": totals}
        except GmailAuthError:
            return _failed(conn, user_id, {**result, "state": "needs_reconnect", "detail": totals}, "Gmail needs reconnecting")
        except GmailThrottled:
            return _failed(conn, user_id, {**result, "state": "throttled", "detail": totals}, "Gmail asked the app to slow down")
        except _Stop as exc:
            return _failed(conn, user_id, {**result, "state": "unreachable", "detail": totals}, str(exc))
        except (httpx.HTTPError, ValueError) as exc:
            return _failed(conn, user_id, {**result, "state": "unreachable", "detail": totals},
                           f"{type(exc).__name__}: {strip_queries(str(exc))[:200]}")
        except Exception as exc:
            if not is_transient_error(exc):
                raise
            # Locked or deadlocked: every queued message stays queued for the next pass.
            return _failed(conn, user_id, {**result, "state": "database_busy", "detail": totals},
                           "The database was busy, so the check stopped; it tries again next time")
        current = dict(conn.execute("SELECT * FROM application_mail_sync WHERE user_id=?", (user_id,)).fetchone())
        with conn:
            _locked_sync(conn, user_id)
            _update_sync(conn, user_id, last_ok_at=now.isoformat(timespec="seconds"), last_error="")
        totals["pending"] = len(_ids(current["pending_ids_json"]))
        # A message that could not be decided was set aside so it holds nothing up; the health panel says so.
        return {**result, "state": "message_errors" if totals.get("error") else "ok", "detail": totals}
    finally:
        lock.release()


def _failed(conn: sqlite3.Connection, user_id: str, result: dict[str, Any], error: str) -> dict[str, Any]:
    if getattr(conn, "in_transaction", False):
        conn.rollback()
    try:
        with conn:
            _update_sync(conn, user_id, last_error=strip_queries(error)[:300])
    except Exception:  # noqa: BLE001 - the state is still reported
        LOGGER.warning("Could not record why an application mail pass stopped", exc_info=True)
    return result


# --- What the student sees and decides ------------------------------------------------------


def status(conn: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    """The switch's state for the Automation panel: when it started, how reading stands, what the backfill found."""
    row = conn.execute("SELECT * FROM application_mail_sync WHERE user_id=?", (user_id,)).fetchone()
    found = _backfill_proposals(conn, user_id)
    approvable = [action for action in found if _approvable(action)]
    waiting = conn.execute(
        "SELECT COUNT(*) FROM application_mail_messages WHERE user_id=? AND state='awaiting_resume'", (user_id,),
    ).fetchone()[0]
    sync = dict(row) if row else {}
    return {
        "mode": automation.mode(conn, user_id, FEATURE),
        "enabled_at": sync.get("enabled_at"),
        "last_pass_at": sync.get("last_pass_at"),
        "last_ok_at": sync.get("last_ok_at"),
        "last_error": sync.get("last_error") or "",
        "backfill_state": sync.get("backfill_state") or "",
        # Every update the look back found, and the ones Approve all acts on: those that waited only
        # because they arrived before the switch was on. The rest (offers, unverified senders, guessed
        # applications, roles not tracked) are for one by one.
        "backfill_found": len(found),
        "backfill_approvable": len(approvable),
        "pending": len(_ids(sync.get("pending_ids_json"))),
        "awaiting_resume": int(waiting),
        "domain_check": mail_trust.psl_available(),
    }


def application_emails(conn: sqlite3.Connection, user_id: str, application_id: str) -> list[dict[str, Any]]:
    """The emails linked to one application, newest first, with a link that opens each thread in Gmail.

    ``matched_by`` says how sure the link is: job_id or company_title (the
    email named the role), company_single (the company alone, its one open
    application: a guess the page labels), or student (the student picked or
    approved it). An ambiguous match is never linked until the student picks.
    """
    rows = conn.execute(
        """
        SELECT gmail_id, thread_id, subject, sender_domain, received_at, kind, state, matched_by FROM application_mail_messages
        WHERE user_id=? AND application_id=? AND state IN ('done', 'awaiting_resume') ORDER BY received_at DESC LIMIT 100
        """,
        (user_id, application_id),
    ).fetchall()
    return [
        {**dict(row), "gmail_url": f"https://mail.google.com/mail/u/0/#all/{quote(str(row['thread_id'] or row['gmail_id']), safe='')}"}
        for row in rows
    ]


def _prefix(gmail_id: str) -> str:
    return f"gmail:{gmail_id}:"


def _message_actions(conn: sqlite3.Connection, user_id: str, gmail_id: str, status_filter: str | None = None) -> list[dict[str, Any]]:
    """The ledger rows one email made: their idempotency keys all start gmail:<id>:."""
    pattern = _prefix(gmail_id).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    clause = " AND status=?" if status_filter else ""
    params: tuple[Any, ...] = (user_id, FEATURE, pattern) + ((status_filter,) if status_filter else ())
    rows = conn.execute(
        f"SELECT * FROM automation_actions WHERE user_id=? AND feature=? AND idempotency_key LIKE ? ESCAPE '\\'{clause} ORDER BY created_at, id",
        params,
    ).fetchall()
    return [automation._decode(row) for row in rows]


def after_decision(conn: sqlite3.Connection, user_id: str, action: dict[str, Any]) -> None:
    """After the student approved or rejected one of this feature's proposals: keep the email card and Emails list in step."""
    if action.get("feature") != FEATURE:
        return
    gmail_id = str((action.get("evidence") or {}).get("gmail_id") or "")
    if not gmail_id:
        return
    with conn:
        if action.get("status") == "applied" and action.get("subject_kind") == "application":
            # The student approved it for this application: the link is theirs now, not a guess.
            conn.execute(
                "UPDATE application_mail_messages SET application_id=?, matched_by='student' WHERE user_id=? AND gmail_id=?",
                (action["subject_id"], user_id, gmail_id),
            )
    _settle_event(conn, user_id, gmail_id, str((action.get("evidence") or {}).get("event_id") or ""),
                  application_id=action["subject_id"] if action.get("subject_kind") == "application" and action.get("status") == "applied" else None)


def _settle_event(conn: sqlite3.Connection, user_id: str, gmail_id: str, event_id: str, *, application_id: str | None) -> None:
    """Once none of an email's proposals waits, its card is decided: confirmed when anything was applied, else ignored."""
    if not event_id:
        return
    actions = _message_actions(conn, user_id, gmail_id)
    if any(action["status"] == "proposed" for action in actions):
        return
    applied = any(action["status"] == "applied" for action in actions)
    with conn:
        conn.execute(
            """
            UPDATE monitored_events SET status=?, decided_at=?, decided_by='student', application_id=COALESCE(?, application_id)
            WHERE id=? AND user_id=? AND status='pending'
            """,
            ("confirmed" if applied else "ignored", utc_now(), application_id, event_id, user_id),
        )


def after_superseded(conn: sqlite3.Connection, user_id: str, action_id: str) -> None:
    """After an approval found the application changed since: settle the email card if nothing else waits."""
    try:
        action = automation._decode(automation.action_row(conn, action_id, user_id))
    except LookupError:
        return
    after_decision(conn, user_id, action)


def _who(conn: sqlite3.Connection, user_id: str, application_id: str) -> str:
    row = conn.execute(
        "SELECT o.company, o.title FROM applications a JOIN opportunities o ON o.id=a.opportunity_id WHERE a.id=? AND a.user_id=?",
        (application_id, user_id),
    ).fetchone()
    return f"{row['company']} ({row['title']})" if row else ""


def _summary_for(conn: sqlite3.Connection, user_id: str, action: dict[str, Any], application_id: str) -> str:
    """The action's summary, naming ``application_id`` instead of the application it was proposed for."""
    old, new = _who(conn, user_id, action["subject_id"]), _who(conn, user_id, application_id) or "the application you chose"
    summary = str(action.get("summary") or "")
    if old and summary.startswith(f"{old}:"):
        return f"{new}:{summary[len(old) + 1:]}"
    return f"{new}: {summary}" if summary else new


def _correct(conn: sqlite3.Connection, user_id: str, action: dict[str, Any], subject_id: str, before: dict[str, Any]) -> dict[str, Any]:
    """automation.register_correction for this feature: plan()'s rules, held against the application the student chose.

    Stages only move forward, nothing leaves a closed stage except to an
    offer (always the student's call), and a rejection never overwrites an
    offer or a withdrawal. A confirmation keeps a stage past Applied and sets
    when they applied. A task or a deadline is never added to a closed
    application, since Urgent never shows it there. An application the app
    archived after no reply (internal_automation.automatic_archive) counts
    as at the stage it was archived from for a stage change, so the pick can
    reopen it; a task or deadline waits until it is reopened (decide_event
    reopens it first when the same email's stage change is approved for it,
    as plan() pairs them). The summary names the chosen application.
    """
    row = conn.execute("SELECT stage FROM applications WHERE id=? AND user_id=?", (subject_id, user_id)).fetchone()
    stage = str(row["stage"]) if row else ""
    # As in plan(): an application the app archived after no reply counts as at the stage it was archived
    # from, so the student's pick can reopen it. Checked inside approve()'s transaction. A student's archive stays.
    archive = internal_automation.automatic_archive(conn, subject_id) if stage == "archived" else None
    who = _who(conn, user_id, subject_id) or "That application"
    if archive and action["action_type"] != "application.stage":
        raise automation.CorrectionRefused(
            f"{who} was archived by the app after no reply, so this email's task or deadline is not added to it while it is "
            "archived. Move it back to Applied first if the email is about it"
        )
    if archive:
        stage = str(archive["from_stage"])
    old = _who(conn, user_id, action["subject_id"])
    after = {key: value for key, value in action["after"].items() if key != "_result"}
    if action["action_type"] == "application.stage":
        target = str(after.get("stage") or "")
        confirmation = "applied_at" in after
        if target == "offer" or target == stage:
            pass  # an offer is the student's call; the same stage changes nothing (approve records it so)
        elif stage in CLOSED_STAGES:
            raise automation.CorrectionRefused(f"{who} is {stage}, so this email cannot move it to {target}")
        elif target == "rejected":
            if stage == "offer":
                raise automation.CorrectionRefused(f"{who} has an offer, and a rejection never overwrites an offer")
        elif confirmation:
            after["stage"] = "applied" if stage == "applying" else stage
        elif STAGE_RANK.get(stage, -1) >= STAGE_RANK.get(target, len(STAGE_RANK)):
            raise automation.CorrectionRefused(f"{who} is already at {stage}, and stages only move forward")
    elif stage in CLOSED_STAGES:
        raise automation.CorrectionRefused(f"{who} is {stage}, so this email's task or deadline is not added to it")
    return {
        "after": after, "summary": _summary_for(conn, user_id, action, subject_id),
        "note": f"You chose a different application than the one proposed ({old})" if old else "",
    }


def _breaker_group(row: dict[str, Any]) -> str | None:
    """automation.register_breaker_group for this feature: one email's changes count once; a capture never counts."""
    if row.get("action_type") == "application.capture_proposal":
        return None
    key = str(row.get("idempotency_key") or "")
    return f"gmail:{key.split(':')[1]}" if key.startswith("gmail:") and key.count(":") >= 2 else str(row.get("id") or key)


automation.register_correction(FEATURE, _correct)
automation.register_breaker_group(FEATURE, _breaker_group, "email")


def _expire(conn: sqlite3.Connection, user_id: str, action: dict[str, Any], note: str, timestamp: str) -> None:
    """Set a proposal aside without a verdict (the breaker never counts it), keeping only what the ledger may."""
    kept = automation.ledger_after(str(action["action_type"]), {key: value for key, value in action["after"].items()})
    with conn:
        conn.execute(
            """
            UPDATE automation_actions SET status='expired', note=?, after_json=?, decided_at=?, decided_by='student'
            WHERE id=? AND user_id=? AND status='proposed'
            """,
            (note, automation._dumps(kept), timestamp, action["id"], user_id),
        )
        automation.release_held(conn, str(action["id"]), user_id)


MOVABLE = ("application.task", "application.deadline")


def _move_applied(conn: sqlite3.Connection, user_id: str, gmail_id: str, application_id: str) -> None:
    """The student said the email is about ``application_id``: what it added on its own elsewhere moves there.

    A task or deadline the app added to another application (the company
    alone matched) is made again for the chosen one, approved by the student,
    and taken back from the other while it is unchanged. One the student has
    edited or finished since stays where it is, and so does everything while
    the chosen one is closed (archived by the app included): a task or
    deadline is never added to a closed application (_correct).
    """
    row = conn.execute("SELECT stage FROM applications WHERE id=? AND user_id=?", (application_id, user_id)).fetchone()
    if row is None or row["stage"] in CLOSED_STAGES:
        return
    for action in _message_actions(conn, user_id, gmail_id, "applied"):
        if (action["subject_kind"] != "application" or action["subject_id"] == application_id
                or action.get("decided_by") != "system" or action["action_type"] not in MOVABLE):
            continue
        after = {key: value for key, value in action["after"].items() if key != "_result"}
        if action["action_type"] == "application.task":
            # The ledger kept only the link's host; the task itself has the link.
            task_id = (action["after"].get("_result") or {}).get("task_id")
            row = conn.execute("SELECT link FROM application_tasks WHERE id=? AND user_id=?", (task_id, user_id)).fetchone()
            task = {key: value for key, value in (after.get("task") or {}).items() if key != "link_host"}
            after = {**after, "task": {**task, "link": str(row["link"] or "") if row else ""}}
        moved = automation.perform(
            conn, user_id=user_id, feature=FEATURE, action_type=action["action_type"], subject_kind="application",
            subject_id=application_id, after=after, evidence={**action["evidence"], "moved_from": action["subject_id"]},
            summary=_summary_for(conn, user_id, action, application_id), basis=action["basis"], confidence=action["confidence"],
            idempotency_key=f"{_prefix(gmail_id)}{application_id}:{action['action_type']}", auto=False,
            policy_version=action.get("policy_version") or POLICY_VERSION,
        )
        if moved is None or moved["status"] != "proposed":
            continue  # off or paused, or already made for that application
        try:
            automation.approve(conn, moved["id"], user_id)
        except (automation.Superseded, ValueError, LookupError):
            continue
        try:
            automation.undo(conn, action["id"], user_id)
        except (automation.Superseded, ValueError, LookupError):
            continue  # edited or finished since: the student's own now


def _reopens(conn: sqlite3.Connection, user_id: str, proposals: list[dict[str, Any]], application_id: str) -> bool:
    """Whether confirming these proposals for ``application_id`` approves a stage change that reopens the app's archive of it."""
    if not internal_automation.automation_archived(conn, application_id):
        return False
    return any(
        action["subject_kind"] == "application" and action["action_type"] == "application.stage"
        and str(action["after"].get("stage") or "") not in CLOSED_STAGES
        and automation.correction_refusal(conn, action["id"], user_id, application_id) is None
        for action in proposals
    )


def decide_event(
    conn: sqlite3.Connection, event: dict[str, Any], decision: str, application_id: str | None, *, user_id: str,
) -> dict[str, Any] | None:
    """The email card's Confirm or Ignore for an email this feature read. None when the plain path decides it.

    The plain path (connections.decide_event_directly, which sets the stage
    the email points to) is only for an email this feature changed nothing
    about: none matched, or its only proposal was to capture an untracked role.

    Confirm approves each of the email's proposals for the application the
    student picked (a correction when it is another one, held to the same
    rules as plan(): if any would be refused, nothing is decided and
    CorrectionRefused says why). Its stage change is approved first: when
    it reopens an application the app archived after no reply, the email's
    task and deadline then go to the reopened application, as plan() pairs
    them; without one, a task or deadline is refused there. What the email
    already added on its own to another application moves to the picked one
    (_move_applied). A proposal
    to capture an untracked role is set aside, since the student picked a
    tracked one. When a proposal finds its application changed since, it is
    left as it is; if nothing of the email was applied, the card is settled
    and Superseded says why. Ignore sets every proposal aside as expired:
    ignoring an email is not the feature getting something wrong, so it never
    trips the breaker.
    """
    from .connections import decide_event_directly, monitored_event

    gmail_id = str((event.get("payload") or {}).get("gmail_id") or "")
    actions = _message_actions(conn, user_id, gmail_id) if gmail_id else []
    tracked = [action for action in actions if action["subject_kind"] == "application"]
    proposals = [action for action in actions if action["status"] == "proposed"]
    if not tracked and not proposals:
        return None
    reopening = bool(decision == "confirm" and application_id and _reopens(conn, user_id, proposals, application_id))
    if decision == "confirm" and application_id:
        for action in proposals:
            if action["subject_kind"] != "application":
                continue
            if reopening and action["action_type"] in MOVABLE:
                continue  # checked when approved, once the email's stage change has reopened the application
            refusal = automation.correction_refusal(conn, action["id"], user_id, application_id)
            if refusal:
                raise automation.CorrectionRefused(refusal)
    timestamp = utc_now()
    superseded: list[str] = []
    # The stage change first (a stable sort keeps the rest in order): it may reopen what a task or deadline needs open.
    for action in sorted(proposals, key=lambda item: item["action_type"] != "application.stage"):
        if decision == "confirm" and action["subject_kind"] == "application":
            try:
                automation.approve(conn, action["id"], user_id, subject_id=application_id)
            except automation.Superseded as exc:
                superseded.append(str(exc))
            except automation.CorrectionRefused as exc:
                if not reopening:
                    raise
                # The reopen did not happen (the application changed meanwhile), so this waits.
                superseded.append(str(exc))
            except ValueError:
                continue  # decided somewhere else meanwhile
        else:
            _expire(conn, user_id, action, "You ignored this email" if decision == "ignore" else "You picked a tracked application for this email",
                    timestamp)
    if decision == "confirm" and application_id:
        _move_applied(conn, user_id, gmail_id, application_id)
        with conn:
            conn.execute(
                "UPDATE application_mail_messages SET application_id=?, matched_by='student' WHERE user_id=? AND gmail_id=?",
                (application_id, user_id, gmail_id),
            )
    if decision == "confirm" and not tracked:
        # The email only proposed capturing an untracked role, and the student picked a tracked
        # application instead: it gets what the email says, as on any email card.
        return decide_event_directly(conn, event, decision, application_id, user_id=user_id)
    applied = any(action["status"] == "applied" for action in _message_actions(conn, user_id, gmail_id))
    with conn:
        conn.execute(
            """
            UPDATE monitored_events SET status=?, application_id=?, decided_at=?, decided_by='student'
            WHERE id=? AND user_id=? AND status='pending'
            """,
            ("confirmed" if decision == "confirm" and applied else "ignored",
             application_id if decision == "confirm" else event.get("application_id"), timestamp, event["id"], user_id),
        )
    if decision == "confirm" and superseded and not applied:
        raise automation.Superseded(superseded[0])
    return monitored_event(conn, event["id"], user_id=user_id)


def _backfill_proposals(conn: sqlite3.Connection, user_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT * FROM automation_actions
        WHERE user_id=? AND feature=? AND status='proposed' AND basis LIKE ?
        ORDER BY created_at, id
        """,
        (user_id, FEATURE, f"%{BACKFILL_BASIS}%"),
    ).fetchall()
    return [automation._decode(row) for row in rows]


def _approvable(action: dict[str, Any]) -> bool:
    """Whether Approve all may approve it: about a tracked application, and waiting only because it came before the switch."""
    reasons = (action.get("evidence") or {}).get("why_proposal")
    return action["subject_kind"] == "application" and reasons == [BEFORE_ENABLED]


def approve_backfill(conn: sqlite3.Connection, user_id: str) -> dict[str, int]:
    """Approve all: the backfill proposals that would have acted on their own had the switch been on.

    Everything else the look back found (an offer, a sender Gmail did not
    vouch for, a guessed application, a newsletter, a date gap, a role not
    tracked) waits for one by one; ``left`` counts those.
    """
    found = _backfill_proposals(conn, user_id)
    approved = superseded = 0
    for action in found:
        if not _approvable(action):
            continue
        try:
            result = automation.approve(conn, action["id"], user_id)
        except automation.Superseded:
            superseded += 1
            after_superseded(conn, user_id, action["id"])
            continue
        except (ValueError, LookupError):
            continue
        approved += 1
        after_decision(conn, user_id, result)
    left = sum(1 for action in found if not _approvable(action))
    return {"approved": approved, "superseded": superseded, "left": left}


# --- Retention ---------------------------------------------------------------------------


def evidence_days() -> int:
    try:
        days = int(os.environ.get("PIPELINE_MAIL_EVIDENCE_DAYS", str(DEFAULT_EVIDENCE_DAYS)).strip())
    except ValueError:
        return DEFAULT_EVIDENCE_DAYS
    return days if days > 0 else DEFAULT_EVIDENCE_DAYS


def purge_excerpts(conn: sqlite3.Connection, *, now: datetime | None = None) -> dict[str, int]:
    """Drop email excerpts older than PIPELINE_MAIL_EVIDENCE_DAYS from the ledger and the email cards; the hashes stay."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=evidence_days())).isoformat(timespec="microseconds")
    actions = events = 0
    with conn:
        # A proposal still waiting after all this time no longer holds a task's link either.
        conn.execute(
            "DELETE FROM automation_held WHERE action_id IN (SELECT id FROM automation_actions WHERE feature=? AND created_at<?)",
            (FEATURE, cutoff),
        )
        for row in conn.execute(
            "SELECT id, evidence_json FROM automation_actions WHERE feature=? AND created_at<?", (FEATURE, cutoff),
        ).fetchall():
            try:
                evidence = json.loads(row["evidence_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(evidence, dict) or not evidence.get("excerpt"):
                continue
            evidence["excerpt"] = ""
            evidence["excerpt_removed_at"] = now.isoformat(timespec="seconds")
            conn.execute("UPDATE automation_actions SET evidence_json=? WHERE id=?", (json.dumps(evidence, sort_keys=True, default=str), row["id"]))
            actions += 1
        for row in conn.execute(
            "SELECT id, payload_json FROM monitored_events WHERE created_at<? AND external_id LIKE ?", (cutoff, "gmail:%"),
        ).fetchall():
            try:
                payload = json.loads(row["payload_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(payload, dict) or payload.get("source") != "application_mail" or not payload.get("body_preview"):
                continue
            payload["body_preview"] = ""
            conn.execute("UPDATE monitored_events SET payload_json=? WHERE id=?", (json.dumps(payload, sort_keys=True), row["id"]))
            events += 1
    return {"mail_excerpts_removed": actions, "mail_previews_removed": events}


# --- Ledger handlers this feature adds ----------------------------------------------------------


class ApplicationDeadline:
    """application.deadline: a deadline an email states, as an email_deadlines row (shown in Urgent).

    Each is new (the idempotency key stops a second copy, and so does the
    table's UNIQUE). Undo deletes it while it is unchanged.
    """

    fields = ("deadline",)

    def read(self, conn: sqlite3.Connection, user_id: str, subject_id: str) -> dict[str, Any]:
        lock = automation.for_update_clause(conn)
        if conn.execute(f"SELECT 1 FROM applications WHERE id=? AND user_id=?{lock}", (subject_id, user_id)).fetchone() is None:
            from .actions import ApplicationNotFoundError

            raise ApplicationNotFoundError(subject_id)
        return {"deadline": None}

    def effective(self, before: dict[str, Any], after: dict[str, Any], timestamp: str) -> dict[str, Any]:
        spec = after.get("deadline")
        if not isinstance(spec, dict) or not spec.get("gmail_id"):
            raise ValueError("A deadline needs the email it came from")
        date.fromisoformat(str(spec.get("deadline_on")))
        return {"deadline": spec}

    def apply(self, conn: sqlite3.Connection, user_id: str, subject_id: str, after: dict[str, Any], *, source: str, timestamp: str) -> dict[str, Any]:
        spec = after["deadline"]
        deadline_id = f"email-deadline-{uuid4().hex}"
        conn.execute(
            """
            INSERT INTO email_deadlines(id, user_id, application_id, gmail_id, deadline_on, quote, sender_domain, received_at, created_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(user_id, gmail_id, application_id) DO NOTHING
            """,
            (deadline_id, user_id, subject_id, spec["gmail_id"], spec["deadline_on"], str(spec.get("quote") or "")[:QUOTE_LIMIT],
             str(spec.get("sender_domain") or "")[:200], str(spec.get("received_at") or ""), timestamp),
        )
        row = conn.execute(
            "SELECT id, deadline_on FROM email_deadlines WHERE user_id=? AND gmail_id=? AND application_id=?",
            (user_id, spec["gmail_id"], subject_id),
        ).fetchone()
        log_application_event(
            conn, subject_id, "deadline_added",
            {"deadline_id": row["id"], "deadline_on": row["deadline_on"], "source": source}, timestamp,
        )
        return {"deadline_id": row["id"], "deadline_on": row["deadline_on"]}

    def undo(self, conn: sqlite3.Connection, user_id: str, subject_id: str, before: dict[str, Any], after: dict[str, Any],
             *, source: str, timestamp: str) -> None:
        created = after.get("_result") or {}
        deleted = conn.execute(
            "DELETE FROM email_deadlines WHERE id=? AND user_id=? AND application_id=? AND deadline_on=?",
            (created.get("deadline_id"), user_id, subject_id, created.get("deadline_on")),
        ).rowcount
        if not deleted:
            raise automation.Superseded("The deadline was already removed")
        log_application_event(
            conn, subject_id, "deadline_removed", {"deadline_id": created.get("deadline_id"), "source": source}, timestamp,
        )


class CaptureProposal:
    """application.capture_proposal: an email about a role that is not tracked. Approving opens a capture draft.

    The draft goes through the ordinary capture confirmation (captures.py):
    nothing enters the tracker until the student checks the fields and
    confirms. Undo is not offered: the draft is the student's to confirm or
    leave, and deleting it would take away something they may have edited.
    """

    fields = ("capture",)
    undoable = False

    def read(self, conn: sqlite3.Connection, user_id: str, subject_id: str) -> dict[str, Any]:
        return {"capture": None}

    def effective(self, before: dict[str, Any], after: dict[str, Any], timestamp: str) -> dict[str, Any]:
        spec = after.get("capture")
        if not isinstance(spec, dict) or not str(spec.get("company") or "").strip():
            raise ValueError("A capture needs a company")
        return {"capture": spec}

    def apply(self, conn: sqlite3.Connection, user_id: str, subject_id: str, after: dict[str, Any], *, source: str, timestamp: str) -> dict[str, Any]:
        spec = after["capture"]
        capture_id = f"capture-{uuid4().hex}"
        url = str(spec.get("url") or "")
        received = str(spec.get("received_at") or "")[:10]
        parsed = {
            "company": str(spec.get("company") or "")[:200], "title": str(spec.get("title") or "")[:500], "location": "",
            "url": url, "description": "",
            "extraction_note": f"From an email received {received}. Check every field, and add the job link if it is missing, before you confirm.",
        }
        conn.execute(
            """
            INSERT INTO opportunity_captures(id, user_id, source_type, source_url, original_name, media_type, storage_path,
                extracted_text, parsed_json, status, created_at)
            VALUES(?, ?, 'url', ?, '', 'message/rfc822', '', '', ?, 'draft', ?)
            """,
            (capture_id, user_id, url, json.dumps(parsed), timestamp),
        )
        return {"capture_id": capture_id}

    def undo(self, conn: sqlite3.Connection, user_id: str, subject_id: str, before: dict[str, Any], after: dict[str, Any],
             *, source: str, timestamp: str) -> None:
        raise ValueError("Opening a capture draft can't be undone")


automation.register_handler("application.deadline", ApplicationDeadline())
automation.register_handler("application.capture_proposal", CaptureProposal())
