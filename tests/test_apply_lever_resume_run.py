"""A Finish in browser run on Lever, through the runner: what is written about the student's résumé before the window is ready, and kept after.

Lever reads a résumé as soon as it is attached (docs/phase5-lever-handoff-spec.md, L1), so a run that dies between the attach and the
window being ready must not later say "Nothing was sent", and a result that forgets to repeat what the window said must not erase it.
The driver does not exist yet: Lever is marked built in this process only (as the sandbox does) and the agent is the canned one. No
browser, no network; every company and posting is fictional.
"""

import json
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import realdata_guard

realdata_guard.install()

from opportunity_app.apply import ats as apply_ats, policy as apply_policy, runner as apply_runner, runs as apply_runs
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


class QuietReadyAgent(CannedAgent):
    """A canned Lever agent whose ready message leaves out what it knows about the résumé (nothing forces an adapter to send the key)."""

    def run(self, *args, link=None, **kwargs):
        class Quiet:
            def ready(self, message):
                link.ready({key: value for key, value in message.items() if key != "resume_sent_to_lever"})

            def __getattr__(self, name):
                return getattr(link, name)

        return super().run(*args, link=Quiet() if link is not None else None, **kwargs)


class QuietReadyFactory(FakeApplyAgentFactory):
    def __call__(self, **kwargs):
        canned = super().__call__(**kwargs)
        quiet = QuietReadyAgent.__new__(QuietReadyAgent)
        quiet.__dict__.update(canned.__dict__)
        return quiet


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


class ReplanSourcesTests(LeverRunCase):
    """The plan the window is filled from is built again by the runner once the page has been read; it must carry the student's L1 choice."""

    def run_and_note_sources(self, setting):
        with self.conn:
            self.conn.execute("UPDATE user_settings SET value=? WHERE user_id=? AND key='apply_lever_resume_upload'", (setting, USER))
        seen = []
        real = apply_policy.sources_for

        def spy(*args, **kwargs):
            found = real(*args, **kwargs)
            seen.append((kwargs.get("ats"), found.resume_upload))
            return found

        with mock.patch.object(apply_policy, "sources_for", spy):
            self.finish(self.lever(FakeApplyAgentFactory(step_delay=0.0, handoff={"wait": 0.1, "outcome": "submitted"})))
        return seen

    def test_the_runner_builds_the_plan_for_the_window_with_the_resume_setting_on(self):
        seen = self.run_and_note_sources("on")
        self.assertIn(("lever", True), seen, "the plan the window is filled from did not know the student let the app attach the résumé")
        self.assertNotIn(("lever", False), seen)

    def test_the_runner_builds_it_with_the_setting_off_too(self):
        self.assertNotIn(("lever", True), self.run_and_note_sources("off"))


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


class ReadyWithoutTheKeyTests(LeverRunCase):
    def test_a_ready_message_without_the_key_keeps_the_planned_marker_and_recovery_says_lever_may_have_the_file(self):
        run_id = self.lever(QuietReadyFactory(step_delay=0.0, handoff={"wait": 20, "outcome": "submitted"}))
        deadline = time.monotonic() + 30
        evidence = {}
        while time.monotonic() < deadline and "handoff_until" not in evidence:
            time.sleep(0.05)
            evidence = json.loads(self.row(run_id)["evidence_json"] or "{}")
        self.assertIn("handoff_until", evidence, "the window never got ready")
        self.assertIs(evidence.get(RESUME_PLANNED_KEY), True, "the ready write erased the planned marker")
        self.assertNotIn("resume_sent_to_lever", evidence, "a key the window never sent was made up as False")
        # The server dies during the student's turn: another process finds the claim and settles it.
        stale = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="microseconds")
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET instance='another-process', heartbeat_at=? WHERE run_id=?", (stale, run_id))
        apply_runs.recover_stale(self.conn, datetime.now(timezone.utc), user_id=USER)
        claim = self.conn.execute("SELECT note FROM application_submit_claims WHERE run_id=?", (run_id,)).fetchone()
        self.assertIn(MAYBE, claim["note"])
        self.assertNotIn("Nothing was sent", claim["note"])
        self.runner.shutdown(30)


if __name__ == "__main__":
    unittest.main()
