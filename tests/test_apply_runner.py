"""Apply for me's runner: the child process, the pipe, the watchdog, the single slot, and the views of a run's row.

No browser and no network. The agents here are fakes (tests/apply_fake_ats.py): a canned rehearsal, one that hangs
after starting a grandchild process, one that raises. What is proven is the machinery around the agent: that a run in a
spawned process comes back, that a deadline kills the child and everything it started, that a stop is heard, that an
exception is reported by its type name only, that the row a run leaves is value-free and complete, and that a
rehearsal or a lookup never touches an application.
"""

import hashlib
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import threading
import types
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import realdata_guard

realdata_guard.install()

from opportunity_app.apply import agent as apply_agent, checks as apply_checks, policy as apply_policy, preflight as apply_preflight, runner as apply_runner, runs as apply_runs
from opportunity_app.apply import runner_child as apply_runner_child
from opportunity_app.apply import security_code as apply_security_code
from opportunity_app.apply.agent_types import AgentJob, ApplyTimeouts, RunResult
from opportunity_app.apply.agent_types import OP_CANCEL, OP_HAND_OVER, OP_HAND_OVER_REPLY
from opportunity_app.apply.runner import ApplyRunner, RunnerBusy, RunRefused, SupervisorHandlers, deadline_for, supervise
from opportunity_app.core.database import connect_product
from opportunity_app.core.timestamps import utc_now
from opportunity_app.student.profile import update_profile

from apply_fake_ats import (
    CrashingAgentFactory, FakeApplyAgentFactory, FakeSchemaClient, HangingAgentFactory, JOB_URL, STOPPED_TEXT, canned_png,
)
from helpers_platform import build_and_migrate
from test_apply_api import seed_student

USER = "local-user"
ACME = "job-a"
GRACE = 5.0


def job(**overrides):
    plan = apply_policy.Plan(
        fields=[apply_policy.PlanField(key="first_name", question="First Name", control="text", required=True, disposition="fill")],
        problems=[], plan_hash="hash-1",
    )
    values = dict(run_id="run-" + "a" * 32, mode="rehearse", page_url="https://job-boards.greenhouse.io/x/jobs/1", plan=plan,
                  schema=[], files={}, lookup=None, screenshot_dir="")
    values.update(overrides)
    return AgentJob(**values)


class Recorder:
    """Handlers that write down what the child sent."""

    def __init__(self, plan="a plan", fail_replan=False, grant=False):
        self.progress, self.beats, self.ticks, self.scans = [], 0, 0, []
        self.plan, self.fail_replan, self.grant = plan, fail_replan, grant

    def handlers(self):
        def replan(scan, uploads):
            self.scans.append((scan, uploads))
            if self.fail_replan:
                raise ValueError("a message that must not cross")
            return self.plan

        def beat():
            self.beats += 1

        def tick():
            self.ticks += 1

        return SupervisorHandlers(progress=lambda step, text: self.progress.append((step, text)), heartbeat=beat, replan=replan,
                                  hand_over=lambda: self.grant, tick=tick)


def run_supervised(factory, *, deadline_s=60, cancel=None, handlers=None, grace=GRACE, **kwargs):
    return supervise(factory, kwargs.pop("job", job()), deadline_s=deadline_s, handlers=handlers or Recorder().handlers(),
                     cancel=cancel or threading.Event(), cancel_grace_s=grace, poll_s=0.05, **kwargs)


def wait_until(check, seconds=10.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.1)
    return check()


class ProbeAgentFactory:
    """A thread-isolated agent that uses the pipe the way the real one does: replan, hand_over, heartbeat, cancelled."""

    isolation = "thread"

    def __init__(self):
        self.seen = {}

    def available(self):
        return ""

    def __call__(self, **kwargs):
        factory = self

        class Agent:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def run(self, plan, *, page_url, schema, files, lookup=None, replan=None, hand_over=None, cancelled=None):
                from opportunity_app.apply.runner_child import ReplanFailed

                kwargs["on_progress"]("read", "Reading the form")
                try:
                    factory.seen["plan"] = replan([{"name": "first_name"}], True)
                except ReplanFailed:
                    factory.seen["plan"] = "REPLAN_FAILED"
                factory.seen["hand_over"] = hand_over()
                kwargs["heartbeat"]()
                time.sleep(0.4)
                return RunResult("rehearsed", ["probe"])

        return Agent()


class QuickAgentFactory:
    """A thread agent that reports one step and returns at once: its result is in the pipe before the supervisor reads the step."""

    isolation = "thread"

    def available(self):
        return ""

    def __call__(self, **kwargs):
        class Agent:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def run(self, plan, **more):
                kwargs["on_progress"]("read", "Reading the form")
                return RunResult("rehearsed", ["finished in time"])

        return Agent()


def when_file_exists(path, then):
    """Call ``then()`` from a thread once ``path`` exists (the tree under test has started), or give up after a minute."""
    def watch():
        end = time.monotonic() + 60
        while time.monotonic() < end and not Path(path).exists():
            time.sleep(0.05)
        then()

    threading.Thread(target=watch, daemon=True).start()


def read_pids(path):
    return [int(part) for part in Path(path).read_text().split()]


class SuperviseTests(unittest.TestCase):
    def test_a_thread_run_comes_back_with_its_result_and_its_steps_in_order(self):
        recorder = Recorder()
        outcome = run_supervised(FakeApplyAgentFactory(step_delay=0), handlers=recorder.handlers())
        self.assertEqual((outcome.stop, outcome.error, outcome.killed_pids), ("", "", []))
        self.assertEqual(outcome.result.outcome, "rehearsed")
        self.assertEqual([step for step, _ in recorder.progress], ["open", "read", "fill", "check", "picture"])
        self.assertEqual(dict(recorder.progress)["fill"], "Filling 1 fields")
        self.assertGreaterEqual(recorder.beats, 4)

    def test_a_spawned_process_run_comes_back_the_same_way(self):
        recorder = Recorder()
        outcome = run_supervised(FakeApplyAgentFactory(isolation="process", step_delay=0), handlers=recorder.handlers(), deadline_s=90)
        self.assertEqual(outcome.stop, "", outcome)
        self.assertEqual(outcome.result.outcome, "rehearsed")
        self.assertEqual(outcome.result.plan[0]["key"], "first_name", "the job's plan was pickled into the child")
        self.assertEqual([step for step, _ in recorder.progress], ["open", "read", "fill", "check", "picture"])

    def test_the_deadline_kills_the_child_and_everything_it_started(self):
        with tempfile.TemporaryDirectory() as folder:
            pid_file = Path(folder) / "pids.txt"
            started = time.monotonic()
            outcome = run_supervised(HangingAgentFactory(str(pid_file)), deadline_s=8)
            self.assertLess(time.monotonic() - started, 30)
            self.assertEqual((outcome.stop, outcome.result), ("deadline", None))
            self.assertTrue(pid_file.exists(), "the child started its grandchild before the deadline")
            child, grandchild = (int(part) for part in pid_file.read_text().split())
            self.assertIn(child, outcome.killed_pids)
            for pid in (child, grandchild):
                self.assertTrue(wait_until(lambda pid=pid: not apply_runner.process_alive(pid), 10), f"process {pid} is still running")

    def test_a_stop_the_agent_hears_ends_the_run_before_the_grace(self):
        cancel = threading.Event()
        threading.Timer(0.4, cancel.set).start()
        started = time.monotonic()
        outcome = run_supervised(FakeApplyAgentFactory(hang=True), cancel=cancel, grace=30)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(outcome.stop, "")
        self.assertEqual((outcome.result.outcome, outcome.result.reasons), ("failed", [STOPPED_TEXT]))

    def test_a_stop_the_agent_does_not_hear_is_followed_by_a_kill_after_the_grace(self):
        with tempfile.TemporaryDirectory() as folder:
            pid_file = Path(folder) / "pids.txt"
            cancel = threading.Event()
            when_file_exists(pid_file, cancel.set)   # the stop comes once the tree is running, so the whole tree has to be ended
            outcome = run_supervised(HangingAgentFactory(str(pid_file)), cancel=cancel, grace=2, deadline_s=60)
            self.assertEqual((outcome.stop, outcome.result), ("cancelled", None))
            child, grandchild = read_pids(pid_file)
            self.assertIn(child, outcome.killed_pids, "the child was killed by this stop")
            for pid in (child, grandchild):
                self.assertTrue(wait_until(lambda pid=pid: not apply_runner.process_alive(pid), 10), f"process {pid} is still running")

    def test_abort_kills_a_tree_that_is_running(self):
        with tempfile.TemporaryDirectory() as folder:
            pid_file = Path(folder) / "pids.txt"
            abort = threading.Event()
            when_file_exists(pid_file, abort.set)
            outcome = run_supervised(HangingAgentFactory(str(pid_file)), abort=abort, deadline_s=90, grace=60)
            self.assertEqual(outcome.stop, "cancelled")
            child, grandchild = read_pids(pid_file)
            self.assertIn(child, outcome.killed_pids)
            for pid in (child, grandchild):
                self.assertTrue(wait_until(lambda pid=pid: not apply_runner.process_alive(pid), 10), f"process {pid} is still running")

    def test_abort_right_after_the_spawn_still_kills_the_child_before_it_has_its_own_process_group(self):
        # The child has not unpickled its job, let alone called setsid: only a kill by pid reaches it on POSIX.
        abort = threading.Event()
        threading.Timer(0.2, abort.set).start()
        started = time.monotonic()
        outcome = run_supervised(FakeApplyAgentFactory(hang=True, isolation="process"), abort=abort, deadline_s=90, grace=60)
        elapsed = time.monotonic() - started
        self.assertEqual(outcome.stop, "cancelled")
        self.assertTrue(outcome.killed_pids, "nothing was targeted")
        for pid in outcome.killed_pids:
            self.assertTrue(wait_until(lambda pid=pid: not apply_runner.process_alive(pid), 10), f"process {pid} is still running")
        self.assertLess(elapsed, 9, "the supervisor sat out the join's ten seconds: the kill did not reach the child")

    def test_a_result_already_waiting_when_the_deadline_comes_is_kept(self):
        slept = []

        def slow_progress(step, text):
            slept.append(step)
            time.sleep(1.3)   # a busy database: the handler outlasts the 1 s deadline while the child's result waits in the pipe

        handlers = SupervisorHandlers(progress=slow_progress)
        outcome = run_supervised(QuickAgentFactory(), deadline_s=1.0, handlers=handlers)
        self.assertEqual(slept, ["read"])
        self.assertEqual((outcome.stop, outcome.error), ("", ""))
        self.assertEqual((outcome.result.outcome, outcome.result.reasons), ("rehearsed", ["finished in time"]))

    def test_an_isolation_that_is_not_exactly_process_or_thread_is_refused_and_only_thread_stays_in_the_server(self):
        for word in ("Process", "proc", "", "THREAD", "none"):
            with self.subTest(isolation=word):
                with self.assertRaises(ValueError):
                    run_supervised(FakeApplyAgentFactory(isolation=word))
        self.assertEqual(run_supervised(FakeApplyAgentFactory(step_delay=0)).result.outcome, "rehearsed")

    def test_a_thread_agent_is_asked_to_stop_at_the_deadline_instead_of_lingering(self):
        ended, sent = threading.Event(), []

        class Loops:
            isolation = "thread"

            def available(self):
                return ""

            def __call__(self, **kwargs):
                class Agent:
                    def __enter__(self):
                        return self

                    def __exit__(self, *exc):
                        return None

                    def run(self, plan, *, cancelled=None, **more):
                        while not cancelled():
                            time.sleep(0.02)
                        ended.set()
                        return RunResult("failed", ["stopped"])

                return Agent()

        real = apply_runner._answer

        def record(pipe, message):
            sent.append(dict(message))
            return real(pipe, message)

        with mock.patch.object(apply_runner, "_answer", record):
            outcome = run_supervised(Loops(), deadline_s=1)
        self.assertEqual((outcome.stop, outcome.result, outcome.killed_pids), ("deadline", None, []))
        self.assertIn({"op": OP_CANCEL}, sent, "the supervisor asked the agent to stop, and did not rely on closing the pipe")
        self.assertTrue(ended.wait(5), "the agent's thread ended, so it is not left running behind the abandoned run")

    def test_a_result_already_waiting_when_the_server_is_stopped_is_kept(self):
        abort = threading.Event()

        def slow_progress(step, text):
            time.sleep(0.5)   # a busy database: the child's result waits in the pipe while the server's shutdown sets abort
            abort.set()

        outcome = run_supervised(QuickAgentFactory(), abort=abort, deadline_s=60, handlers=SupervisorHandlers(progress=slow_progress))
        self.assertEqual((outcome.stop, outcome.error), ("", ""))
        self.assertEqual((outcome.result.outcome, outcome.result.reasons), ("rehearsed", ["finished in time"]))

    def test_a_stop_pressed_just_before_the_deadline_is_the_students_stop_and_not_the_deadline(self):
        sleeper = threading.Event()

        class SlowToStop:
            isolation = "thread"

            def available(self):
                return ""

            def __call__(self, **kwargs):
                class Agent:
                    def __enter__(self):
                        return self

                    def __exit__(self, *exc):
                        return None

                    def run(self, plan, **more):
                        sleeper.wait(6)   # closing the browser takes longer than the time that is left
                        return RunResult("failed", ["stopped"])

                return Agent()

        self.addCleanup(sleeper.set)
        cancel = threading.Event()
        threading.Timer(0.4, cancel.set).start()
        outcome = run_supervised(SlowToStop(), cancel=cancel, deadline_s=1.0, grace=30)
        self.assertEqual((outcome.stop, outcome.result), ("cancelled", None))

    def test_a_crash_is_reported_by_its_type_name_and_never_its_message(self):
        for isolation in ("thread", "process"):
            with self.subTest(isolation=isolation):
                outcome = run_supervised(CrashingAgentFactory(isolation), deadline_s=90)
                self.assertEqual((outcome.stop, outcome.error, outcome.result), ("error", "RuntimeError", None))
                self.assertNotIn("boom", repr(outcome))

    def test_replan_and_hand_over_cross_the_pipe(self):
        probe = ProbeAgentFactory()
        recorder = Recorder(plan="the plan", grant=True)
        outcome = run_supervised(probe, handlers=recorder.handlers())
        self.assertEqual(outcome.result.reasons, ["probe"])
        self.assertEqual(probe.seen, {"plan": "the plan", "hand_over": True})
        self.assertEqual(recorder.scans, [([{"name": "first_name"}], True)])

    def test_a_planner_that_fails_tells_the_child_nothing_but_that_it_failed(self):
        probe = ProbeAgentFactory()
        recorder = Recorder(fail_replan=True)
        run_supervised(probe, handlers=recorder.handlers())
        self.assertEqual(probe.seen["plan"], "REPLAN_FAILED")
        self.assertFalse(probe.seen["hand_over"], "nothing is handed over unless the parent says so")

    def test_the_tick_keeps_the_heartbeat_going_while_the_agent_is_quiet(self):
        recorder = Recorder()
        run_supervised(ProbeAgentFactory(), handlers=recorder.handlers(), tick_s=0.1)
        self.assertGreaterEqual(recorder.ticks, 2)

    def test_the_deadlines_by_kind(self):
        self.assertEqual((deadline_for("lookup"), deadline_for("rehearsal")), (120.0, 300.0))
        self.assertEqual(deadline_for("submit"), 300 + 5 * 60 + 10 * 60)
        # The fill, the student's turn, one budget for everything after the press, three outcome windows, two minutes.
        self.assertEqual(deadline_for("handoff"), 300 + 1200 + 1200 + 90 + 120)
        self.assertEqual(deadline_for("handoff"), 2910.0)
        with self.assertRaises(ValueError):
            deadline_for("nonsense")


ORPHANED_PARENT = """
import sys
from pathlib import Path

sys.path[:0] = [{tests!r}, {root!r}]

if __name__ == "__main__":
    import threading
    from apply_fake_ats import HangingAgentFactory
    from opportunity_app.apply.agent_types import ApplyTimeouts
    from opportunity_app.apply.runner import SupervisorHandlers, supervise
    from test_apply_runner import job

    supervise(
        HangingAgentFactory(sys.argv[1], driver=True), job(timeouts=ApplyTimeouts(orphan_s=2.0)), deadline_s=600,
        handlers=SupervisorHandlers(progress=lambda step, text: None), cancel=threading.Event(), poll_s=0.05,
    )
"""


class StopTimeDrainTests(unittest.TestCase):
    """M5a reads a result already waiting in the pipe when the deadline or the server's shutdown stops a run; a hand-over asked for in
    that same moment must not be committed, since the child is about to be killed (part 2's Finish in browser)."""

    def test_a_hand_over_waiting_in_the_pipe_at_the_stop_is_refused_without_asking_the_handler(self):
        outbox_recv, outbox_send = multiprocessing.Pipe(duplex=False)
        inbox_recv, inbox_send = multiprocessing.Pipe(duplex=False)
        asked = []
        handlers = SupervisorHandlers(hand_over=lambda: asked.append(True) or True)
        outbox_send.send({"op": OP_HAND_OVER, "id": 7, "expires": time.monotonic() + 10})
        outbox_send.send({"op": "result", "result": RunResult("needs_you", ["late"])})
        late = apply_runner._drain_terminal(outbox_recv, handlers, inbox_send)
        self.assertIsNotNone(late)
        self.assertEqual(late[0].reasons, ["late"], "the result that was already waiting is kept")
        self.assertEqual(asked, [], "the hand-over was asked of the handler while the run was being stopped")
        self.assertTrue(inbox_recv.poll(1))
        self.assertEqual(inbox_recv.recv(), {"op": OP_HAND_OVER_REPLY, "id": 7, "ok": False})


class OrphanedChildTests(unittest.TestCase):
    """The watchdog lives in the server. A child that outlives the server (or its deadline) must end itself, whatever its page is doing."""

    def started_pids(self, pid_file):
        self.assertTrue(wait_until(lambda: pid_file.exists() and len(pid_file.read_text().split()) == 2, 60), "the child never started its grandchild")
        return read_pids(pid_file)

    def test_a_child_whose_server_is_killed_hard_ends_itself_and_what_it_started(self):
        with tempfile.TemporaryDirectory() as folder:
            pid_file = Path(folder) / "pids.txt"
            script = Path(folder) / "parent.py"
            script.write_text(ORPHANED_PARENT.format(tests=str(Path(__file__).resolve().parent), root=str(Path(__file__).resolve().parent.parent)), encoding="utf-8")
            parent = subprocess.Popen([sys.executable, str(script), str(pid_file)])
            pids = []
            try:
                pids = self.started_pids(pid_file)
                parent.kill()   # Stop-Process, a crash or launchd's SIGKILL: no shutdown handler runs, so the watchdog is gone
                parent.wait(30)
                for pid in pids:
                    self.assertTrue(wait_until(lambda pid=pid: not apply_runner.process_alive(pid), 30), f"process {pid} is still running after its server was killed")
            finally:
                parent.kill()
                for pid in pids:
                    if apply_runner.process_alive(pid):
                        apply_runner.kill_tree(pid)

    def test_a_child_ends_itself_when_its_own_deadline_has_passed_even_if_nobody_stopped_it(self):
        with tempfile.TemporaryDirectory() as folder:
            pid_file = Path(folder) / "pids.txt"
            context = multiprocessing.get_context("spawn")
            inbox_recv, inbox_send = context.Pipe(duplex=False)
            outbox_recv, outbox_send = context.Pipe(duplex=False)   # both ends stay open here: this child is not orphaned, only late
            mine = job(timeouts=ApplyTimeouts(orphan_s=2.0), deadline_s=3.0)
            child = context.Process(target=apply_runner_child.child_main, args=(HangingAgentFactory(str(pid_file), driver=True), mine, inbox_recv, outbox_send, True), daemon=True)
            child.start()
            try:
                pids = self.started_pids(pid_file)
                child.join(40)
                self.assertFalse(child.is_alive(), "the child outlived its deadline and the grace")
                self.assertEqual(child.exitcode, apply_runner_child.ORPHAN_EXIT_CODE)
                self.assertTrue(wait_until(lambda: not apply_runner.process_alive(pids[1]), 30), "what the child started outlived it")
            finally:
                if child.is_alive() and child.pid:
                    apply_runner.kill_tree(child.pid)
                for end in (inbox_send, outbox_recv):
                    end.close()

    def channel_after_eof(self, *, end_process_when_orphaned):
        """A ChildChannel whose runner end closed: whether it asked to end the process."""
        inbox_recv, inbox_send = multiprocessing.Pipe(duplex=False)
        _outbox_recv, outbox_send = multiprocessing.Pipe(duplex=False)
        with mock.patch.object(apply_runner_child, "_exit_now") as exit_now:
            channel = apply_runner_child.ChildChannel(inbox_recv, outbox_send, ApplyTimeouts(orphan_s=0.2), end_process_when_orphaned=end_process_when_orphaned)
            inbox_send.close()
            self.assertTrue(wait_until(channel.cancelled, 5), "end-of-file on the inbox still sets the cancel flag")
            time.sleep(0.8)
            return exit_now.called

    def test_end_of_file_on_the_inbox_ends_a_process_child_after_the_grace_and_never_a_thread_child(self):
        self.assertTrue(self.channel_after_eof(end_process_when_orphaned=True))
        self.assertFalse(self.channel_after_eof(end_process_when_orphaned=False), "a thread child shares the server's process: ending it would end the server")

    def orphan_grace_after(self, *, handed_over):
        """The grace a process child's channel arms when its runner's pipe closes, after a committed hand-over or before any."""
        inbox_recv, inbox_send = multiprocessing.Pipe(duplex=False)
        outbox_recv, outbox_send = multiprocessing.Pipe(duplex=False)
        with mock.patch.object(apply_runner_child, "_end_process_in") as end_in:
            channel = apply_runner_child.ChildChannel(inbox_recv, outbox_send, ApplyTimeouts(orphan_s=2.0, reply_s=5.0), end_process_when_orphaned=True)
            channel.ends_at = time.monotonic() + 100.0
            if handed_over:
                def parent():
                    asked = outbox_recv.recv()
                    self.assertEqual(asked["op"], OP_HAND_OVER)
                    inbox_send.send({"op": OP_HAND_OVER_REPLY, "id": asked["id"], "ok": True})

                answering = threading.Thread(target=parent, daemon=True)
                answering.start()
                self.assertTrue(channel.hand_over())
                answering.join(5)
            inbox_send.close()
            self.assertTrue(wait_until(lambda: end_in.called, 5), "end-of-file never armed the exit")
            return end_in.call_args.args[0]

    def test_an_orphaned_child_keeps_the_handed_over_window_until_the_agents_cap_and_no_longer(self):
        """M5a ends an orphaned child after orphan_s; once the student's Submit was handed over, the agent keeps the window (part 2)."""
        self.assertEqual(self.orphan_grace_after(handed_over=False), 2.0)
        grace = self.orphan_grace_after(handed_over=True)
        self.assertGreater(grace, 95.0, "the window was cut off while the student may still be pressing Submit")
        self.assertLessEqual(grace, 102.0, "and it ends orphan_s after the agent's own cap, not later")

    def test_a_parent_that_answers_ok_and_dies_at_once_still_hands_the_window_to_the_student(self):
        # The reader thread files the ok and reads end-of-file before the agent's thread wakes to set anything: the filing itself must count.
        inbox_recv, inbox_send = multiprocessing.Pipe(duplex=False)
        _outbox_recv, outbox_send = multiprocessing.Pipe(duplex=False)
        gate = threading.Event()
        real = apply_runner_child.ChildChannel._file

        def file_then_hold(channel, message):
            real(channel, message)
            gate.wait(5)         # the agent's own thread never runs in this test: only the reader has acted

        with mock.patch.object(apply_runner_child, "_end_process_in") as end_in, mock.patch.object(apply_runner_child.ChildChannel, "_file", file_then_hold):
            channel = apply_runner_child.ChildChannel(inbox_recv, outbox_send, ApplyTimeouts(orphan_s=2.0, reply_s=5.0), end_process_when_orphaned=True)
            channel.ends_at = time.monotonic() + 100.0
            inbox_send.send({"op": OP_HAND_OVER_REPLY, "id": 1, "ok": True})
            inbox_send.close()
            gate.set()
            self.assertTrue(wait_until(lambda: end_in.called, 5), "end-of-file never armed the exit")
        self.assertGreater(end_in.call_args.args[0], 95.0, "the window was cut off seconds after the student's POST was continued")

    def test_a_thread_child_never_arms_the_process_exit(self):
        seen = []

        class Spy:
            isolation = "thread"

            def available(self):
                return ""

            def __call__(self, **kwargs):
                class Agent:
                    def __enter__(self_inner):
                        return self_inner

                    def __exit__(self_inner, *exc):
                        return None

                    def run(self_inner, plan, **kw):
                        return RunResult("rehearsed", [])

                return Agent()

        with mock.patch.object(apply_runner_child, "_end_process_in", side_effect=lambda seconds: seen.append(seconds)):
            run_supervised(Spy(), deadline_s=77)
        self.assertEqual(seen, [], "a thread child never arms the process exit")


class LinkProbeFactory:
    """A thread-isolated agent that records whether it was given the handoff link, and what that link says about its deadline."""

    isolation = "thread"

    def __init__(self):
        self.seen = {}

    def available(self):
        return ""

    def __call__(self, **kwargs):
        factory = self

        class Agent:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return None

            def run(self, plan, *, page_url, schema, files, lookup=None, replan=None, hand_over=None, cancelled=None, link=None):
                factory.seen.update(mode=kwargs["mode"], link=type(link).__name__, ends_at=getattr(link, "ends_at", None), at=time.monotonic())
                return RunResult("needs_you", ["probe"], evidence={"handoff_end": "stopped", "browser_closed": True})

        return Agent()


class HandoffWiringTests(unittest.TestCase):
    """What a Finish in browser child is given, and the arithmetic of how long it may take (spec 6.0, 4.6)."""

    def test_only_a_handoff_child_is_given_the_link_and_it_carries_the_cap_on_every_wait(self):
        for mode, expected in (("handoff", "ChildChannel"), ("rehearse", "NoneType"), ("lookup", "NoneType")):
            with self.subTest(mode=mode):
                factory = LinkProbeFactory()
                outcome = run_supervised(factory, job=job(mode=mode, ends_at=time.monotonic() + 1234.0))
                self.assertEqual(outcome.stop, "")
                self.assertEqual(factory.seen["link"], expected)
                if mode == "handoff":
                    self.assertAlmostEqual(factory.seen["ends_at"] - factory.seen["at"], 1234.0, delta=5)

    def test_the_deadline_is_the_sum_of_every_wait_and_the_agents_longest_path_ends_a_minute_before_it(self):
        timeouts = ApplyTimeouts()
        self.assertEqual(deadline_for("handoff"), timeouts.fill_s + timeouts.handoff_s + timeouts.after_hand_over_s + 3 * timeouts.outcome_s + 120.0)
        longest = timeouts.fill_s + timeouts.handoff_s + timeouts.after_hand_over_s + 3 * timeouts.outcome_s
        self.assertLess(longest, deadline_for("handoff") - 60)
        self.assertEqual(timeouts.after_hand_over_s, timeouts.code_read_s + timeouts.security_code_s)
        small = ApplyTimeouts(fill_s=10, handoff_s=20, code_read_s=5, security_code_s=5, outcome_s=2)
        self.assertEqual(deadline_for("handoff", small), 10 + 20 + 10 + 6 + 120)

    def test_the_timeouts_equal_the_reader_constants_of_the_security_code_module(self):
        timeouts = ApplyTimeouts()
        self.assertEqual(timeouts.code_read_s, apply_security_code.CODE_WINDOW.total_seconds())
        self.assertEqual(timeouts.code_poll_s, apply_security_code.POLL_EVERY.total_seconds())
        self.assertEqual(timeouts.code_reply_s, apply_security_code.REPLY_TIMEOUT_S)
        self.assertLessEqual(timeouts.heartbeat_s, 30, "spec 5.2 rule 4: every wait loop beats at least every 30 seconds")
        self.assertLess(timeouts.heartbeat_s, apply_runner.HELD_HEARTBEAT.total_seconds(), "and well inside the time a claim stays held")

    def test_the_runner_stamps_ends_at_a_minute_before_its_deadline(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _, path = build_and_migrate(root)
            conn = connect_product(path)
            try:
                seed_student(conn, root / "resumes")
                with conn:
                    conn.execute("UPDATE opportunities SET url=? WHERE id=?", (JOB_URL, ACME))
                factory = LinkProbeFactory()
                runner = ApplyRunner(cancel_grace_s=GRACE)
                before = time.monotonic()
                run_id = runner.start(
                    conn, database_target=path, user_id=USER, opportunity_id=ACME, kind="handoff", agent_factory=factory,
                    schema_client=FakeSchemaClient(any_job=True), apply_root=root / "apply", resume_root=root / "resumes", posting_confirmed=True,
                )
                self.assertTrue(runner.wait(run_id, 60))
                self.assertAlmostEqual(factory.seen["ends_at"] - before, deadline_for("handoff") - 60, delta=30)
                self.assertEqual(factory.seen["mode"], "handoff")
                self.assertEqual(runner.deadline_s("handoff"), 2910.0)
                self.assertEqual(ApplyRunner(deadlines={"handoff": 100}).deadline_s("handoff"), 100.0)
            finally:
                conn.close()


class TreeTests(unittest.TestCase):
    def test_kill_tree_ends_a_process_and_process_alive_sees_it(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.addCleanup(child.kill)
        self.assertTrue(apply_runner.process_alive(child.pid))
        self.assertIn(child.pid, apply_runner.kill_tree(child.pid))
        child.wait(timeout=20)
        self.assertTrue(wait_until(lambda: not apply_runner.process_alive(child.pid), 10))

    @unittest.skipIf(os.name == "nt", "POSIX only: Windows reads the process table instead")
    def test_without_proc_an_unreaped_zombie_is_not_alive_and_ps_decides(self):
        # macOS has no /proc: a killed child that its parent has not reaped yet still answers os.kill(pid, 0), so ps's state decides.
        def ps(state):
            return mock.patch.object(apply_runner.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout=state, stderr=""))
        with mock.patch("builtins.open", side_effect=FileNotFoundError):
            with ps("Z+"):
                self.assertFalse(apply_runner.process_alive(os.getpid()))
            with ps(""):
                self.assertFalse(apply_runner.process_alive(os.getpid()))
            with ps("S+"):
                self.assertTrue(apply_runner.process_alive(os.getpid()))

    def test_descendants_lists_a_grandchild_and_kill_tree_ends_it_too(self):
        script = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)']); time.sleep(600)"
        child = subprocess.Popen([sys.executable, "-c", script], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.addCleanup(child.kill)
        self.assertTrue(wait_until(lambda: bool(apply_runner.descendants(child.pid)), 30))
        killed = apply_runner.kill_tree(child.pid)
        child.wait(timeout=20)
        self.assertGreaterEqual(len(killed), 2, "the grandchild is among the pids targeted, on Windows as well")
        self.assertEqual(killed[0], child.pid)
        self.assertTrue(all(wait_until(lambda pid=pid: not apply_runner.process_alive(pid), 10) for pid in killed))

    def test_on_windows_closing_never_uses_taskkill_by_tree_only_by_each_verified_pid(self):
        # taskkill /T walks by parent pid alone: a freed pid named as the parent of an unrelated older process would take that process too.
        calls = []
        with mock.patch.object(apply_runner, "os", types.SimpleNamespace(name="nt")),                 mock.patch.object(apply_runner, "descendants", return_value=[300, 200]),                 mock.patch.object(apply_runner.subprocess, "run", side_effect=lambda command, **_kw: calls.append(list(command))):
            killed = apply_runner.kill_tree(100)
        self.assertEqual(killed, [100, 300, 200])
        self.assertTrue(all("/T" not in command for command in calls), calls)
        self.assertEqual(calls, [["taskkill", "/F", "/PID", str(pid)] for pid in (300, 200, 100)], "each verified pid by itself, the child last")

    @unittest.skipUnless(os.name == "nt", "Windows only: POSIX lists processes with ps")
    def test_on_windows_the_process_table_comes_from_a_snapshot_and_not_a_powershell_query(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.addCleanup(child.kill)
        with mock.patch.object(apply_runner.subprocess, "run", side_effect=AssertionError("a process was started to list processes")):
            started = time.monotonic()
            below = apply_runner.descendants(os.getpid())
            took = time.monotonic() - started
        self.assertIn(child.pid, below)
        self.assertLess(took, 1.0, "listing the tree is no longer a second-long query")

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_the_slow_listing_is_bounded_when_the_snapshot_is_not_there(self):
        calls = []

        def run(command, **kwargs):
            calls.append(kwargs.get("timeout"))
            return mock.Mock(stdout="")

        with mock.patch.object(apply_runner, "_windows_process_table", side_effect=OSError("no kernel32")), mock.patch.object(apply_runner.subprocess, "run", run):
            apply_runner.descendants(os.getpid())
        self.assertEqual(calls, [apply_runner.CIM_TIMEOUT_S])
        self.assertLessEqual(apply_runner.CIM_TIMEOUT_S, 10)

    def test_a_pid_that_is_not_running_is_not_alive(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(timeout=20)
        self.assertTrue(wait_until(lambda: not apply_runner.process_alive(child.pid), 10))


class RunnerCase(unittest.TestCase):
    """A throwaway database with a confirmed student, the saved Acme role made a Greenhouse role, and a runner."""

    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.root = Path(tempdir.name)
        _, self.path = build_and_migrate(self.root)
        self.conn = connect_product(self.path)
        self.addCleanup(self.conn.close)
        seed_student(self.conn, self.root / "resumes")
        with self.conn:
            self.conn.execute("UPDATE opportunities SET url=? WHERE id=?", (JOB_URL, ACME))
        self.apply_root = self.root / "apply"
        self.schema = FakeSchemaClient(any_job=True)
        self.runner = ApplyRunner(cancel_grace_s=GRACE)
        self.addCleanup(self.runner.shutdown, 10)

    def start(self, factory=None, *, kind="rehearsal", runner=None, opportunity_id=ACME, **kwargs):
        return (runner or self.runner).start(
            self.conn, database_target=self.path, user_id=USER, opportunity_id=opportunity_id, kind=kind,
            agent_factory=factory or FakeApplyAgentFactory(step_delay=0), schema_client=kwargs.pop("schema_client", self.schema),
            apply_root=self.apply_root, resume_root=self.root / "resumes", posting_confirmed=kwargs.pop("posting_confirmed", True), **kwargs,
        )

    def finish(self, run_id, runner=None):
        self.assertTrue((runner or self.runner).wait(run_id, 60), "the run did not finish")
        return dict(self.conn.execute("SELECT * FROM apply_runs WHERE id=?", (run_id,)).fetchone())

    def counts(self, *tables):
        return {name: self.conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in tables}

    def health(self):
        row = self.conn.execute("SELECT * FROM automation_health WHERE user_id=? AND component=?", (USER, apply_runs.RUNNER_COMPONENT)).fetchone()
        return None if row is None else dict(row)


class RunTests(RunnerCase):
    def test_a_rehearsal_runs_and_leaves_a_complete_value_free_row(self):
        run_id = self.start()
        row = self.finish(run_id)
        self.assertEqual((row["status"], row["outcome"], row["kind"]), ("finished", "rehearsed", "rehearsal"))
        plan = json.loads(row["plan_json"])
        self.assertTrue(plan and row["plan_hash"])
        expected = apply_checks.clean_rehearsal({"outcome": "rehearsed", "plan": plan, "join_problems": [], "check_problems": []})
        self.assertEqual(bool(row["clean"]), expected)
        self.assertFalse(expected, "the fictional form's required questions have no saved answers, so the rehearsal is not clean")
        steps = [item["step"] for item in json.loads(row["progress_json"])]
        self.assertEqual(steps, ["start", "open", "read", "fill", "check", "picture"])
        shots = json.loads(row["screenshots_json"])
        self.assertEqual(len(shots), 1)
        relative = Path(shots[0]["path"])
        self.assertFalse(relative.is_absolute())
        self.assertEqual(relative.parts[:2], (apply_runs.user_folder(USER), ACME))
        stored = self.apply_root / relative
        self.assertEqual(hashlib.sha256(stored.read_bytes()).hexdigest(), shots[0]["sha256"])
        self.assertEqual(stored.read_bytes(), canned_png())
        self.assertEqual(json.loads(row["refused_json"])[0]["rule"], "non_get")
        evidence = json.loads(row["evidence_json"])
        self.assertEqual((evidence["refused_total"], evidence["join_problems"], evidence["check_problems"]), (1, [], []))
        for secret in ("Sam", "Rivera", "sam.rivera@example.test"):
            self.assertNotIn(secret, json.dumps({key: row[key] for key in row}), "the row holds no student value")
        health = self.health()
        self.assertEqual(json.loads(health["detail_json"]), {"busy_since": None, "run_id": run_id, "last_outcome": "rehearsed"})
        self.assertTrue(health["last_ok_at"] and not health["last_error"])
        self.assertIsNone(self.runner.busy())

    def test_a_posting_that_does_not_look_like_the_saved_role_is_not_rehearsed_until_the_student_says_it_is_theirs(self):
        # The fictional board answers every role with Example Robotics' listing, so the saved Acme role differs from it.
        with self.assertRaises(RunRefused) as caught:
            self.start(posting_confirmed=False)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(caught.exception.message, "Check the posting first. Greenhouse's form is for Robotics Software Intern at Example Robotics, not Acme Robotics")
        self.assertEqual(self.counts("apply_runs")["apply_runs"], 0, "a refusal writes no row and uses none of the day's rehearsals")
        self.assertIsNone(self.runner.busy())
        # A lookup only reads a list, and is not filed under a company.
        self.assertEqual(self.finish(self.start(kind="lookup", lookup_key="location_city", lookup_text="Spr", posting_confirmed=False))["outcome"], "looked_up")

    def test_a_confirmed_posting_is_named_in_the_row_and_in_the_summary_as_the_listing_it_was(self):
        row = self.finish(self.start())
        posting = json.loads(row["evidence_json"])["posting"]
        self.assertEqual(posting, {"title": "Robotics Software Intern", "company": "Example Robotics", "differs": True, "confirmed": True})
        view = apply_runner.run_view(self.conn, row)
        self.assertTrue(view["summary"].startswith("Here is what the app would send to Example Robotics for Robotics Software Intern."), view["summary"])

    def test_a_posting_that_matches_the_saved_role_needs_no_word_from_the_student(self):
        with self.conn:
            self.conn.execute("UPDATE opportunities SET company='Example Robotics, Inc.', title='Robotics Intern' WHERE id=?", (ACME,))
        row = self.finish(self.start(posting_confirmed=False))
        self.assertEqual(row["outcome"], "rehearsed")
        self.assertEqual(json.loads(row["evidence_json"])["posting"]["differs"], False)

    def test_a_start_that_fails_after_its_row_was_written_closes_the_row_and_frees_the_slot(self):
        with mock.patch.object(apply_runner, "AgentJob", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.start()
        self.assertIsNone(self.runner.busy())
        rows = self.conn.execute("SELECT status, outcome, reasons_json FROM apply_runs").fetchall()
        self.assertEqual([(row["status"], row["outcome"], json.loads(row["reasons_json"])) for row in rows], [("finished", "failed", [apply_runner.NOT_STARTED])])
        self.assertEqual(self.finish(self.start())["outcome"], "rehearsed")

    def test_a_folder_that_cannot_be_made_refuses_the_start_before_any_row_is_written(self):
        apply_policy.mac_key(self.apply_root)   # the key file's folder exists, so the one that fails is the run's own
        folder = self.apply_root / apply_runs.user_folder(USER) / ACME
        real_mkdir, reached = Path.mkdir, []

        def mkdir(path, *args, **kwargs):
            if Path(path) == folder:
                reached.append(path)
                raise OSError("read-only")
            return real_mkdir(path, *args, **kwargs)

        with mock.patch.object(Path, "mkdir", mkdir):
            with self.assertRaises(OSError):
                self.start()
        self.assertEqual(reached, [folder], "the failure came from the run's folder, not from something earlier")
        self.assertEqual(self.counts("apply_runs")["apply_runs"], 0)
        self.assertIsNone(self.runner.busy())

    def test_a_start_is_refused_while_the_students_account_is_being_deleted_and_works_again_after(self):
        running = self.start(FakeApplyAgentFactory(hang=True))
        with self.runner.held_for(USER, timeout=30):
            self.assertEqual(self.finish(running)["outcome"], "failed", "their run was ended when the hold began")
            before = sorted(path.name for path in (self.apply_root / apply_runs.user_folder(USER)).rglob("*"))
            self.assertIsNone(self.runner.busy(), "the slot is free, but nothing of theirs may use it")
            with self.assertRaises(RunRefused) as caught:
                self.start()
            self.assertEqual((caught.exception.status_code, caught.exception.message), (409, apply_runner.ACCOUNT_ENDING))
            self.assertEqual(self.counts("apply_runs")["apply_runs"], 1, "a refused start wrote no row")
            self.assertEqual(sorted(path.name for path in (self.apply_root / apply_runs.user_folder(USER)).rglob("*")), before, "and made no folder")
        self.assertEqual(self.finish(self.start())["outcome"], "rehearsed", "the hold ended with the block")

    def test_the_hold_is_released_when_the_deletion_fails(self):
        with self.assertRaises(KeyError):
            with self.runner.held_for(USER):
                raise KeyError("the deletion failed")
        self.assertEqual(self.finish(self.start())["outcome"], "rehearsed")

    def test_the_hold_is_only_for_the_student_being_deleted(self):
        with self.runner.held_for("someone-else"):
            self.assertEqual(self.finish(self.start())["outcome"], "rehearsed")

    def test_a_start_racing_a_deletion_either_runs_and_is_ended_or_is_refused_never_left_running(self):
        for _ in range(5):
            started, errors = [], []
            barrier = threading.Barrier(2)

            def racing_start():
                barrier.wait()
                try:
                    started.append(self.start(FakeApplyAgentFactory(hang=True)))
                except (RunRefused, RunnerBusy) as error:
                    errors.append(error)

            thread = threading.Thread(target=racing_start)
            thread.start()
            barrier.wait()
            with self.runner.held_for(USER, timeout=30):
                thread.join(30)
                self.assertIsNone(self.runner.busy(), "nothing of the student's is running inside the hold")
            if started:
                self.assertEqual(self.finish(started[0])["status"], "finished")
            else:
                self.assertTrue(errors)

    def test_the_slot_names_its_student_the_moment_it_is_taken(self):
        class SnapshotLock:
            """A lock that notes whose run the slot holds at the instant it is let go: that is when a deletion that is waiting looks."""

            def __init__(self, runner):
                self.inner, self.runner, self.seen = threading.Lock(), runner, []

            def __enter__(self):
                self.inner.acquire()
                return self

            def __exit__(self, *exc):
                active = self.runner._active
                self.seen.append(None if active is None else active.user_id)
                self.inner.release()

        runner = ApplyRunner()
        runner._lock = SnapshotLock(runner)
        active = runner._reserve(USER)
        self.assertEqual(runner._lock.seen, [USER], "a deletion that looks as the slot is taken must see whose run this is")
        runner._release(active)

    def test_a_console_ctrl_c_that_reaches_the_child_is_the_server_stopping_not_a_browser_that_broke(self):
        class Interrupted:
            isolation = "thread"

            def available(self):
                return ""

            def __call__(self, **kwargs):
                raise KeyboardInterrupt()

        row = self.finish(self.start(Interrupted()))
        self.assertEqual((row["status"], row["outcome"]), ("finished", "failed"))
        self.assertEqual(json.loads(row["reasons_json"])[0], apply_runner.SERVER_STOPPED)
        health = self.health()
        self.assertFalse(health["last_error"], "the runner is not reported as broken")
        self.assertTrue(health["last_ok_at"])

    def test_the_run_is_found_while_it_runs_and_the_row_says_running(self):
        run_id = self.start(FakeApplyAgentFactory(step_delay=0.3))
        self.assertEqual(self.runner.busy(), run_id)
        row = dict(self.conn.execute("SELECT * FROM apply_runs WHERE id=?", (run_id,)).fetchone())
        self.assertEqual((row["status"], row["kind"], row["started_by"]), ("running", "rehearsal", "student"))
        self.assertEqual(self.finish(run_id)["outcome"], "rehearsed")

    def test_a_second_start_while_one_runs_is_refused_and_the_slot_frees_when_it_ends(self):
        first = self.start(FakeApplyAgentFactory(hang=True))
        with self.assertRaises(RunnerBusy) as caught:
            self.start()
        self.assertEqual(str(caught.exception), "Another application is being filled. Wait for it to finish.")
        self.assertEqual(self.counts("apply_runs")["apply_runs"], 1, "a refused start writes no row")
        self.assertFalse(self.runner.cancel("run-" + "0" * 32), "it only stops the run it is running")
        self.assertTrue(self.runner.cancel(first))
        row = self.finish(first)
        self.assertEqual((row["outcome"], json.loads(row["reasons_json"])), ("failed", [STOPPED_TEXT]))
        self.assertIsNone(self.runner.busy())
        self.assertEqual(self.finish(self.start())["outcome"], "rehearsed")

    def test_the_slot_is_free_after_a_refusal(self):
        for client, opportunity, expect in (
            (FakeSchemaClient(closed=True), ACME, "The app couldn't find this posting on Greenhouse. It may be closed"),
            (self.schema, "job-b", "Apply for me works with Greenhouse postings only, for now"),
        ):
            with self.subTest(opportunity=opportunity):
                with self.assertRaises(RunRefused) as caught:
                    self.start(schema_client=client, opportunity_id=opportunity)
                self.assertEqual((caught.exception.status_code, caught.exception.message), (409, expect))
                self.assertIsNone(self.runner.busy())
        self.assertEqual(self.counts("apply_runs")["apply_runs"], 0, "a refusal writes no row and uses none of the day's rehearsals")

    def test_an_unknown_role_is_not_found(self):
        from opportunity_app.applications.actions import OpportunityNotFoundError

        with self.assertRaises(OpportunityNotFoundError):
            self.start(opportunity_id="no-such-role")
        self.assertIsNone(self.runner.busy())

    def test_the_slot_is_free_after_a_crash_and_the_row_never_holds_the_message(self):
        run_id = self.start(CrashingAgentFactory())
        row = self.finish(run_id)
        self.assertEqual((row["outcome"], row["clean"]), ("failed", 0))
        self.assertEqual(json.loads(row["reasons_json"]), [apply_runner.CHILD_DIED])
        self.assertNotIn("boom", json.dumps(row))
        health = self.health()
        self.assertEqual(health["last_error"], "RuntimeError")
        self.assertIsNone(self.runner.busy())
        self.assertEqual(self.finish(self.start())["outcome"], "rehearsed")
        self.assertTrue(self.health()["last_ok_at"])

    def test_a_run_that_takes_too_long_is_stopped_and_says_so(self):
        runner = ApplyRunner(deadlines={"rehearsal": 2}, cancel_grace_s=GRACE)
        self.addCleanup(runner.shutdown, 10)
        row = self.finish(self.start(FakeApplyAgentFactory(hang=True), runner=runner), runner)
        reasons = json.loads(row["reasons_json"])
        self.assertEqual(row["outcome"], "failed")
        self.assertTrue(reasons[0].startswith("The run took longer than") and reasons[0].endswith("No application was sent."), reasons)

    def test_a_stop_pressed_before_the_deadline_is_recorded_as_the_students_stop_when_the_browser_is_slow_to_close(self):
        slow = threading.Event()
        self.addCleanup(slow.set)

        class SlowToStop:
            isolation = "thread"

            def available(self):
                return ""

            def __call__(self, **kwargs):
                class Agent:
                    def __enter__(self):
                        return self

                    def __exit__(self, *exc):
                        return None

                    def run(self, plan, **more):
                        slow.wait(8)
                        return RunResult("failed", ["stopped"])

                return Agent()

        runner = ApplyRunner(deadlines={"rehearsal": 1.5}, cancel_grace_s=30)
        self.addCleanup(runner.shutdown, 10)
        run_id = self.start(SlowToStop(), runner=runner)
        self.assertTrue(runner.cancel(run_id))
        row = self.finish(run_id, runner)
        reasons = json.loads(row["reasons_json"])
        self.assertEqual((row["outcome"], reasons[0]), ("failed", apply_runner.STOPPED), reasons)
        self.assertFalse(any("took longer" in reason for reason in reasons))

    def test_one_students_last_outcome_is_not_written_into_another_students_health_row(self):
        self.assertEqual(self.finish(self.start())["outcome"], "rehearsed")   # the first student's rehearsal ends as 'rehearsed'
        other = "user-two"
        stamp = utc_now()
        data = b"%PDF-1.4 another fictional resume"
        (self.root / "resumes" / "resume-file-2.pdf").write_bytes(data)
        with self.conn:
            self.conn.execute("INSERT INTO users(id, email, display_name, role, created_at, updated_at) VALUES(?, NULL, 'B', 'student', ?, ?)", (other, stamp, stamp))
            self.conn.execute(
                "INSERT INTO resume_files(id, user_id, original_name, media_type, byte_size, sha256, storage_path, created_at) VALUES('resume-file-2', ?, 'Resume.pdf', 'application/pdf', ?, ?, 'resume-file-2.pdf', ?)",
                (other, len(data), hashlib.sha256(data).hexdigest(), stamp))
            self.conn.execute("INSERT INTO resume_versions(id, resume_file_id, user_id, extracted_text, status, created_at, confirmed_at) VALUES('resume-2', 'resume-file-2', ?, 't', 'confirmed', ?, ?)", (other, stamp, stamp))
        update_profile(self.conn, {"name_parts": {"first": "Alex", "last": "Chen", "preferred": ""}, "contact": {"email": "alex.chen@example.test"}},
                       ["name_parts", "contact"], user_id=other)
        run_id = self.runner.start(
            self.conn, database_target=self.path, user_id=other, opportunity_id=ACME, kind="rehearsal", agent_factory=FakeApplyAgentFactory(hang=True),
            schema_client=self.schema, apply_root=self.apply_root, resume_root=self.root / "resumes", posting_confirmed=True,
        )
        def other_health():
            row = self.conn.execute("SELECT detail_json FROM automation_health WHERE user_id=? AND component=?", (other, apply_runs.RUNNER_COMPONENT)).fetchone()
            return None if row is None else json.loads(row[0])

        self.assertTrue(wait_until(lambda: other_health() is not None))
        self.assertEqual(other_health()["last_outcome"], "", "the other student's run is not this student's last outcome")
        self.assertEqual(json.loads(self.health()["detail_json"])["last_outcome"], "rehearsed", "each student keeps their own")
        self.runner.shutdown(30)
        self.assertEqual(other_health()["last_outcome"], "failed")
        self.assertEqual(json.loads(self.health()["detail_json"])["last_outcome"], "rehearsed", "a stop of the other student's run does not touch this one")
        self.assertTrue(run_id)

    def test_a_result_the_row_no_longer_takes_is_not_reported_as_the_outcome(self):
        run_id = self.start(FakeApplyAgentFactory(step_delay=0.5))
        self.assertTrue(wait_until(lambda: self.conn.execute("SELECT progress_json FROM apply_runs WHERE id=?", (run_id,)).fetchone()[0] != "[]"))
        # Another process (a second server on this database, or recover_stale) closed the row first.
        self.assertTrue(apply_runs.finish_run(self.conn, run_id, outcome="failed", reasons=[apply_runner.SERVER_STOPPED]))
        with self.assertLogs(apply_runner.LOGGER, level="WARNING") as logged:
            row = self.finish(run_id)
        self.assertEqual((row["outcome"], json.loads(row["reasons_json"])), ("failed", [apply_runner.SERVER_STOPPED]), "the row kept what closed it")
        self.assertTrue(any("had already been finished" in line for line in logged.output))
        self.assertEqual(json.loads(self.health()["detail_json"])["last_outcome"], "failed", "health says what the row says, not what this run found")

    def test_shutdown_finishes_a_running_row_as_stopped_by_the_app(self):
        run_id = self.start(FakeApplyAgentFactory(hang=True))
        self.runner.shutdown(30)
        row = dict(self.conn.execute("SELECT * FROM apply_runs WHERE id=?", (run_id,)).fetchone())
        self.assertEqual((row["status"], row["outcome"]), ("finished", "failed"))
        self.assertEqual(json.loads(row["reasons_json"])[0], apply_runner.SERVER_STOPPED)
        self.assertIsNone(self.runner.busy())

    def test_the_daily_limit_refuses_a_start_with_its_sentence(self):
        with self.conn:
            self.conn.execute(
                "INSERT INTO profiles(user_id, profile_json, created_at, updated_at) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET profile_json=excluded.profile_json",
                (USER, json.dumps({"apply_agent": {"rehearsals_per_day": 1}}), utc_now(), utc_now()),
            )
        self.finish(self.start())
        with self.assertRaises(RunRefused) as caught:
            self.start()
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("today's limit of 1 rehearsals and option lookups", caught.exception.message)
        self.assertIsNone(self.runner.busy())

    def test_a_rehearsal_and_a_lookup_leave_the_tracker_alone(self):
        tables = ("applications", "opportunity_interactions", "application_events", "application_submit_claims", "answer_library")
        before = self.counts(*tables)
        rehearsal = self.finish(self.start())
        lookup = self.finish(self.start(kind="lookup", lookup_key="location_city", lookup_text="Spring"))
        self.assertEqual(self.counts(*tables), before)
        self.assertEqual((rehearsal["kind"], lookup["kind"]), ("rehearsal", "lookup"))
        self.assertEqual(self.counts("apply_runs")["apply_runs"], 2, "exactly one row each")

    def test_the_resume_is_read_and_verified_and_handed_to_the_agent_in_memory(self):
        seen = {}

        class Factory(FakeApplyAgentFactory):
            def __call__(self, **kwargs):
                agent = super().__call__(**kwargs)
                original = agent.run

                def run(plan, **more):
                    seen["files"], seen["lookup"], seen["screenshot_dir"] = more["files"], more["lookup"], str(kwargs["screenshot_dir"])
                    return original(plan, **more)

                agent.run = run
                return agent

        self.finish(self.start(Factory(step_delay=0)))
        payload = seen["files"]["resume"]
        self.assertEqual((payload.name, payload.mime_type, payload.buffer), ("Resume.pdf", "application/pdf", b"%PDF-1.4 a fictional resume"))
        self.assertEqual(payload.sha256, hashlib.sha256(payload.buffer).hexdigest())
        self.assertIsNone(seen["lookup"])
        self.assertTrue(seen["screenshot_dir"].endswith(ACME))

    def test_a_changed_resume_file_leaves_no_payload_for_the_agent_to_report(self):
        (self.root / "resumes" / "resume-file-1.pdf").write_bytes(b"changed after it was confirmed")
        seen = {}

        class Factory(FakeApplyAgentFactory):
            def __call__(self, **kwargs):
                agent = super().__call__(**kwargs)
                original = agent.run

                def run(plan, **more):
                    seen["files"] = more["files"]
                    return original(plan, **more)

                agent.run = run
                return agent

        self.finish(self.start(Factory(step_delay=0)))
        self.assertEqual(seen["files"], {})

    def test_a_lookup_reads_the_options_for_one_typeahead(self):
        run_id = self.start(kind="lookup", lookup_key="location_city", lookup_text="Spring")
        row = self.finish(run_id)
        self.assertEqual((row["outcome"], row["clean"], row["screenshots_json"], row["plan_json"]), ("looked_up", 0, "[]", "[]"))
        self.assertEqual(json.loads(row["options_json"]), {"location": ["Springfield, Example State, United States", "Springdale, Example State, United States"]})
        evidence = json.loads(row["evidence_json"])
        self.assertEqual(evidence["lookup"], {"key": "location_city", "field": "location", "question": "Location (City)"})
        self.assertEqual(evidence["lookups"], [{"key": "location_city", "question": "Location (City)"}])
        self.assertNotIn("Spring", json.dumps({key: row[key] for key in row if key != "options_json"}), "the typed text is not stored")

    def test_a_lookup_for_a_field_with_no_list_is_refused_before_anything_starts(self):
        for key in ("first_name", "no_such_field", "question_4000000103"):
            with self.subTest(key=key):
                with self.assertRaises(RunRefused) as caught:
                    self.start(kind="lookup", lookup_key=key, lookup_text="x")
                self.assertEqual((caught.exception.status_code, caught.exception.message), (422, "That field has no list of options to look up"))
        self.assertEqual(self.counts("apply_runs")["apply_runs"], 0)
        self.assertIsNone(self.runner.busy())

    def test_no_socket_is_opened_by_a_run_with_the_canned_agent(self):
        boom = AssertionError("the run reached for the network")
        with mock.patch("socket.socket.connect", side_effect=boom), mock.patch("socket.create_connection", side_effect=boom), \
                mock.patch("urllib.request.urlopen", side_effect=boom):
            self.assertEqual(self.finish(self.start())["outcome"], "rehearsed")
            self.assertEqual(self.finish(self.start(kind="lookup", lookup_key="location_city", lookup_text="Spr"))["outcome"], "looked_up")

    def test_a_process_run_through_the_runner_writes_the_same_row(self):
        row = self.finish(self.start(FakeApplyAgentFactory(isolation="process", step_delay=0)))
        self.assertEqual((row["status"], row["outcome"]), ("finished", "rehearsed"))
        self.assertEqual(len(json.loads(row["screenshots_json"])), 1)


class RunInputsTests(RunnerCase):
    def test_it_gives_the_listing_and_a_rehearse_plan_and_writes_nothing(self):
        before = self.counts("apply_runs", "applications", "answer_library", "opportunity_interactions")
        inputs = apply_preflight.run_inputs(self.conn, USER, ACME, client=self.schema, resume_root=self.root / "resumes", apply_root=self.apply_root)
        self.assertEqual(self.counts("apply_runs", "applications", "answer_library", "opportunity_interactions"), before)
        self.assertIn(inputs.result["status"], ("ready", "needs_you"))
        self.assertEqual(inputs.result["board_token"], "examplerobotics")
        self.assertTrue(inputs.result["canonical_url"].endswith("/examplerobotics/jobs/4000000001"))
        self.assertTrue(any(item.name == "location_city" for item in inputs.schema))
        self.assertTrue(inputs.plan.fields)
        self.assertEqual(self.schema.calls, [("examplerobotics", "4000000001")])
        # A second call asks again: a run never reads the hour-long cache.
        apply_preflight.run_inputs(self.conn, USER, ACME, client=self.schema, resume_root=self.root / "resumes")
        self.assertEqual(len(self.schema.calls), 2)

    def test_the_plan_uses_the_installs_key_so_a_replan_agrees_with_it(self):
        one = apply_preflight.run_inputs(self.conn, USER, ACME, client=self.schema, resume_root=self.root / "resumes", apply_root=self.apply_root)
        two = apply_preflight.run_inputs(self.conn, USER, ACME, client=self.schema, resume_root=self.root / "resumes", apply_root=self.apply_root)
        self.assertEqual(one.plan.plan_hash, two.plan.plan_hash)

    def test_a_role_that_cannot_run_has_no_plan(self):
        other = apply_preflight.run_inputs(self.conn, USER, "job-b", client=self.schema)
        self.assertEqual((other.result["status"], other.schema, other.plan), ("unavailable", None, None))
        closed = apply_preflight.run_inputs(self.conn, USER, ACME, client=FakeSchemaClient(closed=True))
        self.assertEqual((closed.result["status"], closed.plan), ("failed", None))

    def test_check_still_reads_the_cache_and_keeps_its_answer(self):
        cache = apply_preflight.SchemaCache()
        first = apply_preflight.check(self.conn, USER, ACME, client=self.schema, cache=cache, resume_root=self.root / "resumes")
        apply_preflight.check(self.conn, USER, ACME, client=self.schema, cache=cache, resume_root=self.root / "resumes")
        self.assertEqual(len(self.schema.calls), 1)
        self.assertEqual(first["status"], "needs_you")


class ViewTests(RunnerCase):
    """run_view over fixed rows: what the page is told for each way a run can end."""

    def make(self, kind="rehearsal", **documents):
        run_id = apply_runs.create_run(
            self.conn, user_id=USER, opportunity_id=ACME, kind=kind, started_by="student", ats="greenhouse", board_token="examplerobotics",
            page_url=JOB_URL, company="acme", deadline_seconds=300,
        )
        outcome = documents.pop("outcome", "")
        if outcome:
            apply_runs.finish_run(self.conn, run_id, outcome=outcome, clean=documents.pop("clean", False), **documents)
        return apply_runner.run_view(self.conn, apply_runs.get_run(self.conn, run_id, user_id=USER))

    def test_every_key_is_always_there(self):
        keys = {"id", "opportunity_id", "kind", "status", "outcome", "clean", "started_at", "finished_at", "heartbeat_at", "deadline_at", "stalled",
                "summary", "measured", "progress", "reasons", "problems", "fields", "options", "lookup", "screenshots", "refused_count",
                "review", "review_note", "reviewed_at", "can_review", "can_cancel",
                "phase", "handed_over", "left_for_you", "handoff_until", "page_defaults", "claim", "can_front"}
        for view in (self.make(), self.make(outcome="rehearsed"), self.make("lookup", outcome="looked_up")):
            self.assertEqual(set(view), keys)

    def test_a_running_run_says_its_last_step_and_can_be_stopped(self):
        view = self.make()
        self.assertEqual((view["status"], view["summary"], view["can_cancel"], view["can_review"], view["stalled"]), ("running", "Starting the browser", True, False, False))
        run_id = view["id"]
        steps = [{"at": utc_now(), "step": "open", "text": "Opening the Greenhouse form"}]
        with self.conn:
            self.conn.execute("UPDATE apply_runs SET progress_json=? WHERE id=?", (json.dumps(steps), run_id))
        view = apply_runner.run_view(self.conn, apply_runs.get_run(self.conn, run_id, user_id=USER))
        self.assertEqual((view["summary"], view["progress"]), ("Opening the Greenhouse form", steps))

    def test_a_run_whose_heartbeat_went_quiet_is_stalled(self):
        view = self.make()
        with self.conn:
            self.conn.execute("UPDATE apply_runs SET heartbeat_at='2020-01-01T00:00:00+00:00' WHERE id=?", (view["id"],))
        stalled = apply_runner.run_view(self.conn, apply_runs.get_run(self.conn, view["id"], user_id=USER))
        self.assertEqual((stalled["stalled"], stalled["summary"]), (True, "The app stopped during this run"))

    def age(self, run_id, seconds, *, beat_ago=0):
        """The row as it looks after the server that ran it was stopped ``seconds`` ago: started long ago, heartbeat as given."""
        from datetime import datetime, timedelta, timezone

        def stamp(ago):
            return (datetime.now(timezone.utc) - timedelta(seconds=ago)).isoformat()

        with self.conn:
            self.conn.execute("UPDATE apply_runs SET started_at=?, heartbeat_at=? WHERE id=?", (stamp(seconds), stamp(beat_ago), run_id))

    def view_of(self, run_id, live=None):
        return apply_runner.run_view(self.conn, apply_runs.get_run(self.conn, run_id, user_id=USER), live)

    def test_a_running_row_this_server_is_not_working_on_is_stalled_at_once_whatever_its_heartbeat_says(self):
        run_id = self.make()["id"]
        self.age(run_id, 600, beat_ago=3)   # a server stopped three seconds ago left a fresh heartbeat
        self.assertFalse(self.view_of(run_id)["stalled"], "without a way to ask which runs are live the heartbeat decides")
        orphan = self.view_of(run_id, lambda _id: False)
        self.assertEqual((orphan["stalled"], orphan["can_cancel"], orphan["summary"]), (True, False, "The app stopped during this run"))
        working = self.view_of(run_id, lambda found: found == run_id)
        self.assertEqual((working["stalled"], working["can_cancel"]), (False, True))

    def test_a_run_that_has_only_just_started_is_not_called_an_orphan_before_its_thread_says_it_is_working(self):
        run_id = self.make()["id"]
        self.assertFalse(self.view_of(run_id, lambda _id: False)["stalled"])
        self.age(run_id, apply_runner.ORPHAN_AFTER_S - 5)
        self.assertFalse(self.view_of(run_id, lambda _id: False)["stalled"])
        self.age(run_id, apply_runner.ORPHAN_AFTER_S + 5)
        self.assertTrue(self.view_of(run_id, lambda _id: False)["stalled"])

    def test_a_finished_run_is_never_an_orphan_and_a_lookup_is_one_like_a_rehearsal(self):
        done = self.make(outcome="rehearsed")["id"]
        self.age(done, 600)
        self.assertFalse(self.view_of(done, lambda _id: False)["stalled"])
        lookup = self.make("lookup")["id"]
        self.age(lookup, 600)
        self.assertTrue(self.view_of(lookup, lambda _id: False)["stalled"], "a lookup is the runner's, like a rehearsal")

    def plan(self):
        return [
            {"key": "first_name", "question": "First Name", "control": "text", "required": True, "options": [], "sensitive": None, "disposition": "fill",
             "source": {"kind": "profile", "ref": "", "company": "", "reusable": False, "links": []}, "value_mac": "m", "file_sha256": "", "problem": ""},
            {"key": "question_1", "question": "Why us?", "control": "textarea", "required": True, "options": [], "sensitive": None, "disposition": "blank",
             "source": {"kind": "none"}, "value_mac": "", "file_sha256": "", "problem": "Answer this question once and the app will reuse it"},
            {"key": "extra", "question": "Anything else?", "control": "textarea", "required": False, "options": [], "sensitive": None, "disposition": "blank",
             "source": {"kind": "none"}, "value_mac": "", "file_sha256": "", "problem": "Optional and unanswered"},
            {"key": "question_2", "question": "Team", "control": "select", "required": True, "options": ["A"], "sensitive": None, "disposition": "fill",
             "source": {"kind": "answer", "company": "Acme Robotics"}, "value_mac": "m", "file_sha256": "", "problem": ""},
            {"key": "question_3", "question": "Work authorization", "control": "select", "required": True, "options": ["Yes"], "sensitive": "work_authorization",
             "disposition": "deferred", "source": {"kind": "sensitive"}, "value_mac": "m", "file_sha256": "", "problem": ""},
            {"key": "resume", "question": "Resume/CV", "control": "file", "required": True, "options": [], "sensitive": None, "disposition": "deferred",
             "source": {"kind": "resume"}, "value_mac": "", "file_sha256": "f", "problem": ""},
            {"key": "location_city", "question": "Location (City)", "control": "text", "required": True, "options": [], "sensitive": None, "disposition": "fill",
             "source": {"kind": "ats_label"}, "value_mac": "m", "file_sha256": "", "problem": ""},
            {"key": "cover_letter", "question": "Cover letter", "control": "file", "required": False, "options": [], "sensitive": None, "disposition": "left_for_you",
             "source": {"kind": "none"}, "value_mac": "", "file_sha256": "", "problem": ""},
        ]

    def statements(self):
        base = {"required": True, "options": [], "value_mac": "m", "file_sha256": "", "problem": ""}
        stored = {"kind": "sensitive", "ref": "store-1", "company": "", "reusable": False, "links": ["https://example.test/privacy"]}
        return [
            {**base, "key": "privacy", "question": "I agree to the privacy notice", "control": "checkbox", "sensitive": "acknowledgment", "disposition": "fill", "source": stored},
            {**base, "key": "agree", "question": "Do you agree to the privacy notice at https://example.test/privacy?", "control": "select", "options": ["Yes", "No"],
             "sensitive": "consent", "disposition": "fill", "source": stored},
            {**base, "key": "gender", "question": "Gender", "control": "select", "options": ["Female"], "sensitive": "eeo_gender", "disposition": "fill",
             "source": {"kind": "sensitive", "ref": "store-2", "company": "", "reusable": False, "links": []}},
            {**base, "key": "agree_later", "question": "Do you agree to the terms?", "control": "select", "options": ["Yes", "No"], "sensitive": "consent",
             "disposition": "deferred", "source": stored},
            {**base, "key": "note", "question": "A note", "control": "text", "sensitive": None, "disposition": "fill", "source": {"kind": "answer", "company": "Acme"}},
            # A tick box the app ticked from a stored work-authorization, sponsorship or age statement, with a document it links to.
            {**base, "key": "authorized", "question": "I am authorized to work in the US (see https://example.test/notice)", "control": "checkbox",
             "sensitive": "work_authorization", "disposition": "fill", "source": {**stored, "ref": "store-3", "links": ["https://example.test/notice"]}},
            {**base, "key": "adult", "question": "I am 18 or older", "control": "checkbox", "sensitive": "age_18", "disposition": "fill",
             "source": {**stored, "ref": "store-4", "links": []}},
        ]

    def test_a_box_ticked_from_a_work_authorization_or_age_statement_is_listed_with_its_documents(self):
        view = self.make("handoff", outcome="submitted", plan=self.statements(), evidence={"handoff_end": "posted"})
        by_key = {item["key"]: item for item in view["fields"]}
        for key in ("authorized", "adult"):
            self.assertTrue(by_key[key]["statement"], f"{key} was ticked by the app and is missing from 'Ticked for you'")
            self.assertEqual(by_key[key]["disposition_text"], "Ticked from your stored statement")
            self.assertEqual(by_key[key]["source_text"], "Your stored answer", "not 'Your consent': it is neither an acknowledgment nor a consent")
        self.assertEqual(by_key["authorized"]["links"], ["https://example.test/notice"])
        # A select of the same category is an answer, not a box the app ticked.
        self.assertFalse(apply_runner._is_statement({"source": {"kind": "sensitive"}, "sensitive": "work_authorization", "control": "select"}))

    def test_a_statement_is_marked_whatever_its_control_so_the_page_can_list_it_with_its_documents(self):
        view = self.make("handoff", outcome="submitted", plan=self.statements(), evidence={"handoff_end": "posted"})
        by_key = {item["key"]: item for item in view["fields"]}
        self.assertEqual({key: item["statement"] for key, item in by_key.items()},
                         {"privacy": True, "agree": True, "gender": False, "agree_later": True, "note": False, "authorized": True, "adult": True})
        self.assertEqual(by_key["privacy"]["disposition_text"], "Ticked from your stored statement")
        self.assertEqual(by_key["agree"]["disposition_text"], "Answered from your stored statement", "a Yes/No question is answered, not ticked")
        self.assertEqual(by_key["gender"]["disposition_text"], "Filled from your stored answer")
        self.assertEqual(by_key["agree"]["links"], ["https://example.test/privacy"])
        self.assertEqual(by_key["agree"]["source_text"], "Your consent for Acme Robotics")
        self.assertEqual(by_key["privacy"]["source_text"], "Your acknowledgment for Acme Robotics")
        self.assertEqual(by_key["gender"]["source_text"], "Your stored answer")

    def test_a_rehearsal_marks_a_statement_it_only_checked(self):
        view = self.make(outcome="rehearsed", plan=self.statements())
        by_key = {item["key"]: item for item in view["fields"]}
        self.assertEqual((by_key["agree_later"]["statement"], by_key["agree_later"]["disposition"], by_key["agree_later"]["links"]),
                         (True, "deferred", ["https://example.test/privacy"]))

    def test_a_handoff_was_handed_over_only_when_the_press_went_on(self):
        plan = self.plan()
        for outcome, evidence, expected in (
            ("needs_you", {"handoff_end": "timeout"}, False), ("needs_you", {"handoff_end": "stopped"}, False), ("failed", {"handoff_end": "closed"}, False),
            ("submitted", {"handoff_end": "posted"}, True), ("unconfirmed", {"handoff_end": "posted"}, True),
        ):
            with self.subTest(outcome=outcome, end=evidence["handoff_end"]):
                self.assertEqual(self.make("handoff", outcome=outcome, plan=plan, evidence=evidence)["handed_over"], expected)
        self.assertFalse(self.make(outcome="rehearsed", plan=plan)["handed_over"])

    def test_a_rehearsal_reads_with_its_sentences_its_problems_and_a_value_free_table(self):
        join = [{"kind": "hidden_control", "key": "spam", "message": "The form hides this field", "question": "Spam", "required": False}]
        check = [{"kind": "not_filled", "key": "question_2", "message": "The field did not take the answer", "question": "Team", "required": True}]
        evidence = {"refused_total": 3, "lookups": [{"key": "a", "question": "Location (City)"}, {"key": "b", "question": "School"}], "join_problems": join, "check_problems": check,
                    "filled_keys": ["first_name", "question_2", "location_city"], "checked_keys": ["question_3"]}
        view = self.make(outcome="rehearsed", plan=self.plan(), evidence=evidence, refused=[{}] * 3, reasons=["The form shows a CAPTCHA checkbox"])
        self.assertEqual(view["summary"], "Here is what the app would send to Acme Robotics for Mechanical Engineering Intern. Your application has not been submitted.")
        self.assertEqual(view["measured"], (
            "During the rehearsal the app blocked 3 requests that could have submitted the form or carried a filled-in answer, to Greenhouse or "
            "anywhere else. Sensitive answers were not put in the page; they go in only if you choose Finish in browser, before you press Submit "
            "application. To find the options for Location (City) "
            "and School, the app sent the text typed into those fields to Greenhouse's lookup service. The app saw nothing else you entered leave the browser."))
        self.assertEqual([(item["key"], item["kind"], item["required"]) for item in view["problems"]],
                         [("question_1", "plan", True), ("extra", "plan", False), ("spam", "hidden_control", False), ("question_2", "not_filled", True)])
        by_key = {item["key"]: item for item in view["fields"]}
        self.assertEqual(by_key["first_name"]["disposition_text"], "Filled in the rehearsal")
        self.assertEqual(by_key["question_2"]["disposition_text"], "Filled in the rehearsal")
        self.assertEqual(by_key["first_name"]["source_text"], "Your profile")
        self.assertEqual(by_key["question_1"]["disposition_text"], "Left blank")
        self.assertEqual(by_key["question_2"]["source_text"], "Your saved answer for Acme Robotics")
        self.assertEqual(by_key["question_3"]["disposition_text"], "Checked against the form; filled when you choose Finish in browser")
        self.assertTrue(by_key["question_3"]["sensitive"])
        self.assertEqual(by_key["question_3"]["source_text"], "Your stored answer")
        self.assertEqual(by_key["resume"]["disposition_text"], "Not attached: this board uploads files as soon as they are attached")
        self.assertEqual(by_key["resume"]["source_text"], "Your confirmed résumé")
        self.assertEqual(by_key["location_city"]["source_text"], "The option you confirmed")
        self.assertEqual(by_key["cover_letter"]["disposition_text"], "Left for you")
        for item in view["fields"]:
            self.assertNotIn("value_mac", item)
            self.assertNotIn("file_sha256", item)
        self.assertTrue(view["can_review"] and not view["can_cancel"])
        self.assertEqual(view["refused_count"], 3)

    def test_the_measured_sentence_falls_back_to_the_refused_list_and_names_one_lookup_plainly(self):
        view = self.make(outcome="rehearsed", plan=[], evidence={"lookups": [{"key": "a", "question": "Location (City)"}]}, refused=[{}])
        self.assertIn("blocked 1 request that could have", view["measured"])
        self.assertIn("options for Location (City), the app sent", view["measured"])
        self.assertEqual(self.make(outcome="needs_you", plan=[])["measured"], "")

    def test_a_list_that_was_only_fetched_is_not_said_to_have_been_sent_what_was_typed(self):
        lookups = [
            {"key": "loc", "question": "Location (City)", "kind": "location", "typed": True},
            {"key": "deg", "question": "Degree", "kind": "degree", "typed": False},
            {"key": "dis", "question": "Discipline", "kind": "discipline", "typed": False},
        ]
        measured = self.make(outcome="rehearsed", plan=[], evidence={"lookups": lookups}, refused=[])["measured"]
        self.assertIn("To find the options for Location (City), the app sent the text typed into those fields", measured)
        self.assertIn("For Degree and Discipline, the app fetched Greenhouse's whole list; nothing you typed went with it.", measured)
        self.assertNotIn("Degree and Discipline, the app sent", measured)
        self.assertNotIn("options for Location (City) and", measured)
        self.assertTrue(measured.endswith("The app saw nothing else you entered leave the browser."))

    def test_a_rehearsal_that_ran_no_lookup_does_not_claim_that_nothing_else_left_the_browser(self):
        measured = self.make(outcome="rehearsed", plan=[], evidence={"refused_total": 4, "lookups": []}, refused=[])["measured"]
        self.assertEqual(measured, (
            "During the rehearsal the app blocked 4 requests that could have submitted the form or carried a filled-in answer, to Greenhouse or "
            "anywhere else. Sensitive answers were not put in the page; they go in only if you choose Finish in browser, before you press Submit "
            "application."))
        self.assertNotIn("nothing else", measured.lower())
        self.assertNotIn("anything else", measured.lower())
        with_lookup = self.make(outcome="rehearsed", plan=[], evidence={"lookups": [{"key": "loc", "question": "Location (City)", "kind": "location", "typed": True}]}, refused=[])
        self.assertTrue(with_lookup["measured"].endswith("The app saw nothing else you entered leave the browser."))

    def test_an_optional_field_the_form_did_not_draw_is_not_said_to_have_stopped_the_rehearsal(self):
        def entry(key, required):
            return {"key": key, "question": key, "control": "text", "required": required, "options": [], "sensitive": None, "disposition": "fill",
                    "source": {"kind": "answer", "ref": "", "company": "", "reusable": False, "links": []}, "value_mac": "m", "file_sha256": "", "problem": ""}

        plan = [entry("first_name", True), entry("portfolio", False), entry("team", True)]
        absent = [{"kind": "missing_control", "key": "portfolio", "message": 'The form has no field for "portfolio"', "question": "portfolio", "required": False}]
        done = self.make(outcome="rehearsed", plan=plan, evidence={"filled_keys": ["first_name"], "check_problems": absent})
        texts = {item["key"]: item["disposition_text"] for item in done["fields"]}
        self.assertEqual(texts["first_name"], "Filled in the rehearsal")
        self.assertEqual(texts["portfolio"], "Not filled: the form has no field for this")
        self.assertEqual(texts["team"], "Not confirmed: the rehearsal stopped before this was filled and read back", "a field the form did draw and was not filled is still unconfirmed")
        # On a run that did stop, the same key was simply not reached.
        stopped = self.make(outcome="needs_you", plan=plan, evidence={"filled_keys": [], "check_problems": absent})
        self.assertEqual({item["key"]: item["disposition_text"] for item in stopped["fields"]}["portfolio"],
                         "Not confirmed: the rehearsal stopped before this was filled and read back")

    def test_a_deferred_field_the_form_does_not_offer_is_not_said_to_be_filled_when_you_submit(self):
        def entry(key, sensitive=None):
            return {"key": key, "question": key, "control": "select", "required": True, "options": ["Yes"], "sensitive": sensitive, "disposition": "deferred",
                    "source": {"kind": "sensitive", "ref": "", "company": "", "reusable": False, "links": []}, "value_mac": "m", "file_sha256": "", "problem": ""}

        plan = [entry("work_authorization", "work_authorization"), entry("gender", "eeo_gender")]
        evidence = {"checked_keys": ["work_authorization", "gender"], "deferred_failed_keys": ["work_authorization"],
                    "check_problems": [{"kind": "deferred", "key": "work_authorization", "message": "The form does not offer the answer", "question": "work_authorization", "required": True}]}
        view = self.make(outcome="needs_you", plan=plan, evidence=evidence)
        texts = {item["key"]: item["disposition_text"] for item in view["fields"]}
        self.assertEqual(texts["gender"], "Checked against the form; filled when you choose Finish in browser")
        self.assertNotIn("filled when you choose", texts["work_authorization"])
        self.assertEqual(texts["work_authorization"], "Checked against the form: the app could not confirm it offers this answer, so it would not be filled")

    def test_the_table_says_filled_only_for_what_the_rehearsal_filled_and_read_back(self):
        def entry(key, disposition="fill", control="text", source="profile", **more):
            return {"key": key, "question": key, "control": control, "required": True, "options": [], "sensitive": None, "disposition": disposition,
                    "source": {"kind": source, "ref": "", "company": "", "reusable": False, "links": []}, "value_mac": "m",
                    "file_sha256": more.pop("file_sha256", ""), "problem": ""}

        plan = [entry("first_name"), entry("last_name"), entry("team", control="select", source="answer"),
                entry("resume", control="file", source="resume", file_sha256="f"), entry("cover_letter", control="file", source="cover_letter", file_sha256="c")]
        # A rehearsal that stopped at the location list: choices come first, so no text was typed and no file attached.
        stopped = self.make(outcome="needs_you", plan=plan, reasons=['The field "Location" did not take the answer'], evidence={"filled_keys": []})
        texts = {item["key"]: item["disposition_text"] for item in stopped["fields"]}
        self.assertEqual(set(texts.values()), {
            "Not confirmed: the rehearsal stopped before this was filled and read back", "Not attached in this rehearsal",
        })
        self.assertNotIn("Filled in the rehearsal", texts.values())
        # A rehearsal that finished: the text and the choice are filled and read back; the résumé was attached; the cover letter never is.
        done = self.make(outcome="rehearsed", plan=plan, evidence={"filled_keys": ["first_name", "last_name", "team", "resume"]})
        texts = {item["key"]: item["disposition_text"] for item in done["fields"]}
        self.assertEqual([texts[key] for key in ("first_name", "last_name", "team", "resume")], ["Filled in the rehearsal"] * 4)
        self.assertEqual(texts["cover_letter"], "Not attached in this rehearsal")
        # A résumé the app could not read is not attached either, whatever the plan says.
        unread = self.make(outcome="rehearsed", plan=plan, evidence={"filled_keys": ["first_name", "last_name", "team"]})
        self.assertEqual({item["key"]: item["disposition_text"] for item in unread["fields"]}["resume"], "Not attached in this rehearsal")
        # A row that has no record of what was filled claims nothing.
        silent = self.make(outcome="rehearsed", plan=plan, evidence={})
        self.assertNotIn("Filled in the rehearsal", [item["disposition_text"] for item in silent["fields"]])

    def test_a_deferred_row_is_said_to_be_checked_only_when_the_rehearsal_compared_it_with_the_form(self):
        def entry(key, sensitive=None, control="select"):
            return {"key": key, "question": key, "control": control, "required": True, "options": ["Yes"], "sensitive": sensitive, "disposition": "deferred",
                    "source": {"kind": "sensitive" if sensitive else "answer", "ref": "", "company": "", "reusable": False, "links": []},
                    "value_mac": "m", "file_sha256": "", "problem": ""}

        plan = [entry("work_authorization", "work_authorization"), entry("gender", "eeo_gender"), entry("team")]
        stopped = self.make(outcome="needs_you", plan=plan, reasons=['The field "Location" did not take the answer'], evidence={"filled_keys": []})
        texts = {item["key"]: item["disposition_text"] for item in stopped["fields"]}
        self.assertEqual(set(texts.values()), {"Not checked: the rehearsal stopped before this was compared with the form"})
        # Stopped part-way through the comparisons: only the keys it got to are checked.
        part = self.make(outcome="needs_you", plan=plan, evidence={"checked_keys": ["work_authorization"]})
        texts = {item["key"]: item["disposition_text"] for item in part["fields"]}
        self.assertEqual(texts["work_authorization"], "Checked against the form; filled when you choose Finish in browser")
        self.assertEqual(texts["gender"], "Not checked: the rehearsal stopped before this was compared with the form")
        self.assertEqual(texts["team"], "Not checked: the rehearsal stopped before this was compared with the form")
        done = self.make(outcome="rehearsed", plan=plan, evidence={"checked_keys": ["work_authorization", "gender", "team"]})
        texts = {item["key"]: item["disposition_text"] for item in done["fields"]}
        self.assertEqual(texts["gender"], "Checked against the form; filled when you choose Finish in browser")
        self.assertEqual(texts["team"], "Checked against the form")
        # A row with no record of what was compared claims nothing.
        silent = self.make(outcome="rehearsed", plan=plan, evidence={})
        self.assertNotIn("Checked against the form", " ".join(item["disposition_text"] for item in silent["fields"]))

    def test_a_join_problem_on_a_field_the_plan_lists_is_shown_once(self):
        wording = "The wording on the form is not the one you agreed to"
        plan = self.plan()
        plan[1]["problem"] = wording
        join = [
            {"kind": "wording_mismatch", "key": "question_1", "message": wording, "question": "Why us?", "required": True},
            {"kind": "unlisted_required", "key": "surprise", "message": "The form has a required field the listing lacks", "question": "Surprise", "required": True},
        ]
        view = self.make(outcome="rehearsed", plan=plan, evidence={"join_problems": join})
        keys = [item["key"] for item in view["problems"]]
        self.assertEqual(keys.count("question_1"), 1, keys)
        self.assertEqual(keys.count("surprise"), 1, keys)
        self.assertEqual(sorted(keys), sorted(set(keys)))

    def test_the_other_outcomes_have_their_own_summaries(self):
        needs = self.make(outcome="needs_you", reasons=["This is Greenhouse's older form, which the app does not fill yet"])
        self.assertEqual(needs["summary"], "The rehearsal stopped: This is Greenhouse's older form, which the app does not fill yet. No application was sent.")
        self.assertTrue(needs["can_review"])
        pages = self.make(outcome="needs_you", reasons=[apply_agent.MORE_PAGES], evidence={"more_pages": True})
        self.assertEqual(pages["summary"], "The rehearsal stopped: This form has more than one page, and the app read only the first. No application was sent.")
        self.assertFalse(pages["clean"], "a form the rehearsal read only the first page of is never clean")
        self.assertNotIn("Here is what the app would send", pages["summary"])
        failed = self.make(outcome="failed", reasons=["Greenhouse answered HTTP 503"])
        self.assertEqual(failed["summary"], "Greenhouse answered HTTP 503. No application was sent.")
        self.assertFalse(failed["can_review"])
        already = self.make(outcome="failed", reasons=[apply_runner.STOPPED])
        self.assertEqual(already["summary"], "You stopped this run. No application was sent.", "a reason that ends in the sentence is not doubled")
        self.assertEqual(self.make(outcome="failed")["summary"], "The run did not finish. No application was sent.")
        lookup_stop = self.make("lookup", outcome="needs_you", reasons=["The form has no field for \"Location\""])
        self.assertEqual(lookup_stop["summary"], 'The lookup stopped: The form has no field for "Location". No application was sent.')
        self.assertFalse(lookup_stop["can_review"], "a lookup is never marked")

    def test_a_lookup_says_how_many_options_came_back(self):
        two = self.make("lookup", outcome="looked_up", options={"location": ["A", "B"]}, evidence={"lookup": {"key": "location_city", "field": "location", "question": "Location (City)"}})
        self.assertEqual(two["summary"], "Greenhouse listed 2 options for what you typed. Pick the one that is yours.")
        self.assertEqual((two["options"], two["lookup"]["field"], two["measured"]), ({"location": ["A", "B"]}, "location", ""))
        one = self.make("lookup", outcome="looked_up", options={"location": ["A"]})
        self.assertEqual(one["summary"], "Greenhouse listed 1 option for what you typed. Pick the one that is yours.")
        none = self.make("lookup", outcome="looked_up", options={"location": []})
        self.assertEqual(none["summary"], "No options came back for what you typed. Try fewer letters, or check the spelling.")
        self.assertIsNone(none["lookup"])

    def test_pictures_are_listed_with_their_url_and_whether_they_are_still_kept(self):
        shots = [{"step": "filled", "path": "abc/x.png", "sha256": "s", "masked": ["gender"]}, {"step": "needs-you", "path": "", "sha256": "t", "masked": []}]
        view = self.make(outcome="rehearsed", screenshots=shots)
        self.assertEqual(view["screenshots"][0], {"index": 0, "step": "filled", "url": f"/api/v1/apply-agent/runs/{view['id']}/screenshots/0", "masked": ["gender"], "available": True})
        self.assertFalse(view["screenshots"][1]["available"])

    def test_screenshot_path_stays_inside_the_students_folder(self):
        folder = self.apply_root / apply_runs.user_folder(USER) / ACME
        folder.mkdir(parents=True)
        (folder / "run-x-filled.png").write_bytes(canned_png())
        outside = self.root / "outside.png"
        outside.write_bytes(canned_png())
        other = self.apply_root / "0123456789abcdef"
        other.mkdir(parents=True)
        (other / "theirs.png").write_bytes(canned_png())
        inside = f"{apply_runs.user_folder(USER)}/{ACME}/run-x-filled.png"
        cases = {
            "inside": (inside, True), "absolute inside": (str(folder / "run-x-filled.png"), True),
            "climbs out": (f"{apply_runs.user_folder(USER)}/../../outside.png", False), "absolute elsewhere": (str(outside), False),
            "another student's": ("0123456789abcdef/theirs.png", False), "purged": ("", False), "missing": (f"{apply_runs.user_folder(USER)}/{ACME}/none.png", False),
        }
        for name, (stored, found) in cases.items():
            with self.subTest(name):
                row = {"screenshots_json": json.dumps([{"step": "filled", "path": stored}])}
                path = apply_runner.screenshot_path(self.apply_root, USER, row, 0)
                self.assertEqual(path is not None, found)
        row = {"screenshots_json": json.dumps([{"step": "filled", "path": inside}])}
        self.assertIsNone(apply_runner.screenshot_path(self.apply_root, USER, row, 1))
        self.assertIsNone(apply_runner.screenshot_path(self.apply_root, USER, row, -1))
        self.assertIsNone(apply_runner.screenshot_path(self.apply_root, USER, {"screenshots_json": "not json"}, 0))

    def test_run_views_lists_a_roles_runs_newest_first_and_can_filter_by_kind(self):
        first = self.make(outcome="rehearsed")["id"]
        second = self.make("lookup", outcome="looked_up")["id"]
        third = self.make()["id"]
        self.assertEqual([item["id"] for item in apply_runner.run_views(self.conn, USER, ACME)], [third, second, first])
        self.assertEqual([item["id"] for item in apply_runner.run_views(self.conn, USER, ACME, kind="rehearsal", limit=1)], [third])
        self.assertEqual([item["id"] for item in apply_runner.run_views(self.conn, USER, ACME, kind="lookup")], [second])
        self.assertEqual(apply_runner.run_views(self.conn, "someone-else", ACME), [])


if __name__ == "__main__":
    unittest.main()
