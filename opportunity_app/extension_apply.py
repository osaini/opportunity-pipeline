"""Local-first assisted-apply pairing, context, artifacts, and session state."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .actions import ApplicationNotFoundError, update_application
from .auth import hash_secret
from .schema import utc_now


PAIRING_TTL_MINUTES = 10
PAIRING_MAX_FAILURES = 10
EXTENSION_ORIGIN = re.compile(r"^chrome-extension://[a-p]{32}$")
SENSITIVE_FIELD = re.compile(
    r"\b(gender|sex|sexual orientation|race|ethnic(?:ity)?|disab(?:ility|led)?|veteran|"
    r"age|birth|sponsor(?:ship)?|authori[sz](?:ed|ation)|citizen(?:ship)?|salary|"
    r"compensation|pronoun|marital|religion|genetic|pregnan(?:cy|t)|eeo)\b",
    re.IGNORECASE,
)
PROHIBITED_CONTROL = re.compile(
    r"\b(submit|next|continue|captcha|consent|send message|contact recruiter)\b",
    re.IGNORECASE,
)
ALLOWED_FIELD_KEYS = {
    "key",
    "label",
    "type",
    "provenance",
    "confidence",
    "requires_review",
    "filled",
    "reason",
    "outcome",
    "required",
    "unsupported",
}


class ExtensionApplyError(ValueError):
    """An extension request is invalid or violates an assisted-apply boundary."""


class ExtensionAuthError(PermissionError):
    """A pairing code or extension device token is invalid."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def validate_extension_origin(origin: str) -> str:
    value = origin.strip().rstrip("/")
    if not EXTENSION_ORIGIN.fullmatch(value):
        raise ExtensionAuthError("A valid Chrome extension origin is required")
    return value


def create_pairing(conn: sqlite3.Connection, *, user_id: str) -> dict[str, Any]:
    code = secrets.token_urlsafe(18)
    pairing_id = f"extension-pairing-{uuid4().hex}"
    timestamp = _now()
    expires_at = timestamp + timedelta(minutes=PAIRING_TTL_MINUTES)
    with conn:
        conn.execute(
            """
            INSERT INTO extension_pairing_challenges(
                id, user_id, code_hash, expires_at, status, created_at
            ) VALUES(?, ?, ?, ?, 'pending', ?)
            """,
            (pairing_id, user_id, hash_secret(code), expires_at.isoformat(), timestamp.isoformat()),
        )
    return {
        "pairing_id": pairing_id,
        "code": code,
        "expires_at": expires_at.isoformat(),
        "server_origin": "http://127.0.0.1:8765",
    }


def redeem_pairing(
    conn: sqlite3.Connection,
    code: str,
    extension_origin: str,
    device_name: str = "Chrome Apply Mode",
) -> dict[str, Any]:
    origin = validate_extension_origin(extension_origin)
    cutoff = (_now() - timedelta(minutes=PAIRING_TTL_MINUTES)).isoformat()
    recent_failures = conn.execute(
        """
        SELECT COUNT(*) FROM extension_pairing_redemption_attempts
        WHERE extension_origin=? AND succeeded=0 AND created_at>=?
        """,
        (origin, cutoff),
    ).fetchone()[0]
    if int(recent_failures) >= PAIRING_MAX_FAILURES:
        raise ExtensionAuthError("Too many pairing attempts; wait before trying again")
    row = conn.execute(
        "SELECT * FROM extension_pairing_challenges WHERE code_hash=?",
        (hash_secret(code.strip()),),
    ).fetchone()
    if not row:
        with conn:
            conn.execute(
                "INSERT INTO extension_pairing_redemption_attempts(id, extension_origin, succeeded, created_at) VALUES(?, ?, 0, ?)",
                (f"pairing-attempt-{uuid4().hex}", origin, utc_now()),
            )
        raise ExtensionAuthError("Pairing code is invalid or expired")
    if row["status"] != "pending":
        raise ExtensionAuthError("Pairing code has already been used")
    if datetime.fromisoformat(str(row["expires_at"])) <= _now():
        with conn:
            conn.execute(
                "UPDATE extension_pairing_challenges SET status='expired' WHERE id=?",
                (row["id"],),
            )
        raise ExtensionAuthError("Pairing code is invalid or expired")

    token = secrets.token_urlsafe(32)
    device_id = f"extension-device-{uuid4().hex}"
    timestamp = utc_now()
    clean_name = device_name.strip()[:120] or "Chrome Apply Mode"
    with conn:
        claimed = conn.execute(
            """
            UPDATE extension_pairing_challenges
            SET status='used', consumed_at=? WHERE id=? AND status='pending'
            """,
            (timestamp, row["id"]),
        )
        if not claimed.rowcount:
            raise ExtensionAuthError("Pairing code has already been used")
        conn.execute(
            """
            INSERT INTO extension_devices(
                id, user_id, token_hash, extension_origin, device_name, created_at
            ) VALUES(?, ?, ?, ?, ?, ?)
            """,
            (device_id, row["user_id"], hash_secret(token), origin, clean_name, timestamp),
        )
        conn.execute(
            "INSERT INTO extension_pairing_redemption_attempts(id, extension_origin, succeeded, created_at) VALUES(?, ?, 1, ?)",
            (f"pairing-attempt-{uuid4().hex}", origin, timestamp),
        )
    return {
        "device_id": device_id,
        "device_token": token,
        "extension_origin": origin,
        "device_name": clean_name,
        "paired_at": timestamp,
    }


def resolve_extension_token(
    conn: sqlite3.Connection, token: str, extension_origin: str
) -> dict[str, str] | None:
    try:
        origin = validate_extension_origin(extension_origin)
    except ExtensionAuthError:
        return None
    row = conn.execute(
        """
        SELECT id, user_id, extension_origin FROM extension_devices
        WHERE token_hash=? AND revoked_at IS NULL
        """,
        (hash_secret(token),),
    ).fetchone()
    if not row or str(row["extension_origin"]) != origin:
        return None
    try:
        with conn:
            conn.execute(
                "UPDATE extension_devices SET last_used_at=? WHERE id=?",
                (utc_now(), row["id"]),
            )
    except Exception:  # usage tracking must never turn valid auth into a 500
        pass
    return {"device_id": str(row["id"]), "user_id": str(row["user_id"])}


def list_devices(conn: sqlite3.Connection, *, user_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, extension_origin, device_name, created_at, last_used_at, revoked_at
        FROM extension_devices WHERE user_id=? ORDER BY created_at DESC
        """,
        (user_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def revoke_device(conn: sqlite3.Connection, device_id: str, *, user_id: str) -> bool:
    with conn:
        cursor = conn.execute(
            """
            UPDATE extension_devices SET revoked_at=?
            WHERE id=? AND user_id=? AND revoked_at IS NULL
            """,
            (utc_now(), device_id, user_id),
        )
    return bool(cursor.rowcount)


def _canonical_url(value: str) -> tuple[str, str, str]:
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return "", "", ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return "", "", ""
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = sorted(
        (key, item)
        for key, item in query
        if not key.casefold().startswith("utm_")
        and key.casefold() not in {"source", "ref", "referrer", "trackingid"}
    )
    path = re.sub(r"/+", "/", parsed.path or "/").rstrip("/") or "/"
    canonical = urllib.parse.urlunsplit(
        (parsed.scheme.casefold(), parsed.hostname.casefold(), path, urllib.parse.urlencode(query), "")
    )
    return canonical, parsed.hostname.casefold(), path


def application_candidates(
    conn: sqlite3.Connection, page_url: str, *, user_id: str
) -> dict[str, Any]:
    canonical, host, path = _canonical_url(page_url)
    if not canonical:
        raise ExtensionApplyError("Application page URL must use HTTP or HTTPS")
    rows = conn.execute(
        """
        SELECT a.id AS application_id, a.opportunity_id, a.stage, a.updated_at,
               o.company, o.title, o.url AS source_url,
               (SELECT MAX(oi.created_at) FROM opportunity_interactions oi
                WHERE oi.opportunity_id=a.opportunity_id AND oi.user_id=a.user_id
                  AND oi.action='apply_opened') AS apply_opened_at
        FROM applications a JOIN opportunities o ON o.id=a.opportunity_id
        WHERE a.user_id=?
        ORDER BY a.updated_at DESC, a.id ASC LIMIT 25
        """,
        (user_id,),
    ).fetchall()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        source_canonical, source_host, source_path = _canonical_url(str(row["source_url"]))
        if source_canonical == canonical:
            match_kind = "exact_url"
        elif source_host == host and source_path == path:
            match_kind = "canonical_url"
        elif source_host == host:
            match_kind = "same_host"
        elif row["apply_opened_at"] and datetime.fromisoformat(str(row["apply_opened_at"])) >= _now() - timedelta(days=30):
            match_kind = "recent_apply"
        else:
            continue
        candidates.append(
            {
                "application_id": row["application_id"],
                "opportunity_id": row["opportunity_id"],
                "company": row["company"],
                "title": row["title"],
                "source_url": row["source_url"],
                "stage": row["stage"],
                "match_kind": match_kind,
                "updated_at": row["updated_at"],
                "apply_opened_at": row["apply_opened_at"],
            }
        )
    priority = {"exact_url": 0, "canonical_url": 1, "same_host": 2, "recent_apply": 3}
    candidates.sort(key=lambda item: str(item["updated_at"]), reverse=True)
    candidates.sort(key=lambda item: priority[item["match_kind"]])
    exact = [item for item in candidates if item["match_kind"] in {"exact_url", "canonical_url"}]
    return {
        "items": candidates[:10],
        "total": min(len(candidates), 10),
        "preselected_application_id": exact[0]["application_id"] if len(exact) == 1 else None,
    }


def _confirmed_profile(conn: sqlite3.Connection, user_id: str) -> dict[str, Any]:
    rows = conn.execute(
        """
        SELECT field_path, value_json FROM profile_facts
        WHERE user_id=? AND confirmed=1 ORDER BY field_path
        """,
        (user_id,),
    ).fetchall()
    return {str(row["field_path"]): json.loads(row["value_json"]) for row in rows}


def _safe_answers(conn: sqlite3.Connection, user_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, question, answer, company, tags_json, updated_at
        FROM answer_library WHERE user_id=? ORDER BY updated_at DESC LIMIT 200
        """,
        (user_id,),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "question": row["question"],
            "answer": row["answer"],
            "company": row["company"],
            "tags": json.loads(row["tags_json"] or "[]"),
            "updated_at": row["updated_at"],
        }
        for row in rows
        if not SENSITIVE_FIELD.search(str(row["question"]))
    ]


def _document_records(conn: sqlite3.Connection, user_id: str) -> list[dict[str, Any]]:
    resumes = conn.execute(
        """
        SELECT rv.id, rf.original_name, rf.media_type, rf.byte_size, rf.sha256,
               rv.confirmed_at
        FROM resume_versions rv JOIN resume_files rf ON rf.id=rv.resume_file_id
        WHERE rv.user_id=? AND rv.status='confirmed'
        ORDER BY rv.confirmed_at DESC, rv.created_at DESC
        """,
        (user_id,),
    ).fetchall()
    generated = conn.execute(
        """
        SELECT ga.id, ga.filename, ga.media_type, ga.byte_size, ga.sha256,
               gd.document_type, gd.opportunity_id, gd.approved_at
        FROM generated_document_artifacts ga
        JOIN generated_documents gd ON gd.id=ga.document_id
        WHERE ga.user_id=? AND gd.status='approved'
        ORDER BY gd.approved_at DESC
        """,
        (user_id,),
    ).fetchall()
    items = [
        {
            "artifact_id": row["id"],
            "document_type": "resume",
            "opportunity_id": None,
            "filename": row["original_name"],
            "media_type": row["media_type"],
            "byte_size": int(row["byte_size"]),
            "sha256": row["sha256"],
            "approved_at": row["confirmed_at"],
            "source_kind": "confirmed_upload",
        }
        for row in resumes
    ]
    items.extend(
        {
            "artifact_id": row["id"],
            "document_type": row["document_type"],
            "opportunity_id": row["opportunity_id"],
            "filename": row["filename"],
            "media_type": row["media_type"],
            "byte_size": int(row["byte_size"]),
            "sha256": row["sha256"],
            "approved_at": row["approved_at"],
            "source_kind": "approved_generated",
        }
        for row in generated
    )
    return items


def apply_context(
    conn: sqlite3.Connection, application_id: str, *, user_id: str
) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT a.id AS application_id, a.opportunity_id, a.stage,
               o.company, o.title, o.url AS source_url, o.active AS opportunity_active,
               o.updated_at AS opportunity_updated_at,
               COALESCE(fs.score, 0) AS score, fs.explanation_json
        FROM applications a
        JOIN opportunities o ON o.id=a.opportunity_id
        LEFT JOIN fit_scores fs ON fs.opportunity_id=o.id
             AND fs.user_id=a.user_id AND fs.ruleset_version='legacy-v1'
        WHERE a.id=? AND a.user_id=?
        """,
        (application_id, user_id),
    ).fetchone()
    if not row:
        raise ApplicationNotFoundError(application_id)
    sessions = conn.execute(
        """
        SELECT id, page_url, ats_type, status, updated_at
        FROM application_form_sessions
        WHERE application_id=? AND user_id=? ORDER BY updated_at DESC
        """,
        (application_id, user_id),
    ).fetchall()
    return {
        "application": {
            "application_id": row["application_id"],
            "opportunity_id": row["opportunity_id"],
            "company": row["company"],
            "title": row["title"],
            "source_url": row["source_url"],
            "stage": row["stage"],
            "opportunity_status": "active" if row["opportunity_active"] else "inactive",
            "opportunity_updated_at": row["opportunity_updated_at"],
        },
        "match": {
            "score": int(row["score"] or 0),
            "explanation": json.loads(row["explanation_json"] or "[]"),
            "ruleset_version": "legacy-v1",
        },
        "confirmed_profile": _confirmed_profile(conn, user_id),
        "answers": _safe_answers(conn, user_id),
        "documents": _document_records(conn, user_id),
        "sessions": [dict(item) for item in sessions],
    }


def sanitize_fields(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    safe_fields: list[dict[str, Any]] = []
    if len(fields) > 500:
        raise ExtensionApplyError("Apply sessions are limited to 500 fields")
    for field in fields:
        field_type = str(field.get("type", "")).casefold()[:80]
        label = str(field.get("label", ""))[:500]
        if field_type in {"submit", "button", "image", "reset"} or PROHIBITED_CONTROL.search(label):
            raise ExtensionApplyError("Submit, navigation, CAPTCHA, consent, and messaging controls are prohibited")
        safe = {key: field[key] for key in ALLOWED_FIELD_KEYS if key in field}
        safe["label"] = label
        safe["type"] = field_type
        safe.pop("proposed_value", None)
        safe.pop("value", None)
        safe_fields.append(safe)
    return safe_fields


def sync_session(
    conn: sqlite3.Connection,
    session_id: str,
    payload: dict[str, Any],
    *,
    user_id: str,
) -> dict[str, Any]:
    page_url = str(payload.get("page_url", ""))
    if not _canonical_url(page_url)[0]:
        raise ExtensionApplyError("Apply sessions require an HTTP or HTTPS page URL")
    application_id = str(payload.get("application_id") or "") or None
    if application_id:
        owned = conn.execute(
            "SELECT 1 FROM applications WHERE id=? AND user_id=?",
            (application_id, user_id),
        ).fetchone()
        if not owned:
            raise ApplicationNotFoundError(application_id)
    status_value = str(payload.get("status", "draft"))
    if status_value not in {"draft", "reviewed", "completed"}:
        raise ExtensionApplyError("Unsupported apply-session status")
    safe_fields = sanitize_fields(list(payload.get("fields") or []))
    existing = conn.execute(
        "SELECT user_id FROM application_form_sessions WHERE id=?", (session_id,)
    ).fetchone()
    if existing and str(existing["user_id"]) != user_id:
        raise ExtensionAuthError("Apply session belongs to another user")
    timestamp = utc_now()
    ats_type = str(payload.get("ats_type", "generic"))[:100] or "generic"
    with conn:
        conn.execute(
            """
            INSERT INTO application_form_sessions(
                id, user_id, application_id, page_url, ats_type,
                fields_json, status, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                application_id=excluded.application_id,
                page_url=excluded.page_url,
                ats_type=excluded.ats_type,
                fields_json=excluded.fields_json,
                status=excluded.status,
                updated_at=excluded.updated_at
            """,
            (
                session_id,
                user_id,
                application_id,
                page_url,
                ats_type,
                json.dumps(safe_fields),
                status_value,
                timestamp,
                timestamp,
            ),
        )
    return {
        "id": session_id,
        "application_id": application_id,
        "page_url": page_url,
        "ats_type": ats_type,
        "fields": safe_fields,
        "status": status_value,
        "updated_at": timestamp,
        "final_submit_available": False,
    }


def sync_step(
    conn: sqlite3.Connection,
    session_id: str,
    step_key: str,
    payload: dict[str, Any],
    *,
    user_id: str,
) -> dict[str, Any]:
    session = conn.execute(
        "SELECT id FROM application_form_sessions WHERE id=? AND user_id=?",
        (session_id, user_id),
    ).fetchone()
    if not session:
        raise ExtensionApplyError("Apply session must be synchronized before its steps")
    page_url = str(payload.get("page_url", ""))
    if not _canonical_url(page_url)[0]:
        raise ExtensionApplyError("Apply steps require an HTTP or HTTPS page URL")
    status_value = str(payload.get("status", "scanned"))
    if status_value not in {"scanned", "reviewed", "filled", "manual", "completed"}:
        raise ExtensionApplyError("Unsupported apply-step status")
    fields = sanitize_fields(list(payload.get("fields") or []))
    supplied_summary = dict(payload.get("summary") or {})
    summary = {
        key: max(0, int(supplied_summary.get(key, 0)))
        for key in ("filled", "failed", "manual", "required_unresolved")
    }
    ats_type = str(payload.get("ats_type", "generic"))[:100] or "generic"
    timestamp = utc_now()
    step_id = f"apply-step-{hashlib.sha256(f'{session_id}:{step_key}'.encode()).hexdigest()[:32]}"
    with conn:
        conn.execute(
            """
            INSERT INTO application_form_session_steps(
                id, session_id, user_id, step_key, page_url, ats_type,
                fields_json, summary_json, status, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id, step_key) DO UPDATE SET
                page_url=excluded.page_url,
                ats_type=excluded.ats_type,
                fields_json=excluded.fields_json,
                summary_json=excluded.summary_json,
                status=excluded.status,
                updated_at=excluded.updated_at
            """,
            (
                step_id,
                session_id,
                user_id,
                step_key[:200],
                page_url,
                ats_type,
                json.dumps(fields),
                json.dumps(summary),
                status_value,
                timestamp,
                timestamp,
            ),
        )
    return {
        "id": step_id,
        "session_id": session_id,
        "step_key": step_key[:200],
        "page_url": page_url,
        "ats_type": ats_type,
        "fields": fields,
        "summary": summary,
        "status": status_value,
        "updated_at": timestamp,
    }


def confirm_submitted(
    conn: sqlite3.Connection, session_id: str, *, user_id: str
) -> dict[str, Any]:
    session = conn.execute(
        """
        SELECT * FROM application_form_sessions
        WHERE id=? AND user_id=?
        """,
        (session_id, user_id),
    ).fetchone()
    if not session or not session["application_id"]:
        raise ApplicationNotFoundError(session_id)
    application = update_application(
        conn,
        str(session["application_id"]),
        stage="applied",
        user_id=user_id,
        source="extension_user_confirmed",
    )
    timestamp = utc_now()
    with conn:
        conn.execute(
            """
            UPDATE application_form_sessions SET status='completed', updated_at=?
            WHERE id=? AND user_id=?
            """,
            (timestamp, session_id, user_id),
        )
        conn.execute(
            """
            INSERT INTO application_events(
                application_id, event_type, from_stage, to_stage, detail_json, created_at
            ) VALUES(?, 'apply_submission_confirmed', NULL, 'applied', ?, ?)
            """,
            (
                application["id"],
                json.dumps({"session_id": session_id, "source": "explicit_user_confirmation"}),
                timestamp,
            ),
        )
    return {
        "session_id": session_id,
        "application_id": application["id"],
        "stage": application["stage"],
        "confirmed_at": timestamp,
        "inferred": False,
    }


def artifact_path(
    conn: sqlite3.Connection,
    artifact_id: str,
    application_id: str,
    storage_root: Path,
    *,
    user_id: str,
) -> tuple[Path, str, str, str]:
    application = conn.execute(
        "SELECT opportunity_id FROM applications WHERE id=? AND user_id=?",
        (application_id, user_id),
    ).fetchone()
    if not application:
        raise ApplicationNotFoundError(application_id)
    resume = conn.execute(
        """
        SELECT rf.storage_path, rf.original_name, rf.media_type, rf.sha256
        FROM resume_versions rv JOIN resume_files rf ON rf.id=rv.resume_file_id
        WHERE rv.id=? AND rv.user_id=? AND rv.status='confirmed'
        """,
        (artifact_id, user_id),
    ).fetchone()
    if resume:
        path = (storage_root.resolve() / str(resume["storage_path"])).resolve()
        if path.parent != storage_root.resolve() or not path.exists():
            raise ExtensionApplyError("Confirmed resume file is unavailable")
        return path, str(resume["original_name"]), str(resume["media_type"]), str(resume["sha256"])
    generated = conn.execute(
        """
        SELECT ga.storage_path, ga.filename, ga.media_type, ga.sha256, gd.opportunity_id
        FROM generated_document_artifacts ga
        JOIN generated_documents gd ON gd.id=ga.document_id
        WHERE ga.id=? AND ga.user_id=? AND gd.status='approved'
        """,
        (artifact_id, user_id),
    ).fetchone()
    if not generated:
        raise ExtensionApplyError("Approved document artifact not found")
    if str(generated["opportunity_id"] or "") != str(application["opportunity_id"]):
        raise ExtensionApplyError("Tailored document belongs to a different opportunity")
    root = (storage_root.resolve() / "generated").resolve()
    path = (root / str(generated["storage_path"])).resolve()
    if path.parent != root or not path.exists():
        raise ExtensionApplyError("Approved document artifact is unavailable")
    return path, str(generated["filename"]), str(generated["media_type"]), str(generated["sha256"])


def answer_is_sensitive(question: str) -> bool:
    return bool(SENSITIVE_FIELD.search(question))
