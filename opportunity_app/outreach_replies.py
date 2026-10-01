"""Reading a company's reply: why a message was taken as it was, whether it is a delivery failure, and which outcome it suggests.

Pure text rules over strings, with no database and no first-party import, so the thank-you reader, the inbox
capture and the outreach records all use the same rules. Each suggestion is one the student confirms.
"""

from __future__ import annotations

import re


# Why a message found in Gmail was taken as it was (outreach_inbox_messages.reason), in the student's words.
# {sender} is who wrote, {local} the part of their address before the @.
REPLY_REASONS = {
    # Logged as replies.
    "thread": "{sender} answered in the Gmail thread of your email",
    "written_to": "{sender} is an address you wrote to",
    "domain_person": "{sender} wrote to you from the company's own domain, as themselves and verified by Gmail",
    "confirmed": "you said {sender}'s email is their reply",
    # Possible replies.
    "thread_outsider": "{sender} wrote in the thread of your email, but is not an address at the company",
    "before_marked_sent": "{sender} wrote before the day you marked the email sent",
    "mailing_tool": "it was sent through a mailing or sales tool",
    "spam": "Gmail put it in Spam",
    "ambiguous": "more than one company you wrote to could have sent it",
    "mentions_company": "it names the company or your email's subject, from an address that is not the company's",
    "reply_to": "only its reply-to address is one you wrote to",
    "job_mail": "it looks like mail from a job-application system",
    "shared_address": "{local}@ is a shared inbox, not one person",
    "not_verified": "Gmail could not verify it came from the company's domain",
    "not_addressed": "you were not in its To or Cc line",
    "name_mismatch": "the sender's name does not match the address",
    "weak_domain": "its domain is only linked to the company through your contact's address",
    "found_late": "it arrived before this check existed, so the app did not count it then",
    "unreadable": "the app could not read its headers; open it in Gmail",
    "auto_generated": "it is marked as sent by an automated system",
    "copied_outsider": "{sender} is someone you copied, not an address at the company",
    # Set aside.
    "own": "your own email",
    "delivery": "a delivery notice",
    "trash": "you moved it to Trash",
    "before": "it arrived before your first email",
    "list": "a mailing-list email",
    "out_of_office": "an automatic reply",
    "acknowledgement": "an automatic receipt",
    "receipt": "a read receipt",
    "spam_unverified": "Gmail put it in Spam and could not verify the sender",
    "automated_sender": "an automated sender at the company's domain",
    "no_company": "from no company you wrote to",
    "gone": "no longer in Gmail",
}


def reply_reason(code: str, sender: str) -> str:
    """A reason code as a sentence about this sender."""
    template = REPLY_REASONS.get(code, code or "")
    return template.format(sender=sender or "They", local=str(sender or "").split("@", 1)[0])


# Ordered: the first match wins. Each is a suggestion the student confirms.
REPLY_PATTERNS = (
    ("offer", r"\b(pleased to offer|offer letter|extend (you )?an offer)\b", "It mentions an offer"),
    ("declined", r"\b(not (currently |actively )?hiring|no (open )?(positions|roles|openings|internships?)|not (able|in a position) to (offer|take|hire|bring)|won'?t be able to|not a fit|pass on this)\b", "It says they are not hiring or cannot take you on"),
    ("paused", r"\b(reach (back )?out (again )?(in|later|next|after)|check back|circle back|touch base (later|in|next)|next (semester|year|summer|spring|fall))\b", "It asks you to come back later"),
    ("call_scheduled", r"\b(schedule|set up|hop on|book|grab|find)\b.{0,40}\b(call|chat|meeting|zoom|time)\b|\bcalendly\b|\bwhen are you (free|available)\b|\byour availability\b", "It proposes a call or asks for your availability"),
)


# A delivery failure notice is not a reply: nobody at the company read the
# email. Read as "replied" it would close a company that never heard from the
# student, so it is checked before REPLY_PATTERNS and before Jev. "bounced" is
# not a status; applying it records the bounce (outreach_delivery.record_bounce).
BOUNCED = "bounced"
BOUNCE_REASON = "It is a delivery failure notice, not a reply: the email did not reach them"
_BOUNCE_NOTICE = re.compile(
    r"\b(mailer-daemon|mail delivery (subsystem|system|failed|failure)|delivery status notification \(failure\)"
    r"|undeliverable|undelivered mail|returned mail|delivery (has )?failed|could ?n[o']t be delivered"
    r"|message (was )?not delivered|address not found|recipient address rejected|user unknown|no such user"
    r"|mailbox (is )?(unavailable|not found|does not exist)|group you tried to contact|permission to post messages"
    r"|550[ -]5\.\d\.\d+)\b"
)
# Gmail is still trying; only a failure is a bounce.
_DELAY_NOTICE = re.compile(r"\(delay\)|\bdelivery (has been |is )?delayed\b|\bwill (retry|keep trying)\b")
_PERMANENT = re.compile(r"\(failure\)|\bpermanent(ly)?\b|\b5\d\d[ -]5\.\d\.\d+")


def bounce_notice(text: str) -> bool:
    lowered = " ".join(str(text).lower().split())
    if _DELAY_NOTICE.search(lowered) and not _PERMANENT.search(lowered):
        return False
    return bool(_BOUNCE_NOTICE.search(lowered))


def suggest_reply_status(text: str) -> dict[str, str]:
    if bounce_notice(text):
        return {"status": BOUNCED, "reason": BOUNCE_REASON}
    lowered = " ".join(str(text).lower().split())
    for status, pattern, reason in REPLY_PATTERNS:
        if re.search(pattern, lowered):
            return {"status": status, "reason": reason}
    return {"status": "replied", "reason": "They replied; nothing in it matched a more specific outcome"}
