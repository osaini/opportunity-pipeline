"""Production operations: durable jobs, retention, account portability, and encrypted backups."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from .schema import connect_product, utc_now
from .database import is_postgres_target


class OperationsError(RuntimeError):
    pass


def enqueue_job(conn: sqlite3.Connection, job_type: str, payload: dict[str, Any], idempotency_key: str, *, max_attempts: int = 3) -> dict[str, Any]:
    if not job_type or not idempotency_key or not 1 <= max_attempts <= 20:
        raise ValueError("Valid job type, idempotency key, and attempt limit are required")
    timestamp = utc_now()
    job_id = f"job-{uuid4().hex}"
    with conn:
        conn.execute(
            """INSERT INTO job_queue(id, job_type, payload_json, idempotency_key, max_attempts, next_attempt_at, created_at, updated_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(idempotency_key) DO NOTHING""",
            (job_id, job_type, json.dumps(payload), idempotency_key, max_attempts, timestamp, timestamp, timestamp),
        )
    row = conn.execute("SELECT * FROM job_queue WHERE idempotency_key=?", (idempotency_key,)).fetchone()
    return _job(row)


def _job(row: Any) -> dict[str, Any]:
    result = dict(row)
    result["payload"] = json.loads(result.pop("payload_json"))
    return result


def run_next_job(
    conn: sqlite3.Connection,
    handlers: dict[str, Callable[[dict[str, Any]], Any]],
    *,
    now: str | None = None,
    only_handled: bool = False,
    exclude_types: tuple[str, ...] = (),
) -> dict[str, Any] | None:
    """Claim and run the next due job.

    By default any job type is claimed, so one nobody handles dies loudly
    instead of waiting forever. The web app's background thread passes
    ``only_handled`` and the maintenance worker ``exclude_types``, so the two
    never take each other's jobs from this shared table.
    """
    now = now or utc_now()
    clauses, params = ["state IN ('queued', 'retry')", "next_attempt_at<=?"], [now]
    if only_handled:
        types = sorted(handlers)
        clauses.append(f"job_type IN ({', '.join('?' for _ in types) or 'NULL'})")
        params.extend(types)
    if exclude_types:
        clauses.append(f"job_type NOT IN ({', '.join('?' for _ in exclude_types)})")
        params.extend(exclude_types)
    with conn:
        row = conn.execute(
            f"""SELECT * FROM job_queue WHERE {' AND '.join(clauses)}
               ORDER BY next_attempt_at, created_at LIMIT 1""",
            params,
        ).fetchone()
        if not row:
            return None
        claimed = conn.execute(
            "UPDATE job_queue SET state='running', locked_at=?, updated_at=? WHERE id=? AND state IN ('queued', 'retry')",
            (now, now, row["id"]),
        )
        if not claimed.rowcount:
            return None
    payload = json.loads(row["payload_json"])
    try:
        handler = handlers[row["job_type"]]
        handler(payload)
    except Exception as exc:  # worker boundary intentionally catches and records failures
        attempts = int(row["attempts"]) + 1
        state = "dead" if attempts >= int(row["max_attempts"]) else "retry"
        delay = min(3600, 2 ** attempts * 30)
        next_attempt = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
        with conn:
            conn.execute(
                "UPDATE job_queue SET state=?, attempts=?, next_attempt_at=?, last_error=?, locked_at=NULL, updated_at=? WHERE id=?",
                (state, attempts, next_attempt, str(exc)[:2000], utc_now(), row["id"]),
            )
    else:
        with conn:
            conn.execute("UPDATE job_queue SET state='succeeded', attempts=attempts+1, locked_at=NULL, updated_at=? WHERE id=?", (utc_now(), row["id"]))
    return _job(conn.execute("SELECT * FROM job_queue WHERE id=?", (row["id"],)).fetchone())


def retry_dead_job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    timestamp = utc_now()
    with conn:
        cursor = conn.execute("UPDATE job_queue SET state='queued', attempts=0, last_error='', next_attempt_at=?, updated_at=? WHERE id=? AND state='dead'", (timestamp, timestamp, job_id))
        if not cursor.rowcount:
            raise OperationsError("Dead-letter job not found")
    return _job(conn.execute("SELECT * FROM job_queue WHERE id=?", (job_id,)).fetchone())


def queue_status(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute("SELECT state, COUNT(*) AS count FROM job_queue GROUP BY state").fetchall()
    states = {state: 0 for state in ("queued", "running", "succeeded", "retry", "dead", "cancelled")}
    states.update({row["state"]: int(row["count"]) for row in rows})
    return {"states": states, "backpressure": states["queued"] + states["retry"] >= 1000}


def recover_stale_jobs(
    conn: sqlite3.Connection, *, stale_before: str, job_types: tuple[str, ...] | None = None,
    exclude_types: tuple[str, ...] = (), reason: str = "worker lease expired",
) -> int:
    """Put jobs a dead worker left running back in line. Optionally only some job types."""
    clauses, params = ["state='running'", "locked_at<?"], [stale_before]
    if job_types is not None:
        clauses.append(f"job_type IN ({', '.join('?' for _ in job_types) or 'NULL'})")
        params.extend(job_types)
    if exclude_types:
        clauses.append(f"job_type NOT IN ({', '.join('?' for _ in exclude_types)})")
        params.extend(exclude_types)
    with conn:
        return conn.execute(
            f"""UPDATE job_queue SET state='retry', locked_at=NULL, next_attempt_at=?,
               last_error=?, updated_at=? WHERE {' AND '.join(clauses)}""",
            (utc_now(), reason, utc_now(), *params),
        ).rowcount


ACCOUNT_QUERIES = {
    "profile": "SELECT * FROM profiles WHERE user_id=?",
    "profile_facts": "SELECT * FROM profile_facts WHERE user_id=?",
    "resume_files": "SELECT * FROM resume_files WHERE user_id=?",
    "resumes": "SELECT * FROM resume_versions WHERE user_id=?",
    "fit_scores": "SELECT * FROM fit_scores WHERE user_id=?",
    "interactions": "SELECT * FROM opportunity_interactions WHERE user_id=?",
    "opportunity_deadlines": "SELECT * FROM opportunity_deadlines WHERE user_id=?",
    "early_program_status": "SELECT * FROM early_program_status WHERE user_id=?",
    "company_tag_choices": "SELECT * FROM company_tag_choices WHERE user_id=?",
    "outreach_company_tags": "SELECT * FROM outreach_company_tags WHERE user_id=?",
    "applications": "SELECT * FROM applications WHERE user_id=?",
    "application_events": "SELECT e.* FROM application_events e JOIN applications a ON a.id=e.application_id WHERE a.user_id=?",
    "application_contacts": "SELECT * FROM application_contacts WHERE user_id=?",
    "application_tasks": "SELECT * FROM application_tasks WHERE user_id=?",
    "reminders": "SELECT * FROM reminders WHERE user_id=?",
    "captures": "SELECT * FROM opportunity_captures WHERE user_id=?",
    "documents": "SELECT * FROM generated_documents WHERE user_id=?",
    "document_artifacts": "SELECT * FROM generated_document_artifacts WHERE user_id=?",
    "answers": "SELECT * FROM answer_library WHERE user_id=?",
    "mock_interviews": "SELECT * FROM mock_interviews WHERE user_id=?",
    "mock_questions": "SELECT q.* FROM mock_questions q JOIN mock_interviews i ON i.id=q.interview_id WHERE i.user_id=?",
    "mock_answers": "SELECT * FROM mock_answers WHERE user_id=?",
    "agent_threads": "SELECT * FROM agent_threads WHERE user_id=?",
    "agent_turns": "SELECT * FROM agent_turns WHERE user_id=?",
    "agent_messages": "SELECT * FROM agent_messages WHERE user_id=?",
    "agent_tool_runs": "SELECT * FROM agent_tool_runs WHERE user_id=?",
    "agent_proposals": "SELECT * FROM agent_proposed_actions WHERE user_id=?",
    "apply_sessions": "SELECT * FROM application_form_sessions WHERE user_id=?",
    "apply_session_steps": "SELECT * FROM application_form_session_steps WHERE user_id=?",
    "extension_pairings": "SELECT * FROM extension_pairing_challenges WHERE user_id=?",
    "extension_devices": "SELECT * FROM extension_devices WHERE user_id=?",
    "dossier_settings": "SELECT * FROM dossier_settings WHERE user_id=?",
    "dossier": "SELECT * FROM dossier_items WHERE user_id=?",
    "consent_grants": "SELECT * FROM dossier_consent_grants WHERE user_id=?",
    "dossier_access": "SELECT l.* FROM dossier_access_log l JOIN dossier_consent_grants g ON g.id=l.grant_id WHERE g.user_id=?",
    "connections": "SELECT * FROM connector_accounts WHERE user_id=?",
    "monitored_events": "SELECT * FROM monitored_events WHERE user_id=?",
    "notification_preferences": "SELECT * FROM notification_preferences WHERE user_id=?",
    "notification_outbox": "SELECT * FROM notification_outbox WHERE user_id=?",
    "phone_verifications": "SELECT * FROM phone_verifications WHERE user_id=?",
    "outreach_targets": "SELECT * FROM outreach_targets WHERE user_id=?",
    "outreach_events": "SELECT * FROM outreach_events WHERE user_id=?",
    "outreach_contact_candidates": "SELECT * FROM outreach_contact_candidates WHERE user_id=?",
    "outreach_discovery_runs": "SELECT * FROM outreach_discovery_runs WHERE user_id=?",
    "outreach_draft_versions": "SELECT * FROM outreach_draft_versions WHERE user_id=?",
    "outreach_send_claims": "SELECT * FROM outreach_send_claims WHERE user_id=?",
}


def export_account(conn: sqlite3.Connection, *, user_id: str) -> dict[str, Any]:
    result: dict[str, Any] = {"format": "opportunity-account-v1", "exported_at": utc_now(), "user_id": user_id}
    for key, query in ACCOUNT_QUERIES.items():
        rows = [dict(row) for row in conn.execute(query, (user_id,)).fetchall()]
        if key == "connections":
            for row in rows:
                row["encrypted_access_token"] = "[redacted]"
                row["encrypted_refresh_token"] = "[redacted]"
        if key in {"extension_pairings", "extension_devices"}:
            for row in rows:
                for secret_field in ("code_hash", "token_hash"):
                    if secret_field in row:
                        row[secret_field] = "[redacted]"
        for row in rows:
            for field in tuple(row):
                if field == "storage_path" or field.endswith("_path"):
                    row[field] = "[private-file-reference-redacted]"
        result[key] = rows
    return result


def delete_account(conn: sqlite3.Connection, storage_roots: list[Path], *, user_id: str) -> dict[str, Any]:
    if len(storage_roots) != 3:
        raise OperationsError("Account deletion requires resume, capture, and interview storage roots")
    owned_files: list[tuple[Path, str]] = []
    for row in conn.execute("SELECT storage_path FROM resume_files WHERE user_id=?", (user_id,)).fetchall():
        owned_files.append((storage_roots[0], str(row["storage_path"] or "")))
    for row in conn.execute("SELECT storage_path FROM generated_document_artifacts WHERE user_id=?", (user_id,)).fetchall():
        owned_files.append((storage_roots[0] / "generated", str(row["storage_path"] or "")))
    for row in conn.execute("SELECT storage_path FROM opportunity_captures WHERE user_id=?", (user_id,)).fetchall():
        owned_files.append((storage_roots[1], str(row["storage_path"] or "")))
    for row in conn.execute("SELECT audio_path FROM mock_answers WHERE user_id=?", (user_id,)).fetchall():
        owned_files.append((storage_roots[2], str(row["audio_path"] or "")))
    digest = hashlib.sha256(user_id.encode()).hexdigest()
    timestamp = utc_now()
    with conn:
        # Employer views reference grants; erase those cached views before cascading the grants.
        conn.execute("DELETE FROM employer_candidates WHERE consent_grant_id IN (SELECT id FROM dossier_consent_grants WHERE user_id=?)", (user_id,))
        cursor = conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        conn.execute("INSERT INTO account_deletion_log(id, user_id_hash, status, detail_json, created_at) VALUES(?, ?, 'completed', ?, ?)",
                     (f"deletion-{uuid4().hex}", digest, json.dumps({"database_rows_removed": bool(cursor.rowcount)}), timestamp))
    removed_files = 0
    for root, stored_path in owned_files:
        if not stored_path:
            continue
        root = root.resolve()
        child = (root / stored_path).resolve()
        if child.parent != root or not child.is_file():
            continue
        child.unlink()
        removed_files += 1
    return {"deleted": True, "files_removed": removed_files, "completed_at": timestamp}


def run_retention(conn: sqlite3.Connection, *, now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    expired = now.isoformat()
    with conn:
        grants = conn.execute("UPDATE dossier_consent_grants SET status='expired' WHERE status='active' AND expires_at<=?", (expired,)).rowcount
        settings = conn.execute("SELECT user_id, retention_days FROM dossier_settings").fetchall()
        stale = 0
        for setting in settings:
            cutoff = (now - timedelta(days=int(setting["retention_days"]))).isoformat()
            stale += conn.execute(
                "UPDATE dossier_items SET status='deleted', updated_at=? WHERE user_id=? AND status='active' AND created_at<?",
                (expired, setting["user_id"], cutoff),
            ).rowcount
    return {"expired_grants": grants, "retired_dossier_items": stale}


def encrypted_backup(source: Path, destination: Path, key: bytes) -> dict[str, Any]:
    from cryptography.fernet import Fernet

    source = source.resolve()
    destination = destination.resolve()
    if not source.is_file() or destination == source:
        raise OperationsError("Backup source must be an existing SQLite file and destination must differ")
    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory) / "snapshot.db"
        source_conn = sqlite3.connect(source)
        target_conn = None
        try:
            target_conn = sqlite3.connect(temporary)
            source_conn.backup(target_conn)
        finally:
            if target_conn is not None:
                target_conn.close()
            source_conn.close()
        plaintext = temporary.read_bytes()
    ciphertext = Fernet(key).encrypt(plaintext)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(ciphertext)
    return {"path": str(destination), "sha256": hashlib.sha256(ciphertext).hexdigest(), "encrypted": True}


def restore_backup(source: Path, destination: Path, key: bytes) -> dict[str, Any]:
    from cryptography.fernet import Fernet, InvalidToken

    source = source.resolve()
    destination = destination.resolve()
    if not source.is_file() or destination == source:
        raise OperationsError("Restore source must exist and destination must differ")
    try:
        plaintext = Fernet(key).decrypt(source.read_bytes())
    except InvalidToken as exc:
        raise OperationsError("Backup key or payload is invalid") from exc
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(plaintext)
    with closing(sqlite3.connect(destination)) as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        destination.unlink(missing_ok=True)
        raise OperationsError("Restored database failed integrity verification")
    return {"path": str(destination), "integrity": integrity, "restored": True}


def encrypted_database_backup(source: Path | str, destination: Path, key: bytes) -> dict[str, Any]:
    if not is_postgres_target(source):
        return encrypted_backup(Path(source), destination, key)
    from cryptography.fernet import Fernet
    destination = destination.resolve()
    with tempfile.TemporaryDirectory() as directory:
        dump = Path(directory) / "postgres.dump"
        completed = subprocess.run(["pg_dump", "--format=custom", "--file", str(dump), str(source)], capture_output=True, text=True, timeout=900)
        if completed.returncode:
            raise OperationsError(f"pg_dump failed: {completed.stderr[-1000:]}")
        ciphertext = Fernet(key).encrypt(dump.read_bytes())
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(ciphertext)
    return {"path": str(destination), "sha256": hashlib.sha256(ciphertext).hexdigest(), "encrypted": True, "backend": "postgresql"}


def restore_database_backup(source: Path, destination: Path | str, key: bytes) -> dict[str, Any]:
    if not is_postgres_target(destination):
        return restore_backup(source, Path(destination), key)
    from cryptography.fernet import Fernet, InvalidToken
    try:
        plaintext = Fernet(key).decrypt(source.resolve().read_bytes())
    except InvalidToken as exc:
        raise OperationsError("Backup key or payload is invalid") from exc
    with tempfile.TemporaryDirectory() as directory:
        dump = Path(directory) / "postgres.dump"
        dump.write_bytes(plaintext)
        completed = subprocess.run(["pg_restore", "--clean", "--if-exists", "--no-owner", "--dbname", str(destination), str(dump)], capture_output=True, text=True, timeout=900)
        if completed.returncode:
            raise OperationsError(f"pg_restore failed: {completed.stderr[-1000:]}")
    with closing(connect_product(str(destination), read_only=True)) as conn:
        count = int(conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0])
    return {"target": "postgresql", "restored": True, "opportunity_count": count}
