"""The checks just before the app sends on its own: a fresh Gmail look, and a second model reading each follow-up."""

import json
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

from opportunity_app import STATIC_DIR, gmail_client, outreach_delivery, outreach_inbox
from opportunity_app.api import create_app
from opportunity_app.outreach import log_event
from opportunity_app.outreach_automation import update_settings
from opportunity_app.outreach_review import review_runner
from opportunity_app.outreach_schedule import MAX_ATTEMPTS, run_due_sends
from opportunity_app.database import connect_product
from opportunity_app.timestamps import utc_now

from helpers_platform import build_and_migrate
from helpers_gmail import ACCOUNT, PDF, SCOPES, FakeGmail, failure_notice, forget_gmail_backoff, rate_limited

AUTH = {"Authorization": "Bearer review-owner"}
USER = "local-user"


def mail(body, *, sender="Greg Lee <greg@bovi.example>", subject="Re: Robotics internship question", headers=""):
    return (
        f"From: {sender}\nTo: {ACCOUNT}\nSubject: {subject}\n{headers}"
        f"MIME-Version: 1.0\nContent-Type: text/plain; charset=UTF-8\n\n{body}\n"
    ).encode()


class Reviewer:
    """A scripted reviewer that records every prompt it is given."""

    def __init__(self, answer=None, error=None):
        self.answer, self.error, self.prompts = answer, error, []

    def __call__(self):
        return "fake-reviewer", self.run

    def run(self, prompt):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return self.answer if isinstance(self.answer, str) else json.dumps(self.answer)


def never():
    raise AssertionError("the reviewer is not asked while its switch is off")


class SendGateTests(unittest.TestCase):
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
            db_path=self.platform_path, access_token="review-owner", static_dir=STATIC_DIR,
            resume_storage=root / "resumes", capture_storage=root / "captures", interview_storage=root / "interviews",
            outreach_gmail_client_factory=self.factory,
        )
        self.client = TestClient(app)
        self.client.__enter__()
        self.conn = connect_product(self.platform_path)
        outreach_delivery._LAST_LOOK.clear()
        outreach_delivery._READ_NOTICES.clear()
        outreach_inbox._LAST_CAPTURE.clear()
        # A 429 below leaves a rate limit in memory, which must not follow into the next test.
        forget_gmail_backoff(self)
        update_settings(self.conn, {"scheduled_sending": True}, user_id=USER)
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

    def post(self, path, body):
        response = self.client.post(path, headers=AUTH, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def target(self, target):
        return self.client.get(f"/api/v1/outreach/{target['id']}", headers=AUTH).json()

    def approved_first_email(self):
        created = self.client.post("/api/v1/outreach", headers=AUTH, json={
            "company": "Bovi", "contact_name": "Greg Lee", "contact_email": "greg@bovi.example", "location": "Austin, TX",
            "email_subject": "Robotics internship question", "email_body": "Hi Greg,\n\nShort note about Bovi.\n\nSam",
        }).json()
        return self.post(f"/api/v1/outreach/{created['id']}/approve", {
            "kind": "initial", "fingerprint": created["draft_fingerprint"], "acknowledge_warnings": True,
        })

    def scheduled_follow_up(self):
        first = self.approved_first_email()
        self.post(f"/api/v1/outreach/{first['id']}/gmail-send", {"kind": "initial", "fingerprint": first["draft_fingerprint"]})
        drafted = self.client.patch(f"/api/v1/outreach/{first['id']}", headers=AUTH, json={
            "follow_up_subject": "Re: Robotics internship question",
            "follow_up_body": "Hi Greg,\n\nFollowing up on my note below. Happy to talk if the timing works.\n\nSam",
        }).json()
        approved = self.post(f"/api/v1/outreach/{drafted['id']}/approve", {
            "kind": "follow_up", "fingerprint": drafted["follow_up_fingerprint"], "acknowledge_warnings": True,
        })
        self.post(f"/api/v1/outreach/{approved['id']}/schedule", {"kind": "follow_up", "fingerprint": approved["follow_up_fingerprint"]})
        outreach_delivery._LAST_LOOK.clear()
        outreach_inbox._LAST_CAPTURE.clear()
        return self.target(approved)

    def due(self, target, kind="follow_up", reviewer=never):
        send_at = datetime.fromisoformat(self.target(target)["scheduled"][kind]["send_at"])
        return run_due_sends(self.conn, client_factory=self.factory, now=send_at + timedelta(minutes=1), reviewer=reviewer)

    def review_on(self):
        update_settings(self.conn, {"follow_up_review": True}, user_id=USER)

    def careers_writes(self, gmail_id="careers-1"):
        """The company's shared inbox writes back: not one person, so only a possible reply (outreach_inbox.py)."""
        self.gmail.raw[gmail_id] = (
            mail("Could you send over your availability?", sender="Bovi Careers <careers@bovi.example>", subject="Next steps"),
            int(datetime.now(timezone.utc).timestamp() * 1000) + 60_000,
        )
        self.gmail.inbox_replies.append(gmail_id)

    def waiting(self, conn, target_id, gmail_id="careers-9", candidates=(), reason="shared_address"):
        """A possible reply on record, as outreach_inbox keeps one, for ``target_id`` and any ``candidates``."""
        stamp = utc_now()
        conn.execute(
            "INSERT INTO outreach_inbox_messages(user_id, gmail_id, target_id, kind, sender, received_at, recorded_at, via, rules, "
            "reason, subject, text, candidates_json) VALUES(?, ?, ?, 'possible', 'careers@bovi.example', ?, ?, 'domain', ?, ?, "
            "'Next steps', 'Could you send over your availability?', ?)",
            (USER, gmail_id, target_id, stamp, stamp, outreach_inbox.RULES, reason, json.dumps(list(candidates))),
        )

    def decide(self, target_id, gmail_id, decision):
        return self.post(f"/api/v1/outreach/{target_id}/possible-replies/{gmail_id}", {"decision": decision})

    def schedule_row(self, target, kind="follow_up"):
        return dict(self.conn.execute(
            "SELECT state, send_at, label, error, attempts FROM outreach_scheduled_sends WHERE target_id=? AND kind=?",
            (target["id"], kind),
        ).fetchone())

    def event_details(self, target, event_type):
        return [event["detail"] for event in self.target(target)["events"] if event["event_type"] == event_type]

    def other_company(self):
        """A second company the student wrote to, which an ambiguous email may also be from."""
        created = self.client.post("/api/v1/outreach", headers=AUTH, json={
            "company": "Kelo", "contact_name": "Ana Ruiz", "contact_email": "ana@kelo.example", "location": "Denver, CO",
            "email_subject": "Sensor internship question", "email_body": "Hi Ana,\n\nShort note about Kelo.\n\nSam",
        })
        self.assertEqual(created.status_code, 201, created.text)
        return created.json()

    def passes_while(self, change):
        """A reviewer that passes the follow-up, while ``change`` lands from another connection as it reads.

        The review can take minutes, and the background inbox watcher writes on
        its own connection meanwhile.
        """
        reviewer = Reviewer({"send": True, "away_until": None, "problems": []})
        answer = reviewer.run

        def run(prompt):
            with closing(connect_product(self.platform_path)) as other:
                with other:
                    change(other)
            return answer(prompt)

        reviewer.run = run
        return reviewer

    def assert_held_for_a_possible_reply(self, target, before):
        """Held for the student, not failed or used up: still scheduled, on the recipient's next weekday morning."""
        held = self.schedule_row(target)
        self.assertEqual(held["state"], "scheduled")
        self.assertEqual(held["attempts"], before["attempts"], "a hold is not a try")
        self.assertTrue(held["error"].startswith("Held:"), held["error"])
        self.assertIn("Bovi may have replied", held["error"])
        zone = ZoneInfo("America/Chicago")
        was = datetime.fromisoformat(before["send_at"]).astimezone(zone)
        now = datetime.fromisoformat(held["send_at"]).astimezone(zone)
        morning = was.date() + timedelta(days=1)
        while morning.weekday() >= 5:
            morning += timedelta(days=1)
        self.assertEqual((now.date(), now.hour, now.minute), (morning, 9, was.minute), "the next weekday morning where they are")
        self.assertIn("their time, from Austin, TX", held["label"])
        self.assertTrue(self.event_details(target, "follow_up_held"), "the hold is in the history")
        self.assertEqual(self.event_details(target, "scheduled_send_failed"), [])
        self.assertEqual(self.event_details(target, "send_cancelled"), [])
        self.assertEqual(len(self.gmail.sent), 1, "only the first email ever went")

    # --- The fresh Gmail look --------------------------------------------------------

    def test_gmail_is_read_again_just_before_an_automatic_follow_up(self):
        target = self.scheduled_follow_up()
        # The last background look was a moment ago, so only a forced look reads Gmail now.
        from opportunity_app.outreach_delivery import check_deliveries
        check_deliveries(self.conn, user_id=USER, client_factory=self.factory)
        first_send = len(self.gmail.requests)
        self.assertEqual([item["state"] for item in self.due(target)], ["sent"])
        paths = [r.url.path for r in self.gmail.requests[first_send:]]
        send_at = next(i for i, path in enumerate(paths) if path.endswith("/messages/send"))
        self.assertTrue(any("/threads/" in path for path in paths[:send_at]), "the sent thread was read again for a bounce")
        self.assertTrue(any(path.endswith("/messages") for path in paths[:send_at]), "the inbox was searched for a reply")

    def test_a_reply_found_just_before_stops_the_follow_up(self):
        target = self.scheduled_follow_up()
        self.gmail.raw["reply-1"] = (mail("Thanks! Let's talk Tuesday."), int(datetime.now(timezone.utc).timestamp() * 1000) + 60_000)
        self.gmail.inbox_replies.append("reply-1")
        self.assertEqual([item["state"] for item in self.due(target)], ["cancelled"])
        self.assertEqual(len(self.gmail.sent), 1, "only the first email ever went")
        after = self.target(target)
        self.assertEqual(after["status"], "replied")
        self.assertIn("They replied", [e["detail"] for e in after["events"] if e["event_type"] == "send_cancelled"][0])

    def test_a_bounce_found_just_before_stops_the_follow_up(self):
        target = self.scheduled_follow_up()
        self.gmail.replies["thread-1"] = [failure_notice()]
        self.assertEqual([item["state"] for item in self.due(target)], ["cancelled"])
        self.assertEqual(len(self.gmail.sent), 1)
        self.assertTrue(self.target(target)["contact_bounced"])

    def test_when_gmail_cannot_be_read_the_email_waits(self):
        target = self.scheduled_follow_up()
        self.gmail.thread_status = 403
        self.assertEqual([item["state"] for item in self.due(target)], ["retrying"])
        self.due(target)
        self.assertEqual([item["state"] for item in self.due(target)], ["failed"])
        stopped = self.target(target)["scheduled"]["follow_up"]
        self.assertIn("Could not check Gmail", stopped["error"])
        self.assertIn("Nothing was sent", stopped["error"])
        self.assertEqual(len(self.gmail.sent), 1)

    def test_a_gmail_error_on_any_read_holds_the_follow_up(self):
        for status in (500, 429):
            with self.subTest(status=status):
                target = self.scheduled_follow_up() if status == 500 else target
                if status == 429:
                    with self.conn:
                        self.conn.execute("UPDATE outreach_scheduled_sends SET state='scheduled', attempts=0, error=''")
                self.gmail.thread_status = status
                self.assertEqual([item["state"] for item in self.due(target)], ["retrying"])
                self.assertIn("Could not check Gmail", self.target(target)["scheduled"]["follow_up"]["error"])
                self.assertEqual(len(self.gmail.sent), 1, "sending still works, but nothing goes unchecked")

    def test_a_gmail_slowdown_during_the_fresh_look_retries_rather_than_fails(self):
        target = self.scheduled_follow_up()
        self.gmail.read_response = rate_limited
        self.assertEqual([item["state"] for item in self.due(target)], ["retrying"])
        held = self.target(target)["scheduled"]["follow_up"]
        self.assertIn("Gmail asked the app to slow down", held["error"])
        self.assertEqual(len(self.gmail.sent), 1)
        # While Gmail's wait lasts, the next try asks Gmail nothing and waits again.
        asked = len(self.gmail.requests)
        self.assertEqual([item["state"] for item in self.due(target)], ["retrying"])
        self.assertEqual(len(self.gmail.requests), asked)

    def test_a_slowdown_that_escapes_a_check_still_only_holds_the_email(self):
        from opportunity_app.outreach_review import fresh_look

        target = self.scheduled_follow_up()
        with mock.patch("opportunity_app.outreach_review.check_deliveries",
                        side_effect=gmail_client.GmailThrottled("Gmail asked the app to slow down")):
            look = fresh_look(self.conn, target["id"], user_id=USER, client_factory=self.factory)
        self.assertEqual(look, {"ok": False, "reason": "Gmail asked the app to slow down"})

    def test_a_failed_bounce_read_is_not_taken_as_no_bounce(self):
        from opportunity_app.outreach_delivery import check_deliveries

        target = self.scheduled_follow_up()
        self.gmail.thread_status = 400
        result = check_deliveries(self.conn, user_id=USER, client_factory=self.factory, force_target=target["id"])
        self.assertEqual(result["state"], "unreachable")
        # A Gmail server error is a passing fault: the check waits, as for a rate limit, and still says it did not read.
        self.gmail.thread_status = 503
        result = check_deliveries(self.conn, user_id=USER, client_factory=self.factory, force_target=target["id"])
        self.assertEqual(result["state"], "throttled")

    def test_a_bounce_from_days_ago_still_stops_the_follow_up(self):
        target = self.scheduled_follow_up()
        # The first email went a week ago, and its bounce was never recorded (the app was off).
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="microseconds")
        with self.conn:
            self.conn.execute("UPDATE outreach_events SET created_at=? WHERE event_type='gmail_sent'", (week_ago,))
        self.gmail.replies["thread-1"] = [failure_notice()]
        self.assertEqual([item["state"] for item in self.due(target)], ["cancelled"])
        self.assertEqual(len(self.gmail.sent), 1)
        self.assertTrue(self.target(target)["contact_bounced"])

    def test_scheduling_needs_gmail_read_access(self):
        first = self.approved_first_email()
        with self.conn:
            self.conn.execute("UPDATE connector_accounts SET scopes_json=?", (json.dumps(SCOPES[:1]),))
        refused = self.client.post(f"/api/v1/outreach/{first['id']}/schedule", headers=AUTH, json={
            "kind": "initial", "fingerprint": first["draft_fingerprint"],
        })
        self.assertEqual(refused.status_code, 422)
        self.assertIn("Reconnect Gmail", refused.json()["detail"])

    # --- The second model ---------------------------------------------------------------

    def test_with_the_switch_off_the_reviewer_is_not_asked(self):
        target = self.scheduled_follow_up()
        self.assertEqual([item["state"] for item in self.due(target, reviewer=never)], ["sent"])
        self.assertEqual(len(self.gmail.sent), 2)

    def test_a_clean_pass_sends_it_and_is_recorded(self):
        self.review_on()
        target = self.scheduled_follow_up()
        reviewer = Reviewer({"send": True, "away_until": None, "problems": []})
        self.assertEqual([item["state"] for item in self.due(target, reviewer=reviewer)], ["sent"])
        self.assertEqual(len(self.gmail.sent), 2)
        prompt = reviewer.prompts[0]
        self.assertIn("Short note about Bovi.", prompt, "it reads the first email")
        self.assertIn("Following up on my note below.", prompt, "and the follow-up")
        self.assertIn("Passed by fake-reviewer", [e["detail"] for e in self.target(target)["events"] if e["event_type"] == "follow_up_reviewed"])

    def test_the_reviewer_sees_an_out_of_office_and_holds_until_they_are_back(self):
        self.review_on()
        target = self.scheduled_follow_up()
        self.gmail.raw["ooo-1"] = (mail("I'm on leave until October 20 with limited email.", subject="Automatic reply: Robotics internship question",
                                        headers="Auto-Submitted: auto-replied\n"), int(datetime.now(timezone.utc).timestamp() * 1000) + 60_000)
        self.gmail.inbox_replies.append("ooo-1")
        send_at = datetime.fromisoformat(self.target(target)["scheduled"]["follow_up"]["send_at"])
        back = (send_at + timedelta(days=12)).date()
        reviewer = Reviewer({"send": False, "away_until": back.isoformat(), "problems": ["The contact is on leave"]})
        self.assertEqual([item["state"] for item in self.due(target, reviewer=reviewer)], ["held"])
        self.assertIn("on leave until October 20", reviewer.prompts[0], "the automatic reply is in the thread it reads")
        self.assertEqual(len(self.gmail.sent), 1)
        held = self.target(target)["scheduled"]["follow_up"]
        self.assertEqual(held["state"], "scheduled")
        local = datetime.fromisoformat(held["send_at"]).astimezone(ZoneInfo("America/Chicago"))
        self.assertGreaterEqual(local.date(), back)
        self.assertEqual(local.hour, 9)

    def test_problems_stop_it_with_the_reasons_on_the_card(self):
        self.review_on()
        target = self.scheduled_follow_up()
        reviewer = Reviewer({"send": False, "away_until": None, "problems": ["It mentions a demo the first email never offered"]})
        self.assertEqual([item["state"] for item in self.due(target, reviewer=reviewer)], ["failed"])
        stopped = self.target(target)["scheduled"]["follow_up"]
        self.assertIn("It mentions a demo the first email never offered", stopped["error"])
        self.assertEqual(len(self.gmail.sent), 1)

    def test_every_unclear_answer_is_a_hold_not_a_pass(self):
        self.review_on()
        for answer, why in (
            ("I think it's fine!", "no JSON"),
            ({"send": "yes", "problems": []}, "send is not a boolean"),
            ({"send": True, "problems": ["Greeting uses the wrong name"]}, "send with problems"),
            ({"send": True, "away_until": "next week", "problems": []}, "a return date that is not a date"),
            ({"send": False, "problems": []}, "no send and no reason"),
            ('The format is {"send": true, "away_until": null, "problems": []}. My answer: {"send": false, "problems": ["x"]}',
             "two objects: the example echoed, then the answer"),
            ('[{"send": true, "away_until": null, "problems": []}]', "a list around the answer"),
        ):
            with self.subTest(why=why):
                target = self.scheduled_follow_up() if why == "no JSON" else target
                if why != "no JSON":
                    with self.conn:
                        self.conn.execute("UPDATE outreach_scheduled_sends SET state='scheduled', error='', attempts=0 WHERE kind='follow_up'")
                self.assertEqual([item["state"] for item in self.due(target, reviewer=Reviewer(answer))], ["failed"])
                self.assertEqual(len(self.gmail.sent), 1)

    def test_a_reviewer_that_cannot_run_holds_the_follow_up(self):
        self.review_on()
        target = self.scheduled_follow_up()
        reviewer = Reviewer(error=RuntimeError("codex exited 1: not signed in"))
        self.assertEqual([item["state"] for item in self.due(target, reviewer=reviewer)], ["failed"])
        self.assertIn("not signed in", self.target(target)["scheduled"]["follow_up"]["error"])
        self.assertEqual(len(self.gmail.sent), 1)

    def test_any_reviewer_failure_holds_it_and_the_run_goes_on(self):
        self.review_on()
        target = self.scheduled_follow_up()

        def broken_factory():
            raise FileNotFoundError("codex is not installed")

        for why, reviewer in (("not installed", broken_factory), ("crashes", Reviewer(error=TypeError("bad"))), ("says nothing", Reviewer(answer=None))):
            with self.subTest(why=why):
                with self.conn:
                    self.conn.execute("UPDATE outreach_scheduled_sends SET state='scheduled', error='', attempts=0 WHERE kind='follow_up'")
                if isinstance(reviewer, Reviewer) and reviewer.answer is None and reviewer.error is None:
                    reviewer.run = lambda prompt: None
                self.assertEqual([item["state"] for item in self.due(target, reviewer=reviewer)], ["failed"])
                stopped = self.target(target)["scheduled"]["follow_up"]
                self.assertEqual(stopped["state"], "failed", "never left stuck in sending")
                self.assertEqual(len(self.gmail.sent), 1)

    def test_the_reviewer_is_the_students_pick_or_another_company_than_the_writer(self):
        # A fixed catalog, so the answer does not depend on which CLIs this machine has.
        from opportunity_app import agent_providers

        both = [
            {"id": provider, "display_name": provider, "model": "m", "configured": provider in {"claude-code", "codex-cli"}, "setup_hint": ""}
            for provider in ("openai", "anthropic", "claude-code", "codex-cli")
        ]
        writer = {"PIPELINE_OUTREACH_PROVIDER": "claude-code", "PIPELINE_OUTREACH_FOLLOW_UP_PROVIDER": ""}
        with mock.patch.object(agent_providers, "provider_catalog", return_value=both):
            with mock.patch.dict("os.environ", {**writer, "PIPELINE_OUTREACH_REVIEW_PROVIDER": ""}):
                self.assertEqual(review_runner()[0], "codex-cli")
            with mock.patch.dict("os.environ", {**writer, "PIPELINE_OUTREACH_REVIEW_PROVIDER": "claude-code"}):
                self.assertEqual(review_runner()[0], "claude-code")
            with mock.patch.dict("os.environ", {**writer, "PIPELINE_OUTREACH_REVIEW_PROVIDER": "gpt"}):
                with self.assertRaises(ValueError):
                    review_runner()

    # --- An email from them that may be a reply (outreach_inbox.py) ---------------------

    def test_a_possible_reply_found_just_before_holds_the_follow_up_for_the_student(self):
        target = self.scheduled_follow_up()
        before = self.schedule_row(target)
        # careers@ answers in a new email: the fresh look finds it, and it may be their reply.
        self.careers_writes()
        self.assertEqual([item["state"] for item in self.due(target)], ["held"])
        after = self.target(target)
        self.assertEqual((after["status"], after["reply_count"], after["possible_reply_count"]), ("sent", 0, 1))
        self.assertEqual([item["gmail_id"] for item in after["possible_replies"]], ["careers-1"])
        self.assert_held_for_a_possible_reply(target, before)
        self.assertIn("may be a reply", self.event_details(target, "follow_up_held")[0])

    def test_a_held_follow_up_goes_once_the_student_says_it_is_not_a_reply(self):
        target = self.scheduled_follow_up()
        self.careers_writes()
        self.assertEqual([item["state"] for item in self.due(target)], ["held"])
        self.assertEqual(self.decide(target["id"], "careers-1", "not_reply")["possible_reply_count"], 0)
        self.assertEqual([item["state"] for item in self.due(target)], ["sent"])
        self.assertEqual(len(self.gmail.sent), 2)
        self.assertEqual(self.target(target)["status"], "followed_up")

    def test_a_held_follow_up_is_cancelled_once_the_student_says_it_is_a_reply(self):
        target = self.scheduled_follow_up()
        self.careers_writes()
        self.assertEqual([item["state"] for item in self.due(target)], ["held"])
        decided = self.decide(target["id"], "careers-1", "reply")
        self.assertEqual((decided["status"], decided["reply_count"]), ("replied", 1))
        self.assertEqual([item["state"] for item in self.due(target)], ["cancelled"])
        self.assertEqual(len(self.gmail.sent), 1)
        self.assertIn("They replied", self.event_details(target, "send_cancelled")[0])

    def test_a_follow_up_stays_held_every_morning_until_the_student_says(self):
        target = self.scheduled_follow_up()
        with self.conn:
            self.waiting(self.conn, target["id"])
        attempts = self.schedule_row(target)["attempts"]
        for run in range(MAX_ATTEMPTS + 1):
            with self.subTest(run=run):
                was = self.schedule_row(target)["send_at"]
                self.assertEqual([item["state"] for item in self.due(target)], ["held"], "never failed for waiting on the student")
                held = self.schedule_row(target)
                self.assertEqual((held["state"], held["attempts"]), ("scheduled", attempts))
                self.assertGreater(held["send_at"], was, "looked at again the next morning")
        self.assertEqual(len(self.event_details(target, "follow_up_held")), MAX_ATTEMPTS + 1)
        self.assertEqual(len(self.gmail.sent), 1)

    def test_the_reviewer_is_not_asked_while_a_possible_reply_waits(self):
        self.review_on()
        target = self.scheduled_follow_up()
        with self.conn:
            self.waiting(self.conn, target["id"])
        before = self.schedule_row(target)
        # never() raises if asked, which would fail the follow-up instead of holding it.
        self.assertEqual([item["state"] for item in self.due(target, reviewer=never)], ["held"])
        self.assert_held_for_a_possible_reply(target, before)
        self.assertEqual(self.event_details(target, "follow_up_reviewed"), [])

    def test_an_email_that_may_be_from_this_company_or_another_holds_it_until_the_other_claims_it(self):
        kelo = self.other_company()
        target = self.scheduled_follow_up()
        # Filed under Kelo, but Bovi could have sent it as well.
        with self.conn:
            self.waiting(self.conn, kelo["id"], gmail_id="either-1", candidates=[target["id"]], reason="ambiguous")
        before = self.schedule_row(target)
        self.assertEqual(self.target(target)["possible_reply_count"], 1, "a candidacy counts for every company it names")
        self.assertEqual([item["state"] for item in self.due(target)], ["held"])
        self.assert_held_for_a_possible_reply(target, before)
        # The student says it is Kelo's reply: Bovi has still not answered, so its follow-up goes.
        self.decide(kelo["id"], "either-1", "reply")
        self.assertEqual((self.target(target)["reply_count"], self.target(target)["possible_reply_count"]), (0, 0))
        self.assertEqual([item["state"] for item in self.due(target)], ["sent"])
        self.assertEqual(len(self.gmail.sent), 2)

    def test_an_email_filed_under_another_company_that_the_student_says_is_this_ones_reply_cancels_it(self):
        kelo = self.other_company()
        target = self.scheduled_follow_up()
        with self.conn:
            self.waiting(self.conn, kelo["id"], gmail_id="either-2", candidates=[target["id"]], reason="ambiguous")
        self.assertEqual([item["state"] for item in self.due(target)], ["held"])
        decided = self.decide(target["id"], "either-2", "reply")
        self.assertEqual((decided["status"], decided["reply_count"]), ("replied", 1))
        self.assertEqual([item["state"] for item in self.due(target)], ["cancelled"])
        self.assertEqual(len(self.gmail.sent), 1)

    def test_a_possible_reply_found_while_the_reviewer_reads_holds_the_follow_up(self):
        self.review_on()
        target = self.scheduled_follow_up()
        before = self.schedule_row(target)
        reviewer = self.passes_while(lambda other: self.waiting(other, target["id"]))
        self.assertEqual([item["state"] for item in self.due(target, reviewer=reviewer)], ["held"])
        self.assertEqual(len(reviewer.prompts), 1, "the reviewer read it and passed it")
        self.assertIn("Passed by fake-reviewer", self.event_details(target, "follow_up_reviewed"))
        self.assert_held_for_a_possible_reply(target, before)

    def test_a_reply_logged_while_the_reviewer_reads_cancels_the_follow_up(self):
        self.review_on()
        target = self.scheduled_follow_up()
        reviewer = self.passes_while(lambda other: log_event(other, target["id"], USER, "reply_logged", detail="Thanks! Let's talk Tuesday."))
        self.assertEqual([item["state"] for item in self.due(target, reviewer=reviewer)], ["cancelled"])
        self.assertEqual(len(reviewer.prompts), 1)
        self.assertEqual(len(self.gmail.sent), 1, "only the first email ever went")
        self.assertIn("They replied", self.event_details(target, "send_cancelled")[0])

    def test_a_bounce_found_while_the_reviewer_reads_stops_the_follow_up(self):
        # Checked before the review too; the review can take minutes, and the background bounce check
        # records one meanwhile. Here the email also reached a Cc, so the status stays sent and only
        # the send itself (outreach_gmail) is left to refuse the bounced address.
        self.review_on()
        target = self.scheduled_follow_up()
        reviewer = self.passes_while(lambda other: outreach_delivery.record_bounce(
            other, target["id"], user_id=USER, reason="Address not found", source="gmail",
            addresses=["greg@bovi.example"], sent={"to": "greg@bovi.example", "cc": "hr@bovi.example"},
        ))
        self.assertNotEqual([item["state"] for item in self.due(target, reviewer=reviewer)], ["sent"])
        after = self.target(target)
        self.assertEqual((after["status"], after["contact_bounced"]), ("sent", True))
        self.assertIn("greg@bovi.example bounced", self.schedule_row(target)["error"])
        self.assertEqual(len(self.gmail.sent), 1, "nothing to an address known to bounce")


if __name__ == "__main__":
    unittest.main()
