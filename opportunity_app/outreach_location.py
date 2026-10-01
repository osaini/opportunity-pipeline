"""Where the student lives and where a company is: regions, home place and how a draft says so.

Geography for outreach, shared by drafting, the profile check, location search and discovery. Regions come only from
the student's config/profile.json (read through ``owner_profile``, so the file is stat'ed and cached once) or, for any
other user on the install, from that user's own confirmed profile facts. Nothing about one student's metros is built in.
"""

from __future__ import annotations

import json
import re
import sqlite3
from functools import lru_cache
from typing import Any

from .legacy import PROFILE_PATH
from .schema import LOCAL_USER_ID


# Where a target's location came from, most authoritative first. A deep search
# location is the model's word until the company's site, a filing, or a searched
# page that states it agrees, or the student confirms the research, so the
# drafter does not rely on it.
LOCATION_BASES = ("manual", "company_site", "sec_form_d", "web_search", "research")


# The bases a draft may rely on. Membership is the test, not absence from a
# denylist: a blank basis means nothing has established the location, and an
# unrecognised one means this code does not know what established it. Both are
# unverified, so the test fails closed.
VERIFIED_BASES = frozenset({"manual", "company_site", "sec_form_d", "web_search"})


# Bases that a page this app opened established, so a location carrying one
# needs no further searching unless it was only inferred.
PAGE_CHECKED_BASES = frozenset({"company_site", "sec_form_d", "web_search"})


US_STATES = {
    "AL": "alabama", "AK": "alaska", "AZ": "arizona", "AR": "arkansas", "CA": "california", "CO": "colorado",
    "CT": "connecticut", "DE": "delaware", "FL": "florida", "GA": "georgia", "HI": "hawaii", "ID": "idaho",
    "IL": "illinois", "IN": "indiana", "IA": "iowa", "KS": "kansas", "KY": "kentucky", "LA": "louisiana",
    "ME": "maine", "MD": "maryland", "MA": "massachusetts", "MI": "michigan", "MN": "minnesota",
    "MS": "mississippi", "MO": "missouri", "MT": "montana", "NE": "nebraska", "NV": "nevada",
    "NH": "new hampshire", "NJ": "new jersey", "NM": "new mexico", "NY": "new york", "NC": "north carolina",
    "ND": "north dakota", "OH": "ohio", "OK": "oklahoma", "OR": "oregon", "PA": "pennsylvania",
    "RI": "rhode island", "SC": "south carolina", "SD": "south dakota", "TN": "tennessee", "TX": "texas",
    "UT": "utah", "VT": "vermont", "VA": "virginia", "WA": "washington", "WV": "west virginia",
    "WI": "wisconsin", "WY": "wyoming", "DC": "district of columbia",
}


_STATE_NAME_PATTERNS = {code: re.compile(rf"\b{name}\b") for code, name in US_STATES.items()}


def _region_states(region: dict[str, Any]) -> set[str]:
    """The US state codes a profile region's state markers name ("tx", "texas")."""
    codes = set()
    for marker in region.get("state_markers") or []:
        text = str(marker).strip()
        if text.upper() in US_STATES:
            codes.add(text.upper())
        codes |= {code for code, name in US_STATES.items() if name == text.casefold()}
    return codes


@lru_cache(maxsize=1024)
def _word_pattern(needle: str) -> re.Pattern[str]:
    return re.compile(rf"\b{re.escape(needle)}\b")


def _mentions(lowered: str, term: Any) -> bool:
    needle = " ".join(str(term or "").casefold().split())
    return bool(needle) and _word_pattern(needle).search(lowered) is not None


def location_region(text: str, regions: list[dict[str, Any]] | None = None) -> str:
    """The student's own region a place name falls in, or "" when it names none.

    Regions come only from the student's config/profile.json, so every student
    gets their own metros and none is built in. A named state has to agree, so
    "Austin, MN" is not a Texas region; only a state code after a comma counts,
    which keeps a school name that starts "UT" from reading as Utah. With a state named, the
    region's places, aliases, and name all count. With none named, only an
    alias or the region's own name does, because a bare town name is common
    elsewhere and "Dublin, Ireland" must not become a California region.

    ``regions`` defaults to the local owner's profile file; pass
    ``user_regions(conn, user_id)`` for anyone else.
    """
    raw = str(text or "")
    lowered = " ".join(raw.casefold().split())
    if not lowered:
        return ""
    states = {code for code in re.findall(r",\s*([A-Z]{2})\b", raw) if code in US_STATES}
    # One pattern per state, not one alternation: "west virginia" must still match both "virginia" and "west virginia".
    states |= {code for code, pattern in _STATE_NAME_PATTERNS.items() if pattern.search(lowered)}
    for region in _profile_regions() if regions is None else regions:
        name = str(region.get("name") or "")
        if not name:
            continue
        own = _region_states(region)
        if states and own and not own & states:
            continue
        terms = [name, *(region.get("aliases") or [])]
        if states:
            terms += list(region.get("places") or [])
        if any(_mentions(lowered, term) for term in terms):
            return name
    return ""


def region_phrase(name: str, regions: list[dict[str, Any]] | None = None) -> str:
    """How an email names the region: its profile "phrase", or "the Bay Area" style for "... Area"."""
    for region in _profile_regions() if regions is None else regions:
        if region.get("name") == name and str(region.get("phrase") or "").strip():
            return str(region["phrase"]).strip()
    return f"the {name}" if name.endswith(" Area") else name


def city_state(text: str) -> tuple[str, str]:
    """("seattle", "WA") from "Seattle, WA" or "Seattle, Washington"; the state is "" when none is named."""
    parts = [part.strip() for part in str(text or "").split(",")]
    city = " ".join(parts[0].casefold().split()) if parts else ""
    for part in parts[1:]:
        if part.upper() in US_STATES:
            return city, part.upper()
        code = next((code for code, name in US_STATES.items() if name == part.casefold()), "")
        if code:
            return city, code
    return city, ""


def student_home(facts: dict[str, Any], regions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Where the student lives during breaks and summers, as an email names it, or {} when unknown.

    Every student's break_location counts, not only one who has set up regions.
    When it falls in one of their regions, the home is that whole metro. Otherwise
    it is the one city it names, and only with a state ("Seattle, WA"), because a
    bare town name is common elsewhere. "year_round" is true when the school is in
    the same region, so the student is there during the school year as well.
    """
    raw = str(facts.get("break_location") or "").strip()
    region = location_region(raw, regions)
    if region:
        return {
            "region": region, "city": "", "state": "", "phrase": region_phrase(region, regions),
            "year_round": region == location_region(str(facts.get("school") or ""), regions),
        }
    city, state = city_state(raw)
    if not city or not state:
        return {}
    return {"region": "", "city": city, "state": state, "phrase": raw.split(",")[0].strip(), "year_round": False}


def near_home(location: str, home: dict[str, Any], regions: list[dict[str, Any]] | None = None) -> bool:
    """Whether a company's location is where the student lives: their home region, or their home city and state."""
    if not home or not str(location or "").strip():
        return False
    if home["region"]:
        return location_region(location, regions) == home["region"]
    return city_state(location) == (home["city"], home["state"])


def home_terms(home: dict[str, Any]) -> list[str]:
    """The words a draft may name the student's home by; any one of them counts as saying it."""
    return [term for term in dict.fromkeys((home.get("phrase", ""), home.get("region", ""))) if term]


def mentions_home(body: str, terms: list[str]) -> bool:
    """Whether the text says the student lives in their home place: "(live in Seattle)", "in the Twin Cities".

    A bare name is not enough, because a school's name can carry it ("Portland State")
    without saying anything about where the student lives.
    """
    text = " ".join(str(body or "").split())
    return any(
        re.search(rf"\bin {re.escape(' '.join(term.split()))}\b", text, re.IGNORECASE) for term in terms if term.strip()
    )


def user_home(conn: sqlite3.Connection, user_id: str, regions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    from .preparation import confirmed_facts

    return student_home(confirmed_facts(conn, user_id), user_regions(conn, user_id) if regions is None else regions)


def location_line_gap(item: dict[str, Any], home: dict[str, Any], regions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Whether a target's cold email must say the student lives nearby, and whether its draft does.

    The line belongs in any draft to a company with a checked location where the
    student lives (outreach_drafting.location_line). A draft written before that
    location was checked, or hand-edited, can lack it; "missing" marks that, so
    the draft is regenerated rather than approved as it stands.
    """
    if not item.get("location_verified") or not near_home(str(item.get("location") or ""), home, regions):
        return {"phrase": "", "terms": [], "missing": False}
    terms = home_terms(home)
    body = str(item.get("email_body") or "")
    return {"phrase": home["phrase"], "terms": terms, "missing": bool(body.strip()) and not mentions_home(body, terms)}


def missing_location_message(target: dict[str, Any]) -> str:
    return (
        f"This draft never says you live in {target['draft_location']['phrase']}, though "
        f"{target['company']} is in {target['location']}. "
        f"Regenerate it, or add '(live in {target['draft_location']['phrase']})' after your school."
    )


def _profile_regions() -> list[dict[str, Any]]:
    """The target regions in config/profile.json (read through owner_profile, so the file is stat'ed and cached once)."""
    regions = owner_profile().get("regions") or []
    return [region for region in regions if isinstance(region, dict)]


def user_regions(conn: sqlite3.Connection | None, user_id: str = LOCAL_USER_ID) -> list[dict[str, Any]]:
    """The outreach regions for one user.

    config/profile.json belongs to the local owner, so only that user reads it.
    Anyone else on the same install gets the regions in their own confirmed
    profile facts, or none, never the owner's metros.
    """
    if conn is None or user_id == LOCAL_USER_ID:
        return _profile_regions()
    from .preparation import confirmed_facts

    regions = confirmed_facts(conn, user_id).get("regions") or []
    return [region for region in regions if isinstance(region, dict)] if isinstance(regions, list) else []


def location_usable(target: dict[str, Any]) -> bool:
    """Whether a draft may rely on the target's location.

    A location the deep search reported is unchecked model output until the
    company's site or a filing states it, or the student confirms the research.
    One inferred from the only place a site names waits for the student to
    confirm it. A location with no basis — an import file's word, or anything
    this code does not recognise — is unchecked too: only a basis in
    ``VERIFIED_BASES`` counts, so an unknown value is never read as confirmed.
    """
    if not target.get("location") or target.get("location_inferred"):
        return False
    basis = target.get("location_basis") or ""
    if basis in VERIFIED_BASES:
        return True
    return basis == "research" and target.get("research_confidence") == "confirmed"


_PROFILE_DATA_CACHE: dict[str, Any] = {"key": None, "data": {}}


def owner_profile() -> dict[str, Any]:
    """config/profile.json, re-read when the file changes."""
    try:
        key = (str(PROFILE_PATH), PROFILE_PATH.stat().st_mtime_ns)
    except OSError:
        return {}
    if _PROFILE_DATA_CACHE["key"] != key:
        try:
            data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        _PROFILE_DATA_CACHE.update(key=key, data=data if isinstance(data, dict) else {})
    return _PROFILE_DATA_CACHE["data"]
