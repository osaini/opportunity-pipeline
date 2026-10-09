"""Only Finish in browser (docs/phase5-lever-handoff-spec.md 6.1): the claim and the runner refuse every other way to apply on Lever.

A claim of any mode but a handoff is refused for Lever inside the claim transaction, and the runner refuses a rehearsal and a lookup before
anything is read, and a Finish in browser too while Lever's driver is not connected (``adapter_built``). No browser and no network.
"""

import dataclasses
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import realdata_guard

realdata_guard.install()

import test_apply_runner as runner_tests
from opportunity_app.apply import ats as apply_ats, runner as apply_runner, runs as apply_runs
from opportunity_app.apply.runs import ClaimRefused
from opportunity_app.applications.actions import OpportunityNotFoundError

from apply_fake_ats import FakeLeverPageClient, LEVER_JOB_ID, LEVER_ROLE_ID, LEVER_SITE, seed_lever_role
from helpers_apply import USER, ApplyCase, setUpModule, tearDownModule  # noqa: F401


class LeverClaimTests(ApplyCase):
    def lever_claim(self, mode, **kwargs):
        if "lv-1" not in self.companies:
            self.opportunity("lv-1")
        return apply_runs.claim(
            self.conn, user_id=USER, opportunity_id="lv-1", mode=mode, ats="lever", board_token=LEVER_SITE, job_ref=f"{LEVER_SITE}/{LEVER_JOB_ID}",
            company="bluefinrobotics", **kwargs,
        )

    def test_every_claim_but_a_handoff_is_refused_for_lever_inside_the_claim_and_leaves_no_row(self):
        for mode in ("one_click", "unattended"):
            with self.subTest(mode=mode):
                with self.assertRaises(ClaimRefused) as caught:
                    self.lever_claim(mode)
                self.assertEqual((caught.exception.code, str(caught.exception)), ("ats_mode", "Lever supports Finish in browser only, for now"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM application_submit_claims").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM applications WHERE opportunity_id='lv-1'").fetchone()[0], 0, "a refused claim makes no application")

    def test_a_handoff_claim_for_lever_is_a_claim_like_any_other_and_a_greenhouse_one_takes_every_mode(self):
        self.assertEqual(self.lever_claim("handoff")["state"], "claimed")
        for mode in ("handoff", "one_click", "unattended"):
            with self.subTest(mode=mode):
                self.start(f"gh-{mode}", mode=mode, job=f"bluefin/{mode}")

    def test_an_ats_nobody_registered_is_not_judged_by_the_claim(self):
        self.opportunity("x-1")
        taken = apply_runs.claim(self.conn, user_id=USER, opportunity_id="x-1", mode="one_click", ats="elsewhere", board_token="b", job_ref="b/1", company="bluefin")
        self.assertEqual(taken["state"], "claimed")


class LeverRunnerTests(runner_tests.RunnerCase):
    def setUp(self):
        super().setUp()
        with self.conn:
            seed_lever_role(self.conn, USER)
        self.pages = FakeLeverPageClient()

    def refuse(self, kind):
        with self.assertRaises(apply_runner.RunRefused) as caught:
            self.start(kind=kind, opportunity_id=LEVER_ROLE_ID, page_client=self.pages)
        self.assertIsNone(self.runner.busy(), "a refusal leaves the slot free")
        return caught.exception

    def test_a_rehearsal_and_a_lookup_are_refused_before_anything_is_read_and_so_is_a_finish_in_browser_while_the_driver_is_not_connected(self):
        applications_before = self.counts("applications")["applications"]
        unbuilt = tuple(dataclasses.replace(spec, adapter_built=False) if spec.key == "lever" else spec for spec in apply_ats.REGISTRY)
        with mock.patch.object(apply_ats, "REGISTRY", unbuilt):
            refused = {kind: self.refuse(kind) for kind in ("rehearsal", "lookup", "handoff")}
        self.assertEqual(
            {kind: (item.status_code, item.code, item.message) for kind, item in refused.items()},
            {"rehearsal": (409, "ats_mode", "Lever supports Finish in browser only, for now"),
             "lookup": (409, "ats_mode", "Lever supports Finish in browser only, for now"),
             "handoff": (409, "ats_not_built", "Finish in browser for Lever postings is not available yet")},
        )
        self.assertEqual(self.pages.calls, [], "the page was not even asked for")
        self.assertEqual(self.counts("apply_runs", "application_submit_claims", "applications"),
                         {"apply_runs": 0, "application_submit_claims": 0, "applications": applications_before})

    def test_with_the_driver_connected_a_rehearsal_and_a_lookup_are_still_refused_by_what_lever_supports_and_before_anything_is_read(self):
        refused = {kind: self.refuse(kind) for kind in ("rehearsal", "lookup")}
        self.assertEqual({kind: (item.status_code, item.code) for kind, item in refused.items()}, {"rehearsal": (409, "ats_mode"), "lookup": (409, "ats_mode")})
        self.assertEqual(self.pages.calls, [])

    def test_with_the_driver_connected_a_finish_in_browser_still_needs_the_lever_switch_and_writes_nothing(self):
        applications_before = self.counts("applications")["applications"]
        refused = self.refuse("handoff")
        self.assertEqual(refused.message, "Apply for me on Lever is off. Turn it on in Profile, under Automation, in the Applications list")
        self.assertEqual(self.pages.calls, [], "the page was not asked for")
        self.assertEqual(self.counts("apply_runs", "application_submit_claims", "applications"),
                         {"apply_runs": 0, "application_submit_claims": 0, "applications": applications_before})
        with self.conn:
            self.conn.execute(
                "INSERT INTO user_settings(user_id, key, value, updated_at) VALUES(?, 'apply_agent_lever', 'on', '2026-10-08T00:00:00+00:00')", (USER,),
            )
        run_id = self.start(kind="handoff", opportunity_id=LEVER_ROLE_ID, page_client=self.pages)
        self.assertEqual(self.finish(run_id)["kind"], "handoff", "with the switch on the start goes on to a run")

    def test_a_lever_role_the_student_cannot_see_is_not_found_before_any_mode_is_refused(self):
        # A capture nobody owns is visible to no one. The refusal for its ATS would say the role exists and is on Lever.
        with self.conn:
            self.conn.execute(
                "INSERT INTO opportunity_sources(opportunity_id, source_key, source_name, external_id, source_url, first_seen_at, last_seen_at) "
                "VALUES(?, 'manual:capture', 'Capture', 'capture-nobody-owns', 'https://example.test/x', '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z')",
                (LEVER_ROLE_ID,),
            )
        for kind in ("rehearsal", "lookup", "handoff"):
            with self.subTest(kind=kind):
                with self.assertRaises(OpportunityNotFoundError):
                    self.start(kind=kind, opportunity_id=LEVER_ROLE_ID, page_client=self.pages)
        self.assertEqual(self.pages.calls, [])

    def test_the_greenhouse_start_is_unchanged(self):
        self.assertEqual(self.finish(self.start())["outcome"], "rehearsed")


if __name__ == "__main__":
    unittest.main()
