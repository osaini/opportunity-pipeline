"""Role-scoped employer, school, and administration workflows."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any
from uuid import uuid4

from .auth import hash_secret
from .dossier import DossierNotFoundError, read_share
from .schema import utc_now


PROTECTED_CRITERIA = re.compile(
    r"\b(age|race|ethnic|gender|sex|religion|disab\w*|veteran|marital|pregnan\w*|national origin|citizen\w*|"
    r"zip|postal|address|photo|pronoun|first name|last name|school|university|college)\b",
    re.IGNORECASE,
)


class EmployerNotFoundError(LookupError):
    pass


def ensure_actor(conn: sqlite3.Connection, user_id: str, role: str) -> None:
    timestamp = utc_now()
    with conn:
        conn.execute(
            """
            INSERT INTO users(id, display_name, role, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET role=excluded.role, updated_at=excluded.updated_at
            """,
            (user_id, role.title(), role, timestamp, timestamp),
        )


def audit(conn: sqlite3.Connection, actor: str, action: str, target_type: str, target_id: str, detail: dict[str, Any] | None = None) -> None:
    conn.execute(
        "INSERT INTO operational_audit(actor_user_id, action, target_type, target_id, detail_json, created_at) VALUES(?, ?, ?, ?, ?, ?)",
        (actor, action, target_type, target_id, json.dumps(detail or {}), utc_now()),
    )


def create_organization(conn: sqlite3.Connection, name: str, organization_type: str, actor: str) -> dict[str, Any]:
    ensure_actor(conn, actor, "employer" if organization_type == "employer" else "school")
    if organization_type not in {"employer", "school"} or not name.strip():
        raise ValueError("A valid organization name and type are required")
    organization_id = f"org-{uuid4().hex}"
    timestamp = utc_now()
    with conn:
        conn.execute("INSERT INTO organizations(id, name, organization_type, created_at, updated_at) VALUES(?, ?, ?, ?, ?)", (organization_id, name.strip(), organization_type, timestamp, timestamp))
        conn.execute("INSERT INTO organization_memberships(organization_id, user_id, membership_role, created_at) VALUES(?, ?, 'owner', ?)", (organization_id, actor, timestamp))
        audit(conn, actor, "organization_created", "organization", organization_id)
    return organization_record(conn, organization_id, actor)


def organization_record(conn: sqlite3.Connection, organization_id: str, actor: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT o.*, m.membership_role FROM organizations o JOIN organization_memberships m ON m.organization_id=o.id
        WHERE o.id=? AND m.user_id=?
        """,
        (organization_id, actor),
    ).fetchone()
    if not row:
        raise EmployerNotFoundError(organization_id)
    return dict(row)


def create_requisition(conn: sqlite3.Connection, organization_id: str, title: str, description: str, rubric: list[dict[str, Any]], actor: str) -> dict[str, Any]:
    organization = organization_record(conn, organization_id, actor)
    if organization["organization_type"] != "employer":
        raise ValueError("Only employer organizations can create requisitions")
    if organization["verification_status"] != "verified":
        raise ValueError("Employer organization must be verified before creating requisitions")
    if not title.strip() or not rubric:
        raise ValueError("Title and rubric are required")
    for criterion in rubric:
        name = str(criterion.get("criterion", ""))
        if not name or PROTECTED_CRITERIA.search(name):
            raise ValueError("Rubric criteria cannot use protected attributes or proxies")
        weight = int(criterion.get("weight", 0))
        if weight < 0 or weight > 100:
            raise ValueError("Rubric weights must be between 0 and 100")
    requisition_id = f"req-{uuid4().hex}"
    timestamp = utc_now()
    with conn:
        conn.execute("INSERT INTO requisitions(id, organization_id, title, description, rubric_json, status, created_at, updated_at) VALUES(?, ?, ?, ?, ?, 'draft', ?, ?)", (requisition_id, organization_id, title.strip(), description, json.dumps(rubric), timestamp, timestamp))
        audit(conn, actor, "requisition_created", "requisition", requisition_id)
    return requisition_record(conn, requisition_id, actor)


def requisition_record(conn: sqlite3.Connection, requisition_id: str, actor: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT r.*, o.name AS organization_name FROM requisitions r
        JOIN organizations o ON o.id=r.organization_id
        JOIN organization_memberships m ON m.organization_id=o.id
        WHERE r.id=? AND m.user_id=?
        """,
        (requisition_id, actor),
    ).fetchone()
    if not row:
        raise EmployerNotFoundError(requisition_id)
    result = dict(row)
    result["rubric"] = json.loads(row["rubric_json"])
    result.pop("rubric_json", None)
    return result


def list_requisitions(conn: sqlite3.Connection, actor: str) -> list[dict[str, Any]]:
    ids = conn.execute(
        """SELECT r.id FROM requisitions r JOIN organization_memberships m
           ON m.organization_id=r.organization_id WHERE m.user_id=? ORDER BY r.created_at DESC""",
        (actor,),
    ).fetchall()
    return [requisition_record(conn, row["id"], actor) for row in ids]


def import_requisitions(conn: sqlite3.Connection, organization_id: str, rows: list[dict[str, Any]], actor: str) -> list[dict[str, Any]]:
    """Import a deliberately small, transparent ATS interchange format."""
    if len(rows) > 500:
        raise ValueError("At most 500 requisitions can be imported at once")
    created = []
    for row in rows:
        created.append(create_requisition(
            conn,
            organization_id,
            str(row.get("title", "")),
            str(row.get("description", "")),
            list(row.get("rubric") or []),
            actor,
        ))
    return created


def _candidate_ranking(
    rubric: list[dict[str, Any]], evidence: list[dict[str, Any]]
) -> dict[str, Any]:
    """Score only explicit consented evidence and expose the full calculation."""
    total_weight = sum(int(item.get("weight", 0)) for item in rubric) or 1
    earned_weight = 0
    criteria = []
    for item in rubric:
        criterion = str(item.get("criterion", "")).strip()
        weight = int(item.get("weight", 0))
        matched_ids = [
            str(record.get("id", ""))
            for record in evidence
            if criterion.casefold() in json.dumps(record, sort_keys=True).casefold()
        ]
        matched_ids = [item_id for item_id in matched_ids if item_id]
        matched = bool(matched_ids)
        if matched:
            earned_weight += weight
        criteria.append(
            {
                "criterion": criterion,
                "weight": weight,
                "matched": matched,
                "evidence_item_ids": matched_ids,
                "method": "case-insensitive exact phrase in consented dossier item",
            }
        )
    return {
        "score": round(earned_weight / total_weight * 100),
        "earned_weight": earned_weight,
        "total_weight": total_weight,
        "criteria": criteria,
        "decision_boundary": "Human review required; this score is not a hiring decision.",
    }


def add_candidate_from_share(conn: sqlite3.Connection, requisition_id: str, share_token: str, actor: str) -> dict[str, Any]:
    requisition = requisition_record(conn, requisition_id, actor)
    shared = read_share(conn, share_token)
    if shared["recipient"].casefold() != requisition["organization_name"].casefold():
        raise ValueError("The consent grant recipient does not match this organization")
    token_hash = hash_secret(share_token)
    grant = conn.execute("SELECT id, status FROM dossier_consent_grants WHERE token_hash=?", (token_hash,)).fetchone()
    if not grant or grant["status"] != "active":
        raise DossierNotFoundError("share")
    rubric = requisition["rubric"]
    ranking = _candidate_ranking(rubric, shared["items"])
    score = int(ranking["score"])
    candidate_id = f"candidate-{uuid4().hex}"
    timestamp = utc_now()
    with conn:
        conn.execute(
            """
            INSERT INTO employer_candidates(id, requisition_id, consent_grant_id, evidence_json, score, status, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, 'review', ?, ?)
            """,
            (candidate_id, requisition_id, grant["id"], json.dumps(shared["items"]), score, timestamp, timestamp),
        )
        audit(conn, actor, "consented_candidate_added", "candidate", candidate_id, {"grant_id": grant["id"]})
    return candidate_record(conn, candidate_id, actor)


def candidate_record(conn: sqlite3.Connection, candidate_id: str, actor: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT c.*, g.status AS consent_status, g.expires_at, r.organization_id,
               r.rubric_json
        FROM employer_candidates c JOIN requisitions r ON r.id=c.requisition_id
        JOIN organization_memberships m ON m.organization_id=r.organization_id
        JOIN dossier_consent_grants g ON g.id=c.consent_grant_id
        WHERE c.id=? AND m.user_id=?
        """,
        (candidate_id, actor),
    ).fetchone()
    if not row or row["consent_status"] != "active":
        raise EmployerNotFoundError(candidate_id)
    result = dict(row)
    result["evidence"] = json.loads(row["evidence_json"])
    result["ranking_explanation"] = _candidate_ranking(
        json.loads(row["rubric_json"]), result["evidence"]
    )
    result["score_integrity"] = (
        int(result["score"]) == int(result["ranking_explanation"]["score"])
    )
    result.pop("evidence_json", None)
    result.pop("rubric_json", None)
    result["recommendation"] = "Human review required; this score only counts explicit rubric evidence."
    return result


def decide_candidate(conn: sqlite3.Connection, candidate_id: str, next_status: str, reason: str, actor: str) -> dict[str, Any]:
    if next_status not in {"shortlisted", "interview", "offer", "rejected"} or not reason.strip():
        raise ValueError("A human status and disposition reason are required")
    candidate = candidate_record(conn, candidate_id, actor)
    timestamp = utc_now()
    decision_id = f"decision-{uuid4().hex}"
    with conn:
        conn.execute("UPDATE employer_candidates SET status=?, updated_at=? WHERE id=?", (next_status, timestamp, candidate_id))
        conn.execute("INSERT INTO employer_decisions(id, candidate_id, actor_user_id, from_status, to_status, reason, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)", (decision_id, candidate_id, actor, candidate["status"], next_status, reason.strip(), timestamp))
        audit(conn, actor, "human_candidate_decision", "candidate", candidate_id, {"decision_id": decision_id})
    return candidate_record(conn, candidate_id, actor)


def list_candidates(conn: sqlite3.Connection, requisition_id: str, actor: str) -> list[dict[str, Any]]:
    requisition_record(conn, requisition_id, actor)
    rows = conn.execute(
        "SELECT id FROM employer_candidates WHERE requisition_id=? ORDER BY score DESC, created_at",
        (requisition_id,),
    ).fetchall()
    # Revoked/expired shares disappear immediately rather than leaking stale evidence.
    visible = []
    for row in rows:
        try:
            visible.append(candidate_record(conn, row["id"], actor))
        except EmployerNotFoundError:
            pass
    return visible


def draft_candidate_message(conn: sqlite3.Connection, candidate_id: str, body: str, actor: str) -> dict[str, Any]:
    candidate_record(conn, candidate_id, actor)
    if not body.strip():
        raise ValueError("Message body is required")
    message_id = f"emsg-{uuid4().hex}"
    timestamp = utc_now()
    with conn:
        conn.execute(
            "INSERT INTO employer_messages(id, candidate_id, actor_user_id, body, created_at) VALUES(?, ?, ?, ?, ?)",
            (message_id, candidate_id, actor, body.strip(), timestamp),
        )
        audit(conn, actor, "candidate_message_drafted", "employer_message", message_id)
    return employer_message(conn, message_id, actor)


def employer_message(conn: sqlite3.Connection, message_id: str, actor: str) -> dict[str, Any]:
    row = conn.execute(
        """SELECT msg.* FROM employer_messages msg JOIN employer_candidates c ON c.id=msg.candidate_id
           JOIN requisitions r ON r.id=c.requisition_id JOIN organization_memberships m ON m.organization_id=r.organization_id
           WHERE msg.id=? AND m.user_id=?""",
        (message_id, actor),
    ).fetchone()
    if not row:
        raise EmployerNotFoundError(message_id)
    candidate_record(conn, row["candidate_id"], actor)
    return dict(row)


def approve_candidate_message(conn: sqlite3.Connection, message_id: str, actor: str, *, deliver: Any = None) -> dict[str, Any]:
    message = employer_message(conn, message_id, actor)
    if message["status"] != "draft":
        raise ValueError("Only a draft message can be approved")
    timestamp = utc_now()
    # Without a live provider, approval records intent only. A live adapter
    # may mark the message delivered; approval never implies hidden delivery.
    delivery = "sandbox_suppressed"
    if deliver is not None:
        result = deliver(message)
        if result and result.get("delivered"):
            delivery = "sent"
    with conn:
        conn.execute("UPDATE employer_messages SET status=?, approved_at=? WHERE id=?", (delivery, timestamp, message_id))
        audit(conn, actor, "candidate_message_approved", "employer_message", message_id, {"delivery": delivery})
    return employer_message(conn, message_id, actor)


def propose_interview(conn: sqlite3.Connection, candidate_id: str, starts_at: str, timezone: str, location: str, actor: str) -> dict[str, Any]:
    candidate_record(conn, candidate_id, actor)
    if not starts_at.strip() or not timezone.strip():
        raise ValueError("Interview start time and timezone are required")
    interview_id = f"einterview-{uuid4().hex}"
    timestamp = utc_now()
    with conn:
        conn.execute(
            "INSERT INTO employer_interviews(id, candidate_id, actor_user_id, starts_at, timezone, location, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (interview_id, candidate_id, actor, starts_at, timezone, location, timestamp),
        )
        audit(conn, actor, "interview_proposed", "employer_interview", interview_id)
    return interview_record(conn, interview_id, actor)


def interview_record(conn: sqlite3.Connection, interview_id: str, actor: str) -> dict[str, Any]:
    row = conn.execute(
        """SELECT i.* FROM employer_interviews i JOIN employer_candidates c ON c.id=i.candidate_id
           JOIN requisitions r ON r.id=c.requisition_id JOIN organization_memberships m ON m.organization_id=r.organization_id
           WHERE i.id=? AND m.user_id=?""",
        (interview_id, actor),
    ).fetchone()
    if not row:
        raise EmployerNotFoundError(interview_id)
    candidate_record(conn, row["candidate_id"], actor)
    return dict(row)


def confirm_interview(conn: sqlite3.Connection, interview_id: str, actor: str) -> dict[str, Any]:
    interview = interview_record(conn, interview_id, actor)
    if interview["status"] != "proposed":
        raise ValueError("Only a proposed interview can be confirmed")
    timestamp = utc_now()
    with conn:
        conn.execute("UPDATE employer_interviews SET status='confirmed', confirmed_at=? WHERE id=?", (timestamp, interview_id))
        audit(conn, actor, "interview_confirmed", "employer_interview", interview_id)
    return interview_record(conn, interview_id, actor)


def school_aggregate(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute("SELECT stage, COUNT(*) AS count FROM applications GROUP BY stage").fetchall()
    return {"applications_by_stage": {row["stage"]: int(row["count"]) for row in rows}, "student_count": conn.execute("SELECT COUNT(*) FROM users WHERE role='student'").fetchone()[0], "contains_student_identifiers": False}


def admin_overview(conn: sqlite3.Connection) -> dict[str, Any]:
    sources = [dict(row) for row in conn.execute("""SELECT s.source_key, s.source_name, COUNT(*) AS records, MAX(s.last_seen_at) AS last_seen_at,
        COALESCE(c.enabled, 1) AS enabled, COALESCE(c.moderation_status, 'approved') AS moderation_status
        FROM opportunity_sources s LEFT JOIN source_controls c ON c.source_key=s.source_key
        GROUP BY s.source_key, s.source_name ORDER BY s.source_name""").fetchall()]
    flags = [dict(row) for row in conn.execute("SELECT * FROM feature_flags ORDER BY key").fetchall()]
    audit_rows = [dict(row) for row in conn.execute("SELECT * FROM operational_audit ORDER BY created_at DESC LIMIT 100").fetchall()]
    moderation = [dict(row) for row in conn.execute("SELECT * FROM moderation_items ORDER BY created_at DESC LIMIT 100").fetchall()]
    candidate_rows = conn.execute(
        """SELECT c.score, c.status, c.evidence_json, r.rubric_json
           FROM employer_candidates c
           JOIN requisitions r ON r.id=c.requisition_id"""
    ).fetchall()
    explanation_integrity = []
    positives = []
    negatives = []
    for candidate in candidate_rows:
        ranking = _candidate_ranking(
            json.loads(candidate["rubric_json"]),
            json.loads(candidate["evidence_json"]),
        )
        explanation_integrity.append(int(ranking["score"]) == int(candidate["score"]))
        if candidate["status"] in {"shortlisted", "interview", "offer"}:
            positives.append(int(candidate["score"]))
        elif candidate["status"] == "rejected":
            negatives.append(int(candidate["score"]))
    ranking_quality = None
    if positives and negatives:
        comparisons = [
            1.0 if positive > negative else 0.5 if positive == negative else 0.0
            for positive in positives
            for negative in negatives
        ]
        ranking_quality = round(sum(comparisons) / len(comparisons), 4)
    explanation_coverage = (
        round(sum(explanation_integrity) / len(explanation_integrity), 4)
        if explanation_integrity
        else None
    )
    total_decisions = int(
        conn.execute("SELECT COUNT(*) FROM employer_decisions").fetchone()[0]
    )
    return {"source_health": sources, "feature_flags": flags, "moderation": moderation, "audit": audit_rows,
            "ranking_metrics": {
                "human_decisions": total_decisions,
                "ranking_quality": ranking_quality,
                "ranking_quality_status": (
                    "measured_pairwise_against_human_outcomes"
                    if ranking_quality is not None
                    else "insufficient_positive_and_negative_human_outcomes"
                ),
                "ranking_quality_methodology": "Pairwise rate that a human-positive candidate score exceeds a human-rejected candidate score; ties count 0.5.",
                "explanation_integrity_rate": explanation_coverage,
                "explanation_quality_status": "calculation_integrity_only_no_human_quality_rating",
                "human_override_rate": None,
                "human_override_status": "not_applicable_no_automated_hiring_decision",
                "subgroup_error_rates": None,
                "subgroup_status": "not_measurable_without_explicit_consented_demographic_data",
                "threshold_status": "not_evaluated_until_thresholds_are_agreed",
            }}


def set_source_control(conn: sqlite3.Connection, source_key: str, enabled: bool, moderation_status: str, note: str, actor: str) -> dict[str, Any]:
    if moderation_status not in {"approved", "review", "blocked"}:
        raise ValueError("Invalid moderation status")
    timestamp = utc_now()
    with conn:
        conn.execute("""INSERT INTO source_controls(source_key, enabled, moderation_status, note, updated_by, updated_at)
            VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT(source_key) DO UPDATE SET enabled=excluded.enabled,
            moderation_status=excluded.moderation_status, note=excluded.note, updated_by=excluded.updated_by, updated_at=excluded.updated_at""",
            (source_key, int(enabled), moderation_status, note, actor, timestamp))
        audit(conn, actor, "source_control_updated", "source", source_key, {"enabled": enabled, "moderation_status": moderation_status})
    return dict(conn.execute("SELECT * FROM source_controls WHERE source_key=?", (source_key,)).fetchone())


def create_moderation_item(conn: sqlite3.Connection, target_type: str, target_id: str, reason: str, actor: str) -> dict[str, Any]:
    if not all(value.strip() for value in (target_type, target_id, reason)):
        raise ValueError("Moderation target and reason are required")
    item_id = f"mod-{uuid4().hex}"
    timestamp = utc_now()
    with conn:
        conn.execute("INSERT INTO moderation_items(id, target_type, target_id, reason, created_by, created_at) VALUES(?, ?, ?, ?, ?, ?)",
                     (item_id, target_type, target_id, reason, actor, timestamp))
        audit(conn, actor, "moderation_item_created", target_type, target_id, {"moderation_id": item_id})
    return dict(conn.execute("SELECT * FROM moderation_items WHERE id=?", (item_id,)).fetchone())


def resolve_moderation_item(conn: sqlite3.Connection, item_id: str, status: str, resolution: str, actor: str) -> dict[str, Any]:
    if status not in {"resolved", "dismissed"} or not resolution.strip():
        raise ValueError("Resolution and terminal status are required")
    timestamp = utc_now()
    with conn:
        cursor = conn.execute("UPDATE moderation_items SET status=?, resolution=?, resolved_by=?, resolved_at=? WHERE id=? AND status='open'",
                              (status, resolution, actor, timestamp, item_id))
        if not cursor.rowcount:
            raise EmployerNotFoundError(item_id)
        audit(conn, actor, "moderation_item_resolved", "moderation_item", item_id, {"status": status})
    return dict(conn.execute("SELECT * FROM moderation_items WHERE id=?", (item_id,)).fetchone())


def set_feature_flag(conn: sqlite3.Connection, key: str, enabled: bool, description: str, actor: str) -> dict[str, Any]:
    timestamp = utc_now()
    with conn:
        conn.execute("INSERT INTO feature_flags(key, enabled, description, updated_by, updated_at) VALUES(?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET enabled=excluded.enabled, description=excluded.description, updated_by=excluded.updated_by, updated_at=excluded.updated_at", (key, int(enabled), description, actor, timestamp))
        audit(conn, actor, "feature_flag_updated", "feature_flag", key, {"enabled": enabled})
    return dict(conn.execute("SELECT * FROM feature_flags WHERE key=?", (key,)).fetchone())


def verify_organization(conn: sqlite3.Connection, organization_id: str, approved: bool, actor: str) -> dict[str, Any]:
    ensure_actor(conn, actor, "admin")
    next_status = "verified" if approved else "rejected"
    timestamp = utc_now()
    with conn:
        cursor = conn.execute("UPDATE organizations SET verification_status=?, updated_at=? WHERE id=?", (next_status, timestamp, organization_id))
        if not cursor.rowcount:
            raise EmployerNotFoundError(organization_id)
        audit(conn, actor, "organization_verification", "organization", organization_id, {"status": next_status})
    return dict(conn.execute("SELECT * FROM organizations WHERE id=?", (organization_id,)).fetchone())
