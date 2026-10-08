"""The child side of an Apply for me run: the pipe to the runner, and the one function a spawned process starts in.

The runner (apply/runner.py) starts ``child_main`` in a process of its own (or, for a fake that opens no browser, a
thread), so a browser that hangs can be killed with its whole process tree and a crash in it cannot take the web app
down. The two sides talk over two one-way pipes, with the messages named in apply/agent_types.py.

A child process also bounds itself. The runner's watchdog is the usual way a hung run ends, but a runner that dies without its shutdown
(Stop-Process, a crash, launchd's SIGKILL) takes the watchdog with it, and a page that never yields never reads the cancel flag. So when
the pipe from the runner reaches end-of-file, and when the job's own deadline passes, the child gives the agent ``ApplyTimeouts.orphan_s``
to finish and then ends the process with ``os._exit``. Its Playwright driver sees its stdin close and takes Chromium down with it. A
Finish in browser run whose hand-over the runner already committed keeps the student's window until the agent's own cap (``ends_at``)
and ends ``orphan_s`` after that instead, still inside the job's deadline. A thread child (a fake that opens no browser) shares the
server's process and never does this.

Standard library and ``agent_types`` only (tests/test_leaf_modules.py holds it to that). This module imports no web app
and no database code. The job it is handed does: it names the agent's factory and carries a ``policy.Plan``, so unpickling
the job in the child imports apply/agent.py and apply/policy.py and what they import. That is import time only, and no
connection is opened. Planning stays in the parent (a ``policy.Sources`` holds a database connection), so ``replan`` here
sends the page's scan over the pipe and waits for the plan that comes back.

What crosses the pipe from the child is value-free by construction: progress sentences, the page's scan (taken with an
empty profile, so it holds structure and no student value), and the final ``RunResult``. An exception is reported by its
type name only, since its message may hold a value.

For Finish in browser the channel is also the agent's ``HandoffLink`` (agent_types.py): it sends the one-way "ready"
and security-code result messages, asks the parent for the emailed security code without ever blocking (a route handler
only runs while the main thread is inside a Playwright call, so a long blocking wait here would hold the student's own
POST in the route queue), and tells the agent when the window should come to the front and when the parent is gone. A
code reply is kept only while the agent's one ask is outstanding, and is handed out once: a reply for any other id is
dropped on arrival, so a late answer can never be filed under an abandoned ask.
"""

from __future__ import annotations

import itertools
import os
import threading
import time
from pathlib import Path
from typing import Any

from .agent_types import (
    OP_CANCEL, OP_ERROR, OP_FILE_CHECK, OP_FILE_CHECK_REPLY, OP_FRONT, OP_HAND_OVER, OP_HAND_OVER_REPLY, OP_HANDOFF_READY, OP_HEARTBEAT,
    OP_PROGRESS, OP_REPLAN, OP_REPLAN_REPLY, OP_RESULT, OP_SECURITY_CODE, OP_SECURITY_CODE_REPLY, OP_SECURITY_CODE_RESULT,
    AgentJob, ApplyTimeouts, RunResult,
)


ORPHAN_EXIT_CODE = 3


def _exit_now() -> None:
    os._exit(ORPHAN_EXIT_CODE)


def _end_process_in(seconds: float) -> threading.Timer:
    """End this process in ``seconds`` (a daemon timer: it never keeps a finished child alive)."""
    timer = threading.Timer(max(0.0, seconds), lambda: _exit_now())
    timer.daemon = True
    timer.start()
    return timer


class ReplanFailed(RuntimeError):
    """The parent gave no plan: it did not answer in time, it could not plan, or the pipe closed."""


class ChildChannel:
    """The child's end of the pipe. Starts a daemon thread that reads the inbox (cancel flag and replies by id)."""

    def __init__(self, inbox: Any, outbox: Any, timeouts: ApplyTimeouts, *, end_process_when_orphaned: bool = False) -> None:
        self._inbox = inbox
        self._end_process_when_orphaned = end_process_when_orphaned
        self._outbox = outbox
        self._timeouts = timeouts
        # The time.monotonic() instant by which the agent's every wait must end (the job's ends_at); 0 means no cap.
        # Not part of HandoffLink: the agent reads it with getattr(link, "ends_at", 0.0).
        self.ends_at = 0.0
        self._cancel = threading.Event()
        self._send_lock = threading.Lock()
        self._replies: dict[int, dict[str, Any]] = {}
        self._arrived = threading.Condition()
        self._closed = False
        self._gone = False
        self._handed_over = False   # the parent committed a hand-over: an orphaned child then keeps the window to its cap
        self._ids = itertools.count(1)
        # The one security-code ask that may be answered, its reply (until the agent takes it) and the window requests.
        self._state_lock = threading.Lock()
        self._code_pending: int | None = None
        self._code_replies: dict[int, dict[str, Any]] = {}
        self._front = 0
        self._reader = threading.Thread(target=self._read, name="apply-child-inbox", daemon=True)
        self._reader.start()

    # --- the inbox ---------------------------------------------------------------------------

    def _read(self) -> None:
        while True:
            try:
                message = self._inbox.recv()
                if not isinstance(message, dict):
                    continue
                self._file(message)
            except Exception:  # noqa: BLE001 - end of file, a closed pipe, or a corrupt frame (an UnpicklingError): fail closed
                break
        # This thread is the inbox's only reader, so it is the one that closes it (close() leaves it alone). Closed from the agent's
        # thread while this one is between taking the descriptor and reading it, the number is free again; in a thread child, which
        # shares the server's process, the next pipe made can take it, and this thread would then read (and lose) that pipe's messages.
        try:
            self._inbox.close()
        except Exception:  # noqa: BLE001 - already closed
            pass
        # The runner went away (it closed its end, or died), or sent something unreadable: nobody can be trusted to hear
        # this run any more, so stop it. Before the hand-over that closes the browser; after it the agent keeps the window
        # for the student until its own time is up (parent_gone). A page that never yields never reads the flag, so a process
        # child also ends itself after the grace (the cancel flag is the polite way, this is the one that works).
        self._gone = True
        self._cancel.set()
        if self._end_process_when_orphaned:
            _end_process_in(self.orphan_grace_s())
        with self._arrived:
            self._closed = True
            self._arrived.notify_all()

    def orphan_grace_s(self) -> float:
        """How long an orphaned process child lives on. ``orphan_s`` before the hand-over; after it the agent keeps the window for
        the student until its own cap (``ends_at``), so the process ends ``orphan_s`` after that cap instead (the job's deadline
        timer still bounds it). A window that was handed over is never cut off while the student may be pressing Submit."""
        grace = self._timeouts.orphan_s
        if self._handed_over and self.ends_at:
            grace = max(grace, self.ends_at - time.monotonic() + self._timeouts.orphan_s)
        return grace

    def _file(self, message: dict[str, Any]) -> None:
        op = message.get("op")
        if op == OP_CANCEL:
            self._cancel.set()
        elif op in (OP_REPLAN_REPLY, OP_HAND_OVER_REPLY, OP_FILE_CHECK_REPLY):
            with self._arrived:
                self._replies[int(message["id"])] = message
                if op == OP_HAND_OVER_REPLY and message.get("ok") is True:
                    # Set here, where the reply is filed, and not when the agent's thread wakes: the parent may commit, answer and die
                    # at once, and the reader would then see end-of-file and work out the grace before that thread ran.
                    self._handed_over = True
                self._arrived.notify_all()
        elif op == OP_SECURITY_CODE_REPLY:
            with self._state_lock:
                ident = message.get("id")
                # Only the one outstanding ask is answered; anything else (an abandoned id, a duplicate) is dropped here.
                if isinstance(ident, int) and not isinstance(ident, bool) and ident == self._code_pending:
                    self._code_replies[ident] = message
        elif op == OP_FRONT:
            with self._state_lock:
                self._front += 1

    def _wait_for(self, ident: int, seconds: float) -> dict[str, Any] | None:
        with self._arrived:
            self._arrived.wait_for(lambda: ident in self._replies or self._closed, timeout=seconds)
            return self._replies.pop(ident, None)

    # --- the outbox --------------------------------------------------------------------------

    def _send(self, message: dict[str, Any]) -> None:
        with self._send_lock:
            self._outbox.send(message)

    def progress(self, step: str, text: str) -> None:
        self._send({"op": OP_PROGRESS, "step": str(step), "text": str(text)})

    def heartbeat(self) -> None:
        self._send({"op": OP_HEARTBEAT})

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def replan(self, scan: list[dict[str, Any]], uploads_on_attach: bool) -> Any:
        """The plan the parent builds for the page as scanned. Raises ReplanFailed when there is none."""
        ident = next(self._ids)
        try:
            self._send({"op": OP_REPLAN, "id": ident, "scan": scan, "uploads_on_attach": bool(uploads_on_attach)})
        except (OSError, ValueError) as exc:
            raise ReplanFailed("the runner is gone") from exc
        reply = self._wait_for(ident, self._timeouts.replan_s)
        if reply is None or reply.get("error") or reply.get("plan") is None:
            raise ReplanFailed("no plan")
        return reply["plan"]

    def hand_over(self) -> bool:
        """Ask the runner to commit the hand-over. Only an explicit ok=True is True; silence and errors are False."""
        ident = next(self._ids)
        try:
            # ``expires`` is when this wait ends (time.monotonic() is one clock for every process on the host): a parent that
            # commits after it must refuse, since this side will already have aborted the student's POST.
            self._send({"op": OP_HAND_OVER, "id": ident, "expires": time.monotonic() + self._timeouts.reply_s})
        except (OSError, ValueError):
            return False
        reply = self._wait_for(ident, self._timeouts.reply_s)
        accepted = bool(reply is not None and reply.get("ok") is True)
        if accepted:
            self._handed_over = True
        return accepted

    def check_file(self, key: str, ref: str, sha256: str) -> bool:
        """Ask the runner whether the document the plan names for field ``key`` (its ``ref`` and the SHA-256 of its text) is still the one
        to attach. Only an explicit ok=True is True; silence, an error and a closed pipe are False."""
        ident = next(self._ids)
        try:
            self._send({"op": OP_FILE_CHECK, "id": ident, "key": str(key), "ref": str(ref), "sha256": str(sha256)})
        except (OSError, ValueError):
            return False
        reply = self._wait_for(ident, self._timeouts.reply_s)
        return bool(reply is not None and reply.get("ok") is True)

    # --- Finish in browser: the agent's HandoffLink ------------------------------------------

    def ready(self, message: dict[str, Any]) -> None:
        """The form is filled and the student's turn begins (one-way). A pipe that cannot take it only means no list is shown."""
        try:
            self._send({**message, "op": OP_HANDOFF_READY})
        except (OSError, ValueError):
            pass

    def ask_code(self) -> int:
        """Ask the parent whether the security code has arrived. Never blocks; returns the ask's id, the only one that may be answered."""
        ident = next(self._ids)
        with self._state_lock:
            self._code_pending = ident
            self._code_replies.pop(ident, None)
        try:
            self._send({"op": OP_SECURITY_CODE, "id": ident})
        except (OSError, ValueError):
            pass   # its reply will be the closed-pipe fallback
        return ident

    def code_reply(self, ident: int) -> dict[str, Any] | None:
        """The reply to ask ``ident`` if it has arrived (and takes it: nothing is kept here after this), else None. Never blocks."""
        with self._state_lock:
            reply = self._code_replies.pop(ident, None)
            if reply is not None:
                if self._code_pending == ident:
                    self._code_pending = None
                return reply
            if self._closed:
                self._code_pending = None
                return {"status": "fallback", "reason": "not_current"}
        return None

    def abandon_code(self) -> None:
        """The agent stops waiting for its outstanding ask (it gave the code to the student, or the run is ending). Never blocks.

        What arrives for it afterwards is dropped on arrival, and one that already arrived is dropped now (I7). The parent is told
        once, so the code its reader may have found is dropped there too and the claim records that the student took over.
        """
        with self._state_lock:
            ident, self._code_pending = self._code_pending, None
            if ident is not None:
                self._code_replies.pop(ident, None)
        if ident is not None:
            self.code_result(ident, False, "abandoned")

    def code_result(self, ident: int, typed: bool, reason: str = "") -> None:
        """What became of the code the parent handed over (one-way): only this makes the parent record that it was typed."""
        try:
            self._send({"op": OP_SECURITY_CODE_RESULT, "id": ident, "typed": bool(typed), "reason": str(reason or "")})
        except (OSError, ValueError):
            pass

    def front_requested(self) -> bool:
        """True once for each request from the parent to bring the window to the front."""
        with self._state_lock:
            if self._front > 0:
                self._front -= 1
                return True
        return False

    def parent_gone(self) -> bool:
        """The pipe from the parent hit end-of-file or carried something unreadable."""
        return self._gone

    # --- the end -----------------------------------------------------------------------------

    def send_result(self, result: RunResult) -> None:
        self._send({"op": OP_RESULT, "result": result})

    def send_error(self, exc: BaseException) -> None:
        """The exception's type name only: its message may hold a value."""
        self._send({"op": OP_ERROR, "error": type(exc).__name__})

    def close(self) -> None:
        """Close the outbox. The inbox is the reader thread's to close, at end-of-file (see _read)."""
        try:
            self._outbox.close()
        except Exception:  # noqa: BLE001 - already closed
            pass


def _report(channel: ChildChannel, result: RunResult | None, failure: BaseException | None) -> None:
    """The one terminal message. A result that cannot be sent (it does not pickle) is reported as an error instead."""
    try:
        if result is not None:
            channel.send_result(result)
            return
        channel.send_error(failure if failure is not None else RuntimeError("no result"))
    except BaseException as exc:  # noqa: BLE001 - the runner may be gone already
        if result is not None:
            try:
                channel.send_error(exc)
            except BaseException:  # noqa: BLE001
                pass


def child_main(factory: Any, job: AgentJob, inbox: Any, outbox: Any, new_session: bool) -> None:
    """Run one job and send exactly one terminal message (a result, or an error). Then close the pipe and return."""
    if new_session and hasattr(os, "setsid"):
        # Its own session and process group, so the runner can kill the browser and everything it started in one go.
        try:
            os.setsid()
        except OSError:
            pass
    channel: ChildChannel | None = None
    result: RunResult | None = None
    try:
        channel = ChildChannel(inbox, outbox, job.timeouts, end_process_when_orphaned=new_session)
        channel.ends_at = float(job.ends_at or 0.0)
        if new_session and job.deadline_s > 0:
            _end_process_in(job.deadline_s + job.timeouts.orphan_s)
        agent = factory(
            mode=job.mode, run_id=job.run_id, screenshot_dir=Path(job.screenshot_dir) if job.screenshot_dir else None,
            timeouts=job.timeouts, on_progress=channel.progress, heartbeat=channel.heartbeat, ats=job.ats,
        )
        # Only a Finish in browser run gets the link: an agent written before it (a lookup or rehearsal fake) never sees it.
        extra: dict[str, Any] = {"link": channel} if job.mode == "handoff" else {}
        if "cover_letter" in job.files:
            # Only a run with a letter to attach can ask the runner to check it again, so an agent written without that (a fake) never sees it.
            extra["check_file"] = channel.check_file
        with agent:
            result = agent.run(
                job.plan, page_url=job.page_url, schema=job.schema, files=job.files, lookup=job.lookup,
                replan=channel.replan, hand_over=channel.hand_over, cancelled=channel.cancelled, **extra,
            )
    except BaseException as exc:  # noqa: BLE001 - everything is reported, by type name only
        # A run that finished and then failed to close its browser still has its result.
        if channel is not None:
            _report(channel, result, exc)
        result = None
    else:
        _report(channel, result, None)
    finally:
        if channel is not None:
            channel.close()
        else:
            for end in (outbox, inbox):
                try:
                    end.close()
                except Exception:  # noqa: BLE001
                    pass
