"""Contact forms for companies with no email: found by the crawl, filled truthfully, sent once."""

import json
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR
from opportunity_app.outreach import forms as outreach_forms
from opportunity_app.automation import ledger as automation, health as automation_health
from opportunity_app.api import create_app
from opportunity_app.outreach.targets import create_target, get_target
from opportunity_app.outreach.automation import AutomationWorker, draft_due, send_form, update_settings
from opportunity_app.outreach.contacts import find_contacts
from opportunity_app.outreach.forms import (
    FormSubmitter,
    contact_forms,
    formatting_problem,
    form_due,
    is_acknowledgement,
    plan_fill,
    submit_contact_form,
)
from opportunity_app.core.database import connect_product
from opportunity_app.core.timestamps import utc_now
from opportunity_app.student.profile import update_profile

from browser_support import requires_chromium
from helpers_platform import build_and_migrate
from helpers_outreach import confirm_facts, safe_fetcher, site_transport
from helpers_gmail import ACCOUNT, ReplyCaptureFixture, mail, now_ms

AUTH = {"Authorization": "Bearer forms-owner"}
USER = "local-user"
IDENTITY = {"name": "Sam Rivera", "email": ACCOUNT, "phone": "512-555-0100", "school": "State University", "link": "", "title": "Student"}
# A made-up mailing address, as identity_for would pass it on from a confirmed profile.
ADDRESS = {"address_line1": "12 Example Lane", "address_line2": "Apt 4", "city": "Riverton", "state": "Oregon",
           "postal_code": "97000", "country": "United States"}
ADDRESS_POINTER = "Add your mailing address on the Profile page, under About you, and the app can fill in address boxes"
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

    def test_each_box_a_problem_leaves_empty_is_named_for_finish_in_browser(self):
        plan = self.plan(self.fields(
            field(5, label="Annual budget", name="budget", required=True),
            field(6, "checkbox", label="I am a current customer", name="customer", required=True),
        ), body="x" * 2500)
        self.assertEqual(plan["left"], [{"index": 4, "label": "How can we help?", "role": "message"},
                                        {"index": 5, "label": "Annual budget", "role": "unknown"},
                                        {"index": 6, "label": "I am a current customer", "role": "checkbox"}])
        self.assertEqual(self.plan(self.fields())["left"], [])

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
        # Runs as the button is pressed, once the check before it said go on.
        self.at_click = lambda: None

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
        if outcome == "student_press":
            # Finish in browser: the student presses the form's own button, and the app is told as they do.
            kwargs["on_press"]()
            self.at_click()
            outcome = "submitted"
        if outcome == "pause_at_button":
            # As FormSubmitter does: asked just before the button, and stopped there.
            self.before_button()
            if should_continue is not None and not should_continue():
                return {"outcome": "failed", "note": outreach_forms.PAUSED_BEFORE_SENDING, "confirmation": "", "filled": [],
                        "screenshot": "", "attached": "", "paused": True}
            self.at_click()
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

    def test_only_a_confirmed_mailing_address_reaches_the_form(self):
        keys = ("address_line1", "address_line2", "city", "state", "postal_code", "country")
        self.submitter.outcomes = ["submitted", "submitted", "submitted"]
        self.assertEqual(self.send(self.approved()).status_code, 200)
        self.assertEqual({key: self.submitter.calls[0]["identity"][key] for key in keys}, dict.fromkeys(keys, ""), "no address on file, none sent")
        address = {"address_line1": " 12  Example Lane ", "city": "Riverton", "state": "Oregon", "postal_code": "97000", "country": "United States"}
        with closing(connect_product(self.platform_path)) as conn:
            confirm_facts(conn, contact={"phone": "512-555-0100", **address})
        self.assertEqual(self.send(self.approved(company="Orbit")).status_code, 200)
        self.assertEqual({key: self.submitter.calls[1]["identity"][key] for key in keys}, {
            "address_line1": "12 Example Lane", "address_line2": "", "city": "Riverton", "state": "Oregon", "postal_code": "97000",
            "country": "United States",
        }, "stray spaces are tidied, nothing else is changed")
        # Edited but not confirmed again is not a confirmed answer.
        with closing(connect_product(self.platform_path)) as conn:
            update_profile(conn, {"contact": {"phone": "512-555-0100", **address}}, [], user_id=USER)
        self.assertEqual(self.send(self.approved(company="Vega")).status_code, 200)
        self.assertEqual({key: self.submitter.calls[2]["identity"][key] for key in keys}, dict.fromkeys(keys, ""))

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

    def claim_state(self, conn):
        row = conn.execute("SELECT state FROM outreach_send_claims").fetchone()
        return None if row is None else row[0]

    def test_a_pause_after_the_hand_over_reports_the_form_as_on_its_way(self):
        self.submitter.outcomes = ["pause_at_button"]
        target = self.approved()
        seen = {}

        def pause_as_it_clicks():
            with closing(connect_product(self.platform_path)) as other:
                seen["claim"] = self.claim_state(other)
                seen["pause"] = automation.set_paused(other, USER, True)

        self.submitter.at_click = pause_as_it_clicks
        with closing(connect_product(self.platform_path)) as conn:
            result = send_form(conn, target["id"], user_id=USER, submitter_factory=self.submitter.factory)
        self.assertEqual(result["outcome"], "submitted", "handed over before the pause, so it went")
        self.assertEqual(seen["claim"], "clicking", "the claim was handed over before the button was pressed")
        [flight] = seen["pause"]["in_flight"]
        self.assertEqual((flight["source"], flight["action"], flight["target_id"], flight["company"]),
                         ("form_claim", "form", target["id"], "Bovi"))
        with closing(connect_product(self.platform_path)) as conn:
            self.assertEqual(self.claim_state(conn), "sent", "settled from the handed-over state")
            self.assertEqual(automation.in_flight(conn, USER), [])

    def test_a_pause_cannot_land_between_the_last_check_and_the_hand_over(self):
        self.submitter.outcomes = ["pause_at_button"]
        target = self.approved()
        real = automation.pause_guard
        calls, seen = [], {}

        def guard(conn, user_id):
            answer = real(conn, user_id)
            calls.append(1)
            if len(calls) == 2:  # the check just before the button (the first is the claim's)
                with closing(connect_product(self.platform_path)) as other:
                    other.execute("PRAGMA busy_timeout = 50")
                    try:
                        automation.set_paused(other, USER, True)
                        seen["pause"] = "landed"
                    except sqlite3.OperationalError as exc:
                        seen["pause"] = str(exc)
            return answer

        with mock.patch.object(automation, "pause_guard", guard), closing(connect_product(self.platform_path)) as conn:
            result = send_form(conn, target["id"], user_id=USER, submitter_factory=self.submitter.factory)
        self.assertEqual(seen["pause"], "database is locked", "a pause waits for the hand-over instead of slipping in after the check")
        self.assertEqual(result["outcome"], "submitted")

    def test_a_claim_left_mid_click_is_treated_as_possibly_sent(self):
        self.submitter.outcomes = ["submitted"]
        target = self.approved()
        long_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="microseconds")
        with closing(connect_product(self.platform_path)) as conn:
            conn.execute(
                "INSERT INTO outreach_send_claims(target_id, user_id, kind, token, state, action, instance, claimed_at) "
                "VALUES(?, ?, 'initial', 'dead', 'clicking', 'form', 'a-process-that-stopped', ?)",
                (target["id"], USER, long_ago),
            )
            conn.commit()
            self.assertEqual([item["target_id"] for item in automation_health.health_summary(conn, USER)["unconfirmed"]], [target["id"]])
        blocked = self.send(target)
        self.assertEqual(blocked.status_code, 428, "it may have gone: the student looks before it is sent again")
        self.assertEqual(self.submitter.calls, [])
        self.assertEqual(self.send(target, retry_unconfirmed=True).json()["outcome"], "submitted")

    def test_a_claim_being_clicked_right_now_is_not_taken_over(self):
        target = self.approved()
        with closing(connect_product(self.platform_path)) as conn:
            conn.execute(
                "INSERT INTO outreach_send_claims(target_id, user_id, kind, token, state, action, instance, claimed_at) "
                "VALUES(?, ?, 'initial', 'busy', 'clicking', 'form', 'another-process', ?)",
                (target["id"], USER, utc_now()),
            )
            conn.commit()
        held = self.send(target, retry_unconfirmed=True)
        self.assertEqual(held.status_code, 409)
        self.assertIn("Finish in browser window", held.json()["detail"], "about the form, not about Gmail")
        self.assertEqual(self.submitter.calls, [])

    def test_the_students_own_form_send_is_not_paused(self):
        self.submitter.outcomes = ["submitted"]
        target = self.approved()
        with closing(connect_product(self.platform_path)) as conn:
            automation.set_paused(conn, USER, True)
        self.assertEqual(self.send(target).json()["outcome"], "submitted")
        self.assertIsNone(self.submitter.calls[0]["should_continue"], "only the automatic path asks about the pause")

    def test_finish_in_browser_holds_the_claim_as_on_its_way_from_the_students_press(self):
        self.submitter.outcomes = ["needs_you", "student_press"]
        states = []

        def at_press():
            with closing(connect_product(self.platform_path)) as conn:
                states.append(conn.execute("SELECT state FROM outreach_send_claims").fetchone()["state"])
                states.append(conn.execute("SELECT state FROM outreach_contact_forms").fetchone()["state"])

        self.submitter.at_click = at_press
        target = self.approved()
        self.send(target)
        self.assertIsNone(self.submitter.calls[0].get("on_press"), "the app's own press needs no word of the student's")
        self.assertEqual(self.send(target, in_browser=True).json()["outcome"], "submitted")
        self.assertEqual(states, [automation.FORM_HANDED_OVER, "unconfirmed"],
                         "from their press on, a restart reads the form as possibly sent, with It arrived on the card")
        self.assertIsNone(self.submitter.calls[1]["should_continue"], "a pause never stops the student's own press")

    def test_a_company_with_only_a_form_gets_an_automatic_draft(self):
        target = self.target(email_body="", email_subject="", location="Austin, TX")
        with closing(connect_product(self.platform_path)) as conn:
            conn.execute("UPDATE outreach_targets SET location_basis='manual'")
            conn.commit()
            self.assertIn(target["id"], draft_due(conn, user_id=USER))


class FormReplyTests(ReplyCaptureFixture, unittest.TestCase):
    """Replies to a message sent through a form are read from Gmail by the company's domain."""

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
        from opportunity_app.outreach.contacts import company_mail_domains

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


# A send button that is a custom element, its own button inside a closed shadow root; it posts the form to
# another site (as a form service would) and thanks the student.
CUSTOM_SEND_FORM = BUDGET_FORM.replace('<button type="submit">Send message</button>', '<x-send role="button"></x-send>').replace("</form>", """</form><script>
  customElements.define("x-send", class extends HTMLElement {
    constructor() {
      super();
      const root = this.attachShadow({ mode: "closed" });
      root.innerHTML = "<span style='display:inline-block;padding:4px 10px;border:1px solid'>Send it</span>";
      this.addEventListener("click", async () => {
        const form = document.querySelector("form");
        await fetch("https://forms.example/submit", { method: "POST", body: new URLSearchParams(new FormData(form)) });
        form.outerHTML = "<p>Thank you for your message! We'll reply soon.</p>";
      });
    }
  });
</script>""")
# A send button outside the form it sends (form="..."), posting to another site by script.
OUTSIDE_SEND_FORM = BUDGET_FORM.replace('<form action="/send" method="post">', '<form id="contact" action="/send" method="post">').replace(
    '<button type="submit">Send message</button>', '').replace("</form>", """</form>
<button type="button" form="contact" id="out">Send message</button><script>
  document.getElementById("out").addEventListener("click", async () => {
    const form = document.getElementById("contact");
    await fetch("https://forms.example/submit", { method: "POST", body: new URLSearchParams(new FormData(form)) });
    form.outerHTML = "<p>Thank you for your message! We'll reply soon.</p>";
  });
</script>""")
# A page that redraws its form once the window is handed over (as a framework would), losing the app's marks,
# and posts it to another site.
REDRAWN_FORM = BUDGET_FORM.replace('action="/send"', 'action="https://forms.example/send"').replace("</form>", """</form><script>
  const wait = setInterval(() => {
    if (!document.querySelector("[data-pipeline-note]")) return;
    clearInterval(wait);
    const form = document.querySelector("form");
    for (const el of form.querySelectorAll("input, textarea")) {
      if (el.type === "checkbox") { if (el.checked) el.setAttribute("checked", ""); }
      else if (el.tagName === "TEXTAREA") el.textContent = el.value;
      else el.setAttribute("value", el.value);
    }
    form.outerHTML = form.outerHTML.replace(/ data-pipeline-[a-z]+="[^"]*"/g, "");
    document.body.dataset.redrawn = "1";
  }, 50);
</script>""")
# A send button that only reports the click to an analytics service and sends nothing.
BROKEN_SEND_FORM = SCRIPT_FORM.replace(
    'await fetch("/api/contact"', 'await fetch("https://analytics.example/collect", {method: "POST", body: "event=click"}); return;\n    await fetch("/api/contact"')
# A form that posts by script and then shows nothing at all: the form stays as it was.
STILL_FORM = SCRIPT_FORM.replace("""root.innerHTML = "<p>Thank you for your message! We'll reply soon.</p>";""", "")
# A page that posts each box's value as it changes: the app's own fills carry the student's details.
EARLY_POST_FORM = BUDGET_FORM.replace("</form>", "</form><script>document.querySelector('[name=name]').addEventListener("
                                      "'change', (e) => fetch('/early', {method: 'POST', body: e.target.value}));</script>")
# A page that must pass its site's own check (a POST, as Cloudflare's challenge makes) before it shows the form.
CHALLENGED_FORM = """<html><body><div id="root">Checking your browser...</div><script>
  fetch('/cdn-cgi/challenge-platform/check', {method: 'POST', body: 'ok'}).then((r) => r.ok && (document.getElementById('root').innerHTML =
    """ + json.dumps(BUDGET_FORM.split("<body>", 1)[1].split("</body>", 1)[0]) + """));
</script></body></html>"""
# A script-built form sent by GET, its values in the address, and thanked in words the app does not know.
GET_SENT_FORM = SCRIPT_FORM.replace(
    'await fetch("/api/contact", {method: "POST", body: JSON.stringify({',
    'await fetch("/mail?" + new URLSearchParams({').replace(
    "message: document.getElementById(\"m\").value})});", "message: document.getElementById(\"m\").value}));").replace(
    "Thank you for your message! We'll reply soon.", "Done.")
# A form whose send button posts a beacon to its own site on every click, as a CDN's page-speed script does.
BEACON_FORM = BUDGET_FORM.replace("</form>", "</form><script>document.querySelector('button').addEventListener("
                                  "'click', () => fetch('/cdn-cgi/rum', {method: 'POST', body: 'rum'}));</script>")
# A script-built form sent as a tracking image's address to another site, thanked in words the app does not know.
PIXEL_SENT_FORM = SCRIPT_FORM.replace(
    'await fetch("/api/contact", {method: "POST", body: JSON.stringify({',
    'new Image().src = "https://forms.example/pixel?" + new URLSearchParams({').replace(
    "message: document.getElementById(\"m\").value})});", "message: document.getElementById(\"m\").value});").replace(
    "Thank you for your message! We'll reply soon.", "Done.")
# A script-built form sent to another site with a file in it, so its body is not text.
FILE_SENT_FORM = SCRIPT_FORM.replace(
    'await fetch("/api/contact", {method: "POST", body: JSON.stringify({',
    'const data = new FormData(); data.append("email", document.getElementById("e").value);\n'
    '    data.append("file", new Blob([new Uint8Array([0xff, 0xfe, 0x00, 0x80, 0x81, 0xc3])]), "cv.pdf");\n'
    '    await fetch("https://forms.example/submit", {method: "POST", body: data}); root.innerHTML = "<p>Done.</p>"; return;\n'
    '    await fetch("/api/contact", {method: "POST", body: JSON.stringify({')
# A script-built form whose send pings its own site and thanks the student, sending nothing of the message.
PING_THANKS_FORM = SCRIPT_FORM.replace(
    'await fetch("/api/contact", {method: "POST", body: JSON.stringify({',
    'await fetch("/api/ping", {method: "POST", body: "x"}); root.innerHTML = "<p>Thank you for your message!</p>"; return;\n'
    '    await fetch("/api/contact", {method: "POST", body: JSON.stringify({')
# A script-built form that thanks the student as they press, and sends the form a second later.
EARLY_THANKS_FORM = SCRIPT_FORM.replace(
    'await fetch("/api/contact", {method: "POST", body: JSON.stringify({',
    'root.insertAdjacentHTML("beforeend", "<p>Thank you for your message!</p>");\n'
    '    await new Promise((done) => setTimeout(done, 1000));\n'
    '    await fetch("/api/contact", {method: "POST", body: JSON.stringify({').replace(
    """root.innerHTML = "<p>Thank you for your message! We'll reply soon.</p>";""", "")
# A contact form between a header with its own "Contact us" button and a footer newsletter form.
SECTIONED_FORM = BUDGET_FORM.replace("<body>", """<body><div id="app"><header><button type="button" id="cta"
  onclick="fetch('/track', {method: 'POST', body: 'cta'})">Contact us</button></header>""").replace("</body>", """<footer>
  <form action="/subscribe" method="post"><input type="email" name="nl" aria-label="Newsletter email"><button type="submit">Submit</button></form>
</footer></div></body>""")
# A script-built form whose send posts only a ping to its own site, carrying none of the student's details.
PING_FORM = SCRIPT_FORM.replace(
    'await fetch("/api/contact", {method: "POST", body: JSON.stringify({',
    'await fetch("/api/ping", {method: "POST", body: "x"}); return;\n    await fetch("/api/contact", {method: "POST", body: JSON.stringify({')


class Site:
    """Serves fixture pages to the browser in place of the network, and records what was posted."""

    def __init__(self, pages):
        self.pages = pages
        self.posts = []

    def route(self, route):
        request = route.request
        path = urlsplit(request.url).path
        if request.method == "POST":
            try:
                data = request.post_data or ""
            except UnicodeDecodeError:  # a body with a file in it
                data = (request.post_data_buffer or b"").decode("utf-8", "replace")
            self.posts.append((path, data))
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


@requires_chromium
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


@requires_chromium
class FinishInBrowserTests(unittest.TestCase):
    """Finish in browser: the app fills what it truthfully can, and the student finishes the form and presses its send button.

    Each test acts as the student through ``student_hook``, which runs once the window is theirs.
    """

    def submit(self, page, student, *, wait=20, pages=None, launch_args=None, site=None):
        # The CAPTCHA box, if the app ticked it, would solve itself: the student's own solving is then the only way past it.
        site = site or Site({"/contact": page, "/recaptcha/api2/anchor": ANCHOR_SOLVES, **(pages or {})})
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        pressed = []
        with FormSubmitter(route_hook=site.route, screenshot_dir=Path(tempdir.name), person_wait=wait, launch_args=launch_args,
                           student_hook=lambda window: student(window, site)) as submitter:
            result = submitter.submit(
                "https://bovi.test/contact", identity=IDENTITY, subject="Robotics internship question",
                body=LETTER, name="bovi", on_press=lambda: pressed.append(1),
            )
        return result, site, pressed

    def test_the_student_answers_what_the_app_cannot_and_presses_send(self):
        seen = {}

        def student(window, site):
            box = window.locator("[name=budget]")
            seen.update(posts=list(site.posts), outline=box.evaluate("(el) => getComputedStyle(el).outlineStyle"),
                        note=window.locator("[data-pipeline-note]").count(), name=window.locator("[name=name]").input_value())
            box.fill("Under $10k")
            window.get_by_role("button", name="Send message").click()

        result, site, pressed = self.submit(BUDGET_FORM, student)
        self.assertEqual(result["outcome"], "submitted", result)
        self.assertEqual(seen, {"posts": [], "outline": "solid", "note": 1, "name": "Sam Rivera"},
                         "filled as far as the app could, the box it left outlined, a note on the page, and nothing sent yet")
        posted = parse_qs(site.posts[0][1])
        self.assertEqual((posted["budget"], posted["name"]), (["Under $10k"], ["Sam Rivera"]))
        self.assertIn("Your message has been sent", result["confirmation"])
        self.assertEqual((result["by_you"], result["changed"]), (["Annual budget"], []))
        self.assertEqual(pressed, [1])

    def test_the_app_never_presses_send_and_a_window_left_alone_sends_nothing(self):
        result, site, pressed = self.submit(PLAIN_FORM, lambda window, site: None, wait=2)
        self.assertEqual(result["outcome"], "needs_you", result)
        self.assertIn("Nothing was sent", result["note"])
        self.assertEqual((site.posts, pressed), ([], []), "every box was filled, and still only the student presses send")

    def test_closing_the_window_sends_nothing(self):
        result, site, pressed = self.submit(BUDGET_FORM, lambda window, site: window.close())
        self.assertEqual(result["outcome"], "needs_you", result)
        self.assertIn("closed", result["note"])
        self.assertIn("Annual budget", result["note"], "and it still says what was left for them")
        self.assertEqual((site.posts, pressed), ([], []))

    def test_a_press_the_browser_stops_is_not_the_send(self):
        def student(window, site):
            send = window.get_by_role("button", name="Send message")
            send.click()  # the budget box is still empty, so the browser's own check stops the form
            window.locator("[name=budget]").fill("Under $10k")
            send.click()

        result, site, pressed = self.submit(BUDGET_FORM, student)
        self.assertEqual(result["outcome"], "submitted", result)
        self.assertEqual((len(site.posts), pressed), (1, [1]))

    def test_a_message_the_student_changed_is_named(self):
        def student(window, site):
            window.locator("[name=budget]").fill("Under $10k")
            window.locator("[name=message]").fill("Hi Bovi team, a shorter note.")
            window.get_by_role("button", name="Send message").click()

        result, site, _pressed = self.submit(BUDGET_FORM, student)
        self.assertEqual(result["outcome"], "submitted", result)
        self.assertEqual(result["changed"], ["Message"])
        self.assertIn("\"Message\"", result["note"])
        self.assertEqual(parse_qs(site.posts[0][1])["message"], ["Hi Bovi team, a shorter note."])

    def test_a_script_built_form_is_sent_by_the_students_click(self):
        result, site, pressed = self.submit(SCRIPT_FORM, lambda window, site: window.get_by_role("button", name="Send").click())
        self.assertEqual(result["outcome"], "submitted", result)
        self.assertEqual(json.loads(site.posts[0][1])["name"], "Sam Rivera")
        self.assertEqual(pressed, [1])

    def test_a_press_the_page_never_answers_then_a_closed_window_is_unconfirmed(self):
        def student(window, site):
            window.get_by_role("button", name="Send message").click()  # the page posts, then shows the same form again
            window.close()

        result, site, pressed = self.submit(SILENT_FORM, student)
        self.assertEqual(result["outcome"], "unconfirmed", result)
        self.assertIn("did not say it arrived", result["note"])
        self.assertEqual((len(site.posts), pressed), (1, [1]))

    def test_a_page_that_posts_while_the_student_types_sends_nothing(self):
        # Lead-capture scripts post each keystroke before anyone presses send. Nothing leaves before the press, and
        # what was held back is said in the window and kept in the history.
        eager = BUDGET_FORM.replace("</form>", "</form><script>document.querySelector('[name=budget]').addEventListener("
                                    "'input', (e) => fetch('/partial', {method: 'POST', body: e.target.value}));</script>")

        def student(window, site):
            window.locator("[name=budget]").fill("Under $10k")
            window.wait_for_timeout(300)
            window.close()

        result, site, pressed = self.submit(eager, student, pages={"/partial": "ok"})
        self.assertEqual(result["outcome"], "needs_you", result)
        self.assertIn("Nothing was sent", result["note"])
        self.assertEqual((site.posts, pressed), ([], []))
        self.assertEqual(result["held_back"], ["POST bovi.test"])

    def test_a_request_held_back_before_the_press_is_named_in_the_window(self):
        eager = BUDGET_FORM.replace("</form>", "</form><script>document.querySelector('[name=budget]').addEventListener("
                                    "'change', (e) => fetch('/check', {method: 'POST', body: e.target.value}));</script>")
        seen = []

        def student(window, site):
            window.locator("[name=budget]").fill("Under $10k")
            window.locator("[name=budget]").blur()

        # The note is redrawn by the window's own look, after the student's hook: read it from the history instead.
        result, site, _pressed = self.submit(eager, student, wait=2)
        self.assertEqual((result["outcome"], site.posts, result["held_back"]), ("needs_you", [], ["POST bovi.test"]), result)

    def test_a_page_script_can_neither_find_the_press_nor_make_one(self):
        # Once the window is handed over (the app's note is on the page), the page's own script looks for the binding,
        # then presses send itself. A script's click is not the student's, so the form it would send never leaves.
        forging = BUDGET_FORM.replace("</form>", """</form><script>
          const wait = setInterval(() => {
            if (!document.querySelector('[data-pipeline-note]')) return;
            clearInterval(wait);
            const found = Object.getOwnPropertyNames(window).filter((n) => /outreach|student/i.test(n));
            document.body.dataset.found = found.join(',') || 'none';
            document.querySelector('[name=budget]').value = 'forged';
            setTimeout(() => document.querySelector('button').click(), 300);
          }, 50);
        </script>""")
        seen = []

        def student(window, site):
            window.wait_for_function("() => document.body.dataset.found", timeout=10_000)
            seen.append(window.evaluate("() => document.body.dataset.found"))

        result, site, pressed = self.submit(forging, student, wait=6)
        self.assertEqual(seen, ["none"], "the binding is in a world of its own")
        self.assertEqual((site.posts, pressed), ([], []), "a script's press is not the student's, and nothing left")
        self.assertEqual(result["outcome"], "needs_you", result)
        self.assertEqual(result["held_back"], ["POST bovi.test"])

    def test_a_page_listener_cannot_hide_the_students_press(self):
        hiding = BUDGET_FORM.replace("<form", "<script>window.addEventListener('click', (e) => e.stopImmediatePropagation(), true);"
                                     "window.addEventListener('keydown', (e) => e.stopImmediatePropagation(), true);</script><form")

        def student(window, site):
            window.locator("[name=budget]").fill("Under $10k")
            window.locator("[name=budget]").press("Enter")

        result, site, pressed = self.submit(hiding, student)
        self.assertEqual(result["outcome"], "submitted", result)
        self.assertEqual((len(site.posts), pressed), (1, [1]))

    def test_a_form_in_a_frame_is_watched_on_the_page_s_site_and_on_another(self):
        for src, launch_args in (("/embed", None), ("https://forms.example/embed", None), ("https://forms.example/embed", ["--site-per-process"])):
            with self.subTest(src=src, launch_args=launch_args):
                outer = f'<html><body><h1>Contact us</h1><iframe src="{src}" width="700" height="500"></iframe></body></html>'

                def student(window, site):
                    inner = window.frame_locator("iframe")
                    inner.locator("[name=budget]").fill("Under $10k")
                    # In another site's process the app's own tick sometimes does not take; the note asks the student.
                    inner.locator("[name=consent]").check()
                    inner.get_by_role("button", name="Send message").click()

                result, site, pressed = self.submit(outer, student, pages={"/embed": BUDGET_FORM}, launch_args=launch_args)
                self.assertEqual(result["outcome"], "submitted", result)
                self.assertEqual((parse_qs(site.posts[0][1])["budget"], pressed), (["Under $10k"], [1]))
                self.assertEqual(result["by_you"], ["Annual budget"])

    def test_nothing_the_app_fills_leaves_before_the_students_press(self):
        def student(window, site):
            window.locator("[name=budget]").fill("Under $10k")
            window.get_by_role("button", name="Send message").click()

        result, site, _pressed = self.submit(EARLY_POST_FORM, student)
        self.assertEqual(result["outcome"], "submitted", result)
        self.assertEqual([path for path, _ in site.posts], ["/send"], "the page's post of what the app filled never left")
        self.assertTrue(result["held_back"])
        self.assertEqual(set(result["held_back"]), {"POST bovi.test"}, "each change the app made, held back")

    def test_a_page_may_pass_its_sites_own_check_before_anything_is_filled(self):
        def student(window, site):
            window.locator("[name=budget]").fill("Under $10k")
            window.get_by_role("button", name="Send message").click()

        result, site, _pressed = self.submit(CHALLENGED_FORM, student, pages={"/cdn-cgi/challenge-platform/check": "ok"})
        self.assertEqual(result["outcome"], "submitted", result)
        self.assertEqual([path for path, _ in site.posts], ["/cdn-cgi/challenge-platform/check", "/send"])

    def test_a_form_sent_by_get_is_never_called_unsent(self):
        def student(window, site):
            window.get_by_role("button", name="Send").click()
            window.wait_for_timeout(500)
            window.close()

        result, site, pressed = self.submit(GET_SENT_FORM, student, pages={"/mail": "ok"})
        self.assertEqual((result["outcome"], pressed), ("unconfirmed", [1]), result)
        self.assertIn("the form left the page", result["note"], "it carried the student's details")

    def test_a_form_sent_as_an_images_address_is_never_called_unsent(self):
        def student(window, site):
            window.get_by_role("button", name="Send").click()
            window.wait_for_timeout(500)
            window.close()

        result, site, pressed = self.submit(PIXEL_SENT_FORM, student, pages={"/pixel": "ok"})
        self.assertEqual((result["outcome"], pressed), ("unconfirmed", [1]), result)
        self.assertIn("the form left the page", result["note"])

    def test_a_form_sent_with_a_file_in_it_is_never_called_unsent(self):
        def student(window, site):
            window.get_by_role("button", name="Send").click()
            window.wait_for_timeout(500)
            window.close()

        result, site, pressed = self.submit(FILE_SENT_FORM, student, pages={"/submit": "ok"})
        self.assertEqual(([path for path, _ in site.posts], pressed), (["/submit"], [1]))
        self.assertEqual(result["outcome"], "unconfirmed", result)
        self.assertIn("the form left the page", result["note"])

    def test_a_press_the_browsers_own_check_stops_opens_nothing(self):
        # The budget box is required and empty: the browser stops the form, and the page's beacon is held back.
        def student(window, site):
            window.get_by_role("button", name="Send message").click()
            window.wait_for_timeout(500)
            window.close()

        result, site, pressed = self.submit(BEACON_FORM, student)
        self.assertEqual((result["outcome"], site.posts, pressed), ("needs_you", [], []), result)

    def test_a_request_without_the_students_details_is_not_the_form(self):
        # After the press, only a ping to the page's own site goes: it carries nothing of the message.
        def student(window, site):
            window.get_by_role("button", name="Send").click()
            window.wait_for_timeout(500)
            window.close()

        result, site, pressed = self.submit(PING_FORM, student, pages={"/api/ping": "ok"})
        self.assertEqual((result["outcome"], [path for path, _ in site.posts], pressed), ("needs_you", ["/api/ping"], [1]), result)
        self.assertIn("Nothing was sent", result["note"])

    def test_thanks_after_a_request_without_the_students_details_is_not_sent(self):
        # The page pings its own site and thanks the student, but nothing carrying the message left.
        def student(window, site):
            window.get_by_role("button", name="Send").click()

        result, site, _pressed = self.submit(PING_THANKS_FORM, student, pages={"/api/ping": "ok"}, wait=3)
        self.assertNotEqual(result["outcome"], "submitted", result)
        self.assertIn("saw nothing carrying it leave", result["note"])

    def test_thanks_before_the_form_left_is_not_its_confirmation(self):
        # The page thanks the student as they press, then sends the form a second later and says nothing more.
        def student(window, site):
            window.get_by_role("button", name="Send").click()

        result, site, _pressed = self.submit(EARLY_THANKS_FORM, student, wait=4)
        self.assertEqual([path for path, _ in site.posts], ["/api/contact"])
        self.assertEqual(result["outcome"], "unconfirmed", result)

    def test_a_custom_element_send_button_is_the_students_press(self):
        def student(window, site):
            window.locator("[name=budget]").fill("Under $10k")
            window.locator("x-send").click()

        result, site, pressed = self.submit(CUSTOM_SEND_FORM, student, pages={"/submit": "ok"})
        self.assertEqual(result["outcome"], "submitted", result)
        self.assertEqual(([path for path, _ in site.posts], pressed), (["/submit"], [1]))

    def test_thanks_from_a_page_whose_send_was_refused_is_not_called_sent(self):
        # The form service answers 404, and the page thanks the student anyway: it may not have arrived.
        def student(window, site):
            window.locator("[name=budget]").fill("Under $10k")
            window.locator("x-send").click()

        result, site, _pressed = self.submit(CUSTOM_SEND_FORM, student, wait=3)
        self.assertEqual(result["outcome"], "unconfirmed", result)
        self.assertIn("saw nothing carrying it leave", result["note"])

    def test_a_form_whose_send_button_the_app_cannot_find_is_not_handed_over(self):
        handed = []
        result, site, _pressed = self.submit(OUTSIDE_SEND_FORM, lambda window, site: handed.append(1))
        self.assertEqual((result["outcome"], handed, site.posts), ("needs_you", [], []), result)
        self.assertIn("could not find the form's send button", result["note"])

    def test_a_form_redrawn_without_the_apps_marks_is_still_sent_by_the_students_press(self):
        def student(window, site):
            window.wait_for_function("() => document.body.dataset.redrawn", timeout=5_000)
            window.locator("[name=budget]").fill("Under $10k")
            window.get_by_role("button", name="Send message").click()

        result, site, pressed = self.submit(REDRAWN_FORM, student)
        self.assertEqual(result["outcome"], "submitted", result)
        self.assertEqual(([path for path, _ in site.posts], pressed), (["/send"], [1]))

    def test_a_press_that_only_reaches_an_analytics_service_sent_nothing(self):
        def student(window, site):
            window.get_by_role("button", name="Send").click()
            window.wait_for_timeout(500)
            window.close()

        result, site, pressed = self.submit(BROKEN_SEND_FORM, student)
        self.assertEqual(result["outcome"], "needs_you", result)
        self.assertIn("Nothing was sent", result["note"])
        self.assertEqual(([path for path, _ in site.posts], pressed), (["/collect"], [1]))

    def test_a_press_that_sent_something_the_page_never_answers_is_unconfirmed(self):
        result, site, pressed = self.submit(STILL_FORM, lambda window, site: window.get_by_role("button", name="Send").click(), wait=4)
        self.assertEqual(result["outcome"], "unconfirmed", result)
        self.assertIn("did not say it arrived", result["note"])
        self.assertEqual(([path for path, _ in site.posts], pressed), (["/api/contact"], [1]))

    def test_a_message_the_app_left_for_the_student_is_named_as_theirs(self):
        short_box = BUDGET_FORM.replace('<textarea name="message" required>', '<textarea name="message" maxlength="60" required>')

        def student(window, site):
            window.locator("[name=budget]").fill("Under $10k")
            window.locator("[name=message]").fill("Hi Bovi team, may I ask about internships?")
            window.get_by_role("button", name="Send message").click()

        result, site, _pressed = self.submit(short_box, student)
        self.assertEqual(result["outcome"], "submitted", result)
        self.assertEqual(result["changed"], ["Message"])
        self.assertIn('"Message"', result["note"])

    def test_a_frame_the_app_cannot_watch_is_not_handed_over(self):
        outer = '<html><body><iframe src="https://forms.example/embed" width="700" height="500"></iframe></body></html>'
        original = FormSubmitter._watch_presses
        handed = []

        def only_the_page(submitter, target, *args):
            return original(submitter, target, *args) if hasattr(target, "main_frame") else ""

        with mock.patch.object(FormSubmitter, "_watch_presses", only_the_page):
            result, site, _pressed = self.submit(outer, lambda window, site: handed.append(1), pages={"/embed": BUDGET_FORM})
        self.assertEqual((result["outcome"], handed, site.posts), ("needs_you", [], []), result)
        self.assertIn("could not watch", result["note"])

    def test_a_listener_that_never_said_it_is_in_place_is_not_handed_over(self):
        handed = []
        with mock.patch.object(outreach_forms, "PRESS_LISTENER", "void 0;"):
            result, site, _pressed = self.submit(BUDGET_FORM, lambda window, site: handed.append(1))
        self.assertEqual((result["outcome"], handed), ("needs_you", []), result)
        self.assertIn("could not watch", result["note"])

    def test_a_press_another_sites_frame_hides_sends_nothing_and_says_so(self):
        # With one process per site (as the headed window runs), another site's frame is watched from when it is
        # found, so its own earlier listener can stop the press. The gate then keeps the form from leaving.
        hiding = BUDGET_FORM.replace("<form", "<script>window.addEventListener('click', (e) => e.stopImmediatePropagation(), true);</script><form").replace(
            'action="/send"', 'action="https://api.formhost.example/send"')
        outer = '<html><body><iframe src="https://forms.example/embed" width="700" height="500"></iframe></body></html>'
        for launch_args in (None, ["--site-per-process"]):
            with self.subTest(launch_args=launch_args):
                def student(window, site):
                    inner = window.frame_locator("iframe")
                    inner.locator("[name=budget]").fill("Under $10k")
                    inner.locator("[name=consent]").check()
                    inner.get_by_role("button", name="Send message").click()
                    window.wait_for_timeout(500)
                    window.close()

                result, site, pressed = self.submit(outer, student, pages={"/embed": hiding}, launch_args=launch_args)
                if launch_args:
                    self.assertEqual((result["outcome"], site.posts, pressed), ("needs_you", [], []), result)
                    self.assertIn("POST api.formhost.example", result["held_back"])
                else:
                    # In the page's own process the frame is watched from load, so the press is seen and the form goes.
                    self.assertEqual(([path for path, _ in site.posts], pressed), (["/send"], [1]), result)

    def test_a_press_before_the_window_is_the_students_sends_nothing(self):
        # The student presses send as the app takes its picture of the filled form, before the hand-over.
        original = FormSubmitter._screenshot

        def pressing(submitter, page, name, element=None):
            if name.endswith("-filled"):
                page.locator("[name=budget]").fill("Under $10k")
                page.get_by_role("button", name="Send message").click()
            return original(submitter, page, name, element)

        with mock.patch.object(FormSubmitter, "_screenshot", pressing):
            result, site, pressed = self.submit(BUDGET_FORM, lambda window, site: window.close())
        self.assertEqual((result["outcome"], site.posts, pressed), ("needs_you", [], []), result)
        self.assertEqual(result["held_back"], ["POST bovi.test"])

    def test_a_press_after_the_window_ends_sends_nothing(self):
        original = FormSubmitter._screenshot

        def pressing(submitter, page, name, element=None):
            if name == "bovi" and not page.is_closed():
                page.get_by_role("button", name="Send message").click()
            return original(submitter, page, name, element)

        with mock.patch.object(FormSubmitter, "_screenshot", pressing):
            result, site, pressed = self.submit(BUDGET_FORM, lambda window, site: window.locator("[name=budget]").fill("Under $10k"), wait=1)
        self.assertEqual((result["outcome"], site.posts, pressed), ("needs_you", [], []), result)

    def test_a_button_that_is_not_send_is_no_press_even_beside_reassuring_words(self):
        helpful = BUDGET_FORM.replace("<button", '<button type="button" onclick="this.nextElementSibling.hidden=false">What happens next?</button>'
                                      '<p hidden>We will respond within two business days.</p><button')

        def student(window, site):
            window.locator("[name=budget]").fill("Under $10k")
            window.get_by_role("button", name="What happens next?").click()
            window.wait_for_timeout(300)
            window.close()

        result, site, pressed = self.submit(helpful, student)
        self.assertEqual((result["outcome"], site.posts, pressed), ("needs_you", [], []), result)

    def test_the_students_send_after_a_reload_is_still_their_press(self):
        elsewhere = BUDGET_FORM.replace('action="/send"', 'action="https://forms-backend.example/send"')

        def student(window, site):
            window.reload()
            for box, value in (("name", "Sam Rivera"), ("email", ACCOUNT), ("message", "Hi Bovi team"), ("budget", "Under $10k")):
                window.locator(f"[name={box}]").fill(value)
            window.locator("[name=consent]").check()
            window.get_by_role("button", name="Send message").click()

        result, site, pressed = self.submit(elsewhere, student)
        self.assertEqual(result["outcome"], "submitted", result)
        self.assertEqual(([path for path, _ in site.posts], pressed), (["/send"], [1]))

    def test_a_form_sent_into_a_new_window_is_never_called_unsent(self):
        popup = BUDGET_FORM.replace('<form action="/send" method="post">', '<form action="/send" method="post" target="_blank">')

        def student(window, site):
            window.locator("[name=budget]").fill("Under $10k")
            window.get_by_role("button", name="Send message").click()
            window.wait_for_timeout(1_000)
            window.close()

        result, site, pressed = self.submit(popup, student)
        self.assertEqual(([path for path, _ in site.posts], pressed), (["/send"], [1]))
        self.assertEqual(result["outcome"], "unconfirmed", result)

    def test_a_confirmation_long_after_the_press_is_still_read(self):
        slow = SCRIPT_FORM.replace('document.getElementById("go").addEventListener("click", async () => {',
                                   'document.getElementById("go").addEventListener("click", async () => {\n    await new Promise((done) => setTimeout(done, 4000));')
        with mock.patch.object(outreach_forms, "CONFIRM_WAIT_SECONDS", 1):
            result, site, pressed = self.submit(slow, lambda window, site: window.get_by_role("button", name="Send").click(), wait=15)
        self.assertEqual(result["outcome"], "submitted", result)
        self.assertEqual(([path for path, _ in site.posts], pressed), (["/api/contact"], [1]))

    def test_a_form_the_server_sends_back_with_an_error_stays_the_students(self):
        # The first send comes back as the same form with an error. The window stays open (the app keeps looking), and
        # the student corrects it and sends again from there.
        retry = BUDGET_FORM.replace('action="/send"', 'action="/check"')
        corrected = BUDGET_FORM.replace("<form", "<p>Please enter a valid budget.</p><form")
        looks = []
        reads = FormSubmitter._page_text

        def look(submitter, page, frame):
            looks.append(1)
            if len(looks) == 4:  # the window is still open some looks after the first send came back
                for box, value in (("name", "Sam Rivera"), ("email", ACCOUNT), ("message", "Hi Bovi team"), ("budget", "Under $10k")):
                    page.locator(f"[name={box}]").fill(value)
                page.locator("[name=consent]").check()
                page.get_by_role("button", name="Send message").click()
            return reads(submitter, page, frame)

        def student(window, site):
            window.locator("[name=budget]").fill("lots")
            window.get_by_role("button", name="Send message").click()
            window.wait_for_load_state()

        with mock.patch.object(FormSubmitter, "_page_text", look):
            result, site, pressed = self.submit(retry, student, pages={"/check": corrected})
        self.assertEqual(result["outcome"], "submitted", result)
        self.assertEqual(([path for path, _ in site.posts], pressed), (["/check", "/send"], [1]))
        self.assertGreaterEqual(len(looks), 4)

    def test_neither_a_newsletters_submit_nor_a_headers_contact_us_is_the_press(self):
        def student(window, site):
            window.locator("[name=budget]").fill("Under $10k")
            window.locator("#cta").click()
            window.locator("[name=nl]").fill(ACCOUNT)
            # Last: held back, the newsletter's own post leaves the window on the browser's error page.
            window.locator("footer").get_by_role("button", name="Submit").click()
            window.wait_for_timeout(300)
            window.close()

        result, site, pressed = self.submit(SECTIONED_FORM, student)
        self.assertEqual((result["outcome"], site.posts, pressed), ("needs_you", [], []), result)

    def test_a_page_cannot_send_as_the_window_closes(self):
        # What a page sends as it closes is sent after the route is no longer asked, so the gate could not hold it. In the
        # window the page's own close handlers never run, a beacon is refused, and nothing is kept alive past the page.
        page = BUDGET_FORM.replace("</form>", """</form><script>
          window.ran = [];
          for (const type of ["pagehide", "unload", "visibilitychange"]) window.addEventListener(type, () => window.ran.push(type));
        </script>""")
        seen = []

        def student(window, site):
            seen.append(window.evaluate("""() => {
              for (const type of ["pagehide", "unload", "visibilitychange"]) window.dispatchEvent(new Event(type));
              return {
                ran: window.ran,
                beacon: navigator.sendBeacon("/beacon", "Sam Rivera"),
                keepalive: new Request("/keepalive", {method: "POST", body: "x", keepalive: true}).keepalive,
                peer: typeof RTCPeerConnection,
              };
            }"""))
            window.close()

        result, site, _pressed = self.submit(page, student)
        self.assertEqual(seen, [{"ran": [], "beacon": False, "keepalive": False, "peer": "undefined"}])
        self.assertEqual((result["outcome"], site.posts), ("needs_you", []), result)

    def test_a_press_reported_after_its_request_still_counts_what_went(self):
        # The press reaches the app a moment after the request it makes, as it can: the gate waits for it, and the
        # request it then lets out is counted.
        withheld = []
        deliver = FormSubmitter._on_press_binding

        def late(submitter, event, tag):
            if '"press"' in (event.get("payload") or "") and not withheld:
                withheld.append((submitter, event, tag))
                return
            deliver(submitter, event, tag)

        waits = FormSubmitter._press_arrives

        def arrives(submitter):
            while withheld:
                deliver(*withheld.pop())
            return waits(submitter)

        def student(window, site):
            window.locator("[name=budget]").fill("Under $10k")
            window.get_by_role("button", name="Send message").click()

        quiet = BUDGET_FORM.replace('action="/send"', 'action="/quiet"')
        with mock.patch.object(FormSubmitter, "_on_press_binding", late), mock.patch.object(FormSubmitter, "_press_arrives", arrives):
            result, site, pressed = self.submit(quiet, student, wait=3)
        self.assertEqual(([path for path, _ in site.posts], pressed), (["/quiet"], [1]))
        self.assertEqual(result["outcome"], "unconfirmed", result)

    def test_what_was_held_back_before_the_press_is_named_in_the_window_and_on_the_card(self):
        seen = []

        def student(window, site):
            seen.append(window.evaluate("() => document.querySelector('[data-pipeline-note]').shadowRoot.textContent"))
            window.close()

        result, site, _pressed = self.submit(EARLY_POST_FORM, student)
        self.assertIn("held back what this page tried to send to bovi.test", seen[0])
        self.assertEqual(result["outcome"], "needs_you", result)
        self.assertIn("tried to reach bovi.test", result["note"])

    def test_a_captcha_is_left_for_the_student(self):
        seen = []

        def student(window, site):
            token = window.locator("[name=g-recaptcha-response]")
            seen.append(token.input_value())
            token.evaluate("(el) => { el.value = 'token-'.padEnd(60, 'x'); }")  # the student solves it
            window.get_by_role("button", name="Send message").click()

        result, site, _pressed = self.submit(CAPTCHA_FORM, student)
        self.assertEqual(result["outcome"], "submitted", result)
        self.assertEqual(seen, [""], "the app does not tick it: a token would expire while they type")
        self.assertTrue(parse_qs(site.posts[-1][1])["g-recaptcha-response"][0].startswith("token-"))


class _StubLocator:
    """One control on the stub page: remembers what was typed and records each action in the shared log."""

    def __init__(self, page, selector):
        self.page, self.selector, self.value = page, selector, ""

    @property
    def first(self):
        return self

    def count(self):
        return 0  # no CAPTCHA widget on this page

    def fill(self, value, timeout=None):
        self.value = value
        self.page.log.append(("fill", self.selector))

    def dispatch_event(self, _name):
        pass

    def blur(self):
        pass

    def input_value(self):
        return self.value

    def screenshot(self, path=None):
        pass

    def is_visible(self):
        return not self.page.clicked

    def click(self, timeout=None):
        self.page.log.append(("click", self.selector))
        self.page.clicked = True


class _StubPage:
    """Just enough of a Playwright page and frame for FormSubmitter.submit, with no browser."""

    url = "https://bovi.test/contact"

    def __init__(self, read):
        self.read, self.log, self.clicked, self.locators = read, [], False, {}
        self.main_frame = self
        self.frames = [self]

    def locator(self, selector):
        return self.locators.setdefault(selector, _StubLocator(self, selector))

    def evaluate(self, script, *_args):
        if script == outreach_forms.EXTRACT_SCRIPT:
            return self.read
        if "innerText" in script:
            return "Thank you! Your message has been sent." if self.clicked else "Contact us"
        raise AssertionError(f"unexpected script: {script[:60]}")

    def goto(self, *_args, **_kwargs):
        return type("Response", (), {"status": 200})()

    def wait_for_load_state(self, *_args, **_kwargs):
        pass

    def wait_for_timeout(self, _ms):
        pass

    def on(self, *_args):
        pass

    def screenshot(self, **_kwargs):
        pass

    def close(self):
        pass


class _GateRoute:
    def __init__(self, method, url, resource_type="fetch"):
        self.request = type("Request", (), {"method": method, "url": url, "resource_type": resource_type})()
        self.aborted = False

    def abort(self, _reason=None):
        self.aborted = True


class FinishInBrowserGateTests(unittest.TestCase):
    """Finish in browser: while the app fills the form, and once the window's outcome is decided, nothing the page posts leaves."""

    def test_only_reads_and_captcha_calls_leave_while_the_window_is_not_the_students(self):
        passed = []
        submitter = FormSubmitter(person_wait=60, route_hook=lambda route: passed.append(route.request.url))
        submitter._gate = "closed"
        submitter._needles = ["sam rivera"]
        submitter._sites = {"bovi.test"}
        routes = [_GateRoute("POST", "https://bovi.test/send"), _GateRoute("GET", "https://bovi.test/contact", "document"),
                  _GateRoute("POST", "https://www.google.com/recaptcha/api2/reload?k=x"),
                  _GateRoute("GET", "https://bovi.test/send?name=Sam+Rivera", "document"),
                  _GateRoute("GET", "https://api.example/mail?name=Sam+Rivera", "fetch"),
                  _GateRoute("GET", "https://www.google.com/recaptcha/api2/reload?n=Sam+Rivera"),
                  _GateRoute("POST", "https://www.google-analytics.com/g/collect")]
        for route in routes:
            submitter._route(route)
        self.assertEqual([route.aborted for route in routes], [True, False, False, True, True, True, True],
                         "anything carrying the student's name is held, whatever it is and wherever it goes")
        self.assertEqual(submitter.held_back, ["POST bovi.test", "GET bovi.test", "GET api.example", "GET www.google.com"],
                         "only what goes to the form's site or carries the student's details is named")

    def test_before_the_first_fill_only_the_students_details_are_held(self):
        submitter = FormSubmitter(person_wait=60, route_hook=lambda route: None)
        submitter._gate = "load"
        submitter._needles = ["sam rivera"]
        routes = [_GateRoute("POST", "https://bovi.test/cdn-cgi/challenge-platform/check"), _GateRoute("GET", "https://bovi.test/x?q=Sam+Rivera")]
        for route in routes:
            submitter._route(route)
        self.assertEqual([route.aborted for route in routes], [False, True])

    def test_the_gate_opens_only_for_a_press(self):
        submitter = FormSubmitter(person_wait=60, route_hook=lambda route: None)
        submitter._gate = "open"
        open_route = _GateRoute("POST", "https://bovi.test/send")
        submitter._route(open_route)
        self.assertFalse(open_route.aborted, "the student's own send leaves once they pressed")


class FormSubmitterCheckTests(unittest.TestCase):
    """The check just before the button, on a stub page, so it runs in every job, with or without Chromium."""

    READ = {
        "fields": [field(0, label="Your name", name="name", required=True),
                   field(1, "email", label="Email", name="email", required=True),
                   field(2, tag="textarea", label="Message", name="message", required=True)],
        "submit": True, "submit_text": "Send", "is_form": False, "heading": "Contact us", "text": "", "hidden": False,
    }

    def submit(self, should_continue):
        page = _StubPage(self.READ)
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        submitter = FormSubmitter(route_hook=lambda _route: None, resolve=lambda _host: ["93.184.216.34"],
                                  screenshot_dir=Path(tempdir.name))
        submitter._context = type("Context", (), {"new_page": lambda _self: page})()  # a started browser
        asked = []

        def ask():
            asked.append(len(page.log))
            page.log.append(("asked", ""))
            return should_continue()

        result = submitter.submit("https://bovi.test/contact", identity=IDENTITY, subject="s", body="Hi Bovi team,\n\nA note.", name="bovi",
                                  should_continue=ask)
        return result, page.log, asked

    def test_a_no_or_a_failed_check_presses_nothing(self):
        def broken():
            raise sqlite3.OperationalError("database is locked")

        for answer in (lambda: False, broken):
            with self.subTest(answer=answer.__name__):
                result, log, asked = self.submit(answer)
                self.assertEqual((result["outcome"], result["note"], result.get("paused")),
                                 ("failed", outreach_forms.PAUSED_BEFORE_SENDING, True), result)
                self.assertEqual(len(asked), 1)
                self.assertEqual([entry[0] for entry in log], ["fill", "fill", "fill", "asked"],
                                 "asked once, after every field was filled, and the button was never pressed")

    def test_a_yes_presses_the_button_right_after_the_check(self):
        result, log, _asked = self.submit(lambda: True)
        self.assertEqual(result["outcome"], "submitted", result)
        self.assertEqual([entry[0] for entry in log], ["fill", "fill", "fill", "asked", "click"])


class _Page:
    def __init__(self, url, raw):
        from opportunity_app.outreach.contacts import PageParser

        self.parser = PageParser()
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
        self.assertEqual(plan["problems"], ['The form requires "Country/Region", and your confirmed profile has no answer for it', ADDRESS_POINTER])
        self.assertEqual([fill for fill in plan["fills"] if fill["index"] == 2], [], "no country is chosen when the profile has none")

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
        # A lone street box: an address in the profile would not answer it either, so the profile is not pointed to.
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


class AddressPlanTests(unittest.TestCase):
    """A required address box is answered from the mailing address the student confirmed, and from nothing else."""

    STATES = [{"value": "", "text": "Please select..."}, {"value": "AR", "text": "Arkansas"}, {"value": "KS", "text": "Kansas"},
              {"value": "OR", "text": "Oregon"}]
    COUNTRIES = [{"value": "", "text": "Select"}, {"value": "us", "text": "United States"}, {"value": "ca", "text": "Canada"}]

    def base(self):
        return [field(0, "email", label="Email", name="email", required=True),
                field(1, "textarea", tag="textarea", label="Message", name="message", required=True)]

    def plan(self, extra, identity=None):
        return plan_fill(self.base() + extra, {**IDENTITY, **ADDRESS} if identity is None else identity,
                         subject="Robotics internship question", body="Hi team,\n\nA note.\n\nSam")

    def select(self, index, label, options, *, required=True, selected="", **values):
        return {**field(index, tag="select", type_="select-one", label=label, name=label.lower(), required=required, options=options, **values),
                "selected": selected}

    def hinted(self, index, hint, **values):
        return {**field(index, **values), "autocomplete": hint}

    def full_form(self, required=True):
        return [
            field(2, label="Address Line 1", name="a1", required=required),
            field(3, label="Address Line 2", name="a2", required=required),
            field(4, label="City", name="city", required=required),
            self.select(5, "State", self.STATES, required=required),
            field(6, label="ZIP Code", name="zip", required=required),
            self.select(7, "Country", self.COUNTRIES, required=required),
        ]

    def values(self, plan):
        return {fill["role"]: fill["value"] for fill in plan["fills"]}

    def test_every_required_address_box_is_answered_from_the_confirmed_address(self):
        plan = self.plan(self.full_form())
        self.assertEqual(plan["problems"], [])
        self.assertEqual({role: value for role, value in self.values(plan).items() if role in outreach_forms.ADDRESS_ROLES}, {
            "address_line1": "12 Example Lane", "address_line2": "Apt 4", "city": "Riverton", "state": "OR", "postal_code": "97000", "country": "us",
        }, "the lists get the choice's own value, the text boxes the student's words")

    def test_an_optional_address_is_left_blank_like_an_optional_phone(self):
        plan = self.plan(self.full_form(required=False))
        self.assertEqual(plan["problems"], [])
        self.assertEqual(set(self.values(plan)), {"email", "message"}, "only the email and message go in")

    def test_without_an_address_each_required_box_is_named_and_the_profile_is_pointed_to(self):
        plan = self.plan(self.full_form(), identity=IDENTITY)
        self.assertEqual(plan["problems"], [
            f'The form requires "{label}", and your confirmed profile has no answer for it'
            for label in ("Address Line 1", "Address Line 2", "City", "State", "ZIP Code", "Country")
        ] + [ADDRESS_POINTER])
        self.assertEqual(set(self.values(plan)), {"email", "message"})

    def test_a_half_filled_address_names_only_what_is_missing(self):
        plan = self.plan(self.full_form(), identity={**IDENTITY, **{**ADDRESS, "postal_code": "", "address_line2": ""}})
        self.assertEqual(plan["problems"], [
            'The form requires "Address Line 2", and your confirmed profile has no answer for it',
            'The form requires "ZIP Code", and your confirmed profile has no answer for it',
            ADDRESS_POINTER,
        ])

    def test_a_state_is_matched_whole_in_whichever_spelling_the_list_uses(self):
        for spelling in ("Texas", "TX", "TX - Texas", "Texas (TX)"):
            with self.subTest(spelling=spelling):
                options = [{"value": "", "text": "Select"}, {"value": "v-ar", "text": "Arkansas"}, {"value": "v-tx", "text": spelling}]
                for student in ("TX", "Texas"):
                    plan = self.plan([self.select(2, "State", options)], {**IDENTITY, "state": student})
                    self.assertEqual((plan["problems"], self.values(plan)["state"]), ([], "v-tx"))
        kansas = self.plan([self.select(2, "State", self.STATES)], {**IDENTITY, "state": "Kansas"})
        self.assertEqual(self.values(kansas)["state"], "KS", "Kansas is not Arkansas")

    def test_a_list_with_no_choice_that_is_the_students_own_stops_the_send(self):
        plan = self.plan([self.select(2, "State", self.STATES)], {**IDENTITY, "state": "Narnia"})
        self.assertEqual(plan["problems"], ['The form requires a choice for "State", and none of its choices is the state in your profile'])
        self.assertNotIn("state", self.values(plan))

    def test_a_country_is_matched_by_any_of_its_usual_names(self):
        for text in ("United States", "United States of America", "USA", "U.S.A."):
            with self.subTest(text=text):
                options = [{"value": "", "text": "Select"}, {"value": "x", "text": text}, {"value": "ca", "text": "Canada"}]
                for student in ("United States", "USA"):
                    plan = self.plan([self.select(2, "Country/Region", options)], {**IDENTITY, "country": student})
                    self.assertEqual((plan["problems"], self.values(plan)["country"]), ([], "x"))

    def test_a_list_already_showing_a_country_is_chosen_from_the_profile_or_stops(self):
        shown = self.select(2, "Country", self.COUNTRIES, required=False, selected="us")
        chosen = self.plan([shown], {**IDENTITY, "country": "Canada"})
        self.assertEqual((chosen["problems"], self.values(chosen)["country"]), ([], "ca"))
        stuck = self.plan([shown], identity=IDENTITY)
        self.assertEqual(stuck["problems"], ['The form requires "Country", and your confirmed profile has no answer for it', ADDRESS_POINTER])

    def test_a_box_that_takes_two_letters_gets_the_abbreviation(self):
        plan = self.plan([field(2, label="State", name="st", required=True, maxlength=2),
                          self.hinted(3, "country", label="Country", name="c", required=True, maxlength=2)])
        self.assertEqual((plan["problems"], self.values(plan)["state"], self.values(plan)["country"]), ([], "OR", "US"))
        short = self.plan([field(2, label="ZIP Code", name="zip", required=True, maxlength=3)])
        self.assertEqual(short["problems"], ['The box "ZIP Code" takes 3 characters, and the ZIP code in your profile is longer'])

    def test_the_browsers_own_hints_name_a_box_whatever_its_label_says(self):
        plan = self.plan([
            self.hinted(2, "address-line1", label="Where", name="f1", required=True),
            self.hinted(3, "address-level2", label="Town or village", name="f2", required=True),
            self.hinted(4, "postal-code", name="f3", required=True),
            self.hinted(5, "section-x country-name", label="Land", name="f4", required=True),
            self.hinted(6, "address-line2", label="More", name="f5", required=True),
        ])
        self.assertEqual((plan["problems"], {fill["index"]: fill["value"] for fill in plan["fills"] if fill["index"] > 1}),
                         ([], {2: "12 Example Lane", 3: "Riverton", 4: "97000", 5: "United States", 6: "Apt 4"}))

    def test_a_street_address_box_takes_both_lines_and_is_not_the_message(self):
        plan = self.plan([self.hinted(2, "street-address", label="Address", name="street", required=True),
                          field(3, label="City", name="city", required=True)])
        values = {fill["index"]: fill["value"] for fill in plan["fills"]}
        self.assertEqual((plan["problems"], values[2], values[1]), ([], "12 Example Lane, Apt 4", "Hi team,\n\nA note.\n\nSam"))
        only = plan_fill([field(0, "email", label="Email", name="email", required=True),
                          self.hinted(1, "street-address", tag="textarea", type_="textarea", label="Street address", name="street", required=True)],
                         {**IDENTITY, **ADDRESS}, subject="s", body="b")
        self.assertIn("The form has no message box the app could find", only["problems"], "an address box is not a message box")

    def test_a_box_that_is_not_the_students_own_address_is_never_given_it(self):
        plan = self.plan([
            field(2, label="Please state your interest", name="interest", required=True),
            field(3, label="City you want to work in", name="wcity", required=True),
            self.hinted(5, "billing postal-code", label="Billing ZIP", name="bzip", required=True),
            self.select(6, "Country code", [{"value": "", "text": "Code"}, {"value": "+1", "text": "+1"}, {"value": "+44", "text": "+44"}]),
            field(7, label="Region", name="region", required=True),
            field(8, label="City / State", name="cs", required=True),
        ])
        self.assertEqual([fill for fill in plan["fills"] if fill["index"] > 1], [], "none of these boxes is answered")
        company = self.plan([field(2, label="Company address", name="coaddr", required=True)])
        self.assertFalse(set(ADDRESS.values()) & {fill["value"] for fill in company["fills"]}, "a company's address is not the student's")
        for label in ("Please state your interest", "City you want to work in", "Billing ZIP", "Country code", "Region", "City / State"):
            self.assertIn(f'The form requires "{label}", and your confirmed profile has no answer for it', plan["problems"])
        self.assertNotIn(ADDRESS_POINTER, plan["problems"], "none of these is an address box the profile could answer")

    def address_values_sent(self, plan):
        sent = {fill["value"] for fill in plan["fills"]}
        return sent & (set(ADDRESS.values()) | {"12 Example Lane, Apt 4", "US", "OR", "us"})

    def test_an_address_hint_for_a_billing_shipping_or_work_address_is_unanswerable(self):
        # Each fell through to the label patterns: the school ("Company", "Business") or the student's name ("name").
        boxes = [
            self.hinted(2, "work street-address", label="Company address", name="f2", required=True),
            self.hinted(3, "billing address-line1", label="Street name", name="f3", required=True),
            self.hinted(4, "shipping address-level2", label="Business city", name="f4", required=True),
            self.hinted(5, "section-a address-level3", label="Company district", name="f5", required=True),
        ]
        self.assertEqual([outreach_forms._role(box) for box in boxes], ["unknown"] * 4)
        plan = self.plan(boxes)
        self.assertEqual([fill for fill in plan["fills"] if fill["index"] > 1], [], "neither the school nor the name goes in")
        for label in ("Company address", "Street name", "Business city", "Company district"):
            self.assertIn(f'The form requires "{label}", and your confirmed profile has no answer for it', plan["problems"])
        self.assertNotIn(ADDRESS_POINTER, plan["problems"])

    def test_words_that_only_look_like_an_address_line_are_not_one(self):
        territories = [{"value": "", "text": "Select"}, {"value": "w", "text": "West"}, {"value": "or", "text": "Oregon"}]
        units = [{"value": "", "text": "Select"}, {"value": "r", "text": "Robotics"}, {"value": "a4", "text": "Apt 4"}]
        boxes = [
            field(2, label="Nation", name="f2", required=True),
            field(3, label="PO Box", name="f3", required=True),
            field(4, label="Street number", name="f4", required=True),
            field(5, label="Street No.", name="f5", required=True),
            self.select(6, "Territory", territories),
            self.select(7, "Unit", units),
        ]
        self.assertFalse({outreach_forms._role(box) for box in boxes} & outreach_forms.ADDRESS_ROLES)
        plan = self.plan(boxes + [field(8, label="City", name="city", required=True)])
        self.assertEqual({fill["index"] for fill in plan["fills"]}, {0, 1, 8}, "only the email, message and city go in")
        for label in ("Nation", "PO Box", "Street number", "Street No."):
            self.assertIn(f'The form requires "{label}", and your confirmed profile has no answer for it', plan["problems"])
        # With the word that makes them an address, they still are one.
        self.assertEqual([outreach_forms._role(field(9, label=label, name="f9")) for label in ("State/Territory", "Apt / Unit", "Unit Address")],
                         ["state", "address_line2", "address_line2"])

    def test_any_other_real_hint_says_what_the_box_is_whatever_its_label(self):
        dial = self.hinted(2, "tel-country-code", label="Country", name="f2", required=True)
        dial_list = {**self.select(3, "Country", self.COUNTRIES), "autocomplete": "tel-country-code"}
        named = self.hinted(4, "name", label="Address", name="f4", required=True)
        organization = self.hinted(5, "organization", label="Street address", name="f5", required=True)
        self.assertEqual([outreach_forms._role(box) for box in (dial, dial_list, named, organization)],
                         ["unknown", "select", "full_name", "company"])
        plan = self.plan([dial, dial_list, field(6, label="City", name="city")])
        self.assertEqual(self.address_values_sent(plan), set(), "a phone's country code is not the student's country")
        self.assertIn('The form requires "Country", and your confirmed profile has no answer for it', plan["problems"])
        # A hint that turns autofill off says nothing about the box.
        self.assertEqual(outreach_forms._role(self.hinted(7, "off", label="City", name="f7")), "city")

    def test_a_box_whose_name_says_it_is_an_organizations_address_is_not_the_students(self):
        boxes = [
            field(2, label="Address", name="company_address", required=True),
            field(3, label="City", name="office_city", required=True),
            field(4, label="ZIP Code", name="billingZip", required=True),
            {**self.select(5, "State", self.STATES), "name": "orgState", "id": "orgState"},
            field(6, label="Country", name="employer-country", required=True),
        ]
        self.assertFalse({outreach_forms._role(box) for box in boxes} & outreach_forms.ADDRESS_ROLES)
        self.assertEqual(self.address_values_sent(self.plan(boxes)), set())
        # A name that only contains such a word is not one ("network_city").
        self.assertEqual(outreach_forms._role(field(7, label="City", name="network_city")), "city")

    def test_with_no_second_line_box_the_street_box_takes_both_lines(self):
        form = [field(2, label="Street Address", name="street", required=True), field(3, label="City", name="city", required=True),
                self.select(4, "State", self.STATES), field(5, label="ZIP Code", name="zip", required=True)]
        plan = self.plan(form)
        self.assertEqual((plan["problems"], {fill["index"]: fill["value"] for fill in plan["fills"]}[2]), ([], "12 Example Lane, Apt 4"))
        no_apartment = self.plan(form, {**IDENTITY, **ADDRESS, "address_line2": ""})
        self.assertEqual({fill["index"]: fill["value"] for fill in no_apartment["fills"]}[2], "12 Example Lane")

    def test_a_lone_address_box_is_never_given_an_address_in_a_made_up_format(self):
        plan = self.plan([field(2, label="Address", name="addr", required=True), self.select(3, "Country", self.COUNTRIES)])
        self.assertEqual(plan["problems"], ['The form requires "Address", and your confirmed profile has no answer for it'],
                         "the profile has an address, so it is not pointed to")
        self.assertEqual({fill["index"]: fill["value"] for fill in plan["fills"] if fill["index"] > 1}, {3: "us"})
        optional = self.plan([field(2, label="Address", name="addr")])
        self.assertEqual((optional["problems"], [fill for fill in optional["fills"] if fill["index"] > 1]), ([], []))

    def test_a_box_hinted_country_gets_the_iso_code(self):
        for student in ("United Kingdom", "UK", "Great Britain", "GB"):
            for maxlength in (2, None):
                with self.subTest(student=student, maxlength=maxlength):
                    plan = self.plan([self.hinted(2, "country", label="Country", name="c", required=True, maxlength=maxlength)],
                                     {**IDENTITY, **ADDRESS, "country": student})
                    self.assertEqual((plan["problems"], self.values(plan)["country"]), ([], "GB"))
        united_states = self.plan([self.hinted(2, "country", label="Country", name="c", required=True)], {**IDENTITY, **ADDRESS})
        self.assertEqual(self.values(united_states)["country"], "US")

    def test_an_address_textarea_does_not_make_a_one_line_message_box_a_question(self):
        def plan(body):
            return plan_fill([
                field(0, "email", label="Email", name="email", required=True),
                field(1, "textarea", tag="textarea", label="Address", name="address", required=True),
                field(2, label="Message", name="message", required=True),
                field(3, label="City", name="city", required=True),
            ], {**IDENTITY, **ADDRESS}, subject="s", body=body)

        long = plan("Hi team,\n\nA note.\n\nSam")
        self.assertNotIn("Hi team,\n\nA note.\n\nSam", [fill["value"] for fill in long["fills"]])
        self.assertEqual({fill["index"]: fill["value"] for fill in long["fills"]}[1], "12 Example Lane, Apt 4")
        self.assertEqual(long["problems"], ["The form's message box is a single line, so the draft's paragraphs would run together"],
                         "the honest reason, not a missing message box")
        short = plan("Hi team, a note. Sam")
        self.assertEqual((short["problems"], {fill["index"]: fill["value"] for fill in short["fills"]}[2]), ([], "Hi team, a note. Sam"))

    def by_index(self, plan):
        return {fill["index"]: fill["value"] for fill in plan["fills"] if fill["index"] > 1}

    def test_a_country_of_nationality_citizenship_or_birth_is_not_the_mailing_address(self):
        boxes = [
            {**self.select(2, "Country", self.COUNTRIES), "name": "nationality", "id": "nationality"},
            {**self.select(3, "Country", self.COUNTRIES), "name": "citizenshipCountry", "id": "citizenshipCountry"},
            field(4, label="Country", name="birth_country", required=True),
            self.hinted(5, "country", label="Country of origin", name="f5", required=True),
            self.hinted(6, "country-name", label="Passport issuing country", name="f6", required=True),
            field(7, label="Country of birth", name="f7", required=True),
        ]
        self.assertFalse({outreach_forms._role(box) for box in boxes} & outreach_forms.ADDRESS_ROLES)
        plan = self.plan(boxes + [field(8, label="City", name="city", required=True)])
        self.assertEqual(self.by_index(plan), {8: "Riverton"}, "only the city is the mailing address")
        for label in ("Country", "Country of origin", "Passport issuing country", "Country of birth"):
            self.assertIn(f'The form requires "{label}", and your confirmed profile has no answer for it', plan["problems"])
        self.assertNotIn(ADDRESS_POINTER, plan["problems"])

    def test_a_home_or_permanent_address_box_is_not_answered_from_the_mailing_address(self):
        # A student's mailing address can be a dorm, and a home country can mean a nationality.
        boxes = [
            self.select(2, "Home country", self.COUNTRIES),
            field(3, label="Home state", name="f3", required=True),
            field(4, label="Permanent address", name="f4", required=True),
            field(5, label="Residential address", name="f5", required=True),
            field(6, label="Residence city", name="f6", required=True),
        ]
        self.assertFalse({outreach_forms._role(box) for box in boxes} & outreach_forms.ADDRESS_ROLES)
        plan = self.plan(boxes + [field(7, label="City", name="city", required=True)])
        self.assertEqual(self.by_index(plan), {7: "Riverton"})
        for label in ("Home country", "Home state", "Permanent address", "Residential address", "Residence city"):
            self.assertIn(f'The form requires "{label}", and your confirmed profile has no answer for it', plan["problems"])

    def test_once_part_of_the_address_is_required_the_rest_goes_into_the_optional_boxes(self):
        optional_rest = self.full_form(required=False)
        optional_rest[0] = field(2, label="Address Line 1", name="a1", required=True)
        plan = self.plan(optional_rest)
        self.assertEqual((plan["problems"], self.by_index(plan)), ([], {
            2: "12 Example Lane", 3: "Apt 4", 4: "Riverton", 5: "OR", 6: "97000", 7: "us"}), "the apartment and the city go in too")
        street_only = self.plan([field(2, label="Address", name="addr", required=True), field(3, label="City", name="city"),
                                 field(4, label="ZIP Code", name="zip")])
        self.assertEqual((street_only["problems"], self.by_index(street_only)), ([], {2: "12 Example Lane, Apt 4", 3: "Riverton", 4: "97000"}))
        # An optional list without the student's state, or a box too short for the ZIP, is left alone, not a problem.
        no_match = self.plan([field(2, label="Street Address", name="street", required=True), field(6, label="City", name="city"),
                              self.select(3, "State", [{"value": "", "text": "Select"}, {"value": "KS", "text": "Kansas"}], required=False),
                              field(4, label="ZIP Code", name="zip", maxlength=3), field(5, label="Company", name="company")])
        self.assertEqual((no_match["problems"], self.by_index(no_match)), ([], {2: "12 Example Lane, Apt 4", 6: "Riverton"}),
                         "nor is any other optional box filled")
        # A required street the profile cannot answer types nothing, so nothing else goes in either.
        missing = self.plan([field(2, label="Street Address", name="street", required=True), field(3, label="City", name="city")],
                            {**IDENTITY, **ADDRESS, "address_line1": "", "address_line2": ""})
        self.assertEqual(self.by_index(missing), {})

    def test_a_required_country_state_city_or_zip_alone_brings_nothing_else(self):
        # The owner's choice: the least the student discloses. Only a required street brings the rest of the address.
        rest = [field(3, label="Street Address", name="street"), field(4, label="Address Line 2", name="a2"),
                field(5, label="City", name="city"), self.select(6, "State", self.STATES, required=False),
                field(7, label="ZIP Code", name="zip"), self.select(8, "Country", self.COUNTRIES, required=False)]
        required_alone = {
            "country": (self.select(2, "Country", self.COUNTRIES), "us"),
            "state": (self.select(2, "State", self.STATES), "OR"),
            "city": (field(2, label="City", name="city", required=True), "Riverton"),
            "postal_code": (field(2, label="ZIP Code", name="zip", required=True), "97000"),
        }
        for role, (box, value) in required_alone.items():
            with self.subTest(role=role):
                plan = self.plan([box] + [other for other in rest if outreach_forms._role(other) != role])
                self.assertEqual((plan["problems"], self.by_index(plan)), ([], {2: value}))
        line2 = self.plan([field(2, label="Address Line 2", name="a2", required=True)] + rest[2:])
        self.assertEqual(self.by_index(line2), {2: "Apt 4"}, "the apartment alone is not the street")

    def notes(self, *labels):
        return [f'The form requires "{label}", and your confirmed profile has no answer for it' for label in labels]

    def test_a_form_that_asks_for_an_address_twice_gets_no_address(self):
        # Required addr_1/city_1/zip_1, then the same boxes again, optional: nothing says which block is the student's.
        repro = self.plan([
            field(2, label="Address", name="addr_1", required=True), field(3, label="City", name="city_1", required=True),
            field(4, label="ZIP Code", name="zip_1", required=True),
            field(5, label="Address", name="addr_2"), field(6, label="City", name="city_2"), field(7, label="ZIP Code", name="zip_2"),
        ])
        self.assertEqual((repro["problems"], self.by_index(repro)), (self.notes("Address", "City", "ZIP Code"), {}),
                         "every required address box waits, and the profile is not pointed to")
        # The second block first, or required too: the same.
        reference_first = self.plan([
            field(2, label="Address", name="ref_a"), field(3, label="City", name="ref_c"),
            field(4, label="Address", name="a1", required=True), field(5, label="City", name="c1", required=True),
        ])
        self.assertEqual((reference_first["problems"], self.by_index(reference_first)), (self.notes("Address", "City"), {}))
        required_twice = self.plan([
            field(2, label="Street", name="s1", required=True), field(3, label="City", name="c1", required=True),
            field(4, label="Street", name="s2", required=True), field(5, label="City", name="city2", required=True),
        ])
        self.assertEqual((required_twice["problems"], self.by_index(required_twice)), (self.notes("Street", "City"), {}))
        # A street box covers both lines, so a later "Line 2" box is a second address too; an optional list showing a
        # country would send it as the student's, so it waits as well.
        both = self.plan([self.hinted(2, "street-address", label="Street Address", name="s", required=True),
                          field(3, label="City", name="c", required=True), self.hinted(4, "address-line2", label="Line 2", name="l2"),
                          self.select(5, "Country", self.COUNTRIES, required=False, selected="us")])
        self.assertEqual((both["problems"], self.by_index(both)), (self.notes("Street Address", "City", "Country"), {}))
        # One box for each part: the address goes in as usual.
        once = self.plan([field(2, label="Address", name="addr_1", required=True), field(3, label="City", name="city_1", required=True)])
        self.assertEqual((once["problems"], self.by_index(once)), ([], {2: "12 Example Lane, Apt 4", 3: "Riverton"}))

    def test_a_box_named_for_a_school_a_headquarters_or_a_relative_is_unanswerable(self):
        boxes = [
            field(2, label="Address", name="school_address", required=True),
            field(3, label="City", name="hq_city", required=True),
            field(4, label="ZIP Code", name="kin_zip", required=True),
            field(5, label="State", name="supervisorState", required=True),
        ]
        self.assertEqual([outreach_forms._role(box) for box in boxes], ["unknown"] * 4, "not the student's address, nor the school's name")
        plan = self.plan(boxes)
        self.assertEqual((plan["problems"], self.by_index(plan)), (self.notes("Address", "City", "ZIP Code", "State"), {}))
        for word in ("campus", "headquarters", "relative", "manager", "landlord", "recipient", "delivery", "venue", "event", "property", "alt"):
            with self.subTest(word=word):
                self.assertEqual(outreach_forms._role(field(7, label="City", name=f"{word}_city")), "unknown")
        # Whole words only: "altitude" and "hqx" are not one of them.
        self.assertEqual([outreach_forms._role(field(8, label="City", name=name)) for name in ("altitude_city", "hqx_city")], ["city", "city"])

    def test_a_box_named_for_someone_else_is_never_given_the_students_address(self):
        boxes = [
            field(2, label="Address", name="emergency_address", required=True),
            field(3, label="City", name="referenceCity", required=True),
            field(4, label="ZIP Code", name="contact_person_zip", required=True),
            field(5, label="Parent/guardian address", name="f5", required=True),
            field(6, label="Previous city", name="f6", required=True),
            field(7, label="Contact person ZIP", name="f7", required=True),
            field(8, label="Alternate address", name="f8", required=True),
            self.select(9, "State", self.STATES, required=True) | {"name": "spouse_state", "id": "spouse_state"},
            # The browser's hint says address; the words the student sees say whose.
            self.hinted(10, "address-line1", label="Emergency contact address", name="f10", required=True),
            self.hinted(11, "postal-code", label="Reference ZIP", name="f11", required=True),
        ]
        self.assertEqual([outreach_forms._role(box) for box in boxes if outreach_forms._role(box) in outreach_forms.ADDRESS_ROLES], [])
        self.assertEqual(self.address_values_sent(self.plan(boxes)), set())
        # A whole word only: "another" and "contactCity" are not someone else's.
        self.assertEqual([outreach_forms._role(field(12, label="City", name=name)) for name in ("another_city", "contactCity")], ["city", "city"])

    def test_street_lines_with_no_city_state_or_zip_box_are_unanswerable(self):
        plan = self.plan([field(2, label="Address Line 1", name="a1", required=True), field(3, label="Address Line 2", name="a2", required=True),
                          self.select(4, "Country", self.COUNTRIES)])
        self.assertEqual(plan["problems"], [f'The form requires "{label}", and your confirmed profile has no answer for it'
                                            for label in ("Address Line 1", "Address Line 2")])
        self.assertEqual(self.by_index(plan), {4: "us"})
        optional_line2 = self.plan([field(2, label="Address Line 1", name="a1", required=True), field(3, label="Address Line 2", name="a2"),
                                    self.select(4, "Country", self.COUNTRIES)])
        self.assertEqual(self.by_index(optional_line2), {4: "us"}, "the country going in does not bring the street with it")

    def test_a_country_box_of_two_or_three_letters_gets_the_iso_code(self):
        for maxlength in (2, 3):
            with self.subTest(maxlength=maxlength):
                box = [field(2, label="Country", name="c", required=True, maxlength=maxlength)]
                self.assertEqual(self.values(self.plan(box, {**IDENTITY, **ADDRESS, "country": "United Kingdom"}))["country"], "GB")
                self.assertEqual(self.values(self.plan(box))["country"], "US")
        roomy = self.plan([field(2, label="Country", name="c", required=True)], {**IDENTITY, **ADDRESS, "country": "United Kingdom"})
        self.assertEqual(self.values(roomy)["country"], "United Kingdom", "a box with room gets the student's own words")


ADDRESS_FORM = """<html><head><meta charset="utf-8"></head><body><form action="/send" method="post">
  <label>Name <input name="name" required></label><label>Email <input type="email" name="email" required></label>
  <label>Address Line 1 <input name="a1" required></label><label>City <input name="city" required></label>
  <label>State <select name="state" required><option value="">Please select...</option><option>Alabama</option><option>Oregon</option></select></label>
  <label>ZIP Code <input name="zip" required></label>
  <label>Country <select name="country" required><option value="">Select</option><option value="US">United States</option><option value="CA">Canada</option></select></label>
  <label>Message <textarea name="message" required></textarea></label><button type="submit">Send</button></form></body></html>"""


@requires_chromium
class AddressBrowserTests(unittest.TestCase):
    def rehearse(self, identity):
        site = Site({"/contact": ADDRESS_FORM})
        with tempfile.TemporaryDirectory() as shots, FormSubmitter(route_hook=site.route, screenshot_dir=Path(shots), rehearse=True) as submitter:
            result = submitter.submit("https://bovi.test/contact", identity=identity, subject="s", body="Hi team,\n\nA note.\n\nSam", name="bovi")
        self.assertEqual(site.posts, [])
        return result

    def test_a_form_that_requires_an_address_is_filled_from_the_confirmed_one(self):
        result = self.rehearse({**IDENTITY, **ADDRESS})
        self.assertEqual(result["outcome"], "rehearsed", result)
        self.assertEqual(result["filled"], ["Name", "Email", "Address Line 1", "City", "State: Oregon", "ZIP Code", "Country: United States", "Message"])

    def test_the_same_form_waits_for_the_student_when_the_profile_has_no_address(self):
        result = self.rehearse(IDENTITY)
        self.assertEqual(result["outcome"], "needs_you", result)
        self.assertIn('The form requires "Address Line 1", and your confirmed profile has no answer for it', result["note"])
        self.assertIn(ADDRESS_POINTER, result["note"])


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


@requires_chromium
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
