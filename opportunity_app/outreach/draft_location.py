"""A cold email's "(live in ...)" line, kept in step with the company's location without writing the draft again.

The line is the app's own words (drafting.location_line), and every draft puts it in one place: right after the
school's name in the opening, which the draft check enforces (drafting.validate_draft). So when a company's location
is checked after its draft was written, and it is where the student lives, the line goes in there and nothing else in
the draft changes. When the location stops being one (changed to somewhere else, or no longer checked), the app's line
comes out the same way. A draft whose opening does not name the school as the profile has it is left alone: its card
still asks for the line (location.missing_location_message), and no model is asked to place it.

The student's own actions (typing or confirming a location, the Add the location line button) run this at once, and
the automation worker runs it over every draft while Write drafts automatically is on, so a location that a site
check, a filing or a web search established is followed too. Only an unapproved draft changes on its own. An approved
one, perhaps scheduled, changes only from the button, which takes the approval back like any edit.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any, Callable

from .drafting import location_line
from .location import home_terms, mentions_home, student_home, user_regions
from .targets import UNSENT_STATUSES, cancel_schedules, get_target, list_targets, log_event
from .versions import keep_current_draft
from ..student.preparation import confirmed_facts
from ..core.timestamps import utc_now

ADDED_EVENT = "location_line_added"
REMOVED_EVENT = "location_line_removed"
LINE_BASIS = "profile:break_location"


def _opening_end(body: str) -> int:
    """Where the opening ends in ``body``: the first paragraph, or the second too when the first is a greeting alone.

    The same opening the draft check reads (drafting validates the line there), as an offset into the text.
    """
    parts = re.split(r"(\n\s*\n)", body)
    offset, seen = 0, 0
    for index in range(0, len(parts), 2):
        paragraph = parts[index]
        end = offset + len(paragraph)
        if paragraph.strip():
            seen += 1
            stripped = paragraph.strip()
            if not (seen == 1 and "\n" not in stripped and stripped.endswith(",")):
                return end
        offset = end + (len(parts[index + 1]) if index + 1 < len(parts) else 0)
    return len(body)


def _school_pattern(school: str) -> re.Pattern[str]:
    words = school.split()
    return re.compile(r"(?<!\w)" + r"\s+".join(re.escape(word) for word in words) + r"(?!\w)", re.IGNORECASE)


def location_line_edit(target: dict[str, Any], facts: dict[str, Any], regions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The edit that brings an unsent draft's location line in step, or None when it is in step or cannot be made exactly.

    Returns {"action": "add" or "remove", "line", "body"}. Adding needs the school's name, as the profile has it,
    in the opening; removing takes out only the app's own line, word for word.
    """
    body = str(target.get("email_body") or "")
    if not body.strip() or target.get("sent_at") or target.get("status") not in UNSENT_STATUSES:
        return None
    home = student_home(facts, regions)
    if not home:
        return None
    line = location_line(facts, target, regions)
    if line:
        if mentions_home(body, home_terms(home)):
            return None
        school = str(facts.get("school") or "").strip()
        match = _school_pattern(school).search(body, 0, _opening_end(body)) if school else None
        if match is None:
            return None
        return {"action": "add", "line": line, "body": f"{body[:match.end()]} {line}{body[match.end():]}"}
    for variant in (f"(live in {home['phrase']} year-round)", f"(live in {home['phrase']})"):
        found = re.search(r"[ \t]?" + re.escape(variant), body)
        if found:
            return {"action": "remove", "line": variant, "body": body[:found.start()] + body[found.end():]}
    return None


def _removed_because(target: dict[str, Any]) -> str:
    if not target.get("location"):
        return f"{target['company']} has no location on file"
    if not target.get("location_verified"):
        return f"{target['company']}'s location, {target['location']}, is not checked yet"
    return f"{target['company']} is in {target['location']}, not where you live"


def sync_location_line(
    conn: sqlite3.Connection,
    target_id: str,
    *,
    user_id: str,
    approved: bool = False,
    before_write: Callable[[], None] | None = None,
) -> dict[str, Any] | None:
    """Bring one draft's location line in step. Returns {"action", "line"} for a change made, else None.

    ``approved`` (the student's button) lets an approved draft change too: its approval is taken back and a scheduled
    send cancelled, like any edit. Otherwise an approved draft is left as it is. ``before_write`` runs first inside the
    transaction; raising there changes nothing. A draft that changed since it was read is left for the next look.
    """
    target = get_target(conn, target_id, user_id=user_id)
    if target["draft_status"] == "approved" and not approved:
        return None
    facts = confirmed_facts(conn, user_id)
    edit = location_line_edit(target, facts, user_regions(conn, user_id))
    if edit is None:
        return None
    with conn:
        if before_write is not None:
            before_write()
        # Take the write lock first, then check the draft is still the one the edit was made from.
        conn.execute("UPDATE outreach_targets SET updated_at=updated_at WHERE id=? AND user_id=?", (target_id, user_id))
        row = conn.execute(
            "SELECT email_body, draft_status, draft_claims_json FROM outreach_targets WHERE id=? AND user_id=?",
            (target_id, user_id),
        ).fetchone()
        if row is None or (row[0], row[1]) != (target["email_body"], target["draft_status"]):
            return None
        claim = {"text": edit["line"], "basis": LINE_BASIS}
        claims = [item for item in json.loads(row[2] or "[]") if item != claim]
        if edit["action"] == "add":
            claims.append(claim)
        assignments: dict[str, Any] = {"email_body": edit["body"], "draft_claims_json": json.dumps(claims)}
        was_approved = target["draft_status"] == "approved"
        if was_approved:
            assignments.update(draft_status="generated", draft_approved_at=None)
        keep_current_draft(conn, target_id, user_id, "initial")
        conn.execute(
            f"UPDATE outreach_targets SET {', '.join(f'{column}=?' for column in assignments)}, updated_at=? WHERE id=? AND user_id=?",
            [*assignments.values(), utc_now(), target_id, user_id],
        )
        if edit["action"] == "add":
            detail = f"Added {edit['line']} after {facts['school']}: {target['company']} is in {target['location']}"
        else:
            detail = f"Took out {edit['line']}: {_removed_because(target)}"
        log_event(conn, target_id, user_id, ADDED_EVENT if edit["action"] == "add" else REMOVED_EVENT, detail=detail)
        if was_approved:
            log_event(conn, target_id, user_id, "approval_withdrawn", detail="The draft's location line changed after approval")
            cancel_schedules(conn, target_id, user_id, ["initial"], "The draft's location line changed after you scheduled it")
    return {"target_id": target_id, "company": target["company"], "action": edit["action"], "line": edit["line"]}


def sync_location_lines(
    conn: sqlite3.Connection, *, user_id: str, before_write: Callable[[], None] | None = None,
) -> list[dict[str, Any]]:
    """Every unapproved, unsent draft whose location line is out of step, put right. The worker's sweep.

    The check reads no page and calls no model, so it runs over every draft each pass.
    """
    facts = confirmed_facts(conn, user_id)
    regions = user_regions(conn, user_id)
    if not student_home(facts, regions):
        return []
    changed = []
    for item in list_targets(conn, user_id=user_id, interested_only=True, statuses=tuple(sorted(UNSENT_STATUSES))):
        if item["draft_status"] == "approved" or location_line_edit(item, facts, regions) is None:
            continue
        result = sync_location_line(conn, item["id"], user_id=user_id, before_write=before_write)
        if result:
            changed.append(result)
    return changed
