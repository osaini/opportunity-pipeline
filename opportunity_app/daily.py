"""Unattended daily pipeline run, for any operating system.

A Python port of scripts/run-daily.ps1, which Windows Task Scheduler keeps
using. launchd (macOS) and systemd (Linux) run this module instead; see
launch.py. Both keep the same state in data/daily-run.json and take the same
lock, so the two can never run over each other.

Steps, each checkpointed so a run cut short by sleep or shutdown resumes from
the step it stopped at:

1. ``pipeline.py run``: fetch every enabled source, score, report
2. ``pipeline.py liveness``: retire postings whose pages are gone
3. ``pipeline.py purge-expired``: delete expired postings from pipeline.db
4. sync platform.db from pipeline.db, then purge it, then queue outreach
   reminders (only once platform.db exists)

A fetch that could not reach some sources (exit 75) stays unfinished and is
retried, up to --max-attempts times, fetching only what is missing. With
--scheduled it is idempotent, for schedulers that fire far more often than
once a day: resume an unfinished run, otherwise start today's run only if it
has not happened and it is past --not-before, otherwise exit quietly.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import ROOT
from .refresh import TEMPFAIL_EXIT, _DailyRunMutex

DATA_DIR = ROOT / "data"
STATE_PATH = DATA_DIR / "daily-run.json"
LOG_PATH = DATA_DIR / "run.log"
RESUME_WINDOW_HOURS = 20

# Runs one Python command line and returns its exit code; output goes to the log.
Runner = Callable[[list[str]], int]


def _utc_stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _log(message: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(message + "\n")


def run_logged(arguments: list[str]) -> int:
    """Run `python <arguments>` from the project root, appending output to run.log."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1")
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        return subprocess.call(
            [sys.executable, *arguments],
            cwd=str(ROOT),
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


def read_state(path: Path = STATE_PATH) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    return {
        "runDate": str(raw.get("runDate") or ""),
        "startedAt": str(raw.get("startedAt") or ""),
        "attempts": int(raw.get("attempts") or 0),
        "completed": [step for step in raw.get("completed") or [] if step],
        "finishedAt": raw.get("finishedAt"),
        "exitCode": raw.get("exitCode"),
    }


def save_state(state: dict[str, Any], path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temporary.replace(path)


def network_available(host: str = "boards-api.greenhouse.io", seconds: int = 60) -> bool:
    deadline = time.monotonic() + seconds
    while True:
        try:
            socket.getaddrinfo(host, 443)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
        time.sleep(5)


def run_daily(
    *,
    scheduled: bool = False,
    not_before: str = "08:00",
    liveness_limit: int = 40,
    max_attempts: int = 4,
    runner: Runner = run_logged,
    now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    network: Callable[[], bool] = network_available,
    state_path: Path = STATE_PATH,
    platform_db: Path = DATA_DIR / "platform.db",
    log: Callable[[str], None] = _log,
) -> int:
    started = now()
    today = started.strftime("%Y-%m-%d")
    state = read_state(state_path)
    resuming = False

    if state and not state["finishedAt"]:
        try:
            began = datetime.fromisoformat(state["startedAt"].replace("Z", "+00:00"))
            age_hours = (started - began).total_seconds() / 3600
        except ValueError:
            age_hours = float("inf")
        if age_hours < RESUME_WINDOW_HOURS:
            resuming = True
        else:
            log(f"--- abandoning unfinished run started {state['startedAt']} ---")

    if not resuming:
        if scheduled:
            if state and state["finishedAt"] and state["runDate"] == today:
                return 0
            hour, _, minute = not_before.partition(":")
            if (started.hour, started.minute) < (int(hour), int(minute or 0)):
                return 0
        state = {
            "runDate": today,
            "startedAt": _utc_stamp(started),
            "attempts": 0,
            "completed": [],
            "finishedAt": None,
            "exitCode": None,
        }

    steps = ["run"]
    if liveness_limit > 0:
        steps.append("liveness")
    steps.append("purge-expired")
    if platform_db.exists():
        steps += ["platform-sync", "platform-purge", "outreach-remind"]
    pending = [step for step in steps if step not in state["completed"]]
    if ("run" in pending or "liveness" in pending) and not network():
        if not scheduled:
            print("No network connection; the run will start when one is available.")
        return 0

    def complete(step: str) -> None:
        state["completed"] = [*state["completed"], step]
        save_state(state, state_path)

    state["attempts"] = int(state["attempts"]) + 1
    save_state(state, state_path)
    last_attempt = state["attempts"] >= max_attempts
    stamp = lambda: now().isoformat()  # noqa: E731 - read the clock per line
    log("")
    if resuming:
        log(
            f"=== {stamp()} run resume (attempt {state['attempts']}, started {state['startedAt']}, "
            f"done: {', '.join(state['completed'])}) ==="
        )
    else:
        log(f"=== {stamp()} run start ===")

    run_exit = int(state["exitCode"]) if state["exitCode"] is not None else 0
    if "run" in pending:
        run_exit = runner(["pipeline.py", "run", "--resume-since", state["startedAt"]])
        state["exitCode"] = run_exit
        if run_exit == TEMPFAIL_EXIT and not last_attempt:
            # Leave 'run' unfinished so the next trigger fetches only what is missing.
            save_state(state, state_path)
            log(f"=== {stamp()} run paused: some sources were unreachable; will resume ===")
            return run_exit
        if run_exit == TEMPFAIL_EXIT:
            log(f"--- some sources still unreachable after {state['attempts']} attempts; keeping what arrived ---")
        elif run_exit != 0:
            # A fatal failure, not a source being down. finishedAt stays unset so
            # the day is never recorded as done; the resume window abandons it.
            save_state(state, state_path)
            log(
                f"=== {stamp()} run failed (exit {run_exit}) on attempt {state['attempts']}; "
                "downstream steps skipped, run left unfinished ==="
            )
            return run_exit
        complete("run")

    # Every later step is non-fatal, as in run-daily.ps1: log and carry on.
    later = {
        "liveness": (["pipeline.py", "liveness", "--limit", str(liveness_limit)], f"liveness (limit {liveness_limit})"),
        "purge-expired": (["pipeline.py", "purge-expired"], "purge expired"),
        "platform-sync": (["-m", "opportunity_app.migrate"], "sync platform database"),
        "platform-purge": (["-m", "opportunity_app.purge"], "purge platform database"),
        "outreach-remind": (["-m", "opportunity_app.outreach_cli", "remind"], "outreach reminders"),
    }
    for step in steps[1:]:
        if step not in pending:
            continue
        arguments, label = later[step]
        log(f"--- {label} ---")
        exit_code = runner(arguments)
        if exit_code != 0:
            log(f"--- {step} exited {exit_code} ---")
        complete(step)

    state["finishedAt"] = _utc_stamp(now())
    save_state(state, state_path)
    log(f"=== {stamp()} run end (exit {run_exit}) ===")
    return run_exit


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scheduled", action="store_true", help="Idempotent mode for a scheduler")
    parser.add_argument("--not-before", default="08:00", help="With --scheduled, the earliest time a new run starts")
    parser.add_argument("--liveness-limit", type=int, default=40)
    parser.add_argument("--max-attempts", type=int, default=4)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    mutex = _DailyRunMutex()
    if not mutex.acquire():
        if not args.scheduled:
            print("Another daily run is already in progress.")
        return 0
    try:
        return run_daily(
            scheduled=args.scheduled,
            not_before=args.not_before,
            liveness_limit=args.liveness_limit,
            max_attempts=args.max_attempts,
        )
    finally:
        mutex.release()


if __name__ == "__main__":
    raise SystemExit(main())
