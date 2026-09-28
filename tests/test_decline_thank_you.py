"""The thank-you after a plain decline (outreach_thank_you.py): when it goes, whether a company qualifies,
what it says, the checks just before it goes, the ledger, and the card's actions."""

import base64
import json
import os
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR, automation, outreach, outreach_delivery, outreach_inbox, outreach_thank_you
from opportunity_app.api import create_app
from opportunity_app.outreach import greeting_line
from opportunity_app.outreach_drafting import resolve_provider
from opportunity_app.outreach_gmail import THANK_YOU_KIND, send_thank_you, thank_you_row
from opportunity_app.outreach_schedule import run_due_sends
from opportunity_app.outreach_settings import OutreachSettings
from opportunity_app.outreach_thank_you import (
    MAX_WORDS,
    STUDENT_WROTE,
    WROTE_AGAIN,
    plan,
    plan_send_at,
    recipient_name,
    stable_delay,
    template,
    validate,
    write,
)
from opportunity_app.schema import connect_product, utc_now
from opportunity_app.typesafe_decisions import TypeSafeResponseError

from helpers_platform import build_and_migrate
from test_inbox_classifiers import FakeJev
from test_outreach_gmail import ACCOUNT, SCOPES, FakeGmail, forget_gmail_backoff, rate_limited
from test_outreach_inbox import mail, now_ms

AUTH = {"Authorization": "Bearer thanks-owner"}
USER = "local-user"
CHICAGO = ZoneInfo("America/Chicago")
NEW_YORK = ZoneInfo("America/New_York")
LOS_ANGELES = ZoneInfo("America/Los_Angeles")
DECLINE = (
    "Thanks for reaching out. Unfortunately we're not hiring interns right now, so we won't be able to take you on. "
    "Best of luck with your search."
)
PASS = '{"send": true, "problems": []}'
CLEAN_PROVIDERS = {
    "PIPELINE_OUTREACH_PROVIDER": "", "PIPELINE_OUTREACH_THANK_YOU_PROVIDER": "", "PIPELINE_OUTREACH_REVIEW_PROVIDER": "",
}


def passing_reviewer():
    return "fake-reviewer", lambda prompt: PASS


def inputs(**overrides):
    base = {
        "decline": DECLINE, "student_name": "Test Student", "greeting": "Hi Dana,", "recipient_name": "Dana Lee",
        "company": "Acme Robotics", "company_full": "Acme Robotics, Inc.",
    }
    return {**base, **overrides}


class FakeWriter:
    """A model that answers each thank-you request with the next of ``answers`` (an exception is raised)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    def complete_text(self, instructions, content):
        self.prompts.append(content)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


# --- When it goes -----------------------------------------------------------------------------


class TimingTests(unittest.TestCase):
    KEY = "target-1:decline-1"

    def at(self, year, month, day, hour, minute=0, zone=CHICAGO):
        return datetime(year, month, day, hour, minute, tzinfo=zone)

    def local(self, when, zone=CHICAGO):
        return when.astimezone(zone)

    def test_a_weekday_morning_decline_is_answered_the_same_day_after_a_human_delay(self):
        received = self.at(2026, 9, 29, 10)  # a Tuesday
        send_at = plan_send_at(received, CHICAGO, self.KEY, received + timedelta(minutes=1))
        delay = send_at - received
        self.assertEqual(delay, stable_delay(self.KEY))
        self.assertTrue(timedelta(minutes=40) <= delay <= timedelta(minutes=150))
        self.assertEqual(self.local(send_at).date(), received.date())

    def test_a_delay_that_crosses_five_goes_the_next_weekday_morning(self):
        received = self.at(2026, 9, 29, 16, 30)
        local = self.local(plan_send_at(received, CHICAGO, self.KEY, received + timedelta(minutes=1)))
        self.assertEqual((local.date().isoformat(), local.hour), ("2026-09-30", 9))
        self.assertLess(local.minute, 40)

    def test_five_oclock_exactly_and_the_evening_go_the_next_morning(self):
        for hour in (17, 21):
            with self.subTest(hour=hour):
                received = self.at(2026, 9, 29, hour)
                local = self.local(plan_send_at(received, CHICAGO, self.KEY, received + timedelta(minutes=1)))
                self.assertEqual((local.date().isoformat(), local.hour), ("2026-09-30", 9))

    def test_friday_evening_and_the_weekend_go_on_monday_morning(self):
        for received in (self.at(2026, 10, 2, 18), self.at(2026, 10, 3, 11), self.at(2026, 10, 4, 9)):
            with self.subTest(received=received.isoformat()):
                local = self.local(plan_send_at(received, CHICAGO, self.KEY, received + timedelta(minutes=1)))
                self.assertEqual((local.strftime("%A"), local.date().isoformat(), local.hour), ("Monday", "2026-10-05", 9))

    def test_an_early_morning_decline_waits_for_nine_plus_the_delay(self):
        received = self.at(2026, 9, 29, 7, 10)
        local = self.local(plan_send_at(received, CHICAGO, self.KEY, received + timedelta(minutes=1)))
        self.assertGreaterEqual((local.hour, local.minute), (9, 40))
        self.assertEqual(local, self.at(2026, 9, 29, 9) + stable_delay(self.KEY))

    def test_a_late_detection_goes_ten_minutes_after_it_was_seen_while_still_before_five(self):
        received = self.at(2026, 9, 29, 10)
        now = self.at(2026, 9, 29, 13)
        self.assertEqual(plan_send_at(received, CHICAGO, self.KEY, now), now + timedelta(minutes=10))
        too_late = self.local(plan_send_at(received, CHICAGO, self.KEY, self.at(2026, 9, 29, 16, 55)))
        self.assertEqual((too_late.date().isoformat(), too_late.hour), ("2026-09-30", 9), "ten minutes on is past five")
        next_day = self.local(plan_send_at(received, CHICAGO, self.KEY, self.at(2026, 9, 30, 11)))
        self.assertEqual((next_day.date().isoformat(), next_day.hour), ("2026-10-01", 9), "a later day is the next morning")

    def test_the_days_the_clocks_change(self):
        # Back to standard time on Sunday, November 1, 2026: Monday is on CST, and the delay is real minutes.
        monday = self.at(2026, 11, 2, 10)
        send_at = plan_send_at(monday, CHICAGO, self.KEY, monday + timedelta(minutes=1))
        self.assertEqual(send_at - monday, stable_delay(self.KEY))
        self.assertEqual(self.local(send_at).tzname(), "CST")
        friday = self.at(2026, 10, 30, 18)
        after = self.local(plan_send_at(friday, CHICAGO, self.KEY, friday + timedelta(minutes=1)))
        self.assertEqual((after.date().isoformat(), after.hour, after.tzname()), ("2026-11-02", 9, "CST"))
        # Forward on Sunday, March 8, 2026: a decline that Sunday goes Monday at nine, daylight time.
        sunday = self.at(2026, 3, 8, 11)
        spring = self.local(plan_send_at(sunday, CHICAGO, self.KEY, sunday + timedelta(minutes=1)))
        self.assertEqual((spring.date().isoformat(), spring.hour, spring.tzname()), ("2026-03-09", 9, "CDT"))

    def test_a_zone_ahead_of_the_students_is_read_in_their_time(self):
        # 1:30 PM for a student in Los Angeles is 4:30 PM in New York, where the delay runs past five.
        received = datetime(2026, 9, 29, 13, 30, tzinfo=LOS_ANGELES)
        local = self.local(plan_send_at(received, NEW_YORK, self.KEY, received + timedelta(minutes=1)), NEW_YORK)
        self.assertEqual((local.date().isoformat(), local.hour), ("2026-09-30", 9))
        morning = datetime(2026, 9, 29, 7, 0, tzinfo=LOS_ANGELES)  # 10:00 in New York
        same_day = plan_send_at(morning, NEW_YORK, self.KEY, morning + timedelta(minutes=1))
        self.assertEqual(same_day - morning, stable_delay(self.KEY), "10 AM their time, though 7 AM for the student")

    def test_the_delay_is_stable_per_company_and_reply_and_spread_between_them(self):
        self.assertEqual(stable_delay(self.KEY), stable_delay(self.KEY))
        delays = {stable_delay(f"target-{number}:decline-1") for number in range(40)}
        self.assertGreater(len(delays), 10)
        self.assertTrue(all(timedelta(minutes=40) <= delay <= timedelta(minutes=150) for delay in delays))
        self.assertIn(timedelta(minutes=40), {stable_delay(str(number)) for number in range(2_000)}, "40 is reachable")
        self.assertIn(timedelta(minutes=150), {stable_delay(str(number)) for number in range(2_000)}, "and so is 150")


# --- What it says ------------------------------------------------------------------------------


class ContentTests(unittest.TestCase):
    GOOD = "Hi Dana,\n\nThank you for letting me know, and for considering it. I wish you and the Acme Robotics team the best.\n\nBest,\nTest Student"

    def test_a_plain_thank_you_passes(self):
        self.assertEqual(validate(self.GOOD, inputs()), [])

    def test_the_checks_refuse_questions_asks_numbers_dashes_and_a_wrong_greeting(self):
        cases = {
            "question": self.GOOD.replace("the best.", "the best. Any advice for me?"),
            "ask": self.GOOD.replace("the best.", "the best. Let me know if anything opens up."),
            "call": self.GOOD.replace("the best.", "the best, and I'd still enjoy a quick call."),
            "keep in mind": self.GOOD.replace("the best.", "the best. Please keep me in mind."),
            "number": self.GOOD.replace("the best.", "the best in 2027."),
            "dash": self.GOOD.replace("letting me know,", "letting me know —"),
            "spaced hyphen": self.GOOD.replace("letting me know,", "letting me know -"),
            "attachment": self.GOOD.replace("the best.", "the best. I attached my resume anyway."),
            "greeting": self.GOOD.replace("Hi Dana,", "Hello Dana,"),
            "sign-off": self.GOOD.replace("Test Student", "Test"),
            "link": self.GOOD.replace("the best.", "the best. https://example.com"),
        }
        for name, body in cases.items():
            with self.subTest(name=name):
                self.assertTrue(validate(body, inputs()), f"{name} should be refused")
        long = self.GOOD.replace("the best.", "the best. " + "Thank you so much for all of it. " * 12)
        self.assertTrue(any(f"over {MAX_WORDS}" in problem for problem in validate(long, inputs())))

    def test_a_name_that_looks_like_an_ask_is_not_one(self):
        named = inputs(company="Connect Robotics", company_full="Connect Robotics", greeting="Hi Dana,")
        body = "Hi Dana,\n\nThank you for considering it. I wish you and the Connect Robotics team the best.\n\nBest,\nTest Student"
        self.assertEqual(validate(body, named), [])
        self.assertEqual(validate(self.GOOD.replace("the best.", "the best at 3M."), inputs(company="3M", company_full="3M")), [],
                         "a number in the company's own name is in the inputs")

    def test_the_template_passes_the_same_checks_with_the_greeting_and_the_sign_off(self):
        for case in (inputs(), inputs(greeting="Hello Acme Robotics team,", recipient_name=""), inputs(greeting="Dear Dana,")):
            with self.subTest(greeting=case["greeting"]):
                body = template(case)
                self.assertEqual(validate(body, case), [])
                lines = [line for line in body.split("\n") if line.strip()]
                self.assertEqual((lines[0], lines[-2], lines[-1]), (case["greeting"], "Best,", "Test Student"))
                self.assertIn("the Acme Robotics team", body)

    def test_the_model_writes_it_and_is_retried_once_then_the_template_stands_in(self):
        env = mock.patch.dict(os.environ, CLEAN_PROVIDERS)
        env.start()
        self.addCleanup(env.stop)
        good = json.dumps({"body": self.GOOD})
        written, by = write(inputs(), provider_factory=lambda *_: FakeWriter(good), provider="anthropic")
        self.assertEqual((written, by.split(":")[0]), (self.GOOD, "anthropic"))
        writer = FakeWriter(json.dumps({"body": self.GOOD.replace("the best.", "the best. Could we talk?")}), good)
        written, by = write(inputs(), provider_factory=lambda *_: writer, provider="anthropic")
        self.assertEqual(written, self.GOOD)
        self.assertIn("refused because", writer.prompts[1], "the retry says why")
        bad = json.dumps({"body": "Hey,\n\nThanks - really."})
        self.assertEqual(write(inputs(), provider_factory=lambda *_: FakeWriter(bad, bad), provider="anthropic"),
                         (template(inputs()), "template"))
        self.assertEqual(write(inputs(), provider_factory=lambda *_: FakeWriter(RuntimeError("model down")), provider="anthropic"),
                         (template(inputs()), "template"))
        self.assertEqual(write(inputs(), provider_factory=None, provider="anthropic"), (template(inputs()), "template"))
        sent = json.loads(writer.prompts[0])
        self.assertEqual(set(sent), {"decline", "student_name", "greeting", "recipient_name", "company"})

    def test_the_writer_has_its_own_selector_that_falls_back_to_the_draft_writer(self):
        with mock.patch.dict(os.environ, {**CLEAN_PROVIDERS, "PIPELINE_OUTREACH_PROVIDER": "anthropic"}):
            self.assertEqual(resolve_provider(None, purpose="thank_you")[0], "anthropic", "empty means the drafts' writer")
            os.environ["PIPELINE_OUTREACH_THANK_YOU_PROVIDER"] = "legacy"
            self.assertEqual(resolve_provider(None, purpose="thank_you"), ("legacy", ""))
            self.assertEqual(resolve_provider(None, purpose="initial")[0], "anthropic")
            self.assertEqual(write(inputs(), provider_factory=lambda *_: FakeWriter(), provider=None), (template(inputs()), "template"))
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, CLEAN_PROVIDERS):
            root = Path(root)
            _, path = build_and_migrate(root)
            settings = OutreachSettings(env_path=root / ".env", attachment_dir=root / "attachment", resume_storage=root / "resumes")
            with closing(connect_product(path)) as conn:
                view = settings.view(conn, user_id=USER)
                self.assertEqual(view["thank_you_provider"]["value"], "")
                self.assertIn("legacy", [option["id"] for option in view["thank_you_provider"]["options"]])
                saved = settings.update(conn, {"thank_you_provider": "legacy"}, user_id=USER)
                self.assertEqual(saved["thank_you_provider"]["value"], "legacy")
                self.assertEqual(os.environ["PIPELINE_OUTREACH_THANK_YOU_PROVIDER"], "legacy")
                self.assertIn("PIPELINE_OUTREACH_THANK_YOU_PROVIDER=legacy", (root / ".env").read_text(encoding="utf-8"))
                with self.assertRaises(ValueError):
                    settings.update(conn, {"thank_you_provider": "gpt-9"}, user_id=USER)

    def test_the_greeting_names_the_person_who_wrote_or_greets_a_shared_inbox(self):
        target = {"company": "Acme Robotics, Inc.", "contact_email": "dana@acme.example", "contact_name": "Dana Lee"}
        self.assertEqual(recipient_name("Sam Park", "sam@acme.example", target), "Sam Park")
        self.assertEqual(recipient_name("Park, Sam", "sam@acme.example", target), "Sam Park")
        self.assertEqual(recipient_name("", "dana@acme.example", target), "Dana Lee", "the contact, by the name on file")
        self.assertEqual(recipient_name("Acme Careers", "jobs@acme.example", target), "")
        self.assertEqual(recipient_name("Acme Robotics", "info@acme.example", target), "")
        self.assertEqual(recipient_name("", "someone@acme.example", target), "")
        style = {"word": "Hello", "unnamed": "{company} team"}
        self.assertEqual(greeting_line(target["company"], recipient_name("Sam Park", "sam@acme.example", target), style), "Hello Sam,")
        self.assertEqual(greeting_line(target["company"], "", style), "Hello Acme Robotics team,")


# --- The platform, Gmail, and Jev ---------------------------------------------------------------


class ThreadedGmail(FakeGmail):
    """FakeGmail that also says which thread each message is in, as Gmail reads a message back."""

    def __init__(self):
        super().__init__()
        self.threads = {}

    def handler(self, request):
        response = super().handler(request)
        path = request.url.path
        if request.method == "GET" and "/messages/" in path and request.url.params.get("format") == "raw" and response.status_code == 200:
            data = response.json()
            data["threadId"] = self.threads.get(data["id"], f"thread-of-{data['id']}")
            return httpx.Response(200, json=data)
        return response


class DeclineCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        _, self.platform_path = build_and_migrate(self.root)
        self.key = Fernet.generate_key().decode()
        self.env = mock.patch.dict("os.environ", {
            "GOOGLE_OAUTH_CLIENT_ID": "client-id", "GOOGLE_OAUTH_CLIENT_SECRET": "client-secret",
            "PIPELINE_CONNECTION_KEY": self.key, "PIPELINE_OUTREACH_ACCOUNT": ACCOUNT,
            "PIPELINE_OUTREACH_COMPOSE": "gmail", "PIPELINE_OUTREACH_ATTACHMENT": "", **CLEAN_PROVIDERS,
        })
        self.env.start()
        # The student's greeting comes from their own profile file; this one names none, so the defaults apply.
        profile = mock.patch.object(outreach, "PROFILE_PATH", self.root / "no-profile.json")
        profile.start()
        self.addCleanup(profile.stop)
        outreach._PROFILE_DATA_CACHE.update(key=None, data={})
        self.gmail = ThreadedGmail()
        self.factory = lambda: httpx.Client(transport=httpx.MockTransport(self.gmail.handler))
        self.jev = FakeJev("declined", 0.93)
        app = create_app(
            db_path=self.platform_path, access_token="thanks-owner", static_dir=STATIC_DIR,
            resume_storage=self.root / "resumes", capture_storage=self.root / "captures", interview_storage=self.root / "interviews",
            outreach_gmail_client_factory=self.factory, inbox_client_factory=lambda: self.jev,
        )
        self.client = TestClient(app)
        self.client.__enter__()
        outreach_delivery._LAST_LOOK.clear()
        outreach_delivery._READ_NOTICES.clear()
        outreach_inbox._LAST_CAPTURE.clear()
        outreach_thank_you._NOTED.clear()
        forget_gmail_backoff(self)
        self.conn = connect_product(self.platform_path)
        self.connect()
        automation.set_mode(self.conn, USER, "jev_inbox_suggestions", "on")
        automation.set_mode(self.conn, USER, "decline_thank_you", "on")

    def tearDown(self):
        self.conn.close()
        self.client.__exit__(None, None, None)
        self.env.stop()
        self.tempdir.cleanup()

    def connect(self, scopes=SCOPES):
        fernet = Fernet(self.key.encode())
        with self.conn:
            self.conn.execute(
                """INSERT INTO connector_accounts(id, user_id, provider, scopes_json, encrypted_access_token, encrypted_refresh_token, status, created_at, updated_at)
                   VALUES(?, ?, 'gmail_drafts', ?, ?, ?, 'connected', ?, ?)""",
                (f"connector-gmail_drafts-{USER}", USER, json.dumps(list(scopes)),
                 fernet.encrypt(b"valid-token").decode(), fernet.encrypt(b"refresh-token").decode(), utc_now(), utc_now()),
            )

    def sent_target(self, **overrides):
        created = self.client.post("/api/v1/outreach", headers=AUTH, json={
            "company": "Acme Robotics, Inc.", "contact_email": "dana@acme.example", "contact_name": "Dana Lee",
            "website": "https://acme.example", "location": "Austin, TX",
            "email_subject": "Robotics internship question", "email_body": "Hi Dana,\n\nShort note about Acme.\n\nTest Student",
            **overrides,
        }).json()
        approved = self.client.post(f"/api/v1/outreach/{created['id']}/approve", headers=AUTH, json={
            "kind": "initial", "fingerprint": created["draft_fingerprint"], "acknowledge_warnings": True,
        })
        self.assertEqual(approved.status_code, 200, approved.text)
        sent = self.client.post(f"/api/v1/outreach/{created['id']}/gmail-send", headers=AUTH, json={
            "kind": "initial", "fingerprint": approved.json()["draft_fingerprint"],
        })
        self.assertEqual(sent.status_code, 200, sent.text)
        return self.target(created["id"])

    def target(self, target_id):
        response = self.client.get(f"/api/v1/outreach/{target_id}", headers=AUTH)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def arrive(self, text=DECLINE, *, message_id="decline-1", sender="Dana Lee <dana@acme.example>",
               subject="Re: Robotics internship question", received=None, thread="t-decline", headers=""):
        received = received if received is not None else now_ms(timedelta(minutes=5))
        self.gmail.threads[message_id] = thread
        self.gmail.replies.setdefault(thread, []).append({"id": message_id, "labelIds": ["INBOX"], "internalDate": str(received)})
        raw = mail(text, sender=sender, subject=subject, headers=f"Message-ID: <{message_id}@acme.example>\n{headers}")
        self.gmail.raw[message_id] = (raw, received)
        self.gmail.inbox_replies.append(message_id)
        return received

    def check(self):
        outreach_inbox._LAST_CAPTURE.clear()
        response = self.client.post("/api/v1/outreach/inbox-check", headers=AUTH)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def decline(self, text=DECLINE, **kwargs):
        received = self.arrive(text, **kwargs)
        self.check()
        return received

    def plan(self, target_id, **kwargs):
        return plan(self.conn, target_id, user_id=USER, provider_factory=None, **kwargs)

    def scheduled(self, target_id):
        return self.conn.execute(
            "SELECT * FROM outreach_scheduled_sends WHERE target_id=? AND kind='thank_you'", (target_id,),
        ).fetchone()

    def planned(self, **decline):
        target = self.sent_target()
        self.decline(**decline)
        outcome = self.plan(target["id"])
        self.assertTrue(outcome["planned"], outcome)
        return target["id"]

    def run_due(self, target_id, *, reviewer=passing_reviewer, later=timedelta(minutes=1)):
        send_at = datetime.fromisoformat(self.scheduled(target_id)["send_at"])
        return run_due_sends(self.conn, client_factory=self.factory, now=send_at + later, reviewer=reviewer)

    def events(self, target_id, event_type):
        return [row["detail"] for row in self.conn.execute(
            "SELECT detail FROM outreach_events WHERE target_id=? AND event_type=? ORDER BY created_at", (target_id, event_type),
        ).fetchall()]

    def notices(self):
        return [notice["title"] for notice in automation.list_notices(self.conn, USER)]

    def log_after_the_reply(self, target_id, event_type, *, received, to_status=None, detail=""):
        """An event the app logged after the reply arrived (the fixture's reply arrives minutes ahead of the clock)."""
        at = datetime.fromtimestamp(received / 1000, tz=timezone.utc) + timedelta(minutes=1)
        with self.conn:
            self.conn.execute(
                "INSERT INTO outreach_events(id, target_id, user_id, event_type, from_status, to_status, detail, created_at) "
                "VALUES(?, ?, ?, ?, NULL, ?, ?, ?)",
                (f"event-{event_type}-{target_id}", target_id, USER, event_type, to_status, detail, at.isoformat(timespec="microseconds")),
            )


# --- Both readings are recorded --------------------------------------------------------------


class ReadingTests(DeclineCase):
    def test_the_reply_keeps_both_readings_and_where_it_came_from_with_one_jev_call(self):
        target = self.sent_target()
        received = self.decline()
        row = self.conn.execute(
            "SELECT detail, detail_json FROM outreach_events WHERE target_id=? AND event_type='reply_logged'", (target["id"],),
        ).fetchone()
        self.assertEqual(row["detail"], DECLINE, "the reply's text stays the event's text")
        data = json.loads(row["detail_json"])
        self.assertEqual(data["readings"]["rules"]["status"], "declined")
        self.assertEqual(data["readings"]["jev"], {"label": "declined", "confidence": 0.93, "model": "jev-1.13.0"})
        self.assertEqual((data["source"], data["gmail_id"], data["thread_id"], data["message_id"]),
                         ("gmail", "decline-1", "t-decline", "<decline-1@acme.example>"))
        self.assertEqual((data["from"], data["from_name"], data["subject"]), ("dana@acme.example", "Dana Lee", "Re: Robotics internship question"))
        self.assertEqual(data["received_at"], datetime.fromtimestamp(received / 1000, tz=timezone.utc).isoformat(timespec="seconds"))
        self.assertEqual(len(self.jev.calls), 1, "Jev is asked once for the suggestion and the reading")
        self.assertEqual(self.target(target["id"])["reply_suggestion"]["status"], "declined")

    def test_without_jev_the_reading_says_why(self):
        self.jev.error = TypeSafeResponseError("never asked")
        automation.set_mode(self.conn, USER, "jev_inbox_suggestions", "off")
        target = self.sent_target()
        self.decline()
        data = json.loads(self.conn.execute(
            "SELECT detail_json FROM outreach_events WHERE target_id=? AND event_type='reply_logged'", (target["id"],),
        ).fetchone()[0])
        self.assertIsNone(data["readings"]["jev"])
        self.assertEqual(data["readings"]["rules"]["status"], "declined")
        self.assertEqual(self.jev.calls, [])

    def test_a_pasted_reply_is_recorded_as_pasted(self):
        target = self.sent_target()
        response = self.client.post(f"/api/v1/outreach/{target['id']}/reply", headers=AUTH, json={"text": DECLINE})
        self.assertIn(response.status_code, (200, 201), response.text)
        data = json.loads(self.conn.execute(
            "SELECT detail_json FROM outreach_events WHERE target_id=? AND event_type='reply_logged'", (target["id"],),
        ).fetchone()[0])
        self.assertEqual(data["source"], "pasted")
        self.assertEqual(self.plan(target["id"])["planned"], False, "no thread to answer, so nothing goes")


# --- Whether a company qualifies ---------------------------------------------------------------


class EligibilityTests(DeclineCase):
    def assert_nothing(self, target_id, why=""):
        outcome = self.plan(target_id)
        self.assertFalse(outcome["planned"], outcome)
        if why:
            self.assertIn(why, outcome["reason"])
        self.assertIsNone(thank_you_row(self.conn, target_id, USER))
        self.assertIsNone(self.scheduled(target_id))
        self.assertEqual(self.events(target_id, "thank_you_scheduled"), [], "nothing is written for a company that does not qualify")
        return outcome

    def test_a_plain_decline_read_as_one_by_both_is_thanked(self):
        target = self.sent_target()
        self.decline()
        outcome = self.plan(target["id"])
        self.assertTrue(outcome["planned"], outcome)
        row = thank_you_row(self.conn, target["id"], USER)
        self.assertEqual((row["state"], row["to_email"], row["to_name"], row["subject"], row["generated_by"]),
                         ("scheduled", "dana@acme.example", "Dana Lee", "Re: Robotics internship question", "template"))
        self.assertTrue(row["body"].startswith("Hi Dana,\n"))
        self.assertTrue(row["body"].endswith("\nTest Student"))
        scheduled = self.scheduled(target["id"])
        self.assertEqual((scheduled["state"], scheduled["fingerprint"], scheduled["label"]), ("scheduled", row["fingerprint"], row["label"]))
        self.assertIn("(their time, from Austin, TX)", row["label"])
        self.assertEqual(self.target(target["id"])["status"], "declined")

    def test_rules_only_jev_only_or_a_disagreement_does_nothing(self):
        cases = (
            ("rules only", DECLINE, dict(error=TypeSafeResponseError("TypeSafe is down"))),
            ("Jev only", "We appreciate the note but are going to decline at this time.", dict(label="declined")),
            ("disagreement", DECLINE, dict(label="call_scheduled")),
        )
        for number, (name, text, jev) in enumerate(cases):
            with self.subTest(name=name):
                self.jev.label, self.jev.error = jev.get("label", "declined"), jev.get("error")
                target = self.sent_target(company=f"Acme {number}", contact_email=f"dana{number}@acme{number}.example",
                                          website=f"https://acme{number}.example")
                self.decline(text, message_id=f"m-{number}", sender=f"Dana <dana{number}@acme{number}.example>", thread=f"t-{number}")
                self.assert_nothing(target["id"], "not a plain decline to both readings")

    def test_jev_below_the_threshold_does_nothing(self):
        self.jev.confidence = 0.4
        target = self.sent_target()
        self.decline()
        outcome = self.assert_nothing(target["id"])
        self.assertIn("Jev was unsure", outcome["reason"])

    def test_jev_off_or_paused_does_nothing(self):
        target = self.sent_target()
        automation.set_paused(self.conn, USER, True)
        self.decline()
        automation.set_paused(self.conn, USER, False)
        self.assert_nothing(target["id"], "Automation is paused, so Jev was not asked")
        self.assertEqual(self.jev.calls, [], "nothing went to TypeSafe while paused")
        # Jev turned off after the switch: the switch can't act, and says so.
        other = self.sent_target(company="Bovi", contact_email="greg@bovi.example", website="https://bovi.example")
        self.decline(message_id="m-bovi", sender="Greg <greg@bovi.example>", thread="t-bovi")
        automation.set_mode(self.conn, USER, "jev_inbox_suggestions", "off")
        report = {}
        outreach_thank_you.run_for_user(self.conn, USER, report, provider_factory=None)
        self.assertEqual(report, {})
        self.assertIsNone(thank_you_row(self.conn, other["id"], USER))
        health = {item["component"]: item for item in automation.health_summary(self.conn, USER)["components"]}
        self.assertIn("needs Jev inbox suggestions on", health["outreach.thank_you"]["last_error"])
        settings = {item["key"]: item for item in automation.settings_payload(self.conn, USER)["features"]}
        self.assertIn("Jev inbox suggestions", settings["decline_thank_you"]["requirement"])
        # While paused, the worker plans nothing at all.
        automation.set_mode(self.conn, USER, "jev_inbox_suggestions", "on")
        automation.set_paused(self.conn, USER, True)
        outreach_thank_you.run_for_user(self.conn, USER, report, provider_factory=None)
        self.assertIsNone(thank_you_row(self.conn, other["id"], USER))

    def test_it_cannot_be_turned_on_without_jev(self):
        automation.set_mode(self.conn, USER, "decline_thank_you", "off")
        automation.set_mode(self.conn, USER, "jev_inbox_suggestions", "off")
        with self.assertRaises(automation.AutomationGateError) as refused:
            automation.set_mode(self.conn, USER, "decline_thank_you", "on")
        self.assertIn("Jev inbox suggestions", str(refused.exception))
        feature = automation.FEATURES["decline_thank_you"]
        self.assertEqual((feature.group, feature.risk, feature.modes), ("outreach", "external", automation.OFF_ON), "no shadow")

    def test_a_call_an_offer_a_later_or_a_plain_reply_does_nothing(self):
        for number, label in enumerate(("call_scheduled", "offer", "paused", "replied")):
            with self.subTest(label=label):
                self.jev.label = label
                target = self.sent_target(company=f"Beta {number}", contact_email=f"kim{number}@beta{number}.example",
                                          website=f"https://beta{number}.example")
                self.decline(message_id=f"b-{number}", sender=f"Kim <kim{number}@beta{number}.example>", thread=f"tb-{number}")
                self.assert_nothing(target["id"])

    def test_an_automatic_reply_does_nothing(self):
        target = self.sent_target()
        self.decline("I'm out of the office until Monday. We're not hiring interns right now.",
                     subject="Automatic reply: Robotics internship question", headers="Auto-Submitted: auto-replied\n")
        self.assert_nothing(target["id"])
        self.assertEqual(self.target(target["id"])["status"], "sent", "an automatic reply changes nothing")

    def test_a_bounced_contact_does_nothing(self):
        target = self.sent_target()
        self.decline()
        with self.conn:
            self.conn.execute("UPDATE outreach_targets SET bounced_addresses_json=? WHERE id=?", (json.dumps(["dana@acme.example"]), target["id"]))
        self.assert_nothing(target["id"], "bounced")

    def test_when_the_student_already_wrote_nothing_goes(self):
        target = self.sent_target()
        received = self.decline()
        self.log_after_the_reply(target["id"], "gmail_sent", received=received,
                                 detail=json.dumps({"kind": "follow_up", "to": "dana@acme.example"}))
        self.assert_nothing(target["id"], "something went to them after their reply")

    def test_it_happens_once_per_company(self):
        target_id = self.planned()
        self.assertFalse(self.plan(target_id)["planned"])
        outreach_thank_you.cancel(self.conn, target_id, user_id=USER)
        self.decline("Following up: still no roles, sorry. We won't be able to take anyone on.", message_id="decline-2")
        self.assertFalse(self.plan(target_id)["planned"], "a second decline is never thanked again")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM outreach_thank_yous").fetchone()[0], 1)

    def test_a_colleagues_decline_is_thanked_and_addressed_to_the_colleague(self):
        target = self.sent_target()
        self.decline(sender="Sam Park <sam@acme.example>", message_id="colleague-1")
        self.assertTrue(self.plan(target["id"])["planned"])
        row = thank_you_row(self.conn, target["id"], USER)
        self.assertEqual((row["to_email"], row["to_name"]), ("sam@acme.example", "Sam Park"))
        self.assertTrue(row["body"].startswith("Hi Sam,\n"))

    def test_a_decline_from_before_the_switch_was_turned_on_is_never_thanked(self):
        automation.set_mode(self.conn, USER, "decline_thank_you", "off")
        target = self.sent_target()
        received = self.decline()
        automation.set_mode(self.conn, USER, "decline_thank_you", "on")
        # Turned on a minute after the decline arrived (the fixture's decline arrives ahead of the clock).
        later = (datetime.fromtimestamp(received / 1000, tz=timezone.utc) + timedelta(minutes=1)).isoformat(timespec="microseconds")
        with self.conn:
            self.conn.execute("UPDATE user_settings SET value=? WHERE user_id=? AND key='decline_thank_you.on_since'", (later, USER))
        self.assert_nothing(target["id"], "before Send a thank-you when someone declines was turned on")

    def test_gmail_without_read_access_does_nothing(self):
        with self.conn:
            self.conn.execute("UPDATE connector_accounts SET scopes_json=?", (json.dumps(SCOPES[:1]),))
        target = self.sent_target()
        self.arrive()
        with self.conn:
            self.conn.execute("UPDATE connector_accounts SET scopes_json=?", (json.dumps(SCOPES),))
        self.check()
        with self.conn:
            self.conn.execute("UPDATE connector_accounts SET scopes_json=?", (json.dumps(SCOPES[:1]),))
        self.assert_nothing(target["id"], "read access")

    def test_the_worker_plans_it_and_records_its_health(self):
        target = self.sent_target()
        self.decline()
        report = {}
        outreach_thank_you.run_for_user(self.conn, USER, report, provider_factory=None)
        self.assertEqual([item["target_id"] for item in report["thank_yous"]], [target["id"]])
        health = {item["component"]: item for item in automation.health_summary(self.conn, USER)["components"]}
        self.assertTrue(health["outreach.thank_you"]["last_ok_at"])
        self.assertEqual(health["outreach.thank_you"]["detail"], {"planned": 1})


# --- The ledger --------------------------------------------------------------------------------


class LedgerTests(DeclineCase):
    def test_the_status_change_is_undoable_and_undoing_it_leaves_the_email_scheduled(self):
        target_id = self.planned()
        rows = {row["action_type"]: row for row in automation.list_actions(self.conn, USER, feature="decline_thank_you")}
        self.assertEqual(set(rows), {"outreach.thank_you", "outreach.status"})
        marker, status = rows["outreach.thank_you"], rows["outreach.status"]
        self.assertEqual((marker["status"], marker["undoable"], status["undoable"]), ("applied", False, True))
        self.assertRegex(marker["summary"], r"^Thank-you to Dana at Acme Robotics scheduled for \w{3} \d{1,2}:\d{2} [AP]M \(their time\)$")
        evidence = marker["evidence"]
        self.assertEqual(evidence["readings"]["rules"]["status"], "declined")
        self.assertEqual(evidence["readings"]["jev"]["label"], "declined")
        self.assertEqual(evidence["reply_gmail_id"], "decline-1")
        self.assertEqual(evidence["planned_for"], self.scheduled(target_id)["send_at"])
        self.assertNotIn("body", marker["after"]["thank_you"], "the words stay on the thank-you")
        undone = automation.undo(self.conn, status["id"], USER)
        self.assertEqual(undone["status"], "undone")
        self.assertEqual(self.target(target_id)["status"], "replied")
        self.assertEqual(self.scheduled(target_id)["state"], "scheduled", "the card's Cancel stops the email, not the undo")
        self.assertEqual(thank_you_row(self.conn, target_id, USER)["state"], "scheduled")
        with self.assertRaises(ValueError):
            automation.undo(self.conn, marker["id"], USER)
        # Still replied after the undo: the thank-you goes all the same.
        self.run_due(target_id)
        self.assertEqual(thank_you_row(self.conn, target_id, USER)["state"], "sent")

    def test_a_pause_during_the_writing_stops_the_scheduling(self):
        target = self.sent_target()
        self.decline()

        class PausingWriter:
            def complete_text(inner, instructions, content):
                with closing(connect_product(self.platform_path)) as other:
                    automation.set_paused(other, USER, True)
                return json.dumps({"body": template(inputs(company="Acme Robotics"))})

        outcome = plan(self.conn, target["id"], user_id=USER, provider_factory=lambda *_: PausingWriter(), provider="anthropic")
        self.assertEqual((outcome["planned"], outcome.get("paused")), (False, True))
        self.assertIsNone(thank_you_row(self.conn, target["id"], USER))
        self.assertEqual(automation.list_actions(self.conn, USER, feature="decline_thank_you"), [])
        self.assertEqual(self.target(target["id"])["status"], "replied")


# --- Just before it goes -----------------------------------------------------------------------


class GateTests(DeclineCase):
    def sent_thank_you(self):
        self.assertGreaterEqual(len(self.gmail.sent), 2, "the first email, then the thank-you")
        return self.gmail.sent[-1]

    def test_it_goes_once_in_their_thread_with_the_threaded_headers(self):
        target_id = self.planned()
        outcome = self.run_due(target_id)
        self.assertEqual([item["state"] for item in outcome], ["sent"])
        sent = self.sent_thank_you()
        self.assertEqual(sent["threadId"], "t-decline")
        message = BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(sent["raw"]))
        self.assertEqual((message["In-Reply-To"], message["References"]), ("<decline-1@acme.example>", "<decline-1@acme.example>"))
        self.assertEqual((str(message["To"]), str(message["Subject"]), str(message["From"])),
                         ("Dana Lee <dana@acme.example>", "Re: Robotics internship question", ACCOUNT))
        self.assertEqual([part.get_content_type() for part in message.walk()], ["multipart/alternative", "text/plain", "text/html"],
                         "plain text and HTML, no attachment")
        row = thank_you_row(self.conn, target_id, USER)
        self.assertEqual(row["state"], "sent")
        self.assertEqual(message.get_body(preferencelist=("plain",)).get_content().strip(), row["body"])
        detail = json.loads(self.events(target_id, "thank_you_sent")[0])
        self.assertEqual((detail["message_id"], detail["thread_id"]), ("sent-2", "thread-2"))
        self.assertEqual(self.target(target_id)["status"], "declined")
        self.assertEqual(self.notices(), [], "notices are only for held or failed thank-yous")
        self.assertEqual(self.run_due(target_id), [], "nothing left to send")
        # Once only: even handed over again, the claim and the record stop a second copy.
        with self.conn:
            self.conn.execute("UPDATE outreach_thank_yous SET state='transmitting' WHERE target_id=?", (target_id,))
        with self.assertRaises(ValueError):
            send_thank_you(self.conn, target_id, user_id=USER, fingerprint=row["fingerprint"], client_factory=self.factory)
        self.assertEqual(len(self.gmail.sent), 2)
        claim = self.conn.execute("SELECT state FROM outreach_send_claims WHERE target_id=? AND kind='thank_you'", (target_id,)).fetchone()
        self.assertEqual(claim["state"], "sent")

    def test_when_they_wrote_again_it_is_cancelled(self):
        target_id = self.planned()
        # Not checked yet: the fresh look just before sending finds it.
        self.arrive("Actually, could you send me your availability for a quick chat?", message_id="again-1",
                    received=now_ms(timedelta(minutes=30)))
        self.assertEqual([item["state"] for item in self.run_due(target_id)], ["cancelled"])
        row = thank_you_row(self.conn, target_id, USER)
        self.assertEqual((row["state"], row["note"]), ("cancelled", WROTE_AGAIN))
        self.assertEqual(len(self.gmail.sent), 1)

    def test_when_the_student_wrote_in_gmail_it_is_cancelled(self):
        target_id = self.planned()
        decline_at = int(self.gmail.replies["t-decline"][0]["internalDate"])
        self.gmail.replies["t-decline"].append({"id": "mine-1", "labelIds": ["SENT"], "internalDate": str(decline_at + 60_000)})
        self.assertEqual([item["state"] for item in self.run_due(target_id)], ["cancelled"])
        self.assertEqual(thank_you_row(self.conn, target_id, USER)["note"], STUDENT_WROTE)
        self.assertEqual(len(self.gmail.sent), 1)

    def test_when_the_student_sent_from_the_app_it_is_cancelled(self):
        target_id = self.planned()
        received = int(self.gmail.replies["t-decline"][0]["internalDate"])
        # "I sent it" after their reply, from the app's history.
        self.log_after_the_reply(target_id, "status", received=received, to_status="followed_up")
        self.assertEqual([item["state"] for item in self.run_due(target_id)], ["cancelled"])
        self.assertEqual(thank_you_row(self.conn, target_id, USER)["note"], STUDENT_WROTE)
        self.assertEqual(len(self.gmail.sent), 1)

    def test_a_throttle_waits_without_using_up_a_try(self):
        target_id = self.planned()
        self.gmail.read_response = lambda: rate_limited()
        self.assertEqual([item["state"] for item in self.run_due(target_id)], ["retrying"])
        row = self.scheduled(target_id)
        self.assertEqual((row["state"], row["attempts"]), ("scheduled", 0), "Gmail's slowdown is not the email's failure")
        self.assertIn("does not use up a try", row["error"])
        self.assertEqual(thank_you_row(self.conn, target_id, USER)["state"], "scheduled")
        self.assertEqual(len(self.gmail.sent), 1)

    def test_a_fresh_look_failure_holds_it_for_a_retry_then_fails_it(self):
        target_id = self.planned()
        self.gmail.thread_status = 403
        self.assertEqual([item["state"] for item in self.run_due(target_id)], ["retrying"])
        row = self.scheduled(target_id)
        self.assertEqual((row["state"], row["attempts"]), ("scheduled", 1))
        self.assertIn("Could not check Gmail", row["error"])
        self.assertEqual(thank_you_row(self.conn, target_id, USER)["state"], "scheduled")
        self.assertEqual(len(self.gmail.sent), 1)
        # A third failed try gives up: it is failed, with a notice.
        self.run_due(target_id)
        self.run_due(target_id)
        self.assertEqual(thank_you_row(self.conn, target_id, USER)["state"], "failed")
        self.assertTrue(any(title.startswith("Thank-you to Acme Robotics was not sent") for title in self.notices()))

    def test_the_reviewer_holds_it_with_a_reason(self):
        target_id = self.planned()
        prompts = []

        def reviewer():
            def run(prompt):
                prompts.append(prompt)
                return '{"send": false, "problems": ["Their reply mentions a call next spring."]}'
            return "fake-reviewer", run

        self.assertEqual([item["state"] for item in self.run_due(target_id, reviewer=reviewer)], ["held"])
        row = thank_you_row(self.conn, target_id, USER)
        self.assertEqual(row["state"], "held")
        self.assertIn("Their reply mentions a call next spring", row["note"])
        self.assertEqual(self.notices(), ["Thank-you to Acme Robotics held: The reviewer held it: Their reply mentions a call next spring"])
        self.assertEqual(len(self.gmail.sent), 1)
        payload = json.loads(prompts[0].split("JSON input:\n", 1)[1])
        self.assertEqual(payload["replies"][0]["text"], DECLINE)
        self.assertEqual(payload["thank_you"]["to"], {"name": "Dana Lee", "email": "dana@acme.example"})
        self.assertEqual(payload["first_email"]["subject"], "Robotics internship question")
        card = self.target(target_id)["thank_you"]
        self.assertEqual((card["state"], card["send_state"]), ("held", None))

    def test_a_reviewer_error_holds_it(self):
        target_id = self.planned()

        def broken():
            return "fake-reviewer", lambda prompt: (_ for _ in ()).throw(RuntimeError("exited 1"))

        self.assertEqual([item["state"] for item in self.run_due(target_id, reviewer=broken)], ["held"])
        self.assertIn("The reviewer could not run", thank_you_row(self.conn, target_id, USER)["note"])

        def missing():
            raise ValueError("No model is set up on this computer to review follow-ups")

        other = self.sent_target(company="Bovi", contact_email="greg@bovi.example", website="https://bovi.example")
        self.decline(message_id="m-bovi", sender="Greg <greg@bovi.example>", thread="t-bovi")
        self.assertTrue(self.plan(other["id"])["planned"])
        self.assertEqual([item["state"] for item in self.run_due(other["id"], reviewer=missing)], ["held"])
        self.assertEqual(len(self.gmail.sent), 2, "two first emails, and no thank-you")

    def test_the_pause_is_checked_at_the_hand_over(self):
        target_id = self.planned()

        def pausing():
            def run(prompt):
                with closing(connect_product(self.platform_path)) as other:
                    automation.set_paused(other, USER, True)
                return PASS
            return "fake-reviewer", run

        self.assertEqual([item["state"] for item in self.run_due(target_id, reviewer=pausing)], ["paused"])
        self.assertEqual(self.scheduled(target_id)["state"], "scheduled")
        self.assertEqual(thank_you_row(self.conn, target_id, USER)["state"], "scheduled")
        self.assertEqual(len(self.gmail.sent), 1)
        self.assertEqual(self.target(target_id)["thank_you"]["send_state"], "scheduled", "the card reads it as waiting")

    def test_turning_the_switch_off_holds_it_at_the_hand_over(self):
        target_id = self.planned()
        automation.set_mode(self.conn, USER, "decline_thank_you", "off")
        self.assertEqual([item["state"] for item in self.run_due(target_id)], ["held"])
        self.assertEqual(thank_you_row(self.conn, target_id, USER)["state"], "held")
        self.assertEqual(len(self.gmail.sent), 1)

    def test_an_unconfirmed_send_fails_and_the_student_looks_before_sending_it_anyway(self):
        target_id = self.planned()
        self.gmail.send_status = 503
        self.assertEqual([item["state"] for item in self.run_due(target_id)], ["failed"])
        row = thank_you_row(self.conn, target_id, USER)
        self.assertEqual(row["state"], "failed")
        self.assertIn("Check your Gmail Sent folder", row["note"])
        self.assertEqual([item["kind"] for item in automation.unconfirmed(self.conn, USER)], [THANK_YOU_KIND])
        self.assertTrue(any(title.startswith("Thank-you to Acme Robotics was not sent") for title in self.notices()))
        self.gmail.send_status = 200
        forget_gmail_backoff(self)
        first = self.client.post(f"/api/v1/outreach/{target_id}/thank-you/send", headers=AUTH, json={"fingerprint": row["fingerprint"]})
        self.assertEqual(first.status_code, 428, first.text)
        check = first.json()["detail"]["check"]
        self.assertEqual(thank_you_row(self.conn, target_id, USER)["state"], "failed", "back where it was")
        sent = self.client.post(f"/api/v1/outreach/{target_id}/thank-you/send", headers=AUTH,
                                json={"fingerprint": row["fingerprint"], "sent_folder_check": check})
        self.assertEqual(sent.status_code, 200, sent.text)
        self.assertEqual(sent.json()["target"]["thank_you"]["state"], "sent")
        self.assertTrue(sent.json()["target"]["thank_you"]["thread_url"].endswith("#all/t-decline"))


# --- The card's actions ------------------------------------------------------------------------


class CardTests(DeclineCase):
    def test_the_card_shows_it_waiting_and_cancel_stops_it(self):
        target_id = self.planned()
        card = self.target(target_id)["thank_you"]
        self.assertEqual((card["state"], card["send_state"], card["to_email"]), ("scheduled", "scheduled", "dana@acme.example"))
        self.assertEqual(card["label"], self.scheduled(target_id)["label"])
        self.assertTrue(card["body"].startswith("Hi Dana,"))
        listed = self.client.get("/api/v1/outreach", headers=AUTH).json()
        item = next(target for target in listed["items"] if target["id"] == target_id)
        self.assertEqual(item["thank_you"]["fingerprint"], card["fingerprint"])
        response = self.client.delete(f"/api/v1/outreach/{target_id}/thank-you", headers=AUTH)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["cancelled"])
        self.assertEqual(response.json()["thank_you"]["state"], "cancelled")
        self.assertEqual(self.scheduled(target_id)["state"], "cancelled")
        self.assertEqual(self.run_due(target_id), [])
        self.assertFalse(self.client.delete(f"/api/v1/outreach/{target_id}/thank-you", headers=AUTH).json()["cancelled"])
        self.assertEqual(len(self.gmail.sent), 1)

    def test_edit_moves_it_to_gmail_drafts_in_the_thread_and_stops_the_automatic_send(self):
        target_id = self.planned()
        response = self.client.post(f"/api/v1/outreach/{target_id}/thank-you/edit", headers=AUTH)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(self.gmail.drafts), 1)
        draft = next(iter(self.gmail.drafts.values()))
        self.assertEqual(draft["message"]["threadId"], "t-decline")
        card = response.json()["target"]["thank_you"]
        self.assertEqual(card["state"], "cancelled")
        self.assertIn("drafts?compose=", card["draft_url"])
        self.assertIn("Gmail Drafts", card["note"])
        self.assertEqual(self.scheduled(target_id)["state"], "cancelled")
        self.assertEqual(self.run_due(target_id), [])
        self.assertEqual(len(self.gmail.sent), 1, "nothing was sent")

    def test_edit_that_gmail_refuses_leaves_it_held_so_nothing_is_lost(self):
        target_id = self.planned()
        self.gmail.draft_status = 400
        response = self.client.post(f"/api/v1/outreach/{target_id}/thank-you/edit", headers=AUTH)
        self.assertEqual(response.status_code, 502, response.text)
        row = thank_you_row(self.conn, target_id, USER)
        self.assertEqual(row["state"], "held")
        self.assertIn("Nothing was sent", row["note"])

    def test_send_it_anyway_sends_a_held_one_and_dismiss_drops_it(self):
        target_id = self.planned()
        holding = lambda: ("fake-reviewer", lambda prompt: '{"send": false, "problems": ["Unsure of the tone."]}')  # noqa: E731
        self.run_due(target_id, reviewer=holding)
        row = thank_you_row(self.conn, target_id, USER)
        self.assertEqual(row["state"], "held")
        stale = self.client.post(f"/api/v1/outreach/{target_id}/thank-you/send", headers=AUTH, json={"fingerprint": "0" * 64})
        self.assertEqual(stale.status_code, 409, stale.text)
        # A pause never stops the student's own send.
        automation.set_paused(self.conn, USER, True)
        sent = self.client.post(f"/api/v1/outreach/{target_id}/thank-you/send", headers=AUTH, json={"fingerprint": row["fingerprint"]})
        self.assertEqual(sent.status_code, 200, sent.text)
        self.assertEqual(self.sent_count(), 2)
        self.assertEqual(thank_you_row(self.conn, target_id, USER)["state"], "sent")
        again = self.client.post(f"/api/v1/outreach/{target_id}/thank-you/send", headers=AUTH, json={"fingerprint": row["fingerprint"]})
        self.assertEqual(again.status_code, 409, again.text)
        # Dismiss, on another company's held thank-you.
        automation.set_paused(self.conn, USER, False)
        other = self.sent_target(company="Bovi", contact_email="greg@bovi.example", website="https://bovi.example")
        self.decline(message_id="m-bovi", sender="Greg <greg@bovi.example>", thread="t-bovi")
        self.assertTrue(self.plan(other["id"])["planned"])
        self.run_due(other["id"], reviewer=holding)
        dismissed = self.client.delete(f"/api/v1/outreach/{other['id']}/thank-you", headers=AUTH)
        self.assertEqual(dismissed.json()["thank_you"]["state"], "cancelled")

    def sent_count(self):
        return len(self.gmail.sent)

    def test_the_actions_answer_404_for_a_company_that_is_not_there(self):
        for method, path in (("delete", "/thank-you"), ("post", "/thank-you/edit")):
            response = getattr(self.client, method)(f"/api/v1/outreach/nope{path}", headers=AUTH)
            self.assertEqual(response.status_code, 404, (path, response.text))
        response = self.client.post("/api/v1/outreach/nope/thank-you/send", headers=AUTH, json={"fingerprint": "a" * 64})
        self.assertEqual(response.status_code, 404, response.text)

    def test_the_account_export_holds_it(self):
        target_id = self.planned()
        from opportunity_app.operations import export_account

        exported = export_account(self.conn, user_id=USER)
        self.assertEqual([row["target_id"] for row in exported["outreach_thank_yous"]], [target_id])


if __name__ == "__main__":
    unittest.main()
