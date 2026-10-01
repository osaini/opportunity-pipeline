"""Resolve the student's timezone once, for every calendar-date decision.

Outreach follow-ups, the Urgent queue and application analytics all decide
"is this overdue?" by comparing calendar dates. They must agree, so they share
this resolver: an explicit notification-preference timezone, else
``PIPELINE_TIMEZONE``, else the machine's own local time.

The machine-local fallback deliberately does not become UTC: on a student's
laptop in Texas a UTC fallback would move every late-evening item to the next
day. Windows has no reliable way to name its zone in IANA terms, so that
fallback is reported as ``"system-local"`` and converts with Python's
system-local rules, which apply the OS's DST rules per instant.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SYSTEM_LOCAL = "system-local"
_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")


def to_local(instant: datetime, zone: ZoneInfo | None) -> datetime:
    """An instant in ``zone``, or in the machine's own zone when ``zone`` is None (system-local)."""
    return instant.astimezone(zone) if zone else instant.astimezone()


def at_wall_clock(naive: datetime, zone: ZoneInfo | None) -> datetime:
    """A naive wall-clock time read as being in ``zone``, or in the machine's own zone when ``zone`` is None.

    Unlike ``UserTimezone.localize`` this does not leave an aware value alone
    (it replaces the zone), and it keeps the value's own ``fold``. It is for
    values built by ``datetime.combine``, which are always naive and fold 0.
    """
    return naive.replace(tzinfo=zone) if zone else naive.astimezone()


@dataclass(frozen=True)
class UserTimezone:
    name: str
    zone: ZoneInfo | None = None

    def to_local(self, instant: datetime) -> datetime:
        return to_local(instant, self.zone)

    def today(self, now: datetime | None = None) -> date:
        return self.to_local(now or datetime.now(timezone.utc)).date()

    def localize(self, naive: datetime) -> datetime:
        """Attach this zone to a wall-clock time.

        DST-ambiguous times resolve to the earlier instant (``fold=0``) and
        nonexistent times keep zoneinfo's pre-transition offset, which reads as
        the shifted-forward instant.
        """
        if naive.tzinfo is not None:
            return naive
        if self.zone:
            return naive.replace(tzinfo=self.zone, fold=0)
        return naive.astimezone()

    def utc_offset(self, now: datetime | None = None) -> str:
        offset = self.to_local(now or datetime.now(timezone.utc)).utcoffset()
        minutes = int(offset.total_seconds() // 60) if offset else 0
        sign = "-" if minutes < 0 else "+"
        minutes = abs(minutes)
        return f"{sign}{minutes // 60:02d}:{minutes % 60:02d}"

    def calendar_date(self, value: str | None) -> date | None:
        """Return the calendar date an instant falls on for this student.

        A ``YYYY-MM-DD`` value is already a calendar date. An aware timestamp is
        converted into this zone. A naive timestamp is wall-clock time here, so
        its own date is the answer. Raises ``ValueError`` for anything else.
        """
        text = str(value or "").strip()
        if not text:
            return None
        if _DATE_ONLY.fullmatch(text):
            return date.fromisoformat(text)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.date()
        return self.to_local(parsed).date()

    def normalize_instant(self, value: str | None, *, field: str = "due_at") -> str | None:
        """Store a user-entered date or date-time as an aware ISO instant."""
        if value is None or not str(value).strip():
            return None
        try:
            parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO date or date-time") from exc
        return self.localize(parsed).isoformat()


def named_timezone(name: str) -> UserTimezone:
    """A resolver for an explicit IANA name; raises ``ValueError`` if unknown."""
    try:
        return UserTimezone(name, ZoneInfo(name))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Unknown IANA timezone") from exc


def user_timezone(conn: sqlite3.Connection, user_id: str) -> UserTimezone:
    row = conn.execute(
        "SELECT timezone, timezone_explicit FROM notification_preferences WHERE user_id=?",
        (user_id,),
    ).fetchone()
    # Rows may be sqlite3.Row, a PostgreSQL dict, or a plain tuple.
    keyed = row is not None and hasattr(row, "keys")
    name = str((row["timezone"] if keyed else row[0]) or "") if row else ""
    explicit = bool(row["timezone_explicit"] if keyed else row[1]) if row else False
    if not explicit:
        name = os.environ.get("PIPELINE_TIMEZONE", "").strip()
    if name:
        try:
            return named_timezone(name)
        except ValueError:
            pass
    return UserTimezone(SYSTEM_LOCAL)
