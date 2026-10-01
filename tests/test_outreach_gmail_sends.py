"""Drafts the student sends or schedules in Gmail itself, and scheduled sends that miss their morning."""

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR, outreach_delivery, outreach_gmail, outreach_gmail_sends, outreach_inbox
from opportunity_app.api import create_app
from opportunity_app.outreach_automation import update_settings
from opportunity_app.outreach_gmail_sends import capture_gmail_sends
from opportunity_app.outreach_schedule import run_due_sends
from opportunity_app.database import connect_product
from opportunity_app.timestamps import utc_now

from helpers_platform import build_and_migrate
from helpers_gmail import ACCOUNT, PDF, SCOPES, FakeGmail, delivery_report, forget_gmail_backoff, rate_limited

AUTH = {"Authorization": "Bearer gmail-sends-owner"}
USER = "local-user"


def sent_message(message_id, *, thread_id="other", to="greg@bovi.example", subject="Robotics internship question", minutes_from_now=5):
    received = int((datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)).timestamp() * 1000)
    return {"id": message_id, "threadId": thread_id, "labelIds": ["SENT"], "internalDate": str(received),
            "payload": {"headers": [{"name": "To", "value": to}, {"name": "Subject", "value": subject}]}}


class GmailSendsTests(unittest.TestCase):
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
            db_path=self.platform_path, access_token="gmail-sends-owner", static_dir=STATIC_DIR,
            resume_storage=root / "resumes", capture_storage=root / "captures", interview_storage=root / "interviews",
            outreach_gmail_client_factory=self.factory,
        )
        self.client = TestClient(app)
        self.client.__enter__()
        self.conn = connect_product(self.platform_path)
        for cache in (outreach_gmail_sends._LAST_LOOK, outreach_delivery._LAST_LOOK, outreach_delivery._READ_NOTICES, outreach_inbox._LAST_CAPTURE):
            cache.clear()
        forget_gmail_backoff(self)
        fernet = Fernet(self.key.encode())
        with self.conn:
            self.conn.execute(
                """INSERT INTO connector_accounts(id, user_id, provider, scopes_json, encrypted_access_token, encrypted_refresh_token, status, created_at, updated_at)
                   VALUES(?, ?, 'gmail_drafts', ?, ?, ?, 'connected', ?, ?)""",
                (f"connector-gmail_drafts-{USER}", USER, json.dumps(SCOPES),
                 fernet.encrypt(b"valid-token").decode(), fernet.encrypt(b"refresh-token").decode(), utc_now(), utc_now()),
            )

    def tearDown(self):
        self.conn.close()
        self.client.__exit__(None, None, None)
        self.env.stop()
        self.tempdir.cleanup()

    def drafted_in_gmail(self):
        created = self.client.post("/api/v1/outreach", headers=AUTH, json={
            "company": "Bovi", "contact_name": "Greg Lee", "contact_email": "greg@bovi.example", "location": "Austin, TX",
            "email_subject": "Robotics internship question", "email_body": "Hi Greg,\n\nShort note about Bovi.\n\nSam",
        }).json()
        approved = self.client.post(f"/api/v1/outreach/{created['id']}/approve", headers=AUTH, json={
            "kind": "initial", "fingerprint": created["draft_fingerprint"], "acknowledge_warnings": True,
        }).json()
        draft = self.client.post(f"/api/v1/outreach/{approved['id']}/gmail-draft", headers=AUTH, json={"kind": "initial"})
        self.assertEqual(draft.status_code, 200, draft.text)
        self.assertEqual(draft.json()["thread_id"], "18c1", "the draft's thread is kept for matching")
        return approved

    def target(self, target):
        return self.client.get(f"/api/v1/outreach/{target['id']}", headers=AUTH).json()

    def check(self):
        outreach_gmail_sends._LAST_LOOK.clear()
        return capture_gmail_sends(self.conn, user_id=USER, client_factory=self.factory)

    def test_a_draft_still_in_drafts_is_left_alone(self):
        target = self.drafted_in_gmail()
        self.assertEqual(self.check(), {"state": "ok", "sent": [], "scheduled": []})
        self.assertEqual(self.target(target)["status"], "drafted")

    def test_a_draft_sent_from_gmail_is_recorded_like_a_send_from_the_app(self):
        target = self.drafted_in_gmail()
        del self.gmail.drafts["r-1"]  # the student sent it in Gmail, which kept its thread
        self.gmail.metadata["ui-1"] = sent_message("ui-1", thread_id="18c1", subject="Robotics internship question (edited)")
        self.gmail.sent_search = ["ui-1"]
        result = self.check()
        self.assertEqual([item["target_id"] for item in result["sent"]], [target["id"]])
        after = self.target(target)
        self.assertEqual(after["status"], "sent")
        self.assertTrue(after["follow_up_at"])
        sent_events = [json.loads(event["detail"]) for event in after["events"] if event["event_type"] == "gmail_sent"]
        self.assertEqual([(event["sent_from"], event["thread_id"]) for event in sent_events], [("gmail", "18c1")])
        again = self.client.post(f"/api/v1/outreach/{target['id']}/gmail-send", headers=AUTH, json={
            "kind": "initial", "fingerprint": target["draft_fingerprint"],
        })
        self.assertEqual(again.status_code, 422, "never sent a second time from the app")
        self.assertEqual(self.gmail.sent, [])
        self.assertEqual(self.check()["sent"], [], "recorded once")

    def sent_events(self, target):
        return [json.loads(event["detail"]) for event in self.target(target)["events"] if event["event_type"] == "gmail_sent"]

    def test_a_send_made_in_gmail_records_when_gmail_sent_it(self):
        target = self.drafted_in_gmail()
        del self.gmail.drafts["r-1"]
        sent = sent_message("ui-1", thread_id="18c1")
        self.gmail.metadata["ui-1"] = sent
        self.gmail.sent_search = ["ui-1"]
        self.assertEqual(len(self.check()["sent"]), 1)
        [event] = self.sent_events(target)
        # Gmail's own time for the message (internalDate, in milliseconds), not when the app noticed it.
        self.assertEqual(event["sent_ms"], int(sent["internalDate"]))
        self.assertIsInstance(event["sent_ms"], int, "a number outreach_inbox compares, not Gmail's string")

    def test_a_reply_that_came_before_the_app_noticed_a_send_made_in_gmail_is_still_a_reply(self):
        target = self.drafted_in_gmail()
        now = datetime.now(timezone.utc)
        with self.conn:
            # The draft was made three hours ago and sent from Gmail two hours ago, with the laptop closed since.
            self.conn.execute("UPDATE outreach_events SET created_at=? WHERE target_id=? AND event_type='gmail_draft_created'",
                              ((now - timedelta(hours=3)).isoformat(timespec="microseconds"), target["id"]))
            # These reply rules started yesterday (mail from before them is never counted without the student).
            self.conn.execute("UPDATE schema_migrations SET applied_at=? WHERE name=?",
                              ((now - timedelta(days=1)).isoformat(), outreach_inbox.MIGRATION))
        del self.gmail.drafts["r-1"]
        sent = sent_message("18c1", thread_id="18c1", minutes_from_now=-120)
        self.gmail.metadata["18c1"] = sent
        self.assertEqual(len(self.check()["sent"]), 1, "the draft's own message, now labelled Sent")
        [event] = self.sent_events(target)
        self.assertEqual(event["sent_ms"], int(sent["internalDate"]))
        recorded = self.conn.execute("SELECT created_at FROM outreach_events WHERE target_id=? AND event_type='gmail_sent'",
                                     (target["id"],)).fetchone()[0]
        self.assertGreater(datetime.fromisoformat(recorded), now - timedelta(minutes=5), "recorded when the app noticed, just now")
        # Greg wrote back an hour ago, in a fresh email: after Gmail sent it, before the app noticed.
        self.gmail.raw["greg-1"] = ((
            f"From: Greg Lee <greg@bovi.example>\nTo: {ACCOUNT}\nSubject: Re: Robotics internship question\n"
            "MIME-Version: 1.0\nContent-Type: text/plain; charset=UTF-8\n\nHappy to talk next week. Does Tuesday work?\n"
        ).encode(), int((now - timedelta(hours=1)).timestamp() * 1000))
        self.gmail.inbox_replies.append("greg-1")
        [watched] = [item for item in outreach_inbox._watched(self.conn, USER, now) if item["id"] == target["id"]]
        self.assertEqual(round(watched["since"].timestamp() * 1000), int(sent["internalDate"]), "watched from when Gmail sent it")
        outreach_inbox._LAST_CAPTURE.clear()
        result = self.client.post("/api/v1/outreach/inbox-check", headers=AUTH).json()
        self.assertEqual([item["target_id"] for item in result["replies"]], [target["id"]], "not set aside as mail from before the send")
        self.assertEqual(self.target(target)["status"], "replied")

    def test_a_bounce_that_came_before_the_app_noticed_a_send_made_in_gmail_is_still_found(self):
        target = self.drafted_in_gmail()
        now = datetime.now(timezone.utc)
        with self.conn:
            # Made three hours ago, sent by Gmail's Schedule send two hours ago while the laptop was closed.
            self.conn.execute("UPDATE outreach_events SET created_at=? WHERE target_id=? AND event_type='gmail_draft_created'",
                              ((now - timedelta(hours=3)).isoformat(timespec="microseconds"), target["id"]))
        del self.gmail.drafts["r-1"]
        sent = sent_message("18c1", thread_id="18c1", minutes_from_now=-120)
        self.gmail.metadata["18c1"] = sent
        self.assertEqual(len(self.check()["sent"]), 1)
        # Greg's address failed a minute after Gmail sent it, and the server's notice is not in the email's thread.
        self.gmail.inbox_notices = ["dsn-elsewhere"]
        self.gmail.raw["dsn-elsewhere"] = (delivery_report(failed=["greg@bovi.example"]), int(sent["internalDate"]) + 60_000)
        outreach_delivery._LAST_LOOK.clear()
        outreach_delivery._READ_NOTICES.clear()
        result = outreach_delivery.check_deliveries(self.conn, user_id=USER, client_factory=self.factory)
        self.assertEqual(result["state"], "ok")
        self.assertEqual([item["target_id"] for item in result["bounced"]], [target["id"]],
                         "the notice came after Gmail sent the email, though before the app noticed the send")

    def test_a_sent_message_is_matched_by_its_own_id_or_by_subject(self):
        target = self.drafted_in_gmail()
        del self.gmail.drafts["r-1"]
        self.gmail.metadata["18c1"] = sent_message("18c1", thread_id="18c1")
        self.assertEqual(len(self.check()["sent"]), 1, "the draft's own message, now labelled Sent")
        self.assertEqual(self.target(target)["status"], "sent")

    def test_an_unrelated_message_to_the_same_person_is_not_taken_for_it(self):
        target = self.drafted_in_gmail()
        del self.gmail.drafts["r-1"]
        self.gmail.metadata["other-1"] = sent_message("other-1", subject="Coffee next week?")
        self.gmail.metadata["old-1"] = sent_message("old-1", minutes_from_now=-120)
        self.gmail.sent_search = ["other-1", "old-1"]
        self.assertEqual(self.check()["sent"], [])
        self.assertEqual(self.target(target)["status"], "drafted")

    def test_a_draft_waiting_in_gmails_scheduled_folder_is_noted_once(self):
        target = self.drafted_in_gmail()
        del self.gmail.drafts["r-1"]
        self.gmail.metadata["18c1"] = {"id": "18c1", "threadId": "18c1", "labelIds": ["SCHEDULED"], "payload": {"headers": []}}
        self.assertEqual([item["target_id"] for item in self.check()["scheduled"]], [target["id"]])
        self.assertEqual(self.check()["scheduled"], [])
        after = self.target(target)
        self.assertEqual(after["status"], "drafted", "not sent until Gmail sends it")
        self.assertEqual([event["event_type"] for event in after["events"]].count("gmail_scheduled"), 1)

    def test_a_gmail_error_is_reported_not_taken_as_not_sent(self):
        target = self.drafted_in_gmail()
        del self.gmail.drafts["r-1"]
        self.gmail.thread_status = 400  # the Sent search fails
        self.assertEqual(self.check()["state"], "unreachable")
        # A Gmail server error holds reads back as a rate limit does; the draft is still not taken as unsent.
        self.gmail.thread_status = 503
        self.assertEqual(self.check()["state"], "throttled")
        self.assertEqual(self.target(target)["status"], "drafted")

    def test_a_look_that_never_reached_gmail_is_taken_again_on_the_next_check(self):
        self.drafted_in_gmail()
        real, unreachable = self.gmail.handler, [True]

        def handler(request):
            if unreachable[0]:
                raise httpx.ConnectError("no route to Gmail")
            return real(request)

        self.gmail.handler = handler
        self.assertEqual(capture_gmail_sends(self.conn, user_id=USER, client_factory=self.factory)["state"], "unreachable")
        unreachable[0] = False
        asked = len(self.gmail.requests)
        # Not self.check(): that forgets every look. The failed look must have been forgotten already.
        self.assertEqual(capture_gmail_sends(self.conn, user_id=USER, client_factory=self.factory)["state"], "ok")
        self.assertGreater(len(self.gmail.requests), asked, "looked again, not an interval later")

    def test_a_gmail_slowdown_is_reported_and_the_draft_looked_at_again_once_it_ends(self):
        self.drafted_in_gmail()
        self.gmail.read_response = rate_limited
        self.assertEqual(capture_gmail_sends(self.conn, user_id=USER, client_factory=self.factory)["state"], "throttled")
        outreach_gmail._BACKOFF.clear()  # Gmail's wait is over
        with self.conn:
            self.conn.execute("UPDATE connector_accounts SET backoff_until=NULL")
        self.gmail.read_response = None
        asked = len(self.gmail.requests)
        # Not self.check(): that forgets every look. The throttled look must have been forgotten already.
        self.assertEqual(capture_gmail_sends(self.conn, user_id=USER, client_factory=self.factory)["state"], "ok")
        self.assertTrue(any("/drafts/" in r.url.path for r in self.gmail.requests[asked:]), "looked at again straight away")

    def test_the_page_check_reports_a_send_made_in_gmail(self):
        target = self.drafted_in_gmail()
        del self.gmail.drafts["r-1"]
        self.gmail.metadata["18c1"] = sent_message("18c1", thread_id="18c1")
        outreach_inbox._LAST_CAPTURE.clear()
        result = self.client.post("/api/v1/outreach/inbox-check", headers=AUTH).json()
        self.assertEqual([item["target_id"] for item in result["sent_in_gmail"]], [target["id"]])


class NeverSendLateTests(GmailSendsTests):
    """Reuses the setup; only these tests run here."""

    def test_a_send_that_missed_its_morning_moves_to_the_next(self):
        update_settings(self.conn, {"scheduled_sending": True}, user_id=USER)
        created = self.client.post("/api/v1/outreach", headers=AUTH, json={
            "company": "Kiva", "contact_email": "ana@kiva.example", "location": "Austin, TX",
            "email_subject": "Hello", "email_body": "Hi Ana,\n\nA note.\n\nSam",
        }).json()
        approved = self.client.post(f"/api/v1/outreach/{created['id']}/approve", headers=AUTH, json={
            "kind": "initial", "fingerprint": created["draft_fingerprint"], "acknowledge_warnings": True,
        }).json()
        scheduled = self.client.post(f"/api/v1/outreach/{approved['id']}/schedule", headers=AUTH, json={
            "kind": "initial", "fingerprint": approved["draft_fingerprint"],
        })
        self.assertEqual(scheduled.status_code, 200, scheduled.text)
        send_at = datetime.fromisoformat(scheduled.json()["send_at"])
        woke = send_at + timedelta(hours=13)  # the laptop was opened that evening
        self.assertEqual([item["state"] for item in run_due_sends(self.conn, client_factory=self.factory, now=woke)], ["moved"])
        self.assertEqual(self.gmail.sent, [], "nothing lands at an odd hour")
        moved = self.target(approved)["scheduled"]["initial"]
        self.assertEqual(moved["state"], "scheduled")
        self.assertGreater(datetime.fromisoformat(moved["send_at"]), woke)
        self.assertIn("asleep or off", moved["error"])
        on_time = datetime.fromisoformat(moved["send_at"]) + timedelta(minutes=30)
        self.assertEqual([item["state"] for item in run_due_sends(self.conn, client_factory=self.factory, now=on_time)], ["sent"])
        self.assertEqual(len(self.gmail.sent), 1)


# The subclass inherits the Gmail-send tests; run them only once.
for _name in [name for name in dir(GmailSendsTests) if name.startswith("test_")]:
    setattr(NeverSendLateTests, _name, None)


if __name__ == "__main__":
    unittest.main()
