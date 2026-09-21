"""Where an outreach company is based and what it has raised, from sources that state it.

Two free sources, never a model:

* The company's own site: schema.org structured data with a postal address, a
  sentence like "headquartered in Austin, TX", or a street address with a ZIP
  code. When a site lists several places at the same level, none is chosen.
  Failing all of those, a site that names exactly one place ("Austin, TX" in
  its footer) gives that place as an inference: it is shown as not yet checked
  and a draft does not rely on it until the student confirms it. A site whose
  pages are empty without JavaScript is rendered in a headless browser when
  Playwright is installed (outreach_render.py).
* SEC Form D filings, which a US startup files after selling shares in a
  private round. EDGAR full-text search finds filings whose issuer has the same
  name as the target (ignoring case, punctuation, and "Inc."), and the filing
  gives the issuer's principal place of business, the amount sold, and the date.
  A name is not proof of identity, so two issuers with the target's name are
  reported as ambiguous, and a filing whose state disagrees with a location the
  target already has is kept as a possible match without changing anything.

Every location keeps its basis and source URL. The student's own entry is never
overwritten, the company's site outranks a filing, and both outrank a location
the deep search reported. SEC asks automated clients to identify themselves, so
lookups run only when PIPELINE_SEC_USER_AGENT names the student and a contact
address (for example "Jane Student jane@example.com").
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import xml.etree.ElementTree as ElementTree
from datetime import date, timedelta
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import httpx

from .outreach import LOCATION_BASES, US_STATES, _log, company_key, get_target, local_today, website_domain
from .outreach_contacts import USER_AGENT, SafeFetcher, _page_priority, _PageParser, _same_site, crawl_site, site_robots
from .outreach_render import PlaywrightRenderer
from .schema import utc_now

SEC_USER_AGENT_ENV = "PIPELINE_SEC_USER_AGENT"
EDGAR_SEARCH = "https://efts.sec.gov/LATEST/search-index"
EDGAR_ARCHIVES = "https://www.sec.gov/Archives/edgar/data"
# EDGAR allows ten requests a second; one lookup makes two.
SEC_REQUEST_GAP = 0.2
# An older filing may describe a company that has since moved, or another
# company that once used the name, so its address does not set a location.
FORM_D_LOCATION_MAX_AGE = timedelta(days=5 * 365)
RECHECK_AFTER = timedelta(days=30)
# Pages that tend to carry a company's address, for a location-only crawl.
LOCATION_PAGE_KEYWORDS = (
    ("contact", 1), ("location", 1), ("office", 1), ("about", 2), ("company", 2), ("visit", 3),
)
LOCATION_MAX_PAGES = 4
BASIS_RANK = {**{basis: rank for rank, basis in enumerate(LOCATION_BASES)}, "": len(LOCATION_BASES)}
# A place the site only mentions ranks below a page that states the company is
# based there, and above the deep search's word.
INFERRED_RANK = (BASIS_RANK["web_search"] + BASIS_RANK["research"]) / 2
# Fewer characters than this across the pages read means the site probably
# builds its text with JavaScript.
THIN_TEXT = 200
BASIS_LABELS = {
    "manual": "your entry", "company_site": "the company's site", "sec_form_d": "an SEC Form D filing",
    "web_search": "a web search", "research": "the deep search",
}

_STATE_CODES = "|".join(US_STATES)
_STATE_NAMES = "|".join(sorted((re.escape(name) for name in US_STATES.values()), key=len, reverse=True))
_STATE = rf"(?:{_STATE_CODES}|(?i:{_STATE_NAMES}))"
_CITY = r"[A-Z][A-Za-z.'’-]+(?:[ -][A-Z][A-Za-z.'’-]+){0,4}"
# "Austin, TX 78701" or "Austin, Texas 78701". The ZIP code is what makes a
# line an address rather than a passing mention.
STREET_ADDRESS = re.compile(rf"\b({_CITY}),\s*({_STATE})\.?,?\s+\d{{5}}(?:-\d{{4}})?\b")
# "Headquartered in Austin, TX", "based out of San Carlos, California", "HQ: Austin, TX".
HEADQUARTERS = re.compile(
    rf"\b(?i:headquartered|headquarters|hq|head office|based)(?:\s+(?i:in|out of)|\s*[:|–—-])\s*"
    rf"(?:(?i:the)\s+)?({_CITY}),\s*({_STATE})\b"
)
# A place standing alone on a line ("Austin, TX", "Buda, Texas 78610, USA") or
# introduced by "in", "at", or a separator. "Shipped to Denver, CO" is not one.
PLACE_MENTION = re.compile(
    rf"(?:^|(?<=[|•·–—:(])\s*|\b(?i:in|at)\s+)({_CITY}),\s*({_STATE})"
    rf"(?:\s+\d{{5}}(?:-\d{{4}})?)?(?:,?\s*(?i:usa|us|united states))?(?=\s*(?:$|[|•·–—),.;]))"
)
HEADQUARTERS_WORDS = re.compile(r"\b(?:headquarters|headquartered|hq|head office)\b", re.IGNORECASE)
# Words that end a street, a unit, or a company name: an address run together
# on one line ("500 Congress Avenue Austin, TX") keeps only what follows them.
# A street word ends a street only after a street name ("500 Congress Avenue
# Austin"); first in the name it is part of the city ("Center Point", "St. Louis").
_STREET_WORDS = {
    "st", "street", "ave", "avenue", "blvd", "boulevard", "rd", "road", "dr", "drive", "ln", "lane", "way",
    "pkwy", "parkway", "hwy", "highway", "ct", "court", "pl", "place", "plaza", "cir", "circle", "loop",
    "trail", "trl", "suite", "ste", "floor", "fl", "unit", "building", "bldg", "center", "centre", "sq",
    "square",
}
# A company form or a label ends what came before the city wherever it appears:
# "Acme, Inc. Austin", "Machinist Austin, TX", "Headquarters Austin, TX".
_LABEL_WORDS = {
    "inc", "llc", "corp", "ltd", "co", "pbc", "copyright", "address", "office", "offices",
    "engineer", "engineering", "machinist", "technician", "intern", "internship", "manager", "contact",
    "location", "locations", "description", "remote", "hybrid", "onsite", "hq", "headquarters", "loc",
}
# A label run into the city by adjacent inline tags: "<b>Headquarters</b>Austin"
# reads as "HeadquartersAustin". Only a known label is split off, so McKinney
# and DeKalb stay whole.
_LABEL_RUN_ON = re.compile(
    r"^(" + "|".join(sorted((re.escape(word) for word in _LABEL_WORDS if len(word) > 2), key=len, reverse=True)) + r")(?=[A-Z])",
    re.IGNORECASE,
)
ORGANIZATION_TYPES = {"organization", "corporation", "localbusiness", "professionalservice", "onlinebusiness"}


class SecUnavailableError(RuntimeError):
    """EDGAR could not be reached or answered with something unreadable."""


# ---------------------------------------------------------------------------
# Locations stated on the company's own site


def _state_code(text: str) -> str:
    value = " ".join(str(text or "").split()).strip(" .")
    if value.upper() in US_STATES:
        return value.upper()
    lowered = value.casefold()
    return next((code for code, name in US_STATES.items() if name == lowered), "")


def _city(text: str, *, from_prose: bool) -> str:
    words = " ".join(str(text or "").split()).split(" ")
    if from_prose:
        words = [part for word in words for part in _LABEL_RUN_ON.sub(r"\1 ", word).split(" ")]
        cut = max((
            index for index, word in enumerate(words)
            if word.strip(".,").casefold() in _LABEL_WORDS or (index and word.strip(".,").casefold() in _STREET_WORDS)
        ), default=-1)
        words = words[cut + 1:]
    city = " ".join(words).strip(" ,.")
    return city.title() if city.isupper() else city


def format_location(city: str, region: str = "", country: str = "", *, from_prose: bool = False) -> str:
    """ "City, ST" for a US place; "City, Region, Country" as given otherwise; "" without a city.

    from_prose marks a city matched in running text, where a street or company
    name run into it ("500 Congress Avenue Austin") is trimmed off.
    """
    city = _city(city, from_prose=from_prose)
    if not city:
        return ""
    state = _state_code(region)
    country = " ".join(str(country or "").split())
    if state and country.casefold() in {"", "us", "usa", "united states", "united states of america"}:
        return f"{city}, {state}"
    parts = [city, " ".join(str(region or "").split())]
    if country.casefold() not in {"us", "usa", "united states", "united states of america"}:
        parts.append(country)
    parts = [part for part in parts if part]
    return ", ".join(parts) if len(parts) > 1 else ""


def same_place(first: str, second: str) -> bool:
    """Whether two location strings name the same city (and state, when both give one)."""
    def split(text: str) -> tuple[str, str]:
        head, _, rest = str(text or "").partition(",")
        return " ".join(head.casefold().split()), _state_code(rest.split(",")[0]) if rest else ""

    (city_a, state_a), (city_b, state_b) = split(first), split(second)
    return bool(city_a) and city_a == city_b and (not state_a or not state_b or state_a == state_b)


def _json_ld_nodes(value: Any):
    if isinstance(value, list):
        for item in value:
            yield from _json_ld_nodes(item)
    elif isinstance(value, dict):
        yield value
        for key in ("@graph", "mainEntity", "publisher", "organization", "parentOrganization"):
            if key in value:
                yield from _json_ld_nodes(value[key])


def _is_organization(node: dict[str, Any]) -> bool:
    types = node.get("@type")
    names = [types] if isinstance(types, str) else types if isinstance(types, list) else []
    lowered = {str(name).casefold() for name in names}
    return bool(lowered & ORGANIZATION_TYPES) or any(name.endswith(("organization", "business")) for name in lowered)


def _json_ld_locations(blocks: list[str]) -> list[str]:
    found = []
    for block in blocks:
        try:
            data = json.loads(block)
        except (ValueError, RecursionError):
            continue
        for node in _json_ld_nodes(data):
            if not _is_organization(node):
                continue
            addresses = node.get("address")
            addresses = addresses if isinstance(addresses, list) else [addresses]
            for address in addresses:
                if isinstance(address, str):
                    found.extend(format_location(city, state, from_prose=True) for city, state in STREET_ADDRESS.findall(address))
                elif isinstance(address, dict):
                    country = address.get("addressCountry")
                    if isinstance(country, dict):
                        country = country.get("name", "")
                    found.append(format_location(
                        str(address.get("addressLocality") or ""), str(address.get("addressRegion") or ""), str(country or ""),
                    ))
    return [location for location in found if location]


def _without_company(location: str, company: str) -> str:
    """ "Acme Austin, TX" from a footer run-on becomes "Austin, TX" for the company Acme."""
    words = company_key(company).split()
    city, _, rest = location.partition(",")
    city_words = city.split()
    if words and len(city_words) > len(words) and [word.casefold() for word in city_words[:len(words)]] == words:
        return ", ".join([" ".join(city_words[len(words):]), rest.strip()])
    return location


def site_location(pages: list[dict[str, Any]], *, company: str = "") -> dict[str, Any]:
    """The place a company's own pages say it is based, or why none was chosen.

    Structured data outranks a stated headquarters, which outranks a bare street
    address, which outranks a place the site merely names. Within the strongest
    level found, the pages must agree on one place. A place from the last level
    is returned with "inferred": the site never says the company is based there.
    """
    levels: dict[int, list[tuple[str, str]]] = {0: [], 1: [], 2: [], 3: []}
    for page in pages:
        parser = page["parser"]
        for location in _json_ld_locations(parser.json_ld):
            levels[0].append((location, page["url"]))
        for line in parser.lines:
            for city, state in HEADQUARTERS.findall(line):
                location = _without_company(format_location(city, state, from_prose=True), company)
                if location:
                    levels[1].append((location, page["url"]))
            for city, state in STREET_ADDRESS.findall(line):
                location = _without_company(format_location(city, state, from_prose=True), company)
                if location:
                    levels[1 if HEADQUARTERS_WORDS.search(line) else 2].append((location, page["url"]))
            for city, state in PLACE_MENTION.findall(line):
                location = _without_company(format_location(city, state, from_prose=True), company)
                if location and len(location.partition(",")[0].split()) <= 3:
                    levels[3].append((location, page["url"]))
    for level in (0, 1, 2, 3):
        found = levels[level]
        if not found:
            continue
        places: list[str] = []
        for location, _url in found:
            if not any(same_place(location, place) for place in places):
                places.append(location)
        if len(places) == 1:
            found_at = {"location": places[0], "source_url": found[0][1]}
            return {**found_at, "inferred": True} if level == 3 else found_at
        return {"location": "", "ambiguous": places[:5]}
    return {"location": ""}


# ---------------------------------------------------------------------------
# Recording a location with its basis


def apply_location(
    conn: sqlite3.Connection,
    target_id: str,
    *,
    user_id: str,
    location: str,
    basis: str,
    source_url: str,
    inferred: bool = False,
) -> str:
    """Record a sourced location unless a better basis already holds one.

    Returns what happened: "recorded", "confirmed" (the same place, now with a
    better basis), "unchanged", or "kept" (an equal or better basis stays).
    An inferred location never confirms anything; it can only fill a gap or
    replace the deep search's word.
    """
    # The decision is made from a row read outside the write, so two enrichment
    # passes running together can both read the weak state and the loser's
    # weaker result can land last. The write is guarded on exactly what was
    # read; if the row moved, the decision is made again against what is there.
    for _ in range(2):
        target = get_target(conn, target_id, user_id=user_id)
        current, current_basis = target["location"], target.get("location_basis") or ""
        current_inferred = bool(target.get("location_inferred"))
        current_rank = INFERRED_RANK if current_inferred else BASIS_RANK.get(current_basis, BASIS_RANK[""])
        if current and (current_basis == "manual" or current_rank < (INFERRED_RANK if inferred else BASIS_RANK[basis])):
            return "kept"
        same = bool(current) and same_place(current, location)
        if current_basis == basis and same and current_inferred == inferred:
            outcome = "unchanged"
        elif same and not inferred:
            outcome = "confirmed"
        else:
            outcome = "recorded"
        if outcome == "unchanged" and current == location and target.get("location_source_url") == source_url:
            return outcome
        with conn:
            cursor = conn.execute(
                "UPDATE outreach_targets SET location=?, location_basis=?, location_source_url=?, location_inferred=?, updated_at=? "
                "WHERE id=? AND user_id=? AND location=? AND location_basis=? AND location_inferred=?",
                (location, basis, source_url, int(inferred), utc_now(), target_id, user_id,
                 current, current_basis, int(current_inferred)),
            )
            if cursor.rowcount == 0:
                continue
            if outcome == "confirmed":
                _log(conn, target_id, user_id, "location_confirmed",
                     detail=f"{BASIS_LABELS[basis].capitalize()} agrees: {location} ({source_url})")
            elif outcome == "recorded":
                replaced = f"; replaced {current} from {BASIS_LABELS.get(current_basis, 'an unknown source')}" if current and not same else ""
                how = "the only place the company's site names" if inferred else BASIS_LABELS[basis]
                _log(conn, target_id, user_id, "location_recorded", detail=f"{location} from {how} ({source_url}){replaced}")
        return outcome
    # Another writer keeps winning. Dropping this result is safe; overwriting
    # whatever landed on a stale decision is not.
    return "kept"


def _text_chars(pages: list[dict[str, Any]]) -> int:
    return sum(len(" ".join(page["parser"].lines)) for page in pages)


def record_site_location(conn: sqlite3.Connection, target_id: str, *, user_id: str, pages: list[dict[str, Any]]) -> dict[str, Any]:
    """Record where the given pages say the company is. text_chars tells a caller whether the pages were empty."""
    company = get_target(conn, target_id, user_id=user_id)["company"]
    found = site_location(pages, company=company)
    size = {"text_chars": _text_chars(pages), "pages": len(pages)}
    if not found["location"]:
        if found.get("ambiguous"):
            with conn:
                _log(conn, target_id, user_id, "location_ambiguous",
                     detail="The company's site names several places: " + "; ".join(found["ambiguous"]))
        return {**found, **size, "outcome": "ambiguous" if found.get("ambiguous") else "none"}
    outcome = apply_location(
        conn, target_id, user_id=user_id, location=found["location"], basis="company_site",
        source_url=found["source_url"], inferred=bool(found.get("inferred")),
    )
    return {**found, **size, "outcome": outcome}


def rendered_pages(
    website: str,
    *,
    renderer: PlaywrightRenderer,
    fetcher: SafeFetcher,
    max_pages: int = LOCATION_MAX_PAGES,
    keywords: tuple[tuple[str, int], ...] = LOCATION_PAGE_KEYWORDS,
) -> list[dict[str, Any]]:
    """Up to max_pages same-site pages as a browser shows them after scripts run, honoring robots.txt.

    keywords picks which links to follow: location pages by default, people pages for contacts.
    """
    domain = website_domain(website)
    if not domain:
        return []
    parsed = urlsplit(website if "//" in website else f"https://{website}")
    start = f"{parsed.scheme}://{parsed.netloc}/"
    robots = site_robots(start, fetcher)
    queue: list[tuple[int, str]] = [(0, start)]
    seen: set[str] = set()
    pages: list[dict[str, Any]] = []
    while queue and len(pages) < max_pages:
        queue.sort()
        _rank, url = queue.pop(0)
        key = url.split("#", 1)[0].rstrip("/")
        if key in seen or not robots.can_fetch(USER_AGENT, url):
            continue
        seen.add(key)
        rendered = renderer.render(url)
        if rendered is None or not _same_site(rendered[0], domain):
            continue
        parser = _PageParser()
        parser.feed(rendered[1])
        parser.close()
        pages.append({"url": rendered[0], "parser": parser, "raw": rendered[1]})
        for href, text in parser.links:
            link = urljoin(rendered[0], href)
            rank = _page_priority(link, text, keywords)
            if rank is not None and link.startswith(("http://", "https://")) and _same_site(link, domain):
                queue.append((rank, link))
    return pages


def render_site_location(
    conn: sqlite3.Connection,
    target_id: str,
    *,
    user_id: str,
    plain: dict[str, Any],
    renderer: PlaywrightRenderer | None,
    fetcher: SafeFetcher,
) -> dict[str, Any]:
    """When the plain fetch read (almost) nothing and found no place, try again in a browser."""
    if renderer is None or plain.get("outcome") != "none" or plain.get("text_chars", 0) >= THIN_TEXT:
        return plain
    website = get_target(conn, target_id, user_id=user_id)["website"]
    pages = rendered_pages(website, renderer=renderer, fetcher=fetcher)
    if not pages:
        return {**plain, "rendered": False, "render_error": renderer.unavailable}
    return {**record_site_location(conn, target_id, user_id=user_id, pages=pages), "rendered": True}


# ---------------------------------------------------------------------------
# SEC Form D


def sec_fetcher() -> SafeFetcher | None:
    """A fetcher that identifies the student to EDGAR, or None when none is configured."""
    agent = os.environ.get(SEC_USER_AGENT_ENV, "").strip()
    if not agent or "@" not in agent:
        return None
    return SafeFetcher(httpx.Client(
        timeout=15.0,
        follow_redirects=False,
        verify=True,
        headers={"User-Agent": agent, "Accept": "application/json, application/xml, text/xml"},
    ))


def _search_name(company: str) -> str:
    words = " ".join(str(company or "").split()).split(" ")
    while len(words) > 1 and words[-1].strip(",.").casefold() in {"inc", "incorporated", "corp", "corporation", "llc", "ltd", "co", "pbc"}:
        words.pop()
    return " ".join(words).rstrip(",")


def _money(text: str | None) -> int | str | None:
    value = str(text or "").strip()
    if not value:
        return None
    if value.isdigit():
        return int(value)
    return value  # "Indefinite"


def _filing_details(xml_text: str) -> dict[str, Any]:
    root = ElementTree.fromstring(xml_text)

    def text(path: str) -> str:
        node = root.find(path)
        return " ".join((node.text or "").split()) if node is not None and node.text else ""

    people = []
    for person in root.findall("./relatedPersonsList/relatedPersonInfo"):
        name = " ".join(filter(None, (
            (person.findtext("./relatedPersonName/firstName") or "").strip(),
            (person.findtext("./relatedPersonName/lastName") or "").strip(),
        )))
        roles = [role.text.strip() for role in person.findall("./relatedPersonRelationshipList/relationship") if role.text]
        if name:
            people.append({"name": name, "roles": roles})
    state = text("./primaryIssuer/issuerAddress/stateOrCountry")
    description = text("./primaryIssuer/issuerAddress/stateOrCountryDescription")
    return {
        "issuer": text("./primaryIssuer/entityName"),
        "location": format_location(
            text("./primaryIssuer/issuerAddress/city"),
            state if state in US_STATES else description.title(),
        ),
        "industry": text("./offeringData/industryGroup/industryGroupType"),
        "first_sale": text("./offeringData/typeOfFiling/dateOfFirstSale/value"),
        "amendment": text("./offeringData/typeOfFiling/newOrAmendment/isAmendment") == "true",
        "total_offering": _money(text("./offeringData/offeringSalesAmounts/totalOfferingAmount")),
        "total_sold": _money(text("./offeringData/offeringSalesAmounts/totalAmountSold")),
        "related_people": people[:10],
    }


def _fetch(fetcher: SafeFetcher, url: str) -> str:
    result = fetcher.fetch(url, same_host_only=True)
    if result.error or result.status != 200:
        raise SecUnavailableError(f"EDGAR returned {result.error or f'HTTP {result.status}'} for {url}")
    return result.text


def form_d_lookup(company: str, *, fetcher: SafeFetcher, today: date, pause: float = SEC_REQUEST_GAP) -> dict[str, Any]:
    """The latest Form D filed by an issuer with exactly the target's name.

    Status is "found", "none", or "ambiguous" (more than one issuer has the name).
    """
    key = company_key(company)
    checked = {"checked_at": today.isoformat()}
    if not key:
        return {**checked, "status": "none"}
    url = f"{EDGAR_SEARCH}?q={quote(chr(34) + _search_name(company) + chr(34))}&forms=D"
    try:
        hits = json.loads(_fetch(fetcher, url))["hits"]["hits"]
    except (ValueError, KeyError, TypeError) as exc:
        raise SecUnavailableError("EDGAR search returned an unreadable answer") from exc
    filings = []
    for hit in hits:
        source = hit.get("_source") or {}
        names = source.get("display_names") or []
        ciks = source.get("ciks") or []
        places = source.get("biz_locations") or []
        for index, (display, cik) in enumerate(zip(names, ciks)):
            issuer = re.sub(r"\s*\(CIK \d+\)\s*$", "", str(display)).strip()
            if company_key(issuer) != key:
                continue
            filings.append({
                "issuer": issuer,
                "cik": str(cik),
                "adsh": str(source.get("adsh") or ""),
                "document": str(hit.get("_id") or "").partition(":")[2] or "primary_doc.xml",
                "filed_at": str(source.get("file_date") or ""),
                "search_location": str(places[index]) if index < len(places) else "",
            })
    issuers = {filing["cik"]: filing for filing in filings}
    if not filings:
        return {**checked, "status": "none"}
    if len(issuers) > 1:
        return {
            **checked, "status": "ambiguous",
            "issuers": [{"issuer": item["issuer"], "cik": item["cik"], "location": item["search_location"]} for item in issuers.values()][:5],
        }
    latest = max(filings, key=lambda filing: filing["filed_at"])
    folder = f"{EDGAR_ARCHIVES}/{int(latest['cik'])}/{latest['adsh'].replace('-', '')}"
    record = {
        **checked,
        "status": "found",
        "issuer": latest["issuer"],
        "cik": latest["cik"],
        "filed_at": latest["filed_at"],
        "filings": len({filing["adsh"] for filing in filings}),
        "location": format_location(*(latest["search_location"].split(",", 1) + [""])[:2]),
        "url": f"{folder}/{latest['adsh']}-index.htm",
    }
    if pause:
        time.sleep(pause)
    try:
        details = _filing_details(_fetch(fetcher, f"{folder}/{latest['document']}"))
    except (SecUnavailableError, ElementTree.ParseError):
        return record  # The search hit alone still names the place and the date.
    record.update({key: value for key, value in details.items() if value not in ("", None, [])})
    record["issuer"] = details["issuer"] or latest["issuer"]
    return record


def record_form_d(conn: sqlite3.Connection, target_id: str, *, user_id: str, form_d: dict[str, Any], today: date) -> dict[str, Any]:
    """Store a lookup and let a recent, uncontested filing set or confirm the location."""
    target = get_target(conn, target_id, user_id=user_id)
    result = dict(form_d)
    outcome = "stored"
    if form_d.get("status") == "found" and form_d.get("location"):
        recent = form_d.get("filed_at", "") >= (today - FORM_D_LOCATION_MAX_AGE).isoformat()
        current = target["location"]
        if current and not same_place(current, form_d["location"]):
            # Same name, different place: likely another company, so nothing moves.
            result["status"] = "mismatch"
            result["mismatch_with"] = current
            outcome = "mismatch"
        elif recent:
            outcome = apply_location(
                conn, target_id, user_id=user_id, location=form_d["location"], basis="sec_form_d", source_url=form_d["url"],
            )
    with conn:
        conn.execute(
            "UPDATE outreach_targets SET sec_form_d_json=?, updated_at=? WHERE id=? AND user_id=?",
            (json.dumps(result, ensure_ascii=False), utc_now(), target_id, user_id),
        )
        if result["status"] in {"found", "mismatch", "ambiguous"} and (target.get("sec_form_d") or {}).get("url") != result.get("url"):
            _log(conn, target_id, user_id, "sec_form_d", detail=_form_d_summary(result))
    return {"status": result["status"], "outcome": outcome, "location": result.get("location", ""), "url": result.get("url", "")}


def _dollars(value: Any) -> str:
    if not isinstance(value, int):
        return str(value or "").lower()
    if value >= 1_000_000:
        return f"${value / 1_000_000:,.1f}M"
    if value >= 1_000:
        return f"${value / 1_000:,.0f}K"
    return f"${value:,}"


def _form_d_summary(record: dict[str, Any]) -> str:
    if record["status"] == "ambiguous":
        return "Several SEC issuers have this name: " + "; ".join(
            f"{item['issuer']} ({item['location'] or 'no location'})" for item in record.get("issuers", [])
        )
    sold = record.get("total_sold")
    raised = f", {_dollars(sold)} sold" if sold not in (None, "") else ""
    note = f"; its address ({record.get('location')}) differs from {record.get('mismatch_with')}, so it may be another company" if record["status"] == "mismatch" else ""
    return f"Form D filed {record.get('filed_at')} by {record.get('issuer')}{raised} ({record.get('url')}){note}"


# ---------------------------------------------------------------------------
# Checking one target, and backfilling many


def enrich_target(
    conn: sqlite3.Connection,
    target_id: str,
    *,
    user_id: str,
    site_fetcher: SafeFetcher | None,
    form_d_fetcher: SafeFetcher | None,
    today: date,
    delay: float = 1.0,
    sec_pause: float = SEC_REQUEST_GAP,
    renderer: PlaywrightRenderer | None = None,
) -> dict[str, Any]:
    """Check the company's site (unless a better basis holds the location) and EDGAR."""
    target = get_target(conn, target_id, user_id=user_id)
    outcome: dict[str, Any] = {
        "target_id": target_id, "company": target["company"], "location_before": target["location"],
        "site": None, "form_d": None, "errors": [],
    }
    settled = target.get("location_basis") == "manual" or (
        target.get("location_basis") == "company_site" and not target.get("location_inferred")
    )
    if site_fetcher is not None and target["website"] and not settled:
        try:
            crawl = crawl_site(
                target["website"], fetcher=site_fetcher, delay=delay, keywords=LOCATION_PAGE_KEYWORDS, max_pages=LOCATION_MAX_PAGES,
            )
            plain = record_site_location(conn, target_id, user_id=user_id, pages=crawl["pages"])
            outcome["site"] = render_site_location(
                conn, target_id, user_id=user_id, plain=plain, renderer=renderer, fetcher=site_fetcher,
            )
        except (ValueError, httpx.HTTPError) as exc:
            outcome["errors"].append(f"site: {exc}")
    if form_d_fetcher is not None:
        try:
            found = form_d_lookup(target["company"], fetcher=form_d_fetcher, today=today, pause=sec_pause)
            outcome["form_d"] = record_form_d(conn, target_id, user_id=user_id, form_d=found, today=today)
        except (SecUnavailableError, httpx.HTTPError) as exc:
            outcome["errors"].append(f"sec: {exc}")
    with conn:
        conn.execute(
            "UPDATE outreach_targets SET profile_checked_at=? WHERE id=? AND user_id=?",
            (today.isoformat(), target_id, user_id),
        )
    outcome["location"] = get_target(conn, target_id, user_id=user_id)["location"]
    return outcome


def enrich_targets(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    site_fetcher: SafeFetcher | None,
    form_d_fetcher: SafeFetcher | None,
    only_missing: bool = True,
    force: bool = False,
    limit: int | None = None,
    delay: float = 1.0,
    sec_pause: float = SEC_REQUEST_GAP,
    renderer: PlaywrightRenderer | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Backfill location and Form D for targets not checked in the last 30 days.

    With only_missing, a target is skipped once it has a sourced location and a
    Form D lookup on record. force ignores the 30 days.
    """
    today = today or local_today(conn, user_id)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, location, location_basis, location_inferred, sec_form_d_json, profile_checked_at FROM outreach_targets
        WHERE user_id=? ORDER BY CASE WHEN profile_checked_at IS NULL THEN 0 ELSE 1 END, profile_checked_at, created_at DESC
        """,
        (user_id,),
    ).fetchall()
    cutoff = (today - RECHECK_AFTER).isoformat()
    due = []
    for row in rows:
        if not force and row["profile_checked_at"] and row["profile_checked_at"] > cutoff:
            continue
        sourced = row["location"] and row["location_basis"] in {"manual", "company_site", "sec_form_d"} and not row["location_inferred"]
        if only_missing and sourced and (row["sec_form_d_json"] or form_d_fetcher is None):
            continue
        due.append(row["id"])
    if limit is not None:
        due = due[:max(0, limit)]
    missing_before = sum(1 for row in rows if not row["location"])
    results = [
        enrich_target(
            conn, target_id, user_id=user_id, site_fetcher=site_fetcher, form_d_fetcher=form_d_fetcher,
            today=today, delay=delay, sec_pause=sec_pause, renderer=renderer,
        )
        for target_id in due
    ]
    missing_after = conn.execute(
        "SELECT COUNT(*) FROM outreach_targets WHERE user_id=? AND location=''", (user_id,),
    ).fetchone()[0]
    return {
        "targets": len(rows),
        "checked": len(results),
        "missing_location_before": missing_before,
        "missing_location_after": missing_after,
        "sec_lookups": form_d_fetcher is not None,
        "results": results,
    }
