"""ATS board discovery: probe the public board APIs for a company name and report what was found."""

from __future__ import annotations

import json
import re
import urllib.parse
from pathlib import Path
from typing import Any, Iterable

from . import paths
from .config import load_json
from .http import request_json
from .identity import identity_tokens
from .paths import display_path
from .sources import is_discovery_candidate


# ---------------------------------------------------------------------------
# ATS board discovery
#
# The approach is from career-ops' `discover-ats.mjs` (MIT, see
# THIRD_PARTY_NOTICES.md): probe the public JSON APIs already supported here,
# and treat a company as resolved only when a board exists AND lists jobs.
#
# The identity check is this project's own requirement, not upstream's. As
# `_source_verification_note` in config/sources.json records, token guessing
# produces convincing impostors -- `greenhouse/archer` is Archer Veterinary
# Clinic, `ashby/sierra` is Sierra AI. A board that returns JSON proves nothing
# about whose board it is, so identity is reported separately from existence and
# only Greenhouse can settle it automatically.
# ---------------------------------------------------------------------------

# Safe charset for a slug interpolated into an ATS URL: a malformed or hostile
# company name can never inject anything unexpected into the request.
DISCOVERY_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def slug_candidates(name: str) -> list[str]:
    """Board slugs worth trying for a company name, most likely first."""
    words = re.sub(r"[^A-Za-z0-9 ]+", " ", name).split()
    if not words:
        return []
    lowered = [word.lower() for word in words]
    candidates = ["".join(lowered), "-".join(lowered)]
    # Ashby boards are case-sensitive and frequently CamelCase (AlephAlpha,
    # DeepL), so the original capitalisation is a distinct candidate.
    candidates.append("".join(word[:1].upper() + word[1:] for word in words))
    if len(lowered) > 1:
        # Last resort: many boards use only the distinctive first word. It is
        # also the likeliest way to land on an unrelated company's board, which
        # is why the slug that matched is always reported.
        candidates.append(lowered[0])
    ordered: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in ordered and DISCOVERY_SLUG_RE.match(candidate):
            ordered.append(candidate)
    return ordered


def _probe_greenhouse(slug: str) -> dict[str, Any] | None:
    quoted = urllib.parse.quote(slug)
    try:
        board = request_json(f"https://boards-api.greenhouse.io/v1/boards/{quoted}", retries=0)
        listing = request_json(
            f"https://boards-api.greenhouse.io/v1/boards/{quoted}/jobs", retries=0
        )
    except RuntimeError:
        return None
    return {
        "board_name": board.get("name"),
        "titles": [job.get("title", "") for job in listing.get("jobs", [])],
        "field": "token",
    }


def _probe_ashby(slug: str) -> dict[str, Any] | None:
    try:
        listing = request_json(
            f"https://api.ashbyhq.com/posting-api/job-board/{urllib.parse.quote(slug)}",
            retries=0,
        )
    except RuntimeError:
        return None
    return {
        # Ashby's public board API carries no company name, so identity here
        # cannot be settled without a human looking at the postings.
        "board_name": None,
        "titles": [job.get("title", "") for job in listing.get("jobs", [])],
        "field": "board",
    }


def _probe_lever(slug: str) -> dict[str, Any] | None:
    try:
        listing = request_json(
            f"https://api.lever.co/v0/postings/{urllib.parse.quote(slug)}?mode=json", retries=0
        )
    except RuntimeError:
        return None
    if not isinstance(listing, list):
        return None
    return {
        "board_name": None,
        "titles": [job.get("text", "") for job in listing if isinstance(job, dict)],
        "field": "site",
    }


# Probed in this order per company; the first board that exists and has jobs wins.
# Workday is absent on purpose: it needs tenant, datacenter, and site, and site
# names are unguessable ("NVIDIAExternalCareerSite" vs "External_Career_Site"),
# so a company name alone cannot resolve one.
DISCOVERY_VENDORS = {
    "greenhouse": _probe_greenhouse,
    "ashby": _probe_ashby,
    "lever": _probe_lever,
}
DISCOVERY_VENDOR_ORDER = ("greenhouse", "ashby", "lever")


def discover_ats(
    companies: list[str],
    sources_config: dict[str, Any],
    discovery_terms: list[str],
    vendors: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Resolve company names to scannable ATS boards. Never writes anything."""
    order = [kind for kind in (vendors or DISCOVERY_VENDOR_ORDER) if kind in DISCOVERY_VENDORS]
    known = {
        (source["kind"], str(source.get(field, "")).lower())
        for source in sources_config.get("ats_sources", [])
        for field in ("token", "board", "site")
        if source.get(field)
    }
    results: list[dict[str, Any]] = []
    for company in companies:
        outcome: dict[str, Any] = {"company": company, "status": "unresolved"}
        for kind in order:
            probe = DISCOVERY_VENDORS[kind]
            for slug in slug_candidates(company):
                if (kind, slug.lower()) in known:
                    outcome = {
                        "company": company,
                        "status": "already-configured",
                        "kind": kind,
                        "slug": slug,
                    }
                    break
                found = probe(slug)
                # A board with no postings at all is indistinguishable from a
                # parked slug, and is not worth a config entry either way.
                if not found or not found["titles"]:
                    continue
                matching = [
                    title
                    for title in found["titles"]
                    if is_discovery_candidate(title, discovery_terms)
                ]
                board_name = found["board_name"]
                if board_name is None:
                    identity = "unverified"
                elif identity_tokens(board_name) == identity_tokens(company):
                    identity = "confirmed"
                else:
                    identity = "review"
                outcome = {
                    "company": company,
                    "status": "resolved",
                    "kind": kind,
                    "slug": slug,
                    "field": found["field"],
                    "board_name": board_name,
                    "identity": identity,
                    "total": len(found["titles"]),
                    "titles": found["titles"],
                    "matching": matching,
                    "entry": {
                        "kind": kind,
                        "company": board_name or company,
                        found["field"]: slug,
                        "enabled": True,
                    },
                }
                break
            if outcome["status"] != "unresolved":
                break
        results.append(outcome)
    return results


def write_discovered_sources(entries: list[dict[str, Any]], path: Path | None = None) -> Path:
    """Append entries to a sources file, preserving the rest of it.

    The default target is the student's own config/sources.local.json, so a
    `git pull` of the shared catalog never conflicts with boards they found.
    """
    path = path or paths.SOURCES_LOCAL_PATH
    config = load_json(path) if path.exists() else {}
    config.setdefault("ats_sources", []).extend(entries)
    # Temp-then-replace so an interrupted write cannot truncate a curated file.
    temp = path.with_suffix(".json.tmp")
    temp.write_text(
        json.dumps(config, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temp.replace(path)
    return path


def report_discovery(
    companies: list[str],
    sources_config: dict[str, Any],
    write: bool = False,
    include_unverified: bool = False,
    vendors: Iterable[str] | None = None,
    shared: bool = False,
) -> list[dict[str, Any]]:
    terms = sources_config["discovery_title_terms"]
    print(f"Probing {len(companies)} company name(s)…", flush=True)
    results = discover_ats(companies, sources_config, terms, vendors)

    writable: list[dict[str, Any]] = []
    for result in results:
        company = result["company"]
        if result["status"] == "already-configured":
            print(f"  = {company}: already in sources.json ({result['kind']}:{result['slug']})")
            continue
        if result["status"] == "unresolved":
            # Not evidence the company has no board: JS-rendered portals,
            # non-standard slugs, and Workday all land here.
            print(f"  - {company}: no board found — check manually")
            continue
        matching = result["matching"]
        summary = f"{result['total']} postings, {len(matching)} matching discovery terms"
        if result["identity"] == "confirmed":
            print(f"  + {company}: {result['kind']}:{result['slug']} — {summary}")
            writable.append(result)
        elif result["identity"] == "review":
            print(
                f"  ? {company}: {result['kind']}:{result['slug']} board is named "
                f"\"{result['board_name']}\" — NOT the same company? {summary}"
            )
        else:
            print(
                f"  ? {company}: {result['kind']}:{result['slug']} — {summary}, "
                f"identity unverified ({result['kind']} exposes no company name)"
            )
            sample = matching[0] if matching else (result["titles"][0] if result["titles"] else "")
            if sample:
                print(f"      sample posting: {sample}")
            if include_unverified:
                writable.append(result)

    # Identity-compared, not value-compared: two results can be equal dicts, and
    # `in` on a list of dicts would then hide one of them from this list.
    writable_ids = {id(result) for result in writable}
    held_back = [
        result
        for result in results
        if result["status"] == "resolved" and id(result) not in writable_ids
    ]
    if held_back:
        # Naming these explicitly matters: they are boards that exist and have
        # postings, so silence would read as "nothing found" rather than "found,
        # but not trustworthy without a look".
        print(f"\n{len(held_back)} board(s) found but held back pending identity confirmation:")
        for result in held_back:
            reason = (
                f"board is named \"{result['board_name']}\""
                if result["identity"] == "review"
                else "no company name exposed by this API — pass --include-unverified once checked"
            )
            print(f"  {result['company']} -> {result['kind']}:{result['slug']} ({reason})")

    if not write:
        if writable:
            print("\nPreview only — nothing written. Entries that WOULD be added:")
            print(json.dumps([result["entry"] for result in writable], indent=2, ensure_ascii=False))
            print("\nRe-run with --write to append them to config/sources.local.json.")
        else:
            print("\nPreview only — nothing written, and nothing currently qualifies to write.")
        return results

    if not writable:
        print("\nNothing to write.")
        return results
    written = write_discovered_sources(
        [result["entry"] for result in writable], paths.SOURCES_PATH if shared else None
    )
    print(f"\nAppended {len(writable)} source(s) to {display_path(written)}.")
    print("Confirm each employer's identity on its board before trusting the postings.")
    return results
