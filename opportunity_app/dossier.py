"""Private evidence-backed career dossier and granular consent shares."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from .auth import hash_secret
from .profile import is_answered
from .schema import utc_now


class DossierNotFoundError(LookupError):
    pass


def _sync_confirmed_facts(conn: sqlite3.Connection, user_id: str) -> None:
    rows = conn.execute(
        "SELECT field_path, value_json, source, updated_at FROM profile_facts WHERE user_id=? AND confirmed=1",
        (user_id,),
    ).fetchall()
    rows = [row for row in rows if is_answered(json.loads(row[1]))]
    timestamp = utc_now()
    with conn:
        # A fact that is no longer confirmed must leave the dossier, and any
        # share still pointing at it is revoked rather than silently shrinking.
        # Deleting (not status='deleted') lets a later re-confirmation return.
        confirmed_paths = {row[0] for row in rows}
        stale = conn.execute(
            "SELECT id, field_path FROM dossier_items WHERE user_id=? AND item_type='confirmed_fact'",
            (user_id,),
        ).fetchall()
        for item_id, field_path in stale:
            if field_path in confirmed_paths:
                continue
            _revoke_grants_containing(conn, item_id, user_id, timestamp)
            conn.execute("DELETE FROM dossier_items WHERE id=? AND user_id=?", (item_id, user_id))
        for row in rows:
            item_id = f"dossier-fact-{hashlib.sha256(f'{user_id}|{row[0]}'.encode()).hexdigest()[:24]}"
            evidence = [{"type": "profile_fact", "field_path": row[0], "source": row[2]}]
            conn.execute(
                """
                INSERT INTO dossier_items(
                    id, user_id, item_type, field_path, value_json, evidence_json,
                    status, created_at, updated_at
                ) VALUES(?, ?, 'confirmed_fact', ?, ?, ?, 'active', ?, ?)
                ON CONFLICT(user_id, item_type, field_path) DO UPDATE SET
                    value_json=excluded.value_json, evidence_json=excluded.evidence_json,
                    updated_at=excluded.updated_at
                """,
                (item_id, user_id, row[0], row[1], json.dumps(evidence), row[3], row[3]),
            )


def dossier(conn: sqlite3.Connection, *, user_id: str) -> dict[str, Any]:
    timestamp = utc_now()
    with conn:
        conn.execute("INSERT OR IGNORE INTO dossier_settings(user_id, updated_at) VALUES(?, ?)", (user_id, timestamp))
    setting = conn.execute("SELECT * FROM dossier_settings WHERE user_id=?", (user_id,)).fetchone()
    if not setting["paused"]:
        _sync_confirmed_facts(conn, user_id)
    rows = conn.execute("SELECT * FROM dossier_items WHERE user_id=? AND status='active' ORDER BY item_type, field_path", (user_id,)).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["value"] = json.loads(row["value_json"])
        item["evidence"] = json.loads(row["evidence_json"])
        item.pop("value_json", None)
        item.pop("evidence_json", None)
        items.append(item)
    grants = []
    for grant in conn.execute("SELECT id, recipient, item_ids_json, status, expires_at, created_at, revoked_at FROM dossier_consent_grants WHERE user_id=? ORDER BY created_at DESC", (user_id,)).fetchall():
        record = dict(grant)
        record["item_ids"] = json.loads(record.pop("item_ids_json"))
        record["access_log"] = [dict(entry) for entry in conn.execute(
            "SELECT action, accessed_at, detail_json FROM dossier_access_log WHERE grant_id=? ORDER BY accessed_at", (grant["id"],)
        ).fetchall()]
        grants.append(record)
    return {"settings": {"paused": bool(setting["paused"]), "retention_days": setting["retention_days"], "updated_at": setting["updated_at"]}, "items": items, "shares": grants}


def update_settings(conn: sqlite3.Connection, paused: bool, retention_days: int, *, user_id: str) -> dict[str, Any]:
    if not 1 <= retention_days <= 3650:
        raise ValueError("retention_days must be between 1 and 3650")
    timestamp = utc_now()
    with conn:
        conn.execute(
            """
            INSERT INTO dossier_settings(user_id, paused, retention_days, updated_at)
            VALUES(?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET
                paused=excluded.paused, retention_days=excluded.retention_days, updated_at=excluded.updated_at
            """,
            (user_id, int(paused), retention_days, timestamp),
        )
    return dossier(conn, user_id=user_id)["settings"]


def save_item(conn: sqlite3.Connection, item_type: str, field_path: str, value: Any, evidence: list[dict[str, Any]], *, user_id: str) -> dict[str, Any]:
    if item_type not in {"user_opinion", "deterministic_analysis", "ai_suggestion"}:
        raise ValueError("Only opinion, analysis, and suggestion items can be added here")
    if not field_path.strip():
        raise ValueError("field_path is required")
    item_id = f"dossier-{uuid4().hex}"
    timestamp = utc_now()
    with conn:
        conn.execute(
            """
            INSERT INTO dossier_items(id, user_id, item_type, field_path, value_json, evidence_json, status, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, 'active', ?, ?)
            ON CONFLICT(user_id, item_type, field_path) DO UPDATE SET
                value_json=excluded.value_json, evidence_json=excluded.evidence_json,
                status='active', updated_at=excluded.updated_at
            """,
            (item_id, user_id, item_type, field_path.strip(), json.dumps(value), json.dumps(evidence), timestamp, timestamp),
        )
    return next(item for item in dossier(conn, user_id=user_id)["items"] if item["item_type"] == item_type and item["field_path"] == field_path.strip())


def delete_item(conn: sqlite3.Connection, item_id: str, *, user_id: str) -> None:
    timestamp = utc_now()
    with conn:
        cursor = conn.execute("UPDATE dossier_items SET status='deleted', updated_at=? WHERE id=? AND user_id=?", (timestamp, item_id, user_id))
        if not cursor.rowcount:
            raise DossierNotFoundError(item_id)
        _revoke_grants_containing(conn, item_id, user_id, timestamp)


def delete_all(conn: sqlite3.Connection, *, user_id: str) -> None:
    timestamp = utc_now()
    with conn:
        conn.execute("UPDATE dossier_items SET status='deleted', updated_at=? WHERE user_id=?", (timestamp, user_id))
        conn.execute("UPDATE dossier_consent_grants SET status='revoked', revoked_at=? WHERE user_id=? AND status='active'", (timestamp, user_id))
        conn.execute("UPDATE dossier_settings SET paused=1, updated_at=? WHERE user_id=?", (timestamp, user_id))


def _revoke_grants_containing(conn: sqlite3.Connection, item_id: str, user_id: str, timestamp: str) -> None:
    grants = conn.execute("SELECT id, item_ids_json FROM dossier_consent_grants WHERE user_id=? AND status='active'", (user_id,)).fetchall()
    for grant in grants:
        if item_id in json.loads(grant["item_ids_json"]):
            conn.execute("UPDATE dossier_consent_grants SET status='revoked', revoked_at=? WHERE id=?", (timestamp, grant["id"]))


def share_preview(conn: sqlite3.Connection, item_ids: list[str], *, user_id: str) -> list[dict[str, Any]]:
    active = {item["id"]: item for item in dossier(conn, user_id=user_id)["items"]}
    if not item_ids or any(item_id not in active for item_id in item_ids):
        raise ValueError("Every shared dossier item must be active and owned by the user")
    return [active[item_id] for item_id in item_ids]


def create_share(conn: sqlite3.Connection, recipient: str, item_ids: list[str], expires_in_days: int, *, user_id: str) -> dict[str, Any]:
    settings = dossier(conn, user_id=user_id)["settings"]
    if settings["paused"]:
        raise ValueError("Dossier memory and sharing are paused")
    preview = share_preview(conn, item_ids, user_id=user_id)
    if not recipient.strip() or not 1 <= expires_in_days <= 365:
        raise ValueError("Recipient and an expiration from 1 to 365 days are required")
    token = secrets.token_urlsafe(32)
    token_hash = hash_secret(token)
    grant_id = f"grant-{uuid4().hex}"
    created = datetime.now(timezone.utc)
    expires = created + timedelta(days=expires_in_days)
    with conn:
        conn.execute(
            """
            INSERT INTO dossier_consent_grants(
                id, user_id, recipient, item_ids_json, token_hash, status, expires_at, created_at
            ) VALUES(?, ?, ?, ?, ?, 'active', ?, ?)
            """,
            (grant_id, user_id, recipient.strip(), json.dumps(item_ids), token_hash, expires.isoformat(), created.isoformat()),
        )
        conn.execute("INSERT INTO dossier_access_log(grant_id, action, accessed_at, detail_json) VALUES(?, 'created', ?, ?)", (grant_id, created.isoformat(), json.dumps({"item_count": len(preview)})))
    return {"id": grant_id, "recipient": recipient.strip(), "item_ids": item_ids, "status": "active", "expires_at": expires.isoformat(), "share_token": token, "preview": preview}


def revoke_share(conn: sqlite3.Connection, grant_id: str, *, user_id: str) -> dict[str, Any]:
    timestamp = utc_now()
    with conn:
        cursor = conn.execute("UPDATE dossier_consent_grants SET status='revoked', revoked_at=? WHERE id=? AND user_id=? AND status='active'", (timestamp, grant_id, user_id))
        if not cursor.rowcount:
            raise DossierNotFoundError(grant_id)
        conn.execute("INSERT INTO dossier_access_log(grant_id, action, accessed_at) VALUES(?, 'revoked', ?)", (grant_id, timestamp))
    return {"id": grant_id, "status": "revoked", "revoked_at": timestamp}


def read_share(conn: sqlite3.Connection, token: str) -> dict[str, Any]:
    token_hash = hash_secret(token)
    row = conn.execute("SELECT * FROM dossier_consent_grants WHERE token_hash=?", (token_hash,)).fetchone()
    if not row or row["status"] != "active":
        raise DossierNotFoundError("share")
    if datetime.fromisoformat(row["expires_at"]) <= datetime.now(timezone.utc):
        with conn:
            conn.execute("UPDATE dossier_consent_grants SET status='expired' WHERE id=?", (row["id"],))
        raise DossierNotFoundError("share")
    item_ids = json.loads(row["item_ids_json"])
    placeholders = ",".join("?" for _ in item_ids)
    items = conn.execute(f"SELECT * FROM dossier_items WHERE user_id=? AND status='active' AND id IN ({placeholders})", [row["user_id"], *item_ids]).fetchall()
    if len(items) != len(item_ids):
        raise DossierNotFoundError("share")
    result_items = [{"id": item["id"], "item_type": item["item_type"], "field_path": item["field_path"], "value": json.loads(item["value_json"]), "evidence": json.loads(item["evidence_json"])} for item in items]
    timestamp = utc_now()
    with conn:
        conn.execute("INSERT INTO dossier_access_log(grant_id, action, accessed_at) VALUES(?, 'viewed', ?)", (row["id"], timestamp))
    return {"recipient": row["recipient"], "expires_at": row["expires_at"], "items": result_items, "accessed_at": timestamp}
