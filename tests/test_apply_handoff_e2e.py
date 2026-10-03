"""Finish in browser end to end: the real runner, a real child process and the real driver against the fictional Greenhouse.

Nothing reaches the network: the driver's route hook serves ``FakeGreenhouse`` and aborts everything else. The student is a
``student_hook`` that fills and presses Submit in the window, the way the person would. What is proven here is the seam between
the packages: the POST passes only after a committed hand-over, a refused hand-over aborts it, and a run that ends on its time
closes the browser before its claim is settled.
"""

import json
import sys
import threading
import time
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import realdata_guard

realdata_guard.install()

import apply_agent_fakes as fakes
from browser_support import requires_chromium
from helpers_apply import setUpModule, tearDownModule  # noqa: F401 (module fixtures: unittest and pytest find them here)
from opportunity_app.apply.agent_types import HANDOFF_NOT_SUBMITTED
from opportunity_app.core.database import connect_product
from opportunity_app.apply import runner as apply_runner, runs as apply_runs, security_code as apply_security_code
from opportunity_app.apply.runner import ApplyRunner
from test_apply_handoff import CODE, HandoffCase

USER = "local-user"


class RealDriverHandoffTests(HandoffCase):
    """``HandoffCase`` gives a throwaway database, a student and a saved role whose posting matches the fictional board's listing."""

    def setUp(self):
        super().setUp()
        self.record = self.root / "e2e-record.json"
        self.addCleanup(self.runner.shutdown, 30)
        self.runner = ApplyRunner(cancel_grace_s=10.0, timeouts=fakes.HANDOFF_TIMEOUTS, security_code_reader=self.reader)
        self.addCleanup(self.runner.shutdown, 30)
        self.settled_with = []

    def factory(self, student, scenario="confirm"):
        return fakes.BrowserAgentFactory(scenario, record_path=str(self.record), mode="handoff", student=student)

    def seen(self):
        return json.loads(self.record.read_text(encoding="utf-8"))

    def watch_settling(self):
        """Record, at the moment the claim is settled, whether any process the run started is still alive."""
        original = ApplyRunner._finish_handoff

        def finish(runner, conn, work, outcome, *, shutting_down):
            self.settled_with.append({
                "alive": [pid for pid in outcome.pids if apply_runner.process_alive(pid)], "pids": dict(outcome.pids),
                "table": {pid: parent for pid, parent in (apply_runner._process_table() or {}).items() if pid in outcome.pids},
                "confirmed": outcome.closed_confirmed, "stop": outcome.stop,
                "claim": dict(conn.execute("SELECT state FROM application_submit_claims WHERE token=?", (work.token,)).fetchone())["state"],
            })
            return original(runner, conn, work, outcome, shutting_down=shutting_down)

        patcher = mock.patch.object(ApplyRunner, "_finish_handoff", finish)
        patcher.start()
        self.addCleanup(patcher.stop)

    @requires_chromium
    def test_the_students_press_passes_only_after_a_committed_hand_over_and_the_claim_ends_submitted(self):
        self.watch_settling()
        committed = []
        original = apply_runs.hand_over

        def spy(conn, token, *, user_id, deadline=None):
            granted = original(conn, token, user_id=user_id, deadline=deadline)
            if granted:
                # Read through another connection, which sees only what was committed (the writing one sees its own open write).
                with closing(connect_product(self.path)) as other:
                    row = dict(other.execute("SELECT state, after_click FROM application_submit_claims WHERE token=?", (token,)).fetchone())
                committed.append((time.monotonic(), row, conn.in_transaction))
            return granted

        with mock.patch.object(apply_runs, "hand_over", spy):
            run_id = self.handoff(self.factory("complete_and_submit"))
            row = self.finished(run_id)
        self.assertEqual((row["outcome"], row["status"]), ("submitted", "finished"), row["reasons_json"])
        self.assertEqual(len(committed), 1)
        self.assertEqual(committed[0][1]["state"], "clicking", "another connection did not see 'clicking' when the hand-over returned: it was not committed")
        self.assertFalse(committed[0][2], "the hand-over returned with its transaction still open")
        seen = self.seen()
        self.assertEqual(len(seen["post_times"]), 1, "the form's POST reached Greenhouse exactly once")
        self.assertLess(committed[0][0], seen["post_times"][0], "the POST reached Greenhouse before the hand-over was committed")
        self.assertEqual(seen["forbidden_clicks"], 0, "the app itself never pressed a submit control")
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["resolved_by"], claim["after_click"]), ("submitted", "page", 1))
        self.assertEqual(self.settled_with[0]["alive"], [], "a process of the run was still alive when the claim was settled")
        self.assertTrue(self.settled_with[0]["pids"], "no process was seen below the driver")
        evidence = json.loads(row["evidence_json"])
        self.assertEqual((evidence["handoff_end"], evidence["browser_closed"], evidence["runner"]["closed_confirmed"]), ("posted", True, True))
        self.assert_settled()

    @requires_chromium
    def test_a_refused_hand_over_aborts_the_post_and_nothing_is_sent(self):
        self.watch_settling()
        with mock.patch.object(apply_runs, "hand_over", lambda *args, **kwargs: False):
            run_id = self.handoff(self.factory("complete_and_submit"))
            row = self.finished(run_id)
        seen = self.seen()
        self.assertEqual((seen["post_times"], seen["non_get"]), ([], []), "a POST reached Greenhouse without a committed hand-over")
        self.assertNotEqual(row["outcome"], "submitted")
        claim = self.claim_of(run_id)
        self.assertNotEqual(claim["state"], "submitted")
        self.assertFalse(claim["handed_over_at"])
        self.assertEqual(claim["after_click"], 0)
        self.assertEqual(self.settled_with[0]["alive"], [], self.settled_with)
        self.assert_settled()

    @requires_chromium
    def test_a_stop_before_the_press_means_the_post_never_leaves(self):
        run_id = self.handoff(self.factory("do_nothing"))
        self.turn(run_id)
        before = self.counts("automation_notices")
        # What the Stop route does: the database flag first, then the message to the agent.
        self.assertTrue(apply_runs.request_cancel(self.conn, self.claim_of(run_id)["token"], user_id=USER))
        self.assertTrue(self.runner.cancel(run_id))
        row = self.finished(run_id)
        self.assertEqual(self.seen()["post_times"], [])
        self.assertEqual(row["outcome"], "needs_you")
        claim = self.claim_of(run_id)
        self.assertEqual((claim["state"], claim["after_click"], claim["note"]), ("needs_you", 0, HANDOFF_NOT_SUBMITTED), "settled as the student's Stop (row 9)")
        self.assertEqual(json.loads(claim["detail_json"]).get("stopped_by"), "student")
        self.assertEqual(json.loads(row["evidence_json"])["runner"]["row"], 9)
        self.assertEqual(self.counts("automation_notices"), before, "the student's own Stop writes no notice")
        self.assert_settled()

    @requires_chromium
    def test_a_run_that_runs_out_of_time_closes_the_browser_before_the_claim_is_settled(self):
        self.watch_settling()
        # A student who never presses Submit: the window's own time (handoff_s) ends the turn.
        self.runner = ApplyRunner(
            cancel_grace_s=10.0, timeouts=replace(fakes.HANDOFF_TIMEOUTS, handoff_s=6), security_code_reader=self.reader,
        )
        self.addCleanup(self.runner.shutdown, 30)
        run_id = self.handoff(self.factory("do_nothing"), runner=self.runner)
        row = self.finished(run_id, self.runner)
        self.assertNotEqual(row["outcome"], "submitted")
        self.assertEqual(self.seen()["post_times"], [])
        evidence = json.loads(row["evidence_json"])
        self.assertEqual(evidence["handoff_end"], "timeout")
        self.assertTrue(evidence["browser_closed"])
        self.assertEqual(self.settled_with[0]["alive"], [], "the browser was still running when the claim was settled")
        self.assertEqual(self.settled_with[0]["claim"], "claimed", "the claim was settled before the browser was gone")
        claim = self.claim_of(run_id)
        self.assertNotEqual(claim["state"], "submitted")
        self.assertEqual(claim["after_click"], 0)
        self.assert_settled()

    @requires_chromium
    def test_the_security_code_crosses_the_pipe_once_is_typed_by_the_driver_and_the_student_presses_submit(self):
        self.watch_settling()
        run_id = self.handoff(self.factory("press_when_typed", "security_code"))
        row = self.finished(run_id)
        self.assertEqual(row["outcome"], "submitted", row["reasons_json"])
        token = self.claim_of(run_id)["token"]
        self.assertEqual(self.reader.asked[0], (USER, token), "the parent answers for its own run's claim")
        self.assertEqual(self.reader.confirmed, [(USER, token, True, "")], "typed is the driver's word, said once")
        steps = [item["step"] for item in json.loads(row["progress_json"])]
        self.assertEqual(steps[-3:], ["submitting", "security_code", "code_typed"])
        evidence = json.loads(row["evidence_json"])
        self.assertEqual((evidence["security_code"]["prompted"], evidence["security_code"]["typed"]), (True, True))
        self.assertEqual(self.seen()["forbidden_clicks"], 0, "the app typed the code but never pressed a submit control")
        self.assertEqual(len(self.seen()["post_times"]), 2, "the form's POST and the code's POST were both the student's presses")
        self.assertNotIn(CODE, self.everywhere(), "the code is in one pipe reply and nowhere else")
        self.assertEqual(self.settled_with[0]["alive"], [])
        self.assert_settled()

    @requires_chromium
    def test_a_reader_that_gives_up_hands_the_code_to_the_student(self):
        self.reader.answers = [apply_security_code.CodeAnswer("fallback", reason="not_found")]
        run_id = self.handoff(self.factory("type_code_late", "security_code"))
        row = self.finished(run_id)
        self.assertEqual(row["outcome"], "submitted", row["reasons_json"])
        steps = [item["step"] for item in json.loads(row["progress_json"])]
        self.assertIn("code_yours", steps, (steps, self.reader.asked, row["evidence_json"], self.reader.confirmed))
        self.assertEqual(self.reader.confirmed, [])
        evidence = json.loads(row["evidence_json"])
        self.assertEqual((evidence["security_code"]["fallback"], evidence["security_code"]["typed"]), (True, False))
        self.assert_settled()


if __name__ == "__main__":
    unittest.main()
