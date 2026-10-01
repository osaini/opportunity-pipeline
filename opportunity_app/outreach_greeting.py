"""How an outreach draft opens: the greeting line, and swapping it when the contact changes.

The greeting is the draft's first line, "Hi Dana," or "Hi Acme team,". The student's own wording (greeting word and
shared-inbox greeting) comes from their profile, never another student's. Pure string handling apart from
``greeting_style``, which reads the profile.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from .outreach_identity import company_key
from .outreach_location import owner_profile
from .schema import LOCAL_USER_ID


# The greeting is the draft's first line: "Hi Dana," or "Hi Acme team,". When
# the contact changes it is the only part written to the old one, so it is
# swapped here with no model call. The draft still goes back for approval.
_GREETING = re.compile(
    r"(?P<word>(?:hi|hello|hey|dear|good (?:morning|afternoon|evening))\s+)(?P<name>[^,!:\n]{1,80}?)(?P<end>\s*[,!:]?)",
    re.IGNORECASE,
)
# The greeting and the first sentence on one line: "Hi Alex, I'm writing...".
_LEADING_GREETING = re.compile(
    r"(?P<word>(?:hi|hello|hey|dear|good (?:morning|afternoon|evening))\s+)(?P<name>[^,!:\n]{1,80}?)(?P<end>\s*[,!:])(?P<rest>\s+\S.*)",
    re.IGNORECASE,
)
_HONORIFICS = {"dr", "mr", "mrs", "ms", "mx", "prof", "professor"}
# Greetings to nobody in particular, which a named contact improves on.
GENERIC_GREETINGS = {"there", "team", "all", "everyone", "hiring team", "recruiting team"}


# How a student opens an email, from their own profile (greeting_word and
# unnamed_greeting). These are the fallbacks when they have not said.
DEFAULT_GREETING = {"word": "Hi", "unnamed": "{company} team"}
_GREETING_WORD = re.compile(r"[^\W\d_][^\W\d_ '\u2019.-]*(?:[ '\u2019.-][^\W\d_]+){0,3}")
# The legal ending people leave off when they say a company's name.
_LEGAL_ENDING = re.compile(r"[,\s]+(?:inc|incorporated|corp|corporation|llc|ltd|pbc)\.?$", re.IGNORECASE)


def greeting_style_error(word: Any, unnamed: Any) -> str | None:
    """Why a greeting word or shared-inbox greeting cannot be used, or None."""
    if word not in (None, "") and not (isinstance(word, str) and len(word.strip()) <= 30 and _GREETING_WORD.fullmatch(word.strip())):
        return "The greeting word must be a word or two, like Hi, Hello, or Dear"
    if unnamed not in (None, ""):
        text = unnamed.strip() if isinstance(unnamed, str) else ""
        if not text or len(text) > 60 or "\n" in text or text.replace("{company}", "").count("{") or text.replace("{company}", "").count("}"):
            return "The shared-inbox greeting must be short, like {company} team or there"
    return None


def greeting_style(conn: sqlite3.Connection | None, user_id: str = LOCAL_USER_ID) -> dict[str, str]:
    """How this student greets: their own words, never another student's.

    The local owner's come from config/profile.json, like their regions; anyone
    else's from their own confirmed profile. Missing or unusable values fall
    back to DEFAULT_GREETING.
    """
    if conn is None or user_id == LOCAL_USER_ID:
        source = owner_profile()
    else:
        from .preparation import confirmed_facts

        source = confirmed_facts(conn, user_id)
    word = source.get("greeting_word")
    unnamed = source.get("unnamed_greeting")
    if greeting_style_error(word, None) or not word:
        word = DEFAULT_GREETING["word"]
    if greeting_style_error(None, unnamed) or not unnamed:
        unnamed = DEFAULT_GREETING["unnamed"]
    return {"word": " ".join(str(word).split()), "unnamed": " ".join(str(unnamed).split())}


def spoken_company(company: str) -> str:
    """The name people call a company by: "Acme Robotics, Inc." is "Acme Robotics"."""
    name = str(company or "").strip()
    while (shorter := _LEGAL_ENDING.sub("", name)) != name:
        name = shorter
    return name or str(company or "").strip()


def unnamed_greeting(company: str, style: dict[str, str]) -> str:
    return style["unnamed"].replace("{company}", spoken_company(company)).strip()


def greeting_line(company: str, contact_name: str, style: dict[str, str]) -> str:
    """The line a draft opens with: the contact's first name, or the student's shared-inbox greeting."""
    return f"{style['word']} {contact_first_name(contact_name) or unnamed_greeting(company, style)},"


def contact_first_name(name: str) -> str:
    words = [word for word in str(name or "").replace(",", " ").split() if word.rstrip(".").casefold() not in _HONORIFICS]
    return words[0] if words else ""


def _own_team(greeted: str, company: str) -> bool:
    """"acme robotics team" for Acme Robotics, Inc.; never another company's team."""
    return bool(company) and greeted.endswith(" team") and company_key(greeted[: -len(" team")]) == company_key(company)


def readdress_greeting(body: str, old_names: set[str], new_name: str, company: str = "") -> tuple[str, str, str] | None:
    """The body greeting ``new_name``, with the old and new greetings.

    The greeting is the first line, or the start of it when the first sentence
    follows on the same line. None when it does not greet one of ``old_names``
    (casefolded) or ``company``'s own team: a greeting the student wrote to
    someone else is theirs.
    """
    lines = body.split("\n")
    index = next((number for number, line in enumerate(lines) if line.strip()), None)
    if index is None:
        return None
    line = lines[index].strip()
    match = _GREETING.fullmatch(line) or _LEADING_GREETING.fullmatch(line)
    greeted = " ".join(match["name"].split()).casefold() if match else ""
    if not match or not (greeted in old_names or _own_team(greeted, company)):
        return None
    old_greeting = f"{match['word']}{match['name']}{match['end']}".strip()
    new_greeting = f"{match['word']}{new_name}{match['end'] or ','}"
    if new_greeting.strip() == old_greeting:
        return None
    lines[index] = new_greeting + (match.groupdict().get("rest") or "")
    return "\n".join(lines), old_greeting, new_greeting.strip()


def without_greeting(body: str) -> list[str]:
    """The body's lines with the greeting taken out: the first line when it is only a
    greeting, or its start when the first sentence follows on the same line."""
    lines = (body or "").split("\n")
    index = next((number for number, line in enumerate(lines) if line.strip()), None)
    if index is None:
        return lines
    line = lines[index].strip()
    leading = _LEADING_GREETING.fullmatch(line)
    if leading:
        return [*lines[:index], leading["rest"].strip(), *lines[index + 1:]]
    if _GREETING.fullmatch(line):
        return [*lines[:index], *lines[index + 1:]]
    return lines


def greets_contact(body: str, contact_name: str, company: str, style: dict[str, str]) -> bool:
    """Whether the body opens with a greeting that fits this contact.

    That is their first name, or a greeting to nobody in particular (the
    company's team, "there"). False for a greeting to anyone else, and for a
    body with no greeting line: the student looks before it goes on its own.
    """
    line = next((line.strip() for line in (body or "").split("\n") if line.strip()), "")
    match = _GREETING.fullmatch(line) or _LEADING_GREETING.fullmatch(line)
    if not match:
        return False
    greeted = " ".join(match["name"].split()).casefold()
    if greeted in GENERIC_GREETINGS or _own_team(greeted, company) or greeted == unnamed_greeting(company, style).casefold():
        return True
    first = contact_first_name(contact_name)
    return bool(first) and greeted == first.casefold()
