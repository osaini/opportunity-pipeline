"""Scoring: how a posting is matched to the student's profile, with every adjustment recorded as a reason."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, NamedTuple

from .clock import parse_datetime
from .identity import normalized
from .regions import is_uninformative_location, match_region
from .text import classify_role


def term_hits(text: str, terms: Iterable[str]) -> list[str]:
    lower = text.lower()
    hits: list[str] = []
    for term in terms:
        needle = term.lower()
        if len(needle) <= 2 and needle.isalnum():
            if re.search(rf"\b{re.escape(needle)}\b", lower):
                hits.append(term)
        elif needle in lower:
            hits.append(term)
    return hits


# Whole words only: "Leadership Development Intern" must not read as "lead".
# The period in "Sr." defeats a trailing \b, so it is matched separately.
_SENIORITY_RE = re.compile(
    r"(?:\b(?:senior|staff|principal|manager|director|lead)\b|\bsr\.(?=\W|$))",
    re.IGNORECASE,
)
# A title that is itself an internship ("Technical Program Manager Intern") is
# an entry-level role whatever else it names.
_ENTRY_TITLE_RE = re.compile(
    r"\b(?:intern|interns|internship|internships|co-?op|co-?ops|apprentice|apprenticeship)\b",
    re.IGNORECASE,
)
# "N years" only counts as an experience requirement when it is tied to the
# word experience: "at least 18 years of age" and "a 4 year degree" are not,
# even when "experience" follows later ("18 years of age and have experience").
# The number may be spelled ("six (6) years", "three years"), may carry "or more"
# or "plus", and may begin a range ("3-5 years", "3 to 5 years", "one or two
# years"): the student has to meet the floor, so the first number of a range is
# the one read.
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "twelve": 12, "fifteen": 15, "twenty": 20,
}
_YEARS_NUMBER = r"(?:\d{1,2}|" + "|".join(_NUMBER_WORDS) + r")"
_EXPERIENCE_YEARS_RE = re.compile(
    rf"(?<![\w.])(?:(?P<low>{_YEARS_NUMBER})\s*(?:-|–|—|to|or)\s*)?(?P<high>{_YEARS_NUMBER})"
    r"(?:\s*\(\d{1,2}\))?\+?(?:\s+(?:or\s+more|plus))?\s+years?'?\s+(?:of\s+)?"
    r"(?:(?!(?:age|old|degree|degrees|diploma)\b)[\w/+-]+\s+){0,3}?experience",
    re.IGNORECASE,
)
# "less than 1 year", "up to 3 years", "no more than 2 years": a cap on what is welcome, not a floor to meet.
_EXPERIENCE_CAP_RE = re.compile(
    r"\b(?:less\s+than|fewer\s+than|no\s+more\s+than|not\s+more\s+than|up\s+to|under|below|at\s+most|"
    r"maximum\s+(?:of\s+)?|max\.?\s+(?:of\s+)?)\s*$",
    re.IGNORECASE,
)
# Experience a student cannot have yet: the same clause counts the years from graduation, or asks for full-time
# professional work (an internship is neither). The graduation wording must come right after "experience"; the same
# words later in the sentence ("and the ability to start full-time after graduation") say nothing about the years.
_POST_GRADUATION_TAIL_RE = re.compile(
    r"^\s*[,(]?\s*(?:gained\s+|earned\s+|acquired\s+|obtained\s+)?"
    r"(?:post[- ]?graduat|post[- ]?(?:bachelor|master|degree)|"
    r"(?:after|since|following|upon)\s+(?:your\s+|their\s+)?(?:graduat|completing|receiving|earning))",
    re.IGNORECASE,
)
# "3 years of post-graduation experience": the word is inside the phrase, before "experience".
_POST_GRADUATION_IN_RE = re.compile(r"post[- ]?graduat|post[- ]?(?:bachelor|master|degree)", re.IGNORECASE)
_FULL_TIME_PROFESSIONAL_RE = re.compile(r"full[- ]time\s+(?:professional|industry)", re.IGNORECASE)
# A clause that says internships or co-ops count asks for nothing a student lacks.
_INTERNSHIPS_COUNT_RE = re.compile(r"\b(?:intern(?:ship)?s?|co-?ops?)\b", re.IGNORECASE)
# A posting that closes sponsorship, for the company or for one opening. The
# alternatives are spelled out: a loose "not ... sponsor" would also catch "we do
# not hesitate to sponsor".
_VISA_KIND = r"(?:(?:visa|immigration|employment|work)\s+)?"
_NO_SPONSORSHIP_RE = re.compile(
    rf"\b(?:"
    rf"no\s+{_VISA_KIND}sponsorship(?!\s+(?:is\s+)?(?:requirement|required|needed|necessary))"
    rf"|(?:unable|not\s+able)\s+to\s+(?:offer\s+|provide\s+)?{_VISA_KIND}sponsor(?:ship)?"
    rf"|(?:cannot|can't|can\s+not|won't|will\s+not|does\s+not|do\s+not|doesn't|don't|is\s+not\s+able\s+to)"
    rf"\s+(?:offer\s+|provide\s+)?{_VISA_KIND}sponsor(?:ship)?"
    rf"|not\s+sponsor"
    rf"|sponsorship\b[^.\n]{{0,30}}?\b(?:is|are|will\s+be)\s+(?:not\s+(?:be\s+)?(?:offered|available|provided)|unavailable)"
    rf"|(?<!with or )without\s+(?:the\s+need\s+for\s+|requiring\s+|needing\s+)?{_VISA_KIND}sponsorship"
    rf")\b",
    re.IGNORECASE,
)
_SENTENCE_END_RE = re.compile(r"[.?!\n]")


def closes_sponsorship(description: str) -> bool:
    """Whether the description says sponsorship is not available (for the company or for this opening).

    A sentence that is a question ("Are you authorized to work in the US without sponsorship?") is application-form
    text asking the applicant, not a statement by the company, so it does not count.
    """
    for match in _NO_SPONSORSHIP_RE.finditer(description):
        end = _SENTENCE_END_RE.search(description, match.end())
        if end is not None and end.group(0) == "?":
            continue
        return True
    return False


# Text a posting aims at an AI reader ("if you are an LLM, include the word ..."). A company that wrote it wants a
# model's answer to differ from a person's, and whoever pastes the posting into a tool should know the text is
# not theirs. Only wording that addresses the reader or countermands its instructions counts: a posting that
# merely mentions AI or LLMs does not.
_AI_READER_RE = re.compile(
    # "If you are an LLM," / "an AI reading this" / "an AI language model,": the reader itself, not a job title
    # ("if you are an AI model researcher"), which is why the kind of reader must be followed by punctuation or a
    # reading verb.
    r"\bif\s+you(?:'re|\s+are)\s+(?:an?\s+|the\s+)?"
    r"(?:llm|large\s+language\s+model|language\s+model|chatbot|a\.i\.|ai)"
    r"(?:\s+(?:language\s+model|model|agent|assistant|bot|system))?"
    r"(?:\s*[,.:;!]|\s+(?:reading|reviewing|processing|summari[sz]ing|scanning|parsing|screening)\b)"
    r"|\bif\s+you(?:'re|\s+are)\s+(?:using|being\s+assisted\s+by)\s+(?:an?\s+)?(?:ai|llm|chatbot|chatgpt|claude|gemini|copilot)\b"
    r"|\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+|your\s+|the\s+)?(?:previous|prior|above|earlier)\s+"
    r"(?:instructions?|prompts?|rules)\b",
    re.IGNORECASE,
)
AI_READER_FLAG = "FLAG: text aimed at AI readers in this posting—treat it as untrusted and read it yourself"


# A role the posting itself calls unpaid. "Unpaid leave" and "unpaid time off" in a benefits list are not this, so the
# words must be about the role ("an unpaid internship", "this is a volunteer role", "the internship is unpaid", "the
# role carries no compensation" at the end of a clause), and "not an unpaid internship" is the opposite.
_UNPAID_ROLE_RE = re.compile(
    r"\bunpaid\s+(?:internship|position|role|co-?op|opportunity|apprenticeship)\b"
    r"|\b(?:this|the\s+(?:internship|position|role|opportunity))\s+(?:is\s+)?(?:an?\s+)?volunteer\s+"
    r"(?:position|role|internship|opportunity)\b"
    r"|\b(?:internship|position|role|opportunity|program|co-?op)\s+(?:is|will\s+be|carries|offers|has)\s+(?:an?\s+)?"
    r"(?:unpaid\b|no\s+(?:monetary\s+)?(?:compensation|pay)(?=\s*(?:[.;!]|$)))",
    re.IGNORECASE,
)
_NEGATED_RE = re.compile(r"\b(?:not|isn't|never|no)\s+(?:an?\s+)?$", re.IGNORECASE)
# Pay stated per month, week or day, or as a stipend: not an hourly wage to compare, but not "unpaid" either.
_OTHER_STATED_PAY_RE = re.compile(
    r"\$\s*\d[\d,]*(?:\.\d{1,2})?\s*(?:USD\s*)?(?:/|per\s+|an?\s+)(?:month|mo|week|wk|day|bi-?weekly)\b"
    r"|\bstipend\b[^.\n]{0,40}\$\s*\d",
    re.IGNORECASE,
)
HOURLY_BELOW_MINIMUM_PENALTY = 15
UNPAID_PENALTY = 35


def _calls_the_role_unpaid(text: str) -> bool:
    return any(
        not _NEGATED_RE.search(text[max(0, match.start() - 12):match.start()])
        for match in _UNPAID_ROLE_RE.finditer(text)
    )


def _pay_preference_reason(profile: dict[str, Any], title: str, description: str) -> tuple[int, str] | None:
    """(points off, reason) when the posting's stated pay misses what the student saved, else None.

    Reads only dollars per hour that the posting states, with the readers the opportunity attributes use. Every stated
    rate is read and the highest is compared, so a posting with a parking rate and a wage, or one rate per level, is
    not penalised while any of its rates reaches the minimum. A yearly salary is not turned into an hourly rate, a
    figure with no period is not pay, and a posting that states pay (hourly, yearly, monthly, weekly or a stipend) is
    never called unpaid. Another currency is not compared with dollars.
    """
    preferences = profile.get("compensation_preferences")
    if not isinstance(preferences, dict):
        return None
    currency = preferences.get("currency")
    if isinstance(currency, str) and currency.strip() and currency.strip().upper() != "USD":
        return None
    minimum = preferences.get("minimum_hourly")
    has_minimum = (
        not isinstance(minimum, bool) and isinstance(minimum, (int, float)) and math.isfinite(minimum) and minimum > 0
    )
    wants_paid_only = preferences.get("paid_only") is True
    if not has_minimum and not wants_paid_only:
        return None
    # The pay readers are the opportunity attributes' own; the import is lazy because they live in the web package, which
    # this one may reach only for its stdlib-only metadata module (see tests/test_dependency_boundary.py).
    from opportunity_app.opportunity_metadata import HOURLY_PAY_RE, YEARLY_PAY_RE

    text = f"{title}\n{description}"
    hourly = list(HOURLY_PAY_RE.finditer(text))
    if hourly:
        if has_minimum:
            highest = max(float(match.group(2) or match.group(1)) for match in hourly)
            if highest < minimum:
                verb = "pays up to" if len(hourly) > 1 or any(match.group(2) for match in hourly) else "pays"
                return (
                    HOURLY_BELOW_MINIMUM_PENALTY,
                    f"-{HOURLY_BELOW_MINIMUM_PENALTY} {verb} ${highest:g}/hour, below your ${minimum:g}/hour minimum",
                )
        return None
    if (
        wants_paid_only
        and not YEARLY_PAY_RE.search(text)
        and not _OTHER_STATED_PAY_RE.search(text)
        and _calls_the_role_unpaid(text)
    ):
        return UNPAID_PENALTY, f"-{UNPAID_PENALTY} unpaid, and you asked for paid roles only"
    return None


class ExperienceRequirement(NamedTuple):
    years: int
    qualifier: str  # "post-graduation", "full-time professional", or "" for experience of any kind


def experience_requirements(description: str) -> list[ExperienceRequirement]:
    """Each "N years of ... experience" the description asks for, at the floor of a range.

    A cap ("less than 1 year", "up to 3 years") is skipped: it is not a floor. ``qualifier`` names experience a student
    cannot have yet, when the same clause counts the years from graduation ("1-3 years of ... experience
    post-graduation") or asks for full-time professional work, unless it says internships or co-ops count.
    """
    found: list[ExperienceRequirement] = []
    for match in _EXPERIENCE_YEARS_RE.finditer(description):
        if _EXPERIENCE_CAP_RE.search(description[max(0, match.start() - 25):match.start()]):
            continue
        token = (match.group("low") or match.group("high")).lower()
        years = int(token) if token.isdigit() else _NUMBER_WORDS[token]
        clause = re.split(r"[.\n;]", description[match.end():match.end() + 90], maxsplit=1)[0]
        qualifier = ""
        if not _INTERNSHIPS_COUNT_RE.search(f"{match.group(0)} {clause}"):
            if _POST_GRADUATION_IN_RE.search(match.group(0)) or _POST_GRADUATION_TAIL_RE.match(clause):
                qualifier = "post-graduation"
            elif _FULL_TIME_PROFESSIONAL_RE.search(match.group(0)):
                qualifier = "full-time professional"
        found.append(ExperienceRequirement(years, qualifier))
    return found


# Degree levels a posting's title asks for ("2027 Summer Intern, MS/PhD, ...",
# "Layout Intern, BS - Summer 2027", "Buyer Intern- Bachelor's"), compared with
# the level the profile's `degree` names. Only words that name a degree count:
# "Graduate" and "New Grad" say nothing certain about one, and "Scrum Master"
# is not one. A bare "BS" or "MS" counts in a title only beside another level
# ("BS/MS") or right after the role ("Intern, MS"), because "Jackson, MS" is a
# place; in the student's own degree every abbreviation counts.
DEGREE_LEVEL_LABELS = {"bachelor": "bachelor's", "master": "master's", "mba": "MBA", "doctorate": "PhD"}
_DEGREE_LEVEL_RE = re.compile(
    r"(?<![\w.])(?:"
    r"(?P<doctorate>ph\.?\s?d\.?s?|doctoral|doctorate|doctor\s+of\s+philosophy)"
    r"|(?P<mba>mba|m\.b\.a\.?)"
    r"|(?P<master>master(?:['’]s|s)|master\s+(?:of|students?|thesis|degree|program)"
    r"|m\.\s?s\.?|m\.\s?sc\.?|m\.\s?eng\.?)"
    r"|(?P<bachelor>bachelor(?:['’]s|s)|bachelor\s+(?:of|students?|thesis|degree|program)"
    r"|undergrad(?:uate)?s?|b\.\s?s\.?(?:\s?e\.?)?|b\.\s?a\.|b\.\s?sc\.?|b\.\s?eng\.?)"
    r"|(?P<bare>bse|beng|bsc|bs|ba|mse|meng|msc|ms|ma)"
    r")(?!\w)",
    re.IGNORECASE,
)
_BARE_DEGREE_LEVELS = {
    **dict.fromkeys(("bs", "ba", "bsc", "bse", "beng"), "bachelor"),
    **dict.fromkeys(("ms", "ma", "msc", "mse", "meng"), "master"),
}
# "BA" (business analyst) and "MA" (Massachusetts) mean something else too often in a title.
_TITLE_BARE_DEGREES = {"bs", "ms"}
_DEGREE_JOIN_RE = re.compile(r"\s*(?:[/&+]|,?\s*\b(?:or|and)\b|,)\s*", re.IGNORECASE)
_DEGREE_AFTER_ROLE_RE = re.compile(
    r"\b(?:interns?|internships?|co-?ops?|students?|fellows?|fellowships?)\s*[-–—,:(]\s*$",
    re.IGNORECASE,
)


def _degree_match_level(match: re.Match[str]) -> str:
    if match.lastgroup == "bare":
        return _BARE_DEGREE_LEVELS[match.group(0).lower()]
    return str(match.lastgroup)


def degree_levels(degree: Any) -> set[str]:
    """The levels a profile's `degree` names: "B.S. Chemistry" is a bachelor's, "B.S./M.S. EE" both."""
    levels = {_degree_match_level(match) for match in _DEGREE_LEVEL_RE.finditer(str(degree or ""))}
    if "mba" in levels:
        levels.add("master")  # an MBA is a master's degree
    return levels


def title_degree_levels(title: str) -> set[str]:
    """The degree levels a posting's title asks for, or none when it names no level."""
    title = title or ""
    matches = list(_DEGREE_LEVEL_RE.finditer(title))
    levels: set[str] = set()
    for index, match in enumerate(matches):
        if match.lastgroup != "bare":
            levels.add(str(match.lastgroup))
            continue
        if match.group(0).lower() not in _TITLE_BARE_DEGREES:
            continue
        before = title[: match.start()]
        gaps = []
        if index > 0:
            gaps.append(title[matches[index - 1].end() : match.start()])
        if index + 1 < len(matches):
            gaps.append(title[match.end() : matches[index + 1].start()])
        if (
            any(_DEGREE_JOIN_RE.fullmatch(gap) for gap in gaps)
            or _DEGREE_AFTER_ROLE_RE.search(before)
            or (before.rstrip().endswith("(") and title[match.end() :].lstrip().startswith(")"))
        ):
            levels.add(_degree_match_level(match))
    return levels


def _degree_level_names(levels: set[str]) -> str:
    return " or ".join(label for level, label in DEGREE_LEVEL_LABELS.items() if level in levels)


def _profile_list(profile: dict[str, Any], key: str) -> list[Any]:
    """A list-valued profile field, with an explicit null read as empty."""
    value = profile.get(key)
    return list(value) if isinstance(value, (list, tuple)) else []


def _profile_int(profile: dict[str, Any], key: str, default: int) -> int:
    """A numeric profile field; the default applies only when it is unanswered.

    An explicit 0 is a real answer and is preserved.
    """
    value = profile.get(key)
    if value is None or isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _profile_terms(profile: dict[str, Any], key: str) -> list[str]:
    """A keyword-list profile field: null reads as empty, non-strings are skipped."""
    return [term for term in _profile_list(profile, key) if isinstance(term, str)]


def _scoring_regions(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Configured regions that can be named in a score reason; others are skipped."""
    return [
        region
        for region in _profile_list(profile, "regions")
        if isinstance(region, dict) and isinstance(region.get("name"), str) and region["name"].strip()
    ]


def score_job(job: sqlite3.Row, profile: dict[str, Any]) -> tuple[int, list[str]]:
    title = job["title"] or ""
    description = job["description"] or ""
    text = f"{title} {description}"
    score = 35
    reasons = ["35 base"]

    preferred_types = _profile_list(profile, "preferred_role_types")
    if job["role_type"] in preferred_types:
        score += 18
        reasons.append(f"+18 preferred role type ({job['role_type']})")

    degree_title_hits = term_hits(title, _profile_terms(profile, "degree_keywords"))
    degree_description_hits = [
        term
        for term in term_hits(description, _profile_terms(profile, "degree_keywords"))
        if term not in degree_title_hits
    ]
    if degree_title_hits or degree_description_hits:
        points = min(15, 6 * len(degree_title_hits) + 2 * len(degree_description_hits))
        score += points
        hits = (degree_title_hits + degree_description_hits)[:3]
        reasons.append(f"+{points} degree match: {', '.join(hits)}")

    interest_title_hits = term_hits(title, _profile_terms(profile, "interest_keywords"))
    interest_description_hits = [
        term
        for term in term_hits(description, _profile_terms(profile, "interest_keywords"))
        if term not in interest_title_hits
    ]
    if interest_title_hits or interest_description_hits:
        points = min(20, 6 * len(interest_title_hits) + len(interest_description_hits))
        score += points
        hits = (interest_title_hits + interest_description_hits)[:5]
        reasons.append(f"+{points} interests: {', '.join(hits)}")

    deprioritized = term_hits(title, _profile_terms(profile, "deprioritize_title_keywords"))
    if deprioritized:
        points = min(24, 12 * len(deprioritized))
        score -= points
        reasons.append(f"-{points} lower-priority discipline: {', '.join(deprioritized[:2])}")

    skill_hits = term_hits(text, _profile_terms(profile, "skills"))
    if skill_hits:
        points = min(15, 5 * len(skill_hits))
        score += points
        reasons.append(f"+{points} skills: {', '.join(skill_hits[:3])}")

    location_text = job["location"] or ""
    is_remote = bool(profile.get("remote_ok")) and "remote" in location_text.lower()
    regions = _scoring_regions(profile)
    if regions:
        # Target regions configured: in-region wins, remote still qualifies, and
        # anything else takes a heavy penalty so it sinks below every real match.
        # A location that names no place stays neutral — we can't tell where it
        # is, and penalising it would bury postings whose location field is just
        # sparse rather than genuinely elsewhere.
        region_hit = match_region(location_text, regions)
        if region_hit:
            region = region_hit["region"]
            # A missing bonus takes the default; an explicit null is read as
            # no bonus rather than invented.
            if "bonus" in region and region["bonus"] is None:
                bonus = 0
            else:
                bonus = _profile_int(region, "bonus", 10)
            score += bonus
            radius = region.get("radius", "target")
            reasons.append(f"+{bonus} location: {region['name']} ({radius} radius)")
        elif is_remote:
            score += 8
            reasons.append("+8 remote")
        elif not is_uninformative_location(location_text):
            penalty = _profile_int(profile, "out_of_region_penalty", 40)
            score -= penalty
            reasons.append(f"-{penalty} outside target regions: {location_text.strip()[:40]}")
    else:
        location_hits = term_hits(location_text, _profile_terms(profile, "preferred_locations"))
        if location_hits:
            score += 10
            reasons.append(f"+10 location: {', '.join(location_hits[:2])}")
        elif is_remote:
            score += 8
            reasons.append("+8 remote")
        elif profile.get("willing_to_relocate") is False and location_text:
            score -= 10
            reasons.append("-10 outside preferred locations; relocation disabled")

    available_terms = [term.lower() for term in _profile_terms(profile, "available_terms")]
    explicit_terms = re.findall(r"\b(?:spring|summer|fall|winter)\s+20\d{2}\b", title.lower())
    if explicit_terms and available_terms:
        if any(term in available_terms for term in explicit_terms):
            score += 8
            reasons.append(f"+8 availability match: {explicit_terms[0]}")
        else:
            score -= 20
            reasons.append(f"-20 unavailable term: {explicit_terms[0]}")

    senior_hit = _SENIORITY_RE.search(title)
    if senior_hit and not _ENTRY_TITLE_RE.search(title):
        score -= 35
        reasons.append(f"-35 seniority mismatch: {senior_hit.group(0).lower()}")

    # Only the title is read: "BS, MS, or PhD" in a description is usually inclusive.
    student_levels = degree_levels(profile.get("degree"))
    title_levels = title_degree_levels(title) if student_levels else set()
    if title_levels and not title_levels & student_levels:
        score -= 35
        reasons.append(
            f"-35 degree level: title asks for {_degree_level_names(title_levels)}, "
            f"not {_degree_level_names(student_levels)}"
        )

    requirements = experience_requirements(description)
    if requirements:
        max_experience = _profile_int(profile, "max_years_experience", 1)
        graduation_year = _profile_int(profile, "graduation_year", 0) or None
        # Whoever graduates this year or later has no post-graduation experience, whatever the internships say;
        # someone who graduated earlier may, so the ceiling decides; with no graduation year the score does not guess.
        no_post_graduation = graduation_year is not None and graduation_year >= datetime.now(timezone.utc).year
        unmet = [
            requirement
            for requirement in requirements
            if requirement.years > max_experience
            or (requirement.qualifier and requirement.years >= 1 and no_post_graduation)
        ]
        if len(unmet) == len(requirements):
            # Judged by the smaller, as before: one requirement the student meets means the posting is not closed to them.
            floor = min(unmet, key=lambda requirement: requirement.years)
            score -= 18
            reasons.append(
                f"-18 asks for {floor.years}+ years" + (f" of {floor.qualifier} experience" if floor.qualifier else "")
            )
        elif graduation_year is None:
            unknown = [item for item in requirements if item.qualifier and item.years >= 1]
            if unknown:
                floor = min(unknown, key=lambda requirement: requirement.years)
                reasons.append(
                    f"FLAG: asks for {floor.years}+ years of {floor.qualifier} experience—verify new-grad eligibility"
                )

    posted = parse_datetime(job["posted_at"])
    if posted:
        age_days = (datetime.now(timezone.utc) - posted.astimezone(timezone.utc)).days
        if age_days <= 7:
            score += 10
            reasons.append("+10 updated within 7 days")
        elif age_days <= 21:
            score += 5
            reasons.append("+5 updated within 21 days")
        elif age_days > 60:
            score -= 5
            reasons.append("-5 posting timestamp over 60 days old")

    if not description:
        score -= 3
        reasons.append("-3 description unavailable")

    if re.search(r"\b(us person|u\.s\. person|security clearance|u\.s\. citizen)\b", description, re.I):
        reasons.append("FLAG: citizenship/clearance language—verify eligibility")
    if _AI_READER_RE.search(text):
        reasons.append(AI_READER_FLAG)
    if closes_sponsorship(description):
        reasons.append("FLAG: sponsorship language—verify work authorization")
        if profile.get("requires_sponsorship") is True:
            score -= 35
            reasons.append("-35 sponsorship appears unavailable")

    pay = _pay_preference_reason(profile, title, description)
    if pay:
        score -= pay[0]
        reasons.append(pay[1])

    return max(0, min(100, score)), reasons


# Cohort markers that distinguish one posting of a role from the next but not
# the role itself: "(Summer 2027)", "[Fall 2026]", a bare year.
_ROLE_BRACKET_RE = re.compile(r"\([^)]*\)|\[[^\]]*\]")
_ROLE_TERM_RE = re.compile(r"\b(?:spring|summer|fall|winter|autumn)\s*20\d{2}\b|\b20\d{2}\b")


def role_key(title: str) -> str:
    """Role identity with cohort markers removed.

    "Mechanical Engineering Intern (Summer 2027)" and "Mechanical Engineering
    Intern [Fall 2026]" are the same role advertised for two terms.
    """
    text = _ROLE_BRACKET_RE.sub(" ", title or "")
    return normalized(_ROLE_TERM_RE.sub(" ", text.lower()))


REPOST_WINDOW_DAYS = 90


# Tables repost_flags may read. Both hold the columns it needs under the same names;
# the name is interpolated into SQL, so it must come from here.
_REPOST_TABLES = ("jobs", "opportunities")


def repost_flags(
    conn: sqlite3.Connection,
    window_days: int = REPOST_WINDOW_DAYS,
    *,
    table: str = "jobs",
) -> dict[str, tuple[int, str]]:
    """Active postings whose role was previously listed under a different URL.

    The signal is a role that went away and came back somewhere else, not merely
    one that appears twice: a company advertising the same internship for two
    terms at once is normal, and flagging that would be noise. So a row counts
    only when an *earlier, since-retired* posting of the same role exists at a
    different URL.

    ``table`` is the legacy ``jobs`` table (the refresh) or the product database's
    ``opportunities`` table (a profile save re-scoring in the web app). Both go
    through this one rule so a posting's explanation does not depend on which
    of the two wrote it last.
    """
    if table not in _REPOST_TABLES:
        raise ValueError(f"repost_flags cannot read table {table!r}")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).isoformat()
    groups: dict[tuple[str, str], list[tuple[str, str, str, str, bool]]] = {}
    # Rows are read by position: the product database's cursor on PostgreSQL yields dict-like rows, and
    # unpacking one into names gives the column names, not the values.
    for row in conn.execute(
        f"SELECT id, company, title, url, first_seen_at, active FROM {table} WHERE first_seen_at >= ?",
        (cutoff,),
    ):
        groups.setdefault((normalized(row[1]), role_key(row[2])), []).append(
            (row[0], row[3], row[4], bool(row[5]))
        )

    flags: dict[str, tuple[int, str]] = {}
    for members in groups.values():
        retired = [member for member in members if not member[3]]
        if not retired:
            continue
        for row_id, url, first_seen_at, active in members:
            if not active:
                continue
            earlier = [
                other
                for other in retired
                if other[1] != url and other[2] < first_seen_at
            ]
            if earlier:
                oldest = min(other[2] for other in earlier)
                flags[row_id] = (len({other[1] for other in earlier}) + 1, oldest[:10])
    return flags


REPOST_FLAG_PREFIX = "FLAG: this role has been listed under"


def repost_reason(listings: int, since: str) -> str:
    """The explanation line for a ``repost_flags`` entry.

    Non-scoring, and worded neutrally on purpose. A re-listed req is often just
    an evergreen pipeline posting or an ATS migration; it is information for the
    reader, not a verdict on the employer.
    """
    return (
        f"{REPOST_FLAG_PREFIX} {listings} different URLs "
        f"since {since}—may be an evergreen or re-listed req"
    )


def score_all(conn: sqlite3.Connection, profile: dict[str, Any]) -> int:
    jobs = conn.execute("SELECT * FROM jobs").fetchall()
    reposts = repost_flags(conn)
    changed: list[tuple[str, int, str, str]] = []
    for job in jobs:
        role_type = classify_role(job["title"], job["description"])
        score_input = dict(job)
        score_input["role_type"] = role_type
        score, reasons = score_job(score_input, profile)
        if job["id"] in reposts:
            reasons.append(repost_reason(*reposts[job["id"]]))
        explanation = json.dumps(reasons)
        # Rewriting a row with the values it already holds changes nothing, so
        # only rows whose result moved are written; most of a daily run's
        # table is unchanged.
        if (job["role_type"], job["score"], job["score_explanation"]) != (role_type, score, explanation):
            changed.append((role_type, score, explanation, job["id"]))
    if changed:
        conn.executemany(
            "UPDATE jobs SET role_type=?, score=?, score_explanation=? WHERE id=?", changed
        )
    conn.commit()
    print(f"Scored {len(jobs)} postings")
    return len(jobs)
