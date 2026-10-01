"""The history of each draft: every version kept, and putting an earlier one back.

Pure storage over outreach_draft_versions and outreach_targets, with no model involved, so bounce handling
(outreach_delivery) and saving a drafted version (outreach_drafting) both depend on this and not on each other.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any
from uuid import uuid4

from .outreach import DRAFT_KINDS, DRAFT_META, cancel_schedules, log_event, get_target
from .schema import utc_now


class DraftVersionNotFoundError(LookupError):
    pass


def insert_version(
    conn: sqlite3.Connection, target_id: str, user_id: str, kind: str, *, source: str,
    subject: str, body: str, claims_json: str, generated_by: str, comments: str, created_at: str,
) -> str:
    version_id = f"draft-version-{uuid4().hex}"
    conn.execute(
        """
        INSERT INTO outreach_draft_versions(id, target_id, user_id, kind, source, subject, body, claims_json, generated_by, comments, created_at)
        VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (version_id, target_id, user_id, kind, source, subject, body, claims_json, generated_by, comments, created_at),
    )
    return version_id


def keep_current_draft(conn: sqlite3.Connection, target_id: str, user_id: str, kind: str) -> None:
    """Store the draft in the editor before it is replaced, unless a version already holds that text.

    This is what keeps a hand-edited draft, or one written before versions were
    kept, from being lost to a regeneration.
    """
    subject_field, body_field, _ = DRAFT_KINDS[kind]
    claims_field, generated_field = DRAFT_META[kind]
    row = conn.execute(
        f"SELECT {subject_field}, {body_field}, {claims_field}, {generated_field}, updated_at "
        "FROM outreach_targets WHERE id=? AND user_id=?",
        (target_id, user_id),
    ).fetchone()
    if not row or not (row[0] or row[1]):
        return
    kept = conn.execute(
        "SELECT 1 FROM outreach_draft_versions WHERE target_id=? AND user_id=? AND kind=? AND subject=? AND body=?",
        (target_id, user_id, kind, row[0], row[1]),
    ).fetchone()
    if kept:
        return
    insert_version(
        conn, target_id, user_id, kind, source="saved", subject=row[0], body=row[1],
        claims_json=row[2] or "[]", generated_by=row[3] or "", comments="", created_at=row[4],
    )


def draft_versions(conn: sqlite3.Connection, target_id: str, *, user_id: str, kind: str) -> list[dict[str, Any]]:
    """Every stored draft of one kind, oldest first, marking the one in the editor now."""
    if kind not in DRAFT_KINDS:
        raise ValueError("kind must be initial or follow_up")
    target = get_target(conn, target_id, user_id=user_id)
    subject_field, body_field, _ = DRAFT_KINDS[kind]
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT id, kind, source, subject, body, claims_json, generated_by, comments, created_at
        FROM outreach_draft_versions WHERE target_id=? AND user_id=? AND kind=?
        ORDER BY created_at, id
        """,
        (target_id, user_id, kind),
    ).fetchall()
    versions = []
    for row in rows:
        version = dict(row)
        version["claims"] = json.loads(version.pop("claims_json") or "[]")
        version["is_current"] = version["subject"] == target[subject_field] and version["body"] == target[body_field]
        versions.append(version)
    return versions


def restore_draft_version(conn: sqlite3.Connection, target_id: str, version_id: str, *, user_id: str) -> dict[str, Any]:
    """Put an earlier draft back in the editor, keeping the one it replaces.

    The restored draft goes back to "generated": it needs approval again, like
    any draft whose words changed.
    """
    target = get_target(conn, target_id, user_id=user_id)
    conn.row_factory = sqlite3.Row
    version = conn.execute(
        "SELECT * FROM outreach_draft_versions WHERE id=? AND target_id=? AND user_id=?",
        (version_id, target_id, user_id),
    ).fetchone()
    if not version:
        raise DraftVersionNotFoundError(version_id)
    kind = version["kind"]
    subject_field, body_field, status_field = DRAFT_KINDS[kind]
    claims_field, generated_field = DRAFT_META[kind]
    if target[subject_field] == version["subject"] and target[body_field] == version["body"]:
        return target
    timestamp = utc_now()
    assignments: dict[str, Any] = {
        subject_field: version["subject"], body_field: version["body"], status_field: "generated",
        claims_field: version["claims_json"], generated_field: version["generated_by"],
    }
    if kind == "initial":
        assignments.update(draft_generated_at=version["created_at"], draft_approved_at=None)
        if target["status"] == "not_started":
            assignments["status"] = "drafted"
    label = "draft" if kind == "initial" else "follow-up"
    with conn:
        keep_current_draft(conn, target_id, user_id, kind)
        conn.execute(
            f"UPDATE outreach_targets SET {', '.join(f'{column}=?' for column in assignments)}, updated_at=? WHERE id=? AND user_id=?",
            [*assignments.values(), timestamp, target_id, user_id],
        )
        if target[status_field] == "approved":
            log_event(conn, target_id, user_id, "approval_withdrawn", detail=f"An earlier {label} was restored")
            cancel_schedules(conn, target_id, user_id, [kind], f"An earlier {label} was restored after you scheduled it")
        log_event(conn, target_id, user_id, "draft_restored" if kind == "initial" else "follow_up_restored",
             detail=f"Restored the {label} from {version['created_at']}")
        if assignments.get("status"):
            log_event(conn, target_id, user_id, "status", from_status=target["status"], to_status="drafted")
    return get_target(conn, target_id, user_id=user_id)
