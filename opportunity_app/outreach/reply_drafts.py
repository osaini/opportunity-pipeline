"""The student's answer to a company's reply, drafted for them to read, approve and send (reply_drafts).

When a company writes back, the app writes the student's answer and keeps it on the company's card: thanks and the
timing for a "get back in touch later", a door a decline left open, a call they offered (with the student's own times
left in "[your availability]"), or a question (with whatever only the student can say left in brackets). When a revisit
date the student set arrives, it writes the note to send that day. Nothing goes out on its own: the student approves
it, and sends it with the same two presses as any email, in the company's thread.

Which replies get one automatically (``answer_due``, from the AutomationWorker, only while the switch is on and
automation is not paused): the company's latest reply, from a person (not a no-reply address), read from Gmail or pasted,
within REPLY_WINDOW; not an offer, by the rules or Jev; no email from them waiting as a possible reply; nothing sent to
them after it (outreach_thank_you.sent_since); no thank-you planned for it, and not a plain decline the thank-you after a
decline will answer on its own; and, when Gmail can be read, no answer of the student's (sent or a draft) in the thread
after it. Each reply is answered once: an answer dismissed or undone is not written again. Revisit notes
(``revisit_due``) go to companies whose revisit date has come, with no answer still waiting and nothing sent since.

What it says is outreach_reply_writing: the student's own facts and the thread, with the in-person line for a company
near them, checked in plain code before it is offered. A date their reply names for getting back in touch ("around March
of next year") is offered as the revisit date, with their words, for the student to use or not (``use_revisit_date``).

Before it goes (``send``) the records are read again and so is Gmail's thread: a newer message from them, anything the
student sent meanwhile, a bounce, or the company set aside stops it with why. The one Gmail call is made under its own
claim (outreach_send_claims, kind 'reply:<draft_id>'), settled as every send's is, so it goes at most once.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable
from uuid import uuid4
from urllib.parse import quote

import httpx

from ..automation import ledger as automation
from ..automation.background import record_health_quietly, step_error
from ..core.database import rollback_quietly
from ..core.timestamps import parse_app_instant, utc_now
from ..integrations.gmail_client import GmailAuthError, GmailThrottled
from ..mail.classifiers import MIN_CONFIDENCE
from ..mail.gmail_connection import GmailClient
from ..mail.message import written_between_quotes
from ..student.preparation import confirmed_facts
from . import thank_you as outreach_thank_you
from .config import sender_account
from .drafting import entry_name, outreach_proof
from .gmail import (
    REPLY_GMAIL_DRAFT_EVENT,
    REPLY_SENT_EVENT,
    SendNeedsCheckError,
    SendUnconfirmedError,
    draft_url,
    folder_check,
    gmail_drafts_status,
    post_under_claim,
    reply_claim_kind,
    reply_fingerprint,
    reply_mime,
    require_account,
    thank_you_row,
    thread_url,
)
from .greeting import greeting_line, greeting_style, spoken_company
from .location import user_regions
from .replies import REPLY_PATTERNS, suggest_reply_status
from .reply_senders import followed_up, mailboxes, no_reply, reply_headers, sent_texts
from .reply_writing import MAX_WORDS, READINGS, STUDENT_FIELDS, in_person, write
from .send_claims import IN_PROGRESS, SendConflictError, claim_reason, claimed_send, send_claim_held, send_claim_row, settle_send_claim
from .targets import (
    REPLY_DRAFT_BEHIND,
    REVISIT_STATUSES,
    OutreachNotFoundError,
    draft_checks,
    get_target,
    latest_reply_ids,
    list_targets,
    local_today,
    log_event,
    update_target,
)
from .thank_you_writing import recipient_name, reply_subject

LOGGER = logging.getLogger(__name__)

FEATURE = "reply_drafts"
HEALTH_COMPONENT = "outreach.reply_drafts"
# The switches the automation worker runs this for.
WORKER_FEATURES = (FEATURE,)
ACTION_TYPE = "outreach.reply_draft"
STATES = ("generated", "approved", "sending", "sent", "in_gmail", "dismissed", "stale", "skipped")
# An answer in one of these can still be changed, approved or sent.
OPEN_STATES = ("generated", "approved")
# Only a reply this recent is answered automatically; an older one is the student's to pick up (Write a reply).
REPLY_WINDOW = timedelta(days=21)
# Model calls are slow: at most this many answers are written per worker pass.
PER_PASS = 2
# A draft that could not be written is tried again after this.
RETRY_AFTER = timedelta(hours=6)
# Statuses whose latest reply may be answered: a reply read from Gmail moves a company to Replied and the student may
# have moved it on; one pasted in leaves it at Sent or Followed up until the student picks a status.
ANSWER_STATUSES = ("sent", "followed_up", "replied", "paused", "call_scheduled", "declined")
MAX_CC = 3

WRITTEN_EVENT = "reply_draft_written"
FAILED_EVENT = "reply_draft_failed"
EDITED_EVENT = "reply_draft_edited"
APPROVED_EVENT = "reply_draft_approved"
DISMISSED_EVENT = "reply_draft_dismissed"
STALE_EVENT = "reply_draft_stale"
SKIPPED_EVENT = "reply_draft_skipped"
REVISIT_EVENT = "revisit_date_from_reply"

ANSWERED_IN_GMAIL = "You already answered them in Gmail, so no reply was drafted"
DRAFT_IN_GMAIL = "You started a reply to them in Gmail, so no reply was drafted"
WROTE_AGAIN = "They wrote again in the thread since. Read their message, then write the reply again"
STUDENT_WROTE = "You already answered them in Gmail, so this was not sent"
SENT_SINCE = "Something went to them after their message (an email, a thank-you or a form), so this was not sent"
UNDONE = "Undone from the automation activity"

# Words that put a reply's reading toward what the student should answer, beyond the rules' first match
# (outreach_replies.REPLY_PATTERNS): a call in any words, a time that is too far off, a plain "not offering".
_CALL_HINT = re.compile(
    r"\b(are|would) you (be )?(free|available|around)\b|\bwhen (are|would) you\b|\b(call|chat|meet|meeting|zoom|coffee|talk)\b[^.!\n]{0,60}\?",
    re.IGNORECASE,
)
_LATER_HINT = re.compile(r"\b(a bit|bit|too|pretty|quite) (far|early|soon)\b|\bfar (out|off|away)\b|\btoo early\b", re.IGNORECASE)
_DECLINE_HINT = re.compile(r"\bnot (currently |actively )?(offering|hiring|taking on|accepting)\b", re.IGNORECASE)


class ReplyDraftChanged(ValueError):
    """The answer is no longer the one shown, or no longer answers their latest message."""


# --- Reading their reply ---------------------------------------------------------------


def own_words(item: dict[str, Any], sent: list[str]) -> str:
    """A reply's own words: above the email it quotes, and anything typed between its quoted lines."""
    between = written_between_quotes(str(item["data"].get("full_text") or ""), sent)
    return f"{item['text']}\n{between}".strip()


def reading_of(item: dict[str, Any], words: str) -> str:
    """What the student should answer: 'offer', or one of READINGS.

    Any reading of an offer (the rules or Jev) is an offer, left to the student. Otherwise a call or a meeting in any
    words comes first, then "later", then a no, then anything else; Jev's reading counts when it is sure enough.
    """
    readings = item["data"].get("readings") if isinstance(item["data"].get("readings"), dict) else {}
    rules = str((readings.get("rules") or {}).get("status") or suggest_reply_status(words)["status"])
    jev = readings.get("jev") if isinstance(readings.get("jev"), dict) else {}
    label = str(jev.get("label") or "")
    try:
        sure = float(jev.get("confidence")) >= MIN_CONFIDENCE
    except (TypeError, ValueError):
        sure = False
    if "offer" in (rules, label):
        return "offer"
    lowered = " ".join(words.split())
    found = {status for status, pattern, _reason in REPLY_PATTERNS if re.search(pattern, lowered.lower())}
    if sure and label in READINGS:
        found.add(label)
    if _CALL_HINT.search(lowered):
        found.add("call_scheduled")
    if _LATER_HINT.search(lowered):
        found.add("paused")
    if _DECLINE_HINT.search(lowered):
        found.add("declined")
    return next((status for status in ("call_scheduled", "paused", "declined") if status in found), "replied")


def _ident(item: dict[str, Any]) -> str:
    """The reply's Gmail id, or its event id when it was pasted (targets.latest_reply_ids names it the same way)."""
    return str(item["data"].get("gmail_id") or "") or str(item["id"])


def _recipients(conn: sqlite3.Connection, target: dict[str, Any], item: dict[str, Any] | None) -> tuple[str, str, str]:
    """(to, to's name, cc) for answering this reply, as a reply-all would: Reply-To when it names one other person, else
    whoever wrote; the reply's other recipients (not the student) in Cc. A pasted reply goes to the contact."""
    account = sender_account().casefold()
    if item is None or item["data"].get("source") != "gmail" or not item["data"].get("from"):
        return str(target.get("contact_email") or ""), str(target.get("contact_name") or ""), str(target.get("contact_cc") or "")
    data = item["data"]
    sender = str(data["from"]).strip()
    reply_to = [address.strip() for address in str(data.get("reply_to") or "").split(",") if address.strip()]
    to = reply_to[0] if len(reply_to) == 1 and reply_to[0].casefold() != account else sender
    name = recipient_name(str(data.get("from_name") or ""), to, target) if to.casefold() == sender.casefold() else ""
    others: list[str] = []
    headers = reply_headers(data)
    if headers is not None:
        for address in sorted(mailboxes(headers, "To", "Cc") or set()):
            if address in {account, to.casefold()} or no_reply(address.split("@", 1)[0]) or address in others:
                continue
            others.append(address)
    return to, name, ", ".join(others[:MAX_CC])


# --- Writing it ------------------------------------------------------------------------


def _your_emails(conn: sqlite3.Connection, target: dict[str, Any], user_id: str) -> list[dict[str, str]]:
    """What the student already sent them, oldest first: the first email, the follow-up, and earlier answers sent from here."""
    emails = []
    if target.get("email_body") and target.get("sent_at"):
        emails.append({"what": "first email", "sent_on": str(target["sent_at"])[:10], "subject": target.get("email_subject") or "",
                       "body": target["email_body"]})
    if target.get("follow_up_body") and followed_up(conn, target["id"], user_id):
        emails.append({"what": "follow-up", "subject": target.get("follow_up_subject") or "", "body": target["follow_up_body"]})
    for row in conn.execute(
        "SELECT detail_json, created_at FROM outreach_events WHERE target_id=? AND user_id=? AND event_type=? ORDER BY created_at",
        (target["id"], user_id, REPLY_SENT_EVENT),
    ).fetchall():
        try:
            data = json.loads(row["detail_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict) and data.get("body"):
            emails.append({"what": "your earlier reply", "sent_on": str(row["created_at"])[:10],
                           "subject": str(data.get("subject") or ""), "body": str(data["body"])})
    return emails


def _primary_experience(facts: dict[str, Any]) -> dict[str, Any] | None:
    proof, lead = outreach_proof(facts)
    if not lead:
        return None
    return next((entry for entries in proof.values() for entry in entries if entry_name(entry) == lead[0]), None)


def compose(
    conn: sqlite3.Connection, target_id: str, *, user_id: str, provider_factory: Callable[[str, str], Any] | None,
    provider: str | None = None, purpose: str = "answer", comments: str = "", current: str = "",
) -> dict[str, Any]:
    """Write an answer (or a revisit note) without saving it. Returns what save_tx stores.

    Raises ValueError (or RuntimeError from the model) when none can be written.
    """
    if purpose not in MAX_WORDS:
        raise ValueError("purpose must be answer or revisit")
    target = get_target(conn, target_id, user_id=user_id)
    found = outreach_thank_you.replies(conn, target_id, user_id)
    item = found[-1] if found else None
    if purpose == "answer" and item is None:
        raise ValueError("There is no reply from them to answer. Log their reply first")
    facts = confirmed_facts(conn, user_id)
    name = " ".join(str(facts.get("name") or "").split())
    if not name:
        raise ValueError("Confirm your name in your profile before writing a reply")
    sent = sent_texts(conn, target, user_id)
    words = own_words(item, sent) if item is not None else ""
    reading = reading_of(item, words) if item is not None else ""
    if purpose == "answer" and reading == "offer":
        raise ValueError("Their reply reads as an offer. Answer that one yourself")
    to, to_name, cc = _recipients(conn, target, item)
    if not to:
        raise ValueError("There is no address to answer: add a contact email first")
    student = {field: facts[field] for field in STUDENT_FIELDS if field in facts}
    student["name"] = name
    place = in_person(facts, target, user_regions(conn, user_id))
    if place:
        for field in ("break_location", "break_times"):
            if facts.get(field):
                student[field] = facts[field]
    company = {field: target[field] for field in ("website", "summary") if target.get(field)}
    if target.get("research_confidence") == "unverified":
        company.pop("summary", None)
    company["company"] = spoken_company(target["company"])
    today = local_today(conn, user_id)
    inputs: dict[str, Any] = {
        "purpose": purpose,
        "reading": reading or "replied",
        "today": today.isoformat(),
        "greeting": greeting_line(target["company"], to_name, greeting_style(conn, user_id)),
        "student": student,
        "company": company,
        "their_reply": {
            "from_name": str(item["data"].get("from_name") or "") if item else "",
            "received_on": item["at"].date().isoformat() if item else "",
            "text": words[:4_000],
        },
        "earlier_replies": [own_words(earlier, sent)[:1_500] for earlier in found[:-1][-3:]],
        "your_emails": _your_emails(conn, target, user_id),
        "in_person": place,
        "max_words": MAX_WORDS[purpose],
    }
    revisit_for = None
    if purpose == "revisit":
        revisit_for = str(target.get("follow_up_at") or "") or None
        inputs["revisit"] = {"date": revisit_for or ""}
        primary = _primary_experience(facts)
        if primary is not None:
            inputs["primary_experience"] = primary
    written = write(inputs, provider_factory=provider_factory, provider=provider, comments=comments, current=current)
    data = item["data"] if item is not None else {}
    gmail_thread = data.get("source") == "gmail" and data.get("thread_id")
    return {
        "purpose": purpose, "reading": reading,
        "reply_event_id": str(item["id"]) if item else "", "reply_gmail_id": _ident(item) if item else "",
        "reply_message_id": str(data.get("message_id") or "") if gmail_thread else "",
        "thread_id": str(data.get("thread_id") or "") if gmail_thread else "",
        "to_email": to, "to_name": to_name, "cc": cc,
        "subject": reply_subject(str(data.get("subject") or ""), str(target.get("email_subject") or "")),
        "body": written["body"], "claims_json": json.dumps(written["claims"], ensure_ascii=False),
        "generated_by": written["generated_by"], "comments": comments,
        "revisit_on": written["revisit_on"] or None, "revisit_said": written["revisit_said"], "revisit_for": revisit_for,
    }


def reply_draft_row(conn: sqlite3.Connection, target_id: str, user_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM outreach_reply_drafts WHERE target_id=? AND user_id=?", (target_id, user_id)).fetchone()
    return dict(row) if row is not None else None


def _same_message(row: dict[str, Any], prepared: dict[str, Any]) -> bool:
    return (row["purpose"], row["reply_gmail_id"], row["revisit_for"] or "") == (
        prepared["purpose"], prepared["reply_gmail_id"], prepared.get("revisit_for") or "",
    )


_COLUMNS = (
    "purpose", "reading", "reply_event_id", "reply_gmail_id", "reply_message_id", "thread_id", "to_email", "to_name", "cc",
    "subject", "body", "claims_json", "generated_by", "comments", "revisit_on", "revisit_said", "revisit_for",
)


def save_tx(conn: sqlite3.Connection, target_id: str, *, user_id: str, prepared: dict[str, Any]) -> dict[str, str]:
    """Store an answer compose wrote, inside a transaction the caller owns: {"draft_id", "fingerprint"}.

    A rewrite of the same message keeps its draft_id, so an earlier try Gmail may have carried out still stops a send;
    an answer to another message (or a revisit note for another date) gets a new one. One being sent, or already sent
    for this message, is not replaced.
    """
    stamp = utc_now()
    row = reply_draft_row(conn, target_id, user_id)
    same = row is not None and _same_message(row, prepared)
    if row is not None and row["state"] == "sending":
        raise ReplyDraftChanged("Your reply is being sent right now, so it was not rewritten")
    if same and row["state"] in ("sent", "in_gmail"):
        raise ReplyDraftChanged("Your reply to this message already went (or is in your Gmail Drafts), so it was not rewritten")
    draft_id = row["draft_id"] if same else uuid4().hex
    fingerprint = reply_fingerprint(
        prepared["to_email"], prepared["cc"], prepared["subject"], prepared["body"], prepared["reply_message_id"], prepared["thread_id"],
    )
    values = {column: prepared.get(column) for column in _COLUMNS}
    conn.execute(
        f"""
        INSERT INTO outreach_reply_drafts(target_id, user_id, draft_id, {', '.join(_COLUMNS)}, state, note, fingerprint,
            approved_at, sent_at, created_at, updated_at)
        VALUES(?, ?, ?, {', '.join('?' for _ in _COLUMNS)}, 'generated', '', ?, NULL, NULL, ?, ?)
        ON CONFLICT(target_id) DO UPDATE SET draft_id=excluded.draft_id,
            {', '.join(f'{column}=excluded.{column}' for column in _COLUMNS)},
            state='generated', note='', fingerprint=excluded.fingerprint, approved_at=NULL, sent_at=NULL,
            created_at=excluded.created_at, updated_at=excluded.updated_at
        """,
        (target_id, user_id, draft_id, *values.values(), fingerprint, stamp, stamp),
    )
    what = "revisit note" if prepared["purpose"] == "revisit" else "reply"
    log_event(conn, target_id, user_id, WRITTEN_EVENT, detail=f"A {what} to {prepared['to_email']} was written ({prepared['generated_by']})")
    return {"draft_id": draft_id, "fingerprint": fingerprint}


def _open_row(conn: sqlite3.Connection, target_id: str, user_id: str) -> dict[str, Any]:
    row = reply_draft_row(conn, target_id, user_id)
    if row is None:
        raise ReplyDraftChanged("There is no reply draft for this company")
    if row["state"] not in OPEN_STATES:
        raise ReplyDraftChanged(f"The reply is {row['state'].replace('_', ' ')}, so it can't be changed here")
    return row


def generate(
    conn: sqlite3.Connection, target_id: str, *, user_id: str, provider_factory: Callable[[str, str], Any] | None,
    provider: str | None = None, comments: str = "", purpose: str | None = None,
) -> dict[str, Any]:
    """Write a reply on request (Write a reply, or Rewrite with comments). Returns the company as its card shows it.

    The purpose is the one asked for; else the open draft's own; else a revisit note when the revisit date has come;
    else an answer to their latest reply.
    """
    comments = comments.replace("\r\n", "\n").strip()
    if len(comments) > 2_000:
        raise ValueError("Keep comments under 2,000 characters")
    target = get_target(conn, target_id, user_id=user_id)
    row = reply_draft_row(conn, target_id, user_id)
    current = ""
    if purpose is None:
        if row is not None and row["state"] in OPEN_STATES and not target["reply_draft"]["behind"]:
            purpose = row["purpose"]
        else:
            purpose = "revisit" if target["revisit_due"] else "answer"
    if row is not None and row["state"] in OPEN_STATES:
        current = f"Subject: {row['subject']}\n\n{row['body']}"
    prepared = compose(
        conn, target_id, user_id=user_id, provider_factory=provider_factory, provider=provider, purpose=purpose,
        comments=comments, current=current,
    )
    with conn:
        save_tx(conn, target_id, user_id=user_id, prepared=prepared)
    return get_target(conn, target_id, user_id=user_id)


def edit(conn: sqlite3.Connection, target_id: str, *, user_id: str, subject: str, body: str, fingerprint: str) -> dict[str, Any]:
    """Save the student's own words. An approved reply goes back for approval."""
    subject = " ".join(subject.split())[:300]
    body = body.replace("\r\n", "\n").strip()
    if not subject or not body:
        raise ValueError("A reply needs a subject and words")
    if len(body) > 20_000:
        raise ValueError("Keep the reply under 20,000 characters")
    get_target(conn, target_id, user_id=user_id)
    with conn:
        row = _open_row(conn, target_id, user_id)
        if row["fingerprint"] != fingerprint:
            raise ReplyDraftChanged("The reply changed since you opened it. Reload and make your change again")
        new = reply_fingerprint(row["to_email"], row["cc"], subject, body, row["reply_message_id"], row["thread_id"])
        if new != row["fingerprint"]:
            conn.execute(
                "UPDATE outreach_reply_drafts SET subject=?, body=?, fingerprint=?, state='generated', approved_at=NULL, note='', "
                "updated_at=? WHERE target_id=? AND user_id=?",
                (subject, body, new, utc_now(), target_id, user_id),
            )
            log_event(conn, target_id, user_id, EDITED_EVENT, detail="You edited the reply" + (
                "; it needs approving again" if row["state"] == "approved" else ""))
    return get_target(conn, target_id, user_id=user_id)


def approve(conn: sqlite3.Connection, target_id: str, *, user_id: str, fingerprint: str) -> dict[str, Any]:
    """Approve the reply as shown. Refused while a bracket is left to fill in, or when they wrote again since."""
    target = get_target(conn, target_id, user_id=user_id)
    with conn:
        row = _open_row(conn, target_id, user_id)
        if row["fingerprint"] != fingerprint:
            raise ReplyDraftChanged("The reply changed after it was shown. Reload and check it before approving")
        checks = draft_checks(row["subject"], row["body"])
        if checks["placeholders"]:
            raise ValueError("Fill these in before approving: " + ", ".join(checks["placeholders"]))
        if target["reply_draft"] and target["reply_draft"]["behind"]:
            raise ReplyDraftChanged(f"{REPLY_DRAFT_BEHIND}. Write it again")
        conn.execute(
            "UPDATE outreach_reply_drafts SET state='approved', approved_at=?, note='', updated_at=? WHERE target_id=? AND user_id=? "
            "AND state='generated'",
            (utc_now(), utc_now(), target_id, user_id),
        )
        log_event(conn, target_id, user_id, APPROVED_EVENT, detail=f"Approved your reply to {row['to_email']}")
    return get_target(conn, target_id, user_id=user_id)


def dismiss(conn: sqlite3.Connection, target_id: str, *, user_id: str) -> dict[str, Any]:
    """Put the reply away. It is not written again for the same message."""
    get_target(conn, target_id, user_id=user_id)
    with conn:
        moved = conn.execute(
            "UPDATE outreach_reply_drafts SET state='dismissed', note='You dismissed it', updated_at=? WHERE target_id=? AND user_id=? "
            "AND state IN ('generated', 'approved', 'stale', 'skipped')",
            (utc_now(), target_id, user_id),
        ).rowcount
        if moved:
            log_event(conn, target_id, user_id, DISMISSED_EVENT, detail="You dismissed the reply draft")
    return get_target(conn, target_id, user_id=user_id)


def use_revisit_date(conn: sqlite3.Connection, target_id: str, *, user_id: str) -> dict[str, Any]:
    """Make the date their reply named the company's revisit date. A company not marked Replied or Paused is marked Paused."""
    target = get_target(conn, target_id, user_id=user_id)
    row = reply_draft_row(conn, target_id, user_id)
    if row is None or not row["revisit_on"]:
        raise ValueError("Their reply names no date to get back in touch")
    values: dict[str, Any] = {"follow_up_at": row["revisit_on"]}
    if target["status"] not in REVISIT_STATUSES:
        values["status"] = "paused"
    updated = update_target(conn, target_id, values, user_id=user_id)
    with conn:
        log_event(conn, target_id, user_id, REVISIT_EVENT,
                  detail=f"Revisit on {row['revisit_on']}, from their words: “{row['revisit_said']}”")
    return updated


# --- Whether it may still go ------------------------------------------------------------


def _reply_item(conn: sqlite3.Connection, target_id: str, user_id: str, ident: str) -> dict[str, Any] | None:
    return next((item for item in outreach_thank_you.replies(conn, target_id, user_id) if _ident(item) == ident), None)


def problem_now(conn: sqlite3.Connection, target: dict[str, Any], row: dict[str, Any]) -> str:
    """What stops this reply going now, from the app's own records; "" when nothing does."""
    newest = latest_reply_ids(conn, target["user_id"], target["id"]).get(target["id"], "")
    if newest and newest != row["reply_gmail_id"]:
        return REPLY_DRAFT_BEHIND
    # An answer goes only if nothing went to them after their message; a revisit note, after it was written (the
    # student's earlier answer to that message is expected before it).
    item = _reply_item(conn, target["id"], target["user_id"], row["reply_gmail_id"]) if row["purpose"] == "answer" else None
    since = item["at"] if item is not None else parse_app_instant(row["created_at"])
    if since is not None and outreach_thank_you.sent_since(
        conn, target["id"], target["user_id"], since, own_kind=reply_claim_kind(row["draft_id"]),
    ):
        return SENT_SINCE
    bounced = set(target.get("bounced_addresses") or [])
    for address in [row["to_email"], *[part.strip() for part in str(row["cc"] or "").split(",") if part.strip()]]:
        if address.casefold() in bounced:
            return f"Email to {address} bounced, so the reply was not sent"
    if target.get("not_interested_at"):
        return "You set the company aside, so the reply was not sent"
    return ""


def _thread_news(gmail: GmailClient, row: dict[str, Any], after_ms: int) -> tuple[str, str]:
    """What Gmail's thread holds after their message: ("they" | "student" | "draft" | "", ""), or ("error", why)."""
    if not row["thread_id"]:
        return "", ""
    try:
        response = gmail.request("GET", f"/threads/{quote(row['thread_id'], safe='')}", params={"format": "minimal"})
    except GmailThrottled:
        return "error", "Gmail asked the app to slow down"
    except GmailAuthError:
        return "error", "Gmail needs to be reconnected"
    except httpx.HTTPError:
        return "error", "Gmail could not be reached"
    if response.status_code != 200:
        return "error", f"Gmail could not read their thread (HTTP {response.status_code})"
    try:
        messages = response.json().get("messages") or []
        if not after_ms:
            answered = next(message for message in messages if str(message.get("id")) == row["reply_gmail_id"])
            after_ms = int(answered.get("internalDate") or 0)
    except (ValueError, AttributeError, StopIteration, TypeError):
        return "error", "Their message was not found in its thread"
    news: set[str] = set()
    for message in messages:
        try:
            at = int(message.get("internalDate") or 0)
        except (TypeError, ValueError):
            return "error", "Gmail's thread could not be read"
        if at <= after_ms or str(message.get("id")) == row["reply_gmail_id"]:
            continue
        labels = message.get("labelIds") or []
        news.add("student" if "SENT" in labels else "draft" if "DRAFT" in labels else "they")
    return next((what for what in ("they", "student", "draft") if what in news), ""), ""


def _after_ms(row: dict[str, Any], item: dict[str, Any] | None) -> int:
    """From when Gmail's thread is read for news, in Gmail's milliseconds; 0 to start at their message, found in the thread.

    An answer starts at their message. A revisit note starts when it was written: the student's own earlier answer
    sits after their message, and is not news.
    """
    if row["purpose"] == "answer" and item is not None and item["data"].get("source") == "gmail":
        return 0
    stamp = parse_app_instant(row["created_at"])
    return int(stamp.timestamp() * 1000) if stamp else 0


def send(
    conn: sqlite3.Connection, target_id: str, *, user_id: str, fingerprint: str, client_factory: Callable[[], Any],
    sent_folder_check: str | None = None,
) -> dict[str, Any]:
    """The student's confirmed Send of an approved reply, once, in their thread. Returns what Gmail sent and the company.

    A revisit note moves the company to Followed up, as a follow-up does.
    """
    target = get_target(conn, target_id, user_id=user_id)
    with conn:
        row = reply_draft_row(conn, target_id, user_id)
        if row is None:
            raise ReplyDraftChanged("There is no reply draft for this company")
        if row["state"] != "approved":
            raise ReplyDraftChanged("Approve the reply before sending it" if row["state"] == "generated"
                                    else f"The reply is {row['state'].replace('_', ' ')}, so it was not sent")
        if row["fingerprint"] != fingerprint:
            raise ReplyDraftChanged("The reply changed after you confirmed it. Review it, then send again")
        checks = draft_checks(row["subject"], row["body"])
        if checks["placeholders"]:
            raise ValueError("Fill these in before sending: " + ", ".join(checks["placeholders"]))
        stop = problem_now(conn, target, row)
        if stop:
            raise ReplyDraftChanged(stop)
        conn.execute(
            "UPDATE outreach_reply_drafts SET state='sending', updated_at=? WHERE target_id=? AND user_id=? AND state='approved'",
            (utc_now(), target_id, user_id),
        )
    try:
        sent = _send_now(conn, target_id, user_id=user_id, row=row, client_factory=client_factory, sent_folder_check=sent_folder_check)
    except BaseException as exc:
        with conn:
            conn.execute(
                "UPDATE outreach_reply_drafts SET state='approved', note=?, updated_at=? WHERE target_id=? AND user_id=? AND state='sending'",
                (str(exc)[:500], utc_now(), target_id, user_id),
            )
        raise
    if row["purpose"] == "revisit":
        try:
            update_target(conn, target_id, {"status": "followed_up"}, user_id=user_id)
        except (ValueError, LookupError):
            LOGGER.warning("The revisit note to outreach target %s went, but its status was not moved", target_id)
    return {**sent, "target": get_target(conn, target_id, user_id=user_id)}


def _send_now(
    conn: sqlite3.Connection, target_id: str, *, user_id: str, row: dict[str, Any], client_factory: Callable[[], Any],
    sent_folder_check: str | None,
) -> dict[str, Any]:
    kind = reply_claim_kind(row["draft_id"])
    reasons: dict[str, str] = {}
    stale_token = ""
    existing = send_claim_row(conn, target_id, user_id, kind)
    if existing is not None:
        if send_claim_held(existing):
            raise SendConflictError(IN_PROGRESS)
        stale_token = existing["token"]
        if existing["state"] == "sent":
            raise ValueError("This reply was already sent from Gmail")
        reasons[f"claim:{stale_token}"] = claim_reason(existing)
    if reasons:
        check = folder_check(reasons)
        if sent_folder_check != check:
            raise SendNeedsCheckError(" ".join(dict.fromkeys(reasons.values())), check)
    account = sender_account()
    item = _reply_item(conn, target_id, user_id, row["reply_gmail_id"]) if row["reply_gmail_id"] else None
    with client_factory() as client:
        # The student asked for this send, so its checks go to Gmail even while background reads wait.
        gmail = GmailClient(conn, client, user_id, wait_out_backoff=False)
        require_account(gmail, account)
        news, why = _thread_news(gmail, row, _after_ms(row, item))
        if news == "error":
            raise RuntimeError(f"Could not read their thread in Gmail first: {why}. Nothing was sent")
        if news == "they":
            raise ReplyDraftChanged(WROTE_AGAIN)
        if news == "student":
            raise ReplyDraftChanged(STUDENT_WROTE)
        if news == "draft":
            raise SendConflictError("You started a reply to them in Gmail. Send it there, or delete that draft to send this one")

        def revalidate() -> tuple[dict[str, Any], str]:
            fresh = reply_draft_row(conn, target_id, user_id)
            if fresh is None or fresh["state"] != "sending" or fresh["fingerprint"] != row["fingerprint"]:
                raise ReplyDraftChanged("The reply changed while it was being sent. Reload and check it")
            stop = problem_now(conn, get_target(conn, target_id, user_id=user_id), fresh)
            if stop:
                raise ReplyDraftChanged(stop)
            return fresh, reply_mime(account, fresh)

        with claimed_send(conn, target_id, user_id, kind, "send", revalidate, stale_token=stale_token) as (token, (fresh, raw)):
            payload: dict[str, Any] = {"raw": raw}
            if fresh["thread_id"]:
                payload["threadId"] = fresh["thread_id"]
            message = post_under_claim(
                conn, gmail, target_id, kind, token, "/messages/send", payload,
                refused="Gmail did not send your reply",
                uncertain="it may have gone out. Check your Gmail Sent folder before sending it again",
            )
            # What Gmail sent is recorded before anything else, so it is never lost.
            try:
                stamp = utc_now()
                detail = {
                    "to": fresh["to_email"], "cc": fresh["cc"], "message_id": str(message.get("id", "")),
                    "thread_id": str(message.get("threadId", "") or fresh["thread_id"]), "draft_id": fresh["draft_id"],
                    "purpose": fresh["purpose"],
                }
                with conn:
                    conn.execute("UPDATE outreach_send_claims SET state='sent' WHERE target_id=? AND kind=? AND token=?", (target_id, kind, token))
                    log_event(conn, target_id, user_id, REPLY_SENT_EVENT, detail=f"Your reply to {fresh['to_email']} was sent",
                              data={**detail, "subject": fresh["subject"], "body": fresh["body"]})
                    conn.execute(
                        "UPDATE outreach_reply_drafts SET state='sent', sent_at=?, note='', updated_at=? WHERE target_id=? AND user_id=?",
                        (stamp, stamp, target_id, user_id),
                    )
            except BaseException:
                settle_send_claim(conn, target_id, kind, token, "sent")
                raise
    return {**detail, "account": account, "sent_at": stamp, "url": thread_url(account, detail["thread_id"]) if detail["thread_id"] else ""}


def to_gmail(conn: sqlite3.Connection, target_id: str, *, user_id: str, client_factory: Callable[[], Any]) -> dict[str, Any]:
    """Put the reply in the student's Gmail Drafts, in their thread, to finish and send there. The app then leaves it."""
    get_target(conn, target_id, user_id=user_id)
    row = _open_row(conn, target_id, user_id)
    kind = reply_claim_kind(row["draft_id"])
    existing = send_claim_row(conn, target_id, user_id, kind)
    if existing is not None:
        if send_claim_held(existing):
            raise SendConflictError(IN_PROGRESS)
        if existing["state"] == "sent":
            raise ValueError("This reply was already sent from Gmail")
        raise SendConflictError(f"{claim_reason(existing)} Until then no draft of it is made.")
    account = sender_account()

    def revalidate() -> dict[str, Any]:
        fresh = reply_draft_row(conn, target_id, user_id)
        if fresh is None or fresh["fingerprint"] != row["fingerprint"] or fresh["state"] not in OPEN_STATES:
            raise ReplyDraftChanged("The reply changed while its draft was being made. Reload and try again")
        return fresh

    with claimed_send(conn, target_id, user_id, kind, "draft", revalidate) as (token, fresh):
        try:
            client = client_factory()
        except BaseException:
            settle_send_claim(conn, target_id, kind, token, None)
            raise
        with client:
            try:
                gmail = GmailClient(conn, client, user_id, wait_out_backoff=False)
                require_account(gmail, account)
                raw = reply_mime(account, fresh)
            except BaseException:
                settle_send_claim(conn, target_id, kind, token, None)
                raise
            message: dict[str, Any] = {"raw": raw}
            if fresh["thread_id"]:
                message["threadId"] = fresh["thread_id"]
            created = post_under_claim(
                conn, gmail, target_id, kind, token, "/drafts", {"message": message},
                refused="Gmail did not create the draft",
                uncertain="the draft may be in your Gmail Drafts. Check Drafts before trying again",
            )
            try:
                made = created.get("message") or {}
                detail = {
                    "draft_id": str(created.get("id", "")), "message_id": str(made.get("id", "")),
                    "thread_id": str(made.get("threadId", "") or fresh["thread_id"]), "reply_draft_id": fresh["draft_id"],
                }
                with conn:
                    log_event(conn, target_id, user_id, REPLY_GMAIL_DRAFT_EVENT, detail="Your reply was put in your Gmail Drafts",
                              data=detail)
                    conn.execute("DELETE FROM outreach_send_claims WHERE target_id=? AND kind=? AND token=?", (target_id, kind, token))
                    conn.execute(
                        "UPDATE outreach_reply_drafts SET state='in_gmail', note=?, updated_at=? WHERE target_id=? AND user_id=?",
                        (draft_url(account, detail["message_id"]), utc_now(), target_id, user_id),
                    )
            except BaseException:
                settle_send_claim(conn, target_id, kind, token, "unconfirmed")
                raise
    return {**detail, "url": draft_url(account, detail["message_id"]), "target": get_target(conn, target_id, user_id=user_id)}


# --- The worker's pass -------------------------------------------------------------------


def _key(target_id: str, purpose: str, ident: str, revisit_for: str | None) -> str:
    return f"reply-draft:{target_id}:{purpose}:{ident}:{revisit_for or ''}"


def _failed_recently(conn: sqlite3.Connection, target_id: str, user_id: str, now: datetime) -> bool:
    row = conn.execute(
        "SELECT MAX(created_at) FROM outreach_events WHERE target_id=? AND user_id=? AND event_type=?", (target_id, user_id, FAILED_EVENT),
    ).fetchone()
    at = parse_app_instant(row[0]) if row and row[0] else None
    return at is not None and now - at < RETRY_AFTER


def answer_due(conn: sqlite3.Connection, user_id: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """The companies whose latest reply should get an answer drafted now: [{"target_id", "ident", "key"}]."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    thank_you_on = automation.mode(conn, user_id, outreach_thank_you.FEATURE) == "on"
    due = []
    for target in list_targets(conn, user_id=user_id, interested_only=True, statuses=ANSWER_STATUSES):
        if not target["reply_count"] or target["possible_reply_count"] or target["contact_bounced"] or target.get("bounced_at"):
            continue
        found = outreach_thank_you.replies(conn, target["id"], user_id)
        if not found:
            continue
        item = found[-1]
        if now - item["at"] > REPLY_WINDOW:
            continue
        ident = _ident(item)
        key = _key(target["id"], "answer", ident, None)
        row = target["reply_draft"]
        if row is not None and row["reply_gmail_id"] == ident and row["purpose"] == "answer":
            continue
        if row is not None and row["state"] in ("sending",):
            continue
        if automation.recorded(conn, user_id, key) or _failed_recently(conn, target["id"], user_id, now):
            continue
        sender = str(item["data"].get("from") or "")
        if sender and no_reply(sender.split("@", 1)[0].casefold()):
            continue
        if reading_of(item, own_words(item, sent_texts(conn, target, user_id))) == "offer":
            continue
        if outreach_thank_you.sent_since(conn, target["id"], user_id, item["at"]):
            continue
        thanked = thank_you_row(conn, target["id"], user_id)
        if thanked is not None and thanked["reply_gmail_id"] == ident and thanked["state"] != "cancelled":
            continue
        if thank_you_on and thanked is None and outreach_thank_you.eligibility(conn, target, user_id)[0] is not None:
            continue  # a plain decline: the thank-you answers it on its own
        due.append({"target_id": target["id"], "ident": ident, "key": key, "item": item, "purpose": "answer"})
    return due


def revisit_due(conn: sqlite3.Connection, user_id: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """The companies whose revisit date has come and that should get the note drafted now."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    today = local_today(conn, user_id, now)
    due = []
    for target in list_targets(conn, user_id=user_id, interested_only=True, statuses=tuple(REVISIT_STATUSES), today=today):
        if not target["revisit_due"] or target["possible_reply_count"] or target["contact_bounced"] or target.get("bounced_at"):
            continue
        revisit_for = str(target["follow_up_at"])
        found = outreach_thank_you.replies(conn, target["id"], user_id)
        ident = _ident(found[-1]) if found else ""
        key = _key(target["id"], "revisit", ident, revisit_for)
        row = target["reply_draft"]
        if row is not None and row["purpose"] == "revisit" and row["revisit_for"] == revisit_for:
            continue
        if row is not None and (row["state"] == "sending" or (row["state"] in OPEN_STATES and not row["behind"])):
            continue  # an answer still waits for the student
        if automation.recorded(conn, user_id, key) or _failed_recently(conn, target["id"], user_id, now):
            continue
        since = datetime.combine(date.fromisoformat(revisit_for), time.min, tzinfo=timezone.utc) - timedelta(days=1)
        if outreach_thank_you.sent_since(conn, target["id"], user_id, since):
            continue
        if not (target.get("contact_email") or found):
            continue
        due.append({"target_id": target["id"], "ident": ident, "key": key, "item": found[-1] if found else None,
                    "purpose": "revisit", "revisit_for": revisit_for})
    return due


def settle_behind(conn: sqlite3.Connection, user_id: str) -> int:
    """Mark answers they have since written past as stale, with why. Returns how many moved."""
    moved = 0
    latest = latest_reply_ids(conn, user_id)
    with conn:
        for row in conn.execute(
            "SELECT target_id, reply_gmail_id FROM outreach_reply_drafts WHERE user_id=? AND state IN ('generated', 'approved')", (user_id,),
        ).fetchall():
            newest = latest.get(row["target_id"], "")
            if newest and newest != row["reply_gmail_id"]:
                conn.execute(
                    "UPDATE outreach_reply_drafts SET state='stale', note=?, updated_at=? WHERE target_id=? AND user_id=? "
                    "AND state IN ('generated', 'approved')",
                    (REPLY_DRAFT_BEHIND, utc_now(), row["target_id"], user_id),
                )
                log_event(conn, row["target_id"], user_id, STALE_EVENT, detail=REPLY_DRAFT_BEHIND)
                moved += 1
    return moved


def _skip(conn: sqlite3.Connection, target_id: str, user_id: str, entry: dict[str, Any], why: str) -> None:
    """Remember that this message needs no answer from here (the student answered in Gmail), so no pass looks again."""
    stamp = utc_now()
    with conn:
        conn.execute(
            """
            INSERT INTO outreach_reply_drafts(target_id, user_id, draft_id, purpose, reply_gmail_id, state, note, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, 'skipped', ?, ?, ?)
            ON CONFLICT(target_id) DO UPDATE SET draft_id=excluded.draft_id, purpose=excluded.purpose,
                reply_gmail_id=excluded.reply_gmail_id, state='skipped', note=excluded.note, updated_at=excluded.updated_at
            WHERE outreach_reply_drafts.state NOT IN ('generated', 'approved', 'sending')
            """,
            (target_id, user_id, uuid4().hex, entry["purpose"], entry["ident"], why, stamp, stamp),
        )
        log_event(conn, target_id, user_id, SKIPPED_EVENT, detail=why)


def _answered_in_gmail(conn: sqlite3.Connection, user_id: str, entry: dict[str, Any], client_factory: Callable[[], Any]) -> str:
    """Why no answer is needed (the student answered, or is answering, in Gmail), or "". Gmail out of reach is ""."""
    item = entry.get("item")
    if entry["purpose"] != "answer" or item is None or item["data"].get("source") != "gmail" or not item["data"].get("thread_id"):
        return ""
    row = {"thread_id": str(item["data"]["thread_id"]), "reply_gmail_id": entry["ident"]}
    try:
        with client_factory() as client:
            news, _why = _thread_news(GmailClient(conn, client, user_id), row, 0)
    except Exception:  # noqa: BLE001 - Gmail out of reach only means the thread was not looked at
        LOGGER.debug("Could not read a thread before drafting a reply", exc_info=True)
        return ""
    return {"student": ANSWERED_IN_GMAIL, "draft": DRAFT_IN_GMAIL}.get(news, "")


def auto_draft(
    conn: sqlite3.Connection, user_id: str, entry: dict[str, Any], *, provider_factory: Callable[[str, str], Any] | None,
    provider: str | None = None, gmail_client_factory: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    """Write one company's answer (or revisit note) and record it in the ledger. It waits for the student; nothing is sent."""
    target_id = entry["target_id"]
    if gmail_client_factory is not None and gmail_drafts_status(conn, user_id=user_id)["bounce_check"]:
        why = _answered_in_gmail(conn, user_id, entry, gmail_client_factory)
        if why:
            _skip(conn, target_id, user_id, entry, why)
            return {"target_id": target_id, "drafted": False, "skipped": True, "reason": why}
    try:
        prepared = compose(conn, target_id, user_id=user_id, provider_factory=provider_factory, provider=provider, purpose=entry["purpose"])
    except (ValueError, RuntimeError, LookupError) as exc:
        with conn:
            log_event(conn, target_id, user_id, FAILED_EVENT, detail=f"{exc}"[:500])
        return {"target_id": target_id, "drafted": False, "error": str(exc)[:500]}
    target = get_target(conn, target_id, user_id=user_id)
    company = spoken_company(target["company"])
    who = prepared["to_name"] or prepared["to_email"]
    what = "a note for your revisit date" if entry["purpose"] == "revisit" else f"your reply to {who}"
    try:
        row = automation.perform(
            conn, user_id=user_id, feature=FEATURE, action_type=ACTION_TYPE, subject_kind="outreach_target", subject_id=target_id,
            after={"reply_draft": prepared},
            evidence={"subject": f"{company}: {'revisit date ' + str(entry.get('revisit_for')) if entry['purpose'] == 'revisit' else 'they replied'}",
                      "company": company, "reading": prepared["reading"], "generated_by": prepared["generated_by"],
                      "revisit_on": prepared["revisit_on"]},
            summary=f"Wrote {what} at {company}. It waits for your approval; nothing is sent",
            basis=f"reply:{prepared['reading'] or entry['purpose']}", confidence=None, idempotency_key=entry["key"], auto=True,
        )
    except automation.NotApplicable as exc:
        return {"target_id": target_id, "drafted": False, "skipped": True, "reason": str(exc)}
    if row is None:
        return {"target_id": target_id, "drafted": False, "skipped": True, "reason": "paused, switched off, or already written"}
    return {"target_id": target_id, "company": company, "drafted": row.get("status") == "applied", "action_id": row["id"],
            "purpose": entry["purpose"]}


def run_for_user(
    conn: sqlite3.Connection, user_id: str, report: dict[str, Any], *, provider_factory: Callable[[str, str], Any] | None,
    provider: str | None = None, gmail_client_factory: Callable[[], Any] | None = None, now: datetime | None = None,
) -> None:
    """One worker pass for one student: mark answers they wrote past, then write up to PER_PASS answers and revisit notes.

    Nothing runs while the switch is off or automation is paused. A failure is recorded as outreach.reply_drafts'
    health and raised for the worker to record too.
    """
    if not automation.is_enabled(conn, user_id, FEATURE):
        return
    written: list[dict[str, Any]] = []
    try:
        settle_behind(conn, user_id)
        for entry in [*answer_due(conn, user_id, now=now), *revisit_due(conn, user_id, now=now)]:
            if len(written) >= PER_PASS or not automation.is_enabled(conn, user_id, FEATURE):
                break
            outcome = auto_draft(
                conn, user_id, entry, provider_factory=provider_factory, provider=provider, gmail_client_factory=gmail_client_factory,
            )
            if outcome.get("drafted") or outcome.get("error"):
                written.append(outcome)
    except Exception as exc:
        LOGGER.exception("Drafting replies failed")
        rollback_quietly(conn, LOGGER, "drafting a reply failed")
        record_health_quietly(conn, user_id, HEALTH_COMPONENT, ok=False, error=step_error(exc))
        raise
    report.setdefault("reply_drafts", []).extend(written)
    record_health_quietly(conn, user_id, HEALTH_COMPONENT, ok=True, detail={"written": sum(1 for item in written if item.get("drafted"))})


# --- The ledger's action type ------------------------------------------------------------


class OutreachReplyDraft(automation.HandlerBase):
    """outreach.reply_draft: an answer to a reply, or a revisit note, reply_drafts wrote for a company.

    ``after['reply_draft']`` is what compose wrote; apply stores it (save_tx) only while it still answers their latest
    message and no answer to that message is open. The ledger keeps who it was to and who wrote it, not its words.
    Undo puts it away only while it is exactly as written and not approved, and it is not written again.
    """

    fields = ("reply_draft",)

    def read(self, conn: sqlite3.Connection, user_id: str, subject_id: str) -> dict[str, Any]:
        if conn.execute(
            f"SELECT 1 FROM outreach_targets WHERE id=? AND user_id=?{automation.for_update_clause(conn)}", (subject_id, user_id),
        ).fetchone() is None:
            raise OutreachNotFoundError(subject_id)
        row = reply_draft_row(conn, user_id=user_id, target_id=subject_id)
        if row is None:
            return {"reply_draft": None}
        return {"reply_draft": f"{row['state']}:{row['purpose']}:{row['reply_gmail_id']}:{row['revisit_for'] or ''}"}

    def effective(self, before: dict[str, Any], after: dict[str, Any], timestamp: str) -> dict[str, Any]:
        prepared = after.get("reply_draft")
        if not isinstance(prepared, dict) or not str(prepared.get("body") or "").strip():
            raise ValueError("An automatic reply needs its words")
        message = f"{prepared['purpose']}:{prepared['reply_gmail_id']}:{prepared.get('revisit_for') or ''}"
        current = str(before.get("reply_draft") or "")
        if current and current.split(":", 1)[1] == message:
            return dict(before)  # this message already has its answer, whatever became of it
        return {"reply_draft": f"generated:{message}"}

    def ledger(self, after: dict[str, Any]) -> dict[str, Any]:
        prepared = after.get("reply_draft")
        if not isinstance(prepared, dict):
            return after
        kept = {key: prepared.get(key) for key in ("purpose", "reading", "to_name", "generated_by", "revisit_on", "revisit_for")}
        return {**after, "reply_draft": kept}

    def apply(
        self, conn: sqlite3.Connection, user_id: str, subject_id: str, after: dict[str, Any], *, source: str, timestamp: str,
    ) -> dict[str, Any]:
        prepared = after["reply_draft"]
        newest = latest_reply_ids(conn, user_id, subject_id).get(subject_id, "")
        if newest != prepared["reply_gmail_id"]:
            raise automation.NotApplicable("They wrote again while it was being written, so it was not saved")
        row = reply_draft_row(conn, subject_id, user_id)
        if row is not None and (row["state"] == "sending" or (row["state"] in OPEN_STATES and _same_message(row, prepared))):
            raise automation.NotApplicable("A reply to this message is already waiting for you")
        try:
            return save_tx(conn, subject_id, user_id=user_id, prepared=prepared)
        except ReplyDraftChanged as exc:
            raise automation.NotApplicable(str(exc)) from exc

    def undo(
        self, conn: sqlite3.Connection, user_id: str, subject_id: str, before: dict[str, Any], after: dict[str, Any],
        *, source: str, timestamp: str,
    ) -> dict[str, Any]:
        result = after.get("_result") or {}
        row = reply_draft_row(conn, subject_id, user_id)
        if row is None or row["draft_id"] != result.get("draft_id"):
            raise automation.Superseded("A newer reply draft replaced it, so it was left as it is")
        if row["state"] != "generated" or row["fingerprint"] != result.get("fingerprint"):
            raise automation.Superseded("You changed, approved or sent the reply since, so it was left as it is")
        conn.execute(
            "UPDATE outreach_reply_drafts SET state='dismissed', note=?, updated_at=? WHERE target_id=? AND user_id=? AND state='generated'",
            (UNDONE, timestamp, subject_id, user_id),
        )
        log_event(conn, subject_id, user_id, DISMISSED_EVENT, detail="The automatic reply draft was undone")
        return {"undo_note": "The reply draft was put away. Write a reply on the company's card if you want one."}


def register() -> None:
    """Add the action type to the ledger's registry. Called once at startup (bootstrap.register_all)."""
    automation.register_handler(ACTION_TYPE, OutreachReplyDraft())
