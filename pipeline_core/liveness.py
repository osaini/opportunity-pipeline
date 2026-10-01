"""Posting liveness: deciding from a fetched page whether a posting is still open."""

from __future__ import annotations

import re
import sqlite3
import sys
import unicodedata
from typing import Any, Iterable

from .clock import now_iso
from .http import request_text
from .store import deduplicate
from .text import apply_controls, strip_html


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
