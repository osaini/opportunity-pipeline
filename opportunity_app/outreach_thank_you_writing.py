"""What a thank-you says: the writer's instructions, the checks on its words, the template it falls back to, and who it is addressed to.

The thank-you after a decline (outreach_thank_you) is one short email that thanks them and asks for nothing. Everything about
its words is here, and nothing about whether, when or to whom it goes. ``validate`` is every reason a text cannot go unread
by the student (too long, a question, an ask, a number from nowhere, a dash, a link); ``template`` is the fixed words that pass
it; ``write`` asks a model, once more with the reasons if it was refused, and falls back to the template; ``recipient_name``
and ``reply_subject`` take the greeting's name and the subject line from the reply's own headers.

It imports no workflow module, so the thank-you workflow, the scheduler and the Gmail send path can each use it.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

from . import agent_providers
from .outreach_greeting import spoken_company
from .outreach_identity import company_key
from .outreach_config import resolve_provider

# One logger for the whole thank-you feature (what its tests and the student's log filters name), whichever module logs.
LOGGER = logging.getLogger("opportunity_app.outreach_thank_you")

MAX_WORDS = 70
SIGN_OFF = "Best"


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
    try:
        parsed = agent_providers.CliAgentProvider.extract_json(raw)
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
            body, problems = _parsed(agent_providers.complete_text(agent, INSTRUCTIONS, asked), inputs)
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


def reply_subject(subject: str, fallback: str) -> str:
    text = " ".join(str(subject or fallback or "").split())
    return text if re.match(r"^re\s*:", text, re.IGNORECASE) else f"Re: {text}".strip()
