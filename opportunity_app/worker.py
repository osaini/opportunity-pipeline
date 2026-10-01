"""Durable bounded worker for product maintenance jobs.

This is the hosted maintenance worker: it drains the durable job queue (operations.run_next_job), runs the
pipeline stages on their schedules, and runs retention. It is not the in-app workers. The student's own
automation (outreach_automation.AutomationWorker) and call-prep research (outreach_call_prep.CallPrepWorker)
run as threads inside the web app, started by api.create_app. Run it with `python -m opportunity_app.worker`.
"""

from __future__ import annotations

import argparse
import os
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import APPLY_ROOT, DEFAULT_PLATFORM_DB
from .database import is_postgres_target, connect_product
from .ingestion import make_stage_handler
from .notifications import connector_health, run_notification_digest, send_due_reminders
from .outreach import queue_follow_up_reminders
from .operations import enqueue_job, recover_stale_jobs, run_next_job, run_retention

STAGES = ("fetch", "enrich", "score", "liveness", "report")
# Run by a thread inside the web app, which recovers its own interrupted jobs.
WEB_APP_JOB_TYPES = ("outreach_call_prep", "outreach_company_research")


def _scheduled_stages() -> dict[str, float]:
    """Read PIPELINE_SCHEDULE_<STAGE>_HOURS cadences; unset means off."""
    schedules = {}
    for stage in STAGES:
        raw = os.environ.get(f"PIPELINE_SCHEDULE_{stage.upper()}_HOURS", "")
        if not raw:
            continue
        try:
            hours = float(raw)
        except ValueError:
            continue
        if hours > 0:
            schedules[stage] = hours
    return schedules


def enqueue_due_schedules(conn, *, now: datetime | None = None) -> list[str]:
    """Enqueue pipeline stages whose cadence elapsed. Default: nothing scheduled."""
    now = now or datetime.now(timezone.utc)
    enqueued = []
    for stage, hours in _scheduled_stages().items():
        bucket = now.strftime("%Y%m%dT%H")
        idempotency_key = f"schedule-{stage}-{bucket}"
        last_success = conn.execute(
            "SELECT MAX(finished_at) FROM ingestion_runs WHERE stage=? AND status='success'",
            (stage,),
        ).fetchone()[0]
        due = True
        if last_success:
            try:
                due = datetime.fromisoformat(str(last_success)) <= now - timedelta(hours=hours)
            except ValueError:
                due = True
        if not due:
            continue
        existing = conn.execute(
            "SELECT state FROM job_queue WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()
        if existing:
            continue
        enqueue_job(conn, f"pipeline_{stage}", {"stage": stage}, idempotency_key)
        enqueued.append(stage)
    return enqueued


def run_once(target: Path | str = DEFAULT_PLATFORM_DB):
    with closing(connect_product(target)) as conn:
        stale_before = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
        recover_stale_jobs(conn, stale_before=stale_before, exclude_types=WEB_APP_JOB_TYPES)
        # Due reminders fire on every pass: they are time-sensitive and the
        # handler is idempotent (completed reminders are never re-sent).
        send_due_reminders(conn)
        queue_follow_up_reminders(conn)
        enqueue_due_schedules(conn)
        stage_handler = make_stage_handler(conn)
        # Apply for me's screenshots belong to the real product database only, as in create_app.
        real_db = not is_postgres_target(target) and Path(target).expanduser().resolve() == DEFAULT_PLATFORM_DB.resolve()
        handlers = {
            "retention": lambda _payload: run_retention(conn, apply_root=APPLY_ROOT if real_db else None),
            "connector_health": lambda _payload: connector_health(conn),
            "notification_digest": lambda payload: run_notification_digest(conn, payload),
            "reminder_dispatch": lambda _payload: send_due_reminders(conn),
        }
        for stage in STAGES:
            handlers[f"pipeline_{stage}"] = stage_handler
        return run_next_job(conn, handlers, exclude_types=WEB_APP_JOB_TYPES)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=str(DEFAULT_PLATFORM_DB), help="SQLite path or PostgreSQL URL")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args()
    if args.once:
        run_once(args.db)
        return 0
    while True:
        record = run_once(args.db)
        if record is None:
            time.sleep(max(0.1, min(args.poll_seconds, 60)))


if __name__ == "__main__":
    raise SystemExit(main())
