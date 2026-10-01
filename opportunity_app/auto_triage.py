"""Saving and passing on new roles by score, after each daily sync (switches auto_save and auto_pass).

Thresholds are the student's own, in their profile, with no defaults:

    "automation": {"auto_save_at": 80, "auto_pass_below": 40}

A missing threshold means its switch cannot be turned on, and the Automation
panel says why (automation.REQUIREMENTS, requirement below). A role is saved
when its score is at or above auto_save_at, and passed when it is below
auto_pass_below and has a description: a sparse posting is never passed,
since its low score may only mean the posting says little.

Only roles the student has never saved, passed, or opened are considered
(no interaction row, and no application), that are active and not duplicates, that carry a score
under the current ruleset, and that were first seen since the switch was
turned on, so turning a switch on never sweeps through the whole backlog.
Every save or pass goes through the ledger with an idempotency key per role
and choice, so a re-run acts once, and each can be undone (the weekly
"Auto-passed this week" list calls it Restore).

Known limit: the daily sync keeps scores only for the local owner
(schema.LOCAL_USER_ID), so these switches work only for that account.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pipeline_core.read_model import RULESET_VERSION
from pipeline_core.visibility import capture_visible_sql

from . import automation
from .database import is_postgres_target
from .profile_store import read_stored_profile
from .schema import LOCAL_USER_ID, connect_product

LOGGER = logging.getLogger(__name__)

HEALTH_COMPONENT = "discovery.auto_triage"
THRESHOLDS = {"auto_save": "auto_save_at", "auto_pass": "auto_pass_below"}
REVIEW_DAYS = 7
_POINTS = re.compile(r"^\s*([+-]?)(\d+)\b")


def _threshold(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
        return None
    return float(value)


def thresholds(conn: sqlite3.Connection, user_id: str) -> dict[str, float | None]:
    """The student's thresholds from their profile's "automation" object; None where missing or not a score."""
    settings = read_stored_profile(conn, user_id).get("automation")
    settings = settings if isinstance(settings, dict) else {}
    return {feature: _threshold(settings.get(key)) for feature, key in THRESHOLDS.items()}


def requirement(conn: sqlite3.Connection, user_id: str, feature: str) -> str:
    """What ``feature`` needs before it can act, or "" (automation.REQUIREMENTS)."""
    if user_id != LOCAL_USER_ID:
        return "Scores are kept only for this computer's main account, so this can't act for yours"
    values = thresholds(conn, user_id)
    key = THRESHOLDS[feature]
    if values[feature] is None:
        return f"Set automation.{key} in your profile to a score from 0 to 100 first"
    save_at, pass_below = values["auto_save"], values["auto_pass"]
    if save_at is not None and pass_below is not None and pass_below > save_at:
        return "Your automation.auto_pass_below is above your automation.auto_save_at, so a role could be both; fix one first"
    return ""


def _switched_on_at(conn: sqlite3.Connection, user_id: str, feature: str) -> str | None:
    """When the switch was last turned on. A switch turned on some other way (no record of when) counts from its row's last change."""
    since = automation.on_since(conn, user_id, feature)
    if since:
        return since
    row = conn.execute(
        "SELECT value, updated_at FROM user_settings WHERE user_id=? AND key=?", (user_id, feature),
    ).fetchone()
    return str(row["updated_at"]) if row is not None and row["value"] == "on" else None


def top_reasons(explanation_json: str | None, limit: int = 3) -> list[str]:
    """The score's biggest adjustments, largest first (the base line is left out)."""
    try:
        reasons = json.loads(explanation_json or "[]")
    except (TypeError, ValueError):
        return []
    weighted = []
    for index, reason in enumerate(reasons if isinstance(reasons, list) else []):
        text = str(reason)
        match = _POINTS.match(text)
        if not match or text.strip().lower().endswith(" base"):
            continue
        weighted.append((-int(match.group(2)), index, text))
    return [text for _weight, _index, text in sorted(weighted)[:limit]]


def candidates(conn: sqlite3.Connection, user_id: str, since: str) -> list[dict[str, Any]]:
    """Untouched (no interaction, no application), active, unique, visible roles first seen at or after ``since``, with a current score."""
    rows = conn.execute(
        f"""
        SELECT o.id, o.title, o.company, o.description, fs.score, fs.explanation_json,
               COALESCE(o.first_seen_at, o.created_at) AS first_seen
        FROM opportunities o
        JOIN fit_scores fs ON fs.opportunity_id = o.id AND fs.user_id = ? AND fs.ruleset_version = ?
        WHERE o.active = 1 AND o.duplicate_of IS NULL
          AND COALESCE(o.first_seen_at, o.created_at) >= ?
          AND NOT EXISTS (SELECT 1 FROM opportunity_interactions i WHERE i.opportunity_id = o.id AND i.user_id = ?)
          AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.opportunity_id = o.id AND a.user_id = ?)
          AND {capture_visible_sql("o")}
        ORDER BY fs.score DESC, o.id
        """,
        (user_id, RULESET_VERSION, since, user_id, user_id, user_id),
    ).fetchall()
    return [dict(row) for row in rows]


def _act(conn: sqlite3.Connection, user_id: str, item: dict[str, Any], choice: str, threshold: float) -> dict[str, Any] | None:
    feature = "auto_save" if choice == "saved" else "auto_pass"
    score = int(item["score"])
    number = f"{threshold:g}"
    what = f"{item['title']} at {item['company']}"
    summary = (
        f"Saved {what}: its score {score} is at or above your {number}" if choice == "saved"
        else f"Passed on {what}: its score {score} is below your {number}"
    )
    reasons = top_reasons(item.get("explanation_json"))
    try:
        return automation.perform(
            conn, user_id=user_id, feature=feature, action_type="opportunity.intent", subject_kind="opportunity",
            subject_id=item["id"], after={"intent": choice, "only_if_untouched": True},
            evidence={
                # What the Automation panel shows as the evidence: the score and what made it.
                "excerpt": f"Score {score}" + (f": {'; '.join(reasons)}" if reasons else ""),
                "subject": what, "title": item["title"], "company": item["company"], "score": score,
                "threshold": threshold, "reasons": reasons, "ruleset_version": RULESET_VERSION,
            },
            summary=summary, basis=f"score{'>=' if choice == 'saved' else '<'}{number}", confidence=None,
            idempotency_key=f"triage:{item['id']}:{choice}", auto=True, policy_version=RULESET_VERSION,
        )
    except automation.NotApplicable:
        return None


def run_auto_triage(conn: sqlite3.Connection, *, user_id: str, now: datetime | None = None) -> dict[str, Any]:
    """Save and pass on new roles by the student's thresholds. Returns what it did and why it did nothing.

    Safe to run again: each role and choice acts once (its idempotency key),
    and a role the student has touched meanwhile is left as it is.
    """
    report: dict[str, Any] = {"saved": [], "passed": [], "considered": 0, "notes": {}}
    enabled = {feature: automation.is_enabled(conn, user_id, feature) for feature in THRESHOLDS}
    values = thresholds(conn, user_id)
    active: dict[str, tuple[float, str]] = {}
    for feature in THRESHOLDS:
        if not enabled[feature]:
            continue
        missing = requirement(conn, user_id, feature)
        since = _switched_on_at(conn, user_id, feature)
        if missing or since is None or values[feature] is None:
            report["notes"][feature] = missing or "It is not on"
            continue
        active[feature] = (values[feature], since)
    if not active:
        return report
    since = min(when for _threshold_value, when in active.values())
    for item in candidates(conn, user_id, since):
        report["considered"] += 1
        first_seen = item["first_seen"]
        score = int(item["score"])
        choice = None
        if "auto_save" in active and score >= active["auto_save"][0] and str(first_seen) >= active["auto_save"][1]:
            choice, threshold = "saved", active["auto_save"][0]
        elif (
            "auto_pass" in active and score < active["auto_pass"][0] and str(first_seen) >= active["auto_pass"][1]
            and str(item.get("description") or "").strip()
        ):
            choice, threshold = "passed", active["auto_pass"][0]
        if choice is None:
            continue
        row = _act(conn, user_id, item, choice, threshold)
        if row is not None and row.get("status") == "applied":
            report[choice].append({"opportunity_id": item["id"], "action_id": row["id"], "score": score})
            if choice == "saved":
                from .resume_variants import safe_pick_after_save

                # A role auto_save saved gets its résumé variant too, like one the student saved.
                safe_pick_after_save(conn, user_id, item["id"])
    return report


def triage_after_sync(target: Path | str, *, user_id: str = LOCAL_USER_ID) -> dict[str, Any] | None:
    """run_auto_triage after a platform sync. Never raises: a failure is logged and recorded in Health, and the sync stands.

    ``target`` is a SQLite path, as a Path or as text (the migrate CLI passes
    text), or a PostgreSQL URL.
    """
    try:
        # As migrate_legacy_database reads its target: connect_product needs a Path for SQLite.
        target = str(target) if is_postgres_target(target) else Path(target).expanduser().resolve()
        conn = connect_product(target)
    except Exception:  # noqa: BLE001 - no database, or it could not be opened: nothing to triage
        LOGGER.exception("Saving and passing on new roles could not start")
        return None
    with closing(conn):
        try:
            on = any(automation.mode(conn, user_id, feature) == "on" for feature in THRESHOLDS)
            report = run_auto_triage(conn, user_id=user_id)
        except Exception as exc:  # noqa: BLE001 - recorded, and the sync still succeeds
            LOGGER.exception("Saving and passing on new roles failed")
            try:
                if getattr(conn, "in_transaction", False):
                    conn.rollback()
                automation.record_health(conn, user_id, HEALTH_COMPONENT, ok=False, error=str(exc))
            except Exception:  # noqa: BLE001
                LOGGER.warning("Could not record the auto-triage failure", exc_info=True)
            return None
        if on:
            try:
                automation.record_health(
                    conn, user_id, HEALTH_COMPONENT, ok=True,
                    detail={"saved": len(report["saved"]), "passed": len(report["passed"]), "considered": report["considered"]},
                )
            except Exception:  # noqa: BLE001 - what was saved and passed stands
                LOGGER.warning("Could not record how saving and passing on new roles went", exc_info=True)
        return report


def auto_passed_this_week(conn: sqlite3.Connection, user_id: str, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """auto_pass actions applied in the last REVIEW_DAYS days and still standing, newest first, each restorable (undo)."""
    since = ((now or datetime.now(timezone.utc)).astimezone(timezone.utc) - timedelta(days=REVIEW_DAYS)).isoformat(timespec="microseconds")
    rows = conn.execute(
        """
        SELECT a.id, a.subject_id, a.summary, a.evidence_json, a.applied_at, o.title, o.company
        FROM automation_actions a LEFT JOIN opportunities o ON o.id = a.subject_id
        WHERE a.user_id=? AND a.feature='auto_pass' AND a.status='applied' AND a.applied_at>=?
        ORDER BY a.applied_at DESC, a.id DESC
        """,
        (user_id, since),
    ).fetchall()
    items = []
    for row in rows:
        try:
            evidence = json.loads(row["evidence_json"] or "{}")
        except (TypeError, ValueError):
            evidence = {}
        items.append({
            "action_id": row["id"], "opportunity_id": row["subject_id"], "summary": row["summary"],
            "title": row["title"] or evidence.get("title") or "", "company": row["company"] or evidence.get("company") or "",
            "score": evidence.get("score"), "threshold": evidence.get("threshold"),
            "reasons": evidence.get("reasons") or [], "applied_at": row["applied_at"],
        })
    return items
