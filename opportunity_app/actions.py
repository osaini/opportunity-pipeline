"""Audited student intent and application state transitions."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .schema import utc_now
from .user_time import named_timezone, user_timezone


INTENT_ACTIONS = {"seen", "saved", "passed", "apply_opened", "undo"}
APPLICATION_STAGES = {
    "applying",
    "applied",
    "interview",
    "offer",
    "rejected",
    "withdrawn",
    "archived",
}
TERMINAL_APPLICATION_STAGES = {"offer", "rejected", "withdrawn", "archived"}
# Stages whose tasks and follow-ups no longer need doing. Unlike reminder
# delivery above, an offer stays open here: replying to it is real work.
CLOSED_APPLICATION_STAGES = {"rejected", "withdrawn", "archived"}


class OpportunityNotFoundError(LookupError):
    pass


class ApplicationNotFoundError(LookupError):
    pass


def _opportunity_exists(conn: sqlite3.Connection, opportunity_id: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM opportunities WHERE id=?", (opportunity_id,)).fetchone()
        is not None
    )


def record_intent(
    conn: sqlite3.Connection,
    opportunity_id: str,
    action: str,
    *,
    user_id: str,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    if action not in INTENT_ACTIONS:
        raise ValueError(f"Unsupported intent action: {action}")
    if not _opportunity_exists(conn, opportunity_id):
        raise OpportunityNotFoundError(opportunity_id)
    if idempotency_key:
        existing_request = conn.execute(
            "SELECT opportunity_id, action, response_json FROM action_requests WHERE user_id=? AND idempotency_key=?",
            (user_id, idempotency_key),
        ).fetchone()
        if existing_request:
            if existing_request["opportunity_id"] != opportunity_id or existing_request["action"] != action:
                raise ValueError("Idempotency key was already used for a different action")
            response = json.loads(existing_request["response_json"])
            response["replayed"] = True
            return response
    latest = conn.execute(
        """
        SELECT action FROM opportunity_interactions
        WHERE opportunity_id=? AND user_id=? ORDER BY id DESC LIMIT 1
        """,
        (opportunity_id, user_id),
    ).fetchone()
    current_state = latest["action"] if latest and latest["action"] in {"saved", "passed"} else ""
    desired_state = {"saved": "saved", "passed": "passed", "undo": ""}.get(action)
    state_unchanged = desired_state is not None and current_state == desired_state
    timestamp = utc_now()
    with conn:
        if not state_unchanged:
            conn.execute(
                """
                INSERT INTO opportunity_interactions(opportunity_id, user_id, action, created_at)
                VALUES(?, ?, ?, ?)
                """,
                (opportunity_id, user_id, action, timestamp),
            )
        application_id: str | None = None
        if action == "apply_opened":
            application_id = f"app-{opportunity_id}"
            conn.execute(
                """
                INSERT INTO applications(
                    id, opportunity_id, user_id, stage, notes, applied_at,
                    follow_up_at, created_at, updated_at
                ) VALUES(?, ?, ?, 'applying', '', NULL, NULL, ?, ?)
                ON CONFLICT(opportunity_id, user_id) DO UPDATE SET
                    updated_at=excluded.updated_at
                """,
                (application_id, opportunity_id, user_id, timestamp, timestamp),
            )
            row = conn.execute(
                "SELECT id, stage FROM applications WHERE opportunity_id=? AND user_id=?",
                (opportunity_id, user_id),
            ).fetchone()
            application_id = str(row["id"])
            conn.execute(
                """
                INSERT INTO application_events(
                    application_id, event_type, from_stage, to_stage,
                    detail_json, created_at
                ) VALUES(?, 'application_opened', ?, ?, '{}', ?)
                """,
                (application_id, row["stage"], row["stage"], timestamp),
            )
        response = {
            "opportunity_id": opportunity_id,
            "action": action,
            "application_id": application_id,
            "created_at": timestamp,
            "replayed": False,
            "unchanged": state_unchanged,
        }
        if idempotency_key:
            conn.execute(
                """
                INSERT INTO action_requests(
                    idempotency_key, user_id, opportunity_id, action,
                    response_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    user_id,
                    opportunity_id,
                    action,
                    json.dumps(response),
                    timestamp,
                ),
            )
    return response


def list_applications(conn: sqlite3.Connection, *, user_id: str) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT
            a.id, a.opportunity_id, a.stage, a.notes, a.applied_at,
            a.follow_up_at, a.created_at, a.updated_at,
            o.company, o.title, o.location, o.region, o.url,
            COALESCE(fs.score, 0) AS score
        FROM applications a
        JOIN opportunities o ON o.id = a.opportunity_id
        LEFT JOIN fit_scores fs
          ON fs.opportunity_id = o.id
         AND fs.user_id = a.user_id
         AND fs.ruleset_version = 'legacy-v1'
        WHERE a.user_id=?
        ORDER BY a.updated_at DESC, a.id ASC
        """,
        (user_id,),
    ).fetchall()
    zone = user_timezone(conn, user_id)
    today = zone.today()
    items = []
    for row in rows:
        item = dict(row)
        try:
            due = zone.calendar_date(item["follow_up_at"])
        except ValueError:
            due = None
        # Same calendar-date rule as the Urgent queue and the analytics; the
        # label uses this date too, so it can never disagree with the flag.
        item["follow_up_on"] = due.isoformat() if due else None
        item["follow_up_overdue"] = bool(
            due and due < today and item["stage"] not in CLOSED_APPLICATION_STAGES
        )
        items.append(item)
    return items


def application_analytics(conn: sqlite3.Connection, *, user_id: str) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    stage_rows = conn.execute(
        "SELECT stage, COUNT(*) AS count FROM applications WHERE user_id=? GROUP BY stage",
        (user_id,),
    ).fetchall()
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=30)).isoformat()
    # "Overdue" is a calendar date before today in the student's timezone, the
    # same rule the Urgent queue uses, and never counts closed applications.
    # Stored instants carry different offsets (or none), so they are compared
    # as parsed dates rather than as text.
    zone = user_timezone(conn, user_id)
    today = zone.today(now)
    open_tasks = conn.execute(
        """
        SELECT t.due_at, a.stage FROM application_tasks t
        JOIN applications a ON a.id = t.application_id
        WHERE t.user_id=? AND t.status='open'
        """,
        (user_id,),
    ).fetchall()
    follow_up_rows = conn.execute(
        """
        SELECT follow_up_at, stage FROM applications
        WHERE user_id=? AND follow_up_at IS NOT NULL AND follow_up_at<>''
        """,
        (user_id,),
    ).fetchall()

    def overdue(value: str | None) -> bool:
        try:
            due = zone.calendar_date(value)
        except ValueError:
            return False
        return due is not None and due < today

    overdue_tasks = sum(
        1 for row in open_tasks
        if row["stage"] not in CLOSED_APPLICATION_STAGES and overdue(row["due_at"])
    )
    overdue_follow_ups = sum(
        1 for row in follow_up_rows
        if row["stage"] not in CLOSED_APPLICATION_STAGES and overdue(row["follow_up_at"])
    )
    activity = conn.execute(
        """
        SELECT COUNT(*) FROM application_events e
        JOIN applications a ON a.id=e.application_id
        WHERE a.user_id=? AND e.created_at>=?
        """,
        (user_id, since),
    ).fetchone()[0]
    follow_ups = len(follow_up_rows)
    stages = {stage: 0 for stage in APPLICATION_STAGES}
    stages.update({str(row["stage"]): int(row["count"]) for row in stage_rows})
    return {
        "total": sum(stages.values()),
        "stages": stages,
        "open_tasks": len(open_tasks),
        "overdue_tasks": overdue_tasks,
        "overdue_follow_ups": overdue_follow_ups,
        "scheduled_follow_ups": int(follow_ups),
        "activity_last_30_days": int(activity),
        "interpretation": "Descriptive activity counts only; they do not claim that one action caused an outcome.",
    }


def application_detail(
    conn: sqlite3.Connection,
    application_id: str,
    *,
    user_id: str,
) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    application = conn.execute(
        """
        SELECT a.*, o.company, o.title, o.location, o.region, o.url, o.deadline_at
        FROM applications a JOIN opportunities o ON o.id=a.opportunity_id
        WHERE a.id=? AND a.user_id=?
        """,
        (application_id, user_id),
    ).fetchone()
    if not application:
        raise ApplicationNotFoundError(application_id)
    events = conn.execute(
        """
        SELECT id, event_type, from_stage, to_stage, detail_json, created_at
        FROM application_events WHERE application_id=? ORDER BY created_at DESC, id DESC
        """,
        (application_id,),
    ).fetchall()
    contacts = conn.execute(
        "SELECT * FROM application_contacts WHERE application_id=? AND user_id=? ORDER BY name COLLATE NOCASE",
        (application_id, user_id),
    ).fetchall()
    tasks = conn.execute(
        """
        SELECT * FROM application_tasks WHERE application_id=? AND user_id=?
        ORDER BY status, CASE WHEN due_at IS NULL THEN 1 ELSE 0 END, due_at, created_at
        """,
        (application_id, user_id),
    ).fetchall()
    reminders = conn.execute(
        """
        SELECT id, reminder_type, due_at, timezone, status, created_at, updated_at
        FROM reminders WHERE application_id=? AND user_id=? ORDER BY due_at
        """,
        (application_id, user_id),
    ).fetchall()
    result = dict(application)
    result["events"] = [
        {
            **dict(row),
            "detail": json.loads(row["detail_json"] or "{}"),
        }
        for row in events
    ]
    for event in result["events"]:
        event.pop("detail_json", None)
    result["contacts"] = [dict(row) for row in contacts]
    result["tasks"] = [dict(row) for row in tasks]
    result["reminders"] = [dict(row) for row in reminders]
    return result


def add_application_contact(
    conn: sqlite3.Connection,
    application_id: str,
    *,
    name: str,
    role: str = "",
    email: str = "",
    phone: str = "",
    user_id: str,
) -> dict[str, Any]:
    application_detail(conn, application_id, user_id=user_id)
    if not name.strip():
        raise ValueError("Contact name is required")
    contact_id = f"contact-{uuid4().hex}"
    timestamp = utc_now()
    with conn:
        conn.execute(
            """
            INSERT INTO application_contacts(
                id, application_id, user_id, name, role, email, phone, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (contact_id, application_id, user_id, name.strip(), role.strip(), email.strip(), phone.strip(), timestamp, timestamp),
        )
        conn.execute(
            """
            INSERT INTO application_events(
                application_id, event_type, from_stage, to_stage, detail_json, created_at
            ) VALUES(?, 'contact_added', NULL, NULL, ?, ?)
            """,
            (application_id, json.dumps({"contact_id": contact_id, "name": name.strip()}), timestamp),
        )
    return dict(conn.execute("SELECT * FROM application_contacts WHERE id=?", (contact_id,)).fetchone())


def add_application_task(
    conn: sqlite3.Connection,
    application_id: str,
    *,
    title: str,
    due_at: str | None = None,
    user_id: str,
    timezone_name: str | None = None,
) -> dict[str, Any]:
    """Add a task; ``due_at`` is stored as an aware instant.

    A browser sends its own IANA zone. Callers without one (the agent's approved
    proposals) fall back to the student's resolved timezone, never UTC.
    """
    application_detail(conn, application_id, user_id=user_id)
    if not title.strip():
        raise ValueError("Task title is required")
    zone = named_timezone(timezone_name) if timezone_name else user_timezone(conn, user_id)
    due_at = zone.normalize_instant(due_at, field="due_at")
    task_id = f"task-{uuid4().hex}"
    timestamp = utc_now()
    with conn:
        conn.execute(
            """
            INSERT INTO application_tasks(
                id, application_id, user_id, title, due_at, status, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, 'open', ?, ?)
            """,
            (task_id, application_id, user_id, title.strip(), due_at or None, timestamp, timestamp),
        )
        conn.execute(
            """
            INSERT INTO application_events(
                application_id, event_type, from_stage, to_stage, detail_json, created_at
            ) VALUES(?, 'task_added', NULL, NULL, ?, ?)
            """,
            (application_id, json.dumps({"task_id": task_id, "title": title.strip()}), timestamp),
        )
    return dict(conn.execute("SELECT * FROM application_tasks WHERE id=?", (task_id,)).fetchone())


def update_application_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    status: str,
    user_id: str,
) -> dict[str, Any]:
    if status not in {"open", "done"}:
        raise ValueError("Task status must be open or done")
    row = conn.execute(
        "SELECT * FROM application_tasks WHERE id=? AND user_id=?",
        (task_id, user_id),
    ).fetchone()
    if not row:
        raise ApplicationNotFoundError(task_id)
    timestamp = utc_now()
    with conn:
        conn.execute(
            "UPDATE application_tasks SET status=?, updated_at=? WHERE id=? AND user_id=?",
            (status, timestamp, task_id, user_id),
        )
        conn.execute(
            """
            INSERT INTO application_events(
                application_id, event_type, from_stage, to_stage, detail_json, created_at
            ) VALUES(?, 'task_status_changed', NULL, NULL, ?, ?)
            """,
            (row["application_id"], json.dumps({"task_id": task_id, "status": status}), timestamp),
        )
    return dict(conn.execute("SELECT * FROM application_tasks WHERE id=?", (task_id,)).fetchone())


def update_application(
    conn: sqlite3.Connection,
    application_id: str,
    *,
    stage: str | None = None,
    notes: str | None = None,
    follow_up_at: str | None = None,
    user_id: str,
    source: str = "user",
    timezone_name: str = "UTC",
) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    existing = conn.execute(
        "SELECT * FROM applications WHERE id=? AND user_id=?",
        (application_id, user_id),
    ).fetchone()
    if not existing:
        raise ApplicationNotFoundError(application_id)
    if stage is not None and stage not in APPLICATION_STAGES:
        raise ValueError(f"Unsupported application stage: {stage}")
    next_stage = stage or str(existing["stage"])
    next_notes = str(existing["notes"]) if notes is None else notes
    next_follow_up = existing["follow_up_at"] if follow_up_at is None else _normalize_due_at(follow_up_at, timezone_name)
    timestamp = utc_now()
    applied_at = existing["applied_at"]
    if next_stage == "applied" and not applied_at:
        applied_at = timestamp
    with conn:
        conn.execute(
            """
            UPDATE applications
            SET stage=?, notes=?, follow_up_at=?, applied_at=?, updated_at=?
            WHERE id=? AND user_id=?
            """,
            (
                next_stage,
                next_notes,
                next_follow_up,
                applied_at,
                timestamp,
                application_id,
                user_id,
            ),
        )
        if next_stage != existing["stage"]:
            conn.execute(
                """
                INSERT INTO application_events(
                    application_id, event_type, from_stage, to_stage,
                    detail_json, created_at
                ) VALUES(?, 'stage_changed', ?, ?, ?, ?)
                """,
                (
                    application_id,
                    existing["stage"],
                    next_stage,
                    json.dumps({"source": source}),
                    timestamp,
                ),
            )
        changed_fields = []
        if notes is not None and notes != existing["notes"]:
            changed_fields.append("notes")
        if follow_up_at is not None and next_follow_up != existing["follow_up_at"]:
            changed_fields.append("follow_up_at")
        if changed_fields:
            conn.execute(
                """
                INSERT INTO application_events(
                    application_id, event_type, from_stage, to_stage,
                    detail_json, created_at
                ) VALUES(?, 'application_updated', NULL, NULL, ?, ?)
                """,
                (
                    application_id,
                    json.dumps({"source": source, "fields": changed_fields}),
                    timestamp,
                ),
            )
        if next_stage in TERMINAL_APPLICATION_STAGES or (follow_up_at is not None and not next_follow_up):
            conn.execute(
                """
                UPDATE reminders SET status='cancelled', updated_at=?
                WHERE application_id=? AND user_id=? AND reminder_type='follow_up' AND status='scheduled'
                """,
                (timestamp, application_id, user_id),
            )
        elif follow_up_at is not None and next_follow_up:
            reminder_id = f"reminder-{application_id}-follow-up"
            conn.execute(
                """
                INSERT INTO reminders(
                    id, application_id, user_id, reminder_type, due_at,
                    timezone, status, created_at, updated_at
                ) VALUES(?, ?, ?, 'follow_up', ?, ?, 'scheduled', ?, ?)
                ON CONFLICT(application_id, user_id, reminder_type) DO UPDATE SET
                    due_at=excluded.due_at,
                    timezone=excluded.timezone,
                    status='scheduled',
                    updated_at=excluded.updated_at
                """,
                (reminder_id, application_id, user_id, next_follow_up, timezone_name, timestamp, timestamp),
            )
    return dict(
        conn.execute(
            "SELECT * FROM applications WHERE id=? AND user_id=?",
            (application_id, user_id),
        ).fetchone()
    )


def _normalize_due_at(value: str | None, timezone_name: str) -> str | None:
    if value is None or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("follow_up_at must be an ISO date or date-time") from exc
    if parsed.tzinfo is None:
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
        except ZoneInfoNotFoundError as exc:
            raise ValueError("Unknown IANA timezone") from exc
    return parsed.isoformat()


def import_applications(
    conn: sqlite3.Connection,
    records: list[dict[str, Any]],
    *,
    user_id: str,
) -> dict[str, Any]:
    if len(records) > 5_000:
        raise ValueError("Application imports are limited to 5,000 rows")
    imported = 0
    errors: list[dict[str, Any]] = []
    for index, record in enumerate(records, start=1):
        opportunity_id = str(record.get("opportunity_id", "")).strip()
        stage = str(record.get("stage", "applying")).strip()
        if not opportunity_id or stage not in APPLICATION_STAGES:
            errors.append({"row": index, "detail": "A valid opportunity_id and stage are required"})
            continue
        if not _opportunity_exists(conn, opportunity_id):
            errors.append({"row": index, "detail": f"Opportunity not found: {opportunity_id}"})
            continue
        existing = conn.execute(
            "SELECT id FROM applications WHERE opportunity_id=? AND user_id=?",
            (opportunity_id, user_id),
        ).fetchone()
        if existing:
            application_id = str(existing["id"])
        else:
            application_id = str(
                record_intent(conn, opportunity_id, "apply_opened", user_id=user_id)["application_id"]
            )
        update_application(
            conn,
            application_id,
            stage=stage,
            notes=str(record.get("notes", ""))[:10_000],
            follow_up_at=str(record.get("follow_up_at") or "")[:80],
            user_id=user_id,
            source="application_import",
        )
        imported += 1
    return {"imported": imported, "skipped": len(errors), "errors": errors[:100]}
