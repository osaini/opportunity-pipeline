"""Cold outreach tracker for companies that have no posting to apply to.

Outreach targets are a separate record type from opportunities. An
opportunity always traces back to a real posting source; a cold lead does
not, so it never enters the opportunity feed, scoring, or the application
tracker. What a target does keep is where its facts came from
(``source_urls``, ``researched_at``) and how sure the contact is
(``contact_confidence``), so an unverified name is never shown as confirmed.

Nothing here sends mail. Drafts are stored and copied out by the student.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
import unicodedata
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from pipeline import PROFILE_PATH

from .inbox_classifiers import classify_reply
from .schema import LOCAL_USER_ID, utc_now
from .typesafe_decisions import DecisionClient
from .user_time import user_timezone


class OutreachNotFoundError(LookupError):
    pass


class DraftChangedError(ValueError):
    pass


OUTREACH_STATUSES = (
    "not_started", "drafted", "sent", "followed_up", "replied",
    "call_scheduled", "offer", "declined", "no_response", "paused",
)
OUTREACH_PRIORITIES = ("P1", "P2", "P3")
CONTACT_CONFIDENCE = ("confirmed", "unverified", "unknown")
# Statuses after which a follow-up reminder no longer makes sense.
CLOSED_STATUSES = {"replied", "call_scheduled", "offer", "declined", "no_response", "paused"}
# Closed statuses that can still carry a date: "get back in touch in January".
# On these, follow_up_at is a revisit date, not a follow-up email reminder.
REVISIT_STATUSES = {"replied", "paused"}
AWAITING_REPLY = {"sent", "followed_up"}
# Statuses the first email can still go out from.
UNSENT_STATUSES = {"not_started", "drafted", "paused"}
# Once a company writes back there may be a call to prepare for.
CALL_PREP_STATUSES = {"replied", "call_scheduled", "offer"}
DEFAULT_FOLLOW_UP_DAYS = 7
OUTREACH_ORIGINS = ("manual", "import", "discovery")
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
# An import file's word about where a company is based is not evidence, so the
# claim is recorded as an event rather than as provenance on the row. These
# bound that record: each field is clipped before serialising, and the finished
# JSON is shrunk until it fits rather than cut (cutting would invalidate it).
CLAIM_FIELD_LIMITS = {
    "location": 200, "location_basis": 40, "location_inferred": 40, "location_source_url": 300,
}
CLAIM_DETAIL_LIMIT = 1_000
# A draft is "generated" (written by a model or by hand, awaiting review) until
# the student approves it. Only an approved draft unlocks the compose link.
DRAFT_STATUSES = ("none", "generated", "approved")
DRAFT_KINDS = {
    "initial": ("email_subject", "email_body", "draft_status"),
    "follow_up": ("follow_up_subject", "follow_up_body", "follow_up_status"),
}
# Each target row with how many of its stored drafts differ from the text in
# the editor now, which is what the student could go back to.
SELECT_TARGETS = """
    SELECT t.*,
        (SELECT COUNT(*) FROM outreach_draft_versions v WHERE v.target_id=t.id AND v.kind='initial'
            AND NOT (v.subject=t.email_subject AND v.body=t.email_body)) AS draft_history_count,
        (SELECT COUNT(*) FROM outreach_draft_versions v WHERE v.target_id=t.id AND v.kind='follow_up'
            AND NOT (v.subject=t.follow_up_subject AND v.body=t.follow_up_body)) AS follow_up_history_count,
        (SELECT COUNT(*) FROM outreach_events e WHERE e.target_id=t.id AND e.event_type='reply_logged') AS reply_count,
        (SELECT j.state FROM job_queue j WHERE j.id=t.call_prep_job_id) AS call_prep_job_state,
        (SELECT j.last_error FROM job_queue j WHERE j.id=t.call_prep_job_id) AS call_prep_job_error,
        (SELECT j.next_attempt_at FROM job_queue j WHERE j.id=t.call_prep_job_id) AS call_prep_job_next_attempt_at,
        (SELECT j.attempts FROM job_queue j WHERE j.id=t.call_prep_job_id) AS call_prep_job_attempts
    FROM outreach_targets t
"""
TEXT_FIELDS = (
    "company", "channel", "website", "location", "summary", "fit_rationale", "activity_signal",
    "contact_name", "contact_role", "contact_email", "contact_cc", "contact_linkedin", "contact_route",
    "deadline_label", "email_subject", "email_body", "follow_up_subject", "follow_up_body", "notes",
    "contact_evidence_url", "call_prep",
)
MULTILINE_FIELDS = {"email_body", "follow_up_body", "call_prep"}
TEXT_LIMITS = {
    "email_body": 20_000, "follow_up_body": 20_000, "call_prep": 20_000, "notes": 10_000, "summary": 5_000,
    "fit_rationale": 5_000, "activity_signal": 5_000, "location": 200,
}
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
EXPORT_FIELDS = [
    "id", "company", "channel", "priority", "status", "contact_name", "contact_role",
    "contact_email", "contact_cc", "contact_linkedin", "contact_route", "contact_confidence",
    "deadline_label", "deadline_date", "sent_at", "follow_up_at", "fit_rationale",
    "activity_signal", "summary", "website", "location", "location_basis", "location_source_url",
    # Without this the file presents a location the site merely mentions as a
    # flat company_site, which overstates it before anyone re-reads the export.
    "location_inferred",
    "email_subject", "email_body", "draft_status",
    "follow_up_subject", "follow_up_body", "follow_up_status", "notes", "source_urls",
    "researched_at", "research_confidence", "origin", "created_at", "updated_at",
]
# Set by the product, never by an import file or a PATCH.
IMPORT_IGNORED_FIELDS = {"id", "created_at", "updated_at", "origin", "draft_status", "follow_up_status"}
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Legal-form words that do not tell two companies apart: "Acme Robotics, Inc."
# and "Acme Robotics" are one company.
LEGAL_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "co", "company", "llc", "ltd", "limited", "plc",
    "pbc", "lp", "llp", "gmbh", "ag", "sa", "bv", "pty",
}
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _clean_date(value: Any, field: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if not _DATE.match(text):
        raise ValueError(f"{field} must be a calendar date (YYYY-MM-DD)")
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} is not a real date") from exc
    return text


def _clean_urls(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        items = [part.strip() for part in re.split(r"[\n,]", value)]
    elif isinstance(value, list):
        items = [str(part).strip() for part in value]
    else:
        raise ValueError("source_urls must be a list of URLs")
    urls = [item for item in items if item]
    for url in urls:
        _validate_web_url(url, "Source URL")
    return urls[:50]


def _validate_web_url(value: str, field: str) -> None:
    from .outreach_contacts import public_web_url_error

    if public_web_url_error(value):
        raise ValueError(f"{field} must be a public http(s) URL without credentials")


def _normalize(payload: dict[str, Any], *, partial: bool) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for field in TEXT_FIELDS:
        if field in payload and payload[field] is not None:
            raw = str(payload[field])
            text = raw.replace("\r\n", "\n").strip() if field in MULTILINE_FIELDS else raw.strip()
            if len(text) > TEXT_LIMITS.get(field, 2_000):
                raise ValueError(f"{field} is too long")
            values[field] = text
    if not partial and not values.get("company"):
        raise ValueError("Company is required")
    if "company" in values and not values["company"]:
        raise ValueError("Company is required")
    if values.get("contact_email") and not _EMAIL.match(values["contact_email"]):
        raise ValueError("Contact email does not look like an email address")
    if values.get("contact_cc") and not _EMAIL.match(values["contact_cc"]):
        raise ValueError("Cc does not look like an email address")
    for field in ("website", "contact_linkedin", "contact_evidence_url"):
        if values.get(field):
            _validate_web_url(values[field], field)
    for field, allowed in (("priority", OUTREACH_PRIORITIES), ("status", OUTREACH_STATUSES), ("contact_confidence", CONTACT_CONFIDENCE)):
        if field in payload and payload[field] is not None and str(payload[field]).strip():
            value = str(payload[field]).strip()
            if value not in allowed:
                raise ValueError(f"{field} must be one of: {', '.join(allowed)}")
            values[field] = value
    for field in ("deadline_date", "sent_at", "follow_up_at", "researched_at"):
        if field in payload:
            values[field] = _clean_date(payload[field], field)
    if "source_urls" in payload:
        values["source_urls_json"] = json.dumps(_clean_urls(payload["source_urls"]))
    return values


def draft_checks(subject: str, body: str) -> dict[str, Any]:
    """Cheap, explainable lint for a cold email draft."""
    words = len(re.findall(r"\b\w+\b", body))
    placeholders = sorted(set(re.findall(r"\[[^\]\n]{1,60}\]", f"{subject}\n{body}")))
    dashes = sum((subject + body).count(mark) for mark in ("\u2014", "\u2013"))
    warnings = []
    if dashes:
        warnings.append(f"Contains {dashes} em or en dash{'es' if dashes != 1 else ''}")
    if placeholders:
        warnings.append("Unfilled placeholders: " + ", ".join(placeholders))
    if words > 200:
        warnings.append(f"{words} words; cold emails read best under 200")
    if body and not subject:
        warnings.append("No subject line")
    return {"word_count": words, "dash_count": dashes, "placeholders": placeholders, "warnings": warnings}


def _region_states(region: dict[str, Any]) -> set[str]:
    """The US state codes a profile region's state markers name ("tx", "texas")."""
    codes = set()
    for marker in region.get("state_markers") or []:
        text = str(marker).strip()
        if text.upper() in US_STATES:
            codes.add(text.upper())
        codes |= {code for code, name in US_STATES.items() if name == text.casefold()}
    return codes


def _mentions(lowered: str, term: Any) -> bool:
    needle = " ".join(str(term or "").casefold().split())
    return bool(needle) and re.search(rf"\b{re.escape(needle)}\b", lowered) is not None


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
    states |= {code for code, name in US_STATES.items() if re.search(rf"\b{name}\b", lowered)}
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


def _city_state(text: str) -> tuple[str, str]:
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
    city, state = _city_state(raw)
    if not city or not state:
        return {}
    return {"region": "", "city": city, "state": state, "phrase": raw.split(",")[0].strip(), "year_round": False}


def near_home(location: str, home: dict[str, Any], regions: list[dict[str, Any]] | None = None) -> bool:
    """Whether a company's location is where the student lives: their home region, or their home city and state."""
    if not home or not str(location or "").strip():
        return False
    if home["region"]:
        return location_region(location, regions) == home["region"]
    return _city_state(location) == (home["city"], home["state"])


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


_PROFILE_REGIONS_CACHE: dict[str, Any] = {"key": None, "regions": []}


def _profile_regions() -> list[dict[str, Any]]:
    """The target regions in config/profile.json, re-read when the file changes."""
    try:
        key = (str(PROFILE_PATH), PROFILE_PATH.stat().st_mtime_ns)
    except OSError:
        return []
    if _PROFILE_REGIONS_CACHE["key"] != key:
        try:
            regions = json.loads(PROFILE_PATH.read_text(encoding="utf-8")).get("regions") or []
        except (OSError, ValueError, AttributeError):
            regions = []
        _PROFILE_REGIONS_CACHE.update(
            key=key, regions=[region for region in regions if isinstance(region, dict)]
        )
    return _PROFILE_REGIONS_CACHE["regions"]


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


def company_key(name: str) -> str:
    """A company name reduced to what identifies it, for matching across sources.

    Case, punctuation, "&" versus "and", a leading "The", and trailing legal
    forms are ignored, so "The Acme Robotics Co." and "acme robotics" match.
    "Acme Robotics Fund I LLC" does not: only trailing legal words are dropped.
    """
    text = unicodedata.normalize("NFKC", str(name or "")).casefold().replace("&", " and ").replace(".", "")
    words = re.sub(r"[^\w\s]", " ", text).split()
    core = list(words)
    if len(core) > 1 and core[0] == "the":
        core.pop(0)
    while len(core) > 1 and core[-1] in LEGAL_SUFFIXES:
        core.pop()
    return " ".join(core or words)


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


class LocationConflictError(ValueError):
    """The location changed between the page the student read and their click.

    Raised so the route can answer 409 rather than the 422 every other
    ``ValueError`` from this module means. It subclasses ``ValueError`` so
    existing callers still catch it, which makes the handler order in
    ``api.py`` load-bearing.
    """


def _claim_detail(payload: dict[str, Any]) -> str:
    """An import file's discarded location claim, as bounded, valid JSON.

    Every value is recorded as the file's own lexical claim, never interpreted.
    A CSV round trip turns ``False`` into the string ``"False"``, which is true
    under ordinary truthiness, so reading the claim would record the opposite
    of what the file said. This is an audit record: it stores the claim.
    """
    budgets = dict(CLAIM_FIELD_LIMITS)
    while True:
        claim = {}
        for field, budget in budgets.items():
            text = "" if payload.get(field) is None else str(payload.get(field))
            claim[field] = text[:budget] + "…" if len(text) > budget else text
        detail = json.dumps({"unverified_import_claim": claim}, ensure_ascii=False, sort_keys=True)
        if len(detail) <= CLAIM_DETAIL_LIMIT:
            return detail
        widest = max(budgets, key=lambda field: budgets[field])
        if budgets[widest] <= 1:
            # Unreachable in practice: the skeleton with every field at one
            # character is ~150 bytes. Kept valid anyway, because a truncated
            # JSON string is exactly the failure this function exists to avoid.
            return json.dumps({"unverified_import_claim": "too long to record"}, sort_keys=True)
        budgets[widest] //= 2


def website_domain(url: str) -> str:
    """The host of a website URL, lowercased, without a leading www."""
    text = str(url or "").strip()
    if not text:
        return ""
    host = urlsplit(text if "//" in text else f"https://{text}").hostname or ""
    return host.lower().removeprefix("www.")


def local_today(conn: sqlite3.Connection, user_id: str, now: datetime | None = None) -> date:
    return user_timezone(conn, user_id).today(now)


def _draft_fingerprint(
    kind: str, subject: str, body: str, contact_email: str, claims_json: str, generated_by: str, contact_cc: str = "",
) -> str:
    fields = [kind, subject, body, contact_email, claims_json, generated_by]
    # Only a draft with a Cc carries it, so every draft approved before Cc
    # existed keeps the fingerprint it was approved and handed off under.
    if contact_cc:
        fields.append(contact_cc)
    canonical = json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _confirmed_claims(claims: Any) -> Any:
    """Re-read "unverified:<field>" citations once the research is confirmed.

    A draft written while the research was unverified cites it that way, and the
    citation is frozen into the stored draft. Confirming the research is the
    student saying they checked those sources, so from then on the same sentence
    rests on confirmed research. Only the read moves: the stored draft and its
    version history keep the wording the model produced.
    """
    if not isinstance(claims, list):
        return claims
    rewritten = []
    for claim in claims:
        basis = str(claim.get("basis", "")) if isinstance(claim, dict) else ""
        if basis.startswith("unverified:"):
            claim = {**claim, "basis": f"research:{basis.split(':', 1)[1]}"}
        rewritten.append(claim)
    return rewritten


def _record(
    row: sqlite3.Row | dict[str, Any],
    today: date | None = None,
    regions: list[dict[str, Any]] | None = None,
    home: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item = dict(row)
    item["source_urls"] = json.loads(item.pop("source_urls_json") or "[]")
    draft_claims_json = item.pop("draft_claims_json", None) or "[]"
    follow_up_claims_json = item.pop("follow_up_claims_json", None) or "[]"
    item["draft_claims"] = json.loads(draft_claims_json)
    item["follow_up_claims"] = json.loads(follow_up_claims_json)
    item["call_prep_claims"] = json.loads(item.pop("call_prep_claims_json", None) or "[]")
    job_state = item.pop("call_prep_job_state", None)
    job = {
        "state": job_state,
        "error": item.pop("call_prep_job_error", None) or "",
        "next_attempt_at": item.pop("call_prep_job_next_attempt_at", None),
        "attempts": int(item.pop("call_prep_job_attempts", None) or 0),
    }
    item["call_prep_job"] = job if job_state else None
    item["reply_count"] = int(item.get("reply_count") or 0)
    if item.get("research_confidence") == "confirmed":
        item["draft_claims"] = _confirmed_claims(item["draft_claims"])
        item["follow_up_claims"] = _confirmed_claims(item["follow_up_claims"])
        item["call_prep_claims"] = _confirmed_claims(item["call_prep_claims"])
    item["draft_fingerprint"] = _draft_fingerprint(
        "initial", item.get("email_subject", ""), item.get("email_body", ""), item.get("contact_email", ""),
        draft_claims_json, item.get("draft_generated_by", ""), item.get("contact_cc", ""),
    )
    item["follow_up_fingerprint"] = _draft_fingerprint(
        "follow_up", item.get("follow_up_subject", ""), item.get("follow_up_body", ""), item.get("contact_email", ""),
        follow_up_claims_json, item.get("follow_up_generated_by", ""), item.get("contact_cc", ""),
    )
    item["location_region"] = location_region(item.get("location", ""), regions)
    item["location_inferred"] = bool(item.get("location_inferred"))
    item["location_verified"] = location_usable(item)
    item["draft_location"] = location_line_gap(item, home or {}, regions)
    form_d =item.pop("sec_form_d_json", None) or ""
    item["sec_form_d"] = json.loads(form_d) if form_d else None
    if item.get("mail_domain_ok") is not None:
        item["mail_domain_ok"] = bool(item["mail_domain_ok"])
    today = today or date.today()
    due = item.get("follow_up_at")
    item["follow_up_due"] = bool(
        due and item["status"] == "sent" and date.fromisoformat(due) <= today
    )
    item["revisit_due"] = bool(
        due and item["status"] in REVISIT_STATUSES and date.fromisoformat(due) <= today
    )
    bounced = json.loads(item.pop("bounced_addresses_json", None) or "[]")
    item["bounced_addresses"] = bounced
    item["contact_bounced"] = bool(item.get("contact_email")) and item["contact_email"].casefold() in bounced
    item["cc_bounced"] = bool(item.get("contact_cc")) and item["contact_cc"].casefold() in bounced
    item["draft_checks"] = draft_checks(item.get("email_subject", ""), item.get("email_body", ""))
    item["follow_up_checks"] = draft_checks(item.get("follow_up_subject", ""), item.get("follow_up_body", ""))
    item["suggestion"] = lifecycle_suggestion(item, today)
    return item


def _event_time(conn: sqlite3.Connection, target_id: str) -> str:
    """Now, but always after the target's latest event.

    Events are listed by created_at alone, and one action often logs two in a
    row. Where the clock is coarse (about 15ms on Windows before Python 3.13)
    both get the same stamp and the history can show them out of order.
    """
    now = utc_now()
    latest = conn.execute("SELECT MAX(created_at) FROM outreach_events WHERE target_id=?", (target_id,)).fetchone()[0]
    if latest and datetime.fromisoformat(latest) >= datetime.fromisoformat(now):
        return (datetime.fromisoformat(latest) + timedelta(microseconds=1)).isoformat(timespec="microseconds")
    return now


def _log(conn: sqlite3.Connection, target_id: str, user_id: str, event_type: str, *, from_status: str | None = None, to_status: str | None = None, detail: str = "") -> None:
    conn.execute(
        """
        INSERT INTO outreach_events(id, target_id, user_id, event_type, from_status, to_status, detail, created_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (f"outreach-event-{uuid4().hex}", target_id, user_id, event_type, from_status, to_status, detail, _event_time(conn, target_id)),
    )


def _apply_status_side_effects(values: dict[str, Any], previous: dict[str, Any] | None, today: date) -> None:
    status = values.get("status")
    if status is None or (previous and previous["status"] == status):
        return
    if status in {"sent", "followed_up"}:
        # An email went out again (or the student says one did), so the bounce
        # is behind them. The bounced addresses stay on record.
        if previous and previous.get("bounced_at"):
            values["bounced_at"] = None
            values["bounce_reason"] = ""
        if status == "sent" and "sent_at" not in values and not (previous and previous.get("sent_at")):
            values["sent_at"] = today.isoformat()
        if status == "sent" and "follow_up_at" not in values:
            values["follow_up_at"] = (today + timedelta(days=DEFAULT_FOLLOW_UP_DAYS)).isoformat()
        elif status == "followed_up" and "follow_up_at" not in values:
            values["follow_up_at"] = today.isoformat()
    elif status in CLOSED_STATUSES and "follow_up_at" not in values:
        # A revisit date survives a move between Paused and Replied; a follow-up
        # email date from Sent does not become one.
        if not (status in REVISIT_STATUSES and previous and previous["status"] in REVISIT_STATUSES):
            values["follow_up_at"] = None


def _apply_draft_side_effects(values: dict[str, Any], previous: dict[str, Any] | None) -> list[str]:
    """Any change to a draft's words or its recipient sends it back for review.

    Returns the draft kinds whose approval was withdrawn, for the event log.
    """
    withdrawn = []
    previous_values = previous or {}
    recipient_changed = previous is not None and any(
        field in values and values[field] != previous_values.get(field, "")
        for field in ("contact_email", "contact_cc")
    )
    for kind, (subject_field, body_field, status_field) in DRAFT_KINDS.items():
        words_changed = any(
            field in values and values[field] != previous_values.get(field, "")
            for field in (subject_field, body_field)
        )
        if not words_changed and not recipient_changed:
            continue
        body = values.get(body_field, previous_values.get(body_field, ""))
        new_status = "generated" if body else "none"
        if previous_values.get(status_field) == "approved":
            withdrawn.append(kind)
        if previous_values.get(status_field) != new_status:
            values[status_field] = new_status
        if kind == "initial" and previous_values.get("draft_approved_at"):
            values["draft_approved_at"] = None
    return withdrawn


# The greeting is the draft's first line: "Hi Dana," or "Hi Acme team,". When
# the contact changes it is the only part written to the old one, so it is
# swapped here with no model call. The draft still goes back for approval.
_GREETING = re.compile(
    r"(?P<word>(?:hi|hello|hey|dear|good (?:morning|afternoon|evening))\s+)(?P<name>[^,!:\n]{1,80}?)(?P<end>\s*[,!:]?)",
    re.IGNORECASE,
)
_HONORIFICS = {"dr", "mr", "mrs", "ms", "mx", "prof", "professor"}
# Greetings to nobody in particular, which a named contact improves on.
_GENERIC_GREETINGS = {"there", "team", "all", "everyone", "hiring team", "recruiting team"}


def contact_first_name(name: str) -> str:
    words = [word for word in str(name or "").replace(",", " ").split() if word.rstrip(".").casefold() not in _HONORIFICS]
    return words[0] if words else ""


def greeting_name(contact_name: str, company: str) -> str:
    """Who a draft greets: the contact's first name, or the company's team for a shared inbox."""
    return contact_first_name(contact_name) or f"{company} team"


def readdress_greeting(body: str, old_names: set[str], new_name: str) -> tuple[str, str, str] | None:
    """The body greeting ``new_name``, with the old and new greeting lines.

    None when the first line is not a greeting to one of ``old_names``
    (casefolded) or to a team: a greeting the student wrote to someone else is
    theirs.
    """
    lines = body.split("\n")
    index = next((number for number, line in enumerate(lines) if line.strip()), None)
    if index is None:
        return None
    old_line = lines[index].strip()
    match = _GREETING.fullmatch(old_line)
    greeted = " ".join(match["name"].split()).casefold() if match else ""
    if not match or not (greeted in old_names or greeted.endswith(" team")):
        return None
    new_line = f"{match['word']}{new_name}{match['end'] or ','}"
    if new_line == old_line:
        return None
    lines[index] = new_line
    return "\n".join(lines), old_line, new_line


def _readdress_drafts(values: dict[str, Any], previous: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Point unsent drafts' greetings at a changed contact. Returns (kind, old line, new line) per draft."""
    name_changed = "contact_name" in values and values["contact_name"] != previous["contact_name"]
    email_changed = "contact_email" in values and values["contact_email"].casefold() != previous["contact_email"].casefold()
    if not (name_changed or email_changed):
        return []
    # With no name on record, greg@ or greg.lee@ still says who "Hi Greg," was for.
    mailbox = re.split(r"[._+-]", previous["contact_email"].split("@", 1)[0])[0] if "@" in previous["contact_email"] else ""
    old_names = {
        name.casefold() for name in (
            contact_first_name(previous["contact_name"]), " ".join(previous["contact_name"].split()),
            mailbox, f"{previous['company']} team", *_GENERIC_GREETINGS,
        ) if name
    }
    new_name = greeting_name(values.get("contact_name", previous["contact_name"]), values.get("company", previous["company"]))
    # A sent email's text is the record of what went out, so only unsent drafts move.
    initial_unsent = not previous.get("sent_at") and previous["status"] in UNSENT_STATUSES
    changed = []
    for kind, (_subject_field, body_field, _status_field) in DRAFT_KINDS.items():
        if not (initial_unsent or (kind == "follow_up" and previous["status"] == "sent")):
            continue
        if body_field in values and values[body_field] != previous[body_field]:
            continue
        swapped = readdress_greeting(previous[body_field] or "", old_names, new_name)
        if swapped:
            values[body_field] = swapped[0]
            changed.append((kind, swapped[1], swapped[2]))
    return changed


# A confirmed address is the most actionable row, so it leads the list; an
# address still waiting on verification comes next, then companies with none.
CONTACT_RANK_SQL = (
    "CASE WHEN COALESCE(contact_email, '') <> '' AND contact_confidence = 'confirmed' THEN 0 "
    "WHEN COALESCE(contact_email, '') <> '' THEN 1 ELSE 2 END"
)


def list_targets(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    status: str = "",
    channel: str = "",
    query: str = "",
    today: date | None = None,
) -> list[dict[str, Any]]:
    today = today or local_today(conn, user_id)
    conn.row_factory = sqlite3.Row
    where = ["user_id=?"]
    params: list[Any] = [user_id]
    if status:
        where.append("status=?")
        params.append(status)
    if channel:
        where.append("channel=?")
        params.append(channel)
    if query.strip():
        term = f"%{query.strip()}%"
        where.append("(company LIKE ? OR contact_name LIKE ? OR location LIKE ? OR summary LIKE ? OR fit_rationale LIKE ? OR notes LIKE ?)")
        params.extend([term] * 6)
    rows = conn.execute(
        f"""
        {SELECT_TARGETS} WHERE {' AND '.join(where)}
        ORDER BY {CONTACT_RANK_SQL}, priority, CASE WHEN follow_up_at IS NULL THEN 1 ELSE 0 END, follow_up_at, company COLLATE NOCASE
        """,
        params,
    ).fetchall()
    regions = user_regions(conn, user_id)
    home = user_home(conn, user_id, regions)
    return [_record(row, today, regions, home) for row in rows]


def is_new_from_search(item: dict[str, Any]) -> bool:
    return item["origin"] == "discovery" and item["status"] in {"not_started", "drafted"} and not item.get("sent_at")


def outreach_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    by_status = {status: 0 for status in OUTREACH_STATUSES}
    for item in items:
        by_status[item["status"]] += 1
    return {
        "by_status": by_status,
        "drafts_awaiting_approval": sum(
            1 for item in items
            if draft_needs_review(item, "initial") or draft_needs_review(item, "follow_up")
        ),
        # Not yet contacted: a deep search draft already moves a target to Drafted.
        "new_from_search": sum(1 for item in items if is_new_from_search(item)),
        "follow_ups_due": sum(1 for item in items if item["follow_up_due"]),
        "revisits_due": sum(1 for item in items if item["revisit_due"]),
        "awaiting_reply": sum(by_status[status] for status in AWAITING_REPLY),
        # A company with no contact yet has nothing to verify.
        "unverified_contacts": sum(
            1 for item in items
            if item["contact_confidence"] == "unverified"
            or (item["contact_confidence"] == "unknown" and (item["contact_email"] or item["contact_name"]))
        ),
        "channels": sorted({item["channel"] for item in items if item["channel"]}),
    }


def draft_needs_review(item: dict[str, Any], kind: str) -> bool:
    if kind == "initial":
        return item["draft_status"] == "generated" and not item.get("sent_at") and item["status"] in {"not_started", "drafted"}
    return item["follow_up_status"] == "generated" and item["status"] == "sent"


def get_target(
    conn: sqlite3.Connection,
    target_id: str,
    *,
    user_id: str,
    include_events: bool = False,
    today: date | None = None,
) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    row = conn.execute(f"{SELECT_TARGETS} WHERE id=? AND user_id=?", (target_id, user_id)).fetchone()
    if not row:
        raise OutreachNotFoundError(target_id)
    regions = user_regions(conn, user_id)
    item = _record(row, today or local_today(conn, user_id), regions, user_home(conn, user_id, regions))
    if include_events:
        item["events"] = [
            dict(event)
            for event in conn.execute(
                "SELECT event_type, from_status, to_status, detail, created_at FROM outreach_events WHERE target_id=? AND user_id=? ORDER BY created_at DESC",
                (target_id, user_id),
            ).fetchall()
        ]
    return item


def create_target(
    conn: sqlite3.Connection,
    payload: dict[str, Any],
    *,
    user_id: str,
    today: date | None = None,
    origin: str = "manual",
    discovery_run_id: str | None = None,
) -> dict[str, Any]:
    if origin not in OUTREACH_ORIGINS:
        raise ValueError(f"origin must be one of: {', '.join(OUTREACH_ORIGINS)}")
    today = today or local_today(conn, user_id)
    values = _normalize(payload, partial=False)
    values.setdefault("status", "not_started")
    _apply_status_side_effects(values, None, today)
    _apply_draft_side_effects(values, None)
    values["origin"] = origin
    if origin == "discovery":
        values["research_confidence"] = "unverified"
    # Only a target the student created here may claim they typed its location.
    # An import file's word is not evidence of anything, so it records no basis
    # at all; what the file claimed is kept as an event below.
    if values.get("location"):
        values["location_basis"] = {"discovery": "research", "manual": "manual"}.get(origin, "")
    if discovery_run_id:
        values["discovery_run_id"] = discovery_run_id
    target_id = f"outreach-{uuid4().hex}"
    timestamp = utc_now()
    columns = ["id", "user_id", *values.keys(), "created_at", "updated_at"]
    try:
        with conn:
            # "Acme Robotics, Inc." is the same company as a tracked "Acme Robotics".
            tracked = conn.execute("SELECT company FROM outreach_targets WHERE user_id=?", (user_id,)).fetchall()
            same = next((row[0] for row in tracked if company_key(row[0]) == company_key(values["company"])), None)
            if same is not None:
                raise ValueError(f"{same} is already in your outreach list")
            conn.execute(
                f"INSERT INTO outreach_targets({', '.join(columns)}) VALUES({', '.join('?' * len(columns))})",
                [target_id, user_id, *values.values(), timestamp, timestamp],
            )
            _log(conn, target_id, user_id, "created", to_status=values["status"])
            # The provenance the import could not honour still has to be
            # answerable later, so the refused claim is recorded verbatim.
            if values.get("location") and origin == "import":
                _log(conn, target_id, user_id, "location_import_claim", detail=_claim_detail(payload))
            # Adding a company back by hand undoes an earlier deletion.
            conn.execute(
                "DELETE FROM outreach_dismissed WHERE user_id=? AND company_key=?",
                (user_id, company_key(values["company"])),
            )
    except Exception as exc:
        if _is_unique_violation(exc):
            raise ValueError(f"{values['company']} is already in your outreach list") from exc
        raise
    return get_target(conn, target_id, user_id=user_id, today=today)


class _ConfirmRaced(Exception):
    """The row moved under a confirmation. Internal; never leaves this module."""


def _confirming(payload: dict[str, Any], previous: dict[str, Any], values: dict[str, Any]) -> bool:
    """Whether this PATCH confirms the location the student was actually shown.

    ``confirm_location`` carries the displayed location, not a flag. A bare
    ``true`` cannot say which place is being vouched for, and an enrichment pass
    can change the location between the page rendering and the click — so a
    flag would let the student's name be attached to a place they never saw,
    permanently, since ``manual`` outranks every source.
    """
    claim = payload.get("confirm_location")
    if claim is None or claim is False:
        return False
    if not isinstance(claim, str) or not claim.strip():
        raise ValueError("confirm_location must be the location the page showed, not a flag")
    if "location" in values or not previous["location"]:
        return False
    if " ".join(claim.split()).casefold() != " ".join(previous["location"].split()).casefold():
        raise LocationConflictError(
            f"This target is now in {previous['location']}, not {claim.strip()}. "
            "Have a look before confirming it."
        )
    return not previous["location_verified"]


def update_target(conn: sqlite3.Connection, target_id: str, payload: dict[str, Any], *, user_id: str, today: date | None = None) -> dict[str, Any]:
    today = today or local_today(conn, user_id)
    previous = get_target(conn, target_id, user_id=user_id, today=today)
    # A confirmation is a compare-and-swap, so it may have to be recomputed once
    # against a row that moved. Everything else runs on the first pass.
    for _ in range(2):
        values = _normalize(payload, partial=True)
        if "location" in values:
            if values["location"] == previous["location"]:
                del values["location"]
            else:
                # The student's own word outranks every source, so it is never overwritten.
                values["location_basis"] = "manual" if values["location"] else ""
                values["location_source_url"] = ""
                values["location_inferred"] = 0
        typed_location = "location" in values
        confirming = _confirming(payload, previous, values)
        if confirming:
            # The student vouches for the place shown. A site mention keeps its page;
            # a deep search location becomes the student's own entry.
            values["location_inferred"] = 0
            if previous["location_basis"] in {"research", ""}:
                values["location_basis"] = "manual"
        _apply_status_side_effects(values, previous, today)
        readdressed = _readdress_drafts(values, previous)
        withdrawn = _apply_draft_side_effects(values, previous)
        if not values:
            return previous
        assignments = ", ".join(f"{column}=?" for column in values)
        # Guarding on the location text alone is not enough: an enrichment pass
        # can attach a stronger basis and a source URL to the *same* text, and a
        # stale confirmation would then overwrite that basis with "manual" while
        # leaving the company's URL attached — "your entry" linking to a page the
        # student never vouched for.
        guard, guarded = "", []
        if confirming:
            guard = " AND location=? AND location_basis=? AND location_inferred=?"
            guarded = [
                previous["location"], previous["location_basis"] or "",
                int(bool(previous["location_inferred"])),
            ]
        try:
            with conn:
                cursor = conn.execute(
                    f"UPDATE outreach_targets SET {assignments}, updated_at=? WHERE id=? AND user_id=?{guard}",
                    [*values.values(), utc_now(), target_id, user_id, *guarded],
                )
                if confirming and cursor.rowcount == 0:
                    raise _ConfirmRaced()
                if "status" in values and values["status"] != previous["status"]:
                    _log(conn, target_id, user_id, "status", from_status=previous["status"], to_status=values["status"])
                if confirming:
                    _log(conn, target_id, user_id, "location_confirmed", detail=f"You confirmed {previous['location']}")
                if typed_location and values["location"]:
                    # Its own event type, not the "location_recorded" a source
                    # establishing a location writes: telling the student's word
                    # apart from a source's is the whole point of this record.
                    _log(conn, target_id, user_id, "location_entered", detail=f"You entered {values['location']}")
                swapped = {kind for kind, _old, _new in readdressed}
                if "initial" not in swapped and (("email_subject" in values and values["email_subject"] != previous["email_subject"]) or (
                    "email_body" in values and values["email_body"] != previous["email_body"]
                )):
                    _log(conn, target_id, user_id, "draft_edited")
                if "follow_up" not in swapped and (("follow_up_subject" in values and values["follow_up_subject"] != previous["follow_up_subject"]) or (
                    "follow_up_body" in values and values["follow_up_body"] != previous["follow_up_body"]
                )):
                    _log(conn, target_id, user_id, "follow_up_edited")
                for kind, old_line, new_line in readdressed:
                    label = "Draft" if kind == "initial" else "Follow-up"
                    _log(conn, target_id, user_id, "greeting_updated", detail=f'{label}: "{old_line}" is now "{new_line}" for the new contact')
                for kind in withdrawn:
                    _log(conn, target_id, user_id, "approval_withdrawn", detail=f"The {kind.replace('_', '-')} draft changed after approval")
        except _ConfirmRaced:
            previous = get_target(conn, target_id, user_id=user_id, today=today)
            if previous["location_verified"]:
                # A page-checked basis landed first. There is nothing left to
                # confirm, and demoting it to "manual" would lose provenance.
                return previous
            continue
        except Exception as exc:
            if _is_unique_violation(exc):
                raise ValueError(f"{values.get('company')} is already in your outreach list") from exc
            raise
        return get_target(conn, target_id, user_id=user_id, today=today)
    raise LocationConflictError(
        "This target's location kept changing while you confirmed it. Have another look."
    )


def _is_unique_violation(exc: Exception) -> bool:
    if isinstance(exc, sqlite3.IntegrityError) and "UNIQUE" in str(exc).upper():
        return True
    if getattr(exc, "sqlstate", None) == "23505" or getattr(exc, "pgcode", None) == "23505":
        return True
    return type(exc).__name__ == "UniqueViolation"


def delete_target(conn: sqlite3.Connection, target_id: str, *, user_id: str) -> bool:
    """Delete a target and remember the company, so the deep search does not bring it back."""
    row = conn.execute("SELECT company, website FROM outreach_targets WHERE id=? AND user_id=?", (target_id, user_id)).fetchone()
    if not row:
        return False
    with conn:
        conn.execute("DELETE FROM outreach_targets WHERE id=? AND user_id=?", (target_id, user_id))
        conn.execute(
            """
            INSERT INTO outreach_dismissed(user_id, company_key, company, domain, dismissed_at) VALUES(?, ?, ?, ?, ?)
            ON CONFLICT(user_id, company_key) DO UPDATE SET company=excluded.company, domain=excluded.domain,
                dismissed_at=excluded.dismissed_at
            """,
            (user_id, company_key(row[0]), row[0], website_domain(str(row[1] or "")), utc_now()),
        )
    return True


def existing_keys(conn: sqlite3.Connection, *, user_id: str) -> tuple[set[str], set[str]]:
    """Company keys (see company_key) and website domains already tracked."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT company, website FROM outreach_targets WHERE user_id=?", (user_id,)).fetchall()
    names = {company_key(row["company"]) for row in rows}
    domains = {website_domain(str(row["website"])) for row in rows}
    return names, domains - {""}


def import_targets(
    conn: sqlite3.Connection,
    records: list[dict[str, Any]],
    *,
    user_id: str,
    origin: str = "import",
    discovery_run_id: str | None = None,
) -> dict[str, Any]:
    """Add new companies; never overwrite a target that already exists.

    A company already exists when its name (ignoring case, punctuation, and
    legal forms like "Inc.") or its website's domain matches one that is
    tracked, so "Acme" and "Acme Robotics" at acme.com stay one row.
    """
    if len(records) > 500:
        raise ValueError("Outreach imports are limited to 500 targets")
    names, domains = existing_keys(conn, user_id=user_id)
    imported, skipped, errors, created_ids = 0, 0, [], []
    for index, record in enumerate(records, start=1):
        record = {key: value for key, value in record.items() if key not in IMPORT_IGNORED_FIELDS}
        research_confidence = record.pop("research_confidence", None)
        if research_confidence == "unverified":
            record["_research_confidence"] = "unverified"
        company = str(record.get("company") or "").strip()
        domain = website_domain(str(record.get("website") or ""))
        if company_key(company) in names or (domain and domain in domains):
            skipped += 1
            continue
        try:
            internal_confidence = record.pop("_research_confidence", None)
            target = create_target(conn, record, user_id=user_id, origin=origin, discovery_run_id=discovery_run_id)
            if internal_confidence == "unverified" and target["research_confidence"] != "unverified":
                with conn:
                    conn.execute(
                        "UPDATE outreach_targets SET research_confidence='unverified' WHERE id=? AND user_id=?",
                        (target["id"], user_id),
                    )
                target = get_target(conn, target["id"], user_id=user_id)
        except ValueError as exc:
            errors.append({"row": index, "company": company, "error": str(exc)})
            continue
        names.add(company_key(company))
        if domain:
            domains.add(domain)
        created_ids.append(target["id"])
        imported += 1
    return {"imported": imported, "skipped": skipped, "errors": errors, "created_ids": created_ids}


def export_csv(items: list[dict[str, Any]]) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=EXPORT_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for item in items:
        row = {**item, "source_urls": "\n".join(item.get("source_urls", []))}
        writer.writerow({key: _encode_csv_cell(value) for key, value in row.items()})
    return output.getvalue()


def parse_import(data: bytes, filename: str) -> list[dict[str, Any]]:
    text_data = data.decode("utf-8-sig")
    if filename.lower().endswith(".csv"):
        records = []
        for row in csv.DictReader(io.StringIO(text_data)):
            record = {key: _decode_csv_cell(value) for key, value in row.items() if key and key not in IMPORT_IGNORED_FIELDS}
            records.append(record)
        return records
    parsed = json.loads(text_data)
    records = parsed if isinstance(parsed, list) else parsed.get("items") if isinstance(parsed, dict) else None
    if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
        raise ValueError("Import must be a JSON list or an object with an items list")
    return records


def _encode_csv_cell(value: Any) -> Any:
    if not isinstance(value, str) or not value:
        return value
    if value.startswith("'"):
        return "'" + value
    return "'" + value if value[0] in "=+-@\t\r" else value


def _decode_csv_cell(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    if value.startswith("''"):
        return value[1:]
    if len(value) > 1 and value[0] == "'" and value[1] in "=+-@\t\r":
        return value[1:]
    return value


def approve_draft(
    conn: sqlite3.Connection,
    target_id: str,
    *,
    user_id: str,
    fingerprint: str,
    kind: str = "initial",
    acknowledge_warnings: bool = False,
) -> dict[str, Any]:
    """Approve a draft for hand-off to the student's own email account.

    Approval never sends anything. It unlocks the compose link, and is withdrawn
    again as soon as the words or the recipient change.
    """
    if kind not in DRAFT_KINDS:
        raise ValueError("kind must be initial or follow_up")
    subject_field, body_field, status_field = DRAFT_KINDS[kind]
    claims_field = "draft_claims_json" if kind == "initial" else "follow_up_claims_json"
    generated_field = "draft_generated_by" if kind == "initial" else "follow_up_generated_by"
    snapshot = conn.execute(
        f"SELECT {subject_field}, {body_field}, contact_email, {claims_field}, {generated_field}, contact_cc "
        "FROM outreach_targets WHERE id=? AND user_id=?",
        (target_id, user_id),
    ).fetchone()
    target = get_target(conn, target_id, user_id=user_id)
    if not snapshot:
        raise OutreachNotFoundError(target_id)
    values = tuple(snapshot[index] for index in range(6))
    expected = _draft_fingerprint(kind, *values)
    if fingerprint != expected:
        raise DraftChangedError("This draft changed since you opened it. Reload and review it again.")
    if not target[body_field] or not target[subject_field]:
        raise ValueError("Write or generate a subject and body before approving")
    if not target["contact_email"]:
        raise ValueError("Add a contact email before approving; the draft has no recipient")
    checks = target["draft_checks"] if kind == "initial" else target["follow_up_checks"]
    if checks["placeholders"]:
        raise ValueError("Fill these placeholders before approving: " + ", ".join(checks["placeholders"]))
    if kind == "initial" and target["draft_location"]["missing"]:
        raise ValueError(missing_location_message(target))
    claims =target["draft_claims"] if kind == "initial" else target["follow_up_claims"]
    research_warning = (
        target.get("research_confidence") == "unverified"
        or any(str(claim.get("basis", "")).startswith("unverified:") for claim in claims)
    )
    warnings = list(checks["warnings"])
    if research_warning:
        warnings.append("this company's research is unverified deep-search text; confirm the research or accept the risk")
    if target["contact_confidence"] == "unverified":
        cc = f"; {target['contact_cc']} is in Cc" if target["contact_cc"] else ""
        warnings.append(f"{target['contact_email']} is a guessed address, not confirmed{cc}; check it or accept the risk")
    if warnings and not acknowledge_warnings:
        raise ValueError("Review these warnings, then approve again to accept them: " + "; ".join(warnings))
    if target[status_field] == "approved":
        return target
    timestamp = utc_now()
    assignments = {status_field: "approved"}
    if kind == "initial":
        assignments["draft_approved_at"] = timestamp
        if target["status"] == "not_started":
            assignments["status"] = "drafted"
    with conn:
        cursor = conn.execute(
            f"UPDATE outreach_targets SET {', '.join(f'{column}=?' for column in assignments)}, updated_at=? "
            f"WHERE id=? AND user_id=? AND {subject_field}=? AND {body_field}=? AND contact_email=? "
            f"AND {claims_field}=? AND {generated_field}=? AND contact_cc=?",
            [*assignments.values(), timestamp, target_id, user_id, *values],
        )
        if not cursor.rowcount:
            raise DraftChangedError("This draft changed since you opened it. Reload and review it again.")
        detail = "; ".join(warnings) if warnings else ""
        _log(conn, target_id, user_id, "draft_approved" if kind == "initial" else "follow_up_approved",
             detail=f"Accepted warnings: {detail}" if detail else "")
        if assignments.get("status"):
            _log(conn, target_id, user_id, "status", from_status=target["status"], to_status="drafted")
    return get_target(conn, target_id, user_id=user_id)


def confirm_research(conn: sqlite3.Connection, target_id: str, *, user_id: str) -> dict[str, Any]:
    target = get_target(conn, target_id, user_id=user_id)
    if target["research_confidence"] == "confirmed":
        return target
    with conn:
        conn.execute(
            "UPDATE outreach_targets SET research_confidence='confirmed', updated_at=? WHERE id=? AND user_id=?",
            (utc_now(), target_id, user_id),
        )
        _log(conn, target_id, user_id, "research_confirmed")
    return get_target(conn, target_id, user_id=user_id)


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


def log_reply(
    conn: sqlite3.Connection, target_id: str, text: str, *, user_id: str, decisions: DecisionClient | None = None,
) -> dict[str, Any]:
    """Record a pasted reply and suggest a status. The status is not changed here.

    With a decisions client the suggestion comes from Jev when it is sure enough;
    without one, or when Jev cannot answer, it comes from REPLY_PATTERNS. A
    delivery failure notice is not a reply, so it is not logged as one: it
    suggests "bounced", which the student applies to record the bounce.
    """
    body = str(text or "").replace("\r\n", "\n").strip()
    if not body:
        raise ValueError("Paste the reply text first")
    if len(body) > 20_000:
        raise ValueError("Reply is too long")
    target = get_target(conn, target_id, user_id=user_id)
    if bounce_notice(body):
        suggestion = {**suggest_reply_status(body), "source": "rules", "confidence": None, "model": "", "fallback_reason": ""}
        return {"suggestion": suggestion, "logged": False, "target": get_target(conn, target["id"], user_id=user_id, include_events=True)}
    suggestion = classify_reply(body, suggest_reply_status, decisions)
    with conn:
        _log(conn, target_id, user_id, "reply_logged", detail=body)
    return {"suggestion": suggestion, "logged": True, "target": get_target(conn, target["id"], user_id=user_id, include_events=True)}


NO_RESPONSE_AFTER_DAYS = 14


def lifecycle_suggestion(item: dict[str, Any], today: date | None = None) -> dict[str, str] | None:
    """Suggest closing a target that never answered a follow-up."""
    today = today or date.today()
    if item["status"] != "followed_up" or not item.get("follow_up_at"):
        return None
    try:
        due = date.fromisoformat(item["follow_up_at"])
    except ValueError:
        return None
    if (today - due).days >= NO_RESPONSE_AFTER_DAYS:
        return {"status": "no_response", "reason": f"No reply {NO_RESPONSE_AFTER_DAYS} days after the follow-up was sent"}
    return None


def queue_follow_up_reminders(
    conn: sqlite3.Connection,
    *,
    today: date | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Queue one in-app reminder per due follow-up or revisit date. Safe to run repeatedly."""
    conn.row_factory = sqlite3.Row
    statuses = ("sent", *sorted(REVISIT_STATUSES))
    rows = conn.execute(
        f"""
        SELECT id, user_id, company, status, follow_up_at FROM outreach_targets
        WHERE status IN ({', '.join('?' for _ in statuses)}) AND follow_up_at IS NOT NULL
        """,
        statuses,
    ).fetchall()
    rows = [
        row for row in rows
        if date.fromisoformat(row["follow_up_at"]) <= (today or local_today(conn, row["user_id"], now))
    ]
    queued = 0
    for row in rows:
        revisit = row["status"] in REVISIT_STATUSES
        event_key = f"outreach-{'revisit' if revisit else 'follow-up'}:{row['id']}:{row['follow_up_at']}"
        payload = {
            "subject": f"Get back in touch with {row['company']}" if revisit else f"Follow up with {row['company']}",
            "outreach_target_id": row["id"],
            "follow_up_at": row["follow_up_at"],
        }
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO notification_outbox(id, user_id, channel, event_key, payload_json, status, created_at)
                VALUES(?, ?, 'in_app', ?, ?, 'queued', ?)
                ON CONFLICT(user_id, channel, event_key) DO NOTHING
                """,
                (f"notification-{uuid4().hex}", row["user_id"], event_key, json.dumps(payload), utc_now()),
            )
        queued += max(cursor.rowcount, 0)
    return {"due": len(rows), "queued": queued}
