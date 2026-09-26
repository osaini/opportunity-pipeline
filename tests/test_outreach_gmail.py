"""Approved outreach drafts written to Gmail Drafts, or sent once confirmed, against a mocked Gmail."""

import base64
import email
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email import policy
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR, outreach_delivery, outreach_gmail
from opportunity_app.api import create_app
from opportunity_app.schema import connect_product, utc_now

from helpers_platform import build_and_migrate

AUTH = {"Authorization": "Bearer gmail-owner"}
USER = "local-user"
ACCOUNT = "student@school.example"
PDF = b"%PDF-1.4 fake resume"
SCOPES = ["https://www.googleapis.com/auth/gmail.compose", "https://www.googleapis.com/auth/gmail.readonly"]


def plain_notice(text, *, subject="Mail delivery failed: returning message to sender", headers=""):
    """A notice with no delivery report, as Exim and some groups send them."""
    return (
        f"From: Mail Delivery System <Mailer-Daemon@mx.example>\nTo: {ACCOUNT}\nSubject: {subject}\n{headers}"
        "MIME-Version: 1.0\nContent-Type: text/plain; charset=UTF-8\n\n"
        f"{text}\n"
    ).encode()


def delivery_report(failed=(), delayed=(), text="Address not found. Your message wasn't delivered."):
    """A standard delivery status notice (RFC 3464), as the raw text Gmail stores."""
    blocks = "".join(
        f"\nFinal-Recipient: rfc822; {address}\nAction: {action}\nStatus: {status}\n"
        f"Diagnostic-Code: smtp; {status.replace('.', '')[:3]} {status} {address} does not exist\n"
        for address, action, status in [*((a, "failed", "5.1.1") for a in failed), *((a, "delayed", "4.4.1") for a in delayed)]
    )
    return (
        "From: Mail Delivery Subsystem <mailer-daemon@googlemail.com>\n"
        f"To: {ACCOUNT}\n"
        "Subject: Delivery Status Notification (Failure)\n"
        "MIME-Version: 1.0\n"
        'Content-Type: multipart/report; report-type=delivery-status; boundary="b"\n\n'
        "--b\n"
        'Content-Type: text/plain; charset="UTF-8"\n\n'
        f"Hello {ACCOUNT},\n\n{text}\n\n"
        "--b\n"
        "Content-Type: message/delivery-status\n\n"
        "Reporting-MTA: dns; googlemail.com\n"
        f"{blocks}\n"
        "--b--\n"
    ).encode()


def failure_notice(notice_id="dsn-1", *, subject="Delivery Status Notification (Failure)",
                   sender="Mail Delivery Subsystem <mailer-daemon@googlemail.com>", failed="", snippet="Address not found"):
    """A delivery status notice as Gmail's threads.get returns it in the metadata format."""
    headers = [{"name": "From", "value": sender}, {"name": "Subject", "value": subject},
               {"name": "Content-Type", "value": 'multipart/report; report-type=delivery-status; boundary="b"'}]
    if failed:
        headers.append({"name": "X-Failed-Recipients", "value": failed})
    return {"id": notice_id, "labelIds": ["INBOX"], "internalDate": "2000", "snippet": snippet,
            "payload": {"mimeType": "multipart/report", "headers": headers}}


class FakeGmail:
    """A scripted Gmail API and token endpoint that records every request."""

    def __init__(self):
        self.requests = []
        self.profile_email = ACCOUNT
        self.expired_tokens = set()
        self.refresh_ok = True
        self.drafts = {}
        self.draft_count = 0
        self.sent = []
        self.send_status = 200
        # Raised after Gmail accepted the message, as a timeout on the answer would be.
        self.send_error = None
        # Raised before the message reaches Gmail.
        self.send_unreached = None
        self.draft_status = 200
        self.draft_error = None
        self.draft_get_status = None
        # Run once, the next time Gmail sees that call: "profile", "send", "create_draft", "get_draft".
        self.hooks = {}
        # Messages Gmail threaded with a sent email after it, by thread id.
        self.replies = {}
        self.thread_status = None
        # Full text of messages by id, and the ids a search for notices finds.
        self.raw = {}
        self.inbox_notices = []

    def run_hook(self, name):
        hook = self.hooks.pop(name, None)
        if hook:
            hook()

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
            self.run_hook("profile")
            return httpx.Response(200, json={"emailAddress": self.profile_email})
        if request.method == "POST" and path.endswith("/drafts"):
            self.run_hook("create_draft")
            if self.draft_status != 200:
                return httpx.Response(self.draft_status)
            self.draft_count += 1
            number = self.draft_count
            self.drafts[f"r-{number}"] = json.loads(request.content)
            if self.draft_error:
                raise self.draft_error
            return httpx.Response(200, json={"id": f"r-{number}", "message": {"id": f"18c{number}", "threadId": f"18c{number}"}})
        if request.method == "POST" and path.endswith("/messages/send"):
            self.run_hook("send")
            if self.send_unreached:
                raise self.send_unreached
            if self.send_status != 200:
                return httpx.Response(self.send_status)
            self.sent.append(json.loads(request.content))
            if self.send_error:
                raise self.send_error
            return httpx.Response(200, json={"id": f"sent-{len(self.sent)}", "threadId": f"thread-{len(self.sent)}"})
        if request.method == "POST" and path.endswith("/drafts/send"):
            draft_id = json.loads(request.content)["id"]
            if draft_id not in self.drafts:
                return httpx.Response(404)
            self.sent.append(self.drafts.pop(draft_id)["message"])
            return httpx.Response(200, json={"id": f"sent-{len(self.sent)}", "threadId": f"thread-{len(self.sent)}"})
        if request.method == "GET" and "/threads/" in path:
            if self.thread_status:
                return httpx.Response(self.thread_status, json={"error": {"message": "Request had insufficient authentication scopes."}})
            thread_id = path.rsplit("/", 1)[1]
            sent = {"id": thread_id.replace("thread-", "sent-"), "labelIds": ["SENT"], "internalDate": "1000"}
            return httpx.Response(200, json={"id": thread_id, "messages": [sent, *self.replies.get(thread_id, [])]})
        if request.method == "GET" and path.endswith("/messages"):
            if self.thread_status:
                return httpx.Response(self.thread_status)
            return httpx.Response(200, json={"messages": [{"id": notice_id} for notice_id in self.inbox_notices]})
        if request.method == "GET" and "/messages/" in path:
            message_id = path.rsplit("/", 1)[1]
            if message_id not in self.raw:
                return httpx.Response(404)
            raw, received = self.raw[message_id]
            return httpx.Response(200, json={"id": message_id, "internalDate": str(received), "raw": base64.urlsafe_b64encode(raw).decode()})
        if request.method == "GET" and "/drafts/" in path:
            self.run_hook("get_draft")
            if self.draft_get_status:
                return httpx.Response(self.draft_get_status)
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
        outreach_delivery._LAST_LOOK.clear()
        outreach_delivery._READ_NOTICES.clear()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.env.stop()
        self.tempdir.cleanup()

    def connect(self, access_token="valid-token", refresh_token="refresh-token", scopes=()):
        fernet = Fernet(self.key.encode())
        with closing(connect_product(self.platform_path)) as conn:
            conn.execute(
                """INSERT INTO connector_accounts(id, user_id, provider, scopes_json, encrypted_access_token, encrypted_refresh_token, status, created_at, updated_at)
                   VALUES(?, ?, 'gmail_drafts', ?, ?, ?, 'connected', ?, ?)""",
                (f"connector-gmail_drafts-{USER}", USER, json.dumps(list(scopes)), fernet.encrypt(access_token.encode()).decode(),
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
            "configured": True, "connected": False, "needs_reconnect": False, "bounce_check": False,
            "account": ACCOUNT, "attachment": "Resume.pdf", "attachment_problem": "",
        })
        self.connect(scopes=SCOPES[:1])
        status = self.client.get("/api/v1/outreach", headers=AUTH).json()["gmail_drafts"]
        self.assertEqual((status["connected"], status["bounce_check"]), (True, False), "connected before bounce checks existed")
        with closing(connect_product(self.platform_path)) as conn:
            conn.execute("UPDATE connector_accounts SET scopes_json=?", (json.dumps(SCOPES),))
            conn.commit()
        self.assertTrue(self.client.get("/api/v1/outreach", headers=AUTH).json()["gmail_drafts"]["bounce_check"])
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

    def test_send_refuses_while_a_gmail_draft_of_it_is_in_drafts(self):
        """A draft can be edited in Gmail, so the app never sends one, and never sends a second copy beside it."""
        self.connect()
        target = self.approved_target()
        self.assertEqual(self.draft(target).status_code, 200)
        response = self.send(target)
        self.assertEqual(response.status_code, 409)
        self.assertIn("in your Gmail Drafts", response.json()["detail"])
        self.assertEqual(self.gmail.sent, [])
        self.assertEqual(len(self.gmail.drafts), 1, "the student's draft is left alone")
        self.assertFalse([r for r in self.gmail.requests if r.url.path.endswith("/send")])

    def test_failed_send_leaves_the_target_unsent(self):
        self.connect()
        target = self.approved_target()
        self.gmail.send_status = 400
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

    # --- Each email goes out at most once ------------------------------------

    def claim(self, target, kind="initial"):
        with closing(connect_product(self.platform_path)) as conn:
            row = conn.execute(
                "SELECT state, action FROM outreach_send_claims WHERE target_id=? AND kind=?", (target["id"], kind)
            ).fetchone()
        return tuple(row) if row else None

    def put_claim(self, target, state, *, instance, age_minutes, action="send", kind="initial"):
        claimed_at = (datetime.now(timezone.utc) - timedelta(minutes=age_minutes)).isoformat(timespec="microseconds")
        with closing(connect_product(self.platform_path)) as conn:
            conn.execute(
                """INSERT INTO outreach_send_claims(target_id, user_id, kind, token, state, action, instance, claimed_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
                (target["id"], USER, kind, "old-token", state, action, instance, claimed_at),
            )
            conn.commit()

    def send_checked(self, target, check, kind="initial"):
        print_ = target["draft_fingerprint" if kind == "initial" else "follow_up_fingerprint"]
        return self.client.post(f"/api/v1/outreach/{target['id']}/gmail-send", headers=AUTH, json={
            "kind": kind, "fingerprint": print_, "sent_folder_check": check,
        })

    def test_a_send_arriving_while_one_is_in_flight_is_refused(self):
        self.connect()
        target = self.approved_target()
        nested = []
        self.gmail.hooks["send"] = lambda: nested.append(self.send(target))
        response = self.send(target)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(nested[0].status_code, 409)
        self.assertEqual(len(self.gmail.sent), 1, "two clicks, two tabs, or a retry still send one email")
        self.assertEqual(self.claim(target), ("sent", "send"))

    def test_a_send_that_gets_no_answer_is_not_sent_again_without_a_look(self):
        self.connect()
        target = self.approved_target()
        self.gmail.send_error = httpx.ReadTimeout("timed out")
        response = self.send(target)
        self.assertEqual(response.status_code, 502)
        self.assertIn("may have gone out", response.json()["detail"])
        self.assertEqual(self.claim(target), ("unconfirmed", "send"))
        self.assertEqual(len(self.gmail.sent), 1, "Gmail did send it; only the answer was lost")

        self.gmail.send_error = None
        again = self.send(target)
        self.assertEqual(again.status_code, 428)
        detail = again.json()["detail"]
        self.assertIn("Sent folder", detail["msg"])
        self.assertEqual(len(self.gmail.sent), 1, "a plain retry sends nothing")
        self.assertEqual(self.send_checked(target, "0" * 32).status_code, 428, "only the named check is accepted")

        checked = self.send_checked(target, detail["check"])
        self.assertEqual(checked.status_code, 200, checked.text)
        self.assertEqual(len(self.gmail.sent), 2)
        self.assertEqual(checked.json()["status"], "sent")
        self.assertEqual(self.claim(target), ("sent", "send"))

    def test_a_check_vouches_for_one_attempt_only(self):
        self.connect()
        target = self.approved_target()
        self.gmail.send_error = httpx.ReadTimeout("timed out")
        self.send(target)
        check = self.send(target).json()["detail"]["check"]
        # The override itself goes unanswered: the old check no longer covers it.
        self.assertEqual(self.send_checked(target, check).status_code, 502)
        self.gmail.send_error = None
        again = self.send_checked(target, check)
        self.assertEqual(again.status_code, 428)
        self.assertNotEqual(again.json()["detail"]["check"], check)
        self.assertEqual(len(self.gmail.sent), 2)

    def test_a_gmail_server_error_on_send_is_treated_as_maybe_sent(self):
        self.connect()
        target = self.approved_target()
        self.gmail.send_status = 503
        response = self.send(target)
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("Nothing was sent", response.json()["detail"])
        self.assertEqual(self.claim(target), ("unconfirmed", "send"))
        self.gmail.send_status = 200
        self.assertEqual(self.send(target).status_code, 428)
        self.assertEqual(self.gmail.sent, [])

    def test_a_refused_or_unreached_send_can_be_retried_at_once(self):
        self.connect()
        target = self.approved_target()
        self.gmail.send_status = 400
        self.assertEqual(self.send(target).status_code, 502)
        self.assertIsNone(self.claim(target))
        self.gmail.send_status = 200
        self.gmail.send_unreached = httpx.ConnectError("no route")
        self.assertEqual(self.send(target).status_code, 502)
        self.assertIsNone(self.claim(target))
        self.gmail.send_unreached = None
        self.assertEqual(self.send(target).status_code, 200)
        self.assertEqual(len(self.gmail.sent), 1)

    def test_a_claim_left_by_a_dead_server_process_asks_for_a_look(self):
        self.connect()
        target = self.approved_target()
        self.put_claim(target, "sending", instance="another-process", age_minutes=10)
        response = self.send(target)
        self.assertEqual(response.status_code, 428)
        self.assertEqual(self.gmail.sent, [])
        checked = self.send_checked(target, response.json()["detail"]["check"])
        self.assertEqual(checked.status_code, 200, checked.text)
        self.assertEqual(len(self.gmail.sent), 1)

    def test_a_claim_this_process_could_not_settle_asks_for_a_look_rather_than_blocking(self):
        self.connect()
        target = self.approved_target()
        # Its request has ended (a failed write left the row as it was), so nothing holds it.
        self.put_claim(target, "sending", instance=outreach_gmail.SERVER_INSTANCE, age_minutes=0)
        response = self.send(target)
        self.assertEqual(response.status_code, 428)
        self.assertEqual(self.gmail.sent, [])

    def test_a_recent_claim_from_another_process_is_still_in_progress(self):
        self.connect()
        target = self.approved_target()
        self.put_claim(target, "sending", instance="another-process", age_minutes=1)
        response = self.send(target)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.gmail.requests, [])

    def test_a_draft_sent_from_gmail_is_not_sent_again_from_the_app(self):
        self.connect()
        target = self.approved_target()
        self.assertEqual(self.draft(target).status_code, 200)
        self.gmail.drafts.clear()  # the student pressed Send in Gmail
        response = self.send(target)
        self.assertEqual(response.status_code, 428)
        self.assertIn("no longer in your Drafts", response.json()["detail"]["msg"])
        self.assertEqual(self.gmail.sent, [])
        checked = self.send_checked(target, response.json()["detail"]["check"])
        self.assertEqual(checked.status_code, 200, checked.text)
        self.assertEqual(len(self.gmail.sent), 1)

    def test_a_vanished_draft_of_an_earlier_version_also_asks_for_a_look(self):
        self.connect()
        target = self.approved_target()
        self.assertEqual(self.draft(target).status_code, 200)
        self.gmail.drafts.clear()
        edited = self.client.patch(f"/api/v1/outreach/{target['id']}", headers=AUTH, json={
            "email_body": "Hi Greg,\n\nA second version.\n\nSam",
        }).json()
        approved = self.client.post(f"/api/v1/outreach/{target['id']}/approve", headers=AUTH, json={
            "kind": "initial", "fingerprint": edited["draft_fingerprint"], "acknowledge_warnings": True,
        })
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(self.send(approved.json()).status_code, 428)
        self.assertEqual(self.gmail.sent, [])

    def test_a_live_draft_of_an_earlier_version_refuses_the_send(self):
        self.connect()
        target = self.approved_target()
        self.assertEqual(self.draft(target).status_code, 200)
        edited = self.client.patch(f"/api/v1/outreach/{target['id']}", headers=AUTH, json={
            "email_body": "Hi Greg,\n\nA second version.\n\nSam",
        }).json()
        approved = self.client.post(f"/api/v1/outreach/{target['id']}/approve", headers=AUTH, json={
            "kind": "initial", "fingerprint": edited["draft_fingerprint"], "acknowledge_warnings": True,
        }).json()
        self.assertEqual(self.send(approved).status_code, 409)
        self.assertEqual(self.gmail.sent, [])
        self.assertEqual(len(self.gmail.drafts), 1, "nothing is deleted on the student's behalf")

    def test_an_unreadable_draft_is_not_taken_as_gone(self):
        self.connect()
        target = self.approved_target()
        self.assertEqual(self.draft(target).status_code, 200)
        self.gmail.draft_get_status = 500
        response = self.send(target)
        self.assertEqual(response.status_code, 502)
        self.assertIn("Nothing was sent", response.json()["detail"])
        self.assertEqual(self.gmail.sent, [])
        self.assertIsNone(self.claim(target))

    def test_a_draft_made_between_the_check_and_the_send_stops_the_send(self):
        self.connect()
        target = self.approved_target()
        self.assertEqual(self.draft(target).status_code, 200)
        self.gmail.drafts.clear()
        check = self.send(target).json()["detail"]["check"]
        self.gmail.hooks["get_draft"] = lambda: self.assertEqual(self.draft(target).status_code, 200)
        response = self.send_checked(target, check)
        self.assertEqual(response.status_code, 409)
        self.assertIn("just made", response.json()["detail"])
        self.assertEqual(self.gmail.sent, [])
        self.assertIsNone(self.claim(target))

    def test_state_changed_after_the_gmail_checks_is_rechecked_before_sending(self):
        self.connect()
        for change, status in (
            ({"status": "sent"}, 422),
            ({"email_body": "Hi Greg,\n\nWords changed in another tab.\n\nSam"}, 422),
        ):
            with self.subTest(change=change):
                target = self.approved_target()
                self.gmail.hooks["profile"] = lambda: self.client.patch(
                    f"/api/v1/outreach/{target['id']}", headers=AUTH, json=change
                )
                response = self.send(target)
                self.assertEqual(response.status_code, status, response.text)
                self.assertEqual(self.gmail.sent, [])
                self.assertIsNone(self.claim(target))
                self.client.delete(f"/api/v1/outreach/{target['id']}", headers=AUTH)

    def test_a_draft_cannot_be_made_while_the_email_is_being_sent(self):
        self.connect()
        target = self.approved_target()
        nested = []
        self.gmail.hooks["send"] = lambda: nested.append(self.draft(target))
        self.assertEqual(self.send(target).status_code, 200)
        self.assertEqual(nested[0].status_code, 409)
        self.assertEqual(self.gmail.drafts, {})

    def test_a_send_cannot_start_while_a_draft_is_being_made(self):
        self.connect()
        target = self.approved_target()
        nested = []
        self.gmail.hooks["create_draft"] = lambda: nested.append(self.send(target))
        self.assertEqual(self.draft(target).status_code, 200)
        self.assertEqual(nested[0].status_code, 409)
        self.assertEqual(self.gmail.sent, [])

    def test_two_draft_requests_make_one_draft(self):
        self.connect()
        target = self.approved_target()
        nested = []
        self.gmail.hooks["create_draft"] = lambda: nested.append(self.draft(target))
        self.assertEqual(self.draft(target).status_code, 200)
        self.assertEqual(nested[0].status_code, 409)
        self.assertEqual(len(self.gmail.drafts), 1)

    def test_a_draft_that_gets_no_answer_asks_for_a_look_in_drafts_before_sending(self):
        self.connect()
        target = self.approved_target()
        self.gmail.draft_error = httpx.ReadTimeout("timed out")
        self.assertEqual(self.draft(target).status_code, 502)
        self.assertEqual(self.claim(target), ("unconfirmed", "draft"))
        self.gmail.draft_error = None
        response = self.send(target)
        self.assertEqual(response.status_code, 428)
        self.assertIn("Drafts and Sent", response.json()["detail"]["msg"])
        self.assertEqual(self.gmail.sent, [])
        self.assertEqual(self.draft(target).status_code, 409, "no second draft beside one that may exist")

    def test_no_draft_is_made_once_the_email_went_out(self):
        self.connect()
        target = self.approved_target()
        self.assertEqual(self.send(target).status_code, 200)
        response = self.draft(target)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.gmail.drafts, {})

    def test_no_draft_is_made_once_the_email_is_marked_sent_by_hand(self):
        self.connect()
        target = self.approved_target()
        self.client.patch(f"/api/v1/outreach/{target['id']}", headers=AUTH, json={"status": "sent"})
        self.assertEqual(self.draft(target).status_code, 422)
        written = self.client.patch(f"/api/v1/outreach/{target['id']}", headers=AUTH, json={
            "follow_up_subject": "Re: Robotics internship question", "follow_up_body": "Hi Greg,\n\nFollowing up.\n\nSam",
        }).json()
        self.client.post(f"/api/v1/outreach/{target['id']}/approve", headers=AUTH, json={
            "kind": "follow_up", "fingerprint": written["follow_up_fingerprint"], "acknowledge_warnings": True,
        })
        self.client.patch(f"/api/v1/outreach/{target['id']}", headers=AUTH, json={"status": "followed_up"})
        follow_up = self.client.post(f"/api/v1/outreach/{target['id']}/gmail-draft", headers=AUTH, json={"kind": "follow_up"})
        self.assertEqual(follow_up.status_code, 422)
        self.assertEqual(self.gmail.drafts, {})

    def test_a_send_is_recorded_even_when_the_status_cannot_be_updated(self):
        self.connect()
        target = self.approved_target()
        with mock.patch("opportunity_app.outreach_gmail.update_target", side_effect=sqlite3.OperationalError("database is locked")):
            response = self.send(target)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["marked"])
        detail = self.client.get(f"/api/v1/outreach/{target['id']}", headers=AUTH).json()
        self.assertIn("gmail_sent", [event["event_type"] for event in detail["events"]])
        self.assertEqual(self.claim(target), ("sent", "send"))
        again = self.send(target)
        self.assertEqual(again.status_code, 422)
        self.assertEqual(len(self.gmail.sent), 1)

    def test_the_app_never_sends_a_gmail_draft(self):
        self.connect()
        target = self.approved_target()
        self.assertEqual(self.send(target).status_code, 200)
        self.assertFalse([r for r in self.gmail.requests if r.url.path.endswith("/drafts/send")])

    def test_oauth_start_requests_compose_and_metadata_for_the_sending_account(self):
        response = self.client.get("/api/v1/connections/oauth/gmail_drafts/start", headers=AUTH)
        self.assertEqual(response.status_code, 200, response.text)
        query = parse_qs(urlparse(response.json()["authorization_url"]).query)
        self.assertEqual(query["scope"], [" ".join(SCOPES)])
        self.assertEqual(query["login_hint"], [ACCOUNT])
        self.assertTrue(query["redirect_uri"][0].endswith("/connections/oauth/gmail_drafts/callback"))
        self.assertEqual(self.client.get("/connections/oauth/gmail_drafts/callback").status_code, 200)


    # --- Bounces --------------------------------------------------------------

    def check(self):
        response = self.client.post("/api/v1/outreach/delivery-check", headers=AUTH)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def target(self, target):
        return self.client.get(f"/api/v1/outreach/{target['id']}", headers=AUTH).json()

    def sent_and_bounced(self):
        self.connect(scopes=SCOPES)
        target = self.approved_target()
        self.assertEqual(self.send(target).status_code, 200)
        self.gmail.replies["thread-1"] = [failure_notice()]
        result = self.check()
        self.assertEqual(result["state"], "ok")
        self.assertEqual([item["target_id"] for item in result["bounced"]], [target["id"]])
        return self.target(target)

    def test_a_bounce_in_the_sent_thread_puts_the_target_back_to_drafted(self):
        bounced = self.sent_and_bounced()
        self.assertEqual((bounced["status"], bounced["sent_at"], bounced["follow_up_at"]), ("drafted", None, None))
        self.assertTrue(bounced["bounced_at"])
        self.assertEqual(bounced["bounce_reason"], "Address not found")
        self.assertEqual(bounced["bounced_addresses"], ["greg@bovi.example"])
        self.assertTrue(bounced["contact_bounced"])
        self.assertIn("bounced", [event["event_type"] for event in bounced["events"]])
        again = self.send(bounced)
        self.assertEqual(again.status_code, 422, "nothing more goes to an address that bounced")
        self.assertIn("bounced", again.json()["detail"])
        self.assertEqual(len(self.gmail.sent), 1)
        self.assertEqual(self.draft(bounced).status_code, 422, "nor is a Gmail draft made to it")

    def test_after_a_bounce_the_email_goes_once_to_a_new_contact(self):
        bounced = self.sent_and_bounced()
        moved = self.client.patch(f"/api/v1/outreach/{bounced['id']}", headers=AUTH, json={
            "contact_name": "Dana Ruiz", "contact_email": "dana@bovi.example",
        }).json()
        self.assertEqual(moved["email_body"].splitlines()[0], "Hi Dana,", "the greeting follows the new contact")
        self.assertEqual(moved["draft_status"], "generated")
        approved = self.client.post(f"/api/v1/outreach/{moved['id']}/approve", headers=AUTH, json={
            "kind": "initial", "fingerprint": moved["draft_fingerprint"], "acknowledge_warnings": True,
        }).json()
        resent = self.send(approved)
        self.assertEqual(resent.status_code, 200, resent.text)
        self.assertEqual(self.sent_message(1)["To"], "dana@bovi.example")
        self.assertTrue(self.sent_message(1).get_body(("plain",)).get_content().startswith("Hi Dana,"))
        after = self.target(approved)
        self.assertEqual((after["status"], after["bounced_at"], after["bounce_reason"]), ("sent", None, ""))
        self.assertEqual(after["bounced_addresses"], ["greg@bovi.example"], "the failed address stays on record")
        self.assertEqual(self.claim(approved), ("sent", "send"))
        self.assertEqual(self.send(approved).status_code, 422, "and the new email still goes out only once")
        self.assertEqual(len(self.gmail.sent), 2)

    def test_a_send_that_bounced_is_no_longer_watched(self):
        bounced = self.sent_and_bounced()
        outreach_delivery._LAST_LOOK.clear()
        self.assertEqual(self.check()["checked"], 0)
        events = [event["event_type"] for event in self.target(bounced)["events"]]
        self.assertEqual(events.count("bounced"), 1)

    def test_the_check_waits_between_looks_at_one_send(self):
        self.connect(scopes=SCOPES)
        target = self.approved_target()
        self.send(target)
        self.assertEqual(self.check()["checked"], 1)
        self.assertEqual(self.check()["checked"], 0, "a fresh send is looked at again after a short wait, not on every load")
        self.assertEqual(self.target(target)["status"], "sent")

    def test_a_cc_bounce_alone_leaves_the_email_sent(self):
        self.connect(scopes=SCOPES)
        target = self.approved_target()
        target = self.client.patch(f"/api/v1/outreach/{target['id']}", headers=AUTH, json={"contact_cc": "info@bovi.example"}).json()
        target = self.client.post(f"/api/v1/outreach/{target['id']}/approve", headers=AUTH, json={
            "kind": "initial", "fingerprint": target["draft_fingerprint"], "acknowledge_warnings": True,
        }).json()
        self.assertEqual(self.send(target).status_code, 200)
        self.gmail.replies["thread-1"] = [failure_notice(failed="info@bovi.example")]
        self.check()
        after = self.target(target)
        self.assertEqual(after["status"], "sent", "the email still reached the contact")
        self.assertTrue(after["cc_bounced"])
        self.assertFalse(after["contact_bounced"])
        self.assertIn("partly_bounced", [event["event_type"] for event in after["events"]])

    def test_a_delay_notice_or_a_real_reply_is_not_a_bounce(self):
        self.connect(scopes=SCOPES)
        target = self.approved_target()
        self.send(target)
        self.gmail.replies["thread-1"] = [
            failure_notice("dsn-delay", subject="Delivery Status Notification (Delay)"),
            {"id": "reply-1", "labelIds": ["INBOX"], "internalDate": "3000", "snippet": "Happy to chat",
             "payload": {"mimeType": "text/plain", "headers": [{"name": "From", "value": "Greg <greg@bovi.example>"}]}},
        ]
        result = self.check()
        self.assertEqual((result["checked"], result["bounced"]), (1, []))
        self.assertEqual(self.target(target)["status"], "sent")

    def test_without_the_metadata_scope_the_check_asks_for_a_reconnect(self):
        self.connect(scopes=SCOPES[:1])
        target = self.approved_target()
        self.send(target)
        self.gmail.thread_status = 403
        self.gmail.replies["thread-1"] = [failure_notice()]
        self.assertEqual(self.check()["state"], "needs_reconnect")
        self.assertEqual(self.target(target)["status"], "sent")
        self.gmail.thread_status = None
        self.assertEqual(len(self.check()["bounced"]), 1, "after reconnecting it looks again straight away")

    def test_the_delivery_report_names_which_recipient_failed(self):
        self.connect(scopes=SCOPES)
        target = self.approved_target()
        target = self.client.patch(f"/api/v1/outreach/{target['id']}", headers=AUTH, json={"contact_cc": "info@bovi.example"}).json()
        target = self.client.post(f"/api/v1/outreach/{target['id']}/approve", headers=AUTH, json={
            "kind": "initial", "fingerprint": target["draft_fingerprint"], "acknowledge_warnings": True,
        }).json()
        self.send(target)
        # The headers name no failed recipient; only the report says it was the Cc.
        self.gmail.replies["thread-1"] = [failure_notice()]
        self.gmail.raw["dsn-1"] = (delivery_report(failed=["info@bovi.example"]), 2000)
        self.check()
        after = self.target(target)
        self.assertEqual(after["status"], "sent")
        self.assertEqual((after["cc_bounced"], after["contact_bounced"]), (True, False))

    def test_the_reason_is_the_notice_in_its_own_words(self):
        self.connect(scopes=SCOPES)
        target = self.approved_target()
        self.send(target)
        self.gmail.replies["thread-1"] = [failure_notice(snippet="Hello student")]
        self.gmail.raw["dsn-1"] = (delivery_report(
            text="The group you tried to contact (info) may not exist, or you may not have permission to post messages to the group.",
        ), 2000)
        self.check()
        after = self.target(target)
        self.assertEqual(after["status"], "drafted", "no report names an address, so the one it went to failed")
        self.assertTrue(after["bounce_reason"].startswith("The group you tried to contact (info) may not exist"))

    def test_a_report_of_delays_only_is_not_a_bounce(self):
        self.connect(scopes=SCOPES)
        target = self.approved_target()
        self.send(target)
        self.gmail.replies["thread-1"] = [failure_notice(subject="Delivery Status Notification")]
        self.gmail.raw["dsn-1"] = (delivery_report(delayed=["greg@bovi.example"], text="Gmail will retry for 46 more hours."), 2000)
        self.assertEqual(self.check()["bounced"], [])
        self.assertEqual(self.target(target)["status"], "sent")

    def test_a_notice_outside_the_thread_is_matched_by_address(self):
        self.connect(scopes=SCOPES)
        target = self.approved_target()
        self.send(target)
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        self.gmail.inbox_notices = ["dsn-elsewhere", "dsn-other"]
        self.gmail.raw["dsn-elsewhere"] = (delivery_report(failed=["greg@bovi.example"]), now_ms)
        self.gmail.raw["dsn-other"] = (delivery_report(failed=["someone@else.example"]), now_ms)
        result = self.check()
        self.assertEqual([item["target_id"] for item in result["bounced"]], [target["id"]])
        self.assertEqual(self.target(target)["status"], "drafted")
        fetched = [r.url.path.rsplit("/", 1)[1] for r in self.gmail.requests if "/messages/dsn" in r.url.path]
        self.assertEqual(sorted(fetched), ["dsn-elsewhere", "dsn-other"])
        second = self.client.post("/api/v1/outreach", headers=AUTH, json={
            "company": "Kiva", "contact_email": "ana@kiva.example", "email_subject": "Hello", "email_body": "Hi Ana,\n\nA note.",
        }).json()
        second = self.client.post(f"/api/v1/outreach/{second['id']}/approve", headers=AUTH, json={
            "kind": "initial", "fingerprint": second["draft_fingerprint"], "acknowledge_warnings": True,
        }).json()
        self.send(second)
        self.check()
        refetched = [r.url.path for r in self.gmail.requests if "/messages/dsn" in r.url.path]
        self.assertEqual(len(refetched), 2, "a notice already read is not fetched again")

    def test_a_notice_from_before_the_send_is_not_about_it(self):
        self.connect(scopes=SCOPES)
        target = self.approved_target()
        self.send(target)
        an_hour_ago = int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp() * 1000)
        self.gmail.inbox_notices = ["dsn-old"]
        self.gmail.raw["dsn-old"] = (delivery_report(failed=["greg@bovi.example"]), an_hour_ago)
        self.assertEqual(self.check()["bounced"], [])
        self.assertEqual(self.target(target)["status"], "sent")

    def cc_target(self):
        target = self.approved_target()
        target = self.client.patch(f"/api/v1/outreach/{target['id']}", headers=AUTH, json={"contact_cc": "info@bovi.example"}).json()
        return self.client.post(f"/api/v1/outreach/{target['id']}/approve", headers=AUTH, json={
            "kind": "initial", "fingerprint": target["draft_fingerprint"], "acknowledge_warnings": True,
        }).json()

    def test_a_bad_to_with_the_cc_reached_does_not_reopen_the_email(self):
        # The designed case: a guessed address in To, the shared inbox in Cc.
        self.connect(scopes=SCOPES)
        target = self.cc_target()
        self.send(target)
        self.gmail.replies["thread-1"] = [failure_notice()]
        self.gmail.raw["dsn-1"] = (delivery_report(failed=["greg@bovi.example"]), 2000)
        self.check()
        after = self.target(target)
        self.assertEqual(after["status"], "sent", "the Cc got it, so the company heard from the student")
        self.assertEqual((after["contact_bounced"], after["cc_bounced"]), (True, False))
        self.assertIsNone(after["bounced_at"])
        self.assertIn("partly_bounced", [event["event_type"] for event in after["events"]])
        self.assertEqual(self.send(after).status_code, 422, "the first email is not sent a second time")
        self.assertEqual(len(self.gmail.sent), 1)

    def test_when_every_recipient_failed_it_is_reopened(self):
        self.connect(scopes=SCOPES)
        target = self.cc_target()
        self.send(target)
        self.gmail.replies["thread-1"] = [failure_notice()]
        self.gmail.raw["dsn-1"] = (delivery_report(failed=["greg@bovi.example", "info@bovi.example"]), 2000)
        self.check()
        self.assertEqual(self.target(target)["status"], "drafted")

    def test_a_plain_text_delay_notice_is_not_a_bounce(self):
        self.connect(scopes=SCOPES)
        target = self.approved_target()
        self.send(target)
        self.gmail.replies["thread-1"] = [failure_notice(subject="Delivery Status Notification")]
        self.gmail.raw["dsn-1"] = (plain_notice("There was a temporary problem. We will retry for 2 more days.",
                                                subject="Delivery Status Notification"), 2000)
        self.assertEqual(self.check()["bounced"], [])
        self.assertEqual(self.target(target)["status"], "sent")

    def test_a_notice_outside_the_thread_can_name_the_address_in_a_header(self):
        self.connect(scopes=SCOPES)
        target = self.approved_target()
        self.send(target)
        self.gmail.inbox_notices = ["exim-1"]
        self.gmail.raw["exim-1"] = (plain_notice("This message was created automatically by mail delivery software.",
                                                 headers="X-Failed-Recipients: greg@bovi.example\n"),
                                    int(datetime.now(timezone.utc).timestamp() * 1000) + 30_000)
        self.check()
        self.assertEqual(self.target(target)["status"], "drafted")

    def test_a_notice_from_just_before_the_send_is_not_about_it(self):
        self.connect(scopes=SCOPES)
        target = self.approved_target()
        forty_seconds_ago = int((datetime.now(timezone.utc) - timedelta(seconds=40)).timestamp() * 1000)
        self.send(target)
        self.gmail.inbox_notices = ["dsn-early"]
        self.gmail.raw["dsn-early"] = (delivery_report(failed=["greg@bovi.example"]), forty_seconds_ago)
        self.assertEqual(self.check()["bounced"], [])

    def test_a_notice_is_reported_once(self):
        self.connect(scopes=SCOPES)
        target = self.cc_target()
        self.send(target)
        self.gmail.replies["thread-1"] = [failure_notice(notice_id="dsn-cc")]
        self.gmail.raw["dsn-cc"] = (delivery_report(failed=["info@bovi.example"]), 2000)
        self.gmail.inbox_notices = ["dsn-cc"]
        self.assertEqual(len(self.check()["bounced"]), 1, "found in the thread and the search, reported once")
        outreach_delivery._LAST_LOOK.clear()
        outreach_delivery._READ_NOTICES.clear()
        self.assertEqual(self.check()["bounced"], [], "and not again on the next check")

    def test_a_company_given_up_on_is_reopened_by_a_bounce(self):
        self.connect(scopes=SCOPES)
        target = self.approved_target()
        self.send(target)
        self.client.patch(f"/api/v1/outreach/{target['id']}", headers=AUTH, json={"status": "no_response"})
        marked = self.client.post(f"/api/v1/outreach/{target['id']}/bounce", headers=AUTH, json={"text": "Address not found"})
        self.assertEqual(marked.json()["status"], "drafted")

    def test_the_check_asks_nothing_of_gmail_when_no_send_is_recent(self):
        self.connect(scopes=SCOPES)
        self.assertEqual(self.check(), {"state": "ok", "checked": 0, "bounced": []})
        self.assertEqual(self.gmail.requests, [])


if __name__ == "__main__":
    unittest.main()
