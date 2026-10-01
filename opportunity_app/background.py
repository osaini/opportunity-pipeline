"""Background work: polling threads, single-run job managers, and the health helpers their steps share.

Standard library and the two stdlib-only leaves timestamps and mail_message at
import time (automation.record_health is imported where used), so any worker
can import this without loading the mail reader or the Gmail stack.

- PollingWorker: one daemon thread that runs a pass, then sleeps until the next
  interval or until wake() is called. AutomationWorker, CallPrepWorker and
  InboxWatcher are built on it.
- SingleFlightManager: one background job at a time, with a status the web app
  polls. DiscoveryManager and RecontactManager are built on it.
- step_error, record_health_quietly: what a worker records when one of its steps
  fails, with nothing private in it. (Rolling a failed step back is
  database.rollback_quietly.)
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from typing import Any, Callable

from .mail_message import strip_queries
from .timestamps import utc_now

LOGGER = logging.getLogger(__name__)


class PollingWorker:
    """A daemon thread that runs one pass, waits, and runs the next.

    A subclass sets thread_name, failure_message and logger (so a failed pass is
    logged where it always was), and writes _run_pass(). before_start() runs on
    the caller's thread, after the stop flag is cleared and before the thread
    starts. Attribute names _stop, _wake and _thread are kept: a subclass's own
    loop reads them.
    """

    thread_name = "polling-worker"
    failure_message = "Background pass failed"
    logger: logging.Logger = LOGGER

    def __init__(self, interval_seconds: float) -> None:
        self._interval = interval_seconds
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def _run_pass(self) -> Any:
        raise NotImplementedError

    def before_start(self) -> None:
        """Hook for work that must finish before the thread starts."""

    def wake(self) -> None:
        self._wake.set()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.before_start()
        self._thread = threading.Thread(target=self._loop, name=self.thread_name, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._run_pass()
            except Exception:  # the thread must outlive any one bad pass
                self.logger.exception(self.failure_message)
            self._wake.wait(self._interval)
            self._wake.clear()


class SingleFlightManager:
    """Runs one background job at a time and reports how it is going.

    A subclass names the error to raise when a job is already running
    (busy_error, busy_message) and any extra keys its status carries
    (idle_extra, filled by _launch's keyword arguments while a job runs). The
    status is always a copy, so a caller can keep it.
    """

    busy_error: type[Exception] = RuntimeError
    busy_message = "A job is already running"
    idle_extra: dict[str, Any] = {}

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {
            "state": "idle", **self.idle_extra, "started_at": None, "finished_at": None, "error": None, "result": None,
        }
        self._thread: threading.Thread | None = None

    def status(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._state))

    def _launch(self, thread_name: str, work: Callable[[], Any], **extra: Any) -> dict[str, Any]:
        """Start work() on its own thread. Its return value becomes the status's result; an exception, its error."""
        with self._lock:
            if self._state["state"] == "running":
                raise self.busy_error(self.busy_message)
            self._state = {
                "state": "running", **extra, "started_at": utc_now(), "finished_at": None, "error": None, "result": None,
            }

        def run() -> None:
            result, error = None, None
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001 - reported to the UI
                error = str(exc)[:1_000]
            with self._lock:
                self._state.update(state="failed" if error else "succeeded", error=error, result=result, finished_at=utc_now())

        self._thread = threading.Thread(target=run, name=thread_name, daemon=True)
        self._thread.start()
        return self.status()

    def wait(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)


# --- What a failed step records ----------------------------------------------------------

_ADDRESS = re.compile(r"""[^\s@<>"'(),;:]+@[^\s@<>"'(),;:]+""")


def step_error(exc: BaseException) -> str:
    """An exception as a health error: its type and message, with any address and any URL's query string taken out."""
    words = _ADDRESS.sub("[address]", strip_queries(str(exc)))
    return f"{type(exc).__name__}: {words[:200]}"


def record_health_quietly(
    conn: sqlite3.Connection, user_id: str, component: str, *, ok: bool, error: str = "", detail: dict[str, Any] | None = None,
) -> None:
    """automation.record_health, which opens its own transaction; a failure to record is logged, never raised."""
    from . import automation

    try:
        automation.record_health(conn, user_id, component, ok=ok, error=error, detail=detail)
    except Exception:  # noqa: BLE001 - the pass goes on to the next step and the next student
        LOGGER.warning("Could not record the health of %s", component, exc_info=True)
