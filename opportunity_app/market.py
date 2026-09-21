"""Reproducible market snapshots and editorially gated issues."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from .profile import get_profile
from .schema import utc_now


class MarketNotFoundError(LookupError):
    pass


def _snapshot_data(conn: sqlite3.Connection, as_of: str) -> tuple[dict[str, Any], list[str]]:
    rows = conn.execute(
        """
        SELECT id, role_type, region, source_name, first_seen_at, deadline_at,
               terms_json, pay_min, pay_max, pay_period
        FROM opportunity_read_model
        WHERE active=1 AND duplicate_of IS NULL AND first_seen_at<=?
        ORDER BY id
        """,
        (as_of,),
    ).fetchall()
    cutoff = (datetime.fromisoformat(as_of.replace("Z", "+00:00")) - timedelta(days=7)).isoformat()
    roles = Counter(str(row["role_type"] or "unknown") for row in rows)
    regions = Counter(str(row["region"] or "unknown") for row in rows)
    sources = Counter(str(row["source_name"] or "unknown") for row in rows)
    terms = Counter(str(term) for row in rows for term in json.loads(row["terms_json"] or "[]"))
    deadlines = [row["deadline_at"] for row in rows if row["deadline_at"]]
    data = {
        "as_of": as_of,
        "active_roles": len(rows),
        "new_last_7_days": sum(str(row["first_seen_at"]) >= cutoff for row in rows),
        "role_types": dict(sorted(roles.items())),
        "regions": dict(sorted(regions.items())),
        "sources": dict(sorted(sources.items())),
        "terms": dict(sorted(terms.items())),
        "known_compensation": sum(row["pay_min"] is not None for row in rows),
        "verified_deadlines": len(deadlines),
        "next_deadline": min(deadlines) if deadlines else None,
        "unknown_compensation": sum(row["pay_min"] is None for row in rows),
        "methodology_flags": {
            "active_only": True,
            "duplicates_excluded": True,
            "unknowns_not_imputed": True,
            "sampled": False,
        },
    }
    return data, [str(row["id"]) for row in rows]


def create_snapshot(conn: sqlite3.Connection, as_of: str | None = None) -> dict[str, Any]:
    resolved_as_of = as_of or datetime.now(timezone.utc).isoformat()
    try:
        datetime.fromisoformat(resolved_as_of.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("as_of must be an ISO timestamp") from exc
    data, opportunity_ids = _snapshot_data(conn, resolved_as_of)
    canonical = json.dumps({"data": data, "opportunity_ids": opportunity_ids}, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    snapshot_id = f"snapshot-{digest[:24]}"
    timestamp = utc_now()
    with conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO market_snapshots(id, as_of, data_json, data_hash, opportunity_ids_json, created_at)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (snapshot_id, resolved_as_of, json.dumps(data), digest, json.dumps(opportunity_ids), timestamp),
        )
    return snapshot_record(conn, snapshot_id)


def snapshot_record(conn: sqlite3.Connection, snapshot_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM market_snapshots WHERE id=?", (snapshot_id,)).fetchone()
    if not row:
        raise MarketNotFoundError(snapshot_id)
    return {"id": row["id"], "as_of": row["as_of"], "data": json.loads(row["data_json"]), "data_hash": row["data_hash"], "opportunity_ids": json.loads(row["opportunity_ids_json"]), "created_at": row["created_at"]}


def verify_snapshot(conn: sqlite3.Connection, snapshot_id: str) -> dict[str, Any]:
    stored = snapshot_record(conn, snapshot_id)
    data, opportunity_ids = _snapshot_data(conn, stored["as_of"])
    canonical = json.dumps({"data": data, "opportunity_ids": opportunity_ids}, sort_keys=True, separators=(",", ":"))
    recomputed = hashlib.sha256(canonical.encode()).hexdigest()
    return {"snapshot_id": snapshot_id, "matches": recomputed == stored["data_hash"], "stored_hash": stored["data_hash"], "recomputed_hash": recomputed}


def create_issue(conn: sqlite3.Connection, snapshot_id: str, title: str, slug: str, *, user_id: str) -> dict[str, Any]:
    snapshot = snapshot_record(conn, snapshot_id)
    profile = get_profile(conn, user_id=user_id)["profile"]
    region_names = [item.get("name", "") if isinstance(item, dict) else str(item) for item in profile.get("regions", [])]
    personalized = {
        "label": "Personalized interpretation — not a market-wide fact",
        "target_regions": {name: snapshot["data"]["regions"].get(name, 0) for name in region_names if name},
        "confirmed_profile_fields_used": ["regions"],
    }
    issue_id = f"issue-{uuid4().hex}"
    timestamp = utc_now()
    methodology = "Counts use the dated active, canonical opportunity snapshot. Unknown compensation and deadlines remain unknown; no values are imputed."
    with conn:
        conn.execute(
            """
            INSERT INTO market_issues(
                id, snapshot_id, slug, title, market_json, personalized_json,
                methodology, status, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, 'draft', ?)
            """,
            (issue_id, snapshot_id, slug, title, json.dumps(snapshot["data"]), json.dumps(personalized), methodology, timestamp),
        )
    return issue_record(conn, issue_id)


def issue_record(conn: sqlite3.Connection, issue_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM market_issues WHERE id=?", (issue_id,)).fetchone()
    if not row:
        raise MarketNotFoundError(issue_id)
    return {**dict(row), "market": json.loads(row["market_json"]), "personalized": json.loads(row["personalized_json"])}


def publish_issue(conn: sqlite3.Connection, issue_id: str) -> dict[str, Any]:
    issue = issue_record(conn, issue_id)
    verification = verify_snapshot(conn, issue["snapshot_id"])
    if not verification["matches"]:
        raise ValueError("Snapshot no longer reproduces; publication is blocked")
    timestamp = utc_now()
    with conn:
        conn.execute("UPDATE market_issues SET status='published', published_at=? WHERE id=?", (timestamp, issue_id))
    return issue_record(conn, issue_id)


def public_issues(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    ids = [row[0] for row in conn.execute("SELECT id FROM market_issues WHERE status='published' ORDER BY published_at DESC").fetchall()]
    result = []
    for issue_id in ids:
        issue = issue_record(conn, str(issue_id))
        result.append({key: value for key, value in issue.items() if key not in {"personalized", "personalized_json"}})
    return result
