"""Contact forms for companies with no email: found by the crawl, filled truthfully, sent once."""

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR, automation, outreach_forms
from opportunity_app.api import create_app
from opportunity_app.outreach import create_target, get_target
from opportunity_app.outreach_automation import AutomationWorker, draft_due, send_form, update_settings
from opportunity_app.outreach_contacts import find_contacts
from opportunity_app.outreach_forms import (
    FormSubmitter,
    contact_forms,
    formatting_problem,
    form_due,
    is_acknowledgement,
    plan_fill,
    submit_contact_form,
)
from opportunity_app.schema import connect_product, utc_now

from helpers_platform import build_and_migrate
from test_outreach_discovery import safe_fetcher, site_transport
from test_outreach_drafting import confirm_facts
from test_outreach_gmail import ACCOUNT
from test_outreach_inbox import ReplyCaptureTests, mail, now_ms

AUTH = {"Authorization": "Bearer forms-owner"}
USER = "local-user"
IDENTITY = {"name": "Sam Rivera", "email": ACCOUNT, "phone": "512-555-0100", "school": "State University", "link": "", "title": "Student"}
CONTACT_PAGE = """
<html><head><script src="https://www.google.com/recaptcha/api.js"></script></head><body>
<form action="/search" role="search"><input type="text" name="q"><textarea name="notes"></textarea><input type="email" name="e"></form>
<form id="newsletter" action="/subscribe"><input type="email" name="email"><button>Join</button></form>
<form id="contact-form" action="/send" method="post">
  <label for="n">Your name</label><input id="n" name="name" required>
  <label for="e">Email</label><input id="e" type="email" name="email" required>
  <label for="m">Message</label><textarea id="m" name="message" maxlength="3000" required></textarea>
  <div class="g-recaptcha" data-sitekey="x"></div>
  <button type="submit">Send</button>
</form></body></html>
"""


def field(index, type_="text", *, tag="input", label="", name="", required=False, visible=True, maxlength=None, options=(), accept=""):
    return {"index": index, "tag": tag, "type": type_, "label": label, "name": name, "id": name, "placeholder": "",
            "autocomplete": "", "required": required, "visible": visible, "maxlength": maxlength, "options": list(options), "accept": accept}


class FindingTests(unittest.TestCase):
    def test_only_the_contact_form_counts_and_its_captcha_is_noted(self):
        forms = contact_forms("https://bovi.test/contact", CONTACT_PAGE)
        self.assertEqual(len(forms), 1, "search and newsletter forms are not contact forms")
        form = forms[0]
        self.assertEqual(form["captcha"], "recaptcha")
        self.assertEqual([f["name"] for f in form["fields"]], ["name", "email", "message"])
        self.assertEqual(form["fields"][2]["label"], "Message")
        self.assertEqual(form["fields"][2]["maxlength"], 3000)

    def test_a_login_form_with_a_textarea_is_not_a_contact_form(self):
        page = '<form><input type="email" name="email"><input type="password" name="pw"><textarea></textarea></form>'
        self.assertEqual(contact_forms("https://bovi.test/", page), [])

    def test_a_script_built_hubspot_form_is_noted_without_fields(self):
        page = '<script src="//js.hsforms.net/forms/embed/v2.js"></script><script>hbspt.forms.create({})</script>'
        self.assertEqual(contact_forms("https://bovi.test/contact", page)[0]["fields"], [])

    def test_the_crawl_records_the_form_for_a_company_with_no_address(self):
        _, platform = build_and_migrate(Path(self._tmp()))
        site = {"bovi.test": {"/robots.txt": "", "/": '<a href="/contact">Contact</a><p>We build robots.</p>', "/contact": CONTACT_PAGE}}
        transport, _requested = site_transport(site)
        with closing(connect_product(platform)) as conn, httpx.Client(transport=transport) as client:
            target = create_target(conn, {"company": "Bovi", "website": "https://bovi.test"}, user_id=USER)
            result = find_contacts(conn, target["id"], user_id=USER, fetcher=safe_fetcher(client), delay=0)
            self.assertEqual(result["contact_form"], "https://bovi.test/contact")
            form = get_target(conn, target["id"], user_id=USER, include_events=True)
            self.assertEqual((form["contact_form"]["page_url"], form["contact_form"]["state"]), ("https://bovi.test/contact", "found"))
            self.assertEqual(form["contact_form"]["captcha"], "recaptcha")
            find_contacts(conn, target["id"], user_id=USER, fetcher=safe_fetcher(client), delay=0)
            found = [e for e in get_target(conn, target["id"], user_id=USER, include_events=True)["events"] if e["event_type"] == "contact_form_found"]
            self.assertEqual(len(found), 1, "a second crawl does not log the same form again")
            # A form already used keeps its record through later crawls.
            conn.execute("UPDATE outreach_contact_forms SET state='submitted'")
            conn.commit()
            find_contacts(conn, target["id"], user_id=USER, fetcher=safe_fetcher(client), delay=0)
            self.assertEqual(get_target(conn, target["id"], user_id=USER)["contact_form"]["state"], "submitted")

    def _tmp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        return tempdir.name


class FillPlanTests(unittest.TestCase):
    def fields(self, *extra):
        return [
            field(0, label="First name", name="first_name", required=True),
            field(1, label="Last name", name="last_name", required=True),
            field(2, "email", label="Work email", name="email", required=True),
            field(3, label="Subject", name="subject"),
            field(4, "textarea", tag="textarea", label="How can we help?", name="message", required=True, maxlength=2000),
            *extra,
        ]

    def plan(self, fields, body="Hi Bovi team,\n\nA short note.\n\nSam", attachment=""):
        return plan_fill(fields, IDENTITY, subject="Robotics internship question", body=body, attachment=attachment)

    def values(self, plan):
        return {fill["role"]: fill["value"] for fill in plan["fills"]}

    def test_the_students_confirmed_facts_go_in_and_nothing_else(self):
        plan = self.plan(self.fields(
            field(5, "tel", label="Phone", name="phone"),
            field(6, label="Company", name="company"),
            field(7, "checkbox", label="Sign me up for the newsletter", name="news"),
        ))
        self.assertEqual(plan["problems"], [])
        self.assertEqual(self.values(plan), {
            "first_name": "Sam", "last_name": "Rivera", "email": ACCOUNT, "subject": "Robotics internship question",
            "message": "Hi Bovi team,\n\nA short note.\n\nSam",
        }, "an optional phone, company, or newsletter box stays empty")

    def test_a_hidden_spam_trap_is_never_filled(self):
        plan = self.plan(self.fields(field(5, label="Leave this empty", name="website", visible=False)))
        self.assertNotIn(5, [fill["index"] for fill in plan["fills"]])

    def test_required_consent_is_ticked_and_required_details_come_from_the_profile(self):
        plan = self.plan(self.fields(
            field(5, "checkbox", label="I agree to the privacy policy", name="consent", required=True),
            field(6, "tel", label="Phone", name="phone", required=True),
            field(7, tag="select", type_="select-one", label="Reason", name="reason", required=True,
                  options=[{"value": "", "text": "Select…"}, {"value": "sales", "text": "Sales"}, {"value": "jobs", "text": "Careers"}]),
        ))
        self.assertEqual(plan["problems"], [])
        by_index = {fill["index"]: fill for fill in plan["fills"]}
        self.assertEqual(by_index[5]["action"], "check")
        self.assertEqual(by_index[6]["value"], "512-555-0100")
        self.assertEqual(by_index[7]["value"], "jobs")

    def test_a_question_the_app_cannot_answer_truthfully_stops_it(self):
        plan = self.plan(self.fields(
            field(5, label="Annual budget", name="budget", required=True),
            field(6, tag="select", type_="select-one", label="Industry", name="industry", required=True,
                  options=[{"value": "a", "text": "Automotive"}, {"value": "b", "text": "Banking"}]),
        ))
        self.assertEqual(len(plan["problems"]), 2)
        self.assertIn("Annual budget", plan["problems"][0])

    def test_a_draft_longer_than_the_box_is_not_cut(self):
        plan = self.plan(self.fields(), body="x" * 2500)
        self.assertEqual(plan["problems"], ["The message box takes 2000 characters and the draft is 2500. Shorten the draft"])

    def test_the_resume_goes_in_a_file_field_that_takes_it(self):
        plan = self.plan(self.fields(field(5, "file", label="Attach resume", name="cv", accept=".pdf,.docx")), attachment="C:/r/Resume.pdf")
        self.assertEqual(self.values(plan)["file"], "C:/r/Resume.pdf")
        plan = self.plan(self.fields(field(5, "file", label="Attach resume", name="cv", required=True)))
        self.assertIn("no resume is set to attach", plan["problems"][0])

    def test_a_form_with_no_email_field_is_refused(self):
        plan = self.plan([field(0, label="Name", name="name"), field(1, "textarea", tag="textarea", label="Message", name="m")])
        self.assertIn("The form has no email field, so a reply could not reach you", plan["problems"])

    def test_formatting_differences_are_named(self):
        letter = "Hi Bovi team,\n\nI’m Sam — hello.\n\nSam"
        self.assertEqual(formatting_problem(letter, letter.replace("\n", "\r\n")), "")
        self.assertEqual(formatting_problem(letter, letter.replace("\n\n", " ")),
                         "the draft has 2 blank lines between paragraphs and the box kept 0")
        self.assertEqual(formatting_problem(letter, letter[:12]), f"it cut the text off after 12 of {len(letter)} characters")
        self.assertEqual(formatting_problem(letter, letter.replace("’", "")), "it dropped '’'")


class AcknowledgementTests(unittest.TestCase):
    def test_a_receipt_right_after_the_form_is_automatic_but_a_person_later_is_not(self):
        self.assertTrue(is_acknowledgement("hello@bovi.test", "We received your message", "Thanks!", 1))
        self.assertTrue(is_acknowledgement("noreply@bovi.test", "Hi Sam", "Anything", 600))
        self.assertTrue(is_acknowledgement("ana@bovi.test", "Your submission", "Here is a copy of your submission", 600))
        self.assertFalse(is_acknowledgement("ana@bovi.test", "Hi Sam", "Thank you for reaching out! Let's talk Tuesday.", 180))


class FakeSubmitter:
    """Stands in for the browser: records what it was asked and answers with a scripted outcome."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
        self.in_browser = []
        self.before_button = lambda: None

    def factory(self, *, in_browser=False):
        self.in_browser.append(in_browser)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def submit(self, page_url, *, should_continue=None, **kwargs):
        self.calls.append({"page_url": page_url, "should_continue": should_continue, **kwargs})
        outcome = self.outcomes.pop(0)
        if outcome == "pause_at_button":
            # As FormSubmitter does: asked just before the button, and stopped there.
            self.before_button()
            if should_continue is not None and not should_continue():
                return {"outcome": "failed", "note": outreach_forms.PAUSED_BEFORE_SENDING, "confirmation": "", "filled": [],
                        "screenshot": "", "attached": "", "paused": True}
            outcome = "submitted"
        return {"outcome": outcome, "note": "" if outcome == "submitted" else f"{outcome} for a reason",
                "confirmation": "Thanks! Your message has been sent." if outcome == "submitted" else "",
                "filled": ["Your name", "Email", "Message"], "screenshot": "", "attached": getattr(self, "attached", "")}


class FormSendTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        _, self.platform_path = build_and_migrate(root)
        self.env = mock.patch.dict("os.environ", {"PIPELINE_OUTREACH_ACCOUNT": ACCOUNT, "PIPELINE_OUTREACH_ATTACHMENT": ""})
        self.env.start()
        self.submitter = FakeSubmitter([])
        self.client = self.app(self.submitter.factory)
        with closing(connect_product(self.platform_path)) as conn:
            confirm_facts(conn, name="Sam Rivera", school="State University")

    def app(self, factory):
        root = Path(self.tempdir.name)
        client = TestClient(create_app(
            db_path=self.platform_path, access_token="forms-owner", static_dir=STATIC_DIR,
            resume_storage=root / "resumes", capture_storage=root / "captures", interview_storage=root / "interviews",
            outreach_form_submitter_factory=factory,
        ))
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def tearDown(self):
        self.env.stop()
        self.tempdir.cleanup()

    def target(self, with_form=True, **values):
        created = self.client.post("/api/v1/outreach", headers=AUTH, json={
            "company": "Bovi", "website": "https://bovi.test",
            "email_subject": "Robotics internship question", "email_body": "Hi Bovi team,\n\nA short note.\n\nSam", **values,
        }).json()
        if with_form:
            saved = self.client.put(f"/api/v1/outreach/{created['id']}/contact-form", headers=AUTH, json={"page_url": "https://bovi.test/contact"})
            self.assertEqual(saved.status_code, 200, saved.text)
            created = saved.json()
        return created

    def approved(self, **values):
        target = self.target(**values)
        approved = self.client.post(f"/api/v1/outreach/{target['id']}/approve", headers=AUTH, json={
            "kind": "initial", "fingerprint": target["draft_fingerprint"], "acknowledge_warnings": True,
        })
        self.assertEqual(approved.status_code, 200, approved.text)
        return approved.json()

    def send(self, target, **extra):
        return self.client.post(f"/api/v1/outreach/{target['id']}/form-submit", headers=AUTH, json={
            "fingerprint": target["draft_fingerprint"], **extra,
        })

    def get(self, target):
        return self.client.get(f"/api/v1/outreach/{target['id']}", headers=AUTH).json()

    def test_a_draft_with_no_email_can_be_approved_only_when_a_form_is_on_record(self):
        target = self.target(with_form=False)
        refused = self.client.post(f"/api/v1/outreach/{target['id']}/approve", headers=AUTH, json={
            "kind": "initial", "fingerprint": target["draft_fingerprint"], "acknowledge_warnings": True,
        })
        self.assertEqual(refused.status_code, 422)
        self.assertEqual(self.approved(company="Orbit")["draft_status"], "approved")

    def test_a_confirmed_submission_marks_the_company_sent_and_goes_once(self):
        self.submitter.outcomes = ["submitted"]
        target = self.approved()
        response = self.send(target)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual((body["outcome"], body["confirmation"]), ("submitted", "Thanks! Your message has been sent."))
        call = self.submitter.calls[0]
        self.assertEqual((call["page_url"], call["body"], call["identity"]["email"]), ("https://bovi.test/contact", target["email_body"], ACCOUNT))
        after = self.get(target)
        self.assertEqual((after["status"], after["contact_form"]["state"]), ("sent", "submitted"))
        self.assertTrue(after["sent_at"] and after["follow_up_at"])
        event = next(e for e in after["events"] if e["event_type"] == "form_submitted")
        self.assertEqual(json.loads(event["detail"])["fingerprint"], target["draft_fingerprint"])
        again = self.send(target)
        self.assertEqual(again.status_code, 422)
        self.assertEqual(len(self.submitter.calls), 1, "a first message goes out once")

    def test_the_history_names_a_resume_only_when_the_form_took_it(self):
        # Orbital Arc: a resume was set to attach, the form had no file field, and the history said it went.
        resume = Path(self.tempdir.name) / "Resume.pdf"
        resume.write_bytes(b"%PDF-1.4 resume")
        with mock.patch.dict("os.environ", {"PIPELINE_OUTREACH_ATTACHMENT": str(resume)}):
            self.submitter.outcomes = ["submitted", "submitted"]
            no_field = self.approved()
            self.send(no_field)
            self.submitter.attached = "Resume.pdf"
            with_field = self.approved(company="Orbit")
            self.send(with_field)

        def recorded(target):
            event = next(e for e in self.get(target)["events"] if e["event_type"] == "form_submitted")
            return json.loads(event["detail"])["attachment"]

        self.assertEqual(self.submitter.calls[0]["attachment"], str(resume), "the resume is offered to the form")
        self.assertEqual(recorded(no_field), "", "a form with no file field took no resume")
        self.assertEqual(recorded(with_field), "Resume.pdf")

    def test_an_unconfirmed_send_is_not_repeated_until_the_student_has_looked(self):
        self.submitter.outcomes = ["unconfirmed", "submitted"]
        target = self.approved()
        self.assertEqual(self.send(target).json()["outcome"], "unconfirmed")
        after = self.get(target)
        self.assertEqual((after["status"], after["contact_form"]["state"]), ("drafted", "unconfirmed"))
        blocked = self.send(target)
        self.assertEqual(blocked.status_code, 428)
        self.assertEqual(len(self.submitter.calls), 1)
        self.assertEqual(self.send(target, retry_unconfirmed=True).json()["outcome"], "submitted")

    def test_an_unconfirmed_send_the_student_saw_arrive_is_marked_sent_by_hand(self):
        self.submitter.outcomes = ["unconfirmed"]
        target = self.approved()
        self.send(target)
        marked = self.client.patch(f"/api/v1/outreach/{target['id']}", headers=AUTH, json={"status": "sent"})
        self.assertEqual(marked.status_code, 200, marked.text)
        self.assertEqual(marked.json()["status"], "sent")
        self.assertEqual(self.send(target, retry_unconfirmed=True).status_code, 422, "and it is not sent again")

    def test_nothing_sent_leaves_the_company_as_it_was_and_can_be_finished_in_the_browser(self):
        self.submitter.outcomes = ["needs_you", "submitted"]
        target = self.approved()
        result = self.send(target).json()
        self.assertEqual((result["outcome"], result["note"]), ("needs_you", "needs_you for a reason"))
        after = self.get(target)
        self.assertEqual((after["status"], after["contact_form"]["state"]), ("drafted", "needs_you"))
        with closing(connect_product(self.platform_path)) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM outreach_send_claims").fetchone(), "nothing left, so nothing is held")
        self.assertEqual(self.send(target, in_browser=True).json()["outcome"], "submitted")
        self.assertEqual(self.submitter.in_browser, [False, True])

    def test_a_changed_draft_or_a_company_with_an_email_is_refused(self):
        target = self.approved()
        stale = self.client.post(f"/api/v1/outreach/{target['id']}/form-submit", headers=AUTH, json={"fingerprint": "0" * 64})
        self.assertEqual(stale.status_code, 409)
        with_email = self.approved(company="Orbit", contact_email="greg@orbit.test")
        refused = self.send(with_email)
        self.assertEqual(refused.status_code, 422)
        self.assertIn("send the email instead", refused.json()["detail"])
        self.assertEqual(self.submitter.calls, [])

    def test_a_copy_without_a_browser_says_so(self):
        client = self.app(None)
        target = self.approved()
        response = client.post(f"/api/v1/outreach/{target['id']}/form-submit", headers=AUTH, json={"fingerprint": target["draft_fingerprint"]})
        self.assertEqual(response.status_code, 503)

    def test_the_worker_sends_only_with_the_switch_on_and_parks_a_refusal(self):
        self.submitter.outcomes = ["submitted"]
        target = self.approved()
        worker = AutomationWorker(self.platform_path, fetcher_factory=lambda: None, form_submitter_factory=self.submitter.factory)
        with closing(connect_product(self.platform_path)) as conn:
            self.assertEqual(form_due(conn, user_id=USER), [target["id"]])
            self.assertEqual(worker.run_once()["forms"], [], "off until the student turns it on")
            update_settings(conn, {"form_submission": True}, user_id=USER)
        self.assertEqual(worker.run_once()["forms"][0]["outcome"], "submitted")
        self.assertEqual(self.get(target)["status"], "sent")
        # A company the app cannot write for (no confirmed name) is parked, not retried every pass.
        other = self.approved(company="Orbit")
        with closing(connect_product(self.platform_path)) as conn:
            conn.execute("DELETE FROM profile_facts WHERE field_path='name'")
            conn.commit()
        self.assertEqual(worker.run_once()["forms"][0]["outcome"], "refused")
        self.assertEqual(self.get(other)["contact_form"]["state"], "needs_you")
        self.assertEqual(worker.run_once()["forms"], [])

    def form_state(self, target):
        return self.get(target)["contact_form"]["state"]

    def test_an_automatic_submit_while_paused_leaves_the_form_waiting(self):
        self.submitter.outcomes = ["submitted"]
        target = self.approved()
        worker = AutomationWorker(self.platform_path, fetcher_factory=lambda: None, form_submitter_factory=self.submitter.factory)
        with closing(connect_product(self.platform_path)) as conn:
            update_settings(conn, {"form_submission": True}, user_id=USER)
            automation.set_paused(conn, USER, True)
            self.assertEqual(worker.run_once()["forms"], [], "a paused student's forms are not tried")
            # Paused by the time the worker's claim is taken: refused there, and not parked.
            self.assertEqual(send_form(conn, target["id"], user_id=USER, submitter_factory=self.submitter.factory)["outcome"], "paused")
            self.assertIsNone(conn.execute("SELECT 1 FROM outreach_send_claims").fetchone(), "the claim went back with the refusal")
        self.assertEqual(self.submitter.calls, [])
        self.assertEqual(self.form_state(target), "found")
        with closing(connect_product(self.platform_path)) as conn:
            automation.set_paused(conn, USER, False)
        self.assertEqual(worker.run_once()["forms"][0]["outcome"], "submitted", "it goes once they resume")

    def test_a_pause_just_before_the_button_sends_nothing_and_keeps_the_form_waiting(self):
        self.submitter.outcomes = ["pause_at_button"]
        target = self.approved()

        def pause():
            with closing(connect_product(self.platform_path)) as conn:
                automation.set_paused(conn, USER, True)

        self.submitter.before_button = pause
        with closing(connect_product(self.platform_path)) as conn:
            result = send_form(conn, target["id"], user_id=USER, submitter_factory=self.submitter.factory)
        self.assertEqual((result["outcome"], result["note"]), ("failed", outreach_forms.PAUSED_BEFORE_SENDING))
        after = self.get(target)
        self.assertEqual((after["contact_form"]["state"], after["status"]), ("found", "drafted"))
        with closing(connect_product(self.platform_path)) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM outreach_send_claims").fetchone())

    def test_the_students_own_form_send_is_not_paused(self):
        self.submitter.outcomes = ["submitted"]
        target = self.approved()
        with closing(connect_product(self.platform_path)) as conn:
            automation.set_paused(conn, USER, True)
        self.assertEqual(self.send(target).json()["outcome"], "submitted")
        self.assertIsNone(self.submitter.calls[0]["should_continue"], "only the automatic path asks about the pause")

    def test_a_company_with_only_a_form_gets_an_automatic_draft(self):
        target = self.target(email_body="", email_subject="", location="Austin, TX")
        with closing(connect_product(self.platform_path)) as conn:
            conn.execute("UPDATE outreach_targets SET location_basis='manual'")
            conn.commit()
            self.assertIn(target["id"], draft_due(conn, user_id=USER))


class FormReplyTests(unittest.TestCase):
    """Replies to a message sent through a form are read from Gmail by the company's domain."""

    setUp = ReplyCaptureTests.setUp
    tearDown = ReplyCaptureTests.tearDown
    connect = ReplyCaptureTests.connect
    arrive = ReplyCaptureTests.arrive
    check = ReplyCaptureTests.check
    target = ReplyCaptureTests.target
    replies = ReplyCaptureTests.replies

    def form_target(self):
        created = self.client.post("/api/v1/outreach", headers=AUTH_INBOX, json={
            "company": "Bovi", "website": "https://bovi.example",
            "email_subject": "Robotics internship question", "email_body": "Hi Bovi team,\n\nShort note.\n\nSam",
        }).json()
        with closing(connect_product(self.platform_path)) as conn:
            conn.execute(
                "INSERT INTO outreach_contact_forms(target_id, user_id, page_url, state, found_at, updated_at) VALUES(?, ?, ?, 'submitted', ?, ?)",
                (created["id"], USER, "https://bovi.example/contact", utc_now(), utc_now()),
            )
            conn.execute(
                "INSERT INTO outreach_events(id, target_id, user_id, event_type, detail, created_at) VALUES(?, ?, ?, 'form_submitted', '{}', ?)",
                (f"form-{created['id']}", created["id"], USER, utc_now()),
            )
            conn.execute("UPDATE outreach_targets SET status='sent', sent_at=? WHERE id=?", (datetime.now(timezone.utc).date().isoformat(), created["id"]))
            conn.commit()
        return created

    def test_a_fresh_email_from_their_domain_is_a_reply_but_the_form_receipt_is_not(self):
        target = self.form_target()
        self.arrive("ack-1", mail("Thanks for contacting Bovi. We received your message.", sender="Bovi <hello@bovi.example>",
                                  subject="We received your message"), received=now_ms(timedelta(minutes=1)))
        result = self.check()
        self.assertEqual((result["replies"], len(result["automatic"])), ([], 1))
        self.assertEqual(self.target(target)["status"], "sent")
        self.arrive("ana-1", mail("Thank you for reaching out! Could you talk Tuesday?", sender="Ana <ana@bovi.example>",
                                  subject="Internship"), received=now_ms(timedelta(hours=3)))
        self.assertEqual(len(self.check()["replies"]), 1)
        self.assertEqual(self.target(target)["status"], "replied")


    def test_a_reply_from_another_domain_the_company_mails_from_is_captured(self):
        # Persona AI: website persona.ai, email addresses at personainc.ai.
        target = self.form_target()
        with closing(connect_product(self.platform_path)) as conn:
            conn.execute("UPDATE outreach_targets SET mail_domains_json=? WHERE id=?", ('["boviinc.example"]', target["id"]))
            conn.commit()
        self.arrive("ana-2", mail("Thanks for writing. Could you talk Thursday?", sender="Ana <ana@boviinc.example>",
                                  subject="Your note"), received=now_ms(timedelta(hours=2)))
        self.arrive("other-1", mail("Unrelated", sender="Sam <sam@unrelated.example>", subject="Hi"), received=now_ms(timedelta(hours=2)))
        replies = self.check()["replies"]
        self.assertEqual([(item["company"], item["from"]) for item in replies], [("Bovi", "ana@boviinc.example")])
        self.assertEqual(self.target(target)["status"], "replied")


class CompanyMailDomainTests(unittest.TestCase):
    def test_only_domains_sharing_the_company_name_count(self):
        footer = _Page("https://persona.test/", (
            '<footer><a href="mailto:investors@personainc.test">investors@personainc.test</a> '
            "PR@personainc.test hello@persona.test jobs@careers.persona.test "
            "support@webflow.test someone@gmail.com</footer>"
        )).record
        from opportunity_app.outreach_contacts import company_mail_domains

        self.assertEqual(company_mail_domains([footer], "persona.test"), ["personainc.test"])

    def test_the_contact_search_keeps_them_on_the_company(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        _, platform = build_and_migrate(Path(tempdir.name))
        site = {"persona.test": {"/robots.txt": "", "/": "<footer>investors@personainc.test</footer>"}}
        transport, _requested = site_transport(site)
        with closing(connect_product(platform)) as conn, httpx.Client(transport=transport) as client:
            target = create_target(conn, {"company": "Persona", "website": "https://persona.test"}, user_id=USER)
            find_contacts(conn, target["id"], user_id=USER, fetcher=safe_fetcher(client), delay=0)
            self.assertEqual(get_target(conn, target["id"], user_id=USER)["mail_domains"], ["personainc.test"])


AUTH_INBOX = {"Authorization": "Bearer inbox-owner"}


# --- In a real browser ----------------------------------------------------------------------

def _chromium_available():
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            playwright.chromium.launch().close()
        return True
    except Exception:  # noqa: BLE001 - no package or no browser: the browser tests skip
        return False


CHROMIUM = _chromium_available()
# A draft as the drafter writes one: paragraphs with blank lines between them,
# curly apostrophes, an em dash, and accented letters.
LETTER = (
    "Hi Bovi team,\n\n"
    "I’m Sam Rivera, a mechanical engineering student — I build small robot arms and read about "
    "Bovi’s gripper work at José’s talk last month.\n\n"
    "Would you have 15 minutes for a call next week? I’d like to ask about summer internships.\n\n"
    "Thanks,\nSam Rivera"
)
THANKS = "<html><body><h1>Thanks! Your message has been sent. We'll be in touch.</h1></body></html>"
PLAIN_FORM = """
<html><head><meta charset="utf-8"></head><body><p>We'll get back to you within a week.</p>
<form action="/send" method="post">
  <label>Name <input name="name" required></label>
  <label>Email <input type="email" name="email" required></label>
  <input name="website" style="position:absolute;left:-9999px" tabindex="-1" autocomplete="off">
  <label>Message <textarea name="message" required></textarea></label>
  <label><input type="checkbox" name="consent" required> I agree to the privacy policy</label>
  <label><input type="checkbox" name="news"> Send me the newsletter</label>
  <button type="submit">Send message</button>
</form></body></html>
"""
SCRIPT_FORM = """
<html><body><div id="root"></div><script>
  const root = document.getElementById("root");
  root.innerHTML = '<div class="contact"><input placeholder="Full name" id="n"><input placeholder="Email" id="e" type="email">'
    + '<textarea placeholder="Message" id="m"></textarea><button id="go">Send</button></div>';
  document.getElementById("go").addEventListener("click", async () => {
    await fetch("/api/contact", {method: "POST", body: JSON.stringify({
      name: document.getElementById("n").value, email: document.getElementById("e").value, message: document.getElementById("m").value})});
    root.innerHTML = "<p>Thank you for your message! We'll reply soon.</p>";
  });
</script></body></html>
"""
SILENT_FORM = PLAIN_FORM.replace('action="/send"', 'action="/quiet"')
BUDGET_FORM = PLAIN_FORM.replace("<button", '<label>Annual budget <input name="budget" required></label><button')
CAPTCHA_FORM = PLAIN_FORM.replace(
    "<button", '<iframe src="/recaptcha/api2/anchor?k=x" width="300" height="80"></iframe>'
    '<textarea name="g-recaptcha-response" style="display:none"></textarea><button',
)
ANCHOR_SOLVES = """<html><body><div id="recaptcha-anchor" role="checkbox" style="width:30px;height:30px;border:1px solid"
  onclick="parent.document.querySelector('[name=g-recaptcha-response]').value='token-'.padEnd(60,'x')"></div></body></html>"""
ANCHOR_CHALLENGES = """<html><body><div id="recaptcha-anchor" role="checkbox" style="width:30px;height:30px;border:1px solid"></div></body></html>"""


class Site:
    """Serves fixture pages to the browser in place of the network, and records what was posted."""

    def __init__(self, pages):
        self.pages = pages
        self.posts = []

    def route(self, route):
        request = route.request
        path = urlsplit(request.url).path
        if request.method == "POST":
            self.posts.append((path, request.post_data or ""))
        if path == "/send":
            route.fulfill(status=200, content_type="text/html", body=THANKS)
        elif path == "/quiet":
            route.fulfill(status=200, content_type="text/html", body=PLAIN_FORM)
        elif path == "/api/contact":
            route.fulfill(status=200, content_type="application/json", body="{}")
        elif path in self.pages:
            route.fulfill(status=200, content_type="text/html", body=self.pages[path])
        else:
            route.fulfill(status=404, body="")


@unittest.skipUnless(CHROMIUM, "Playwright's Chromium is not installed")
class BrowserSubmitTests(unittest.TestCase):
    def submit(self, page, *, anchor=ANCHOR_SOLVES, body=LETTER, screenshots=None):
        site = Site({"/contact": page, "/recaptcha/api2/anchor": anchor})
        if screenshots is None:
            tempdir = tempfile.TemporaryDirectory()
            self.addCleanup(tempdir.cleanup)
            screenshots = Path(tempdir.name)
        with FormSubmitter(route_hook=site.route, screenshot_dir=screenshots) as submitter:
            result = submitter.submit(
                "https://bovi.test/contact", identity=IDENTITY, subject="Robotics internship question",
                body=body, name="bovi",
            )
        return result, site

    def test_a_plain_form_is_filled_truthfully_and_confirmed(self):
        result, site = self.submit(PLAIN_FORM)
        self.assertEqual(result["outcome"], "submitted", result)
        self.assertIn("Your message has been sent", result["confirmation"])
        posted = parse_qs(site.posts[0][1])
        self.assertEqual(posted["name"], ["Sam Rivera"])
        self.assertEqual(posted["email"], [ACCOUNT])
        self.assertEqual(posted["consent"], ["on"])
        self.assertNotIn("news", posted, "the newsletter box stays unticked")
        self.assertEqual(posted.get("website", [""]), [""], "the hidden spam trap stays empty")
        self.assertTrue(Path(result["screenshot"]).exists())
        self.assertTrue(Path(result["filled_screenshot"]).exists(), "the filled form is kept as it looked before sending")

    def test_the_message_keeps_its_blank_lines_and_punctuation(self):
        result, site = self.submit(PLAIN_FORM)
        self.assertEqual(result["outcome"], "submitted", result)
        # Browsers send a textarea's line breaks as CRLF; nothing else may change.
        self.assertEqual(parse_qs(site.posts[0][1])["message"], [LETTER.replace("\n", "\r\n")])

    def test_a_site_script_that_flattens_the_message_stops_the_send(self):
        flattening = PLAIN_FORM.replace("</form>", "</form><script>document.querySelector('textarea').addEventListener("
                                        "'input', (e) => { e.target.value = e.target.value.replace(/\\s*\\n+\\s*/g, ' '); });</script>")
        result, site = self.submit(flattening)
        self.assertEqual(result["outcome"], "needs_you", result)
        self.assertIn("blank lines between paragraphs", result["note"])
        self.assertEqual(site.posts, [])

    def test_a_rehearsal_fills_everything_and_sends_nothing(self):
        # This page also posts each keystroke, as lead-capture scripts do before anyone presses send.
        eager = PLAIN_FORM.replace("</form>", "</form><script>document.querySelector('textarea').addEventListener("
                                   "'input', (e) => fetch('/partial', {method: 'POST', body: e.target.value}));</script>")
        site = Site({"/contact": eager})
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        with FormSubmitter(route_hook=site.route, screenshot_dir=Path(tempdir.name), rehearse=True) as submitter:
            result = submitter.submit("https://bovi.test/contact", identity=IDENTITY, subject="s", body=LETTER, name="bovi")
        self.assertEqual(result["outcome"], "rehearsed", result)
        self.assertIn("Send message", result["note"])
        self.assertTrue(Path(result["filled_screenshot"]).exists())
        self.assertEqual(site.posts, [], "nothing left the page, not even the keystroke capture")
        self.assertTrue(any("/partial" in refused for refused in submitter.refused))

    def test_a_pause_just_before_the_button_presses_nothing(self):
        def broken():
            raise sqlite3.OperationalError("database is locked")

        for answer in (lambda: False, broken):
            with self.subTest(answer=answer.__name__):
                site = Site({"/contact": PLAIN_FORM})
                asked = []
                tempdir = tempfile.TemporaryDirectory()
                self.addCleanup(tempdir.cleanup)
                with FormSubmitter(route_hook=site.route, screenshot_dir=Path(tempdir.name)) as submitter:
                    result = submitter.submit(
                        "https://bovi.test/contact", identity=IDENTITY, subject="s", body=LETTER, name="bovi",
                        should_continue=lambda: asked.append(1) or answer(),
                    )
                self.assertEqual((result["outcome"], result["note"], result.get("paused")),
                                 ("failed", outreach_forms.PAUSED_BEFORE_SENDING, True), result)
                self.assertEqual(asked, [1], "asked once, after filling, before the button")
                self.assertEqual(site.posts, [], "a check that fails counts as paused: nothing is sent")

    def test_a_single_line_message_box_is_refused(self):
        single = PLAIN_FORM.replace('<textarea name="message" required></textarea>', '<input name="message" placeholder="Your message" required>')
        result, site = self.submit(single)
        self.assertEqual(result["outcome"], "needs_you", result)
        self.assertIn("paragraphs would run together", result["note"])
        self.assertEqual(site.posts, [])

    def test_a_script_built_form_without_a_form_element_is_sent(self):
        result, site = self.submit(SCRIPT_FORM)
        self.assertEqual(result["outcome"], "submitted", result)
        self.assertEqual(json.loads(site.posts[0][1])["name"], "Sam Rivera")

    def test_a_question_it_cannot_answer_sends_nothing(self):
        result, site = self.submit(BUDGET_FORM)
        self.assertEqual(result["outcome"], "needs_you")
        self.assertIn("Annual budget", result["note"])
        self.assertEqual(site.posts, [])

    def test_a_send_the_page_does_not_confirm_is_unconfirmed(self):
        with mock.patch.object(outreach_forms, "CONFIRM_WAIT_SECONDS", 3):
            result, site = self.submit(SILENT_FORM)
        self.assertEqual(result["outcome"], "unconfirmed", result)
        self.assertEqual(len(site.posts), 1)

    def test_a_checkbox_captcha_is_ticked_and_the_form_goes(self):
        result, site = self.submit(CAPTCHA_FORM)
        self.assertEqual(result["outcome"], "submitted", result)
        self.assertTrue(parse_qs(site.posts[-1][1])["g-recaptcha-response"][0].startswith("token-"))

    def test_a_captcha_that_asks_for_a_challenge_waits_for_the_student(self):
        with mock.patch.object(outreach_forms, "CAPTCHA_WAIT_SECONDS", 1):
            result, site = self.submit(CAPTCHA_FORM, anchor=ANCHOR_CHALLENGES)
        self.assertEqual(result["outcome"], "needs_you")
        self.assertIn("Finish in browser", result["note"])
        self.assertEqual([post for post in site.posts if post[0] == "/send"], [])


class _Page:
    def __init__(self, url, raw):
        from opportunity_app.outreach_contacts import _PageParser

        self.parser = _PageParser()
        self.parser.feed(raw)
        self.parser.close()
        self.record = {"url": url, "raw": raw, "parser": self.parser}


class RealWorldFindingTests(unittest.TestCase):
    """Shapes met on the student's real companies' sites (2026-09-26 rehearsal of 97 sites)."""

    def test_a_hubspot_form_on_a_regional_host_is_found(self):
        page = '<script src="https://js-na2.hsforms.net/forms/embed/245932403.js" defer></script>'
        self.assertEqual(len(contact_forms("https://singularity.test/", page)), 1)

    def test_fields_with_no_form_element_around_them_are_found(self):
        page = '<div class="contact"><input placeholder="Email" type="email"><textarea placeholder="Message"></textarea><button>Send</button></div>'
        self.assertEqual(contact_forms("https://warhead.test/contact", page)[0]["fields"], [])

    def test_the_contact_page_beyond_the_crawl_is_fetched(self):
        home = _Page("https://carbon.test/", '<a href="/contact">Contact us</a><a href="/team">Team</a>').record
        transport, requested = site_transport({"carbon.test": {"/robots.txt": "", "/contact": CONTACT_PAGE}})
        with httpx.Client(transport=transport) as client:
            form = outreach_forms.find_contact_form([home], fetcher=safe_fetcher(client))
        self.assertEqual(form["page_url"], "https://carbon.test/contact")

    def test_the_contact_page_form_beats_a_homepage_pop_up(self):
        # Anvil: a hidden pop-up form on the homepage, the real one on /contact.
        home = _Page("https://anvil.test/", '<a href="/contact">Contact</a>' + CONTACT_PAGE.replace('id="contact-form"', 'id="popup"')).record
        transport, _requested = site_transport({"anvil.test": {"/robots.txt": "", "/contact": CONTACT_PAGE}})
        with httpx.Client(transport=transport) as client:
            form = outreach_forms.find_contact_form([home], fetcher=safe_fetcher(client))
        self.assertEqual(form["page_url"], "https://anvil.test/contact")

    def test_a_form_built_by_scripts_is_found_by_rendering_the_contact_page(self):
        home = _Page("https://aurelius.test/", '<a href="/contact">Contact</a>').record

        class Renderer:
            def __init__(self):
                self.asked = []

            def render(self, url, *, styles=False):
                self.asked.append(url)
                assert styles, "a form search loads the page's styles"
                return url, CONTACT_PAGE

        transport, _requested = site_transport({"aurelius.test": {"/robots.txt": "", "/contact": "<div id='root'></div>"}})
        renderer = Renderer()
        with httpx.Client(transport=transport) as client:
            form = outreach_forms.find_contact_form([home], fetcher=safe_fetcher(client), renderer=renderer)
        self.assertEqual((form["page_url"], renderer.asked), ("https://aurelius.test/contact", ["https://aurelius.test/contact"]))


class RealWorldPlanTests(unittest.TestCase):
    def plan(self, fields):
        return plan_fill(fields, IDENTITY, subject="Robotics internship question", body="Hi team,\n\nA note.\n\nSam")

    def base(self):
        return [field(0, "email", label="Email", name="email", required=True),
                field(1, "textarea", tag="textarea", label="Message", name="message", required=True)]

    def test_a_country_or_state_is_never_chosen_for_the_student(self):
        plan = self.plan(self.base() + [
            field(2, tag="select", type_="select-one", label="Country/Region", name="country", required=True,
                  options=[{"value": "", "text": "Please Select"}, {"value": "us", "text": "United States"}, {"value": "o", "text": "Other"}]),
        ])
        self.assertEqual(plan["problems"], ['The form requires "Country/Region", and your confirmed profile has no answer for it'])

    def test_how_did_you_hear_is_answered_only_with_other(self):
        heard = lambda options: field(2, tag="select", type_="select-one", label="How Did You Hear About Us?*", name="heard",  # noqa: E731
                                      required=True, options=options)
        with_other = self.plan(self.base() + [heard([{"value": "li", "text": "LinkedIn"}, {"value": "o", "text": "Other"}])])
        self.assertEqual({fill["role"]: fill["value"] for fill in with_other["fills"]}["select"], "o")
        without = self.plan(self.base() + [heard([{"value": "li", "text": "LinkedIn"}, {"value": "g", "text": "Google"}])])
        self.assertEqual(len(without["problems"]), 1)

    def with_hint(self, index, hint, **values):
        return {**field(index, **values), "autocomplete": hint}

    def test_autofill_hints_are_read_exactly(self):
        # "country-name" is not a name and "organization-title" is not a company (Persona, Atlas).
        plan = self.plan(self.base() + [
            self.with_hint(2, "country-name", label="Country/Region", name="country"),
            self.with_hint(3, "organization-title", label="Role", name="role", required=True),
            self.with_hint(4, "organization", label="Company", name="company", required=True),
        ])
        values = {fill["index"]: fill["value"] for fill in plan["fills"]}
        self.assertNotIn(2, values, "a country is never filled")
        self.assertEqual((values[3], values[4]), ("Student", "State University"))

    def test_email_in_a_placeholder_does_not_make_an_email_field(self):
        # Tensr: "Topic", placeholder "Subject of your email".
        topic = {**field(2, label="Topic", name="topic", required=True), "placeholder": "Subject of your email"}
        values = {fill["index"]: fill["role"] for fill in self.plan(self.base() + [topic])["fills"]}
        self.assertEqual(values[2], "subject")

    def test_a_list_showing_an_answer_is_chosen_honestly_or_stops(self):
        who = lambda options, selected: {**field(2, tag="select", type_="select-one", label="I'm reaching out as", name="as", options=options),  # noqa: E731
                                         "selected": selected}
        chosen = self.plan(self.base() + [who([{"value": "c", "text": "Customer (production work)"}, {"value": "s", "text": "Student"}], "c")])
        self.assertEqual({fill["index"]: fill["value"] for fill in chosen["fills"]}[2], "s")
        stuck = self.plan(self.base() + [who([{"value": "c", "text": "Customer"}, {"value": "o", "text": "OEM engineer"}], "c")])
        self.assertEqual(len(stuck["problems"]), 1)
        self.assertIn('shows "Customer"', stuck["problems"][0])

    def test_options_that_claim_too_much_are_never_chosen(self):
        # Kinetiq/Simora "Researcher / academic", Micora "Project inquiry", Critical Materials "International Partnership Inquiry".
        ask = lambda options: {**field(2, tag="select", type_="select-one", label="What's this about?", name="about", required=True,  # noqa: E731
                                        options=[{"value": str(i), "text": text} for i, text in enumerate(options, 1)]), "selected": ""}
        for options in (["Customer", "Researcher / academic"], ["Project inquiry", "Press"], ["International Partnership Inquiry", "Media"]):
            with self.subTest(options=options):
                self.assertEqual(len(self.plan(self.base() + [ask(options)])["problems"]), 1)
        chosen = self.plan(self.base() + [ask(["Project inquiry", "General inquiry", "Careers"])])
        self.assertEqual([fill["label"] for fill in chosen["fills"] if fill["index"] == 2], ["What's this about?: Careers"])

    def test_notes_name_each_missing_answer_once_in_plain_words(self):
        plan = plan_fill(self.base() + [
            field(2, "tel", label="Phone*", name="phone", required=True), field(3, "tel", name="phone", required=True),
            field(4, label="Address Line 1(required)", name="a1", required=True),
        ], {**IDENTITY, "phone": ""}, subject="s", body="b")
        self.assertEqual(plan["problems"], [
            'The form requires "Phone", and your confirmed profile has no answer for it',
            'The form requires "Address Line 1", and your confirmed profile has no answer for it',
        ])

    def test_a_star_marks_a_required_field(self):
        plan = self.plan(self.base() + [field(2, "checkbox", label="I agree to the privacy policy*", name="privacy")])
        self.assertEqual([fill["action"] for fill in plan["fills"] if fill["index"] == 2], ["check"])

    def test_a_one_line_topic_beside_a_message_box_gets_the_subject(self):
        # Mara: the draft went into "Inquiry topic" instead of "Message".
        plan = self.plan(self.base() + [field(2, label="Inquiry topic", name="topic")])
        roles = {fill["index"]: fill["role"] for fill in plan["fills"]}
        self.assertEqual((roles[1], roles[2], plan["problems"]), ("message", "subject", []))

    def test_name_beside_a_last_name_field_gets_the_first_name(self):
        plan = self.plan(self.base() + [field(2, label="Name", name="name", required=True), field(3, label="Last Name", name="last", required=True)])
        values = {fill["index"]: fill["value"] for fill in plan["fills"]}
        self.assertEqual((values[2], values[3]), ("Sam", "Rivera"))


LABEL_GRID_FORM = """<html><head><meta charset="utf-8"></head><body>
<form action="/send" method="post"><div class="grid">
  <div>Name</div><input name="a" required>
  <div>Company / Affiliation</div><input name="b">
  <div>Email</div><input name="c" type="email" required>
  <div>Topic</div><input name="d">
  <div>Country/Region</div><input name="e">
  <div>Role</div><input name="f">
</div>
<div class="field"><label>State<select name="state"><option value="">Please select...</option><option>Alabama</option></select></label></div>
<div class="field"><span>Message</span><textarea name="message" required></textarea></div>
<button type="submit">Send</button></form></body></html>"""
WORDPRESS_FORM = PLAIN_FORM.replace('<form action="/send" method="post">',
                                    '<form action="/send" method="post" class="contact-form commentsblock jetpack-contact-form__form">')
HIDDEN_FORM = PLAIN_FORM.replace('<form action="/send"', '<form style="display:none" action="/send"')


@unittest.skipUnless(CHROMIUM, "Playwright's Chromium is not installed")
class RealWorldBrowserTests(unittest.TestCase):
    def rehearse(self, page):
        site = Site({"/contact": page})
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        with FormSubmitter(route_hook=site.route, screenshot_dir=Path(tempdir.name), rehearse=True) as submitter:
            result = submitter.submit("https://bovi.test/contact", identity=IDENTITY, subject="Robotics internship question",
                                      body=LETTER, name="bovi")
        self.assertEqual(site.posts, [])
        return result

    def test_labels_come_from_each_fields_own_text_not_its_neighbours(self):
        result = self.rehearse(LABEL_GRID_FORM)
        self.assertEqual(result["outcome"], "rehearsed", result)
        # Topic gets the subject, and the country, role, and company stay empty: nothing is guessed.
        self.assertEqual(result["filled"], ["Name", "Email", "Topic", "Message"])

    def test_a_wordpress_contact_form_is_not_mistaken_for_a_comment_form(self):
        self.assertEqual(self.rehearse(WORDPRESS_FORM)["outcome"], "rehearsed")

    def test_a_contact_form_that_also_offers_demos_is_still_a_contact_form(self):
        # Libra: "Contact Us / Book a Demo" above a plain form; Anvil: "Request a quote" only as a dropdown option.
        both = PLAIN_FORM.replace("<form", "<h2>Contact Us / Book a Demo</h2><form", 1)
        self.assertEqual(self.rehearse(both)["outcome"], "rehearsed")
        purpose = PLAIN_FORM.replace("<button", '<label>Purpose <select name="p"><option>Request a quote</option><option>General question</option></select></label><button')
        self.assertEqual(self.rehearse(purpose)["outcome"], "rehearsed")

    def test_a_choice_that_takes_the_form_away_is_swapped_for_the_next_honest_one(self):
        # Skyways: "Career Opportunities" replaces the form with a pointer to their jobs page.
        page = """<html><head><meta charset="utf-8"></head><body><form action="/send" method="post">
          <label>Inquiry type * <select name="kind" required><option value="">Select</option><option>General Inquiry</option>
            <option>Career Opportunities</option></select></label>
          <div id="rest"><label>Name <input name="name" required></label><label>Email <input type="email" name="email" required></label>
          <label>Message <textarea name="message" required></textarea></label><button type="submit">Send</button></div>
          <p id="jobs" style="display:none">See our careers page.</p></form>
          <script>document.querySelector("select").addEventListener("change", (e) => {
            const jobs = e.target.value === "Career Opportunities";
            document.getElementById("rest").style.display = jobs ? "none" : "";
            document.getElementById("jobs").style.display = jobs ? "" : "none"; });</script></body></html>"""
        result = self.rehearse(page)
        self.assertEqual(result["outcome"], "rehearsed", result)
        self.assertIn("Inquiry type: General Inquiry", result["filled"])

    def test_the_resume_counts_as_attached_only_when_the_form_has_a_file_field(self):
        resume = Path(tempfile.mkdtemp()) / "Resume.pdf"
        resume.write_bytes(b"%PDF-1.4 resume")
        self.addCleanup(lambda: resume.unlink(missing_ok=True))
        with_file = PLAIN_FORM.replace("<button", '<label>Resume <input type="file" name="cv" accept=".pdf"></label><button')
        for page, expected in ((PLAIN_FORM, ""), (with_file, "Resume.pdf")):
            site = Site({"/contact": page})
            with tempfile.TemporaryDirectory() as shots, FormSubmitter(route_hook=site.route, screenshot_dir=Path(shots), rehearse=True) as submitter:
                result = submitter.submit("https://bovi.test/contact", identity=IDENTITY, subject="s", body=LETTER,
                                          attachment=str(resume), name="bovi")
            self.assertEqual((result["outcome"], result["attached"]), ("rehearsed", expected))

    def test_a_demo_request_form_is_not_used_for_an_email(self):
        demo = PLAIN_FORM.replace("<form", "<h2>Request a demo</h2><form", 1)
        result = self.rehearse(demo)
        self.assertEqual(result["outcome"], "needs_you")
        self.assertIn("Request a demo", result["note"])

    def test_a_hidden_form_is_not_filled_blind(self):
        result = self.rehearse(HIDDEN_FORM)
        self.assertEqual(result["outcome"], "failed")
        self.assertIn("hidden", result["note"])


if __name__ == "__main__":
    unittest.main()
