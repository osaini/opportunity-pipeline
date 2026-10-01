"""The rules that read one job-system email: what it says, what it means, which application it is about, and what dates it states.

Everything here works on one message at a time and never talks to Gmail or writes the sync cursor: parsing a Gmail
``messages.get`` answer, the keyword and sender rules that classify it, naming the company and role, matching it to the
student's applications, and reading a deadline out of the text. ``application_inbox`` is the engine that fetches mail,
keeps the cursor and the queues, and decides what to do with what these rules find; its module docstring describes the
whole feature, including what each tier of match and each classification may do on its own.

``tests/test_application_mail_eval.py`` runs ``classify_rules`` over the labelled fixture and so does not need the
engine. The one thing here that touches the database is the matcher's read of the student's applications.
"""

from __future__ import annotations

import email
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

from pipeline_core.identity import identity_tokens, normalized

from . import internal_automation, mail_trust
from .connections import classify_monitored_message
from .extension_apply import split_canonical_url
from .inbox_classifiers import classify_email
from .mail_message import (
    URL,
    clean_url,
    decode_base64url,
    has_list_headers,
    host_of,
    html_text_spaced,
    received_or_epoch,
)
from .typesafe_decisions import DecisionClient


EXCERPT_LIMIT = 500
QUOTE_LIMIT = 160


# How far ahead a date with no year may be read as this year's (or next year's) date.
YEARLESS_WINDOW_DAYS = 180
TEXT_LIMIT = 20_000


CLOSED_STAGES = {"rejected", "withdrawn", "archived"}


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


def job_ids(url: str) -> set[str]:
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
            app["job_ids"] |= job_ids(str(url or ""))
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
        link_ids |= job_ids(link)
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
