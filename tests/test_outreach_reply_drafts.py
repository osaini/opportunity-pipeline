"""The student's answer to a company's reply (outreach/reply_drafts.py, outreach/reply_writing.py): how a reply is read,
what the answer may say, which replies get one on their own, and approving and sending it in the company's thread."""

import base64
import json
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import realdata_guard
except ImportError:  # imported as tests.test_outreach_reply_drafts, with tests/ not on sys.path
    from tests import realdata_guard
realdata_guard.install()

import test_decline_thank_you as decline

from opportunity_app.automation import ledger as automation
from opportunity_app.core.timestamps import utc_now
from opportunity_app.outreach import reply_drafts, reply_writing, thank_you as outreach_thank_you
from opportunity_app.outreach.gmail import REPLY_SENT_EVENT, reply_claim_kind

AUTH = decline.AUTH
USER = decline.USER
FakeWriter = decline.FakeWriter

LATER = "Thanks for writing. We'd love to consider you, but we decide that a few months before summer. Please reach back around March of next year."
CALL = "Are you free Tuesday at 11am for a 15 minute chat? We're not hiring at the moment, but would love to connect."
FAR = "Would love to talk but 2027 seems a bit far for us."
ON_FILE = "At this time, we are not offering internship opportunities. We would be happy to keep your information on file."


def writing_inputs(**overrides):
    base = {
        "purpose": "answer", "reading": "paused", "today": "2026-10-13", "greeting": "Hi Dana,",
        "student": {"name": "Test Student", "school": "Example State University", "available_terms": ["summer 2027"]},
        "company": {"company": "Acme Robotics"},
        "their_reply": {"from_name": "Dana Lee", "received_on": "2026-10-12", "text": LATER},
        "earlier_replies": [], "your_emails": [], "in_person": {}, "max_words": 90,
    }
    return {**base, **overrides}


def answer(body, claims=None, **extra):
    return json.dumps({"body": body, "claims": claims if claims is not None else [{"text": "March", "basis": "their_reply"}], **extra})


GOOD = "Hi Dana,\n\nThanks for getting back to me. I'll reach back around March, and I'll apply through your site then.\n\nBest,\nTest Student"


def item(text, readings=None, source="gmail"):
    return {"id": "e1", "text": text, "data": {"source": source, "readings": readings or {}}, "at": datetime.now(timezone.utc)}


class ReadingTests(unittest.TestCase):
    def test_a_call_in_any_words_comes_before_the_no_beside_it(self):
        self.assertEqual(reply_drafts.reading_of(item(CALL), CALL), "call_scheduled")

    def test_later_and_too_far_off_read_as_later(self):
        self.assertEqual(reply_drafts.reading_of(item(LATER), LATER), "paused")
        self.assertEqual(reply_drafts.reading_of(item(FAR), FAR), "paused")

    def test_not_offering_reads_as_a_no(self):
        self.assertEqual(reply_drafts.reading_of(item(ON_FILE), ON_FILE), "declined")

    def test_an_offer_by_either_reading_is_left_to_the_student(self):
        jev = {"rules": {"status": "replied"}, "jev": {"label": "offer", "confidence": 0.4}}
        self.assertEqual(reply_drafts.reading_of(item("Welcome aboard", jev), "Welcome aboard"), "offer")
        self.assertEqual(reply_drafts.reading_of(item("We are pleased to offer you a spot."), "We are pleased to offer you a spot."), "offer")

    def test_a_sure_jev_reading_counts_and_an_unsure_one_does_not(self):
        sure = {"rules": {"status": "replied"}, "jev": {"label": "declined", "confidence": 0.9}}
        unsure = {"rules": {"status": "replied"}, "jev": {"label": "declined", "confidence": 0.2}}
        self.assertEqual(reply_drafts.reading_of(item("Thanks for the note.", sure), "Thanks for the note."), "declined")
        self.assertEqual(reply_drafts.reading_of(item("Thanks for the note.", unsure), "Thanks for the note."), "replied")


class WritingTests(unittest.TestCase):
    def test_a_good_answer_passes(self):
        claims, problems = reply_writing.validate(GOOD, [{"text": "March", "basis": "their_reply"}], writing_inputs())
        self.assertEqual(problems, [])
        self.assertEqual(claims, [{"text": "March", "basis": "their_reply"}])

    def test_a_day_or_a_time_the_student_did_not_choose_is_refused_but_a_bracket_is_not(self):
        inputs = writing_inputs(reading="call_scheduled", their_reply={"from_name": "", "received_on": "", "text": CALL})
        chosen = GOOD.replace("I'll reach back around March, and I'll apply through your site then.", "Tuesday at 11am works for me.")
        _, problems = reply_writing.validate(chosen, [{"text": "a chat", "basis": "their_reply"}], inputs)
        self.assertTrue(any("names a day or a time" in problem for problem in problems), problems)
        left = GOOD.replace("I'll reach back around March, and I'll apply through your site then.", "I'd be glad to talk. [your availability] works for me.")
        _, problems = reply_writing.validate(left, [{"text": "a chat", "basis": "their_reply"}], inputs)
        self.assertEqual(problems, [])

    def test_a_company_near_home_must_hear_when_the_student_is_there(self):
        inputs = writing_inputs(in_person={"where": "the Bay Area", "when": "over winter break"})
        _, problems = reply_writing.validate(GOOD, [{"text": "March", "basis": "their_reply"}], inputs)
        self.assertTrue(any("would like to meet in person" in problem for problem in problems), problems)
        met = GOOD.replace("then.", "then. I'm in the Bay Area over winter break and would love to meet in person.")
        _, problems = reply_writing.validate(met, [{"text": "in the Bay Area over winter break", "basis": "in_person"}], inputs)
        self.assertEqual(problems, [])

    def test_numbers_bases_greeting_and_name_are_checked(self):
        bad = "Hello there,\n\nI cut weight by 80 percent.\n\nBest,\nSomeone"
        _, problems = reply_writing.validate(bad, [{"text": "80", "basis": "profile:secret"}], writing_inputs())
        words = " ".join(problems)
        for expected in ("open with 'Hi Dana,'", "student's name", "numbers found in none", "profile:secret"):
            self.assertIn(expected, words)

    def test_a_revisit_date_is_kept_only_with_their_own_words_and_a_date_ahead(self):
        inputs = writing_inputs()
        self.assertEqual(reply_writing.revisit_suggestion({"revisit_on": "2027-03-01", "revisit_said": "around March of next year"}, inputs),
                         ("2027-03-01", "around March of next year"))
        self.assertEqual(reply_writing.revisit_suggestion({"revisit_on": "2027-03-01", "revisit_said": "in the spring"}, inputs), ("", ""),
                         "words they never wrote are not theirs")
        self.assertEqual(reply_writing.revisit_suggestion({"revisit_on": "2026-01-01", "revisit_said": "around March"}, inputs), ("", ""))
        self.assertEqual(reply_writing.revisit_suggestion({"revisit_on": "2027-03-01", "revisit_said": "around March"},
                                                          writing_inputs(purpose="revisit")), ("", ""))

    def test_the_template_passes_its_own_checks_for_every_reading(self):
        place = {"where": "the Bay Area", "when": "over winter break"}
        for reading in (*reply_writing.READINGS, "revisit"):
            with self.subTest(reading=reading):
                inputs = writing_inputs(reading=reading, in_person=place, purpose="revisit" if reading == "revisit" else "answer",
                                        max_words=130 if reading == "revisit" else 90)
                body, claims = reply_writing.template(inputs)
                self.assertEqual(reply_writing.validate(body, claims, inputs)[1], [])

    def test_the_model_gets_one_retry_and_then_the_student_sees_why(self):
        inputs = writing_inputs()
        retried = FakeWriter(answer("Tuesday works"), answer(GOOD, revisit_on="2027-03-01", revisit_said="around March of next year"))
        written = reply_writing.write(inputs, provider_factory=lambda *_: retried, provider="anthropic")
        self.assertEqual(written["body"], GOOD)
        self.assertEqual(written["revisit_on"], "2027-03-01")
        self.assertIn("was refused because", retried.prompts[1])
        with self.assertRaisesRegex(ValueError, "did not pass the checks"):
            reply_writing.write(inputs, provider_factory=lambda *_: FakeWriter(answer("no"), answer("no")), provider="anthropic")

    def test_without_a_model_the_template_is_used_and_comments_are_refused(self):
        written = reply_writing.write(writing_inputs(), provider_factory=None, provider="legacy")
        self.assertEqual(written["generated_by"], "template")
        with self.assertRaisesRegex(ValueError, "cannot follow comments"):
            reply_writing.write(writing_inputs(), provider_factory=None, provider="legacy", comments="shorter")

    def test_in_person_is_home_with_break_times_or_the_school_region(self):
        regions = [{"name": "Austin", "state_markers": ["tx"], "places": ["austin"]}, {"name": "Bay Area", "state_markers": ["ca"], "places": ["san francisco"]}]
        facts = {"break_location": "Bay Area", "break_times": "over winter break", "school": "Example University, Austin, TX"}
        sf = {"location": "San Francisco, CA", "location_basis": "manual"}
        austin = {"location": "Austin, TX", "location_basis": "manual"}
        self.assertEqual(reply_writing.in_person(facts, sf, regions), {"where": "the Bay Area", "when": "over winter break"})
        self.assertEqual(reply_writing.in_person({**facts, "break_times": ""}, sf, regions)["when"], reply_writing.HOME_WHEN)
        self.assertEqual(reply_writing.in_person(facts, austin, regions), {"where": "Austin", "when": reply_writing.SCHOOL_WHEN})
        self.assertEqual(reply_writing.in_person(facts, {**sf, "location_basis": "research"}, regions), {}, "an unchecked location is not used")


class ReplyCase(decline.DeclineCase):
    """A sent cold email to Acme with Gmail connected, and the reply drafts switch on (the thank-you switch off)."""

    def setUp(self):
        super().setUp()
        automation.set_mode(self.conn, USER, "decline_thank_you", "off")
        automation.set_mode(self.conn, USER, "reply_drafts", "on")
        self.env_reply = decline.mock.patch.dict("os.environ", {"PIPELINE_OUTREACH_REPLY_PROVIDER": "legacy"})
        self.env_reply.start()
        self.addCleanup(self.env_reply.stop)

    def run_pass(self, **kwargs):
        report = {}
        reply_drafts.run_for_user(self.conn, USER, report, provider_factory=None, gmail_client_factory=self.factory, **kwargs)
        return report

    def answered(self, text=LATER, **arrive):
        target = self.sent_target()
        self.decline(text, **arrive)
        self.run_pass()
        return self.target(target["id"])

    def fact(self, field, value):
        with self.conn:
            self.conn.execute(
                "INSERT INTO profile_facts(user_id, field_path, value_json, source, confirmed, created_at, updated_at) VALUES(?, ?, ?, 'user', 1, ?, ?) "
                "ON CONFLICT(user_id, field_path) DO UPDATE SET value_json=excluded.value_json, confirmed=1",
                (USER, field, json.dumps(value), utc_now(), utc_now()),
            )

    def sent_message(self, index=-1):
        sent = self.gmail.sent[index]
        return sent, BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(sent["raw"]))


class AutomaticDraftTests(ReplyCase):
    def test_a_later_reply_gets_an_answer_in_their_thread_waiting_for_approval(self):
        target = self.answered()
        draft = target["reply_draft"]
        self.assertEqual(draft["state"], "generated")
        self.assertEqual(draft["purpose"], "answer")
        self.assertEqual(draft["reading"], "paused")
        self.assertEqual(draft["to_email"], "dana@acme.com")
        self.assertEqual(draft["thread_id"], "t-decline")
        self.assertTrue(draft["subject"].startswith("Re: "))
        self.assertTrue(draft["body"].startswith("Hi Dana,"))
        self.assertFalse(draft["behind"])
        self.assertEqual(self.gmail.sent[1:], [], "nothing is sent")
        row = reply_drafts.reply_draft_row(self.conn, target["id"], USER)
        self.assertEqual(row["reply_message_id"], "<decline-1@acme.com>")
        action = self.conn.execute("SELECT * FROM automation_actions WHERE feature='reply_drafts'").fetchone()
        self.assertEqual(action["status"], "applied")
        self.assertNotIn(draft["body"], action["after_json"], "the ledger keeps who it was to, not the words")

    def test_each_reply_is_answered_once(self):
        target = self.answered()
        first = reply_drafts.reply_draft_row(self.conn, target["id"], USER)
        self.assertEqual(self.run_pass().get("reply_drafts"), [])
        self.assertEqual(reply_drafts.reply_draft_row(self.conn, target["id"], USER)["draft_id"], first["draft_id"])

    def test_nothing_is_written_while_the_switch_is_off(self):
        automation.set_mode(self.conn, USER, "reply_drafts", "off")
        self.assertIsNone(self.answered()["reply_draft"])

    def test_an_offer_is_left_to_the_student(self):
        self.assertIsNone(self.answered("We are pleased to offer you a summer internship.")["reply_draft"])

    def test_a_plain_decline_the_thank_you_answers_gets_no_draft(self):
        automation.set_mode(self.conn, USER, "decline_thank_you", "on")
        self.assertIsNone(self.answered(decline.DECLINE)["reply_draft"])

    def test_a_reply_the_student_already_answered_in_gmail_is_noted_and_left(self):
        target = self.sent_target()
        received = self.arrive(LATER)
        self.check()
        self.gmail.replies["t-decline"].append({"id": "mine-1", "labelIds": ["SENT"], "internalDate": str(received + 60_000)})
        self.run_pass()
        draft = self.target(target["id"])["reply_draft"]
        self.assertEqual(draft["state"], "skipped")
        self.assertEqual(draft["note"], reply_drafts.ANSWERED_IN_GMAIL)
        self.run_pass()
        self.assertEqual(self.events(target["id"], reply_drafts.SKIPPED_EVENT), [reply_drafts.ANSWERED_IN_GMAIL], "and not looked at again")

    def test_an_old_reply_is_left_for_the_write_button(self):
        target = self.sent_target()
        self.decline(LATER)
        self.run_pass(now=datetime.now(timezone.utc) + reply_drafts.REPLY_WINDOW + timedelta(days=1))
        self.assertIsNone(self.target(target["id"])["reply_draft"])

    def test_undo_puts_it_away_and_it_is_not_written_again(self):
        target = self.answered()
        action = self.conn.execute("SELECT id FROM automation_actions WHERE feature='reply_drafts'").fetchone()
        automation.undo(self.conn, action["id"], USER)
        self.assertEqual(self.target(target["id"])["reply_draft"]["state"], "dismissed")
        self.run_pass()
        self.assertEqual(self.target(target["id"])["reply_draft"]["state"], "dismissed")

    def test_a_newer_reply_leaves_the_draft_behind_and_gets_its_own(self):
        target = self.answered()
        first = reply_drafts.reply_draft_row(self.conn, target["id"], USER)["draft_id"]
        self.decline("One more thing: are you free for a chat? ", message_id="decline-2", received=decline.now_ms(timedelta(minutes=10)))
        self.assertTrue(self.target(target["id"])["reply_draft"]["behind"])
        self.run_pass()
        row = reply_drafts.reply_draft_row(self.conn, target["id"], USER)
        self.assertNotEqual(row["draft_id"], first)
        self.assertEqual(row["reply_gmail_id"], "decline-2")
        self.assertEqual(row["reading"], "call_scheduled")


class ApproveAndSendTests(ReplyCase):
    def test_a_bracket_blocks_approval_until_the_student_fills_it(self):
        target = self.answered(CALL)
        draft = target["reply_draft"]
        self.assertIn("[your availability]", draft["body"])
        refused = self.client.post(f"/api/v1/outreach/{target['id']}/reply-draft/approve", headers=AUTH, json={"fingerprint": draft["fingerprint"]})
        self.assertEqual(refused.status_code, 422)
        self.assertIn("[your availability]", refused.json()["detail"])
        edited = self.client.patch(f"/api/v1/outreach/{target['id']}/reply-draft", headers=AUTH, json={
            "subject": draft["subject"], "body": draft["body"].replace("[your availability]", "Any afternoon next week"),
            "fingerprint": draft["fingerprint"],
        })
        self.assertEqual(edited.status_code, 200, edited.text)
        approved = self.client.post(f"/api/v1/outreach/{target['id']}/reply-draft/approve", headers=AUTH,
                                    json={"fingerprint": edited.json()["reply_draft"]["fingerprint"]})
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertEqual(approved.json()["reply_draft"]["state"], "approved")

    def approved(self, text=LATER, **arrive):
        target = self.answered(text, **arrive)
        draft = target["reply_draft"]
        approved = self.client.post(f"/api/v1/outreach/{target['id']}/reply-draft/approve", headers=AUTH, json={"fingerprint": draft["fingerprint"]})
        self.assertEqual(approved.status_code, 200, approved.text)
        return approved.json()

    def send(self, target, **extra):
        return self.client.post(f"/api/v1/outreach/{target['id']}/reply-draft/send", headers=AUTH,
                                json={"fingerprint": target["reply_draft"]["fingerprint"], **extra})

    def test_sending_goes_once_in_their_thread_and_is_recorded(self):
        target = self.approved()
        sent = self.send(target)
        self.assertEqual(sent.status_code, 200, sent.text)
        request, message = self.sent_message()
        self.assertEqual(request["threadId"], "t-decline")
        self.assertEqual(message["In-Reply-To"], "<decline-1@acme.com>")
        self.assertIn("dana@acme.com", message["To"])
        self.assertEqual(sent.json()["target"]["reply_draft"]["state"], "sent")
        row = reply_drafts.reply_draft_row(self.conn, target["id"], USER)
        claim = self.conn.execute("SELECT state FROM outreach_send_claims WHERE kind=?", (reply_claim_kind(row["draft_id"]),)).fetchone()
        self.assertEqual(claim["state"], "sent")
        self.assertEqual(len(self.events(target["id"], REPLY_SENT_EVENT)), 1)
        again = self.send(target)
        self.assertEqual(again.status_code, 409)
        self.assertEqual(len(self.gmail.sent), 2, "the first email and the reply, once")

    def test_its_own_send_is_not_news_to_it_when_their_reply_came_before(self):
        # As real mail arrives: the first email a day ago, their reply an hour ago, and the answer's claim taken now.
        target = self.approved()
        day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        with self.conn:
            self.conn.execute("UPDATE outreach_events SET created_at=? WHERE event_type IN ('gmail_sent', 'status')", (day_ago,))
            self.conn.execute("UPDATE outreach_send_claims SET claimed_at=?", (day_ago,))
            row = self.conn.execute("SELECT id, detail_json FROM outreach_events WHERE event_type='reply_logged'").fetchone()
            self.conn.execute("UPDATE outreach_events SET detail_json=? WHERE id=?",
                              (json.dumps({**json.loads(row["detail_json"]), "received_at": hour_ago}), row["id"]))
        self.gmail.replies["t-decline"][0]["internalDate"] = str(int(datetime.fromisoformat(hour_ago).timestamp() * 1000))
        sent = self.send(target)
        self.assertEqual(sent.status_code, 200, sent.text)

    def test_an_unapproved_or_changed_reply_is_not_sent(self):
        target = self.answered()
        self.assertEqual(self.send(target).status_code, 409)
        target = self.approved_again(target)
        self.assertEqual(self.send({**target, "reply_draft": {**target["reply_draft"], "fingerprint": "0" * 64}}).status_code, 409)
        self.assertEqual(len(self.gmail.sent), 1)

    def approved_again(self, target):
        response = self.client.post(f"/api/v1/outreach/{target['id']}/reply-draft/approve", headers=AUTH,
                                    json={"fingerprint": target["reply_draft"]["fingerprint"]})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_a_reply_they_wrote_since_stops_it(self):
        target = self.approved()
        self.decline("Actually, one more question for you?", message_id="decline-2", received=decline.now_ms(timedelta(minutes=10)))
        refused = self.send(target)
        self.assertEqual(refused.status_code, 409)
        self.assertEqual(len(self.gmail.sent), 1)

    def test_an_answer_of_the_students_own_in_the_thread_stops_it(self):
        target = self.approved()
        # After their reply, which the fixture has arrive a few minutes ahead of the clock.
        self.gmail.replies["t-decline"].append({"id": "mine-1", "labelIds": ["SENT"], "internalDate": str(decline.now_ms(timedelta(minutes=20)))})
        refused = self.send(target)
        self.assertEqual(refused.status_code, 409)
        self.assertIn("already answered", refused.json()["detail"])
        self.assertEqual(self.target(target["id"])["reply_draft"]["state"], "approved", "it waits, with why")

    def test_a_send_gmail_may_have_carried_out_asks_the_student_to_look_first(self):
        target = self.approved()
        self.gmail.send_status = 503
        self.assertEqual(self.send(target).status_code, 502)
        self.gmail.send_status = 200
        first = self.send(target)
        self.assertEqual(first.status_code, 428)
        check = first.json()["detail"]["check"]
        self.assertEqual(self.send(target, sent_folder_check=check).status_code, 200)

    def test_put_in_gmail_drafts_makes_a_draft_in_their_thread_and_the_app_leaves_it(self):
        target = self.answered()
        made = self.client.post(f"/api/v1/outreach/{target['id']}/reply-draft/gmail-draft", headers=AUTH)
        self.assertEqual(made.status_code, 200, made.text)
        self.assertEqual(next(iter(self.gmail.drafts.values()))["message"]["threadId"], "t-decline")
        self.assertEqual(made.json()["target"]["reply_draft"]["state"], "in_gmail")
        self.assertTrue(outreach_thank_you.sent_since(self.conn, target["id"], USER, datetime.now(timezone.utc) - timedelta(hours=1)))

    def test_a_sent_reply_counts_as_something_that_went_to_them(self):
        target = self.approved()
        before = datetime.now(timezone.utc)
        self.assertFalse(outreach_thank_you.sent_since(self.conn, target["id"], USER, before))
        self.assertEqual(self.send(target).status_code, 200)
        self.assertTrue(outreach_thank_you.sent_since(self.conn, target["id"], USER, before), "so no thank-you follows it")

    def test_dismiss_puts_it_away(self):
        target = self.answered()
        dismissed = self.client.delete(f"/api/v1/outreach/{target['id']}/reply-draft", headers=AUTH)
        self.assertEqual(dismissed.json()["reply_draft"]["state"], "dismissed")


class RevisitTests(ReplyCase):
    def test_the_date_their_reply_names_becomes_the_revisit_date_on_request(self):
        target = self.sent_target()
        self.decline(LATER)
        writer = FakeWriter(answer(GOOD, revisit_on="2027-03-01", revisit_said="around March of next year"))
        decline.mock.patch.dict("os.environ", {"PIPELINE_OUTREACH_REPLY_PROVIDER": "anthropic"}).start()
        reply_drafts.generate(self.conn, target["id"], user_id=USER, provider_factory=lambda *_: writer)
        draft = self.target(target["id"])["reply_draft"]
        self.assertEqual((draft["revisit_on"], draft["revisit_said"]), ("2027-03-01", "around March of next year"))
        used = self.client.post(f"/api/v1/outreach/{target['id']}/reply-draft/revisit", headers=AUTH)
        self.assertEqual(used.status_code, 200, used.text)
        self.assertEqual(used.json()["follow_up_at"], "2027-03-01")
        self.assertIn(used.json()["status"], ("replied", "paused"))

    def test_on_the_revisit_date_the_note_is_written_and_sending_it_marks_them_followed_up(self):
        target = self.answered()
        self.client.patch(f"/api/v1/outreach/{target['id']}", headers=AUTH, json={"status": "paused", "follow_up_at": date.today().isoformat()})
        # The answer to their reply went already, and the first email weeks ago: nothing went near the revisit date.
        weeks_ago = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        with self.conn:
            self.conn.execute("UPDATE outreach_reply_drafts SET state='sent', sent_at=?", (weeks_ago,))
            self.conn.execute("UPDATE outreach_events SET created_at=? WHERE event_type IN ('gmail_sent', 'status')", (weeks_ago,))
            self.conn.execute("UPDATE outreach_send_claims SET claimed_at=?", (weeks_ago,))
        self.run_pass()
        draft = self.target(target["id"])["reply_draft"]
        self.assertEqual(draft["purpose"], "revisit")
        self.assertEqual(draft["revisit_for"], date.today().isoformat())
        approved = self.client.post(f"/api/v1/outreach/{target['id']}/reply-draft/approve", headers=AUTH, json={"fingerprint": draft["fingerprint"]})
        self.assertEqual(approved.status_code, 200, approved.text)
        sent = self.client.post(f"/api/v1/outreach/{target['id']}/reply-draft/send", headers=AUTH,
                                json={"fingerprint": approved.json()["reply_draft"]["fingerprint"]})
        self.assertEqual(sent.status_code, 200, sent.text)
        self.assertEqual(sent.json()["target"]["status"], "followed_up")


class ManualWriteTests(ReplyCase):
    def test_write_a_reply_works_with_the_switch_off_and_needs_a_reply(self):
        automation.set_mode(self.conn, USER, "reply_drafts", "off")
        target = self.sent_target()
        missing = self.client.post(f"/api/v1/outreach/{target['id']}/reply-draft", headers=AUTH, json={})
        self.assertEqual(missing.status_code, 422)
        self.decline(FAR)
        written = self.client.post(f"/api/v1/outreach/{target['id']}/reply-draft", headers=AUTH, json={})
        self.assertEqual(written.status_code, 200, written.text)
        self.assertEqual(written.json()["reply_draft"]["reading"], "paused")

    def test_a_rewrite_keeps_the_draft_id_so_an_unconfirmed_send_still_holds(self):
        target = self.sent_target()
        self.decline(FAR)
        self.client.post(f"/api/v1/outreach/{target['id']}/reply-draft", headers=AUTH, json={})
        first = reply_drafts.reply_draft_row(self.conn, target["id"], USER)["draft_id"]
        self.client.post(f"/api/v1/outreach/{target['id']}/reply-draft", headers=AUTH, json={})
        self.assertEqual(reply_drafts.reply_draft_row(self.conn, target["id"], USER)["draft_id"], first)

    def test_the_card_and_the_account_export_carry_it(self):
        target = self.answered()
        listed = self.client.get("/api/v1/outreach", headers=AUTH).json()
        rows = listed["items"] if isinstance(listed, dict) else listed
        self.assertEqual(next(row for row in rows if row["id"] == target["id"])["reply_draft"]["state"], "generated")
        exported = self.client.get("/api/v1/account/export", headers=AUTH)
        self.assertEqual(exported.status_code, 200, exported.text)
        self.assertEqual(len(exported.json()["outreach_reply_drafts"]), 1)


if __name__ == "__main__":
    unittest.main()
