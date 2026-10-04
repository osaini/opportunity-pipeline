"""The job-board adapters (Greenhouse, Lever, Ashby, SmartRecruiters, Workday, USAJOBS, Adzuna).

Each adapter turns one configured source into a ``Listing`` of records. ``_SOURCE_FETCHERS`` looks the adapters up
late, through lambdas, so a test that patches one adapter on this module is the one the fetch uses.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Iterable

from .http import _http_json, request_json, request_json_post
from .identity import normalized
from .text import strip_html


class Listing(list):
    """The records one source kept, plus what its fetch proved about the listing.

    `listed` counts every posting the source returned before the discovery
    filter, and `complete` is False when a page cap cut the listing short. A
    plain list carries neither, so `upsert_jobs` keeps retiring absent rows
    unconditionally for the callers that pass one (CSV, email, agent imports).
    Board fetches return a Listing, and absence from one retires a posting only
    when the fetch proved it read the whole board -- see `_retirable`.
    """

    def __init__(self, records: Iterable[dict[str, Any]] = (), *, listed: int, complete: bool = True):
        super().__init__(records)
        self.listed = listed
        self.complete = complete


# Discovery terms are stems, so a term has to match a whole word or a word
# carrying one of these suffixes -- and nothing else. Plain substring matching
# reads "intern" inside "Internal Medicine", which is invisible on an employer
# board but swamps USAJOBS: the VA alone posts hundreds of internal-medicine
# physician roles, and they outnumbered the real engineering hits there.
_DISCOVERY_SUFFIXES = "(?:s|es|ship|ships)?"


def is_discovery_candidate(title: str, terms: Iterable[str]) -> bool:
    haystack = normalized(title)
    for term in terms:
        needle = normalized(term)
        if needle and re.search(rf"\b{re.escape(needle)}{_DISCOVERY_SUFFIXES}\b", haystack):
            return True
    return False


def greenhouse_jobs(source: dict[str, Any], discovery_terms: list[str]) -> Listing:
    token = source["token"]
    base = f"https://boards-api.greenhouse.io/v1/boards/{urllib.parse.quote(token)}"
    # `content=true` returns every job's description in the listing itself.
    # Without it the board has to be asked once more per candidate, which on
    # 2026-09-20 was 262 extra requests to one host -- Rocket Lab alone went
    # from 94 requests (17.5s) to 1 (0.4s). Checked against the live API on 13
    # boards before switching: all 257 resulting records were field-for-field
    # identical to the per-job calls. The listing is larger (SpaceX's is ~3 MB
    # compressed, because it describes all 2,500 jobs, not just the dozen
    # kept), which is the trade: far fewer requests for more bytes.
    listing = request_json(f"{base}/jobs?content=true")
    candidates = [job for job in listing.get("jobs", []) if is_discovery_candidate(job.get("title", ""), discovery_terms)]
    jobs: list[dict[str, Any]] = []
    for item in candidates:
        # A job that came back without a description is fetched individually
        # rather than stored empty: scoring reads the description, so a silent
        # change to the listing would otherwise quietly degrade every score.
        detail = item if "content" in item else request_json(f"{base}/jobs/{item['id']}")
        jobs.append(
            {
                "external_id": str(item["id"]),
                "company": source["company"],
                "title": detail.get("title") or item.get("title", ""),
                "location": (detail.get("location") or item.get("location") or {}).get("name", ""),
                "url": detail.get("absolute_url") or item.get("absolute_url", ""),
                "description": strip_html(detail.get("content", "")),
                "posted_at": detail.get("updated_at") or item.get("updated_at"),
            }
        )
    return Listing(jobs, listed=len(listing.get("jobs", [])))


def lever_jobs(source: dict[str, Any], discovery_terms: list[str]) -> Listing:
    site = source["site"]
    region = source.get("region", "global")
    host = "api.eu.lever.co" if region == "eu" else "api.lever.co"
    listing = request_json(f"https://{host}/v0/postings/{urllib.parse.quote(site)}?mode=json")
    jobs: list[dict[str, Any]] = []
    for item in listing:
        if not is_discovery_candidate(item.get("text", ""), discovery_terms):
            continue
        categories = item.get("categories") or {}
        # `lists` holds the bulleted sections ("What you'll do", "What we require"); the plain description is only
        # the opening paragraph, so without them a years or sponsorship requirement never reaches the score.
        sections = item.get("lists")
        lists = [
            strip_html(f"{entry.get('text') or ''} {entry.get('content') or ''}")
            for entry in (sections if isinstance(sections, list) else [])
            if isinstance(entry, dict)
        ]
        description = " ".join(
            part
            for part in [
                strip_html(item.get("descriptionPlain") or item.get("description", "")),
                *lists,
                strip_html(item.get("additionalPlain") or item.get("additional", "")),
            ]
            if part
        )
        jobs.append(
            {
                "external_id": str(item["id"]),
                "company": source["company"],
                "title": item.get("text", ""),
                "location": categories.get("location", ""),
                "url": item.get("hostedUrl") or item.get("applyUrl", ""),
                "description": description,
                "posted_at": _epoch_milliseconds_to_iso(item.get("createdAt")),
            }
        )
    return Listing(jobs, listed=len(listing))


def _epoch_milliseconds_to_iso(value: Any) -> str | None:
    """A UTC ISO time for a positive millisecond epoch (Lever's `createdAt`), None for anything else.

    A posting date the source did not give stays unknown: 0, a negative number, a bool, text, NaN or a value past
    year 9999 are not dates.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not value > 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def ashby_jobs(source: dict[str, Any], discovery_terms: list[str]) -> Listing:
    board = source["board"]
    listing = request_json(
        f"https://api.ashbyhq.com/posting-api/job-board/{urllib.parse.quote(board)}?includeCompensation=true"
    )
    jobs: list[dict[str, Any]] = []
    for item in listing.get("jobs", []):
        title = item.get("title", "")
        if not is_discovery_candidate(title, discovery_terms):
            continue
        # isListed false is a posting the company keeps off its public board (a draft, an internal or a closed req).
        if item.get("isListed") is False:
            continue
        description = strip_html(item.get("descriptionHtml", ""))
        pay = _ashby_pay_sentence(item.get("compensation"))
        jobs.append(
            {
                "external_id": str(item["id"]),
                "company": source["company"],
                "title": title,
                "location": item.get("location", ""),
                "url": item.get("jobUrl") or item.get("applyUrl", ""),
                "description": f"{description} {pay}".strip() if pay else description,
                "posted_at": item.get("publishedAt"),
            }
        )
    return Listing(jobs, listed=len(listing.get("jobs", [])))


_ASHBY_PAY_PERIODS = {"1 YEAR": "year", "1 HOUR": "hour"}


def _ashby_pay_sentence(compensation: Any) -> str:
    """A sentence in the form the pay reader knows, from Ashby's structured salary, or "" when it states none.

    Ashby gives the amount, the period and the currency as fields, which the description often lacks (the pay
    reader needs "per year" or "per hour" beside the figure). Only a USD salary paid by the year or the hour is
    written out: the reader names any pay it finds dollars, and a period it does not know would be a guess.
    Equity, bonus and commission are not pay.
    """
    if not isinstance(compensation, dict):
        return ""
    components = compensation.get("summaryComponents")
    for component in components if isinstance(components, list) else []:
        if not isinstance(component, dict) or component.get("compensationType") != "Salary":
            continue
        interval = component.get("interval")
        period = _ASHBY_PAY_PERIODS.get(interval) if isinstance(interval, str) else None
        low, high = component.get("minValue"), component.get("maxValue")
        if period is None or component.get("currencyCode") != "USD":
            continue
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not v > 0 for v in (low, high)):
            continue
        low, high = sorted((low, high))
        amount = _pay_amount if period == "hour" else _pay_dollars
        text = amount(low) if low == high else f"{amount(low)} - {amount(high)}"
        return f"Pay listed on the Ashby posting: {text} per {period}."
    return ""


def _pay_dollars(value: float) -> str:
    return f"${round(value):,}"


def _pay_amount(value: float) -> str:
    return "$" + f"{value:.2f}".rstrip("0").rstrip(".")


def smartrecruiters_jobs(source: dict[str, Any], discovery_terms: list[str]) -> Listing:
    company_id = source["company_id"]
    base = f"https://api.smartrecruiters.com/v1/companies/{urllib.parse.quote(company_id)}/postings"
    candidates: list[dict[str, Any]] = []
    offset = 0
    complete = True
    for _ in range(20):  # safety cap: 20 pages * 100 = 2000 postings max
        listing = request_json(f"{base}?limit=100&offset={offset}")
        content = listing.get("content", [])
        if not content:
            break
        candidates.extend(content)
        offset += len(content)
        if offset >= listing.get("totalFound", 0):
            break
    else:
        # The cap cut the board short, so a posting past it only looks absent.
        complete = False
    jobs: list[dict[str, Any]] = []
    for item in candidates:
        title = item.get("name", "")
        if not is_discovery_candidate(title, discovery_terms):
            continue
        detail = request_json(f"{base}/{item['id']}")
        location = item.get("location") or {}
        location_str = ", ".join(
            filter(None, [location.get("city"), location.get("region"), location.get("country")])
        )
        description_html = (
            ((detail.get("jobAd") or {}).get("sections") or {}).get("jobDescription") or {}
        ).get("text", "")
        jobs.append(
            {
                "external_id": str(item["id"]),
                "company": source["company"],
                "title": title,
                "location": location_str,
                "url": item.get("postingUrl") or item.get("ref", ""),
                "description": strip_html(description_html),
                "posted_at": item.get("releasedDate"),
            }
        )
    return Listing(jobs, listed=len(candidates), complete=complete)


# Pages read per discovery term. A Workday search is full-text, so "intern"
# matches every posting whose description mentions interns and "co-op" matched
# 2,000+ at several tenants (2026-09-27). Results are mostly relevance-ordered
# -- NVIDIA's "intern" search held 109 intern titles across its first six
# pages, then none -- so a term stops after this many pages in a row without a
# discovery match, and in any case at the cap. Either way the listing is
# incomplete, and absence from it retires only a posting unseen for the grace.
WORKDAY_MAX_PAGES_PER_TERM = 10
WORKDAY_MISS_PAGES = 2


def workday_jobs(source: dict[str, Any], discovery_terms: list[str]) -> Listing:
    # Workday tenants can list thousands of postings with no relevance/date
    # ordering guarantee, so a blank/paginated listing call can bury intern
    # roles far past any sane page cap. Workday's own searchText performs a
    # real server-side full-text search, so we search once per discovery
    # term instead and de-dupe results across terms.
    tenant, datacenter, site = source["tenant"], source["datacenter"], source["site"]
    base = f"https://{tenant}.{datacenter}.myworkdayjobs.com"
    endpoint = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    jobs: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    limit = 20
    listed = 0
    complete = True
    for term in discovery_terms:
        offset = 0
        total = 0
        misses = 0
        for _ in range(WORKDAY_MAX_PAGES_PER_TERM):
            data = request_json_post(
                endpoint, {"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": term}
            )
            # Workday reports `total` on the first page only and 0 on every later
            # page (seen on NVIDIA, GDIT and NXP, 2026-09-27). Reading it per page
            # ended every search after 20 results.
            if offset == 0:
                total = int(data.get("total") or 0)
            postings = data.get("jobPostings", [])
            if not postings:
                break
            listed += len(postings)
            matched = False
            for item in postings:
                external_path = item.get("externalPath", "")
                title = item.get("title", "")
                if not is_discovery_candidate(title, discovery_terms):
                    continue
                matched = True
                if external_path in seen_paths:
                    continue
                seen_paths.add(external_path)
                bullets = " ".join(item.get("bulletFields") or [])
                posted_text = item.get("postedOn", "")
                jobs.append(
                    {
                        "external_id": external_path or title,
                        "company": source["company"],
                        "title": title,
                        "location": item.get("locationsText", ""),
                        "url": f"{base}/en-US/{site}{external_path}",
                        "description": strip_html(f"{posted_text} {bullets}".strip()),
                        # Workday's own postedOn is relative text ("Posted Today"),
                        # not a parseable date, so we leave posted_at unset rather
                        # than fabricate a timestamp.
                        "posted_at": None,
                    }
                )
            offset += limit
            if offset >= total:
                break
            misses = 0 if matched else misses + 1
            if misses >= WORKDAY_MISS_PAGES:
                complete = False
                break
        else:
            complete = False
    return Listing(jobs, listed=listed, complete=complete)


# Documented USAJOBS search limits: 500 rows per page, 10,000 rows per query.
# https://developer.usajobs.gov/guides/rate-limiting
USAJOBS_RESULTS_PER_PAGE = 500
USAJOBS_MAX_ROWS_PER_QUERY = 10_000
# A nationwide federal announcement can list dozens of duty stations. The full
# list is worth keeping for region matching, but not at unbounded width in the
# shortlist, so it is trimmed to the first few plus a count.
USAJOBS_MAX_LOCATIONS = 6


def _usajobs_search(headers: dict[str, str], params: dict[str, str]) -> Iterable[dict[str, Any]]:
    """Yield every result item for one query, walking all pages.

    A single unpaged request returns only the first slice of what is usually a
    multi-thousand-row federal result set, so page 1 alone silently drops most
    matches. USAJOBS reports its page count in
    `SearchResult.UserArea.NumberOfPages`; the walk also stops at the documented
    10,000-row query ceiling and on an empty page, so a missing or wrong count
    cannot turn into an unbounded request loop.
    """
    page = 1
    fetched = 0
    while True:
        query = dict(params, ResultsPerPage=str(USAJOBS_RESULTS_PER_PAGE), Page=str(page))
        data = _http_json(
            f"https://data.usajobs.gov/api/search?{urllib.parse.urlencode(query)}",
            headers=headers,
        )
        result = data.get("SearchResult", {})
        items = result.get("SearchResultItems", [])
        yield from items
        fetched += len(items)
        try:
            total_pages = int(result.get("UserArea", {}).get("NumberOfPages", 1) or 1)
        except (TypeError, ValueError):
            total_pages = 1
        if not items or page >= total_pages or fetched >= USAJOBS_MAX_ROWS_PER_QUERY:
            return
        page += 1


def _usajobs_text(value: Any) -> str:
    """Flatten one UserArea.Details field to text.

    These fields are not consistently typed: the API documents MajorDuties as a
    string but returns a list of strings, and some detail fields arrive as
    `{"Content": ...}` objects. Coercing here keeps a shape change from raising
    mid-fetch and losing the whole source.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return _usajobs_text(value.get("Content", ""))
    if isinstance(value, list):
        return " ".join(part for part in (_usajobs_text(item) for item in value) if part)
    return ""


def _usajobs_description(descriptor: dict[str, Any]) -> str:
    """Best available posting text, richest field first.

    `Fields=Full` adds a UserArea.Details block whose summary and duties carry
    far more scoring signal than the qualification blurb returned by default.
    Falling through the list keeps a `Fields=Min` source usable.
    """
    details = descriptor.get("UserArea", {}).get("Details", {})
    parts = [
        _usajobs_text(details.get("JobSummary")),
        _usajobs_text(details.get("MajorDuties")),
        _usajobs_text(details.get("Education")),
        _usajobs_text(descriptor.get("QualificationSummary")),
    ]
    return strip_html(" ".join(part for part in parts if part))


def _usajobs_location(descriptor: dict[str, Any]) -> str:
    names = [
        (entry.get("LocationName") or "").strip()
        for entry in descriptor.get("PositionLocation", [])
    ]
    unique = list(dict.fromkeys(name for name in names if name))
    if not unique:
        return (descriptor.get("PositionLocationDisplay") or "").strip()
    if len(unique) > USAJOBS_MAX_LOCATIONS:
        return f"{'; '.join(unique[:USAJOBS_MAX_LOCATIONS])} (+{len(unique) - USAJOBS_MAX_LOCATIONS} more)"
    return "; ".join(unique)


def usajobs_jobs(source: dict[str, Any], discovery_terms: list[str]) -> Listing:
    api_key = os.environ.get("USAJOBS_API_KEY")
    if not api_key:
        raise ValueError(
            "USAJOBS_API_KEY not set. Register a free key at https://developer.usajobs.gov/ "
            "and put it in .env or export it (see docs/guide/sources.md#api-keys)."
        )
    contact_email = source.get("contact_email") or os.environ.get("USAJOBS_CONTACT_EMAIL", "")
    if not contact_email:
        raise ValueError(
            "USAJOBS requires the email the key was registered with "
            "(source.contact_email, or USAJOBS_CONTACT_EMAIL in .env)."
        )
    headers = {"Host": "data.usajobs.gov", "User-Agent": contact_email, "Authorization-Key": api_key}
    default_fields = source.get("fields", "Full")
    jobs: dict[str, dict[str, Any]] = {}
    listed = 0

    # Each query stands alone rather than sharing one filter set, because the
    # API ANDs its filters and some combinations annihilate each other:
    # Keyword=mechanical engineering with HiringPath=student returned 4 rows
    # against 255 for the keyword alone (measured 2026-07-26). The useful shapes
    # are therefore a hiring-path sweep with no keyword and a keyword sweep with
    # no hiring path. A source with no `queries` is itself a single query.
    for spec in source.get("queries") or [source]:
        params = {"Fields": spec.get("fields", default_fields)}
        # Semicolon is the API's documented multi-value separator; urlencode
        # escapes it. Every filter is optional.
        for param, option in (
            ("HiringPath", "hiring_paths"),
            ("JobCategoryCode", "job_category_codes"),
            ("LocationName", "location_names"),
            ("Organization", "organizations"),
        ):
            values = [str(value).strip() for value in spec.get(option, []) if str(value).strip()]
            if values:
                params[param] = ";".join(values)
        if spec.get("radius"):
            params["Radius"] = str(spec["radius"])
        if spec.get("posted_within_days"):
            params["DatePosted"] = str(spec["posted_within_days"])

        keywords = spec.get("keywords") or ([spec["keyword"]] if spec.get("keyword") else [])
        # No keyword means one unkeyworded request, which is how a hiring-path
        # sweep reaches postings whose titles never say "intern".
        for keyword in keywords or [None]:
            query = dict(params, Keyword=str(keyword)) if keyword else dict(params)
            listed += _usajobs_collect(headers, query, source, discovery_terms, jobs)
    return Listing(jobs.values(), listed=listed)


def _usajobs_collect(
    headers: dict[str, str],
    query: dict[str, str],
    source: dict[str, Any],
    discovery_terms: list[str],
    jobs: dict[str, dict[str, Any]],
) -> int:
    """Normalize one query's results into `jobs`, keyed by announcement id.

    Queries overlap by design, so the shared dict is what keeps an announcement
    matched by several of them from being stored several times. Returns how
    many results the query answered with, before any filtering.
    """
    answered = 0
    for item in _usajobs_search(headers, query):
        answered += 1
        descriptor = item.get("MatchedObjectDescriptor", {})
        title = descriptor.get("PositionTitle", "")
        if not is_discovery_candidate(title, discovery_terms):
            continue
        external_id = str(item.get("MatchedObjectId", "")) or descriptor.get("PositionID", "")
        if not external_id or external_id in jobs:
            continue
        jobs[external_id] = {
            "external_id": external_id,
            "company": descriptor.get("OrganizationName")
            or descriptor.get("DepartmentName")
            or source.get("company", "USAJOBS"),
            "title": title,
            "location": _usajobs_location(descriptor),
            "url": descriptor.get("PositionURI", ""),
            "description": _usajobs_description(descriptor),
            "posted_at": descriptor.get("PublicationStartDate"),
        }
    return answered


# Adzuna aggregates postings from employers this pipeline has no direct feed
# for: small manufacturers, machine shops, staffing firms, and companies on an
# ATS with no public API. It is the only configured source with a real
# radius-based location filter, which is what makes it useful for "within N km
# of Austin" rather than "matches a city name we happened to list".
#
# Two limits shape the adapter. The free tier is rate limited (documented at 25
# calls/minute), so page depth is capped per query. And search results carry a
# truncated description snippet, not the full posting -- Adzuna rows therefore
# score on title and location much like job-alert emails, and are good
# candidates for `enrich`.
ADZUNA_MAX_PAGES = 5
ADZUNA_RESULTS_PER_PAGE = 50


def adzuna_jobs(source: dict[str, Any], discovery_terms: list[str]) -> Listing:
    app_id = os.environ.get("ADZUNA_APP_ID")
    app_key = os.environ.get("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        raise ValueError(
            "ADZUNA_APP_ID/ADZUNA_APP_KEY not set. Register a free application at "
            "https://developer.adzuna.com/ and put both in .env (see docs/guide/sources.md#api-keys)."
        )
    country = source.get("country", "us")
    jobs: dict[str, dict[str, Any]] = {}
    listed = 0
    complete = True
    # Queries overlap on purpose -- "mechanical intern" near Austin and
    # "manufacturing co-op" near Austin return an intersecting set -- so results
    # are keyed by Adzuna's id and the first sighting wins.
    for spec in source.get("queries") or [source]:
        params = {
            "app_id": app_id,
            "app_key": app_key,
            "results_per_page": str(ADZUNA_RESULTS_PER_PAGE),
            "content-type": "application/json",
        }
        for param, option in (
            ("what", "what"),
            ("what_phrase", "what_phrase"),
            ("what_exclude", "what_exclude"),
            ("where", "where"),
        ):
            value = str(spec.get(option, "") or "").strip()
            if value:
                params[param] = value
        # `distance` is kilometres in Adzuna's API regardless of country, and is
        # ignored unless `where` is also set.
        if spec.get("distance_km"):
            params["distance"] = str(spec["distance_km"])
        if spec.get("posted_within_days"):
            params["max_days_old"] = str(spec["posted_within_days"])
        params["sort_by"] = spec.get("sort_by", "date")

        for page in range(1, int(spec.get("max_pages", ADZUNA_MAX_PAGES)) + 1):
            data = request_json(
                f"https://api.adzuna.com/v1/api/jobs/{urllib.parse.quote(country)}"
                f"/search/{page}?{urllib.parse.urlencode(params)}"
            )
            results = data.get("results", [])
            if not results:
                break
            listed += len(results)
            for item in results:
                title = item.get("title", "")
                if not is_discovery_candidate(title, discovery_terms):
                    continue
                external_id = str(item.get("id", ""))
                if not external_id or external_id in jobs:
                    continue
                location = item.get("location") or {}
                jobs[external_id] = {
                    "external_id": external_id,
                    # Adzuna redacts the employer on some listings, in which
                    # case there is genuinely no company to record.
                    "company": (item.get("company") or {}).get("display_name")
                    or source.get("company", "Adzuna"),
                    "title": strip_html(title),
                    "location": location.get("display_name", ""),
                    "url": item.get("redirect_url", ""),
                    "description": strip_html(item.get("description", "")),
                    "posted_at": item.get("created"),
                }
            if len(results) < ADZUNA_RESULTS_PER_PAGE:
                break
        else:
            # The page cap ended this query while results were still coming.
            complete = False
    return Listing(jobs.values(), listed=listed, complete=complete)


_SOURCE_FETCHERS = {
    "greenhouse": lambda source, terms: greenhouse_jobs(source, terms),
    "lever": lambda source, terms: lever_jobs(source, terms),
    "ashby": lambda source, terms: ashby_jobs(source, terms),
    "smartrecruiters": lambda source, terms: smartrecruiters_jobs(source, terms),
    "workday": lambda source, terms: workday_jobs(source, terms),
    "usajobs": lambda source, terms: usajobs_jobs(source, terms),
    "adzuna": lambda source, terms: adzuna_jobs(source, terms),
}


def _fetch_source(source: dict[str, Any], terms: list[str]) -> Listing:
    """Fetch one source. Runs on a worker thread and touches no database."""

    kind = source["kind"]
    fetcher = _SOURCE_FETCHERS.get(kind)
    if fetcher is None:
        raise ValueError(f"Unsupported source kind: {kind}")
    return fetcher(source, terms)
