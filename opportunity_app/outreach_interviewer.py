"""Who the student will talk to on the call, and notes on them for getting them talking about themselves.

People like to talk about their own work. Call prep researches the interviewer
so the student can ask about their path, their decisions, and what they are
building, instead of spending the call on what the web already says.

Who (``find_interviewer``), in this order:

- the student's own entry on the company (``interviewer_name``, and
  ``interviewer_linkedin`` for the profile), which always wins;
- the mailbox: the people who wrote from the company's domain, as the app read
  them from the student's outreach inbox (outreach_inbox_messages, replies and
  possible replies alike, since a calendar invitation often waits as a
  possible reply). "The company's domain" is the inbox's own rule
  (outreach_inbox): the website's, the mail domains the company is known by, and
  the contact's when it shares the website's name; at a university, only the
  addresses the student wrote to. Whoever sent the calendar invitation for the
  soonest call still to come (the newest invitation when no date can be read
  from the subjects) is who the student is meeting; otherwise the latest person
  who wrote. A cancelled invitation withdraws that call. A shared inbox, a
  no-reply address, a scheduling inbox (interviews@), or a name made of role
  words is not a person and never counts, but an invitation it sent still
  gives the time of the call, and the person is then the latest who wrote;
- the contact the student emailed, when that is a named person. That is
  "unconfirmed" when a reply is on file that was not read for who wrote it, or
  someone wrote from another domain: the student is told to check, and nothing
  is looked up on LinkedIn until they name the person.

The notes (``read_interviewer``) come from their LinkedIn profile, read through
the student's LinkedIn test account (outreach_linkedin.py, with its account
checks). With no profile link on file, LinkedIn is searched for their name and
the company, and a result is used only when it is the one person whose result
names both; several leave the choice to the student. A profile is "confirmed"
only when its header carries the person's name and its work history names the
company (never its posts, and a one-word company only as it is written). A link
the student typed in with no name names the person from the profile's own
header, not from the mailbox. A profile that is not confirmed is kept with the
reason, and no notes are made from it.

A model turns the profile text into short notes, each with the words from the
profile it rests on, and each note is kept only when those words are found in
the profile and the note says no more than they do (the same check as company
research, outreach_research). The profile text itself is not stored, and neither
is the text of a refused note.
"""

from __future__ import annotations

import html
import json
import re
import sqlite3
import subprocess
import unicodedata
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from . import outreach_research as research
from . import quote_check
from .integrations.agent_providers import CliAgentProvider, complete_text
from .mail_message import mailbox_key
from .mail_trust import registrable_domain
from .outreach import log_event, get_target
from .outreach_identity import LEGAL_SUFFIXES
from .outreach_config import resolve_provider
from .outreach_contacts import is_shared_inbox
from .outreach_identity import (
    company_words,
    contact_domain,
    domain_of,
    is_institution,
    is_own,
    is_person,
    is_platform_host,
    own_domains,
    role_word,
    site_domain,
    university_alias,
    website_strength,
)
from .outreach_linkedin import CMD_META, LinkedInClient, LinkedInUnavailable, username_from
from .core.timestamps import parse_app_instant, utc_now
from .integrations.web_fetch import FetchResult

# Kinds of inbox message a person at the company wrote (outreach_inbox).
PERSON_KINDS = ("reply", "possible")
_INVITATION = re.compile(r"^(?:updated\s+invitation(?:\s+with\s+note)?|invitation)\s*:", re.IGNORECASE)
# Calendars withdraw an invitation with "Canceled event: Call @ ...", "Canceled event with note: ...", "Canceled: ...",
# "Cancelled event: ..." or "Invitation canceled".
_CANCELED = re.compile(r"^(?:cancell?ed(?:\s+event)?(?:\s+with\s+note)?\s*:|invitation\s+cancell?ed)", re.IGNORECASE)
# Words that make a mailbox or a display name a function's, not a person's (interviews@, "Acme Robotics Interviews").
_SCHEDULING_PART = re.compile(r"^(?:interview|schedul|calendar|meeting|booking|appointment|invite|invitation)", re.IGNORECASE)
_ROLE_NAME_ENDINGS = frozenset(
    "interview interviews interviewing recruiting recruitment team scheduling scheduler talent hiring careers calendar "
    "meeting meetings invites invitations notifications acquisition staffing".split()
)
_ADDRESS_GROUP = re.compile(r"\s*\([^()]*@[^()]*\)\s*$")
_DATE_WORD = (
    r"mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:r(?:s(?:day)?)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?|"
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?|\d{1,4}"
)
# "Invitation: Call @ Tue Sep 29, 2026 10am - 10:30am (CDT) (you@school.edu)": the time, after an "@" that starts a day or a date.
_WHEN = re.compile(rf"\s@\s+((?:{_DATE_WORD})\b.*)$", re.IGNORECASE)
_MONTHS = {name: number for number, name in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), start=1)}
_MEETING_DAY = re.compile(r"\b([a-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b|\b(\d{4})-(\d{2})-(\d{2})\b", re.IGNORECASE)
TOPICS = {
    "now": "What they do now",
    "path": "How they got here",
    "education": "Education",
    "posts": "What they post about",
    "other": "Also",
}
MAX_NOTES = 10
MAX_NOTE_CHARS = 250
RETRY_AFTER = timedelta(days=1)
# Sections of a profile where a person's work is named; their posts and schooling are not proof they work somewhere.
NOT_WORK_SECTIONS = ("posts", "education")

NOTES_INSTRUCTIONS = """You read one person's LinkedIn profile for a university student who will talk with them on a call. People enjoy talking about their own work, so the notes are hooks for questions that get this person talking about themselves: what they do now and what they are building, how they got here (earlier roles, changes of field, companies they founded), what they studied, and what they post or write about.

Write at most 10 notes. Each note:
- topic: one of now, path, education, posts, other.
- text: the hook as a short note the student will copy out by hand, under 15 words ("Boeing: flight test rigs, 12 aircraft programs"). Copy names, titles, numbers, and dates exactly as the profile writes them. Say nothing the quote does not say.
- quote: the words from the profile that state it, copied exactly, 5 to 30 words.

Rules:
- Use only the profile text given. Never add anything you know or guess about this person.
- Leave out contact details, anything about their family or health, and anything that is not about their work, studies, or public posts.
- Plain text, no markdown, no em dashes or en dashes.

Reply with exactly one JSON object and nothing else:
{"notes": [{"topic": "now", "text": "...", "quote": "..."}]}"""


def _person_name(name: str, company: str) -> str:
    """A name that reads as a person's: two or more words, not the company's own name or only role words ("Acme Recruiting Team")."""
    clean = " ".join(str(name or "").replace('"', "").split())
    clean = re.sub(r"\s*\((?:via\s+)?google calendar\)\s*$", "", clean, flags=re.IGNORECASE)
    words = quote_check.word_tokens(clean)
    if len(words) < 2 or " ".join(words) in quote_check.company_names(company) or CMD_META.search(clean):
        return ""
    ignore = set(company_words(company).split())
    if all(word in ignore or role_word(word) for word in words) or words[-1] in _ROLE_NAME_ENDINGS:
        return ""
    return clean


def _scheduling_inbox(address: str) -> bool:
    """Whether an address is a function's (interviews@, scheduling@, recruiting@, calendar@), never one person's."""
    local = str(address or "").split("@", 1)[0].casefold().split("+", 1)[0]
    return is_shared_inbox(address) or any(_SCHEDULING_PART.match(part) for part in re.split(r"[._\-]+", local) if part)


def _meeting(subject: str) -> str:
    """The time an invitation's subject gives ("Tue Sep 29, 2026 10am - 10:30am (CDT)"), or "" when it gives none."""
    title = _ADDRESS_GROUP.sub("", _INVITATION.sub("", subject or "").strip())
    match = _WHEN.search(title)
    return " ".join(match.group(1).split()) if match else ""


def _meeting_day(meeting: str) -> date | None:
    match = _MEETING_DAY.search(meeting or "")
    if not match:
        return None
    try:
        if match.group(4):
            return date(int(match.group(4)), int(match.group(5)), int(match.group(6)))
        month = _MONTHS.get(match.group(1).casefold())
        return date(int(match.group(3)), month, int(match.group(2))) if month else None
    except ValueError:
        return None


def company_domains(target: dict[str, Any]) -> tuple[dict[str, bool], set[str]]:
    """The domains the inbox counts as this company's, each with whether it is as good as proof, and the mailboxes written to.

    The same rules outreach_inbox uses to attach a message to a company, so the
    two never disagree: careers.acme.com is acme.com, a university's website
    stands for nobody at the university, and a contact stands for their own
    domain only when it shares the website's name.
    """
    own = own_domains()
    company = target["company"]
    site = site_domain(target.get("website") or "", own, company)
    domains: dict[str, bool] = {site: website_strength(target.get("website") or "", site, company)} if site else {}
    try:
        mail_domains = target.get("mail_domains") or json.loads(target.get("mail_domains_json") or "[]")
    except (TypeError, ValueError):
        mail_domains = []
    for other in (str(item).casefold() for item in mail_domains):
        if other and "." in other and not is_institution(other) and not is_own(other, own):
            domains[other] = True
    written: set[str] = set()
    for field in ("contact_email", "contact_cc"):
        address = str(target.get(field) or "").strip().casefold()
        if "@" not in address:
            continue
        domain, strong = contact_domain(address, site, own, company)
        if field == "contact_email":
            written.add(mailbox_key(address))
            if domain:
                domains[domain] = domains.get(domain, False) or strong
            continue
        # Someone only copied is the company's only when at one of its domains (outreach_inbox's rule): a copied outsider
        # at another company is never named as its interviewer.
        if domain and site:
            domains[domain] = domains.get(domain, False) or strong
        host = domain_of(address)
        if any(host == known or host.endswith(f".{known}") for known in domains):
            written.add(mailbox_key(address))
    return domains, written


def _at_company(sender: str, domains: dict[str, bool], written: set[str]) -> bool:
    """Whether a sender is at the company: an address the student wrote to, or at one of its domains.

    A university-wide domain (stateu.edu) counts only for the addresses written
    to, never for everyone at the university; a department's own host does.
    """
    if mailbox_key(sender) in written or (university_alias(sender) and university_alias(sender) in {university_alias(address) for address in written}):
        return True
    host = domain_of(sender)
    for own, strong in domains.items():
        if host == own or host.endswith(f".{own}"):
            if strong or not is_institution(own) or own != (registrable_domain(own) or own):
                return True
    return False


def _elsewhere(sender: str, at_company: bool) -> bool:
    """Whether a person-shaped sender the inbox tied to this company is at a domain it does not know."""
    host = domain_of(sender)
    return not at_company and bool(host) and not is_institution(host) and not is_platform_host(host) and not is_own(host, own_domains())


def _mailbox(
    conn: sqlite3.Connection, target: dict[str, Any], user_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """The people at the company who wrote, those tied to it from a domain it does not own, and the calendar
    invitations sent by an inbox that is not a person (interviews@); each newest first."""
    domains, written = company_domains(target)
    contact = mailbox_key(str(target.get("contact_email") or ""))
    contact_name = _person_name(target.get("contact_name") or "", target["company"])
    rows = conn.execute(
        f"""
        SELECT kind, sender, from_name, subject, received_at FROM outreach_inbox_messages
        WHERE user_id=? AND target_id=? AND kind IN ({', '.join('?' for _ in PERSON_KINDS)})
        ORDER BY received_at DESC
        """,
        (user_id, target["id"], *PERSON_KINDS),
    ).fetchall()
    people: dict[str, dict[str, Any]] = {}
    elsewhere: dict[str, dict[str, Any]] = {}
    calendar: dict[str, dict[str, Any]] = {}
    for row in rows:
        sender, from_name, subject, received = (str(row[1] or "").casefold(), row[2], str(row[3] or ""), str(row[4] or ""))
        name = _person_name(from_name, target["company"])
        # A reply from the exact address the student wrote to is the contact's, whatever short name it is signed with.
        if not name and contact and mailbox_key(sender) == contact:
            name = contact_name
        at_company = _at_company(sender, domains, written)
        if not name or _scheduling_inbox(sender) or not is_person(sender, target["company"]):
            # Not a person, but what a company's scheduling inbox invites the student to is still a call at the company.
            if at_company:
                group = calendar.setdefault(sender, {"name": "", "email": sender, "latest": received, "invitation": "", "invited_at": "", "settled": False})
                _settle(group, subject, received)
            continue
        if not at_company and not _elsewhere(sender, at_company):
            continue
        group = people if at_company else elsewhere
        person = group.setdefault(sender, {"name": name, "email": sender, "latest": received, "invitation": "", "invited_at": "", "settled": False})
        _settle(person, subject, received)
    return list(people.values()), list(elsewhere.values()), list(calendar.values())


def _settle(person: dict[str, Any], subject: str, received: str) -> None:
    """Read one message, newest first: a sender's newest calendar message decides whether they have a call coming; a
    cancellation withdraws the invitation before it, and an older invitation never stands in for a newer one."""
    if person["settled"]:
        return
    if _CANCELED.match(subject):
        person["settled"] = True
    elif _INVITATION.match(subject):
        person.update(invitation=subject, invited_at=received, settled=True)


def _next_call(invited: list[dict[str, Any]], today: date) -> dict[str, Any]:
    """The invitation for the call that is next: the soonest one still to come; the newest invitation when no date can be
    read from the subjects of those to come, and when every call is past."""
    def rank(person: dict[str, Any]) -> tuple[bool, bool, int, str]:
        day = _meeting_day(_meeting(person["invitation"]))
        coming = day is None or day >= today - timedelta(days=1)
        dated = coming and day is not None
        return coming, dated, -day.toordinal() if dated and day else 0, person["invited_at"]
    return max(invited, key=rank)


def mailbox_people(conn: sqlite3.Connection, target: dict[str, Any], user_id: str) -> list[dict[str, Any]]:
    """The people at the company who wrote to the student, newest first, with what they sent."""
    return _mailbox(conn, target, user_id)[0]


def find_interviewer(conn: sqlite3.Connection, target: dict[str, Any], user_id: str, now: datetime | None = None) -> dict[str, Any]:
    """Who the call is with, how that is known, and the others it could be. Empty name when nothing says."""
    if target.get("interviewer_name"):
        return {"name": target["interviewer_name"], "email": "", "basis": "student", "evidence": "You named them", "meeting": "", "others": [], "elsewhere": []}
    people, elsewhere, calendar = _mailbox(conn, target, user_id)
    invited = [person for person in people if person["invitation"]]
    today = (now or datetime.now(timezone.utc)).date()
    meeting = ""
    if invited:
        chosen = _next_call(invited, today)
        meeting = _meeting(chosen["invitation"])
        title = _ADDRESS_GROUP.sub("", _INVITATION.sub("", chosen["invitation"]).strip())
        evidence = f"Sent the calendar invitation \"{title[:160]}\""
        basis = "mailbox_invitation"
    elif people:
        chosen = people[0]
        booked = [entry for entry in calendar if entry["invitation"]]
        meeting = _meeting(_next_call(booked, today)["invitation"]) if booked else ""
        evidence = f"Wrote to you last, on {chosen['latest'][:10]}"
        basis = "mailbox_sender"
    else:
        contact = _person_name(target.get("contact_name") or "", target["company"])
        # A reply logged by hand, or one from another domain, was not read for who wrote it: the contact may not be who wrote.
        unread = bool(elsewhere or target.get("reply_count") or target.get("possible_reply_count"))
        others = [{"name": person["name"], "email": person["email"]} for person in elsewhere]
        if unread:
            named = f" ({', '.join(person['name'] for person in elsewhere)} wrote from another address)" if elsewhere else ""
            reason = f"a reply is on file that was not read for who wrote it{named}, so check this is who you are talking to"
        if contact and not unread:
            return {"name": contact, "email": target.get("contact_email") or "", "basis": "contact",
                    "evidence": "The person you emailed; no one else was found in your mailbox", "meeting": "", "others": [], "elsewhere": []}
        if contact:
            return {"name": contact, "email": target.get("contact_email") or "", "basis": "contact_unconfirmed",
                    "evidence": f"The person you emailed; {reason}", "meeting": "", "others": [], "elsewhere": others}
        return {"name": "", "email": "", "basis": "", "meeting": "", "others": [], "elsewhere": others,
                "evidence": reason[0].upper() + reason[1:] if unread else "No person at the company has written to you yet"}
    return {
        "name": chosen["name"], "email": chosen["email"], "basis": basis, "evidence": evidence,
        "meeting": meeting,
        "others": [{"name": person["name"], "email": person["email"]} for person in people if person is not chosen],
        "elsewhere": [{"name": person["name"], "email": person["email"]} for person in elsewhere],
    }


_DEGREE = re.compile(r"^\W*(?:1st|2nd|3rd\+?)\W*$", re.IGNORECASE)
# What ends one search result: the button under it, or an anonymous result the search gives no link for.
_RESULT_EDGE = re.compile(r"^\W*(?:linkedin member|connect|follow|message|pending|save)\W*$", re.IGNORECASE)


# Headings in a profile's text; the About section, between "About" and the next of these, is what a person says of themselves.
_HEADINGS = frozenset(
    "about experience education skills posts activity featured licenses projects volunteering recommendations "
    "languages honors interests courses publications".split()
)
_SEPARATORS = "·•|"


def _company_pattern(company: str) -> str:
    """The company's name as a regex: its written words, in any case (ChargeBot is Chargebot), and its legal suffix if written."""
    words = [word for word in re.findall(r"[\w&'-]+", company) if word.casefold() not in LEGAL_SUFFIXES]
    suffix = "|".join(re.escape(word) for word in sorted(LEGAL_SUFFIXES, key=len, reverse=True))
    return r"\s+".join(re.escape(word) for word in words) + rf"(?:[,.\s]+(?:{suffix})\.?)?" if words else ""


def _company_named(text: str, company: str) -> bool:
    """Whether work history text names the company as an employer, and not a common word that is its name.

    Either a line that is the company's name alone (an experience entry's
    company line, with "· Full-time" after it at most), or "Title at Company" or
    "Title @ Company" with the company as a whole word or phrase and not
    followed by another capitalised word ("Agent at Mercury Insurance" is not
    Mercury). About sections are what the person says of themselves and are not read.
    """
    name = _company_pattern(company)
    if not name:
        return False
    alone = re.compile(rf"^{name}\s*(?:[{_SEPARATORS}].*)?$", re.IGNORECASE)
    titled = re.compile(rf"(?:^|\s)(?:at|@)\s+({name})(?!\w)", re.IGNORECASE)
    about = False
    for line in (line.strip() for line in text.splitlines()):
        heading = line.rstrip(":").casefold()
        if heading in _HEADINGS:
            about = heading == "about"
            continue
        if about or not line:
            continue
        if alone.match(line):
            return True
        for match in titled.finditer(line):
            if not re.match(r"\s+[A-Z]", line[match.end():]):
                return True
    return False


def _folded(text: str) -> list[str]:
    """A name's words in lower case with the accents dropped (José Núñez is jose nunez)."""
    plain = "".join(char for char in unicodedata.normalize("NFKD", str(text or "")) if not unicodedata.combining(char))
    return quote_check.word_tokens(plain)


def _same_name(wanted: str, found: set[str]) -> bool:
    """Whether the words ``found`` (folded, from a profile or a result) carry the person's name.

    Every word of the name, accents aside; or, when the surname and any middle
    names match exactly, a given name that is the start of the other (3 letters
    or more): Dan is Daniel.
    """
    words = _folded(wanted)
    if not words:
        return False
    if set(words) <= found:
        return True
    given, surname = words[0], words[-1]
    if len(words) < 2 or surname not in found or not set(words[1:-1]) <= found:
        return False
    return any(len(word) >= 3 and len(given) >= 3 and (word.startswith(given) or given.startswith(word)) for word in found)


def _result_blocks(people: list[dict[str, str]]) -> list[str] | None:
    """Each search result's own text, in order, or None when the results cannot be lined up one to one with the page's text.

    The server gives one page of text for all results and links only for the
    people it can name, so a result with no link (an anonymous one) can sit
    between two that have. Each result runs from its own name, found in order,
    to the first of: the next linked result, an anonymous result, the button
    under it, another result's degree line ("2nd"), or its own name again.
    """
    texts = {person.get("text", "") for person in people}
    if len(texts) != 1:
        return None
    lines = [line.strip() for line in texts.pop().splitlines()]
    starts: list[int] = []
    cursor = 0
    for person in people:
        wanted = quote_check.word_tokens(person["name"])
        found = next((index for index in range(cursor, len(lines)) if wanted and quote_check.word_tokens(lines[index])[:len(wanted)] == wanted), None)
        if found is None:
            return None
        starts.append(found)
        cursor = found + 1
    blocks = []
    for number, start in enumerate(starts):
        end = starts[number + 1] if number + 1 < len(starts) else len(lines)
        own = quote_check.word_tokens(people[number]["name"])
        for index in range(start + 1, end):
            if _RESULT_EDGE.match(lines[index]) or quote_check.word_tokens(lines[index])[:len(own)] == own:
                end = index
                break
            # Another result's name is the line before its degree ("Dana Ortiz", then "3rd").
            if _DEGREE.match(lines[index]) and index > start + 1:
                end = index - 1
                break
        blocks.append("\n".join(lines[start:end]))
    return blocks


def pick_profile(people: list[dict[str, str]], name: str, company: str) -> tuple[str, list[dict[str, str]]]:
    """The one search result that is this person at this company, or "" and the people by that name to choose from."""
    named = [person for person in people if _same_name(name, set(_folded(person["name"])))]
    blocks = _result_blocks(named) if named else None
    matches = [person for person, block in zip(named, blocks or []) if _company_named(block, company)]
    if len(matches) == 1:
        return matches[0]["username"], []
    return "", [{"username": person["username"], "name": person["name"]} for person in named][:5]


def _profile_page(profile: dict[str, Any]) -> quote_check.ResearchPage:
    """The profile as a page with one line for each of its lines.

    The word checks hold a note to the lines around its quote, so a profile fed
    in as one long line would let a name or a number anywhere in it back any note.
    """
    lines = [line.strip() for text in profile["sections"].values() for line in str(text).splitlines() if line.strip()]
    return quote_check.ResearchPage(FetchResult(profile["url"], 200, "".join(f"<p>{html.escape(line)}</p>" for line in lines)))


def _header(profile: dict[str, Any]) -> list[str]:
    """The first lines of the profile's top card, where its owner's name is."""
    sections = profile["sections"]
    top = sections.get("main_profile") or next(iter(sections.values()), "")
    return [line.strip() for line in top.splitlines() if line.strip()][:3]


def profile_name(profile: dict[str, Any], company: str) -> str:
    """The name at the head of a profile, or "" when its first line does not read as a person's."""
    first = (_header(profile) or [""])[0]
    return _person_name(first, company) if len(first) <= 80 and len(first.split()) <= 6 else ""


def confirm_profile(profile: dict[str, Any], name: str, company: str) -> str:
    """"" when the profile is this person at this company, else why it is not.

    Its header must carry the person's name, and its work history (not its
    posts or schooling) must name the company.
    """
    header = {token for line in _header(profile) for token in _folded(line)}
    if not _same_name(name, header):
        theirs = profile_name(profile, company)
        return f"this profile is {theirs}, not {name}" if theirs and name else f"the profile's owner could not be checked against {name or 'a name'}"
    work = "\n".join(text for key, text in profile["sections"].items() if key not in NOT_WORK_SECTIONS)
    return "" if _company_named(work, company) else f"the profile's work history never names {company}"


def check_notes(
    raw: str, profile: dict[str, Any], name: str, judge: Callable[[str, str], str] | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """The notes whose words are in the profile and that say no more than those words; and the rest, with why.

    ``judge`` is the second read outreach_research uses for company facts (a separate call on the page's own
    passage, possibly the same model as the writer): it
    reads each note beside the profile's own lines around its quote, and a note
    it says no to, or gives no answer for, is not kept (two jobs merged into one, a title put on the
    wrong company). Without one, the word checks alone decide, as the notes are
    only hooks for the student's own questions.

    A refused note keeps only its topic and the reason: its text is model
    output the profile did not back, and may copy lines it should not keep.
    """
    parsed = CliAgentProvider.extract_json(raw)
    page = _profile_page(profile)
    kept: list[dict[str, str]] = []
    refused: list[dict[str, str]] = []
    for entry in parsed.get("notes") or []:
        if not isinstance(entry, dict):
            continue
        topic = entry.get("topic") if entry.get("topic") in TOPICS else "other"
        text = " ".join(str(entry.get("text") or "").split())[:MAX_NOTE_CHARS]
        quote = " ".join(str(entry.get("quote") or "").split())[:600]
        if not text:
            continue
        if len(kept) >= MAX_NOTES:
            refused.append({"topic": topic, "reason": f"over the {MAX_NOTES} notes kept"})
            continue
        span = page.find_quote(quote)
        if span is None:
            refused.append({"topic": topic, "reason": "the quoted words are not in the profile"})
            continue
        if quote_check.says_not(text) != quote_check.says_not(quote):
            refused.append({"topic": topic, "reason": "the note and its quote disagree on a not"})
            continue
        missing = page.missing(text, span, "", person=name)
        if missing:
            refused.append({"topic": topic, "reason": missing.replace("the quote and the lines around it", "the profile near the quote")})
            continue
        kept.append({"topic": topic, "text": text, "quote": quote, "_span": span})
    if judge is not None and kept:
        items = [{"id": f"n{index}", "fact": note["text"], "about": name, "page": profile["url"], **page.passage(note["_span"])}
                 for index, note in enumerate(kept)]
        verdicts = quote_check.second_read(items, judge)
        confirmed = []
        for index, note in enumerate(kept):
            verdict = verdicts.get(f"n{index}")
            if verdict is None:
                # A note nothing has read against the profile is a model's claim, so it is not kept as one that was checked.
                refused.append({"topic": note["topic"], "reason": "the second read of the profile gave no answer for it, so it was not kept"})
            elif not verdict[0]:
                refused.append({"topic": note["topic"], "reason": f"a second read of the profile says it does not state this: {verdict[1] or 'no reason given'}"})
            else:
                confirmed.append(note)
        kept = confirmed
    kept = [{key: value for key, value in note.items() if key != "_span"} for note in kept]
    if len(refused) > MAX_NOTES:
        refused = [*refused[:MAX_NOTES], {"topic": "", "reason": f"{len(refused) - MAX_NOTES} more refused"}]
    return kept, refused


def who_key(name: str, linkedin: str) -> str:
    return f"{' '.join(quote_check.word_tokens(name))}|{username_from(linkedin)}"


def interviewer_due(conn: sqlite3.Connection, target: dict[str, Any], user_id: str, now: datetime | None = None) -> bool:
    """Whether call prep should look the interviewer up.

    Always when who it is has changed (a new person wrote, or the student named
    someone or gave a link). Otherwise only when the last look found no notes,
    and not within a day of it.
    """
    record = interviewer_of(target)
    who = find_interviewer(conn, target, user_id, now)
    if who_key(who["name"], target.get("interviewer_linkedin") or "") != record.get("key"):
        return True
    if record.get("notes"):
        return False
    tried = parse_app_instant(target.get("interviewer_tried_at")) if target.get("interviewer_tried_at") else None
    return tried is None or (now or datetime.now(timezone.utc)) - tried >= RETRY_AFTER


def read_interviewer(
    conn: sqlite3.Connection,
    target_id: str,
    *,
    user_id: str,
    client: LinkedInClient,
    writer: Callable[[str, str], str] | None,
) -> dict[str, Any]:
    """Find who the call is with and read their LinkedIn profile into notes. Returns the target.

    ``writer`` sends the notes instructions and the profile text to a model and
    returns its reply; without one the profile's link is kept with no notes.
    Who it is is stored even when LinkedIn cannot be read, with the reason,
    whatever went wrong: the last person's record is never left standing.
    """
    target = get_target(conn, target_id, user_id=user_id)
    with conn:
        conn.execute("UPDATE outreach_targets SET interviewer_tried_at=? WHERE id=? AND user_id=?", (utc_now(), target_id, user_id))
    who = find_interviewer(conn, target, user_id)
    key = who_key(who["name"], target.get("interviewer_linkedin") or "")
    # Only how many people LinkedIn offered is kept: their names and profile links are other people's, and nothing shows them.
    record: dict[str, Any] = {**who, "key": key, "linkedin": None, "notes": [], "refused": [], "candidate_count": 0}
    error = ""
    try:
        link = username_from(target.get("interviewer_linkedin") or "")
        if not who["name"] and not link:
            raise LinkedInUnavailable(who["evidence"])
        if who["basis"] == "contact_unconfirmed" and not link:
            raise LinkedInUnavailable("Not searched on LinkedIn until you say who it is: add their name under Call prep")
        username = link
        if not username:
            username, candidates = pick_profile(
                client.search_people(f"{who['name']} {target['company']}"), who["name"], target["company"],
            )
            record["candidate_count"] = len(candidates)
        if not username:
            count = record["candidate_count"]
            found = (
                f"LinkedIn has several people named {who['name']}" if count > 1
                else f"LinkedIn has one person named {who['name']}, but their result does not name {target['company']}" if count
                else f"LinkedIn has no one found as {who['name']} at {target['company']}"
            )
            raise LinkedInUnavailable(f"{found}; add their profile link under Call prep")
        profile = client.profile(username)
        if link and not target.get("interviewer_name"):
            # A link the student typed in says who this is: the profile's own header, never the mailbox's person.
            theirs = profile_name(profile, target["company"])
            if theirs and (not who["name"] or not _same_name(who["name"], set(_folded(theirs)))):
                mailbox = f"; the mailbox names {who['name']}, who may be someone else" if who["name"] else ""
                who = {**who, "name": theirs, "email": "", "meeting": "", "basis": "student", "evidence": f"You gave their LinkedIn link{mailbox}"}
                record.update(name=who["name"], email="", meeting="", basis="student", evidence=who["evidence"])
        problem = confirm_profile(profile, who["name"], target["company"])
        record["linkedin"] = {"url": profile["url"], "username": username, "confirmed": not problem, "read_at": utc_now()}
        if problem:
            record["linkedin"]["why"] = problem
        elif writer is not None:
            raw = writer(NOTES_INSTRUCTIONS, json.dumps({"name": who["name"], "profile": profile["sections"]}, ensure_ascii=False))
            record["notes"], record["refused"] = check_notes(raw, profile, who["name"], judge=writer)
    except (LinkedInUnavailable, RuntimeError, ValueError, OSError) as exc:
        error = " ".join(str(exc).split())[:500]
    except subprocess.TimeoutExpired:
        error = "LinkedIn took too long to answer"
    except Exception as exc:  # noqa: BLE001 - whatever a client or a model writer does, the new person is stored, never the last one
        error = f"LinkedIn could not be read ({type(exc).__name__}): its answer was not one this understands"
    with conn:
        now = conn.execute("SELECT company, website FROM outreach_targets WHERE id=? AND user_id=?", (target_id, user_id)).fetchone()
        if now is not None and research.company_changed(target, now[0], now[1]):
            # Renamed while LinkedIn was read: the profile was matched to the old company, so it is not kept.
            log_event(conn, target_id, user_id, "interviewer_read", detail="The company changed during the look-up, so it was not kept")
            return get_target(conn, target_id, user_id=user_id)
        conn.execute(
            "UPDATE outreach_targets SET interviewer_json=?, interviewer_at=?, interviewer_error=?, updated_at=? WHERE id=? AND user_id=?",
            (json.dumps(record, ensure_ascii=False), utc_now(), error, utc_now(), target_id, user_id),
        )
        log_event(conn, target_id, user_id, "interviewer_read", detail=error or f"{record['name']}: {len(record['notes'])} notes from LinkedIn")
    return get_target(conn, target_id, user_id=user_id)


def web_interviewer(
    provider_factory: Callable[[str, str], Any], provider: str | None,
) -> Callable[[sqlite3.Connection, str, str], dict[str, Any]]:
    """Look up one company's interviewer with the student's LinkedIn test account and the call prep writer."""

    def look_up(conn: sqlite3.Connection, target_id: str, user_id: str) -> dict[str, Any]:
        return read_interviewer(conn, target_id, user_id=user_id, client=LinkedInClient(), writer=model_writer(provider_factory, provider))

    return look_up


def model_writer(provider_factory: Callable[[str, str], Any], provider: str | None) -> Callable[[str, str], str] | None:
    """The call prep writer (outreach_config.resolve_provider), or None when it is the no-AI template."""
    provider_id, model = resolve_provider(provider, purpose="call_prep")
    if provider_id == "legacy":
        return None
    agent = provider_factory(provider_id, model)
    return lambda instructions, content: complete_text(agent, instructions, content, max_output_tokens=1500)


def interviewer_of(target: dict[str, Any]) -> dict[str, Any]:
    record = target.get("interviewer") or {}
    return record if isinstance(record, dict) else {}
