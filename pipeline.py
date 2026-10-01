#!/usr/bin/env python3
"""Local, source-linked internship discovery and application pipeline."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import unicodedata
import urllib.parse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait as futures_wait
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from pipeline_core import paths
from pipeline_core.artifacts import write_artifact
from pipeline_core.clock import now_iso, parse_datetime
from pipeline_core.config import load_env_file, load_json, load_profile, load_sources, source_key
from pipeline_core.http import _source_host, request_json, request_text, TransientFetchError
from pipeline_core.identity import identity_tokens, normalized, sort_key
from pipeline_core.paths import display_path
from pipeline_core.read_model import RANKED_VIEW_PER_COMPANY
from pipeline_core.regions import region_label
from pipeline_core.scoring import score_all
from pipeline_core.sources import _fetch_source, is_discovery_candidate, Listing
from pipeline_core.text import (
    apply_controls,
    canonical_url,
    classify_role,
    CROSSLIST_THRESHOLD,
    fingerprint,
    fingerprint_text,
    stable_id,
    strip_html,
)


VALID_STATUSES = {
    "discovered",
    "shortlisted",
    "applying",
    "applied",
    "interview",
    "offer",
    "rejected",
    "withdrawn",
}


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    """Open (and create) a pipeline database: `db_path`, or the module's DB_PATH read now."""
    path = paths.DB_PATH if db_path is None else db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            source_key TEXT NOT NULL,
            source_name TEXT NOT NULL,
            external_id TEXT NOT NULL,
            company TEXT NOT NULL,
            title TEXT NOT NULL,
            location TEXT NOT NULL DEFAULT '',
            role_type TEXT NOT NULL DEFAULT 'other',
            url TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            posted_at TEXT,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            fingerprint TEXT NOT NULL,
            duplicate_of TEXT,
            score INTEGER NOT NULL DEFAULT 0,
            score_explanation TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'discovered',
            notes TEXT NOT NULL DEFAULT '',
            applied_at TEXT,
            follow_up_at TEXT,
            UNIQUE(source_key, external_id)
        );
        CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(score DESC);
        CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint ON jobs(fingerprint);
        CREATE TABLE IF NOT EXISTS fetch_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_key TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            outcome TEXT NOT NULL,
            fetched_count INTEGER NOT NULL DEFAULT 0,
            error TEXT
        );
        """
    )
    # Added after the first databases existed, so CREATE TABLE above will not
    # introduce it for them. Existing rows keep an empty fingerprint until their
    # source is fetched again, and the dedupe pass that reads it skips blanks.
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "content_fingerprint" not in columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN content_fingerprint TEXT NOT NULL DEFAULT ''")
    # How many postings a board listed before the discovery filter; NULL for
    # runs from before it was recorded. Retirement reads it (_retirement_hold).
    run_columns = {row["name"] for row in conn.execute("PRAGMA table_info(fetch_runs)")}
    if "listed_count" not in run_columns:
        conn.execute("ALTER TABLE fetch_runs ADD COLUMN listed_count INTEGER")
    return conn


# `run` exits with this (EX_TEMPFAIL) when a source could not be reached at all,
# so the scheduled wrapper knows the fetch is incomplete and retries it later
# instead of recording the day as done.
EXIT_TEMPFAIL = 75


class FatalDatabaseError(RuntimeError):
    """The local database itself is unusable -- disk full, file gone, a dead
    connection. Unlike a source failing, this cannot be isolated to one source
    and recorded: the row recording it would fail too. The run stops rather
    than reporting a day that looks complete."""


# Concurrency ceilings for the fetch. 34 of the 73 enabled sources share
# boards-api.greenhouse.io and 21 share api.ashbyhq.com, so a purely global
# pool would put a third of its workers on one hostname. Four per host is
# lighter than a person with a few tabs open; twelve overall keeps the pool
# busy across the other hosts while Greenhouse works through its queue.
FETCH_MAX_WORKERS = 12
FETCH_MAX_PER_HOST = 4

# A description this short carries no scoring signal beyond the title, so it is
# treated as absent and eligible for enrichment.
THIN_DESCRIPTION_CHARS = 200


def _richer_description(existing: str, incoming: str) -> str:
    """Pick the description that carries more scoring signal.

    A recurring sweep normally rediscovers a posting it already knows, and
    search-result rows carry no description. Letting that thin row overwrite
    enriched text would silently undo `enrich` on every subsequent import. An
    incoming description that is substantive on its own still wins, so a real
    source refresh stays authoritative.
    """
    if len(incoming) >= THIN_DESCRIPTION_CHARS:
        return incoming
    return incoming if len(incoming) >= len(existing) else existing


def upsert_jobs(
    conn: sqlite3.Connection,
    source_key: str,
    source_name: str,
    records: list[dict[str, Any]],
    seen: str | None = None,
    *,
    dedupe: bool = True,
) -> int:
    # `dedupe=False` leaves the duplicate_of pass to the caller: import_discovered
    # upserts every channel and deduplicates once before its single commit. The
    # pass is a pure function of the table, so the final links are the same.
    # fetch_all keeps the per-source pass, so a failing pass rolls that source
    # back and records an error instead of committing it as a success.
    #
    # `seen` becomes first_seen_at/last_seen_at, both of which are ranking keys:
    # `discovered` sorts on first_seen_at and `score` falls back to
    # last_seen_at. Reading the clock here would make a posting with no
    # posted_at rank by whichever source happened to finish first, so the fetch
    # passes one timestamp for the whole cycle. Defaulted for callers that
    # upsert a single source outside a cycle.
    seen = seen or now_iso()
    ids: list[str] = []
    for record in records:
        url = canonical_url(record["url"])
        external_id = record["external_id"]
        job_id = stable_id(source_key, external_id)
        ids.append(job_id)
        description = record["description"]
        location = record["location"]
        # Merge against what is already stored rather than in the ON CONFLICT
        # clause, so role_type and fingerprint below describe the values that
        # actually land in the row.
        existing = conn.execute(
            "SELECT description, location, content_fingerprint FROM jobs WHERE source_key=? AND external_id=?",
            (source_key, external_id),
        ).fetchone()
        if existing:
            description = _richer_description(existing["description"], description)
            location = location or existing["location"]
        role_type = classify_role(record["title"], description)
        fp = fingerprint(record["company"], record["title"], location)
        # The SimHash depends only on the description, so a row whose merged
        # description is the stored one keeps its stored fingerprint. Blank
        # means "never computed" (or too short to fingerprint), so it is
        # recomputed.
        if existing and description == existing["description"] and existing["content_fingerprint"]:
            content_fp = existing["content_fingerprint"]
        else:
            content_fp = fingerprint_text(description)
        conn.execute(
            """
            INSERT INTO jobs (
                id, source_key, source_name, external_id, company, title, location,
                role_type, url, description, posted_at, first_seen_at, last_seen_at,
                active, fingerprint, content_fingerprint
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(source_key, external_id) DO UPDATE SET
                source_name=excluded.source_name,
                company=excluded.company,
                title=excluded.title,
                location=excluded.location,
                role_type=excluded.role_type,
                url=excluded.url,
                description=excluded.description,
                posted_at=COALESCE(excluded.posted_at, jobs.posted_at),
                last_seen_at=excluded.last_seen_at,
                active=1,
                fingerprint=excluded.fingerprint,
                content_fingerprint=excluded.content_fingerprint
            """,
            (
                job_id,
                source_key,
                source_name,
                external_id,
                record["company"],
                record["title"],
                location,
                role_type,
                url,
                description,
                record.get("posted_at"),
                seen,
                seen,
                fp,
                content_fp,
            ),
        )
    _retire_absent(conn, source_key, records, set(ids), seen)
    if dedupe:
        deduplicate(conn)
    return len(records)


# Guards on retiring a posting because a board fetch no longer lists it. The
# rule is the one jobleft's crawler states (packages/crawler/src/lifecycle.ts
# in github.com/blueturboguy07/jobleft; the idea, not its code): absence is
# evidence only when the fetch proved it read the whole board. Retirement
# matters because purge-expired deletes retired rows the same day, so one odd
# answer -- a 200 with `"jobs": []`, or a changed shape that parses as empty --
# used to delete every posting the board had.
#
# An empty listing proves nothing on its own: it looks exactly like a broken
# crawl. A board that really emptied out is retired once it has answered empty
# this many fetches in a row and its postings have gone unseen for the grace.
EMPTY_LISTING_MIN_STREAK = 3
# A listing that shrank to under half of the previous fetch's (from at least
# this many postings) may be a partial answer, so only postings already unseen
# for the grace are retired. A real mass closure retires on the next fetch,
# once the smaller listing is itself the baseline.
LISTING_SHRINK_MIN_PRIOR = 10
RETIRE_GRACE = timedelta(hours=48)


def _retirement_hold(conn: sqlite3.Connection, source_key: str, listing: Listing) -> tuple[str, str] | None:
    """Why absence from this listing cannot retire everything, or None.

    Returns `(mode, reason)`: mode `none` retires nothing, `stale` retires only
    postings unseen for RETIRE_GRACE.
    """
    prior = [
        row["listed_count"]
        for row in conn.execute(
            "SELECT listed_count FROM fetch_runs WHERE source_key=? AND outcome='success' "
            "ORDER BY id DESC LIMIT ?",
            (source_key, EMPTY_LISTING_MIN_STREAK),
        )
    ]
    if listing.listed == 0:
        streak = 1
        for count in prior:
            if count != 0:
                break
            streak += 1
        if streak < EMPTY_LISTING_MIN_STREAK:
            return "none", f"the board listed nothing ({streak} of {EMPTY_LISTING_MIN_STREAK} empty answers)"
        return "stale", f"the board has listed nothing {streak} times running"
    if not listing.complete:
        return "stale", "the fetch read only part of the listing"
    previous = prior[0] if prior else None
    if previous is not None and previous >= LISTING_SHRINK_MIN_PRIOR and listing.listed * 2 < previous:
        return "stale", f"the listing shrank from {previous} to {listing.listed} postings"
    return None


def _retire_absent(
    conn: sqlite3.Connection,
    source_key: str,
    records: list[dict[str, Any]],
    present: set[str],
    seen: str,
) -> None:
    absent = [
        row
        for row in conn.execute(
            "SELECT id, last_seen_at FROM jobs WHERE source_key=? AND active=1", (source_key,)
        )
        if row["id"] not in present
    ]
    doomed = [row["id"] for row in absent]
    # A plain list is a whole batch its caller vouches for (CSV, email, agent
    # imports), so only a board Listing is second-guessed.
    hold = _retirement_hold(conn, source_key, records) if absent and isinstance(records, Listing) else None
    if hold:
        mode, reason = hold
        cutoff = (parse_datetime(seen) or datetime.now(timezone.utc)) - RETIRE_GRACE
        doomed = [
            row["id"]
            for row in absent
            if mode == "stale" and (parse_datetime(row["last_seen_at"]) or cutoff) <= cutoff
        ]
        if len(doomed) < len(absent):
            print(
                f"  Kept {len(absent) - len(doomed)} unlisted posting(s) open for {source_key}: {reason}",
                flush=True,
            )
    for start in range(0, len(doomed), 500):
        chunk = doomed[start : start + 500]
        placeholders = ",".join("?" for _ in chunk)
        conn.execute(f"UPDATE jobs SET active=0 WHERE id IN ({placeholders})", chunk)


STATUS_PRIORITY = {
    "offer": 7,
    "interview": 6,
    "applied": 5,
    "applying": 4,
    "shortlisted": 3,
    "discovered": 2,
    "withdrawn": 1,
    "rejected": 0,
}


def _canonical_of(group: list[sqlite3.Row]) -> sqlite3.Row:
    """Prefer the furthest-along copy, then a real source, then the fullest text."""
    return max(
        group,
        key=lambda row: (
            STATUS_PRIORITY.get(row["status"], 0),
            not row["source_key"].startswith(("manual:", "agent:")),
            len(row["description"]),
        ),
    )


def location_cities(location: str) -> set[str]:
    """City tokens from a location field, tolerating multi-location strings.

    Greenhouse packs several places into one field
    ("Austin, Texas, United States; South San Francisco, California, ...")
    while LinkedIn gives a single "Austin, TX". Comparing city tokens is what
    lets those be recognised as the same opportunity.
    """
    cities: set[str] = set()
    for segment in (location or "").split(";"):
        head = normalized(segment.strip().split(",")[0])
        if head:
            cities.add(head)
    return cities


def locations_compatible(left: str, right: str) -> bool:
    """True when two location fields could describe the same posting.

    A blank side is compatible with anything -- a sparse location field is not
    evidence of a different city, the same reasoning the scorer uses when it
    declines to penalise an uninformative location.
    """
    left_cities, right_cities = location_cities(left), location_cities(right)
    if not left_cities or not right_cities:
        return True
    return bool(left_cities & right_cities)


def deduplicate(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT id, fingerprint, content_fingerprint, status, source_key, description,
               company, title, location
        FROM jobs
        WHERE active=1
        ORDER BY fingerprint, id
        """
    ).fetchall()

    # Pass 1: identical company + title + location.
    groups: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        groups.setdefault(row["fingerprint"], []).append(row)
    resolved: dict[str, str] = {}
    for group in groups.values():
        if len(group) < 2:
            continue
        canonical = _canonical_of(group)["id"]
        for row in group:
            if row["id"] != canonical:
                resolved[row["id"]] = canonical

    # Pass 2: same company and title across sources whose locations do not
    # contradict each other. Pass 1 misses these because each channel formats
    # locations differently, which would otherwise show one job twice -- once
    # from its ATS and once from LinkedIn.
    by_role: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        if row["id"] in resolved:
            continue
        by_role.setdefault((normalized(row["company"]), normalized(row["title"])), []).append(row)
    for group in by_role.values():
        if len(group) < 2:
            continue
        # One company/title group can span several cities, and compatibility is
        # not transitive because a blank location matches anything. Peel off one
        # cluster at a time around its own canonical instead of measuring the
        # whole group against a single winner, which would leave the duplicates
        # in every other city unresolved.
        remaining = group
        while len(remaining) > 1:
            canonical_row = _canonical_of(remaining)
            cluster = [
                row
                for row in remaining
                if row["id"] != canonical_row["id"]
                and locations_compatible(row["location"], canonical_row["location"])
            ]
            for row in cluster:
                resolved[row["id"]] = canonical_row["id"]
            assigned = {canonical_row["id"], *(row["id"] for row in cluster)}
            remaining = [row for row in remaining if row["id"] not in assigned]

    # Pass 3: near-identical description bodies across different sources. Passes
    # 1 and 2 both key on the company name, so they cannot reconcile a posting
    # that arrives from the employer's own board and again from a channel that
    # restyled the company and rewrote the title. Employers rarely rewrite the
    # requirements text, which is what makes the body the reliable key.
    #
    # Guarded deliberately: only across different sources, because one employer
    # legitimately posts several near-identical reqs on its own board, and only
    # where locations do not contradict, because the same JD used for two cities
    # is two opportunities.
    #
    # Measured 2026-08-05 against 217 fingerprintable rows / 369 comparable
    # cross-source pairs: nothing reached the threshold, and the threshold must
    # stay where it is. Adzuna truncates every description to exactly 500
    # characters, so its copy of a posting is a prefix of the real body rather
    # than a near-verbatim match -- the true Figure pairing scored only 0.781.
    # Lowering the bar to catch it is not safe: two *different* Figure roles
    # scored 0.719 off their shared company boilerplate, and unrelated companies
    # (Neuralink vs Base Power) scored 0.703. The gap between a true match and a
    # false one is 0.06, so this pass earns its keep only on sources that carry
    # full bodies -- which is what `enrich` gives agent-discovered rows.
    candidates = [
        row for row in rows if row["id"] not in resolved and row["content_fingerprint"]
    ]

    # The pair loop is quadratic in the fingerprintable rows, so what it does per
    # pair is kept to integer work: each row's fingerprint and city set are
    # computed once up front, and the similarity test (an XOR and a popcount)
    # runs before the location test (a set intersection). Both are pure and
    # joined by AND, so the order they run in cannot change which rows match.
    # The threshold becomes the largest differing-bit count it admits, found by
    # evaluating the same `(64 - distance) / 64 >= threshold` expression that
    # fingerprint_similarity uses.
    max_distance = max(
        (distance for distance in range(65) if (64 - distance) / 64 >= CROSSLIST_THRESHOLD),
        default=-1,
    )
    fingerprints = [int(row["content_fingerprint"], 16) for row in candidates]
    cities = [location_cities(row["location"]) for row in candidates]
    clustered: set[str] = set()
    for index, row in enumerate(candidates):
        if row["id"] in clustered:
            continue
        cluster = [row]
        row_fingerprint = fingerprints[index]
        row_cities = cities[index]
        row_source = row["source_key"]
        for other_index in range(index + 1, len(candidates)):
            other = candidates[other_index]
            if other["id"] in clustered or other["source_key"] == row_source:
                continue
            if (row_fingerprint ^ fingerprints[other_index]).bit_count() > max_distance:
                continue
            other_cities = cities[other_index]
            # locations_compatible: a blank side matches anything.
            if row_cities and other_cities and not (row_cities & other_cities):
                continue
            cluster.append(other)
            clustered.add(other["id"])
        if len(cluster) > 1:
            clustered.add(row["id"])
            canonical = _canonical_of(cluster)["id"]
            for member in cluster:
                if member["id"] != canonical:
                    resolved[member["id"]] = canonical

    # Write only the rows whose link changed. `resolved` is the complete answer:
    # a row that is not in it has no duplicate_of, so a row linked in the table
    # but absent from `resolved` (including an inactive one) is cleared.
    current = {
        row["id"]: row["duplicate_of"]
        for row in conn.execute("SELECT id, duplicate_of FROM jobs WHERE duplicate_of IS NOT NULL")
    }
    changes = [
        (resolved.get(job_id), job_id)
        for job_id, linked in current.items()
        if resolved.get(job_id) != linked
    ]
    changes.extend(
        (canonical, duplicate) for duplicate, canonical in resolved.items() if duplicate not in current
    )
    if changes:
        conn.executemany("UPDATE jobs SET duplicate_of=? WHERE id=?", changes)


# ---------------------------------------------------------------------------
# Posting liveness
#
# Ported from career-ops (https://github.com/santifer/career-ops), MIT licence,
# (c) 2026 Santiago Fernandez de Valderrama -- see THIRD_PARTY_NOTICES.md. The
# upstream file is `liveness-core.mjs`; its pattern set encodes failures found
# against real portals, and the comments explaining why each guard exists are
# kept because they are the reason the guard is there.
# ---------------------------------------------------------------------------

_SMART_SINGLE_QUOTES = "‘’ʼ′´`"
_SMART_DOUBLE_QUOTES = "“”″"


def normalize_for_match(text: str | None) -> str:
    """Fold a page into the alphabet the liveness patterns are written in.

    Portals write closure banners with typographic punctuation and accents:
    WTTJ renders "Cette offre n'est plus disponible." with U+2019, not an ASCII
    apostrophe. A pattern spelled with a plain apostrophe silently never
    matches, so a clearly expired posting falls through to "no apply control"
    and is never filtered. Normalise once here and spell every pattern below in
    ASCII quotes, without diacritics, with collapsed whitespace.
    """
    if not isinstance(text, str):
        return ""
    for char in _SMART_SINGLE_QUOTES:
        text = text.replace(char, "'")
    for char in _SMART_DOUBLE_QUOTES:
        text = text.replace(char, '"')
    decomposed = unicodedata.normalize("NFD", text)
    without_marks = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", without_marks)


_HARD_EXPIRED_PATTERNS = [
    r"job (is )?no longer available",
    r"job.*no longer open",
    # Generalised "filled" signal. A narrow /position has been filled/ misses the
    # phrasing SPA-based ATSs inject on a filled requisition -- "the job you are
    # trying to apply for has been filled" -- so those pages return HTTP 200 with
    # a generic Apply control and read as active. Require a job noun within 60
    # characters, then "has been filled", but not when the thing filled is an
    # application or a form, and not "filled out". Both guards avoid the worse
    # error: reading a LIVE posting whose copy says "once the application form
    # has been filled..." as expired.
    r"\b(?:job|jobs|position|role|posting|opening|vacancy|requisition|req|listing)\b"
    r"[\s\S]{0,60}?(?<!application\s)(?<!form\s)has been filled\b(?!\s+out)",
    r"this job has expired",
    r"job posting has expired",
    r"no longer accepting applications",
    r"this (position|role|job) (is )?no longer",
    r"this job (listing )?is closed",
    r"job (listing )?not found",
    r"the page you are looking for doesn.t exist",
    r"applications?\s+(?:(?:have|are|is)\s+)?closed",
    r"closed on \d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)",
    r"closed on (?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{1,2}",
    r"diese stelle (ist )?(nicht mehr|bereits) besetzt",
    # French closure banners, spelled accent-free on purpose: normalize_for_match
    # strips diacritics, so "expiree" here matches "expiree" on the page.
    r"offre (expiree|n'est plus disponible)",
    r"(cette )?offre n'est plus (disponible|en ligne|active)",
    r"(offre|poste|annonce) (deja )?pourvu(e)?",
    r"offre (cloturee|desactivee|terminee)",
    r"ce poste n'est plus (disponible|a pourvoir|ouvert)",
    r"recrutement (termine|cloture)",
    r"candidatures (closes|cloturees)",
]

_LISTING_PAGE_PATTERNS = [
    r"\d+\s+jobs?\s+found",
    r"search for jobs page is loaded",
]

# Anti-bot interstitials (Cloudflare "Just a moment...", captcha walls) render a
# tiny challenge page instead of the posting. They must NOT read as expired: the
# body is short and lacks an apply control, so without this guard they fall
# through to insufficient_content -> expired, and a live job would be retired
# and filtered out permanently.
_BOT_CHALLENGE_PATTERNS = [
    r"just a moment",
    r"performing security verification",
    r"checking your browser before",
    r"verify you are (a |not a )?human",
    r"enable javascript and cookies to continue",
    r"attention required.*cloudflare",
    r"\bray id\b",
    r"\bcf-ray\b",
    r"please complete the security check",
]

_EXPIRED_URL_PATTERNS = [r"[?&]error=true"]

_APPLY_PATTERNS = [
    r"\bapply\b",
    r"\bsolicitar\b",
    r"\bbewerben\b",
    r"\bpostuler\b",
    r"submit application",
    r"easy apply",
    r"start application",
    r"ich bewerbe mich",
]

_MIN_CONTENT_CHARS = 300

# A job-detail URL almost always carries the posting's identity: a numeric
# requisition id (Greenhouse, Workday pid) or a UUID (Lever, Ashby). If the
# requested URL had one and the final URL lost it, the browser landed elsewhere.
_JOB_ID_TOKEN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|\d{5,}",
    re.IGNORECASE,
)


def _first_match(patterns: Iterable[str], text: str) -> str | None:
    for pattern in patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return pattern
    return None


def _job_id_token(url: str) -> str | None:
    matches = _JOB_ID_TOKEN.findall(url or "")
    return matches[-1].lower() if matches else None


def classify_liveness(
    status: int = 0,
    requested_url: str = "",
    final_url: str = "",
    body_text: str = "",
    controls: Iterable[str] | None = None,
) -> dict[str, str]:
    """Decide whether a posting page is still open.

    Three-valued on purpose: `active`, `expired`, or `uncertain`. Only
    `expired` may retire a job. A false "expired" removes a real opportunity
    from the pipeline for good, which is far worse than carrying a dead posting
    for another week, so every ambiguous signal resolves to `uncertain`.
    """
    body = normalize_for_match(body_text)
    labels = [normalize_for_match(control) for control in (controls or [])]

    if status in (404, 410):
        return {"result": "expired", "code": "http_gone", "reason": f"HTTP {status}"}

    # Bot walls are never expired. Checked before the content-length and
    # listing-page heuristics, which would otherwise misread the short challenge
    # body as a dead posting. 403/503 are access-blocked signals, not "gone" --
    # a genuinely removed posting returns 404/410 or a closure banner.
    bot_challenge = _first_match(_BOT_CHALLENGE_PATTERNS, body)
    if bot_challenge:
        return {
            "result": "uncertain",
            "code": "bot_challenge",
            "reason": f"anti-bot challenge: {bot_challenge}",
        }
    if status in (403, 503):
        return {
            "result": "uncertain",
            "code": "access_blocked",
            "reason": f"HTTP {status} (access blocked, likely anti-bot)",
        }
    # A throttle says "ask again later", never "this posting is gone". Its body
    # is usually a one-line notice, which fell through to the content-length
    # heuristic below and read as expired -- retiring a live posting, which
    # purge-expired then deletes. This is the exact outcome the docstring above
    # calls far worse than carrying a dead posting for another week.
    if status == 429:
        return {
            "result": "uncertain",
            "code": "rate_limited",
            "reason": "HTTP 429 (rate limited, not evidence the posting is gone)",
        }
    # Any other 5xx is a transient origin error (502/504 gateway hiccups, 500s
    # during a deploy), not evidence the posting is gone. Without this guard the
    # short error body falls through to the insufficient-content heuristic and
    # reads as expired.
    if status >= 500:
        return {
            "result": "uncertain",
            "code": "server_error",
            "reason": f"HTTP {status} (transient server error)",
        }

    expired_url = _first_match(_EXPIRED_URL_PATTERNS, final_url or "")
    if expired_url:
        return {"result": "expired", "code": "expired_url", "reason": f"redirect to {final_url}"}

    expired_body = _first_match(_HARD_EXPIRED_PATTERNS, body)
    if expired_body:
        return {
            "result": "expired",
            "code": "expired_body",
            "reason": f"pattern matched: {expired_body}",
        }

    # A dead permalink that redirects to a generic search page still shows
    # "Apply" buttons -- on OTHER jobs' cards. When the requested URL carried a
    # job identifier and the final URL lost it, the page being read is not the
    # posting, so its apply controls are not evidence of liveness. Uncertain
    # rather than expired: a portal migration redirects live postings too.
    job_id = _job_id_token(requested_url)
    if job_id and final_url and job_id not in final_url.lower():
        return {
            "result": "uncertain",
            "code": "redirected_off_posting",
            "reason": f'redirected to {final_url} -- job id "{job_id}" missing from final URL',
        }

    if any(_first_match(_APPLY_PATTERNS, label) for label in labels):
        return {
            "result": "active",
            "code": "apply_control_visible",
            "reason": "visible apply control detected",
        }

    listing_page = _first_match(_LISTING_PAGE_PATTERNS, body)
    if listing_page:
        return {
            "result": "expired",
            "code": "listing_page",
            "reason": f"pattern matched: {listing_page}",
        }

    if len(body.strip()) < _MIN_CONTENT_CHARS:
        return {
            "result": "expired",
            "code": "insufficient_content",
            "reason": "insufficient content -- likely nav/footer only",
        }

    return {
        "result": "uncertain",
        "code": "no_apply_control",
        "reason": "content present but no visible apply control found",
    }


# Rows fetched from an ATS board are retired automatically: `upsert_jobs`
# deactivates anything missing from the source's latest batch. Rows that arrive
# without a batch behind them have no such mechanism -- `import-discovered` runs
# only when a human or agent session invokes it, and it is not part of `run` --
# so these are what the liveness check exists for.
LIVENESS_DEFAULT_PREFIXES = ("agent:", "manual:")

# A posting the user has already engaged with stays visible even when its page
# is gone. Losing sight of something you picked out is a worse failure than
# carrying a stale row, and the same reasoning drives `_canonical_of`.
# `shortlisted` is included because that status means you chose this posting
# deliberately -- it should never disappear without you seeing why.
LIVENESS_PROTECTED_STATUSES = {"shortlisted", "applying", "applied", "interview", "offer"}

# Channels whose postings were imported through a renderer or an authenticated
# session, not a plain GET. `agent:jina` exists precisely because those pages
# need JavaScript to become legible, and `agent:linkedin` needs a session.
# Re-fetching their URLs here with bare urllib returns an empty SPA shell or a
# login wall, which the thin-page heuristic would read as a dead posting and
# retire -- deleting a live opportunity. Hard evidence (404/410, an explicit
# closure banner) is still trusted for these; only the weak heuristic is not.
LIVENESS_UNRENDERABLE_SOURCES = ("agent:jina", "agent:linkedin")


def check_liveness(
    conn: sqlite3.Connection,
    limit: int | None = None,
    check_all: bool = False,
    dry_run: bool = False,
) -> dict[str, int]:
    prefixes = LIVENESS_DEFAULT_PREFIXES
    if check_all:
        query = "SELECT id, url, status, company, title, source_key FROM jobs WHERE active=1"
        params: list[Any] = []
    else:
        clauses = " OR ".join("source_key LIKE ?" for _ in prefixes)
        query = f"SELECT id, url, status, company, title, source_key FROM jobs WHERE active=1 AND ({clauses})"
        params = [f"{prefix}%" for prefix in prefixes]
    query += " ORDER BY last_seen_at ASC"
    if limit:
        query += " LIMIT ?"
        params.append(limit)
    rows = conn.execute(query, params).fetchall()

    tally = {"active": 0, "expired": 0, "uncertain": 0, "error": 0, "retired": 0}
    if not rows:
        print("No postings to check.")
        return tally

    print(f"Checking {len(rows)} posting(s)…", flush=True)
    changed = False
    for row in rows:
        label = f"{row['company']} — {row['title']}"
        try:
            status, final_url, body = request_text(row["url"])
        except RuntimeError as exc:
            # A request that never completed is not evidence about the posting.
            tally["error"] += 1
            print(f"  ? {label}: request failed ({exc})", file=sys.stderr)
            continue
        verdict = classify_liveness(
            status=status,
            requested_url=row["url"],
            final_url=final_url,
            body_text=strip_html(body),
            controls=apply_controls(body),
        )
        if verdict["code"] == "insufficient_content" and row["source_key"].startswith(
            LIVENESS_UNRENDERABLE_SOURCES
        ):
            verdict = {
                "result": "uncertain",
                "code": "needs_rendering",
                "reason": (
                    f"thin page, but {row['source_key']} postings need JavaScript or a "
                    "session to render - not evidence the posting is gone"
                ),
            }
        tally[verdict["result"]] += 1
        if verdict["result"] == "expired":
            if row["status"] in LIVENESS_PROTECTED_STATUSES:
                print(f"  ! {label}: {verdict['reason']} — kept ({row['status']})")
            elif dry_run:
                print(f"  x {label}: {verdict['reason']} — would retire")
            else:
                conn.execute("UPDATE jobs SET active=0 WHERE id=?", (row["id"],))
                tally["retired"] += 1
                changed = True
                print(f"  x {label}: {verdict['reason']} — retired")
        elif verdict["result"] == "active":
            print(f"  ok {label}")
        else:
            print(f"  ? {label}: {verdict['reason']}")

        if not dry_run:
            # Bumped for every completed verdict, not just `active`. The field
            # means "since checked" (see stale_label), and advancing it only on
            # success starved the queue: `--limit` orders by last_seen_at, so a
            # cohort of permanently-uncertain rows -- LinkedIn behind an auth
            # wall is a standing example -- would be rechecked every single day
            # while nothing else was ever reached.
            conn.execute("UPDATE jobs SET last_seen_at=? WHERE id=?", (now_iso(), row["id"]))
            # Committed per row, mirroring how `fetch_all` isolates each source:
            # an exception partway through must not roll back the verdicts
            # already established for earlier rows.
            conn.commit()

    if changed:
        # Retiring a row can orphan duplicates that pointed at it, and
        # `deduplicate` only considers active rows, so the links are rebuilt.
        deduplicate(conn)
    conn.commit()
    print(
        f"Live {tally['active']}, expired {tally['expired']} "
        f"({tally['retired']} retired), uncertain {tally['uncertain']}, errors {tally['error']}"
    )
    return tally


# A posting with an application behind it is history the student still needs,
# so it survives the purge even after it closes. These mirror the statuses the
# product migration turns into `applications` rows.
PURGE_PROTECTED_STATUSES = {"applying", "applied", "interview", "offer", "rejected", "withdrawn"}
PURGE_BACKUPS_KEPT = 14


def backup_sqlite(conn: sqlite3.Connection, label: str, keep: int = PURGE_BACKUPS_KEPT) -> Path | None:
    """Snapshot a file-backed SQLite database before a destructive change.

    Written to a `backups/` directory beside the database with SQLite's online
    backup API, which is consistent even while another process (the dashboard)
    has the database open. Only the newest `keep` snapshots for `label` are
    retained. Returns None for an in-memory database, which has nothing to lose.
    Any failure propagates so the caller never deletes without a backup.
    """
    main = next((row for row in conn.execute("PRAGMA database_list") if row[1] == "main"), None)
    if main is None or not main[2]:
        return None
    backup_dir = Path(main[2]).parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    # Prune only snapshots this function names, never backups made by hand.
    pattern = re.compile(rf"{re.escape(label)}-(\d{{8}}T\d{{12}})Z\.db")
    stamps = sorted(match[1] for path in backup_dir.iterdir() if (match := pattern.fullmatch(path.name)))
    # Name the snapshot after the newest existing one even when the clock
    # disagrees. Two backups in one clock tick would otherwise share a name,
    # the second overwriting the first, and after the clock steps back the new
    # snapshot would sort oldest and be pruned below before the caller used it.
    taken = datetime.now(timezone.utc)
    if stamps:
        newest = datetime.strptime(stamps[-1], "%Y%m%dT%H%M%S%f").replace(tzinfo=timezone.utc)
        taken = max(taken, newest + timedelta(microseconds=1))
    target = backup_dir / f"{label}-{taken.strftime('%Y%m%dT%H%M%S%fZ')}.db"
    destination = sqlite3.connect(target)
    try:
        conn.backup(destination)
    finally:
        destination.close()
    snapshots = sorted(path for path in backup_dir.iterdir() if pattern.fullmatch(path.name))
    for stale in snapshots[:-keep] if keep > 0 else []:
        stale.unlink(missing_ok=True)
    return target


def _deadline_passed(job: sqlite3.Row, today: str) -> bool:
    # Imported lazily: pipeline.py stays runnable without the product package
    # on the path for every other command.
    from opportunity_app.opportunity_metadata import extract_deadline

    deadline = extract_deadline("\n".join((job["title"], job["location"], job["description"])))
    # The deadline day itself is still open, so only strictly earlier dates count.
    return bool(deadline) and deadline[:10] < today


def purge_expired(
    conn: sqlite3.Connection,
    today: str | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Delete retired postings and postings whose stated deadline has passed.

    `active=0` is set by a source batch that no longer lists the posting or by
    a liveness verdict of `expired`. Unlike those, this is a hard delete: if a
    source lists the posting again it comes back as a new row.
    """
    today = today or datetime.now(timezone.utc).date().isoformat()
    rows = conn.execute(
        "SELECT id, company, title, location, description, status, active FROM jobs"
    ).fetchall()
    tally = {"retired": 0, "past_deadline": 0, "kept": 0, "deleted": 0}
    doomed: list[str] = []
    for row in rows:
        if not row["active"]:
            reason = "retired"
        elif _deadline_passed(row, today):
            reason = "past_deadline"
        else:
            continue
        tally[reason] += 1
        if row["status"] in PURGE_PROTECTED_STATUSES:
            tally["kept"] += 1
            continue
        doomed.append(row["id"])

    if dry_run:
        print(
            f"Would delete {len(doomed)} posting(s): {tally['retired']} retired, "
            f"{tally['past_deadline']} past deadline, {tally['kept']} kept for applications"
        )
        return tally

    if doomed:
        # Deletion is permanent and a retirement can come from one bad fetch or
        # liveness verdict, so snapshot first. A failed backup raises and
        # nothing is deleted.
        conn.commit()
        backup = backup_sqlite(conn, "pipeline")
        if backup:
            print(f"Backed up to {backup}")
    for start in range(0, len(doomed), 500):
        chunk = doomed[start : start + 500]
        placeholders = ",".join("?" for _ in chunk)
        conn.execute(f"DELETE FROM jobs WHERE id IN ({placeholders})", chunk)
    tally["deleted"] = len(doomed)
    if doomed:
        # A deleted row may have been a canonical other rows pointed at.
        deduplicate(conn)
    conn.commit()
    print(
        f"Deleted {tally['deleted']} posting(s): {tally['retired']} retired, "
        f"{tally['past_deadline']} past deadline, {tally['kept']} kept for applications"
    )
    return tally


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


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _succeeded_since(conn: sqlite3.Connection, since: datetime) -> set[str]:
    """Source keys with a successful fetch that started at or after `since`."""
    succeeded = set()
    for row in conn.execute("SELECT source_key, started_at FROM fetch_runs WHERE outcome='success'"):
        try:
            if _parse_utc(row["started_at"]) >= since:
                succeeded.add(row["source_key"])
        except ValueError:
            continue
    return succeeded


def fetch_all(
    conn: sqlite3.Connection,
    sources_config: dict[str, Any],
    resume_since: str | None = None,
    *,
    max_workers: int = FETCH_MAX_WORKERS,
    max_per_host: int = FETCH_MAX_PER_HOST,
) -> int:
    """Fetch every enabled source; return how many failed transiently.

    Sources are fetched concurrently, but never more than `max_per_host` at a
    time against one hostname -- 34 of the enabled sources share Greenhouse's
    API host and 21 share Ashby's, so an unbounded pool would hammer two
    servers. The scheduler below submits a source only when both a global and a
    host slot are free; blocking a worker on a semaphore instead would let
    queued Greenhouse work occupy the pool and starve every other host.

    Only worker threads do network I/O. Every database write happens here, on
    the calling thread, so the single sqlite connection is never shared.

    With `resume_since`, sources that already succeeded since that moment are
    skipped, so a run interrupted by sleep or shutdown picks up where it
    stopped instead of refetching everything.
    """
    terms = sources_config["discovery_title_terms"]
    enabled = [source for source in sources_config["ats_sources"] if source.get("enabled", True)]
    done = _succeeded_since(conn, _parse_utc(resume_since)) if resume_since else set()
    if done:
        print(f"Resuming: {len(done)} source(s) already fetched in this run", flush=True)

    queue = []
    for source in enabled:
        key = source_key(source)
        if key not in done:
            queue.append((source, key))
    # One observation timestamp for the whole cycle. See upsert_jobs: reading
    # the clock per source would make undated postings rank by completion
    # order, which under concurrency is arbitrary.
    cycle_seen = now_iso()
    transient_failures = 0
    host_active: dict[str, int] = {}
    in_flight: dict[Any, tuple[dict[str, Any], str, int, str]] = {}

    def record_outcome(
        run_id: int, outcome: str, *, count: int = 0, listed: int | None = None, error: str = ""
    ) -> None:
        """Write a source's terminal state.

        Raises FatalDatabaseError if even this cannot be written: at that point
        the run can no longer report honestly and must stop rather than finish
        looking complete.
        """
        try:
            if outcome == "success":
                conn.execute(
                    "UPDATE fetch_runs SET finished_at=?, outcome='success', fetched_count=?, "
                    "listed_count=? WHERE id=?",
                    (now_iso(), count, listed, run_id),
                )
            else:
                conn.execute(
                    "UPDATE fetch_runs SET finished_at=?, outcome='error', error=? WHERE id=?",
                    (now_iso(), error[:1000], run_id),
                )
            conn.commit()
        except sqlite3.Error as exc:
            raise FatalDatabaseError(f"cannot record fetch outcome: {exc}") from exc

    pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="fetch")
    try:
        while queue or in_flight:
                # Submit everything that currently fits under both ceilings. The
                # queue is scanned rather than popped in order, so a host at its
                # limit does not block sources behind it that could run now.
                index = 0
                while index < len(queue) and len(in_flight) < max_workers:
                    source, run_key = queue[index]
                    host = _source_host(source)
                    if host_active.get(host, 0) >= max_per_host:
                        index += 1
                        continue
                    queue.pop(index)
                    try:
                        run_id = conn.execute(
                            "INSERT INTO fetch_runs(source_key, started_at, outcome) VALUES (?, ?, 'running')",
                            (run_key, now_iso()),
                        ).lastrowid
                        conn.commit()
                    except sqlite3.Error as exc:
                        raise FatalDatabaseError(f"cannot open a fetch run: {exc}") from exc
                    # Printed immediately before submitting, so this line agrees
                    # with started_at and an interrupted run leaves a visible
                    # `running` row for the source that was in flight.
                    print(f"Fetching {source['company']} ({source['kind']})…", flush=True)
                    future = pool.submit(_fetch_source, source, terms)
                    in_flight[future] = (source, run_key, run_id, host)
                    host_active[host] = host_active.get(host, 0) + 1

                if not in_flight:
                    # Every remaining source sits behind a host limit while nothing
                    # is running. Only reachable with a limit of zero, which would
                    # otherwise spin here forever.
                    raise ValueError("fetch concurrency limits admit no work")

                finished, _ = futures_wait(in_flight, return_when=FIRST_COMPLETED)
                for future in finished:
                    source, run_key, run_id, host = in_flight.pop(future)
                    host_active[host] -= 1
                    label = f"{source['company']} ({source['kind']})"
                    try:
                        records = future.result()
                    except Exception as exc:  # Keep other sources useful if one employer is down.
                        record_outcome(run_id, "error", error=str(exc))
                        if isinstance(exc, TransientFetchError):
                            transient_failures += 1
                        print(f"  ERROR {label}: {exc}", file=sys.stderr, flush=True)
                        print(f"  Done {label}: failed", flush=True)
                        continue
                    try:
                        # The duplicate_of pass stays inside each source's upsert:
                        # a failure in it rolls that source back (inserts,
                        # retirements and links together) and records an error,
                        # which a single pass after the last source could not do
                        # once earlier sources were marked successful.
                        count = upsert_jobs(
                            conn, run_key, source.get("label", source["company"]), records, cycle_seen
                        )
                    except FatalDatabaseError:
                        raise
                    except Exception as exc:
                        # Roll the partial batch back before recording the failure.
                        # Without this, committing the error row also commits
                        # however many postings landed before the error.
                        try:
                            conn.rollback()
                        except Exception as rollback_exc:
                            raise FatalDatabaseError(
                                f"cannot roll back a failed source: {rollback_exc}"
                            ) from exc
                        record_outcome(run_id, "error", error=str(exc))
                        print(f"  ERROR {label}: {exc}", file=sys.stderr, flush=True)
                        print(f"  Done {label}: failed", flush=True)
                        continue
                    record_outcome(run_id, "success", count=count, listed=getattr(records, "listed", None))
                    print(f"  Done {label}: {count} candidate postings saved", flush=True)
    finally:
        # A fatal database error must not wait on eleven other sources first.
        # Queued work is dropped immediately; anything already inside a socket
        # read still finishes that request, which Python cannot interrupt.
        for pending_future in in_flight:
            pending_future.cancel()
        pool.shutdown(wait=True, cancel_futures=True)
    return transient_failures


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


def stale_label(last_seen_at: str, stale_after_days: int) -> str:
    last_seen = parse_datetime(last_seen_at)
    if not last_seen:
        return "unknown"
    age = (datetime.now(timezone.utc) - last_seen.astimezone(timezone.utc)).days
    return f"{age}d since checked" + (" — STALE" if age > stale_after_days else "")


def display_reasons(reasons: list[str], limit: int = 5) -> list[str]:
    return [reason for reason in reasons if reason != "35 base"][:limit]


def cap_per_company(
    ranked: list[sqlite3.Row], limit: int, per_company: int = RANKED_VIEW_PER_COMPANY
) -> tuple[list[sqlite3.Row], dict[str, int]]:
    """The first `limit` of `ranked`, keeping each employer's top `per_company`.

    Also returns, per employer that reached the cap, how many of its postings
    were left out, keyed by `sort_key`. An employer cut short by `limit`
    rather than the cap is not in it: those postings did not rank high enough,
    which the shortlist's length already says.
    """

    totals: dict[str, int] = {}
    for job in ranked:
        key = sort_key(job["company"])
        totals[key] = totals.get(key, 0) + 1
    shown: dict[str, int] = {}
    kept: list[sqlite3.Row] = []
    for job in ranked:
        if len(kept) >= limit:
            break
        key = sort_key(job["company"])
        if shown.get(key, 0) >= per_company:
            continue
        shown[key] = shown.get(key, 0) + 1
        kept.append(job)
    hidden = {
        key: totals[key] - count
        for key, count in shown.items()
        if count >= per_company and totals[key] > count
    }
    return kept, hidden


def report(conn: sqlite3.Connection, sources_config: dict[str, Any], limit: int) -> int:
    stale_days = int(sources_config.get("stale_after_days", 7))
    ranked = conn.execute(
        """
        SELECT * FROM jobs
        WHERE active=1 AND duplicate_of IS NULL AND status NOT IN ('rejected', 'withdrawn')
        ORDER BY score DESC, COALESCE(posted_at, last_seen_at) DESC
        """
    ).fetchall()
    # The CSV is the uncapped export; the Markdown shortlist is read top to
    # bottom, so one employer may fill at most its per-employer share of it.
    jobs = ranked[:limit]
    shortlist, hidden = cap_per_company(ranked, limit)
    generated = now_iso()
    lines = [
        "# Opportunity shortlist",
        "",
        f"Generated `{generated}` from source data. Scores are ranking hints, not facts.",
        "",
        "## Top matches",
        "",
    ]
    if not shortlist:
        lines.append("No active postings yet. Run `python3 pipeline.py run` or import login-only results.")
    shown: dict[str, int] = {}
    for index, job in enumerate(shortlist, start=1):
        reasons = json.loads(job["score_explanation"])
        reasons_for_display = display_reasons(reasons)
        lines.extend(
            [
                f"### {index}. [{job['title']}]({job['url']}) — {job['company']} ({job['score']}/100)",
                "",
                f"- Location: {job['location'] or 'not provided'}",
                f"- Type: {job['role_type']} · Status: {job['status']}",
                f"- Source: {job['source_name']} · Freshness: {stale_label(job['last_seen_at'], stale_days)}",
                f"- Why ranked here: {'; '.join(reasons_for_display) or 'base score only'}",
                f"- Pipeline ID: `{job['id']}`",
                "",
            ]
        )
        key = sort_key(job["company"])
        shown[key] = shown.get(key, 0) + 1
        if key in hidden and shown[key] == RANKED_VIEW_PER_COMPANY:
            # Said at the employer's last listed posting, never left silent.
            lines.extend(
                [
                    f"*+{hidden[key]} more from {job['company']}, not listed here: the shortlist "
                    f"shows each employer's top {RANKED_VIEW_PER_COMPANY}. The web dashboard's "
                    f"\"+{hidden[key]} more\" button on this employer lists them all.*",
                    "",
                ]
            )
    lines.extend(["## Manual check queue", ""])
    for item in sources_config.get("manual_check_sources", []):
        cadence = item.get("cadence", "weekly")
        lines.append(f"- [{item['name']}]({item['url']}) — {cadence}; {item.get('note', '')}".rstrip())
    lines.extend(
        [
            "",
            "## Next actions",
            "",
            "1. Open the top roles and verify eligibility/deadline at the source.",
            "2. Mark a role: `python3 pipeline.py update <ID> shortlisted`.",
            "3. Add login-only finds to `data/manual_jobs.csv`, then rerun the pipeline.",
            "4. Never treat an aggregator copy as authoritative; apply on the employer or university page.",
            "",
        ]
    )
    paths.OUTPUT_MD.parent.mkdir(parents=True, exist_ok=True)
    paths.OUTPUT_MD.write_text("\n".join(lines), encoding="utf-8")

    with paths.OUTPUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["id", "score", "status", "company", "title", "location", "role_type", "url", "source", "last_seen_at"]
        )
        for job in jobs:
            writer.writerow(
                [
                    job["id"],
                    job["score"],
                    job["status"],
                    job["company"],
                    job["title"],
                    job["location"],
                    job["role_type"],
                    job["url"],
                    job["source_name"],
                    job["last_seen_at"],
                ]
            )
    print(
        f"Wrote {len(shortlist)} matches to {paths.OUTPUT_MD.relative_to(paths.ROOT)} "
        f"(top {RANKED_VIEW_PER_COMPANY} per employer) and {len(jobs)} to {paths.OUTPUT_CSV.relative_to(paths.ROOT)}"
    )
    return len(jobs)


_DASHBOARD_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Internship Opportunity Dashboard</title>
<style>
  :root {
    color-scheme: light;
    --surface: #fcfcfb;
    --plane: #f9f9f7;
    --ink: #0b0b0b;
    --ink-2: #52514e;
    --ink-muted: #898781;
    --hairline: #e1e0d9;
    --rule: #c3c2b7;
    --ring: rgba(11, 11, 11, 0.10);
    --wash: rgba(11, 11, 11, 0.03);
    /* Categorical slots 1-3 of the validated palette; region is identity, not
       magnitude, so each region keeps its hue no matter how the table is
       filtered or sorted. Every chip also carries its text label, which is the
       relief for aqua sitting under 3:1 on the light surface. */
    --region-1: #2a78d6;
    --region-2: #eb6834;
    --region-3: #1baf7a;
    --region-0: #898781;
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) {
      color-scheme: dark;
      --surface: #1a1a19;
      --plane: #0d0d0d;
      --ink: #ffffff;
      --ink-2: #c3c2b7;
      --ink-muted: #898781;
      --hairline: #2c2c2a;
      --rule: #383835;
      --ring: rgba(255, 255, 255, 0.10);
      --wash: rgba(255, 255, 255, 0.04);
      --region-1: #3987e5;
      --region-2: #d95926;
      --region-3: #199e70;
      --region-0: #898781;
    }
  }
  * { box-sizing: border-box; }
  body {
    font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    margin: 0; padding: 2.5rem 1.5rem 4rem; max-width: 1180px; margin-inline: auto;
    background: var(--plane); color: var(--ink);
    -webkit-font-smoothing: antialiased;
  }
  h1 { font-size: 1.5rem; font-weight: 620; letter-spacing: -0.015em; margin: 0 0 0.3rem; }
  .meta { color: var(--ink-muted); font-size: 0.82rem; margin: 0 0 1.75rem; }

  /* Stat tiles: the headline numbers, proportional figures per the type rule. */
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 0.75rem; margin-bottom: 1.75rem; }
  .tile { background: var(--surface); border: 1px solid var(--ring); border-radius: 12px; padding: 0.9rem 1rem; }
  .tile-value { font-size: 1.75rem; font-weight: 600; letter-spacing: -0.02em; line-height: 1.1; }
  .tile-label { font-size: 0.75rem; color: var(--ink-muted); margin-top: 0.2rem; }

  .controls { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-bottom: 0.9rem; }
  .controls select, .controls input {
    padding: 0.45rem 0.6rem; font: inherit; font-size: 0.85rem;
    background: var(--surface); color: var(--ink);
    border: 1px solid var(--ring); border-radius: 8px;
  }
  .controls input { flex: 1 1 220px; min-width: 180px; }
  .controls select:focus-visible, .controls input:focus-visible { outline: 2px solid var(--region-1); outline-offset: 1px; }
  .count { font-size: 0.8rem; color: var(--ink-muted); margin: 0 0 0.75rem; }

  .table-wrap { background: var(--surface); border: 1px solid var(--ring); border-radius: 12px; overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 0.88rem; }
  thead th {
    text-align: left; font-size: 0.72rem; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.06em; color: var(--ink-muted);
    padding: 0.7rem 0.9rem; border-bottom: 1px solid var(--rule); white-space: nowrap;
  }
  tbody td { padding: 0.8rem 0.9rem; border-bottom: 1px solid var(--hairline); vertical-align: top; }
  tbody tr:last-child td { border-bottom: none; }
  tbody tr:hover { background: var(--wash); }
  tr.is-new td:first-child { box-shadow: inset 2px 0 0 var(--ink); }
  tr.is-read { opacity: 0.55; }

  .job-title { color: var(--ink); font-weight: 550; text-decoration: none; }
  .job-title:hover { text-decoration: underline; }
  .company { color: var(--ink-2); font-size: 0.82rem; margin-top: 0.15rem; }
  .badge {
    display: inline-block; font-size: 0.62rem; font-weight: 700; letter-spacing: 0.05em;
    padding: 0.1rem 0.35rem; border-radius: 4px; margin-left: 0.45rem; vertical-align: 1px;
    background: var(--ink); color: var(--surface);
  }

  /* Score: length carries magnitude, so the meter stays neutral — a hue here
     would read as a fourth region. */
  .score-value { font-weight: 600; font-variant-numeric: tabular-nums; }
  .meter { width: 64px; height: 3px; border-radius: 2px; background: var(--hairline); margin-top: 0.4rem; }
  .meter-fill { height: 100%; border-radius: 2px; background: var(--ink-2); }

  .chip { display: inline-flex; align-items: center; gap: 0.35rem; font-size: 0.75rem; color: var(--ink-2); margin-top: 0.25rem; }
  .chip::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: var(--chip-color, var(--region-0)); flex: none; }
  .region-1 { --chip-color: var(--region-1); }
  .region-2 { --chip-color: var(--region-2); }
  .region-3 { --chip-color: var(--region-3); }
  .region-0 { --chip-color: var(--region-0); }
  .loc { color: var(--ink); }
  .muted { color: var(--ink-muted); }

  details { margin-top: 0.4rem; }
  details summary { cursor: pointer; font-size: 0.76rem; color: var(--ink-muted); }
  details p { font-size: 0.78rem; color: var(--ink-2); margin: 0.35rem 0 0; line-height: 1.45; }
</style>
</head>
<body>
<h1>Internship Opportunity Dashboard</h1>
<p class="meta">Generated GENERATED_AT_PLACEHOLDER &middot; Scores are ranking hints, not facts.</p>
<div class="tiles">
  <div class="tile"><div class="tile-value" id="tile-total">0</div><div class="tile-label">Opportunities shown</div></div>
  <div class="tile"><div class="tile-value" id="tile-new">0</div><div class="tile-label">New since last visit</div></div>
  <div class="tile"><div class="tile-value" id="tile-region">0</div><div class="tile-label">In target regions</div></div>
  <div class="tile"><div class="tile-value" id="tile-top">0</div><div class="tile-label">Top score</div></div>
</div>
<div class="controls">
  <input type="text" id="search" placeholder="Search title or company">
  <select id="sort">
    <option value="score-desc">Score (high to low)</option>
    <option value="score-asc">Score (low to high)</option>
    <option value="company">Company (A-Z)</option>
    <option value="posted">Most recently posted</option>
    <option value="discovered">Most recently discovered</option>
  </select>
  <select id="filter-region"><option value="">All regions</option></select>
  <select id="filter-role"><option value="">All role types</option></select>
  <select id="filter-status"><option value="">All statuses</option></select>
  <select id="filter-source"><option value="">All sources</option></select>
</div>
<p class="count" id="count"></p>
<div class="table-wrap">
<table>
  <thead>
    <tr><th>Title / Company</th><th>Score</th><th>Location</th><th>Type</th><th>Status</th><th>Source</th></tr>
  </thead>
  <tbody id="rows"></tbody>
</table>
</div>
<script id="job-data" type="application/json">JOB_DATA_PLACEHOLDER</script>
<script>
(function () {
  var LAST_OPENED_KEY = 'internship_dashboard_last_opened_at';
  var READ_PREFIX = 'internship_dashboard_read:';
  var jobs = JSON.parse(document.getElementById('job-data').textContent);

  // Some browsers (and strict configurations, e.g. Safari privacy settings)
  // treat file:// pages as an opaque origin and throw on any localStorage
  // access rather than just being absent. Fall back to an in-memory store
  // so the dashboard still renders — new/read just won't persist there.
  var memoryStore = {};
  var storageAvailable = true;
  try {
    var probeKey = '__internship_dashboard_probe__';
    window.localStorage.setItem(probeKey, '1');
    window.localStorage.removeItem(probeKey);
  } catch (err) {
    storageAvailable = false;
  }
  var safeStorage = {
    getItem: function (key) {
      if (storageAvailable) {
        try { return window.localStorage.getItem(key); } catch (err) { /* fall through */ }
      }
      return Object.prototype.hasOwnProperty.call(memoryStore, key) ? memoryStore[key] : null;
    },
    setItem: function (key, value) {
      if (storageAvailable) {
        try { window.localStorage.setItem(key, value); return; } catch (err) { /* fall through */ }
      }
      memoryStore[key] = value;
    },
  };
  if (!storageAvailable) {
    console.warn('Dashboard: localStorage unavailable on this origin; new/read status will not persist across reloads.');
  }

  var storedLastOpened = safeStorage.getItem(LAST_OPENED_KEY);
  var isFirstEverOpen = storedLastOpened === null;
  var lastOpenedAt = storedLastOpened ? new Date(storedLastOpened) : null;

  jobs.forEach(function (job) {
    job.isNew = !isFirstEverOpen && lastOpenedAt !== null && !!job.first_seen_at
      && new Date(job.first_seen_at) > lastOpenedAt;
    job.isRead = safeStorage.getItem(READ_PREFIX + job.id) === '1';
  });
  safeStorage.setItem(LAST_OPENED_KEY, new Date().toISOString());

  var roleSelect = document.getElementById('filter-role');
  var statusSelect = document.getElementById('filter-status');
  var sourceSelect = document.getElementById('filter-source');
  var regionSelect = document.getElementById('filter-region');

  function uniqueSorted(values) {
    return Array.from(new Set(values.filter(Boolean))).sort();
  }
  function populate(select, values) {
    uniqueSorted(values).forEach(function (value) {
      var opt = document.createElement('option');
      opt.value = value;
      opt.textContent = value;
      select.appendChild(opt);
    });
  }
  populate(roleSelect, jobs.map(function (j) { return j.role_type; }));
  populate(statusSelect, jobs.map(function (j) { return j.status; }));
  populate(sourceSelect, jobs.map(function (j) { return j.source_name; }));
  populate(regionSelect, jobs.map(function (j) { return j.region; }));

  // Colour follows the region, never its rank: the slot is fixed once from the
  // full job set, so filtering the table never repaints the survivors. Past the
  // three validated slots regions fold into the muted slot rather than cycling
  // hues, which would put two indistinguishable colours on screen.
  var SLOTS = ['region-1', 'region-2', 'region-3'];
  var RESERVED_REGIONS = { 'Other': 1, 'Unknown': 1, 'Remote': 1 };
  var REGION_CLASS = { 'Other': 'region-0', 'Unknown': 'region-0' };
  (function assignRegionSlots() {
    var targets = uniqueSorted(jobs.map(function (j) { return j.region; }))
      .filter(function (name) { return !RESERVED_REGIONS[name]; });
    targets.forEach(function (name, i) {
      REGION_CLASS[name] = i < SLOTS.length ? SLOTS[i] : 'region-0';
    });
    REGION_CLASS.Remote = targets.length < SLOTS.length ? SLOTS[targets.length] : 'region-0';
  })();

  // Meter length is relative to the best score on the board, so the bars stay
  // comparable to each other rather than to an arbitrary ceiling.
  var meterMax = jobs.reduce(function (max, job) { return Math.max(max, job.score); }, 1);

  var rowsEl = document.getElementById('rows');
  var countEl = document.getElementById('count');
  var searchEl = document.getElementById('search');
  var sortEl = document.getElementById('sort');

  function sortJobs(list) {
    var mode = sortEl.value;
    var sorted = list.slice();
    if (mode === 'score-desc') {
      sorted.sort(function (a, b) { return b.score - a.score; });
    } else if (mode === 'score-asc') {
      sorted.sort(function (a, b) { return a.score - b.score; });
    } else if (mode === 'company') {
      sorted.sort(function (a, b) { return a.company.localeCompare(b.company); });
    } else if (mode === 'posted') {
      sorted.sort(function (a, b) {
        return new Date(b.posted_at || b.last_seen_at) - new Date(a.posted_at || a.last_seen_at);
      });
    } else if (mode === 'discovered') {
      sorted.sort(function (a, b) { return new Date(b.first_seen_at) - new Date(a.first_seen_at); });
    }
    return sorted;
  }

  function markRead(job) {
    job.isRead = true;
    safeStorage.setItem(READ_PREFIX + job.id, '1');
  }

  function render() {
    var query = searchEl.value.trim().toLowerCase();
    var role = roleSelect.value;
    var status = statusSelect.value;
    var source = sourceSelect.value;

    var region = regionSelect.value;

    var filtered = jobs.filter(function (job) {
      if (role && job.role_type !== role) return false;
      if (status && job.status !== status) return false;
      if (source && job.source_name !== source) return false;
      if (region && job.region !== region) return false;
      if (query) {
        var haystack = (job.title + ' ' + job.company).toLowerCase();
        if (haystack.indexOf(query) === -1) return false;
      }
      return true;
    });
    filtered = sortJobs(filtered);

    rowsEl.textContent = '';
    filtered.forEach(function (job) {
      var tr = document.createElement('tr');
      tr.className = (job.isNew ? 'is-new ' : '') + (job.isRead ? 'is-read' : '');

      var titleTd = document.createElement('td');
      var link = document.createElement('a');
      link.href = job.url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.className = 'job-title';
      link.textContent = job.title;
      link.addEventListener('click', function () { markRead(job); tr.classList.add('is-read'); });
      titleTd.appendChild(link);
      if (job.isNew) {
        var badge = document.createElement('span');
        badge.className = 'badge badge-new';
        badge.textContent = 'NEW';
        titleTd.appendChild(badge);
      }
      var companyDiv = document.createElement('div');
      companyDiv.className = 'company';
      companyDiv.textContent = job.company;
      titleTd.appendChild(companyDiv);

      if (job.reasons && job.reasons.length) {
        var details = document.createElement('details');
        var summary = document.createElement('summary');
        summary.textContent = 'Why ranked here';
        details.appendChild(summary);
        var reasonsP = document.createElement('p');
        reasonsP.textContent = job.reasons.join('; ');
        details.appendChild(reasonsP);
        titleTd.appendChild(details);
      }
      tr.appendChild(titleTd);

      var scoreTd = document.createElement('td');
      var scoreValue = document.createElement('div');
      scoreValue.className = 'score-value';
      scoreValue.textContent = job.score;
      scoreTd.appendChild(scoreValue);
      var meter = document.createElement('div');
      meter.className = 'meter';
      var meterFill = document.createElement('div');
      meterFill.className = 'meter-fill';
      meterFill.style.width = Math.round((Math.max(0, job.score) / meterMax) * 100) + '%';
      meter.appendChild(meterFill);
      scoreTd.appendChild(meter);
      tr.appendChild(scoreTd);

      var locationTd = document.createElement('td');
      var locationLine = document.createElement('div');
      locationLine.className = job.location ? 'loc' : 'muted';
      locationLine.textContent = job.location || 'not provided';
      locationTd.appendChild(locationLine);
      if (job.region) {
        var chip = document.createElement('span');
        chip.className = 'chip ' + (REGION_CLASS[job.region] || 'region-0');
        chip.textContent = job.region;
        locationTd.appendChild(chip);
      }
      tr.appendChild(locationTd);

      var typeTd = document.createElement('td');
      typeTd.textContent = job.role_type;
      tr.appendChild(typeTd);

      var statusTd = document.createElement('td');
      statusTd.textContent = job.status;
      tr.appendChild(statusTd);

      var sourceTd = document.createElement('td');
      sourceTd.textContent = job.source_name + ' · ' + job.freshness;
      tr.appendChild(sourceTd);

      rowsEl.appendChild(tr);
    });

    countEl.textContent = filtered.length + ' of ' + jobs.length + ' opportunities shown';

    var inRegion = filtered.filter(function (job) {
      return job.region && !RESERVED_REGIONS[job.region];
    }).length;
    var topScore = filtered.reduce(function (max, job) { return Math.max(max, job.score); }, -Infinity);
    document.getElementById('tile-total').textContent = filtered.length;
    document.getElementById('tile-new').textContent = filtered.filter(function (job) { return job.isNew; }).length;
    document.getElementById('tile-region').textContent = inRegion;
    document.getElementById('tile-top').textContent = filtered.length ? topScore : '—';
  }

  searchEl.addEventListener('input', render);
  sortEl.addEventListener('change', render);
  roleSelect.addEventListener('change', render);
  statusSelect.addEventListener('change', render);
  sourceSelect.addEventListener('change', render);
  regionSelect.addEventListener('change', render);

  render();
})();
</script>
</body>
</html>
"""


def build_dashboard_html(jobs: list[dict[str, Any]], generated_at: str) -> str:
    # Escaping "</" prevents a job title/description containing "</script>"
    # from breaking out of the embedded JSON data block.
    payload = json.dumps(jobs).replace("</", "<\\/")
    doc = _DASHBOARD_HTML_TEMPLATE.replace("GENERATED_AT_PLACEHOLDER", html.escape(generated_at))
    doc = doc.replace("JOB_DATA_PLACEHOLDER", payload)
    return doc


def render_dashboard(
    conn: sqlite3.Connection,
    sources_config: dict[str, Any],
    dashboard_limit: int,
    profile: dict[str, Any] | None = None,
) -> int:
    stale_days = int(sources_config.get("stale_after_days", 7))
    rows = conn.execute(
        """
        SELECT * FROM jobs
        WHERE active=1 AND duplicate_of IS NULL
        ORDER BY score DESC, COALESCE(posted_at, last_seen_at) DESC
        LIMIT ?
        """,
        (dashboard_limit,),
    ).fetchall()
    payload = [
        {
            "id": row["id"],
            "title": row["title"],
            "company": row["company"],
            "location": row["location"],
            "region": region_label(row["location"], profile or {}),
            "role_type": row["role_type"],
            "status": row["status"],
            "score": row["score"],
            "reasons": display_reasons(json.loads(row["score_explanation"])),
            "source_name": row["source_name"],
            "url": row["url"],
            "first_seen_at": row["first_seen_at"],
            "last_seen_at": row["last_seen_at"],
            "posted_at": row["posted_at"],
            "freshness": stale_label(row["last_seen_at"], stale_days),
        }
        for row in rows
    ]
    paths.OUTPUT_DASHBOARD.parent.mkdir(parents=True, exist_ok=True)
    paths.OUTPUT_DASHBOARD.write_text(build_dashboard_html(payload, now_iso()), encoding="utf-8")
    print(f"Wrote {len(payload)} opportunities to {paths.OUTPUT_DASHBOARD.relative_to(paths.ROOT)}")
    return len(payload)


def update_status(
    conn: sqlite3.Connection,
    job_id: str,
    status: str,
    notes: str | None,
    follow_up: str | None,
) -> None:
    if status not in VALID_STATUSES:
        raise SystemExit(f"Invalid status. Choose one of: {', '.join(sorted(VALID_STATUSES))}")
    existing = conn.execute("SELECT id FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not existing:
        raise SystemExit(f"No job found with ID {job_id}")
    applied_at = now_iso() if status == "applied" else None
    conn.execute(
        """
        UPDATE jobs
        SET status=?,
            notes=COALESCE(?, notes),
            follow_up_at=COALESCE(?, follow_up_at),
            applied_at=CASE WHEN ?='applied' THEN COALESCE(applied_at, ?) ELSE applied_at END
        WHERE id=?
        """,
        (status, notes, follow_up, status, applied_at, job_id),
    )
    conn.commit()
    # Plain ASCII arrow: the Windows console defaults to cp1252, which has no
    # mapping for U+2192, so an arrow here crashed `update` with a
    # UnicodeEncodeError before it could print the confirmation.
    print(f"Updated {job_id} -> {status}")


def show_status(conn: sqlite3.Connection) -> None:
    totals = conn.execute(
        "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status ORDER BY count DESC"
    ).fetchall()
    active = conn.execute("SELECT COUNT(*) FROM jobs WHERE active=1 AND duplicate_of IS NULL").fetchone()[0]
    print(f"{active} active unique postings")
    for row in totals:
        print(f"  {row['status']}: {row['count']}")
    errors = conn.execute(
        """
        SELECT run.source_key, run.finished_at, run.error
        FROM fetch_runs AS run
        JOIN (
            SELECT source_key, MAX(id) AS latest_id
            FROM fetch_runs
            GROUP BY source_key
        ) AS latest ON latest.latest_id=run.id
        WHERE run.outcome='error'
        ORDER BY run.id DESC
        """
    ).fetchall()
    if errors:
        print("Recent source errors:")
        for row in errors:
            print(f"  {row['source_key']} at {row['finished_at']}: {row['error']}")


def doctor(profile: dict[str, Any], sources_config: dict[str, Any]) -> int:
    exit_code = 0
    missing: list[str] = []
    for key in (
        "graduation_year",
        "preferred_locations",
        "regions",
        "skills",
        "interest_keywords",
        "work_authorized_us",
        "requires_sponsorship",
        "hours_per_week",
        "available_terms",
        "compensation_preferences",
    ):
        value = profile.get(key)
        # preferred_locations is only the fallback for a profile with no regions.
        if key == "preferred_locations" and profile.get("regions"):
            continue
        if value is None or value == []:
            missing.append(key)
    if missing:
        print("Profile is usable but incomplete:")
        for key in missing:
            print(f"  - {key}")
        print("Edit config/profile.json, or ask your agent to follow SETUP.md.")
        exit_code = 1

    usajobs_sources = [
        source
        for source in sources_config.get("ats_sources", [])
        if source.get("kind") == "usajobs" and source.get("enabled", True)
    ]
    if usajobs_sources and not os.environ.get("USAJOBS_API_KEY"):
        print("USAJOBS source is enabled but USAJOBS_API_KEY is not set.")
        print("Register a free key at https://developer.usajobs.gov/, then copy .env.example")
        print("to .env and fill it in (.env is gitignored).")
        exit_code = 1
    # The API rejects requests whose User-Agent is not the address the key was
    # registered under, so a missing email fails just as hard as a missing key.
    if usajobs_sources and not os.environ.get("USAJOBS_CONTACT_EMAIL"):
        if any(not source.get("contact_email") for source in usajobs_sources):
            print("USAJOBS source is enabled but no contact email is set.")
            print("Set USAJOBS_CONTACT_EMAIL in .env to the address the key was registered with.")
            exit_code = 1

    adzuna_sources = [
        source
        for source in sources_config.get("ats_sources", [])
        if source.get("kind") == "adzuna" and source.get("enabled", True)
    ]
    # Adzuna issues the pair together and rejects a request missing either half,
    # so both are reported rather than only the first one found missing.
    adzuna_missing = [
        name for name in ("ADZUNA_APP_ID", "ADZUNA_APP_KEY") if not os.environ.get(name)
    ]
    if adzuna_sources and adzuna_missing:
        print(f"Adzuna source is enabled but {' and '.join(adzuna_missing)} not set.")
        print("Register a free application at https://developer.adzuna.com/, then copy")
        print(".env.example to .env and fill both values in (.env is gitignored).")
        exit_code = 1

    if exit_code == 0:
        print("Profile has all high-impact fields.")
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    resume_help = "Skip sources already fetched successfully since this ISO-8601 time"
    fetch_parser = sub.add_parser("fetch", help="Fetch enabled public ATS sources")
    fetch_parser.add_argument("--resume-since", help=resume_help)
    import_parser = sub.add_parser("import-csv", help="Import login-only or manually found postings")
    import_parser.add_argument("path", nargs="?", default=str(paths.MANUAL_PATH))
    import_email_parser = sub.add_parser(
        "import-emails", help="Import LinkedIn job-alert email JSON (see README)"
    )
    import_email_parser.add_argument("path", nargs="?", default=str(paths.EMAIL_IMPORT_PATH))
    import_discovered_parser = sub.add_parser(
        "import-discovered",
        help="Import agent-discovered postings from search/public pages/lists (see README)",
    )
    import_discovered_parser.add_argument("path", nargs="?", default=str(paths.DISCOVERED_IMPORT_PATH))
    enrich_parser = sub.add_parser(
        "enrich", help="Backfill descriptions an agent read from public posting pages"
    )
    enrich_parser.add_argument("path", nargs="?", default=str(paths.ENRICHMENT_PATH))
    enrich_parser.add_argument(
        "--force",
        action="store_true",
        help="Replace existing descriptions too, not just thin ones",
    )
    sub.add_parser("score", help="Recompute transparent fit scores")
    report_parser = sub.add_parser("report", help="Write Markdown, CSV, and dashboard shortlists")
    report_parser.add_argument("--limit", type=int, default=30)
    report_parser.add_argument("--dashboard-limit", type=int, default=300)
    run_parser = sub.add_parser("run", help="Fetch, import, score, and report")
    run_parser.add_argument("--limit", type=int, default=30)
    run_parser.add_argument("--dashboard-limit", type=int, default=300)
    run_parser.add_argument("--resume-since", help=resume_help)
    update_parser = sub.add_parser("update", help="Update application status")
    update_parser.add_argument("job_id")
    update_parser.add_argument("status")
    update_parser.add_argument("--notes")
    update_parser.add_argument("--follow-up", help="ISO date, e.g. 2026-08-05")
    resume_parser = sub.add_parser(
        "resume", help="Render your resume, optionally emphasised for one posting"
    )
    resume_parser.add_argument("--job", help="Posting ID to tailor emphasis toward")
    resume_parser.add_argument("--pdf", action="store_true", help="Also render a PDF")
    cover_parser = sub.add_parser("cover-letter", help="Draft a cover letter for one posting")
    cover_parser.add_argument("--job", required=True, help="Posting ID (see output/shortlist.md)")
    cover_parser.add_argument("--pdf", action="store_true", help="Also render a PDF")
    discover_parser = sub.add_parser(
        "discover-ats",
        help="Resolve company names to Greenhouse/Ashby/Lever boards (preview by default)",
    )
    discover_parser.add_argument("companies", nargs="*", help="Company names to probe")
    discover_parser.add_argument(
        "--in",
        dest="companies_path",
        help="JSON file holding a list of company names, or {\"companies\": [...]}",
    )
    discover_parser.add_argument(
        "--vendors",
        help=f"Comma-separated subset of {','.join(DISCOVERY_VENDOR_ORDER)}",
    )
    discover_parser.add_argument(
        "--write",
        action="store_true",
        help="Append identity-confirmed entries to config/sources.local.json",
    )
    discover_parser.add_argument(
        "--shared",
        action="store_true",
        help="With --write, append to the tracked shared catalog config/sources.json instead",
    )
    discover_parser.add_argument(
        "--include-unverified",
        action="store_true",
        help="Also write Ashby/Lever hits, whose APIs expose no company name to check",
    )
    liveness_parser = sub.add_parser(
        "liveness",
        help="Check whether imported postings are still open, and retire dead ones",
    )
    liveness_parser.add_argument(
        "--limit", type=int, help="Check at most N postings, least recently seen first"
    )
    liveness_parser.add_argument(
        "--all",
        action="store_true",
        dest="check_all",
        help="Also check ATS-sourced rows, which their own source batch already retires",
    )
    liveness_parser.add_argument(
        "--dry-run", action="store_true", help="Report verdicts without retiring anything"
    )
    purge_parser = sub.add_parser(
        "purge-expired",
        help="Delete retired postings and postings past their stated deadline",
    )
    purge_parser.add_argument(
        "--dry-run", action="store_true", help="Report what would be deleted without deleting"
    )
    sub.add_parser("status", help="Show pipeline counts and recent fetch errors")
    sub.add_parser("doctor", help="Check whether high-impact profile fields are filled")
    return parser


def main() -> int:
    # Company names and job titles come from scraped pages and routinely carry
    # characters the Windows console's cp1252 default cannot encode ("Ørsted",
    # curly quotes, typographic dashes). Printing one raises UnicodeEncodeError
    # mid-command, which would abort a run that was otherwise succeeding. The
    # test suite guards the literals in this file; only this guards the data.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, OSError):
            # Not a real console (piped, captured in tests): nothing to fix.
            pass

    args = build_parser().parse_args()
    load_env_file()
    profile = load_profile()
    sources = load_sources()
    conn = connect()
    try:
        if args.command == "fetch":
            if fetch_all(conn, sources, args.resume_since):
                return EXIT_TEMPFAIL
        elif args.command == "import-csv":
            import_manual(conn, Path(args.path).expanduser().resolve())
        elif args.command == "import-emails":
            import_emails(conn, Path(args.path).expanduser().resolve())
        elif args.command == "import-discovered":
            import_discovered(conn, Path(args.path).expanduser().resolve())
        elif args.command == "enrich":
            enrich_descriptions(conn, Path(args.path).expanduser().resolve(), args.force)
        elif args.command == "score":
            score_all(conn, profile)
        elif args.command == "report":
            report(conn, sources, args.limit)
            render_dashboard(conn, sources, args.dashboard_limit, profile)
        elif args.command == "run":
            transient_failures = fetch_all(conn, sources, args.resume_since)
            # Score and report even when some sources were unreachable, so the
            # shortlist reflects what did arrive; the exit code asks for a retry.
            import_manual(conn, paths.MANUAL_PATH)
            score_all(conn, profile)
            report(conn, sources, args.limit)
            render_dashboard(conn, sources, args.dashboard_limit, profile)
            if transient_failures:
                print(
                    f"{transient_failures} source(s) were unreachable; rerun with "
                    "--resume-since to fetch only what is missing",
                    file=sys.stderr,
                )
                return EXIT_TEMPFAIL
        elif args.command == "update":
            update_status(conn, args.job_id, args.status, args.notes, args.follow_up)
        elif args.command in {"resume", "cover-letter"}:
            write_artifact(conn, args.command, args.job, args.pdf)
        elif args.command == "discover-ats":
            companies = list(args.companies)
            if args.companies_path:
                payload = load_json(Path(args.companies_path).expanduser().resolve())
                listed = payload["companies"] if isinstance(payload, dict) else payload
                companies += [str(name).strip() for name in listed if str(name).strip()]
            if not companies:
                raise SystemExit("No company names given. Pass them as arguments or via --in.")
            report_discovery(
                companies,
                sources,
                args.write,
                args.include_unverified,
                args.vendors.split(",") if args.vendors else None,
                shared=args.shared,
            )
        elif args.command == "liveness":
            check_liveness(conn, args.limit, args.check_all, args.dry_run)
        elif args.command == "purge-expired":
            purge_expired(conn, dry_run=args.dry_run)
        elif args.command == "status":
            show_status(conn)
        elif args.command == "doctor":
            return doctor(profile, sources)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
