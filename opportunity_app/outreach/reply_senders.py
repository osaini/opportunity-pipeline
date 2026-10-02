"""Who a thank-you may go to: the rules R1 to R7 that a reply must pass, and what the student sent it in answer to.

A reply both readings call a decline can still be the wrong one to thank: a help desk's automatic acknowledgement, a job
system's rejection sent in a recruiter's name, a blast the student was Bcc'd on. Each rule reads the reply's own headers as
Gmail delivered them (mail_message.KEPT_HEADERS, kept on its reply_logged event); a reply whose headers are not on record, or
cannot be read, fails them all. ``thank_you_blockers`` applies them, and ``blocker_note`` and ``blocker_reason`` word the
result for the card and the log. ``sent_texts`` is what the student sent the company, which a reply quotes.

``mail_trust`` is used as a module (mail_trust.listed, mail_trust.sender_lists) and R7's ``authenticate`` is imported where
it is used, so a test that patches mail_trust.authenticate (or the others) is read at the call.
"""

from __future__ import annotations

import email
import logging
import re
import sqlite3
from datetime import timedelta
from email import headerregistry, policy
from email.message import EmailMessage
from typing import Any

from pipeline_core.identity import identity_tokens, normalized

from ..mail import trust as mail_trust
from .contact_names import GENERIC_LOCAL_PARTS, ROLE_INBOX_LOCAL_PARTS, ROLE_INBOX_QUALIFIERS, website_domain
from ..core.json_values import json_dict
from ..mail.message import hosts_in, is_automatic
from .config import sender_account
from .contacts import is_shared_inbox, made_of
from .forms import ALWAYS_AUTOMATIC
from .gmail import SENT_EVENT
from ..core.timestamps import parse_app_instant

# One logger for the whole thank-you feature (what its tests and the student's log filters name), whichever module logs.
LOGGER = logging.getLogger("opportunity_app.outreach.thank_you")

# Read with separators and digits gone (no_reply): "no.reply", "do_not_reply", "noreply-jobs", "bounces2".
_NO_REPLY = re.compile(r"^(?:(?:no|donot|dont)reply[a-z]*|bounce[a-z]*|notifications?|mailerdaemon|postmaster)$")


def sent_texts(conn: sqlite3.Connection, target: dict[str, Any], user_id: str) -> list[str]:
    """What the student sent them, which a reply quotes: the first email, and the follow-up when one went."""
    texts = [str(target.get("email_body") or "")]
    if target.get("follow_up_body") and followed_up(conn, target["id"], user_id):
        texts.append(str(target["follow_up_body"]))
    return [text for text in texts if text.strip()]


# The card's words for each rule, after NOT_THANKED. Plain and short.
BLOCKER_WORDS = {
    "headers": "its email headers are missing or could not be read",
    "R1": "it was not in your thread or from the address you wrote to",
    "R2": "it was found more than a day after it arrived",
    "account": "your sending address (PIPELINE_OUTREACH_ACCOUNT) is not set, so it could not be confirmed as addressed to you",
    "R3": "it was not addressed to you",
    "R4": "sent by an automated system",
    "R5": "sent through a job application system",
    "links": "the links in it could not all be read",
    "R6": "sent from a shared inbox, not a person",
    "R7": "Gmail could not confirm who sent it",
}
NOT_THANKED = "Not thanked automatically"
# A reply the app found later than this after it arrived (a re-check surfacing old mail) is never thanked.
DETECTION_LIMIT = timedelta(hours=24)
# Links in the body that mark job-system mail (mail_trust's shipped list). Not job boards or scheduling tools:
# a recruiter's own signature links LinkedIn, and a person may offer a Calendly link.
_LINK_CATEGORIES = ("ats", "assessment", "applicant_tracking")
# The one category of the shipped list that is no job system: documentation domains.
_NOT_JOB_SYSTEMS = {"reserved"}
_BULK_PRECEDENCE = {"bulk", "list", "junk"}
# Any of these, with any value, says a system sent it.
_SYSTEM_HEADERS = ("X-Auto-Response-Suppress", "List-Unsubscribe", "List-Id")
_HEADER_NAME = re.compile(r"[A-Za-z0-9-]+")
_BARE_LINE = re.compile(r"\r(?![\n \t])|\r?\n(?![ \t])")
_DKIM_DOMAIN = re.compile(r"(?:^|;)\s*d\s*=\s*([^;\s]+)", re.IGNORECASE)
_QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"')
_COMMENT = re.compile(r"\([^()]*\)")
_ANGLE = re.compile(r"<([^<>]*)>")
_MAILBOX = re.compile(r"""[^\s@<>()\[\]",;:]+@[^\s@<>()\[\]",;:']+""")
# An address that is a group: its name (no "@" or "<", so never a mailbox), ":", its members, ";", and whatever
# follows up to the next comma.
_GROUP = re.compile(r"[^<>@,;:\[\]]*:([^;]*);([^,]*)")
# Words that join a company's name into an inbox of its own ("joinacme", "workatacme", "teamacme").
_COMPANY_INBOX_WORDS = frozenset({"join", "work", "at", "with", "the", "go", "get", "meet", "hi", "hey"})


def blocker_note(blockers: list[str]) -> str:
    """The card's note for a reply not thanked on its own: the first rule it failed, in plain words."""
    return f"{NOT_THANKED}: {BLOCKER_WORDS.get(blockers[0], 'it did not pass the checks')}"


def blocker_reason(blockers: list[str]) -> str:
    """The debug log's words: the note, and every rule that failed by name."""
    return f"{blocker_note(blockers)} (failed: {', '.join(blockers)})"


def _blanked(text: str) -> str | None:
    """An address header with its quoted strings and comments blanked out; None when one is left open."""
    text = _QUOTED.sub(" ", text)
    for _ in range(5):  # nested comments, innermost first
        stripped = _COMMENT.sub(" ", text)
        if stripped == text:
            break
        text = stripped
    return None if any(mark in text for mark in '"()') else text


def _unreadable_address(raw: str) -> bool:
    """An address header with a quote or comment left open, or a group followed by more than a comma or its end.

    Python before 3.14.7 raises on such a group ("undisclosed-recipients:;;",
    "team: a@b.com; c@d.com") and later versions read what follows as another
    address, so reply_headers holds address headers to this rather than to
    the parser. A group's own whitespace and comments may follow it, though
    not an empty group's written with nothing between ":" and ";" (Python
    does not skip them there), and a quoted string never may. What follows a
    mailbox, as in "a@b.com; c@d.com", is part of it.
    """
    text = _blanked(_QUOTED.sub("q", "".join(raw.splitlines())))
    if text is None:
        return True
    rest = _ANGLE.sub("<>", text)
    while True:
        group = _GROUP.match(rest)
        if group:
            members, tail = group.groups()
            if tail.strip() if members else tail:
                return True
            end = group.end()
        else:
            end = rest.find(",")
            if end < 0:
                return False
        rest = rest[end + 1:]


def reply_headers(data: dict[str, Any]) -> EmailMessage | None:
    """A reply's kept headers as a message with no body, or None when they are not on record or cannot be read.

    Every header is parsed here (Python parses one only when it is first
    read), so one that cannot be fails closed here instead of raising in a
    rule. An address header also fails closed on a quote or comment left
    open, or a group with more after it (``_unreadable_address``), whether or
    not this Python can parse it, so every version reads a reply the same way.
    """
    pairs = data.get("headers")
    if not isinstance(pairs, list) or not pairs:
        return None
    lines = []
    for pair in pairs:
        if not (isinstance(pair, list) and len(pair) == 2 and all(isinstance(item, str) for item in pair)):
            return None
        name, value = pair
        # A line that does not continue its header would be read as another header.
        if not _HEADER_NAME.fullmatch(name) or _BARE_LINE.search(value):
            return None
        lines.append(f"{name}: {value}")
    try:
        message = email.message_from_string("\n".join(lines) + "\n\n", policy=policy.default)
        for (_name, value), (_raw_name, raw) in zip(message.items(), message.raw_items()):
            str(value)
            if isinstance(value, headerregistry.AddressHeader) and _unreadable_address(str(raw)):
                return None
        return message
    except Exception:  # noqa: BLE001 - headers that cannot be read fail closed
        return None


def mailboxes(message: EmailMessage, *names: str) -> set[str] | None:
    """The mailboxes the named address headers deliver to, casefolded; None when one cannot be read.

    Read from each header as it arrived rather than from Python's strict
    parse, which drops or mangles forms real mail uses ("'a@b.com'
    <a@b.com>", a semicolon list, "Name [Team] <a@b.com>", "<mailto:a@b.com>")
    and reads "a@b.com <c@d.com>" as a@b.com. A mailbox is the address in
    angle brackets when a part has one, whatever its display name says and
    however much that looks like an address; otherwise the part itself when
    it is exactly an address. Quoted strings and comments are display text
    and never count, and a group's name is not a mailbox
    ("undisclosed-recipients:;" has none). A quote or comment left open
    means nothing after it can be told apart from a name: None.
    """
    wanted = {name.casefold() for name in names}
    found: set[str] = set()
    for name, raw in message.raw_items():
        if str(name).casefold() not in wanted:
            continue
        text = _blanked(" ".join(str(raw).split()))
        if text is None:
            return None
        for part in re.split(r"[,;]", text):
            angles = _ANGLE.findall(part)
            outside = _ANGLE.sub(" ", part)
            if len(angles) > 1 or "<" in outside or ">" in outside:
                continue  # not one clear mailbox, so it never counts as the student
            address = angles[0].strip() if angles else outside.rsplit(":", 1)[-1].strip()
            if angles and address.casefold().startswith("mailto:"):
                address = address[len("mailto:"):].strip()
            if _MAILBOX.fullmatch(address):
                found.add(address.casefold())
    return found


def _job_system(host: str, categories: tuple[str, ...]) -> bool:
    """A host, or its registrable domain, on one of ``categories`` of mail_trust's shipped list."""
    text = str(host or "").strip().strip("<>").rsplit("@", 1)[-1].rstrip(".").casefold()
    return bool(text) and bool(mail_trust.listed(text, categories) or mail_trust.listed(mail_trust.registrable_domain(text) or "", categories))


def _job_system_mail(domain: str, target: dict[str, Any]) -> bool:
    """A domain a job system sends, relays or signs mail from: any list of mail_trust's shipped list but
    documentation domains (job boards and applicant-tracking systems included), unless it is the company's
    own (a reply from someone at LinkedIn or ADP, when LinkedIn or ADP is who they wrote to).

    The company's own only when both its website and its name say so: a
    careers page on a job system (website acme.bamboohr.com) is not Acme's
    domain, and a company named like one ("Lever Industries") does not own it.
    """
    categories = tuple(category for category in mail_trust.sender_lists() if category not in _NOT_JOB_SYSTEMS)
    if not _job_system(domain, categories):
        return False
    company = str(target.get("company") or "")
    own = identity_tokens(company) | {"".join(normalized(company).split())}
    found = mail_trust.registrable_domain(str(domain).strip().strip("<>").rsplit("@", 1)[-1]) or ""
    site = mail_trust.registrable_domain(website_domain(str(target.get("website") or ""))) or ""
    return not (found and found == site and found.split(".", 1)[0] in own)


def no_reply(local: str) -> bool:
    """A local part that takes no replies: no-reply, no.reply, do.not.reply, noreply-jobs, bounces2."""
    return bool(_NO_REPLY.match(re.sub(r"[^a-z]", "", str(local or "").casefold().split("+", 1)[0])))


def _company_inbox(local: str, target: dict[str, Any]) -> bool:
    """A local part that is the company's own name or slug, alone or with role words or a word that joins it
    ("acme", "acme-robotics", "acme.careers", "acmecareers", "careersacme", "teamacme", "joinacme")."""
    company = str(target.get("company") or "")
    tokens = identity_tokens(company)
    compact = "".join(re.split(r"[^a-z0-9]+", str(local or "").casefold().split("+", 1)[0]))
    if not compact:
        return False
    names = {"".join(word for word in normalized(company).split() if word in tokens), "".join(normalized(company).split())}
    site = mail_trust.registrable_domain(website_domain(str(target.get("website") or ""))) or ""
    names.add(site.split(".", 1)[0])
    names.discard("")
    # The company's own words. One of two letters ("of") is part of a name, never a sign of one on its own.
    pieces = names | {token for token in tokens if len(token) >= 3}
    words = pieces | tokens | GENERIC_LOCAL_PARTS | ROLE_INBOX_LOCAL_PARTS | ROLE_INBOX_QUALIFIERS | _COMPANY_INBOX_WORDS
    forms = {compact, re.sub(r"\d+", "", compact)} - {""}
    return any(form in names | tokens or made_of(form, words, pieces) for form in forms)


def thank_you_blockers(conn: sqlite3.Connection, target: dict[str, Any], reply: dict[str, Any]) -> list[str]:
    """The rules a reply fails for a thank-you to go on its own: [] when it passes them all.

    ``reply`` is one of ``replies()``. Every rule must hold:

    - R1: in the student's thread (the Gmail thread of an email the app sent
      them), or from the contact's own address or the Cc. A reply matched only
      by the company's domain fails. A reply read under outreach_inbox's reply
      rules says how it was matched (data "reason", outreach_replies.REPLY_REASONS):
      it must also be "thread" or "written_to", so one the student confirmed
      from a possible reply ("confirmed") never passes.
    - R2: logged at most DETECTION_LIMIT after Gmail received it.
    - R3: the student's sending address (PIPELINE_OUTREACH_ACCOUNT) is a
      mailbox in To or Cc (``mailboxes``); Bcc only, undisclosed recipients,
      or the address only as a display name, fails. "account" instead when
      no sending address is set, so nothing can be confirmed.
    - R4: written by a person: Auto-Submitted absent or "no"; no
      X-Auto-Response-Suppress, List-Unsubscribe or List-Id; no Precedence
      bulk, list or junk; not an automatic reply (mail_message.is_automatic).
    - R5: not job-system mail: the From, Sender, Return-Path and every DKIM
      d= domain, by registrable domain, are on no list of mail_trust's
      shipped list but documentation domains (ats, assessment, scheduling,
      job boards and applicant-tracking systems), unless it is the company's
      own name; and no link in it is to an ats, assessment or
      applicant-tracking host, leaving out hosts that the student's own email
      to them links (which a reply quotes). "links" when its links could not
      all be read.
    - R6: from one person: not a shared, role or no-reply inbox
      (outreach_contacts.is_shared_inbox, however its words are joined), and
      not the company's own name, alone or with role words.
    - R7: Gmail itself vouches for the sender (mail_trust.authenticate).

    "headers" when its headers are not on record or one cannot be read:
    every rule that reads them fails closed.
    """
    from ..mail.trust import authenticate

    data = reply["data"]
    sender = str(data.get("from") or "").strip().casefold()
    failed: list[str] = []
    # R1: the thread, or the exact address.
    threads = {
        str(json_dict(row["detail"]).get("thread_id") or "") for row in conn.execute(
            "SELECT detail FROM outreach_events WHERE target_id=? AND user_id=? AND event_type=?",
            (target["id"], target["user_id"], SENT_EVENT),
        ).fetchall()
    } - {""}
    written_to = {str(target.get(field) or "").strip().casefold() for field in ("contact_email", "contact_cc")} - {""}
    # Both must agree: the match outreach_inbox made (a reply logged before its reply rules has none), and the records.
    matched = data.get("reason") in ("thread", "written_to") if "reason" in data else True
    if not matched or not ((data.get("thread_id") and str(data["thread_id"]) in threads) or (sender and sender in written_to)):
        failed.append("R1")
    # R2: found within a day of arriving.
    detected, arrived = parse_app_instant(reply.get("created_at")), parse_app_instant(data.get("received_at"))
    if detected is None or arrived is None or detected - arrived > DETECTION_LIMIT:
        failed.append("R2")
    unreadable = [*failed, "headers"]
    message = reply_headers(data)
    if message is None:
        return unreadable
    try:
        # R3: to the student, in To or Cc.
        student = sender_account().strip().casefold()
        recipients = mailboxes(message, "To", "Cc")
        if recipients is None:
            return unreadable
        if not student:
            failed.append("account")
        elif student not in recipients:
            failed.append("R3")
        # R4: a person wrote it.
        auto = str(message.get("Auto-Submitted", "") or "").split(";", 1)[0].strip().casefold()
        precedence = str(message.get("Precedence", "") or "").strip().casefold()
        if (auto not in ("", "no") or any(message.get_all(name) is not None for name in _SYSTEM_HEADERS)
                or precedence in _BULK_PRECEDENCE or is_automatic(message) or ALWAYS_AUTOMATIC.search(str(reply.get("text") or ""))):
            failed.append("R4")
        # R5: no job system sent it, relayed it, signed it, or is linked in it.
        return_paths, relays = mailboxes(message, "Return-Path"), mailboxes(message, "Sender")
        if return_paths is None or relays is None:
            return unreadable
        signers = [found for value in message.get_all("DKIM-Signature") or []
                   for found in _DKIM_DOMAIN.findall(" ".join(str(value).split()))]
        hosts = data.get("link_hosts")
        readable = isinstance(hosts, list) and all(isinstance(host, str) for host in hosts)
        # A link the student's own email has is theirs, quoted back: never a sign of who sent the reply.
        theirs = hosts_in("\n".join(sent_texts(conn, target, target["user_id"]))) if readable else set()
        if (not return_paths or any(_job_system_mail(domain, target) for domain in (sender, *relays, *return_paths, *signers))
                or (readable and any(_job_system(host, _LINK_CATEGORIES) for host in hosts if host not in theirs))):
            failed.append("R5")
        if not readable:
            failed.append("links")
        # R6: one person's own address.
        local = sender.split("@", 1)[0]
        if not local or is_shared_inbox(sender) or no_reply(local) or _company_inbox(local, target):
            failed.append("R6")
        # R7: Gmail vouches for who sent it.
        verdict = authenticate(message)
        if not verdict.ok or verdict.from_address != sender:
            failed.append("R7")
    except Exception:  # noqa: BLE001 - a header that cannot be read fails closed, and never ends a pass
        LOGGER.debug("Could not read the headers of a reply for outreach target %s", target.get("id"), exc_info=True)
        return unreadable
    return failed


def followed_up(conn: sqlite3.Connection, target_id: str, user_id: str) -> bool:
    for row in conn.execute(
        "SELECT event_type, to_status, detail FROM outreach_events WHERE target_id=? AND user_id=? AND event_type IN (?, 'status')",
        (target_id, user_id, SENT_EVENT),
    ).fetchall():
        if row["event_type"] == "status" and row["to_status"] == "followed_up":
            return True
        if row["event_type"] == SENT_EVENT and json_dict(row["detail"]).get("kind") == "follow_up":
            return True
    return False
