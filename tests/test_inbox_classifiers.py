"""Jev inbox suggestions and their fallback to the keyword rules.

Every path that cannot use Jev -- no key, the student has not turned it on,
TypeSafe errors, an unreadable answer, or low confidence -- must give the same
suggestion the rules give today, and say that the rules gave it.
"""

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
from opportunity_app.connections import classify_monitored_message
from opportunity_app.inbox_classifiers import (
    MIN_CONFIDENCE,
    build_client,
    classify_email,
    classify_reply,
    client_for,
    enabled,
    set_enabled,
)
from opportunity_app.outreach import suggest_reply_status
from opportunity_app.schema import LOCAL_USER_ID, connect_product
from opportunity_app.typesafe_decisions import TypeSafeNotConfigured, TypeSafeResponseError

from helpers_platform import build_and_migrate

AUTH = {"Authorization": "Bearer inbox-owner"}
# The rules read this as declined; a reader sees the call it proposes.
MIXED_REPLY = "We're not hiring interns right now, but I'd be glad to hop on a call next week."
# A Google Groups rejection of a send to a company's info@ inbox, as Gmail showed it.
GROUP_BOUNCE = """Delivery Status Notification (Failure)
Mail Delivery Subsystem <mailer-daemon@googlemail.com>
Hello student@school.example,

We're writing to let you know that the group you tried to contact (info) may not exist, or you may not have
permission to post messages to the group. A few more details on why you weren't able to post:

 * You might have spelled or formatted the group name incorrectly.
 * The owner of the group may have removed this group.
"""


class FakeJev:
    """Answers every Choice with one label at one confidence, and counts calls."""

    configured = True
    model = "jev-1.13.0"

    def __init__(self, label=None, confidence=0.93, error=None, answer=None):
        self.label, self.confidence, self.error, self.answer = label, confidence, error, answer
        self.calls = []

    def evaluate(self, *, state, questions):
        self.calls.append(state)
        if self.error is not None:
            raise self.error
        (question_id, question), = questions.items()
        if self.answer is not None:
            return {"model": self.model, "answers": {question_id: self.answer}, "usage": {"input_tokens": 1, "output_tokens": 0}}
        options = list(question["criteria"])
        label = self.label or options[0]
        return {
            "model": self.model,
            "answers": {question_id: {
                "type": "choice", "choice": label, "confidence": self.confidence,
                "probabilities": {option: (self.confidence if option == label else 0.0) for option in options},
            }},
            "usage": {"input_tokens": 1, "output_tokens": 0},
        }


class BounceNoticeTests(unittest.TestCase):
    def test_delivery_failures_suggest_bounced(self):
        for text in (
            GROUP_BOUNCE,
            "Address not found\nYour message wasn't delivered to greg@bovi.example because the address couldn't be found.",
            "Undeliverable: Internship question\nDelivery has failed to these recipients or groups: greg@bovi.example",
            "Mail delivery failed: returning message to sender\n550 5.1.1 <greg@bovi.example>: Recipient address rejected: User unknown",
        ):
            with self.subTest(text=text[:40]):
                self.assertEqual(suggest_reply_status(text)["status"], "bounced")

    def test_a_delay_is_not_a_bounce(self):
        delay = "Delivery Status Notification (Delay)\nThere was a temporary problem delivering your message. Gmail will retry for 46 more hours."
        self.assertNotEqual(suggest_reply_status(delay)["status"], "bounced")

    def test_an_ordinary_reply_is_not_a_bounce(self):
        self.assertEqual(suggest_reply_status("Thanks for reaching out, we're not hiring interns this term.")["status"], "declined")
        self.assertEqual(suggest_reply_status("Got it, I'll forward this to our CTO.")["status"], "replied")


class ClassifyReplyTests(unittest.TestCase):
    def test_without_a_client_the_rules_answer(self):
        suggestion = classify_reply(MIXED_REPLY, suggest_reply_status, None)
        self.assertEqual(suggestion["status"], suggest_reply_status(MIXED_REPLY)["status"])
        self.assertEqual(suggestion["source"], "rules")
        self.assertIsNone(suggestion["confidence"])
        self.assertEqual(suggestion["fallback_reason"], "")

    def test_a_confident_jev_answer_is_used_and_labelled(self):
        jev = FakeJev("call_scheduled", 0.91)
        suggestion = classify_reply(MIXED_REPLY, suggest_reply_status, jev)
        self.assertEqual(suggestion["status"], "call_scheduled")
        self.assertEqual(suggestion["source"], "jev")
        self.assertEqual(suggestion["model"], "jev-1.13.0")
        self.assertIn("Jev suggestion, 91% sure", suggestion["reason"])
        self.assertEqual(jev.calls, [{"reply": MIXED_REPLY}])

    def test_an_unsure_jev_answer_gives_way_to_the_rules(self):
        suggestion = classify_reply(MIXED_REPLY, suggest_reply_status, FakeJev("call_scheduled", MIN_CONFIDENCE - 0.01))
        self.assertEqual(suggestion["status"], suggest_reply_status(MIXED_REPLY)["status"])
        self.assertEqual(suggestion["source"], "rules")
        self.assertIn("Jev was unsure", suggestion["fallback_reason"])

    def test_a_typesafe_error_gives_way_to_the_rules(self):
        suggestion = classify_reply(MIXED_REPLY, suggest_reply_status, FakeJev(error=TypeSafeResponseError("TypeSafe timed out")))
        self.assertEqual(suggestion["source"], "rules")
        self.assertIn("timed out", suggestion["fallback_reason"])

    def test_an_unreadable_answer_gives_way_to_the_rules(self):
        for answer in ({"type": "choice"}, {"type": "choice", "choice": "hired", "confidence": 0.99}):
            with self.subTest(answer=answer):
                suggestion = classify_reply(MIXED_REPLY, suggest_reply_status, FakeJev(answer=answer))
                self.assertEqual(suggestion["source"], "rules")
                self.assertIn("could not read", suggestion["fallback_reason"])


class ClassifyEmailTests(unittest.TestCase):
    SUBJECT, BODY = "Next steps", "We'd love to set up an interview. What times work for you next week?"

    def test_without_a_client_the_rules_answer(self):
        event_type, confidence, how = classify_email(self.SUBJECT, self.BODY, classify_monitored_message, None)
        self.assertEqual((event_type, confidence), classify_monitored_message(self.SUBJECT, self.BODY))
        self.assertEqual(how["source"], "rules")

    def test_a_confident_jev_answer_carries_its_own_confidence(self):
        event_type, confidence, how = classify_email(self.SUBJECT, self.BODY, classify_monitored_message, FakeJev("interview", 0.88))
        self.assertEqual((event_type, confidence), ("interview", 0.88))
        self.assertEqual(how["source"], "jev")

    def test_errors_and_low_confidence_give_way_to_the_rules(self):
        rules = classify_monitored_message(self.SUBJECT, self.BODY)
        clients = {"rate limited": FakeJev(error=TypeSafeResponseError("TypeSafe returned HTTP 429")), "unsure": FakeJev("interview", 0.3)}
        for case, client in clients.items():
            with self.subTest(case):
                event_type, confidence, how = classify_email(self.SUBJECT, self.BODY, classify_monitored_message, client)
                self.assertEqual((event_type, confidence), rules)
                self.assertEqual(how["source"], "rules")
                self.assertTrue(how["fallback_reason"])


class SettingTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        _, self.platform_path = build_and_migrate(Path(self.tempdir.name))
        self.conn = connect_product(self.platform_path)

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def test_off_by_default_and_per_student(self):
        self.assertFalse(enabled(self.conn, user_id=LOCAL_USER_ID))
        set_enabled(self.conn, True, user_id=LOCAL_USER_ID)
        self.assertTrue(enabled(self.conn, user_id=LOCAL_USER_ID))
        set_enabled(self.conn, False, user_id=LOCAL_USER_ID)
        self.assertFalse(enabled(self.conn, user_id=LOCAL_USER_ID))

    def test_no_client_unless_turned_on_and_configured(self):
        jev = FakeJev()
        self.assertIsNone(client_for(self.conn, lambda: jev, user_id=LOCAL_USER_ID), "off by default")
        set_enabled(self.conn, True, user_id=LOCAL_USER_ID)
        self.assertIs(client_for(self.conn, lambda: jev, user_id=LOCAL_USER_ID), jev)
        self.assertIsNone(client_for(self.conn, lambda: None, user_id=LOCAL_USER_ID), "no key on this copy")

        def misconfigured():
            raise TypeSafeNotConfigured("TYPESAFE_BASE_URL must use HTTPS")

        self.assertIsNone(client_for(self.conn, misconfigured, user_id=LOCAL_USER_ID))

    def test_build_client_without_a_key_is_none(self):
        with mock.patch.dict("os.environ", {"TYPESAFE_API_KEY": ""}):
            self.assertIsNone(build_client())
        with mock.patch.dict("os.environ", {"TYPESAFE_API_KEY": "k", "TYPESAFE_BASE_URL": "http://example.com"}):
            self.assertIsNone(build_client(), "a bad base URL falls back instead of raising")


class InboxSuggestionApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        _, self.platform_path = build_and_migrate(Path(self.tempdir.name))
        self.jev = FakeJev("call_scheduled", 0.9)
        self.factory = lambda: self.jev
        app = create_app(
            db_path=self.platform_path, access_token="inbox-owner", static_dir=STATIC_DIR,
            inbox_client_factory=lambda: self.factory(),
        )
        self.client = TestClient(app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tempdir.cleanup()

    def log_reply(self, text=MIXED_REPLY):
        created = self.client.post("/api/v1/outreach", headers=AUTH, json={"company": "Bovi", "status": "sent"}).json()
        response = self.client.post(f"/api/v1/outreach/{created['id']}/reply", headers=AUTH, json={"text": text})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["suggestion"]

    def test_off_by_default_sends_nothing(self):
        status = self.client.get("/api/v1/typesafe/inbox-suggestions", headers=AUTH).json()
        self.assertEqual((status["available"], status["enabled"]), (True, False))
        suggestion = self.log_reply()
        self.assertEqual(suggestion["source"], "rules")
        self.assertEqual(suggestion["status"], suggest_reply_status(MIXED_REPLY)["status"])
        self.assertEqual(self.jev.calls, [], "nothing leaves the machine until the student turns it on")

    def test_turned_on_a_reply_is_classified_by_jev(self):
        put = self.client.put("/api/v1/typesafe/inbox-suggestions", headers=AUTH, json={"enabled": True})
        self.assertEqual(put.status_code, 200, put.text)
        self.assertTrue(put.json()["enabled"])
        suggestion = self.log_reply()
        self.assertEqual((suggestion["status"], suggestion["source"]), ("call_scheduled", "jev"))

    def test_turned_on_without_a_key_the_rules_answer(self):
        self.factory = lambda: None
        self.client.put("/api/v1/typesafe/inbox-suggestions", headers=AUTH, json={"enabled": True})
        status = self.client.get("/api/v1/typesafe/inbox-suggestions", headers=AUTH).json()
        self.assertEqual((status["available"], status["enabled"]), (False, True))
        self.assertEqual(self.log_reply()["source"], "rules")

    def test_a_typesafe_outage_does_not_fail_logging_a_reply(self):
        self.jev.error = TypeSafeResponseError("Could not reach TypeSafe")
        self.client.put("/api/v1/typesafe/inbox-suggestions", headers=AUTH, json={"enabled": True})
        suggestion = self.log_reply()
        self.assertEqual(suggestion["source"], "rules")
        self.assertIn("Could not reach TypeSafe", suggestion["fallback_reason"])

    def test_a_pasted_bounce_is_not_a_reply_and_never_reaches_jev(self):
        self.client.put("/api/v1/typesafe/inbox-suggestions", headers=AUTH, json={"enabled": True})
        created = self.client.post("/api/v1/outreach", headers=AUTH, json={
            "company": "Bovi", "status": "sent", "contact_email": "info@bovi.example",
        }).json()
        response = self.client.post(f"/api/v1/outreach/{created['id']}/reply", headers=AUTH, json={"text": GROUP_BOUNCE})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["suggestion"]["status"], body["suggestion"]["source"]), ("bounced", "rules"))
        self.assertFalse(body["logged"])
        self.assertEqual(self.jev.calls, [], "a bounce is settled by the rules; Jev has no bounced label")
        self.assertEqual(body["target"]["reply_count"], 0)
        self.assertEqual(body["target"]["status"], "sent", "the suggestion changes nothing until it is applied")

        marked = self.client.post(f"/api/v1/outreach/{created['id']}/bounce", headers=AUTH, json={"text": GROUP_BOUNCE})
        self.assertEqual(marked.status_code, 200, marked.text)
        target = marked.json()
        self.assertEqual((target["status"], target["sent_at"], target["follow_up_at"]), ("drafted", None, None))
        self.assertEqual(target["bounced_addresses"], ["info@bovi.example"])
        self.assertTrue(target["contact_bounced"])
        self.assertEqual(target["bounce_reason"], "Delivery Status Notification (Failure)")

    def test_connector_email_records_how_it_was_classified(self):
        connector = self.client.post("/api/v1/connections", headers=AUTH, json={"provider": "sandbox"}).json()
        message = {"connector_id": connector["id"], "subject": "Next steps", "body": "Can you interview Tuesday?", "sender": "r@example.com"}
        first = self.client.post("/api/v1/monitored-events", headers=AUTH, json={**message, "external_id": "m1"}).json()
        self.assertEqual(first["payload"]["classified_by"]["source"], "rules")
        self.client.put("/api/v1/typesafe/inbox-suggestions", headers=AUTH, json={"enabled": True})
        self.jev.label = "interview"
        second = self.client.post("/api/v1/monitored-events", headers=AUTH, json={**message, "external_id": "m2"}).json()
        self.assertEqual((second["event_type"], second["confidence"]), ("interview", 0.9))
        self.assertEqual(second["payload"]["classified_by"]["source"], "jev")
        self.assertEqual(second["status"], "pending", "still a suggestion the student confirms")

    def test_the_setting_is_exported_with_the_account(self):
        self.client.put("/api/v1/typesafe/inbox-suggestions", headers=AUTH, json={"enabled": True})
        with closing(connect_product(self.platform_path)) as conn:
            from opportunity_app.operations import export_account

            exported = export_account(conn, user_id=LOCAL_USER_ID)
        self.assertEqual([row["key"] for row in exported["user_settings"]], ["jev_inbox_suggestions"])


if __name__ == "__main__":
    unittest.main()
