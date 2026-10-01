"""A short thank-you after a plain decline, sent on its own (decline_thank_you). Phase 6, preliminary.

When a contact the student cold-emailed replies with a plain no, the app
writes a few lines of thanks and sends them in the same thread, with no
approval step. The student chose this for this one case, and chose no shadow
period: the switch is off until they turn it on. Everything else a reply can
say (a call, an offer, a question, a referral, "maybe later") stays theirs.

Deciding (``plan``, from the AutomationWorker, only while the switch is on,
automation is not paused, Jev inbox suggestions are on, and
PIPELINE_OUTREACH_ACCOUNT names the address the student sends from):

- The company's latest reply was read from Gmail by capture_replies, from the
  contact, the Cc, or anyone at the company's domain (outreach_inbox's owner
  rules), and was not an automatic reply or a delivery failure. It came from
  an address that takes replies (no "no-reply"), with no Reply-To elsewhere,
  and its whole message was kept (full_text). Nothing may be typed into the
  email it quotes: not between its "> " lines, and, below an Outlook
  From:/Sent: header (which marks no line), no line that is not the
  student's own first email or follow-up (mail_message.written_between_quotes).
  Anything there leaves it to the student.
- It passes every rule in ``thank_you_blockers``, read from its own headers as
  Gmail delivered them: in the student's thread or from the address they
  wrote to (R1), found within a day of arriving (R2), addressed to the
  student in To or Cc (R3), written by a person, not a system (R4), not sent,
  relayed, signed or linked by a job system, job board or applicant-tracking
  system (R5), from one person rather than a shared inbox or the company's
  own name, however its words are joined (R6), and vouched for by Gmail's
  sender check (R7). A reply whose headers or links are not on record, or
  cannot be read, fails them.
- Both readings say declined: the keyword rules and Jev, each kept on the
  reply_logged event (inbox_classifiers.read_reply), Jev at least
  MIN_CONFIDENCE sure. With Jev off, paused, or unavailable at capture there is
  no Jev reading, so nothing is sent. The rules are also read strictly
  (``plain_decline_problem``): no call, offer, "later", referral or question
  anywhere in it, and, failing closed on any wording not listed, every part of
  it above the signature is the rules' own no, a stock pleasantry (thanks,
  good luck), a greeting, or a name in the thread.
- Every earlier reply, from anyone there, is a plain no by that same strict
  reading, kept whole, and neither stored reading calls it a call, an offer or
  "later"; no suggestion other than declined is waiting for the student.
- It is the company's first thank-you (one per company, ever), the decline
  arrived after the switch was last turned on (turning it on never thanks an
  old one), nothing went to them after that reply, they were emailed first, the
  address has not bounced, and Gmail is connected with read access.

A company that does not qualify is noted once, in the debug log, and nothing
is written for it.

When (``plan_send_at``): a decline that arrived before 5pm on a weekday, in
the recipient's time zone, is answered after a normal human delay (40 to 150
minutes, stable per company and reply, never before 9:00 plus that delay, and
at least 10 minutes after the app saw it), the same day. At or after 5pm, on a
weekend, or when the delay runs past 5pm, it goes the next weekday morning
between 9:00 and 9:40, like any scheduled send.

What (``write``): a model chosen under PIPELINE_OUTREACH_THANK_YOU_PROVIDER
(empty means the first-email writer) writes it from the decline, the student's
name and greeting style, the recipient's name, and the company. Plain code
checks it (``validate``): at most 70 words, no question, no number the inputs
do not have, no attachment, no ask or promise, no dash, the student's greeting
for this recipient, and the student's name last. One retry; after that, or
with no model, a fixed template that passes the same checks.

Scheduling is one transaction (automation.perform_in, after pause_guard): the
thank-you and its outreach_scheduled_sends row (kind 'thank_you'), the status
change to Declined (undoable; undoing it leaves the email scheduled), and the
ledger entry that says it was scheduled (not undoable).

Sending is the scheduled-send machinery (outreach_schedule.run_due_sends), so
the pause, the hand-over guard, stuck recovery and missed mornings all apply,
and it goes only 9:00 to 17:00 on a weekday in their zone (``in_window``): one
a retry, a pause or a slow check carried outside that waits for their next
weekday morning. Just before it goes (``gate``), every check fails closed:
Gmail is read again (fresh_look); a new message from them (arrived after the
decline, or logged after the thank-you was planned), or anything the student
sent them, cancels it; a paused, moved-on, or bounced company cancels it; Jev
inbox suggestions turned off, or PIPELINE_OUTREACH_ACCOUNT cleared, holds it
(``requirement_hold``); a reply that no longer passes
thank_you_blockers cancels it, with the rule on the card ("Not thanked
automatically: sent by an automated system"); a second model
(outreach_review.review_choice, a different family when one is set up) must
pass it, or it is held with the reason on the card and a notice (in fixed
words, never the reviewer's); then the records are read again, and last the
thread itself (a reply or a draft of the student's there cancels it). The
hand-over and the send claim read the records once more (``problem_now``),
so a reply logged while the reviewer ran still stops it. A reply logged at
any time closes a thank-you not yet on its way (``on_new_reply``). The
student can cancel it, move it to their Gmail Drafts to edit (which stops the
automatic send), or send a held one anyway.
"""

from __future__ import annotations

import email
import hashlib
import json
import logging
import re
import sqlite3
import threading
from datetime import datetime, time, timedelta, timezone
from email import headerregistry, policy
from email.message import EmailMessage
from typing import Any, Callable, Iterable
from urllib.parse import quote

import httpx

from pipeline_core.identity import identity_tokens, normalized

from . import automation, outreach_callbacks
from .background import record_health_quietly, step_error
from .database import rollback_quietly
from .inbox_classifiers import JEV_NOT_ASKED, MIN_CONFIDENCE
from .json_values import json_dict
from .mail_message import FULL_TEXT_LIMIT, hosts_in, is_automatic, written_between_quotes
from .outreach_config import resolve_provider, sender_account
from .outreach import (
    REPLY_PATTERNS,
    OutreachNotFoundError,
    log_event,
    company_key,
    contact_first_name,
    get_target,
    greeting_line,
    greeting_style,
    spoken_company,
    suggest_reply_status,
)
from .gmail_client import GmailAuthError, GmailThrottled
from .outreach_gmail import (
    SENT_EVENT,
    THANK_YOU_DRAFT_EVENT,
    THANK_YOU_KIND,
    THANK_YOU_SENT_EVENT,
    SendConflictError,
    SendUnconfirmedError,
    ThankYouChanged,
    _Gmail,
    backoff_until,
    create_thank_you_draft,
    gmail_drafts_status,
    send_thank_you,
    thank_you_fingerprint,
    thank_you_row,
)
from .outreach_schedule import (
    GMAIL_HOLD_MARGIN,
    KindHooks,
    finish_send,
    hold_for_retry,
    next_morning,
    recipient_zone,
    register_kind,
    send_time_label,
    wait_for_gmail,
)
from .timestamps import parse_app_instant, utc_now
from .user_time import at_wall_clock, to_local

LOGGER = logging.getLogger(__name__)

FEATURE = "decline_thank_you"
HEALTH_COMPONENT = "outreach.thank_you"
# The switches the automation worker runs this for.
WORKER_FEATURES = (FEATURE,)
STATES = ("planned", "scheduled", "sending", "transmitting", "sent", "cancelled", "held", "failed")
# A thank-you in one of these can still be stopped or changed.
OPEN_STATES = ("planned", "scheduled", "held", "failed")
# The company statuses a thank-you is planned for, and may still go to.
ELIGIBLE_STATUSES = ("replied", "declined")
SCHEDULED_EVENT = "thank_you_scheduled"
CANCELLED_EVENT = "thank_you_cancelled"
HELD_EVENT = "thank_you_held"
FAILED_EVENT = "thank_you_failed"
REVIEWED_EVENT = "thank_you_reviewed"
# Model calls are slow: at most this many thank-yous are written per worker pass.
PLAN_PER_PASS = 3
WORKDAY_START = time(9, 0)
WORKDAY_END = time(17, 0)
DELAY_MINUTES = (40, 150)
# However late the app sees a decline, the thank-you waits at least this long.
DETECTION_MARGIN = timedelta(minutes=10)
MAX_WORDS = 70
SIGN_OFF = "Best"
WROTE_AGAIN = "They wrote again, so the thank-you was not sent. Read their reply."
STUDENT_WROTE = "You wrote to them after their reply, so the thank-you was not sent."
NOT_INTERESTED_STOP = "You marked the company not interested, so the thank-you was not sent."
DRAFT_STARTED = "You started a reply to them in Gmail, so the thank-you was not sent."
SWITCHED_OFF = "Send a thank-you when someone declines was turned off before it went, so it was not sent"
JEV_OFF = "Jev inbox suggestions was turned off before it went, so it was not sent automatically"
NO_ACCOUNT = (
    "PIPELINE_OUTREACH_ACCOUNT is not set, so their reply could not be confirmed as addressed to you and it was "
    "not sent automatically"
)
# An email from the company that may be a reply (outreach_inbox.py) waits for the student: it may say more than no.
MAY_HAVE_REPLIED = "An email from them that may be a reply is waiting for you to check, so the thank-you was held"
EDITED = "You chose to edit it yourself, so it went to your Gmail Drafts and was not sent automatically"
STUCK_SENDING = "The app stopped while sending this. Check your Gmail Sent folder before sending it again"
# What a notice may say about why a thank-you stopped: fixed words only, never the reviewer's or an email's.
_NOTICE_REASONS = (
    ("The reviewer held it", "the reviewer did not pass it"),
    ("The reviewer could not run", "the reviewer could not run"),
    (SWITCHED_OFF, "the switch was turned off"),
    (JEV_OFF, "Jev inbox suggestions was turned off"),
    (NO_ACCOUNT, "your sending address is not set"),
    (MAY_HAVE_REPLIED, "an email from them may be a reply"),
    ("Could not check Gmail", "Gmail could not be checked first"),
    ("Could not read their thread", "Gmail could not be checked first"),
)

_NOTED: set[tuple[str, str, str]] = set()
_NOTED_LOCK = threading.Lock()


def in_window(moment: datetime, zone: Any) -> bool:
    """Whether a thank-you may go at ``moment``: a weekday, 9:00 to before 17:00, in the recipient's zone.

    Checked when it is planned (plan_send_at) and again as it goes (outreach_schedule), since a retry, a
    wait on Gmail, a pause, a sleeping computer or a slow check can carry it past five.
    """
    local = to_local(moment, zone)
    return local.weekday() < 5 and WORKDAY_START <= local.time() < WORKDAY_END


# --- When it goes ------------------------------------------------------------------------


def stable_delay(key: str) -> timedelta:
    """A normal human response time, 40 to 150 minutes, the same every time for the same key."""
    low, high = DELAY_MINUTES
    return timedelta(minutes=low + int(hashlib.sha256(key.encode()).hexdigest(), 16) % (high - low + 1))


def plan_send_at(reply_received_at: datetime, zone: Any, key: str, now: datetime, *, seed: str | None = None) -> datetime:
    """When a thank-you for a decline received at ``reply_received_at`` goes, as UTC.

    ``zone`` is the recipient's (outreach_schedule.recipient_zone). ``key``
    makes the delay stable (the company and the reply); ``seed`` the morning
    minute, as next_morning spreads every scheduled send (defaults to ``key``).
    Arithmetic is done in UTC, so a day when the clocks change moves nothing.
    """
    received = reply_received_at.astimezone(timezone.utc)
    now = now.astimezone(timezone.utc)
    local = to_local(received, zone)
    if local.weekday() < 5 and local.time() < WORKDAY_END:
        delay = stable_delay(key)
        nine = datetime.combine(local.date(), WORKDAY_START)
        nine = at_wall_clock(nine, zone).astimezone(timezone.utc)
        candidate = max(received + delay, nine + delay)
        send_at = max(candidate, now + DETECTION_MARGIN)
        there = to_local(send_at, zone)
        if there.date() == local.date() and there.time() < WORKDAY_END:
            return send_at
    return next_morning(max(received, now), zone, seed or key)


def _when_words(send_at: datetime, zone: Any, basis: str) -> str:
    """"Tue 11:32 AM (their time)", as the activity feed says it."""
    local = to_local(send_at, zone)
    whose = "their time" if basis.startswith("their time") else "your time"
    return f"{local:%a} {f'{local:%I:%M %p}'.lstrip('0')} ({whose})"


# --- What it says ------------------------------------------------------------------------

INSTRUCTIONS = """You write a short thank-you reply for a university student. The student cold-emailed a company about an
internship, and someone there replied to say no. The student wants to thank them, and nothing else.

Rules:
- Use only the JSON input. Never invent anything.
- Open with the greeting line in the input, exactly as written, on its own line.
- Then two or three short sentences: thank them for getting back to the student and for considering it, and wish them
  and their team well.
- Do not ask for anything. No question, no "let me know", no call, chat or meeting, no "keep me in mind", no asking
  them to reconsider or to pass anything on, and no promise or plan of the student's: no "I will" or "I'll", no
  applying, no "next year", and no writing again.
- No numbers, no attachment, no links, and no dashes of any kind between words.
- At most 60 words in all. Plain text, no markdown.
- End with a short sign-off line, then the student's name exactly as given, alone on the last line.

Reply with exactly one JSON object and nothing else:
{"body": "..."}"""

_ASKS = re.compile(
    r"\b(let me know|would you|could you|could we|can we|can you|will you|call(?:s|ed|ing)?|chat(?:s|ted|ting)?"
    r"|meet(?:s|ing|ings)?|connect(?:s|ed|ing)?|keep me in mind|reconsider\w*|keep in touch|stay in touch|reach out"
    r"|in the future|down the road|if anything changes|refer(?:ral|rals|red)?|introduc\w+|later|hope to hear"
    r"|look(?:ing)? forward|follow(?:ing)? up|touch base|circle back|opening|openings|position|positions|role|roles"
    # A promise, or a plan of the student's: "I will be sure to apply again next year".
    r"|i will|i'll|i shall|apply|applying|re-?apply\w*|someday|some day|hope to|next (?:year|summer|spring|fall|autumn"
    r"|winter|semester|term|quarter|cycle|round|time))\b"
    # "Again" promises more, except in "thank you again".
    r"|(?<!thanks )(?<!thank you )\bagain\b",
    re.IGNORECASE,
)
_ATTACHMENT = re.compile(r"\b(attach\w*|enclos\w*|r[eé]sum[eé]s?|cv|portfolio|transcript)\b", re.IGNORECASE)
_DASH = re.compile(r"[—–‒―]|\s-{1,2}\s|--|^-|-$", re.MULTILINE)
_LINK = re.compile(r"https?://|www\.|\S@\S")
_NUMBER = re.compile(r"(?<![\w@.])\d[\d,.]*%?")


def _numbers(text: str) -> set[str]:
    return {match.rstrip(".,") for match in _NUMBER.findall(text)} - {""}


def _without(text: str, names: list[str]) -> str:
    """The text with these names taken out, so a name such as "Connect Robotics" is not read as an ask."""
    for name in sorted({name for name in names if name}, key=len, reverse=True):
        text = re.sub(re.escape(name), " ", text, flags=re.IGNORECASE)
    return text


def validate(body: str, inputs: dict[str, Any]) -> list[str]:
    """Every reason a thank-you cannot go as written. Plain code only; an empty list passes."""
    text = body.replace("\r\n", "\n").strip()
    if not text:
        return ["it is empty"]
    problems = []
    words = len(re.findall(r"\b\w+\b", text))
    if words > MAX_WORDS:
        problems.append(f"it runs {words} words, over {MAX_WORDS}")
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if lines[0] != inputs["greeting"]:
        problems.append(f"it opens with {lines[0][:80]!r}; open with {inputs['greeting']!r} on its own line")
    if lines[-1] != inputs["student_name"]:
        problems.append(f"it must end with the student's name, {inputs['student_name']!r}, alone on the last line")
    names = [inputs["company"], inputs.get("company_full", ""), inputs.get("recipient_name", ""), inputs["student_name"]]
    scan = _without("\n".join(lines[1:]), names)
    if "?" in scan:
        problems.append("it asks a question")
    allowed = _numbers(" ".join(str(inputs.get(key) or "") for key in ("decline", "student_name", "recipient_name", "company", "company_full", "greeting")))
    extra = sorted(_numbers(text) - allowed)
    if extra:
        problems.append("it states numbers found in none of the inputs: " + ", ".join(extra))
    attachment = _ATTACHMENT.search(scan)
    if attachment:
        problems.append(f"it mentions an attachment or a document ({attachment.group(0)!r})")
    ask = _ASKS.search(scan)
    if ask:
        problems.append(f"it asks for something or promises more ({ask.group(0)!r}); it may only thank them")
    if _DASH.search(scan):
        problems.append("it uses a dash")
    if _LINK.search(scan):
        problems.append("it has a link or an address in it")
    return problems


def template(inputs: dict[str, Any]) -> str:
    """The thank-you with no model: fixed words that pass validate."""
    return (
        f"{inputs['greeting']}\n\nThank you for getting back to me, and for taking the time to consider it. "
        f"I appreciate it, and I wish you and the {inputs['company']} team all the best.\n\n{SIGN_OFF},\n{inputs['student_name']}"
    )


def _parsed(raw: str, inputs: dict[str, Any]) -> tuple[str, list[str]]:
    from .agent_providers import CliAgentProvider

    try:
        parsed = CliAgentProvider.extract_json(raw)
    except ValueError:
        return "", ["it was not one JSON object with a body"]
    body = str(parsed.get("body") or "").replace("\r\n", "\n").strip()
    return body, validate(body, inputs)


def write(
    inputs: dict[str, Any], *, provider_factory: Callable[[str, str], Any] | None, provider: str | None = None,
) -> tuple[str, str]:
    """The thank-you's words and who wrote them ("<provider>:<model>", or "template").

    The model gets one retry with the reasons it was refused; a model that is
    unavailable, fails twice, or is not set up leaves the template.
    """
    from .agent_providers import complete_text

    try:
        provider_id, model = resolve_provider(provider, purpose="thank_you")
    except ValueError:
        provider_id, model = "legacy", ""
    if provider_id == "legacy" or provider_factory is None:
        return template(inputs), "template"
    visible = {key: inputs[key] for key in ("decline", "student_name", "greeting", "recipient_name", "company") if inputs.get(key)}
    content = json.dumps(visible, ensure_ascii=False, indent=2)
    asked = content
    try:
        agent = provider_factory(provider_id, model)
        for _attempt in range(2):
            body, problems = _parsed(complete_text(agent, INSTRUCTIONS, asked), inputs)
            if not problems:
                return body, f"{provider_id}:{model}"
            asked = f"{content}\n\nYour previous thank-you was refused because " + "; ".join(problems) + ". Write it again following every rule."
    except Exception as exc:  # noqa: BLE001 - a model that cannot run leaves the template
        LOGGER.info("The thank-you writer could not run (%s); using the template", type(exc).__name__)
    return template(inputs), "template"


_TEAM_WORDS = re.compile(
    r"\b(team|careers?|jobs|recruit\w*|talent|hiring|hr|people|info|support|hello|contact|admin|office|no-?reply|notifications?)\b",
    re.IGNORECASE,
)


# Letters after a name that are not a name: "Dana Lee, PhD", "John Smith, Jr.", "Jane Doe, SHRM-CP".
_SUFFIXES = re.compile(
    r"(jr|sr|ii|iii|iv|phd|ph\.d|md|mba|ms|msc|ma|ba|bs|bsc|meng|beng|mfa|mph|jd|cpa|cfa|pe|esq|pmp|rn|phr|sphr"
    r"|shrm-?cp|shrm-?scp)\.?",
    re.IGNORECASE,
)


def _letters(word: str) -> str:
    return word.replace("-", "").replace("'", "").replace(".", "")


def _credential(part: str) -> bool:
    """Every word is a suffix or a credential: listed, or two or more capitals ("MBA", "SHRM-CP")."""
    words = part.split()
    return bool(words) and all(
        _SUFFIXES.fullmatch(word) or (_letters(word).isalpha() and _letters(word).isupper() and len(_letters(word)) >= 2)
        for word in words
    )


def _given_name(part: str) -> bool:
    """One or two capitalised words ("Dana", "Mary Ann", "John A."), none a suffix or all capitals."""
    words = part.split()
    if not 1 <= len(words) <= 2:
        return False

    def word_ok(word: str, first: bool) -> bool:
        if not first and re.fullmatch(r"[A-Z]\.?", word):
            return True  # an initial
        letters = _letters(word)
        return (letters.isalpha() and word[:1].isupper() and not letters.isupper() and not _SUFFIXES.fullmatch(word))

    return all(word_ok(word, index == 0) for index, word in enumerate(words))


# A word that makes what follows a comma a job title, not a given name: "Dana Lee, Founder", "Sam Park, Head of Talent".
_TITLE_WORDS = re.compile(
    r"\b(founder|co-?founder|founding|ceo|cto|coo|cfo|cmo|cpo|chro|cso|vp|svp|evp|avp|president|chair\w*|director|manager|head"
    r"|lead|principal|partner|owner|chief|officer|executive|engineer\w*|scientist|researcher|professor|recruit\w*|talent"
    r"|coordinator|specialist|associate|analyst|advis[oe]r|consultant|assistant|administrator|admin|developer|architect"
    r"|designer|intern|operations|sales|marketing|product|people|hr|research|design|senior|sr|staff|general|managing"
    r"|technical|hiring|team|dr|mr|mrs|ms|mx|prof)\b",
    re.IGNORECASE,
)


def _display_name(name: str, company: str = "") -> str:
    """A From display name in the order people say it, or "" when which part is the name is not plain.

    "Lee, Dana" is Dana Lee: one word before the comma, then a given name.
    After a whole name, what follows the comma is a credential, a title or
    the company ("Dana Lee, PhD", "Dana Lee, Founder", "Jane Doe, Acme") and
    is dropped. Anything else ("Lee, DANA", "Van Berg, Anna", "Lee, Founder")
    is left unnamed, so the greeting falls back rather than say "Hi Founder,".
    """
    parts = [part.strip() for part in name.split(",") if part.strip()]
    if not parts:
        return ""
    head, rest = parts[0], parts[1:]
    whole = len(head.split()) >= 2
    # Suffixes and credentials at the end. Right after a lone surname, an all-capitals word may be the
    # given name ("Lee, DANA"), so there only listed suffixes are dropped.
    while rest and (_SUFFIXES.fullmatch(rest[-1]) or ((whole or len(rest) > 1) and _credential(rest[-1]))):
        rest.pop()
    if not rest:
        return head
    company_words = {word.casefold() for word in re.findall(r"[A-Za-z]+", spoken_company(company))} - {"the", "and", "of"}

    def not_a_name(part: str) -> bool:
        return bool(_TITLE_WORDS.search(part)) or any(word.casefold() in company_words for word in re.findall(r"[A-Za-z]+", part))

    if whole:
        return head if all(not_a_name(part) for part in rest) else ""
    if len(rest) == 1 and _given_name(rest[0]) and not not_a_name(rest[0]):
        return f"{rest[0]} {head}"
    return ""


def recipient_name(from_name: str, to_email: str, target: dict[str, Any]) -> str:
    """The name of the person who wrote, from their From header, or the contact's name when it was the contact.

    A shared inbox ("Acme Careers", "Hiring Team") names nobody, so it is left
    out and the student's shared-inbox greeting is used. "Lee, Dana" is Dana
    Lee, while "Dana Lee, PhD" and "Dana Lee, Founder" are Dana Lee, never
    "PhD Dana Lee" or "Founder Dana Lee"; a From name whose order is not plain
    names nobody (_display_name).
    """
    name = " ".join(str(from_name or "").replace('"', " ").split())
    if "@" in name:
        name = ""
    if "," in name:
        name = _display_name(name, str(target.get("company") or ""))
    company = spoken_company(str(target.get("company") or ""))
    if name and (_TEAM_WORDS.search(name) or (company and (
        company_key(name) == company_key(company) or re.search(rf"\b{re.escape(company)}\b", name, re.IGNORECASE)
    ))):
        name = ""
    if not name and to_email.casefold() == str(target.get("contact_email") or "").casefold():
        name = str(target.get("contact_name") or "")
    return name


def _subject(subject: str, fallback: str) -> str:
    text = " ".join(str(subject or fallback or "").split())
    return text if re.match(r"^re\s*:", text, re.IGNORECASE) else f"Re: {text}".strip()


# --- Whether a company qualifies ---------------------------------------------------------


def replies(conn: sqlite3.Connection, target_id: str, user_id: str) -> list[dict[str, Any]]:
    """Every reply logged for the company, oldest first by when it arrived (Gmail's time; a pasted one, when it was logged).

    Ordered by arrival, not by when it was logged: one Gmail check logs the
    newest message first.
    """
    items = []
    for row in conn.execute(
        "SELECT id, detail, detail_json, created_at FROM outreach_events WHERE target_id=? AND user_id=? AND event_type='reply_logged'",
        (target_id, user_id),
    ).fetchall():
        data = json_dict(row["detail_json"])
        at = parse_app_instant(data.get("received_at")) or parse_app_instant(row["created_at"])
        if at is None:
            continue
        items.append({"id": row["id"], "text": row["detail"] or "", "data": data, "created_at": row["created_at"], "at": at})
    return sorted(items, key=lambda item: (item["at"], item["created_at"]))


def latest_reply(conn: sqlite3.Connection, target_id: str, user_id: str) -> dict[str, Any] | None:
    found = replies(conn, target_id, user_id)
    return found[-1] if found else None


def sent_since(conn: sqlite3.Connection, target_id: str, user_id: str, since: datetime) -> bool:
    """Whether anything went to the company after ``since``: an email, a thank-you, a form, "I sent it", or a send under way."""
    from .outreach_forms import SUBMITTED_EVENT as FORM_SUBMITTED, UNCONFIRMED_EVENT as FORM_UNCONFIRMED

    for row in conn.execute(
        "SELECT event_type, to_status, created_at FROM outreach_events WHERE target_id=? AND user_id=? "
        "AND event_type IN (?, ?, ?, ?, ?, 'status')",
        (target_id, user_id, SENT_EVENT, THANK_YOU_SENT_EVENT, THANK_YOU_DRAFT_EVENT, FORM_SUBMITTED, FORM_UNCONFIRMED),
    ).fetchall():
        at = parse_app_instant(row["created_at"])
        if at is None or at <= since:
            continue
        if row["event_type"] == "status" and row["to_status"] not in ("sent", "followed_up"):
            continue
        return True
    # An email or form of the student's that may be on its way, or may have gone.
    for row in conn.execute(
        "SELECT claimed_at FROM outreach_send_claims WHERE target_id=? AND user_id=? AND kind<>? AND action IN ('send', 'form') "
        "AND state IN ('sending', 'sent', 'unconfirmed', 'clicking')",
        (target_id, user_id, THANK_YOU_KIND),
    ).fetchall():
        at = parse_app_instant(row["claimed_at"])
        if at is not None and at > since:
            return True
    return False


def _readings_words(readings: dict[str, Any]) -> str:
    rules = (readings.get("rules") or {}).get("status") or "nothing"
    jev = readings.get("jev") or {}
    said = jev.get("label") or f"nothing ({readings.get('jev_fallback') or JEV_NOT_ASKED})"
    return f"the rules read it as {rules}, and Jev as {said}"


def _pattern(status: str) -> str:
    return next(pattern for name, pattern, _reason in REPLY_PATTERNS if name == status)


# Anything in a reply that keeps a door open, beyond what the rules' own patterns catch: a call or a
# meeting in any words, "later", a pointer to a job board, someone else to talk to. A plain decline has none.
_OPEN_DOOR = re.compile(
    r"\b(call|calls|chat|chats|meet|meeting|meetings|zoom|coffee|talk|speak|schedule\w*|interview (?:you|with)"
    r"|next (?:year|summer|spring|fall|autumn|winter|semester|term|quarter|cycle|round|time)|in the future"
    r"|future (?:openings?|roles?|positions?|opportunit\w+|internships?|hiring|needs)|down the road|later|someday|revisit"
    r"|re-?apply\w*|apply|application portal|careers? (?:page|site|portal)|job board|posting|posted"
    r"|keep (?:you|your \w+) (?:in mind|on file)|on file|in touch|reach (?:back )?out|check back|circle back|touch base"
    r"|let you know|keep you posted|get back to you|if anything changes|open(?:s|ed|ing)? up"
    r"|talk to|colleague\w*|co-?workers?|forward\w*|refer\w*|introduc\w*|connect\w*|loop\w* in|cc'?e?d"
    r"|pass(?:ed|ing)? (?:this|it|your|along)|contact (?:him|her|them|my|our)|point you|try (?:reaching|contacting|emailing))\b"
    r"|(?<!thanks )(?<!thank you )\bagain\b",
    re.IGNORECASE,
)


def plain_decline_problem(text: str, names: Iterable[str] = ()) -> str:
    """Why the rules, read strictly, do not see a plain decline in ``text``; "" when they do.

    The rules' reading (suggest_reply_status) stops at its first match and
    looks for a decline before a call or a "later", so "We're not hiring, but
    happy to set up a call" reads as declined there. Here the decline must be
    there and nothing else may be: no offer, call, "later" or referral, by the
    rules' patterns or in plainer words, and no question. And, failing closed
    on any wording not listed here, every part of what they wrote (each clause,
    above a signature) must be the rules' own no, a stock pleasantry such as
    thanks or good luck, a greeting, or one of ``names`` (the people and the
    company in the thread): anything else is more than no (_more_than_no).
    """
    lowered = " ".join(str(text).translate(_FLAT_QUOTES).lower().split())
    if not re.search(_pattern("declined"), lowered):
        return "the rules find no plain no in it"
    for status in ("offer", "paused", "call_scheduled"):
        if re.search(_pattern(status), lowered):
            return f"the rules also read it as {status.replace('_', ' ')}"
    if "?" in lowered:
        return "it asks a question"
    found = _OPEN_DOOR.search(lowered)
    if found:
        return f"it says more than no ({found.group(0)!r})"
    more = _more_than_no(str(text), names)
    if more:
        return f"it says more than no ({more[:80]!r})"
    return ""


# --- The strict reading's list: the no, and nothing else ------------------------------------------

_FLAT_QUOTES = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u00a0": " ", "\u200b": None})
# Where one part of a reply ends and the next begins. Punctuation is dropped; a joining word stays at the start of
# the part it opens, so a dangling "yet" or "until" is a part of its own and never silently lost.
_CLAUSE_BREAK = re.compile(
    r"[.!?;:,()\[\]{}\"]+|\s[-–—]+\s|[–—]"
    r"|(?=\b(?:but|however|though|although|yet|so(?! much\b| very\b)|and|plus|also|except|unless|until|till|while"
    r"|whereas|instead|otherwise|if|when|whenever|once|because|since)\b)",
    re.IGNORECASE,
)
# A part may open with one of these and still be the no or a pleasantry: "but we're not hiring", "and good luck".
_LEADING = {"and", "so", "but", "however"}
_N = r"zzname(?: zzname)*"
_US = rf"(?:us|me|{_N}|our (?:company|team|work|startup|lab|group|firm|organization|mission|product))"
_ACTS = rf"(?:reaching out(?: to {_US})?|getting in touch|contacting {_US}|writing(?: to {_US})?|thinking of {_US}|considering {_US}|following up)"
_THINGS = (
    r"(?:(?:the|your) (?:kind |thoughtful |nice |lovely )?(?:note|email|e-mail|message|outreach|words|patience|time|interest)"
    rf"(?: in (?:{_US}|working (?:with|at|for) {_US}))?)"
)
_SEARCH = r"(?:(?:with|in|on) (?:your|the) (?:job |internship )?(?:search|hunt|studies|career|applications|journey|endeavors|endeavours))"
_SIGN_OFFS = (
    r"best|best regards|kind regards|warm regards|warmest regards|regards|warmly|warm wishes|cheers|sincerely|thanks|thank you"
    r"|many thanks|thanks again|thank you again|thanks so much|thank you so much|all the best|best wishes|best of luck|good luck"
    r"|take care|respectfully|yours|yours truly|yours sincerely|with thanks"
)
# The pleasantries a plain no may carry, whole: a part must match one from start to end.
_PLEASANTRY = re.compile(
    r"(?:thanks|thank you|many thanks|much appreciated)(?: (?:so|very) much| a lot| a ton)?(?: again)?"
    rf"(?: for (?:{_ACTS}|{_THINGS}))?"
    rf"|for (?:{_ACTS}|{_THINGS})"
    rf"|(?:(?:i|we) )?(?:really |truly |do |sincerely |greatly )?appreciate (?:it|you {_ACTS}|{_ACTS}|{_THINGS})"
    r"|(?:it was |it's |it is )?(?:great|nice|good|lovely|a pleasure) (?:to hear|hearing) from you"
    rf"|(?:best of luck|good luck|all the best|best wishes)(?: {_SEARCH})?(?: zzname)*"
    rf"|(?:(?:i|we) )?wish(?:ing)? you (?:the best|all the best|the best of luck|good luck|luck|every success|success|well)(?: {_SEARCH})?"
    r"|(?:(?:i|we) )?hope (?:you're|you are) (?:doing )?well|(?:(?:i|we) )?hope all is well"
    r"|(?:(?:i|we) )?hope (?:this|this note|this email|my note|my email) finds you well"
    r"|(?:(?:(?:i|we) )?hope you (?:have|are having|'re having)|have) a (?:great|good|nice|wonderful|lovely) (?:day|week|weekend|semester)"
    r"|(?:i'm |i am |we're |we are )?(?:so |very |really )?(?:sorry|apologies)"
    r"(?: for the (?:late|slow|delayed) (?:reply|response)| for the delay| to disappoint| about that)?"
    rf"|(?:{_SIGN_OFFS})(?: zzname)*"
)
_GREETING = re.compile(r"(?i:hi|hello|hey|dear|greetings|good (?:morning|afternoon|evening))(?: (?:there|all|everyone|zzname|[A-Z][\w'.-]*)){0,3}")
# Words around the no that add nothing to it: "Unfortunately", "I'm afraid", "right now", "this summer".
_SOFTENERS = re.compile(r"\b(?:i'm afraid|i am afraid|unfortunately|sadly|regrettably|alas)\b")
_NOW = re.compile(
    r"\b(?:right now|at (?:this|the) (?:time|moment|point|stage)|at present|currently|presently"
    r"|this (?:year|summer|spring|fall|autumn|winter|semester|term|cycle|season|round|quarter))\b"
)
# What else a part holding the no may say, word by word ("we're not hiring interns", "so we won't be able to
# take you on"). No modal, no time but now, no "in", no "until", no verb of more: those are more than no.
_DECLINE_FILLER = frozenset("""
a an the this that it it's its there there's here we we're we've we'd we'll us our i i'm i've me my you you're your
is are am be been was were have has do does don't doesn't not no any anyone anybody all or really just still actively
right now for to on of with at take offer bring board onboard intern interns internship internships student students
co-op co-ops coop coops people candidates applicants new more additional extra position positions role roles opening
openings spot spots program programs team company startup side available open able as such therefore zzname
""".split())
# A part holding the no has one subject: "we're not hiring interns this summer we have openings this fall" has two.
# ("you" and "it" are left out: "take you on" and "take it on" have them as objects.)
_SUBJECTS = frozenset("we we're we've we'd we'll i i'm i've it's there there's you're".split())
# Words that are names only when the names list says so, and never masked: they mean something in a reply.
_NOT_NAMES = frozenset("""
may will can hope summer spring fall winter june april august march soon later next again grant chase mark bill rich
sunny joy faith grace art dean drew jack ray rose pat sue hi hello dear best thanks team the and of inc llc ltd co corp
talk call chat meet connect reach touch future open apply hire hiring careers jobs
""".split())
_SENT_FROM = re.compile(r"(?:sent from|get outlook for) .{1,40}", re.IGNORECASE)
# A signature line that says any of these is a message, not a signature ("We're Hiring", "Book a Call"). A title
# such as "Early Careers Recruiter" or "University Internships" is still a signature.
_NOT_SIGNATURE = re.compile(
    r"\b(hiring|join|apply|calendly|book|schedule|meet|call|chat|coffee|lunch|contact|reach|try|talk|connect|refer\w*"
    r"|colleague|cc|p\.?s)\b",
    re.IGNORECASE,
)
_WEBLIKE = re.compile(r"(?:https?://|www\.)\S+|[\w-]+(?:\.[\w-]+)+(?:/\S*)?")
_SIGNATURE_JOINERS = {"of", "and", "at", "the", "for", "in", "&", "de", "la", "van", "von", "der", "du", "le", "da", "di"}


def _name_words(names: Iterable[str]) -> set[str]:
    words: set[str] = set()
    for name in names:
        for word in re.findall(r"[A-Za-z][A-Za-z'.-]*", str(name or "")):
            word = word.strip(".'-").casefold()
            if len(word) >= 2 and word not in _NOT_NAMES:
                words.add(word)
    return words


def _masked(part: str, words: set[str]) -> str:
    """The part with each capitalised name word as "zzname"."""
    return re.sub(r"[A-Za-z][A-Za-z'-]*", lambda found: "zzname" if found.group(0)[0].isupper() and found.group(0).casefold() in words
                  else found.group(0), part)


def _sign_off(line: str) -> bool:
    """"Best,", "Thanks!", "Cheers, Dana": a line that closes the message."""
    pieces = re.split(r"[,!.\-–—]", line, maxsplit=1)
    first, rest = pieces[0], (pieces[1] if len(pieces) > 1 else "")
    words = rest.strip(" ,!.-").split()
    return bool(re.fullmatch(_SIGN_OFFS, first.strip().lower())) and len(words) <= 3 and all(word[:1].isupper() for word in words)


def _signature_line(line: str) -> bool:
    """A line of a signature: a name, a title, a company, an address, a number or a link, and no message."""
    if len(line) > 120 or "?" in line or _NOT_SIGNATURE.search(line):
        return False
    if _SENT_FROM.fullmatch(line.strip()):
        return True
    for token in line.split():
        word = token.strip("()[]{}|,;:·•*_\"'")
        if not word or not any(character.isalpha() for character in word):
            continue
        if "@" in word or any(character.isdigit() for character in word) or _WEBLIKE.fullmatch(word.lower()):
            continue
        if word[0].isupper() or word.casefold() in _SIGNATURE_JOINERS:
            continue
        return False
    return True


def _without_signature(lines: list[str]) -> list[str]:
    """The lines above the signature: what follows a sign-off line ("Best,") or a "--" line, when every line of it
    reads as a signature, and a closing "Sent from my iPhone"."""
    while lines and _SENT_FROM.fullmatch(lines[-1]):
        lines = lines[:-1]
    for index, line in enumerate(lines):
        if line in {"--", "-- "} and all(_signature_line(rest) for rest in lines[index + 1:]):
            return lines[:index]
        if _sign_off(line) and all(_signature_line(rest) for rest in lines[index + 1:]):
            return lines[:index + 1]
    return lines


def _part_is_no_or_pleasantry(part: str, words: set[str]) -> bool:
    masked = _masked(part, words)
    if _GREETING.fullmatch(masked):
        return True
    lowered = masked.lower()
    tokens = lowered.split()
    if len(tokens) > 1 and tokens[0] in _LEADING:
        lowered = " ".join(tokens[1:])
    if all(token == "zzname" for token in lowered.split()) or _PLEASANTRY.fullmatch(lowered):
        return True
    rest = " ".join(_NOW.sub(" ", _SOFTENERS.sub(" ", lowered)).split())
    if not rest:
        return True  # "Unfortunately", "I'm afraid", "at this time"
    if not re.search(_pattern("declined"), rest):
        return False
    left = re.sub(_pattern("declined"), " ", rest).split()
    return all(token in _DECLINE_FILLER for token in left) and sum(token in _SUBJECTS for token in left) <= 1


def _more_than_no(text: str, names: Iterable[str]) -> str:
    """The first part of what they wrote that is neither the rules' no nor on the list of pleasantries; "" when none is.

    Fails closed: a wording the list does not know ("Maybe in a few months",
    "I've shared your resume with our CTO", "We've decided not to move
    forward") is more than no, so the student answers it.
    """
    lines = [" ".join(line.translate(_FLAT_QUOTES).split()) for line in str(text).replace("\r\n", "\n").split("\n")]
    words = _name_words(names)
    for line in _without_signature([line for line in lines if line]):
        for part in _CLAUSE_BREAK.split(line):
            part = (part or "").strip(" '*_-")
            if part and not _part_is_no_or_pleasantry(part, words):
                return part
    return ""


def _unquoted(item: dict[str, Any], sent: Iterable[str] = ()) -> str:
    """A reply's own words: above the email it quotes, and anything typed into it (between its quoted lines, or
    below an Outlook header in lines that are not the student's own, ``sent``)."""
    between = written_between_quotes(str(item["data"].get("full_text") or ""), sent)
    return f"{item['text']}\n{between}".strip()


# Read with separators and digits gone (_no_reply): "no.reply", "do_not_reply", "noreply-jobs", "bounces2".
_NO_REPLY = re.compile(r"^(?:(?:no|donot|dont)reply[a-z]*|bounce[a-z]*|notifications?|mailerdaemon|postmaster)$")
# What any reply since the first email may not say, by either reading, for a thank-you to go.
_STUDENTS = ("call_scheduled", "offer", "paused")


def _open_reply(item: dict[str, Any], sent: Iterable[str]) -> str:
    """Whether an earlier reply keeps something open (a call, an offer, "later"), by Jev, the rules, or the rules on its own words."""
    readings = item["data"].get("readings") if isinstance(item["data"].get("readings"), dict) else {}
    said = {
        (readings.get("rules") or {}).get("status"),
        (readings.get("jev") or {}).get("label") if isinstance(readings.get("jev"), dict) else None,
        suggest_reply_status(_unquoted(item, sent))["status"],
    }
    found = sorted(status for status in said if status in _STUDENTS)
    return found[0].replace("_", " ") if found else ""


def _sent_texts(conn: sqlite3.Connection, target: dict[str, Any], user_id: str) -> list[str]:
    """What the student sent them, which a reply quotes: the first email, and the follow-up when one went."""
    texts = [str(target.get("email_body") or "")]
    if target.get("follow_up_body") and _followed_up(conn, target["id"], user_id):
        texts.append(str(target["follow_up_body"]))
    return [text for text in texts if text.strip()]


def _names(conn: sqlite3.Connection, target: dict[str, Any], user_id: str, *more: str) -> list[str]:
    """The names a plain no may use: the student's, the contact's, the company's, and whoever wrote."""
    from .preparation import confirmed_facts

    student = str(confirmed_facts(conn, user_id).get("name") or "")
    company = str(target.get("company") or "")
    return [student, str(target.get("contact_name") or ""), company, spoken_company(company), *more]


def _whole_text_kept(item: dict[str, Any]) -> bool:
    """A Gmail reply's whole message is on record (a pasted one is kept whole as its text)."""
    data = item["data"]
    return data.get("source") != "gmail" or ("full_text" in data and len(str(data["full_text"])) <= FULL_TEXT_LIMIT)


# --- Who a thank-you may go to: rules R1 to R7 ------------------------------------------------------
#
# A reply both readings call a decline can still be the wrong one to thank: a help desk's automatic
# acknowledgement, a job system's rejection sent in a recruiter's name, a blast the student was Bcc'd on.
# Each rule reads the reply's own headers as Gmail delivered them (mail_message.KEPT_HEADERS, kept on
# its reply_logged event); a reply whose headers are not on record, or cannot be read, fails them all.

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
    from .mail_trust import listed, registrable_domain

    text = str(host or "").strip().strip("<>").rsplit("@", 1)[-1].rstrip(".").casefold()
    return bool(text) and bool(listed(text, categories) or listed(registrable_domain(text) or "", categories))


def _job_system_mail(domain: str, target: dict[str, Any]) -> bool:
    """A domain a job system sends, relays or signs mail from: any list of mail_trust's shipped list but
    documentation domains (job boards and applicant-tracking systems included), unless it is the company's
    own (a reply from someone at LinkedIn or ADP, when LinkedIn or ADP is who they wrote to).

    The company's own only when both its website and its name say so: a
    careers page on a job system (website acme.bamboohr.com) is not Acme's
    domain, and a company named like one ("Lever Industries") does not own it.
    """
    from .contact_names import website_domain
    from .mail_trust import registrable_domain, sender_lists

    categories = tuple(category for category in sender_lists() if category not in _NOT_JOB_SYSTEMS)
    if not _job_system(domain, categories):
        return False
    company = str(target.get("company") or "")
    own = identity_tokens(company) | {"".join(normalized(company).split())}
    found = registrable_domain(str(domain).strip().strip("<>").rsplit("@", 1)[-1]) or ""
    site = registrable_domain(website_domain(str(target.get("website") or ""))) or ""
    return not (found and found == site and found.split(".", 1)[0] in own)


def _no_reply(local: str) -> bool:
    """A local part that takes no replies: no-reply, no.reply, do.not.reply, noreply-jobs, bounces2."""
    return bool(_NO_REPLY.match(re.sub(r"[^a-z]", "", str(local or "").casefold().split("+", 1)[0])))


def _company_inbox(local: str, target: dict[str, Any]) -> bool:
    """A local part that is the company's own name or slug, alone or with role words or a word that joins it
    ("acme", "acme-robotics", "acme.careers", "acmecareers", "careersacme", "teamacme", "joinacme")."""
    from .mail_trust import registrable_domain
    from .contact_names import GENERIC_LOCAL_PARTS, ROLE_INBOX_LOCAL_PARTS, ROLE_INBOX_QUALIFIERS, website_domain
    from .outreach_contacts import made_of

    company = str(target.get("company") or "")
    tokens = identity_tokens(company)
    compact = "".join(re.split(r"[^a-z0-9]+", str(local or "").casefold().split("+", 1)[0]))
    if not compact:
        return False
    names = {"".join(word for word in normalized(company).split() if word in tokens), "".join(normalized(company).split())}
    site = registrable_domain(website_domain(str(target.get("website") or ""))) or ""
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
      rules says how it was matched (data "reason", outreach.REPLY_REASONS):
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
    from .mail_trust import authenticate
    from .outreach_contacts import is_shared_inbox
    from .outreach_forms import ALWAYS_AUTOMATIC

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
        theirs = hosts_in("\n".join(_sent_texts(conn, target, target["user_id"]))) if readable else set()
        if (not return_paths or any(_job_system_mail(domain, target) for domain in (sender, *relays, *return_paths, *signers))
                or (readable and any(_job_system(host, _LINK_CATEGORIES) for host in hosts if host not in theirs))):
            failed.append("R5")
        if not readable:
            failed.append("links")
        # R6: one person's own address.
        local = sender.split("@", 1)[0]
        if not local or is_shared_inbox(sender) or _no_reply(local) or _company_inbox(local, target):
            failed.append("R6")
        # R7: Gmail vouches for who sent it.
        verdict = authenticate(message)
        if not verdict.ok or verdict.from_address != sender:
            failed.append("R7")
    except Exception:  # noqa: BLE001 - a header that cannot be read fails closed, and never ends a pass
        LOGGER.debug("Could not read the headers of a reply for outreach target %s", target.get("id"), exc_info=True)
        return unreadable
    return failed


def eligibility(conn: sqlite3.Connection, target: dict[str, Any], user_id: str) -> tuple[dict[str, Any] | None, str]:
    """(the decline to thank them for, "") when the company qualifies, else (None, why not)."""
    if target["status"] not in ELIGIBLE_STATUSES:
        return None, f"the company is marked {target['status']}"
    if thank_you_row(conn, target["id"], user_id) is not None:
        return None, "it already has its one thank-you"
    found = replies(conn, target["id"], user_id)
    reply = found[-1] if found else None
    if reply is None:
        return None, "no reply is on record"
    data = reply["data"]
    if data.get("source") != "gmail" or not data.get("gmail_id") or not data.get("from"):
        return None, "the latest reply was not read from Gmail, so there is no thread to answer"
    if not data.get("thread_id") or not data.get("message_id"):
        return None, "the latest reply has no thread or message id to answer in"
    if "full_text" not in data or len(str(data["full_text"])) > FULL_TEXT_LIMIT:
        return None, "the whole of their reply is not on record, so nothing typed between its quoted lines can be ruled out"
    blockers = thank_you_blockers(conn, target, reply)
    if blockers:
        return None, blocker_reason(blockers)
    sender = str(data["from"]).casefold()
    if _no_reply(sender.split("@", 1)[0]):
        return None, "their reply came from an address that takes no replies"
    reply_to = {address.strip() for address in str(data.get("reply_to") or "").casefold().split(",") if address.strip()}
    if reply_to and reply_to != {sender}:
        return None, "their reply asks for answers to go to another address, so it is left for you"
    readings = data.get("readings") if isinstance(data.get("readings"), dict) else {}
    rules = (readings.get("rules") or {}).get("status")
    jev = readings.get("jev") if isinstance(readings.get("jev"), dict) else {}
    if rules != "declined" or jev.get("label") != "declined":
        return None, f"not a plain decline to both readings: {_readings_words(readings)}"
    try:
        confidence = float(jev.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < MIN_CONFIDENCE:
        return None, f"Jev was only {round(confidence * 100)}% sure"
    # The strict reading covers what they wrote: above the quote, and anything typed into it. The quoted
    # lines themselves are the student's own email, which often asks for a call, so they are not read as
    # theirs; below an Outlook header, a line counts as the student's only when it is in what they sent.
    sent = _sent_texts(conn, target, user_id)
    if written_between_quotes(str(data["full_text"]), sent):
        return None, "they wrote between the lines of the email they quoted (or below its header), so it is left for you"
    problem = plain_decline_problem(_unquoted(reply, sent), _names(conn, target, user_id, str(data.get("from_name") or "")))
    if problem:
        return None, f"not a plain decline to the rules read strictly: {problem}"
    # Every earlier reply, from anyone there, must be a plain no too, by the same strict reading: a call or a
    # question from the Cc, which both readings may still call a decline, is the student's to answer.
    for earlier in found[:-1]:
        kept = _open_reply(earlier, sent)
        if kept:
            return None, f"an earlier reply reads as {kept}, and that is left for you"
        if not _whole_text_kept(earlier):
            return None, "the whole of an earlier reply is not on record, so it is left for you"
        problem = plain_decline_problem(
            _unquoted(earlier, sent), _names(conn, target, user_id, str(earlier["data"].get("from_name") or "")),
        )
        if problem:
            return None, f"an earlier reply is not a plain no ({problem}), and that is left for you"
    suggestion = target.get("reply_suggestion")
    if suggestion and suggestion.get("status") not in (None, "declined"):
        return None, f"a suggestion of {str(suggestion['status']).replace('_', ' ')} is waiting for you"
    if target.get("possible_reply_count"):
        return None, "an email from them that may be a reply is waiting for you to check"
    since = parse_app_instant(data.get("received_at")) or reply["at"]
    # Only a decline that arrived while the switch was on: turning it on never thanks an old one.
    switched_on = parse_app_instant(automation.on_since(conn, user_id, FEATURE))
    if switched_on is None or since < switched_on:
        return None, "their reply came before Send a thank-you when someone declines was turned on"
    if sent_since(conn, target["id"], user_id, since):
        return None, "something went to them after their reply"
    if not target.get("sent_at"):
        return None, "no email to them is on record before their reply"
    if target["contact_bounced"] or sender in target["bounced_addresses"] or target.get("bounced_at"):
        return None, "an email to them bounced"
    if not gmail_drafts_status(conn, user_id=user_id)["bounce_check"]:
        return None, "Gmail is not connected with read access"
    return reply, ""


def _note_ineligible(user_id: str, target_id: str, reason: str) -> None:
    """Say once, in the debug log, why a company gets no thank-you. Nothing is written for it."""
    key = (user_id, target_id, reason)
    with _NOTED_LOCK:
        if key in _NOTED:
            return
        _NOTED.add(key)
    LOGGER.debug("No thank-you for outreach target %s: %s", target_id, reason)


def due(conn: sqlite3.Connection, user_id: str) -> list[str]:
    """Companies with a logged reply, marked Replied or Declined, and no thank-you yet: the ones worth checking."""
    return [str(row[0]) for row in conn.execute(
        f"""
        SELECT t.id FROM outreach_targets t
        WHERE t.user_id=? AND t.status IN ({', '.join('?' for _ in ELIGIBLE_STATUSES)}) AND t.not_interested_at IS NULL
          AND NOT EXISTS (SELECT 1 FROM outreach_thank_yous y WHERE y.target_id=t.id)
          AND EXISTS (SELECT 1 FROM outreach_events e WHERE e.target_id=t.id AND e.event_type='reply_logged')
        ORDER BY t.updated_at, t.id
        """,
        (user_id, *ELIGIBLE_STATUSES),
    ).fetchall()]


# --- Planning ----------------------------------------------------------------------------


def _idempotency(target_id: str, gmail_id: str) -> tuple[str, str]:
    return f"thank-you:{target_id}:{gmail_id}", f"thank-you-status:{target_id}:{gmail_id}"


def _breaker_group(row: dict[str, Any]) -> str | None:
    """Both changes made for one decline (the thank-you and the status) count once toward the breaker."""
    parts = str(row.get("idempotency_key") or "").split(":")
    return ":".join(parts[1:]) if len(parts) >= 3 and parts[0] in {"thank-you", "thank-you-status"} else None


def plan(
    conn: sqlite3.Connection, target_id: str, *, user_id: str, provider_factory: Callable[[str, str], Any] | None,
    provider: str | None = None, now: datetime | None = None,
) -> dict[str, Any]:
    """Write and schedule the thank-you for one company, if it qualifies. Returns what happened.

    ``planned`` True when it was scheduled now; otherwise ``reason`` says why
    not (a company that does not qualify is noted once and nothing is written).
    """
    target = get_target(conn, target_id, user_id=user_id)
    reply, reason = eligibility(conn, target, user_id)
    if reply is None:
        _note_ineligible(user_id, target_id, reason)
        return {"target_id": target_id, "planned": False, "reason": reason}
    from .preparation import confirmed_facts

    student_name = " ".join(str(confirmed_facts(conn, user_id).get("name") or "").split())
    if not student_name:
        reason = "the student's name is not confirmed in their profile"
        _note_ineligible(user_id, target_id, reason)
        return {"target_id": target_id, "planned": False, "reason": reason}
    data = reply["data"]
    to_email = str(data["from"])
    to_name = recipient_name(str(data.get("from_name") or ""), to_email, target)
    inputs = {
        "decline": reply["text"][:4_000],
        "student_name": student_name,
        "greeting": greeting_line(target["company"], to_name, greeting_style(conn, user_id)),
        "recipient_name": to_name,
        "company": spoken_company(target["company"]),
        "company_full": target["company"],
    }
    body, generated_by = write(inputs, provider_factory=provider_factory, provider=provider)
    # Timed after the model call, which can take a while, so "at least ten minutes from now" still holds.
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    zone, basis = recipient_zone(conn, target, user_id=user_id)
    received = parse_app_instant(data.get("received_at")) or reply["at"]
    send_at = plan_send_at(received, zone, f"{target_id}:{data['gmail_id']}", now, seed=f"{target_id}:{THANK_YOU_KIND}")
    label = send_time_label(send_at, zone, basis)
    subject = _subject(str(data.get("subject") or ""), target.get("email_subject") or "")
    fingerprint = thank_you_fingerprint(to_email, to_name, subject, body, str(data["message_id"]), str(data["thread_id"]))
    readings = data.get("readings") or {}
    who = contact_first_name(to_name) or to_email
    company = spoken_company(target["company"])
    planned = {
        "reply_gmail_id": data["gmail_id"], "reply_message_id": data["message_id"], "thread_id": data["thread_id"],
        "to_email": to_email, "to_name": to_name, "subject": subject, "body": body, "generated_by": generated_by,
        "fingerprint": fingerprint, "send_at": send_at.isoformat(timespec="seconds"), "label": label,
        "timezone": getattr(zone, "key", "system-local"),
    }
    thank_you_key, status_key = _idempotency(target_id, str(data["gmail_id"]))
    evidence = {
        "subject": f"{to_name or to_email} declined for {company}",
        "company": company, "reply_gmail_id": data["gmail_id"], "reply_received_at": data.get("received_at"),
        "readings": readings, "planned_for": planned["send_at"], "label": label, "generated_by": generated_by,
    }
    confidence = (readings.get("jev") or {}).get("confidence")
    try:
        with conn:
            # Written first: a pause (or the switch turned off) during the model call stops it here.
            if (automation.pause_guard(conn, user_id) or automation.mode(conn, user_id, FEATURE) != "on"
                    or automation.requirement(conn, user_id, FEATURE)):
                return {"target_id": target_id, "planned": False, "paused": True, "reason": "paused or switched off"}
            # Checked again inside the write: a reply or a send that landed during the model call wins.
            latest = latest_reply(conn, target_id, user_id)
            if latest is None or latest["data"].get("gmail_id") != data["gmail_id"] or sent_since(conn, target_id, user_id, received):
                return {"target_id": target_id, "planned": False, "reason": "their reply or yours changed while it was being written"}
            row = automation.perform_in(
                conn, user_id=user_id, feature=FEATURE, action_type="outreach.thank_you", subject_kind="outreach_target",
                subject_id=target_id, after={"thank_you": planned}, evidence=evidence,
                summary=f"Thank-you to {who} at {company} scheduled for {_when_words(send_at, zone, basis)}",
                basis="decline:rules+jev", confidence=confidence, idempotency_key=thank_you_key, auto=True,
            )
            if row is None or row.get("status") != "applied" or row.get("created_at") != row.get("applied_at"):
                return {"target_id": target_id, "planned": False, "reason": "it already has its one thank-you"}
            log_event(conn, target_id, user_id, SCHEDULED_EVENT, detail=f"A thank-you to {to_email} goes out {label}")
            automation.perform_in(
                conn, user_id=user_id, feature=FEATURE, action_type="outreach.status", subject_kind="outreach_target",
                subject_id=target_id, after={"status": "declined", "only_from": "replied"},
                evidence={**evidence, "subject": f"{company} declined, by both the rules and Jev"},
                summary=f"Marked {company} Declined: both the rules and Jev read their reply as a no",
                basis="decline:rules+jev", confidence=confidence, idempotency_key=status_key, auto=True,
            )
    except Exception as exc:
        from .database import is_unique_violation

        if not is_unique_violation(exc):
            raise
        return {"target_id": target_id, "planned": False, "reason": "another pass scheduled it first"}
    return {"target_id": target_id, "company": company, "planned": True, "send_at": planned["send_at"], "label": label,
            "generated_by": generated_by}


def _record(conn: sqlite3.Connection, user_id: str, *, ok: bool, error: str = "", detail: dict[str, Any] | None = None) -> None:
    record_health_quietly(conn, user_id, HEALTH_COMPONENT, ok=ok, error=error, detail=detail)


def run_for_user(
    conn: sqlite3.Connection, user_id: str, report: dict[str, Any], *,
    provider_factory: Callable[[str, str], Any] | None, provider: str | None = None, now: datetime | None = None,
) -> None:
    """One worker pass for one student: plan up to PLAN_PER_PASS thank-yous, and record the pass's health.

    Nothing runs while the switch is off or automation is paused. A failure is
    recorded as outreach.thank_you's health and raised for the worker to record too.
    """
    if not automation.is_enabled(conn, user_id, FEATURE):
        return
    missing = automation.requirement(conn, user_id, FEATURE)
    if missing:
        _record(conn, user_id, ok=False, error=f"No thank-yous are planned: {missing}")
        return
    planned: list[dict[str, Any]] = []
    try:
        for target_id in due(conn, user_id):
            if len(planned) >= PLAN_PER_PASS or not automation.is_enabled(conn, user_id, FEATURE):
                break
            outcome = plan(conn, target_id, user_id=user_id, provider_factory=provider_factory, provider=provider, now=now)
            if outcome.get("planned"):
                planned.append(outcome)
    except Exception as exc:
        LOGGER.exception("Planning thank-yous failed")
        rollback_quietly(conn, LOGGER, "planning a thank-you failed")
        _record(conn, user_id, ok=False, error=step_error(exc))
        raise
    report.setdefault("thank_yous", []).extend(planned)
    _record(conn, user_id, ok=True, detail={"planned": len(planned)})


# --- Settling a thank-you ----------------------------------------------------------------


def notice_reason(state: str, note: str) -> str:
    """Why a thank-you stopped, in fixed words: a notice can become a desktop pop-up, which never carries
    email text, and a reviewer's problems often quote their reply. The card keeps the full reason."""
    for start, words in _NOTICE_REASONS:
        if note.startswith(start):
            return words
    if any(words in note for words in ("may have gone out", "Check your Gmail Sent folder", "Gmail did not confirm", "Gmail did not answer")):
        return "it may have gone out, so check your Gmail Sent folder"
    return "open Outreach to see why"


def settle_in(conn: sqlite3.Connection, target_id: str, user_id: str, state: str, note: str = "") -> bool:
    """Move a thank-you to ``state`` with why, inside the caller's transaction, and say so. True when it moved.

    A held or failed one also leaves the student a notice (the only notices
    this feature leaves). A thank-you already sent or cancelled stays as it is.
    """
    if state not in STATES:
        raise ValueError(f"Unknown thank-you state: {state}")
    stamp = utc_now()
    if state == "sent":
        return bool(conn.execute(
            "UPDATE outreach_thank_yous SET state='sent', note='', updated_at=? WHERE target_id=? AND user_id=? AND state<>'sent'",
            (stamp, target_id, user_id),
        ).rowcount)
    moved = conn.execute(
        "UPDATE outreach_thank_yous SET state=?, note=?, updated_at=? WHERE target_id=? AND user_id=? "
        "AND state IN ('planned', 'scheduled', 'sending', 'transmitting', 'held', 'failed')",
        (state, note[:500], stamp, target_id, user_id),
    ).rowcount
    if not moved:
        return False
    event = {"cancelled": CANCELLED_EVENT, "held": HELD_EVENT, "failed": FAILED_EVENT}.get(state)
    if event:
        log_event(conn, target_id, user_id, event, detail=note[:1_000])
    if state in {"held", "failed"}:
        row = conn.execute("SELECT company FROM outreach_targets WHERE id=? AND user_id=?", (target_id, user_id)).fetchone()
        company = spoken_company(row["company"]) if row is not None else "a company"
        # 'failed' covers a send Gmail may have carried out, so it is "stopped", never "was not sent".
        title = f"Thank-you to {company} {'held' if state == 'held' else 'stopped'}: {notice_reason(state, note)}"
        automation.insert_notice(
            conn, user_id, event_key=f"thank-you:{state}:{target_id}:{stamp}", level="warning", title=title,
            body="Open Outreach to read it, then send it anyway or dismiss it.", timestamp=stamp,
        )
    return True


def back_in_line(conn: sqlite3.Connection, target_id: str, user_id: str) -> None:
    """A thank-you handed over but not sent (Gmail out of reach) waits again. Inside the caller's transaction."""
    conn.execute(
        "UPDATE outreach_thank_yous SET state='scheduled', updated_at=? WHERE target_id=? AND user_id=? AND state='transmitting'",
        (utc_now(), target_id, user_id),
    )


def schedule_moved(conn: sqlite3.Connection, row: Any, send_at: datetime, label: str) -> None:
    """Its scheduled send was given a new time (the next weekday morning): the thank-you shows the same. Inside the caller's transaction."""
    conn.execute(
        "UPDATE outreach_thank_yous SET send_at=?, label=?, updated_at=? WHERE target_id=? AND user_id=? AND state='scheduled'",
        (send_at.isoformat(timespec="seconds"), label, utc_now(), row["target_id"], row["user_id"]),
    )


def schedule_handed_over(conn: sqlite3.Connection, row: Any, stamp: str) -> None:
    """Its scheduled send was handed to Gmail: the thank-you is transmitting. Inside the hand-over's transaction."""
    conn.execute(
        "UPDATE outreach_thank_yous SET state='transmitting', updated_at=? WHERE target_id=? AND user_id=? AND state='scheduled'",
        (stamp, row["target_id"], row["user_id"]),
    )


# --- Just before it goes -----------------------------------------------------------------

REVIEW_INSTRUCTIONS = """You check a thank-you email before it is sent automatically on a university student's behalf.
Nobody else reads it first, so be strict: when unsure, do not send.

The student cold-emailed a company about an internship, and someone there replied. The JSON input has the company, the
student's first email (and follow-up, if one went), every reply that came back, who wrote the latest reply (from its
own From and Reply-To headers), and the thank-you about to go out. Each reply's "whole_message" is the message as it
arrived, including the parts of the student's email it quotes: lines starting with ">", or everything below a
"From:" and "Sent:" header or an "Original Message" line, which marks no line. People sometimes answer
between the quoted lines or below them, so read every line that is not the student's own words, wherever it is.

Answer each question:
1. Is every reply, not only the latest, free of anything the student should answer themselves: a question, a referral
   to someone else, a "later", "next year" or "keep in touch", an offer, or anything about a call or a meeting? Is the
   latest reply a plain decline? If not, do not send.
2. Does the thank-you only thank them: no ask, no promise, and no claim that is not in the thread? If not, do not send.
3. Is it addressed to the person who wrote the latest reply, at the address they wrote from, with a greeting that
   fits their name? If not, do not send.
4. Is the tone right: short, warm and gracious, with no pressure, disappointment, guilt or sarcasm? If not, do not send.

List every problem you found, in one plain sentence each. send is true only when you found none.
Reply with exactly one JSON object and nothing else:
{"send": true, "problems": []}"""


def _followed_up(conn: sqlite3.Connection, target_id: str, user_id: str) -> bool:
    for row in conn.execute(
        "SELECT event_type, to_status, detail FROM outreach_events WHERE target_id=? AND user_id=? AND event_type IN (?, 'status')",
        (target_id, user_id, SENT_EVENT),
    ).fetchall():
        if row["event_type"] == "status" and row["to_status"] == "followed_up":
            return True
        if row["event_type"] == SENT_EVENT and json_dict(row["detail"]).get("kind") == "follow_up":
            return True
    return False


def review(conn: sqlite3.Connection, target_id: str, *, user_id: str, runner: Callable[[str], str], reviewer: str) -> dict[str, Any]:
    """Whether the thank-you may go, with the reviewer's problems. Every failure to get a clear answer holds it."""
    from .outreach_review import ask_reviewer

    target = get_target(conn, target_id, user_id=user_id)
    thank_you = thank_you_row(conn, target_id, user_id)
    held: dict[str, Any] = {"send": False, "reviewer": reviewer}
    if thank_you is None:
        return {**held, "problems": ["The thank-you is no longer there"]}
    found = replies(conn, target_id, user_id)
    decline = next((item for item in found if item["data"].get("gmail_id") == thank_you["reply_gmail_id"]), None)
    if decline is None:
        return {**held, "problems": ["The reply it answers is no longer on record"]}
    payload: dict[str, Any] = {
        "company": target["company"],
        "first_email": {"sent_on": target["sent_at"], "subject": target["email_subject"], "body": target["email_body"]},
        "replies": [
            {"on": item["at"].date().isoformat(), "from": str(item["data"].get("from") or ""),
             "from_name": str(item["data"].get("from_name") or ""),
             # The whole message when it was kept (quoted lines and inline answers too), else its words above the quote.
             # A thank-you goes only when every reply was kept whole within FULL_TEXT_LIMIT, so none is cut here.
             "whole_message": str(item["data"].get("full_text") or item["text"])[:FULL_TEXT_LIMIT]}
            for item in found
        ],
        # Read from the decline's own headers, not from the thank-you, so question 3 compares two things.
        "latest_reply_from": {"name": str(decline["data"].get("from_name") or ""), "email": str(decline["data"].get("from") or ""),
                              "reply_to": str(decline["data"].get("reply_to") or "")},
        "thank_you": {"to": {"name": thank_you["to_name"], "email": thank_you["to_email"]},
                      "subject": thank_you["subject"], "body": thank_you["body"]},
    }
    if target.get("follow_up_body") and _followed_up(conn, target_id, user_id):
        payload["follow_up"] = {"subject": target["follow_up_subject"], "body": target["follow_up_body"]}
    prompt = f"{REVIEW_INSTRUCTIONS}\n\nJSON input:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    # A reviewer that cannot run holds it, whatever it raised.
    answer, hold = ask_reviewer(runner, prompt, held, catch=(Exception,))
    if hold is not None:
        return hold
    send, problems = answer["send"], answer["problems"]
    if not send or problems:
        return {**held, "problems": [item[:300] for item in problems] or ["The reviewer did not pass it and gave no reason"]}
    return {"send": True, "problems": [], "reviewer": reviewer}


def _thread_news(conn: sqlite3.Connection, client_factory: Callable[[], Any], user_id: str, thank_you: dict[str, Any]) -> tuple[str, str]:
    """What Gmail's own thread shows after their decline: ("they" | "student" | "draft" | "", ""), or ("error", why).

    A reply the student typed in Gmail itself, sent or still a draft, is on record nowhere else.
    """
    try:
        with client_factory() as client:
            gmail = _Gmail(conn, client, user_id)
            response = gmail.request("GET", f"/threads/{quote(thank_you['thread_id'], safe='')}", params={"format": "minimal"})
    except GmailThrottled:
        return "throttled", "Gmail asked the app to slow down"
    except GmailAuthError:
        return "error", "Gmail needs to be reconnected"
    except httpx.HTTPError:
        return "error", "Gmail could not be reached"
    if response.status_code != 200:
        return "error", f"Gmail could not read their thread (HTTP {response.status_code})"
    try:
        messages = response.json().get("messages") or []
        decline = next(message for message in messages if str(message.get("id")) == thank_you["reply_gmail_id"])
        decline_at = int(decline.get("internalDate") or 0)
    except (ValueError, AttributeError, StopIteration, TypeError):
        return "error", "Their reply was not found in its thread"
    news: set[str] = set()
    for message in messages:
        if str(message.get("id")) == thank_you["reply_gmail_id"]:
            continue
        labels = message.get("labelIds") or []
        try:
            at = int(message.get("internalDate") or 0)
        except (TypeError, ValueError):
            return "error", "Gmail's thread could not be read"
        if at <= decline_at:
            continue
        # The student's reply, sent or still being written, is theirs. The app's own Edit draft never gets
        # here: Edit stops the automatic send first.
        news.add("student" if "SENT" in labels else "draft" if "DRAFT" in labels else "they")
    return next((what for what in ("student", "they", "draft") if what in news), ""), ""


def problem_now(
    conn: sqlite3.Connection, target_id: str, user_id: str, thank_you: dict[str, Any], *, manual: bool = False,
) -> tuple[str, str] | None:
    """What stops this thank-you going now, from the app's own records: (the state it ends in, why), or None.

    Read at every step before it goes (the check just before sending, again
    after the reviewer, the hand-over, and the claim for the one call to
    Gmail), so a reply or a send logged meanwhile, even while the reviewer
    ran, still stops it. They wrote again when a reply arrived after their
    decline, or was logged after the thank-you was planned whenever it
    arrived. ``manual`` is the student's own Send it anyway, which the
    company's status does not stop.
    """
    try:
        target = get_target(conn, target_id, user_id=user_id)
    except OutreachNotFoundError:
        return "cancelled", "The company is no longer in your outreach list"
    found = replies(conn, target_id, user_id)
    decline = next((item for item in found if item["data"].get("gmail_id") == thank_you["reply_gmail_id"]), None)
    if decline is None:
        return "cancelled", "The reply it answers is no longer on record"
    planned = parse_app_instant(thank_you.get("created_at"))
    for item in found:
        if item is decline:
            continue
        logged = parse_app_instant(item["created_at"])
        if (item["at"], item["created_at"]) > (decline["at"], decline["created_at"]) or (
            planned is not None and logged is not None and logged > planned
        ):
            return "cancelled", WROTE_AGAIN
    if sent_since(conn, target_id, user_id, decline["at"]):
        return "cancelled", STUDENT_WROTE
    if not manual and target["status"] == "paused":
        return "cancelled", "The company is marked Paused, so the thank-you was not sent"
    if not manual and target.get("not_interested_at"):
        return "cancelled", NOT_INTERESTED_STOP
    if not manual and target["status"] not in ELIGIBLE_STATUSES:
        return "cancelled", f"The company is now marked {target['status'].replace('_', ' ')}, so the thank-you was not sent"
    if target["contact_bounced"] or thank_you["to_email"].casefold() in target["bounced_addresses"] or target.get("bounced_at"):
        return "cancelled", "An email to them bounced, so the thank-you was not sent"
    if not manual and target.get("possible_reply_count"):
        # Held, not cancelled: when the student says it is not a reply, Send it anyway still offers it.
        return "held", MAY_HAVE_REPLIED
    return None


def blockers_now(conn: sqlite3.Connection, target_id: str, user_id: str, thank_you: dict[str, Any]) -> list[str]:
    """thank_you_blockers for the reply a planned thank-you answers, read from the records now."""
    target = get_target(conn, target_id, user_id=user_id)
    decline = next((item for item in replies(conn, target_id, user_id) if item["data"].get("gmail_id") == thank_you["reply_gmail_id"]), None)
    return ["headers"] if decline is None else thank_you_blockers(conn, target, decline)


def requirement_hold(conn: sqlite3.Connection, user_id: str) -> str:
    """Why a thank-you is held for something the switch needs (automation.REQUIREMENTS), in the card's words; "" when met."""
    if not automation.requirement(conn, user_id, FEATURE):
        return ""
    return JEV_OFF if automation.mode(conn, user_id, "jev_inbox_suggestions") != "on" else NO_ACCOUNT


def hand_over_stop(conn: sqlite3.Connection, row: Any, now: datetime) -> tuple[str, str] | None:
    """What stops a thank-you at the hand-over, inside its transaction after the pause guard; None to hand it over.

    ("held", why) when the switch or Jev inbox suggestions was turned off, or
    the sending address cleared (each is part of what lets it go unapproved,
    requirement_hold); ("cancelled", why) for
    problem_now; ("later", "") when it is now outside its window in their zone
    (a slow check ran past five), so it waits for their next weekday morning.
    """
    user_id = row["user_id"]
    if automation.mode(conn, user_id, FEATURE) != "on":
        return "held", SWITCHED_OFF
    missing = requirement_hold(conn, user_id)
    if missing:
        return "held", missing
    thank_you = thank_you_row(conn, row["target_id"], user_id)
    if thank_you is None or thank_you["state"] != "scheduled" or thank_you["fingerprint"] != row["fingerprint"]:
        return "cancelled", "The thank-you changed or was stopped after it was scheduled"
    stop = problem_now(conn, row["target_id"], user_id, thank_you)
    if stop:
        return stop
    zone, _basis = recipient_zone(conn, get_target(conn, row["target_id"], user_id=user_id), user_id=user_id)
    if not in_window(now, zone):
        return "later", ""
    return None


def gate(
    conn: sqlite3.Connection, row: Any, *, client_factory: Callable[[], Any], now: datetime,
    reviewer: Callable[[], tuple[str, Callable[[str], str]]] | None,
) -> str | None:
    """The checks after fresh_look, just before a thank-you is handed over. None to send; else the outcome it stopped with.

    The app's records first (problem_now), then Jev's switch, then the
    reviewer, which can take minutes; then the records again, for anything
    logged while it ran; and last Gmail's own thread, read just before the
    hand-over (which reads the records once more).
    """
    from .outreach_review import review_log_detail, review_runner

    target_id, user_id = row["target_id"], row["user_id"]
    thank_you = thank_you_row(conn, target_id, user_id)
    if thank_you is None or thank_you["state"] != "scheduled" or thank_you["fingerprint"] != row["fingerprint"]:
        finish_send(conn, row, "cancelled", "The thank-you changed or was stopped after it was scheduled")
        return "cancelled"
    stop = problem_now(conn, target_id, user_id, thank_you)
    if stop:
        finish_send(conn, row, *stop)
        return stop[0]
    missing = requirement_hold(conn, user_id)
    if missing:
        finish_send(conn, row, "held", missing)
        return "held"
    # The reply's own rules (R1 to R7), read again: the student's address or the company may have changed.
    blockers = blockers_now(conn, target_id, user_id, thank_you)
    if blockers:
        LOGGER.debug("Thank-you for outreach target %s not sent: %s", target_id, blocker_reason(blockers))
        finish_send(conn, row, "cancelled", blocker_note(blockers))
        return "cancelled"
    try:
        name, run = (reviewer or (lambda: review_runner("thank_you")))()
        verdict = review(conn, target_id, user_id=user_id, runner=run, reviewer=name)
    except Exception as exc:  # noqa: BLE001 - a reviewer that cannot be set up holds it
        finish_send(conn, row, "held", f"The reviewer could not run: {exc}"[:500])
        return "held"
    with conn:
        log_event(conn, target_id, user_id, REVIEWED_EVENT, detail=review_log_detail(name, verdict))
    if not verdict["send"]:
        finish_send(conn, row, "held", "The reviewer held it: " + "; ".join(verdict["problems"]))
        return "held"
    # The reviewer can take minutes, and the InboxWatcher keeps reading Gmail meanwhile.
    fresh = thank_you_row(conn, target_id, user_id)
    stop = problem_now(conn, target_id, user_id, fresh) if fresh is not None else ("cancelled", "The thank-you is no longer there")
    if stop:
        finish_send(conn, row, *stop)
        return stop[0]
    news, why = _thread_news(conn, client_factory, user_id, thank_you)
    if news == "throttled":
        hold = backoff_until(user_id)
        if hold is not None:
            return wait_for_gmail(conn, row, hold + GMAIL_HOLD_MARGIN)
        return hold_for_retry(conn, row, now, f"Could not read their thread in Gmail first: {why}")
    if news == "error":
        return hold_for_retry(conn, row, now, f"Could not read their thread in Gmail first: {why}")
    if news in {"they", "student", "draft"}:
        finish_send(conn, row, "cancelled", {"they": WROTE_AGAIN, "student": STUDENT_WROTE, "draft": DRAFT_STARTED}[news])
        return "cancelled"
    return None


def recover_stuck(conn: sqlite3.Connection, now: datetime, stuck_after: timedelta) -> int:
    """A Send it anyway cut off mid-way (the app stopped) leaves its thank-you 'sending' with nothing to finish it.

    Past ``stuck_after``, with no request still holding its claim, it is
    stopped ('failed') with a note to look in Gmail's Sent folder, so the card
    can offer it again; a claim it left unconfirmed makes the next Send it
    anyway ask for that look first. Returns how many were stopped.
    """
    from .outreach_gmail import send_claim_row, send_claim_held

    cutoff = (now - stuck_after).isoformat(timespec="microseconds")
    stopped = 0
    for row in conn.execute(
        "SELECT target_id, user_id FROM outreach_thank_yous WHERE state='sending' AND updated_at<?", (cutoff,),
    ).fetchall():
        claim = send_claim_row(conn, row["target_id"], row["user_id"], THANK_YOU_KIND)
        if claim is not None and send_claim_held(claim):
            continue
        with conn:
            if settle_in(conn, row["target_id"], row["user_id"], "failed", STUCK_SENDING):
                stopped += 1
    return stopped


# --- What the student can do on the card -------------------------------------------------


def _scheduled_state(conn: sqlite3.Connection, target_id: str, user_id: str) -> str | None:
    row = conn.execute(
        "SELECT state FROM outreach_scheduled_sends WHERE target_id=? AND user_id=? AND kind=?", (target_id, user_id, THANK_YOU_KIND),
    ).fetchone()
    return None if row is None else str(row["state"])


def _stop_schedule(conn: sqlite3.Connection, target_id: str, user_id: str, reason: str) -> None:
    conn.execute(
        "UPDATE outreach_scheduled_sends SET state='cancelled', error=?, updated_at=? "
        "WHERE target_id=? AND user_id=? AND kind=? AND state IN ('scheduled', 'sending', 'failed')",
        (reason[:500], utc_now(), target_id, user_id, THANK_YOU_KIND),
    )


def _unconfirmed_claim(conn: sqlite3.Connection, target_id: str, user_id: str) -> bool:
    """Whether Gmail may have carried out an earlier try at this thank-you (a send, or an Edit's draft) without saying so."""
    return conn.execute(
        "SELECT 1 FROM outreach_send_claims WHERE target_id=? AND user_id=? AND kind=? AND state='unconfirmed'",
        (target_id, user_id, THANK_YOU_KIND),
    ).fetchone() is not None


def _with_doubt(conn: sqlite3.Connection, target_id: str, user_id: str, state: str, reason: str) -> str:
    """A reason for closing a thank-you, which says so when an earlier try may have gone out."""
    if state == "failed" and _unconfirmed_claim(conn, target_id, user_id):
        return f"{reason.rstrip('.')}. Gmail never confirmed whether the earlier try went, so check your Gmail Sent folder."
    return reason


def cancel(conn: sqlite3.Connection, target_id: str, *, user_id: str, reason: str | None = None) -> bool:
    """Stop the thank-you (Cancel, or Dismiss for a held or stopped one). False when there was nothing left to stop."""
    get_target(conn, target_id, user_id=user_id)
    with conn:
        # Written first, so on SQLite the worker's hand-over and this cannot cross.
        automation.pause_guard(conn, user_id)
        before = thank_you_row(conn, target_id, user_id)
        state = before["state"] if before is not None else ""
        if reason is None:
            reason = "You cancelled it" if state in {"planned", "scheduled"} else "You dismissed it"
        reason = _with_doubt(conn, target_id, user_id, state, reason)
        _stop_schedule(conn, target_id, user_id, reason)
        # Read after the stop: a hand-over that won the race (on PostgreSQL, the row's lock) is too far along.
        if _scheduled_state(conn, target_id, user_id) == "transmitting":
            return False
        return _close(conn, target_id, user_id, reason)


def on_new_reply(conn: sqlite3.Connection, target_id: str, user_id: str) -> None:
    """They wrote again (a reply was just logged): a thank-you not yet on its way stops at once.

    The check just before sending would stop a waiting one anyway; this also
    stops a held or stopped one, so the card never offers Send it anyway for
    a thank-you their newer message may have overtaken. One already handed to
    Gmail is left to the claim's own check. Inside the caller's transaction.
    """
    _stop_open(conn, target_id, user_id, WROTE_AGAIN)


def on_not_interested(conn: sqlite3.Connection, target_id: str, user_id: str) -> None:
    """The student marked the company not interested: a thank-you not yet on its way stops, as on_new_reply. Inside the caller's transaction."""
    _stop_open(conn, target_id, user_id, NOT_INTERESTED_STOP)


def _stop_open(conn: sqlite3.Connection, target_id: str, user_id: str, why: str) -> None:
    row = conn.execute("SELECT state FROM outreach_thank_yous WHERE target_id=? AND user_id=?", (target_id, user_id)).fetchone()
    if row is None or row["state"] not in OPEN_STATES:
        return
    reason = _with_doubt(conn, target_id, user_id, str(row["state"]), why)
    _stop_schedule(conn, target_id, user_id, reason)
    if _scheduled_state(conn, target_id, user_id) == "transmitting":
        return
    _close(conn, target_id, user_id, reason)


def _close(conn: sqlite3.Connection, target_id: str, user_id: str, reason: str) -> bool:
    """Cancel a thank-you that can still be stopped, and say so. Inside the caller's transaction."""
    moved = conn.execute(
        f"UPDATE outreach_thank_yous SET state='cancelled', note=?, updated_at=? WHERE target_id=? AND user_id=? "
        f"AND state IN ({', '.join('?' for _ in OPEN_STATES)})",
        (reason[:500], utc_now(), target_id, user_id, *OPEN_STATES),
    ).rowcount
    if moved:
        log_event(conn, target_id, user_id, CANCELLED_EVENT, detail=reason[:1_000])
    return bool(moved)


def edit_in_gmail(conn: sqlite3.Connection, target_id: str, *, user_id: str, client_factory: Callable[[], Any]) -> dict[str, Any]:
    """Edit: stop the automatic send and put the thank-you in the student's Gmail Drafts, in their thread.

    The student edits and sends it there. If Gmail does not make the draft,
    the thank-you goes back to held (or stopped, if it was) so the student can
    still send or dismiss it; when Gmail may have made it without saying so,
    the note says to look in Drafts, and the draft's unconfirmed claim makes
    Send it anyway ask for that look first.
    """
    get_target(conn, target_id, user_id=user_id)
    with conn:
        automation.pause_guard(conn, user_id)
        row = thank_you_row(conn, target_id, user_id)
        if row is None:
            raise ThankYouChanged("There is no thank-you for this company")
        if row["state"] in {"transmitting", "sending"}:
            raise SendConflictError("The thank-you is being sent right now, so it can't be edited")
        if row["state"] not in OPEN_STATES:
            raise ThankYouChanged(f"The thank-you is {row['state']}, so there is nothing to edit")
        _stop_schedule(conn, target_id, user_id, EDITED)
        if _scheduled_state(conn, target_id, user_id) == "transmitting" or not _close(conn, target_id, user_id, EDITED):
            raise SendConflictError("The thank-you is being sent right now, so it can't be edited")
    try:
        return create_thank_you_draft(conn, target_id, user_id=user_id, client_factory=client_factory)
    except Exception as exc:
        if isinstance(exc, SendUnconfirmedError):
            note = f"{exc}. Delete any copy in your Gmail Drafts before sending this one"
        elif isinstance(exc, SendConflictError):
            note = str(exc)
        elif "Nothing was sent" in str(exc):
            note = f"Could not put it in your Gmail Drafts: {exc}"
        else:
            note = f"Could not put it in your Gmail Drafts ({exc}). Nothing was sent"
        with conn:
            conn.execute(
                "UPDATE outreach_thank_yous SET state=?, note=?, updated_at=? WHERE target_id=? AND user_id=? AND state='cancelled'",
                ("failed" if row["state"] == "failed" else "held", note[:500], utc_now(), target_id, user_id),
            )
        raise


def send_anyway(
    conn: sqlite3.Connection, target_id: str, *, user_id: str, fingerprint: str, client_factory: Callable[[], Any],
    sent_folder_check: str | None = None,
) -> dict[str, Any]:
    """Send it anyway: the student's own confirmed send of a held or stopped thank-you. A pause never stops it.

    Not after they wrote again or the student wrote to them: the thank-you is
    closed with why instead (problem_now), and the send claim checks it once more.
    """
    get_target(conn, target_id, user_id=user_id)
    with conn:
        row = thank_you_row(conn, target_id, user_id)
        if row is None:
            raise ThankYouChanged("There is no thank-you for this company")
        if row["state"] not in {"held", "failed"}:
            raise ThankYouChanged(f"The thank-you is {row['state']}; only a held or stopped one is sent from here")
        if row["fingerprint"] != fingerprint:
            raise ThankYouChanged("The thank-you changed after it was shown. Reload and check it before sending")
        stop = problem_now(conn, target_id, user_id, row, manual=True)
        if stop is None:
            conn.execute(
                "UPDATE outreach_thank_yous SET state='sending', updated_at=? WHERE target_id=? AND user_id=? AND state=?",
                (utc_now(), target_id, user_id, row["state"]),
            )
            _stop_schedule(conn, target_id, user_id, "You sent it yourself")
        else:
            _close(conn, target_id, user_id, _with_doubt(conn, target_id, user_id, row["state"], stop[1]))
    if stop is not None:
        raise ThankYouChanged(stop[1])
    try:
        return send_thank_you(
            conn, target_id, user_id=user_id, fingerprint=fingerprint, client_factory=client_factory,
            sent_folder_check=sent_folder_check, automatic=False,
        )
    except BaseException as exc:
        # Back to where it was, with why, so the card can offer it again.
        with conn:
            conn.execute(
                "UPDATE outreach_thank_yous SET state=?, note=?, updated_at=? WHERE target_id=? AND user_id=? AND state='sending'",
                (row["state"], (str(exc) or row["note"])[:500], utc_now(), target_id, user_id),
            )
        raise


def register() -> None:
    """Hand in everything the lower modules call this workflow for. Called once at startup (bootstrap.register_all).

    The automation breaker's grouping of a decline's two changes; the callbacks the outreach records and the Gmail
    send path make (outreach_callbacks); and the scheduler's hooks for a scheduled send of this kind
    (outreach_schedule.KindHooks), so outreach_schedule need not import this module.
    """
    automation.register_breaker_group(FEATURE, _breaker_group, "decline")
    outreach_callbacks.on_new_reply.register(on_new_reply)
    outreach_callbacks.on_not_interested.register(on_not_interested)
    outreach_callbacks.thank_you_problem_now.register(problem_now)
    register_kind(THANK_YOU_KIND, KindHooks(
        settle=settle_in, gate=gate, in_window=in_window, hand_over_stop=hand_over_stop, back_in_line=back_in_line,
        moved=schedule_moved, handed_over=schedule_handed_over, recover_stuck=recover_stuck,
    ))
