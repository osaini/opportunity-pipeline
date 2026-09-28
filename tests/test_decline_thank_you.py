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
    """FakeGmail that also says which thread each message is in, as Gmail reads a message back.

    ``thread_hooks`` run once, the next time a thread is read; ``thread_responses`` answer one thread's reads.
    """

    def __init__(self):
        super().__init__()
        self.threads = {}
        self.thread_hooks = {}
        self.thread_responses = {}

    def handler(self, request):
        path = request.url.path
        if request.method == "GET" and "/threads/" in path:
            thread_id = path.rsplit("/", 1)[1]
            hook = self.thread_hooks.pop(thread_id, None)
            if hook:
                hook()
            if thread_id in self.thread_responses:
                self.requests.append(request)
                return self.thread_responses[thread_id]()
        response = super().handler(request)
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

    def run_due(self, target_id, *, reviewer=passing_reviewer, later=timedelta()):
        send_at = datetime.fromisoformat(self.scheduled(target_id)["send_at"])
        return run_due_sends(self.conn, client_factory=self.factory, now=send_at + later, reviewer=reviewer)

    def due_at(self, target_id, local, *, state="scheduled", updated_at=None):
        """Set a thank-you's send to a fixed time, so a test does not depend on the time of day it runs."""
        when = local.astimezone(timezone.utc).isoformat(timespec="seconds")
        with self.conn:
            self.conn.execute("UPDATE outreach_scheduled_sends SET send_at=?, state=?, updated_at=COALESCE(?, updated_at) "
                              "WHERE target_id=? AND kind='thank_you'", (when, state, updated_at, target_id))

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
        self.due_at(target_id, datetime(2026, 9, 29, 10, 0, tzinfo=CHICAGO))
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
        self.assertIn("Thank-you to Acme Robotics stopped: Gmail could not be checked first", self.notices())

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
        # A notice can become a desktop pop-up, so it names the reason in fixed words, never the reviewer's.
        self.assertEqual(self.notices(), ["Thank-you to Acme Robotics held: the reviewer did not pass it"])
        self.assertEqual(len(self.gmail.sent), 1)
        payload = json.loads(prompts[0].split("JSON input:\n", 1)[1])
        self.assertEqual(payload["replies"][0]["whole_message"], DECLINE)
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
        self.assertIn("Thank-you to Acme Robotics stopped: it may have gone out, so check your Gmail Sent folder", self.notices())
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


# --- The review's findings: what must never be thanked, and every layer that stops a thank-you ---------------

INLINE = (
    "Unfortunately we're not in a position to take interns right now.\n\n"
    "On Mon, Sep 28, 2026 at 9:00 AM Test Student <student@example.com> wrote:\n"
    "> Would you have 15 minutes for a call next week?\n"
    "Happy to do a quick call though. How is Thursday at 2?"
)
QUOTED = (
    f"{DECLINE}\n\nOn Mon, Sep 28, 2026 at 9:00 AM Test Student <student@example.com>\nwrote:\n"
    "> Hi Dana,\n> Would you have 15 minutes for a call next week?\n"
)
CALL = "Happy to chat! When are you free for a call next week?"


class TextJev(FakeJev):
    """Jev reading a call where the words say one, and a decline otherwise."""

    def evaluate(self, *, state, questions):
        self.label = "call_scheduled" if "call" in json.dumps(state).lower() else "declined"
        return super().evaluate(state=state, questions=questions)


class QuotingTests(unittest.TestCase):
    def parse(self, raw):
        return BytesParser(policy=policy.default).parsebytes(raw)

    def test_an_answer_typed_between_the_quoted_lines_is_found(self):
        self.assertEqual(outreach_inbox.strip_quoted(INLINE), "Unfortunately we're not in a position to take interns right now.")
        self.assertEqual(outreach_inbox.written_between_quotes(INLINE), "Happy to do a quick call though. How is Thursday at 2?")
        self.assertEqual(outreach_inbox.written_between_quotes(QUOTED), "", "a quote with nothing typed in it, its attribution split over two lines")
        self.assertEqual(outreach_inbox.written_between_quotes(DECLINE), "")
        outlook = f"{DECLINE}\n\nFrom: Test Student <student@example.com>\nSent: Monday, September 28, 2026 9:00 AM\nWould you have time for a call?"
        self.assertEqual(outreach_inbox.written_between_quotes(outlook), "", "an Outlook quote is unmarked, so nothing in it can be told apart")

    def test_an_html_reply_keeps_its_quote_marked_and_what_follows_it(self):
        raw = (
            "From: Dana <dana@acme.example>\nTo: s@example.com\nSubject: Re: hi\nMIME-Version: 1.0\n"
            "Content-Type: text/html; charset=UTF-8\n\n<div>We&#39;re not hiring interns.</div><div class=\"gmail_quote\">"
            "<div>On Mon, Sep 28, 2026 Sam wrote:</div><blockquote>Would you have time<br>for a call?</blockquote>"
            "<div>Actually, Thursday works?</div></div>\n"
        ).encode()
        message = self.parse(raw)
        self.assertEqual(outreach_inbox.reply_text(message), "We're not hiring interns.")
        whole = outreach_inbox.full_reply_text(message)
        self.assertIn("> Would you have time", whole)
        self.assertIn("> for a call?", whole)
        self.assertEqual(outreach_inbox.written_between_quotes(whole), "Actually, Thursday works?")


class StrictRulesTests(unittest.TestCase):
    def test_a_no_with_anything_more_is_not_plain(self):
        for text in (
            "We're not currently hiring, but happy to set up a call to talk about next year.",
            "We are not hiring right now, but reach out next spring!",
            "No openings at the moment. Are you graduating in 2027?",
            "Not a fit for us right now, but you should talk to my colleague Sam.",
            "We're not hiring interns right now. Happy to do a quick call though.",
            "We won't be able to take anyone on, but we'll let you know if anything opens up.",
            "Not hiring now; when we're hiring again I'll forward your note.",
        ):
            with self.subTest(text=text):
                self.assertEqual(outreach.suggest_reply_status(text)["status"], "declined", "the rules alone read each as a no")
                self.assertTrue(outreach_thank_you.plain_decline_problem(text))

    def test_a_plain_no_is_plain(self):
        self.assertEqual(outreach_thank_you.plain_decline_problem(DECLINE), "")
        self.assertEqual(outreach_thank_you.plain_decline_problem("Thanks again for reaching out. We're not hiring interns this year."), "",
                         "\"thanks again\" promises nothing")
        self.assertIn("no plain no", outreach_thank_you.plain_decline_problem("Thanks for your note!"))


class ReviewFindingContentTests(unittest.TestCase):
    GOOD = ContentTests.GOOD

    def test_promises_and_plans_are_refused(self):
        for phrase in ("I will be sure to apply again next year.", "I'll keep an eye out.", "Maybe someday we will work together.",
                       "I hope to work with you.", "I will reapply.", "I'd love to try again."):
            with self.subTest(phrase=phrase):
                body = self.GOOD.replace("the best.", f"the best. {phrase}")
                self.assertTrue(validate(body, inputs()), f"{phrase!r} should be refused")
        self.assertEqual(validate(self.GOOD.replace("Thank you for letting me know", "Thank you again for letting me know"), inputs()), [],
                         "\"thank you again\" is only thanks")

    def test_a_name_with_letters_after_it_is_not_turned_around(self):
        target = {"company": "Acme Robotics, Inc.", "contact_email": "dana@acme.example", "contact_name": "Dana Lee"}
        for from_name, expected in (
            ("Dana Lee, PhD", "Dana Lee"), ("Sam Park, MBA", "Sam Park"), ("John Smith, Jr.", "John Smith"),
            ("Jane Doe, SHRM-CP", "Jane Doe"), ("Lee, Dana, PhD", "Dana Lee"), ("Park, Sam", "Sam Park"),
            ("Smith, John A.", "John A. Smith"),
        ):
            with self.subTest(from_name=from_name):
                name = recipient_name(from_name, "someone@acme.example", target)
                self.assertEqual(name, expected)
                self.assertEqual(greeting_line(target["company"], name, {"word": "Hi", "unnamed": "{company} team"}),
                                 f"Hi {expected.split()[0]},")
        self.assertEqual(recipient_name("The Acme Robotics Crew", "crew@acme.example", target), "", "the company's own name names nobody")

    def test_the_writer_selector_is_what_picks_the_thank_you_writer(self):
        called = []

        def factory(provider_id, model):
            called.append(provider_id)
            return FakeWriter(json.dumps({"body": self.GOOD}))

        with mock.patch.dict(os.environ, {**CLEAN_PROVIDERS, "PIPELINE_OUTREACH_PROVIDER": "anthropic",
                                          "PIPELINE_OUTREACH_THANK_YOU_PROVIDER": "legacy"}):
            self.assertEqual(write(inputs(), provider_factory=factory, provider=None), (template(inputs()), "template"))
            self.assertEqual(called, [], "legacy for thank-yous: no model is asked, whatever the drafts use")
        with mock.patch.dict(os.environ, {**CLEAN_PROVIDERS, "PIPELINE_OUTREACH_PROVIDER": "legacy",
                                          "PIPELINE_OUTREACH_THANK_YOU_PROVIDER": "anthropic"}):
            body, by = write(inputs(), provider_factory=factory, provider=None)
            self.assertEqual((body, by.split(":")[0], called), (self.GOOD, "anthropic", ["anthropic"]))


class ReviewFindingEligibilityTests(DeclineCase):
    def assert_not_planned(self, target_id, why):
        outcome = self.plan(target_id)
        self.assertFalse(outcome["planned"], outcome)
        self.assertIn(why, outcome["reason"])
        self.assertIsNone(thank_you_row(self.conn, target_id, USER))

    def test_a_call_offered_between_the_quoted_lines_is_left_for_the_student(self):
        target = self.sent_target()
        self.decline(INLINE)
        data = json.loads(self.conn.execute(
            "SELECT detail_json FROM outreach_events WHERE target_id=? AND event_type='reply_logged'", (target["id"],),
        ).fetchone()[0])
        self.assertIn("How is Thursday at 2?", data["full_text"], "the whole message is kept beside its top part")
        self.assertEqual(data["readings"]["rules"]["status"], "declined", "the top part alone reads as a no")
        self.assert_not_planned(target["id"], "between the lines")

    def test_a_decline_that_quotes_the_students_email_is_still_thanked_and_the_reviewer_reads_it_whole(self):
        target_id = self.planned(text=QUOTED)
        prompts = []

        def reviewer():
            return "fake-reviewer", lambda prompt: prompts.append(prompt) or PASS

        self.assertEqual([item["state"] for item in self.run_due(target_id, reviewer=reviewer)], ["sent"])
        payload = json.loads(prompts[0].split("JSON input:\n", 1)[1])
        self.assertIn("> Would you have 15 minutes for a call next week?", payload["replies"][0]["whole_message"])
        self.assertIn("between the quoted lines", prompts[0])
        self.assertIn("every reply, not only the latest", prompts[0])

    def test_the_reviewer_is_told_who_wrote_from_their_own_headers(self):
        target_id = self.planned(sender="\"Dana Lee, PhD\" <dana@acme.example>")
        prompts = []

        def reviewer():
            return "fake-reviewer", lambda prompt: prompts.append(prompt) or PASS

        self.run_due(target_id, reviewer=reviewer)
        payload = json.loads(prompts[0].split("JSON input:\n", 1)[1])
        # Two things to compare: what their message says, and what the thank-you was addressed from it.
        self.assertEqual(payload["latest_reply_from"], {"name": "Dana Lee, PhD", "email": "dana@acme.example", "reply_to": ""})
        self.assertEqual(payload["thank_you"]["to"], {"name": "Dana Lee", "email": "dana@acme.example"})
        self.assertTrue(payload["thank_you"]["body"].startswith("Hi Dana,\n"))

    def test_a_reply_kept_without_its_whole_text_is_not_thanked(self):
        target = self.sent_target()
        self.decline()
        row = self.conn.execute("SELECT id, detail_json FROM outreach_events WHERE target_id=? AND event_type='reply_logged'",
                                (target["id"],)).fetchone()
        data = json.loads(row["detail_json"])
        data.pop("full_text")
        with self.conn:
            self.conn.execute("UPDATE outreach_events SET detail_json=? WHERE id=?", (json.dumps(data), row["id"]))
        self.assert_not_planned(target["id"], "whole of their reply is not on record")

    def test_no_and_a_call_is_not_a_plain_decline_whatever_jev_says(self):
        target = self.sent_target()
        self.decline("We're not currently hiring, but happy to set up a call to talk about next year.")
        self.assert_not_planned(target["id"], "rules read strictly")

    def test_an_earlier_call_offer_from_the_cc_is_left_for_the_student(self):
        self.jev.__class__ = TextJev
        target = self.sent_target(contact_cc="cto@acme.example")
        self.arrive(CALL, message_id="call-1", sender="Chris CTO <cto@acme.example>", received=now_ms(timedelta(minutes=2)))
        self.arrive(DECLINE, message_id="decline-1", received=now_ms(timedelta(minutes=5)))
        self.check()
        self.assert_not_planned(target["id"], "an earlier reply reads as call scheduled")

    def test_a_suggestion_waiting_for_the_student_is_left_alone(self):
        target = self.sent_target()
        self.decline()
        with self.conn:
            self.conn.execute("UPDATE outreach_targets SET reply_suggestion_json=? WHERE id=?",
                              (json.dumps({"status": "call_scheduled", "reason": "a call"}), target["id"]))
        self.assert_not_planned(target["id"], "suggestion of call scheduled is waiting")

    def test_a_no_reply_address_or_a_reply_to_elsewhere_is_never_answered(self):
        target = self.sent_target()
        self.decline(sender="Acme Robotics <no-reply@acme.example>", message_id="nr-1")
        self.assert_not_planned(target["id"], "takes no replies")
        other = self.sent_target(company="Bovi", contact_email="greg@bovi.example", website="https://bovi.example")
        self.decline(sender="Bovi Jobs <jobs@bovi.example>", message_id="rt-1", thread="t-rt",
                     headers="Reply-To: Greg <greg@bovi.example>\n")
        self.assert_not_planned(other["id"], "asks for answers to go to another address")
        same = self.sent_target(company="Cato", contact_email="kim@cato.example", website="https://cato.example")
        self.decline(sender="Kim <kim@cato.example>", message_id="rt-2", thread="t-rt2", headers="Reply-To: kim@cato.example\n")
        self.assertTrue(self.plan(same["id"])["planned"], "a Reply-To that is the sender changes nothing")

    def test_a_decline_found_by_a_scheduled_sends_check_is_read_by_jev_too(self):
        acme = self.planned()
        bovi = self.sent_target(company="Bovi", contact_email="greg@bovi.example", website="https://bovi.example")
        self.arrive(message_id="m-bovi", sender="Greg <greg@bovi.example>", thread="t-bovi")
        send_at = datetime.fromisoformat(self.scheduled(acme)["send_at"])
        run_due_sends(self.conn, client_factory=self.factory, now=send_at + timedelta(minutes=1), reviewer=passing_reviewer,
                      decisions_for=lambda conn, user_id: self.jev)
        readings = json.loads(self.conn.execute(
            "SELECT detail_json FROM outreach_events WHERE target_id=? AND event_type='reply_logged'", (bovi["id"],),
        ).fetchone()[0])["readings"]
        self.assertEqual(readings["jev"]["label"], "declined")
        self.assertTrue(self.plan(bovi["id"])["planned"])

    def test_without_a_jev_client_the_reading_says_jev_was_not_asked(self):
        acme = self.planned()
        bovi = self.sent_target(company="Bovi", contact_email="greg@bovi.example", website="https://bovi.example")
        self.arrive(message_id="m-bovi", sender="Greg <greg@bovi.example>", thread="t-bovi")
        self.run_due(acme)
        readings = json.loads(self.conn.execute(
            "SELECT detail_json FROM outreach_events WHERE target_id=? AND event_type='reply_logged'", (bovi["id"],),
        ).fetchone()[0])["readings"]
        self.assertEqual((readings["jev"], readings["jev_fallback"]), (None, "Jev was not asked"))
        self.assert_not_planned(bovi["id"], "Jev as nothing (Jev was not asked)")

    def test_the_worker_hands_its_jev_client_to_the_scheduled_sends(self):
        from opportunity_app.outreach_automation import AutomationWorker

        decisions, hook = (lambda conn, user_id: self.jev), (lambda conn, target_id, user_id: None)
        worker = AutomationWorker(self.platform_path, fetcher_factory=lambda: None, gmail_client_factory=self.factory,
                                  decisions_for=decisions, on_reply=hook)
        with mock.patch("opportunity_app.outreach_schedule.run_due_sends", return_value=[]) as sends:
            worker.run_once()
        self.assertIs(sends.call_args.kwargs["decisions_for"], decisions)
        self.assertIs(sends.call_args.kwargs["on_reply"], hook)


class ReviewFindingGateTests(DeclineCase):
    def raw_reply(self, target_id, *, gmail_id="late-1", received=None, text="Actually, could we talk on Thursday?"):
        """A reply on record, logged now from another connection, without the capture's own side effects."""
        received = received or datetime.now(timezone.utc) + timedelta(minutes=30)
        data = {"source": "gmail", "gmail_id": gmail_id, "from": "dana@acme.example", "received_at": received.isoformat(timespec="seconds"),
                "thread_id": "t-decline", "message_id": f"<{gmail_id}@acme.example>", "full_text": text, "readings": {}}
        with closing(connect_product(self.platform_path)) as other, other:
            outreach._log(other, target_id, USER, "reply_logged", detail=text, data=data)

    def raw_send(self, target_id, thread):
        """An email of the student's, logged from another connection just after their reply arrived."""
        received = int(self.gmail.replies[thread][0]["internalDate"])
        at = datetime.fromtimestamp(received / 1000, tz=timezone.utc) + timedelta(minutes=1)
        with closing(connect_product(self.platform_path)) as other, other:
            other.execute(
                "INSERT INTO outreach_events(id, target_id, user_id, event_type, detail, detail_json, created_at) "
                "VALUES(?, ?, ?, 'gmail_sent', ?, '{}', ?)",
                (f"raw-send-{target_id}", target_id, USER, json.dumps({"kind": "follow_up"}), at.isoformat(timespec="microseconds")),
            )

    def assert_stopped(self, target_id, outcome, state, note, *, first_emails=1):
        self.assertEqual([item["state"] for item in outcome], [state])
        row = thank_you_row(self.conn, target_id, USER)
        self.assertEqual((row["state"], row["note"]), (state, note))
        self.assertEqual(len(self.gmail.sent), first_emails, "no thank-you went")

    def test_a_reply_captured_while_the_reviewer_runs_stops_it(self):
        target_id = self.planned()

        def reviewer():
            def run(prompt):
                # The InboxWatcher, on its own thread and connection, logs their next message meanwhile.
                self.arrive("Actually, can we do a call Thursday?", message_id="again-1", received=now_ms(timedelta(minutes=30)))
                outreach_inbox._LAST_CAPTURE.clear()
                with closing(connect_product(self.platform_path)) as other:
                    outreach_inbox.capture_replies(other, user_id=USER, client_factory=self.factory, decisions=self.jev)
                return PASS
            return "fake-reviewer", run

        self.assert_stopped(target_id, self.run_due(target_id, reviewer=reviewer), "cancelled", WROTE_AGAIN)

    def reads(self, ending):
        return sum(1 for request in self.gmail.requests if request.url.path.endswith(ending))

    def test_a_reply_or_a_send_logged_while_the_reviewer_runs_stops_it_on_the_records_alone(self):
        target_id = self.planned()
        self.assert_stopped(target_id, self.run_due(target_id, reviewer=lambda: ("fake", lambda prompt: self.raw_reply(target_id) or PASS)),
                            "cancelled", WROTE_AGAIN)
        self.assertEqual(self.reads("/threads/t-decline"), 0, "stopped by the records read again after the reviewer, before the thread")
        other = self.sent_target(company="Bovi", contact_email="greg@bovi.example", website="https://bovi.example")
        self.decline(message_id="m-bovi", sender="Greg <greg@bovi.example>", thread="t-bovi")
        self.assertTrue(self.plan(other["id"])["planned"])
        outcome = self.run_due(other["id"], reviewer=lambda: ("fake", lambda prompt: self.raw_send(other["id"], "t-bovi") or PASS))
        self.assert_stopped(other["id"], outcome, "cancelled", STUDENT_WROTE, first_emails=2)

    def test_a_reply_logged_after_the_thread_read_is_caught_at_the_hand_over(self):
        target_id = self.planned()
        self.gmail.thread_hooks["t-decline"] = lambda: self.raw_reply(target_id)
        with mock.patch("opportunity_app.outreach_schedule.send_thank_you", wraps=send_thank_you) as sender:
            self.assert_stopped(target_id, self.run_due(target_id), "cancelled", WROTE_AGAIN)
        self.assertEqual(self.scheduled(target_id)["state"], "cancelled")
        self.assertEqual(sender.call_count, 0, "stopped at the hand-over, before the send began")

    def test_a_reply_logged_just_before_the_send_is_caught_under_the_claim(self):
        target_id = self.planned()
        # The profile check comes after the hand-over and before the claim is taken for the one call that sends.
        self.gmail.hooks["profile"] = lambda: self.raw_reply(target_id)
        self.assert_stopped(target_id, self.run_due(target_id), "cancelled", WROTE_AGAIN)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM outreach_send_claims WHERE target_id=? AND kind='thank_you'", (target_id,)).fetchone(),
                          "the claim was rolled back with the check")

    def test_a_reply_logged_after_planning_stops_it_whenever_it_arrived(self):
        target_id = self.planned()
        decline_at = datetime.fromtimestamp(int(self.gmail.replies["t-decline"][0]["internalDate"]) / 1000, tz=timezone.utc)
        # Arrived before their decline (moved out of spam, say), but found only now.
        self.raw_reply(target_id, gmail_id="spam-1", received=decline_at - timedelta(minutes=3), text=CALL)
        self.assert_stopped(target_id, self.run_due(target_id), "cancelled", WROTE_AGAIN)

    def test_turning_jev_off_holds_a_waiting_thank_you(self):
        target_id = self.planned()
        automation.set_mode(self.conn, USER, "jev_inbox_suggestions", "off")
        self.assertEqual(automation.mode(self.conn, USER, "decline_thank_you"), "on", "the switch itself stays on")
        self.assert_stopped(target_id, self.run_due(target_id), "held", outreach_thank_you.JEV_OFF)
        self.assertEqual(self.notices(), ["Thank-you to Acme Robotics held: Jev inbox suggestions was turned off"])
        self.assertEqual(self.events(target_id, "thank_you_reviewed"), [], "held before the reviewer was asked")

    def test_turning_jev_off_while_the_reviewer_runs_holds_it_at_the_hand_over(self):
        target_id = self.planned()

        def reviewer():
            def run(prompt):
                with closing(connect_product(self.platform_path)) as other:
                    automation.set_mode(other, USER, "jev_inbox_suggestions", "off")
                return PASS
            return "fake-reviewer", run

        self.assert_stopped(target_id, self.run_due(target_id, reviewer=reviewer), "held", outreach_thank_you.JEV_OFF)

    def test_a_draft_the_student_started_in_the_thread_stops_it(self):
        target_id = self.planned()
        decline_at = int(self.gmail.replies["t-decline"][0]["internalDate"])
        self.gmail.replies["t-decline"].append({"id": "draft-1", "labelIds": ["DRAFT"], "internalDate": str(decline_at + 60_000)})
        self.assert_stopped(target_id, self.run_due(target_id), "cancelled", outreach_thank_you.DRAFT_STARTED)

    def test_a_message_only_gmails_thread_shows_stops_it(self):
        target_id = self.planned()
        decline_at = int(self.gmail.replies["t-decline"][0]["internalDate"])
        # From an address the app does not watch, so only the thread read sees it.
        self.gmail.replies["t-decline"].append({"id": "other-1", "labelIds": ["INBOX"], "internalDate": str(decline_at + 60_000)})
        self.assert_stopped(target_id, self.run_due(target_id), "cancelled", WROTE_AGAIN)

    def test_a_thread_read_that_fails_holds_it_for_a_retry(self):
        target_id = self.planned()
        # Not a 5xx: Gmail's reads take one as a slowdown (the next test). fresh_look reads other threads, so it passes.
        self.gmail.thread_responses["t-decline"] = lambda: httpx.Response(404)
        self.assertEqual([item["state"] for item in self.run_due(target_id)], ["retrying"])
        row = self.scheduled(target_id)
        self.assertEqual((row["state"], row["attempts"]), ("scheduled", 1))
        self.assertIn("Could not read their thread in Gmail first", row["error"])
        self.assertEqual(thank_you_row(self.conn, target_id, USER)["state"], "scheduled")
        self.assertEqual(len(self.gmail.sent), 1)

    def test_a_thread_read_gmail_throttles_waits_without_using_a_try(self):
        target_id = self.planned()
        self.gmail.thread_responses["t-decline"] = lambda: rate_limited()
        self.assertEqual([item["state"] for item in self.run_due(target_id)], ["retrying"])
        row = self.scheduled(target_id)
        self.assertEqual((row["state"], row["attempts"]), ("scheduled", 0), "Gmail's slowdown is not the thank-you's failure")
        self.assertIn("does not use up a try", row["error"])
        self.assertEqual(len(self.gmail.sent), 1)

    def test_a_paused_moved_on_bounced_or_deleted_company_gets_no_thank_you(self):
        for number, change in enumerate(("paused", "call_scheduled", "bounced", "deleted")):
            with self.subTest(change=change):
                target = self.sent_target(company=f"Dora {number}", contact_email=f"dana@dora{number}.example",
                                          website=f"https://dora{number}.example")
                self.decline(message_id=f"d-{number}", sender=f"Dana Lee <dana@dora{number}.example>", thread=f"t-d{number}")
                self.assertTrue(self.plan(target["id"])["planned"])
                sent = len(self.gmail.sent)
                if change == "deleted":
                    self.assertEqual(self.client.delete(f"/api/v1/outreach/{target['id']}", headers=AUTH).status_code, 204)
                    self.assertEqual(run_due_sends(self.conn, client_factory=self.factory, now=datetime.now(timezone.utc) + timedelta(days=7),
                                                   reviewer=passing_reviewer), [])
                    stop = outreach_thank_you.problem_now(self.conn, target["id"], USER, {"reply_gmail_id": f"d-{number}"})
                    self.assertEqual(stop[0], "cancelled")
                else:
                    if change == "bounced":
                        with self.conn:
                            self.conn.execute("UPDATE outreach_targets SET bounced_addresses_json=? WHERE id=?",
                                              (json.dumps([f"dana@dora{number}.example"]), target["id"]))
                    else:
                        with self.conn:
                            self.conn.execute("UPDATE outreach_targets SET status=? WHERE id=?", (change, target["id"]))
                    self.assertEqual([item["state"] for item in self.run_due(target["id"])], ["cancelled"])
                    self.assertEqual(thank_you_row(self.conn, target["id"], USER)["state"], "cancelled")
                self.assertEqual(len(self.gmail.sent), sent, "no thank-you went")


class ReviewFindingWindowTests(DeclineCase):
    def sends(self, target_id, local, reviewer=passing_reviewer):
        return [item["state"] for item in run_due_sends(self.conn, client_factory=self.factory, now=local, reviewer=reviewer)]

    def next_send(self, target_id):
        return datetime.fromisoformat(self.scheduled(target_id)["send_at"]).astimezone(CHICAGO)

    def assert_morning(self, target_id, day):
        when = self.next_send(target_id)
        self.assertEqual((when.date().isoformat(), when.hour), (day, 9))
        self.assertLess(when.minute, 40)
        self.assertEqual(thank_you_row(self.conn, target_id, USER)["state"], "scheduled")
        self.assertEqual(len(self.gmail.sent), 1, "nothing went in their evening, night or weekend")

    def test_a_friday_late_wake_waits_for_monday_morning(self):
        target_id = self.planned()
        self.due_at(target_id, datetime(2026, 10, 2, 16, 50, tzinfo=CHICAGO))
        self.assertEqual(self.sends(target_id, datetime(2026, 10, 2, 18, 40, tzinfo=CHICAGO)), ["moved"])
        self.assert_morning(target_id, "2026-10-05")
        self.assertEqual(self.events(target_id, "thank_you_reviewed"), [], "moved before any check ran, not only at the hand-over")
        self.assertEqual(self.scheduled(target_id)["error"], "Its time came outside 9 to 5 on a weekday in their time zone")
        self.assertEqual(thank_you_row(self.conn, target_id, USER)["label"], self.scheduled(target_id)["label"])

    def test_a_retry_that_crosses_five_waits_for_the_next_morning(self):
        target_id = self.planned()
        self.due_at(target_id, datetime(2026, 9, 29, 16, 55, tzinfo=CHICAGO))
        self.gmail.thread_status = 403
        self.assertEqual(self.sends(target_id, datetime(2026, 9, 29, 16, 56, tzinfo=CHICAGO)), ["retrying"])
        self.assertEqual((self.next_send(target_id).hour, self.next_send(target_id).minute), (17, 6))
        self.gmail.thread_status = None
        self.assertEqual(self.sends(target_id, datetime(2026, 9, 29, 17, 7, tzinfo=CHICAGO)), ["moved"])
        self.assert_morning(target_id, "2026-09-30")

    def test_stuck_recovery_before_nine_waits_for_nine(self):
        target_id = self.planned()
        self.due_at(target_id, datetime(2026, 10, 2, 16, 58, tzinfo=CHICAGO), state="sending", updated_at="2026-10-02T21:58:00+00:00")
        self.assertEqual(self.sends(target_id, datetime(2026, 10, 5, 7, 30, tzinfo=CHICAGO)), [])
        self.assertEqual((self.next_send(target_id).hour, self.next_send(target_id).minute), (7, 40))
        self.assertEqual(self.sends(target_id, datetime(2026, 10, 5, 7, 41, tzinfo=CHICAGO)), ["moved"])
        self.assert_morning(target_id, "2026-10-05")

    def test_resuming_after_five_waits_for_the_next_morning(self):
        target_id = self.planned()
        self.due_at(target_id, datetime(2026, 9, 29, 16, 40, tzinfo=CHICAGO))
        automation.set_paused(self.conn, USER, True)
        self.assertEqual(self.sends(target_id, datetime(2026, 9, 29, 16, 41, tzinfo=CHICAGO)), [])
        automation.set_paused(self.conn, USER, False)
        self.assertEqual(self.sends(target_id, datetime(2026, 9, 29, 18, 20, tzinfo=CHICAGO)), ["moved"])
        self.assert_morning(target_id, "2026-09-30")

    def test_a_review_that_runs_past_five_is_moved_at_the_hand_over(self):
        from opportunity_app import outreach_schedule

        target_id = self.planned()
        self.due_at(target_id, datetime(2026, 9, 29, 16, 55, tzinfo=CHICAGO))
        real = datetime.now(timezone.utc)
        late = {"by": timedelta()}

        def reviewer():
            def run(prompt):
                late["by"] = timedelta(minutes=20)  # the model took twenty minutes
                return PASS
            return "slow-reviewer", run

        with mock.patch.object(outreach_schedule, "_clock", side_effect=lambda: real + late["by"]):
            self.assertEqual(self.sends(target_id, datetime(2026, 9, 29, 16, 56, tzinfo=CHICAGO), reviewer=reviewer), ["moved"])
        self.assert_morning(target_id, "2026-09-30")
        self.assertEqual(self.events(target_id, "thank_you_reviewed"), ["Passed by slow-reviewer"], "the checks ran, and the hand-over moved it")

    def test_within_the_window_it_still_goes(self):
        target_id = self.planned()
        self.due_at(target_id, datetime(2026, 9, 29, 11, 32, tzinfo=CHICAGO))
        self.assertEqual(self.sends(target_id, datetime(2026, 9, 29, 11, 33, tzinfo=CHICAGO)), ["sent"])


class ReviewFindingCardTests(DeclineCase):
    def held(self, **decline):
        target_id = self.planned(**decline)
        holding = lambda: ("fake-reviewer", lambda prompt: '{"send": false, "problems": ["Unsure of the tone."]}')  # noqa: E731
        self.run_due(target_id, reviewer=holding)
        self.assertEqual(thank_you_row(self.conn, target_id, USER)["state"], "held")
        return target_id

    def send_anyway(self, target_id, **extra):
        row = thank_you_row(self.conn, target_id, USER)
        return self.client.post(f"/api/v1/outreach/{target_id}/thank-you/send", headers=AUTH,
                                json={"fingerprint": row["fingerprint"], **extra})

    def test_a_new_reply_closes_a_held_thank_you_so_it_cannot_be_sent_anyway(self):
        target_id = self.held()
        self.decline("Actually, we might have an opening in spring.", message_id="again-1")
        row = thank_you_row(self.conn, target_id, USER)
        self.assertEqual((row["state"], row["note"]), ("cancelled", WROTE_AGAIN))
        self.assertEqual(self.send_anyway(target_id).status_code, 409)
        self.assertEqual(len(self.gmail.sent), 1)

    def test_send_it_anyway_is_refused_after_the_student_wrote_to_them(self):
        target_id = self.held()
        received = int(self.gmail.replies["t-decline"][0]["internalDate"])
        self.log_after_the_reply(target_id, "gmail_sent", received=received, detail=json.dumps({"kind": "follow_up"}))
        response = self.send_anyway(target_id)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"], STUDENT_WROTE)
        row = thank_you_row(self.conn, target_id, USER)
        self.assertEqual((row["state"], row["note"]), ("cancelled", STUDENT_WROTE))
        self.assertEqual(len(self.gmail.sent), 1)

    def test_an_edit_gmail_did_not_confirm_keeps_a_claim_so_send_it_anyway_asks_first(self):
        target_id = self.planned()
        self.gmail.draft_status = 503
        response = self.client.post(f"/api/v1/outreach/{target_id}/thank-you/edit", headers=AUTH)
        self.assertEqual(response.status_code, 502, response.text)
        row = thank_you_row(self.conn, target_id, USER)
        self.assertEqual(row["state"], "held")
        self.assertIn("may be in your Gmail Drafts", row["note"])
        self.assertNotIn("Nothing was sent", row["note"])
        claim = self.conn.execute("SELECT state, action FROM outreach_send_claims WHERE target_id=? AND kind='thank_you'", (target_id,)).fetchone()
        self.assertEqual((claim["state"], claim["action"]), ("unconfirmed", "draft"))
        self.gmail.draft_status = 200
        first = self.send_anyway(target_id)
        self.assertEqual(first.status_code, 428, first.text)
        self.assertIn("Drafts", first.json()["detail"]["msg"])
        self.assertNotIn("I sent it", first.json()["detail"]["msg"])
        sent = self.send_anyway(target_id, sent_folder_check=first.json()["detail"]["check"])
        self.assertEqual(sent.status_code, 200, sent.text)
        self.assertEqual(len(self.gmail.sent), 2)

    def test_edit_is_refused_while_an_earlier_send_may_have_gone(self):
        target_id = self.planned()
        self.gmail.send_status = 503
        self.run_due(target_id)
        self.gmail.send_status = 200
        response = self.client.post(f"/api/v1/outreach/{target_id}/thank-you/edit", headers=AUTH)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.gmail.drafts, {}, "no copy in Drafts that could go a second time")
        self.assertEqual(thank_you_row(self.conn, target_id, USER)["state"], "failed", "back where it was")

    def test_a_send_it_anyway_the_app_stopped_during_is_recovered(self):
        target_id = self.held()
        with self.conn:
            self.conn.execute("UPDATE outreach_thank_yous SET state='sending', updated_at='2026-01-01T00:00:00+00:00' WHERE target_id=?",
                              (target_id,))
        run_due_sends(self.conn, client_factory=self.factory, reviewer=passing_reviewer)
        row = thank_you_row(self.conn, target_id, USER)
        self.assertEqual((row["state"], row["note"]), ("failed", outreach_thank_you.STUCK_SENDING))
        self.assertIn("Thank-you to Acme Robotics stopped: it may have gone out, so check your Gmail Sent folder", self.notices())
        self.assertEqual(self.target(target_id)["thank_you"]["state"], "failed", "the card offers Send it anyway and Dismiss again")

    def test_a_send_it_anyway_still_running_is_left_alone(self):
        target_id = self.held()
        with self.conn:
            self.conn.execute("UPDATE outreach_thank_yous SET state='sending', updated_at='2026-01-01T00:00:00+00:00' WHERE target_id=?",
                              (target_id,))
            self.conn.execute(
                "INSERT INTO outreach_send_claims(target_id, user_id, kind, token, state, action, instance, claimed_at) "
                "VALUES(?, ?, 'thank_you', 'live', 'sending', 'send', 'another-process', ?)", (target_id, USER, utc_now()),
            )
        run_due_sends(self.conn, client_factory=self.factory, reviewer=passing_reviewer)
        self.assertEqual(thank_you_row(self.conn, target_id, USER)["state"], "sending")

    def test_dismissing_an_unconfirmed_thank_you_clears_the_warning_and_keeps_the_doubt_on_the_card(self):
        target_id = self.planned()
        self.gmail.send_status = 503
        self.assertEqual([item["state"] for item in self.run_due(target_id)], ["failed"])
        self.assertEqual([item["kind"] for item in automation.unconfirmed(self.conn, USER)], [THANK_YOU_KIND])
        dismissed = self.client.delete(f"/api/v1/outreach/{target_id}/thank-you", headers=AUTH)
        self.assertTrue(dismissed.json()["cancelled"])
        self.assertEqual(automation.unconfirmed(self.conn, USER), [])
        card = self.target(target_id)["thank_you"]
        self.assertEqual(card["state"], "cancelled")
        self.assertIn("You dismissed it", card["note"])
        self.assertIn("check your Gmail Sent folder", card["note"])

    def test_undoing_the_decline_says_the_thank_you_is_still_scheduled(self):
        target_id = self.planned()
        status = next(row for row in automation.list_actions(self.conn, USER, feature="decline_thank_you")
                      if row["action_type"] == "outreach.status")
        undone = automation.undo(self.conn, status["id"], USER)
        self.assertIn("The thank-you to Dana is still scheduled; cancel it on the company's card", undone["undo_note"])
        self.assertEqual(self.scheduled(target_id)["state"], "scheduled")

    def test_the_card_says_a_waiting_one_will_be_held_when_its_switch_or_jev_is_off(self):
        target_id = self.planned()
        self.assertEqual(self.target(target_id)["thank_you"]["will_hold"], "")
        automation.set_mode(self.conn, USER, "decline_thank_you", "off")
        self.assertEqual(self.target(target_id)["thank_you"]["will_hold"], "Send a thank-you when someone declines is off")
        automation.set_mode(self.conn, USER, "decline_thank_you", "on")
        automation.set_mode(self.conn, USER, "jev_inbox_suggestions", "off")
        self.assertIn("needs Jev inbox suggestions on", self.target(target_id)["thank_you"]["will_hold"])

    def test_the_card_greets_them_by_the_name_the_email_uses(self):
        target = self.sent_target()
        self.decline(sender="Dr. Priya Shah <priya@acme.example>", message_id="dr-1")
        self.assertTrue(self.plan(target["id"])["planned"])
        card = self.target(target["id"])["thank_you"]
        self.assertEqual((card["to_name"], card["to_first_name"]), ("Dr. Priya Shah", "Priya"))
        self.assertTrue(card["body"].startswith("Hi Priya,\n"))

    def test_the_settings_name_the_reviewer_of_each_kind(self):
        from opportunity_app import agent_providers

        def catalog(*ready):
            names = {"openai": "OpenAI", "anthropic": "Anthropic", "claude-code": "Claude Code", "codex-cli": "Codex CLI"}
            return [{"id": key, "display_name": name, "model": f"{key}-model", "configured": key in ready, "setup_hint": ""}
                    for key, name in names.items()]

        settings = OutreachSettings(env_path=self.root / ".env", attachment_dir=self.root / "attachment", resume_storage=self.root / "resumes")
        with mock.patch.dict(os.environ, {"PIPELINE_OUTREACH_FOLLOW_UP_PROVIDER": "codex-cli", "PIPELINE_OUTREACH_THANK_YOU_PROVIDER": "claude-code"}), \
                mock.patch.object(agent_providers, "provider_catalog", return_value=catalog("claude-code", "codex-cli")):
            review = settings.view(self.conn, user_id=USER)["review_provider"]
        self.assertEqual((review["automatic"]["id"], review["automatic_thank_you"]["id"]), ("claude-code", "codex-cli"),
                         "each reviewer is from a different company than that kind's writer")
        with mock.patch.dict(os.environ, {}), mock.patch.object(agent_providers, "provider_catalog", return_value=catalog()):
            review = settings.view(self.conn, user_id=USER)["review_provider"]
        self.assertEqual(review["automatic_thank_you"]["problem"], "No model is set up on this computer to review thank-yous")


if __name__ == "__main__":
    unittest.main()
