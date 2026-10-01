"""Importers: manual CSV rows, LinkedIn alert emails, agent-discovered postings and description enrichment."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .clock import parse_datetime
from .paths import display_path
from .store import deduplicate, THIN_DESCRIPTION_CHARS, upsert_jobs
from .text import canonical_url, fingerprint, fingerprint_text, strip_html


def import_manual(conn: sqlite3.Connection, path: Path) -> int:
    if not path.exists():
        raise SystemExit(f"Manual import file not found: {path}")
    records: list[dict[str, Any]] = []
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for line_number, row in enumerate(csv.DictReader(handle), start=2):
            if not any((value or "").strip() for value in row.values()):
                continue
            missing = [field for field in ("company", "title", "url") if not (row.get(field) or "").strip()]
            if missing:
                raise SystemExit(f"{path}:{line_number} missing {', '.join(missing)}")
            url = row["url"].strip()
            records.append(
                {
                    "external_id": row.get("external_id", "").strip() or url_external_id(url, linkedin_ids=False),
                    "company": row["company"].strip(),
                    "title": row["title"].strip(),
                    "location": row.get("location", "").strip(),
                    "url": url,
                    "description": row.get("description", "").strip(),
                    "posted_at": row.get("posted_at", "").strip() or None,
                }
            )
    count = upsert_jobs(conn, "manual:csv", "Manual / login-only sources", records)
    conn.commit()
    print(f"Imported {count} manual postings from {display_path(path)}")
    return count


LINKEDIN_JOB_ID_RE = re.compile(r"/jobs/view/(\d+)")


def url_external_id(url: str, *, linkedin_ids: bool) -> str:
    """The external id an imported posting gets when its source gave none.

    Email and discovered imports keep a LinkedIn job's own id (`linkedin_ids=True`).
    The manual CSV import does not (`False`): it hashes the URL even for a LinkedIn
    link, and changing that would re-key existing manual rows and duplicate them.
    """
    if linkedin_ids:
        match = LINKEDIN_JOB_ID_RE.search(url)
        if match:
            return match.group(1)
    return hashlib.sha256(canonical_url(url).encode("utf-8")).hexdigest()[:20]
LINKEDIN_POSTED_HINT_RE = re.compile(r"posted on (\d{1,2}/\d{1,2}/\d{4})", re.I)


def parse_linkedin_posted_hint(hint: str | None) -> str | None:
    if not hint:
        return None
    match = LINKEDIN_POSTED_HINT_RE.search(hint)
    if not match:
        return None
    try:
        return (
            datetime.strptime(match.group(1), "%m/%d/%Y")
            .replace(tzinfo=timezone.utc)
            .isoformat()
        )
    except ValueError:
        return None


def import_emails(conn: sqlite3.Connection, path: Path) -> int:
    """Import job postings extracted from LinkedIn job-alert emails.

    Unlike import_manual(), this is tolerant of malformed records: the
    records come from best-effort email extraction (done by an agent
    session reading Gmail), not a curated CSV, so a bad row is skipped
    with a warning rather than aborting the whole import.
    """
    if not path.exists():
        raise SystemExit(f"Email import file not found: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    records: list[dict[str, Any]] = []
    skipped = 0
    for index, item in enumerate(raw):
        company = (item.get("company") or "").strip()
        title = (item.get("title") or "").strip()
        url = (item.get("url") or "").strip()
        if not (company and title and url):
            print(f"  skipping record {index}: missing company/title/url", file=sys.stderr)
            skipped += 1
            continue
        external_id = url_external_id(url, linkedin_ids=True)
        records.append(
            {
                "external_id": external_id,
                "company": company,
                "title": title,
                "location": (item.get("location") or "").strip(),
                "url": url,
                "description": (item.get("match_note") or "").strip(),
                "posted_at": parse_linkedin_posted_hint(item.get("posted_hint")),
            }
        )
    count = upsert_jobs(conn, "manual:linkedin-email", "LinkedIn job-alert email", records)
    conn.commit()
    print(f"Imported {count} LinkedIn email postings from {display_path(path)} ({skipped} skipped)")
    return count


# Channels an agent session may attribute a discovered posting to. Each maps to
# its own source_key so one channel's import never deactivates another's finds,
# and so the dashboard's source filter stays meaningful. Deliberately an
# allowlist: an unrecognised channel is skipped rather than silently creating a
# new provenance label, because "where did this come from" is the one field a
# human reviewer cannot reconstruct later.
AGENT_CHANNELS = {
    "exa": "Agent: Exa semantic search",
    "jina": "Agent: public page read (Jina Reader)",
    "github": "Agent: community internship list (GitHub)",
    "rss": "Agent: RSS/Atom feed",
    "linkedin": "Agent: LinkedIn search (authenticated session)",
}

LINKEDIN_BASE = "https://www.linkedin.com"
# The LinkedIn scraper returns site-relative hrefs ("/jobs/view/4433615587/").
LINKEDIN_RELATIVE_RE = re.compile(r"^/(?:jobs|in|company)/")
# ...and appends LinkedIn's verification-badge UI text to scraped job titles
# ("Software Engineer Intern with verification"), which is chrome, not a title.
LINKEDIN_BADGE_SUFFIX_RE = re.compile(r"\s*with verification\s*$", re.I)


# get_job_details returns one unstructured text blob: a short header (company,
# title, location), the real posting under "About the job", and then a long tail
# of LinkedIn chrome. That tail includes a "More jobs" carousel advertising
# *other companies'* postings, so keeping it would let unrelated employers'
# keywords score this posting. Cut at the earliest chrome marker.
LINKEDIN_DESCRIPTION_START = "About the job"
LINKEDIN_CHROME_MARKERS = (
    "Benefits found in job post",
    "Set alert for similar jobs",
    "Unlock hiring insights",
    "About the company",
    "More jobs",
    "Looking for talent?",
    "Put your best foot forward",
)


def parse_linkedin_job_posting(text: str) -> dict[str, str]:
    """Pull company, title, location, and the real description out of a blob.

    Deliberately a pure function: the agent session fetches the text, this
    parses it, so the extraction is deterministic and testable offline instead
    of being re-improvised per run.

    Anything it cannot identify confidently comes back as an empty string. A
    blank location scores neutral in this pipeline while a wrong one draws the
    out-of-region penalty, so guessing is strictly worse than abstaining.
    """
    blocks = [block.strip() for block in re.split(r"\n\s*\n", text or "") if block.strip()]
    result = {"company": "", "title": "", "location": "", "description": ""}
    if blocks:
        result["company"] = blocks[0]
    if len(blocks) > 1:
        result["title"] = LINKEDIN_BADGE_SUFFIX_RE.sub("", blocks[1]).strip()
    if len(blocks) > 2:
        # "Austin, TX · Reposted 18 hours ago · Over 100 people clicked apply"
        candidate = blocks[2].split("·")[0].strip()
        # Only trust it if it reads like a place, not like the next chrome line.
        if "," in candidate or candidate.lower() in {"remote", "on-site", "hybrid"}:
            result["location"] = candidate

    start = text.find(LINKEDIN_DESCRIPTION_START)
    if start != -1:
        body_start = start + len(LINKEDIN_DESCRIPTION_START)
        cuts = [
            index
            for index in (text.find(marker, body_start) for marker in LINKEDIN_CHROME_MARKERS)
            if index != -1
        ]
        body = text[body_start : min(cuts)] if cuts else text[body_start:]
        result["description"] = body.strip()
    return result


def _normalize_agent_url(channel: str, url: str) -> str:
    """Absolutize the site-relative URLs the LinkedIn scraper emits."""
    if channel == "linkedin" and LINKEDIN_RELATIVE_RE.match(url):
        return LINKEDIN_BASE + url
    return url


def _normalize_agent_title(channel: str, title: str) -> str:
    if channel == "linkedin":
        return LINKEDIN_BADGE_SUFFIX_RE.sub("", title).strip()
    return title


def _agent_posted_at(item: dict[str, Any]) -> str | None:
    """Accept either a real ISO timestamp or a LinkedIn-style 'Posted on M/D/YYYY'."""
    explicit = (item.get("posted_at") or "").strip()
    if explicit:
        parsed = parse_datetime(explicit)
        if parsed:
            return parsed.isoformat()
    return parse_linkedin_posted_hint(item.get("posted_hint"))


def _parse_discovered_payload(path: Path, raw: Any) -> tuple[list[Any], set[str]]:
    """Accept a bare posting list, or an envelope that also names what was searched.

    The list form stays the common case. The envelope exists so a session can
    say "I searched exa and it returned nothing", which a list of postings has
    no way to express and which retirement depends on.
    """
    if isinstance(raw, list):
        return raw, set()
    if not isinstance(raw, dict):
        raise SystemExit(
            f"{path}: expected a JSON list of postings, or an object with 'postings'"
        )
    postings = raw.get("postings")
    if not isinstance(postings, list):
        raise SystemExit(f"{path}: envelope form needs a 'postings' list")
    searched: set[str] = set()
    for name in raw.get("searched_channels") or []:
        channel = str(name).strip().lower()
        if channel not in AGENT_CHANNELS:
            known = ", ".join(sorted(AGENT_CHANNELS))
            print(
                f"  ignoring unknown searched channel {channel!r} (known: {known})",
                file=sys.stderr,
            )
            continue
        searched.add(channel)
    return postings, searched


def import_discovered(conn: sqlite3.Connection, path: Path) -> int:
    """Import postings an agent session found through Agent Reach channels.

    Same tolerant contract as import_emails(): these records come from
    best-effort agent extraction across search results, public pages, and
    community lists, so one malformed row is skipped with a warning rather
    than aborting an otherwise good batch.

    Every record must name the `channel` it came from and carry a real URL, so
    each posting on the dashboard stays traceable to something a human can open
    and verify. Like import-emails, this is not part of `run` -- the discovery
    step happens in an agent session, not in this process.
    """
    if not path.exists():
        raise SystemExit(f"Discovered-jobs file not found: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw, searched_channels = _parse_discovered_payload(path, raw)

    by_channel: dict[str, list[dict[str, Any]]] = {}
    skipped = 0
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            print(f"  skipping record {index}: not an object", file=sys.stderr)
            skipped += 1
            continue
        channel = (item.get("channel") or "").strip().lower()
        company = (item.get("company") or "").strip()
        title = (item.get("title") or "").strip()
        url = (item.get("url") or "").strip()
        location = (item.get("location") or "").strip()
        description = (item.get("description") or "").strip()
        # A LinkedIn record may hand over the raw get_job_details text instead of
        # pre-split fields. Explicit fields still win, so a caller can correct a
        # bad parse without editing the blob.
        raw_posting = (item.get("raw_posting") or "").strip()
        if raw_posting:
            parsed = parse_linkedin_job_posting(raw_posting)
            company = company or parsed["company"]
            title = title or parsed["title"]
            location = location or parsed["location"]
            description = description or parsed["description"]
        if channel not in AGENT_CHANNELS:
            known = ", ".join(sorted(AGENT_CHANNELS))
            print(
                f"  skipping record {index}: unknown channel {channel!r} (known: {known})",
                file=sys.stderr,
            )
            skipped += 1
            continue
        if not (company and title and url):
            print(f"  skipping record {index}: missing company/title/url", file=sys.stderr)
            skipped += 1
            continue
        url = _normalize_agent_url(channel, url)
        title = _normalize_agent_title(channel, title)
        if not title:
            print(f"  skipping record {index}: title was only badge text", file=sys.stderr)
            skipped += 1
            continue
        if not url.lower().startswith(("http://", "https://")):
            print(f"  skipping record {index}: url is not http(s): {url!r}", file=sys.stderr)
            skipped += 1
            continue
        external_id = url_external_id(url, linkedin_ids=True)
        by_channel.setdefault(channel, []).append(
            {
                "external_id": external_id,
                "company": company,
                "title": title,
                "location": location,
                "url": url,
                "description": description,
                "posted_at": _agent_posted_at(item),
            }
        )

    # A channel the session searched but that yielded nothing still has to run
    # through upsert_jobs, with an empty batch, or its previous rows stay active
    # forever -- a channel going quiet is exactly when retirement matters.
    total = 0
    channels = sorted(searched_channels | set(by_channel))
    for channel in channels:
        records = by_channel.get(channel, [])
        total += upsert_jobs(
            conn, f"agent:{channel}", AGENT_CHANNELS[channel], records, dedupe=False
        )
        print(f"  {channel}: {len(records)} postings")
    if channels:
        # One duplicate_of pass for the whole import instead of one per channel.
        deduplicate(conn)
    conn.commit()
    print(
        f"Imported {total} agent-discovered postings from {display_path(path)} "
        f"({skipped} skipped)"
    )
    return total


def enrich_descriptions(conn: sqlite3.Connection, path: Path, force: bool = False) -> int:
    """Backfill descriptions an agent session fetched from public posting pages.

    LinkedIn's alert emails and most search-result rows carry no job
    description, so those postings score on title and location alone. An agent
    session can read the public posting page (Jina Reader) and write the text
    here to give them the same scoring surface as an ATS-sourced posting.

    Only thin descriptions are replaced unless force=True, so re-running this
    never overwrites the richer text an ATS API already provided. Scores are not
    recomputed here -- run `score` afterwards.
    """
    if not path.exists():
        raise SystemExit(f"Enrichment file not found: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise SystemExit(f"{path}: expected a JSON list of enrichment records")

    updated = 0
    skipped = 0
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            print(f"  skipping record {index}: not an object", file=sys.stderr)
            skipped += 1
            continue
        description = strip_html((item.get("description") or "").strip())
        job_id = (item.get("id") or "").strip()
        url = (item.get("url") or "").strip()
        if not description:
            print(f"  skipping record {index}: empty description", file=sys.stderr)
            skipped += 1
            continue
        columns = "id, description, company, title, location"
        if job_id:
            rows = conn.execute(f"SELECT {columns} FROM jobs WHERE id=?", (job_id,)).fetchall()
        elif url:
            rows = conn.execute(
                f"SELECT {columns} FROM jobs WHERE url=?", (canonical_url(url),)
            ).fetchall()
        else:
            print(f"  skipping record {index}: needs an id or url", file=sys.stderr)
            skipped += 1
            continue
        if not rows:
            target = job_id or url
            print(f"  skipping record {index}: no posting matches {target!r}", file=sys.stderr)
            skipped += 1
            continue
        for row in rows:
            if not force and len(row["description"]) >= THIN_DESCRIPTION_CHARS:
                skipped += 1
                continue
            posted_at = _agent_posted_at(item)
            # Backfilling a blank location changes what this posting is
            # comparable to, so the fingerprint has to move with it. A blank
            # location is compatible with every city, meaning such a row may
            # already be hidden behind a canonical it now contradicts.
            location = row["location"] or (item.get("location") or "").strip()
            conn.execute(
                """
                UPDATE jobs SET
                    description=?,
                    location=?,
                    posted_at=COALESCE(posted_at, ?),
                    fingerprint=?,
                    content_fingerprint=?
                WHERE id=?
                """,
                (
                    description,
                    location,
                    posted_at,
                    fingerprint(row["company"], row["title"], location),
                    # Enrichment is usually the first time a thin posting has a
                    # body worth fingerprinting at all, so this is where a
                    # cross-source duplicate becomes detectable.
                    fingerprint_text(description),
                    row["id"],
                ),
            )
            updated += 1
    if updated:
        # Re-run with the new fingerprints and locations, so a posting that is
        # no longer a duplicate becomes visible before `score` and `report`.
        deduplicate(conn)
    conn.commit()
    print(
        f"Enriched {updated} descriptions from {display_path(path)} ({skipped} skipped). "
        "Run `python3 pipeline.py score` to refresh scores."
    )
    return updated
