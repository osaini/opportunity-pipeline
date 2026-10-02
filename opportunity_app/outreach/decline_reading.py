"""Reading a reply strictly: is it a no, and nothing else?

The rules' own reading (outreach_replies.suggest_reply_status) stops at its first match, so "we're not hiring, but happy to set up a
call" reads as declined there. A thank-you goes without the student reading the reply first, so it needs the stricter question
this module answers: ``plain_decline_problem`` returns "" only when the decline is there and no door is left open (a call,
"later", a referral, a job board, another person), nothing is asked, and every clause is the rules' own no, a stock
pleasantry, a greeting, or one of the names in the thread. ``readings_words`` says how the rules and Jev each read a reply, for
the card's reason when the two disagree.

Pure text, no database, so it can be tested and changed on its own.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from ..mail.classifiers import JEV_NOT_ASKED
from .replies import REPLY_PATTERNS


def readings_words(readings: dict[str, Any]) -> str:
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
