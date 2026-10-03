"""Apply for me, the runner: one run at a time, in a process of its own, with a watchdog that can kill the whole tree.

``ApplyRunner.start`` takes the single slot, reads the posting and the student's data the way the check does (never
from a cache), refuses what may not run, writes the run's row and starts a supervisor thread. The thread asks
``supervise`` to start the agent in a child process (multiprocessing ``spawn``, its own session on POSIX) and then pumps
the pipe between them: progress and heartbeats go to the row, a replan request is answered with a plan built here (a
``policy.Sources`` holds a database connection, so planning never crosses the pipe), a hand-over is always refused (M5a
has no claim; lookups and rehearsals never submit), and the one terminal message ends the run.

The watchdog is the point of the process boundary. At the deadline, on a stop request that is not heard within the
grace, and when the server shuts down, ``kill_tree`` ends the child and every process it started (Chromium and its
helpers): the descendants are listed first, then ``taskkill /T /F`` on Windows, or on POSIX each descendant, the child
itself and its process group, because Playwright starts Chromium in a session of its own. A fake agent that opens no browser may run in a thread
instead (``isolation = "thread"``); a thread cannot be killed, so at the deadline it is asked to stop and abandoned.

Everything a run leaves behind is value-free (apply/agent_types.py): sentences, public page text, hashes, request facts.
An exception from the child is reported by its type name, never its message.

The run views at the bottom turn a stored row into what the page shows (spec 10.4): the summary sentence, the measured
sentence, the problems, a value-free table of the fields, and the pictures.
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import os
import signal
import sqlite3
import subprocess
import threading
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from pipeline_core.identity import employer_key

from . import checks as apply_checks, greenhouse as apply_greenhouse, policy as apply_policy, preflight as apply_preflight, runs as apply_runs
from .agent_types import (
    MODE_FOR_KIND, OP_CANCEL, OP_ERROR, OP_HAND_OVER, OP_HAND_OVER_REPLY, OP_HEARTBEAT, OP_PROGRESS, OP_REPLAN, OP_REPLAN_REPLY, OP_RESULT,
    ISOLATIONS, OUTCOMES, PROGRESS_STEPS, STOPPED, AgentJob, ApplyTimeouts, FilePayload, LookupRequest, RunResult,
)
from .claims import HELD_HEARTBEAT
from .runner_child import child_main
from ..applications.extension import confirmed_resume_file
from ..automation import ledger as automation
from ..core.database import connect_product, rollback_quietly
from ..core.json_values import json_as
from ..core.timestamps import parse_app_instant, utc_now

LOGGER = logging.getLogger(__name__)

# How long a run may take, by kind, in seconds (6.0, 4.6). A submit or a handoff waits for the student too.
DEADLINES = {"lookup": 120.0, "rehearsal": 300.0}
CANCEL_GRACE_S = 10.0
HEARTBEAT_EVERY_S = 20.0
# A row that says 'running' while this server has no run of that id is the leftover of a server that was stopped mid-run. It is
# given this long first, since a run's row is written a moment before its supervisor thread says it is working.
ORPHAN_AFTER_S = 15.0
PROGRESS_KEEP = 50
# How long a finished child gets to leave on its own before its tree is killed.
EXIT_GRACE_S = 10.0
# The slow Windows fallback for the process table (PowerShell and CIM) may not hold up a kill for longer than this.
CIM_TIMEOUT_S = 5.0

BUSY = "Another application is being filled. Wait for it to finish."
NO_SENT = "No application was sent."
TOO_LONG = "The run took longer than {minutes} minutes, so the app stopped it. No application was sent."
CHILD_DIED = "The browser stopped before the run finished. No application was sent."
SERVER_STOPPED = "The app stopped during this run. No application was sent."
NOT_STARTED = "The app could not start this run. No application was sent."
POSTING_FIRST = "Check the posting first."
NO_LOOKUP_FIELD = "That field has no list of options to look up"
NO_RUN = "No run with that id"
FINISHED_ALREADY = "This run has already finished"
NOT_RUNNING = "This run is not running in this app"
ACCOUNT_ENDING = "This account is being deleted, so nothing can be started"
NO_PICTURE = "This picture is no longer kept"
NOT_REVIEWABLE = "Only a rehearsal that finished or stopped with something for you can be marked"

STOP_DEADLINE, STOP_CANCELLED, STOP_CHILD_DIED, STOP_ERROR = "deadline", "cancelled", "child_died", "error"


def deadline_for(kind: str, timeouts: ApplyTimeouts = ApplyTimeouts()) -> float:
    """Seconds a run of this kind may take before the watchdog ends it."""
    if kind in DEADLINES:
        return DEADLINES[kind]
    if kind == "submit":
        return 300.0 + timeouts.person_s + timeouts.security_code_s
    if kind == "handoff":
        return timeouts.handoff_s + timeouts.security_code_s + 120.0
    raise ValueError(f"Unsupported run kind: {kind}")


class RunnerBusy(Exception):
    """The one slot is taken."""

    def __init__(self) -> None:
        super().__init__(BUSY)


class RunRefused(Exception):
    """A run that may not start, with the HTTP status the route answers and the sentence for the student."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


# --- Killing a tree ----------------------------------------------------------------------------------------------


def _windows_process_table() -> str:
    """Every process as "pid ppid" lines, from a Toolhelp snapshot (milliseconds; a PowerShell CIM query takes over a second). "" when unavailable."""
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel32.Process32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)   # TH32CS_SNAPPROCESS
    if snapshot in (None, wintypes.HANDLE(-1).value):
        return ""
    lines: list[str] = []
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(ProcessEntry)
        found = kernel32.Process32First(snapshot, ctypes.byref(entry))
        while found:
            lines.append(f"{entry.th32ProcessID} {entry.th32ParentProcessID}")
            found = kernel32.Process32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return "\n".join(lines)


def _process_table() -> str:
    """Every process as "pid ppid" lines: ``ps`` on POSIX, a Toolhelp snapshot on Windows (a CIM query only if that fails). "" when the listing is unavailable."""
    try:
        if os.name == "nt":
            try:
                table = _windows_process_table()
            except (OSError, AttributeError, ValueError, ImportError):
                table = ""
            if table:
                return table
            # Bounded: the listing only names pids for the caller, and taskkill walks the tree itself.
            return subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Get-CimInstance Win32_Process | ForEach-Object { '{0} {1}' -f $_.ProcessId, $_.ParentProcessId }"],
                capture_output=True, text=True, timeout=CIM_TIMEOUT_S, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout
        return subprocess.run(["ps", "-A", "-o", "pid=", "-o", "ppid="], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def descendants(pid: int) -> list[int]:
    """Every process below ``pid``, children listed first. Empty when the process table cannot be read."""
    children: dict[int, list[int]] = {}
    for line in _process_table().splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    found: list[int] = []
    queue = [pid]
    while queue:
        for child in children.get(queue.pop(0), []):
            if child not in found and child != pid:
                found.append(child)
                queue.append(child)
    return found


def kill_tree(pid: int) -> list[int]:
    """End ``pid`` and everything it started, and return the pids targeted (so a caller can check each is gone). Never raises."""
    below = descendants(pid)
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            LOGGER.warning("taskkill did not answer for a run's browser")
        return [pid, *below]
    # The descendants go first. Then the process itself, always by pid: a child that has not yet called setsid shares the
    # runner's process group and has none of its own, so a group kill alone would miss it. Then its group, when it leads one,
    # which takes whatever stayed in it. Chromium started its own session, so it is not in the group and is killed by pid above.
    actions: list[Callable[[], None]] = [lambda target=target: os.kill(target, signal.SIGKILL) for target in below]
    actions.append(lambda: os.kill(pid, signal.SIGKILL))
    try:
        if os.getpgid(pid) == pid and pid != os.getpgrp():
            actions.append(lambda: os.killpg(pid, signal.SIGKILL))
    except OSError:
        pass
    for action in actions:
        try:
            action()
        except (ProcessLookupError, PermissionError, OSError):
            pass
    return [pid, *below]


def process_alive(pid: int) -> bool:
    """Whether a process with this pid is running (a zombie is not)."""
    if os.name == "nt":
        try:
            listing = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], capture_output=True, text=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        return any(len(row) > 1 and row[1].strip().strip('"') == str(pid) for row in (line.split('","') for line in listing.splitlines()))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as stat:
            return stat.read().rsplit(")", 1)[-1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


# --- Supervising one child ---------------------------------------------------------------------------------------


@dataclass
class SupervisorHandlers:
    progress: Callable[[str, str], None] = lambda step, text: None
    heartbeat: Callable[[], None] = lambda: None
    replan: Callable[[list[dict[str, Any]], bool], Any] | None = None
    hand_over: Callable[[], bool] = lambda: False
    # Called every HEARTBEAT_EVERY_S while the child lives, so a quiet agent does not look dead.
    tick: Callable[[], None] = lambda: None


@dataclass
class Supervised:
    result: RunResult | None
    stop: str = ""          # "" | "deadline" | "cancelled" | "child_died" | "error"
    error: str = ""         # the child's exception type name
    killed_pids: list[int] = field(default_factory=list)


def _guard(call: Callable[[], Any]) -> Any:
    """A handler that fails (the database was busy) must not end the run: it is logged, without a value."""
    try:
        return call()
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("An Apply for me run handler failed: %s", type(exc).__name__)
        return None


def _answer(inbox: Any, message: dict[str, Any]) -> None:
    try:
        inbox.send(message)
    except (OSError, ValueError):
        pass  # the child is gone


def _dispatch(message: Any, handlers: SupervisorHandlers, inbox: Any) -> tuple[bool, RunResult | None, str]:
    """Act on one message from the child. (done, result, error type name): done is True for the terminal message."""
    if not isinstance(message, dict):
        return False, None, ""
    op = message.get("op")
    if op == OP_PROGRESS:
        _guard(lambda: handlers.progress(str(message.get("step") or ""), str(message.get("text") or "")))
    elif op == OP_HEARTBEAT:
        _guard(handlers.heartbeat)
    elif op == OP_REPLAN:
        ident = message.get("id")
        try:
            if handlers.replan is None:
                raise LookupError("no planner")
            plan = handlers.replan(list(message.get("scan") or []), bool(message.get("uploads_on_attach")))
            reply: dict[str, Any] = {"op": OP_REPLAN_REPLY, "id": ident, "plan": plan, "error": ""}
        except Exception as exc:  # noqa: BLE001 - the child is told the type name only
            reply = {"op": OP_REPLAN_REPLY, "id": ident, "plan": None, "error": type(exc).__name__}
        _answer(inbox, reply)
    elif op == OP_HAND_OVER:
        try:
            granted = bool(handlers.hand_over())
        except Exception:  # noqa: BLE001
            granted = False
        _answer(inbox, {"op": OP_HAND_OVER_REPLY, "id": message.get("id"), "ok": granted})
    elif op == OP_RESULT:
        result = message.get("result")
        return True, result if isinstance(result, RunResult) else None, "" if isinstance(result, RunResult) else "BadResult"
    elif op == OP_ERROR:
        return True, None, str(message.get("error") or "Error")
    return False, None, ""


def _drain_terminal(outbox: Any, handlers: SupervisorHandlers, inbox: Any) -> tuple[RunResult | None, str] | None:
    """Read what is already in the pipe, without waiting. (result, error) for the terminal message if it is there, else None."""
    try:
        while outbox.poll(0):
            done, result, error = _dispatch(outbox.recv(), handlers, inbox)
            if done:
                return result, error
    except (EOFError, OSError):
        return None
    return None


def supervise(
    factory: Any, job: AgentJob, *, deadline_s: float, handlers: SupervisorHandlers, cancel: threading.Event,
    cancel_grace_s: float = CANCEL_GRACE_S, poll_s: float = 0.25, abort: threading.Event | None = None,
    tick_s: float = HEARTBEAT_EVERY_S,
) -> Supervised:
    """Run one job in a child (a spawned process, or a thread for a fake) and pump its pipe until it ends or is stopped.

    ``cancel`` asks the agent to stop (``OP_CANCEL``) and gives it ``cancel_grace_s`` before the tree is killed;
    ``abort`` kills at once (the server is shutting down). At ``deadline_s`` from now the tree is killed.
    """
    isolation = getattr(factory, "isolation", "process")
    if isolation not in ISOLATIONS:
        raise ValueError(f"Unsupported agent isolation: {isolation!r}")
    in_process = isolation != "thread"   # only the exact word "thread" runs the agent where the watchdog cannot kill it
    if in_process:
        job = replace(job, deadline_s=deadline_s)   # the child bounds itself by it too, for the day this watchdog is gone
    context = multiprocessing.get_context("spawn")
    inbox_recv, inbox_send = context.Pipe(duplex=False)      # parent -> child
    outbox_recv, outbox_send = context.Pipe(duplex=False)    # child -> parent
    worker: Any
    if in_process:
        worker = context.Process(target=child_main, args=(factory, job, inbox_recv, outbox_send, True), daemon=True)
    else:
        worker = threading.Thread(target=child_main, args=(factory, job, inbox_recv, outbox_send, False), daemon=True, name="apply-child")
    try:
        worker.start()
    except BaseException:
        for end in (inbox_recv, inbox_send, outbox_recv, outbox_send):
            end.close()
        raise
    if in_process:
        # The child holds its own copies now. Without these closed here, its death would never show as end-of-file.
        inbox_recv.close()
        outbox_send.close()
    started = time.monotonic()
    deadline_at = started + deadline_s
    last_tick = started
    cancel_sent: float | None = None
    outcome = Supervised(None)
    try:
        while True:
            now = time.monotonic()
            stopping = abort is not None and abort.is_set()
            if stopping or now >= deadline_at:
                # A terminal message that is already waiting is a result the child finished in time (a rehearsal that ended as the server
                # was stopped, or as the deadline came): keep it.
                late = _drain_terminal(outbox_recv, handlers, inbox_send)
                if late is not None:
                    outcome.result, outcome.error = late[0], late[1]
                    if late[0] is None:
                        outcome.stop = STOP_ERROR
                    break
                # A Stop pressed shortly before the deadline is still the student's: the agent was closing its browser when time ran out.
                outcome.stop = STOP_CANCELLED if stopping or cancel.is_set() else STOP_DEADLINE
                if not in_process:
                    _answer(inbox_send, {"op": OP_CANCEL})   # a thread cannot be killed: ask it to end rather than linger
                break
            if cancel.is_set():
                if cancel_sent is None:
                    _answer(inbox_send, {"op": OP_CANCEL})
                    cancel_sent = now
                elif now - cancel_sent >= cancel_grace_s:
                    outcome.stop = STOP_CANCELLED
                    break
            if now - last_tick >= tick_s:
                _guard(handlers.tick)
                last_tick = now
            try:
                arrived = outbox_recv.poll(min(poll_s, max(0.0, deadline_at - now)))
                message = outbox_recv.recv() if arrived else None
            except (EOFError, OSError):
                outcome.stop = STOP_CHILD_DIED
                break
            if arrived:
                done, result, error = _dispatch(message, handlers, inbox_send)
                if done:
                    outcome.result = result
                    if result is None:
                        outcome.stop, outcome.error = STOP_ERROR, error
                    break
                continue
            if not worker.is_alive():
                # It may have sent its last message just before it exited: read what is still in the pipe first.
                try:
                    if outbox_recv.poll(0):
                        continue
                except (EOFError, OSError):
                    pass
                outcome.stop = STOP_CHILD_DIED
                break
    finally:
        if in_process:
            outcome.killed_pids = _end_child(worker, outcome.stop)
        else:
            if not outcome.stop:
                worker.join(EXIT_GRACE_S)
        for end in (inbox_send, outbox_recv):
            try:
                end.close()
            except OSError:
                pass
    return outcome


def _end_child(worker: Any, stop: str) -> list[int]:
    """Make sure a process child and everything it started is gone. A child that sent its result gets a moment to leave first."""
    if not stop or stop == STOP_ERROR:
        worker.join(EXIT_GRACE_S)
    killed: list[int] = []
    if worker.is_alive() and worker.pid:
        killed = kill_tree(worker.pid)
    worker.join(10)
    return killed


# --- The runner ---------------------------------------------------------------------------------------------------


@dataclass
class _Active:
    run_id: str = ""
    user_id: str = ""
    cancel: threading.Event = field(default_factory=threading.Event)
    abort: threading.Event = field(default_factory=threading.Event)
    shutting_down: bool = False


@dataclass
class _Job:
    """What the supervisor thread needs, gathered by ``start`` in the request."""

    run_id: str
    kind: str
    user_id: str
    opportunity_id: str
    company: str
    page_url: str
    database_target: Any
    apply_root: Path
    resume_root: Path
    factory: Any
    schema: list[Any]
    job: AgentJob
    deadline_s: float
    lookup: LookupRequest | None = None
    # The listing the check read (public text): its title and company, whether it differed from the saved role, and whether the student said it was right.
    posting: dict[str, Any] = field(default_factory=dict)


def _stamp() -> str:
    return utc_now()


def _too_long(deadline_s: float) -> str:
    minutes = round(deadline_s / 60)
    return "The run took longer than a minute, so the app stopped it. No application was sent." if minutes <= 1 else TOO_LONG.format(minutes=minutes)


class ApplyRunner:
    """The single slot. One per app, so in the real server (one app per process) it is the process-wide slot of the spec."""

    def __init__(self, *, deadlines: Mapping[str, float] | None = None, cancel_grace_s: float = CANCEL_GRACE_S) -> None:
        self._deadlines = dict(DEADLINES)
        self._deadlines.update(deadlines or {})
        self._cancel_grace_s = cancel_grace_s
        self._lock = threading.Lock()
        self._active: _Active | None = None
        self._deleting: set[str] = set()   # students whose account is being deleted: nothing of theirs may start meanwhile
        self._done: dict[str, threading.Event] = {}
        self._last_outcomes: dict[str, str] = {}   # each student's own last outcome: one student's run is never written into another's health row

    # --- the slot

    def busy(self) -> str | None:
        """The id of the run in the slot (``"pending"`` while a start is still reading the posting), or None."""
        with self._lock:
            return None if self._active is None else (self._active.run_id or "pending")

    def _reserve(self, user_id: str) -> _Active:
        with self._lock:
            if user_id in self._deleting:
                raise RunRefused(409, ACCOUNT_ENDING)
            if self._active is not None:
                raise RunnerBusy()
            self._active = _Active(user_id=user_id)   # named inside the lock, so a deletion that looks never sees an unnamed run
            return self._active

    def _release(self, active: _Active) -> None:
        with self._lock:
            if self._active is active:
                self._active = None

    def deadline_s(self, kind: str) -> float:
        return float(self._deadlines.get(kind) or deadline_for(kind))

    def cancel(self, run_id: str) -> bool:
        """Ask the run to stop. False when this runner is not running it (another process's, or one that has ended)."""
        with self._lock:
            active = self._active
            if active is None or active.run_id != run_id:
                return False
            active.cancel.set()
            return True

    def stop_for_user(self, user_id: str, timeout: float = 30.0) -> bool:
        """End the run this student has in the slot, killing its browser at once, and wait until its row is written.

        True when nothing of theirs is running any more. Deleting an account calls it first: a run that outlived the deletion
        would write a picture of the filled form into a folder that is gone.
        """
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self._lock:
                active = self._active
                if active is None or active.user_id != user_id:
                    return True
                active.abort.set()
                active.cancel.set()
            time.sleep(0.05)
        return False

    @contextmanager
    def held_for(self, user_id: str, timeout: float = 30.0) -> Iterator[None]:
        """Account deletion: end this student's run, and let none of theirs start until the block is over.

        Raises RunnerBusy when their run has not ended within ``timeout``. A start that arrives meanwhile (a second tab, a
        token caller) is refused, since it would make the folder the deletion removes, and write a picture into it.
        """
        with self._lock:
            self._deleting.add(user_id)
        try:
            if not self.stop_for_user(user_id, timeout):
                raise RunnerBusy()
            yield
        finally:
            with self._lock:
                self._deleting.discard(user_id)

    def wait(self, run_id: str, timeout: float) -> bool:
        """For tests: True once this run has been finished (its row written, the slot free)."""
        with self._lock:
            event = self._done.get(run_id)
        return bool(event is not None and event.wait(timeout))

    def shutdown(self, timeout: float = 10.0) -> None:
        """The server is stopping: kill the child's tree and finish the run as stopped by the app."""
        with self._lock:
            active = self._active
            event = self._done.get(active.run_id) if active is not None else None
            if active is not None:
                active.shutting_down = True
                active.abort.set()
                active.cancel.set()
        if event is not None:
            event.wait(timeout)

    # --- starting a run

    def start(
        self, conn: sqlite3.Connection, *, database_target: Any, user_id: str, opportunity_id: str, kind: str, agent_factory: Any,
        schema_client: Any, apply_root: Path, resume_root: Path, lookup_key: str = "", lookup_text: str = "",
        posting_confirmed: bool = False, now: datetime | None = None,
    ) -> str:
        """Check the role, write the run's row and start the supervisor. Returns the run id; the run goes on in a thread.

        A rehearsal of a posting that does not look like the saved role (``posting.differs``) is refused until the student
        has said it is the right one (``posting_confirmed``): the run is filed under the saved role's company.

        Raises RunnerBusy, RunRefused (the sentence and status for the route) and OpportunityNotFoundError.
        """
        if kind not in ("lookup", "rehearsal"):
            raise ValueError(f"Unsupported run kind: {kind}")
        active = self._reserve(user_id)
        run_id = ""
        try:
            inputs = apply_preflight.run_inputs(
                conn, user_id, opportunity_id, client=schema_client, mode="rehearse", resume_root=resume_root, apply_root=apply_root, now=now,
            )
            result = inputs.result
            if inputs.plan is None or inputs.schema is None or result["status"] in ("unavailable", "failed"):
                raise RunRefused(409, str(result.get("message") or apply_preflight.NOT_GREENHOUSE))
            posting = result.get("posting") if isinstance(result.get("posting"), dict) else {}
            if kind == "rehearsal" and posting.get("differs") and not posting_confirmed:
                raise RunRefused(409, f"{POSTING_FIRST} {posting.get('difference') or ''}".strip())
            block = apply_runs.rehearsal_block(conn, user_id, now)
            if block:
                raise RunRefused(409, block)
            lookup: LookupRequest | None = None
            if kind == "lookup":
                target = next((item for item in inputs.schema if item.name == lookup_key), None)
                label_field = apply_policy.label_field_of(target) if target is not None else ""
                if target is None or label_field not in apply_policy.ALLOWED_ATS_LABEL_FIELDS:
                    raise RunRefused(422, NO_LOOKUP_FIELD)
                lookup = LookupRequest(key=target.name, field=label_field, question=target.label, text=lookup_text)
            files = {} if kind == "lookup" else self._files(conn, user_id, inputs.plan, resume_root)
            page_url = str(result["canonical_url"])
            deadline = self.deadline_s(kind)
            screenshot_dir = ""
            if kind != "lookup":
                # Made before the row, so a full or read-only apply folder refuses the start instead of leaving a row behind.
                folder = Path(apply_root) / apply_runs.user_folder(user_id) / apply_runs.opportunity_folder(opportunity_id)
                folder.mkdir(parents=True, exist_ok=True)
                screenshot_dir = str(folder.resolve())
            run_id = apply_runs.create_run(
                conn, user_id=user_id, opportunity_id=opportunity_id, kind=kind, started_by="student", ats=apply_greenhouse.ATS_GREENHOUSE,
                board_token=str(result["board_token"]), page_url=page_url, company=employer_key(str(result["company"])),
                deadline_seconds=int(deadline), now=now,
            )
            job = AgentJob(
                run_id=run_id, mode=MODE_FOR_KIND[kind], page_url=page_url, plan=inputs.plan, schema=list(inputs.schema), files=files,
                lookup=lookup, screenshot_dir=screenshot_dir,
            )
            work = _Job(
                run_id=run_id, kind=kind, user_id=user_id, opportunity_id=opportunity_id, company=str(result["company"]), page_url=page_url,
                database_target=database_target, apply_root=Path(apply_root), resume_root=Path(resume_root), factory=agent_factory,
                schema=list(inputs.schema), job=job, deadline_s=deadline, lookup=lookup,
                posting={
                    "title": str(posting.get("title") or ""), "company": str(posting.get("company") or ""),
                    "differs": bool(posting.get("differs")), "confirmed": bool(posting.get("differs") and posting_confirmed),
                },
            )
            done = threading.Event()
            with self._lock:
                active.run_id = run_id
                self._done[run_id] = done
                for old in list(self._done)[:-50]:
                    self._done.pop(old, None)
            thread = threading.Thread(target=self._thread, args=(active, work, done), name=f"apply-runner-{run_id}", daemon=True)
            thread.start()
            return run_id
        except BaseException:
            if run_id:
                self._abandon(conn, run_id)
            self._release(active)
            raise

    @staticmethod
    def _abandon(conn: sqlite3.Connection, run_id: str) -> None:
        """A start that failed after its row was written: finish the row now, so it is not left 'running' with a Stop that does nothing."""
        try:
            apply_runs.finish_run(conn, run_id, outcome="failed", clean=False, reasons=[NOT_STARTED])
        except Exception as exc:  # noqa: BLE001 - recover_stale finishes the row later
            LOGGER.error("An Apply for me run that did not start could not be closed (%s)", type(exc).__name__)
            rollback_quietly(conn, LOGGER, "closing an Apply for me run that did not start")

    @staticmethod
    def _files(conn: sqlite3.Connection, user_id: str, plan: Any, resume_root: Path) -> dict[str, FilePayload]:
        """The résumé to attach, read now and verified against the hash stored at upload. None when it cannot be read: the agent says so."""
        for entry in plan.fields:
            if entry.source.kind == "resume" and entry.disposition in ("fill", "deferred"):
                try:
                    path, name, media_type, sha = confirmed_resume_file(conn, entry.source.ref, resume_root, user_id=user_id, verify=True)
                    return {entry.key: FilePayload(name=name, mime_type=media_type, buffer=path.read_bytes(), sha256=sha)}
                except Exception:  # noqa: BLE001 - a missing or changed file leaves no payload; the rehearsal then reports NO_FILE
                    return {}
        return {}

    # --- the supervisor thread

    def _thread(self, active: _Active, work: _Job, done: threading.Event) -> None:
        try:
            with apply_runs.running_run(work.run_id), closing(connect_product(work.database_target)) as conn:
                self._work(conn, active, work)
        except Exception as exc:  # noqa: BLE001 - recover_stale finishes the row later; no value is logged
            LOGGER.error("An Apply for me run ended without recording its result (%s)", type(exc).__name__)
        finally:
            self._release(active)
            done.set()

    def _work(self, conn: sqlite3.Connection, active: _Active, work: _Job) -> None:
        run_id, user_id = work.run_id, work.user_id
        steps: list[dict[str, str]] = []

        def note(step: str, text: str) -> None:
            steps.append({"at": _stamp(), "step": step, "text": text})
            del steps[:-PROGRESS_KEEP]
            with conn:
                conn.execute(
                    "UPDATE apply_runs SET progress_json=?, heartbeat_at=? WHERE id=? AND status='running'",
                    (json.dumps(steps, sort_keys=True), _stamp(), run_id),
                )

        busy_since = _stamp()
        self._health(conn, user_id, ok=True, detail={"busy_since": busy_since, "run_id": run_id, "last_outcome": self._last_outcomes.get(user_id, "")})
        outcome = Supervised(None, stop=STOP_ERROR, error="NotStarted")
        supervising = False
        try:
            _guard(lambda: note("start", PROGRESS_STEPS["start"]))
            sources = apply_policy.sources_for(
                conn, user_id, work.opportunity_id, company=work.company, storage_root=work.resume_root, key=apply_policy.mac_key(work.apply_root),
            )

            def planner(scan: list[dict[str, Any]], uploads_on_attach: bool) -> Any:
                return apply_policy.build_plan(
                    apply_policy.with_page_labels(work.schema, scan), scan, sources, work.company, "rehearse",
                    canonical_url=work.page_url, adapter_version=apply_greenhouse.ADAPTER_VERSION, uploads_on_attach=uploads_on_attach,
                )

            handlers = SupervisorHandlers(
                progress=lambda step, text: note(step, text),
                heartbeat=lambda: apply_runs.heartbeat_run(conn, run_id),
                replan=planner, hand_over=lambda: False,
                tick=lambda: apply_runs.heartbeat_run(conn, run_id),
            )
            supervising = True
            outcome = supervise(
                work.factory, work.job, deadline_s=work.deadline_s, handlers=handlers, cancel=active.cancel, abort=active.abort,
                cancel_grace_s=self._cancel_grace_s,
            )
        except Exception as exc:  # noqa: BLE001 - the type name is kept, never the message
            LOGGER.error("An Apply for me run could not be supervised (%s)", type(exc).__name__)
            outcome = Supervised(None, stop=STOP_ERROR, error=type(exc).__name__ if supervising else "NotStarted")
        # A console's Ctrl+C reaches the child as well as the server on Windows, and the child can report it before the server's
        # own shutdown starts. It is the server stopping, not a browser that broke.
        interrupted = outcome.stop == STOP_ERROR and outcome.error == "KeyboardInterrupt"
        final = self._finish(conn, work, outcome, shutting_down=active.shutting_down or interrupted)
        self._last_outcomes[user_id] = final
        died = outcome.stop in (STOP_CHILD_DIED, STOP_ERROR) and not interrupted
        self._health(
            conn, user_id, ok=not died, error=(outcome.error or outcome.stop) if died else "",
            detail={"busy_since": None, "run_id": run_id, "last_outcome": final},
        )

    def _finish(self, conn: sqlite3.Connection, work: _Job, outcome: Supervised, *, shutting_down: bool) -> str:
        """Write the run's result (3.4). The outcome is ``failed`` whenever the runner stopped the run or no result came."""
        result = outcome.result
        reasons: list[str] = []
        if outcome.stop or result is None:
            if shutting_down:
                reasons.append(SERVER_STOPPED)
            elif outcome.stop == STOP_DEADLINE:
                reasons.append(_too_long(work.deadline_s))
            elif outcome.stop == STOP_CANCELLED:
                reasons.append(STOPPED)
            else:
                reasons.append(NOT_STARTED if outcome.error == "NotStarted" else CHILD_DIED)
        fits = result is not None and not outcome.stop and result.outcome in _OUTCOMES_FOR_KIND.get(work.kind, ())
        final = result.outcome if fits and result is not None else "failed"
        if result is not None:
            if not outcome.stop and not fits:
                reasons.append(CHILD_DIED)   # a result this kind of run cannot end in is a fault of the agent, not a finding
            reasons.extend(item for item in result.reasons if item not in reasons)
        clean = False
        if result is not None and work.kind == "rehearsal" and final == "rehearsed":
            clean = apply_checks.clean_rehearsal({
                "outcome": final, "plan": result.plan, "join_problems": result.join_problems, "check_problems": result.check_problems,
            })
        evidence: dict[str, Any] = dict(result.evidence) if result is not None else {}
        if result is not None:
            evidence["join_problems"] = list(result.join_problems)
            evidence["check_problems"] = list(result.check_problems)
        if work.posting:
            evidence["posting"] = dict(work.posting)
        if work.lookup is not None:
            evidence["lookup"] = {"key": work.lookup.key, "field": work.lookup.field, "question": work.lookup.question}
        documents: dict[str, Any] = {"reasons": reasons, "evidence": evidence}
        plan_hash = None
        if result is not None:
            documents.update(
                plan=list(result.plan), options=dict(result.options), refused=list(result.refused)[:500],
                screenshots=_relative_screenshots(work.apply_root, result.screenshots),
            )
            plan_hash = result.plan_hash or None
        try:
            if not apply_runs.finish_run(conn, work.run_id, outcome=final, clean=clean, plan_hash=plan_hash, **documents):
                # Closed elsewhere first (a second server on this database, or recover_stale): this result was not stored, so what is
                # reported (health, last outcome) is what the row says, not what this run found.
                LOGGER.warning("An Apply for me run's result was not recorded: its row had already been finished")
                stored = conn.execute("SELECT outcome FROM apply_runs WHERE id=?", (work.run_id,)).fetchone()
                final = str(stored["outcome"] or "failed") if stored is not None else "failed"
        except Exception as exc:  # noqa: BLE001 - recover_stale finishes the row later
            LOGGER.error("An Apply for me run could not record its result (%s)", type(exc).__name__)
            rollback_quietly(conn, LOGGER, "recording an Apply for me run's result")
        return final

    @staticmethod
    def _health(conn: sqlite3.Connection, user_id: str, *, ok: bool, error: str = "", detail: dict[str, Any] | None = None) -> None:
        try:
            automation.record_health(conn, user_id, apply_runs.RUNNER_COMPONENT, ok=ok, error=error, detail=detail)
        except Exception as exc:  # noqa: BLE001 - a health row never stops a run
            LOGGER.warning("The Apply for me runner health row was not written (%s)", type(exc).__name__)
            rollback_quietly(conn, LOGGER, "writing the Apply for me runner's health row")


# What each kind of run may end in. Anything else a child reports is a failure of the child, not a result.
_OUTCOMES_FOR_KIND = {
    "lookup": ("looked_up", "needs_you", "failed"),
    "rehearsal": ("rehearsed", "needs_you", "failed"),
}
assert all(set(items) <= set(OUTCOMES) for items in _OUTCOMES_FOR_KIND.values())


def _relative_screenshots(apply_root: Path, shots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The screenshots with paths relative to the apply folder (posix). One outside it is dropped."""
    root = Path(apply_root).resolve()
    kept: list[dict[str, Any]] = []
    for shot in shots:
        try:
            relative = Path(str(shot.get("path") or "")).resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            continue
        kept.append({"step": str(shot.get("step") or ""), "path": relative, "sha256": str(shot.get("sha256") or ""),
                     "masked": [str(key) for key in (shot.get("masked") or [])]})
    return kept


# --- What the page shows ------------------------------------------------------------------------------------------

_SOURCE_TEXT = {
    "profile": "Your profile", "ats_label": "The option you confirmed", "sensitive": "Your stored answer",
    "resume": "Your confirmed résumé", "cover_letter": "Your approved cover letter", "none": "",
}


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _list_words(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _is_file(entry: Mapping[str, Any]) -> bool:
    source = entry.get("source") if isinstance(entry.get("source"), dict) else {}
    return entry.get("control") == "file" or source.get("kind") in ("resume", "cover_letter") or bool(entry.get("file_sha256"))


def _disposition_text(
    entry: Mapping[str, Any], done: frozenset[str] = frozenset(), checked: frozenset[str] = frozenset(), *,
    failed: frozenset[str] = frozenset(), absent: frozenset[str] = frozenset(), ended: bool = False,
) -> str:
    """What the rehearsal did with a field. ``done`` is the keys the agent reports it filled and read back (or attached and checked),
    ``checked`` the deferred keys it compared with the form and ``failed`` those whose comparison found the form does not offer the answer
    (or could not be read), ``absent`` the keys the form turned out not to draw, and ``ended`` whether the rehearsal ran to its end: the
    plan alone says what was meant to happen, never what did."""
    disposition = str(entry.get("disposition") or "")
    key = str(entry.get("key") or "")
    if disposition == "fill":
        if key in done:
            return "Filled in the rehearsal"
        if _is_file(entry):
            return "Not attached in this rehearsal"
        if ended and key in absent:
            return "Not filled: the form has no field for this"
        return "Not confirmed: the rehearsal stopped before this was filled and read back"
    if disposition == "deferred":
        if _is_file(entry):
            return "Not attached: this board uploads files as soon as they are attached"
        if key not in checked:
            return "Not checked: the rehearsal stopped before this was compared with the form"
        if key in failed:
            return "Checked against the form: the app could not confirm it offers this answer, so it would not be filled"
        if entry.get("sensitive"):
            return "Checked against the form; filled only when you submit"
        return "Checked against the form"
    if disposition == "left_for_you":
        return "Left for you"
    return "Left blank"


def _source_text(entry: Mapping[str, Any]) -> str:
    source = entry.get("source") if isinstance(entry.get("source"), dict) else {}
    kind = str(source.get("kind") or "none")
    if kind == "answer":
        company = str(source.get("company") or "")
        return f"Your saved answer for {company}" if company else "Your saved answer"
    return _SOURCE_TEXT.get(kind, "")


def _problem_item(item: Mapping[str, Any], kind: str = "") -> dict[str, Any]:
    return {"key": str(item.get("key") or ""), "question": str(item.get("question") or ""), "message": str(item.get("message") or item.get("problem") or ""),
            "required": bool(item.get("required", True)), "kind": kind or str(item.get("kind") or "")}


def _sentence(reason: str) -> str:
    return reason if reason[-1:] in ".!?" else f"{reason}."


def _summary(
    row: Mapping[str, Any], reasons: list[str], options: Mapping[str, Any], company: str, title: str, progress: list[dict[str, Any]], stalled: bool,
    posting: Mapping[str, Any] | None = None,
) -> str:
    if row["status"] == "running":
        if stalled:
            return "The app stopped during this run"
        return str(progress[-1].get("text") or "") if progress else PROGRESS_STEPS["start"]
    outcome = str(row["outcome"] or "")
    first = reasons[0] if reasons else "The run did not finish"
    if outcome == "looked_up":
        count = sum(len(items) for items in options.values() if isinstance(items, list))
        if not count:
            return "No options came back for what you typed. Try fewer letters, or check the spelling."
        return f"Greenhouse listed {_plural(count, 'option')} for what you typed. Pick the one that is yours."
    if outcome == "rehearsed":
        # The listing the form came from, when the run recorded it: the saved role's own words only when they are the same posting.
        if posting and (posting.get("title") or posting.get("company")):
            company, title = str(posting.get("company") or company), str(posting.get("title") or title)
        return f"Here is what the app would send to {company or 'the company'} for {title or 'this role'}. Your application has not been submitted."
    if outcome == "needs_you":
        what = "lookup" if row["kind"] == "lookup" else "rehearsal"
        return f"The {what} stopped: {first.rstrip('.')}. {NO_SENT}"
    return first if first.endswith(NO_SENT) else f"{_sentence(first)} {NO_SENT}"


def _measured(row: Mapping[str, Any], evidence: Mapping[str, Any], refused: list[Any]) -> str:
    if row["outcome"] != "rehearsed":
        return ""
    total = evidence.get("refused_total")
    count = total if isinstance(total, int) and not isinstance(total, bool) else len(refused)
    text = (
        f"During the rehearsal the app blocked {_plural(count, 'request')} that could have submitted the form or carried a filled-in answer, "
        "to Greenhouse or anywhere else. Sensitive answers were not put in the page; they go in only when you submit."
    )
    asked = [item for item in evidence.get("lookups") or [] if isinstance(item, dict) and (item.get("question") or item.get("key"))]
    # A lookup that sends the typed text is told apart from one that only fetches a whole list (degree, discipline).
    typed = [str(item.get("question") or item.get("key")) for item in asked if item.get("typed", True)]
    listed = [str(item.get("question") or item.get("key")) for item in asked if not item.get("typed", True)]
    if typed:
        text += f" To find the options for {_list_words(typed)}, the app sent the text typed into those fields to Greenhouse's lookup service."
    if listed:
        text += f" For {_list_words(listed)}, the app fetched Greenhouse's whole list; nothing you typed went with it."
    # The closing clause is about what went to a lookup service: with no lookup it has nothing to say "else" to (10.4.1).
    if typed or listed:
        text += " The app saw nothing else you entered leave the browser."
    return text


def reviewable(row: Mapping[str, Any]) -> bool:
    """Whether the student may mark this run right or wrong: a rehearsal that finished with a form to judge or a stop to judge."""
    return bool(row["kind"] == "rehearsal" and row["status"] == "finished" and row["outcome"] in ("rehearsed", "needs_you"))


def orphaned(row: Mapping[str, Any], live: Callable[[str], bool] | None) -> bool:
    """A lookup or rehearsal that says 'running' while this server holds no run of that id: a server stopped mid-run left it.

    Only this server runs lookups and rehearsals, so the row is dead whatever its heartbeat says (a stopped server's last heartbeat
    is fresh for two minutes). ``live`` says whether this process holds the run; None leaves the row to its heartbeat alone.
    """
    if live is None or row["status"] != "running" or row["kind"] not in ("lookup", "rehearsal"):
        return False
    started = parse_app_instant(row["started_at"])
    if started is None or (datetime.now(timezone.utc) - started).total_seconds() < ORPHAN_AFTER_S:
        return False
    return not live(str(row["id"]))


def run_view(conn: sqlite3.Connection, row: Mapping[str, Any], live: Callable[[str], bool] | None = None) -> dict[str, Any]:
    """A stored run as the page shows it (every key always present). Carries no field value: the row has none.

    ``live`` is whether this process is working on a run (see ``orphaned``); the routes pass it.
    """
    opportunity = conn.execute("SELECT title, company FROM opportunities WHERE id=?", (row["opportunity_id"],)).fetchone()
    title, company = (str(opportunity["title"] or ""), str(opportunity["company"] or "")) if opportunity else ("", "")
    reasons = [str(item) for item in json_as(row["reasons_json"], [])]
    plan = [item for item in json_as(row["plan_json"], []) if isinstance(item, dict)]
    options = json_as(row["options_json"], {})
    evidence = json_as(row["evidence_json"], {})
    refused = json_as(row["refused_json"], [])
    progress = [
        {"at": str(item.get("at") or ""), "step": str(item.get("step") or ""), "text": str(item.get("text") or "")}
        for item in json_as(row["progress_json"], []) if isinstance(item, dict)
    ]
    beat = parse_app_instant(row["heartbeat_at"])
    stalled = row["status"] == "running" and (beat is None or datetime.now(timezone.utc) - beat > HELD_HEARTBEAT or orphaned(row, live))
    problems = [
        {"key": str(entry.get("key") or ""), "question": str(entry.get("question") or ""), "message": str(entry["problem"]),
         "required": bool(entry.get("required")), "kind": "plan"}
        for entry in sorted((entry for entry in plan if entry.get("problem")), key=lambda entry: not entry.get("required"))
    ]
    # A join problem on a field the plan lists is already in the list above, with the same sentence: it is shown once.
    planned = {str(entry.get("key") or "") for entry in plan if entry.get("problem")}
    problems.extend(
        _problem_item(item) for item in evidence.get("join_problems") or []
        if isinstance(item, dict) and str(item.get("key") or "") not in planned
    )
    problems.extend(_problem_item(item) for item in evidence.get("check_problems") or [] if isinstance(item, dict))
    filled = frozenset(str(key) for key in evidence.get("filled_keys") or [])
    checked = frozenset(str(key) for key in evidence.get("checked_keys") or [])
    failed = frozenset(str(key) for key in evidence.get("deferred_failed_keys") or [])
    absent = frozenset(
        str(item.get("key") or "") for item in evidence.get("check_problems") or [] if isinstance(item, dict) and item.get("kind") == "missing_control"
    )
    ended = row["outcome"] == "rehearsed"
    fields = [
        {"key": str(entry.get("key") or ""), "question": str(entry.get("question") or ""), "required": bool(entry.get("required")),
         "sensitive": bool(entry.get("sensitive")), "disposition": str(entry.get("disposition") or ""),
         "disposition_text": _disposition_text(entry, filled, checked, failed=failed, absent=absent, ended=ended), "source_text": _source_text(entry), "problem": str(entry.get("problem") or "")}
        for entry in plan
    ]
    shots = [
        {"index": index, "step": str(shot.get("step") or ""), "url": f"/api/v1/apply-agent/runs/{row['id']}/screenshots/{index}",
         "masked": [str(key) for key in (shot.get("masked") or [])], "available": bool(shot.get("path"))}
        for index, shot in enumerate(json_as(row["screenshots_json"], [])) if isinstance(shot, dict)
    ]
    lookup = evidence.get("lookup")
    return {
        "id": row["id"], "opportunity_id": row["opportunity_id"], "kind": row["kind"], "status": row["status"],
        "outcome": row["outcome"] or "", "clean": bool(row["clean"]),
        "started_at": row["started_at"], "finished_at": row["finished_at"], "heartbeat_at": row["heartbeat_at"], "deadline_at": row["deadline_at"],
        "stalled": bool(stalled),
        "summary": _summary(
            row, reasons, options if isinstance(options, dict) else {}, company, title, progress, bool(stalled),
            evidence.get("posting") if isinstance(evidence.get("posting"), dict) else None,
        ),
        "measured": _measured(row, evidence, refused if isinstance(refused, list) else []),
        "progress": progress, "reasons": reasons, "problems": problems, "fields": fields,
        "options": options if isinstance(options, dict) else {}, "lookup": lookup if isinstance(lookup, dict) else None,
        "screenshots": shots, "refused_count": len(refused) if isinstance(refused, list) else 0,
        "review": row["review"] or "", "review_note": row["review_note"] or "", "reviewed_at": row["reviewed_at"],
        "can_review": reviewable(row),
        "can_cancel": row["status"] == "running" and not stalled,
    }


def run_views(
    conn: sqlite3.Connection, user_id: str, opportunity_id: str, *, kind: str = "", limit: int = 5, live: Callable[[str], bool] | None = None,
) -> list[dict[str, Any]]:
    """This student's lookups and rehearsals for one role, newest first."""
    kinds = (kind,) if kind in ("lookup", "rehearsal") else ("lookup", "rehearsal")
    marks = ", ".join("?" for _ in kinds)
    rows = conn.execute(
        f"SELECT * FROM apply_runs WHERE user_id=? AND opportunity_id=? AND kind IN ({marks}) ORDER BY started_at DESC LIMIT ?",
        (user_id, opportunity_id, *kinds, max(1, int(limit))),
    ).fetchall()
    return [run_view(conn, dict(row), live) for row in rows]


def screenshot_path(apply_root: Path, user_id: str, row: Mapping[str, Any], index: int) -> Path | None:
    """The file behind one of a run's pictures, or None when there is none, it was purged, or it is not in the student's folder."""
    shots = json_as(row["screenshots_json"], [])
    if not 0 <= index < len(shots) or not isinstance(shots[index], dict):
        return None
    stored = str(shots[index].get("path") or "")
    if not stored:
        return None
    root = (Path(apply_root) / apply_runs.user_folder(user_id)).resolve()
    path = Path(stored)
    try:
        resolved = (path if path.is_absolute() else Path(apply_root) / path).resolve()
        resolved.relative_to(root)
    except (ValueError, OSError):
        return None
    return resolved if resolved.is_file() else None
