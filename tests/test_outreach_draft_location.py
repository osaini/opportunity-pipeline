"""A draft's "(live in ...)" line follows the company's location without the draft being written again."""

import json
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR
from opportunity_app.api import create_app
from opportunity_app.automation import ledger as automation
from opportunity_app.core.database import connect_product
from opportunity_app.outreach.automation import AutomationWorker, update_settings
from opportunity_app.outreach.draft_location import ADDED_EVENT, REMOVED_EVENT, sync_location_line, sync_location_lines
from opportunity_app.outreach.targets import approve_draft, create_target, get_target, update_target
from opportunity_app.outreach.versions import draft_versions

from helpers_platform import build_and_migrate, use_profile_regions
from helpers_outreach import DRAFTING_AUTH as AUTH, USER, confirm_facts

LINE = "(live in the Bay Area)"
# A greeting alone in the first paragraph, so the opening is the second, as drafting reads it.
BODY = (
    "Hi Greg,\n\nI'm Test Student, studying Mechanical Engineering at UT Austin. Bovi's work on dairy robotics "
    "caught my attention.\n\nWould you be open to a 15 minute call?\n\nThank you,\nTest Student"
)
WITH_LINE = BODY.replace("at UT Austin.", f"at UT Austin {LINE}.")
CLAIMS = [{"text": "Test Student", "basis": "profile:name"}, {"text": "UT Austin", "basis": "profile:school"}]
LINE_CLAIM = {"text": LINE, "basis": "profile:break_location"}


class DraftCase:
    """A throwaway database with a student who lives in the Bay Area, and a way to give a company a draft."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        use_profile_regions(self)
        _, self.platform_path = build_and_migrate(Path(self.tempdir.name))
        self.conn = connect_product(self.platform_path)
        confirm_facts(self.conn, school="UT Austin", degree="Mechanical Engineering", break_location="Bay Area")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def draft(self, body=BODY, claims=CLAIMS, **values):
        target = create_target(self.conn, {
            "company": "Bovi", "website": "https://bovi.example", "contact_name": "Greg Hall",
            "contact_email": "greg@bovi.example", **values,
        }, user_id=USER)
        with self.conn:
            self.conn.execute(
                "UPDATE outreach_targets SET email_subject='Hello Bovi', email_body=?, draft_claims_json=?, "
                "draft_status='generated', status='drafted' WHERE id=?",
                (body, json.dumps(claims), target["id"]),
            )
        return get_target(self.conn, target["id"], user_id=USER)

    def events(self, target_id):
        return [event["event_type"] for event in get_target(self.conn, target_id, user_id=USER, include_events=True)["events"]]


class DraftLocationTests(DraftCase, unittest.TestCase):
    def test_a_location_checked_later_puts_the_line_after_the_school_and_changes_nothing_else(self):
        target = self.draft()
        update_target(self.conn, target["id"], {"location": "San Carlos, CA"}, user_id=USER)
        self.assertTrue(get_target(self.conn, target["id"], user_id=USER)["draft_location"]["missing"], "the instrument: it lacks the line")
        self.assertEqual(sync_location_line(self.conn, target["id"], user_id=USER)["action"], "add")
        after = get_target(self.conn, target["id"], user_id=USER)
        self.assertEqual(after["email_body"], WITH_LINE)
        self.assertEqual(after["draft_claims"], [*CLAIMS, LINE_CLAIM])
        self.assertEqual((after["draft_status"], after["draft_location"]["missing"]), ("generated", False))
        self.assertIn(ADDED_EVENT, self.events(target["id"]))
        self.assertIn(BODY, [version["body"] for version in draft_versions(self.conn, target["id"], user_id=USER, kind="initial")],
                      "the draft before the line is kept in the history")
        approved = approve_draft(self.conn, target["id"], user_id=USER, fingerprint=after["draft_fingerprint"], acknowledge_warnings=True)
        self.assertEqual(approved["draft_status"], "approved", "the approval check no longer blocks it")
        self.assertIsNone(sync_location_line(self.conn, target["id"], user_id=USER), "nothing more to do")

    def test_the_line_goes_in_the_opening_even_when_the_greeting_shares_it(self):
        body = BODY.replace("Hi Greg,\n\n", "Hi Greg,\n")
        target = self.draft(body=body, location="Oakland, CA")
        sync_location_line(self.conn, target["id"], user_id=USER)
        self.assertEqual(get_target(self.conn, target["id"], user_id=USER)["email_body"], body.replace("at UT Austin.", f"at UT Austin {LINE}."))

    def test_a_year_round_home_gets_the_year_round_line(self):
        confirm_facts(self.conn, school="University of Texas at Austin", break_location="Austin")
        self.conn.commit()
        body = BODY.replace("UT Austin", "University of Texas at Austin")
        target = self.draft(body=body, location="Round Rock, TX")
        self.assertEqual(sync_location_line(self.conn, target["id"], user_id=USER)["line"], "(live in Austin year-round)")
        self.assertIn("at University of Texas at Austin (live in Austin year-round).", get_target(self.conn, target["id"], user_id=USER)["email_body"])

    def test_a_draft_whose_opening_does_not_name_the_school_is_left_for_the_student(self):
        body = BODY.replace("studying Mechanical Engineering at UT Austin", "a mechanical engineering student") + "\n\nUT Austin"
        target = self.draft(body=body, location="San Carlos, CA")
        self.assertIsNone(sync_location_line(self.conn, target["id"], user_id=USER))
        after = get_target(self.conn, target["id"], user_id=USER)
        self.assertEqual((after["email_body"], after["draft_location"]["missing"]), (body, True), "never placed by a guess")

    def test_a_draft_that_already_says_where_the_student_lives_is_left_alone(self):
        body = BODY.replace("caught my attention.", "caught my attention, and I live in the Bay Area.")
        target = self.draft(body=body, location="San Carlos, CA")
        self.assertIsNone(sync_location_line(self.conn, target["id"], user_id=USER))
        self.assertEqual(get_target(self.conn, target["id"], user_id=USER)["email_body"], body)

    def test_a_location_away_from_home_takes_the_apps_line_out(self):
        target = self.draft(body=WITH_LINE, claims=[*CLAIMS, LINE_CLAIM], location="San Carlos, CA")
        self.assertIsNone(sync_location_line(self.conn, target["id"], user_id=USER), "the instrument: in step while the company is local")
        update_target(self.conn, target["id"], {"location": "Austin, TX"}, user_id=USER)
        self.assertEqual(sync_location_line(self.conn, target["id"], user_id=USER)["action"], "remove")
        after = get_target(self.conn, target["id"], user_id=USER, include_events=True)
        self.assertEqual((after["email_body"], after["draft_claims"]), (BODY, CLAIMS))
        removed = next(event for event in after["events"] if event["event_type"] == REMOVED_EVENT)
        self.assertIn("Bovi is in Austin, TX, not where you live", removed["detail"])

    def test_an_approved_draft_changes_only_from_the_button_and_is_approved_again_by_the_student(self):
        target = self.draft()
        approve_draft(self.conn, target["id"], user_id=USER, fingerprint=target["draft_fingerprint"], acknowledge_warnings=True)
        update_target(self.conn, target["id"], {"location": "San Carlos, CA"}, user_id=USER)
        self.assertIsNone(sync_location_line(self.conn, target["id"], user_id=USER))
        self.assertEqual(sync_location_lines(self.conn, user_id=USER), [])
        self.assertEqual(get_target(self.conn, target["id"], user_id=USER)["email_body"], BODY, "not on its own")

        self.assertEqual(sync_location_line(self.conn, target["id"], user_id=USER, approved=True)["action"], "add")
        after = get_target(self.conn, target["id"], user_id=USER)
        self.assertEqual((after["email_body"], after["draft_status"]), (WITH_LINE, "generated"))
        self.assertIn("approval_withdrawn", self.events(target["id"]))

    def test_a_draft_edited_while_the_line_was_worked_out_is_not_overwritten(self):
        target = self.draft(location="San Carlos, CA")

        def student_edits():
            with closing(connect_product(self.platform_path)) as other:
                update_target(other, target["id"], {"email_body": BODY + "\n\nP.S. Edited."}, user_id=USER)

        self.assertIsNone(sync_location_line(self.conn, target["id"], user_id=USER, before_write=student_edits))
        self.assertEqual(get_target(self.conn, target["id"], user_id=USER)["email_body"], BODY + "\n\nP.S. Edited.")

    def test_a_sent_email_is_never_changed(self):
        target = self.draft(location="San Carlos, CA")
        update_target(self.conn, target["id"], {"status": "sent"}, user_id=USER)
        self.assertIsNone(sync_location_line(self.conn, target["id"], user_id=USER, approved=True))
        self.assertEqual(get_target(self.conn, target["id"], user_id=USER)["email_body"], BODY)


class WorkerSweepTests(DraftCase, unittest.TestCase):
    """The automation worker follows a location that a site check, a filing or a web search established."""

    def worker(self):
        return AutomationWorker(self.platform_path, fetcher_factory=lambda: None)

    def test_the_worker_puts_the_line_in_only_while_write_drafts_automatically_is_on(self):
        target = self.draft(location="San Carlos, CA")
        update_settings(self.conn, {"bounce_recovery": True}, user_id=USER)
        self.assertNotIn("location_lines", self.worker().run_once())
        self.assertEqual(get_target(self.conn, target["id"], user_id=USER)["email_body"], BODY)

        update_settings(self.conn, {"auto_drafts": True}, user_id=USER)
        automation.set_paused(self.conn, USER, True)
        self.assertNotIn("location_lines", self.worker().run_once(), "nothing while paused")
        automation.set_paused(self.conn, USER, False)
        report = self.worker().run_once()
        self.assertEqual([(item["target_id"], item["action"]) for item in report["location_lines"]], [(target["id"], "add")])
        self.assertEqual(get_target(self.conn, target["id"], user_id=USER)["email_body"], WITH_LINE)


class DraftLocationApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        # Registered first, so it runs last: after the client and the connection let go of the database.
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        use_profile_regions(self)
        _, self.platform_path = build_and_migrate(root)
        app = create_app(
            db_path=self.platform_path, access_token="drafting-owner", static_dir=STATIC_DIR,
            resume_storage=root / "resumes", capture_storage=root / "captures", interview_storage=root / "interviews",
        )
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.conn = connect_product(self.platform_path)
        self.addCleanup(self.conn.close)
        confirm_facts(self.conn, school="UT Austin", degree="Mechanical Engineering", break_location="Bay Area")
        self.conn.commit()
        env = mock.patch.dict("os.environ", {"PIPELINE_OUTREACH_ACCOUNT": "student@example.edu"})
        env.start()
        self.addCleanup(env.stop)

    def draft(self, body=BODY):
        created = self.client.post("/api/v1/outreach", headers=AUTH, json={
            "company": "Bovi", "contact_name": "Greg Hall", "contact_email": "greg@bovi.example",
        }).json()
        with self.conn:
            self.conn.execute(
                "UPDATE outreach_targets SET email_subject='Hello Bovi', email_body=?, draft_claims_json=?, "
                "draft_status='generated', status='drafted' WHERE id=?",
                (body, json.dumps(CLAIMS), created["id"]),
            )
        return created["id"]

    def test_typing_a_location_puts_the_line_in_at_once(self):
        target_id = self.draft()
        saved = self.client.patch(f"/api/v1/outreach/{target_id}", headers=AUTH, json={"location": "San Carlos, CA"})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual((saved.json()["email_body"], saved.json()["draft_location"]["missing"]), (WITH_LINE, False))
        saved = self.client.patch(f"/api/v1/outreach/{target_id}", headers=AUTH, json={"location": "Austin, TX"})
        self.assertEqual(saved.json()["email_body"], BODY, "and out again when the company is not local")

    def test_the_button_adds_the_line_to_an_approved_draft_and_takes_the_approval_back(self):
        target_id = self.draft()
        target = self.client.get(f"/api/v1/outreach/{target_id}", headers=AUTH).json()
        approved = self.client.post(f"/api/v1/outreach/{target_id}/approve", headers=AUTH,
                                    json={"fingerprint": target["draft_fingerprint"], "acknowledge_warnings": True})
        self.assertEqual(approved.status_code, 200, approved.text)
        saved = self.client.patch(f"/api/v1/outreach/{target_id}", headers=AUTH, json={"location": "San Carlos, CA"}).json()
        self.assertEqual((saved["email_body"], saved["draft_status"], saved["draft_location"]["missing"]), (BODY, "approved", True))

        added = self.client.post(f"/api/v1/outreach/{target_id}/location-line", headers=AUTH)
        self.assertEqual(added.status_code, 200, added.text)
        self.assertEqual((added.json()["email_body"], added.json()["draft_status"]), (WITH_LINE, "generated"))

    def test_the_button_says_so_when_the_school_is_not_in_the_opening(self):
        target_id = self.draft(body=BODY.replace("studying Mechanical Engineering at UT Austin", "an engineering student"))
        self.client.patch(f"/api/v1/outreach/{target_id}", headers=AUTH, json={"location": "San Carlos, CA"})
        refused = self.client.post(f"/api/v1/outreach/{target_id}/location-line", headers=AUTH)
        self.assertEqual(refused.status_code, 422)
        self.assertIn("never names your school", refused.json()["detail"])
        self.assertEqual(self.client.post("/api/v1/outreach/nope/location-line", headers=AUTH).status_code, 404)


if __name__ == "__main__":
    unittest.main()
