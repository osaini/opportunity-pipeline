"""Which role is a Greenhouse role, where Greenhouse lives, and the constants that say so.

The one place the Apply for me modules agree on Greenhouse itself: its hosts and domains, the adapter's version, the
URLs a posting and its form listing live at, and how a saved role is recognised as a Greenhouse posting (``identify``).
The plan (apply_policy), the request rules (apply_checks), the schema fetch (apply_schema_client), the run ledger
(apply_runs) and the preflight each read these instead of keeping a copy, so a host or a version changes in one place.

Standard library only, and no import of any other first-party module: the rules in apply_checks import it.
"""

from __future__ import annotations

import re
import sqlite3
from urllib.parse import parse_qs, urlsplit

ATS_GREENHOUSE = "greenhouse"
# The adapter's version (docs/phase5-apply-agent-spec.md 4.4). A rehearsal counts toward the gate only for
# the version the adapter has now, so a change to its selectors or rules means rehearsing again.
ADAPTER_VERSION = "greenhouse-1"

# --- Hosts --------------------------------------------------------------------------------------

# Where a main-frame navigation may go. Never my.greenhouse.io: that is the
# student's own MyGreenhouse account, which the agent never signs in to.
BOARD_HOSTS = frozenset({"job-boards.greenhouse.io", "boards.greenhouse.io"})
# The submit path in the served HTML belongs to this host, not the job-boards one.
SUBMIT_HOST = "boards.greenhouse.io"
GREENHOUSE_DOMAIN = "greenhouse.io"
# Greenhouse's public Job Board API, where a posting's form listing is read.
API_HOST = "boards-api.greenhouse.io"
# Greenhouse's own senders (mail/data/application_senders.json), for a confirmation the reader could not match to a role.
GREENHOUSE_SENDER_DOMAINS = ("greenhouse.io", "greenhouse-mail.io")

# --- Identifying the posting (4.4) --------------------------------------------------------------

_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
_JOB_ID = re.compile(r"^\d+$")
_JOB_PATH = re.compile(r"^/([A-Za-z0-9][A-Za-z0-9_-]{0,79})/jobs/(\d+)/?$")


def canonical_url(board_token: str, job_id: str) -> str:
    return f"https://job-boards.greenhouse.io/{board_token}/jobs/{job_id}"


def schema_url(board_token: str, job_id: str) -> str:
    """Greenhouse's public, keyless listing of what an application form asks (read-only GET)."""
    return f"https://{API_HOST}/v1/boards/{board_token}/jobs/{job_id}?questions=true"


def _from_url(url: str) -> tuple[str, str] | None:
    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError:
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme not in ("http", "https") or host not in BOARD_HOSTS:
        return None
    match = _JOB_PATH.match(parts.path)
    if match:
        return match.group(1), match.group(2)
    if parts.path.rstrip("/") == "/embed/job_app":
        query = parse_qs(parts.query)
        token, job = (query.get("for") or [""])[0], (query.get("token") or [""])[0]
        if _TOKEN.match(token) and _JOB_ID.match(job):
            return token, job
    return None


def identify(conn: sqlite3.Connection, opportunity_id: str) -> tuple[str, str] | None:
    """The Greenhouse (board token, job id) this saved role is, or None when it is not one the app can fill.

    A token parsed from the role's own URL, then from a source URL, wins. Otherwise the source key
    ``greenhouse:<token>`` and the source's external id are used, and the id must be all digits. A company
    site that carries only ``gh_jid`` is not supported.
    """
    row = conn.execute("SELECT url FROM opportunities WHERE id=?", (opportunity_id,)).fetchone()
    if row is None:
        return None
    found = _from_url(row[0])
    if found:
        return found
    sources = conn.execute(
        "SELECT source_url, source_key, external_id FROM opportunity_sources WHERE opportunity_id=? ORDER BY last_seen_at DESC, source_key",
        (opportunity_id,),
    ).fetchall()
    for source in sources:
        found = _from_url(source[0])
        if found:
            return found
    # The pattern is a parameter, not part of the SQL: a literal % breaks on PostgreSQL, where ? becomes %s.
    for source in conn.execute(
        "SELECT source_key, external_id FROM opportunity_sources WHERE opportunity_id=? AND source_key LIKE ? ORDER BY last_seen_at DESC, source_key",
        (opportunity_id, "greenhouse:%"),
    ).fetchall():
        token, job = str(source[0])[len("greenhouse:"):], str(source[1] or "")
        if _TOKEN.match(token) and _JOB_ID.match(job):
            return token, job
    return None
