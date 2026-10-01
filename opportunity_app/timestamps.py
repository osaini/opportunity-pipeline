"""Canonical UTC for the timestamps that decide ranking.

`opportunities.posted_at` is stored exactly as each source API sent it, because
the source's own words are what the product promises to preserve. Boards do not
agree on a spelling: Adzuna sends `2026-07-18T09:14:00Z`, Ashby sends
`2026-05-01T00:00:00.000Z`, Greenhouse sends offsets like `-04:00`.

Ordering those as text does not order them in time. Measured against SQLite:

    2026-09-21T01:49:17.999999+00:00   <-- sorts first
    2026-09-21T01:49:17Z               <-- sorts last, but is nearly a second EARLIER

The `Z` and `+00:00` spellings are the trap; mixing second and microsecond
precision within one spelling happens to be safe, because a missing fraction
sorts before any fraction and a missing fraction means `.000000`.

So a derived column holds the canonical form and the raw string is never
touched. Canonical means fixed width: six fractional digits and a literal
`+00:00`, so lexicographic order is chronological order with no exceptions.

Standard library only: `pipeline.py` and `pipeline_core/` must stay
dependency-free and this sits alongside them.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone


def canonical_utc(value: str | None) -> str | None:
    """Return `value` as fixed-width UTC, or None when that is not honest.

    None is returned for anything that does not carry a timezone, including a
    naive timestamp. Python would read a naive string in the *machine's* local
    zone, so calling it UTC would invent an offset and silently reorder cards
    on a laptop that travels. A source that does not say when it means is
    recorded as not having said.
    """

    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # fromisoformat handles `Z` from Python 3.11, but normalising it here keeps
    # the behaviour identical on every supported version.
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_app_instant(value: object) -> datetime | None:
    """Read a stamp THIS APP wrote: an aware datetime, or None when it will not parse.

    Only for stamps the app itself recorded (`utc_now()` values, event and
    ledger times, token grant times). A stamp without an offset is read as UTC,
    because the app only ever writes UTC. A non-UTC offset is kept as written,
    not converted.

    Never use this for a date a source sent, such as `posted_at`. For those a
    naive stamp means "the source did not say when", and `canonical_utc` above
    answers that case the opposite way, with None. Reading a source's naive
    stamp as UTC would invent an offset.
    """

    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# The last stamp utc_now handed out in this process, so the next is always later.
_LAST_NOW = datetime.min.replace(tzinfo=timezone.utc)
_NOW_LOCK = threading.Lock()


def utc_now() -> str:
    """Now, in UTC to the microsecond, and always later than the last call in this process.

    Rows are ordered by these stamps (the automation ledger, notices, events),
    and application_events is unique on one. Some clocks tick only every 15 ms
    or so (Windows before Python 3.13), which would hand two writes in a row
    the same stamp and leave their order to chance. A tie moves on by a
    microsecond instead.
    """
    global _LAST_NOW
    with _NOW_LOCK:
        now = datetime.now(timezone.utc)
        if now <= _LAST_NOW:
            now = _LAST_NOW + timedelta(microseconds=1)
        _LAST_NOW = now
    return now.isoformat(timespec="microseconds")
