"""What the student's answer to a company's reply says: the writer's instructions, the checks on its words, and the template.

An answer (outreach_reply_drafts) is a short email in the company's thread that the student reads, can edit, approves
and sends with their own two presses. Everything about its words is here, and nothing about which reply it answers,
when it is written, or how it goes. ``in_person`` is the line about meeting that a company near the student gets, from
the student's own profile; ``validate`` is every reason a text cannot be offered as a draft; ``template`` is the fixed
words used with no model; ``write`` asks a model, once more with the reasons if it was refused, and falls back to the
template. Whatever only the student can say (when they are free, an answer the profile does not hold) is left in
square brackets, and a draft with a bracket left in it cannot be approved (targets.draft_checks).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta
from typing import Any, Callable

from ..integrations import agent_providers
from .config import resolve_provider
from .drafting import OFFER_TO_SHARE_WORK, field_basis
from .location import location_region, location_usable, near_home, region_phrase, student_home
from .number_check import number_keys, supported_numbers
from .targets import draft_checks

LOGGER = logging.getLogger(__name__)

PURPOSES = ("answer", "revisit")
# What a reply was read as (outreach_replies.REPLY_PATTERNS and Jev), and how the writer is told to answer it.
READINGS = ("paused", "declined", "call_scheduled", "replied")
MAX_WORDS = {"answer": 90, "revisit": 130}
# A draft is refused this far past its target.
WORD_SLACK = 25
SIGN_OFFS = ("Best,", "Thanks,", "Thank you,", "Best regards,", "Cheers,")
# A student fact a reply may state. Contact details, pay and work authorization stay out.
STUDENT_FIELDS = ("name", "school", "degree", "graduation_year", "available_terms", "preferred_role_types")
# The fixed bases besides profile:<field> and research:<field>.
THEIR_REPLY, YOUR_EMAIL, IN_PERSON = "their_reply", "your_email", "in_person"
# When the student is near a company, but their profile does not say when they are home: what break_location means.
HOME_WHEN = "during breaks and summers"
SCHOOL_WHEN = "during the school year"
# Stock phrasing that reads as machine written in a reply.
FILLER = (
    "circling back", "circle back", "hope this email finds you", "hope this finds you", "i'm reaching out", "i am reaching out",
    "i'm writing to", "i am writing to", "passionate", "thrilled", "excited", "delve", "leverage", "cutting-edge",
    "innovative", "just following up", "touching base",
)
# A day or a time the student did not choose. Their availability is theirs to write, in the bracket left for it.
_WHEN = re.compile(
    r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|tues|thurs)\b"
    r"|\b\d{1,2}(:\d{2})?\s*(a\.?m\.?|p\.?m\.?)(?!\w)|\b\d{1,2}:\d{2}\b|\b\d{1,2}/\d{1,2}(/\d{2,4})?\b"
    r"|\b(noon|midnight|tomorrow|tonight)\b",
    re.IGNORECASE,
)
_BRACKETS = re.compile(r"\[[^\]\n]{1,60}\]")
_DASH = re.compile(r"[—–‒―]|\s-{1,2}\s|--")
_LINK = re.compile(r"https?://|www\.")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


INSTRUCTIONS = """You write a short email for a university student, answering a reply from a company the student cold-emailed
about an internship. The student reads it, can change it, and sends it themselves.

The JSON input has:
- their_reply: what the person wrote most recently, in their own words (the student's quoted email is left out), with
  earlier_replies before it.
- your_emails: what the student already sent them, oldest first.
- reading: what their reply was read as: "paused" (get back in touch later), "declined" (a no, perhaps with a door left
  open), "call_scheduled" (they want a call or a meeting), or "replied" (anything else, such as a question).
- purpose: "answer" (answer their reply now) or "revisit" (the student said they would get back in touch, and that time
  has come; revisit gives the date and their words).
- student and company: the only facts you may state about the student and the company.
- in_person: when not empty, the student is in the company's area at the time it names. Say so in one plain clause,
  in its own words for where and when, and that the student would be glad to meet in person then.
- greeting: the first line, exactly as written.
- today: today's date.

How to answer, by what they said:
- paused: thank them, and say the student will get back in touch at the time they named, in their terms ("around
  March"). If they named no time, do not invent one.
- declined: thank them for getting back to the student. If they left a door open (keeping the student on file, a later
  cycle, someone else to talk to), answer that one thing. Never argue, and never ask them to reconsider.
- call_scheduled: thank them and say the student would be glad to talk. Never choose, accept or propose a day, a date or
  a time: write "[your availability]" where the student's times go, for the student to fill in.
- replied: answer what they asked, using only the student facts. Anything only the student can answer goes in square
  brackets, such as "[your start date]", for the student to fill in.
- revisit: open by pointing back to what they said ("You mentioned getting back in touch around March"), then say in one
  sentence that the student would still like to intern for the earliest term in available_terms, and ask whether they
  would consider it. One short result from primary_experience is allowed, in that entry's own words.

Always:
- If they asked the student to do something (apply through their website, read their careers page, fill in a form), say
  plainly that the student will do it.
- If they questioned the student's fit or experience, acknowledge it in one plain, gracious clause. Never argue, defend
  with numbers, or overclaim.
- At most max_words words. Plain text, no markdown, no dashes between words, no links, no attachments.
- Sound like the student writing to a person: contractions, plain words, no gushing. Never write "circling back", "hope
  this email finds you", "I'm reaching out", "passionate", "excited", "thrilled", "leverage" or "delve".
- Use only facts in the JSON input. Never invent a date, a number, a name, an achievement, or anything about the company.
- Never offer to send or share documents, data, designs or code from the student's work.
- Put one blank line after the greeting and before the sign-off. End with a short sign-off ("Best," or "Thanks,") on its
  own line, then the student's name exactly as given, alone on the last line.
- List every factual claim with its basis: "profile:<field>" for a student fact, "research:<field>" for a company fact,
  "their_reply" for something they said, "your_email" for something the student already wrote to them, and
  "in_person" for the in-person clause.
- revisit_on: only when purpose is "answer" and their reply names a time to get back in touch: the first day that time
  could mean, as YYYY-MM-DD, after today ("around March of next year" is the first of March of the year after today's).
  revisit_said: their exact words for that time, copied from their_reply. Otherwise both are "".

Reply with exactly one JSON object and nothing else:
{"body": "...", "claims": [{"text": "...", "basis": "..."}], "revisit_on": "", "revisit_said": ""}"""


def in_person(facts: dict[str, Any], target: dict[str, Any], regions: list[dict[str, Any]] | None) -> dict[str, str]:
    """Where and when the student could meet this company in person, from their profile; {} when they could not.

    A company in the student's home area (break_location) gets their home and when they are there (break_times,
    or "during breaks and summers", which is what break_location means); one in their school's region, when that is
    not home too, gets the school's region during the school year. Only a checked location counts, as for the
    "(live in ...)" line of a first email (outreach_location.location_usable).
    """
    if not location_usable(target):
        return {}
    where = str(target.get("location") or "")
    home = student_home(facts, regions)
    if home and near_home(where, home, regions):
        when = "year-round" if home["year_round"] else " ".join(str(facts.get("break_times") or "").split()) or HOME_WHEN
        return {"where": home["phrase"], "when": when}
    school = location_region(str(facts.get("school") or ""), regions)
    if school and location_region(where, regions) == school:
        return {"where": region_phrase(school, regions), "when": SCHOOL_WHEN}
    return {}


def allowed_bases(inputs: dict[str, Any]) -> set[str]:
    bases = {f"profile:{field}" for field in inputs["student"]} | {f"research:{field}" for field in inputs["company"]}
    bases |= {THEIR_REPLY, YOUR_EMAIL}
    if inputs.get("in_person"):
        bases.add(IN_PERSON)
    if inputs.get("primary_experience"):
        bases.add("profile:primary_experience")
    return bases


def _plain(text: str) -> str:
    return " ".join(str(text or "").split()).casefold()


def _input_text(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if key != "max_words":
                yield from _input_text(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _input_text(item)
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        yield str(value)


def revisit_suggestion(parsed: dict[str, Any], inputs: dict[str, Any]) -> tuple[str, str]:
    """(revisit_on, revisit_said) when the reply names a time to come back and the model's date is plausible; else ("", "").

    The words must be theirs (copied from their reply), and the date on or after today and within two years. A date
    that fails is dropped rather than shown: the student can still pick one themselves.
    """
    if inputs.get("purpose") != "answer":
        return "", ""
    on = str(parsed.get("revisit_on") or "").strip()
    said = " ".join(str(parsed.get("revisit_said") or "").split())
    if not on or not said or not _DATE.match(on):
        return "", ""
    try:
        day = date.fromisoformat(on)
        today = date.fromisoformat(inputs["today"])
    except ValueError:
        return "", ""
    if not today < day <= today + timedelta(days=730):
        return "", ""
    if len(said) < 3 or _plain(said) not in _plain(inputs["their_reply"]["text"]):
        return "", ""
    return on, said[:200]


def validate(body: str, claims: Any, inputs: dict[str, Any]) -> tuple[list[dict[str, str]], list[str]]:
    """The claims kept, and every reason the words cannot be offered as a draft. Plain code only; no reasons passes."""
    text = body.replace("\r\n", "\n").strip()
    if not text:
        return [], ["it is empty"]
    problems: list[str] = []
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if lines[0] != inputs["greeting"]:
        problems.append(f"it opens with {lines[0][:80]!r}; open with {inputs['greeting']!r} on its own line")
    if lines[-1] != inputs["student"]["name"]:
        problems.append(f"it must end with the student's name, {inputs['student']['name']!r}, alone on the last line")
    checks = draft_checks("", text)
    limit = inputs["max_words"] + WORD_SLACK
    if checks["word_count"] > limit:
        problems.append(f"it runs {checks['word_count']} words, over the {inputs['max_words']} word target")
    unbracketed = _BRACKETS.sub(" ", text)
    if _DASH.search(unbracketed):
        problems.append("it uses a dash between words")
    if _LINK.search(unbracketed):
        problems.append("it has a link in it")
    allowed_numbers = supported_numbers(_input_text(inputs))
    extra = sorted({needed for _, needed in number_keys(unbracketed)} - allowed_numbers)
    if extra:
        problems.append("it states numbers found in none of the inputs: " + ", ".join(extra))
    when = _WHEN.search(unbracketed)
    if when:
        problems.append(
            f"it names a day or a time ({when.group(0)!r}); leave the student's availability in \"[your availability]\" for them to fill in"
        )
    lowered = text.casefold()
    filler = [phrase for phrase in FILLER if phrase in lowered]
    if filler:
        problems.append("it uses stock phrasing: " + ", ".join(repr(phrase) for phrase in filler))
    offer = OFFER_TO_SHARE_WORK.search(unbracketed)
    if offer:
        problems.append(f"it offers to hand over work material ({offer.group(0)!r}), which may belong to an employer")
    place = inputs.get("in_person") or {}
    if place and _plain(place["where"]) not in _plain(text):
        problems.append(f"it never says the student is in {place['where']} {place['when']} and would like to meet in person")
    allowed = allowed_bases(inputs)
    kept: list[dict[str, str]] = []
    for claim in claims if isinstance(claims, list) else []:
        if not isinstance(claim, dict):
            continue
        basis = field_basis(str(claim.get("basis") or "").strip())
        said = str(claim.get("text") or "").strip()[:500]
        if basis not in allowed:
            problems.append(f"the claim {said[:80]!r} cites {basis or 'nothing'}, which is not in the inputs")
        kept.append({"text": said, "basis": basis})
    if not kept:
        problems.append("it cites no basis for any of its claims")
    return kept, problems


def template(inputs: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    """The answer with no model: fixed words by what their reply was read as, which pass validate."""
    name = inputs["student"]["name"]
    company = inputs["company"]["company"]
    place = inputs.get("in_person") or {}
    claims = [{"text": name, "basis": "profile:name"}]
    meet = ""
    if place:
        meet = f" I'm in {place['where']} {place['when']}, and I'd be glad to meet in person then if that works for you."
        claims.append({"text": f"in {place['where']} {place['when']}", "basis": IN_PERSON})
    if inputs["purpose"] == "revisit":
        terms = inputs["student"].get("available_terms") or []
        term = f" for {terms[0]}" if terms else ""
        if terms:
            claims.append({"text": str(terms[0]), "basis": "profile:available_terms"})
        middle = (
            f"You mentioned I should get back in touch around now, so I wanted to follow up. I'd still like to intern at "
            f"{company}{term}. Would you consider me?{meet}"
        )
    elif inputs["reading"] == "call_scheduled":
        middle = f"Thank you for getting back to me. I'd be glad to talk. [your availability] works for me.{meet}"
    elif inputs["reading"] == "paused":
        middle = f"Thank you for getting back to me. I'll get back in touch then.{meet}"
    elif inputs["reading"] == "declined":
        middle = f"Thank you for getting back to me and for considering it. I appreciate it.{meet}"
    else:
        middle = f"Thank you for getting back to me. [your answer to their question]{meet}"
    claims.append({"text": "Thank you for getting back to me", "basis": THEIR_REPLY})
    return f"{inputs['greeting']}\n\n{middle.strip()}\n\nBest,\n{name}", claims


def _parsed(raw: str, inputs: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    try:
        parsed = agent_providers.CliAgentProvider.extract_json(raw)
    except ValueError:
        return {}, ["it was not one JSON object with a body"]
    body = str(parsed.get("body") or "").replace("\r\n", "\n").strip()
    claims, problems = validate(body, parsed.get("claims"), inputs)
    on, said = revisit_suggestion(parsed, inputs)
    return {"body": body, "claims": claims, "revisit_on": on, "revisit_said": said}, problems


def visible(inputs: dict[str, Any]) -> dict[str, Any]:
    """What the model is shown: the inputs, less what only the checks use."""
    return {key: value for key, value in inputs.items() if key not in {"names"}}


def write(
    inputs: dict[str, Any], *, provider_factory: Callable[[str, str], Any] | None, provider: str | None = None,
    comments: str = "", current: str = "",
) -> dict[str, Any]:
    """The answer's words, the claims they rest on, any revisit date their reply names, and who wrote it.

    The model gets one retry with the reasons it was refused. With no model set up the template is used; a model that
    fails twice raises ValueError with the reasons, so the student sees why rather than a template they did not ask for.
    ``comments`` are the student's notes on ``current`` (the draft they are looking at); they steer the words but are
    never a source.
    """
    provider_id, model = resolve_provider(provider, purpose="reply")
    if provider_id == "legacy" or provider_factory is None:
        if comments:
            raise ValueError("The template writer cannot follow comments. Clear them, or set up a model to write replies.")
        body, claims = template(inputs)
        return {"body": body, "claims": claims, "revisit_on": "", "revisit_said": "", "generated_by": "template"}
    content = json.dumps(visible(inputs), ensure_ascii=False, indent=2)
    if comments:
        content += (
            "\n\nThe student read the current draft and asked for changes. Follow their comments wherever they do not break a "
            "rule. The comments and the current draft are not sources: take every fact from the JSON input above.\n\n"
            f"Current draft:\n{current}\n\nThe student's comments:\n{comments}"
        )
    agent = provider_factory(provider_id, model)
    asked = content
    problems: list[str] = []
    for _attempt in range(2):
        written, problems = _parsed(agent_providers.complete_text(agent, INSTRUCTIONS, asked), inputs)
        if not problems:
            return {**written, "generated_by": f"{provider_id}:{model}"}
        asked = f"{content}\n\nYour previous draft was refused because " + "; ".join(problems) + ". Write it again following every rule."
    raise ValueError("The reply the model wrote did not pass the checks: " + "; ".join(problems))
