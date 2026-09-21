"""Where a company is based, from a web search, for the ones nothing else places.

The company's own site and its Form D filings settle most locations
(outreach_profile.py). What is left is usually a site that states its city
nowhere and a company too young or too private to have filed: a plain search
finds those in one result, on a YC or accelerator page, a funding announcement,
or a news story.

A model does the searching, so nothing it says is taken on its word. Python
opens the page the model cites and keeps the location only when that page
loads, names the company, and names that city without placing it in another
state. What passes is recorded as basis "web_search" with that page as its
source, which ranks below the company's own site and a filing and above the
deep search's word. A location the student typed is never touched.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Callable

from .agent_providers import CliAgentProvider
from .outreach import PAGE_CHECKED_BASES, website_domain
from .outreach_contacts import SafeFetcher, public_web_url_error
from .outreach_discovery import _mentions_company
from .outreach_profile import _state_code, apply_location, format_location

Runner = Callable[[str], str]

BASIS = "web_search"
# One CLI run researches this many companies. A larger batch is cheaper; a
# smaller one keeps each company's searching separate.
BATCH_SIZE = 8

PROMPT = """You are finding where each of these companies is based. A university student is writing to them and needs to know which city each one is in.

## Companies
{companies}

## Rules
- Use web search and fetch. Only answer from a page you actually opened in this session.
- location is the city and state as the page states it, for example "Cedar Park, TX". Give one place, not a list.
- source_url is the page that states it. Prefer the company's own site, then a funding announcement or filing, then an accelerator, investor, or directory page. Never cite a page you did not open, and never cite a search results page.
- When a company's headquarters and its engineering or manufacturing site are in different cities, give the engineering site and say so in note.
- When no page states where a company is based, give an empty location and say why in note. An empty answer is better than a guess.
- Answer for every company listed, in the same order.

## Output
Reply with exactly one JSON object and nothing else:
{{"companies": [{{"company": "", "location": "City, ST", "source_url": "https://...", "note": ""}}]}}"""


def build_prompt(targets: list[dict[str, Any]]) -> str:
    lines = [
        f"- {target['company']}" + (f" ({target['website']})" if target.get("website") else "")
        for target in targets
    ]
    return PROMPT.format(companies="\n".join(lines))


def clean_place(text: str) -> str:
    """ "cedar park, tx" as "Cedar Park, TX", or "" when it is not a city and state."""
    parts = [part.strip() for part in str(text or "").split(",")]
    if len(parts) < 2 or not parts[0]:
        return ""
    city = parts[0].title() if parts[0].islower() else parts[0]
    return format_location(city, parts[1], parts[2] if len(parts) > 2 else "")


# How far past a city name to read for the state it is in: "Austin, Texas" and
# "San Francisco, CA 94107" both name it within a few words.
STATE_AFTER_CITY = 30


def _state_after(tail: str) -> str:
    """The state named right after a city, as a code or a full name, or ""."""
    code = re.match(r",\s*([A-Z]{2})\b", tail)
    if code:
        return _state_code(code.group(1))
    for words in (2, 1):
        name = re.match(r"[,.]?\s*(" + r"\s+".join([r"[A-Za-z]+"] * words) + r")\b", tail)
        if name and len(name.group(1)) > 2 and _state_code(name.group(1)):
            return _state_code(name.group(1))
    return ""


def _states_the_place(text: str, location: str) -> bool:
    """Whether the page names the city and never puts it in a different state.

    A press release datelined "SAN FRANCISCO" states the city without naming
    California, so a missing state is not a contradiction. "Austin, MN" is.
    """
    page = " ".join(str(text).split())
    city, _, rest = location.partition(",")
    city = " ".join(city.split())
    mentions = list(re.finditer(rf"\b{re.escape(city)}\b", page, re.IGNORECASE)) if city else []
    state = _state_code(rest)
    if not mentions or not state:
        return bool(mentions)
    named = [_state_after(page[mention.end():mention.end() + STATE_AFTER_CITY]) for mention in mentions]
    return any(found == state for found in named) or not any(named)


def check_proposal(
    proposal: dict[str, Any],
    target: dict[str, Any],
    *,
    fetcher: SafeFetcher,
) -> dict[str, Any]:
    """Open the cited page and decide whether it supports the proposed location.

    Returns the location and source URL to record, or a reason it was refused.
    """
    location = clean_place(proposal.get("location"))
    source_url = str(proposal.get("source_url") or "").strip()
    note = " ".join(str(proposal.get("note") or "").split())[:500]
    if not location:
        reason = "no page states where it is based" if not str(proposal.get("location") or "").strip() else (
            f"{str(proposal.get('location'))[:80]!r} is not a city and state"
        )
        return {"location": "", "source_url": "", "note": note, "reason": reason}
    if public_web_url_error(source_url):
        return {"location": "", "source_url": "", "note": note, "reason": "its source is not a public http(s) page"}
    result = fetcher.fetch(source_url, same_host_only=False)
    if result.error or result.status >= 400:
        detail = result.error or f"HTTP {result.status}"
        return {"location": "", "source_url": "", "note": note, "reason": f"its source did not load ({detail})"}
    if not _mentions_company(result.text, target["company"], website_domain(target.get("website", ""))):
        return {"location": "", "source_url": "", "note": note, "reason": "its source does not mention the company"}
    if not _states_the_place(result.text, location):
        return {"location": "", "source_url": "", "note": note, "reason": f"its source does not place it in {location}"}
    return {"location": location, "source_url": result.url, "note": note, "reason": ""}


def locate_batch(
    conn: sqlite3.Connection,
    targets: list[dict[str, Any]],
    *,
    user_id: str,
    runner: Runner,
    fetcher: SafeFetcher,
) -> list[dict[str, Any]]:
    """Research one batch of companies and record every location its source backs up."""
    raw = runner(build_prompt(targets))
    parsed = CliAgentProvider.extract_json(raw)
    proposals = parsed.get("companies")
    if not isinstance(proposals, list):
        raise ValueError("The location search reply had no companies list")
    by_name = {target["company"].casefold(): target for target in targets}
    seen: set[str] = set()
    results = []
    for proposal in proposals:
        if not isinstance(proposal, dict):
            continue
        name = " ".join(str(proposal.get("company") or "").split()).casefold()
        target = by_name.get(name)
        if target is None or name in seen:
            continue
        seen.add(name)
        checked = check_proposal(proposal, target, fetcher=fetcher)
        outcome = "refused" if checked["reason"] else apply_location(
            conn, target["id"], user_id=user_id, location=checked["location"],
            basis=BASIS, source_url=checked["source_url"],
        )
        results.append({
            "target_id": target["id"], "company": target["company"], "location": checked["location"],
            "source_url": checked["source_url"], "note": checked["note"], "reason": checked["reason"],
            "outcome": outcome,
        })
    for name, target in by_name.items():
        if name not in seen:
            results.append({
                "target_id": target["id"], "company": target["company"], "location": "", "source_url": "",
                "note": "", "reason": "the search did not answer for it", "outcome": "refused",
            })
    return results


def needs_a_location(row: dict[str, Any]) -> bool:
    """Whether a search could still help: no location, or one nothing has checked.

    Only the student's own entry and a basis a page established settle the
    question. The deep search's word, an import file's, and any basis this code
    does not recognise are all still open — naming them one by one would let an
    unrecognised basis be skipped forever while counting as unverified.
    """
    if not row["location"]:
        return True
    if row["location_basis"] == "manual":
        return False
    if row["location_inferred"]:
        return True
    return row["location_basis"] not in PAGE_CHECKED_BASES


def locate_targets(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    runner: Runner,
    fetcher: SafeFetcher,
    limit: int | None = None,
    batch_size: int = BATCH_SIZE,
    target_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Search for every target's location that nothing better has settled."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, company, website, location, location_basis, location_inferred FROM outreach_targets "
        "WHERE user_id=? ORDER BY company COLLATE NOCASE",
        (user_id,),
    ).fetchall()
    chosen = set(target_ids) if target_ids is not None else None
    due = [dict(row) for row in rows if dict(row)["id"] in chosen] if chosen is not None else [
        dict(row) for row in rows if needs_a_location(dict(row))
    ]
    if limit is not None:
        due = due[:max(0, limit)]
    batch_size = max(1, int(batch_size))
    results: list[dict[str, Any]] = []
    for start in range(0, len(due), batch_size):
        results.extend(locate_batch(
            conn, due[start:start + batch_size], user_id=user_id, runner=runner, fetcher=fetcher,
        ))
    recorded = sum(1 for result in results if result["outcome"] in {"recorded", "confirmed"})
    return {
        "targets": len(rows),
        "searched": len(due),
        "recorded": recorded,
        "refused": sum(1 for result in results if result["outcome"] == "refused"),
        "missing_location_after": conn.execute(
            "SELECT COUNT(*) FROM outreach_targets WHERE user_id=? AND location=''", (user_id,),
        ).fetchone()[0],
        "results": results,
    }
