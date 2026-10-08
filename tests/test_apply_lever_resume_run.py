"""A Finish in browser run on Lever, through the runner: what is written about the student's résumé before the window is ready, and kept after.

Lever reads a résumé as soon as it is attached (docs/phase5-lever-handoff-spec.md, L1), so a run that dies between the attach and the
window being ready must not later say "Nothing was sent", and a result that forgets to repeat what the window said must not erase it.
The driver does not exist yet: Lever is marked built in this process only (as the sandbox does) and the agent is the canned one. No
browser, no network; every company and posting is fictional.
"""

import json
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import realdata_guard

realdata_guard.install()

from opportunity_app.apply import ats as apply_ats, runner as apply_runner
from opportunity_app.apply.agent_types import RESUME_PLANNED_KEY

import test_apply_runner as runner_tests
from apply_fake_ats import CannedAgent, FakeApplyAgentFactory, FakeLeverPageClient, LEVER_ROLE_ID, seed_lever_role
from helpers_apply import USER, setUpModule, tearDownModule  # noqa: F401

RECEIVED = "Lever received your résumé."
MAYBE = "Lever may have received your résumé."


class ForgetfulAgent(CannedAgent):
    """A canned Lever agent whose result leaves out what its ready message said about the résumé (nothing forces an adapter to repeat it)."""

    def run(self, *args, **kwargs):
        result = super().run(*args, **kwargs)
        result.evidence.pop("resume_sent_to_lever", None)
        return result


class ForgetfulFactory(FakeApplyAgentFactory):
    def __call__(self, **kwargs):
        canned = super().__call__(**kwargs)
        forgetful = ForgetfulAgent.__new__(ForgetfulAgent)
        forgetful.__dict__.update(canned.__dict__)
        return forgetful


class LeverRunCase(runner_tests.RunnerCase):
    def setUp(self):
        super().setUp()
        built = tuple(replace(spec, adapter_built=True) if spec.key == apply_ats.LEVER.key else spec for spec in apply_ats.REGISTRY)
        patcher = mock.patch.object(apply_ats, "REGISTRY", built)
        patcher.start()
        self.addCleanup(patcher.stop)
        with self.conn:
            seed_lever_role(self.conn, USER)
            for key in ("apply_agent", "apply_agent_lever", "apply_lever_resume_upload"):
                self.conn.execute(
                    "INSERT INTO user_settings(user_id, key, value, updated_at) VALUES(?, ?, 'on', ?) ON CONFLICT(user_id, key) DO UPDATE SET value='on'",
                    (USER, key, "2026-09-29T12:00:00+00:00"),
                )

    def lever(self, factory, **kwargs):
        return self.start(factory, kind="handoff", opportunity_id=LEVER_ROLE_ID, page_client=FakeLeverPageClient(), **kwargs)

    def row(self, run_id):
        return dict(self.conn.execute("SELECT * FROM apply_runs WHERE id=?", (run_id,)).fetchone())

    def view(self, run_id):
        return apply_runner.run_view(self.conn, self.row(run_id), local=self.runner.busy())


class PlannedAttachTests(LeverRunCase):
    def test_the_run_row_says_the_app_will_attach_the_resume_before_the_window_is_ready(self):
        run_id = self.lever(FakeApplyAgentFactory(step_delay=3.0, handoff={"wait": 0.2, "outcome": "submitted"}))
        self.assertEqual(json.loads(self.row(run_id)["evidence_json"]), {RESUME_PLANNED_KEY: True})
        self.runner.shutdown(30)
        self.finish(run_id)

    def test_a_run_stopped_before_the_window_was_ready_says_lever_may_have_the_file(self):
        run_id = self.lever(FakeApplyAgentFactory(step_delay=3.0, handoff={"wait": 0.2, "outcome": "submitted"}))
        self.runner.shutdown(30)   # the server stops while the agent is still attaching
        row = self.finish(run_id)
        self.assertEqual(row["status"], "finished")
        claim = self.conn.execute("SELECT state, note FROM application_submit_claims WHERE run_id=?", (run_id,)).fetchone()
        self.assertIn(MAYBE, claim["note"])
        self.assertNotIn("Nothing was sent", claim["note"])
        self.assertIn(MAYBE, self.view(run_id)["summary"])
        self.assertNotIn("Nothing was sent", self.view(run_id)["summary"])

    def test_a_greenhouse_run_writes_no_such_record(self):
        run_id = self.start(kind="handoff", factory=FakeApplyAgentFactory(step_delay=0.0, handoff={"wait": 0.1, "outcome": "submitted"}))
        self.assertNotIn(RESUME_PLANNED_KEY, self.row(run_id)["evidence_json"])
        self.finish(run_id)


class KeptEvidenceTests(LeverRunCase):
    def test_a_result_that_leaves_out_the_resume_does_not_erase_what_the_window_said(self):
        run_id = self.lever(ForgetfulFactory(step_delay=0.0, handoff={"wait": 0.2, "outcome": "unconfirmed"}))
        row = self.finish(run_id)
        evidence = json.loads(row["evidence_json"])
        self.assertIs(evidence.get("resume_sent_to_lever"), True)
        self.assertIs(self.view(run_id)["resume_sent_to_lever"], True)


if __name__ == "__main__":
    unittest.main()
