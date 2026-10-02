"""Manual refresh and purge, started from the web app.

Runs the same steps as scripts/run-daily.ps1 in one background thread and
publishes per-step progress for the UI to poll:

1. ``pipeline.py run``: fetch every enabled source, score, report
2. ``pipeline.py liveness``: retire postings whose pages are gone
3. ``pipeline.py purge-expired``: delete expired postings from pipeline.db
4. sync platform.db from pipeline.db, retiring postings it no longer holds,
   then save and pass on new roles by the student's thresholds (auto_triage)
5. purge expired postings from platform.db

The pipeline CLI runs as a subprocess, as the worker does, so a hung fetch
cannot take the web server down. Progress comes from the lines it already
prints. Only one refresh runs at a time, and it shares the daily run's lock
so a manual refresh never overlaps the scheduled one.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

from . import DEFAULT_LEGACY_DB, DEFAULT_PROFILE, ROOT
from .auto_triage import triage_after_sync
from .core.daily_lock import TEMPFAIL_EXIT, DailyRunMutex
from .legacy import load_sources
from .purge import purge_expired_opportunities
from .legacy_sync import migrate_legacy_database
from .core.database import connect_product
from .core.timestamps import utc_now

PIPELINE_CLI = ROOT / "pipeline.py"
SOURCES_CONFIG = ROOT / "config" / "sources.json"
LIVENESS_LIMIT = 40

STEPS = (
    ("pull", "Pull new postings"),
    ("liveness", "Check posting links"),
    ("purge-legacy", "Purge expired postings (daily database)"),
    ("sync", "Sync postings into this app"),
    ("purge-app", "Purge expired postings (this app)"),
)

# Runs one CLI command, calling on_line for every output line; returns the exit code.
CommandRunner = Callable[[list[str], Callable[[str], None]], int]


def fresh_steps() -> list[dict[str, Any]]:
    return [
        {"key": key, "label": label, "state": "pending", "done": 0, "total": 0, "detail": ""}
        for key, label in STEPS
    ]


class RefreshBusy(RuntimeError):
    """A refresh is already running, here or as the scheduled daily run."""


def run_streaming(arguments: list[str], on_line: Callable[[str], None]) -> int:
    env = dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1")
    env.pop("PIPELINE_NOTIFICATIONS_LIVE", None)
    process = subprocess.Popen(
        [sys.executable, "-u", str(PIPELINE_CLI), *arguments],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        # The web server runs windowless; its children must not open a console.
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert process.stdout is not None
    for line in process.stdout:
        on_line(line.rstrip("\r\n"))
    return process.wait()


class RefreshManager:
    def __init__(
        self,
        platform_target: Path | str,
        *,
        legacy_path: Path = DEFAULT_LEGACY_DB,
        profile_path: Path = DEFAULT_PROFILE,
        sources_path: Path = SOURCES_CONFIG,
        runner: CommandRunner = run_streaming,
        mutex_factory: Callable[[], Any] = DailyRunMutex,
    ) -> None:
        self.platform_target = platform_target
        self.legacy_path = legacy_path
        self.profile_path = profile_path
        self.sources_path = sources_path
        self._runner = runner
        self._mutex_factory = mutex_factory
        self._lock = threading.Lock()
        self._state: dict[str, Any] | None = None
        self._thread: threading.Thread | None = None

    # -- public API -----------------------------------------------------------

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self._state is None:
                return {"state": "idle", "steps": fresh_steps(), "started_at": None,
                        "finished_at": None, "error": None}
            return json.loads(json.dumps(self._state))

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._state is not None and self._state["state"] == "running":
                raise RefreshBusy("A refresh is already running.")
            self._state = {
                "state": "running",
                "started_at": utc_now(),
                "finished_at": None,
                "error": None,
                "steps": fresh_steps(),
            }
        acquired = threading.Event()
        outcome: dict[str, bool] = {}

        def run() -> None:
            mutex = self._mutex_factory()
            outcome["owned"] = mutex.acquire()
            acquired.set()
            if not outcome["owned"]:
                return
            try:
                self._run_steps()
            finally:
                mutex.release()

        self._thread = threading.Thread(target=run, name="manual-refresh", daemon=True)
        self._thread.start()
        acquired.wait(timeout=10)
        if not outcome.get("owned"):
            with self._lock:
                self._state = None
            raise RefreshBusy("The scheduled daily refresh is running. Try again when it finishes.")
        return self.status()

    def wait(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    # -- steps ----------------------------------------------------------------

    def _update(self, key: str, **changes: Any) -> None:
        with self._lock:
            assert self._state is not None
            for step in self._state["steps"]:
                if step["key"] == key:
                    step.update(changes)

    def _run_steps(self) -> None:
        handlers = {
            "pull": self._pull,
            "liveness": self._liveness,
            "purge-legacy": self._purge_legacy,
            "sync": self._sync,
            "purge-app": self._purge_app,
        }
        failure = None
        for key, _label in STEPS:
            if failure is not None:
                self._update(key, state="skipped", detail="Skipped because an earlier step failed")
                continue
            self._update(key, state="running")
            try:
                handlers[key]()
            except Exception as exc:  # noqa: BLE001 - reported to the UI, not swallowed
                failure = f"{dict(STEPS)[key]} failed: {exc}"
                self._update(key, state="failed", detail=str(exc)[:500])
        with self._lock:
            assert self._state is not None
            self._state["state"] = "failed" if failure else "succeeded"
            self._state["error"] = failure
            self._state["finished_at"] = utc_now()

    def _enabled_source_count(self) -> int:
        try:
            config = load_sources(
                self.sources_path, self.sources_path.with_name("sources.local.json")
            )
        except (OSError, SystemExit):
            return 0
        return sum(1 for source in config.get("ats_sources", []) if source.get("enabled", True))

    def _pull(self) -> None:
        sources = self._enabled_source_count()
        # One unit per source, plus one for scoring and writing the reports.
        total = sources + 1
        self._update("pull", total=total, detail="Starting")
        fetched = 0

        def on_line(line: str) -> None:
            # Progress advances on completion lines, never on start lines.
            # The fetch runs sources concurrently, so a dozen "Fetching …"
            # lines can arrive before any source has finished; inferring
            # completion from the next start -- which this did -- would report
            # eleven sources done while none were.
            nonlocal fetched
            started = re.match(r"Fetching (.+) \(\w+\)", line)
            if started:
                self._update("pull", done=min(fetched, sources), detail=f"Fetching {started.group(1)}")
                return
            if re.match(r"\s+Done (.+) \(\w+\):", line):
                fetched += 1
                self._update(
                    "pull",
                    done=min(fetched, sources),
                    detail=f"Fetched {min(fetched, sources)} of {sources} sources",
                )
                return
            if line.startswith(("Imported ", "Scored ")):
                self._update("pull", done=sources, detail="Scoring and writing reports")

        exit_code = self._runner(["run"], on_line)
        if exit_code == TEMPFAIL_EXIT:
            self._update("pull", state="done", done=total,
                         detail="Some sources were unreachable; kept everything that arrived")
        elif exit_code != 0:
            raise RuntimeError(f"pipeline.py run exited {exit_code}")
        else:
            self._update("pull", state="done", done=total, detail=f"Fetched {fetched} sources")

    def _liveness(self) -> None:
        checked = 0
        tally = {"retired": 0}

        def on_line(line: str) -> None:
            nonlocal checked
            match = re.match(r"Checking (\d+) posting", line)
            if match:
                self._update("liveness", total=int(match.group(1)), detail="Checking links")
            elif re.match(r"  (ok|x|!|\?) ", line):
                checked += 1
                if line.startswith("  x ") and line.endswith("retired"):
                    tally["retired"] += 1
                self._update("liveness", done=checked)

        exit_code = self._runner(["liveness", "--limit", str(LIVENESS_LIMIT)], on_line)
        detail = f"Checked {checked}; retired {tally['retired']}" if checked else "No postings needed a check"
        if exit_code != 0:
            # The daily script logs and carries on; a flaky link check must not
            # block the sync.
            detail = f"{detail} (exited {exit_code})"
        self._update("liveness", state="done", done=max(checked, 1), total=max(checked, 1), detail=detail)

    def _purge_legacy(self) -> None:
        deleted = {"count": 0}

        def on_line(line: str) -> None:
            match = re.match(r"Deleted (\d+) posting", line)
            if match:
                deleted["count"] = int(match.group(1))

        self._update("purge-legacy", total=1)
        exit_code = self._runner(["purge-expired"], on_line)
        detail = f"Deleted {deleted['count']}"
        if exit_code != 0:
            # Non-fatal, as in the daily script: the sync is still worth running.
            detail = f"Purge exited {exit_code}; nothing may have been deleted"
        self._update("purge-legacy", state="done", done=1, detail=detail)

    def _sync(self) -> None:
        def progress(done: int, total: int) -> None:
            self._update("sync", done=done, total=total, detail="Writing postings")

        result = migrate_legacy_database(
            self.legacy_path, self.platform_target, self.profile_path, progress=progress
        )
        detail = f"{result.active_unique_target} active; retired {result.retired_missing} no longer listed"
        # Save and pass on the new roles by the student's thresholds (auto_save,
        # auto_pass). It never fails the sync: a failure is logged and shown in Health.
        triaged = triage_after_sync(self.platform_target)
        if triaged and (triaged["saved"] or triaged["passed"]):
            detail += f"; automatically saved {len(triaged['saved'])} and passed on {len(triaged['passed'])}"
        self._update("sync", state="done", done=1, total=1, detail=detail)

    def _purge_app(self) -> None:
        self._update("purge-app", total=1)
        with closing(connect_product(self.platform_target)) as conn:
            result = purge_expired_opportunities(conn)
        kept = result["kept_for_applications"]
        detail = f"Deleted {result['deleted']}"
        if kept:
            detail += f"; kept {kept} with an application"
        self._update("purge-app", state="done", done=1, detail=detail)
