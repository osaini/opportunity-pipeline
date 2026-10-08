"""Which saved role is a Lever posting, where Lever lives, and the constants that say so.

The Lever twin of ``greenhouse.py`` (docs/phase5-lever-handoff-spec.md, section 5.3): its hosts, the adapter's version, the
address of a posting's application page, and how a saved role is recognised as a Lever posting (``identify``).

Only the pure, read-only half exists so far. The ATS registry entry and the request rules for Lever come with the seam that
lets a second ATS in (spec 5.2), and nothing in the app calls this module yet.

Standard library only, and no import of any other first-party module, like ``greenhouse.py``.
"""

from __future__ import annotations

import re
import sqlite3
from typing import NamedTuple
from urllib.parse import urlsplit

ATS_LEVER = "lever"
# The adapter's version (spec 11, R1). Any change to its selectors or rules means a new value.
ADAPTER_VERSION = "lever-1"

# --- Hosts --------------------------------------------------------------------------------------

# The two hosts a posting's page lives on. The EU host keeps EU postings: a posting never moves to the other one.
LEVER_HOSTS = ("jobs.lever.co", "jobs.eu.lever.co")
DEFAULT_HOST = LEVER_HOSTS[0]
LEVER_DOMAIN = "lever.co"
# Lever's own senders (mail/data/application_senders.json): its applicant confirmation comes from hire.lever.co (spec 3.15).
LEVER_SENDER_DOMAINS = ("hire.lever.co", "lever.co")


def is_lever_sender(domain: str) -> bool:
    """Whether a sender domain is Lever's or a subdomain of it."""
    domain = (domain or "").lower().rstrip(".")
    return any(domain == known or domain.endswith(f".{known}") for known in LEVER_SENDER_DOMAINS)


# --- Identifying the posting (5.3) --------------------------------------------------------------

_SITE = re.compile(r"[A-Za-z0-9_-]{1,100}")
_JOB_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# (matched whole) /{site}/{uuid}, then at most /apply or /thanks, then at most one closing slash. Nothing else follows the uuid.
_JOB_PATH = re.compile(r"/([A-Za-z0-9_-]{1,100})/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:/(?:apply|thanks))?/?")


class LeverRef(NamedTuple):
    """A Lever posting: the company's site (its board name), the posting's uuid and the host it lives on."""

    site: str
    job_id: str
    host: str


def canonical_url(site: str, job_id: str, host: str = DEFAULT_HOST) -> str:
    """The posting's application page, the address the form is read from and the browser opens (spec 3.1, 6.2)."""
    return f"https://{host}/{site}/{job_id}/apply"


def _from_url(url: str) -> LeverRef | None:
    try:
        parts = urlsplit(str(url or "").strip())
        port = parts.port
    except ValueError:
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    # A user name, a password or a port makes it another address that only looks like Lever's.
    if parts.scheme not in ("http", "https") or host not in LEVER_HOSTS or port is not None or parts.username or parts.password:
        return None
    match = _JOB_PATH.fullmatch(parts.path)
    if not match:
        return None
    return LeverRef(match.group(1), match.group(2), host)


def identify(conn: sqlite3.Connection, opportunity_id: str) -> LeverRef | None:
    """The Lever (site, posting uuid, host) this saved role is, or None when it is not one the app can fill.

    The role's own URL is read first, then its source URLs, then its ``lever:<site>`` source rows. The URL decides the
    host, so an EU posting stays on the EU host. A row with only the source key (no URL) has no host to read, so it takes
    the global one. A company site that embeds Lever is a Lever posting only if its URL names ``jobs.lever.co`` or
    ``jobs.eu.lever.co``.
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
        (opportunity_id, "lever:%"),
    ).fetchall():
        site, job = str(source[0])[len("lever:"):], str(source[1] or "")
        if _SITE.fullmatch(site) and _JOB_ID.fullmatch(job):
            return LeverRef(site, job, DEFAULT_HOST)
    return None
