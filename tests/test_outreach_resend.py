"""A bounced first email sent again on its own to the new contact, when only the greeting changed (bounce_auto_resend)."""

import base64
import email
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from email import policy
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR
from opportunity_app.outreach import delivery as outreach_delivery, inbox as outreach_inbox
from opportunity_app.api import create_app
from opportunity_app.outreach.targets import log_event, create_target, get_target
from opportunity_app.outreach.automation import AutomationWorker, RESEND_EVENT, recover_contact, recovery_due, resend_refusal, update_settings
from opportunity_app.outreach.delivery import record_bounce
from opportunity_app.outreach.schedule import RESEND_LABEL, run_due_sends
from opportunity_app.core.database import connect_product
from opportunity_app.core.timestamps import utc_now

from helpers_platform import build_and_migrate
from helpers_outreach import safe_fetcher, site_transport
from helpers_gmail import ACCOUNT, PDF, SCOPES, FakeGmail

AUTH = {"Authorization": "Bearer resend-owner"}
STYLE = {"word": "Hi", "unnamed": "{company} team"}
USER = "local-user"
BODY = "Hi Greg,\n\nShort note about Bovi.\n\nSam"
# Dana has a published address; careers@ and info@ are the site's shared inboxes.
SITE = {
    "bovi.test": {
        "/robots.txt": "",
        "/": '<nav><a href="/team">Team</a></nav><p>Write to <a href="mailto:careers@bovi.test">careers@bovi.test</a> '
             'or <a href="mailto:info@bovi.test">info@bovi.test</a></p>',
        "/team": '<div><h3><a href="mailto:dana.ruiz@bovi.test">Dana Ruiz</a></h3><p>Co-Founder &amp; CTO</p></div>',
    },
}
# Dana is named on the site with no address of her own, so the app can only guess hers; hello@ is listed.
GUESS_SITE = {
    "bovi.test": {
        "/robots.txt": "",
        "/": '<nav><a href="/team">Team</a></nav><p>Write to <a href="mailto:hello@bovi.test">hello@bovi.test</a></p>',
        "/team": '<div><h3>Dana Ruiz</h3><p>Co-Founder &amp; CTO</p></div>',
    },
}


def careers_mail():
    """The site's careers@ inbox writing back: not one person, so only a possible reply (outreach_inbox.py)."""
    return (
        f"From: Bovi Careers <careers@bovi.test>\nTo: {ACCOUNT}\nSubject: Next steps\n"
        "MIME-Version: 1.0\nContent-Type: text/plain; charset=UTF-8\n\nCould you send over your availability?\n"
    ).encode()


class ResendRuleTests(unittest.TestCase):
    """The rule on its own: which new contacts and which drafts may go again without a click."""

    def before(self, **values):
        return {"draft_status": "approved", "email_subject": "Question", "email_body": BODY, **values}

    def after(self, **values):
        return {"email_subject": "Question", "email_body": BODY.replace("Hi Greg,", "Hi Dana,"), "company": "Bovi",
                "contact_name": "Dana Ruiz", "contact_bounced": False, "cc_bounced": False, **values}

    def choice(self, basis, cc=True):
        return {"to": {"email": "dana@bovi.test"}, "cc": {"email": "info@bovi.test"} if cc else None, "basis": basis}

    def test_a_confirmed_address_or_a_site_inbox_needs_no_cc(self):
        for basis in ("confirmed", "shared_inbox"):
            self.assertEqual(resend_refusal(self.before(), self.after(), self.choice(basis, cc=False), resent_before=False, style=STYLE), "", basis)

    def test_a_guess_goes_only_with_a_site_inbox_in_cc(self):
        for basis in ("strong_guess", "weak_guess"):
            self.assertEqual(resend_refusal(self.before(), self.after(), self.choice(basis), resent_before=False, style=STYLE), "", basis)
            self.assertIn("is a guess", resend_refusal(self.before(), self.after(), self.choice(basis, cc=False), resent_before=False, style=STYLE))

    def test_only_the_greeting_may_change(self):
        edited = self.after(email_body="Hi Dana,\n\nA different note.\n\nSam")
        self.assertIn("More than the greeting", resend_refusal(self.before(), edited, self.choice("confirmed"), resent_before=False, style=STYLE))
        retitled = self.after(email_subject="Another question")
        self.assertIn("More than the greeting", resend_refusal(self.before(), retitled, self.choice("confirmed"), resent_before=False, style=STYLE))

    def test_only_an_approved_draft_and_only_once(self):
        self.assertIn("not an approved draft",
                      resend_refusal(self.before(draft_status="generated"), self.after(), self.choice("confirmed"), resent_before=False, style=STYLE))
        self.assertIn("already resent once", resend_refusal(self.before(), self.after(), self.choice("confirmed"), resent_before=True, style=STYLE))

    def test_the_greeting_must_fit_the_new_contact(self):
        for body, fits in (("Hi Dana,\n\nShort note about Bovi.\n\nSam", True),
                           ("Hi Bovi team,\n\nShort note about Bovi.\n\nSam", True),
                           ("Hi Dana, short note about Bovi.\n\nSam", True),
                           ("Hi Greg,\n\nShort note about Bovi.\n\nSam", False),
                           ("Short note about Bovi.\n\nSam", False)):
            before = self.before(email_body=body.replace("Dana", "Greg"))
            refusal = resend_refusal(before, self.after(email_body=body), self.choice("confirmed"), resent_before=False, style=STYLE)
            self.assertEqual(refusal == "", fits, (body, refusal))

    def test_a_first_sentence_on_the_greeting_line_counts_as_the_words(self):
        before = self.before(email_body="Hi Greg, short note about Bovi.\n\nSam")
        after = self.after(email_body="Hi Dana, a different note.\n\nSam")
        self.assertIn("More than the greeting", resend_refusal(before, after, self.choice("confirmed"), resent_before=False, style=STYLE))

    def test_never_to_an_address_that_bounced(self):
        self.assertIn("bounced", resend_refusal(self.before(), self.after(cc_bounced=True), self.choice("weak_guess"), resent_before=False, style=STYLE))

    def test_never_when_they_may_have_answered_the_first_email(self):
        # Otherwise a clean resend: a confirmed address, only the greeting changed.
        self.assertEqual(resend_refusal(self.before(), self.after(), self.choice("confirmed"), resent_before=False, style=STYLE), "")
        suggestion = {"status": "call_scheduled", "reason": "They offered a call"}
        for answered in ({"reply_count": 1}, {"reply_suggestion": suggestion}, {"possible_reply_count": 1}):
            for side in ("before", "after"):
                with self.subTest(answered=answered, side=side):
                    before = self.before(**answered) if side == "before" else self.before()
                    after = self.after(**answered) if side == "after" else self.after()
                    refusal = resend_refusal(before, after, self.choice("confirmed"), resent_before=False, style=STYLE)
                    self.assertIn("may have answered", refusal)


class ResendAfterBounceTests(unittest.TestCase):
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
            db_path=self.platform_path, access_token="resend-owner", static_dir=STATIC_DIR,
            resume_storage=root / "resumes", capture_storage=root / "captures", interview_storage=root / "interviews",
            outreach_gmail_client_factory=self.factory,
        )
        self.client = TestClient(app)
        self.client.__enter__()
        self.conn = connect_product(self.platform_path)
        update_settings(self.conn, {"bounce_recovery": True, "bounce_auto_resend": True}, user_id=USER)

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

    def approved(self, **values):
        created = self.client.post("/api/v1/outreach", headers=AUTH, json={
            "company": "Bovi", "website": "https://bovi.test", "contact_email": "greg@bovi.test", "contact_name": "Greg Hall",
            "location": "Austin, TX", "email_subject": "Robotics internship question", "email_body": BODY, **values,
        }).json()
        approved = self.client.post(f"/api/v1/outreach/{created['id']}/approve", headers=AUTH, json={
            "kind": "initial", "fingerprint": created["draft_fingerprint"], "acknowledge_warnings": True,
        })
        self.assertEqual(approved.status_code, 200, approved.text)
        return approved.json()

    def sent_and_bounced(self, **values):
        """The approved email sent from the app, then Gmail's notice that greg@ does not exist."""
        self.connect()
        target = self.approved(**values)
        sent = self.client.post(f"/api/v1/outreach/{target['id']}/gmail-send", headers=AUTH, json={
            "kind": "initial", "fingerprint": target["draft_fingerprint"],
        })
        self.assertEqual(sent.status_code, 200, sent.text)
        record_bounce(self.conn, target["id"], user_id=USER, reason="Address not found", source="gmail")
        return target

    def recover(self, target, site=SITE, mx=False):
        # With no mail server on record (mx=False) the app guesses no addresses.
        transport, _ = site_transport(site, mx=mx)
        with httpx.Client(transport=transport) as client:
            return recover_contact(self.conn, target["id"], user_id=USER, fetcher=safe_fetcher(client), contact_delay=0, automatic=True)

    def recipients(self, index):
        message = email.message_from_bytes(base64.urlsafe_b64decode(self.gmail.sent[index]["raw"]), policy=policy.default)
        return message["To"], message["Cc"], message.get_body(("plain",)).get_content()

    def events(self, target, event_type):
        after = get_target(self.conn, target["id"], user_id=USER, include_events=True)
        return [event["detail"] for event in after["events"] if event["event_type"] == event_type]

    def waiting(self, target, gmail_id="careers-9"):
        """An email from the company's careers@ inbox kept as a possible reply, as outreach_inbox keeps one."""
        stamp = utc_now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO outreach_inbox_messages(user_id, gmail_id, target_id, kind, sender, received_at, recorded_at, via, "
                "rules, reason, subject, text) VALUES(?, ?, ?, 'possible', 'careers@bovi.test', ?, ?, 'domain', ?, "
                "'shared_address', 'Next steps', 'Could you send over your availability?')",
                (USER, gmail_id, target["id"], stamp, stamp, outreach_inbox.RULES),
            )

    def queued_resend(self):
        """The bounced email readdressed to Dana and queued to go again at once."""
        target = self.sent_and_bounced()
        result = self.recover(target)
        self.assertTrue(result["resent"], result["detail"])
        self.assertEqual(get_target(self.conn, target["id"], user_id=USER)["scheduled"]["initial"]["label"], RESEND_LABEL)
        outreach_delivery._LAST_LOOK.clear()
        outreach_inbox._LAST_CAPTURE.clear()
        return target

    def cancelled_as_answered(self, target):
        """Stopped as a first email someone may have answered, with nothing more sent."""
        self.assertEqual(len(self.gmail.sent), 1, "only the email that bounced ever went")
        row = self.conn.execute(
            "SELECT state, error FROM outreach_scheduled_sends WHERE target_id=? AND kind='initial'", (target["id"],),
        ).fetchone()
        self.assertEqual(row["state"], "cancelled")
        self.assertIn("may have answered your earlier email", row["error"])
        self.assertIn("may have answered your earlier email", self.events(target, "send_cancelled")[-1])
        self.assertEqual(get_target(self.conn, target["id"], user_id=USER)["scheduled"], {})

    # --- They may have answered the first email (outreach_inbox.py) ----------------------------

    def test_no_resend_while_an_email_from_them_may_be_a_reply(self):
        target = self.sent_and_bounced()
        self.waiting(target)
        result = self.recover(target)
        self.assertFalse(result["resent"], result["detail"])
        self.assertIn("may have answered", result["detail"])
        after = get_target(self.conn, target["id"], user_id=USER)
        self.assertEqual((after["draft_status"], after["scheduled"]), ("generated", {}), "nothing approved that the student did not")
        self.assertEqual(self.events(target, RESEND_EVENT), [])
        self.assertEqual(run_due_sends(self.conn, client_factory=self.factory), [])
        self.assertEqual(len(self.gmail.sent), 1)

    def test_a_queued_resend_stops_when_their_careers_inbox_writes_back_first(self):
        target = self.queued_resend()
        # careers@ answers the first email (it reached them after all) before the worker's next pass:
        # the check just before sending finds it.
        self.gmail.raw["careers-1"] = (careers_mail(), int(datetime.now(timezone.utc).timestamp() * 1000) + 60_000)
        self.gmail.inbox_replies.append("careers-1")
        self.assertEqual([item["state"] for item in run_due_sends(self.conn, client_factory=self.factory)], ["cancelled"])
        after = get_target(self.conn, target["id"], user_id=USER)
        self.assertEqual([(item["gmail_id"], item["reason"]) for item in after["possible_replies"]], [("careers-1", "shared_address")])
        self.cancelled_as_answered(target)

    def test_a_queued_resend_stops_for_a_possible_reply_on_record(self):
        target = self.queued_resend()
        self.waiting(target)
        self.assertEqual([item["state"] for item in run_due_sends(self.conn, client_factory=self.factory)], ["cancelled"])
        self.cancelled_as_answered(target)

    def test_a_queued_resend_stops_for_a_reply_logged_meanwhile(self):
        target = self.queued_resend()
        # Pasted by the student, or found by the background watcher, while it waited in line.
        with self.conn:
            log_event(self.conn, target["id"], USER, "reply_logged", detail="Greg moved on; Dana will get back to you.")
        self.assertEqual([item["state"] for item in run_due_sends(self.conn, client_factory=self.factory)], ["cancelled"])
        self.cancelled_as_answered(target)

    def test_the_contact_is_not_searched_again_while_an_email_from_them_may_be_a_reply(self):
        # The design (outreach_inbox.py, holds): every automatic step waits for the student, the
        # contact search after a bounce included; once they say it is not a reply, it runs.
        target = self.sent_and_bounced()
        self.waiting(target)
        self.assertEqual(recovery_due(self.conn, user_id=USER), [])
        decided = self.client.post(f"/api/v1/outreach/{target['id']}/possible-replies/careers-9", headers=AUTH,
                                   json={"decision": "not_reply"})
        self.assertEqual(decided.status_code, 200, decided.text)
        self.assertEqual(recovery_due(self.conn, user_id=USER), [target["id"]])

    def test_the_bounced_email_goes_again_to_the_new_contact_on_the_next_pass(self):
        target = self.sent_and_bounced()
        result = self.recover(target)
        self.assertTrue(result["resent"], result["detail"])
        self.assertIn("Sending it again now to dana.ruiz@bovi.test", result["detail"])
        queued = get_target(self.conn, target["id"], user_id=USER)
        self.assertEqual((queued["draft_status"], queued["scheduled"]["initial"]["label"]), ("approved", RESEND_LABEL))
        self.assertEqual(len(self.events(target, RESEND_EVENT)), 1, "the approval is recorded as automatic")
        self.assertEqual([item["state"] for item in run_due_sends(self.conn, client_factory=self.factory)], ["sent"])
        self.assertEqual(len(self.gmail.sent), 2)
        to, cc, body = self.recipients(1)
        self.assertEqual((to, cc), ("dana.ruiz@bovi.test", None), "a confirmed address goes with no Cc")
        self.assertTrue(body.startswith("Hi Dana,"))
        self.assertIn("Short note about Bovi.", body, "the words the student approved")
        self.assertEqual(get_target(self.conn, target["id"], user_id=USER)["status"], "sent")

    def test_a_second_bounce_waits_for_the_student(self):
        target = self.sent_and_bounced()
        self.recover(target)
        run_due_sends(self.conn, client_factory=self.factory)
        record_bounce(self.conn, target["id"], user_id=USER, reason="Address not found", source="gmail")
        result = self.recover(target)
        self.assertFalse(result["resent"])
        self.assertIn("already resent once", result["detail"])
        after = get_target(self.conn, target["id"], user_id=USER)
        self.assertEqual((after["draft_status"], after["scheduled"]), ("generated", {}))
        self.assertEqual(len(self.gmail.sent), 2, "nothing more went out")

    def test_with_the_switch_off_the_student_reviews_it_as_before(self):
        update_settings(self.conn, {"bounce_auto_resend": False}, user_id=USER)
        target = self.sent_and_bounced()
        result = self.recover(target)
        self.assertFalse(result["resent"])
        self.assertTrue(result["detail"].endswith("Review the draft, then send it again."))
        self.assertEqual(get_target(self.conn, target["id"], user_id=USER)["draft_status"], "generated")
        self.assertEqual(run_due_sends(self.conn, client_factory=self.factory), [])
        self.assertEqual(len(self.gmail.sent), 1)

    def test_a_recovery_the_student_runs_by_hand_does_not_send(self):
        target = self.sent_and_bounced()
        transport, _ = site_transport(SITE, mx=False)
        with httpx.Client(transport=transport) as client:
            result = recover_contact(self.conn, target["id"], user_id=USER, fetcher=safe_fetcher(client), contact_delay=0)
        self.assertFalse(result["resent"])
        self.assertEqual(get_target(self.conn, target["id"], user_id=USER)["draft_status"], "generated")

    def test_a_paused_student_gets_no_resend(self):
        target = self.sent_and_bounced()
        self.client.put("/api/v1/automation/settings", headers=AUTH, json={"paused": True})
        # The worker skips recovery for a paused student; called anyway, it applies nothing.
        self.assertTrue(self.recover(target).get("paused"))
        self.assertEqual(get_target(self.conn, target["id"], user_id=USER)["scheduled"], {})
        self.assertEqual(len(self.gmail.sent), 1)

    def test_without_gmail_the_approval_is_taken_back(self):
        created = create_target(self.conn, {
            "company": "Bovi", "website": "https://bovi.test", "contact_email": "greg@bovi.test", "contact_name": "Greg Hall",
            "location": "Austin, TX", "email_subject": "Robotics internship question", "email_body": BODY,
        }, user_id=USER)
        approved = self.client.post(f"/api/v1/outreach/{created['id']}/approve", headers=AUTH, json={
            "kind": "initial", "fingerprint": created["draft_fingerprint"], "acknowledge_warnings": True,
        }).json()
        # Sent some other way (marked sent by hand), so there is no Gmail connection to resend from.
        self.client.patch(f"/api/v1/outreach/{approved['id']}", headers=AUTH, json={"status": "sent"})
        record_bounce(self.conn, approved["id"], user_id=USER, reason="Address not found")
        result = self.recover(approved)
        self.assertFalse(result["resent"])
        self.assertIn("Connect Gmail", result["detail"])
        after = get_target(self.conn, approved["id"], user_id=USER, include_events=True)
        self.assertEqual((after["draft_status"], after["scheduled"]), ("generated", {}))
        self.assertEqual(self.events(approved, RESEND_EVENT), [])

    # Found by an independent review (Codex, 2026-09-28) of the tests above.

    def test_a_guess_goes_with_the_sites_inbox_in_cc_all_the_way_to_gmail(self):
        target = self.sent_and_bounced()
        result = self.recover(target, site=GUESS_SITE, mx=True)
        self.assertTrue(result["resent"], result["detail"])
        self.assertIn("weak guess", result["detail"])
        run_due_sends(self.conn, client_factory=self.factory)
        self.assertEqual(len(self.gmail.sent), 2)
        to, cc, body = self.recipients(1)
        self.assertEqual((to, cc), ("dana@bovi.test", "hello@bovi.test"), "a wrong guess still reaches the company")
        self.assertTrue(body.startswith("Hi Dana,"))

    def test_a_greeting_to_someone_other_than_the_old_contact_is_not_resent(self):
        # Sent to the shared inbox but written to Greg: the app leaves a greeting the student chose alone,
        # so going to Dana it would still say "Hi Greg,".
        target = self.sent_and_bounced(contact_email="careers@bovi.test", contact_name="")
        result = self.recover(target)
        self.assertFalse(result["resent"], result["detail"])
        self.assertIn("greeting", result["detail"])
        after = get_target(self.conn, target["id"], user_id=USER)
        self.assertEqual((after["draft_status"], after["scheduled"]), ("generated", {}))
        self.assertEqual(len(self.gmail.sent), 1)

    def test_a_body_with_no_greeting_is_not_resent(self):
        target = self.sent_and_bounced(email_body="Short note about Bovi.\n\nSam")
        result = self.recover(target)
        self.assertFalse(result["resent"], result["detail"])
        self.assertEqual(len(self.gmail.sent), 1)

    def test_the_worker_sends_it_in_the_same_pass_that_found_the_contact(self):
        target = self.sent_and_bounced()

        def fetcher_factory():
            transport, _ = site_transport(SITE, mx=False)
            return safe_fetcher(httpx.Client(transport=transport))

        worker = AutomationWorker(self.platform_path, fetcher_factory=fetcher_factory, contact_delay=0,
                                  gmail_client_factory=self.factory)
        report = worker.run_once()
        self.assertEqual([item["resent"] for item in report["recovered"]], [True])
        self.assertEqual([item["state"] for item in report["sent"]], ["sent"])
        self.assertEqual(self.recipients(1)[0], "dana.ruiz@bovi.test")
        self.assertEqual(get_target(self.conn, target["id"], user_id=USER)["status"], "sent")

    def test_turning_the_switch_off_does_not_stop_one_already_queued(self):
        # As with scheduled sending: the switch governs what is queued next; Cancel or pause stops one queued.
        target = self.sent_and_bounced()
        self.assertTrue(self.recover(target)["resent"])
        update_settings(self.conn, {"bounce_auto_resend": False}, user_id=USER)
        self.assertEqual([item["state"] for item in run_due_sends(self.conn, client_factory=self.factory)], ["sent"])
        self.assertEqual(len(self.gmail.sent), 2)

    def test_the_students_own_send_and_the_resend_send_once_between_them(self):
        target = self.sent_and_bounced()
        self.assertTrue(self.recover(target)["resent"])
        queued = get_target(self.conn, target["id"], user_id=USER)
        sent = self.client.post(f"/api/v1/outreach/{target['id']}/gmail-send", headers=AUTH, json={
            "kind": "initial", "fingerprint": queued["draft_fingerprint"],
        })
        self.assertEqual(sent.status_code, 200, sent.text)
        self.assertEqual([item["state"] for item in run_due_sends(self.conn, client_factory=self.factory)], [])
        self.assertEqual(len(self.gmail.sent), 2, "the original, then one resend: the student's")
        self.assertEqual(get_target(self.conn, target["id"], user_id=USER)["scheduled"], {})

    def test_no_resend_when_the_cc_still_got_the_first_email(self):
        target = self.sent_and_bounced(contact_cc="hello@bovi.test")
        # Set up again: sent_and_bounced took greg@ as failing; the notice named only greg@, so hello@ got it.
        after = get_target(self.conn, target["id"], user_id=USER)
        self.assertEqual(after["status"], "sent", "a Cc was reached, so the company still heard from the student")
        self.assertEqual(recovery_due(self.conn, user_id=USER), [])
        self.assertEqual(len(self.gmail.sent), 1)


if __name__ == "__main__":
    unittest.main()
