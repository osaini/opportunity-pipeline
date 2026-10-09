"""Which saved role is a Lever posting, where Lever lives, and the constants that say so.

The Lever twin of ``greenhouse.py`` (docs/phase5-lever-handoff-spec.md, section 5.3): its hosts, the adapter's version, the
address of a posting's application page, and how a saved role is recognised as a Lever posting (``identify``).

This is the pure, read-only half: the registry entry for Lever is in ``ats.py`` and its request policy in ``checks.py``. The
browser side (the adapter that fills a Lever form) is ``lever_adapter.py``, which ``agent.ADAPTERS`` holds (spec 12, LV4).

Standard library only, and no import of any other first-party module, like ``greenhouse.py``.
"""

from __future__ import annotations

import re
import sqlite3
from typing import NamedTuple
from urllib.parse import urlsplit

ATS_LEVER = "lever"
# How a sentence names it (the ATS spec's ``display_name`` and the request policy's are this).
DISPLAY_NAME = "Lever"
# The adapter's version (spec 11, R1). Any change to its selectors or rules means a new value.
ADAPTER_VERSION = "lever-1"

# What the plan calls a field the Lever page has and the app cannot read, or a control the parser has no family for (``ats.lever_parse_schema``
# makes them; ``policy`` leaves them to the student, with the reason in ``description``).
UNREADABLE_TYPE = "lever_unreadable"
UNKNOWN_TYPE = "lever_unknown"
# Typed on the page by the student, a name and a date, and required once the disability question is answered at all (spec 3.6). Never filled.
EEO_SIGNATURE_FIELDS = ("eeo[disabilitySignature]", "eeo[disabilitySignatureDate]")
# The schema names ``lever_form`` gives the four EEO questions. Disability is never answered, whatever is stored (spec 6.6).
EEO_DISABILITY = "disability_status"
# Fixed fields the plan never fills and never lets a stored answer fill: an identity disclosure, and a marketing consent no exact statement covers.
NEVER_PLANNED = ("pronouns", "consent[marketing]")

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


# --- The paths a posting's page asks for (spec 7) -----------------------------------------------

# Both on the posting's own host. The page's lookup of a place name, and the page's reading of an attached résumé.
SEARCH_LOCATIONS_PATH = "/searchLocations"
PARSE_RESUME_PATH = "/parseResume"

# JavaScript's ``\s`` (ECMAScript WhiteSpace and LineTerminator), spelled out: Python's ``\s`` differs at the edges (it takes U+001C to U+001F and U+0085, and not U+FEFF).
_JS_SPACE = "\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_UNSAFE_CHAR = re.compile(r'[<>:"/\\|?*' + _JS_SPACE + "]")


def posted_file_name(name: str) -> str:
    r"""The file name Lever's own page posts to ``/parseResume`` for a file the browser calls ``name`` (its ``sanitizeFilename``, read from
    /js/parseResume.js on 2026-10-08): each of ``< > : " / \ | ? *`` and each whitespace character becomes an underscore, leading and trailing dots
    go, leading and trailing underscores go, each run of underscores becomes one, and nothing left is "untitled". Parentheses, apostrophes, commas,
    ``&``, ``+``, ``#`` and letters of any script stay as they are. The request rules compare the name a request carries with this, so the
    rule must stay Lever's own: a copy of it that drifts refuses the student's own file or lets a script's name through.
    """
    cleaned = _UNSAFE_CHAR.sub("_", name or "")
    cleaned = re.sub(r"^\.+|\.+\Z", "", cleaned)
    cleaned = re.sub(r"^_+|_+\Z", "", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned)
    return cleaned or "untitled"


def apply_path(site: str, job_id: str) -> str:
    """The path the application form posts to: the form has no ``action``, so it posts to its own page (spec 3.2, 6.12)."""
    return f"/{site}/{job_id}/apply"


def thanks_path(site: str, job_id: str) -> str:
    """The path of the posting's confirmation page (spec 3.12, 6.13). A plain GET of it also answers 200, so reaching it proves nothing alone."""
    return f"/{site}/{job_id}/thanks"


def from_url(url: str) -> LeverRef | None:
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
    found = from_url(row[0])
    if found:
        return found
    sources = conn.execute(
        "SELECT source_url, source_key, external_id FROM opportunity_sources WHERE opportunity_id=? ORDER BY last_seen_at DESC, source_key",
        (opportunity_id,),
    ).fetchall()
    for source in sources:
        found = from_url(source[0])
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
