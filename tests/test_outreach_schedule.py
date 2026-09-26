"""Approved emails queued for the recipient's weekday morning, and sent then through the once-only path."""

import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR
from opportunity_app.api import create_app
from opportunity_app.outreach_automation import AutomationWorker
from opportunity_app.outreach_schedule import next_morning, recipient_zone, run_due_sends
from opportunity_app.schema import connect_product, utc_now

from helpers_platform import build_and_migrate
from test_outreach_gmail import ACCOUNT, PDF, SCOPES, FakeGmail

AUTH = {"Authorization": "Bearer schedule-owner"}
USER = "local-user"
CHICAGO = ZoneInfo("America/Chicago")


class SendTimeTests(unittest.TestCase):
    def local(self, when):
        return when.astimezone(CHICAGO)

    def test_the_next_weekday_morning_in_their_zone(self):
        friday_noon = datetime(2026, 9, 25, 12, 0, tzinfo=CHICAGO)
        monday = self.local(next_morning(friday_noon, CHICAGO, "seed"))
        self.assertEqual((monday.strftime("%A"), monday.hour), ("Monday", 9))
        self.assertLess(monday.minute, 40)
        tuesday_early = datetime(2026, 9, 29, 7, 0, tzinfo=CHICAGO)
        same_day = self.local(next_morning(tuesday_early, CHICAGO, "seed"))
        self.assertEqual((same_day.date(), same_day.hour), (tuesday_early.date(), 9), "before 9 it goes the same morning")
        saturday = datetime(2026, 9, 26, 8, 0, tzinfo=CHICAGO)
        self.assertEqual(self.local(next_morning(saturday, CHICAGO, "seed")).strftime("%A"), "Monday")

    def test_the_minute_is_spread_but_stable(self):
        now = datetime(2026, 9, 25, 12, 0, tzinfo=CHICAGO)
        minutes = {self.local(next_morning(now, CHICAGO, f"target-{n}")).minute for n in range(30)}
        self.assertGreater(len(minutes), 5, "a batch does not all land at 9:00")
        self.assertEqual(next_morning(now, CHICAGO, "same"), next_morning(now, CHICAGO, "same"))

    def test_the_zone_comes_from_their_state_or_else_the_students(self):
        with tempfile.TemporaryDirectory() as root:
            _, path = build_and_migrate(Path(root))
            with closing(connect_product(path)) as conn:
                def zone_for(location):
                    return str(recipient_zone(conn, {"location": location}, user_id=USER)[0])

                self.assertEqual(zone_for("Austin, TX"), "America/Chicago")
                self.assertEqual(zone_for("El Paso, TX"), "America/Denver")
                self.assertEqual(zone_for("Seattle, Washington"), "America/Los_Angeles")
                _zone, basis = recipient_zone(conn, {"location": "Berlin, Germany"}, user_id=USER)
                self.assertIn("your time", basis)


class ScheduledSendTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        _, self.platform_path = build_and_migrate(root)
        attachment = root / "Resume.pdf"
        attachment.write_bytes(PDF)
        self.key = Fernet.generate_key().decode()
        self.env = mock.patch.dict("os.environ", {
            "GOOGLE_OAUTH_CLIENT_ID": "client-id", "GOOGLE_OAUTH_CLIENT_SECRET": "client-secret",
            "PIPELINE_CONNECTION_KEY": self.key, "PIPELINE_OUTREACH_ACCOUNT": ACCOUNT,
            "PIPELINE_OUTREACH_COMPOSE": "gmail", "PIPELINE_OUTREACH_ATTACHMENT": str(attachment),
        })
        self.env.start()
        self.gmail = FakeGmail()
        self.factory = lambda: httpx.Client(transport=httpx.MockTransport(self.gmail.handler))
        app = create_app(
            db_path=self.platform_path, access_token="schedule-owner", static_dir=STATIC_DIR,
            resume_storage=root / "resumes", capture_storage=root / "captures", interview_storage=root / "interviews",
            outreach_gmail_client_factory=self.factory,
        )
        self.client = TestClient(app)
        self.client.__enter__()
        self.conn = connect_product(self.platform_path)

    def tearDown(self):
        self.conn.close()
        self.client.__exit__(None, None, None)
        self.env.stop()
        self.tempdir.cleanup()

    def connect(self):
        fernet = Fernet(self.key.encode())
        with self.conn:
            self.conn.execute(
                """INSERT INTO connector_accounts(id, user_id, provider, scopes_json, encrypted_access_token, encrypted_refresh_token, status, created_at, updated_at)
                   VALUES(?, ?, 'gmail_drafts', ?, ?, ?, 'connected', ?, ?)""",
                (f"connector-gmail_drafts-{USER}", USER, str(SCOPES).replace("'", '"'),
                 fernet.encrypt(b"valid-token").decode(), fernet.encrypt(b"refresh-token").decode(), utc_now(), utc_now()),
            )

    def approved(self):
        created = self.client.post("/api/v1/outreach", headers=AUTH, json={
            "company": "Bovi", "contact_email": "greg@bovi.example", "location": "Austin, TX",
            "email_subject": "Robotics internship question", "email_body": "Hi Greg,\n\nShort note about Bovi.\n\nSam",
        }).json()
        approved = self.client.post(f"/api/v1/outreach/{created['id']}/approve", headers=AUTH, json={
            "kind": "initial", "fingerprint": created["draft_fingerprint"], "acknowledge_warnings": True,
        })
        self.assertEqual(approved.status_code, 200, approved.text)
        return approved.json()

    def schedule(self, target):
        return self.client.post(f"/api/v1/outreach/{target['id']}/schedule", headers=AUTH, json={
            "kind": "initial", "fingerprint": target["draft_fingerprint"],
        })

    def target(self, target):
        return self.client.get(f"/api/v1/outreach/{target['id']}", headers=AUTH).json()

    def due(self, target):
        """Run the sender as if the scheduled time had come."""
        send_at = datetime.fromisoformat(self.target(target)["scheduled"]["initial"]["send_at"])
        return run_due_sends(self.conn, client_factory=self.factory, now=send_at + timedelta(minutes=1))

    def test_a_scheduled_email_goes_out_once_at_its_time(self):
        self.connect()
        target = self.approved()
        scheduled = self.schedule(target)
        self.assertEqual(scheduled.status_code, 200, scheduled.text)
        self.assertIn("their time, from Austin, TX", scheduled.json()["label"])
        self.assertEqual(self.target(target)["scheduled"]["initial"]["state"], "scheduled")
        self.assertEqual(run_due_sends(self.conn, client_factory=self.factory), [], "not before its time")
        self.assertEqual(self.gmail.sent, [])
        self.assertEqual([item["state"] for item in self.due(target)], ["sent"])
        self.assertEqual(len(self.gmail.sent), 1)
        after = self.target(target)
        self.assertEqual((after["status"], after["scheduled"]), ("sent", {}))
        self.assertEqual(run_due_sends(self.conn, client_factory=self.factory, now=datetime.now(timezone.utc) + timedelta(days=9)), [])
        self.assertEqual(len(self.gmail.sent), 1)

    def test_changing_the_draft_cancels_the_schedule(self):
        self.connect()
        target = self.approved()
        self.schedule(target)
        send_at = self.target(target)["scheduled"]["initial"]["send_at"]
        self.client.patch(f"/api/v1/outreach/{target['id']}", headers=AUTH, json={"email_body": "Hi Greg,\n\nNew words.\n\nSam"})
        after = self.target(target)
        self.assertEqual(after["scheduled"], {})
        self.assertIn("send_cancelled", [event["event_type"] for event in after["events"]])
        self.assertEqual(run_due_sends(self.conn, client_factory=self.factory, now=datetime.fromisoformat(send_at) + timedelta(minutes=1)), [])
        self.assertEqual(self.gmail.sent, [])

    def test_a_new_recipient_cancels_it_too(self):
        self.connect()
        target = self.approved()
        self.schedule(target)
        self.client.patch(f"/api/v1/outreach/{target['id']}", headers=AUTH, json={"contact_email": "dana@bovi.example"})
        self.assertEqual(self.target(target)["scheduled"], {})

    def test_cancel_and_send_now_each_stop_it(self):
        self.connect()
        target = self.approved()
        self.schedule(target)
        cancelled = self.client.delete(f"/api/v1/outreach/{target['id']}/schedule", headers=AUTH)
        self.assertEqual(cancelled.status_code, 200, cancelled.text)
        self.assertEqual(cancelled.json()["scheduled"], {})
        self.schedule(target)
        sent = self.client.post(f"/api/v1/outreach/{target['id']}/gmail-send", headers=AUTH, json={
            "kind": "initial", "fingerprint": target["draft_fingerprint"],
        })
        self.assertEqual(sent.status_code, 200, sent.text)
        self.assertEqual(self.target(target)["scheduled"], {})
        self.assertEqual(run_due_sends(self.conn, client_factory=self.factory, now=datetime.now(timezone.utc) + timedelta(days=9)), [])
        self.assertEqual(len(self.gmail.sent), 1)

    def test_marking_it_sent_by_hand_cancels_the_scheduled_copy(self):
        self.connect()
        target = self.approved()
        self.schedule(target)
        self.client.patch(f"/api/v1/outreach/{target['id']}", headers=AUTH, json={"status": "sent"})
        after = self.target(target)
        self.assertEqual(after["scheduled"], {})
        self.assertIn("You marked it sent", [event["detail"] for event in after["events"] if event["event_type"] == "send_cancelled"])
        self.assertEqual(run_due_sends(self.conn, client_factory=self.factory, now=datetime.now(timezone.utc) + timedelta(days=9)), [])
        self.assertEqual(self.gmail.sent, [])

    def test_scheduling_needs_gmail_and_an_approved_draft(self):
        target = self.approved()
        refused = self.schedule(target)
        self.assertEqual(refused.status_code, 422)
        self.assertIn("Connect Gmail", refused.json()["detail"])
        self.connect()
        stale = self.client.post(f"/api/v1/outreach/{target['id']}/schedule", headers=AUTH, json={"kind": "initial", "fingerprint": "old"})
        self.assertEqual(stale.status_code, 409)

    def test_gmail_out_of_reach_is_retried_then_reported(self):
        self.connect()
        target = self.approved()
        self.schedule(target)
        self.gmail.send_unreached = httpx.ConnectError("offline")
        self.assertEqual([item["state"] for item in self.due(target)], ["retrying"])
        self.assertEqual(self.target(target)["scheduled"]["initial"]["state"], "scheduled")
        self.due(target)
        self.assertEqual([item["state"] for item in self.due(target)], ["failed"])
        stopped = self.target(target)["scheduled"]["initial"]
        self.assertEqual(stopped["state"], "failed")
        self.assertIn("Nothing was sent", stopped["error"])
        self.assertEqual(self.gmail.sent, [])

    def test_a_send_cut_off_by_a_restart_is_reported_not_repeated(self):
        self.connect()
        target = self.approved()
        self.schedule(target)
        long_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="microseconds")
        with self.conn:
            self.conn.execute("UPDATE outreach_scheduled_sends SET state='sending', updated_at=?", (long_ago,))
        run_due_sends(self.conn, client_factory=self.factory)
        stopped = self.target(target)["scheduled"]["initial"]
        self.assertEqual(stopped["state"], "failed")
        self.assertIn("Check your Gmail Sent folder", stopped["error"])
        self.assertEqual(self.gmail.sent, [])

    def test_the_worker_sends_what_is_due_even_with_the_switch_off(self):
        self.connect()
        target = self.approved()
        self.schedule(target)
        with self.conn:
            self.conn.execute("UPDATE outreach_scheduled_sends SET send_at=?", ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds"),))
        worker = AutomationWorker(self.platform_path, fetcher_factory=lambda: None, gmail_client_factory=self.factory)
        self.assertEqual([item["state"] for item in worker.run_once()["sent"]], ["sent"])
        self.assertEqual(self.target(target)["status"], "sent")


if __name__ == "__main__":
    unittest.main()
