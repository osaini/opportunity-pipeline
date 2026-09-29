"""Replies to outreach, read from Gmail so the student never pastes them.

For every company the student has written to, the app looks in Gmail for mail
that may answer them: from the addresses written to, from anyone at the
company's own domains, in the Gmail thread of any email the student sent them
(whoever wrote it), and mail naming the company or the email's subject from
anywhere else. Spam is looked in too; Trash is not. Each message is read once
(outreach_inbox_messages) and sorted by how strong the evidence is:

- A reply: someone at the company answered in the thread of the student's
  email, an address the student wrote to wrote back, or a person at the
  company's own website domain wrote to the student, verified by Gmail, with a
  name that matches the address. It is logged exactly as a pasted reply would
  be, so call prep and the history see it. A company still waiting on a reply
  (or given up on) moves to Replied: that someone wrote back is a fact. What
  the reply means (declined, a call, an offer) stays a suggestion the student
  applies or dismisses, from Jev when it is on and sure, else the rules.
- A possible reply: anything weaker a person may have written (a careers@
  inbox, a colleague Gmail could not verify, someone outside the company in the
  thread, mail in Spam or sent through a mailing tool, mail that could be from
  two companies). It is shown on the company's card, in Urgent and as a notice,
  and until the student says whether it is a reply, every automatic step that
  assumes silence (a follow-up, closing as No response, a resend) waits.
- An automatic reply (out of office) is noted and changes nothing.
- Set aside, with the reason kept: the student's own mail, delivery notices
  (left to outreach_delivery), mail from before the first email, mailing-list
  mail from a shared or automated sender, automated senders at the company.

Every row says why (reason) and under which rules (RULES). A row set aside by
older rules is read again under the current ones, whoever wrote it.

``InboxWatcher`` runs both checks on a background thread, so a reply or a
bounce is caught even when the page is closed. Each of its steps stands alone:
one that fails is recorded in automation_health and the next still runs.
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
import unicodedata
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote, urlsplit

import httpx

from pipeline import identity_tokens, normalized

from . import automation
from .inbox_classifiers import read_reply
from .mail_trust import FREEMAIL, READ_CATEGORIES, authenticate, host_of, listed, not_an_employer, registrable_domain, sender_lists
from .outreach import (
    BOUNCED,
    _log,
    get_target,
    reply_reason,
    suggest_reply_status,
    update_target,
    website_domain,
)
from .outreach_contacts import GENERIC_LOCAL_PARTS
from .outreach_delivery import _headers_say_failure, check_deliveries
from .outreach_gmail import (
    PROVIDER,
    SENT_EVENT,
    ClientFactory,
    GmailAuthError,
    GmailThrottled,
    _connector,
    _Gmail,
    gmail_notices,
    persist_gmail_health,
)
from .outreach_drafting import sender_account
from .outreach_forms import (
    ACKNOWLEDGEMENT,
    ACKNOWLEDGEMENT_WINDOW_MINUTES,
    ALWAYS_AUTOMATIC,
    NO_REPLY_SENDER,
    SUBMITTED_EVENT as FORM_SUBMITTED,
    UNCONFIRMED_EVENT as FORM_UNCONFIRMED,
    is_acknowledgement,
)
from .schema import connect_product, utc_now
from .typesafe_decisions import DecisionClient
from .user_time import user_timezone

LOGGER = logging.getLogger(__name__)

# The version of the rules below. A row judged under an older one (0: before
# replies from other addresses were read) is judged again if it was set aside.
RULES = 2
MIGRATION = "0041_outreach_reply_rules.sql"
# Waiting on them, given up on, or back to a draft after a bounce: a reply moves these to Replied.
REOPENED_BY_REPLY = {"sent", "followed_up", "no_response", "drafted"}
# Replies can come months later; a company is looked for while its latest email is this recent.
REPLY_WINDOW = timedelta(days=180)
CAPTURE_EVERY = timedelta(seconds=60)
# Emails fetched in full per background check; the rest wait for the next one
# (every message read is remembered). The look before an automatic send reads all of its company's.
READ_BUDGET = 50
# Old set-aside rows read again per check.
REJUDGE_BUDGET = 25
# Sent threads read directly per check when the recent-mail listing could not cover them.
THREAD_FALLBACK = 20
# A search's from:(...) list is kept under this length; a longer one is split.
QUERY_LENGTH = 1_500
_DAEMONS = {"mailer-daemon", "mailerdaemon", "mail-daemon", "postmaster"}
# "Re:", "RE[2]:", "[Acme] Re:", and replies in other languages (AW, SV, Antw, VS, Rif, Odp, Ynt).
# Forwards are not answers: a forward is fresh content.
_ANSWER_SUBJECT = re.compile(r"^\s*(\[[^\]]{1,60}\]\s*)*(re|aw|sv|antw|vs|rif|odp|ynt)\s*(\[\d+\]|\(\d+\))?\s*[:：]", re.IGNORECASE)
# Senders that are a machine rather than a person or a shared inbox.
_MACHINE_SENDER = re.compile(
    r"^(no-?reply|do-?not-?reply|donotreply|notifications?|notify|alerts?|mailer|bounces?|news(letters?)?|"
    r"marketing|updates?|digest|calendar(-notification)?|invitations?|billing|invoices?|receipts?|"
    r"system|robot|bot|automated|auto|wordpress|forms?)([+._-]|$)|(no-?reply|noreply|donotreply)",
    re.IGNORECASE,
)
_AUTO_SUBJECT = re.compile(
    r"^\s*(automatic reply|auto(matic)?[- ]?reply|auto:|out of (the )?office|ooo\b|away from|automatische antwort|"
    r"abwesenheit|r[ée]ponse automatique|absence|respuesta autom[áa]tica|fuera de la oficina|risposta automatica|"
    r"resposta autom[áa]tica|automatisch antwoord|automatiskt svar|autosvar)",
    re.IGNORECASE,
)
# The first part of a website host that names a section, not the company (careers.acme.com is acme.com).
_SECTION_LABELS = {"www", "careers", "jobs", "about", "en", "home", "go", "get", "app", "blog", "team", "info", "corp", "company"}
# Hosts where a website is one tenant of many (a LinkedIn page, a Google Site,
# a store builder, an applicant system): anyone at the host is not the company.
# A company's own big domain (microsoft.com) is never here.
PLATFORM_HOSTS = {
    "sites.google.com", "linkedin.com", "facebook.com", "instagram.com", "x.com", "twitter.com", "youtube.com",
    "tiktok.com", "medium.com", "substack.com", "github.io", "gitlab.io", "notion.site", "notion.so", "wixsite.com",
    "squarespace.com", "wordpress.com", "weebly.com", "godaddysites.com", "myshopify.com", "carrd.co", "linktr.ee",
    "crunchbase.com", "angel.co", "wellfound.com", "ycombinator.com", "producthunt.com", "webflow.io", "framer.website",
    "framer.ai", "canva.site", "hs-sites.com", "hubspotpagebuilder.com", "about.me", "bit.ly", "atlassian.net",
    "teamtailor.com", "personio.de", "personio.com", "rippling.com", "dover.com", "gem.com", "pinpointhq.com",
    "jazz.co", "applytojob.com", "eightfold.ai", "oraclecloud.com", "csod.com", "zohorecruit.com", "workable.com",
    "recruitee.com", "breezy.hr", "bamboohr.com", "jobvite.com", "ashbyhq.com", "lever.co", "greenhouse.io",
    "github.com", "gitlab.com", "bitbucket.org", "huggingface.co", "kaggle.com", "discord.com", "discord.gg",
    "slack.com", "meetup.com", "eventbrite.com", "devpost.com", "behance.net", "dribbble.com", "angellist.com",
}
# Registrable domains that stand for a whole university or government: never "anyone at the domain".
_INSTITUTION = re.compile(r"(^|\.)(edu|gov|mil)$|(^|\.)(ac|edu|gov|mil|govt|gob|gouv)\.[a-z]{2}$")
_URL = re.compile(r"""https?://[^\s<>"'`]+""", re.IGNORECASE)
# Where the quoted email starts in a reply: "On Fri, ... wrote:", "-----Original Message-----",
# Outlook's "From: ... Sent: ..." header block, or a "> " line.
_ON_WROTE = re.compile(r"^\s*On .{0,300}wrote:\s*$", re.IGNORECASE | re.DOTALL)
_ORIGINAL = re.compile(r"^\s*-{2,}\s*(original message|forwarded message)\s*-{2,}\s*$", re.IGNORECASE)

_LAST_CAPTURE: dict[str, datetime] = {}
_CAPTURE_LOCK = threading.Lock()
# Where a search that ran out of pages stopped, to go on from there next time; and
# when the recent-mail listing last read everything, per student. In memory: a
# restart means one wider look.
_RESUME: dict[tuple[str, str], str] = {}
_LAST_SWEEP: dict[str, datetime] = {}
_MEMORY_LOCK = threading.Lock()

OnReply = Callable[[sqlite3.Connection, str, str], None]

REPLY, AUTOMATIC, POSSIBLE, IGNORED, DISMISSED = "reply", "automatic", "possible", "ignored", "dismissed"
# What older rules decided that is judged again under these: they set aside, or took as automatic, mail a
# person may have written (a recruiter's email through a sales tool was "automatic" to them).
REJUDGED = (IGNORED, AUTOMATIC)
# What the job-mail reader leaves to outreach (application_inbox._outreach_owns, owned_sql): a
# reply or automatic reply from the thread or an address written to, and a
# possible reply until the student decides it (unless it looks like job mail).
# A reply matched only by the company's domain may be job mail as well, and
# what outreach set aside or the student dismissed is the job-mail reader's too.
def owned_sql(table: str = "") -> str:
    """The SQL condition for a row outreach holds, on ``table``'s columns (an alias such as "o", or none)."""
    column = f"{table}." if table else ""
    return (
        f"(({column}kind='possible' AND {column}reason<>'job_mail') OR "
        f"({column}kind='reply' AND {column}via NOT IN ('domain', 'name', 'reply_to')) OR "
        f"({column}kind='automatic' AND {column}rules >= {RULES} AND {column}via NOT IN ('domain', 'name', 'reply_to')))"
    )


# --- Reading a message ----------------------------------------------------------


def _html_text(markup: str) -> str:
    markup = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h\d)>", "\n", markup)
    markup = re.sub(r"(?is)<blockquote\b.*", "", markup)
    return html.unescape(re.sub(r"<[^>]+>", "", markup))


def _quote_start(lines: list[str]) -> tuple[int, str] | None:
    """Where the quoted email starts, and how: "quote" (a "> " line), "wrote" or "wrote2" (an "On ... wrote:"
    line, or one split over two lines), or "header" (Outlook's From:/Sent: block, or an Original Message line)."""
    for index, line in enumerate(lines):
        pair = f"{line} {lines[index + 1]}" if index + 1 < len(lines) else line
        header_block = line.startswith("From:") and any(
            following.startswith(("Sent:", "Date:")) for following in lines[index + 1:index + 3]
        )
        if line.lstrip().startswith(">"):
            return index, "quote"
        if _ORIGINAL.match(line) or header_block:
            return index, "header"
        if _ON_WROTE.match(line):
            return index, "wrote"
        if line.lstrip().startswith("On ") and _ON_WROTE.match(pair):
            return index, "wrote2"
    return None


def strip_quoted(text: str) -> str:
    """The reply above the email it quotes."""
    lines = text.replace("\r\n", "\n").split("\n")
    found = _quote_start(lines)
    if found is not None:
        lines = lines[:found[0]]
    return "\n".join(lines).strip()


def written_between_quotes(text: str, sent: Iterable[str] = ()) -> str:
    """What the sender wrote after the email they quote begins: between its quoted lines, or below them.

    strip_quoted keeps only what is above the quote, so an answer typed inline
    ("> Would you have time for a call?" then "Sure, Thursday?") is lost there.
    A "> " quote marks its lines. An Outlook-style quote (a From:/Sent: block,
    or an Original Message line) does not, so below it every line that is not
    a header field and not the student's own words (``sent``: the emails they
    sent, which it quotes) counts as written by the sender. With nothing in
    ``sent``, every line below such a header counts: nothing is ruled out.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    found = _quote_start(lines)
    if found is None:
        return ""
    index, how = found
    if how == "header":
        return _written_below_header(lines[index:], sent)
    rest = lines[index + {"quote": 0, "wrote": 1, "wrote2": 2}[how]:]
    return "\n".join(line for line in rest if line.strip() and not line.lstrip().startswith(">")).strip()


# The fields of an Outlook header block ("From:", "Sent:", "To:", "Subject:"), with or without bold marks.
_HEADER_FIELD = re.compile(r"^\s*\**\s*(from|sent|date|to|cc|bcc|subject|importance|reply-to)\s*:", re.IGNORECASE)
_QUOTE_MARKS = re.compile(r"^[\s>]+")
_FLAT = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u00a0": " ", "\u200b": None, "\ufeff": None})


def _flat(text: str) -> str:
    """Text as compared with what the student sent: curly quotes straightened, spaces collapsed, case folded."""
    return " ".join(str(text).translate(_FLAT).split()).casefold()


def _header_starts(lines: list[str], index: int) -> bool:
    line = lines[index]
    return bool(_ORIGINAL.match(line)) or (
        _HEADER_FIELD.match(line) is not None and line.lstrip(" *").casefold().startswith("from")
        and any(_HEADER_FIELD.match(following) and following.lstrip(" *").casefold().startswith(("sent", "date"))
                for following in [other for other in lines[index + 1:index + 6] if other.strip()][:2])
    )


def _written_below_header(lines: list[str], sent: Iterable[str]) -> str:
    """The lines below an Outlook quote's header that are neither header fields nor the student's own words.

    Words are the student's when they are a whole line of what they sent, or
    four or more words that run on in it. A paragraph is tried whole first,
    since a long line can come back re-wrapped into short pieces; a paragraph
    that is not theirs as a whole is tried line by line. A header block quoted
    further down (the thread's older email) is skipped the same way. What is
    left, the sender wrote.
    """
    bodies = [str(body) for body in sent if str(body or "").strip()]
    whole_lines = {_flat(line) for body in bodies for line in body.replace("\r\n", "\n").split("\n")} - {""}
    running = " ".join(_flat(body) for body in bodies)

    def theirs(words: str) -> bool:  # the student's own
        return not words or words in whole_lines or (
            len(words.split()) >= 4 and re.search(rf"(?<!\w){re.escape(words)}(?!\w)", running) is not None
        )

    paragraphs: list[list[str]] = [[]]
    in_header = False
    for index, line in enumerate(lines):
        if not line.strip():
            paragraphs.append([])
            continue
        if index == 0 or _header_starts(lines, index):
            in_header = True
            paragraphs.append([])
            continue
        if in_header and _HEADER_FIELD.match(line):
            continue
        in_header = False
        paragraphs[-1].append(line.strip())
    written: list[str] = []
    for paragraph in paragraphs:
        if theirs(_flat(" ".join(_QUOTE_MARKS.sub("", line) for line in paragraph))):
            continue
        written.extend(line for line in paragraph if not theirs(_flat(_QUOTE_MARKS.sub("", line))))
    return "\n".join(written)


def _html_full_text(markup: str) -> str:
    """HTML as text with each quoted (<blockquote>) line marked "> ", as a plain-text reply marks it."""
    markup = re.sub(r"(?is)<(script|style)\b.*?</\1>", "", markup)
    markup = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h\d)>", "\n", markup)
    markup = re.sub(r"(?i)<blockquote\b[^>]*>", "\n\x00quote-open\x00\n", markup)
    markup = re.sub(r"(?i)</blockquote\s*>", "\n\x00quote-close\x00\n", markup)
    text = html.unescape(re.sub(r"<[^>]+>", "", markup))
    depth, lines = 0, []
    for line in text.split("\n"):
        if line == "\x00quote-open\x00":
            depth += 1
        elif line == "\x00quote-close\x00":
            depth = max(0, depth - 1)
        else:
            lines.append(f"> {line}" if depth and line.strip() else line)
    return "\n".join(lines)


FULL_TEXT_LIMIT = 20_000


def _body_text(message: EmailMessage, *, whole: bool) -> str:
    body = message.get_body(preferencelist=("plain", "html"))
    if body is None:
        return ""
    try:
        text = str(body.get_content())
    except (LookupError, ValueError):
        return ""
    if body.get_content_type() == "text/html":
        return _html_full_text(text) if whole else _html_text(text)
    return text


def reply_text(message: EmailMessage) -> str:
    return strip_quoted(_body_text(message, whole=False))[:20_000]


def full_reply_text(message: EmailMessage) -> str:
    """The whole message as it arrived, quoted lines marked "> ", so an answer typed inline is kept."""
    return _body_text(message, whole=True).replace("\r\n", "\n").strip()


def _header(message: EmailMessage, name: str, raw: str) -> Any:
    """One header as the message's policy parses it, or its raw text when the parser fails on it (a stray ":;")."""
    try:
        return message.policy.header_fetch_parse(name, raw)
    except Exception:  # noqa: BLE001 - the header parser raises IndexError and others on odd headers
        return raw


def _addresses(message: EmailMessage, *names: str) -> list[str]:
    """Every address in these headers, lowercased. Reads the parsed header, which copes with 'Reyes, Dana <…>'.

    Each header is parsed on its own, so one the parser cannot read (a Cc of
    "a@b.com, :;") falls back to its raw text instead of failing the message.
    """
    found: list[str] = []
    for name in names:
        for header, raw in message.raw_items():
            if str(header).casefold() != name.casefold():
                continue
            value = _header(message, header, raw)
            try:
                parsed = [str(address.addr_spec) for address in getattr(value, "addresses", ())]
            except Exception:  # noqa: BLE001 - as above
                parsed = []
            if not parsed:
                parsed = [address for _name, address in getaddresses([str(raw)])]
            found += [address.strip().casefold() for address in parsed if "@" in address]
    return found


def _sender(message: EmailMessage) -> tuple[str, str]:
    """(display name, address) of the one who wrote it. Copes with 'Lee, Greg <greg@…>', where a comma splits the name."""
    value = next((_header(message, name, raw) for name, raw in message.raw_items() if str(name).casefold() == "from"), None)
    try:
        parsed = list(getattr(value, "addresses", ()))
    except Exception:  # noqa: BLE001 - the header parser raises IndexError and others on odd headers
        parsed = []
    for address in parsed:
        if "@" in str(address.addr_spec):
            name = str(address.display_name or "")
            # The name before a comma the header parser split off ("Lee, Greg"): both halves are the name.
            if parsed.index(address) > 0 and not name.count("@"):
                name = " ".join(str(part.display_name or part.addr_spec or "") for part in parsed[:parsed.index(address) + 1]).strip()
            return name, str(address.addr_spec).strip().casefold()
    found = _addresses(message, "From")
    if found:
        return parseaddr(str(value or ""))[0], found[0]
    name, address = parseaddr(str(value or ""))
    return name, address.strip().casefold()


def _received(data: dict[str, Any], message: EmailMessage) -> datetime | None:
    """When Gmail received it; its Date header when Gmail gives no time; None when neither says."""
    stamp = int(data.get("internalDate") or 0)
    if stamp > 0:
        return datetime.fromtimestamp(stamp / 1000, tz=timezone.utc)
    try:
        dated = parsedate_to_datetime(str(message.get("Date", "")))
    except (TypeError, ValueError, IndexError):
        return None
    return (dated if dated.tzinfo else dated.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def answers_something(message: EmailMessage) -> bool:
    """Whether a message is written as a reply, not a fresh email or a forward."""
    return bool(message.get("In-Reply-To") or message.get("References")) or bool(
        _ANSWER_SUBJECT.match(str(message.get("Subject", "")))
    )


def is_bulk(message: EmailMessage) -> bool:
    """Sent to many, or through a mailing or sales tool: list headers, or a bulk precedence."""
    if message.get("List-Unsubscribe") or message.get("List-Id"):
        return True
    if str(message.get("Precedence", "")).strip().casefold() in {"bulk", "junk", "list"}:
        return True
    auto = str(message.get("Auto-Submitted", "")).strip().casefold()
    return bool(auto) and auto not in {"no", "auto-replied"}


def is_automatic(message: EmailMessage) -> bool:
    """An out-of-office or other automatic answer (RFC 3834), by its headers or its subject."""
    if str(message.get("Auto-Submitted", "")).strip().casefold() == "auto-replied":
        return True
    if message.get("X-Autoreply") or message.get("X-Autorespond"):
        return True
    if str(message.get("Precedence", "")).strip().casefold() == "auto_reply":
        return True
    return bool(_AUTO_SUBJECT.search(str(message.get("Subject", ""))))


def _delivery_kind(message: EmailMessage, sender: str) -> str:
    """'delivery' for a delivery or delay notice, from any sender; 'receipt' for a read receipt; else ''."""
    content_type = str(message.get("Content-Type", "")).casefold()
    if "multipart/report" in content_type and "disposition-notification" in content_type:
        return "receipt"
    if sender.split("@", 1)[0] in _DAEMONS or message.get("X-Failed-Recipients"):
        return "delivery"
    if "multipart/report" in content_type:
        return "delivery"
    return ""


def _machine(local: str) -> bool:
    return bool(_MACHINE_SENDER.search(local)) or bool(NO_REPLY_SENDER.search(f"{local}@"))


# Words that make an address a team's or a function's (recruitment@, hiring-team@, bovi.careers@): a part of the
# address that starts with a long one, or is exactly a short one (so hrishi@ and stafford@ stay people).
_ROLE_PREFIX = re.compile(
    r"recruit|hiring|talent|career|campus|universit|internship|student|people|communit|research|admission|feedback|"
    r"educat|sponsor|welcome|onboard|partner|investor|founder|contact|inquir|enquir|support|operations|marketing",
    re.IGNORECASE,
)
_ROLE_EXACT = {
    "jobs", "job", "team", "help", "info", "hello", "hi", "sales", "admin", "ops", "hr", "staff", "group", "lab", "labs",
    "press", "media", "store", "shop", "event", "events", "office", "intern", "interns", "people", "careers", "billing",
}


def _role_word(token: str) -> bool:
    return token in _ROLE_EXACT or bool(_ROLE_PREFIX.match(token))


def is_person(address: str, company: str = "") -> bool:
    """Whether an address looks like one person's (dana@, d.reyes@), not a shared inbox (careers@, recruitment@), a
    company-named one (bovi@ for Bovi), or a machine (noreply@)."""
    local = str(address or "").split("@", 1)[0].casefold().split("+", 1)[0]
    if not local or local in GENERIC_LOCAL_PARTS or _machine(local):
        return False
    tokens = [token for token in re.split(r"[._\-]+", local) if token]
    if any(_role_word(token) for token in tokens):
        return False
    stem = _letters(_domain(address).split(".")[0]) if "@" in str(address) else ""
    names = set(_company_words(company).split()) if company else set()
    return not ((stem and _letters(local) == stem) or (names and set(tokens) <= names))


def _letters(text: str) -> str:
    return re.sub(r"[^a-z]", "", unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold())


def name_matches_address(display: str, address: str, company: str = "") -> bool:
    """Whether the From name and address are one person's: Dana Reyes as dana@, d.reyes@, dreyes@ or reyes.d@.

    Words of the company's name or a role ("Bovi Careers") are not a person's name, and one must remain.
    """
    ignore = set(_company_words(company).split()) if company else set()
    words = [_letters(word) for word in re.split(r"[\s,.'\-]+", display) if _letters(word)]
    words = [word for word in words if word not in ignore and not _role_word(word)]
    local = _letters(str(address).split("@", 1)[0].split("+", 1)[0])
    if not words or not local:
        return False
    if local in words or any(len(word) >= 3 and word in local for word in words):
        return True
    first, last = words[0], words[-1]
    return len(words) >= 2 and local in {first[0] + last, first + last[0], last + first[0], "".join(word[0] for word in words)}


def _link_hosts(message: EmailMessage) -> set[str]:
    return {host for host in (host_of(url) for url in _URL.findall(_body_text(message, whole=False))) if host}


def _job_mail(message: EmailMessage, sender: str) -> bool:
    """Whether a job system had a hand in it: its sender, return path or signer is an applicant, assessment or scheduling
    system, or it links to an applicant or assessment system (a LinkedIn or Calendly link in a signature does not count)."""
    hosts = {_domain(sender), *(_domain(address) for address in _addresses(message, "Return-Path"))}
    for result in str(message.get("Authentication-Results", "")).split(";"):
        signer = re.search(r"header\.(?:d|i)=@?([^\s;]+)", result)
        if signer:
            hosts.add(signer.group(1).casefold())
    return any(listed(host, READ_CATEGORIES) for host in hosts if host) or any(
        listed(host, ("ats", "assessment")) for host in _link_hosts(message)
    )


# Sales-engagement tools: a rep's sequence email reads like a person writing, so it is only ever a possible reply.
_SALES_HOSTS = (
    "hubspotlinks.com", "hs-sales-engage.com", "hubspotemail.net", "sidekickopen", "outreach.io", "salesloft.com",
    "apollo.io", "lemlist.com", "mixmax.com", "yesware.com", "mailtrack.io", "reply.io", "mailshake.com",
)


def _sales_tool(message: EmailMessage) -> bool:
    if any(str(name).casefold().startswith("x-hubspot") for name in message.keys()):
        return True
    hosts = _link_hosts(message) | {_domain(address) for address in _addresses(message, "Return-Path")}
    return any(marker in host for host in hosts for marker in _SALES_HOSTS)


# --- Who each company is ------------------------------------------------------------


def _domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].casefold().strip().rstrip(".>") if "@" in address else ""


def _normal(address: str) -> str:
    """An address as a mailbox: lowercased, without a +tag, and Gmail's dots and googlemail.com folded away."""
    text = str(address or "").strip().casefold()
    if "@" not in text:
        return text
    local, domain = text.rsplit("@", 1)
    local = local.split("+", 1)[0] or local
    if domain in {"gmail.com", "googlemail.com"}:
        local, domain = local.replace(".", ""), "gmail.com"
    return f"{local}@{domain}"


def _platform(host: str) -> bool:
    return any(host == shared or host.endswith(f".{shared}") for shared in PLATFORM_HOSTS) or not_an_employer(host)


def _institution(host: str) -> bool:
    return bool(_INSTITUTION.search(registrable_domain(host) or host))


def _own_domains() -> set[str]:
    """The student's own sending domain and its registrable domain: their school's or employer's mail is never a company's."""
    domain = _domain(sender_account())
    return {item for item in (domain, registrable_domain(domain) or "") if item}


def _is_own(host: str, own: set[str]) -> bool:
    return bool(own) and (host in own or (registrable_domain(host) or host) in own or any(host.endswith(f".{item}") for item in own))


def _names_host(company: str, host: str) -> bool:
    """Whether a company's name carries a host's name: Rippling for rippling.com, never Acme for linkedin.com."""
    stem = re.sub(r"[^a-z0-9]", "", (registrable_domain(host) or host).split(".")[0])
    words = [re.sub(r"[^a-z0-9]", "", word) for word in str(company or "").casefold().split()]
    return len(stem) >= 3 and (stem in words or stem == "".join(words))


def _website_strength(url: str, host: str, company: str) -> bool:
    """Whether a website's domain is as good as proof: its root, or a page on a host its name carries (not uwaterloo.ca/bovi-lab)."""
    text = str(url or "").strip()
    try:
        path = urlsplit(text if "//" in text else f"https://{text}").path
    except ValueError:
        return False
    return path in {"", "/"} or _names_host(company, host) or len([part for part in path.split("/") if part]) == 1 and not _INSTITUTION.search(host)


def _website_domain(url: str, own: set[str], company: str = "") -> str:
    """The domain a company's website stands for, or '' when it says nothing (a page on a platform, a university, the student's own).

    A platform's own site (www.rippling.com for Rippling) is its company's
    domain; a page on it (linkedin.com/company/acme, sites.google.com/view/acme,
    acme.wixsite.com) is not.
    """
    host = website_domain(url)
    if not host or "." not in host or host in FREEMAIL or _institution(host) or _is_own(host, own):
        return ""
    if _platform(host) and not _names_host(company, host):
        text = str(url or "").strip()
        try:
            path = urlsplit(text if "//" in text else f"https://{text}").path
        except ValueError:
            path = "/x"
        if host != (registrable_domain(host) or host) or path not in {"", "/"}:
            return ""
    labels = host.split(".")
    # careers.acme.com is acme.com: one label naming a section of the site is dropped, no more.
    if len(labels) > 2 and labels[0] in _SECTION_LABELS:
        rest = ".".join(labels[1:])
        if registrable_domain(host) is None or registrable_domain(rest) == registrable_domain(host):
            host = rest
    return host


def _stem(domain: str) -> str:
    return (registrable_domain(domain) or domain).split(".")[0]


_STEM_SUFFIXES = {"", "inc", "hq", "co", "corp", "labs", "lab", "ai", "io", "tech", "group", "global", "us", "usa", "mail", "team", "app", "hq"}


def _contact_domain(address: str, site: str, own: set[str], company: str = "") -> tuple[str, bool]:
    """The domain of a contact's address as a company domain, and whether it is as good as the website's.

    Only when it shares the website's name (bovirobotics.us for
    bovirobotics.com), or there is no website: a contact at an ISP, a VC, an
    agency or a platform stands for nobody else there. A contact at a
    university stands for their own department's host (cs.stateu.edu, never
    stateu.edu), and only weakly: what comes from there is at most a possible reply.
    """
    domain = _domain(address)
    if not domain or domain in FREEMAIL or (registrable_domain(domain) or domain) in FREEMAIL or _is_own(domain, own):
        return "", False
    if site and (domain == site or domain.endswith(f".{site}")):
        return domain, True
    if _institution(domain):
        return (domain, False) if not site else ("", False)
    if _platform(domain):
        # A platform's own people (jane@rippling.com for Rippling, with no website on file) are its company's.
        return (domain, False) if not site and _names_host(company, domain) and domain == (registrable_domain(domain) or domain) \
            else ("", False)
    if site:
        stem, theirs = _stem(site), _stem(domain)
        # The same name, or the name and a corporate word (bovirobotics.us, bovirobotics-inc.com), never another company's.
        rest = re.sub(r"[^a-z0-9]", "", theirs[len(stem):]) if theirs.startswith(stem) else None
        return (domain, True) if len(stem) >= 4 and rest in _STEM_SUFFIXES else ("", False)
    return domain, False


def _alias(address: str) -> str:
    """At a university, one person's mailboxes on its hosts (jkim@stateu.edu, jkim@cs.stateu.edu) as one; else ''."""
    domain = _domain(address)
    if not domain or not _institution(domain):
        return ""
    return f"{address.split('@', 1)[0].casefold().split('+', 1)[0]}@{registrable_domain(domain) or domain}"


def _company_words(company: str) -> str:
    """A company's name as it is written in mail, without its legal suffix: 'Bovi Robotics, Inc.' is 'bovi robotics'."""
    tokens = identity_tokens(company)
    return " ".join(word for word in normalized(company).split() if word in tokens)


def _stamp(value: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _distinctive(company: str) -> bool:
    """Whether a company's name is specific enough to search mail for: two words, or one of six letters or more."""
    words = _company_words(company).split()
    return len(words) >= 2 or any(len(word) >= 6 for word in words)


def _watched(conn: sqlite3.Connection, user_id: str, now: datetime) -> list[dict[str, Any]]:
    """Companies written to recently enough to hear back from, with everything that speaks for them.

    A company is watched by what was sent, not by its status: an email the app
    or Gmail sent (at the time Gmail sent it, when known), a contact form, a
    move to Sent or Followed up, or the sent date. It stays watched while its
    latest email is within REPLY_WINDOW, whatever its status became (a bounce
    that set it back to Drafted included); a reply counts from the first.
    """
    rows = conn.execute(
        "SELECT id, company, status, contact_email, contact_cc, website, sent_at, mail_domains_json, email_subject "
        "FROM outreach_targets WHERE user_id=?",
        (user_id,),
    ).fetchall()
    sends: dict[str, list[tuple[datetime, dict[str, Any]]]] = {}
    for event in conn.execute(
        "SELECT target_id, detail, created_at FROM outreach_events WHERE user_id=? AND event_type=?", (user_id, SENT_EVENT)
    ).fetchall():
        try:
            detail = json.loads(event["detail"])
        except (TypeError, ValueError):
            continue
        if not isinstance(detail, dict):
            continue
        at = _stamp(event["created_at"])
        # A send made in Gmail is recorded when the app notices it, which can be hours later.
        sent_ms = detail.get("sent_ms")
        if isinstance(sent_ms, (int, float)) and sent_ms > 0:
            gmail_time = datetime.fromtimestamp(sent_ms / 1000, tz=timezone.utc)
            at = min(at, gmail_time) if at else gmail_time
        if at:
            sends.setdefault(event["target_id"], []).append((at, detail))
    # A message sent through a contact form has no address to watch, only the company's domain.
    forms: dict[str, list[datetime]] = {}
    for event in conn.execute(
        "SELECT target_id, created_at FROM outreach_events WHERE user_id=? AND event_type IN (?, ?)",
        (user_id, FORM_SUBMITTED, FORM_UNCONFIRMED),
    ).fetchall():
        if _stamp(event["created_at"]):
            forms.setdefault(event["target_id"], []).append(_stamp(event["created_at"]))
    marked: dict[str, list[datetime]] = {}
    for event in conn.execute(
        "SELECT target_id, created_at FROM outreach_events WHERE user_id=? AND event_type='status' AND to_status IN ('sent', 'followed_up')",
        (user_id,),
    ).fetchall():
        if _stamp(event["created_at"]):
            marked.setdefault(event["target_id"], []).append(_stamp(event["created_at"]))
    account = _normal(sender_account())
    own = _own_domains()
    watched = []
    for row in rows:
        own_sends = sends.get(row["id"], [])
        written = [str(row[field] or "") for field in ("contact_email", "contact_cc")]
        written += [str(detail.get(field) or "") for _at, detail in own_sends for field in ("to", "cc")]
        # The contact and whoever the emails went to are the company's; someone only copied (a referrer), or added in
        # Gmail's To line, is the company's only when at one of its domains (worked out below).
        primary = {str(row["contact_email"] or "").strip().casefold()} | {
            str(detail.get("to") or "").strip().casefold() for _at, detail in own_sends}
        # A draft sent from Gmail went to whoever its To line named, which the student may have changed there.
        written += [address for _at, detail in own_sends for _name, address in getaddresses([str(detail.get("sent_to_header") or "")])]
        addresses = {text.strip().casefold() for text in written if "@" in text}
        addresses = {address for address in addresses if _normal(address) != account}
        # The app's own sends say to the second when the email went; a send marked
        # by hand has only a date, so the day before it is the earliest a reply counts.
        starts: list[tuple[datetime, bool]] = [(at, False) for at, _detail in own_sends]
        starts += [(at, False) for at in forms.get(row["id"], [])]
        first_app = min((at for at, _date_only in starts), default=None)
        # Sent by hand: when it was marked sent, and the day before its sent date, both only approximate.
        # They count while nothing the app sent is earlier (an app send marks the company sent too).
        starts += [(at, True) for at in marked.get(row["id"], []) if first_app is None or at < first_app - timedelta(hours=1)]
        if row["sent_at"]:
            try:
                day = date.fromisoformat(str(row["sent_at"])[:10])
                # sent_at is the student's local date: up to a day from the UTC date of the app's own send.
                if first_app is None or day < first_app.date() - timedelta(days=1):
                    starts.append((datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) - timedelta(days=1), True))
            except ValueError:
                pass
        # Anyone at the company's own domains may answer, whatever address the contact uses:
        # its website's, another its own site shows it mailing from (persona.ai, personainc.ai),
        # and a contact's that shares the website's name (a site on .com, mail on .us).
        site = _website_domain(row["website"] or "", own, row["company"])
        domains: dict[str, bool] = {site: _website_strength(row["website"] or "", site, row["company"])} if site else {}
        try:
            mail_domains = [str(other).casefold() for other in json.loads(row["mail_domains_json"] or "[]")]
        except (TypeError, ValueError):
            mail_domains = []
        for other in mail_domains:
            if other and other not in FREEMAIL and not _institution(other) and not _is_own(other, own):
                domains[other] = True
        aliases = {_alias(address) for address in addresses} - {""}
        company = {address for address in addresses if address in primary or any(
            _domain(address) == own_domain or _domain(address).endswith(f".{own_domain}") for own_domain in domains)}
        outsiders = {_normal(address) for address in addresses - company}
        for field in ("contact_email", "contact_cc"):
            domain, strong = _contact_domain(str(row[field] or ""), site, own, row["company"])
            if domain:
                domains[domain] = domains.get(domain, False) or strong
        via_form = row["id"] in forms
        # A company written to through its form is watched even with no domain: its threads and its name are looked for.
        if not (addresses or via_form) or not starts:
            continue
        latest = max(at for at, _date_only in starts)
        if now - latest > REPLY_WINDOW:
            continue
        since, date_only = min(starts, key=lambda start: start[0])
        watched.append({
            "id": row["id"], "company": row["company"], "status": row["status"], "addresses": addresses,
            "mailboxes": {_normal(address) for address in company} | aliases, "outsiders": outsiders, "aliases": aliases,
            "domains": domains, "since": since,
            "since_is_date": date_only, "last": latest, "via_form": via_form,
            "subject": " ".join(str(row["email_subject"] or "").split()),
            # Each email's Gmail thread, and when it went: whoever writes in it is answering it.
            "threads": {str(detail["thread_id"]): at for at, detail in own_sends if detail.get("thread_id")},
        })
    return watched


def watched_ids(conn: sqlite3.Connection, user_id: str, now: datetime | None = None) -> set[str]:
    """The companies capture_replies looks for replies from. Any other company's replies are never looked for.

    A company drops out when it has no address to search for (a LinkedIn
    message, a contact form without a website domain), no record of an email
    to it, or its latest email is more than REPLY_WINDOW ago.
    """
    return {str(target["id"]) for target in _watched(conn, user_id, now or datetime.now(timezone.utc))}


def _at_domain(domain: str, target: dict[str, Any]) -> bool | None:
    """Whether a sender's domain is one of the company's: True when it is a strong one, False a weak one, None not at all."""
    matched = [strong for own, strong in target["domains"].items() if domain == own or domain.endswith(f".{own}")]
    return any(matched) if matched else None


def _at_company(sender: str, target: dict[str, Any]) -> bool:
    return _normal(sender) in target["mailboxes"] or _at_domain(_domain(sender), target) is not None


def _owner(
    watched: list[dict[str, Any]], message: EmailMessage, sender: str, thread_id: str = "",
) -> tuple[list[dict[str, Any]], str]:
    """The companies a message may be from, the most recently written first, and how it was matched.

    ``watched`` is every watched company, never one search's batch. In order: a
    message in the Gmail thread of an email to a company is that company's
    ("thread"); else the sender is an address the student wrote to
    ("address"); else anyone at a company's domain ("domain"); else only its
    Reply-To or Sender is an address written to ("reply_to").
    """
    if thread_id:
        for target in watched:
            if thread_id in target["threads"]:
                return [target], "thread"
    by_latest = sorted(watched, key=lambda target: target["last"], reverse=True)
    mailbox, domain = _normal(sender), _domain(sender)
    names = {mailbox, _alias(sender)} - {""}
    for how, matches in (
        ("address", [target for target in by_latest if names & target["mailboxes"]]),
        ("domain", [target for target in by_latest if domain and _at_domain(domain, target) is not None]),
        ("outsider", [target for target in by_latest if mailbox in target["outsiders"]]),
    ):
        if matches:
            return matches, how
    # Relayed: an applicant system or LinkedIn writes for someone whose Reply-To is at the company.
    others = {_normal(address) for address in _addresses(message, "Reply-To", "Sender")} - {mailbox}
    matches = [target for target in by_latest if others & target["mailboxes"]
               or any(_at_domain(_domain(other), target) is not None for other in others)]
    return (matches, "reply_to") if matches else ([], "")


def _named(watched: list[dict[str, Any]], message: EmailMessage, text: str) -> list[dict[str, Any]]:
    """Companies a message names: the exact subject of the email to them (when no other company's shares it), or their name."""
    subject = " ".join(str(message.get("Subject", "")).split()).casefold()
    words = f" {normalized(f'{subject} {text}')} "
    shared = _shared_subjects(watched)
    found = []
    for target in sorted(watched, key=lambda item: item["last"], reverse=True):
        own = target["subject"].casefold()
        name = _company_words(target["company"])
        single = len(name.split()) == 1
        written = str(target["company"]).split(",")[0].strip()
        named = f" {name} " in words and (not single or bool(re.search(rf"\b{re.escape(written)}\b", f"{message.get('Subject', '')} {text}")))
        if (own and own not in shared and own in subject) or (_distinctive(target["company"]) and named):
            found.append(target)
    return found


def _shared_subjects(watched: list[dict[str, Any]]) -> set[str]:
    seen: dict[str, int] = {}
    for target in watched:
        if target["subject"]:
            seen[target["subject"].casefold()] = seen.get(target["subject"].casefold(), 0) + 1
    return {subject for subject, count in seen.items() if count > 1}


def _thread_of(message: EmailMessage, target: dict[str, Any]) -> str:
    """Set by read() on the parsed message: the Gmail thread it was found in."""
    return str(getattr(message, "gmail_thread_id", "") or "")


def _addressed(message: EmailMessage, account: str) -> bool:
    """Whether the student was in its To or Cc line, not only a blind copy."""
    recipients = {_normal(address) for address in _addresses(message, "To", "Cc")}
    if account:
        return _normal(account) in recipients
    return bool(recipients) and "undisclosed" not in str(message.get("To", "")).casefold()


def _minutes_after_send(target: dict[str, Any], received_at: datetime) -> float:
    sent = [at for at in [target["since"], target["last"], *target["threads"].values()] if at <= received_at]
    return (received_at - max(sent)).total_seconds() / 60 if sent else float("inf")


def _acknowledged(target: dict[str, Any], sender: str, subject: str, text: str, received_at: datetime) -> bool:
    """A receipt ("we received your message", "this is an automated message"), from a sender that is not a person."""
    words = f"{subject} {text[:2000]}"
    if ALWAYS_AUTOMATIC.search(words):
        return True
    if target["via_form"]:
        # After a contact form, as ever: a no-reply sender or receipt wording soon after is the form's receipt.
        return is_acknowledgement(sender, subject, text, _minutes_after_send(target, received_at))
    # A receipt does not ask anything.
    return "?" not in text and _minutes_after_send(target, received_at) <= ACKNOWLEDGEMENT_WINDOW_MINUTES \
        and bool(ACKNOWLEDGEMENT.search(words))


def _judge(
    message: EmailMessage, *, targets: list[dict[str, Any]], how: str, sender: str, display: str, account: str,
    labels: list[str], received_at: datetime, text: str,
) -> tuple[str, str]:
    """What a message found for a company is (REPLY, AUTOMATIC, POSSIBLE or IGNORED), and the reason code (outreach.REPLY_REASONS).

    Only plain machine mail is set aside. What is plainly someone at the
    company answering is a reply. Anything in between is a possible reply,
    which the student sees and settles, and which holds every automatic step
    meanwhile. See the module docstring.
    """
    target = targets[0]
    local = sender.split("@", 1)[0]
    subject = str(message.get("Subject", ""))
    if (account and _normal(sender) == _normal(account)) or "SENT" in labels or "DRAFT" in labels:
        return IGNORED, "own"
    delivery = _delivery_kind(message, sender)
    if delivery:
        return (AUTOMATIC, "receipt") if delivery == "receipt" else (IGNORED, "delivery")
    if "TRASH" in labels:
        return IGNORED, "trash"
    # Someone at the company writing through a relay (an applicant system, LinkedIn): its Reply-To. That lets the
    # message be shown, never counted: counting rests on the sender's own address.
    relayed = any(is_person(other, target["company"]) and _at_company(other, target)
                  for other in _addresses(message, "Reply-To", "Sender"))
    own_person = is_person(sender, target["company"])
    person = own_person or relayed
    if how == "thread":
        sent = target["threads"].get(_thread_of(message, target), None)
        if sent is not None and received_at < sent - timedelta(minutes=5):
            return IGNORED, "before"  # an earlier message in the thread the student's email joined
        if _at_company(sender, target):
            if is_automatic(message):
                return AUTOMATIC, "out_of_office"
            generated = str(message.get("Auto-Submitted", "")).strip().casefold() not in {"", "no"} \
                or bool(message.get("X-Auto-Response-Suppress"))
            if not own_person and (generated or _acknowledged(target, sender, subject, text, received_at)):
                return AUTOMATIC, "acknowledgement"
            if generated:
                # A person's address, but a system sent it (a help desk, an out-of-office in another language): shown.
                return POSSIBLE, "auto_generated"
            if not own_person and _machine(local):
                return POSSIBLE, "automated_sender"
            return REPLY, "thread"
        automated = is_automatic(message) or (
            not person and (_machine(local) or listed(_domain(sender), tuple(sender_lists())) or not is_person(sender))
            and _acknowledged(target, sender, subject, text, received_at)
        )
        return (AUTOMATIC, "acknowledgement") if automated else (POSSIBLE, "thread_outsider")
    if all(received_at < candidate["since"] for candidate in targets):
        near = target["since_is_date"] and received_at >= target["since"] - timedelta(days=14)
        if near and (how == "address" or (how in {"domain", "reply_to"} and person)):
            return POSSIBLE, "before_marked_sent"
        return IGNORED, "before"
    if how == "name":
        if is_bulk(message) or not person or not _addressed(message, account) or is_automatic(message):
            return IGNORED, "no_company"
    elif is_bulk(message):
        # From the address written to, or a person, through a mailing or sales tool: shown, never set aside.
        if how != "address" and not person and not answers_something(message):
            return IGNORED, "list"
        return POSSIBLE, "mailing_tool"
    if is_automatic(message):
        return AUTOMATIC, "out_of_office"
    if not person and _acknowledged(target, sender, subject, text, received_at):
        return AUTOMATIC, "acknowledgement"
    verified = authenticate(message).ok
    if "SPAM" in labels and how != "address" and not verified:
        # A person at the company's own domain whose mail Gmail doubted is still shown; the rest in Spam is not.
        if not (person and how == "domain" and _at_domain(_domain(sender), target)):
            return IGNORED, "spam_unverified"
    kind, reason = _judge_match(message, targets=targets, how=how, sender=sender, display=display, account=account,
                                person=person, verified=verified)
    if kind == REPLY and relayed and not own_person:
        return POSSIBLE, "reply_to"
    if "SPAM" in labels and kind == REPLY:
        return POSSIBLE, "spam"
    return kind, reason


def _judge_match(
    message: EmailMessage, *, targets: list[dict[str, Any]], how: str, sender: str, display: str, account: str,
    person: bool, verified: bool,
) -> tuple[str, str]:
    """The last steps of _judge: how strong the match to the company is."""
    if how in {"name", "reply_to"} and _job_mail(message, sender):
        return POSSIBLE, "job_mail"
    if len(targets) > 1:
        return POSSIBLE, "ambiguous"
    if how == "name":
        return POSSIBLE, "mentions_company"
    if how == "reply_to":
        return POSSIBLE, "reply_to"
    if how == "outsider":
        return POSSIBLE, "copied_outsider"
    if how == "address":
        # An applicant system writes in the name of the recruiter (and from the careers@ inbox) the student wrote to.
        if _job_mail(message, sender):
            return POSSIBLE, "job_mail"
        # A blast they blind-copied everyone on ("all positions are filled") is not an answer to the student.
        if not _addressed(message, account) and not answers_something(message):
            return POSSIBLE, "not_addressed"
        return REPLY, "written_to"
    local = sender.split("@", 1)[0]
    if _job_mail(message, sender):
        return POSSIBLE, "job_mail"
    if _machine(local):
        # An automated sender at the company is shown when it answers something or follows a contact form
        # (an interview invitation from no-reply@ their applicant system is job mail, above); account mail
        # from a big company's no-reply@ (a security code) is set aside.
        shown = _addressed(message, account) and (answers_something(message) or targets[0]["via_form"])
        return (POSSIBLE, "automated_sender") if shown else (IGNORED, "automated_sender")
    if not person:
        return POSSIBLE, "shared_address"
    if _sales_tool(message):
        return POSSIBLE, "mailing_tool"
    if not _at_domain(_domain(sender), targets[0]):
        return POSSIBLE, "weak_domain"
    if not verified:
        return POSSIBLE, "not_verified"
    if not _addressed(message, account):
        return POSSIBLE, "not_addressed"
    if not name_matches_address(display, sender, targets[0]["company"]):
        return POSSIBLE, "name_mismatch"
    return REPLY, "domain_person"


# --- Recording ------------------------------------------------------------------------


def _seen(conn: sqlite3.Connection, user_id: str, gmail_id: str) -> bool:
    """Read already, and judged under these rules (a row older rules set aside is read again)."""
    row = conn.execute(
        "SELECT kind, rules FROM outreach_inbox_messages WHERE user_id=? AND gmail_id=?", (user_id, gmail_id)
    ).fetchone()
    return row is not None and not (row[0] in REJUDGED and int(row[1] or 0) < RULES)


def _remember(
    conn: sqlite3.Connection, user_id: str, gmail_id: str, target_id: str, kind: str, sender: str, received: str, *,
    via: str = "", reason: str = "", thread_id: str = "", subject: str = "", text: str = "", candidates: list[str] | None = None,
    message_id: str = "", from_name: str = "", in_spam: bool = False, meta: dict[str, Any] | None = None,
) -> bool:
    """Record a message as read. False when another check already recorded it.

    It takes the place of a row older rules set aside. The subject, Message-ID
    and sender's name are kept for a reply or a possible reply; the words, and
    the rest of what a reply_logged event keeps (``meta``: REPLY_META), only
    for a possible reply, until the student decides.
    """
    kept = kind in {REPLY, POSSIBLE}
    stored = {key: _meta_value(value) for key, value in (meta or {}).items() if key in REPLY_META} if kind == POSSIBLE else {}
    return bool(conn.execute(
        """
        INSERT INTO outreach_inbox_messages(
            user_id, gmail_id, target_id, kind, sender, received_at, recorded_at, via, rules, reason, thread_id,
            subject, text, candidates_json, message_id, from_name, in_spam, meta_json
        )
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id, gmail_id) DO UPDATE SET
            target_id=excluded.target_id, kind=excluded.kind, sender=excluded.sender, received_at=excluded.received_at,
            recorded_at=excluded.recorded_at, via=excluded.via, rules=excluded.rules, reason=excluded.reason,
            thread_id=excluded.thread_id, subject=excluded.subject, text=excluded.text,
            candidates_json=excluded.candidates_json, message_id=excluded.message_id, from_name=excluded.from_name,
            in_spam=excluded.in_spam, meta_json=excluded.meta_json
        WHERE outreach_inbox_messages.kind IN ('ignored', 'automatic') AND outreach_inbox_messages.rules < ?
        """,
        (user_id, gmail_id, target_id, kind, sender, received, utc_now(), via, RULES, reason, thread_id,
         subject[:300] if kept else "", text if kind == POSSIBLE else "", json.dumps(candidates or []),
         message_id[:500] if kept else "", from_name[:200] if kept else "", 1 if in_spam else 0,
         json.dumps(stored, sort_keys=True), RULES),
    ).rowcount)


# What a reply_logged event keeps about where a Gmail reply came from, beside its text and readings.
# full_text is the whole message, quoted lines and anything typed between them included (a thank-you
# after a decline reads it: outreach_thank_you); reply_to is its Reply-To address, when it has one;
# headers are the KEPT_HEADERS as they arrived, and link_hosts the host of every link in it (hosts only;
# None when they could not all be read), which outreach_thank_you.thank_you_blockers checks before anything
# is sent on its own.
REPLY_META = {"thread_id", "message_id", "subject", "from_name", "full_text", "reply_to", "headers", "link_hosts"}
# The headers kept with a Gmail reply: who it was to, whether a person or a system sent it, and Gmail's own sender
# check. Headers only, never more of the body than full_text already keeps.
KEPT_HEADERS = (
    "From", "Sender", "To", "Cc", "Subject", "Return-Path", "Auto-Submitted", "X-Auto-Response-Suppress",
    "List-Unsubscribe", "List-Id", "Precedence", "X-Autoreply", "X-Autorespond", "DKIM-Signature", "Authentication-Results",
)
# A message with more of these, or a longer one, is not kept at all, so a check that reads them fails closed.
KEPT_HEADER_LIMIT = 4_000
KEPT_HEADER_COUNT = 40
LINK_HOST_LIMIT = 50
_LINK = re.compile(r"""https?://[^\s<>"'`]+""", re.IGNORECASE)


def kept_headers(message: EmailMessage) -> list[list[str]] | None:
    """The KEPT_HEADERS of a message, in the order they arrived and as they arrived ([name, raw value] pairs).

    None when there are too many or one is too long to keep whole: a check
    that reads them then finds none, and fails closed.
    """
    wanted = {name.casefold() for name in KEPT_HEADERS}
    kept: list[list[str]] = []
    for name, value in message.raw_items():
        if str(name).casefold() not in wanted:
            continue
        text = str(value).encode("utf-8", "surrogateescape").decode("utf-8", "replace")
        if len(text) > KEPT_HEADER_LIMIT or len(kept) >= KEPT_HEADER_COUNT:
            return None
        kept.append([str(name), text])
    return kept


def _hosts(text: str) -> Iterable[str]:
    from .mail_trust import host_of

    for url in _LINK.finditer(html.unescape(str(text or ""))):
        host = host_of(url.group(0).rstrip(".,;:!?)]}'\""))
        if host:
            yield host


def hosts_in(text: str) -> set[str]:
    """The host of every link in a text. Hosts only."""
    return set(_hosts(text))


def link_hosts(message: EmailMessage) -> list[str] | None:
    """The host of every link in a message's text parts, plain and HTML (href too), quoted parts included. Hosts only.

    None when a text part cannot be read (a charset Python does not know) or
    it links more than LINK_HOST_LIMIT hosts: like kept_headers, a check that
    reads them then fails closed rather than missing a link it never saw.
    """
    hosts: list[str] = []
    for part in message.walk():
        if part.is_multipart() or part.get_content_maintype() != "text":
            continue
        try:
            content = str(part.get_content())
        except Exception:  # noqa: BLE001 - a part that cannot be read hides its links, so none are vouched for
            return None
        for host in _hosts(content):
            if host not in hosts:
                if len(hosts) >= LINK_HOST_LIMIT:
                    return None
                hosts.append(host)
    return hosts


def _meta_value(value: Any) -> Any:
    return value if isinstance(value, list) or value is None else str(value)


def reply_meta(message: EmailMessage, *, thread_id: str, message_id: str, subject: str, from_name: str) -> dict[str, Any]:
    """What a reply_logged event keeps about a Gmail message beside its text (REPLY_META).

    A header too odd to read leaves its field out, so a check that needs it
    (outreach_thank_you) fails closed rather than the capture stopping.
    """
    meta: dict[str, Any] = {
        "thread_id": thread_id, "message_id": message_id, "subject": subject, "from_name": " ".join(from_name.split())[:120],
    }
    readers: dict[str, Callable[[], Any]] = {
        # One character past the limit says it was cut (outreach_thank_you reads it whole or not at all).
        "full_text": lambda: full_reply_text(message)[:FULL_TEXT_LIMIT + 1],
        "reply_to": lambda: ", ".join(
            address.casefold() for _name, address in getaddresses([str(message.get("Reply-To", ""))]) if address
        )[:300],
        "headers": lambda: kept_headers(message),
        "link_hosts": lambda: link_hosts(message),
    }
    for key, read in readers.items():
        try:
            meta[key] = read()
        except (IndexError, KeyError, LookupError, AttributeError, TypeError, ValueError, UnicodeError):
            LOGGER.warning("Could not keep %s of an outreach reply", key, exc_info=True)
    return meta


def _same_words(one: str, other: str) -> bool:
    plain = lambda text: " ".join(str(text or "").split()).casefold()[:300]
    return bool(plain(one)) and plain(one) == plain(other)


def _found_words(reason: str, sender: str, target: dict[str, Any]) -> str:
    """How a reply found in Gmail was matched to the company, for its history."""
    wrote_to = ", ".join(sorted(target.get("addresses") or [])) or "the company"
    return f"Found in Gmail: {reply_reason(reason, sender)}. You wrote to {wrote_to}."


def _record_reply(
    conn: sqlite3.Connection, target: dict[str, Any], *, user_id: str, gmail_id: str, sender: str,
    received: str, text: str, decisions: DecisionClient | None, via: str = "", reason: str = "", thread_id: str = "",
    subject: str = "", message_id: str = "", from_name: str = "", addresses: set[str] | None = None,
    claim: Callable[[], bool] | None = None, notify: bool = True, late: bool = False, meta: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Log one reply, or None when another check (the background one, say) got to it first.

    The reading (Jev or the rules) is asked for before anything is written, so
    no transaction waits on it. ``claim`` takes the message instead of
    recording it as read (a possible reply the student confirmed is recorded
    already); False means someone else took it first. A reply whose words are
    already logged (the student pasted it) is not logged twice.

    The reply_logged event keeps, beside the reply's text, where it came from
    (its Gmail and RFC ids, its thread, its subject, who wrote it and when, and
    how it was matched to the company: ``via`` and ``reason``) and both
    readings of it: Jev's answer or why there was none, and the rules'
    status, worked out apart (inbox_classifiers.read_reply, one Jev call). A
    thank-you after a decline reads them (outreach_thank_you).
    """
    suggestion, readings = read_reply(text, suggest_reply_status, decisions)
    if suggestion["status"] == BOUNCED:
        # A person wrote this, so it is a reply even if it talks about a failed delivery.
        suggestion = {**suggestion, "status": "replied"}
    data = {
        "source": "gmail", "gmail_id": gmail_id, "from": sender, "received_at": received, "readings": readings,
        **{key: _meta_value(value) for key, value in (meta or {}).items() if key in REPLY_META},
        # How it was matched to the company (outreach.REPLY_REASONS). A thank-you answers only a reply in the
        # student's thread or from an address they wrote to (outreach_thank_you, R1).
        "via": via, "reason": reason,
    }
    with conn:
        taken = claim() if claim is not None else _remember(
            conn, user_id, gmail_id, target["id"], REPLY, sender, received, via=via, reason=reason, thread_id=thread_id,
            subject=subject, message_id=message_id, from_name=from_name,
        )
        if not taken:
            return None
        logged = [row[0] for row in conn.execute(
            "SELECT detail FROM outreach_events WHERE target_id=? AND user_id=? AND event_type='reply_logged'", (target["id"], user_id),
        ).fetchall()]
        if not any(_same_words(earlier, text) for earlier in logged):
            _log(conn, target["id"], user_id, "reply_logged", detail=text, data=data)
            # They wrote again: a thank-you after their earlier decline that has not gone stops now.
            from .outreach_thank_you import on_new_reply  # imported here: it imports this module's neighbours

            on_new_reply(conn, target["id"], user_id)
        if reason and claim is None:
            _log(conn, target["id"], user_id, "reply_found",
                 detail=_found_words(reason, sender, {"addresses": addresses if addresses is not None else target.get("addresses")}))
        if notify:
            # Said outside the page too (and on the desktop when the student turned that on). Never an address or words.
            day = _stamp(received)
            title = f"{target['company']} replied" + (
                f" on {day:%b} {day.day} (found late)" if late and day else ""
            )
            automation._insert_notice(conn, user_id, event_key=f"outreach-reply:{gmail_id}", level="info", title=title,
                                      body="Their reply is logged in Outreach.", timestamp=utc_now())
    current = get_target(conn, target["id"], user_id=user_id)
    if current["status"] in REOPENED_BY_REPLY:
        update_target(conn, target["id"], {"status": "replied"}, user_id=user_id)
        current = get_target(conn, target["id"], user_id=user_id)
    stored = ""
    if suggestion["status"] not in {current["status"], "replied"}:
        stored = json.dumps({**suggestion, "from": sender, "received_at": received}, sort_keys=True)
    with conn:
        waiting = conn.execute(
            "SELECT reply_suggestion_json FROM outreach_targets WHERE id=? AND user_id=?", (target["id"], user_id),
        ).fetchone()
        try:
            newer = json.loads((waiting[0] if waiting else "") or "null") or {}
        except (TypeError, ValueError):
            newer = {}
        # A reply found late (in a thread, or read again) never replaces what a newer one suggests.
        if not (isinstance(newer, dict) and str(newer.get("received_at") or "") > received):
            conn.execute(
                "UPDATE outreach_targets SET reply_suggestion_json=?, updated_at=? WHERE id=? AND user_id=?",
                (stored, utc_now(), target["id"], user_id),
            )
    return {"target_id": target["id"], "company": target["company"], "from": sender, "suggestion": suggestion}


def _record_possible(
    conn: sqlite3.Connection, targets: list[dict[str, Any]], *, user_id: str, gmail_id: str, sender: str, received: str,
    via: str, reason: str, thread_id: str, subject: str, text: str, message_id: str, from_name: str, notify: bool = True,
    in_spam: bool = False, meta: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Keep a message that may be a reply for the student to settle. None when another check got to it first."""
    target = targets[0]
    with conn:
        if not _remember(
            conn, user_id, gmail_id, target["id"], POSSIBLE, sender, received, via=via, reason=reason, thread_id=thread_id,
            subject=subject, text=text, candidates=[other["id"] for other in targets[1:]], message_id=message_id,
            from_name=from_name, in_spam=in_spam, meta=meta,
        ):
            return None
        for owner in targets:
            _log(conn, owner["id"], user_id, "possible_reply",
                 detail=f"{sender}: “{subject or '(no subject)'}”. Not counted as a reply until you say, "
                        f"because {reply_reason(reason, sender)}.")
        if notify:
            title = f"{target['company']} may have replied" if len(targets) == 1 else "A company you wrote to may have replied"
            body = "Open Outreach to check it." + (" Gmail put it in Spam." if in_spam else "")
            automation._insert_notice(conn, user_id, event_key=f"outreach-possible-reply:{gmail_id}", level="info",
                                      title=title, body=body, timestamp=utc_now())
    return {"target_id": target["id"], "company": target["company"], "from": sender, "reason": reason,
            "companies": [other["company"] for other in targets]}


class PossibleReplyNotFound(LookupError):
    """No email of that id waits, or ever waited, as a possible reply for that company."""


class PossibleReplySettled(ValueError):
    """The student (or another tab) already said whether that email is a reply."""


def decide_possible_reply(
    conn: sqlite3.Connection, target_id: str, gmail_id: str, decision: str, *, user_id: str,
    decisions: DecisionClient | None = None, on_reply: OnReply | None = None,
) -> dict[str, Any]:
    """The student says whether an email kept as a possible reply is one, on this company's card. Returns the company.

    "reply" logs it as this company's reply exactly as a reply found in Gmail
    (the Replied move, the reading, call prep); "not_reply" sets it aside for
    good. Either way it stops holding every company it could have been from,
    and its words leave outreach_inbox_messages: a logged reply keeps them in
    the history, a dismissed one is not kept at all.
    """
    if decision not in {"reply", "not_reply"}:
        raise ValueError("Say reply or not_reply")
    target = get_target(conn, target_id, user_id=user_id)
    row = conn.execute(
        "SELECT target_id, kind, sender, received_at, text, subject, via, reason, thread_id, message_id, from_name, "
        "candidates_json, meta_json, decided_at FROM outreach_inbox_messages WHERE user_id=? AND gmail_id=?",
        (user_id, gmail_id),
    ).fetchone()
    try:
        others = [str(other) for other in json.loads(row["candidates_json"] or "[]")] if row else []
    except (TypeError, ValueError):
        others = []
    if row is not None and row["kind"] != POSSIBLE and row["decided_at"]:
        raise PossibleReplySettled("You already said whether this email is a reply")
    if row is None or row["kind"] != POSSIBLE or target_id not in {str(row["target_id"]), *others}:
        raise PossibleReplyNotFound(gmail_id)
    stamp = utc_now()
    sender = str(row["sender"])

    def settle(kind: str) -> bool:
        return bool(conn.execute(
            "UPDATE outreach_inbox_messages SET kind=?, target_id=?, text='', meta_json='{}', candidates_json='[]', decided_at=?, "
            "reason=CASE WHEN ?='reply' THEN 'confirmed' ELSE reason END, "
            "subject=CASE WHEN ?='reply' THEN subject ELSE '' END, message_id=CASE WHEN ?='reply' THEN message_id ELSE '' END, "
            "from_name=CASE WHEN ?='reply' THEN from_name ELSE '' END WHERE user_id=? AND gmail_id=? AND kind=?",
            (kind, target_id, stamp, kind, kind, kind, kind, user_id, gmail_id, POSSIBLE),
        ).rowcount)

    if decision == "reply":
        text = str(row["text"] or "") or f"(An email from {sender} with no text, subject: {row['subject']})"

        def confirm() -> bool:
            if not settle(REPLY):
                return False
            _log(conn, target_id, user_id, "possible_reply_confirmed",
                 detail=f"{sender}: “{row['subject'] or '(no subject)'}”. You said it is their reply.")
            return True

        try:
            meta = json.loads(row["meta_json"] or "{}")
        except (TypeError, ValueError):
            meta = {}
        # Logged as the student's call ("confirmed"), never as the match itself: nothing automatic answers it.
        captured = _record_reply(
            conn, target, user_id=user_id, gmail_id=gmail_id, sender=sender, received=str(row["received_at"] or stamp),
            text=text, decisions=decisions, claim=confirm, notify=False, via=str(row["via"] or ""), reason="confirmed",
            meta=meta if isinstance(meta, dict) else {},
        )
        if captured is None:
            raise PossibleReplySettled("You already said whether this email is a reply")
        if on_reply:
            on_reply(conn, target_id, user_id)
    else:
        with conn:
            if not settle(DISMISSED):
                raise PossibleReplySettled("You already said whether this email is a reply")
            _log(conn, target_id, user_id, "possible_reply_dismissed",
                 detail=f"{sender}: “{row['subject'] or '(no subject)'}”. You said it is not a reply.")
    return get_target(conn, target_id, user_id=user_id, include_events=True)


# --- Looking in Gmail -----------------------------------------------------------------

# Pages of search results read per check; a search with more goes on from there on the next check.
MAX_PAGES = 10


def _search(gmail: _Gmail, user_id: str, query: str, *, page_size: int = 100) -> tuple[list[dict[str, Any]], int, bool]:
    """What a search finds (Spam included), the status of a failed page (0 if none), and whether pages were left.

    The first page is always read, for what arrived since; when the pages ran
    out last time, the next ones go on from where that search stopped.
    """
    found: list[dict[str, Any]] = []
    with _MEMORY_LOCK:
        resume = _RESUME.pop((user_id, query), "")
    token = ""
    for page in range(MAX_PAGES):
        # includeSpamTrash: a reply Gmail filed as Spam is still a reply (each query leaves Trash out).
        params: dict[str, Any] = {"q": query, "maxResults": page_size, "includeSpamTrash": "true"}
        if token:
            params["pageToken"] = token
        listing = gmail.request("GET", "/messages", params=params)
        if listing.status_code != 200:
            if resume:
                with _MEMORY_LOCK:
                    _RESUME[(user_id, query)] = resume
            return found, listing.status_code, bool(resume)
        data = listing.json()
        found.extend(data.get("messages") or [])
        token = str(data.get("nextPageToken") or "")
        if page == 0 and token and resume:
            token = resume
        if not token:
            return found, 0, False
    with _MEMORY_LOCK:
        _RESUME[(user_id, query)] = token
    return found, 0, True


def _chunks(watched: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Companies in batches whose from:(...) list stays short enough for one search."""
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    length = 0
    for target in watched:
        size = sum(len(term) + 4 for term in (*target["addresses"], *target["aliases"], *target["domains"]))
        if current and (length + size > QUERY_LENGTH or len(current) >= 20):
            chunks.append(current)
            current, length = [], 0
        current.append(target)
        length += size
    if current:
        chunks.append(current)
    return chunks


def _window(chunk: list[dict[str, Any]], now: datetime) -> str:
    since = max(min(target["since"] for target in chunk), now - REPLY_WINDOW) - timedelta(days=1)
    return f"after:{since:%Y/%m/%d} -in:sent -in:drafts -in:trash"


def _query(chunk: list[dict[str, Any]], now: datetime | None = None) -> str:
    """The from:(...) search for a batch of companies, or '' when none of them has an address or a domain."""
    terms = sorted({term for target in chunk for term in (*target["addresses"], *target["aliases"], *target["domains"])})
    return f"from:({' OR '.join(terms)}) {_window(chunk, now or datetime.now(timezone.utc))}" if terms else ""


def _name_query(chunk: list[dict[str, Any]], watched: list[dict[str, Any]], now: datetime) -> str:
    """A search for mail naming these companies or their emails' exact subjects, from anywhere, or '' when none is specific enough."""
    shared = _shared_subjects(watched)
    plain = lambda text: " ".join(text.replace('"', " ").split())
    terms = [f'subject:"{plain(target["subject"])}"' for target in chunk
             if len(target["subject"]) >= 8 and target["subject"].casefold() not in shared]
    terms += [f'"{_company_words(target["company"])}"' for target in chunk if _distinctive(target["company"])]
    terms = sorted(set(terms))
    return f"{{{' '.join(terms)}}} {_window(chunk, now)}" if terms else ""


SWEEP_SETTING = "outreach_reply_sweep"


def _sweep_start(conn: sqlite3.Connection, user_id: str, watched: list[dict[str, Any]], now: datetime) -> datetime | None:
    """How far back the recent-mail listing reads: a day before the last sweep that read everything, else the oldest sent thread.

    The mark is kept (user_settings SWEEP_SETTING) with the rules it was made
    under, so the first sweep under new rules goes back to the oldest thread.
    """
    sent = [at for target in watched for at in target["threads"].values()]
    if not sent:
        return None
    with _MEMORY_LOCK:
        last = _LAST_SWEEP.get(user_id)
    if last is None:
        row = conn.execute("SELECT value FROM user_settings WHERE user_id=? AND key=?", (user_id, SWEEP_SETTING)).fetchone()
        try:
            mark = json.loads(row[0]) if row else {}
        except (TypeError, ValueError):
            mark = {}
        last = _stamp(mark.get("at")) if isinstance(mark, dict) and mark.get("rules") == RULES else None
    start = last - timedelta(days=1) if last else min(sent)
    return max(start, min(sent) - timedelta(minutes=5), now - REPLY_WINDOW)


def _swept(conn: sqlite3.Connection, user_id: str, now: datetime) -> None:
    """Note a sweep that listed and read everything since its start."""
    with _MEMORY_LOCK:
        _LAST_SWEEP[user_id] = now
    with conn:
        conn.execute(
            "INSERT INTO user_settings(user_id, key, value, updated_at) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (user_id, SWEEP_SETTING, json.dumps({"at": now.isoformat(timespec="seconds"), "rules": RULES}), utc_now()),
        )


def _activated_at(conn: sqlite3.Connection) -> datetime | None:
    """When these rules started: mail from before then was judged by older ones, so it is never counted without the student."""
    row = conn.execute("SELECT applied_at FROM schema_migrations WHERE name=?", (MIGRATION,)).fetchone()
    return _stamp(row[0]) if row else None


def capture_replies(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    client_factory: ClientFactory,
    decisions: DecisionClient | None = None,
    on_reply: OnReply | None = None,
    now: datetime | None = None,
    force: bool = False,
    force_target: str | None = None,
) -> dict[str, Any]:
    """Find new replies in Gmail and log each one. See the module docstring for what each does.

    ``state`` is as for check_deliveries. Runs at most once a minute per
    student unless ``force`` or ``force_target`` (``skipped`` is then True);
    nothing is asked of Gmail when no company is waiting to hear back.
    ``force_target`` also reads every Gmail thread of that company's emails
    now and reads all it finds for them, however many (the look before an
    automatic send); a background check reads at most READ_BUDGET messages.
    ``possible`` lists messages that may be replies, kept for the student.
    """
    now = now or datetime.now(timezone.utc)
    result: dict[str, Any] = {"state": "ok", "replies": [], "automatic": [], "possible": []}
    with _CAPTURE_LOCK:
        last = _LAST_CAPTURE.get(user_id)
        if not (force or force_target) and last is not None and now - last < CAPTURE_EVERY:
            return {**result, "skipped": True}
        _LAST_CAPTURE[user_id] = now
    watched = _watched(conn, user_id, now)
    if not watched:
        with conn:
            conn.execute(
                "UPDATE outreach_inbox_messages SET target_id='', rules=?, reason='no_company' WHERE user_id=? AND kind=? AND rules < ?",
                (RULES, user_id, IGNORED, RULES),
            )
            conn.execute(
                "UPDATE outreach_inbox_messages SET rules=? WHERE user_id=? AND kind=? AND rules < ?", (RULES, user_id, AUTOMATIC, RULES),
            )
        return result
    row = _connector(conn, user_id)
    if not row or row["status"] != "connected":
        return {**result, "state": "not_connected" if not row or row["status"] == "disconnected" else "needs_reconnect"}
    account = sender_account().casefold()
    activated = _activated_at(conn)
    budget = [READ_BUDGET]
    late: list[dict[str, Any]] = []

    def read(gmail: _Gmail, gmail_id: str, thread_hint: str = "", *, by_name: bool = False, exempt: bool = False) -> bool:
        """Read one message found for a company, once, and log, show, note or set it aside.

        False when it was not read (the budget ran out, or Gmail did not answer), so a later check reads it.
        ``exempt`` reads are outside the budget: a sent thread's messages, and the forced company's mail.
        """
        if not gmail_id or _seen(conn, user_id, gmail_id):
            return True
        if not exempt:
            if budget[0] <= 0:
                result["incomplete"] = True
                return False
            budget[0] -= 1
        fetched = gmail.request("GET", f"/messages/{quote(gmail_id, safe='')}", params={"format": "raw"})
        if fetched.status_code == 404:
            with conn:  # deleted since it was found; a row older rules left is settled as gone
                conn.execute(
                    "UPDATE outreach_inbox_messages SET rules=?, reason='gone' WHERE user_id=? AND gmail_id=? "
                    "AND kind IN ('ignored', 'automatic') AND rules < ?",
                    (RULES, user_id, gmail_id, RULES),
                )
            return True
        if fetched.status_code != 200:
            result["state"] = "unreachable"
            return False
        data = fetched.json()
        labels = [str(label) for label in data.get("labelIds") or []]
        thread_id = str(data.get("threadId") or thread_hint or "")
        try:
            raw = str(data.get("raw", ""))
            message = email.message_from_bytes(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)), policy=policy.default)
            # No time from Gmail or the Date header: taken as arriving now, never as "before the first email".
            received_at = _received(data, message) or now
            display, sender = _sender(message)
            subject = " ".join(str(message.get("Subject", "")).split())[:300]
            text = reply_text(message)
            message.gmail_thread_id = thread_id  # type: ignore[attr-defined]
            targets, how = _owner(watched, message, sender, thread_id)
            if not targets and by_name:
                targets, how = _named(watched, message, _body_text(message, whole=False)), "name"
            kind, reason = _judge(message, targets=targets, how=how, sender=sender, display=display, account=account,
                                  labels=labels, received_at=received_at, text=text) if targets else (IGNORED, "no_company")
        except (IndexError, KeyError, LookupError, AttributeError, TypeError, ValueError, UnicodeError):
            # Headers Python's parser cannot read (a stray quote): never the end of every later check. In a sent
            # thread it is shown, to open in Gmail; elsewhere it is set aside with why.
            LOGGER.warning("Could not read an email found for outreach replies", exc_info=True)
            owner = next((target for target in watched if thread_id and thread_id in target["threads"]), None)
            if owner is None and not by_name and "message" in locals():
                # The raw From text, which never goes through the header parser that just failed.
                loose = parseaddr(next((str(value) for name, value in message.raw_items() if name.lower() == "from"), ""))[1]
                loose = loose.strip().casefold()
                if "@" in loose:
                    owner = next((target for target in sorted(watched, key=lambda item: item["last"], reverse=True)
                                  if _at_company(loose, target)), None)
            stamp = now.isoformat(timespec="seconds")
            if owner is None:
                with conn:
                    _remember(conn, user_id, gmail_id, "", IGNORED, "", stamp, reason="unreadable", thread_id=thread_id)
                return True
            possible = _record_possible(
                conn, [owner], user_id=user_id, gmail_id=gmail_id, sender="", received=stamp, via="thread",
                reason="unreadable", thread_id=thread_id, subject="", text="", message_id="", from_name="",
                in_spam="SPAM" in labels,
            )
            if possible is not None:
                result["possible"].append(possible)
            return True
        received = received_at.isoformat(timespec="seconds")
        if not targets:
            with conn:  # nothing about it is kept but that it was read
                _remember(conn, user_id, gmail_id, "", IGNORED, "", received, reason="no_company")
            return True
        # Mail from before these rules was judged by older ones: never counted now without the student, unless
        # someone at the company answered in the thread of the student's email and older rules never saw it.
        judged_before = conn.execute(
            "SELECT 1 FROM outreach_inbox_messages WHERE user_id=? AND gmail_id=? AND rules < ?", (user_id, gmail_id, RULES),
        ).fetchone() is not None
        early = (activated is not None and received_at < activated) or judged_before
        if early and kind == REPLY and (reason != "thread" or judged_before):
            kind, reason = POSSIBLE, "found_late"
        target = targets[0]
        facts = {"via": how, "reason": reason, "thread_id": thread_id}
        if kind in {IGNORED, AUTOMATIC}:
            with conn:
                noted = conn.execute(
                    "SELECT kind FROM outreach_inbox_messages WHERE user_id=? AND gmail_id=?", (user_id, gmail_id),
                ).fetchone()
                if not _remember(conn, user_id, gmail_id, target["id"], kind, sender, received, **facts):
                    return True
                if kind == AUTOMATIC and not (noted and noted[0] == AUTOMATIC):
                    _log(conn, target["id"], user_id, "auto_reply", detail=f"{sender}: {' '.join(text.split())[:300]}")
                    result["automatic"].append({"target_id": target["id"], "company": target["company"], "from": sender})
            return True
        if not text:
            text = f"(An email from {sender} with no text, subject: {subject})"
        message_id = " ".join(str(message.get("Message-ID", "")).split())
        meta = reply_meta(message, thread_id=thread_id, message_id=message_id, subject=subject, from_name=display)
        if kind == POSSIBLE:
            possible = _record_possible(
                conn, targets, user_id=user_id, gmail_id=gmail_id, sender=sender, received=received, subject=subject,
                text=text, message_id=message_id, from_name=display, notify=not early, in_spam="SPAM" in labels, meta=meta,
                **facts,
            )
            if possible is not None:
                result["possible"].append(possible)
                if early:
                    late.append(possible)
            return True
        captured = _record_reply(
            conn, get_target(conn, target["id"], user_id=user_id), user_id=user_id, gmail_id=gmail_id, sender=sender,
            received=received, text=text, decisions=decisions, subject=subject, message_id=message_id, from_name=display,
            addresses=target["addresses"], late=now - received_at > timedelta(days=1), meta=meta, **facts,
        )
        if captured is None:
            return True
        result["replies"].append(captured)
        if on_reply:
            on_reply(conn, target["id"], user_id)
        return True

    def read_thread(gmail: _Gmail, thread_id: str) -> bool:
        """Read one sent thread directly. False when it could not be read (then the check is not complete)."""
        response = gmail.request(
            "GET", f"/threads/{quote(thread_id, safe='')}",
            params=[("format", "metadata"), *(("metadataHeaders", name) for name in _THREAD_HEADERS)],
        )
        if response.status_code == 404:
            return True  # the thread was deleted: nothing left to find in it
        if response.status_code == 403:
            raise _NeedsReconnect
        if response.status_code != 200:
            result["state"] = "unreachable"
            return False
        complete = True
        for message in response.json().get("messages") or []:
            labels = message.get("labelIds") or []
            headers = {str(item.get("name", "")).lower(): str(item.get("value", ""))
                       for item in (message.get("payload") or {}).get("headers") or []}
            if "SENT" in labels or "DRAFT" in labels or _headers_say_failure(message):
                continue  # the student's own, or a delivery notice (outreach_delivery reads those)
            if account and _normal(parseaddr(headers.get("from", ""))[1]) == _normal(account):
                continue
            complete = read(gmail, str(message.get("id", "")), thread_id, exempt=True) and complete
        return complete

    def look(gmail: _Gmail) -> None:
        """Every look of one check, in order. Raises _NeedsReconnect when Gmail refuses a read."""
        # The company an automatic send is about to go to: its threads and its mail, now, before anything else,
        # outside the budget, so the look before the send is complete for it.
        forced = next((target for target in watched if target["id"] == force_target), None)
        done_threads: set[str] = set()
        if forced is not None:
            for thread_id in forced["threads"]:
                if read_thread(gmail, thread_id):
                    done_threads.add(thread_id)
            query = _query([forced], now)
            for reference in (listing(gmail, query)[0] if query else []):
                read(gmail, str(reference.get("id", "")), str(reference.get("threadId") or ""), exempt=True)
        # Whoever writes in the thread of an email the student sent is answering it, from any address:
        # every message received lately is listed (5 quota units a page) and those in sent threads are
        # read, first and outside the budget, as the strongest evidence there is.
        start = _sweep_start(conn, user_id, watched, now)
        if start is not None:
            sent_threads = {thread_id: at for target in watched for thread_id, at in target["threads"].items()}
            query = f"-in:sent -in:drafts -in:trash after:{int(start.timestamp())}"
            references, complete = listing(gmail, query, page_size=500)
            read_all = False
            try:
                read_all = True
                for reference in references:
                    thread_id = str(reference.get("threadId") or "")
                    if thread_id in sent_threads:
                        if read(gmail, str(reference.get("id", "")), thread_id, exempt=True):
                            done_threads.add(thread_id)
                        else:
                            read_all = False
            finally:
                if not read_all:
                    # A message it listed could not be read: the next sweep lists again from the newest page,
                    # never going on past it from where this one stopped.
                    with _MEMORY_LOCK:
                        _RESUME.pop((user_id, query), None)
            complete = complete and read_all
            if complete:
                _swept(conn, user_id, now)
            else:
                # The listing did not reach back far enough, or a message could not be read: the newest
                # sent threads are read directly, and the next sweep starts from the same place.
                left = sorted((thread for thread in sent_threads if thread not in done_threads),
                              key=lambda thread: sent_threads[thread], reverse=True)
                for thread_id in left[:THREAD_FALLBACK]:
                    read_thread(gmail, thread_id)
        # Rows older rules set aside are read again under these.
        for row in conn.execute(
            "SELECT gmail_id FROM outreach_inbox_messages WHERE user_id=? AND kind IN ('ignored', 'automatic') AND rules < ? "
            "ORDER BY received_at DESC LIMIT ?",
            (user_id, RULES, REJUDGE_BUDGET),
        ).fetchall():
            read(gmail, str(row[0]))
        for chunk in _chunks(watched):
            query = _query(chunk, now)
            for reference in (listing(gmail, query)[0] if query else []):
                read(gmail, str(reference.get("id", "")), str(reference.get("threadId") or ""))
        # Mail naming a company, or the exact subject of the email to it, from an address that is not theirs.
        for chunk in _chunks(watched):
            query = _name_query(chunk, watched, now)
            for reference in (listing(gmail, query)[0] if query else []):
                read(gmail, str(reference.get("id", "")), str(reference.get("threadId") or ""), by_name=True)

    def listing(gmail: _Gmail, query: str, *, page_size: int = 100) -> tuple[list[dict[str, Any]], bool]:
        """A search's results, and whether it read them all. A failed one leaves the check not ok."""
        references, failed, more = _search(gmail, user_id, query, page_size=page_size)
        if failed == 403:
            raise _NeedsReconnect
        if failed:
            # A search that failed found nothing yet; say so, so a send waiting on it holds.
            result["state"] = "unreachable"
        return references, not failed and not more

    outcome = result
    try:
        with client_factory() as client:
            look(_Gmail(conn, client, user_id))
    except (_NeedsReconnect, GmailAuthError):
        outcome = {**result, "state": "needs_reconnect"}
    except GmailThrottled:
        # Gmail asked to slow down: what was not read yet is read on a later check, and a send waiting on it holds.
        outcome = {**result, "state": "throttled"}
    except (httpx.HTTPError, ValueError):
        outcome = {**result, "state": "unreachable"}
    if late:
        # Mail from before these rules, now shown: one notice for the lot, not one each.
        count = len(late)
        automation.notice(
            conn, user_id, event_key=f"outreach-possible-backlog:{now:%Y%m%d%H%M%S}:{late[0]['target_id']}", level="info",
            title=f"{count} earlier {'email' if count == 1 else 'emails'} may be {'a reply' if count == 1 else 'replies'}",
            body="The app had not counted them. Open Outreach to check each one.",
        )
    return outcome


_THREAD_HEADERS = ("From", "To", "Cc", "Subject", "Content-Type", "X-Failed-Recipients")


class _NeedsReconnect(Exception):
    """Gmail refused a read: the connection predates the read scope."""


# --- In the background ----------------------------------------------------------------

# What a check's state means, as automation_health records it. Never an address or a message's words.
STEP_ERRORS = {
    "needs_reconnect": "Gmail needs reconnecting",
    "not_connected": "Gmail is not connected",
    "unreachable": "Gmail could not be reached",
    "throttled": "Gmail asked the app to slow down",
    "message_errors": "Some job emails could not be read and were set aside",
    "database_busy": "The database was busy, so the job-email check stopped; it tries again next time",
}
RECONNECT_ERROR = "Gmail needs reconnecting"
CONNECTION = "inbox.connection"
_ADDRESS = re.compile(r"""[^\s@<>"'(),;:]+@[^\s@<>"'(),;:]+""")


_QUERY = re.compile(r"(https?://[^\s?#]+)[?#][^\s]*")


def _step_error(exc: BaseException) -> str:
    """An exception as a health error: its type and message, with any address and any URL's query string taken out."""
    words = _ADDRESS.sub("[address]", _QUERY.sub(lambda found: found.group(1), str(exc)))
    return f"{type(exc).__name__}: {words[:200]}"


def _record(
    conn: sqlite3.Connection, user_id: str, component: str, *, ok: bool, error: str = "", detail: dict[str, Any] | None = None,
) -> None:
    """automation.record_health, which opens its own transaction; a failure to record is logged, never raised."""
    try:
        automation.record_health(conn, user_id, component, ok=ok, error=error, detail=detail)
    except Exception:  # noqa: BLE001 - the pass goes on to the next step and the next student
        LOGGER.warning("Could not record the health of %s", component, exc_info=True)


def _discard_open_transaction(conn: sqlite3.Connection) -> None:
    """After a step failed: roll back what it left uncommitted, so recording its health does not commit it."""
    try:
        if getattr(conn, "in_transaction", False):
            conn.rollback()
    except Exception:  # noqa: BLE001
        LOGGER.warning("Could not roll back after an inbox step failed", exc_info=True)


def _save_gmail_health(conn: sqlite3.Connection, user_id: str) -> None:
    """Write the Gmail connection's health that memory holds (outreach_gmail.persist_gmail_health).

    Called only between the watcher's steps, where nothing of the watcher's is
    pending: every step commits its own writes, and a step that failed was
    rolled back (_discard_open_transaction). What can still be open is the
    read-only transaction psycopg starts on PostgreSQL for a step's first
    SELECT, which makes every Gmail health write inside the steps wait. Ending
    it here commits nothing but reads; on SQLite no transaction is open here.
    """
    try:
        if getattr(conn, "in_transaction", False):
            conn.commit()
        persist_gmail_health(conn, user_id)
    except Exception:  # noqa: BLE001 - the pass goes on to the next step and the next student
        _discard_open_transaction(conn)
        LOGGER.warning("Could not save the Gmail connection's health", exc_info=True)


def _moment(stamp: Any) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _local(conn: sqlite3.Connection, user_id: str, stamp: str) -> str:
    """A stored UTC time as the student reads it: "Sat, Sep 26 at 3:14 PM"."""
    moment = _moment(stamp)
    if moment is None:
        return str(stamp)
    local = user_timezone(conn, user_id).to_local(moment)
    return f"{local:%a, %b} {local.day} at {f'{local:%I:%M %p}'.lstrip('0')}"


def _record_connection(conn: sqlite3.Connection, user_id: str, status: str, updated_at: str) -> None:
    """The inbox.connection health row, from the connector's status.

    Connected is ok. A connection the student disconnected on purpose is not a
    problem, so it is recorded as ok with the state "disconnected", once, and
    never as "needs reconnecting". Only a connection in 'error' is an error,
    and it says since when: the earliest break seen while it stays broken, so a
    pass every few minutes neither moves "since" nor the error's own time.
    """
    try:
        existing = conn.execute(
            "SELECT last_error, detail_json FROM automation_health WHERE user_id=? AND component=?", (user_id, CONNECTION),
        ).fetchone()
        before = json.loads(existing["detail_json"] or "{}") if existing else {}
    except Exception:  # noqa: BLE001 - unread, the row is simply written again
        LOGGER.warning("Could not read the inbox connection's health", exc_info=True)
        _discard_open_transaction(conn)
        existing, before = None, {}
    if not isinstance(before, dict):
        before = {}
    if status == "connected":
        _record(conn, user_id, CONNECTION, ok=True, detail={"state": "connected"})
        return
    if status == "disconnected":
        if before.get("state") != "disconnected":
            _record(conn, user_id, CONNECTION, ok=True, detail={"state": "disconnected"})
        return
    since = str(updated_at or utc_now())
    earlier, now_seen = _moment(before.get("since")), _moment(since)
    if before.get("state") == "error" and earlier is not None and (now_seen is None or earlier < now_seen):
        since = str(before["since"])
    error = f"{RECONNECT_ERROR} (since {_local(conn, user_id, since)})"
    if existing is not None and before.get("state") == "error" and before.get("since") == since and existing["last_error"] == error:
        return  # already recorded: last_error_at stays when it was first seen
    _record(conn, user_id, CONNECTION, ok=False, error=error, detail={"state": "error", "since": since})


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
        """One pass over every student's Gmail connection, whatever its state.

        A broken or disconnected connection is only noted (a health row, and a
        notice when it is broken); nothing is asked of Gmail. A connected one
        runs each check on its own: a check that fails or raises is recorded and
        the next one runs. Between the steps, and at the end of each student,
        the Gmail health that memory holds is saved (_save_gmail_health), so a
        rate limit or a success seen inside a step reaches the row on
        PostgreSQL too, where it could not be written mid-step.
        """
        with closing(connect_product(self.platform_target)) as conn:
            rows = [(row[0], row[1], row[2]) for row in conn.execute(
                "SELECT user_id, status, updated_at FROM connector_accounts WHERE provider=? ORDER BY user_id", (PROVIDER,)
            ).fetchall()]
            for user_id, status, updated_at in rows:
                if status == "connected":
                    self._check(conn, user_id)
                else:
                    _record_connection(conn, user_id, status, updated_at)
                self._notices(conn, user_id)
                _save_gmail_health(conn, user_id)

    def _check(self, conn: sqlite3.Connection, user_id: str) -> None:
        from . import application_inbox  # imported here: it imports this module
        from .outreach_gmail_sends import capture_gmail_sends  # imported here: it imports the scheduler

        factory = self._client_factory
        steps: list[tuple[str, Callable[[], dict[str, Any]]]] = [
            # A draft sent from Gmail first, so its bounce and replies are watched in the same pass.
            ("inbox.sends", lambda: capture_gmail_sends(conn, user_id=user_id, client_factory=factory)),
            ("inbox.deliveries", lambda: check_deliveries(conn, user_id=user_id, client_factory=factory)),
            ("inbox.replies", lambda: capture_replies(
                conn, user_id=user_id, client_factory=factory,
                decisions=self._decisions_for(conn, user_id), on_reply=self._on_reply,
            )),
        ]
        # Job-system mail after the replies, so a reply outreach owns is never read as one.
        # Only when the student turned it on (or into shadow); off, its cursor is forgotten.
        try:
            switch = automation.mode(conn, user_id, application_inbox.FEATURE)
        except Exception:  # noqa: BLE001 - the other steps still run
            _discard_open_transaction(conn)
            LOGGER.warning("Could not read the application mail switch", exc_info=True)
            # Not knowing is not "off": the cursor is left alone, and the next pass asks again.
            switch = None
        if switch not in (None, "off"):
            steps.append((application_inbox.HEALTH_COMPONENT, lambda: application_inbox.run_pass(
                conn, user_id=user_id, client_factory=factory, decisions=self._decisions_for(conn, user_id),
            )))
        elif switch == "off":
            application_inbox.note_off(conn, user_id)
        for component, step in steps:
            try:
                outcome = step() or {}
                state = str(outcome.get("state", "ok"))
            except Exception as exc:  # noqa: BLE001 - one step failing never stops the others
                LOGGER.warning("Inbox step %s failed", component, exc_info=True)
                _discard_open_transaction(conn)
                _record(conn, user_id, component, ok=False, error=_step_error(exc))
            else:
                detail = outcome.get("detail") if isinstance(outcome.get("detail"), dict) else None
                if outcome.get("skipped"):
                    pass  # it ran less than its interval ago; its last outcome stands
                elif state == "ok":
                    _record(conn, user_id, component, ok=True, detail=detail)
                else:
                    _record(conn, user_id, component, ok=False, error=STEP_ERRORS.get(state, f"Gmail check stopped: {state}"),
                            detail=detail)
            _save_gmail_health(conn, user_id)
        # A 401 during the checks can leave the connection broken (or the student may have disconnected meanwhile).
        row = _connector(conn, user_id)
        if row is None:
            return
        _record_connection(conn, user_id, row["status"], row["updated_at"])

    def _notices(self, conn: sqlite3.Connection, user_id: str) -> None:
        try:
            gmail_notices(conn, user_id)
        except Exception:  # noqa: BLE001 - the next student is still checked
            _discard_open_transaction(conn)
            LOGGER.warning("Could not leave Gmail connection notices", exc_info=True)

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
