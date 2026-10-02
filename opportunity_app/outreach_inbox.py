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

``inbox_watcher.InboxWatcher`` runs both checks on a background thread, so a
reply or a bounce is caught even when the page is closed.
"""

from __future__ import annotations

import email
import json
import logging
import re
import sqlite3
import threading
from datetime import date, datetime, timedelta, timezone
from email import policy
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr
from typing import Any, Callable
from urllib.parse import quote

import httpx

from pipeline_core.identity import normalized

from . import automation, mail_message, outreach_callbacks
from .inbox_classifiers import read_reply
from .mail_message import (
    FULL_TEXT_LIMIT,
    MAILER_DAEMONS,
    URL,
    answers_something,
    body_text,
    full_reply_text,
    header_map,
    host_of,
    is_automatic,
    is_bulk_or_generated,
    kept_headers,
    link_hosts_or_none,
    mailbox_key,
    reply_text,
)
from .mail_trust import FREEMAIL, READ_CATEGORIES, authenticate, listed, sender_lists
from .outreach import log_event, get_target, update_target
from .outreach_replies import BOUNCED, reply_reason, suggest_reply_status
from .integrations.gmail_client import (
    ClientFactory,
    GmailAuthError,
    GmailNeedsReadScope,
    GmailThrottled,
    connection_state,
)
from .outreach_delivery import headers_say_failure
from .outreach_gmail import SENT_EVENT
from .gmail_connection import connector_row, GmailClient
from .outreach_config import sender_account
from .outreach_identity import (
    company_words,
    contact_domain,
    domain_of,
    is_distinctive,
    is_institution,
    is_machine_local,
    is_own,
    is_person,
    name_matches_address,
    own_domains,
    site_domain,
    university_alias,
    website_strength,
)
from .outreach_forms import (
    ACKNOWLEDGEMENT,
    ACKNOWLEDGEMENT_WINDOW_MINUTES,
    ALWAYS_AUTOMATIC,
    SUBMITTED_EVENT as FORM_SUBMITTED,
    UNCONFIRMED_EVENT as FORM_UNCONFIRMED,
    is_acknowledgement,
)
from .core.settings_store import get_setting, put_setting
from .core.timestamps import parse_app_instant, utc_now
from .integrations.typesafe_decisions import DecisionClient

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


def _delivery_kind(message: EmailMessage, sender: str) -> str:
    """'delivery' for a delivery or delay notice, from any sender; 'receipt' for a read receipt; else ''."""
    content_type = str(message.get("Content-Type", "")).casefold()
    if "multipart/report" in content_type and "disposition-notification" in content_type:
        return "receipt"
    if sender.split("@", 1)[0] in MAILER_DAEMONS or message.get("X-Failed-Recipients"):
        return "delivery"
    if "multipart/report" in content_type:
        return "delivery"
    return ""


def _link_hosts(message: EmailMessage) -> set[str]:
    return {host for host in (host_of(url) for url in URL.findall(body_text(message, whole=False))) if host}


def _job_mail(message: EmailMessage, sender: str) -> bool:
    """Whether a job system had a hand in it: its sender, return path or signer is an applicant, assessment or scheduling
    system, or it links to an applicant or assessment system (a LinkedIn or Calendly link in a signature does not count)."""
    hosts = {domain_of(sender), *(domain_of(address) for address in mail_message.addresses(message, "Return-Path"))}
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
    hosts = _link_hosts(message) | {domain_of(address) for address in mail_message.addresses(message, "Return-Path")}
    return any(marker in host for host in hosts for marker in _SALES_HOSTS)




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
        at = parse_app_instant(event["created_at"])
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
        if parse_app_instant(event["created_at"]):
            forms.setdefault(event["target_id"], []).append(parse_app_instant(event["created_at"]))
    marked: dict[str, list[datetime]] = {}
    for event in conn.execute(
        "SELECT target_id, created_at FROM outreach_events WHERE user_id=? AND event_type='status' AND to_status IN ('sent', 'followed_up')",
        (user_id,),
    ).fetchall():
        if parse_app_instant(event["created_at"]):
            marked.setdefault(event["target_id"], []).append(parse_app_instant(event["created_at"]))
    account = mailbox_key(sender_account())
    own = own_domains()
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
        addresses = {address for address in addresses if mailbox_key(address) != account}
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
        site = site_domain(row["website"] or "", own, row["company"])
        domains: dict[str, bool] = {site: website_strength(row["website"] or "", site, row["company"])} if site else {}
        try:
            mail_domains = [str(other).casefold() for other in json.loads(row["mail_domains_json"] or "[]")]
        except (TypeError, ValueError):
            mail_domains = []
        for other in mail_domains:
            if other and other not in FREEMAIL and not is_institution(other) and not is_own(other, own):
                domains[other] = True
        aliases = {university_alias(address) for address in addresses} - {""}
        company = {address for address in addresses if address in primary or any(
            domain_of(address) == own_domain or domain_of(address).endswith(f".{own_domain}") for own_domain in domains)}
        outsiders = {mailbox_key(address) for address in addresses - company}
        for field in ("contact_email", "contact_cc"):
            domain, strong = contact_domain(str(row[field] or ""), site, own, row["company"])
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
            "mailboxes": {mailbox_key(address) for address in company} | aliases, "outsiders": outsiders, "aliases": aliases,
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
    return mailbox_key(sender) in target["mailboxes"] or _at_domain(domain_of(sender), target) is not None


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
    mailbox, domain = mailbox_key(sender), domain_of(sender)
    names = {mailbox, university_alias(sender)} - {""}
    for how, matches in (
        ("address", [target for target in by_latest if names & target["mailboxes"]]),
        ("domain", [target for target in by_latest if domain and _at_domain(domain, target) is not None]),
        ("outsider", [target for target in by_latest if mailbox in target["outsiders"]]),
    ):
        if matches:
            return matches, how
    # Relayed: an applicant system or LinkedIn writes for someone whose Reply-To is at the company.
    others = {mailbox_key(address) for address in mail_message.addresses(message, "Reply-To", "Sender")} - {mailbox}
    matches = [target for target in by_latest if others & target["mailboxes"]
               or any(_at_domain(domain_of(other), target) is not None for other in others)]
    return (matches, "reply_to") if matches else ([], "")


def _named(watched: list[dict[str, Any]], message: EmailMessage, text: str) -> list[dict[str, Any]]:
    """Companies a message names: the exact subject of the email to them (when no other company's shares it), or their name."""
    subject = " ".join(str(message.get("Subject", "")).split()).casefold()
    words = f" {normalized(f'{subject} {text}')} "
    shared = _shared_subjects(watched)
    found = []
    for target in sorted(watched, key=lambda item: item["last"], reverse=True):
        own = target["subject"].casefold()
        name = company_words(target["company"])
        single = len(name.split()) == 1
        written = str(target["company"]).split(",")[0].strip()
        named = f" {name} " in words and (not single or bool(re.search(rf"\b{re.escape(written)}\b", f"{message.get('Subject', '')} {text}")))
        if (own and own not in shared and own in subject) or (is_distinctive(target["company"]) and named):
            found.append(target)
    return found


def _shared_subjects(watched: list[dict[str, Any]]) -> set[str]:
    seen: dict[str, int] = {}
    for target in watched:
        if target["subject"]:
            seen[target["subject"].casefold()] = seen.get(target["subject"].casefold(), 0) + 1
    return {subject for subject, count in seen.items() if count > 1}


def _addressed(message: EmailMessage, account: str) -> bool:
    """Whether the student was in its To or Cc line, not only a blind copy."""
    recipients = {mailbox_key(address) for address in mail_message.addresses(message, "To", "Cc")}
    if account:
        return mailbox_key(account) in recipients
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
    labels: list[str], received_at: datetime, text: str, thread_id: str,
) -> tuple[str, str]:
    """What a message found for a company is (REPLY, AUTOMATIC, POSSIBLE or IGNORED), and the reason code (outreach_replies.REPLY_REASONS).

    Only plain machine mail is set aside. What is plainly someone at the
    company answering is a reply. Anything in between is a possible reply,
    which the student sees and settles, and which holds every automatic step
    meanwhile. See the module docstring.

    ``thread_id`` is the Gmail thread the message was found in.
    """
    target = targets[0]
    local = sender.split("@", 1)[0]
    subject = str(message.get("Subject", ""))
    if (account and mailbox_key(sender) == mailbox_key(account)) or "SENT" in labels or "DRAFT" in labels:
        return IGNORED, "own"
    delivery = _delivery_kind(message, sender)
    if delivery:
        return (AUTOMATIC, "receipt") if delivery == "receipt" else (IGNORED, "delivery")
    if "TRASH" in labels:
        return IGNORED, "trash"
    # Someone at the company writing through a relay (an applicant system, LinkedIn): its Reply-To. That lets the
    # message be shown, never counted: counting rests on the sender's own address.
    relayed = any(is_person(other, target["company"]) and _at_company(other, target)
                  for other in mail_message.addresses(message, "Reply-To", "Sender"))
    own_person = is_person(sender, target["company"])
    person = own_person or relayed
    if how == "thread":
        sent = target["threads"].get(thread_id, None)
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
            if not own_person and is_machine_local(local):
                return POSSIBLE, "automated_sender"
            return REPLY, "thread"
        automated = is_automatic(message) or (
            not person and (is_machine_local(local) or listed(domain_of(sender), tuple(sender_lists())) or not is_person(sender))
            and _acknowledged(target, sender, subject, text, received_at)
        )
        return (AUTOMATIC, "acknowledgement") if automated else (POSSIBLE, "thread_outsider")
    if all(received_at < candidate["since"] for candidate in targets):
        near = target["since_is_date"] and received_at >= target["since"] - timedelta(days=14)
        if near and (how == "address" or (how in {"domain", "reply_to"} and person)):
            return POSSIBLE, "before_marked_sent"
        return IGNORED, "before"
    if how == "name":
        if is_bulk_or_generated(message) or not person or not _addressed(message, account) or is_automatic(message):
            return IGNORED, "no_company"
    elif is_bulk_or_generated(message):
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
        if not (person and how == "domain" and _at_domain(domain_of(sender), target)):
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
    if is_machine_local(local):
        # An automated sender at the company is shown when it answers something or follows a contact form
        # (an interview invitation from no-reply@ their applicant system is job mail, above); account mail
        # from a big company's no-reply@ (a security code) is set aside.
        shown = _addressed(message, account) and (answers_something(message) or targets[0]["via_form"])
        return (POSSIBLE, "automated_sender") if shown else (IGNORED, "automated_sender")
    if not person:
        return POSSIBLE, "shared_address"
    if _sales_tool(message):
        return POSSIBLE, "mailing_tool"
    if not _at_domain(domain_of(sender), targets[0]):
        return POSSIBLE, "weak_domain"
    if not verified:
        return POSSIBLE, "not_verified"
    if not _addressed(message, account):
        return POSSIBLE, "not_addressed"
    if not name_matches_address(display, sender, targets[0]["company"]):
        return POSSIBLE, "name_mismatch"
    return REPLY, "domain_person"


# --- Recording ------------------------------------------------------------------------


def _stored(conn: sqlite3.Connection, user_id: str, gmail_id: str) -> Any:
    """The (kind, rules) row kept for a message read before, or None."""
    return conn.execute(
        "SELECT kind, rules FROM outreach_inbox_messages WHERE user_id=? AND gmail_id=?", (user_id, gmail_id)
    ).fetchone()


def _settled(row: Any) -> bool:
    """Whether a stored row means read already and judged under these rules (a row older rules set aside is read again)."""
    return row is not None and not (row[0] in REJUDGED and int(row[1] or 0) < RULES)


def _seen(conn: sqlite3.Connection, user_id: str, gmail_id: str) -> bool:
    """Read already, and judged under these rules (a row older rules set aside is read again)."""
    return _settled(_stored(conn, user_id, gmail_id))


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
# None when they could not all be read), which outreach_reply_senders.thank_you_blockers checks before anything
# is sent on its own.
REPLY_META = {"thread_id", "message_id", "subject", "from_name", "full_text", "reply_to", "headers", "link_hosts"}


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
        "link_hosts": lambda: link_hosts_or_none(message),
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
        # How it was matched to the company (outreach_replies.REPLY_REASONS). A thank-you answers only a reply in the
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
            log_event(conn, target["id"], user_id, "reply_logged", detail=text, data=data)
            # They wrote again: a thank-you after their earlier decline that has not gone stops now.
            outreach_callbacks.on_new_reply(conn, target["id"], user_id)
        if reason and claim is None:
            log_event(conn, target["id"], user_id, "reply_found",
                 detail=_found_words(reason, sender, {"addresses": addresses if addresses is not None else target.get("addresses")}))
        if notify:
            # Said outside the page too (and on the desktop when the student turned that on). Never an address or words.
            day = parse_app_instant(received)
            title = f"{target['company']} replied" + (
                f" on {day:%b} {day.day} (found late)" if late and day else ""
            )
            automation.insert_notice(conn, user_id, event_key=f"outreach-reply:{gmail_id}", level="info", title=title,
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
            log_event(conn, owner["id"], user_id, "possible_reply",
                 detail=f"{sender}: “{subject or '(no subject)'}”. Not counted as a reply until you say, "
                        f"because {reply_reason(reason, sender)}.")
        if notify:
            title = f"{target['company']} may have replied" if len(targets) == 1 else "A company you wrote to may have replied"
            body = "Open Outreach to check it." + (" Gmail put it in Spam." if in_spam else "")
            automation.insert_notice(conn, user_id, event_key=f"outreach-possible-reply:{gmail_id}", level="info",
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
            log_event(conn, target_id, user_id, "possible_reply_confirmed",
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
            log_event(conn, target_id, user_id, "possible_reply_dismissed",
                 detail=f"{sender}: “{row['subject'] or '(no subject)'}”. You said it is not a reply.")
    return get_target(conn, target_id, user_id=user_id, include_events=True)


# --- Looking in Gmail -----------------------------------------------------------------

# Pages of search results read per check; a search with more goes on from there on the next check.
MAX_PAGES = 10


def _search(gmail: GmailClient, user_id: str, query: str, *, page_size: int = 100) -> tuple[list[dict[str, Any]], int, bool]:
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
    terms += [f'"{company_words(target["company"])}"' for target in chunk if is_distinctive(target["company"])]
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
        stored = get_setting(conn, user_id, SWEEP_SETTING)
        try:
            mark = json.loads(stored) if stored is not None else {}
        except (TypeError, ValueError):
            mark = {}
        last = parse_app_instant(mark.get("at")) if isinstance(mark, dict) and mark.get("rules") == RULES else None
    start = last - timedelta(days=1) if last else min(sent)
    return max(start, min(sent) - timedelta(minutes=5), now - REPLY_WINDOW)


def _swept(conn: sqlite3.Connection, user_id: str, now: datetime) -> None:
    """Note a sweep that listed and read everything since its start."""
    with _MEMORY_LOCK:
        _LAST_SWEEP[user_id] = now
    with conn:
        put_setting(
            conn, user_id, SWEEP_SETTING, json.dumps({"at": now.isoformat(timespec="seconds"), "rules": RULES}), utc_now(),
        )


def _activated_at(conn: sqlite3.Connection) -> datetime | None:
    """When these rules started: mail from before then was judged by older ones, so it is never counted without the student."""
    row = conn.execute("SELECT applied_at FROM schema_migrations WHERE name=?", (MIGRATION,)).fetchone()
    return parse_app_instant(row[0]) if row else None


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
    state = connection_state(connector_row(conn, user_id))
    if state != "connected":
        return {**result, "state": state}
    account = sender_account().casefold()
    activated = _activated_at(conn)
    budget = [READ_BUDGET]
    late: list[dict[str, Any]] = []

    def read(gmail: GmailClient, gmail_id: str, thread_hint: str = "", *, by_name: bool = False, exempt: bool = False) -> bool:
        """Read one message found for a company, once, and log, show, note or set it aside.

        False when it was not read (the budget ran out, or Gmail did not answer), so a later check reads it.
        ``exempt`` reads are outside the budget: a sent thread's messages, and the forced company's mail.
        """
        if not gmail_id:
            return True
        stored = _stored(conn, user_id, gmail_id)
        if _settled(stored):
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
            message = email.message_from_bytes(mail_message.decode_base64url(raw), policy=policy.default)
            # No time from Gmail or the Date header: taken as arriving now, never as "before the first email".
            received_at = mail_message.received_or_none(data, message) or now
            display, sender = mail_message.sender(message)
            subject = " ".join(str(message.get("Subject", "")).split())[:300]
            text = reply_text(message)
            targets, how = _owner(watched, message, sender, thread_id)
            if not targets and by_name:
                targets, how = _named(watched, message, body_text(message, whole=False)), "name"
            kind, reason = _judge(message, targets=targets, how=how, sender=sender, display=display, account=account,
                                  labels=labels, received_at=received_at, text=text, thread_id=thread_id) if targets else (IGNORED, "no_company")
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
        # The row read above: a row that passed _settled as unsettled and exists is one older rules set aside.
        judged_before = stored is not None and int(stored[1] or 0) < RULES
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
                    log_event(conn, target["id"], user_id, "auto_reply", detail=f"{sender}: {' '.join(text.split())[:300]}")
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

    def read_thread(gmail: GmailClient, thread_id: str) -> bool:
        """Read one sent thread directly. False when it could not be read (then the check is not complete)."""
        response = gmail.request(
            "GET", f"/threads/{quote(thread_id, safe='')}",
            params=[("format", "metadata"), *(("metadataHeaders", name) for name in _THREAD_HEADERS)],
        )
        if response.status_code == 404:
            return True  # the thread was deleted: nothing left to find in it
        if response.status_code == 403:
            raise GmailNeedsReadScope
        if response.status_code != 200:
            result["state"] = "unreachable"
            return False
        complete = True
        for message in response.json().get("messages") or []:
            labels = message.get("labelIds") or []
            headers = header_map(message)
            if "SENT" in labels or "DRAFT" in labels or headers_say_failure(message):
                continue  # the student's own, or a delivery notice (outreach_delivery reads those)
            if account and mailbox_key(parseaddr(headers.get("from", ""))[1]) == mailbox_key(account):
                continue
            complete = read(gmail, str(message.get("id", "")), thread_id, exempt=True) and complete
        return complete

    def look(gmail: GmailClient) -> None:
        """Every look of one check, in order. Raises GmailNeedsReadScope when Gmail refuses a read."""
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

    def listing(gmail: GmailClient, query: str, *, page_size: int = 100) -> tuple[list[dict[str, Any]], bool]:
        """A search's results, and whether it read them all. A failed one leaves the check not ok."""
        references, failed, more = _search(gmail, user_id, query, page_size=page_size)
        if failed == 403:
            raise GmailNeedsReadScope
        if failed:
            # A search that failed found nothing yet; say so, so a send waiting on it holds.
            result["state"] = "unreachable"
        return references, not failed and not more

    outcome = result
    try:
        with client_factory() as client:
            look(GmailClient(conn, client, user_id))
    except (GmailNeedsReadScope, GmailAuthError):
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


