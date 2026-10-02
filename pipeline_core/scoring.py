"""Scoring: how a posting is matched to the student's profile, with every adjustment recorded as a reason."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

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
_EXPERIENCE_YEARS_RE = re.compile(
    r"(\d{1,2})\+?\s+years?'?\s+(?:of\s+)?"
    r"(?:(?!(?:age|old|degree|degrees|diploma)\b)[\w/+-]+\s+){0,3}?experience",
    re.IGNORECASE,
)
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
    except (TypeError, ValueError):
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

    year_matches = [int(value) for value in _EXPERIENCE_YEARS_RE.findall(description)]
    max_experience = _profile_int(profile, "max_years_experience", 1)
    if year_matches and min(year_matches) > max_experience:
        score -= 18
        reasons.append(f"-18 asks for {min(year_matches)}+ years")

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
    if re.search(r"\b(no sponsorship|unable to sponsor|not sponsor)\b", description, re.I):
        reasons.append("FLAG: sponsorship language—verify work authorization")
        if profile.get("requires_sponsorship") is True:
            score -= 35
            reasons.append("-35 sponsorship appears unavailable")

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
