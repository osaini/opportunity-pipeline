"""The legacy pipeline database: schema, upserts, retirement of vanished postings and cross-listing dedup."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import paths
from .clock import now_iso, parse_datetime
from .identity import normalized
from .sources import Listing
from .text import canonical_url, classify_role, CROSSLIST_THRESHOLD, fingerprint, fingerprint_text, stable_id


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


class FatalDatabaseError(RuntimeError):
    """The local database itself is unusable -- disk full, file gone, a dead
    connection. Unlike a source failing, this cannot be isolated to one source
    and recorded: the row recording it would fail too. The run stops rather
    than reporting a day that looks complete."""


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
