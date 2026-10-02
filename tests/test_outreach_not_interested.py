"""Not interested: a company the student sets aside is kept, shows only under its own tab, and no automation acts on it."""

import sys
import tempfile
import unittest
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR, internal_automation
from opportunity_app.core import schema
from opportunity_app.api import create_app
from opportunity_app.outreach import (
    log_event,
    create_target,
    existing_keys,
    get_target,
    list_targets,
    queue_follow_up_reminders,
    update_target,
)
from opportunity_app.outreach_identity import company_key
from opportunity_app.outreach_automation import draft_due
from opportunity_app.outreach_call_prep import auto_queue_call_prep
from opportunity_app.outreach_recontact import eligible_targets
from opportunity_app.outreach_research import due_for_research
from opportunity_app.outreach_thank_you import due as thank_you_due
from opportunity_app.core.database import connect_product
from opportunity_app.applications.urgent import urgent_queue

from helpers_platform import build_and_migrate

AUTH = {"Authorization": "Bearer set-aside-owner"}
USER = "local-user"
REPO = Path(__file__).resolve().parents[1]


class NotInterestedApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        _, self.platform_path = build_and_migrate(root)
        app = create_app(
            db_path=self.platform_path, access_token="set-aside-owner", static_dir=STATIC_DIR,
            resume_storage=root / "resumes", capture_storage=root / "captures", interview_storage=root / "interviews",
        )
        self.client = TestClient(app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tempdir.cleanup()

    def create(self, **overrides):
        response = self.client.post("/api/v1/outreach", headers=AUTH, json={
            "company": "Lumen Fabrication", "website": "https://lumenfab.example", "location": "Austin, TX",
            "contact_name": "Maya Chen", "contact_email": "maya@lumenfab.example", **overrides,
        })
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def mark(self, target, value=True):
        response = self.client.patch(f"/api/v1/outreach/{target['id']}", headers=AUTH, json={"not_interested": value})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def events(self, target):
        detail = self.client.get(f"/api/v1/outreach/{target['id']}", headers=AUTH).json()
        return [event["event_type"] for event in detail["events"]]

    def test_marking_keeps_the_company_and_records_when_and_moving_back_clears_it(self):
        target = self.create()
        self.assertIsNone(target["not_interested_at"])
        marked = self.mark(target)
        self.assertTrue(marked["not_interested_at"])
        listing = self.client.get("/api/v1/outreach", headers=AUTH).json()
        self.assertEqual([item["id"] for item in listing["items"]], [target["id"]], "kept, never deleted")
        self.assertEqual(self.mark(target)["not_interested_at"], marked["not_interested_at"], "marking again changes nothing")
        self.assertEqual(self.events(target).count("not_interested"), 1)
        back = self.mark(target, False)
        self.assertIsNone(back["not_interested_at"])
        self.assertIn("interested_again", self.events(target))
        self.assertEqual(back["status"], target["status"], "its status is left as it was")

    def test_a_company_set_aside_cannot_be_deleted_until_it_is_moved_back(self):
        target = self.create()
        self.mark(target)
        refused = self.client.delete(f"/api/v1/outreach/{target['id']}", headers=AUTH)
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertIn("Not interested", refused.json()["detail"])
        self.assertEqual(self.client.get(f"/api/v1/outreach/{target['id']}", headers=AUTH).status_code, 200)
        self.mark(target, False)
        self.assertEqual(self.client.delete(f"/api/v1/outreach/{target['id']}", headers=AUTH).status_code, 204)

    def test_export_and_import_keep_it_set_aside(self):
        target = self.create()
        marked = self.mark(target)
        csv_export = self.client.get("/api/v1/outreach/export?format=csv", headers=AUTH)
        self.assertIn("not_interested_at", csv_export.text.splitlines()[0])
        json_export = self.client.get("/api/v1/outreach/export?format=json", headers=AUTH)
        self.assertEqual(json_export.json()["items"][0]["not_interested_at"], marked["not_interested_at"])
        with tempfile.TemporaryDirectory() as other:
            _, other_path = build_and_migrate(Path(other))
            app = create_app(
                db_path=other_path, access_token="set-aside-owner", static_dir=STATIC_DIR, resume_storage=Path(other) / "resumes",
                capture_storage=Path(other) / "captures", interview_storage=Path(other) / "interviews",
            )
            with TestClient(app) as fresh:
                imported = fresh.post("/api/v1/outreach/import", headers=AUTH,
                                      files={"upload": ("outreach.csv", csv_export.content, "text/csv")}).json()
                self.assertEqual(imported["imported"], 1, imported)
                [item] = fresh.get("/api/v1/outreach", headers=AUTH).json()["items"]
                self.assertEqual(item["not_interested_at"], marked["not_interested_at"])

    def test_the_deep_search_still_finds_it_tracked(self):
        target = self.create()
        self.mark(target)
        with closing(connect_product(self.platform_path)) as conn:
            names, domains = existing_keys(conn, user_id=USER)
        self.assertIn(company_key("Lumen Fabrication"), names)
        self.assertIn("lumenfab.example", domains)


class NotInterestedAutomationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        _, self.platform_path = build_and_migrate(Path(self.tempdir.name))
        self.conn = connect_product(self.platform_path)
        self.addCleanup(self.conn.close)

    def target(self, name="Tidewater Robotics", **values):
        slug = name.lower().replace(" ", "")
        return create_target(self.conn, {
            "company": name, "website": f"https://{slug}.example", "location": "Houston, TX",
            "contact_email": f"jordan@{slug}.example", **values,
        }, user_id=USER)

    def sent(self, name="Fernwood Bio", **values):
        return self.target(name, status="sent", sent_at=(date.today() - timedelta(days=9)).isoformat(),
                           follow_up_at=(date.today() - timedelta(days=2)).isoformat(),
                           email_subject="Internship question", email_body="Hi Alex,\n\nA short note.\n\nSam", **values)

    def set_aside(self, target):
        return update_target(self.conn, target["id"], {"not_interested": True}, user_id=USER)

    def test_list_targets_leaves_it_out_only_when_automation_asks(self):
        kept, other = self.target(), self.target("Quarry Analytics")
        self.set_aside(kept)
        self.assertEqual({item["id"] for item in list_targets(self.conn, user_id=USER)}, {kept["id"], other["id"]})
        self.assertEqual([item["id"] for item in list_targets(self.conn, user_id=USER, interested_only=True)], [other["id"]])

    def test_no_first_draft_is_written_for_it(self):
        target = self.target()
        self.assertEqual(draft_due(self.conn, user_id=USER), [target["id"]])
        self.set_aside(target)
        self.assertEqual(draft_due(self.conn, user_id=USER), [])

    def test_no_follow_up_comes_due_is_drafted_or_reminded(self):
        target = self.sent()
        self.assertTrue(get_target(self.conn, target["id"], user_id=USER)["follow_up_due"])
        self.assertEqual([item["id"] for item in internal_automation.follow_up_draft_due(self.conn, USER)], [target["id"]])
        self.set_aside(target)
        self.assertFalse(get_target(self.conn, target["id"], user_id=USER)["follow_up_due"])
        self.assertEqual(internal_automation.follow_up_draft_due(self.conn, USER), [])
        self.assertEqual(queue_follow_up_reminders(self.conn)["due"], 0)

    def test_a_quiet_company_is_not_closed_as_no_response(self):
        target = self.target("Meridian Soil", status="followed_up", sent_at=(date.today() - timedelta(days=27)).isoformat(),
                             follow_up_at=(date.today() - timedelta(days=20)).isoformat(),
                             email_subject="Internship question", email_body="Hi Tom,\n\nA short note.\n\nSam")
        self.assertEqual([item["id"] for item in internal_automation.auto_close_due(self.conn, USER)], [target["id"]])
        self.set_aside(target)
        self.assertEqual(internal_automation.auto_close_due(self.conn, USER), [])

    def test_no_revisit_comes_due(self):
        target = self.target("Cobalt Instruments", status="paused", follow_up_at=(date.today() - timedelta(days=1)).isoformat())
        self.assertTrue(get_target(self.conn, target["id"], user_id=USER)["revisit_due"])
        self.set_aside(target)
        self.assertFalse(get_target(self.conn, target["id"], user_id=USER)["revisit_due"])

    def test_urgent_leaves_it_out(self):
        target = self.sent()
        shown = lambda: [item.get("outreach_target_id") for item in urgent_queue(self.conn, user_id=USER)["items"]]
        self.assertIn(target["id"], shown())
        self.set_aside(target)
        self.assertNotIn(target["id"], shown())

    def test_find_people_and_company_research_leave_it_alone(self):
        target = self.target(contact_email="info@tidewaterrobotics.example")
        self.assertEqual(eligible_targets(self.conn, user_id=USER), [target["id"]])
        self.assertEqual(due_for_research(self.conn, user_id=USER, only_replied=False), [target["id"]])
        self.set_aside(target)
        self.assertEqual(eligible_targets(self.conn, user_id=USER), [])
        self.assertEqual(due_for_research(self.conn, user_id=USER, only_replied=False), [])

    def replied(self, name):
        target = self.target(name, status="replied", sent_at=(date.today() - timedelta(days=5)).isoformat(),
                             email_subject="Internship question", email_body="Hi,\n\nA short note.\n\nSam")
        with self.conn:
            log_event(self.conn, target["id"], USER, "reply_logged", detail="Thanks for writing. Could we talk next week?")
        return target

    def test_call_prep_and_the_thank_you_do_not_start_on_their_own(self):
        kept, other = self.replied("Pinecone Health"), self.replied("Harbor Freight Labs")
        self.assertEqual(set(thank_you_due(self.conn, USER)), {kept["id"], other["id"]})
        self.set_aside(kept)
        self.assertEqual(thank_you_due(self.conn, USER), [other["id"]])
        self.assertFalse(auto_queue_call_prep(self.conn, kept["id"], user_id=USER, reason="Status moved to replied"))
        self.assertIsNone(get_target(self.conn, kept["id"], user_id=USER).get("call_prep_job"))
        self.assertTrue(auto_queue_call_prep(self.conn, other["id"], user_id=USER, reason="Status moved to replied"),
                        "the same company still in play gets call prep on its own")

    def test_the_column_is_added_once_and_repairs_a_database_that_lacks_it(self):
        migration = REPO / "migrations" / "0047_outreach_not_interested.sql"
        self.assertIn("0047_outreach_not_interested.sql", {row[0] for row in self.conn.execute("SELECT name FROM schema_migrations")})
        schema._apply_outreach_not_interested(self.conn, migration.read_text(encoding="utf-8"))  # a second run changes nothing
        with self.conn:
            self.conn.execute("ALTER TABLE outreach_targets DROP COLUMN not_interested_at")
        schema._apply_outreach_not_interested(self.conn, migration.read_text(encoding="utf-8"))
        self.assertIn("not_interested_at", {row[1] for row in self.conn.execute("PRAGMA table_info(outreach_targets)")})


if __name__ == "__main__":
    unittest.main()
