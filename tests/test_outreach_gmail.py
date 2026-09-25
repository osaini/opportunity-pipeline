"""Approved outreach drafts written to Gmail Drafts, or sent once confirmed, against a mocked Gmail."""

import base64
import email
import json
import sys
import tempfile
import unittest
from contextlib import closing
from email import policy
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR
from opportunity_app.api import create_app
from opportunity_app.schema import connect_product, utc_now

from helpers_platform import build_and_migrate

AUTH = {"Authorization": "Bearer gmail-owner"}
USER = "local-user"
ACCOUNT = "student@school.example"
PDF = b"%PDF-1.4 fake resume"


class FakeGmail:
    """A scripted Gmail API and token endpoint that records every request."""

    def __init__(self):
        self.requests = []
        self.profile_email = ACCOUNT
        self.expired_tokens = set()
        self.refresh_ok = True
        self.drafts = {}
        self.sent = []
        self.send_status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.url.host == "oauth2.googleapis.com":
            if not self.refresh_ok:
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "fresh-token", "expires_in": 3600})
        if request.headers.get("Authorization", "").removeprefix("Bearer ") in self.expired_tokens:
            return httpx.Response(401, json={"error": {"code": 401}})
        if path.endswith("/profile"):
            return httpx.Response(200, json={"emailAddress": self.profile_email})
        if request.method == "POST" and path.endswith("/drafts"):
            number = len(self.drafts) + 1
            self.drafts[f"r-{number}"] = json.loads(request.content)
            return httpx.Response(200, json={"id": f"r-{number}", "message": {"id": f"18c{number}", "threadId": f"18c{number}"}})
        if request.method == "POST" and path.endswith("/messages/send"):
            if self.send_status != 200:
                return httpx.Response(self.send_status)
            self.sent.append(json.loads(request.content))
            return httpx.Response(200, json={"id": f"sent-{len(self.sent)}", "threadId": f"thread-{len(self.sent)}"})
        if request.method == "POST" and path.endswith("/drafts/send"):
            draft_id = json.loads(request.content)["id"]
            if draft_id not in self.drafts:
                return httpx.Response(404)
            self.sent.append(self.drafts.pop(draft_id)["message"])
            return httpx.Response(200, json={"id": f"sent-{len(self.sent)}", "threadId": f"thread-{len(self.sent)}"})
        if request.method == "GET" and "/drafts/" in path:
            draft_id = path.rsplit("/", 1)[1]
            return httpx.Response(200, json={"id": draft_id}) if draft_id in self.drafts else httpx.Response(404)
        return httpx.Response(500)


class GmailDraftTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        _, self.platform_path = build_and_migrate(root)
        self.attachment = root / "Resume.pdf"
        self.attachment.write_bytes(PDF)
        self.key = Fernet.generate_key().decode()
        self.env = mock.patch.dict("os.environ", {
            "GOOGLE_OAUTH_CLIENT_ID": "client-id",
            "GOOGLE_OAUTH_CLIENT_SECRET": "client-secret",
            "PIPELINE_CONNECTION_KEY": self.key,
            "PIPELINE_OUTREACH_ACCOUNT": ACCOUNT,
            "PIPELINE_OUTREACH_COMPOSE": "gmail",
            "PIPELINE_OUTREACH_ATTACHMENT": str(self.attachment),
        })
        self.env.start()
        self.gmail = FakeGmail()
        app = create_app(
            db_path=self.platform_path,
            access_token="gmail-owner",
            static_dir=STATIC_DIR,
            resume_storage=root / "resumes",
            capture_storage=root / "captures",
            interview_storage=root / "interviews",
            outreach_gmail_client_factory=lambda: httpx.Client(transport=httpx.MockTransport(self.gmail.handler)),
        )
        self.client = TestClient(app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.env.stop()
        self.tempdir.cleanup()

    def connect(self, access_token="valid-token", refresh_token="refresh-token"):
        fernet = Fernet(self.key.encode())
        with closing(connect_product(self.platform_path)) as conn:
            conn.execute(
                """INSERT INTO connector_accounts(id, user_id, provider, scopes_json, encrypted_access_token, encrypted_refresh_token, status, created_at, updated_at)
                   VALUES(?, ?, 'gmail_drafts', '[]', ?, ?, 'connected', ?, ?)""",
                (f"connector-gmail_drafts-{USER}", USER, fernet.encrypt(access_token.encode()).decode(),
                 fernet.encrypt(refresh_token.encode()).decode(), utc_now(), utc_now()),
            )
            conn.commit()

    def approved_target(self):
        created = self.client.post("/api/v1/outreach", headers=AUTH, json={
            "company": "Bovi", "contact_email": "greg@bovi.example",
            "email_subject": "Robotics internship question", "email_body": "Hi Greg,\n\nShort note about Bovi.\n\nSam",
        }).json()
        approved = self.client.post(f"/api/v1/outreach/{created['id']}/approve", headers=AUTH, json={
            "kind": "initial", "fingerprint": created["draft_fingerprint"], "acknowledge_warnings": True,
        })
        self.assertEqual(approved.status_code, 200, approved.text)
        return approved.json()

    def draft(self, target):
        return self.client.post(f"/api/v1/outreach/{target['id']}/gmail-draft", headers=AUTH, json={"kind": "initial"})

    def test_listing_reports_connection_and_attachment(self):
        listing = self.client.get("/api/v1/outreach", headers=AUTH).json()
        self.assertEqual(listing["gmail_drafts"], {
            "configured": True, "connected": False, "needs_reconnect": False,
            "account": ACCOUNT, "attachment": "Resume.pdf", "attachment_problem": "",
        })
        self.connect()
        self.assertTrue(self.client.get("/api/v1/outreach", headers=AUTH).json()["gmail_drafts"]["connected"])
        with mock.patch.dict("os.environ", {"PIPELINE_CONNECTION_KEY": ""}):
            status = self.client.get("/api/v1/outreach", headers=AUTH).json()["gmail_drafts"]
        self.assertEqual((status["configured"], status["connected"]), (False, False))

    def test_creates_draft_with_attachment_and_never_sends(self):
        self.connect()
        target = self.approved_target()
        response = self.draft(target)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertFalse(body["reused"])
        self.assertEqual(body["attachment"], "Resume.pdf")
        url = urlparse(body["url"])
        self.assertEqual(url.netloc, "mail.google.com")
        self.assertEqual(parse_qs(url.query)["authuser"], [ACCOUNT])
        self.assertEqual(url.fragment, "drafts?compose=18c1")

        raw = self.gmail.drafts["r-1"]["message"]["raw"]
        message = email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)
        self.assertEqual((message["To"], message["From"], message["Subject"]), ("greg@bovi.example", ACCOUNT, "Robotics internship question"))
        self.assertIn("Short note about Bovi.", message.get_body(("plain",)).get_content())
        attachments = list(message.iter_attachments())
        self.assertEqual([part.get_filename() for part in attachments], ["Resume.pdf"])
        self.assertEqual(attachments[0].get_content_type(), "application/pdf")
        self.assertEqual(attachments[0].get_content(), PDF)

        self.assertFalse([r for r in self.gmail.requests if "send" in r.url.path], "the app must never send mail")
        events = self.client.get(f"/api/v1/outreach/{target['id']}", headers=AUTH).json()["events"]
        self.assertIn("gmail_draft_created", [event["event_type"] for event in events])

    def test_signature_links_are_live_links_in_the_html_part(self):
        self.connect()
        created = self.client.post("/api/v1/outreach", headers=AUTH, json={
            "company": "Bovi", "contact_email": "greg@bovi.example", "email_subject": "Robotics internship question",
            "email_body": "Hi Greg,\n\nA <short> note about Bovi.\n\nSam Rivera\n"
                          + ACCOUNT + " | https://sam.example/portfolio | https://www.linkedin.com/in/sam-rivera-example/",
        }).json()
        approved = self.client.post(f"/api/v1/outreach/{created['id']}/approve", headers=AUTH, json={
            "kind": "initial", "fingerprint": created["draft_fingerprint"], "acknowledge_warnings": True,
        })
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(self.draft(approved.json()).status_code, 200)

        raw = self.gmail.drafts["r-1"]["message"]["raw"]
        message = email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)
        plain = message.get_body(("plain",)).get_content()
        markup = message.get_body(("html",)).get_content()
        # The approved words are unchanged in both parts; only the URLs gain anchors.
        self.assertIn("A <short> note about Bovi.", plain)
        self.assertIn("A &lt;short&gt; note about Bovi.", markup)
        for url in ("https://sam.example/portfolio", "https://www.linkedin.com/in/sam-rivera-example/"):
            self.assertIn(url, plain)
            self.assertIn(f'<a href="{url}">{url}</a>', markup)
        self.assertEqual(markup.count("<a href="), 2, "only the two signature links become links")
        self.assertIn("Sam Rivera<br>", markup, "the sign-off keeps its line breaks")
        self.assertEqual([part.get_filename() for part in message.iter_attachments()], ["Resume.pdf"])

    def test_second_click_reopens_the_same_draft_until_it_is_gone(self):
        self.connect()
        target = self.approved_target()
        first = self.draft(target).json()
        second = self.draft(target).json()
        self.assertTrue(second["reused"])
        self.assertEqual(second["draft_id"], first["draft_id"])
        self.assertEqual(len(self.gmail.drafts), 1)
        self.gmail.drafts.clear()  # sent or deleted in Gmail
        third = self.draft(target).json()
        self.assertFalse(third["reused"])
        self.assertEqual(len(self.gmail.drafts), 1)

    def test_changed_attachment_content_creates_a_fresh_draft(self):
        self.connect()
        target = self.approved_target()
        first = self.draft(target).json()
        self.attachment.write_bytes(b"%PDF-1.4 updated resume")

        second_response = self.draft(target)
        self.assertEqual(second_response.status_code, 200, second_response.text)
        second = second_response.json()

        self.assertFalse(second["reused"])
        self.assertNotEqual(second["draft_id"], first["draft_id"])
        self.assertNotIn("attachment_sha256", second)
        self.assertEqual(len(self.gmail.drafts), 2)

    def test_unapproved_draft_is_refused(self):
        self.connect()
        created = self.client.post("/api/v1/outreach", headers=AUTH, json={
            "company": "Unapproved", "contact_email": "a@b.example", "email_subject": "Hi", "email_body": "Body",
        }).json()
        response = self.draft(created)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.gmail.requests, [])

    def test_not_connected_is_a_conflict(self):
        response = self.draft(self.approved_target())
        self.assertEqual(response.status_code, 409)
        self.assertIn("Connect Gmail", response.json()["detail"])

    def test_expired_access_token_is_renewed_once(self):
        self.connect(access_token="stale-token")
        self.gmail.expired_tokens.add("stale-token")
        response = self.draft(self.approved_target())
        self.assertEqual(response.status_code, 200, response.text)
        with closing(connect_product(self.platform_path)) as conn:
            stored = conn.execute("SELECT encrypted_access_token FROM connector_accounts WHERE provider='gmail_drafts'").fetchone()[0]
        self.assertEqual(Fernet(self.key.encode()).decrypt(stored.encode()).decode(), "fresh-token")

    def test_revoked_connection_asks_to_reconnect(self):
        self.connect(access_token="stale-token")
        self.gmail.expired_tokens.add("stale-token")
        self.gmail.refresh_ok = False
        response = self.draft(self.approved_target())
        self.assertEqual(response.status_code, 409)
        self.assertIn("reconnect Gmail", response.json()["detail"])
        status = self.client.get("/api/v1/outreach", headers=AUTH).json()["gmail_drafts"]
        self.assertEqual((status["connected"], status["needs_reconnect"]), (False, True))

    def test_wrong_google_account_is_refused(self):
        self.connect()
        self.gmail.profile_email = "someone-else@gmail.example"
        response = self.draft(self.approved_target())
        self.assertEqual(response.status_code, 409)
        self.assertIn(ACCOUNT, response.json()["detail"])
        self.assertEqual(self.gmail.drafts, {})

    def test_missing_attachment_is_refused_rather_than_dropped(self):
        self.connect()
        target = self.approved_target()
        self.attachment.unlink()
        response = self.draft(target)
        self.assertEqual(response.status_code, 422)
        self.assertIn("Resume.pdf", response.json()["detail"])
        self.assertEqual(self.gmail.drafts, {})

    def send(self, target, kind="initial", fingerprint=None):
        print_ = fingerprint or target["draft_fingerprint" if kind == "initial" else "follow_up_fingerprint"]
        return self.client.post(f"/api/v1/outreach/{target['id']}/gmail-send", headers=AUTH, json={"kind": kind, "fingerprint": print_})

    def sent_message(self, index=0):
        return email.message_from_bytes(base64.urlsafe_b64decode(self.gmail.sent[index]["raw"]), policy=policy.default)

    def test_send_delivers_the_approved_email_and_marks_it_sent(self):
        self.connect()
        target = self.approved_target()
        response = self.send(target)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["to"], body["account"], body["status"]), ("greg@bovi.example", ACCOUNT, "sent"))
        self.assertTrue(body["follow_up_at"])

        message = self.sent_message()
        self.assertEqual((message["To"], message["From"], message["Subject"]), ("greg@bovi.example", ACCOUNT, "Robotics internship question"))
        self.assertIn("Short note about Bovi.", message.get_body(("plain",)).get_content())
        self.assertEqual([part.get_filename() for part in message.iter_attachments()], ["Resume.pdf"])

        detail = self.client.get(f"/api/v1/outreach/{target['id']}", headers=AUTH).json()
        self.assertEqual(detail["status"], "sent")
        self.assertTrue(detail["sent_at"])
        self.assertIn("gmail_sent", [event["event_type"] for event in detail["events"]])

    def test_send_goes_out_once(self):
        self.connect()
        target = self.approved_target()
        self.assertEqual(self.send(target).status_code, 200)
        again = self.send(target)
        self.assertEqual(again.status_code, 422)
        self.assertEqual(len(self.gmail.sent), 1)

    def test_send_refuses_words_changed_after_confirming(self):
        self.connect()
        target = self.approved_target()
        response = self.send(target, fingerprint="0" * 64)
        self.assertEqual(response.status_code, 409)
        self.assertIn("changed", response.json()["detail"])
        self.assertEqual(self.gmail.sent, [])

    def test_send_refuses_an_unapproved_draft(self):
        self.connect()
        created = self.client.post("/api/v1/outreach", headers=AUTH, json={
            "company": "Unapproved", "contact_email": "a@b.example", "email_subject": "Hi", "email_body": "Body",
        }).json()
        response = self.send(created)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.gmail.requests, [])

    def test_send_refuses_a_target_already_marked_sent_by_hand(self):
        self.connect()
        target = self.approved_target()
        self.client.patch(f"/api/v1/outreach/{target['id']}", headers=AUTH, json={"status": "sent"})
        response = self.send(target)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.gmail.requests, [])

    def test_send_uses_the_existing_gmail_draft_so_no_stale_copy_is_left(self):
        self.connect()
        target = self.approved_target()
        self.assertEqual(self.draft(target).status_code, 200)
        self.assertEqual(self.send(target).status_code, 200)
        self.assertEqual(self.gmail.drafts, {}, "the draft was sent, not duplicated")
        self.assertEqual(len(self.gmail.sent), 1)
        self.assertFalse([r for r in self.gmail.requests if r.url.path.endswith("/messages/send")])

    def test_failed_send_leaves_the_target_unsent(self):
        self.connect()
        target = self.approved_target()
        self.gmail.send_status = 500
        response = self.send(target)
        self.assertEqual(response.status_code, 502)
        self.assertIn("Nothing was sent", response.json()["detail"])
        detail = self.client.get(f"/api/v1/outreach/{target['id']}", headers=AUTH).json()
        self.assertNotEqual(detail["status"], "sent")
        self.assertNotIn("gmail_sent", [event["event_type"] for event in detail["events"]])

    def test_send_refuses_the_wrong_google_account(self):
        self.connect()
        self.gmail.profile_email = "someone-else@gmail.example"
        response = self.send(self.approved_target())
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.gmail.sent, [])

    def test_follow_up_sends_only_after_the_first_email(self):
        self.connect()
        target = self.approved_target()
        written = self.client.patch(f"/api/v1/outreach/{target['id']}", headers=AUTH, json={
            "follow_up_subject": "Re: Robotics internship question", "follow_up_body": "Hi Greg,\n\nFollowing up.\n\nSam",
        }).json()
        approved = self.client.post(f"/api/v1/outreach/{target['id']}/approve", headers=AUTH, json={
            "kind": "follow_up", "fingerprint": written["follow_up_fingerprint"], "acknowledge_warnings": True,
        })
        self.assertEqual(approved.status_code, 200, approved.text)
        early = self.send(approved.json(), kind="follow_up")
        self.assertEqual(early.status_code, 422)
        self.assertEqual(self.gmail.sent, [])

        self.assertEqual(self.send(target).status_code, 200)
        response = self.send(approved.json(), kind="follow_up")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "followed_up")
        self.assertEqual(self.sent_message(1)["Subject"], "Re: Robotics internship question")

    def test_oauth_start_requests_only_compose_for_the_sending_account(self):
        response = self.client.get("/api/v1/connections/oauth/gmail_drafts/start", headers=AUTH)
        self.assertEqual(response.status_code, 200, response.text)
        query = parse_qs(urlparse(response.json()["authorization_url"]).query)
        self.assertEqual(query["scope"], ["https://www.googleapis.com/auth/gmail.compose"])
        self.assertEqual(query["login_hint"], [ACCOUNT])
        self.assertTrue(query["redirect_uri"][0].endswith("/connections/oauth/gmail_drafts/callback"))
        self.assertEqual(self.client.get("/connections/oauth/gmail_drafts/callback").status_code, 200)


if __name__ == "__main__":
    unittest.main()
