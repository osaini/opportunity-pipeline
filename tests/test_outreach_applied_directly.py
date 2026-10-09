"""Applied directly: a company the student applied to on its own site is set aside like Not interested, for its own reason."""

import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR
from opportunity_app.api import create_app
from opportunity_app.automation import internal as internal_automation
from opportunity_app.core import schema
from opportunity_app.core.database import connect_product
from opportunity_app.outreach.automation import draft_due
from opportunity_app.outreach.targets import create_target, get_target, list_targets, log_event, queue_follow_up_reminders, update_target
from opportunity_app.outreach.thank_you import due as thank_you_due, problem_now

from helpers_platform import build_and_migrate

AUTH = {"Authorization": "Bearer applied-owner"}
USER = "local-user"
REPO = Path(__file__).resolve().parents[1]


class AppliedDirectlyApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        _, self.platform_path = build_and_migrate(root)
        app = create_app(
            db_path=self.platform_path, access_token="applied-owner", static_dir=STATIC_DIR,
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

    def patch(self, target, **body):
        response = self.client.patch(f"/api/v1/outreach/{target['id']}", headers=AUTH, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def events(self, target):
        detail = self.client.get(f"/api/v1/outreach/{target['id']}", headers=AUTH).json()
        return [event["event_type"] for event in detail["events"]]

    def test_marking_keeps_the_company_and_says_why_and_moving_back_clears_both(self):
        target = self.create()
        self.assertFalse(target["applied_directly"])
        marked = self.patch(target, applied_directly=True)
        self.assertTrue(marked["not_interested_at"])
        self.assertTrue(marked["applied_directly"])
        self.assertEqual(marked["set_aside_reason"], "applied_directly")
        self.assertEqual(self.patch(target, applied_directly=True)["not_interested_at"], marked["not_interested_at"], "again changes nothing")
        self.assertEqual(self.events(target).count("applied_directly"), 1)
        back = self.patch(target, not_interested=False)
        self.assertIsNone(back["not_interested_at"])
        self.assertFalse(back["applied_directly"])
        self.assertEqual(back["set_aside_reason"], "")
        self.assertIn("interested_again", self.events(target))
        self.assertEqual(back["status"], target["status"], "its status is left as it was")

    def test_not_interested_is_not_read_as_applied_directly_and_the_two_can_be_switched(self):
        target = self.create()
        self.assertFalse(self.patch(target, not_interested=True)["applied_directly"])
        switched = self.patch(target, applied_directly=True)
        self.assertTrue(switched["applied_directly"])
        self.assertEqual(self.events(target).count("applied_directly"), 1, "moving between reasons is recorded")
        again = self.patch(target, not_interested=True)
        self.assertFalse(again["applied_directly"])
        self.assertTrue(again["not_interested_at"])
        self.assertEqual(self.events(target).count("not_interested"), 2)

    def test_it_is_kept_not_deleted_until_moved_back(self):
        target = self.create()
        self.patch(target, applied_directly=True)
        refused = self.client.delete(f"/api/v1/outreach/{target['id']}", headers=AUTH)
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertIn("Applied directly", refused.json()["detail"])
        self.patch(target, applied_directly=False)
        self.assertEqual(self.client.delete(f"/api/v1/outreach/{target['id']}", headers=AUTH).status_code, 204)

    def test_export_and_import_keep_the_reason(self):
        target = self.create()
        marked = self.patch(target, applied_directly=True)
        csv_export = self.client.get("/api/v1/outreach/export?format=csv", headers=AUTH)
        self.assertIn("set_aside_reason", csv_export.text.splitlines()[0])
        with tempfile.TemporaryDirectory() as other:
            _, other_path = build_and_migrate(Path(other))
            app = create_app(
                db_path=other_path, access_token="applied-owner", static_dir=STATIC_DIR, resume_storage=Path(other) / "resumes",
                capture_storage=Path(other) / "captures", interview_storage=Path(other) / "interviews",
            )
            with TestClient(app) as fresh:
                imported = fresh.post("/api/v1/outreach/import", headers=AUTH,
                                      files={"upload": ("outreach.csv", csv_export.content, "text/csv")}).json()
                self.assertEqual(imported["imported"], 1, imported)
                [item] = fresh.get("/api/v1/outreach", headers=AUTH).json()["items"]
                self.assertEqual(item["not_interested_at"], marked["not_interested_at"])
                self.assertTrue(item["applied_directly"])


class AppliedDirectlyAutomationTests(unittest.TestCase):
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

    def applied(self, target):
        return update_target(self.conn, target["id"], {"applied_directly": True}, user_id=USER)

    def test_automation_skips_it_like_a_company_not_interested(self):
        target = self.target()
        self.assertEqual(draft_due(self.conn, user_id=USER), [target["id"]])
        self.applied(target)
        self.assertEqual(draft_due(self.conn, user_id=USER), [])
        self.assertEqual(list_targets(self.conn, user_id=USER, interested_only=True), [])

    def test_no_follow_up_comes_due_is_drafted_or_reminded(self):
        target = self.target("Fernwood Bio", status="sent", sent_at=(date.today() - timedelta(days=9)).isoformat(),
                             follow_up_at=(date.today() - timedelta(days=2)).isoformat(),
                             email_subject="Internship question", email_body="Hi Alex,\n\nA short note.\n\nSam")
        self.assertTrue(get_target(self.conn, target["id"], user_id=USER)["follow_up_due"])
        self.applied(target)
        self.assertFalse(get_target(self.conn, target["id"], user_id=USER)["follow_up_due"])
        self.assertEqual(internal_automation.follow_up_draft_due(self.conn, USER), [])
        self.assertEqual(queue_follow_up_reminders(self.conn)["due"], 0)

    def test_a_thank_you_stops_and_says_applied_directly(self):
        target = self.target("Pinecone Health", status="replied", sent_at=(date.today() - timedelta(days=5)).isoformat(),
                             email_subject="Internship question", email_body="Hi,\n\nA short note.\n\nSam")
        with self.conn:
            log_event(self.conn, target["id"], USER, "reply_logged", detail="Thanks, but we are not hiring.", data={"gmail_id": "d-1"})
        self.assertEqual(thank_you_due(self.conn, USER), [target["id"]])
        self.applied(target)
        self.assertEqual(thank_you_due(self.conn, USER), [])
        stopped = problem_now(
            self.conn, target["id"], USER,
            {"reply_gmail_id": "d-1", "to_email": "jordan@pinecone.example", "created_at": datetime.now(timezone.utc).isoformat()},
        )
        self.assertIsNotNone(stopped)
        self.assertIn("applied directly", stopped[1])
        self.assertNotIn("not interested", stopped[1])

    def test_the_column_is_added_once_and_repairs_a_database_that_lacks_it(self):
        migration = REPO / "migrations" / "0050_outreach_applied_directly.sql"
        self.assertIn("0050_outreach_applied_directly.sql", {row[0] for row in self.conn.execute("SELECT name FROM schema_migrations")})
        schema._apply_outreach_applied_directly(self.conn, migration.read_text(encoding="utf-8"))  # a second run changes nothing
        with self.conn:
            self.conn.execute("ALTER TABLE outreach_targets DROP COLUMN set_aside_reason")
        schema._apply_outreach_applied_directly(self.conn, migration.read_text(encoding="utf-8"))
        self.assertIn("set_aside_reason", {row[1] for row in self.conn.execute("PRAGMA table_info(outreach_targets)")})


if __name__ == "__main__":
    unittest.main()
