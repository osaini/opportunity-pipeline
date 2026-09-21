"""Personal addresses printed on pages other than the company's own site.

A founder's address often appears somewhere the company site does not: the
media contact on a press release, the corresponding author of a paper, a
university lab page, a patent, a conference speaker page, a GitHub profile.

A model does the searching, so nothing it says is taken on its word. Python
opens each page the model cites and keeps the address only when:

- the page is not a people-search or email-finder site (those publish guesses
  and scraped personal data, not addresses anyone published),
- the site's robots.txt lets this tool read it,
- the address is on the company's own mail domain and is not a shared inbox,
- that exact address is printed on the page (a masked "j***@acme.com" is not),
- and the person's name is printed on the page too.

An address that passes is recorded as "published_elsewhere", unverified: the
company did not publish it. If the page turns out to be on the company's own
site, it is recorded as site_published and confirmed, as the crawl would have.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Callable
from urllib.parse import urlsplit

from .agent_providers import CliAgentProvider
from .outreach import _log, website_domain
from .outreach_contacts import (
    EMAIL_PATTERN,
    SafeFetcher,
    _email_on_domain,
    _emails_from_page,
    _is_generic,
    _PageParser,
    _same_site,
    list_candidates,
    public_web_url_error,
    site_robots,
    store_candidate,
    USER_AGENT,
)
from .schema import utc_now

Runner = Callable[[str], str]

BATCH_SIZE = 6
MAX_PEOPLE_PER_COMPANY = 4
# Sites that sell or scrape contact data. Their "emails" are guesses or
# personal records, never an address the person published.
BLOCKED_HOSTS = (
    "rocketreach.co", "zoominfo.com", "apollo.io", "hunter.io", "lusha.com", "signalhire.com",
    "contactout.com", "clearbit.com", "snov.io", "voilanorbert.com", "anymailfinder.com", "leadiq.com",
    "seamless.ai", "adapt.io", "uplead.com", "kaspr.io", "getprospect.com", "findthatlead.com",
    "skrapp.io", "salesql.com", "rocketreach.com", "datanyze.com", "lead411.com", "aeroleads.com",
    "emailhippo.com", "email-format.com", "emailformat.com", "leadfuze.com", "swordfish.ai",
    "fastpeoplesearch.com", "truepeoplesearch.com", "spokeo.com", "whitepages.com", "beenverified.com",
    "peoplefinders.com", "radaris.com", "intelius.com", "instantcheckmate.com", "thatsthem.com",
    "peekyou.com", "familytreenow.com", "usphonebook.com", "clustrmaps.com", "nuwber.com",
    "idcrawl.com", "cyberbackgroundchecks.com", "peoplelooker.com", "truthfinder.com",
    "crunchbase.com", "pitchbook.com", "cbinsights.com", "signalhire.co", "contactrocket.com",
    "linkedin.com",  # behind a login; reading it breaks its terms
)

PROMPT = """You are helping a university student find the published work email of a specific person at each of these small companies, so the student can write to them about an internship.

## Companies
{companies}

## What to find
For each company, up to {limit} people who work there, preferably a founder, whoever hires, or whoever leads the team closest to the student's field, whose email address on the company's domain is printed on a public page OTHER than the company's own website. Good places: a press release's media contact, the corresponding author of a research paper, a university lab or alumni page, a patent, a conference speaker page, a GitHub profile, a podcast or news article.

## Rules
- Use web search and fetch. Only report an address you saw printed on a page you actually opened in this session. Copy it exactly.
- source_url is that page. Never cite a search results page.
- Never use people-search, data-broker, or email-finder sites (RocketReach, ZoomInfo, Apollo, Hunter, Lusha, ContactOut, FastPeopleSearch, Spokeo, Whitepages, and the like), and never LinkedIn. Their addresses are guesses.
- Never construct or guess an address from a name. A missing answer is better than a guess.
- Skip shared inboxes such as info@, hello@, careers@, press@.
- Answer for every company listed, in the same order. Give an empty people list when you found nothing.

## Output
Reply with exactly one JSON object and nothing else:
{{"companies": [{{"company": "", "people": [{{"name": "", "role": "", "email": "", "source_url": "https://..."}}]}}]}}"""


def build_prompt(targets: list[dict[str, Any]]) -> str:
    lines = [
        f"- {target['company']}" + (f" ({target['website']}, mail domain {website_domain(target['website'])})" if target.get("website") else "")
        for target in targets
    ]
    return PROMPT.format(companies="\n".join(lines), limit=MAX_PEOPLE_PER_COMPANY)


def blocked_host(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return any(host == blocked or host.endswith(f".{blocked}") for blocked in BLOCKED_HOSTS)


def _normalized(text: str) -> str:
    return " ".join(str(text).split()).casefold()


def names(text: str, name: str) -> bool:
    """Whether the text prints this name as whole words: "Ann Lee" is not in "Joann Lee"."""
    return re.search(rf"(?<!\w){re.escape(_normalized(name))}(?!\w)", _normalized(text)) is not None


def hop_guard(fetcher: SafeFetcher) -> Callable[[str], str | None]:
    """Refuse any URL, first or redirected to, on a blocked host or disallowed by its robots.txt."""
    robots_by_origin: dict[str, Any] = {}

    def check(url: str) -> str | None:
        if blocked_host(url):
            return "blocked host"
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}/"
        if origin not in robots_by_origin:
            robots_by_origin[origin] = site_robots(origin, fetcher)
        if not robots_by_origin[origin].can_fetch(USER_AGENT, url):
            return "robots"
        return None

    return check


def check_person(proposal: dict[str, Any], target: dict[str, Any], *, fetcher: SafeFetcher) -> dict[str, Any]:
    """Open the cited page and decide whether it prints this person's address.

    Returns a candidate to store, or {"reason": ...} saying why it was refused.
    """
    name = " ".join(str(proposal.get("name") or "").split())[:200]
    role = " ".join(str(proposal.get("role") or "").split())[:200]
    email = str(proposal.get("email") or "").strip().lower()
    source_url = str(proposal.get("source_url") or "").strip()
    domain = website_domain(target.get("website", ""))
    if not domain:
        return {"reason": "the company has no website to take its mail domain from"}
    if not name or not EMAIL_PATTERN.fullmatch(email):
        return {"reason": "no name or no usable address"}
    if not _email_on_domain(email, domain):
        return {"reason": f"{email} is not on {domain}"}
    if _is_generic(email):
        return {"reason": f"{email} is a shared inbox"}
    if public_web_url_error(source_url):
        return {"reason": "its source is not a public http(s) page"}
    result = fetcher.fetch(source_url, same_host_only=False, hop_check=hop_guard(fetcher))
    if result.error == "blocked host":
        return {"reason": "its source is, or redirects through, a people-search or email-finder site"}
    if result.error == "robots":
        return {"reason": "its source's robots.txt does not allow reading it"}
    if result.error or result.status >= 400:
        return {"reason": f"its source did not load ({result.error or f'HTTP {result.status}'})"}
    kind = result.content_type.lower()
    if kind and "html" not in kind and "text/plain" not in kind:
        return {"reason": f"its source is not a web page ({kind.split(';')[0]})"}
    parser = _PageParser()
    parser.feed(result.text)
    parser.close()
    printed = _emails_from_page(parser)
    if email not in printed:
        return {"reason": f"its source does not print {email}"}
    if not names(" ".join(parser.lines), name):
        return {"reason": f"its source does not name {name}"}
    own_site = _same_site(result.url, domain)
    return {
        "name": name,
        "role": role,
        "email": email,
        "method": "site_published" if own_site else "published_elsewhere",
        "confidence": "confirmed" if own_site else "unverified",
        "evidence_url": result.url,
        "verification": "",
        "pattern_observed": False,
        "note": "" if own_site else f"Printed on {urlsplit(result.url).hostname}",
        "reason": "",
    }


def search_batch(
    conn: sqlite3.Connection,
    targets: list[dict[str, Any]],
    *,
    user_id: str,
    runner: Runner,
    fetcher: SafeFetcher,
    verifier: Any = None,
) -> list[dict[str, Any]]:
    """Research one batch of companies and store every address its page backs up."""
    parsed = CliAgentProvider.extract_json(runner(build_prompt(targets)))
    answers = parsed.get("companies")
    if not isinstance(answers, list):
        raise ValueError("The email search reply had no companies list")
    by_name = {target["company"].casefold(): target for target in targets}
    seen: set[str] = set()
    results: list[dict[str, Any]] = []
    for answer in answers:
        if not isinstance(answer, dict):
            continue
        key = " ".join(str(answer.get("company") or "").split()).casefold()
        target = by_name.get(key)
        if target is None or key in seen:
            continue
        seen.add(key)
        people = answer.get("people") if isinstance(answer.get("people"), list) else []
        checked = [
            {**check_person(person, target, fetcher=fetcher), "proposed": str(person.get("email") or "")[:320]}
            for person in people[:MAX_PEOPLE_PER_COMPANY] if isinstance(person, dict)
        ]
        kept = [item for item in checked if not item["reason"]]
        if verifier is not None and kept:
            verdicts = verifier.check(website_domain(target["website"]), [item["email"] for item in kept if item["confidence"] != "confirmed"])
            for item in kept:
                item["verification"] = verdicts.get(item["email"], "")
        timestamp = utc_now()
        with conn:
            for item in kept:
                store_candidate(conn, target["id"], user_id, item, timestamp)
            _log(conn, target["id"], user_id, "email_search",
                 detail=f"{len(kept)} of {len(checked)} proposed addresses printed on their pages"[:2_000])
        results.append({
            "target_id": target["id"], "company": target["company"],
            "kept": [item["email"] for item in kept],
            "refused": [{"email": item["proposed"], "reason": item["reason"]} for item in checked if item["reason"]],
        })
    for key, target in by_name.items():
        if key not in seen:
            results.append({"target_id": target["id"], "company": target["company"], "kept": [],
                            "refused": [], "error": "the search did not answer for it"})
    return results


def needs_a_person(conn: sqlite3.Connection, target_id: str, *, user_id: str) -> bool:
    """Whether a search could still help: no confirmed personal address and no strong guess."""
    from .outreach_contacts import choose_contact

    choice = choose_contact(list_candidates(conn, target_id, user_id=user_id))
    return choice is None or choice["basis"] not in {"confirmed", "strong_guess"}


def search_emails(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    runner: Runner,
    fetcher: SafeFetcher,
    target_ids: list[str],
    verifier: Any = None,
    batch_size: int = BATCH_SIZE,
) -> dict[str, Any]:
    """Search other sites for the named targets' people. Targets without a website are skipped."""
    conn.row_factory = sqlite3.Row
    targets = []
    for target_id in target_ids:
        row = conn.execute(
            "SELECT id, company, website FROM outreach_targets WHERE id=? AND user_id=?", (target_id, user_id),
        ).fetchone()
        if row is not None and row["website"]:
            targets.append(dict(row))
    batch_size = max(1, int(batch_size))
    results: list[dict[str, Any]] = []
    for start in range(0, len(targets), batch_size):
        results.extend(search_batch(
            conn, targets[start:start + batch_size], user_id=user_id, runner=runner, fetcher=fetcher, verifier=verifier,
        ))
    return {
        "searched": len(targets),
        "found": sum(len(result["kept"]) for result in results),
        "results": results,
    }
