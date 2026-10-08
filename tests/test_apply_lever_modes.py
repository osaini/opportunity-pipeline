"""Only Finish in browser, and not yet (docs/phase5-lever-handoff-spec.md 6.1): the claim and the runner refuse every other way to apply on Lever.

A claim of any mode but a handoff is refused for Lever inside the claim transaction, and the runner refuses a rehearsal and a lookup before
anything is read and a Finish in browser until Lever has a driver. No browser and no network.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import realdata_guard

realdata_guard.install()

import test_apply_runner as runner_tests
from opportunity_app.apply import runner as apply_runner, runs as apply_runs
from opportunity_app.apply.runs import ClaimRefused

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

    def test_a_rehearsal_a_lookup_and_a_finish_in_browser_are_each_refused_before_anything_is_read(self):
        refused = {kind: self.refuse(kind) for kind in ("rehearsal", "lookup", "handoff")}
        self.assertEqual(
            {kind: (item.status_code, item.code, item.message) for kind, item in refused.items()},
            {"rehearsal": (409, "ats_mode", "Lever supports Finish in browser only, for now"),
             "lookup": (409, "ats_mode", "Lever supports Finish in browser only, for now"),
             "handoff": (409, "ats_not_built", "Finish in browser for Lever postings is not available yet")},
        )
        self.assertEqual(self.pages.calls, [], "the page was not even asked for")
        self.assertEqual(self.counts("apply_runs", "application_submit_claims", "applications"),
                         {"apply_runs": 0, "application_submit_claims": 0, "applications": self.counts("applications")["applications"]})

    def test_the_greenhouse_start_is_unchanged(self):
        self.assertEqual(self.finish(self.start())["outcome"], "rehearsed")


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
