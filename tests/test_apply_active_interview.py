"""Apply for me asks before a second application goes to a company that already has the student mid-process.

A student with an interview in progress (or an offer) at a company who sends a second application there, even to
another role, can cross wires with the recruiter who is already talking to them. The app knows the stage, so it says so
and lets the student tick past it; unattended mode never can, because the tick is the student's own decision.

No browser and nothing that reaches a network: every company, board and posting here is fictional.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opportunity_app.apply import runs as apply_runs
from opportunity_app.apply.runs import ClaimRefused
from opportunity_app.core.timestamps import utc_now
from pipeline_core.identity import employer_key

from helpers_apply import ApplyCase, BLUEFIN, USER, setUpModule, tearDownModule  # noqa: F401 (module fixtures: unittest and pytest find them here)

import realdata_guard
# Guards this module's own run against opening data/*.db (AGENTS.md hard rule 1); see tests/realdata_guard.py.
realdata_guard.install()


class ActiveInterviewTests(ApplyCase):
    def application(self, opportunity_id, stage, *, company=BLUEFIN, title="Embedded Intern"):
        self.opportunity(opportunity_id, company, title)
        stamp = utc_now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO applications(id, opportunity_id, user_id, stage, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?)",
                (f"app-{opportunity_id}", opportunity_id, USER, stage, stamp, stamp),
            )

    def block(self, opportunity_id="new-job", company=BLUEFIN, **kwargs):
        if opportunity_id not in self.companies:
            self.opportunity(opportunity_id, company)
        return apply_runs.duplicate_block(
            self.conn, USER, opportunity_id=opportunity_id, ats="greenhouse", job_ref=f"bluefin/{opportunity_id}",
            company=employer_key(company), **kwargs,
        )

    def test_an_interview_at_the_same_company_asks_first(self):
        self.application("interviewing", "interview", title="Controls Intern")
        block = self.block()
        self.assertEqual((block.kind, block.code), ("ask", "active_at_company"))
        self.assertIn("an interview in progress at", block.message)
        self.assertIn("Controls Intern", block.message)
        self.assertIn(BLUEFIN, block.message)

    def test_an_offer_at_the_same_company_asks_too(self):
        self.application("offered", "offer")
        block = self.block()
        self.assertEqual((block.kind, block.code), ("ask", "active_at_company"))
        self.assertIn("an offer from", block.message)

    def test_every_other_stage_and_every_other_company_asks_nothing(self):
        for stage in ("applying", "applied", "rejected", "withdrawn", "archived"):
            with self.subTest(stage=stage):
                self.application(f"at-{stage}", stage)
        self.application("elsewhere", "interview", company="Harbor Dynamics")
        self.assertIsNone(self.block())

    def test_the_same_company_under_another_spelling_is_the_same_company(self):
        self.application("interviewing", "interview", company="Bluefin Robotics, Inc.")
        self.assertEqual(self.block(company=BLUEFIN).code, "active_at_company")

    def test_the_posting_itself_is_not_another_role(self):
        # An application already at 'interview' for this very posting is the stage block's to say, not this ask's.
        self.application("same", "interview")
        block = self.block("same")
        self.assertEqual((block.kind, block.code), ("failed", "stage"))

    def test_the_tick_lets_the_attempt_through_and_is_recorded(self):
        self.application("interviewing", "interview")
        with self.assertRaises(ClaimRefused) as caught:
            self.start("new-job", "handoff", now=self.at(1))
        self.assertEqual((caught.exception.code, caught.exception.ask), ("active_at_company", True))
        self.assertIsNone(self.stage("new-job"), "being refused never creates an application")
        claim = self.start("new-job", "handoff", now=self.at(2), acknowledged=("active_at_company",))
        self.assertEqual(self.claim_row(claim["token"])["detail_json"], '{"acknowledged": ["active_at_company"]}')

    def test_unattended_mode_cannot_tick_past_it(self):
        self.application("interviewing", "interview")
        with self.assertRaises(ClaimRefused) as caught:
            self.start("new-job", "unattended", now=self.at(1), acknowledged=("active_at_company",))
        self.assertEqual((caught.exception.code, caught.exception.ask), ("active_at_company", True))
        self.assertIsNone(self.stage("new-job"))

    def test_a_company_with_no_usable_name_matches_nothing(self):
        # employer_key("") and employer_key("The Company") are both "": two roles with no company are not the same company.
        self.application("interviewing", "interview", company="")
        self.assertIsNone(self.block(company=""))
        self.assertIsNone(self.block("other-new-job", company="The Company"))

    def test_the_tick_is_for_this_ask_only(self):
        self.application("interviewing", "interview")
        with self.assertRaises(ClaimRefused) as caught:
            self.start("new-job", "handoff", now=self.at(1), acknowledged=("company_limit", "released_job"))
        self.assertEqual(caught.exception.code, "active_at_company")


class TickListTests(unittest.TestCase):
    """Found in review: the start request capped the tick list at four, and the check can now offer five."""

    CODES = ["company_limit", "released_job", "unmatched_confirmation", "applying_old", "active_at_company"]

    def test_every_tick_the_check_can_offer_fits_in_one_request(self):
        from opportunity_app.web.models.apply_agent import ApplyHandoffRequest

        self.assertEqual(ApplyHandoffRequest(acknowledged=self.CODES).acknowledged, self.CODES)

    def test_a_longer_list_is_still_refused(self):
        from pydantic import ValidationError

        from opportunity_app.web.models.apply_agent import ApplyHandoffRequest

        with self.assertRaises(ValidationError):
            ApplyHandoffRequest(acknowledged=self.CODES + ["company_limit"])


if __name__ == "__main__":
    unittest.main()
