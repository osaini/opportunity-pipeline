"""The Urgent action queue and the deadlines a student records for a role.

Postings almost never state a deadline, so Urgent is built from every dated
record that really exists: deadlines stated in posting text, deadlines the
student entered, deadlines from their own program research, outreach
deadlines, open application tasks, and follow-up dates. Each row keeps a label saying where its date came from. Nothing is
estimated; a posting's age is never turned into a closing date.

"Overdue" and "today" are calendar dates in the student's timezone
(``user_time.user_timezone``), the same rule Outreach and the application
analytics use.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from pipeline_core.visibility import CAPTURE_SOURCE_KEY, capture_visible_sql  # noqa: F401  (re-exported)

from .early_programs import early_programs
from .outreach import CLOSED_STATUSES as OUTREACH_CLOSED, REVISIT_STATUSES as OUTREACH_REVISIT
from .schema import utc_now
from .user_time import UserTimezone, user_timezone

logger = logging.getLogger(__name__)

# A task or follow-up on a closed application no longer needs doing. An offer
# is not closed: "reply to the offer by Friday" is exactly an urgent task.
CLOSED_APPLICATION_STAGES = ("rejected", "withdrawn", "archived")
# Once submitted, a role's application deadline no longer applies.
SUBMITTED_OR_CLOSED_STAGES = ("applied", "interview", "offer", *CLOSED_APPLICATION_STAGES)
OVERDUE_LOOKBACK_DAYS = 60
ATTENTION_DAYS = 2
MAX_SKIPPED = 20
NOTE_LIMIT = 200

DATE_SOURCE_LABELS = {
    "posting_deadline": "Stated in posting text",
    "your_deadline": "You entered",
    # A program's `evidence` field is about eligibility, not where its date
    # came from, and dates are often the student's own estimates. One neutral
    # label; the entry's deadline_note travels with the row as `date_note`.
    "program_deadline": "From your program research",
    "outreach_deadline": "Outreach record deadline",
    "task": "Task due",
    "application_follow_up": "Follow-up date",
    "outreach_follow_up": "Follow-up scheduled",
    "outreach_revisit": "Revisit date you set",
}
KIND_PRIORITY = {
    "posting_deadline": 0,
    "your_deadline": 0,
    "program_deadline": 0,
    "outreach_deadline": 0,
    "task": 1,
    "application_follow_up": 2,
    "outreach_follow_up": 2,
    "outreach_revisit": 2,
}

_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")
# Warnings about one malformed record are logged once per process, so the nav
# badge refreshing after every mutation cannot flood the log.
_warned: set[tuple[str, str]] = set()


class DeadlineNotFoundError(LookupError):
    pass


def visible_opportunity(conn: sqlite3.Connection, user_id: str, opportunity_id: str) -> bool:
    row = conn.execute(
        f"""
        SELECT o.id FROM opportunities o
        WHERE o.id = ? AND o.active = 1 AND o.duplicate_of IS NULL
          AND {capture_visible_sql("o")}
        """,
        (opportunity_id, user_id),
    ).fetchone()
    return row is not None


def _clean_deadline(value: Any) -> str:
    text = value if isinstance(value, str) else ""
    if not _DATE_ONLY.fullmatch(text):
        raise ValueError("deadline_on must be a calendar date (YYYY-MM-DD)")
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("deadline_on is not a real date") from exc
    if not 2020 <= parsed.year <= 2100:
        raise ValueError("deadline_on must be between 2020 and 2100")
    return text


def _clean_note(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) > NOTE_LIMIT:
        raise ValueError(f"note must be at most {NOTE_LIMIT} characters")
    return text


def _is_foreign_key_error(exc: Exception) -> bool:
    if isinstance(exc, sqlite3.IntegrityError):
        return "FOREIGN KEY" in str(exc).upper()
    try:
        from psycopg import errors  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - PostgreSQL is optional
        return False
    return isinstance(exc, errors.ForeignKeyViolation)


def set_user_deadline(
    conn: sqlite3.Connection,
    opportunity_id: str,
    *,
    user_id: str,
    deadline_on: Any,
    note: Any = "",
) -> dict[str, Any]:
    deadline = _clean_deadline(deadline_on)
    clean_note = _clean_note(note)
    timestamp = utc_now()
    try:
        with conn:
            # Visibility is part of the write itself, so a refresh that retires
            # or de-duplicates the posting between a check and the insert cannot
            # slip a deadline in: the SELECT then yields no row and nothing is
            # written.
            cursor = conn.execute(
                f"""
                INSERT INTO opportunity_deadlines(
                    user_id, opportunity_id, deadline_on, note, created_at, updated_at
                )
                SELECT ?, o.id, ?, ?, ?, ?
                FROM opportunities o
                WHERE o.id = ? AND o.active = 1 AND o.duplicate_of IS NULL
                  AND {capture_visible_sql("o")}
                ON CONFLICT(user_id, opportunity_id) DO UPDATE SET
                    deadline_on = excluded.deadline_on,
                    note = excluded.note,
                    updated_at = excluded.updated_at
                """,
                (user_id, deadline, clean_note, timestamp, timestamp, opportunity_id, user_id),
            )
            if not cursor.rowcount:
                raise DeadlineNotFoundError(opportunity_id)
    except Exception as exc:
        # A posting deleted outright mid-write fails the foreign key instead.
        if _is_foreign_key_error(exc):
            raise DeadlineNotFoundError(opportunity_id) from exc
        raise
    return user_deadline(conn, opportunity_id, user_id=user_id) or {}


def clear_user_deadline(conn: sqlite3.Connection, opportunity_id: str, *, user_id: str) -> bool:
    """Delete the caller's own deadline, whatever state the posting is in."""
    with conn:
        cursor = conn.execute(
            "DELETE FROM opportunity_deadlines WHERE user_id = ? AND opportunity_id = ?",
            (user_id, opportunity_id),
        )
    return bool(cursor.rowcount)


def user_deadline(conn: sqlite3.Connection, opportunity_id: str, *, user_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT deadline_on, note, created_at, updated_at FROM opportunity_deadlines
        WHERE user_id = ? AND opportunity_id = ?
        """,
        (user_id, opportunity_id),
    ).fetchone()
    if not row:
        return None
    return {
        "deadline_on": row["deadline_on"],
        "note": row["note"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def user_deadlines_for(conn: sqlite3.Connection, opportunity_ids: list[str], *, user_id: str) -> dict[str, str]:
    """The caller's deadlines for one page of opportunities, in one query."""
    ids = [str(value) for value in opportunity_ids if value]
    if not ids:
        # PostgreSQL rejects an empty IN ().
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = conn.execute(
        f"""
        SELECT opportunity_id, deadline_on FROM opportunity_deadlines
        WHERE user_id = ? AND opportunity_id IN ({placeholders})
        """,
        (user_id, *ids),
    ).fetchall()
    return {str(row["opportunity_id"]): str(row["deadline_on"]) for row in rows}


def _in_clause(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _latest_intent_sql(alias: str = "o") -> str:
    return f"""(
        SELECT i.action FROM opportunity_interactions i
        WHERE i.opportunity_id = {alias}.id AND i.user_id = ?
        ORDER BY i.id DESC LIMIT 1
    )"""


def _source_name_sql(alias: str = "o") -> str:
    return f"""(
        SELECT s.source_name FROM opportunity_sources s
        WHERE s.opportunity_id = {alias}.id
        ORDER BY s.source_key, s.external_id LIMIT 1
    )"""


def _posting_rows(conn: sqlite3.Connection, user_id: str) -> list[dict[str, Any]]:
    submitted = _in_clause(SUBMITTED_OR_CLOSED_STAGES)
    base = f"""
        FROM opportunities o
        LEFT JOIN applications a ON a.opportunity_id = o.id AND a.user_id = ?
        WHERE o.active = 1 AND o.duplicate_of IS NULL
          AND {capture_visible_sql("o")}
          AND (a.stage IS NULL OR a.stage NOT IN ({submitted}))
    """
    columns = f"""
        o.id AS opportunity_id, o.company, o.title, a.id AS application_id, a.stage,
        {_latest_intent_sql("o")} AS latest_action,
        {_source_name_sql("o")} AS source_name
    """
    listed = conn.execute(
        f"SELECT {columns}, o.deadline_at AS raw_date {base} AND o.deadline_at IS NOT NULL AND o.deadline_at <> ''",
        (user_id, user_id, user_id),
    ).fetchall()
    entered = conn.execute(
        f"""
        SELECT {columns}, d.deadline_on AS raw_date
        FROM opportunity_deadlines d
        JOIN opportunities o ON o.id = d.opportunity_id
        LEFT JOIN applications a ON a.opportunity_id = o.id AND a.user_id = ?
        WHERE d.user_id = ? AND o.active = 1 AND o.duplicate_of IS NULL
          AND {capture_visible_sql("o")}
          AND (a.stage IS NULL OR a.stage NOT IN ({submitted}))
        """,
        (user_id, user_id, user_id, user_id),
    ).fetchall()
    rows = []
    for kind, found in (("posting_deadline", listed), ("your_deadline", entered)):
        for row in found:
            if row["latest_action"] == "passed":
                continue
            rows.append({
                "kind": kind,
                "record_id": str(row["opportunity_id"]),
                # A stated deadline is stored as midnight UTC but means a
                # calendar date; converting it would move it a day earlier.
                "raw_date": str(row["raw_date"])[:10],
                "date_only": True,
                "title": row["title"],
                "company": row["company"],
                "source_name": row["source_name"] if kind == "posting_deadline" else None,
                "opportunity_id": str(row["opportunity_id"]),
                "application_id": row["application_id"],
                "saved": row["latest_action"] == "saved",
                "stage": row["stage"],
            })
    return rows


def _program_rows(
    conn: sqlite3.Connection, user_id: str, path: Path | None, now: datetime | None,
) -> list[dict[str, Any]]:
    """Deadlines from the student's researched program list that are still ahead.

    Only a program the student has not marked applied or skipped counts, and a
    deadline already past is left out: a missed program deadline is closed,
    not a task to catch up on. A list that cannot be read adds nothing here;
    the Programs tab reports why.
    """
    if path is None:
        return []
    listed = early_programs(conn, user_id=user_id, path=path, now=now)
    return [
        {
            "kind": "program_deadline",
            "record_id": item["id"],
            "raw_date": item["deadline_on"],
            "date_only": True,
            "title": item["name"],
            "company": item["host"],
            "source_name": item["source_note"] or None,
            "date_note": item.get("deadline_note") or None,
            "program_id": item["id"],
        }
        for item in listed["items"]
        if item["bucket"] in ("open", "upcoming") and item["deadline_on"] and item["days_left"] >= 0
    ]


def _application_rows(conn: sqlite3.Connection, user_id: str) -> list[dict[str, Any]]:
    closed = _in_clause(CLOSED_APPLICATION_STAGES)
    rows = []
    tasks = conn.execute(
        f"""
        SELECT t.id AS task_id, t.title AS task_title, t.due_at, a.id AS application_id,
               a.stage, o.id AS opportunity_id, o.company, o.title
        FROM application_tasks t
        JOIN applications a ON a.id = t.application_id AND a.user_id = t.user_id
        JOIN opportunities o ON o.id = a.opportunity_id
        WHERE t.user_id = ? AND t.status = 'open' AND t.due_at IS NOT NULL AND t.due_at <> ''
          AND a.stage NOT IN ({closed})
        """,
        (user_id,),
    ).fetchall()
    for row in tasks:
        rows.append({
            "kind": "task",
            "record_id": str(row["task_id"]),
            "raw_date": row["due_at"],
            "date_only": False,
            "title": row["task_title"],
            "company": row["company"],
            "subtitle": row["title"],
            "opportunity_id": str(row["opportunity_id"]),
            "application_id": row["application_id"],
            "task_id": row["task_id"],
            "stage": row["stage"],
        })
    follow_ups = conn.execute(
        f"""
        SELECT a.id AS application_id, a.follow_up_at, a.stage, o.id AS opportunity_id, o.company, o.title
        FROM applications a
        JOIN opportunities o ON o.id = a.opportunity_id
        WHERE a.user_id = ? AND a.follow_up_at IS NOT NULL AND a.follow_up_at <> ''
          AND a.stage NOT IN ({closed})
        """,
        (user_id,),
    ).fetchall()
    for row in follow_ups:
        rows.append({
            "kind": "application_follow_up",
            "record_id": str(row["application_id"]),
            "raw_date": row["follow_up_at"],
            "date_only": False,
            "title": row["title"],
            "company": row["company"],
            "opportunity_id": str(row["opportunity_id"]),
            "application_id": row["application_id"],
            "stage": row["stage"],
        })
    return rows


def _outreach_rows(conn: sqlite3.Connection, user_id: str) -> list[dict[str, Any]]:
    closed = _in_clause(tuple(sorted(OUTREACH_CLOSED)))
    revisit = _in_clause(tuple(sorted(OUTREACH_REVISIT)))
    rows = []
    targets = conn.execute(
        f"""
        SELECT id, company, status, deadline_date, follow_up_at
        FROM outreach_targets
        WHERE user_id = ? AND (
            status NOT IN ({closed})
            OR (status IN ({revisit}) AND follow_up_at IS NOT NULL AND follow_up_at <> '')
        )
        """,
        (user_id,),
    ).fetchall()
    for row in targets:
        base = {
            "record_id": str(row["id"]),
            "date_only": True,
            "title": row["company"],
            "company": row["company"],
            "outreach_target_id": str(row["id"]),
            "stage": row["status"],
        }
        # A paused or replied company surfaces only for the date you set to revisit it.
        if row["status"] in OUTREACH_REVISIT:
            rows.append({**base, "kind": "outreach_revisit", "raw_date": row["follow_up_at"]})
            continue
        if row["deadline_date"]:
            rows.append({**base, "kind": "outreach_deadline", "raw_date": row["deadline_date"]})
        # Same rule as Outreach's own "follow-up due": only while awaiting a first reply.
        if row["follow_up_at"] and row["status"] == "sent":
            rows.append({**base, "kind": "outreach_follow_up", "raw_date": row["follow_up_at"]})
    return rows


def _warn_once(key: str, reason: str) -> None:
    if (key, reason) in _warned:
        return
    _warned.add((key, reason))
    logger.warning("urgent_item_skipped", extra={"urgent_key": key, "reason": reason})


def _item_date(zone: UserTimezone, row: dict[str, Any]) -> date:
    raw = str(row["raw_date"] or "").strip()
    if row["date_only"]:
        if not _DATE_ONLY.fullmatch(raw):
            raise ValueError("not a calendar date")
        return date.fromisoformat(raw)
    parsed = zone.calendar_date(raw)
    if parsed is None:
        raise ValueError("empty date")
    return parsed


def urgent_queue(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    days: int = 14,
    now: datetime | None = None,
    programs_path: Path | None = None,
) -> dict[str, Any]:
    if not 1 <= days <= 60:
        raise ValueError("days must be between 1 and 60")
    zone = user_timezone(conn, user_id)
    today = zone.today(now)
    last_upcoming = today + timedelta(days=days - 1)
    oldest_overdue = today - timedelta(days=OVERDUE_LOOKBACK_DAYS)

    candidates = [
        *_posting_rows(conn, user_id), *_program_rows(conn, user_id, programs_path, now),
        *_application_rows(conn, user_id), *_outreach_rows(conn, user_id),
    ]
    items: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    skipped_count = 0
    older_overdue = 0
    for row in candidates:
        key = f"{row['kind']}:{row['record_id']}"
        try:
            when = _item_date(zone, row)
        except (TypeError, ValueError) as exc:
            skipped_count += 1
            reason = f"unparseable date: {exc}"
            _warn_once(key, reason)
            if len(skipped) < MAX_SKIPPED:
                skipped.append({
                    "kind": row["kind"], "key": key, "company": row.get("company") or "",
                    "title": row.get("title") or "", "reason": reason,
                })
            continue
        if when < oldest_overdue:
            older_overdue += 1
            continue
        if when > last_upcoming:
            continue
        days_until = (when - today).days
        items.append({
            "key": key,
            "kind": row["kind"],
            "date": when.isoformat(),
            "date_source": DATE_SOURCE_LABELS[row["kind"]],
            "overdue": days_until < 0,
            "days_until": days_until,
            "title": row.get("title") or "",
            "subtitle": row.get("subtitle"),
            "company": row.get("company") or "",
            "source_name": row.get("source_name"),
            "date_note": row.get("date_note"),
            "opportunity_id": row.get("opportunity_id"),
            "application_id": row.get("application_id"),
            "outreach_target_id": row.get("outreach_target_id"),
            "program_id": row.get("program_id"),
            "task_id": row.get("task_id"),
            "saved": bool(row.get("saved")),
            "stage": row.get("stage"),
        })
    items.sort(key=lambda item: (
        not item["overdue"],
        item["date"],
        KIND_PRIORITY[item["kind"]],
        str(item["title"]).casefold(),
        item["key"],
    ))
    overdue = sum(1 for item in items if item["overdue"])
    upcoming = len(items) - overdue
    near = sum(1 for item in items if 0 <= item["days_until"] <= ATTENTION_DAYS)
    return {
        "today": today.isoformat(),
        "timezone": zone.name,
        "utc_offset": zone.utc_offset(now),
        "window_days": days,
        "counts": {"overdue": overdue, "upcoming": upcoming, "attention": overdue + near},
        "older_overdue": older_overdue,
        "skipped_count": skipped_count,
        "skipped": skipped,
        "items": items,
    }
