"""Replies to outreach read from Gmail: logged once, a quiet company moved to Replied, the rest suggested."""

import json
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR, automation, outreach_delivery, outreach_gmail, outreach_inbox
from opportunity_app.api import create_app
from opportunity_app.outreach_inbox import InboxWatcher, reply_text, strip_quoted
from opportunity_app.schema import connect_product, utc_now

from helpers_platform import build_and_migrate
from test_outreach_gmail import ACCOUNT, PDF, SCOPES, AlwaysInTransaction, FakeGmail, forget_gmail_backoff, rate_limited

AUTH = {"Authorization": "Bearer inbox-owner"}
USER = "local-user"


def now_ms(offset=timedelta()):
    return int((datetime.now(timezone.utc) + offset).timestamp() * 1000)


def mail(body, *, sender="Greg Lee <greg@bovi.example>", subject="Re: Robotics internship question", headers=""):
    """A message as the raw text Gmail stores."""
    return (
        f"From: {sender}\nTo: {ACCOUNT}\nSubject: {subject}\n{headers}"
        "MIME-Version: 1.0\nContent-Type: text/plain; charset=UTF-8\n\n"
        f"{body}\n"
    ).encode()


class QuotedTextTests(unittest.TestCase):
    def test_the_quoted_email_is_cut_off(self):
        gmail = "Sounds good, Thursday works.\n\nOn Fri, Sep 26, 2026 at 10:02 AM Sam Rivera <sam@school.example>\nwrote:\n> Hi Greg,\n> A short note."
        self.assertEqual(strip_quoted(gmail), "Sounds good, Thursday works.")
        outlook = "Thanks, passing this on.\n\nFrom: Sam Rivera <sam@school.example>\nSent: Friday, September 26, 2026 10:02 AM\nTo: Greg"
        self.assertEqual(strip_quoted(outlook), "Thanks, passing this on.")
        self.assertEqual(strip_quoted("Yes!\n> Would you consider me"), "Yes!")

    def test_an_html_only_reply_is_read_as_text(self):
        raw = (
            f"From: Greg <greg@bovi.example>\nTo: {ACCOUNT}\nSubject: Re: hi\nMIME-Version: 1.0\n"
            "Content-Type: text/html; charset=UTF-8\n\n<div>Let&#39;s talk<br>Tuesday?</div><blockquote>old</blockquote>\n"
        ).encode()
        self.assertEqual(reply_text(BytesParser(policy=policy.default).parsebytes(raw)), "Let's talk\nTuesday?")


class ReplyCaptureTests(unittest.TestCase):
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
            db_path=self.platform_path, access_token="inbox-owner", static_dir=STATIC_DIR,
            resume_storage=root / "resumes", capture_storage=root / "captures", interview_storage=root / "interviews",
            outreach_gmail_client_factory=self.factory,
        )
        self.client = TestClient(app)
        self.client.__enter__()
        outreach_delivery._LAST_LOOK.clear()
        outreach_delivery._READ_NOTICES.clear()
        outreach_inbox._LAST_CAPTURE.clear()
        forget_gmail_backoff(self)
        self.connect()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.env.stop()
        self.tempdir.cleanup()

    def connect(self, scopes=SCOPES):
        fernet = Fernet(self.key.encode())
        with closing(connect_product(self.platform_path)) as conn:
            conn.execute(
                """INSERT INTO connector_accounts(id, user_id, provider, scopes_json, encrypted_access_token, encrypted_refresh_token, status, created_at, updated_at)
                   VALUES(?, ?, 'gmail_drafts', ?, ?, ?, 'connected', ?, ?)""",
                (f"connector-gmail_drafts-{USER}", USER, str(list(scopes)).replace("'", '"'),
                 fernet.encrypt(b"valid-token").decode(), fernet.encrypt(b"refresh-token").decode(), utc_now(), utc_now()),
            )
            conn.commit()

    def sent_target(self, **overrides):
        created = self.client.post("/api/v1/outreach", headers=AUTH, json={
            "company": "Bovi", "contact_email": "greg@bovi.example", "website": "https://bovi.example",
            "email_subject": "Robotics internship question", "email_body": "Hi Greg,\n\nShort note about Bovi.\n\nSam",
            **overrides,
        }).json()
        approved = self.client.post(f"/api/v1/outreach/{created['id']}/approve", headers=AUTH, json={
            "kind": "initial", "fingerprint": created["draft_fingerprint"], "acknowledge_warnings": True,
        }).json()
        sent = self.client.post(f"/api/v1/outreach/{approved['id']}/gmail-send", headers=AUTH, json={
            "kind": "initial", "fingerprint": approved["draft_fingerprint"],
        })
        self.assertEqual(sent.status_code, 200, sent.text)
        return self.target(approved)

    def arrive(self, message_id, raw, received=None):
        self.gmail.raw[message_id] = (raw, received if received is not None else now_ms(timedelta(minutes=5)))
        self.gmail.inbox_replies.append(message_id)

    def check(self):
        outreach_inbox._LAST_CAPTURE.clear()
        response = self.client.post("/api/v1/outreach/inbox-check", headers=AUTH)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def target(self, target):
        return self.client.get(f"/api/v1/outreach/{target['id']}", headers=AUTH).json()

    def replies(self, target):
        return [event["detail"] for event in self.target(target)["events"] if event["event_type"] == "reply_logged"]

    def test_a_reply_is_logged_and_the_company_moves_to_replied(self):
        target = self.sent_target()
        self.assertTrue(target["follow_up_at"])
        self.arrive("reply-1", mail(
            "Happy to hop on a call. When are you free next week?\n\n"
            "On Fri, Sep 26, 2026 at 10:02 AM Sam <sam@school.example> wrote:\n> Hi Greg,"
        ))
        result = self.check()
        self.assertEqual([item["target_id"] for item in result["replies"]], [target["id"]])
        after = self.target(target)
        self.assertEqual((after["status"], after["follow_up_at"]), ("replied", None), "someone wrote back: no follow-up")
        self.assertEqual(self.replies(target), ["Happy to hop on a call. When are you free next week?"])
        self.assertEqual(after["reply_suggestion"]["status"], "call_scheduled")
        self.assertEqual(after["reply_suggestion"]["from"], "greg@bovi.example")
        self.assertEqual(after["suggestion"]["status"], "call_scheduled", "offered, not applied")
        self.assertIsNotNone(after["call_prep_job"], "call prep starts from the captured reply")
        self.assertIn("from:(bovi.example OR greg@bovi.example)", [q for q in self.gmail.searches if "mailer" not in q][0])

    def test_the_same_reply_is_logged_once(self):
        target = self.sent_target()
        self.arrive("reply-1", mail("Thanks, got it."))
        self.check()
        self.assertEqual(self.check()["replies"], [])
        self.assertEqual(len(self.replies(target)), 1)
        fetched = [r for r in self.gmail.requests if r.url.path.endswith("/messages/reply-1")]
        self.assertEqual(len(fetched), 1, "a message already read is not fetched again")

    def test_an_out_of_office_reply_changes_nothing(self):
        target = self.sent_target()
        self.arrive("auto-1", mail("I'm away until October 6.", subject="Automatic reply: Robotics internship question",
                                   headers="Auto-Submitted: auto-replied\n"))
        result = self.check()
        self.assertEqual((result["replies"], len(result["automatic"])), ([], 1))
        after = self.target(target)
        self.assertEqual(after["status"], "sent")
        self.assertEqual(after["reply_count"], 0)
        self.assertIn("auto_reply", [event["event_type"] for event in after["events"]])

    def test_a_colleague_answering_counts_but_a_newsletter_or_fresh_email_does_not(self):
        target = self.sent_target()
        # From the contact's own address, as a shared info@ inbox sends newsletters.
        self.arrive("news-1", mail("Our September update", sender="Bovi <greg@bovi.example>", subject="Bovi news",
                                   headers="List-Unsubscribe: <mailto:unsub@bovi.example>\n"))
        self.arrive("sales-1", mail("Want a demo?", sender="Sales <sales@bovi.example>", subject="Bovi demo"))
        self.assertEqual(self.check()["replies"], [])
        self.assertEqual(self.target(target)["status"], "sent")
        self.arrive("ceo-1", mail("Greg forwarded your note. Let's talk.", sender="Ana Bovi <ana@bovi.example>"))
        self.assertEqual(len(self.check()["replies"]), 1)
        self.assertEqual(self.target(target)["status"], "replied")

    def test_a_contact_on_a_shared_email_domain_is_matched_only_by_address(self):
        target = self.sent_target(contact_email="greg.bovi@gmail.com", website="https://bovi.example")
        self.arrive("stranger-1", mail("Re: something else", sender="Someone <someone@gmail.com>"))
        self.assertEqual(self.check()["replies"], [])
        self.assertFalse([q for q in self.gmail.searches if "gmail.com OR" in q or q.startswith("from:(gmail.com")])
        self.arrive("greg-1", mail("Yes, let's chat.", sender="Greg <greg.bovi@gmail.com>"))
        self.assertEqual(len(self.check()["replies"]), 1)
        self.assertEqual(self.target(target)["status"], "replied")

    def test_mail_from_before_the_send_is_not_a_reply(self):
        target = self.sent_target()
        self.arrive("old-1", mail("Re: an older thread"), received=now_ms(-timedelta(days=3)))
        self.assertEqual(self.check()["replies"], [])
        self.assertEqual(self.target(target)["status"], "sent")

    def test_a_company_marked_sent_by_hand_is_watched_too(self):
        created = self.client.post("/api/v1/outreach", headers=AUTH, json={
            "company": "Kiva", "contact_email": "ana@kiva.example", "status": "sent",
        }).json()
        self.arrive("kiva-1", mail("We'd love to talk.", sender="Ana <ana@kiva.example>"))
        self.assertEqual(len(self.check()["replies"]), 1)
        self.assertEqual(self.target(created)["status"], "replied")

    def test_a_decline_is_suggested_and_answering_it_clears_the_suggestion(self):
        target = self.sent_target()
        self.arrive("no-1", mail("Thanks for reaching out, but we're not hiring interns this term."))
        self.check()
        after = self.target(target)
        self.assertEqual((after["status"], after["reply_suggestion"]["status"]), ("replied", "declined"))
        declined = self.client.patch(f"/api/v1/outreach/{target['id']}", headers=AUTH, json={"status": "declined"}).json()
        self.assertIsNone(declined["reply_suggestion"])

    def test_a_suggestion_can_be_dismissed(self):
        target = self.sent_target()
        self.arrive("call-1", mail("Can we set up a call? What's your availability?"))
        self.check()
        dismissed = self.client.delete(f"/api/v1/outreach/{target['id']}/reply-suggestion", headers=AUTH)
        self.assertEqual(dismissed.status_code, 200, dismissed.text)
        self.assertIsNone(dismissed.json()["reply_suggestion"])
        self.assertEqual(dismissed.json()["status"], "replied")

    def test_a_reply_about_a_failed_delivery_is_still_a_reply(self):
        target = self.sent_target()
        self.arrive("person-1", mail("Your first message was undeliverable to our old inbox, but I got this one. Let's talk."))
        self.check()
        after = self.target(target)
        self.assertEqual(after["status"], "replied")
        self.assertIsNone(after["reply_suggestion"])

    def test_without_the_read_scope_it_asks_for_a_reconnect(self):
        self.sent_target()
        self.gmail.thread_status = 403
        self.assertEqual(self.check()["state"], "needs_reconnect")

    def test_mail_from_just_before_the_send_is_not_a_reply(self):
        target = self.sent_target()
        self.arrive("early-1", mail("Re: our earlier chat"), received=now_ms(-timedelta(minutes=1)))
        self.assertEqual(self.check()["replies"], [])
        self.assertEqual(self.target(target)["status"], "sent")

    def test_anyone_at_the_companys_own_domain_counts_even_if_the_contact_is_elsewhere(self):
        target = self.sent_target(contact_email="greg@bovi-mail.example")
        self.arrive("ana-1", mail("Greg passed this on. Let's talk.", sender="Ana <ana@eng.bovi.example>"))
        self.assertEqual(len(self.check()["replies"]), 1)
        self.assertEqual(self.target(target)["status"], "replied")

    def test_a_search_that_fails_is_reported_not_taken_as_nothing(self):
        self.sent_target()
        self.gmail.thread_status = 400
        self.assertEqual(self.check()["state"], "unreachable")
        # A Gmail server error is a passing fault: reads wait, as for a rate limit, and it is still not "nothing".
        self.gmail.thread_status = 503
        self.assertEqual(self.check()["state"], "throttled")
        self.assertIsNotNone(outreach_gmail.backoff_until(USER))

    def test_every_page_of_results_is_read(self):
        target = self.sent_target()
        self.gmail.page_size = 1
        for number in range(3):
            self.arrive(f"news-{number}", mail("Our update", subject="Bovi news", headers="List-Unsubscribe: <mailto:x@bovi.example>\n"))
        self.arrive("reply-late", mail("Yes, let's talk."))
        self.assertEqual(len(self.check()["replies"]), 1, "the reply on the last page is found")
        self.assertEqual(self.target(target)["status"], "replied")

    def test_two_checks_reading_the_same_reply_log_it_once(self):
        from opportunity_app.outreach_inbox import _record_reply

        target = self.sent_target()
        with closing(connect_product(self.platform_path)) as conn:
            current = self.target(target)
            first = _record_reply(conn, current, user_id=USER, gmail_id="same-1", sender="greg@bovi.example",
                                  received=utc_now(), text="Sure.", decisions=None)
            second = _record_reply(conn, current, user_id=USER, gmail_id="same-1", sender="greg@bovi.example",
                                   received=utc_now(), text="Sure.", decisions=None)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(self.replies(target)), 1)

    def test_the_background_watcher_captures_replies(self):
        target = self.sent_target()
        self.arrive("reply-1", mail("Sure, let's talk."))
        outreach_inbox._LAST_CAPTURE.clear()
        watcher = InboxWatcher(self.platform_path, client_factory=self.factory, decisions_for=lambda conn, user_id: None)
        watcher.run_once()
        self.assertEqual(self.target(target)["status"], "replied")
        health = self.health()
        for component in ("inbox.sends", "inbox.deliveries", "inbox.replies", "inbox.connection"):
            self.assertIsNotNone(health[component]["last_ok_at"], component)
            self.assertEqual(health[component]["last_error"], "", component)

    # --- The background watcher, one step at a time ----------------------------------

    def watcher(self):
        return InboxWatcher(self.platform_path, client_factory=self.factory, decisions_for=lambda conn, user_id: None)

    def health(self):
        with closing(connect_product(self.platform_path)) as conn:
            return {row["component"]: dict(row) for row in conn.execute(
                "SELECT * FROM automation_health WHERE user_id=?", (USER,),
            ).fetchall()}

    def test_a_step_that_raises_does_not_stop_the_others(self):
        deliveries = mock.Mock(return_value={"state": "ok", "checked": 0, "bounced": []})
        replies = mock.Mock(return_value={"state": "unreachable", "replies": [], "automatic": []})
        with mock.patch("opportunity_app.outreach_gmail_sends.capture_gmail_sends",
                        side_effect=RuntimeError("could not read the draft to greg@bovi.example")),                 mock.patch.object(outreach_inbox, "check_deliveries", deliveries),                 mock.patch.object(outreach_inbox, "capture_replies", replies):
            self.watcher().run_once()
        deliveries.assert_called_once()
        replies.assert_called_once()
        health = self.health()
        self.assertEqual(health["inbox.sends"]["last_error"], "RuntimeError: could not read the draft to [address]",
                         "the error names the failure, never an address")
        self.assertIsNone(health["inbox.sends"]["last_ok_at"])
        self.assertIsNotNone(health["inbox.deliveries"]["last_ok_at"])
        self.assertEqual(health["inbox.deliveries"]["last_error"], "")
        self.assertEqual(health["inbox.replies"]["last_error"], "Gmail could not be reached")
        self.assertIsNotNone(health["inbox.connection"]["last_ok_at"])

    def test_a_broken_connection_gets_a_notice_and_gmail_is_not_asked(self):
        self.sent_target()
        asked = len(self.gmail.requests)
        with closing(connect_product(self.platform_path)) as conn:
            conn.execute("UPDATE connector_accounts SET status='error', updated_at='2026-09-27T10:00:00+00:00'")
            conn.commit()
        self.watcher().run_once()
        first = self.health()["inbox.connection"]
        self.watcher().run_once()
        self.assertEqual(len(self.gmail.requests), asked, "nothing is asked of a connection that needs reconnecting")
        health = self.health()
        self.assertTrue(health["inbox.connection"]["last_error"].startswith("Gmail needs reconnecting (since "),
                        health["inbox.connection"]["last_error"])
        self.assertEqual(json.loads(health["inbox.connection"]["detail_json"]),
                         {"state": "error", "since": "2026-09-27T10:00:00+00:00"})
        self.assertEqual(health["inbox.connection"]["last_error_at"], first["last_error_at"],
                         "a later pass does not move when it broke")
        self.assertNotIn("inbox.replies", health)
        with closing(connect_product(self.platform_path)) as conn:
            notices = automation.list_notices(conn, USER)
        self.assertEqual([(n["event_key"], n["level"]) for n in notices], [("gmail-expired:2026-09-27T10:00:00+00:00", "problem")],
                         "one notice, however many passes")

    def test_a_break_keeps_its_earliest_since_until_the_connection_works_again(self):
        with closing(connect_product(self.platform_path)) as conn:
            conn.execute("UPDATE connector_accounts SET status='error', updated_at='2026-09-27T10:00:00+00:00'")
            conn.commit()
        self.watcher().run_once()
        with closing(connect_product(self.platform_path)) as conn:
            # Something touched the row later while it stayed broken.
            conn.execute("UPDATE connector_accounts SET updated_at='2026-09-27T11:30:00+00:00'")
            conn.commit()
        self.watcher().run_once()
        self.assertEqual(json.loads(self.health()["inbox.connection"]["detail_json"])["since"], "2026-09-27T10:00:00+00:00")
        # Reconnected, then broken again: a new break has its own since.
        with closing(connect_product(self.platform_path)) as conn:
            conn.execute("UPDATE connector_accounts SET status='connected'")
            conn.commit()
        self.watcher().run_once()
        connected = self.health()["inbox.connection"]
        self.assertEqual(json.loads(connected["detail_json"]), {"state": "connected"})
        with closing(connect_product(self.platform_path)) as conn:
            conn.execute("UPDATE connector_accounts SET status='error', updated_at='2026-09-28T09:00:00+00:00'")
            conn.commit()
        self.watcher().run_once()
        self.assertEqual(json.loads(self.health()["inbox.connection"]["detail_json"]),
                         {"state": "error", "since": "2026-09-28T09:00:00+00:00"})

    def test_a_connection_the_student_disconnected_is_not_an_error(self):
        self.sent_target()
        with closing(connect_product(self.platform_path)) as conn:
            disconnected = conn.execute("SELECT id FROM connector_accounts WHERE provider='gmail_drafts'").fetchone()[0]
        self.assertEqual(self.client.delete(f"/api/v1/connections/{disconnected}", headers=AUTH).status_code, 200)
        asked = len(self.gmail.requests)
        self.watcher().run_once()
        first = self.health()["inbox.connection"]
        self.watcher().run_once()
        self.assertEqual(len(self.gmail.requests), asked, "nothing is asked of Gmail")
        row = self.health()["inbox.connection"]
        self.assertEqual((row["last_error"], row["last_error_at"]), ("", None), "never 'needs reconnecting'")
        self.assertIsNotNone(row["last_ok_at"])
        self.assertEqual(json.loads(row["detail_json"]), {"state": "disconnected"})
        self.assertEqual(row["updated_at"], first["updated_at"], "recorded once, not on every pass")
        with closing(connect_product(self.platform_path)) as conn:
            self.assertEqual(automation.list_notices(conn, USER), [], "and no notice")

    def test_on_postgresql_the_watcher_saves_gmail_health_between_its_steps(self):
        # On PostgreSQL every connection is "in a transaction" after its first read, so a
        # rate limit seen inside a step cannot be written there; the watcher saves it after.
        target = self.sent_target()
        self.arrive("reply-1", mail("Sure, let's talk."))
        with closing(connect_product(self.platform_path)) as conn:
            conn.execute("UPDATE connector_accounts SET backoff_until=NULL, last_error='', last_ok_at=NULL")
            conn.commit()
        outreach_gmail._BACKOFF.clear()
        outreach_gmail._HEALTH.clear()
        self.gmail.read_response = rate_limited
        outreach_inbox._LAST_CAPTURE.clear()
        real_connect = outreach_inbox.connect_product
        with mock.patch.object(outreach_inbox, "connect_product", lambda target: AlwaysInTransaction(real_connect(target))):
            self.watcher().run_once()
        with closing(connect_product(self.platform_path)) as conn:
            row = dict(conn.execute("SELECT * FROM connector_accounts WHERE provider='gmail_drafts'").fetchone())
            self.assertTrue(row["backoff_until"], "the hold survives a restart")
            self.assertEqual(row["last_error"], "Gmail asked the app to slow down (HTTP 403)")
            self.assertEqual(automation.gmail_health(conn, USER)["state"], "throttled")
        # Gmail answers again: the next pass records it, and the reply is captured.
        outreach_gmail._BACKOFF.clear()
        with closing(connect_product(self.platform_path)) as conn:
            conn.execute("UPDATE connector_accounts SET backoff_until=NULL")
            conn.commit()
        self.gmail.read_response = None
        outreach_inbox._LAST_CAPTURE.clear()
        with mock.patch.object(outreach_inbox, "connect_product", lambda target: AlwaysInTransaction(real_connect(target))):
            self.watcher().run_once()
        with closing(connect_product(self.platform_path)) as conn:
            row = dict(conn.execute("SELECT * FROM connector_accounts WHERE provider='gmail_drafts'").fetchone())
        self.assertIsNotNone(row["last_ok_at"])
        self.assertEqual((row["last_error"], row["backoff_until"]), ("", None))
        self.assertEqual(self.target(target)["status"], "replied")

    def test_a_gmail_slowdown_is_recorded_as_one_and_never_as_a_reconnect(self):
        target = self.sent_target()
        self.arrive("reply-1", mail("Sure, let's talk."))
        self.gmail.read_response = rate_limited
        outreach_inbox._LAST_CAPTURE.clear()
        self.watcher().run_once()
        health = self.health()
        self.assertEqual(health["inbox.deliveries"]["last_error"], "Gmail asked the app to slow down")
        self.assertEqual(health["inbox.replies"]["last_error"], "Gmail asked the app to slow down")
        self.assertIsNotNone(health["inbox.connection"]["last_ok_at"])
        self.assertEqual(self.target(target)["status"], "sent", "the reply waits for the next look")
        with closing(connect_product(self.platform_path)) as conn:
            self.assertEqual(conn.execute("SELECT status FROM connector_accounts").fetchone()[0], "connected")
            self.assertEqual(automation.list_notices(conn, USER), [])


if __name__ == "__main__":
    unittest.main()
