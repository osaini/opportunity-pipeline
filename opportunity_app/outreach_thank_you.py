"""A short thank-you after a plain decline, sent on its own (decline_thank_you). Phase 6, preliminary.

When a contact the student cold-emailed replies with a plain no, the app
writes a few lines of thanks and sends them in the same thread, with no
approval step. The student chose this for this one case, and chose no shadow
period: the switch is off until they turn it on. Everything else a reply can
say (a call, an offer, a question, a referral, "maybe later") stays theirs.

Deciding (``plan``, from the AutomationWorker, only while the switch is on,
automation is not paused, and Jev inbox suggestions are on):

- The company's latest reply was read from Gmail by capture_replies, from the
  contact, the Cc, or anyone at the company's domain (outreach_inbox's owner
  rules), and was not an automatic reply or a delivery failure.
- Both readings say declined: the keyword rules and Jev, each kept on the
  reply_logged event (inbox_classifiers.read_reply), Jev at least
  MIN_CONFIDENCE sure. With Jev off, paused, or unavailable at capture there is
  no Jev reading, so nothing is sent.
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
the pause, the hand-over guard, stuck recovery and missed mornings all apply.
Just before it goes (``gate``), every check fails closed: Gmail is read again
(fresh_look) and so is the thread itself; a new message from them, or anything
the student sent them, cancels it; a paused, moved-on, or bounced company
cancels it; and a second model (outreach_review.review_choice, a different
family when one is set up) must pass it, or it is held with the reason on the
card and a notice. The student can cancel it, move it to their Gmail Drafts to
edit (which stops the automatic send), or send a held one anyway.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import threading
from datetime import datetime, time, timedelta, timezone
from typing import Any, Callable
from urllib.parse import quote

import httpx

from . import automation
from .inbox_classifiers import MIN_CONFIDENCE
from .outreach import (
    OutreachNotFoundError,
    _log,
    company_key,
    contact_first_name,
    get_target,
    greeting_line,
    greeting_style,
    spoken_company,
)
from .outreach_gmail import (
    SENT_EVENT,
    THANK_YOU_DRAFT_EVENT,
    THANK_YOU_KIND,
    THANK_YOU_SENT_EVENT,
    GmailAuthError,
    GmailThrottled,
    SendConflictError,
    ThankYouChanged,
    _Gmail,
    backoff_until,
    create_thank_you_draft,
    gmail_drafts_status,
    send_thank_you,
    thank_you_fingerprint,
    thank_you_row,
)
from .outreach_schedule import _label, next_morning, recipient_zone
from .schema import utc_now

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
SWITCHED_OFF = "Send a thank-you when someone declines was turned off before it went, so it was not sent"
EDITED = "You chose to edit it yourself, so it went to your Gmail Drafts and was not sent automatically"

_NOTED: set[tuple[str, str, str]] = set()
_NOTED_LOCK = threading.Lock()


def _parse(value: Any) -> datetime | None:
    return automation._parse(value)


def _local(moment: datetime, zone: Any) -> datetime:
    return moment.astimezone(zone) if zone else moment.astimezone()


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
    local = _local(received, zone)
    if local.weekday() < 5 and local.time() < WORKDAY_END:
        delay = stable_delay(key)
        nine = datetime.combine(local.date(), WORKDAY_START)
        nine = (nine.replace(tzinfo=zone) if zone else nine.astimezone()).astimezone(timezone.utc)
        candidate = max(received + delay, nine + delay)
        send_at = max(candidate, now + DETECTION_MARGIN)
        there = _local(send_at, zone)
        if there.date() == local.date() and there.time() < WORKDAY_END:
            return send_at
    return next_morning(max(received, now), zone, seed or key)


def _when_words(send_at: datetime, zone: Any, basis: str) -> str:
    """"Tue 11:32 AM (their time)", as the activity feed says it."""
    local = _local(send_at, zone)
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
  them to reconsider or to pass anything on, and no promise to write again.
- No numbers, no attachment, no links, and no dashes of any kind between words.
- At most 60 words in all. Plain text, no markdown.
- End with a short sign-off line, then the student's name exactly as given, alone on the last line.

Reply with exactly one JSON object and nothing else:
{"body": "..."}"""

_ASKS = re.compile(
    r"\b(let me know|would you|could you|could we|can we|can you|will you|call(?:s|ed|ing)?|chat(?:s|ted|ting)?"
    r"|meet(?:s|ing|ings)?|connect(?:s|ed|ing)?|keep me in mind|reconsider\w*|keep in touch|stay in touch|reach out"
    r"|in the future|down the road|if anything changes|refer(?:ral|rals|red)?|introduc\w+|later|hope to hear"
    r"|look(?:ing)? forward|follow(?:ing)? up|touch base|circle back|opening|openings|position|positions|role|roles)\b",
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
    from .outreach_drafting import resolve_provider

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


def recipient_name(from_name: str, to_email: str, target: dict[str, Any]) -> str:
    """The name of the person who wrote, from their From header, or the contact's name when it was the contact.

    A shared inbox ("Acme Careers", "Hiring Team") names nobody, so it is left
    out and the student's shared-inbox greeting is used. "Lee, Dana" is Dana Lee.
    """
    name = " ".join(str(from_name or "").replace('"', " ").split())
    if "@" in name:
        name = ""
    if name.count(",") == 1:
        last, first = (part.strip() for part in name.split(","))
        name = f"{first} {last}".strip()
    company = spoken_company(str(target.get("company") or ""))
    if name and (_TEAM_WORDS.search(name) or (company and (
        company_key(name) == company_key(company) or re.search(rf"{re.escape(company)}", name, re.IGNORECASE)
    ))):
        name = ""
    if not name and to_email.casefold() == str(target.get("contact_email") or "").casefold():
        name = str(target.get("contact_name") or "")
    return name


def _subject(subject: str, fallback: str) -> str:
    text = " ".join(str(subject or fallback or "").split())
    return text if re.match(r"^re\s*:", text, re.IGNORECASE) else f"Re: {text}".strip()


# --- Whether a company qualifies ---------------------------------------------------------


def _data(text: Any) -> dict[str, Any]:
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


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
        data = _data(row["detail_json"])
        at = _parse(data.get("received_at")) or _parse(row["created_at"])
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
        at = _parse(row["created_at"])
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
        at = _parse(row["claimed_at"])
        if at is not None and at > since:
            return True
    return False


def _readings_words(readings: dict[str, Any]) -> str:
    rules = (readings.get("rules") or {}).get("status") or "nothing"
    jev = readings.get("jev") or {}
    said = jev.get("label") or f"nothing ({readings.get('jev_fallback') or 'Jev was off'})"
    return f"the rules read it as {rules}, and Jev as {said}"


def eligibility(conn: sqlite3.Connection, target: dict[str, Any], user_id: str) -> tuple[dict[str, Any] | None, str]:
    """(the decline to thank them for, "") when the company qualifies, else (None, why not)."""
    if target["status"] not in ELIGIBLE_STATUSES:
        return None, f"the company is marked {target['status']}"
    if thank_you_row(conn, target["id"], user_id) is not None:
        return None, "it already has its one thank-you"
    reply = latest_reply(conn, target["id"], user_id)
    if reply is None:
        return None, "no reply is on record"
    data = reply["data"]
    if data.get("source") != "gmail" or not data.get("gmail_id") or not data.get("from"):
        return None, "the latest reply was not read from Gmail, so there is no thread to answer"
    if not data.get("thread_id") or not data.get("message_id"):
        return None, "the latest reply has no thread or message id to answer in"
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
    since = _parse(data.get("received_at")) or reply["at"]
    # Only a decline that arrived while the switch was on: turning it on never thanks an old one.
    switched_on = _parse(automation.on_since(conn, user_id, FEATURE))
    if switched_on is None or since < switched_on:
        return None, "their reply came before Send a thank-you when someone declines was turned on"
    if sent_since(conn, target["id"], user_id, since):
        return None, "something went to them after their reply"
    if not target.get("sent_at"):
        return None, "no email to them is on record before their reply"
    sender = str(data["from"]).casefold()
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
        WHERE t.user_id=? AND t.status IN ({', '.join('?' for _ in ELIGIBLE_STATUSES)})
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


automation.register_breaker_group(FEATURE, _breaker_group, "decline")


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
    received = _parse(data.get("received_at")) or reply["at"]
    send_at = plan_send_at(received, zone, f"{target_id}:{data['gmail_id']}", now, seed=f"{target_id}:{THANK_YOU_KIND}")
    label = _label(send_at, zone, basis)
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
            _log(conn, target_id, user_id, SCHEDULED_EVENT, detail=f"A thank-you to {to_email} goes out {label}")
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
    from .outreach_inbox import _record as record

    record(conn, user_id, HEALTH_COMPONENT, ok=ok, error=error, detail=detail)


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        if getattr(conn, "in_transaction", False):
            conn.rollback()
    except Exception:  # noqa: BLE001
        LOGGER.warning("Could not roll back after planning a thank-you failed", exc_info=True)


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
        _rollback(conn)
        from .outreach_inbox import _step_error

        _record(conn, user_id, ok=False, error=_step_error(exc))
        raise
    report.setdefault("thank_yous", []).extend(planned)
    _record(conn, user_id, ok=True, detail={"planned": len(planned)})


# --- Settling a thank-you ----------------------------------------------------------------


def _plain(reason: str) -> str:
    """A reason fit for a notice: no address, no link, and short."""
    text = re.sub(r"https?://\S+", "[link]", str(reason or ""))
    text = re.sub(r"[^\s@<>\"'(),;:]+@[^\s@<>\"'(),;:]+", "[address]", text)
    text = " ".join(text.split()).rstrip(".")
    return text[:160]


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
        _log(conn, target_id, user_id, event, detail=note[:1_000])
    if state in {"held", "failed"}:
        row = conn.execute("SELECT company FROM outreach_targets WHERE id=? AND user_id=?", (target_id, user_id)).fetchone()
        company = spoken_company(row["company"]) if row is not None else "a company"
        title = (f"Thank-you to {company} held: {_plain(note)}" if state == "held"
                 else f"Thank-you to {company} was not sent: {_plain(note)}")
        automation._insert_notice(
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


# --- Just before it goes -----------------------------------------------------------------

REVIEW_INSTRUCTIONS = """You check a thank-you email before it is sent automatically on a university student's behalf.
Nobody else reads it first, so be strict: when unsure, do not send.

The student cold-emailed a company about an internship, and someone there replied. The JSON input has the company, the
student's first email (and follow-up, if one went), every reply that came back, who wrote the latest reply, and the
thank-you about to go out.

Answer each question:
1. Is the latest reply a plain decline: no question, no referral to someone else, no "later", "next year" or "keep in
   touch", and nothing about a call or a meeting? If not, do not send.
2. Does the thank-you only thank them: no ask, no promise, and no claim that is not in the thread? If not, do not send.
3. Is it addressed to the person who wrote the latest reply? If not, do not send.
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
        if row["event_type"] == SENT_EVENT and _data(row["detail"]).get("kind") == "follow_up":
            return True
    return False


def review(conn: sqlite3.Connection, target_id: str, *, user_id: str, runner: Callable[[str], str], reviewer: str) -> dict[str, Any]:
    """Whether the thank-you may go, with the reviewer's problems. Every failure to get a clear answer holds it."""
    from .outreach_review import _one_answer

    target = get_target(conn, target_id, user_id=user_id)
    thank_you = thank_you_row(conn, target_id, user_id)
    held: dict[str, Any] = {"send": False, "reviewer": reviewer}
    if thank_you is None:
        return {**held, "problems": ["The thank-you is no longer there"]}
    payload: dict[str, Any] = {
        "company": target["company"],
        "first_email": {"sent_on": target["sent_at"], "subject": target["email_subject"], "body": target["email_body"]},
        "replies": [
            {"on": item["at"].date().isoformat(), "from": str(item["data"].get("from") or ""), "text": item["text"][:4_000]}
            for item in replies(conn, target_id, user_id)
        ],
        "latest_reply_from": {"name": thank_you["to_name"], "email": thank_you["to_email"]},
        "thank_you": {"to": {"name": thank_you["to_name"], "email": thank_you["to_email"]},
                      "subject": thank_you["subject"], "body": thank_you["body"]},
    }
    if target.get("follow_up_body") and _followed_up(conn, target_id, user_id):
        payload["follow_up"] = {"subject": target["follow_up_subject"], "body": target["follow_up_body"]}
    prompt = f"{REVIEW_INSTRUCTIONS}\n\nJSON input:\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
    try:
        output = runner(prompt)
    except Exception as exc:  # noqa: BLE001 - a reviewer that cannot run holds it
        return {**held, "problems": [f"The reviewer could not run: {exc}"[:300]]}
    answer = _one_answer(output)
    if answer is None:
        return {**held, "problems": ["The reviewer's answer could not be read"]}
    send, problems = answer.get("send"), answer.get("problems")
    if not isinstance(send, bool) or not isinstance(problems, list) or not all(isinstance(item, str) for item in problems):
        return {**held, "problems": ["The reviewer's answer could not be read"]}
    if not send or problems:
        return {**held, "problems": [item[:300] for item in problems] or ["The reviewer did not pass it and gave no reason"]}
    return {"send": True, "problems": [], "reviewer": reviewer}


def _thread_news(conn: sqlite3.Connection, client_factory: Callable[[], Any], user_id: str, thank_you: dict[str, Any]) -> tuple[str, str]:
    """What Gmail's own thread shows after their decline: ("they" | "student" | "", ""), or ("error", why).

    A reply the student typed in Gmail itself is on record nowhere else.
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
    news = ""
    for message in messages:
        if str(message.get("id")) == thank_you["reply_gmail_id"]:
            continue
        labels = message.get("labelIds") or []
        try:
            at = int(message.get("internalDate") or 0)
        except (TypeError, ValueError):
            return "error", "Gmail's thread could not be read"
        if at <= decline_at or "DRAFT" in labels:
            continue
        if "SENT" in labels:
            return "student", ""
        news = "they"
    return news, ""


def gate(
    conn: sqlite3.Connection, row: Any, *, client_factory: Callable[[], Any], now: datetime,
    reviewer: Callable[[], tuple[str, Callable[[str], str]]] | None,
) -> str | None:
    """The checks after fresh_look, just before a thank-you is handed over. None to send; else the outcome it stopped with."""
    from .outreach_review import review_runner
    from .outreach_schedule import GMAIL_HOLD_MARGIN, _finish, _hold_for_retry, _wait_for_gmail

    target_id, user_id = row["target_id"], row["user_id"]
    try:
        target = get_target(conn, target_id, user_id=user_id)
    except OutreachNotFoundError:
        _finish(conn, row, "cancelled", "The company is no longer in your outreach list")
        return "cancelled"
    thank_you = thank_you_row(conn, target_id, user_id)
    if thank_you is None or thank_you["state"] != "scheduled" or thank_you["fingerprint"] != row["fingerprint"]:
        _finish(conn, row, "cancelled", "The thank-you changed or was stopped after it was scheduled")
        return "cancelled"
    found = replies(conn, target_id, user_id)
    decline = next((item for item in found if item["data"].get("gmail_id") == thank_you["reply_gmail_id"]), None)
    if decline is None:
        _finish(conn, row, "cancelled", "The reply it answers is no longer on record")
        return "cancelled"
    if any(item is not decline and (item["at"], item["created_at"]) > (decline["at"], decline["created_at"]) for item in found):
        _finish(conn, row, "cancelled", WROTE_AGAIN)
        return "cancelled"
    if sent_since(conn, target_id, user_id, decline["at"]):
        _finish(conn, row, "cancelled", STUDENT_WROTE)
        return "cancelled"
    if target["status"] == "paused":
        _finish(conn, row, "cancelled", "The company is marked Paused, so the thank-you was not sent")
        return "cancelled"
    if target["status"] not in ELIGIBLE_STATUSES:
        _finish(conn, row, "cancelled", f"The company is now marked {target['status'].replace('_', ' ')}, so the thank-you was not sent")
        return "cancelled"
    if target["contact_bounced"] or thank_you["to_email"].casefold() in target["bounced_addresses"] or target.get("bounced_at"):
        _finish(conn, row, "cancelled", "An email to them bounced, so the thank-you was not sent")
        return "cancelled"
    news, why = _thread_news(conn, client_factory, user_id, thank_you)
    if news == "throttled":
        hold = backoff_until(user_id)
        if hold is not None:
            return _wait_for_gmail(conn, row, hold + GMAIL_HOLD_MARGIN)
        return _hold_for_retry(conn, row, now, f"Could not read their thread in Gmail first: {why}")
    if news == "error":
        return _hold_for_retry(conn, row, now, f"Could not read their thread in Gmail first: {why}")
    if news == "they":
        _finish(conn, row, "cancelled", WROTE_AGAIN)
        return "cancelled"
    if news == "student":
        _finish(conn, row, "cancelled", STUDENT_WROTE)
        return "cancelled"
    try:
        name, run = (reviewer or (lambda: review_runner("thank_you")))()
        verdict = review(conn, target_id, user_id=user_id, runner=run, reviewer=name)
    except Exception as exc:  # noqa: BLE001 - a reviewer that cannot be set up holds it
        _finish(conn, row, "held", f"The reviewer could not run: {exc}"[:500])
        return "held"
    with conn:
        _log(conn, target_id, user_id, REVIEWED_EVENT, detail=(
            f"Passed by {name}" if verdict["send"] else f"Held by {name}: " + "; ".join(verdict["problems"])
        )[:1_000])
    if verdict["send"]:
        return None
    _finish(conn, row, "held", "The reviewer held it: " + "; ".join(verdict["problems"]))
    return "held"


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


def cancel(conn: sqlite3.Connection, target_id: str, *, user_id: str, reason: str = "You cancelled it") -> bool:
    """Stop the thank-you (Cancel, or Dismiss for a held one). False when there was nothing left to stop."""
    get_target(conn, target_id, user_id=user_id)
    with conn:
        # Written first, so on SQLite the worker's hand-over and this cannot cross.
        automation.pause_guard(conn, user_id)
        _stop_schedule(conn, target_id, user_id, reason)
        # Read after the stop: a hand-over that won the race (on PostgreSQL, the row's lock) is too far along.
        if _scheduled_state(conn, target_id, user_id) == "transmitting":
            return False
        return _close(conn, target_id, user_id, reason)


def _close(conn: sqlite3.Connection, target_id: str, user_id: str, reason: str) -> bool:
    """Cancel a thank-you that can still be stopped, and say so. Inside the caller's transaction."""
    moved = conn.execute(
        f"UPDATE outreach_thank_yous SET state='cancelled', note=?, updated_at=? WHERE target_id=? AND user_id=? "
        f"AND state IN ({', '.join('?' for _ in OPEN_STATES)})",
        (reason[:500], utc_now(), target_id, user_id, *OPEN_STATES),
    ).rowcount
    if moved:
        _log(conn, target_id, user_id, CANCELLED_EVENT, detail=reason[:1_000])
    return bool(moved)


def edit_in_gmail(conn: sqlite3.Connection, target_id: str, *, user_id: str, client_factory: Callable[[], Any]) -> dict[str, Any]:
    """Edit: stop the automatic send and put the thank-you in the student's Gmail Drafts, in their thread.

    The student edits and sends it there. If Gmail cannot make the draft, the
    thank-you is held (nothing was sent) so the student can still send or
    dismiss it.
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
        with conn:
            conn.execute(
                "UPDATE outreach_thank_yous SET state='held', note=?, updated_at=? WHERE target_id=? AND user_id=? AND state='cancelled'",
                (f"Could not put it in your Gmail Drafts ({exc}). Nothing was sent"[:500], utc_now(), target_id, user_id),
            )
        raise


def send_anyway(
    conn: sqlite3.Connection, target_id: str, *, user_id: str, fingerprint: str, client_factory: Callable[[], Any],
    sent_folder_check: str | None = None,
) -> dict[str, Any]:
    """Send it anyway: the student's own confirmed send of a held or stopped thank-you. A pause never stops it."""
    get_target(conn, target_id, user_id=user_id)
    with conn:
        row = thank_you_row(conn, target_id, user_id)
        if row is None:
            raise ThankYouChanged("There is no thank-you for this company")
        if row["state"] not in {"held", "failed"}:
            raise ThankYouChanged(f"The thank-you is {row['state']}; only a held or stopped one is sent from here")
        if row["fingerprint"] != fingerprint:
            raise ThankYouChanged("The thank-you changed after it was shown. Reload and check it before sending")
        conn.execute(
            "UPDATE outreach_thank_yous SET state='sending', updated_at=? WHERE target_id=? AND user_id=? AND state=?",
            (utc_now(), target_id, user_id, row["state"]),
        )
        _stop_schedule(conn, target_id, user_id, "You sent it yourself")
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
