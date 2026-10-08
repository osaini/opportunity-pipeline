"""Apply for me, the runner: one run at a time, in a process of its own, with a watchdog that can kill the whole tree.

``ApplyRunner.start`` takes the single slot, reads the posting and the student's data the way the check does (never
from a cache), refuses what may not run, writes the run's row and starts a supervisor thread. The thread asks
``supervise`` to start the agent in a child process (multiprocessing ``spawn``, its own session on POSIX) and then pumps
the pipe between them: progress and heartbeats go to the row, a replan request is answered with a plan built here (a
``policy.Sources`` holds a database connection, so planning never crosses the pipe), and the one terminal message ends
the run. A lookup or a rehearsal never submits, so its hand-over is always refused.

Finish in browser (kind ``handoff``) is the same machinery with a claim. ``start`` takes the claim first (the claim is
the lock, ``runs.claim``), and only then makes the run's row, so a refused start leaves nothing behind. The agent fills
the form, sends "ready" and waits for the student, who presses Submit application in the window themselves. The press
is a POST the agent's route handler holds until the parent has committed the hand-over (``runs.hand_over``, the claim
becomes ``clicking``) and answered: a refused, late or lost answer aborts the POST. The pump answers the emailed security
code through a worker thread of its own (Gmail can be slow, and the pump must keep heartbeating), and only the pump
thread ever sends on the pipe to the child.

The watchdog is the point of the process boundary. At the deadline, on a stop request that is not heard within the
grace, and when the server shuts down, ``kill_tree`` ends the child and every process it started (Chromium and its
helpers): the descendants are listed first, then on Windows each one by pid (never ``taskkill /T``, which walks by parent pid with no identity check), or on POSIX each descendant, the child
itself and its process group, because Playwright starts Chromium in a session of its own. For a handoff the browser is also
confirmed gone by pid (``supervise`` snapshots the child's descendants while it runs and again before it kills, kills every
survivor, and re-checks each with ``process_alive``) before the claim may be settled as "nothing was sent" (invariant I3). A
fake agent that opens no browser may run in a thread instead (``isolation = "thread"``); a thread cannot be killed, so at the
deadline it is asked to stop and abandoned.

Settling a handoff is one pure function, ``handoff_settlement``: what the claim row says (read after the child ended),
what came back, and whether the window is confirmed closed give the claim's state, its after_click and its note. After
the hand-over nothing is ever called "not sent" except from the route's own record that the POST was aborted.

Everything a run leaves behind is value-free (apply/agent_types.py): sentences, public page text, hashes, request facts.
An exception from the child is reported by its type name, never its message.

The run views at the bottom turn a stored row into what the page shows (spec 10.4): the summary sentence, the measured
sentence, the problems, a value-free table of the fields, and the pictures.
"""

from __future__ import annotations

import inspect
import json
import logging
import multiprocessing
import os
import re
import signal
import sqlite3
import subprocess
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing, contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence
from uuid import uuid4

from pipeline_core.identity import employer_key

from . import (
    checks as apply_checks, claims as apply_claims, greenhouse as apply_greenhouse, policy as apply_policy, preflight as apply_preflight,
    runs as apply_runs, security_code as apply_security_code, watch as apply_watch,
)
from .agent_types import (
    HANDOFF_NOT_SUBMITTED, ISOLATIONS, MODE_FOR_KIND, OP_CANCEL, OP_ERROR, OP_FILE_CHECK, OP_FILE_CHECK_REPLY, OP_FRONT, OP_HAND_OVER, OP_HAND_OVER_REPLY, OP_HANDOFF_READY,
    OP_HEARTBEAT, OP_PROGRESS, OP_REPLAN, OP_REPLAN_REPLY, OP_RESULT, OP_SECURITY_CODE, OP_SECURITY_CODE_REPLY, OP_SECURITY_CODE_RESULT,
    OUTCOMES, PROGRESS_STEPS, STOPPED, WINDOW_CLOSED, WINDOW_UNCONFIRMED, YOUR_TURN, YOUR_TURN_NONE_LEFT, AgentJob, ApplyTimeouts,
    FilePayload, LookupRequest, RunResult,
)
from .checks import UNCONFIRMED_NOTE
from .claims import HELD_HEARTBEAT
from .classify import STATEMENT_CATEGORIES
from .runner_child import child_main
from ..applications.extension import confirmed_resume_file
from ..automation import ledger as automation
from ..core.database import connect_product, rollback_quietly
from ..core.json_values import json_as
from ..core.timestamps import parse_app_instant, utc_now
from ..student import artifacts as document_artifacts

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
# How long the runner keeps re-checking the processes it killed before it says one is still running.
VERIFY_S = 5.0
# How soon after a hand-over request the parent must have committed it, below the child's own wait (it gave up at reply_s).
HAND_OVER_MARGIN_S = 2.0
# The cap on every agent wait is the watchdog's deadline less this, so the agent ends its waits before the watchdog fires.
ENDS_AT_MARGIN_S = 60.0

BUSY = "Another application is being filled. Wait for it to finish."
NO_SENT = "No application was sent."
TOO_LONG = "The run took longer than {minutes} minutes, so the app stopped it. No application was sent."
CHILD_DIED = "The browser stopped before the run finished. No application was sent."
SERVER_STOPPED = "The app stopped during this run. No application was sent."
NOT_STARTED = "The app could not start this run. No application was sent."
NO_LOOKUP_FIELD = "That field has no list of options to look up"
NO_RUN = "No run with that id"
FINISHED_ALREADY = "This run has already finished."
NOT_RUNNING = "This run is not running in this app"
ACCOUNT_ENDING = "This account is being deleted, so nothing can be started"
NO_PICTURE = "This picture is no longer kept"
NOT_REVIEWABLE = "Only a rehearsal that finished or stopped with something for you can be marked"
HANDED_OVER = "You already pressed Submit application in the window, so it can't be stopped now."
NOT_HANDOFF = "This run has no window to bring forward"
POSTING = "Check the posting first. {difference}"
START_FAILED = "The browser could not start, so nothing was sent. Try again."

STOP_DEADLINE, STOP_CANCELLED, STOP_CHILD_DIED, STOP_ERROR = "deadline", "cancelled", "child_died", "error"


def deadline_for(kind: str, timeouts: ApplyTimeouts = ApplyTimeouts()) -> float:
    """Seconds a run of this kind may take before the watchdog ends it.

    A handoff counts the fill, the student's turn, the one shared budget every wait after the press draws from (the
    reader's window and the student's own code time), three outcome windows and two minutes: 300 + 1200 + 1200 + 90 + 120
    = 2910 seconds (48.5 minutes) with the defaults. The agent's waits end 60 seconds before that, so the watchdog is a
    backstop for a hang, not the way a normal run ends.
    """
    if kind in DEADLINES:
        return DEADLINES[kind]
    if kind == "submit":
        return 300.0 + timeouts.person_s + timeouts.security_code_s
    if kind == "handoff":
        return timeouts.fill_s + timeouts.handoff_s + timeouts.after_hand_over_s + 3 * timeouts.outcome_s + 120.0
    raise ValueError(f"Unsupported run kind: {kind}")


class RunnerBusy(Exception):
    """The one slot is taken."""

    def __init__(self) -> None:
        super().__init__(BUSY)


class RunRefused(Exception):
    """A run that may not start, with the HTTP status the route answers and the sentence for the student.

    ``code`` and ``ask`` come from a refused claim (apply_runs.ClaimRefused): ``ask`` means a tick from the student
    (``code``) would allow it, and ``code`` is "posting" when the posting differs from the saved role.
    """

    def __init__(self, status_code: int, message: str, *, code: str = "", ask: bool = False) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code
        self.ask = ask


# --- Killing a tree ----------------------------------------------------------------------------------------------


def _windows_process_table() -> dict[int, int] | None:
    """{pid: parent pid} of every process, from the toolhelp snapshot (standard library only). None when it cannot be read."""
    try:
        import ctypes
        from ctypes import wintypes

        class ProcessEntry(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ProcessEntry)]
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ProcessEntry)]
        kernel32.Process32NextW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)   # TH32CS_SNAPPROCESS
        invalid = (1 << (8 * ctypes.sizeof(ctypes.c_void_p))) - 1
        if snapshot in (None, 0, invalid):
            return None
        try:
            entry = ProcessEntry()
            entry.dwSize = ctypes.sizeof(ProcessEntry)
            table: dict[int, int] = {}
            more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while more:
                table[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
                more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
            return table or None
        finally:
            kernel32.CloseHandle(snapshot)
    except Exception:  # noqa: BLE001 - no ctypes, no kernel32, or a refused call: the caller treats the table as unknown
        return None


def _parse_table(listing: str) -> dict[int, int] | None:
    table: dict[int, int] = {}
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = int(parts[1])
    return table or None


def _process_table() -> dict[int, int] | None:
    """{pid: parent pid} of every running process, or None when unreadable: ``ps`` on POSIX, a Toolhelp snapshot on Windows
    (milliseconds; a PowerShell CIM query takes over a second, so it is only the bounded fallback when the snapshot fails)."""
    try:
        if os.name == "nt":
            try:
                table = _windows_process_table()
            except (OSError, AttributeError, ValueError, ImportError):
                table = None
            if table:
                return table
            # Bounded: the listing only names pids for the caller, and taskkill walks the tree itself.
            return _parse_table(subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 "Get-CimInstance Win32_Process | ForEach-Object { '{0} {1}' -f $_.ProcessId, $_.ParentProcessId }"],
                capture_output=True, text=True, timeout=CIM_TIMEOUT_S, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout)
        return _parse_table(subprocess.run(["ps", "-A", "-o", "pid=", "-o", "ppid="], capture_output=True, text=True, timeout=20).stdout)
    except (OSError, subprocess.SubprocessError):
        return None


def _descendants_of(
    table: Mapping[int, int], pid: int, *, started: Callable[[int], str | None] | None = None, known: Mapping[int, str | None] | None = None,
) -> list[int]:
    """Every process below ``pid`` in a {pid: parent pid} table, children listed first.

    ``started`` (Windows) returns a process's creation time as an opaque string that sorts by time, and turns the walk into a check
    of identity as well as of the parent pid: the toolhelp snapshot never updates a parent pid when the parent exits and Windows
    hands the freed pid to another process, so an old, unrelated process (and all that is below it) can name the child's pid as its
    parent. A child counts only if it was created at or after its parent, and at or after ``pid`` itself. ``known`` holds creation
    times recorded while the processes were still running, used for a parent that has exited since (a driver that died and left its
    browser). A process whose creation time cannot be read is not listed: it is gone, or it is not one of ours.
    """
    children: dict[int, list[int]] = {}
    for child, parent in table.items():
        children.setdefault(parent, []).append(child)
    recorded = known or {}

    def born(process: int) -> str | None:
        return recorded.get(process) or (started(process) if started is not None else None)

    root = born(pid) if started is not None else None
    found: list[int] = []
    queue = [pid]
    while queue:
        parent = queue.pop(0)
        floor = (born(parent) or root) if started is not None else None
        for child in children.get(parent, []):
            if child in found or child == pid:
                continue
            if started is not None:
                created = born(child)
                if created is None or (floor is not None and created < floor) or (root is not None and created < root):
                    continue        # older than its parent: the name of a freed pid in a stale table, not a child of it
            found.append(child)
            queue.append(child)
    return found


def _start_check() -> Callable[[int], str | None] | None:
    """The creation-time reader for the process walk: Windows only, where a stale parent pid is possible (a POSIX orphan is reparented)."""
    return process_start if os.name == "nt" else None


def _group_members(pid: int) -> list[int]:
    """POSIX: every process in the process group ``pid`` leads, ``pid`` itself left out. [] on Windows or when ``ps`` cannot say.

    The child calls setsid, so what it started shares its group, and an orphan keeps its group when it is reparented to init: a
    process that the child started a moment before it died is still found here, where the parent-pid walk no longer finds it.
    """
    if os.name == "nt" or pid <= 1:
        return []
    try:
        if pid == os.getpgrp():
            return []        # the child has not left the server's own group: that group is not the child's
        listing = subprocess.run(["ps", "-A", "-o", "pid=", "-o", "pgid="], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    members: list[int] = []
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit() and int(parts[1]) == pid and int(parts[0]) != pid:
            members.append(int(parts[0]))
    return members


def _tree_below(table: Mapping[int, int], pid: int, known: Mapping[int, str | None] | None = None) -> list[int]:
    """Everything that belongs to the child ``pid``: below it by parent pid (with the identity check on Windows), and in its group."""
    found = _descendants_of(table, pid, started=_start_check(), known=known)
    for member in _group_members(pid):
        if member not in found:
            found.append(member)
    return found


def descendants(pid: int) -> list[int]:
    """Every process below ``pid``, children first listed first. [] when there are none or the process list cannot be read."""
    table = _process_table()
    return _tree_below(table, pid) if table else []


def _kill_pid(pid: int) -> None:
    """End one process by its pid, whatever its parent. Never raises."""
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            LOGGER.warning("taskkill did not answer for a run's browser process")
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def kill_tree(pid: int) -> list[int]:
    """End ``pid`` and everything it started, and return the pids targeted (so a caller can check each is gone). Never raises.

    On Windows no ``taskkill /T`` is used: it walks the tree by parent pid alone, and a freed pid that an unrelated older process names
    as its parent would be killed with it. Every pid of the identity-checked listing (its creation
    time is no older than its parent's) is killed by pid, the child last; what a later look finds is left to ``_kill_survivors``.
    """
    below = descendants(pid)
    if os.name == "nt":
        for target in (*below, pid):
            _kill_pid(target)
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


def _windows_process(pid: int) -> tuple[bool, str | None, bool]:
    """(the process exists, its creation time as hex or None, whether it is still running) from OpenProcess.

    "Exists" is False only when Windows says there is no such process; any other failure to look is "exists, unknown", which
    callers count as running (a check that cannot be made never says a browser is gone).
    """
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            error = ctypes.get_last_error()
            return (False, None, False) if error == 87 else (True, None, True)   # ERROR_INVALID_PARAMETER: no such process
        try:
            code = wintypes.DWORD()
            running = True
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                running = code.value == 259   # STILL_ACTIVE
            created, ended, kernel, user = (wintypes.FILETIME() for _ in range(4))
            started = None
            if kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(ended), ctypes.byref(kernel), ctypes.byref(user)):
                started = f"{created.dwHighDateTime:08x}{created.dwLowDateTime:08x}"
            return True, started, running
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001 - no ctypes or kernel32: unknown, which counts as running
        return True, None, True


def process_access_denied(pid: int) -> bool:
    """Windows only: whether Windows refuses this program a look at the process (ERROR_ACCESS_DENIED). False everywhere else.

    A process of ours runs as the same user, so its creation time can always be read. A pid that is running and that Windows will not
    let us look at is another program's (a system process that was handed the pid a Chromium process freed).
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(0x1000, False, int(pid))   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5   # ERROR_ACCESS_DENIED
        kernel32.CloseHandle(handle)
        return False
    except Exception:  # noqa: BLE001 - a check that cannot be made is not a refusal
        return False


def process_start(pid: int) -> str | None:
    """When this process started, as an opaque string that differs for a different process that is given the same pid. None if unreadable.

    Taken when a process is first seen and compared before it is killed or counted, so a pid that was freed and handed to an
    unrelated program is never taken for the browser that had it (I3's check and the kill are about our processes only).
    """
    if os.name == "nt":
        return _windows_process(pid)[1]
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as stat:
            fields = stat.read().rsplit(")", 1)[-1].split()
            return fields[19] if len(fields) > 19 else None   # starttime, in clock ticks since boot
    except (OSError, IndexError):
        pass
    try:
        listing = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return listing or None


def process_alive(pid: int) -> bool:
    """Whether a process with this pid is running (a zombie is not). A check that cannot be made says True."""
    if os.name == "nt":
        exists, _started, running = _windows_process(pid)
        return exists and running
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as stat:
            return stat.read().rsplit(")", 1)[-1].split()[0] != "Z"
    except FileNotFoundError:
        pass       # no /proc (macOS): ask ps, which shows an unreaped child as Z
    except (OSError, IndexError):
        return True
    try:
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return True
    return bool(state) and not state.startswith("Z")


# --- Supervising one child ---------------------------------------------------------------------------------------


@dataclass
class SupervisorHandlers:
    progress: Callable[[str, str], None] = lambda step, text: None
    heartbeat: Callable[[], None] = lambda: None
    replan: Callable[[list[dict[str, Any]], bool], Any] | None = None
    # The child's hand-over request. A handler that takes a ``deadline`` keyword is given the time.monotonic() instant after
    # which a commit is too late (the child has given up waiting); one that does not is called without it.
    hand_over: Callable[..., bool] = lambda: False
    # Called every HEARTBEAT_EVERY_S while the child lives, so a quiet agent does not look dead.
    tick: Callable[[], None] = lambda: None
    # (key, source ref, SHA-256 of the text) of a cover letter the agent is about to attach: True only when it is still the latest
    # version, still approved and unchanged (D11). Reads the database; no network.
    check_file: Callable[[str, str, str], bool] | None = None
    # Finish in browser. None of these may block on a network call: they run on the one thread that pumps the pipe.
    handoff_ready: Callable[[dict[str, Any]], None] = lambda message: None
    security_code: Callable[[int], None] = lambda ident: None          # starts the off-thread answer to ask ``ident``
    security_code_result: Callable[[dict[str, Any]], None] = lambda message: None
    # Replies that are ready to go to the child (each {"op": "security_code_reply", "id", "status", ...}); taken once. Never logged.
    code_replies: Callable[[], list[dict[str, Any]]] = lambda: []


@dataclass
class Supervised:
    result: RunResult | None
    stop: str = ""          # "" | "deadline" | "cancelled" | "child_died" | "error"
    error: str = ""         # the child's exception type name
    killed_pids: list[int] = field(default_factory=list)
    # Process isolation: every process seen below the child while it ran ({pid: parent pid}), whether the browser may have
    # existed (the child said anything past starting), and whether every one of them is confirmed gone. A thread has no pids,
    # so its closed_confirmed is True.
    pids: dict[int, int] = field(default_factory=dict)
    # When each of those processes (and the child itself) started, as ``process_start`` read it the moment it was seen: the pid alone
    # is not an identity, since a freed pid is handed to an unrelated program. None when it could not be read.
    started: dict[int, str | None] = field(default_factory=dict)
    saw_activity: bool = False
    snapshot_failed: bool = False
    closed_confirmed: bool = True


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


def _call_hand_over(handler: Callable[..., bool], deadline: float) -> bool:
    try:
        parameters = inspect.signature(handler).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "deadline" in parameters or any(item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values()):
        return bool(handler(deadline=deadline))
    return bool(handler())


@dataclass
class _Pump:
    """What ``_dispatch`` needs besides the message: where the child runs and what has been seen of it."""

    worker: Any
    in_process: bool
    reply_s: float
    outcome: Supervised
    last_snapshot: float = 0.0
    abort: threading.Event | None = None     # set when the server is shutting down or the account is being deleted: nothing is committed


def _snapshot(pump: _Pump, *, force: bool = False) -> None:
    """Remember the processes below the child now, so a browser that outlives its driver can still be found by pid."""
    if not pump.in_process or not pump.worker.pid:
        return
    now = time.monotonic()
    if not force and now - pump.last_snapshot < 2.0:
        return
    pump.last_snapshot = now
    table = _process_table()
    if table is None:
        pump.outcome.snapshot_failed = True
        return
    pump.outcome.snapshot_failed = False
    _remember(pump.outcome, table, _tree_below(table, pump.worker.pid, pump.outcome.started))


def _remember(outcome: Supervised, table: Mapping[int, int], found: Sequence[int]) -> None:
    """Record the processes just seen below the child, each with its parent and when it started. Nothing is ever dropped here."""
    for pid in found:
        outcome.pids[pid] = table.get(pid, 0)
        started = process_start(pid)
        if started is not None or pid not in outcome.started:
            outcome.started[pid] = started    # a process below the child now is one of ours, whatever held that pid before


def _dispatch(message: Any, handlers: SupervisorHandlers, inbox: Any, pump: _Pump | None = None) -> tuple[bool, RunResult | None, str]:
    """Act on one message from the child. (done, result, error type name): done is True for the terminal message."""
    if not isinstance(message, dict):
        return False, None, ""
    op = message.get("op")
    if pump is not None and op not in (OP_RESULT, OP_ERROR):
        pump.outcome.saw_activity = True
    if op == OP_PROGRESS:
        if pump is not None:
            _snapshot(pump)
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
    elif op == OP_FILE_CHECK:
        try:
            current = handlers.check_file is not None and handlers.check_file(
                str(message.get("key") or ""), str(message.get("ref") or ""), str(message.get("sha256") or "")) is True
        except Exception:  # noqa: BLE001 - when unsure, the document is not attached
            current = False
        _answer(inbox, {"op": OP_FILE_CHECK_REPLY, "id": message.get("id"), "ok": current})
    elif op == OP_HAND_OVER and pump is not None and pump.abort is not None and pump.abort.is_set():
        # The run is about to be killed (the supervise loop only looks at ``abort`` at the top of an iteration): the student's Submit is
        # not committed, so the child aborts the POST instead of continuing one the next iteration would cut off.
        _answer(inbox, {"op": OP_HAND_OVER_REPLY, "id": message.get("id"), "ok": False})
    elif op == OP_HAND_OVER:
        received = time.monotonic()
        reply_s = pump.reply_s if pump is not None else ApplyTimeouts().reply_s
        deadline = received + reply_s - HAND_OVER_MARGIN_S
        expires = message.get("expires")
        if isinstance(expires, (int, float)) and not isinstance(expires, bool):
            # The child stamped when it stops waiting; a commit after that would leave the claim handed over with the POST aborted.
            deadline = min(deadline, float(expires) - HAND_OVER_MARGIN_S)
        try:
            granted = _call_hand_over(handlers.hand_over, deadline)
        except Exception:  # noqa: BLE001
            granted = False
        # Sent only now, after the handler returned: the hand-over is committed before the child may continue the POST (I1).
        _answer(inbox, {"op": OP_HAND_OVER_REPLY, "id": message.get("id"), "ok": granted})
    elif op == OP_HANDOFF_READY:
        if pump is not None:
            _snapshot(pump, force=True)
        _guard(lambda: handlers.handoff_ready(message))
    elif op == OP_SECURITY_CODE:
        _guard(lambda: handlers.security_code(int(message.get("id") or 0)))
    elif op == OP_SECURITY_CODE_RESULT:
        _guard(lambda: handlers.security_code_result(message))
    elif op == OP_RESULT:
        result = message.get("result")
        return True, result if isinstance(result, RunResult) else None, "" if isinstance(result, RunResult) else "BadResult"
    elif op == OP_ERROR:
        return True, None, str(message.get("error") or "Error")
    return False, None, ""


def _drain_terminal(outbox: Any, handlers: SupervisorHandlers, inbox: Any, pump: _Pump | None = None) -> tuple[RunResult | None, str] | None:
    """Read what is already in the pipe, without waiting. (result, error) for the terminal message if it is there, else None.

    Called only when the run is being stopped (the deadline, or the server shutting down): a hand-over asked for now is refused
    without asking the handler, so a run that is about to be killed never commits the student's Submit.
    """
    try:
        while outbox.poll(0):
            message = outbox.recv()
            if isinstance(message, dict) and message.get("op") == OP_HAND_OVER:
                if pump is not None:
                    pump.outcome.saw_activity = True
                _answer(inbox, {"op": OP_HAND_OVER_REPLY, "id": message.get("id"), "ok": False})
                continue
            done, result, error = _dispatch(message, handlers, inbox, pump)
            if done:
                return result, error
    except (EOFError, OSError):
        return None
    return None


def supervise(
    factory: Any, job: AgentJob, *, deadline_s: float, handlers: SupervisorHandlers, cancel: threading.Event,
    cancel_grace_s: float = CANCEL_GRACE_S, poll_s: float = 0.25, abort: threading.Event | None = None,
    tick_s: float = HEARTBEAT_EVERY_S, front: Callable[[], int] | None = None,
) -> Supervised:
    """Run one job in a child (a spawned process, or a thread for a fake) and pump its pipe until it ends or is stopped.

    ``cancel`` asks the agent to stop (``OP_CANCEL``) and gives it ``cancel_grace_s`` before the tree is killed;
    ``abort`` kills at once (the server is shutting down). At ``deadline_s`` from now the tree is killed. ``front`` returns
    how many times the window has been asked to come forward; each new count sends one ``OP_FRONT`` (from here: this is
    the only thread that sends to the child).
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
    if in_process and worker.pid:
        outcome.started[worker.pid] = process_start(worker.pid)
    pump = _Pump(worker=worker, in_process=in_process, reply_s=job.timeouts.reply_s, outcome=outcome, abort=abort)
    fronts_sent = 0
    try:
        while True:
            now = time.monotonic()
            stopping = abort is not None and abort.is_set()
            if stopping or now >= deadline_at:
                # A terminal message that is already waiting is a result the child finished in time (a rehearsal that ended as the server
                # was stopped, or as the deadline came): keep it.
                late = _drain_terminal(outbox_recv, handlers, inbox_send, pump)
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
            # Answers the off-thread security-code reader has ready, and the student's request to bring the window forward.
            for reply in _guard(handlers.code_replies) or []:
                _answer(inbox_send, reply)
            if front is not None:
                asked = _guard(front) or 0
                if isinstance(asked, int) and asked > fronts_sent:
                    _answer(inbox_send, {"op": OP_FRONT})
                    fronts_sent += 1
            try:
                arrived = outbox_recv.poll(min(poll_s, max(0.0, deadline_at - now)))
                message = outbox_recv.recv() if arrived else None
            except (EOFError, OSError):
                outcome.stop = STOP_CHILD_DIED
                break
            if arrived:
                done, result, error = _dispatch(message, handlers, inbox_send, pump)
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
            _end_child(worker, outcome)
        else:
            if not outcome.stop:
                worker.join(EXIT_GRACE_S)
        for end in (inbox_send, outbox_recv):
            try:
                end.close()
            except OSError:
                pass
    return outcome


def _end_child(worker: Any, outcome: Supervised) -> None:
    """Make sure a process child and everything it started is gone, and say whether that is confirmed (I3).

    A child that sent its result gets a moment to leave first. Then, before anything is killed, the processes below the
    child are listed once more (also when the child already exited: its browser may have outlived it, and on Windows the
    parent pid of an orphan is still the child's), every one is killed by pid, and each is re-checked with
    ``process_alive`` for a few seconds. ``outcome.closed_confirmed`` is False when one is still running, or when the
    list of processes could not be read after the child had done anything that may have opened a window.
    """
    if not outcome.stop or outcome.stop == STOP_ERROR:
        worker.join(EXIT_GRACE_S)
    pid = worker.pid
    table = _process_table() if pid else None
    if pid and table is None:
        outcome.snapshot_failed = True
    elif table is not None:
        outcome.snapshot_failed = False
        _remember(outcome, table, _tree_below(table, pid, outcome.started))
    killed: list[int] = []
    if worker.is_alive() and pid:
        killed = kill_tree(pid)
    elif pid and os.name != "nt" and any(item != pid for item in outcome.pids):
        # The child is gone but processes it started are not (a driver that died with a browser or a helper running): its group still
        # has members, so the group id cannot have been handed to anything else, and one signal takes whatever started since the look.
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    worker.join(10)
    foreign: list[int] = []
    survivors = _kill_survivors(outcome.pids, own=pid, started=outcome.started, foreign=foreign)
    outcome.killed_pids = [item for item in dict.fromkeys([*killed, *outcome.pids]) if item not in foreign]
    outcome.closed_confirmed = not survivors and not (outcome.snapshot_failed and outcome.saw_activity)


def _kill_survivors(
    pids: Mapping[int, int], *, own: int | None, started: Mapping[int, str | None] | None = None, foreign: list[int] | None = None,
) -> list[int]:
    """Kill, by pid, every process of ``pids`` (and the child ``own``) that is still running; the pids still running after VERIFY_S.

    A pid is ours only while the process holding it started when the one first seen there did (``started``, from
    ``process_start``). One that started at another time is an unrelated program that was handed a freed pid: it is never killed,
    never counted as a survivor, and named in ``foreign``. Where no start time was recorded, a pid whose recorded parent is still
    running and is no longer its parent's child is treated the same way.
    """
    known = dict(started or {})
    targets = [pid for pid in dict.fromkeys([*([own] if own else []), *pids])]
    end = time.monotonic() + VERIFY_S
    alive: list[int] = []
    first = True

    def ours(pid: int) -> bool:
        recorded = known.get(pid)
        if not recorded:
            return True
        current = process_start(pid)
        if current is None:
            # Unreadable now. Our own processes can always be read, so one that Windows refuses to show is another program's (a freed pid
            # handed to a system process): not ours. Unreadable for any other reason is not shown to be someone else, so still counted.
            return not process_access_denied(pid)
        return current == recorded

    while True:
        alive = []
        for pid in list(targets):
            if not process_alive(pid):
                continue
            if ours(pid):
                alive.append(pid)
            else:
                targets.remove(pid)
                if foreign is not None:
                    foreign.append(pid)
        # The budget ends the killing, never the checking: a machine so busy that one pass took longer than VERIFY_S still looks
        # again after the kill before it says a process is still running.
        if not alive or (not first and time.monotonic() >= end):
            break
        table = _process_table() if first else None
        for pid in alive:
            parent = pids.get(pid)
            if first and not known.get(pid) and table is not None and parent and table.get(pid, parent) != parent and process_alive(parent):
                targets.remove(pid)   # reused by something else while its parent lives: not ours
                if foreign is not None:
                    foreign.append(pid)
                continue
            _kill_pid(pid)
        first = False
        alive = [pid for pid in alive if pid in targets]
        if not alive:
            break
        time.sleep(0.2)
    return [pid for pid in alive if pid in targets]


# --- The runner ---------------------------------------------------------------------------------------------------


@dataclass
class _Active:
    run_id: str = ""
    user_id: str = ""
    cancel: threading.Event = field(default_factory=threading.Event)
    abort: threading.Event = field(default_factory=threading.Event)
    shutting_down: bool = False
    # How many times the student asked for the window to come forward. The pump thread, the only one that sends to the
    # child, turns each new count into one OP_FRONT.
    front: int = 0


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
    token: str = ""            # a handoff's claim
    # The parent's own record that it handed the claim over (or tried to and cannot say), set by the hand-over callback on the supervisor
    # thread. Read at settle when the claim itself cannot be read and no result came back.
    handed_over_seen: bool = False
    timeouts: ApplyTimeouts = field(default_factory=ApplyTimeouts)


def _stamp() -> str:
    return utc_now()


def _too_long(deadline_s: float) -> str:
    minutes = round(deadline_s / 60)
    return "The run took longer than a minute, so the app stopped it. No application was sent." if minutes <= 1 else TOO_LONG.format(minutes=minutes)


class _CodeAnswers:
    """The off-thread answers to the child's security-code asks (D10 B), so a slow Gmail never holds the pump.

    One answer is in flight at a time. An ask that arrives while one is running is remembered and answered with that
    answer's result. The pump takes the finished replies each turn (``ready``) and sends them; a reply carries the code
    only when it was found, and is built, sent and dropped: nothing here is logged or kept after that.
    """

    def __init__(self, reader: Any, database_target: Any, user_id: str, token: str) -> None:
        self._reader = reader
        self._target = database_target
        self._user_id = user_id
        self._token = token
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="apply-security-code")
        self._lock = threading.Lock()
        self._future: Future[Any] | None = None
        self._waiting: list[int] = []

    def ask(self, ident: int) -> None:
        with self._lock:
            self._waiting.append(ident)
            if self._future is None:
                self._future = self._executor.submit(self._answer)

    def _answer(self) -> Any:
        # Its own connection, opened here; the arguments are this run's student and this run's own claim, never a token the child names.
        with closing(connect_product(self._target)) as conn:
            return self._reader.answer(conn, user_id=self._user_id, token=self._token)

    def ready(self) -> list[dict[str, Any]]:
        with self._lock:
            future = self._future
            if future is None or not future.done() or not self._waiting:
                return []
            self._future = None
            waiting, self._waiting = self._waiting, []
        try:
            message: dict[str, Any] = dict(future.result().message())
        except Exception as exc:  # noqa: BLE001 - the child is told "fallback", never why
            LOGGER.warning("The security-code reader failed (%s)", type(exc).__name__)
            message = {"status": "fallback", "reason": "not_current"}
        return [{**message, "op": OP_SECURITY_CODE_REPLY, "id": ident} for ident in waiting]

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
        with self._lock:
            running, self._future, self._waiting = self._future, None, []
        if running is not None and not running.done():
            # A lookup that is still reading Gmail when the run ends finishes later; whatever it hands out is forgotten then.
            running.add_done_callback(lambda _done: self._reader.forget(self._token))


class ApplyRunner:
    """The single slot. One per app, so in the real server (one app per process) it is the process-wide slot of the spec."""

    def __init__(
        self, *, deadlines: Mapping[str, float] | None = None, cancel_grace_s: float = CANCEL_GRACE_S,
        timeouts: ApplyTimeouts | None = None, security_code_reader: Any = None,
    ) -> None:
        self._timeouts = timeouts or ApplyTimeouts()
        self._deadlines = dict(DEADLINES)
        self._deadlines.update(deadlines or {})
        self._cancel_grace_s = cancel_grace_s
        self._reader = security_code_reader if security_code_reader is not None else apply_security_code.READER
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
        return float(self._deadlines.get(kind) or deadline_for(kind, self._timeouts))

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

    def front(self, run_id: str) -> bool:
        """Ask for the run's window to come to the front. False when this runner is not running it.

        Only counts the request: the supervisor thread is the one that sends to the child.
        """
        with self._lock:
            active = self._active
            if active is None or active.run_id != run_id:
                return False
            active.front += 1
            return True

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
        acknowledged: Sequence[str] = (), posting_confirmed: bool = False, now: datetime | None = None,
    ) -> str:
        """Check the role, write the run's row and start the supervisor. Returns the run id; the run goes on in a thread.

        A rehearsal or a Finish in browser run of a posting that does not look like the saved role (``posting.differs``) is
        refused until the student has said it is the right one (``posting_confirmed``): the run is filed under the saved
        role's company.

        Raises RunnerBusy, RunRefused (the sentence and status for the route) and OpportunityNotFoundError. A refusal
        writes nothing: no run, and for Finish in browser no claim, application or event.
        """
        if kind not in ("lookup", "rehearsal", "handoff"):
            raise ValueError(f"Unsupported run kind: {kind}")
        active = self._reserve(user_id)
        run_id = ""
        token = ""
        try:
            handoff = kind == "handoff"
            inputs = apply_preflight.run_inputs(
                conn, user_id, opportunity_id, client=schema_client, mode="handoff" if handoff else "rehearse",
                resume_root=resume_root, apply_root=apply_root, now=now,
            )
            result = inputs.result
            if inputs.plan is None or inputs.schema is None or result["status"] in ("unavailable", "failed"):
                raise RunRefused(409, str(result.get("message") or apply_preflight.NOT_GREENHOUSE))
            posting = result.get("posting") if isinstance(result.get("posting"), dict) else {}
            if kind in ("rehearsal", "handoff") and posting.get("differs") and not posting_confirmed:
                # Source integrity: a form for another posting or employer is not filled for the student without their word. Finish in
                # browser's page answers with a confirmation box (the code); a rehearsal's says the sentence.
                raise RunRefused(409, POSTING.format(difference=str(posting.get("difference") or "")).strip(), code="posting" if handoff else "")
            if not handoff:
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
            page_url = str(result["canonical_url"])
            deadline = self.deadline_s(kind)
            screenshot_dir = ""
            if kind != "lookup":
                # Made before the claim and the row, so a full or read-only apply folder refuses the start instead of leaving either behind.
                folder = Path(apply_root) / apply_runs.user_folder(user_id) / apply_runs.opportunity_folder(opportunity_id)
                folder.mkdir(parents=True, exist_ok=True)
                screenshot_dir = str(folder.resolve())
            if handoff:
                # The claim is the lock and comes first (it needs the run's id, so the id is made here); a refused claim leaves no row.
                run_id = f"run-{uuid4().hex}"
                try:
                    taken = apply_runs.claim(
                        conn, user_id=user_id, opportunity_id=opportunity_id, mode="handoff", ats=apply_greenhouse.ATS_GREENHOUSE,
                        board_token=str(result["board_token"]), job_ref=f"{result['board_token']}/{result['job_id']}",
                        company=employer_key(str(result["company"])), plan_hash=inputs.plan.plan_hash, run_id=run_id,
                        stage_policy=apply_runs.stage_policy_for("handoff"), acknowledged=tuple(acknowledged), now=now,
                    )
                except apply_runs.ClaimRefused as exc:
                    raise RunRefused(409, str(exc), code=exc.code, ask=exc.ask) from exc
                token = str(taken["token"])
            files = {} if kind == "lookup" else self._files(conn, user_id, inputs.plan, resume_root)
            run_id = apply_runs.create_run(
                conn, user_id=user_id, opportunity_id=opportunity_id, kind=kind, started_by="student", ats=apply_greenhouse.ATS_GREENHOUSE,
                board_token=str(result["board_token"]), page_url=page_url, company=employer_key(str(result["company"])),
                deadline_seconds=int(deadline), run_id=run_id or None, application_id=taken["application_id"] if handoff else None,
                claim_token=token, now=now,
            )
            ends_at = 0.0
            if handoff:
                # Every wait in the agent ends before the watchdog would fire (a deadline under two minutes, in a test, keeps half of it).
                ends_at = time.monotonic() + max(deadline - ENDS_AT_MARGIN_S, deadline / 2)
            job = AgentJob(
                run_id=run_id, mode=MODE_FOR_KIND[kind], page_url=page_url, plan=inputs.plan, schema=list(inputs.schema), files=files,
                lookup=lookup, screenshot_dir=screenshot_dir, timeouts=self._timeouts, ends_at=ends_at,
            )
            work = _Job(
                run_id=run_id, kind=kind, user_id=user_id, opportunity_id=opportunity_id, company=str(result["company"]), page_url=page_url,
                database_target=database_target, apply_root=Path(apply_root), resume_root=Path(resume_root), factory=agent_factory,
                schema=list(inputs.schema), job=job, deadline_s=deadline, lookup=lookup, token=token, timeouts=self._timeouts,
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
            if token or run_id:
                self._abandon(conn, user_id, token, run_id=run_id)
            self._release(active)
            raise

    @staticmethod
    def _abandon(conn: sqlite3.Connection, user_id: str, token: str, *, run_id: str) -> None:
        """A start that failed after its claim or its row: close what was written, so nothing is left 'running' or 'claimed'.

        A Finish in browser start settles the claim (and, if it exists, the run) as stopped before anything was sent, in one
        transaction when the run's row exists (record_result). Any other start finishes its row, so it is not left 'running' with
        a Stop that does nothing and a day's rehearsal used up. Never raises.
        """
        if not token:
            try:
                apply_runs.finish_run(conn, run_id, outcome="failed", clean=False, reasons=[NOT_STARTED])
            except Exception as exc:  # noqa: BLE001 - recover_stale finishes the row later
                LOGGER.error("An Apply for me run that did not start could not be closed (%s)", type(exc).__name__)
                rollback_quietly(conn, LOGGER, "closing an Apply for me run that did not start")
            return
        try:
            rollback_quietly(conn, LOGGER, "a Finish in browser start that failed")
            exists = bool(run_id) and conn.execute(
                "SELECT 1 FROM apply_runs WHERE id=? AND user_id=? AND status='running'", (run_id, user_id),
            ).fetchone() is not None
            if exists:
                apply_runs.record_result(
                    conn, user_id=user_id, token=token, run_id=run_id, state="failed", outcome="failed", note=START_FAILED,
                    reasons=[START_FAILED], after_click=False, expected_states=("claimed",), notify=True,
                )
            else:
                apply_runs.settle(conn, token, user_id=user_id, state="failed", note=START_FAILED, after_click=False)
        except Exception as exc:  # noqa: BLE001 - recover_stale finishes it later
            LOGGER.error("A failed Finish in browser start could not settle its claim (%s)", type(exc).__name__)
            rollback_quietly(conn, LOGGER, "settling a Finish in browser start that failed")
        finally:
            apply_claims.forget(token)

    @staticmethod
    def _files(conn: sqlite3.Connection, user_id: str, plan: Any, resume_root: Path) -> dict[str, FilePayload]:
        """The files to attach, read now and verified: the résumé against the hash stored at upload, the cover letter against the text
        the plan was made from. A file that cannot be read has no payload: the agent says so."""
        files: dict[str, FilePayload] = {}
        for entry in plan.fields:
            if entry.disposition not in ("fill", "deferred") or entry.key in files:
                continue
            try:
                if entry.source.kind == "resume":
                    path, name, media_type, sha = confirmed_resume_file(conn, entry.source.ref, resume_root, user_id=user_id, verify=True)
                    files[entry.key] = FilePayload(name=name, mime_type=media_type, buffer=path.read_bytes(), sha256=sha)
                elif entry.source.kind == "cover_letter":
                    document_id = entry.source.ref.partition("@")[0]
                    letter = document_artifacts.attachable_file(conn, document_id, resume_root, user_id=user_id)
                    files[entry.key] = FilePayload(
                        name=letter.name, mime_type=letter.media_type, buffer=letter.data, sha256=letter.sha256, content_sha256=letter.content_sha256,
                    )
            except Exception:  # noqa: BLE001 - a missing or changed file leaves no payload; the rehearsal then reports NO_FILE
                continue
        return files

    # --- the supervisor thread

    def _thread(self, active: _Active, work: _Job, done: threading.Event) -> None:
        try:
            with apply_runs.running_run(work.run_id), closing(connect_product(work.database_target)) as conn:
                self._work(conn, active, work)
        except Exception as exc:  # noqa: BLE001 - recover_stale finishes the row later; no value is logged
            LOGGER.error("An Apply for me run ended without recording its result (%s)", type(exc).__name__)
        finally:
            if work.token:
                apply_claims.forget(work.token)
                self._reader.forget(work.token)
            self._release(active)
            done.set()

    def _work(self, conn: sqlite3.Connection, active: _Active, work: _Job) -> None:
        run_id, user_id = work.run_id, work.user_id
        handoff = work.kind == "handoff"
        steps: list[dict[str, str]] = []
        codes: _CodeAnswers | None = None
        def note(step: str, text: str) -> None:
            steps.append({"at": _stamp(), "step": step, "text": text})
            del steps[:-PROGRESS_KEEP]
            with conn:
                conn.execute(
                    "UPDATE apply_runs SET progress_json=?, heartbeat_at=? WHERE id=? AND status='running'",
                    (json.dumps(steps, sort_keys=True), _stamp(), run_id),
                )

        def beat() -> None:
            apply_runs.heartbeat_run(conn, run_id)
            if handoff:
                apply_runs.heartbeat(conn, work.token)

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
                    apply_policy.with_page_labels(work.schema, scan), scan, sources, work.company, "handoff" if handoff else "rehearse",
                    canonical_url=work.page_url, adapter_version=apply_greenhouse.ADAPTER_VERSION, uploads_on_attach=uploads_on_attach,
                )

            def check_file(_key: str, ref: str, sha256: str) -> bool:
                document_id, _, version = ref.partition("@")
                return bool(version.isdigit() and apply_policy.letter_is_current(
                    conn, user_id, work.opportunity_id, document_id=document_id, version=int(version), content_sha256=sha256))

            handlers = SupervisorHandlers(
                progress=lambda step, text: note(step, text), heartbeat=beat, replan=planner, hand_over=lambda: False, tick=beat,
                check_file=check_file,
            )
            if handoff:
                codes = _CodeAnswers(self._reader, work.database_target, user_id, work.token)

                def hand_over(deadline: float | None = None) -> bool:
                    if active.abort.is_set():
                        return False     # shutting down or deleting the account: a commit now would be followed by a kill mid-submit
                    try:
                        handed = bool(apply_runs.hand_over(conn, work.token, user_id=user_id, deadline=deadline))
                        if handed:
                            work.handed_over_seen = True
                        return handed
                    except Exception as exc:  # noqa: BLE001 - when unsure, nothing is sent
                        LOGGER.warning("A hand-over could not be recorded (%s)", type(exc).__name__)
                        work.handed_over_seen = True   # the commit may have landed before the error: never called "not sent" from here
                        rollback_quietly(conn, LOGGER, "recording a Finish in browser hand-over")
                        return False

                def ready(message: dict[str, Any]) -> None:
                    shots = _relative_screenshots(work.apply_root, [message["screenshot"]] if isinstance(message.get("screenshot"), dict) else [])
                    # The window closes when the agent says it will (its turn is shortened by a slow fill), never later than handoff_s.
                    kept = message.get("handoff_in_s")
                    seconds = work.timeouts.handoff_s
                    if isinstance(kept, (int, float)) and not isinstance(kept, bool) and 0 <= kept <= work.timeouts.handoff_s:
                        seconds = float(kept)
                    until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
                    stored = apply_runs.record_handoff_ready(
                        conn, user_id=user_id, run_id=run_id, token=work.token, plan=[item for item in message.get("plan") or [] if isinstance(item, dict)],
                        plan_hash=str(message.get("plan_hash") or ""), screenshots=shots, handoff_until=apply_watch.iso_utc(until),
                        evidence={
                            "left_for_you": _left_items(message.get("left")), "captcha_widget": bool(message.get("captcha_widget")),
                            "page_defaults": [str(key) for key in message.get("page_defaults") or []],
                            "handoff_until": apply_watch.iso_utc(until),
                        },
                    )
                    if not stored:
                        LOGGER.warning("The Finish in browser window was ready but its claim was no longer waiting")

                handlers.hand_over = hand_over
                handlers.handoff_ready = ready
                handlers.security_code = codes.ask
                handlers.code_replies = codes.ready
                handlers.security_code_result = lambda message: self._reader.confirm(
                    conn, user_id=user_id, token=work.token, typed=bool(message.get("typed")), reason=str(message.get("reason") or ""),
                )
            supervising = True
            outcome = supervise(
                work.factory, work.job, deadline_s=work.deadline_s, handlers=handlers, cancel=active.cancel, abort=active.abort,
                cancel_grace_s=self._cancel_grace_s, tick_s=HEARTBEAT_EVERY_S, front=(lambda: active.front) if handoff else None,
            )
        except Exception as exc:  # noqa: BLE001 - the type name is kept, never the message
            LOGGER.error("An Apply for me run could not be supervised (%s)", type(exc).__name__)
            outcome = Supervised(None, stop=STOP_ERROR, error=type(exc).__name__ if supervising else "NotStarted")
        finally:
            if codes is not None:
                codes.close()
        # A console's Ctrl+C reaches the child as well as the server on Windows, and the child can report it before the server's
        # own shutdown starts. It is the server stopping, not a browser that broke.
        interrupted = outcome.stop == STOP_ERROR and outcome.error == "KeyboardInterrupt"
        shutting_down = active.shutting_down or interrupted
        final = self._finish_handoff(conn, work, outcome, shutting_down=shutting_down) if handoff \
            else self._finish(conn, work, outcome, shutting_down=shutting_down)
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

    # --- settling a Finish in browser run

    def _finish_handoff(self, conn: sqlite3.Connection, work: _Job, outcome: Supervised, *, shutting_down: bool) -> str:
        """Settle a handoff's claim and finish its run in one transaction (5.3 table), after the browser is confirmed gone.

        The decision is made from the claim as it is now (read here, after the child ended), and the write names the state it
        was made from, so a claim that moved meanwhile (another process's recovery) is never overwritten: it is read again and
        decided once more, and after a second mismatch only the run is finished.
        """
        user_id, token = work.user_id, work.token
        result = outcome.result   # a result that arrived before a stop still counts
        settlement: Settlement | None = None
        write_failed = False
        for _attempt in range(2):
            try:
                row = self._read_claim(conn, token, user_id)
            except Exception as exc:  # noqa: BLE001
                LOGGER.error("A Finish in browser claim could not be read (%s)", type(exc).__name__)
                rollback_quietly(conn, LOGGER, "reading a Finish in browser claim")
                # The claim is unread, so "not sent" is said only when the child's own result proves it: a result that never handed
                # over and saw no continued submit, with the parent's own record agreeing. With no result (the child died, was killed
                # or the server stopped, perhaps after the student's press) or a result that handed over, the run is finished as "may
                # have been sent" and the claim is left to recovery, which reads it from the database.
                evidence = result.evidence if result is not None and isinstance(result.evidence, dict) else {}
                proven_unsent = (
                    result is not None and not result.handed_over and evidence.get("submit_continued") is not True and not work.handed_over_seen
                )
                if proven_unsent:
                    settlement = handoff_settlement(
                        result, stop=outcome.stop, shutting_down=shutting_down, claim_state="claimed", cancel_requested=False,
                        handed_over=False, closed_confirmed=outcome.closed_confirmed,
                        minutes=round(work.deadline_s / 60), not_started=outcome.error == "NotStarted",
                    )
                else:
                    settlement = Settlement("unconfirmed", "unconfirmed", UNCONFIRMED_NOTE, [UNCONFIRMED_NOTE], True, row=6)
                self._keep_unconfirmed(work, settlement)
                self._finish_run_only(conn, work, settlement.outcome if settlement.outcome in OUTCOMES else "failed", {"reasons": settlement.reasons})
                return settlement.outcome
            claim_state = str(row["state"]) if row is not None else ""
            detail = json_as(row["detail_json"] if row is not None else "", {})
            settlement = handoff_settlement(
                result, stop=outcome.stop, shutting_down=shutting_down, claim_state=claim_state,
                cancel_requested=bool(row["cancel_requested"]) if row is not None else False,
                handed_over=bool(row is not None and row["handed_over_at"]), closed_confirmed=outcome.closed_confirmed,
                minutes=round(work.deadline_s / 60), not_started=outcome.error == "NotStarted",
            )
            if settlement.integrity_error:
                LOGGER.error("A Finish in browser run came back handed over while its claim was not (run %s)", work.run_id)
            evidence: dict[str, Any] = dict(result.evidence) if result is not None else {}
            reader = detail.get(apply_security_code.RECORD_KEY)   # the reader's own record; detail.security_code is 6.14's boolean
            evidence["security_code_reader"] = reader if isinstance(reader, dict) else {}
            evidence["runner"] = {
                "stop": outcome.stop, "closed_confirmed": outcome.closed_confirmed, "claim_state": claim_state, "row": settlement.row,
            }
            documents = self._handoff_documents(work, result, settlement, evidence)
            if not settlement.state:
                # Row 2: the claim is already settled (or gone); the run is finished with what came back, and the claim is left alone.
                apply_claims.take_unconfirmed(work.token)
                self._finish_run_only(conn, work, settlement.outcome, documents)
                return settlement.outcome
            # record_result takes the token out of RUNNING even when its write fails, so a recovery pass that lands before the second
            # attempt would settle the still-'claimed' claim as "Nothing was sent". The decision is therefore kept before the first write.
            self._keep_unconfirmed(work, settlement)
            written = self._record_handoff(conn, work, settlement, documents, result, detail)
            if written is not None:
                apply_claims.take_unconfirmed(work.token)   # the database answered: the claim says what it says now
            if written is not None and written.get("settled"):
                return settlement.outcome
            write_failed = written is None
        # The claim moved twice between our read and our write, or the write failed twice: finish the run, leave the claim to recovery.
        LOGGER.error("A Finish in browser claim kept moving while its run was settled (run %s)", work.run_id)
        assert settlement is not None
        if write_failed:
            self._keep_unconfirmed(work, settlement)
        self._finish_run_only(conn, work, settlement.outcome if settlement.outcome in OUTCOMES else "failed", {"reasons": settlement.reasons})
        return settlement.outcome

    @staticmethod
    def _read_claim(conn: sqlite3.Connection, token: str, user_id: str) -> Any:
        """The claim as it is now. A busy database is tried again twice (the connection already waits for its own lock)."""
        for attempt in range(3):
            try:
                return conn.execute(
                    "SELECT state, cancel_requested, handed_over_at, detail_json FROM application_submit_claims WHERE token=? AND user_id=?",
                    (token, user_id),
                ).fetchone()
            except Exception:  # noqa: BLE001 - the last one is raised
                if attempt == 2:
                    raise
                rollback_quietly(conn, LOGGER, "reading a Finish in browser claim")
                time.sleep(0.25 * (attempt + 1))
        return None

    @staticmethod
    def _keep_unconfirmed(work: _Job, settlement: "Settlement") -> None:
        """A settlement that said "may have been sent" and could not be written: kept for recover_stale, which never says "Nothing was sent" then.

        The token has already left claims.RUNNING (record_result forgets it whether or not its write worked), so without this the next
        recovery pass settles the still-'claimed' claim as failed, with the sentence that nothing was sent.
        """
        if settlement.state == "unconfirmed" and settlement.after_click:
            apply_claims.mark_unconfirmed(work.token, settlement.note)

    @staticmethod
    def _handoff_documents(work: _Job, result: RunResult | None, settlement: "Settlement", evidence: dict[str, Any]) -> dict[str, Any]:
        documents: dict[str, Any] = {"reasons": list(settlement.reasons), "evidence": evidence}
        if result is not None:
            documents.update(
                plan=list(result.plan) or None, refused=list(result.refused)[:500], requests=list(result.requests)[:200],
                screenshots=_relative_screenshots(work.apply_root, result.screenshots) if result.screenshots else None,
                plan_hash=result.plan_hash or None,
            )
        return documents

    @staticmethod
    def _finish_run_only(conn: sqlite3.Connection, work: _Job, outcome: str, documents: Mapping[str, Any]) -> None:
        try:
            body = {key: value for key, value in documents.items() if key not in ("plan_hash",)}
            apply_runs.finish_run(conn, work.run_id, outcome=outcome if outcome in OUTCOMES else "failed", plan_hash=documents.get("plan_hash"), **body)
        except Exception as exc:  # noqa: BLE001 - recover_stale finishes the row later
            LOGGER.error("An Apply for me run could not record its result (%s)", type(exc).__name__)
            rollback_quietly(conn, LOGGER, "recording an Apply for me run's result")

    def _record_handoff(
        self, conn: sqlite3.Connection, work: _Job, settlement: "Settlement", documents: Mapping[str, Any], result: RunResult | None,
        detail: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        evidence = documents.get("evidence") if isinstance(documents.get("evidence"), dict) else {}
        shots = documents.get("screenshots") or []
        event_detail = {
            "run_id": work.run_id, "mode": "handoff", "confirmation_path": str(evidence.get("confirmation_path") or ""),
            "screenshot_sha256": str(shots[-1].get("sha256") or "") if shots and isinstance(shots[-1], dict) else "",
            "post_status": evidence.get("submit_status"), "security_code": bool((evidence.get("security_code") or {}).get("prompted"))
            if isinstance(evidence.get("security_code"), dict) else False, "by": "student_in_window",
        }
        claim_detail: dict[str, Any] = {"waiting": ""}
        if event_detail["security_code"]:
            # 6.14's boolean: the statistics count a prompt from the reader's record or from this, so a prompt the reader never recorded
            # (its first answer failed, or the child never asked) is still counted.
            claim_detail["security_code"] = True
        if settlement.stopped_by:
            claim_detail["stopped_by"] = settlement.stopped_by
        try:
            return apply_runs.record_result(
                conn, user_id=work.user_id, token=work.token, run_id=work.run_id, state=settlement.state, outcome=settlement.outcome,
                note=settlement.note, reasons=list(settlement.reasons), after_click=settlement.after_click,
                confirmation_seen=settlement.confirmation_seen, watch=apply_watch.watch_for(conn, work.user_id, "handoff"),
                evidence=dict(evidence), screenshots=documents.get("screenshots"), requests=documents.get("requests"),
                refused=documents.get("refused"), plan=documents.get("plan"), plan_hash=documents.get("plan_hash"), detail=claim_detail,
                event_detail=event_detail, notify=settlement.notify, expected_states=settlement.expected_states,
            )
        except Exception as exc:  # noqa: BLE001 - recover_stale settles the claim later
            LOGGER.error("A Finish in browser result could not be recorded (%s)", type(exc).__name__)
            rollback_quietly(conn, LOGGER, "recording a Finish in browser result")
            return None

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
    "handoff": ("submitted", "unconfirmed", "needs_you", "failed"),
}
assert all(set(items) <= set(OUTCOMES) for items in _OUTCOMES_FOR_KIND.values())


@dataclass(frozen=True)
class Settlement:
    """How a Finish in browser attempt ends: the claim's state and note, the run's outcome, and what to tell the student.

    ``state`` is "" when the claim is left alone (it was already settled). ``expected_states`` names the claim state the
    decision was made from, so the write never lands on a claim that has moved since. ``row`` is the table row that matched.
    """

    state: str
    outcome: str
    note: str
    reasons: list[str]
    after_click: bool
    confirmation_seen: bool = False
    notify: bool = True
    stopped_by: str = ""
    expected_states: tuple[str, ...] = ()
    integrity_error: bool = False
    row: int = 0


def handoff_settlement(
    result: RunResult | None, *, stop: str, shutting_down: bool, claim_state: str, cancel_requested: bool, handed_over: bool,
    closed_confirmed: bool, minutes: int, not_started: bool = False,
) -> Settlement:
    """The 5.3 table, first row that matches wins. Pure: every input is a fact read after the child (and its browser) ended.

    After the hand-over nothing is called "not sent" except row 5, from the route's own record that the POST was aborted;
    before it, "not sent" is only said once the window is confirmed closed (row 7 first).
    """
    evidence = result.evidence if result is not None and isinstance(result.evidence, dict) else {}
    notes = [item for item in (result.reasons if result is not None else []) if item]
    first = notes[0] if notes else ""
    if claim_state not in ("claimed", "clicking"):
        if result is not None and result.outcome == "submitted" and result.confirmation_seen and claim_state == "unconfirmed":
            # Row 1: the confirmation page is stronger evidence than a recovery that could only say it may have been sent.
            return Settlement("submitted", "submitted", "", notes, True, confirmation_seen=True, expected_states=("unconfirmed",), row=1)
        # Row 2: the claim is settled (or gone); the run is finished with what came back.
        fits = result is not None and result.outcome in _OUTCOMES_FOR_KIND["handoff"]
        outcome = result.outcome if fits and result is not None else "failed"
        if (claim_state == "unconfirmed" or handed_over) and outcome in ("needs_you", "failed") and _says_not_sent(notes or [CHILD_DIED]):
            # The claim says the application may have been sent (a recovery moved it, or the student's press was handed over): the
            # child's own "nothing was sent" must not be the run's sentence beside it.
            return Settlement("", "unconfirmed", UNCONFIRMED_NOTE, [UNCONFIRMED_NOTE], True, row=2)
        return Settlement("", outcome, first, notes or [CHILD_DIED], False, row=2)
    sent = claim_state == "clicking" or handed_over
    if sent:
        if result is not None and result.handed_over:
            if result.outcome == "submitted" and result.confirmation_seen:
                return Settlement("submitted", "submitted", "", notes, True, confirmation_seen=True, expected_states=(claim_state,), row=3)
            if result.outcome in ("failed", "needs_you") and result.after_click and not _says_not_sent([*notes, first]):
                return Settlement(result.outcome, result.outcome, first, notes, True, expected_states=(claim_state,), row=4)
            if result.outcome == "failed" and not result.after_click and evidence.get("submit_continued") is False:
                return Settlement("failed", "failed", first, notes, False, expected_states=(claim_state,), row=5)
        # Row 6: anything else once the claim was handed over. Never a sentence of the child's: it may say nothing was sent.
        return Settlement("unconfirmed", "unconfirmed", UNCONFIRMED_NOTE, [UNCONFIRMED_NOTE], True, expected_states=(claim_state,), row=6)
    # The claim is still 'claimed': the application was never handed over.
    if not closed_confirmed:
        return Settlement("unconfirmed", "unconfirmed", WINDOW_UNCONFIRMED, [WINDOW_UNCONFIRMED], True, expected_states=("claimed",), row=7)
    passed = result is not None and (
        result.handed_over or evidence.get("submit_continued") is True
        or any(item.get("passed") and str(item.get("method") or "").upper() not in ("", "GET", "HEAD", "OPTIONS") for item in result.requests if isinstance(item, dict))
    )
    if passed:
        return Settlement("unconfirmed", "unconfirmed", UNCONFIRMED_NOTE, [UNCONFIRMED_NOTE], True, notify=True, expected_states=("claimed",), integrity_error=True, row=8)
    if cancel_requested:
        return Settlement("needs_you", "needs_you", HANDOFF_NOT_SUBMITTED, [HANDOFF_NOT_SUBMITTED], False, notify=False,
                          stopped_by="student", expected_states=("claimed",), row=9)
    if shutting_down:
        return Settlement("failed", "failed", SERVER_STOPPED, [SERVER_STOPPED], False, expected_states=("claimed",), row=10)
    end = str(evidence.get("handoff_end") or "")
    if result is not None and end == "closed" and evidence.get("browser_closed") is True:
        text = first or WINDOW_CLOSED
        return Settlement("needs_you", "needs_you", text, [text], False, notify=False, stopped_by="student", expected_states=("claimed",), row=11)
    if result is not None and end in ("stopped", "closed"):
        return Settlement("failed", "failed", CHILD_DIED, [CHILD_DIED], False, expected_states=("claimed",), row=12)
    if result is not None:
        kept = result.outcome in ("needs_you", "failed") and bool(first)
        text = first if kept else CHILD_DIED
        return Settlement(result.outcome if kept else "failed", result.outcome if kept else "failed", text, [text], False, expected_states=("claimed",), row=13)
    if stop == STOP_DEADLINE:
        text = _too_long(minutes * 60.0)
        return Settlement("failed", "failed", text, [text], False, expected_states=("claimed",), row=14)
    text = NOT_STARTED if not_started else CHILD_DIED   # nothing ran at all: the browser never opened
    return Settlement("failed", "failed", text, [text], False, expected_states=("claimed",), row=15)


def _says_not_sent(sentences: Sequence[str]) -> bool:
    """Whether any of the child's sentences says nothing was sent. After a hand-over only the route's own record may say it."""
    return any(_SAYS_NOT_SENT.search(str(item)) for item in sentences if item)


def _left_items(items: Any) -> list[dict[str, str]]:
    """The agent's list of what is left for the student, kept to its three words: key, question and reason (never a value)."""
    kept = []
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict):
            kept.append({"key": str(item.get("key") or ""), "question": str(item.get("question") or ""), "reason": str(item.get("reason") or "")})
    return kept


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
# The phases of a Finish in browser run that is still running (the last progress step names it), else "filling".
_HANDOFF_PHASES = ("your_turn", "submitting", "security_code", "code_typed", "code_yours", "challenge")
_SAYS_NOT_SENT = re.compile(r"not sent|nothing was sent|no application was sent", re.IGNORECASE)


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def _list_words(items: list[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def _is_file(entry: Mapping[str, Any]) -> bool:
    source = entry.get("source") if isinstance(entry.get("source"), dict) else {}
    return entry.get("control") == "file" or source.get("kind") in ("resume", "cover_letter") or bool(entry.get("file_sha256"))


def _is_statement(entry: Mapping[str, Any]) -> bool:
    """The entry is answered from a statement the student stored: any sensitive box the app ticks (an acknowledgment, a consent, or a
    work authorization, sponsorship or age statement) or a Yes/No agreement question. The plan treats every sensitive checkbox this way
    (policy._plan_sensitive: its links are read and checked against the stored statement's), so the view follows it, and the student is
    shown each box the app ticked and the documents it links to before pressing Submit."""
    source = entry.get("source") if isinstance(entry.get("source"), dict) else {}
    if source.get("kind") != "sensitive":
        return False
    return entry.get("control") == "checkbox" or (entry.get("sensitive") in STATEMENT_CATEGORIES and entry.get("control") == "select")


def _disposition_text(
    entry: Mapping[str, Any], done: frozenset[str] = frozenset(), checked: frozenset[str] = frozenset(), *,
    failed: frozenset[str] = frozenset(), absent: frozenset[str] = frozenset(), ended: bool = False, kind: str = "rehearsal",
) -> str:
    """What the run did with a field. For a rehearsal: ``done`` is the keys the agent reports it filled and read back (or attached and
    checked), ``checked`` the deferred keys it compared with the form and ``failed`` those whose comparison found the form does not offer
    the answer (or could not be read), ``absent`` the keys the form turned out not to draw, and ``ended`` whether the rehearsal ran to its
    end: the plan alone says what was meant to happen, never what did. For a handoff (``kind``) the plan the agent sent already says what
    it filled: a field it never reached or never read back comes back as blank, with a note."""
    disposition = str(entry.get("disposition") or "")
    key = str(entry.get("key") or "")
    source = entry.get("source") if isinstance(entry.get("source"), dict) else {}
    if kind == "handoff":
        if disposition == "fill":
            if source.get("kind") == "sensitive":
                if _is_statement(entry):
                    return "Ticked from your stored statement" if entry.get("control") == "checkbox" else "Answered from your stored statement"
                return "Filled from your stored answer"
            return "Filled in the window"
        if disposition == "left_for_you":
            return "Left for you"
        return "Left blank"
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
            return "Checked against the form; filled when you choose Finish in browser"
        return "Checked against the form"
    if disposition == "left_for_you":
        return "Left for you"
    return "Left blank"


def _source_text(entry: Mapping[str, Any], company: str = "") -> str:
    source = entry.get("source") if isinstance(entry.get("source"), dict) else {}
    kind = str(source.get("kind") or "none")
    if kind == "answer":
        name = str(source.get("company") or "")
        return f"Your saved answer for {name}" if name else "Your saved answer"
    if _is_statement(entry) and entry.get("sensitive") in STATEMENT_CATEGORIES:
        noun = "acknowledgment" if entry.get("sensitive") == "acknowledgment" else "consent"
        return f"Your {noun} for {company}" if company else f"Your {noun}"
    if kind == "cover_letter":
        version = str(source.get("ref") or "").rpartition("@")[2]
        return f"Approved cover letter, version {version}" if version.isdigit() else _SOURCE_TEXT["cover_letter"]
    return _SOURCE_TEXT.get(kind, "")


def _problem_item(item: Mapping[str, Any], kind: str = "") -> dict[str, Any]:
    return {"key": str(item.get("key") or ""), "question": str(item.get("question") or ""), "message": str(item.get("message") or item.get("problem") or ""),
            "required": bool(item.get("required", True)), "kind": kind or str(item.get("kind") or "")}


def _sentence(reason: str) -> str:
    return reason if reason[-1:] in ".!?" else f"{reason}."


def _summary(
    row: Mapping[str, Any], reasons: list[str], options: Mapping[str, Any], company: str, title: str, progress: list[dict[str, Any]], stalled: bool,
    posting: Mapping[str, Any] | None = None,
    *, phase: str = "", nothing_left: bool = False, claim: Mapping[str, Any] | None = None, after_click: bool = False,
) -> str:
    if row["status"] == "running":
        if stalled:
            return "The app stopped during this run"
        if row["kind"] == "handoff" and phase == "your_turn":
            return YOUR_TURN_NONE_LEFT if nothing_left else YOUR_TURN
        if row["kind"] == "handoff" and phase in _HANDOFF_PHASES:
            return PROGRESS_STEPS[phase]
        return str(progress[-1].get("text") or "") if progress else PROGRESS_STEPS["start"]
    outcome = str(row["outcome"] or "")
    first = reasons[0] if reasons else "The run did not finish"
    if row["kind"] == "handoff":
        if outcome == "submitted":
            ask = bool(claim and claim.get("ask_mark_applied"))
            return "Greenhouse showed its confirmation page. Mark as applied?" if ask else "Greenhouse showed its confirmation page."
        if outcome == "unconfirmed":
            return first
        # A stopped or failed handoff says "not sent" only when it was never handed over, and not twice.
        return first if after_click or _SAYS_NOT_SENT.search(first) else f"{_sentence(first)} {NO_SENT}"
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
        "to Greenhouse or anywhere else. Sensitive answers were not put in the page; they go in only if you choose Finish in browser, "
        "before you press Submit application."
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


def _claim_facts(conn: sqlite3.Connection, row: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str, bool, bool]:
    """(the claim's card, its state, whether it was handed over, whether the student asked to stop) for a run that has a claim."""
    token = str(row["claim_token"] or "")
    if not token:
        return None, "", False, False
    card = apply_watch.claim_card(conn, str(row["user_id"]), token)
    fact = conn.execute(
        "SELECT state, after_click, cancel_requested FROM application_submit_claims WHERE token=? AND user_id=?", (token, row["user_id"]),
    ).fetchone()
    if fact is None:
        return card, "", False, False
    return card, str(fact["state"]), bool(fact["after_click"]), bool(fact["cancel_requested"])


def _handoff_phase(progress: list[dict[str, Any]], card: Mapping[str, Any] | None, claim_state: str) -> str:
    """Where a running Finish in browser run is: filling, the student's turn, submitting, or one of the code steps.

    The last progress step names it, but the claim leads: the agent says ready (the claim says it is the student's turn)
    just before it reports the step, and the parent commits the hand-over just before the agent reports submitting, so for
    those moments the claim is the truth and the step catches up.
    """
    step = progress[-1]["step"] if progress else ""
    if claim_state == "clicking" and step not in _HANDOFF_PHASES[1:]:
        return "submitting"
    if step in _HANDOFF_PHASES:
        return step if not (step == "your_turn" and claim_state == "clicking") else "submitting"
    return "your_turn" if card is not None and card.get("status") == "your_turn" else "filling"


def run_view(
    conn: sqlite3.Connection, row: Mapping[str, Any], live: Callable[[str], bool] | None = None, *, local: str | None = None,
) -> dict[str, Any]:
    """A stored run as the page shows it (every key always present). Carries no field value: the row has none.

    ``live`` is whether this process is working on a run (see ``orphaned``); the routes pass it. ``local`` is the id of the run
    this app's runner is running now (``ApplyRunner.busy()``): the window can be brought forward only for that one.
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
    running = row["status"] == "running"
    stalled = running and (beat is None or datetime.now(timezone.utc) - beat > HELD_HEARTBEAT or orphaned(row, live))
    handoff = row["kind"] == "handoff"
    card, claim_state, after_click, _asked = _claim_facts(conn, row) if handoff else (None, "", False, False)
    phase = _handoff_phase(progress, card, claim_state) if handoff and running else ""
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
    kind_for_words = "handoff" if handoff else "rehearsal"
    fields = [
        {"key": str(entry.get("key") or ""), "question": str(entry.get("question") or ""), "required": bool(entry.get("required")),
         "sensitive": bool(entry.get("sensitive")), "disposition": str(entry.get("disposition") or ""),
         "disposition_text": _disposition_text(entry, filled, checked, failed=failed, absent=absent, ended=ended, kind=kind_for_words),
         "source_text": _source_text(entry, company), "problem": str(entry.get("problem") or ""),
         "control": str(entry.get("control") or ""), "statement": _is_statement(entry),
         "source_kind": str((entry.get("source") or {}).get("kind") or "") if isinstance(entry.get("source"), dict) else "",
         "links": [str(link) for link in ((entry.get("source") or {}).get("links") or [])] if isinstance(entry.get("source"), dict) else [],
         "note": str(entry.get("note") or "")}
        for entry in plan
    ]
    questions = {str(entry.get("key") or ""): str(entry.get("question") or "") for entry in plan}
    left = _left_items(evidence.get("left_for_you"))
    defaults = [{"key": str(key), "question": questions.get(str(key), "")} for key in evidence.get("page_defaults") or [] if isinstance(key, str)]
    shots = [
        {"index": index, "step": str(shot.get("step") or ""), "url": f"/api/v1/apply-agent/runs/{row['id']}/screenshots/{index}",
         "masked": [str(key) for key in (shot.get("masked") or [])], "available": bool(shot.get("path"))}
        for index, shot in enumerate(json_as(row["screenshots_json"], [])) if isinstance(shot, dict)
    ]
    lookup = evidence.get("lookup")
    until = evidence.get("handoff_until")
    can_cancel = running and not stalled and (claim_state == "claimed" if handoff else True)
    can_front = bool(handoff and running and claim_state in ("claimed", "clicking") and local is not None and local == row["id"])
    return {
        "id": row["id"], "opportunity_id": row["opportunity_id"], "kind": row["kind"], "status": row["status"],
        "outcome": row["outcome"] or "", "clean": bool(row["clean"]),
        "started_at": row["started_at"], "finished_at": row["finished_at"], "heartbeat_at": row["heartbeat_at"], "deadline_at": row["deadline_at"],
        "stalled": bool(stalled),
        "summary": _summary(
            row, reasons, options if isinstance(options, dict) else {}, company, title, progress, bool(stalled),
            evidence.get("posting") if isinstance(evidence.get("posting"), dict) else None,
            phase=phase, nothing_left=not left, claim=card, after_click=after_click,
        ),
        "measured": _measured(row, evidence, refused if isinstance(refused, list) else []),
        "progress": progress, "reasons": reasons, "problems": problems, "fields": fields,
        "options": options if isinstance(options, dict) else {}, "lookup": lookup if isinstance(lookup, dict) else None,
        "screenshots": shots, "refused_count": len(refused) if isinstance(refused, list) else 0,
        "review": row["review"] or "", "review_note": row["review_note"] or "", "reviewed_at": row["reviewed_at"],
        "can_review": reviewable(row),
        "can_cancel": bool(can_cancel),
        "phase": phase, "handed_over": bool(handoff and (after_click or evidence.get("handoff_end") == "posted")), "left_for_you": left, "handoff_until": str(until) if isinstance(until, str) and until and running else None,
        "page_defaults": defaults, "claim": card, "can_front": can_front,
    }


def run_views(
    conn: sqlite3.Connection, user_id: str, opportunity_id: str, *, kind: str = "", limit: int = 5,
    live: Callable[[str], bool] | None = None, local: str | None = None,
) -> list[dict[str, Any]]:
    """This student's lookups, rehearsals and Finish in browser runs for one role, newest first."""
    kinds = (kind,) if kind in ("lookup", "rehearsal", "handoff") else ("lookup", "rehearsal", "handoff")
    marks = ", ".join("?" for _ in kinds)
    rows = conn.execute(
        f"SELECT * FROM apply_runs WHERE user_id=? AND opportunity_id=? AND kind IN ({marks}) ORDER BY started_at DESC LIMIT ?",
        (user_id, opportunity_id, *kinds, max(1, int(limit))),
    ).fetchall()
    return [run_view(conn, dict(row), live, local=local) for row in rows]


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

