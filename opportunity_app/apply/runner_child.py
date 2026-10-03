"""The child side of an Apply for me run: the pipe to the runner, and the one function a spawned process starts in.

The runner (apply/runner.py) starts ``child_main`` in a process of its own (or, for a fake that opens no browser, a
thread), so a browser that hangs can be killed with its whole process tree and a crash in it cannot take the web app
down. The two sides talk over two one-way pipes, with the messages named in apply/agent_types.py.

Standard library and ``agent_types`` only (tests/test_leaf_modules.py holds it to that). This module imports no web app
and no database code. The job it is handed does: it names the agent's factory and carries a ``policy.Plan``, so unpickling
the job in the child imports apply/agent.py and apply/policy.py and what they import. That is import time only, and no
connection is opened. Planning stays in the parent (a ``policy.Sources`` holds a database connection), so ``replan`` here
sends the page's scan over the pipe and waits for the plan that comes back.

What crosses the pipe from the child is value-free by construction: progress sentences, the page's scan (taken with an
empty profile, so it holds structure and no student value), and the final ``RunResult``. An exception is reported by its
type name only, since its message may hold a value.
"""

from __future__ import annotations

import itertools
import os
import threading
from pathlib import Path
from typing import Any

from .agent_types import (
    OP_CANCEL, OP_ERROR, OP_HAND_OVER, OP_HAND_OVER_REPLY, OP_HEARTBEAT, OP_PROGRESS, OP_REPLAN, OP_REPLAN_REPLY, OP_RESULT,
    AgentJob, ApplyTimeouts, RunResult,
)


class ReplanFailed(RuntimeError):
    """The parent gave no plan: it did not answer in time, it could not plan, or the pipe closed."""


class ChildChannel:
    """The child's end of the pipe. Starts a daemon thread that reads the inbox (cancel flag and replies by id)."""

    def __init__(self, inbox: Any, outbox: Any, timeouts: ApplyTimeouts) -> None:
        self._inbox = inbox
        self._outbox = outbox
        self._timeouts = timeouts
        self._cancel = threading.Event()
        self._send_lock = threading.Lock()
        self._replies: dict[int, dict[str, Any]] = {}
        self._arrived = threading.Condition()
        self._closed = False
        self._ids = itertools.count(1)
        self._reader = threading.Thread(target=self._read, name="apply-child-inbox", daemon=True)
        self._reader.start()

    # --- the inbox ---------------------------------------------------------------------------

    def _read(self) -> None:
        while True:
            try:
                message = self._inbox.recv()
            except (EOFError, OSError, ValueError):
                break
            if not isinstance(message, dict):
                continue
            op = message.get("op")
            if op == OP_CANCEL:
                self._cancel.set()
            elif op in (OP_REPLAN_REPLY, OP_HAND_OVER_REPLY):
                with self._arrived:
                    self._replies[int(message.get("id", 0))] = message
                    self._arrived.notify_all()
        # The runner went away (it closed its end, or died): nobody is left to hear this run, so stop it.
        self._cancel.set()
        with self._arrived:
            self._closed = True
            self._arrived.notify_all()

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
            self._send({"op": OP_HAND_OVER, "id": ident})
        except (OSError, ValueError):
            return False
        reply = self._wait_for(ident, self._timeouts.reply_s)
        return bool(reply is not None and reply.get("ok") is True)

    def send_result(self, result: RunResult) -> None:
        self._send({"op": OP_RESULT, "result": result})

    def send_error(self, exc: BaseException) -> None:
        """The exception's type name only: its message may hold a value."""
        self._send({"op": OP_ERROR, "error": type(exc).__name__})

    def close(self) -> None:
        for end in (self._outbox, self._inbox):
            try:
                end.close()
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
        channel = ChildChannel(inbox, outbox, job.timeouts)
        agent = factory(
            mode=job.mode, run_id=job.run_id, screenshot_dir=Path(job.screenshot_dir) if job.screenshot_dir else None,
            timeouts=job.timeouts, on_progress=channel.progress, heartbeat=channel.heartbeat,
        )
        with agent:
            result = agent.run(
                job.plan, page_url=job.page_url, schema=job.schema, files=job.files, lookup=job.lookup,
                replan=channel.replan, hand_over=channel.hand_over, cancelled=channel.cancelled,
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
