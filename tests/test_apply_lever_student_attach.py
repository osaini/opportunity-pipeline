"""A file the student attaches in the Lever window, through the runner: what the page and the record say while the turn goes on, and after a stop.

Lever reads a file the moment it is attached, so from then on the ATS holds it (docs/phase5-lever-handoff-spec.md 6.12 step 7). The agent says so as
it happens and the runner writes it at once, so a run that ends with no result, or a server that stops, cannot later say "Nothing was sent". The
agent is the canned one; the real driver's side is in tests/test_apply_lever_handoff_browser.py. No browser, no network; everything is fictional.
"""

import json
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import realdata_guard

realdata_guard.install()

from opportunity_app.apply import runner as apply_runner, runs as apply_runs
from opportunity_app.apply.agent_types import RESUME_CHANGED_STEP, STUDENT_RESUME_STEP, progress_text

from apply_fake_ats import FakeApplyAgentFactory
from helpers_apply import USER, setUpModule, tearDownModule  # noqa: F401
from test_apply_lever_resume_run import LeverRunCase

RECEIVED = "Lever received your résumé."
CHANGED = ["Current company", "Current location"]


def attaching(wait=30, outcome="submitted", changed=CHANGED):
    return FakeApplyAgentFactory(step_delay=0.0, handoff={"wait": wait, "outcome": outcome, "student_attaches": {"changed": list(changed)}})


class StudentAttachRunTests(LeverRunCase):
    def setUp(self):
        super().setUp()
        with self.conn:
            # The app attaches nothing here: whatever Lever holds, the student sent.
            self.conn.execute("UPDATE user_settings SET value='off' WHERE user_id=? AND key='apply_lever_resume_upload'", (USER,))

    def until(self, run_id, step):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            steps = [item["step"] for item in json.loads(self.row(run_id)["progress_json"] or "[]")]
            if step in steps:
                return
            time.sleep(0.05)
        self.fail(f"the run never reported {step}")

    def test_while_the_turn_goes_on_the_page_says_lever_holds_the_file_and_which_fields_it_filled(self):
        run_id = self.lever(attaching())
        self.until(run_id, RESUME_CHANGED_STEP)
        text = progress_text(RESUME_CHANGED_STEP, "Lever", fields="Current company and Current location", them="them")
        view = self.view(run_id)
        self.assertEqual((view["status"], view["phase"], view["summary"]), ("running", "resume_changed", text))
        self.assertEqual(text, "Lever filled Current company and Current location from the résumé you attached. Check them before you press Submit application")
        self.assertIs(view["resume_sent_to_lever"], True, "the page says so as soon as the file went")
        evidence = json.loads(self.row(run_id)["evidence_json"])
        self.assertEqual(evidence["student_attached_resume"], {"count": 1}, "written when the file went, not when the run ended")
        self.assertIs(evidence["resume_sent_to_lever"], False, "the app attached nothing")
        self.runner.cancel(run_id)
        self.finish(run_id)

    def test_a_stop_after_the_students_attach_says_lever_received_the_file(self):
        run_id = self.lever(attaching())
        self.until(run_id, STUDENT_RESUME_STEP)
        token = self.conn.execute("SELECT token FROM application_submit_claims WHERE run_id=?", (run_id,)).fetchone()["token"]
        # What the Stop route does: the database flag first, then the message to the agent.
        self.assertTrue(apply_runs.request_cancel(self.conn, token, user_id=USER))
        self.assertTrue(self.runner.cancel(run_id))
        row = self.finish(run_id)
        claim = self.conn.execute("SELECT state, after_click, note FROM application_submit_claims WHERE run_id=?", (run_id,)).fetchone()
        self.assertEqual((claim["state"], claim["after_click"]), ("needs_you", 0))
        self.assertIn(RECEIVED, claim["note"])
        self.assertNotIn("Nothing was sent", claim["note"])
        self.assertIn(RECEIVED, self.view(run_id)["summary"])
        self.assertEqual(json.loads(row["evidence_json"])["student_attached_resume"]["count"], 1)

    def test_a_server_that_stops_after_the_attach_does_not_say_nothing_was_sent(self):
        run_id = self.lever(attaching())
        self.until(run_id, STUDENT_RESUME_STEP)
        stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="microseconds")
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET instance='another-process', heartbeat_at=? WHERE run_id=?", (stale, run_id))
        apply_runs.recover_stale(self.conn, datetime.now(timezone.utc), user_id=USER)
        claim = self.conn.execute("SELECT note FROM application_submit_claims WHERE run_id=?", (run_id,)).fetchone()
        self.assertIn(RECEIVED, claim["note"])
        self.assertNotIn("Nothing was sent", claim["note"])
        self.runner.shutdown(30)

    def test_the_result_of_a_run_that_never_mentions_the_file_still_keeps_what_the_parent_saw(self):
        class Forgetful(FakeApplyAgentFactory):
            def __call__(self, **kwargs):
                agent = super().__call__(**kwargs)
                real = agent.run

                def run(*args, **more):
                    result = real(*args, **more)
                    result.evidence.pop("student_attached_resume", None)
                    return result

                agent.run = run
                return agent

        run_id = self.lever(Forgetful(step_delay=0.0, handoff={"wait": 0.3, "outcome": "unconfirmed", "student_attaches": {"changed": []}}))
        row = self.finish(run_id)
        self.assertEqual(json.loads(row["evidence_json"])["student_attached_resume"], {"count": 1})

    def test_a_result_that_names_the_file_keeps_its_hash_over_the_parents_note(self):
        row = self.finish(self.lever(attaching(wait=0.3, outcome="unconfirmed")))
        record = json.loads(row["evidence_json"])["student_attached_resume"]
        self.assertEqual((record["count"], len(record["sha256"]), record["changed"]), (1, 64, CHANGED))

    def test_a_greenhouse_run_has_no_such_step_and_no_such_record(self):
        run_id = self.start(kind="handoff", factory=attaching(wait=0.2))
        row = self.finish(run_id)
        self.assertNotIn("student_attached_resume", row["evidence_json"] or "")


class PhaseTests(unittest.TestCase):
    def test_the_step_that_names_the_fields_is_the_students_turn_and_shows_its_own_sentence(self):
        told = [{"step": "your_turn", "text": "x"}, {"step": RESUME_CHANGED_STEP, "text": "Lever filled Current company from the résumé you attached. Check it before you press Submit application"}]
        card = {"status": "your_turn"}
        self.assertEqual(apply_runner._handoff_phase(told, card, "claimed"), RESUME_CHANGED_STEP)
        self.assertEqual(apply_runner._handoff_phase(told, card, "clicking"), "submitting", "the student pressed Submit after reading it")
        told_file_only = [{"step": "your_turn", "text": "x"}, {"step": STUDENT_RESUME_STEP, "text": "y"}]
        self.assertEqual(apply_runner._handoff_phase(told_file_only, card, "claimed"), "your_turn", "a file that changed nothing is still the turn")


if __name__ == "__main__":
    unittest.main()
