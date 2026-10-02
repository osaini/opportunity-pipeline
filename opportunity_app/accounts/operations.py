"""Production operations: durable jobs, retention, and account portability (backups.py holds the encrypted backups)."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from ..core.timestamps import utc_now


class OperationsError(RuntimeError):
    pass


class JobDeferred(Exception):
    """A handler's job cannot run yet (for example, the student paused automation).

    run_next_job puts it back in line at ``until``, as it was, without using
    up a try or recording a failure, so it runs once whatever held it ends.
    """

    def __init__(self, reason: str, until: datetime) -> None:
        super().__init__(reason)
        self.until = until


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
    except JobDeferred as deferred:
        # Back to the state it was claimed from, attempts unchanged: nothing was tried.
        with conn:
            conn.execute(
                "UPDATE job_queue SET state=?, next_attempt_at=?, last_error=?, locked_at=NULL, updated_at=? WHERE id=?",
                (row["state"], deferred.until.astimezone(timezone.utc).isoformat(timespec="seconds"), str(deferred)[:2000],
                 utc_now(), row["id"]),
            )
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
    "resume_picks": "SELECT * FROM opportunity_resume_picks WHERE user_id=?",
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
    # The thank-you after a decline, its words included, as the student saw it on the card.
    "outreach_thank_yous": "SELECT * FROM outreach_thank_yous WHERE user_id=?",
    "user_settings": "SELECT * FROM user_settings WHERE user_id=?",
    # Everything automation did, proposed, or would have done, with the evidence it acted on; its notices; its health.
    "automation_actions": "SELECT * FROM automation_actions WHERE user_id=?",
    "automation_notices": "SELECT * FROM automation_notices WHERE user_id=?",
    "automation_health": "SELECT * FROM automation_health WHERE user_id=?",
    # Application mail: how far reading has got, what was read (no bodies), deadlines emails stated, trusted domains.
    "application_mail_sync": "SELECT * FROM application_mail_sync WHERE user_id=?",
    "application_mail_messages": "SELECT * FROM application_mail_messages WHERE user_id=?",
    "email_deadlines": "SELECT * FROM email_deadlines WHERE user_id=?",
    "employer_domains": "SELECT * FROM employer_domains WHERE user_id=?",
    # Every email outreach read for a reply, with why it was taken as it was; a possible reply's words while it waits.
    "outreach_inbox_messages": "SELECT * FROM outreach_inbox_messages WHERE user_id=?",
    # Apply for me: every attempt and run (value-free; screenshot paths are redacted below), and what the student
    # allowed it to answer. The per-install key that hashes values (data/private/apply/hash-key) is never exported.
    "application_submit_claims": "SELECT * FROM application_submit_claims WHERE user_id=?",
    "apply_runs": "SELECT * FROM apply_runs WHERE user_id=?",
    "apply_sensitive_answers": "SELECT * FROM apply_sensitive_answers WHERE user_id=?",
    "apply_ats_labels": "SELECT * FROM apply_ats_labels WHERE user_id=?",
    # Outreach scheduling, contact forms found, companies dismissed, and tag state.
    "outreach_scheduled_sends": "SELECT * FROM outreach_scheduled_sends WHERE user_id=?",
    "outreach_contact_forms": "SELECT * FROM outreach_contact_forms WHERE user_id=?",
    "outreach_dismissed": "SELECT * FROM outreach_dismissed WHERE user_id=?",
    "outreach_tag_state": "SELECT * FROM outreach_tag_state WHERE user_id=?",
    # Gmail: the outreach threads the app labelled, and the sent-mail searches it ran (their queries name contacts).
    "outreach_label_threads": "SELECT * FROM outreach_label_threads WHERE user_id=?",
    "outreach_label_searches": "SELECT * FROM outreach_label_searches WHERE user_id=?",
    # Left out on purpose, each with its reason in tests/test_account_coverage.py (EXPORT_EXCLUDED): user_credentials,
    # user_api_tokens, oauth_states, recovery_challenges (secrets), automation_held (a waiting proposal's full link, token
    # and all; migration 0038 keeps it out of the export), action_requests (response cache), organization_memberships.
}


# Tables that carry a user_id but, unlike every other, no ON DELETE CASCADE foreign key to users (0044 created them
# without one), so deleting the users row leaves their rows behind. delete_account erases them itself.
# tests/test_account_coverage.py fails if a table with a user_id and no cascade is not named here.
ACCOUNT_EXPLICIT_DELETES = ("outreach_label_threads", "outreach_label_searches")


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
        if key == "apply_runs":
            # Only the file paths inside the screenshots list are private; each screenshot's hash is kept.
            for row in rows:
                row["screenshots_json"] = _redact_screenshot_paths(row["screenshots_json"])
        for row in rows:
            for field in tuple(row):
                if field == "storage_path" or field.endswith("_path"):
                    row[field] = "[private-file-reference-redacted]"
        result[key] = rows
    return result


def _redact_screenshot_paths(text: Any) -> str:
    try:
        shots = json.loads(text or "[]")
    except (TypeError, ValueError):
        return "[]"
    for shot in shots if isinstance(shots, list) else []:
        if isinstance(shot, dict) and shot.get("path"):
            shot["path"] = "[private-file-reference-redacted]"
    return json.dumps(shots, sort_keys=True)


def delete_account(
    conn: sqlite3.Connection, storage_roots: list[Path], *, user_id: str, apply_root: Path | None = None,
) -> dict[str, Any]:
    """Delete the account and its files. ``apply_root`` (Apply for me's screenshots) is removed too, when given."""
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
    removed_files = 0
    if apply_root is not None:
        # The whole folder of this student's screenshots, before the rows that name them go.
        from ..apply_runs import delete_apply_folder

        removed_files += delete_apply_folder(apply_root, user_id)
    with conn:
        # Employer views reference grants; erase those cached views before cascading the grants.
        conn.execute("DELETE FROM employer_candidates WHERE consent_grant_id IN (SELECT id FROM dossier_consent_grants WHERE user_id=?)", (user_id,))
        cursor = conn.execute("DELETE FROM users WHERE id=?", (user_id,))
        for table in ACCOUNT_EXPLICIT_DELETES:
            conn.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
        conn.execute("INSERT INTO account_deletion_log(id, user_id_hash, status, detail_json, created_at) VALUES(?, ?, 'completed', ?, ?)",
                     (f"deletion-{uuid4().hex}", digest, json.dumps({"database_rows_removed": bool(cursor.rowcount)}), timestamp))
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


def run_retention(conn: sqlite3.Connection, *, now: datetime | None = None, apply_root: Path | None = None) -> dict[str, int]:
    """Expire grants and old evidence. With ``apply_root``, Apply for me's screenshots too (apply_runs.purge_evidence)."""
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
    # Email excerpts kept as evidence go after PIPELINE_MAIL_EVIDENCE_DAYS; the ledger rows stay, with their hashes.
    # So do the words of a possible reply left waiting that long; the card still links to it in Gmail.
    from ..applications.inbox import evidence_days, purge_excerpts

    cutoff = (now - timedelta(days=evidence_days())).isoformat(timespec="seconds")
    with conn:
        waiting = conn.execute(
            "UPDATE outreach_inbox_messages SET text='', meta_json='{}' WHERE kind='possible' "
            "AND (text<>'' OR meta_json<>'{}') AND recorded_at<?",
            (cutoff,),
        ).rowcount
    counts = {"expired_grants": grants, "retired_dossier_items": stale, "possible_reply_words": waiting, **purge_excerpts(conn, now=now)}
    if apply_root is not None:
        from ..apply_runs import purge_evidence

        counts.update(purge_evidence(conn, apply_root=apply_root, now=now))
    return counts


def service_overview(
    conn: sqlite3.Connection, overview: dict[str, Any], metrics: dict[str, Any], traces: Any,
) -> dict[str, Any]:
    """Add this process's request metrics, queue state, alerts, SLO status and recent traces to the administrator `overview`.

    `metrics` and `traces` are the running app's counters and its recent request traces (web/context.py's AppRuntime).
    Returns `overview`, changed in place.
    """
    requests = int(metrics["requests"])
    read_latencies = sorted(metrics["read_latency_ms"])
    write_latencies = sorted(metrics["write_latency_ms"])

    def p95(values: list[float]) -> float | None:
        if not values:
            return None
        index = max(0, (len(values) * 95 + 99) // 100 - 1)
        return round(float(values[index]), 3)

    read_p95 = p95(read_latencies)
    write_p95 = p95(write_latencies)
    error_rate = round(int(metrics["errors"]) / requests, 4) if requests else 0.0
    overview["service"] = {
        "requests": requests,
        "errors": int(metrics["errors"]),
        "error_rate": error_rate,
        "rate_limited": int(metrics["rate_limited"]),
        "average_latency_ms": round(float(metrics["latency_ms_total"]) / requests, 3) if requests else 0,
        "read_p95_ms": read_p95,
        "write_p95_ms": write_p95,
    }
    queue = queue_status(conn)
    overview["queue"] = queue
    alerts = []
    if error_rate > 0.02:
        alerts.append({"key": "api_error_rate", "severity": "critical", "value": error_rate, "threshold": 0.02})
    if read_p95 is not None and read_p95 > 750:
        alerts.append({"key": "read_p95_ms", "severity": "warning", "value": read_p95, "threshold": 750})
    if write_p95 is not None and write_p95 > 1_500:
        alerts.append({"key": "write_p95_ms", "severity": "warning", "value": write_p95, "threshold": 1_500})
    if queue["states"]["dead"]:
        alerts.append({"key": "dead_letter_jobs", "severity": "critical", "value": queue["states"]["dead"], "threshold": 0})
    if queue["backpressure"]:
        alerts.append({"key": "queue_backpressure", "severity": "critical", "value": True, "threshold": False})
    overview["slo"] = {
        "availability_target": 0.995,
        "read_p95_target_ms": 750,
        "write_p95_target_ms": 1_500,
        "queue_age_target_seconds": 600,
        "status": "alerting" if alerts else "within_observed_thresholds",
        "scope": "current process window; external durable telemetry required for monthly SLOs",
    }
    overview["alerts"] = alerts
    overview["recent_traces"] = list(traces)[-50:]
    overview["product_analytics"] = {
        "application_events": int(conn.execute("SELECT COUNT(*) FROM application_events").fetchone()[0]),
        "apply_sessions": int(conn.execute("SELECT COUNT(*) FROM application_form_sessions").fetchone()[0]),
        "agent_turns": int(conn.execute("SELECT COUNT(*) FROM agent_turns").fetchone()[0]),
        "active_dossier_shares": int(conn.execute("SELECT COUNT(*) FROM dossier_consent_grants WHERE status='active'").fetchone()[0]),
        "contains_user_identifiers": False,
    }
    return overview
