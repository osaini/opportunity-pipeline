"""What runs by itself, and whether it is working, for the web app's status panel.

Three questions a student cannot answer from the dashboard otherwise:

- Are the scheduled jobs (daily refresh, twice-weekly deep search, start at
  sign-in) installed, and when did they last and next run?
- Did this morning's daily run finish, and how?
- Which job boards have stopped answering, and since when?

Everything here only reads: the scheduler, data/daily-run.json, and the
fetch_runs table in data/pipeline.db. Installing a job goes through
launch.install, the same code as ``python -m opportunity_app.launch install-*``.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import DEFAULT_LEGACY_DB, ROOT
from .daily import STATE_PATH as DAILY_STATE_PATH, read_state as read_daily_state

SOURCES_CONFIG = ROOT / "config" / "sources.json"

JOBS = {
    "daily": {"label": "Daily refresh", "schedule": "Every morning at 08:00, and again on wake or sign-in if missed"},
    "outreach": {"label": "Outreach deep search", "schedule": "Monday and Thursday mornings"},
    "autostart": {"label": "Start the app at sign-in", "schedule": "When you sign in to this computer"},
}
INSTALLABLE = ("daily", "outreach")
# A board that has not answered for this long is worth a look even if its
# latest attempt happened to succeed.
STALE_AFTER = timedelta(days=3)
# Task Scheduler reports a task that never ran with this placeholder date.
NEVER_RAN_YEAR = 2000


class Scheduler:
    """The operating system's scheduler: which jobs exist, and installing one."""

    def jobs(self) -> dict[str, dict[str, Any]]:
        from .launch import LABEL_PREFIX, UNIT_PREFIX, WINDOWS_TASKS

        if sys.platform == "win32":
            return _windows_tasks({job: task for job, (_script, task) in WINDOWS_TASKS.items()})
        found: dict[str, dict[str, Any]] = {}
        for job in JOBS:
            if sys.platform == "darwin":
                path = Path.home() / "Library" / "LaunchAgents" / f"{LABEL_PREFIX}.{job}.plist"
            else:
                units = Path.home() / ".config" / "systemd" / "user"
                path = units / f"{UNIT_PREFIX}-{job}.timer"
                if not path.exists():
                    path = units / f"{UNIT_PREFIX}-{job}.service"
            found[job] = {"installed": path.exists(), "state": "", "last_run_at": None, "next_run_at": None, "last_result": None}
        return found

    def install(self, job: str) -> int:
        from .launch import install

        return install(job)


def _windows_tasks(names: dict[str, str]) -> dict[str, dict[str, Any]]:
    # One PowerShell call for every task; Get-ScheduledTaskInfo has the run
    # times schtasks.exe only prints in the machine's locale.
    script = (
        "$ErrorActionPreference='SilentlyContinue';$out=@{};"
        f"foreach($n in @({','.join(repr(name) for name in names.values())})){{"
        "$t=Get-ScheduledTask -TaskName $n;if($t){$i=$t|Get-ScheduledTaskInfo;"
        "$out[$n]=@{state=\"$($t.State)\";"
        "last=$(if($i.LastRunTime){$i.LastRunTime.ToUniversalTime().ToString('o')}else{$null});"
        "next=$(if($i.NextRunTime){$i.NextRunTime.ToUniversalTime().ToString('o')}else{$null});"
        "result=$i.LastTaskResult}}};$out|ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=20, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        raw = json.loads(completed.stdout or "{}") or {}
    except (OSError, subprocess.TimeoutExpired, ValueError):
        raw = {}
    found = {}
    for job, name in names.items():
        task = raw.get(name)
        if not isinstance(task, dict):
            found[job] = {"installed": False, "state": "", "last_run_at": None, "next_run_at": None, "last_result": None}
            continue
        last = task.get("last")
        if last and last[:4].isdigit() and int(last[:4]) < NEVER_RAN_YEAR:
            last = None
        found[job] = {
            "installed": True,
            "state": str(task.get("state") or ""),
            "last_run_at": last,
            "next_run_at": task.get("next"),
            # 0 is success; 267009 (0x41301) means it is running right now.
            "last_result": task.get("result") if last else None,
        }
    return found


def _parse(stamp: str | None) -> datetime | None:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def source_health(
    legacy_path: Path, sources_path: Path = SOURCES_CONFIG, *, now: datetime | None = None,
) -> dict[str, Any]:
    """The latest fetch of every enabled board, failing and stale ones first."""
    from pipeline import _source_identity, load_sources

    now = now or datetime.now(timezone.utc)
    try:
        config = load_sources(sources_path, sources_path.with_name("sources.local.json"))
    except (OSError, SystemExit, ValueError):
        return {"available": False, "enabled": 0, "failing": 0, "stale": 0, "items": []}
    enabled = []
    for source in config.get("ats_sources", []):
        if not source.get("enabled", True):
            continue
        try:
            key = f'{source["kind"]}:{_source_identity(source)}'
        except KeyError:
            continue
        enabled.append((key, source.get("name") or source.get("company") or key))
    if not legacy_path.exists():
        return {"available": False, "enabled": len(enabled), "failing": 0, "stale": 0, "items": []}

    runs: dict[str, list[sqlite3.Row]] = {}
    with closing(sqlite3.connect(f"file:{legacy_path.as_posix()}?mode=ro", uri=True, timeout=5)) as conn:
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                # The last ten attempts per source are enough to count a streak.
                """
                SELECT source_key, finished_at, started_at, outcome, fetched_count, error FROM (
                    SELECT *, ROW_NUMBER() OVER (PARTITION BY source_key ORDER BY id DESC) AS recent
                    FROM fetch_runs WHERE outcome IN ('success', 'error')
                ) WHERE recent <= 10 ORDER BY source_key, recent
                """
            ).fetchall()
        except sqlite3.Error:
            rows = []
        last_success = dict(conn.execute(
            "SELECT source_key, MAX(finished_at) FROM fetch_runs WHERE outcome='success' GROUP BY source_key"
        ).fetchall()) if rows else {}
    for row in rows:
        runs.setdefault(row["source_key"], []).append(row)

    items = []
    for key, name in enabled:
        history = runs.get(key, [])
        succeeded_at = last_success.get(key)
        streak = 0
        for row in history:
            if row["outcome"] != "error":
                break
            streak += 1
        latest = history[0] if history else None
        if latest is None:
            health = "never"
        elif latest["outcome"] == "error":
            health = "failing"
        elif (last := _parse(succeeded_at)) is not None and now - last > STALE_AFTER:
            health = "stale"
        else:
            health = "ok"
        items.append({
            "key": key,
            "name": name,
            "health": health,
            "last_attempt_at": latest["finished_at"] or latest["started_at"] if latest else None,
            "last_success_at": succeeded_at,
            "failures_in_a_row": streak,
            "last_fetched": latest["fetched_count"] if latest and latest["outcome"] == "success" else None,
            "last_error": (latest["error"] or "")[:300] if latest and latest["outcome"] == "error" else "",
        })
    order = {"failing": 0, "stale": 1, "never": 2, "ok": 3}
    items.sort(key=lambda item: (order[item["health"]], -item["failures_in_a_row"], item["name"].lower()))
    return {
        "available": True,
        "enabled": len(enabled),
        "failing": sum(1 for item in items if item["health"] == "failing"),
        "stale": sum(1 for item in items if item["health"] == "stale"),
        "items": items,
    }


def daily_run(state_path: Path = DAILY_STATE_PATH, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    state = read_daily_state(state_path)
    if not state:
        return {"ran": False, "run_date": None, "started_at": None, "finished_at": None, "exit_code": None,
                "in_progress": False, "overdue": True}
    finished = _parse(state["finishedAt"])
    started = _parse(state["startedAt"])
    latest = finished or started
    return {
        "ran": True,
        "run_date": state["runDate"] or None,
        "started_at": state["startedAt"] or None,
        "finished_at": state["finishedAt"],
        "exit_code": state["exitCode"],
        "in_progress": not state["finishedAt"],
        # More than a day and a half without a run means the schedule is not firing.
        "overdue": latest is None or now - latest > timedelta(hours=36),
    }


class SystemStatus:
    """Collects the status panel, caching the scheduler query briefly."""

    def __init__(
        self,
        *,
        legacy_path: Path = DEFAULT_LEGACY_DB,
        sources_path: Path = SOURCES_CONFIG,
        daily_state_path: Path = DAILY_STATE_PATH,
        scheduler: Scheduler | None = None,
        cache_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.legacy_path = legacy_path
        self.sources_path = sources_path
        self.daily_state_path = daily_state_path
        self.scheduler = scheduler or Scheduler()
        self._cache_seconds = cache_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._jobs: tuple[float, dict[str, dict[str, Any]]] | None = None

    def _scheduled(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            if self._jobs and self._clock() - self._jobs[0] < self._cache_seconds:
                return self._jobs[1]
        jobs = self.scheduler.jobs()
        with self._lock:
            self._jobs = (self._clock(), jobs)
        return jobs

    def status(self) -> dict[str, Any]:
        scheduled = self._scheduled()
        jobs = [
            {"job": job, **JOBS[job], **scheduled.get(job, {"installed": False, "state": "", "last_run_at": None,
                                                             "next_run_at": None, "last_result": None})}
            for job in JOBS
        ]
        daily = daily_run(self.daily_state_path)
        sources = source_health(self.legacy_path, self.sources_path)
        problems = []
        for job in jobs:
            if job["job"] != "autostart" and not job["installed"]:
                problems.append(f"{job['label']} is not scheduled")
        if daily["overdue"] and not daily["in_progress"]:
            problems.append("The daily refresh has not run in over a day")
        elif daily["ran"] and daily["exit_code"] not in (None, 0):
            problems.append("The last daily refresh did not finish cleanly")
        if sources["failing"]:
            problems.append(f"{sources['failing']} job board{'s' if sources['failing'] != 1 else ''} failing")
        return {"available": True, "jobs": jobs, "daily": daily, "sources": sources, "problems": problems}

    def install(self, job: str) -> dict[str, Any]:
        # Installing the sign-in job also starts it, which would launch a second
        # server beside this one; that one stays a launcher command.
        if job not in INSTALLABLE:
            raise ValueError(f"Unknown job: {job}")
        code = self.scheduler.install(job)
        with self._lock:
            self._jobs = None
        if code != 0:
            raise RuntimeError(f"Installing {JOBS[job]['label'].lower()} failed (exit code {code})")
        return self.status()
