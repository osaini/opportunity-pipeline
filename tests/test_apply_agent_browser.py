"""The apply agent in a real Chromium, against the fictional Greenhouse (skipped without Chromium, required in CI's `browser-python`).

Every test drives ``ApplyAgent`` through its own request policy: pages are served by ``FakeGreenhouse`` through the agent's
``route_hook``, so ``route_decision`` runs first and only what it lets through reaches the fake. The fake records every
request it received and counts clicks on the buttons the agent must never press. Whatever a test is about, ``go`` asserts
the promise of this milestone afterwards: no request but GET, HEAD and OPTIONS reached the fake, the submit path was never
hit, and no forbidden button was clicked. Nothing here reaches the network.

Where the agent runs in its own process (the runner's way), the last class drives it through ``runner.supervise``.
"""

import base64
import hashlib
import json
import pickle
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))

import realdata_guard

realdata_guard.install()

import apply_agent_fakes as fakes
import browser_support
from apply_fake_ats import API_HOST, JOB_PATH, JOB_URL, LEGACY_JOB_URL, LOOKUP_OPTIONS, OFFSITE_HOST, SUBMIT_HOST, FakeGreenhouse, fixture_json
from browser_support import requires_chromium, requires_headed
import helpers_apply
from apply_fake_ats import kill_if_same_process, press_submit
from helpers_apply import USER, ApplyCase, FakePlan, Store, answer, planned, setUpModule, tearDownModule  # noqa: F401  (the module fixtures)

from opportunity_app.apply import agent as apply_agent
from opportunity_app.apply import checks as apply_checks
from opportunity_app.apply import policy as apply_policy
from opportunity_app.apply import runner as apply_runner
from opportunity_app.apply import runs as apply_runs
from opportunity_app.apply.agent import ApplyAgent, GreenhouseAdapter
from opportunity_app.apply import agent_types as apply_agent_types
from opportunity_app.apply.agent_types import (
    HANDOFF_CRASHED, HANDOFF_EARLY, HANDOFF_ELSEWHERE, HANDOFF_HIDDEN, HANDOFF_NO_LOADER, HANDOFF_NOT_SUBMITTED, HANDOFF_S3, HANDOFF_UNRECORDED, HANDOFF_UPLOAD, LEFT_COVER_LETTER_CHANGED,
    PROGRESS_STEPS, WINDOW_CLOSED, AgentJob, ApplyTimeouts, LookupRequest,
)

EMAIL = "sam.rivera@example.test"
GUARDED = {"last_name": "Rivera", "email": EMAIL, "question_4000000101": fakes.WHY, "question_4000000102": fakes.PORTFOLIO}
STATE_JS = """() => {
  const one = (id) => document.getElementById(id);
  const mirror = (name) => { const e = document.querySelector('input[name="' + name + '"][aria-hidden]'); return e ? e.value : null; };
  const shown = (name) => Array.from(one(name).closest('.rs').querySelectorAll('.select__single-value, .select__multi-value__label')).map((n) => n.textContent.trim());
  const file = one('resume').files[0];
  const letter = one('cover_letter').files[0];
  return {
    first: one('first_name').value, last: one('last_name').value, email: one('email').value, phone: one('phone').value,
    why: one('question_4000000101').value, portfolio: one('question_4000000102').value, trap: one('website_url').value,
    team: shown('question_4000000103'), location: shown('location_city'), languages: shown('question_4000000104'),
    work_mirror: mirror('question_4000000105'), sponsor_mirror: mirror('question_4000000106'), gender_mirror: mirror('gender'),
    work_shown: shown('question_4000000105'), gender_shown: shown('gender'),
    privacy: one('question_4000000109').checked, accurate: one('question_4000000110').checked, gdpr: one('gdpr_consent_given').checked,
    informed: one('question_4000000113').checked, previous: (document.querySelector('input[name="question_4000000111"]:checked') || {}).value || null,
    term: one('question_4000000108').value,
    resume: file ? {name: file.name, size: file.size} : null,
    letter: letter ? {name: letter.name, size: letter.size, shown: one('cover_letter-file-name').textContent} : null,
  };
}"""
PIXELS_JS = """(args) => new Promise((resolve) => {
  const image = new Image();
  image.onload = () => {
    const canvas = document.createElement('canvas');
    canvas.width = image.width; canvas.height = image.height;
    const context = canvas.getContext('2d');
    context.drawImage(image, 0, 0);
    const solid = (box) => {
      for (let x = Math.ceil(box.x) + 4; x < Math.floor(box.x + box.w) - 4; x += 7) {
        for (let y = Math.ceil(box.y) + 4; y < Math.floor(box.y + box.h) - 4; y += 7) {
          const d = context.getImageData(x, y, 1, 1).data;
          if (d[0] !== 0 || d[1] !== 0 || d[2] !== 0 || d[3] !== 255) return false;
        }
      }
      return true;
    };
    resolve({width: image.width, height: image.height, masked: args.masked.map(solid), open_solid: args.open.map(solid)});
  };
  image.src = 'data:image/png;base64,' + args.data;
})"""
BOXES_JS = """(names) => names.map((name) => {
  const holder = document.getElementById(name).closest('.field');
  const r = holder.getBoundingClientRect();
  return {x: r.x + window.scrollX, y: r.y + window.scrollY, w: r.width, h: r.height};
})"""


def carries_a_value(seen):
    """Whether a request that reached the fake holds any planned value (raw or URL-encoded)."""
    text = f"{seen['path']}?{seen['query']} {seen['post_data']}"
    return any(value in text or quote(value, safe="") in text for value in GUARDED.values())


class AgentCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.dir = Path(self.tempdir.name)

    def go(self, scenario="confirm", *, mode="rehearse", sources=None, plan=None, schema=None, files="default", lookup=None, replan=None,
           cancelled=None, lookup_endpoints=fakes.FIXTURE_LOOKUP, inspect=None, on_progress=None, headless=True, timeouts=None, check_file="default", letter_required=False):
        sources = sources or fakes.full_sources()
        schema = fakes.fixture_schema() if schema is None else schema
        steps, beats, holder, asked = [], [], {}, []
        files = {"resume": fakes.resume_payload()} if files == "default" else files
        if check_file == "default":
            # The runner hands the agent a check only when there is a cover letter to attach; this one says yes and keeps what it was asked.
            check_file = (lambda key, ref, sha: asked.append((key, ref, sha)) or True) if "cover_letter" in files else None

        def progress(step, text):
            steps.append(step)
            if on_progress:
                on_progress(holder["agent"], step)

        fake = FakeGreenhouse(scenario)
        fake.letter_required = letter_required
        agent = fakes.RecordingAgent(
            fake=fake, mode=mode, adapter=GreenhouseAdapter(), run_id="run-test", screenshot_dir=self.dir,
            timeouts=timeouts or fakes.TEST_TIMEOUTS, lookup_endpoints=lookup_endpoints, on_progress=progress, heartbeat=lambda: beats.append(1),
        )
        agent.headless = headless
        holder["agent"] = agent
        with agent:
            result = agent.run(
                fakes.draft_plan(sources, schema=schema) if plan is None else plan, page_url=JOB_URL, schema=schema,
                files=files, lookup=lookup,
                replan=replan or fakes.fixture_replan(sources, schema=schema), cancelled=cancelled, check_file=check_file,
            )
            seen = inspect(agent._page, result) if inspect else None
            record = agent.record()
        # The promise of this milestone, whatever the test was about.
        self.assertEqual(record["non_get"], [], "a request that could carry an application reached the fake")
        self.assertFalse(record["submit_path_hit"])
        # -1 means the run never opened a page, so there was nothing to click.
        self.assertIn(record["forbidden_clicks"], (0, -1) if not record["requests"] else (0,), "the agent pressed a button it must never press")
        self.assertEqual([entry for entry in record["requests"] if entry["method"] not in ("GET", "HEAD", "OPTIONS")], [])
        return SimpleNamespace(result=result, record=record, steps=steps, beats=beats, seen=seen, agent=agent, asked=asked)

    def state(self, page, _result=None):
        return page.evaluate(STATE_JS)

    def refused(self, run, **match):
        return [entry for entry in run.result.refused if all(entry.get(name) == value for name, value in match.items())]

    def reached(self, run):
        return [(entry["method"], entry["host"], entry["path"]) for entry in run.record["requests"]]


@requires_chromium
class RehearsalTests(AgentCase):
    def test_a_clean_run_fills_reads_back_and_stops_without_sending_anything(self):
        run = self.go(inspect=self.state)
        result = run.result
        self.assertEqual((result.outcome, result.reasons), ("rehearsed", []))
        self.assertEqual(run.steps, ["open", "read", "fill", "check", "picture"])
        self.assertTrue(run.beats)
        self.assertEqual(result.check_problems, [])
        self.assertEqual(result.join_problems, [], "the form's four bare consent boxes are not a disagreement with the listing")
        self.assertTrue(result.plan_hash)
        self.assertFalse(result.handed_over)
        self.assertEqual(result.requests, [])
        state = run.seen
        self.assertEqual((state["first"], state["last"], state["email"], state["phone"]), ("Sam", "Rivera", EMAIL, "555-0100"))
        self.assertEqual((state["why"], state["portfolio"]), (fakes.WHY, fakes.PORTFOLIO))
        self.assertEqual((state["team"], state["location"], state["previous"]), (["Controls"], [LOOKUP_OPTIONS[0]], "0"))
        self.assertEqual(state["resume"], {"name": fakes.RESUME_NAME, "size": len(fakes.RESUME_BYTES)})
        self.assertEqual(state["trap"], "")
        evidence = result.evidence
        self.assertEqual(evidence["page"], "application_form_new")
        self.assertEqual(evidence["loader"], {"submit_path": True, "confirmation_path": True})
        self.assertEqual((evidence["submit_path_hit"], evidence["uploads_on_attach"], evidence["captcha_widget"]), (False, False, False))
        self.assertEqual(evidence["lookups"], [{"key": "location_city", "question": "Location (City)", "kind": "location", "typed": True}])
        self.assertEqual([entry for entry in result.refused if entry["method"] != "GET"], [])
        done = set(evidence["filled_keys"])
        for entry in result.plan:
            if entry["disposition"] == "fill" and entry["control"] != "file":
                self.assertIn(entry["key"], done, f"{entry['key']} was filled and read back")
            elif entry["disposition"] != "fill":
                self.assertNotIn(entry["key"], done, f"{entry['key']} was not filled")

    def test_sensitive_fields_are_checked_against_the_page_and_left_empty_in_it(self):
        run = self.go(inspect=self.state)
        state = run.seen
        self.assertEqual((state["work_mirror"], state["sponsor_mirror"], state["gender_mirror"]), ("", "", ""))
        self.assertEqual((state["work_shown"], state["gender_shown"]), ([], []))
        self.assertEqual((state["privacy"], state["accurate"], state["gdpr"]), (False, False, False))
        dispositions = {entry["key"]: entry["disposition"] for entry in run.result.plan}
        for key in ("question_4000000105", "question_4000000106", "gender", "hispanic_ethnicity", "veteran_status", "disability_status"):
            self.assertEqual(dispositions[key], "deferred", key)
        self.assertEqual(run.result.check_problems, [], "every deferred answer is one the page offers")

    def test_a_deferred_answer_the_page_does_not_offer_is_a_problem_and_still_not_typed(self):
        schema = fakes.fixture_schema()
        entries = [planned("question_4000000105", "Are you legally authorized to work in the United States?", "Maybe", disposition="deferred",
                           control="select", source="sensitive")]
        plan = FakePlan(entries)
        run = self.go(plan=plan, replan=lambda scan, uploads: plan, schema=schema, inspect=self.state)
        self.assertEqual(run.result.outcome, "rehearsed")
        self.assertEqual([problem["key"] for problem in run.result.check_problems if problem["kind"] == "deferred"], ["question_4000000105"])
        self.assertEqual(run.seen["work_mirror"], "")
        self.assertEqual(run.result.evidence["checked_keys"], ["question_4000000105"])
        self.assertEqual(run.result.evidence["deferred_failed_keys"], ["question_4000000105"], "compared, and the form does not offer it: not a pass")

    def test_a_deferred_answer_the_page_offers_is_checked_and_not_failed(self):
        run = self.go(inspect=self.state)
        self.assertEqual(run.result.evidence["deferred_failed_keys"], [])
        self.assertIn("question_4000000105", run.result.evidence["checked_keys"])

    def deferred_plan(self):
        entries = [
            planned("first_name", "First Name", "Sam", control="text", source="profile"),
            planned("question_4000000105", "Are you legally authorized to work in the United States?", "Yes", disposition="deferred", control="select", source="sensitive"),
            planned("last_name", "Last Name", "Rivera", control="text", source="profile"),
        ]
        return FakePlan(entries)

    def test_a_deferred_field_whose_options_cannot_be_read_is_a_problem_and_the_rehearsal_goes_on(self):
        plan = self.deferred_plan()
        broken = {
            "a playwright error": RuntimeError("the element was covered"),
            "a menu that will not close": apply_agent._Stop("needs_you", apply_agent.FIELD_TOOK.format(question="Are you legally authorized to work in the United States?")),
        }
        for label, error in broken.items():
            with self.subTest(label):
                with mock.patch.object(GreenhouseAdapter, "read_options", side_effect=error):
                    run = self.go(plan=plan, replan=lambda scan, uploads: plan, inspect=self.state)
                self.assertEqual((run.result.outcome, run.result.reasons), ("rehearsed", []), "nothing was ever put in the field, so it cannot have 'not taken' an answer")
                self.assertEqual((run.seen["first"], run.seen["last"]), ("Sam", "Rivera"), "the rest of the form was filled and read back")
                self.assertEqual(run.seen["work_mirror"], "")
                problems = [problem for problem in run.result.check_problems if problem["key"] == "question_4000000105"]
                self.assertEqual([(p["kind"], p["message"]) for p in problems], [(
                    "deferred", apply_agent.DEFERRED_UNREADABLE.format(question="Are you legally authorized to work in the United States?"))])
                self.assertNotIn("the element was covered", json.dumps(run.result.check_problems), "an exception's message is never kept")
                self.assertEqual(run.result.evidence["deferred_failed_keys"], ["question_4000000105"])
                self.assertEqual(run.result.evidence["filled_keys"], ["first_name", "last_name"])
                self.assertTrue([shot for shot in run.result.screenshots if shot["step"] == "filled"], "the rest of the run (check, picture) was not lost")

    def test_a_stop_pressed_while_a_deferred_field_is_read_still_stops_the_run(self):
        plan = self.deferred_plan()
        with mock.patch.object(GreenhouseAdapter, "read_options", side_effect=apply_agent._Stop("failed", apply_agent_types.STOPPED)):
            run = self.go(plan=plan, replan=lambda scan, uploads: plan)
        self.assertEqual((run.result.outcome, run.result.reasons), ("failed", [apply_agent_types.STOPPED]))

    def test_a_native_select_a_multi_select_and_a_checkbox_take_what_the_plan_says(self):
        entries = [
            planned("question_4000000108", "Preferred internship term", "Fall 2027", required=False, control="select", source="answer"),
            planned("question_4000000104", "Which programming languages have you used?", ["Python", "Rust"], required=False, control="multiselect", source="answer"),
            planned("question_4000000113", "Keep me informed about future openings at Example Robotics", False, required=False, control="checkbox", source="answer"),
        ]
        plan = FakePlan(entries)
        run = self.go(plan=plan, replan=lambda scan, uploads: plan, inspect=self.state)
        self.assertEqual(run.result.outcome, "rehearsed", run.result.reasons)
        self.assertEqual(run.seen["term"], "2")
        self.assertEqual(run.seen["languages"], ["Python", "Rust"])
        self.assertFalse(run.seen["informed"], "the pre-ticked box was cleared because the plan said no")

    def test_a_radio_group_ticks_the_input_whose_label_is_the_planned_option(self):
        run = self.go(inspect=self.state)
        self.assertEqual(run.seen["previous"], "0")
        sources = fakes.full_sources(extra_answers=())
        sources.answers[:] = [row for row in sources.answers if row["question"] != "Have you previously worked at Example Robotics?"]
        sources.answers.append({**sources.answers[0], "id": "a-prev", "question": "Have you previously worked at Example Robotics?", "answer": "Yes"})
        again = self.go(sources=sources, inspect=self.state)
        self.assertEqual(again.seen["previous"], "1")

    def test_a_react_select_takes_the_option_with_exactly_the_planned_text_never_the_first(self):
        sources = fakes.full_sources()
        sources.answers[:] = [row for row in sources.answers if row["question"] != "Which team are you most interested in?"]
        sources.answers.append({**sources.answers[0], "id": "a-team", "question": "Which team are you most interested in?", "answer": "Firmware"})
        run = self.go(sources=sources, inspect=self.state)
        self.assertEqual(run.seen["team"], ["Firmware"])
        second = fakes.full_sources(location=LOOKUP_OPTIONS[1])
        run = self.go(sources=second, inspect=self.state)
        self.assertEqual(run.seen["location"], [LOOKUP_OPTIONS[1]], "the second option of the lookup, not the first")

    def test_an_option_the_menu_lacks_ends_the_run_and_chooses_nothing(self):
        listing = fixture_json("schema_new.json")
        team = next(field for block in listing["questions"] for field in block["fields"] if field["name"] == "question_4000000103")
        team["values"].append({"label": "Systems", "value": 4})
        schema = apply_policy.parse_schema(listing)
        sources = fakes.full_sources()
        sources.answers[:] = [row for row in sources.answers if row["question"] != "Which team are you most interested in?"]
        sources.answers.append({**sources.answers[0], "id": "a-team", "question": "Which team are you most interested in?", "answer": "Systems"})
        run = self.go(sources=sources, schema=schema, inspect=self.state)
        self.assertEqual((run.result.outcome, run.result.reasons[0]), ("needs_you", apply_agent.FIELD_TOOK.format(question="Which team are you most interested in?")))
        self.assertEqual(run.seen["team"], [])
        self.assertEqual([shot["step"] for shot in run.result.screenshots], ["needs-you"])

    def test_a_location_label_the_lookup_does_not_list_is_not_replaced_by_the_top_result(self):
        run = self.go(sources=fakes.full_sources(location="Springfield, Other State, United States"), inspect=self.state)
        self.assertEqual((run.result.outcome, run.result.reasons[0]), ("needs_you", apply_agent.FIELD_TOOK.format(question="Location (City)")))
        self.assertEqual(run.seen["location"], [])

    def test_the_policy_blanks_a_css_hidden_listed_field_so_the_plan_never_targets_it(self):
        listing = fixture_json("schema_new.json")
        listing["questions"].append({"description": None, "label": "Leave this empty", "required": False, "fields": [{"name": "website_url", "type": "input_text", "values": []}]})
        schema = apply_policy.parse_schema(listing)
        sources = fakes.full_sources(extra_answers=(answer("Leave this empty", "a bot would fill this in"),))
        run = self.go(sources=sources, schema=schema, inspect=self.state)
        self.assertEqual(run.seen["trap"], "")
        self.assertIn("hidden_control", {entry["kind"] for entry in run.result.join_problems if entry["key"] == "website_url"})
        self.assertEqual(run.result.outcome, "rehearsed")
        trap = [entry for entry in run.result.plan if entry["key"] == "website_url"]
        self.assertEqual(trap and trap[0]["disposition"], "blank")

    def test_a_plan_that_targets_a_css_hidden_field_ends_needs_you_and_leaves_it_empty(self):
        # The policy would blank it (the test above); the agent is handed a plan that does not, and must not type into what a person cannot see.
        trap_plan = FakePlan([
            planned("first_name", "First Name", "Sam", control="text", source="profile"),
            planned("website_url", "Leave this empty", "a bot would fill this in", required=False, control="text", source="answer"),
            planned("last_name", "Last Name", "Rivera", control="text", source="profile"),
        ])
        with mock.patch.object(apply_agent, "ACTION_TIMEOUT_MS", 700):   # Playwright waits for the box to be visible before it types
            run = self.go(plan=trap_plan, replan=lambda scan, uploads: trap_plan, inspect=self.state)
        self.assertEqual((run.result.outcome, run.result.reasons[0]), ("needs_you", apply_agent.FIELD_TOOK.format(question="Leave this empty")))
        self.assertEqual(run.seen["trap"], "", "nothing was typed into the field a person cannot see")
        self.assertNotIn("website_url", run.result.evidence["filled_keys"])
        self.assertNotIn("last_name", run.result.evidence["filled_keys"], "the run stopped there")
        self.assertEqual([shot["step"] for shot in run.result.screenshots], ["needs-you"])

    def test_a_resume_whose_bytes_are_not_the_confirmed_ones_is_not_attached(self):
        files = {"resume": fakes.resume_payload(data=b"%PDF-1.4 a different file")}
        run = self.go(files=files, inspect=self.state)
        self.assertEqual((run.result.outcome, run.result.reasons[0]), ("needs_you", apply_agent.FILE_CHANGED))
        self.assertIsNone(run.seen["resume"])

    def test_a_missing_resume_file_is_a_gap_in_the_rehearsal_not_a_stop(self):
        run = self.go(files={}, inspect=self.state)
        self.assertEqual(run.result.outcome, "rehearsed")
        self.assertIn("resume", [problem["key"] for problem in run.result.check_problems if problem["kind"] == "file"])
        self.assertIsNone(run.seen["resume"])

    def test_a_form_with_more_than_one_page_is_not_rehearsed_as_if_it_were_read_whole(self):
        for scenario in ("next_button", "continue_link", "step_indicator", "continue_to_step", "next_section", "next_review", "page_slash_counter",
                         "next_button_aria", "counter_outside", "next_review_submit", "continue_to_submit", "go_to_step"):
            with self.subTest(scenario=scenario):
                run = self.go(scenario)
                result = run.result
                self.assertEqual((result.outcome, result.reasons), ("needs_you", [apply_agent.MORE_PAGES]))
                self.assertFalse(apply_checks.clean_rehearsal({
                    "outcome": result.outcome, "plan": result.plan, "join_problems": result.join_problems, "check_problems": result.check_problems,
                }))
                self.assertTrue(result.evidence["more_pages"])
                self.assertEqual(result.requests, [])
                self.assertEqual([entry for entry in result.refused if entry["method"] != "GET"], [])

    def test_the_submit_button_and_the_forms_other_buttons_are_not_a_second_page(self):
        run = self.go()
        self.assertEqual(run.result.outcome, "rehearsed")
        self.assertFalse(run.result.evidence["more_pages"])

    def test_a_file_the_form_does_not_accept_is_refused_before_it_is_attached(self):
        payload = fakes.resume_payload(name="resume.exe")
        run = self.go(files={"resume": payload}, inspect=self.state)
        self.assertEqual((run.result.outcome, run.result.reasons[0]), ("needs_you", apply_agent.FILE_TYPE.format(question="Resume/CV")))
        self.assertIsNone(run.seen["resume"])

    def test_a_form_that_uploads_on_attach_defers_the_resume_and_is_not_clean(self):
        run = self.go("s3_upload", inspect=self.state)
        self.assertEqual(run.result.outcome, "rehearsed")
        self.assertIn(apply_agent.S3_NOTE, run.result.reasons)
        self.assertTrue(run.result.evidence["uploads_on_attach"])
        self.assertIsNone(run.seen["resume"], "the file was not attached, so it was not uploaded")
        resume = next(entry for entry in run.result.plan if entry["key"] == "resume")
        self.assertEqual(resume["disposition"], "deferred")
        self.assertFalse(apply_checks.clean_rehearsal({
            "outcome": run.result.outcome, "plan": run.result.plan, "join_problems": [], "check_problems": run.result.check_problems,
        }))

    def test_a_missing_loader_is_noted_and_the_rehearsal_goes_on(self):
        run = self.go("loader_missing")
        self.assertEqual(run.result.outcome, "rehearsed")
        self.assertIn(apply_agent.NO_LOADER, run.result.reasons)
        self.assertFalse(run.result.evidence["loader"]["submit_path"])
        self.assertTrue(run.result.evidence["loader"]["confirmation_path"])

    def test_a_posting_that_sends_applicants_elsewhere_stops_at_the_boundary(self):
        run = self.go("redirect_offsite")
        self.assertEqual((run.result.outcome, run.result.reasons), ("needs_you", [apply_agent.OFFSITE.format(host=OFFSITE_HOST)]))
        self.assertEqual(self.refused(run, rule="offsite_navigation", host=OFFSITE_HOST)[0]["method"], "GET")
        self.assertNotIn(OFFSITE_HOST, [host for _method, host, _path in self.reached(run)])

    def test_a_closed_posting_fails_with_the_closed_sentence(self):
        run = self.go("closed")
        self.assertEqual((run.result.outcome, run.result.reasons), ("failed", [apply_agent.CLOSED]))
        self.assertEqual(run.result.screenshots, [])

    def test_a_legacy_form_is_left_to_the_student(self):
        agent = fakes.RecordingAgent(fake=FakeGreenhouse("confirm"), mode="rehearse", adapter=GreenhouseAdapter(), timeouts=fakes.TEST_TIMEOUTS, lookup_endpoints=())
        with agent:
            result = agent.run(FakePlan([]), page_url=LEGACY_JOB_URL, schema=[], files={}, replan=lambda scan, uploads: FakePlan([]))
        self.assertEqual((result.outcome, result.reasons), ("needs_you", [apply_agent.LEGACY]))

    def test_a_run_stopped_after_the_first_field_sends_nothing_and_takes_no_picture(self):
        holder = {}
        run = self.go(cancelled=lambda: len(holder["agent"].fake.requests) > 1 if "agent" in holder else False, on_progress=lambda agent, step: holder.setdefault("agent", agent))
        self.assertEqual((run.result.outcome, run.result.reasons[0]), ("failed", apply_agent.STOPPED))
        self.assertEqual(run.result.screenshots, [])
        self.assertEqual(run.result.requests, [])

    def test_a_run_stopped_before_it_starts_opens_nothing(self):
        run = self.go(cancelled=lambda: True)
        self.assertEqual((run.result.outcome, run.result.reasons), ("failed", [apply_agent.STOPPED]))
        self.assertEqual(run.record["requests"], [])

    def test_a_plan_that_cannot_be_made_fails_the_run(self):
        def broken(scan, uploads):
            raise ValueError("a message with 4000-0100 in it")

        run = self.go(replan=broken)
        self.assertEqual((run.result.outcome, run.result.reasons), ("failed", [apply_agent.PLAN_FAILED]))
        self.assertNotIn("4000", json.dumps(run.result.reasons))

    def test_the_form_changing_twice_as_it_is_filled_stops_the_run(self):
        calls = []
        sources = fakes.full_sources()
        real = fakes.fixture_replan(sources)

        def replan(scan, uploads):
            calls.append(len(scan))
            return real(scan, uploads)

        def grow(agent, step):
            if step == "fill":
                # The form draws one new field every time the agent scans it, so the form has changed at the second scan and again
                # at the third, however long each step takes (a timer here made the test depend on the machine's speed).
                agent._page.evaluate("""() => {
                  const real = globalThis.OpportunityApplyEngine;
                  let count = 0;
                  globalThis.OpportunityApplyEngine = {...real, scan: (...args) => {
                    count += 1;
                    const box = document.createElement('input');
                    box.type = 'text'; box.id = 'extra_' + count; box.name = 'extra_' + count;
                    document.getElementById('application-form').appendChild(box);
                    return real.scan(...args);
                  }};
                }""")

        run = self.go(replan=replan, on_progress=grow)
        self.assertEqual(run.result.outcome, "needs_you", run.result.reasons)
        self.assertEqual(run.result.reasons[0], apply_agent.KEPT_CHANGING)
        self.assertEqual(len(calls), 2, "planned once at the start and once after the first change")

    def test_adjacent_deferred_react_selects_are_each_closed_after_they_are_read(self):
        # Work authorization and sponsorship, and the whole EEO block, are deferred and sit one under the other. A menu left open
        # covers the next one (the fixture's menu floats over what follows, and ignores a press on its own input, as react-select does).
        def menus(page, _result):
            return page.evaluate("() => Array.from(document.querySelectorAll('.select__input')).map((e) => e.getAttribute('aria-expanded'))")

        run = self.go(inspect=menus)
        self.assertEqual((run.result.outcome, run.result.reasons), ("rehearsed", []))
        self.assertEqual(run.result.check_problems, [])
        deferred = [entry["key"] for entry in run.result.plan if entry["disposition"] == "deferred" and entry["control"] != "file"]
        self.assertGreaterEqual(len(deferred), 5)
        self.assertEqual(set(run.seen), {"false"}, "a menu was left open")

    def test_a_deferred_free_text_answer_needs_only_its_box_on_the_form(self):
        entries = [
            planned("question_4000000107", "What are your salary expectations?", "Negotiable", required=False, disposition="deferred", control="text",
                    source="sensitive", sensitive="compensation"),
            planned("no_such_box", "A box the form lacks", "Whatever", required=False, disposition="deferred", control="text", source="sensitive"),
        ]
        plan = FakePlan(entries)
        run = self.go(plan=plan, replan=lambda scan, uploads: plan, inspect=self.state)
        self.assertEqual(run.result.outcome, "rehearsed", run.result.reasons)
        self.assertEqual([problem["key"] for problem in run.result.check_problems if problem["kind"] == "deferred"], ["no_such_box"],
                         "a free-text answer is not looked up in a list of options")

    def test_a_deferred_statement_box_must_carry_the_stored_statement(self):
        label = "I have read the Example Robotics privacy notice"
        entries = [
            planned("question_4000000109", label, True, disposition="deferred", control="checkbox", source="sensitive", sensitive="acknowledgment",
                    statement=f"Privacy. {label}"),
            planned("question_4000000110", "I certify that the information I have provided is accurate", True, disposition="deferred",
                    control="checkbox", source="sensitive", sensitive="acknowledgment", statement="I agree to the terms of the offer letter"),
        ]
        plan = FakePlan(entries)
        run = self.go(plan=plan, replan=lambda scan, uploads: plan, inspect=self.state)
        self.assertEqual(run.result.outcome, "rehearsed", run.result.reasons)
        self.assertEqual([problem["key"] for problem in run.result.check_problems if problem["kind"] == "deferred"], ["question_4000000110"])
        self.assertEqual((run.seen["privacy"], run.seen["accurate"]), (False, False), "a deferred box is never ticked")

    def test_a_form_that_sends_the_applicant_to_another_posting_is_not_rehearsed_as_this_one(self):
        run = self.go("redirect_other_posting")
        self.assertEqual((run.result.outcome, run.result.reasons), ("needs_you", [apply_agent.DIFFERENT_POSTING]))
        self.assertEqual(run.result.screenshots[0]["step"], "needs-you")

    def test_an_offsite_popup_is_refused_and_stops_the_run_without_calling_it_the_postings_destination(self):
        run = self.go("popup_offsite")
        self.assertEqual((run.result.outcome, run.result.reasons), ("needs_you", [apply_agent.POPUP]))
        self.assertNotIn(OFFSITE_HOST, run.result.reasons[0], "a popup says nothing about where the posting sends applicants")
        refusal = self.refused(run, rule="offsite_navigation", host=OFFSITE_HOST)
        self.assertEqual([entry.get("popup") for entry in refusal], [True], "the refusal is recorded, and marked as a popup's")
        self.assertNotIn(OFFSITE_HOST, [host for _method, host, _path in self.reached(run)])

    def test_a_page_script_that_sends_the_main_frame_away_after_the_first_field_ends_the_run_with_the_postings_destination(self):
        run = self.go("navigate_offsite_after_input", inspect=lambda page, _result: page.url)
        self.assertEqual((run.result.outcome, run.result.reasons), ("needs_you", [apply_agent.OFFSITE.format(host=OFFSITE_HOST)]))
        self.assertEqual(self.refused(run, rule="offsite_navigation", host=OFFSITE_HOST)[0].get("popup"), None, "it is the page's own navigation")
        self.assertNotIn(OFFSITE_HOST, [host for _method, host, _path in self.reached(run)])
        self.assertNotIn("filled", [shot["step"] for shot in run.result.screenshots], "a rehearsal that lost its form is not pictured as filled")
        self.assertEqual(run.result.check_problems, [], "no check was run on a page that was no longer the form")

    def test_an_optional_listed_field_the_page_does_not_draw_is_left_blank_and_the_rest_of_the_form_is_still_rehearsed(self):
        run = self.go("no_portfolio", inspect=lambda page, _result: page.locator("#question_4000000102").count())
        result = run.result
        self.assertEqual((result.outcome, result.reasons), ("rehearsed", []), "nothing stops a run for a field that is not there")
        self.assertEqual(run.seen, 0)
        self.assertEqual(result.join_problems, [], "an optional question the page does not draw is not a disagreement with the listing (6.5, 9.2)")
        field = next(entry for entry in result.plan if entry["key"] == "question_4000000102")
        self.assertEqual(field["disposition"], "blank")
        self.assertEqual(field["problem"], apply_checks.OPTIONAL_NOT_DRAWN_MESSAGE.format(question=field["question"]), "it still shows as left blank, and says why")
        self.assertNotIn("question_4000000102", result.evidence["filled_keys"])
        for key in ("first_name", "last_name", "email", "question_4000000101"):
            self.assertIn(key, result.evidence["filled_keys"], f"{key} was filled and read back")
        self.assertTrue(apply_checks.clean_rehearsal({
            "outcome": result.outcome, "check_problems": result.check_problems,
            # The form's data-consent box carries a statement only the form shows, which the plan honestly cannot match to one the student stored:
            # a gap of its own, so it is left out here. Nothing else is in the way.
            "plan": [entry for entry in result.plan if entry["key"] != "gdpr_consent_given"], "join_problems": result.join_problems,
        }), "an optional field left blank does not make a rehearsal unclean (9.2)")
        gdpr = next(entry for entry in result.plan if entry["key"] == "gdpr_consent_given")
        self.assertTrue(gdpr["problem"] and gdpr["disposition"] == "blank", "the consent box the plan cannot match is still a gap the student sees")

    def test_an_optional_planned_field_that_goes_missing_after_the_plan_is_a_gap_and_not_a_stop(self):
        entries = [
            planned("first_name", "First Name", "Sam", control="text", source="profile"),
            planned("no_such_box", "A box the form lacks", "https://portfolio.example.test/sam", required=False, control="text", source="answer"),
            planned("last_name", "Last Name", "Rivera", control="text", source="profile"),
        ]
        plan = FakePlan(entries)
        run = self.go(plan=plan, replan=lambda scan, uploads: plan, inspect=self.state)
        self.assertEqual((run.result.outcome, run.result.reasons), ("rehearsed", []))
        self.assertEqual((run.seen["first"], run.seen["last"]), ("Sam", "Rivera"), "the fields on either side of it were filled")
        self.assertEqual(run.result.evidence["filled_keys"], ["first_name", "last_name"], "the missing one is not said to be filled")
        missing = [problem for problem in run.result.check_problems if problem["key"] == "no_such_box"]
        self.assertEqual([(problem["kind"], problem["required"], problem["message"]) for problem in missing],
                         [("missing_control", False, apply_agent.NO_CONTROL.format(question="A box the form lacks"))], "said once")

    def test_a_required_planned_field_that_goes_missing_after_the_plan_still_stops_the_run(self):
        entries = [planned("no_such_box", "A box the form lacks", "Whatever", control="text", source="answer")]
        plan = FakePlan(entries)
        run = self.go(plan=plan, replan=lambda scan, uploads: plan)
        self.assertEqual((run.result.outcome, run.result.reasons[0]), ("needs_you", apply_agent.NO_CONTROL.format(question="A box the form lacks")))

    def letter_run(self, *, files=None, check_file="default", sources=None, schema=None, scenario="confirm", **more):
        """A rehearsal of the fictional form with a required cover letter and an approved one to attach."""
        sources = sources or fakes.with_letter(fakes.full_sources())
        files = {"resume": fakes.resume_payload(), "cover_letter": fakes.letter_payload()} if files is None else files
        return self.go(scenario, sources=sources, schema=schema or fakes.schema_with_required_letter(), files=files, check_file=check_file, inspect=self.state,
                       letter_required=True, **more)

    def letter_problems(self, run):
        return [(problem["kind"], problem["message"], problem["required"]) for problem in run.result.check_problems
                if problem["key"] == "cover_letter" and problem["kind"] == "file"]

    def test_an_approved_cover_letter_is_attached_under_its_own_name_and_read_back(self):
        run = self.letter_run()
        self.assertEqual((run.result.outcome, run.result.reasons), ("rehearsed", []))
        self.assertEqual((run.seen["letter"]["name"], run.seen["letter"]["size"]), (fakes.LETTER_NAME, len(fakes.LETTER_BYTES)))
        self.assertIn(fakes.LETTER_NAME, run.seen["letter"]["shown"], "the group shows the name the student sees")
        self.assertEqual(run.seen["resume"], {"name": fakes.RESUME_NAME, "size": len(fakes.RESUME_BYTES)}, "the résumé is attached as before")
        self.assertIn("cover_letter", run.result.evidence["filled_keys"])
        self.assertEqual([problem for problem in run.result.check_problems if problem["key"] == "cover_letter"], [], "the required field is not empty, and nothing else is wrong with it")
        entry = next(item for item in run.result.plan if item["key"] == "cover_letter")
        self.assertEqual((entry["disposition"], entry["source"]["kind"], entry["source"]["ref"]), ("fill", "cover_letter", "doc-1@2"))
        self.assertEqual(run.asked, [("cover_letter", "doc-1@2", fakes.letter_source()["content_sha256"])], "the runner was asked once, just before the file went in")

    def test_the_cover_letter_is_attached_only_after_the_runner_says_it_is_still_the_approved_one(self):
        def broken(key, ref, sha):
            raise RuntimeError("the pipe broke")

        for label, check in (("no", lambda key, ref, sha: False), ("none given", None), ("raises", broken)):
            with self.subTest(label):
                run = self.letter_run(check_file=check)
                self.assertEqual(run.result.outcome, "rehearsed")
                self.assertIsNone(run.seen["letter"], "nothing was attached")
                self.assertEqual(self.letter_problems(run), [("file", apply_agent.LETTER_CHANGED.format(question="Cover Letter"), True)])
                self.assertNotIn("cover_letter", run.result.evidence["filled_keys"])
                self.assertNotIn("the pipe broke", json.dumps(run.result.check_problems), "an exception's message is never kept")
                self.assertEqual(run.seen["resume"]["name"], fakes.RESUME_NAME, "the résumé does not wait for the letter")

    def test_a_letter_whose_bytes_or_text_are_not_the_ones_the_plan_names_is_not_attached(self):
        other_text = fakes.letter_payload(text="Dear Hiring Team,\n\nA different letter.\n")
        own = fakes.letter_payload()
        tampered = fakes.FilePayload(name=fakes.LETTER_NAME, mime_type="application/pdf", buffer=b"%PDF-1.4 other bytes", sha256=own.sha256, content_sha256=own.content_sha256)
        unhashed = fakes.FilePayload(name=fakes.LETTER_NAME, mime_type="application/pdf", buffer=fakes.LETTER_BYTES)
        for label, payload in (("text", other_text), ("bytes", tampered), ("no hashes", unhashed)):
            with self.subTest(label):
                run = self.letter_run(files={"resume": fakes.resume_payload(), "cover_letter": payload})
                self.assertEqual(run.result.outcome, "rehearsed")
                self.assertIsNone(run.seen["letter"])
                self.assertEqual(len(self.letter_problems(run)), 1)
                self.assertEqual(run.asked, [], "the runner is not asked about a file that is already wrong")

    def test_a_letter_under_another_name_than_the_plan_shows_is_not_attached(self):
        renamed = fakes.letter_payload(name="Example-Robotics-Other-Role-cover_letter-v2.pdf")
        run = self.letter_run(files={"resume": fakes.resume_payload(), "cover_letter": renamed})
        self.assertEqual(run.result.outcome, "rehearsed")
        self.assertIsNone(run.seen["letter"], "the preview would name a file the employer did not get")
        self.assertEqual(len(self.letter_problems(run)), 1)
        self.assertEqual(run.asked, [], "the runner is not asked about a file that is already wrong")

    def test_a_missing_letter_file_is_a_gap_in_the_rehearsal(self):
        run = self.letter_run(files={"resume": fakes.resume_payload()})
        self.assertEqual(run.result.outcome, "rehearsed")
        self.assertIsNone(run.seen["letter"])
        self.assertEqual(self.letter_problems(run), [("file", apply_agent.NO_FILE.format(question="Cover Letter"), True)])

    def test_a_letter_the_form_does_not_accept_is_refused_before_it_is_attached(self):
        sources = fakes.with_letter(fakes.full_sources())
        sources = replace(sources, cover_letter={**sources.cover_letter, "file_name": "letter.exe"})
        run = self.letter_run(sources=sources, files={"resume": fakes.resume_payload(), "cover_letter": fakes.letter_payload(name="letter.exe")})
        self.assertEqual((run.result.outcome, run.result.reasons[0]), ("needs_you", apply_agent.FILE_TYPE.format(question="Cover Letter")))
        self.assertIsNone(run.seen["letter"])

    def test_an_optional_cover_letter_is_left_empty_even_when_a_letter_is_approved(self):
        run = self.go(sources=fakes.with_letter(fakes.full_sources()), files={"resume": fakes.resume_payload()}, inspect=self.state)
        self.assertEqual(run.result.outcome, "rehearsed")
        self.assertIsNone(run.seen["letter"])
        entry = next(item for item in run.result.plan if item["key"] == "cover_letter")
        self.assertEqual((entry["disposition"], entry["source"]["kind"]), ("blank", "none"))

    def test_a_form_that_uploads_on_attach_defers_the_letter_too(self):
        run = self.letter_run(scenario="s3_upload")
        self.assertEqual(run.result.outcome, "rehearsed")
        self.assertIsNone(run.seen["letter"], "nothing is uploaded before the student presses Submit")
        entry = next(item for item in run.result.plan if item["key"] == "cover_letter")
        self.assertEqual(entry["disposition"], "deferred")
        self.assertEqual(run.asked, [])

    def test_a_custom_cover_letter_question_is_never_answered_with_the_letter(self):
        listing = fixture_json("schema_new.json")
        listing["questions"].append({"description": None, "label": "Cover letter", "required": False, "fields": [{"name": "question_4000000150", "type": "input_file", "values": []}]})
        listing["questions"].append({"description": None, "label": "Writing sample", "required": True, "fields": [{"name": "question_4000000151", "type": "input_file", "values": []}]})
        plan = fakes.draft_plan(fakes.with_letter(fakes.full_sources()), schema=apply_policy.parse_schema(listing))
        for key in ("question_4000000150", "question_4000000151"):
            entry = next(item for item in plan.fields if item.key == key)
            self.assertEqual((entry.source.kind, entry.file_sha256), ("none", ""), "only the field named cover_letter gets the letter")
        self.assertEqual(next(item for item in plan.fields if item.key == "cover_letter").disposition, "blank", "the named field is optional here, so it is left empty")

    def test_a_missing_resume_keeps_its_own_sentence(self):
        again = self.go(files={})
        resume = [problem for problem in again.result.check_problems if problem["key"] == "resume" and problem["kind"] == "file"]
        self.assertEqual([problem["message"] for problem in resume], [apply_agent.NO_FILE.format(question="Resume/CV")])

    def test_a_rehearsal_that_stopped_before_the_deferred_answers_were_compared_says_it_checked_none_of_them(self):
        listing = fixture_json("schema_new.json")
        team = next(field for block in listing["questions"] for field in block["fields"] if field["name"] == "question_4000000103")
        team["values"].append({"label": "Systems", "value": 4})
        schema = apply_policy.parse_schema(listing)
        sources = fakes.full_sources()
        sources.answers[:] = [row for row in sources.answers if row["question"] != "Which team are you most interested in?"]
        sources.answers.append({**sources.answers[0], "id": "a-team", "question": "Which team are you most interested in?", "answer": "Systems"})
        stopped = self.go(sources=sources, schema=schema)
        self.assertEqual(stopped.result.outcome, "needs_you")
        deferred = [entry["key"] for entry in stopped.result.plan if entry["disposition"] == "deferred" and entry["control"] != "file"]
        self.assertGreaterEqual(len(deferred), 5)
        self.assertEqual(stopped.result.evidence["checked_keys"], [], "the run stopped before the comparison, so none was compared")
        done = self.go()
        self.assertEqual(done.result.evidence["checked_keys"], sorted(deferred), "a finished run compared every deferred answer")


@requires_chromium
class PreSubmitCheckTests(AgentCase):
    """Spec 12.4 through the agent: what ``run`` hands the check, and what the result then says."""

    def not_clean(self, run):
        return not apply_checks.clean_rehearsal({
            "outcome": run.result.outcome, "plan": run.result.plan, "join_problems": run.result.join_problems, "check_problems": run.result.check_problems,
        })

    def problems(self, run, kind):
        return {problem["key"] for problem in run.result.check_problems if problem["kind"] == kind}

    def test_a_value_a_page_script_put_in_a_field_the_plan_did_not_fill_is_a_problem_and_the_run_is_not_clean(self):
        def plant(agent, step):
            if step == "fill":
                agent._page.evaluate("() => { document.getElementById('question_4000000107').value = '90000'; }")

        run = self.go(on_progress=plant)
        self.assertEqual(run.result.outcome, "rehearsed", run.result.reasons)
        self.assertIn("question_4000000107", self.problems(run, "unplanned_value"))
        self.assertTrue(self.not_clean(run))

    def test_required_fields_marked_by_an_asterisk_or_only_by_a_hidden_mirror_that_stay_empty_are_problems(self):
        # A plan that fills only the Portfolio box: First Name (an asterisk) and the Team react-select (only its mirror says required) stay empty.
        entries = [planned("question_4000000102", "Portfolio or project link", fakes.PORTFOLIO, required=False, source="answer")]
        plan = FakePlan(entries)
        run = self.go(plan=plan, replan=lambda scan, uploads: plan)
        self.assertEqual(run.result.outcome, "rehearsed", run.result.reasons)
        empty = self.problems(run, "empty")
        self.assertIn("first_name", empty, "a required field marked only by an asterisk")
        self.assertIn("question_4000000103", empty, "a required react-select marked only by its hidden mirror")
        self.assertTrue(self.not_clean(run))

    def test_a_default_the_page_set_on_an_optional_field_is_not_a_problem(self):
        # The term select starts on its real first option and the "keep me informed" box starts ticked; the plan leaves both alone.
        run = self.go()
        self.assertEqual(run.result.outcome, "rehearsed", run.result.reasons)
        flagged = {problem["key"] for problem in run.result.check_problems}
        self.assertNotIn("question_4000000108", flagged)
        self.assertNotIn("question_4000000113", flagged)
        self.assertEqual(run.result.check_problems, [])


@requires_chromium
class HostileFormTests(AgentCase):
    """What a page's own scripts try while the agent fills, and what reaches the fake."""

    def test_a_script_that_posts_on_every_keystroke_is_refused_and_nothing_reaches_it(self):
        run = self.go("eager_script")
        self.assertEqual(run.result.outcome, "rehearsed")
        posts = self.refused(run, method="POST", host="analytics.example-robotics.test")
        self.assertTrue(posts)
        self.assertNotIn("analytics.example-robotics.test", [host for _m, host, _p in self.reached(run)])
        self.assertTrue(any(entry.get("rule") == "value_guard" for entry in posts))

    def test_a_beacon_that_carries_a_value_is_refused_on_any_host(self):
        for scenario, host in (("eager_get", "pixel.example-robotics.test"), ("eager_get_greenhouse", "job-boards.greenhouse.io")):
            with self.subTest(scenario=scenario):
                run = self.go(scenario)
                self.assertEqual(run.result.outcome, "rehearsed")
                guarded = self.refused(run, rule="value_guard", host=host)
                self.assertTrue(guarded, "the beacon that carried a value was not refused by the value guard")
                self.assertTrue(all(entry.get("field_key") for entry in guarded))
                self.assertEqual([seen for seen in run.record["requests"] if carries_a_value(seen)], [])
                self.assertNotIn("pixel.example-robotics.test", [h for _m, h, _p in self.reached(run)])

    def test_after_the_first_input_only_the_typed_fields_lookup_and_static_assets_get_through(self):
        run = self.go("stray_get")
        self.assertEqual(run.result.outcome, "rehearsed")
        reached = self.reached(run)
        self.assertEqual(reached[0], ("GET", "job-boards.greenhouse.io", JOB_PATH))
        for method, host, path in reached[1:]:
            self.assertEqual((method, host, path), ("GET", API_HOST, "/fake-lookup/location"))
        self.assertTrue(self.refused(run, rule="after_first_input"))
        paths = {path for _m, _h, path in reached}
        self.assertNotIn("/track", paths)
        self.assertNotIn("/fake-lookup/school", paths)

    def test_a_lookup_that_carries_another_fields_value_is_refused_and_the_field_stays_unfilled(self):
        def plant(agent, step):
            if step == "fill":
                agent._page.evaluate("(value) => { document.getElementById('email').value = value; }", EMAIL)

        run = self.go("lookup_leak", on_progress=plant, inspect=self.state)
        self.assertEqual((run.result.outcome, run.result.reasons[0]), ("needs_you", apply_agent.FIELD_TOOK.format(question="Location (City)")))
        refusal = self.refused(run, rule="value_guard", host=API_HOST)
        self.assertTrue(refusal)
        self.assertEqual(refusal[0]["field_key"], "email")
        self.assertEqual(run.seen["location"], [])
        self.assertEqual([seen for seen in run.record["requests"] if carries_a_value(seen)], [])

    def test_every_channel_that_skips_the_route_handler_is_gone_from_every_frame_a_script_can_make(self):
        def kinds(page, _result):
            script = "(names) => names.map((name) => typeof window[name])"
            made = page.evaluate(
                "(names) => { const f = document.createElement('iframe'); document.body.appendChild(f); const g = document.createElement('iframe'); g.srcdoc = '<p>x</p>';"
                " document.body.appendChild(g); const w = window.open('about:blank');"
                " return [f.contentWindow, g.contentWindow, w].map((win) => names.map((name) => typeof win[name])); }", SIDE_CHANNEL_NAMES,
            )
            return [frame.evaluate(script, SIDE_CHANNEL_NAMES) for frame in page.frames if frame.url] + made

        run = self.go(inspect=kinds)
        self.assertEqual(run.result.outcome, "rehearsed")
        self.assertGreaterEqual(len(run.seen), 4, "the page, an iframe, a srcdoc iframe and a popup")
        for found in run.seen:
            self.assertEqual(found, ["undefined"] * len(SIDE_CHANNEL_NAMES))

    def test_no_student_value_is_ever_handed_to_the_pages_javascript(self):
        # Every way Playwright runs a script in the page, watched for the whole run: the script may be given nothing, an element the agent
        # found, or the scan's empty profile and answers, and never a planned value, sensitive or not. (The static test reads the source;
        # this one reads what a real run did.)
        from playwright.sync_api import ElementHandle, Frame, Locator, Page

        calls = []

        def spy(cls, name):
            real = getattr(cls, name)

            def wrapper(self, expression, arg=None, *args, **kwargs):
                calls.append((cls.__name__, name, arg))
                return real(self, expression, arg, *args, **kwargs)

            return mock.patch.object(cls, name, wrapper)

        patches = [spy(cls, name) for cls in (Page, Frame, Locator) for name in ("evaluate", "evaluate_all", "evaluate_handle") if hasattr(cls, name)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        run = self.go(inspect=self.state)
        self.assertEqual(run.result.outcome, "rehearsed", run.result.reasons)
        self.assertGreater(len(calls), 20, "the spy saw the run's scripts")
        handed = [arg for _cls, _name, arg in calls if arg is not None and not isinstance(arg, ElementHandle)]
        self.assertTrue(handed, "the scan's input was seen")
        for arg in handed:
            self.assertEqual(arg, {"profile": {}, "answers": []})
        planned_values = [str(value) for value in GUARDED.values()] + ["Sam", "555-0100"]
        for _cls, _name, arg in calls:
            for value in planned_values:
                self.assertNotIn(value, repr(arg))

    def test_a_websocket_is_refused_and_recorded_and_never_connected(self):
        run = self.go("websocket")
        self.assertEqual(run.result.outcome, "rehearsed")
        sockets = self.refused(run, method="WEBSOCKET")
        self.assertEqual([entry["host"] for entry in sockets], ["socket.example-robotics.test"])
        self.assertEqual(sockets[0]["rule"], "websocket")
        self.assertEqual(run.record["websockets"], [])

    def test_a_script_that_submits_the_form_while_it_is_filled_never_reaches_the_submit_path(self):
        run = self.go("request_submit_during_fill")
        self.assertEqual(run.result.outcome, "rehearsed")
        refused = self.refused(run, method="POST", host=SUBMIT_HOST)
        self.assertTrue(refused, "the page's own submit was not seen and refused")
        self.assertFalse(run.result.evidence["submit_path_hit"])

    def test_a_page_that_would_upload_a_file_on_attach_is_never_asked_to(self):
        run = self.go("s3_upload")
        self.assertEqual(self.refused(run, rule="s3_upload"), [])
        self.assertNotIn("example-robotics-uploads.s3.amazonaws.com", [host for _m, host, _p in self.reached(run)])


SIDE_CHANNEL_NAMES = [
    "RTCPeerConnection", "webkitRTCPeerConnection", "RTCDataChannel", "RTCSessionDescription", "RTCIceCandidate", "fetchLater", "FetchLaterResult",
    "IdentityCredential", "IdentityProvider", "WebTransport", "WebTransportBidirectionalStream", "WebTransportDatagramDuplexStream", "WebSocketStream",
    "Worker", "SharedWorker", "sharedStorage", "SharedStorage", "SharedStorageWorklet",
]
PROTECTED_AUDIENCE_CALLS = ["joinAdInterestGroup", "leaveAdInterestGroup", "runAdAuction", "updateAdInterestGroups", "createAuctionNonce"]
# The agent's launch switches for a test that serves its page from a loopback address: the rule that leaves only Greenhouse's names
# resolvable would leave that address out too, so the test adds it (and nothing else).
LOOPBACK_ARGS = [arg for arg in ApplyAgent.launch_options(True)["args"] if not arg.startswith("--host-resolver-rules")] + [apply_agent.resolver_rule(("127.0.0.1",))]


class Listeners:
    """A loopback server that serves one page and records every request, two TCP listeners (for WebSockets) and a UDP one (for QUIC).

    Each WebSocket gets a listener of its own: Chromium opens one handshake at a time to an address, and a listener that never answers
    would keep the second one waiting, so a channel would look closed when it was only queued.
    """

    PAGE = """<!doctype html><html><body><input id="email" value="sam.rivera@example.test"><script>
      const value = document.getElementById('email').value, base = location.origin, quic = 'https://127.0.0.1:%(udp)d', other = 'http://localhost:%(port)d';
      const stream = 'ws://127.0.0.1:%(stream)d', worker = 'ws://127.0.0.1:%(worker)d', ice = '127.0.0.1:%(rtc)d', iceTcp = '127.0.0.1:%(rtctcp)d';
      const attempt = (name, fn) => { try { fn(); } catch (error) { /* the page cannot, and says nothing */ } };
      attempt('fetchLater', () => fetchLater(base + '/fetch-later?v=' + value, {method: 'POST', body: value, activateAfter: 0}));
      attempt('fedcm', () => navigator.credentials.get({identity: {providers: [{configURL: base + '/fedcm/' + value + '/config.json', clientId: 'x'}]}}).catch(() => 0));
      attempt('stream', () => new WebSocketStream(stream + '/stream?v=' + value));
      attempt('worker', () => new Worker(URL.createObjectURL(new Blob(["new WebSocket('" + worker + "/worker?v=" + value + "')"], {type: 'text/javascript'}))));
      attempt('speculation', () => {
        const rules = document.createElement('script'); rules.type = 'speculationrules';
        rules.textContent = JSON.stringify({prefetch: [{source: 'list', urls: [other + '/spec-prefetch?v=' + value]}], prerender: [{source: 'list', urls: ['/spec-prerender?v=' + value]}]});
        document.head.appendChild(rules);
      });
      attempt('cross-site', () => { new Image().src = other + '/cross-site?v=' + value; });
      attempt('webtransport', () => new WebTransport(quic + '/wt?v=' + value));
      attempt('worker-webtransport', () => new Worker(URL.createObjectURL(new Blob(["new WebTransport('" + quic + "/wtw?v=" + value + "')"], {type: 'text/javascript'}))));
      // An ICE server named by an IP literal needs no name lookup, so the resolver rule does not stand in its way: STUN and TURN go out over a UDP socket of their own.
      attempt('webrtc', () => {
        const peer = new RTCPeerConnection({iceServers: [{urls: 'stun:' + ice}, {urls: 'turn:' + ice, username: value, credential: 'x'}, {urls: 'turn:' + iceTcp + '?transport=tcp', username: value, credential: 'x'}]});
        peer.createDataChannel('x');
        peer.createOffer().then((offer) => peer.setLocalDescription(offer));
      });
      // What a page sends as it goes away: no route handler is called for these once the page is closing, plain requests included.
      for (const event of ['pagehide', 'unload', 'beforeunload', 'visibilitychange']) addEventListener(event, () => {
        attempt('dismissal-image', () => { new Image().src = base + '/dismissal-image-' + event + '?v=' + value; });
        attempt('dismissal-fetch', () => fetch(base + '/dismissal-fetch-' + event + '?v=' + value, {method: 'POST', body: value}));
        attempt('dismissal-xhr', () => { const x = new XMLHttpRequest(); x.open('POST', base + '/dismissal-xhr-' + event + '?v=' + value); x.send(value); });
        attempt('dismissal-link', () => { const l = document.createElement('link'); l.rel = 'stylesheet'; l.href = base + '/dismissal-link-' + event + '?v=' + value; document.head.appendChild(l); });
      });
      addEventListener('pagehide', () => {
        attempt('beacon', () => navigator.sendBeacon(base + '/beacon?v=' + value, value));
        attempt('keepalive', () => fetch(base + '/keepalive?v=' + value, {method: 'POST', body: value, keepalive: true}));
        attempt('keepalive-request', () => fetch(new Request(base + '/keepalive-request?v=' + value, {method: 'POST', body: value, keepalive: true})));
      });
    </script></body></html>"""

    def __init__(self, page=None):
        self.page = page or self.PAGE

    def __enter__(self):
        self.http_paths, self.tcp_lines, self.udp_count, self.rtc_count = [], [], 0, 0
        self.tcp = {}
        for name in ("stream", "worker"):
            self.tcp[name] = socket.socket()
            self.tcp[name].bind(("127.0.0.1", 0))
            self.tcp[name].listen(16)
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", 0))
        self.udp.settimeout(0.2)
        # The ICE server: one UDP socket (STUN, TURN) and a TCP listener (TURN over TCP).
        self.rtc_tcp = socket.socket()
        self.rtc_tcp.bind(("127.0.0.1", 0))
        self.rtc = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtc.bind(("127.0.0.1", 0))
        self.rtc.settimeout(0.2)
        self.rtc_tcp.listen(16)
        listeners = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def handle_any(self):
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                listeners.http_paths.append((self.command, self.path, body.decode("utf-8", "replace")))
                page = (listeners.page % {
                    "stream": listeners.tcp["stream"].getsockname()[1], "worker": listeners.tcp["worker"].getsockname()[1], "udp": listeners.udp.getsockname()[1],
                    "rtc": listeners.rtc.getsockname()[1], "rtctcp": listeners.rtc_tcp.getsockname()[1], "port": listeners.port,
                }).encode() if self.path == "/" else b""
                self.send_response(200 if page else 204)
                if page:
                    self.send_header("content-type", "text/html")
                self.send_header("content-length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)

            do_GET = do_POST = handle_any

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.stopping = False
        for target in (self.server.serve_forever, self.read_udp, self.read_rtc):
            threading.Thread(target=target, daemon=True).start()
        for listener in self.tcp.values():
            threading.Thread(target=self.accept_tcp, args=(listener,), daemon=True).start()
        threading.Thread(target=self.accept_ice_tcp, daemon=True).start()
        return self

    def accept_ice_tcp(self):
        while not self.stopping:
            try:
                connection, _ = self.rtc_tcp.accept()
            except OSError:
                return
            self.rtc_count += 1
            connection.close()

    def accept_tcp(self, listener):
        def read(connection):
            connection.settimeout(2)
            try:
                self.tcp_lines.append(connection.recv(300).decode("latin-1").split("\r\n")[0])
            except OSError:
                pass
            finally:
                connection.close()

        while not self.stopping:
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            threading.Thread(target=read, args=(connection,), daemon=True).start()

    def read_udp(self):
        while not self.stopping:
            try:
                self.udp.recvfrom(2048)
                self.udp_count += 1
            except socket.timeout:
                continue
            except OSError:
                return

    def read_rtc(self):
        while not self.stopping:
            try:
                self.rtc.recvfrom(2048)
                self.rtc_count += 1
            except socket.timeout:
                continue
            except OSError:
                return

    def __exit__(self, *_exc):
        self.stopping = True
        self.server.shutdown()
        self.server.server_close()
        for listener in self.tcp.values():
            listener.close()
        self.udp.close()
        self.rtc.close()
        self.rtc_tcp.close()

    def channels(self):
        """Which of the page's attempts reached a listener."""
        paths = [path for _method, path, _body in self.http_paths]
        reached = {
            "fetchLater": any(path.startswith("/fetch-later") for path in paths),
            "fedcm": any("fedcm" in path or "web-identity" in path for path in paths),
            "stream": any("/stream" in line for line in self.tcp_lines),
            "worker": any("/worker" in line for line in self.tcp_lines),
            "webtransport": self.udp_count > 0,
            "speculation": any(path.startswith("/spec-prefetch") for path in paths),   # a prefetch the browser makes to another site
            "prerender": any(path.startswith("/spec-prerender") for path in paths),    # a prerender it makes of the page's own site
            "cross_site": any(path.startswith("/cross-site") for path in paths),       # a script's own request to another site (nothing routes it here)
            "webrtc": self.rtc_count > 0,
            "dismissal": any(path.startswith("/dismissal-") for path in paths),        # a plain request from a pagehide, unload or visibilitychange handler
            "beacon": any(path.startswith("/beacon") for path in paths),               # sent as the page goes away
            "keepalive": any(path.startswith("/keepalive?") for path in paths),        # a keepalive fetch sent as the page goes away
            "keepalive_request": any(path.startswith("/keepalive-request") for path in paths),   # the same, built from a Request object
            "same_site_speculation": any(path.startswith("/spec-same-") for path in paths),      # what a page whose own scripts fight the sweep still gets out
        }
        return {name for name, seen in reached.items() if seen}


@requires_chromium
class SideChannelTests(unittest.TestCase):
    """Chromium sends some bytes without any request the route handler sees: a script that reads a typed value could carry it out.

    The page here is served by a loopback listener, so nothing about the page's origin or local-network rules is in the way; what is
    measured is whether the listeners hear anything. The control browser has none of the agent's protections and must be heard on every
    channel, which is what makes silence under the agent's own launch options and init script mean something.
    """

    ALL = {"fetchLater", "fedcm", "stream", "worker", "webtransport", "speculation", "prerender", "cross_site", "webrtc", "beacon", "keepalive", "keepalive_request", "dismissal"}
    # A page whose own scripts first overwrite the methods the init script's sweep would use, then insert rules for a same-site prefetch and
    # prerender, at once, a tick later, inside a shadow root and inside an iframe the page makes.
    HOSTILE = """<!doctype html><html><body><input id="email" value="sam.rivera@example.test"><script>
      const value = document.getElementById('email').value;
      Element.prototype.remove = function () {};
      Node.prototype.removeChild = function () { return null; };
      Document.prototype.querySelectorAll = () => [];
      Element.prototype.querySelectorAll = () => [];
      DocumentFragment.prototype.querySelectorAll = () => [];
      NodeList.prototype.forEach = function () {};
      Array.prototype.forEach = function () {};
      Function.prototype.call = function () {};
      Function.prototype.apply = function () {};
      const rules = (tag) => {
        const node = document.createElement('script'); node.type = 'speculationrules';
        node.textContent = JSON.stringify({prefetch: [{source: 'list', urls: ['/spec-same-prefetch-' + tag + '?v=' + value]}], prerender: [{source: 'list', urls: ['/spec-same-prerender-' + tag + '?v=' + value]}]});
        return node;
      };
      document.head.appendChild(rules('now'));
      setTimeout(() => document.head.appendChild(rules('later')), 300);
      try { const host = document.createElement('div'); document.body.appendChild(host); host.attachShadow({mode: 'open'}).appendChild(rules('shadow')); } catch (error) { /* none */ }
      try { const frame = document.createElement('iframe'); document.body.appendChild(frame); frame.contentDocument.head.appendChild(rules('frame')); } catch (error) { /* none */ }
      try { const link = document.createElement('link'); link.rel = 'prerender'; link.href = '/spec-same-link?v=' + value; document.head.appendChild(link); } catch (error) { /* none */ }
    </script></body></html>"""

    def attempt(self, *, args=(), init=None, page=None, route=False, before_close=None):
        """Which channels a listener heard. ``route`` refuses every request but the page's own, as the agent's route handler does."""
        from playwright.sync_api import sync_playwright

        with Listeners(page) as listeners, sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=list(args))
            context = browser.new_context(**ApplyAgent.context_options())
            if init:
                context.add_init_script(init)
            seen = []
            if route:
                def handler(request_route):
                    seen.append(request_route.request.url)
                    if request_route.request.is_navigation_request() and request_route.request.url == f"http://127.0.0.1:{listeners.port}/":
                        request_route.continue_()
                    else:
                        request_route.abort()

                context.route("**/*", handler)
            page = context.new_page()
            page.goto(f"http://127.0.0.1:{listeners.port}/")
            page.wait_for_timeout(2500)   # not time.sleep: Playwright's sync API runs a route handler only while it is being waited on
            if before_close:
                before_close(page)
            page.close()   # fetchLater, a beacon and a keepalive fetch also fire when the page goes away
            time.sleep(1.5)
            context.close()
            browser.close()
            self.routed = seen
            return listeners.channels()

    def test_the_control_is_heard_on_every_channel(self):
        heard = self.attempt()
        # QUIC to a loopback port can be filtered by a sandbox; the four that go over TCP and HTTP must be heard.
        self.assertGreaterEqual(heard, {"fetchLater", "fedcm", "stream", "worker", "speculation", "prerender", "cross_site", "webrtc", "dismissal"}, heard)
        self.assertTrue(heard & {"beacon", "keepalive", "keepalive_request"}, heard)   # Chromium sometimes drops one of the three as the page closes

    def test_the_agents_launch_options_and_init_script_leave_every_listener_silent(self):
        heard = self.attempt(args=LOOPBACK_ARGS, init=apply_agent.NO_SIDE_CHANNELS)
        self.assertEqual(heard, set())

    def test_the_init_script_alone_closes_every_channel_but_a_plain_request_to_another_site(self):
        # Speculation rules (a prefetch to another site, a prerender of this one) are taken out of the page; a script's own request
        # to another site is not the init script's to stop (the agent's route handler refuses it, and the resolver rule cannot resolve it).
        self.assertEqual(self.attempt(init=apply_agent.NO_SIDE_CHANNELS), {"cross_site"})

    def test_the_launch_switches_alone_close_what_they_name(self):
        heard = self.attempt(args=LOOPBACK_ARGS)
        # fetchLater, WebSocketStream and FedCM are the switches' to close, and the resolver rule closes every name but Greenhouse's: a
        # prefetch to another site and a script's request to one go nowhere. Workers, WebTransport, WebRTC (an ICE server named by an IP
        # address needs no lookup) and a prerender of the page's own site have no switch: the init script's alone.
        self.assertEqual(heard & {"fetchLater", "fedcm", "stream", "speculation", "cross_site"}, set())
        self.assertIn("worker", heard, "what no switch closes is still open without the init script")
        self.assertIn("webrtc", heard, "no launch switch silences WebRTC to an ICE server named by an IP address, so the init script is its only layer")

    def test_a_beacon_and_a_keepalive_fetch_sent_as_the_page_closes_are_never_sent(self):
        # The route handler is not called for a request a closing page makes, so the page's own scripts must not be able to make one.
        control = self.attempt(args=LOOPBACK_ARGS, route=True)
        self.assertIn("dismissal", control, "plain requests made as the page closes are heard too, with a route that refuses everything but the page")
        # Chromium sometimes drops one of the three requests a closing page makes at once, so the control needs one of them, not each.
        self.assertTrue(control & {"beacon", "keepalive", "keepalive_request"}, "the control must be heard, with a route that refuses everything but the page")
        heard = self.attempt(args=LOOPBACK_ARGS, init=apply_agent.NO_SIDE_CHANNELS, route=True)
        self.assertEqual(heard & {"beacon", "keepalive", "keepalive_request", "dismissal"}, set())

    def test_a_beacon_is_refused_and_a_keepalive_fetch_is_not_kept_alive_while_the_page_is_open(self):
        from playwright.sync_api import sync_playwright

        with Listeners() as listeners, sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=LOOPBACK_ARGS)
            context = browser.new_context(**ApplyAgent.context_options())
            context.add_init_script(apply_agent.NO_SIDE_CHANNELS)
            page = context.new_page()
            page.goto(f"http://127.0.0.1:{listeners.port}/")
            states = page.evaluate(
                "() => ({beacon: navigator.sendBeacon('/x', 'y'), "
                "kept: (() => { try { return new Request('/x', {method: 'POST', body: 'y', keepalive: true}).keepalive; } catch (error) { return 'threw'; } })(), "
                "plain: new Request('/x').keepalive})")
            frame = page.evaluate(
                "() => { const f = document.createElement('iframe'); document.body.appendChild(f); return f.contentWindow.navigator.sendBeacon('/x', 'y'); }")
            browser.close()
        self.assertIs(states["beacon"], False)
        self.assertIn(states["kept"], (False, "threw"))
        self.assertIs(states["plain"], False)
        self.assertIs(frame, False)

    def test_a_page_that_overwrites_the_methods_the_sweep_uses_still_gets_no_speculation_rule_acted_on(self):
        control = self.attempt(page=self.HOSTILE)
        self.assertIn("same_site_speculation", control, "the control must be heard: the page's rules work when nothing takes them out")
        heard = self.attempt(args=LOOPBACK_ARGS, init=apply_agent.NO_SIDE_CHANNELS, page=self.HOSTILE)
        self.assertNotIn("same_site_speculation", heard)
        self.assertEqual(heard, set())

    def test_no_switch_closes_webrtc_to_an_ice_server_named_by_ip_so_the_init_script_is_its_only_layer(self):
        # Measured on Playwright 1.62's Chromium: STUN over UDP and TURN over TCP both reach a listener under the resolver rule, and none of
        # these switches silences both. If one ever does, add it to LAUNCH_ARGS and turn this into a test that it closes WebRTC.
        for switch in ("--force-webrtc-ip-handling-policy=disable_non_proxied_udp", "--disable-blink-features=RTCPeerConnection", "--disable-features=WebRtcHideLocalIpsWithMdns"):
            with self.subTest(switch=switch):
                self.assertIn("webrtc", self.attempt(args=[*LOOPBACK_ARGS, switch]))
        self.assertNotIn("webrtc", self.attempt(args=LOOPBACK_ARGS, init=apply_agent.NO_SIDE_CHANNELS))

    def test_the_resolver_rule_leaves_the_page_itself_loadable_and_nothing_else_resolvable(self):
        # The control for the two tests above: the page, served from the one address the rule was told about, loaded; any other name did not.
        with Listeners() as listeners:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, args=LOOPBACK_ARGS)
                page = browser.new_context(**ApplyAgent.context_options()).new_page()
                page.goto(f"http://127.0.0.1:{listeners.port}/")
                failed = page.evaluate("(url) => fetch(url, {mode: 'no-cors'}).then(() => 'reached', () => 'failed')", f"http://localhost:{listeners.port}/probe")
                bare = browser.new_context(**ApplyAgent.context_options()).new_page()
                browser.close()
            self.assertEqual(failed, "failed")
            self.assertFalse(any(path.startswith("/probe") for _method, path, _body in listeners.http_paths))
            self.assertTrue(bare is not None)

    def test_the_protected_audience_calls_and_shared_storage_are_gone_from_every_frame(self):
        script = "(names) => names.map((name) => typeof navigator[name])"
        with Listeners() as listeners:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, args=LOOPBACK_ARGS)
                context = browser.new_context(**ApplyAgent.context_options())
                context.add_init_script(apply_agent.NO_SIDE_CHANNELS)
                page = context.new_page()
                page.goto(f"http://127.0.0.1:{listeners.port}/")
                frame = page.evaluate(
                    "(names) => { const f = document.createElement('iframe'); document.body.appendChild(f); return names.map((name) => typeof f.contentWindow.navigator[name]); }",
                    PROTECTED_AUDIENCE_CALLS)
                main = page.evaluate(script, PROTECTED_AUDIENCE_CALLS)
                joined = page.evaluate(
                    "() => { try { return typeof navigator.joinAdInterestGroup === 'function' ? navigator.joinAdInterestGroup({owner: 'https://secret.example', name: 'n'}, 1000).then(() => 'ok', () => 'rejected') : 'gone'; } catch (e) { return 'gone'; } }")
                browser.close()
            self.assertEqual(main, ["undefined"] * len(PROTECTED_AUDIENCE_CALLS))
            self.assertEqual(frame, ["undefined"] * len(PROTECTED_AUDIENCE_CALLS))
            self.assertEqual(joined, "gone")

    def test_a_prerender_link_to_another_site_goes_nowhere_under_the_resolver_rule(self):
        # A link the page adds is acted on by a headed browser with no request the route handler sees; its target still has to resolve.
        with Listeners() as listeners:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, args=LOOPBACK_ARGS)
                context = browser.new_context(**ApplyAgent.context_options())
                context.add_init_script(apply_agent.NO_SIDE_CHANNELS)
                page = context.new_page()
                page.goto(f"http://127.0.0.1:{listeners.port}/")
                page.evaluate(
                    "(url) => { for (const rel of ['prerender', 'prefetch', 'preload']) { const l = document.createElement('link'); l.rel = rel; l.href = url + rel; document.head.appendChild(l); } }",
                    f"http://localhost:{listeners.port}/link-")
                time.sleep(2)
                browser.close()
            self.assertFalse([path for _method, path, _body in listeners.http_paths if path.startswith("/link-")])


@requires_chromium
class LookupTests(AgentCase):
    def lookup(self, typed="Spring", **kwargs):
        return self.go(mode="lookup", plan=None, lookup=LookupRequest("location_city", "location", "Location (City)", typed),
                       replan=None, files={}, **kwargs)

    def test_a_lookup_returns_the_options_the_list_offers_and_picks_none(self):
        run = self.lookup(inspect=self.state)
        self.assertEqual((run.result.outcome, run.result.reasons), ("looked_up", []))
        self.assertEqual(run.result.options, {"location": list(LOOKUP_OPTIONS)})
        self.assertEqual(run.seen["location"], [], "an option was offered, not chosen")
        self.assertEqual(run.result.plan, [])
        self.assertEqual(run.result.screenshots, [], "a lookup takes no picture")
        self.assertEqual(run.steps, ["open", "lookup"])

    def test_after_the_first_input_only_the_lookup_of_that_field_reaches_the_service(self):
        run = self.lookup()
        reached = self.reached(run)
        self.assertEqual(reached[0], ("GET", "job-boards.greenhouse.io", JOB_PATH))
        self.assertTrue(all(entry == ("GET", API_HOST, "/fake-lookup/location") for entry in reached[1:]))
        self.assertEqual(run.result.evidence["lookups"], [{"key": "location_city", "question": "Location (City)", "kind": "location", "typed": True}])
        self.assertFalse(run.result.refused)

    def test_text_that_matches_nothing_gives_no_options_and_says_so(self):
        run = self.lookup("Zzz")
        self.assertEqual((run.result.outcome, run.result.options), ("looked_up", {"location": []}))
        self.assertEqual(run.result.reasons, [apply_agent.NO_OPTIONS])

    def test_with_no_pinned_endpoint_the_service_is_not_asked(self):
        run = self.lookup(lookup_endpoints=())
        self.assertEqual(run.result.options, {"location": []})
        self.assertEqual(run.result.reasons, [apply_agent.NO_OPTIONS, apply_agent.NO_ENDPOINT])
        self.assertEqual(self.reached(run), [("GET", "job-boards.greenhouse.io", JOB_PATH)])
        self.assertTrue(self.refused(run, rule="value_guard"))

    def test_a_field_the_form_does_not_have_is_needs_you(self):
        run = self.go(mode="lookup", plan=None, replan=None, files={}, lookup=LookupRequest("no_such_field", "school", "School", "Uni"))
        self.assertEqual((run.result.outcome, run.result.reasons[0]), ("needs_you", apply_agent.NO_CONTROL.format(question="School")))

    def test_a_lookup_waits_for_the_page_to_settle_before_the_first_input(self):
        order = []
        real_wait, real_type = ApplyAgent._wait, ApplyAgent._type

        def wait(agent, seconds):
            order.append(("wait", seconds))
            real_wait(agent, seconds)

        def typed(agent, *args, **kwargs):
            order.append(("type", 0))
            return real_type(agent, *args, **kwargs)

        settle = apply_agent.ApplyTimeouts(settle_s=0.31, between_fields_s=0, choice_settle_s=1.0, navigation_s=15)
        with mock.patch.object(ApplyAgent, "_wait", wait), mock.patch.object(ApplyAgent, "_type", typed):
            run = self.lookup(timeouts=settle)
        self.assertEqual(run.result.outcome, "looked_up")
        self.assertIn(("wait", 0.31), order)
        self.assertLess(order.index(("wait", 0.31)), order.index(("type", 0)), "the first input came before the settle wait")

    def test_a_lookup_is_stopped_by_a_cancel(self):
        run = self.lookup(cancelled=lambda: True)
        self.assertEqual((run.result.outcome, run.result.reasons), ("failed", [apply_agent.STOPPED]))


@requires_chromium
class MaskingTests(AgentCase):
    def test_every_sensitive_field_is_covered_in_the_picture_and_others_are_not(self):
        sensitive = ["gender", "hispanic_ethnicity", "veteran_status", "disability_status", "question_4000000105", "question_4000000106"]
        plain = ["first_name", "question_4000000101"]

        def pixels(page, result):
            shots = [shot for shot in result.screenshots if shot["step"] == "filled"]
            if not shots:
                return None
            data = base64.b64encode(Path(shots[0]["path"]).read_bytes()).decode()
            boxes = page.evaluate(BOXES_JS, sensitive + plain)
            reader = page.context.new_page()
            try:
                reader.set_content("<canvas></canvas>")
                return reader.evaluate(PIXELS_JS, {"data": data, "masked": boxes[: len(sensitive)], "open": boxes[len(sensitive):]})
            finally:
                reader.close()

        run = self.go(inspect=pixels)
        shot = run.result.screenshots[0]
        self.assertEqual(shot["step"], "filled")
        self.assertTrue(set(sensitive) <= set(shot["masked"]), shot["masked"])
        self.assertTrue(Path(shot["path"]).is_absolute())
        self.assertEqual(shot["sha256"], hashlib.sha256(Path(shot["path"]).read_bytes()).hexdigest())
        self.assertFalse(Path(shot["path"] + ".tmp").exists())
        self.assertIsNotNone(run.seen)
        self.assertEqual(run.seen["masked"], [True] * len(sensitive), "a sensitive field's container was not all mask colour")
        self.assertEqual(run.seen["open_solid"], [False] * len(plain), "a field that is not sensitive was covered")

    def test_a_run_without_a_picture_folder_takes_none(self):
        agent = fakes.RecordingAgent(fake=FakeGreenhouse("confirm"), mode="rehearse", adapter=GreenhouseAdapter(), screenshot_dir=None,
                                     timeouts=fakes.TEST_TIMEOUTS, lookup_endpoints=fakes.FIXTURE_LOOKUP)
        sources = fakes.full_sources()
        with agent:
            result = agent.run(fakes.draft_plan(sources), page_url=JOB_URL, schema=fakes.fixture_schema(), files={"resume": fakes.resume_payload()},
                               replan=fakes.fixture_replan(sources))
        self.assertEqual((result.outcome, result.screenshots), ("rehearsed", []))


@requires_chromium
class LaunchTests(AgentCase):
    def recorded(self, headless):
        calls = {}
        real_launch, real_context = apply_agent._launch_browser, apply_agent._new_context

        def launch(playwright, **options):
            calls["launch"] = options
            return real_launch(playwright, **options)

        def context(browser, **options):
            calls["context"] = options
            return real_context(browser, **options)

        with mock.patch.object(apply_agent, "_launch_browser", launch), mock.patch.object(apply_agent, "_new_context", context):
            self.go(headless=headless, mode="lookup", plan=None, replan=None, files={},
                    lookup=LookupRequest("location_city", "location", "Location (City)", "Spring"))
        return calls

    def test_the_browser_is_launched_with_the_options_the_agent_declares_and_nothing_else(self):
        calls = self.recorded(True)
        self.assertEqual(calls["launch"], ApplyAgent.launch_options(True))
        self.assertEqual(calls["context"], ApplyAgent.context_options())

    @requires_headed
    def test_a_visible_window_opens_the_same_way(self):
        calls = self.recorded(False)
        self.assertEqual(calls["launch"], ApplyAgent.launch_options(False))
        self.assertEqual(calls["launch"], {"headless": False, "args": list(apply_agent.LAUNCH_ARGS)}, "a visible window, and the same switches as the headless one")
        self.assertEqual(calls["context"], ApplyAgent.context_options())


CODE = "12345678"
TEAM = "Which team are you most interested in?"
CODE_BOXES_JS = "() => Array.from(document.querySelectorAll('#security-code input')).map((box) => box.value)"


def sources_without(*questions):
    sources = fakes.full_sources()
    sources.answers[:] = [row for row in sources.answers if row["question"] not in questions]
    return sources


class HandoffCase(unittest.TestCase):
    """A handoff run in a thread, against the fictional Greenhouse, with a test standing in for the student and for the parent.

    ``student`` names a hook of ``fakes.STUDENTS`` (or pass ``hook``, a callable of ``(page, step)``). ``accept`` is what the parent
    answers to the hand-over: True, False, or "raise". After every run: no forbidden button was pressed, nothing but a POST to the
    submit path reached the fake, the agent never asked to press Submit, and the window is closed.
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.dir = Path(self.tempdir.name)

    def handoff(self, scenario="confirm", *, student="complete_and_submit", hook=None, sources=None, schema=None, files="default", link="default",
                accept=True, cancelled=None, timeouts=None, on_progress=None, beat=None, plan=None, label_checkboxes=True, check_file="default", letter_required=False):
        sources = sources or sources_without(TEAM)
        schema = fakes.fixture_schema() if schema is None else schema
        fake = fakes.HandoffGreenhouse(scenario)
        fake.letter_required = letter_required
        link = fakes.FakeLink() if link == "default" else link
        steps, beats, purposes, asked, texts = [], [], [], [], []
        calls = SimpleNamespace(count=0, posts=[], answers=[])
        holder = {}
        files = {"resume": fakes.resume_payload()} if files == "default" else files
        if check_file == "default":
            check_file = (lambda key, ref, sha: asked.append((key, ref, sha)) or True) if "cover_letter" in files else None

        def hand_over():
            calls.count += 1
            calls.posts.append(len(fake.submit_posts()))   # what had reached the fake when the agent asked
            calls.answers.append(accept)
            if accept == "raise":
                raise RuntimeError("the parent is gone")
            return accept is True

        def progress(step, text):
            steps.append(step)
            texts.append((step, text))
            if on_progress:
                on_progress(holder["agent"], step)

        def heartbeat():
            beats.append(time.monotonic())
            if beat:
                beat(holder["agent"])

        agent = fakes.RecordingAgent(
            fake=fake, mode="handoff", adapter=GreenhouseAdapter(), run_id="run-test", screenshot_dir=self.dir,
            timeouts=timeouts or fakes.HANDOFF_TIMEOUTS, lookup_endpoints=fakes.FIXTURE_LOOKUP, on_progress=progress, heartbeat=heartbeat,
            student_hook=hook or fakes.STUDENTS[student],
        )
        holder["agent"] = agent
        real_click = agent._click

        def click(locator, purpose, key=""):
            purposes.append(purpose)
            return real_click(locator, purpose, key)

        agent._click = click
        with agent:
            result = agent.run(
                fakes.draft_plan(sources, schema=schema, mode="handoff") if plan is None else plan, page_url=JOB_URL, schema=schema,
                files=files,
                replan=fakes.fixture_replan(sources, schema=schema, mode="handoff", label_checkboxes=label_checkboxes), hand_over=hand_over,
                cancelled=cancelled, link=link, check_file=check_file,
            )
            record = agent.record()
        run = SimpleNamespace(result=result, record=record, steps=steps, texts=texts, beats=beats, link=link, fake=fake, calls=calls, agent=agent, purposes=purposes, asked=asked)
        self.assertIn(record["forbidden_clicks"], (0, -1), "the agent pressed a button it must never press")
        self.assertEqual([entry for entry in record["non_get"] if not (entry["host"] == SUBMIT_HOST and entry["path"] == JOB_PATH)], [],
                         "a request other than the submit POST reached the fake")
        self.assertNotIn("submit", purposes, "the agent asked to press Submit: only the student does")
        self.assertTrue(agent._page is None or agent._page.is_closed(), "the window was left open")
        return run

    def refused(self, run, **match):
        return [entry for entry in run.result.refused if all(entry.get(name) == value for name, value in match.items())]

    def submit_posts(self, run):
        return len(run.fake.submit_posts())

    def assertSent_nothing(self, run):
        self.assertEqual(self.submit_posts(run), 0, "a submit POST reached the fake")
        self.assertFalse(run.result.handed_over)
        self.assertFalse(run.result.after_click)


@requires_chromium
class HandoffTests(HandoffCase):
    def test_the_student_completes_and_submits(self):
        run = self.handoff()
        result = run.result
        self.assertEqual((result.outcome, result.handed_over, result.after_click, result.confirmation_seen), ("submitted", True, True, True), result.reasons)
        self.assertEqual(run.calls.count, 1)
        self.assertEqual(run.calls.posts, [0], "the POST had already reached Greenhouse when the app asked the parent")
        self.assertEqual(self.submit_posts(run), 1)
        evidence = result.evidence
        self.assertEqual((evidence["handoff_end"], evidence["submit_continued"], evidence["submit_post"], evidence["submit_status"]), ("posted", True, True, 200))
        self.assertTrue(evidence["browser_closed"])
        self.assertTrue(evidence["confirmation_path"])
        self.assertEqual(run.steps[:5], ["open", "read", "fill", "check", "picture"])
        self.assertEqual(run.steps[5:], ["your_turn", "submitting"])
        ready = run.link.ready_messages
        self.assertEqual(len(ready), 1)
        left = {item["key"]: item for item in ready[0]["left"]}
        self.assertIn("question_4000000103", left, "the question with no saved answer is left for the student")
        self.assertTrue(left["question_4000000103"]["reason"])
        entries = {entry["key"]: entry for entry in result.plan}
        self.assertEqual(entries["question_4000000103"]["disposition"], "left_for_you")
        self.assertEqual(entries["first_name"]["disposition"], "fill")
        self.assertEqual(ready[0]["plan_hash"], result.plan_hash)
        self.assertTrue(ready[0]["screenshot"] and Path(ready[0]["screenshot"]["path"]).exists())
        self.assertEqual([entry for entry in result.requests if entry["method"] == "POST" and entry["passed"] and entry["host"] == SUBMIT_HOST][0]["status"], 200)
        for entry in result.requests:
            self.assertEqual(set(entry), {"method", "host", "path", "status", "passed"})
        self.assertTrue(run.beats)

    def test_a_refused_hand_over_aborts_the_post(self):
        run = self.handoff(accept=False)
        self.assertEqual((run.result.outcome, run.result.reasons), ("needs_you", [HANDOFF_UNRECORDED]))
        self.assertSent_nothing(run)
        self.assertEqual(run.result.evidence["handoff_end"], "refused")
        self.assertTrue(self.refused(run, rule="hand_over_refused"))
        self.assertTrue(run.result.evidence["browser_closed"])

    def test_a_hand_over_that_raises_is_refused(self):
        run = self.handoff(accept="raise")
        self.assertEqual((run.result.outcome, run.result.reasons), ("needs_you", [HANDOFF_UNRECORDED]))
        self.assertSent_nothing(run)

    def test_the_timeout_closes_the_browser_before_the_result(self):
        holder = {}

        def watch(page, step):
            holder["page"] = page

        run = self.handoff(hook=watch, timeouts=replace(fakes.HANDOFF_TIMEOUTS, handoff_s=1))
        self.assertEqual((run.result.outcome, run.result.reasons), ("needs_you", [HANDOFF_NOT_SUBMITTED]))
        self.assertSent_nothing(run)
        self.assertEqual(run.result.evidence["handoff_end"], "timeout")
        self.assertTrue(run.result.evidence["browser_closed"])
        page = holder["page"]
        self.assertTrue(page.is_closed(), "the window was still open when the result was returned")
        with self.assertRaises(Exception):
            press_submit(page)
        self.assertEqual(run.calls.count, 0)

    def test_closing_the_window_sends_nothing(self):
        run = self.handoff(student="close_window")
        self.assertEqual((run.result.outcome, run.result.reasons), ("needs_you", [HANDOFF_NOT_SUBMITTED]))
        self.assertEqual(run.result.evidence["handoff_end"], "closed")
        self.assertSent_nothing(run)

    def test_stop_during_the_turn_sends_nothing(self):
        link = fakes.FakeLink()
        run = self.handoff(student="do_nothing", link=link, cancelled=lambda: bool(link.ready_messages))
        self.assertEqual((run.result.outcome, run.result.reasons), ("needs_you", [HANDOFF_NOT_SUBMITTED]))
        self.assertEqual(run.result.evidence["handoff_end"], "stopped")
        self.assertSent_nothing(run)

    def test_closing_the_window_during_the_fill_sends_nothing(self):
        run = self.handoff(on_progress=lambda agent, step: agent._page.close() if step == "fill" else None)
        self.assertEqual((run.result.outcome, run.result.reasons), ("failed", [WINDOW_CLOSED]))
        self.assertEqual(run.result.evidence["handoff_end"], "closed")
        self.assertSent_nothing(run)
        self.assertEqual(run.link.ready_messages, [])

    def test_stop_during_the_fill_sends_nothing(self):
        steps = []
        run = self.handoff(on_progress=lambda agent, step: steps.append(step), cancelled=lambda: "fill" in steps)
        self.assertEqual((run.result.outcome, run.result.reasons), ("failed", [apply_agent.STOPPED]))
        self.assertEqual(run.result.evidence["handoff_end"], "stopped")
        self.assertSent_nothing(run)
        self.assertTrue(run.result.evidence["browser_closed"])

    def test_a_second_submit_is_aborted(self):
        run = self.handoff("double_submit")
        self.assertEqual(run.result.outcome, "submitted", run.result.reasons)
        self.assertEqual(self.submit_posts(run), 1, "the second POST reached Greenhouse")
        self.assertTrue(self.refused(run, rule="second_submit_post"))
        self.assertEqual(run.calls.count, 1)
        self.assertEqual([entry["passed"] for entry in run.result.requests if entry["host"] == SUBMIT_HOST], [True, False])

    def test_a_post_elsewhere_ends_the_turn(self):
        run = self.handoff("other_path_post")
        self.assertEqual((run.result.outcome, run.result.reasons), ("needs_you", [HANDOFF_ELSEWHERE]))
        self.assertSent_nothing(run)
        self.assertEqual(run.calls.count, 0, "the parent was asked to commit a POST that was not the submit path's")
        self.assertEqual(run.result.evidence["handoff_end"], "elsewhere")

    def test_a_form_that_posts_its_application_to_an_address_the_app_does_not_recognize_is_stopped_and_the_student_is_told(self):
        run = self.handoff("form_posts_elsewhere", timeouts=replace(fakes.HANDOFF_TIMEOUTS, handoff_s=4))
        result = run.result
        # The refusal stays: nothing reached the other address, the parent was never asked to commit, and the window stayed the student's.
        self.assertEqual((result.outcome, result.reasons), ("needs_you", [HANDOFF_NOT_SUBMITTED]))
        self.assertEqual(result.evidence["handoff_end"], "timeout")
        self.assertEqual(run.calls.count, 0)
        self.assertSent_nothing(run)
        sent = self.refused(run, host="apply.example-robotics.test")
        self.assertEqual([entry["method"] for entry in sent], ["POST"], "the request to the other address was refused (by the value guard: it carried the email)")
        # ... and the student is told what happened, in the app's words, with the host and without anything the form held.
        told = [text for step, text in run.texts if step == "form_elsewhere"]
        self.assertEqual(told, [PROGRESS_STEPS["form_elsewhere"].format(host="apply.example-robotics.test")])
        self.assertIn("doesn't recognize", told[0])
        self.assertNotIn("Nothing was sent", told[0], "the step is about the stopped request, not about the application")
        self.assertIn("stopped that request", told[0])
        self.assertEqual(result.evidence["elsewhere_seen"], {"host": "apply.example-robotics.test"})
        self.assertLess(run.steps.index("your_turn"), run.steps.index("form_elsewhere"))
        self.assertNotIn("Rivera", json.dumps(result.evidence) + json.dumps(told))

    def test_a_beacon_after_the_press_is_refused_without_telling_the_student_the_form_tried_to_send(self):
        run = self.handoff("telemetry")
        self.assertNotIn("form_elsewhere", run.steps)
        self.assertNotIn("elsewhere_seen", run.result.evidence)

    def test_a_tracker_that_reports_the_submit_click_leaves_no_notice_on_a_submission_that_went_through(self):
        # The tracker's request is refused like any other, but the form's own submission is handed over and sent: nothing the student is told
        # or shown afterwards may say that nothing was sent.
        run = self.handoff("tracker_on_submit")
        result = run.result
        self.assertEqual(result.outcome, "submitted", result.reasons)
        self.assertTrue(result.handed_over)
        self.assertEqual(self.refused(run, host="events.example-analytics.test")[0]["method"], "POST", "the tracker's request was refused")
        self.assertEqual(run.fake.requests_to("events.example-analytics.test"), [])
        self.assertNotIn("elsewhere_seen", result.evidence, "the submission went on, so what the form tried before it is not the run's to report")
        self.assertEqual([text for step, text in run.texts if step == "form_elsewhere" and re.search("nothing was sent", text, re.IGNORECASE)], [])

    def test_telemetry_does_not_end_the_turn(self):
        run = self.handoff("telemetry")
        self.assertEqual(run.result.outcome, "submitted", run.result.reasons)
        telemetry = self.refused(run, rule="telemetry", host="c.spl.greenhouse.io")
        self.assertTrue(any(entry["method"] == "POST" for entry in telemetry), run.result.refused)
        self.assertTrue(any(entry["method"] == "GET" for entry in telemetry), run.result.refused)
        self.assertLessEqual(len([entry for entry in run.result.refused if entry.get("rule") == "telemetry"]), apply_agent.MAX_TELEMETRY_RECORDED)
        self.assertEqual(run.fake.requests_to("c.spl.greenhouse.io"), [])
        self.assertEqual(self.submit_posts(run), 1)

    def test_the_security_code_is_read_and_typed_once(self):
        link = fakes.FakeLink(({"status": "waiting"}, {"status": "waiting"}, {"status": "found", "code": CODE}))
        seen, fronts = {}, []

        def peek(agent, step):
            if step == "code_typed":
                seen["boxes"] = agent._page.evaluate(CODE_BOXES_JS)
                seen["posts"] = len(agent.fake.submit_posts())

        run = self.handoff("security_code", student="press_when_typed", link=link, on_progress=peek)
        result = run.result
        self.assertEqual((result.outcome, result.evidence["security_code"]["typed"]), ("submitted", True), result.reasons)
        self.assertEqual(seen["boxes"], list(CODE), "the eight boxes did not hold the code when the student was told to press Submit")
        self.assertEqual(seen["posts"], 1, "the code was submitted by the app")
        self.assertEqual(self.submit_posts(run), 2, "the 428, then the student's own press")
        self.assertEqual(len(link.asks), 3)
        self.assertEqual(link.reasked, 0, "the agent asked again while a reply was outstanding")
        self.assertEqual(link.results, [(3, True, "")])
        self.assertIn("code_typed", run.steps)
        self.assertEqual(run.purposes.count("submit"), 0)
        evidence = result.evidence["security_code"]
        self.assertEqual((evidence["prompted"], evidence["posted"], evidence["rounds"], evidence["auto_submit_blocked"]), (True, True, 1, False))
        self.assertGreater(len(run.beats), 3, "no heartbeat while the app waited for the code")
        self.assertNotIn(CODE.encode(), pickle.dumps(result))
        self.assertNotIn(CODE, json.dumps(result.evidence) + json.dumps(result.reasons) + repr(run.steps) + repr(link.results) + repr(link.ready_messages))
        self.assertNotIn(CODE, repr(vars(link)))
        self.assertEqual(run.calls.count, 1, "the second POST is the code's, not a second hand-over")

    def test_the_window_is_brought_forward_for_the_turn_and_for_the_code(self):
        calls = []
        link = fakes.FakeLink(({"status": "found", "code": CODE},))
        link.front_requests = 1   # the student pressed "Bring the window forward"

        def watch(agent, step):
            if not calls:
                real = agent._to_front
                agent._to_front = lambda: (calls.append(step), real())[1]

        run = self.handoff("security_code", student="press_when_typed", link=link, on_progress=watch)
        self.assertEqual(run.result.outcome, "submitted", run.result.reasons)
        self.assertGreaterEqual(len(calls), 3, calls)
        self.assertEqual(link.fronts_taken, 1)

    def test_an_auto_submitting_code_widget_sends_nothing_until_the_student_presses(self):
        link = fakes.FakeLink(({"status": "found", "code": CODE},))
        run = self.handoff("security_code_autosubmit", student="press_when_typed", link=link)
        result = run.result
        self.assertEqual(result.outcome, "submitted", result.reasons)
        self.assertTrue(self.refused(run, rule="code_post_while_typing"), "the widget's own submit was not refused")
        evidence = result.evidence["security_code"]
        self.assertTrue(evidence["auto_submit_blocked"])
        self.assertEqual(evidence["reason"], "auto_submit_blocked")
        self.assertIn("code_typed", run.steps, "the code is in the boxes: the student is told to press Submit, not to type it")
        self.assertNotIn("code_yours", run.steps)
        self.assertEqual(self.submit_posts(run), 2, "the 428 and exactly one code POST, the student's own")
        self.assertEqual(link.results, [(1, True, "")])

    def test_a_widget_that_submits_at_once_and_again_after_2_5_seconds_is_refused_until_the_student_presses(self):
        # Owner decision 2026-10-08 (Q4): the code POST waits for the student's press, however long the widget waits and however often it retries.
        link = fakes.FakeLink(({"status": "found", "code": CODE},))
        fakes.PRESSES.clear()
        run = self.handoff("security_code_retry", student="press_after_the_widget_gave_up", link=link)
        result = run.result
        self.assertEqual(result.outcome, "submitted", result.reasons)
        self.assertEqual(len(fakes.PRESSES), 1)
        posts = run.fake.post_times
        self.assertEqual(len(posts), 2, "the 428 and exactly one code POST")
        self.assertGreater(posts[1], fakes.PRESSES[0], "the code POST reached Greenhouse before the student pressed Submit")
        self.assertTrue(self.refused(run, rule="code_post_while_typing") or self.refused(run, rule="code_post_before_press"))
        self.assertGreaterEqual(len(self.refused(run, rule="code_post_before_press")), 1, "the retry after 2.5 s was not refused by name")
        evidence = result.evidence["security_code"]
        self.assertTrue(evidence["auto_submit_blocked"])
        self.assertIn("code_typed", run.steps)
        self.assertNotIn("code_yours", run.steps)
        self.assertEqual(link.results, [(1, True, "")])

    def test_a_retrying_widget_cannot_send_the_app_typed_code_again_at_a_second_prompt_without_a_new_press(self):
        # The press that sent the first code POST is used up. When the code is asked for again, the boxes still hold what the app typed and the
        # widget keeps retrying: every code POST, the second prompt's included, needs a press made after the one before it went through.
        link = fakes.FakeLink(({"status": "found", "code": CODE},))
        fakes.PRESSES.clear()
        run = self.handoff("security_code_retry_twice", student="press_at_each_prompt", link=link,
                           timeouts=replace(fakes.HANDOFF_TIMEOUTS, security_code_s=20, code_read_s=4))
        self.assertEqual(run.result.outcome, "submitted", run.result.reasons)
        self.assertEqual(len(fakes.PRESSES), 2)
        posts = run.fake.post_times
        self.assertEqual(len(posts), 3, "the 428, the first code POST (428 again) and the second code POST")
        self.assertGreater(posts[1], fakes.PRESSES[0])
        self.assertGreater(posts[2], fakes.PRESSES[1], "the second prompt's code POST reached Greenhouse before the student pressed Submit again")

    def test_a_code_widget_cannot_fake_the_students_press(self):
        link = fakes.FakeLink(({"status": "found", "code": CODE},))
        fakes.PRESSES.clear()
        seen = {}

        def look(agent, step):
            if step == "code_typed" and "agent" not in seen:
                seen["agent"] = agent

        def student(page, step):
            agent = seen.get("agent")
            if step == "security_code" and agent is not None and page.locator("#security-input-0").count() and page.input_value("#security-input-0"):
                if page.evaluate("() => !!window.__forged") and "before" not in seen:
                    seen["before"] = (len(agent.fake.submit_posts()), getattr(agent._state, "code_pressed", False), page.evaluate("() => window.__forged"))
            fakes.press_after_the_widget_gave_up(page, step)

        run = self.handoff("security_code_forger", hook=student, link=link, on_progress=look)
        result = run.result
        self.assertEqual(result.outcome, "submitted", result.reasons)
        self.assertIn("before", seen, "the widget never ran its forgeries")
        posts, pressed, forged = seen["before"]
        self.assertEqual(posts, 1, "a forged press let the code POST through")
        self.assertFalse(pressed, "the app counted a press the student never made")
        self.assertEqual(forged["tried"], ["dispatch", "click", "pointer"])
        # The form's own scripts define these three; anything else the page can see that a fresh frame lacks would be the app's.
        self.assertEqual(sorted(forged["functions"]), ["grAfterSubmit", "grLookupUrl", "grValidate"], forged["functions"])
        self.assertEqual(len(run.fake.post_times), 2, "the 428 and exactly one code POST")
        self.assertGreater(run.fake.post_times[1], fakes.PRESSES[0])
        self.assertGreaterEqual(len(self.refused(run, rule="code_post_before_press")), 1)

    def test_a_second_press_while_the_code_post_is_still_answering_is_refused_and_starts_no_second_prompt(self):
        # A real submit takes one to three seconds, and the boxes stay on the page until it answers: that is one prompt, not two.
        link = fakes.FakeLink(({"status": "found", "code": CODE},))
        pressed = []

        def student(page, step):
            if step == "handoff":
                fakes.complete_and_submit(page, step)
            elif step == "security_code" and not pressed and page.locator("#security-input-0").count() and page.input_value("#security-input-0"):
                page.wait_for_timeout(2300)          # the app's own guard against a widget that sends by itself
                pressed.append(time.monotonic())
                press_submit(page)
            elif step in ("security_code", "outcome") and pressed and fakes._once(page, "impatient"):
                press_submit(page)                   # the first answer has not come: a double click, an impatient second press

        run = self.handoff("security_code_slow", hook=student, link=link)
        result = run.result
        self.assertEqual((result.outcome, result.after_click, result.handed_over), ("submitted", True, True), result.reasons)
        self.assertEqual(self.submit_posts(run), 2, "the 428 and the one code POST: the second press went to Greenhouse too")
        self.assertTrue(self.refused(run, rule="second_submit_post"), "the second press was not refused by name")
        self.assertEqual(run.steps.count("security_code"), 1, "the boxes on the page while the code POST was on its way started a second prompt")
        self.assertNotIn("code_yours", run.steps, "the student was told to type and press again while the first press was on its way")
        self.assertEqual(result.evidence["security_code"]["rounds"], 1)
        self.assertEqual(link.results, [(1, True, "")])

    def test_a_press_right_after_the_code_is_typed_goes_through_and_is_not_the_widget_sending_by_itself(self):
        # Before 2026-10-08 the app refused every send for two seconds after the last box. The wait is for the student's press now, so a
        # quick student is not held up, and the press (reported by the browser a few ms before its request) is not mistaken for the widget.
        link = fakes.FakeLink(({"status": "found", "code": CODE},))

        def student(page, step):
            if step == "handoff":
                fakes.complete_and_submit(page, step)
            elif step == "security_code" and page.locator("#security-input-0").count() and page.input_value("#security-input-0") and fakes._once(page, "quick"):
                press_submit(page)                   # at once: the first look after the app told the student

        run = self.handoff("security_code", hook=student, link=link)
        result = run.result
        self.assertEqual(result.outcome, "submitted", result.reasons)
        self.assertEqual(self.refused(run, rule="code_post_before_press"), [], "a real press was refused for want of a press")
        self.assertEqual(self.refused(run, rule="code_post_while_typing"), [])
        evidence = result.evidence["security_code"]
        self.assertFalse(evidence["auto_submit_blocked"], "the student's own press was recorded as the widget sending by itself")
        self.assertNotIn("code_yours", run.steps)
        self.assertEqual(self.submit_posts(run), 2)

    def test_the_enter_key_in_the_form_is_the_students_press_too(self):
        link = fakes.FakeLink(({"status": "found", "code": CODE},))

        def student(page, step):
            if step == "handoff":
                fakes.complete_and_submit(page, step)
            elif step == "security_code" and page.locator("#security-input-0").count() and page.input_value("#security-input-0") and fakes._once(page, "enter"):
                page.focus("#security-input-7")
                page.keyboard.press("Enter")

        run = self.handoff("security_code", hook=student, link=link)
        self.assertEqual(run.result.outcome, "submitted", run.result.reasons)
        self.assertEqual(self.refused(run, rule="code_post_before_press"), [])

    def test_a_script_click_on_submit_is_not_the_students_press(self):
        link = fakes.FakeLink(({"status": "found", "code": CODE},))

        def student(page, step):
            if step == "handoff":
                fakes.complete_and_submit(page, step)
            elif step == "security_code" and page.locator("#security-input-0").count() and page.input_value("#security-input-0") and fakes._once(page, "script"):
                page.evaluate("() => document.querySelector('form#application-form button[type=submit]').click()")

        run = self.handoff("security_code", hook=student, link=link)
        self.assertEqual(self.submit_posts(run), 1, "a click made by script sent the code")
        self.assertTrue(self.refused(run, rule="code_post_before_press"))
        self.assertEqual(run.result.outcome, "needs_you", run.result.reasons)

    def test_a_security_code_left_in_the_boxes_is_covered_in_the_final_picture(self):
        link = fakes.FakeLink(({"status": "found", "code": CODE},))
        seen = {}

        def where(agent, step):
            if step == "code_typed":
                seen["boxes"] = agent._page.evaluate("""() => Array.from(document.querySelectorAll('#security-code input')).map((box) => {
                  const r = box.getBoundingClientRect();
                  return {x: r.x + window.scrollX, y: r.y + window.scrollY, w: r.width, h: r.height};
                })""")

        run = self.handoff("security_code", student="complete_and_submit", link=link, on_progress=where,
                           timeouts=replace(fakes.HANDOFF_TIMEOUTS, code_read_s=3, security_code_s=3))
        result = run.result
        self.assertEqual((result.outcome, result.reasons), ("needs_you", [apply_checks.SECURITY_CODE_NOTE]), "nobody pressed Submit with the code")
        self.assertEqual(result.evidence["security_code"]["typed"], True)
        final = [shot for shot in result.screenshots if shot["step"] == "final"]
        self.assertEqual(len(final), 1, "the run ended with the code on the page and took no final picture to check")
        self.assertEqual(len(seen["boxes"]), 8)
        data = base64.b64encode(Path(final[0]["path"]).read_bytes()).decode()
        from playwright.sync_api import sync_playwright

        with sync_playwright() as driver:
            browser = driver.chromium.launch(headless=True)
            try:
                reader = browser.new_page()
                reader.set_content("<canvas></canvas>")
                covered = reader.evaluate("""(args) => new Promise((resolve) => {
                  const image = new Image();
                  image.onload = () => {
                    const canvas = document.createElement('canvas');
                    canvas.width = image.width; canvas.height = image.height;
                    const context = canvas.getContext('2d');
                    context.drawImage(image, 0, 0);
                    const black = (x, y) => { const d = context.getImageData(Math.round(x), Math.round(y), 1, 1).data; return d[0] === 0 && d[1] === 0 && d[2] === 0 && d[3] === 255; };
                    resolve(args.boxes.map((b) => [[0.3, 0.3], [0.7, 0.3], [0.5, 0.5], [0.3, 0.7], [0.7, 0.7]].every(([u, v]) => black(b.x + b.w * u, b.y + b.h * v))));
                  };
                  image.src = 'data:image/png;base64,' + args.data;
                })""", {"data": data, "boxes": seen["boxes"]})
            finally:
                browser.close()
        self.assertEqual(covered, [True] * 8, "a box that held the emailed code was not all mask colour in the final picture")

    def test_a_slow_reply_is_not_lost(self):
        link = fakes.FakeLink(((3.0, {"status": "found", "code": CODE}),))
        run = self.handoff("security_code", student="press_when_typed", link=link,
                           timeouts=replace(fakes.HANDOFF_TIMEOUTS, code_reply_s=1, code_read_s=5, security_code_s=8))
        self.assertEqual((run.result.outcome, run.result.evidence["security_code"]["typed"]), ("submitted", True), run.result.reasons)
        self.assertEqual((link.asks, link.reasked), ([1], 0), "a second ask was sent while the first had no reply")
        self.assertEqual(link.results, [(1, True, "")])

    def test_a_bad_code_is_not_typed(self):
        link = fakes.FakeLink(({"status": "found", "code": "12 45;78"},))
        done = []

        def student(page, step):
            if step == "handoff":
                fakes.complete_and_submit(page, step)
            elif step == "security_code" and "code_yours" in run_steps and fakes._once(page, "late"):
                fakes.type_security_code(page)
                page.wait_for_timeout(2300)
                press_submit(page)

        run_steps = []
        run = self.handoff("security_code", hook=student, link=link, on_progress=lambda agent, step: run_steps.append(step))
        self.assertEqual(run.result.outcome, "submitted", run.result.reasons)
        self.assertEqual(link.results, [(1, False, "bad_code")])
        self.assertFalse(run.result.evidence["security_code"]["typed"])
        self.assertEqual(run.result.evidence["security_code"]["reason"], "bad_code")
        self.assertIn("code_yours", run.steps)
        self.assertNotIn("code_typed", run.steps)

    def test_a_code_is_not_typed_over_boxes_that_hold_something(self):
        link = fakes.FakeLink(({"status": "found", "code": CODE},))
        steps = []

        def student(page, step):
            if step == "handoff":
                fakes.complete_and_submit(page, step)
            elif step == "security_code" and not steps.count("code_yours") and fakes._once(page, "early"):
                page.fill("#security-input-0", "x")      # the page or the person got there first
            elif step == "security_code" and "code_yours" in steps and fakes._once(page, "late"):
                page.wait_for_timeout(2300)
                press_submit(page)

        run = self.handoff("security_code", hook=student, link=link, on_progress=lambda agent, step: steps.append(step))
        self.assertEqual(link.results, [(1, False, "inputs_not_empty")])
        self.assertEqual(run.result.outcome, "submitted", run.result.reasons)

    def test_a_second_prompt_is_the_students_and_the_code_is_never_typed_twice(self):
        link = fakes.FakeLink(({"status": "found", "code": CODE},))
        run = self.handoff("security_code_twice", student="press_when_typed", link=link,
                           timeouts=replace(fakes.HANDOFF_TIMEOUTS, security_code_s=12, code_read_s=4))
        self.assertEqual(run.result.outcome, "submitted", run.result.reasons)
        self.assertEqual(link.asks, [1], "round 2 asked the reader again")
        self.assertEqual(link.results, [(1, True, "")], "the code was typed more than once")
        self.assertEqual(run.result.evidence["security_code"]["rounds"], 2)
        self.assertEqual(self.submit_posts(run), 3)
        self.assertEqual(run.steps.count("security_code"), 2)

    def test_a_reader_fallback_leaves_the_code_to_the_student(self):
        link = fakes.FakeLink(({"status": "fallback", "reason": "no_mail"},))
        steps = []

        def student(page, step):
            if step == "handoff":
                fakes.complete_and_submit(page, step)
            elif step == "security_code" and "code_yours" in steps and fakes._once(page, "late"):
                fakes.type_security_code(page)
                page.wait_for_timeout(2300)
                press_submit(page)

        run = self.handoff("security_code", hook=student, link=link, on_progress=lambda agent, step: steps.append(step))
        self.assertEqual(run.result.outcome, "submitted", run.result.reasons)
        evidence = run.result.evidence["security_code"]
        self.assertEqual((evidence["typed"], evidence["fallback"]), (False, True))
        self.assertEqual(link.results, [], "the agent reported typing a code it never took")
        self.assertIn("code_yours", run.steps)

    def test_nobody_types_the_code(self):
        run = self.handoff("security_code", timeouts=replace(fakes.HANDOFF_TIMEOUTS, code_read_s=1, security_code_s=2))
        self.assertEqual((run.result.outcome, run.result.after_click, run.result.handed_over), ("needs_you", True, True))
        self.assertEqual(run.result.reasons, [apply_checks.SECURITY_CODE_NOTE])
        self.assertEqual(self.submit_posts(run), 1)

    def test_a_press_during_the_final_picture_is_aborted_and_cannot_contradict_the_outcome(self):
        # The outcome is decided, then the final picture is taken (several round trips) and the window closed. A student who types the
        # code and presses Submit right then must not get a POST through under an outcome that says Submit was not pressed.
        pressed = []

        def arm(agent, step):
            if step == "submitting" and not pressed:
                real = agent._screenshot

                def shot(name):
                    if name == "final" and not pressed:
                        pressed.append(name)
                        fakes.type_security_code(agent._page)
                        press_submit(agent._page)
                    real(name)

                agent._screenshot = shot

        run = self.handoff("security_code", on_progress=arm, timeouts=replace(fakes.HANDOFF_TIMEOUTS, code_read_s=1, security_code_s=2))
        self.assertEqual(pressed, ["final"], "the press during the final picture was never made")
        self.assertEqual(self.submit_posts(run), 1, "a code POST was continued during the final picture")
        self.assertTrue(self.refused(run, rule="closing"), "the press during the picture was not aborted by name")
        self.assertEqual((run.result.outcome, run.result.reasons), ("needs_you", [apply_checks.SECURITY_CODE_NOTE]))

    def test_a_code_the_boxes_did_not_keep_is_not_reported_as_typed(self):
        # A widget that clears what it was given on change: the boxes are empty after the app typed, so the student must be told to type.
        link = fakes.FakeLink(({"status": "found", "code": CODE},))

        def clearing_widget(agent, step):
            if step == "security_code" and fakes._once(agent._page, "widget"):
                agent._page.evaluate("""() => document.querySelectorAll('#security-code input').forEach(
                  (box) => box.addEventListener('change', () => { box.value = ''; }))""")

        run = self.handoff("security_code", link=link, on_progress=clearing_widget,
                           timeouts=replace(fakes.HANDOFF_TIMEOUTS, code_read_s=3, security_code_s=2))
        self.assertEqual(link.results, [(1, False, "typing_failed")], "the boxes were empty and the code was reported as typed")
        evidence = run.result.evidence["security_code"]
        self.assertEqual((evidence["typed"], evidence["fallback"], evidence["reason"]), (False, True, "typing_failed"))
        self.assertIn("code_yours", run.steps)
        self.assertNotIn("code_typed", run.steps)

    def test_a_box_that_will_not_take_its_character_is_the_students_to_finish_not_a_crash(self):
        link = fakes.FakeLink(({"status": "found", "code": CODE},))
        typed = []

        def failing_box(agent, step):
            if step == "security_code" and fakes._once(agent._page, "widget"):
                real = agent._type

                def flaky(locator, value, key, **kwargs):
                    if key == "security_code":
                        typed.append(value)
                        if len(typed) == 3:
                            raise TimeoutError("the box went away")
                    return real(locator, value, key, **kwargs)

                agent._type = flaky

        run = self.handoff("security_code", link=link, on_progress=failing_box,
                           timeouts=replace(fakes.HANDOFF_TIMEOUTS, code_read_s=3, security_code_s=2))
        self.assertEqual(link.results, [(1, False, "typing_failed")], "an exception while typing left the parent without the app's word")
        self.assertEqual((run.result.outcome, run.result.reasons), ("needs_you", [apply_checks.SECURITY_CODE_NOTE]),
                         "the window was closed mid-application instead of left to the student")
        self.assertIn("code_yours", run.steps)

    def test_a_renderer_crash_in_the_students_turn_is_not_the_student_closing_the_window(self):
        def crash(page, step):
            if step == "handoff" and fakes._once(page, "crash"):
                try:
                    page.goto("chrome://crash")
                except Exception:  # noqa: BLE001 - the page dies under the call
                    pass

        run = self.handoff(hook=crash)
        self.assertEqual(run.result.evidence["handoff_end"], "crashed", "a crashed window was recorded as one the student closed")
        self.assertEqual((run.result.outcome, run.result.reasons), ("needs_you", [HANDOFF_CRASHED]))
        self.assertSent_nothing(run)

    def test_an_ask_the_agent_stops_waiting_for_is_abandoned_so_a_late_code_is_never_kept(self):
        # The reader's answer is slower than the window the agent gives it: the student's turn begins, and the ask is dropped.
        link = fakes.FakeLink(((60.0, {"status": "found", "code": CODE}),))
        run = self.handoff("security_code", link=link, timeouts=replace(fakes.HANDOFF_TIMEOUTS, code_read_s=1, security_code_s=2))
        self.assertEqual((run.result.outcome, run.result.reasons), ("needs_you", [apply_checks.SECURITY_CODE_NOTE]))
        self.assertEqual(link.asks, [1])
        self.assertEqual(link.abandoned, [1], "the agent stopped waiting for its ask and did not say so")
        self.assertEqual(link.results, [], "nothing was typed")
        self.assertIn("code_yours", run.steps)

    def test_an_ask_answered_in_time_is_not_abandoned_afterwards(self):
        link = fakes.FakeLink(({"status": "found", "code": CODE},))
        run = self.handoff("security_code", student="press_when_typed", link=link)
        self.assertEqual(run.result.outcome, "submitted", run.result.reasons)
        self.assertEqual(link.abandoned, [])

    def test_a_student_who_presses_with_an_ask_outstanding_abandons_it(self):
        link = fakes.FakeLink(((60.0, {"status": "found", "code": CODE}),))
        run = self.handoff("security_code", student="type_code_and_submit", link=link,
                           timeouts=replace(fakes.HANDOFF_TIMEOUTS, code_read_s=8, security_code_s=8))
        self.assertEqual(run.result.outcome, "submitted", run.result.reasons)
        self.assertEqual(link.abandoned, [1], "the code POST was the student's, and the ask for the emailed code was left outstanding")
        self.assertEqual(link.results, [])

    def test_the_parent_gone_after_hand_over_keeps_the_window_bounded(self):
        link = fakes.FakeLink(({"status": "waiting"},))
        started = time.monotonic()

        def student(page, step):
            if step == "handoff":
                fakes.complete_and_submit(page, step)
            elif step == "security_code":
                link.gone = True

        def beat(agent):
            if link.gone:
                raise OSError("the pipe is closed")

        run = self.handoff("security_code", hook=student, link=link, beat=beat, timeouts=replace(fakes.HANDOFF_TIMEOUTS, code_read_s=2, security_code_s=3))
        self.assertLess(time.monotonic() - started, 40)
        self.assertEqual((run.result.outcome, run.result.after_click), ("needs_you", True), run.result.reasons)
        self.assertEqual(run.result.reasons, [apply_checks.SECURITY_CODE_NOTE], "an exception was reported as the outcome")
        self.assertTrue(run.result.evidence["parent_gone"])
        self.assertLessEqual(len(link.asks), 1, "the agent asked for a code after the parent was gone")
        self.assertIn("code_yours", run.steps)

    def test_sensitive_answers_and_an_exact_statement_are_filled_before_the_turn(self):
        run = self.handoff(student="do_nothing", timeouts=replace(fakes.HANDOFF_TIMEOUTS, handoff_s=1))
        self.assertEqual(run.result.reasons, [HANDOFF_NOT_SUBMITTED])
        ready = run.link.ready_messages[0]
        entries = {entry["key"]: entry for entry in ready["plan"]}
        for key in ("question_4000000105", "question_4000000106", "gender", "question_4000000109", "question_4000000110"):
            self.assertEqual((entries[key]["disposition"], entries[key]["source"]["kind"]), ("fill", "sensitive"), key)
        left = {item["key"]: item for item in ready["left"]}
        self.assertFalse(set(left) & {"question_4000000105", "question_4000000109", "question_4000000110", "gender"})
        # The consent statement is on the page only: the student reads it and ticks it (D9 B), so it is theirs.
        self.assertIn("gdpr_consent_given", left)
        self.assertEqual(entries["gdpr_consent_given"]["disposition"], "left_for_you")
        shot = ready["screenshot"]
        self.assertTrue({"gender", "hispanic_ethnicity", "veteran_status", "disability_status", "question_4000000109", "question_4000000110"} <= set(shot["masked"]), shot["masked"])

    def test_a_statement_that_points_to_other_documents_is_left_for_the_student(self):
        sources = sources_without(TEAM)
        entries = [dict(entry, links=("https://other.example.test/terms",)) if entry["id"] == "store-question_4000000109" else entry
                   for entry in sources.sensitive_lookup.entries]
        sources = replace(sources, sensitive_lookup=Store(*entries))
        run = self.handoff(student="do_nothing", sources=sources, timeouts=replace(fakes.HANDOFF_TIMEOUTS, handoff_s=1))
        ready = run.link.ready_messages[0]
        self.assertIn("question_4000000109", {item["key"] for item in ready["left"]})
        self.assertEqual({entry["key"]: entry["disposition"] for entry in ready["plan"]}["question_4000000109"], "left_for_you")

    def test_a_field_that_does_not_take_is_left_for_the_student(self):
        listing = fixture_json("schema_new.json")
        team = next(field for block in listing["questions"] for field in block["fields"] if field["name"] == "question_4000000103")
        team["values"].append({"label": "Systems", "value": 4})
        sources = fakes.full_sources()
        sources.answers[:] = [row for row in sources.answers if row["question"] != TEAM]
        sources.answers.append({**sources.answers[0], "id": "a-team", "question": TEAM, "answer": "Systems"})
        run = self.handoff(schema=apply_policy.parse_schema(listing), sources=sources)
        self.assertEqual(run.result.outcome, "submitted", run.result.reasons)
        left = {item["key"]: item for item in run.link.ready_messages[0]["left"]}
        self.assertIn("question_4000000103", left)
        self.assertEqual(left["question_4000000103"]["reason"], apply_agent.LEFT_FIELD.format(question=TEAM))
        self.assertEqual({entry["key"]: entry["disposition"] for entry in run.result.plan}["question_4000000103"], "left_for_you")

    def test_a_submit_during_the_fill_stops_the_run(self):
        run = self.handoff("request_submit_during_fill", sources=fakes.full_sources())
        self.assertEqual((run.result.outcome, run.result.reasons), ("needs_you", [HANDOFF_EARLY]))
        self.assertSent_nothing(run)
        self.assertEqual(run.result.evidence["handoff_end"], "early")
        self.assertEqual(run.link.ready_messages, [])

    def test_a_press_after_the_check_is_caught(self):
        def press(agent, step):
            if step == "check":
                # The consent box is the student's to tick (its statement is on the form only); with it ticked the page's own check passes.
                agent._page.evaluate("() => { document.getElementById('gdpr_consent_given').checked = true; document.getElementById('application-form').requestSubmit(); }")

        run = self.handoff(sources=fakes.full_sources(), on_progress=press)
        self.assertEqual((run.result.outcome, run.result.reasons), ("needs_you", [HANDOFF_EARLY]))
        self.assertSent_nothing(run)
        self.assertEqual(run.link.ready_messages, [])

    def test_a_run_that_stopped_before_it_filled_anything_never_says_a_field_was_filled(self):
        for scenario in ("loader_missing", "s3_upload", "request_submit_during_fill"):
            with self.subTest(scenario=scenario):
                run = self.handoff(scenario, student="do_nothing", sources=fakes.full_sources())
                self.assertEqual(run.result.outcome, "needs_you")
                self.assertTrue(run.result.plan, "the run returned no plan at all")
                self.assertEqual([entry["key"] for entry in run.result.plan if entry["disposition"] == "fill"], [],
                                 "a field the run never touched is described as filled")
                untouched = [entry for entry in run.result.plan if entry.get("note") == apply_agent.NOT_FILLED]
                typed = [entry for entry in run.result.plan if entry.get("note") == apply_agent.TYPED_NOT_CHECKED]
                self.assertTrue(untouched, "a field the app would have filled does not say the run stopped first")
                self.assertEqual([entry["key"] for entry in run.result.plan if entry["disposition"] == "fill"], [])
                if scenario == "request_submit_during_fill":
                    # The submit fires 50 ms after the first input event: at least one field had been typed by then, and "nothing was put
                    # in it" would be false of it (the page's own scripts have seen what was typed).
                    self.assertTrue(typed, "a field typed before the stop is described as untouched")
                    self.assertTrue(all(entry["disposition"] == "blank" for entry in typed))
                else:
                    self.assertEqual(typed, [], "a run that stopped before any input claims a field was typed")
                self.assertSent_nothing(run)

    def test_the_fields_a_run_did_fill_and_read_back_are_the_only_ones_called_filled(self):
        run = self.handoff(sources=fakes.full_sources())
        by_key = {entry["key"]: entry for entry in run.result.plan}
        self.assertEqual(by_key["first_name"]["disposition"], "fill")
        self.assertEqual(by_key["email"]["disposition"], "fill")
        self.assertEqual({entry["key"]: entry["disposition"] for entry in run.link.ready_messages[0]["plan"]},
                         {key: entry["disposition"] for key, entry in by_key.items()}, "the list before the press and the result disagree")

    def test_no_loader_or_upload_on_attach_stops_before_any_input(self):
        for scenario, sentence in (("loader_missing", HANDOFF_NO_LOADER), ("loader_other_host", HANDOFF_NO_LOADER), ("s3_upload", HANDOFF_S3)):
            with self.subTest(scenario=scenario):
                run = self.handoff(scenario, student="do_nothing")
                self.assertEqual((run.result.outcome, run.result.reasons), ("needs_you", [sentence]))
                self.assertEqual(run.steps, ["open"], "the form was read or filled")
                self.assertSent_nothing(run)
                self.assertEqual(run.link.ready_messages, [])
                self.assertEqual(run.result.evidence["handoff_end"], "board", "a property of the board, which a second try meets again")

    def test_an_upload_on_attach_without_the_marker_stops_the_run(self):
        run = self.handoff("upload_on_attach_unmarked")
        self.assertEqual((run.result.outcome, run.result.reasons), ("needs_you", [HANDOFF_S3]))
        self.assertEqual(run.result.evidence["upload_refused"], {"host": "example-robotics-uploads.s3.amazonaws.com", "rule": "s3_upload"})
        self.assertEqual(run.result.evidence["handoff_end"], "board", "a board that uploads on attach is a property of the board, not something the student did")
        self.assertEqual(run.link.ready_messages, [])
        self.assertSent_nothing(run)

    def test_an_upload_during_the_turn_ends_it(self):
        run = self.handoff("upload_cover_letter", student="attach_and_upload")
        self.assertEqual((run.result.outcome, run.result.reasons), ("needs_you", [HANDOFF_UPLOAD]))
        self.assertEqual(run.result.evidence["handoff_end"], "upload")
        self.assertEqual(run.calls.count, 0)
        self.assertSent_nothing(run)
        self.assertEqual(len(run.link.ready_messages), 1)

    def test_a_file_posted_to_a_form_address_during_the_turn_is_an_upload_not_an_unknown_address(self):
        def upload(page, step):
            if step == "handoff" and fakes._once(page, "parse"):
                page.evaluate("""() => { const form = new FormData(); form.append('resume', new File(['%PDF-1.4'], 'cv.pdf', {type: 'application/pdf'}));
                  fetch('https://boards-api.greenhouse.io/v1/parse_resume', {method: 'POST', body: form}).catch(() => {}); }""")
                page.wait_for_timeout(800)

        run = self.handoff(hook=upload)
        self.assertEqual((run.result.outcome, run.result.reasons), ("needs_you", [HANDOFF_UPLOAD]))
        self.assertEqual(run.result.evidence["handoff_end"], "upload")
        self.assertSent_nothing(run)

    def test_a_422_after_the_press_names_the_field_and_never_what_was_typed(self):
        run = self.handoff("error_echoes_input")
        self.assertEqual((run.result.outcome, run.result.after_click, run.result.handed_over), ("failed", True, True))
        self.assertEqual(run.result.reasons, ['Greenhouse refused the form (HTTP 422). Greenhouse marked "Email" as wrong'])
        self.assertNotIn("sam.rivera", json.dumps(run.result.evidence) + json.dumps(run.result.reasons) + json.dumps(run.result.requests))

    def test_a_server_error_after_the_press_is_unconfirmed(self):
        run = self.handoff("server_500")
        self.assertEqual((run.result.outcome, run.result.after_click, run.result.handed_over), ("unconfirmed", True, True))
        self.assertEqual(run.result.reasons, [apply_checks.UNCONFIRMED_NOTE])
        self.assertFalse(run.result.confirmation_seen)

    def test_the_page_closing_right_after_the_press_is_unconfirmed(self):
        run = self.handoff(student="press_and_close")
        self.assertEqual((run.result.outcome, run.result.after_click, run.result.handed_over), ("unconfirmed", True, True))
        self.assertNotEqual(run.result.outcome, "failed")
        self.assertEqual(run.result.reasons, [apply_checks.UNCONFIRMED_NOTE])

    def letter_handoff(self, *, student="do_nothing", files=None, check_file="default", seen=None, **more):
        """A Finish in browser run on a form that requires a cover letter, with an approved one to attach. ``seen`` collects the page at the student's turn."""
        files = {"resume": fakes.resume_payload(), "cover_letter": fakes.letter_payload()} if files is None else files

        def watch(page, step):
            if step == "handoff" and seen is not None and fakes._once(page, "look"):
                seen.append(page.evaluate("() => { const f = document.getElementById('cover_letter').files[0]; return f ? {name: f.name, size: f.size} : null; }"))
            fakes.STUDENTS[student](page, step)

        return self.handoff(
            student=student, hook=watch, schema=fakes.schema_with_required_letter(), sources=fakes.with_letter(sources_without(TEAM)), files=files,
            check_file=check_file, letter_required=True, timeouts=replace(fakes.HANDOFF_TIMEOUTS, handoff_s=1) if student == "do_nothing" else None, **more,
        )

    def test_an_approved_cover_letter_is_attached_in_the_window_and_shown_as_filled(self):
        seen = []
        run = self.letter_handoff(seen=seen)
        self.assertEqual(seen, [{"name": fakes.LETTER_NAME, "size": len(fakes.LETTER_BYTES)}], "the file is in the page when the student's turn begins")
        ready = run.link.ready_messages[0]
        for entries in (ready["plan"], run.result.plan):
            letter = next(entry for entry in entries if entry["key"] == "cover_letter")
            self.assertEqual((letter["disposition"], letter["problem"], letter["source"]["ref"]), ("fill", "", "doc-1@2"))
        self.assertNotIn("cover_letter", [item["key"] for item in ready["left"]])
        self.assertEqual(run.asked, [("cover_letter", "doc-1@2", fakes.letter_source()["content_sha256"])])
        self.assertIn("cover_letter", run.result.evidence["filled_keys"])

    def test_the_student_submits_a_form_with_the_letter_the_app_attached(self):
        run = self.letter_handoff(student="complete_and_submit")
        self.assertEqual((run.result.outcome, run.result.handed_over), ("submitted", True), run.result.reasons)
        self.assertIn(fakes.LETTER_NAME, run.fake.submit_posts()[0].post_data, "the file went out under the name the student saw")

    def test_a_cover_letter_that_is_no_longer_the_approved_one_is_left_for_the_student(self):
        seen = []
        run = self.letter_handoff(check_file=lambda key, ref, sha: False, seen=seen)
        self.assertEqual(seen, [None], "nothing was attached")
        ready = run.link.ready_messages[0]
        for entries in (ready["plan"], run.result.plan):
            letter = next(entry for entry in entries if entry["key"] == "cover_letter")
            self.assertEqual((letter["disposition"], letter["problem"]), ("left_for_you", LEFT_COVER_LETTER_CHANGED))
        self.assertIn(LEFT_COVER_LETTER_CHANGED, [item["reason"] for item in ready["left"] if item["key"] == "cover_letter"])
        self.assertNotIn("cover_letter", run.result.evidence["filled_keys"])

    def test_a_cover_letter_with_nothing_approved_is_left_for_the_student(self):
        seen = []
        run = self.handoff(student="do_nothing", schema=fakes.schema_with_required_letter(), sources=sources_without(TEAM), letter_required=True,
                           timeouts=replace(fakes.HANDOFF_TIMEOUTS, handoff_s=1), hook=lambda page, step: seen.append(page.evaluate("document.getElementById('cover_letter').files.length")) if step == "handoff" else None)
        letter = next(entry for entry in run.link.ready_messages[0]["plan"] if entry["key"] == "cover_letter")
        self.assertEqual(letter["disposition"], "left_for_you")
        self.assertEqual(set(seen), {0})
        self.assertEqual(run.asked, [])

    def test_a_hidden_field_the_app_would_have_filled_stops_before_any_input(self):
        listing = fixture_json("schema_new.json")
        listing["questions"].append({"description": None, "label": "Leave this empty", "required": False, "fields": [{"name": "website_url", "type": "input_text", "values": []}]})
        sources = fakes.full_sources(extra_answers=(answer("Leave this empty", "a bot would fill this in"),))
        run = self.handoff(student="do_nothing", schema=apply_policy.parse_schema(listing), sources=sources)
        self.assertEqual((run.result.outcome, run.result.reasons), ("needs_you", [HANDOFF_HIDDEN.format(question="Leave this empty")]))
        self.assertEqual(run.result.evidence["handoff_end"], "board")
        self.assertNotIn("fill", run.steps, "a field was typed into before the hidden one was found")
        self.assertEqual(run.link.ready_messages, [])
        self.assertSent_nothing(run)

    def test_a_join_problem_leaves_the_field_for_the_student_with_the_join_s_own_words(self):
        listing = fixture_json("schema_new.json")
        for question in listing["questions"]:
            if question["label"] == "Last Name":
                question["label"] = "Family name"      # the page still asks for the old wording
        run = self.handoff(student="do_nothing", schema=apply_policy.parse_schema(listing), timeouts=replace(fakes.HANDOFF_TIMEOUTS, handoff_s=1))
        ready = run.link.ready_messages[0]
        left = {item["key"]: item for item in ready["left"]}
        self.assertIn("last_name", left)
        self.assertIn("The form's wording differs", left["last_name"]["reason"])
        self.assertEqual({entry["key"]: entry["disposition"] for entry in ready["plan"]}["last_name"], "left_for_you")

    def test_a_bare_consent_box_whose_words_differ_from_the_listing_is_left_for_the_student_and_not_ticked(self):
        run = self.handoff("consent_box_reworded", student="do_nothing", label_checkboxes=False, timeouts=replace(fakes.HANDOFF_TIMEOUTS, handoff_s=1))
        ready = run.link.ready_messages[0]
        left = {item["key"]: item for item in ready["left"]}
        self.assertIn("The form's wording differs", left["question_4000000110"]["reason"])
        self.assertEqual({entry["key"]: entry["disposition"] for entry in ready["plan"]}["question_4000000110"], "left_for_you")
        self.assertNotIn("question_4000000110", run.result.evidence["filled_keys"], "the box was ticked from the listing's words, not the form's")
        # The box that does say what the listing says is still the app's to tick.
        self.assertNotIn("question_4000000109", left)

    def test_a_bare_consent_box_is_not_left_for_the_student_for_its_wording(self):
        run = self.handoff(student="do_nothing", label_checkboxes=False, timeouts=replace(fakes.HANDOFF_TIMEOUTS, handoff_s=1))
        ready = run.link.ready_messages[0]
        self.assertEqual([item for item in ready["left"] if "wording differs" in item["reason"]], [])

    def test_a_late_fill_shortens_the_turn_and_never_the_time_after_the_press(self):
        t = fakes.HANDOFF_TIMEOUTS
        link = fakes.FakeLink()
        link.ends_at = time.monotonic() + t.after_hand_over_s + 3 * t.outcome_s + 9    # the runner's cap, read from the link
        started = time.monotonic()
        run = self.handoff(student="do_nothing", link=link)
        self.assertEqual((run.result.outcome, run.result.reasons), ("needs_you", [HANDOFF_NOT_SUBMITTED]))
        self.assertEqual(run.result.evidence["handoff_end"], "timeout")
        self.assertLess(time.monotonic() - started, 9 + 6, "the turn ran to handoff_s instead of the time the cap left")

    def test_without_a_link_the_turn_and_the_code_still_run_and_the_student_types_the_code(self):
        run = self.handoff("security_code", student="type_code_and_submit", link=None)
        self.assertEqual(run.result.outcome, "submitted", run.result.reasons)
        self.assertEqual((run.result.evidence["security_code"]["fallback"], run.result.evidence["security_code"]["typed"]), (True, False))

    def test_a_dead_pipe_is_never_an_exception_on_the_heartbeat_path(self):
        def beat(agent):
            raise OSError("the pipe is closed")

        run = self.handoff(student="do_nothing", beat=beat, timeouts=replace(fakes.HANDOFF_TIMEOUTS, handoff_s=1))
        self.assertEqual((run.result.outcome, run.result.reasons), ("needs_you", [HANDOFF_NOT_SUBMITTED]))
        self.assertTrue(run.result.evidence["parent_gone"])

    def test_a_challenge_after_the_press_is_waited_on_and_then_left_to_the_student(self):
        started = time.monotonic()
        run = self.handoff("challenge", timeouts=replace(fakes.HANDOFF_TIMEOUTS, code_read_s=1, security_code_s=2))
        self.assertEqual((run.result.outcome, run.result.after_click, run.result.handed_over), ("needs_you", True, True))
        self.assertEqual(run.result.reasons, [apply_checks.CHALLENGE_NOTE])
        self.assertIn("challenge", run.steps)
        self.assertTrue(run.result.evidence["challenge"])
        self.assertGreaterEqual(time.monotonic() - started, 3, "the app did not wait for the student to finish the check")

    def test_a_recaptcha_frame_that_is_loaded_but_invisible_is_not_a_challenge(self):
        run = self.handoff("bframe_hidden")
        self.assertEqual((run.result.outcome, run.result.after_click), ("failed", True))
        self.assertTrue(run.result.reasons[0].startswith("Greenhouse refused the form (HTTP 422)"), run.result.reasons)
        self.assertNotIn("challenge", run.steps)

    def test_a_rehearsal_reports_the_optional_fields_the_page_set_itself(self):
        sources = fakes.full_sources()
        agent = fakes.RecordingAgent(fake=FakeGreenhouse("confirm"), mode="rehearse", adapter=GreenhouseAdapter(), timeouts=fakes.TEST_TIMEOUTS,
                                     lookup_endpoints=fakes.FIXTURE_LOOKUP)
        with agent:
            result = agent.run(fakes.draft_plan(sources), page_url=JOB_URL, schema=fakes.fixture_schema(), files={"resume": fakes.resume_payload()},
                               replan=fakes.fixture_replan(sources))
        self.assertEqual(result.outcome, "rehearsed")
        self.assertEqual(sorted(result.evidence["page_defaults"]), ["question_4000000108", "question_4000000113"])


@requires_chromium
class HandoffProcessTests(ApplyCase):
    """A handoff in a spawned child, supervised the way the runner does it, with the hand-over committed in a real claim."""

    def setUp(self):
        super().setUp()
        self.pictures = self.root / "pictures"
        self.record = self.root / "record.json"
        self.token = self.start("op-handoff", "handoff", run_id="run-" + "c" * 32)["token"]

    def supervise(self, factory, *, timeouts=None, deadline_s=120, hand_over=None, sources=None):
        sources = sources or sources_without(TEAM)
        job = AgentJob(
            run_id="run-" + "c" * 32, mode="handoff", page_url=JOB_URL, plan=fakes.draft_plan(sources, mode="handoff"), schema=fakes.fixture_schema(),
            files={"resume": fakes.resume_payload()}, lookup=None, screenshot_dir=str(self.pictures), timeouts=timeouts or ApplyTimeouts(),
        )
        log = SimpleNamespace(calls=0, committed_at=None, state="", open_transaction=None, ready=[], progress=[])

        def commit(deadline=None):
            log.calls += 1
            granted = apply_runs.hand_over(self.conn, self.token, user_id=USER, deadline=deadline)
            if granted:
                log.committed_at = time.monotonic()
                # Through another connection: it sees only what was committed, which self.conn's own open write would hide.
                log.state = self.committed_claim_row(self.token)["state"]
                log.open_transaction = self.conn.in_transaction
            return granted

        handlers = apply_runner.SupervisorHandlers(
            progress=lambda step, text: log.progress.append(step), replan=fakes.fixture_replan(sources, mode="handoff", label_checkboxes=True),
            hand_over=hand_over or commit, handoff_ready=log.ready.append,
        )
        done = apply_runner.supervise(factory, job, deadline_s=deadline_s, handlers=handlers, cancel=threading.Event(), poll_s=0.1)
        return done, log

    def factory(self, student="complete_and_submit", scenario="confirm"):
        return fakes.BrowserAgentFactory(scenario, record_path=str(self.record), mode="handoff", student=student)

    def seen(self):
        return json.loads(self.record.read_text(encoding="utf-8"))

    def test_the_claim_is_clicking_before_the_post_reaches_greenhouse(self):
        done, log = self.supervise(self.factory())
        self.assertEqual((done.stop, done.error), ("", ""))
        self.assertEqual(done.result.outcome, "submitted", done.result.reasons)
        self.assertEqual((log.calls, log.state, log.open_transaction), (1, "clicking", False), "the parent was not asked once, or the claim was not committed as clicking")
        seen = self.seen()
        self.assertEqual(len(seen["post_times"]), 1)
        self.assertLess(log.committed_at, seen["post_times"][0], "the POST reached Greenhouse before the hand-over was committed")
        self.assertEqual((seen["forbidden_clicks"], len(log.ready)), (0, 1))
        self.assertTrue(done.closed_confirmed)

    def test_a_stop_pressed_before_the_press_means_the_post_never_reaches_greenhouse(self):
        self.assertTrue(apply_runs.request_cancel(self.conn, self.token, user_id=USER))
        done, log = self.supervise(self.factory())
        self.assertEqual(done.result.outcome, "needs_you")
        self.assertEqual(done.result.reasons, [HANDOFF_UNRECORDED])
        self.assertEqual((self.seen()["post_times"], self.seen()["non_get"]), ([], []))
        row = self.claim_row(self.token)
        self.assertEqual((row["state"], row["handed_over_at"]), ("claimed", None))

    def test_a_hand_over_that_would_be_committed_after_the_child_gave_up_is_refused(self):
        # The child waits reply_s for the answer; the parent keeps 2 s of that for the reply itself. With 1.5 s there is no time at all.
        done, log = self.supervise(self.factory(), timeouts=replace(fakes.HANDOFF_TIMEOUTS, reply_s=1.5))
        self.assertEqual(done.result.reasons, [HANDOFF_UNRECORDED])
        self.assertEqual((self.seen()["post_times"], log.state), ([], ""))
        self.assertEqual(self.claim_row(self.token)["state"], "claimed", "a hand-over committed that the child could not wait for")

    def test_a_driver_that_dies_in_the_turn_leaves_no_browser_behind(self):
        done, log = self.supervise(self.factory(student="die_in_the_turn"))
        self.assertEqual(done.stop, apply_runner.STOP_CHILD_DIED)
        self.assertIsNone(done.result)
        self.assertEqual(log.calls, 0)
        self.assertTrue(done.pids, "no process was seen below the driver: Chromium could not be found by pid")
        for pid in done.pids:
            self.assertFalse(apply_runner.process_alive(pid), f"process {pid} of the run is still alive")
        self.assertTrue(done.closed_confirmed)
        self.assertEqual(self.claim_row(self.token)["state"], "claimed")

    def test_a_browser_that_outlived_its_killed_driver_is_found_and_killed_by_pid(self):
        # The driver is killed and Chromium's helpers are frozen where they are, so some outlive it: the parent's kill by pid is
        # what ends them (a driver that merely dies takes Chromium down with it, and proves nothing about that kill).
        before = {}
        real = apply_runner._kill_survivors

        def look(pids, **kwargs):
            before["alive"] = [pid for pid in pids if apply_runner.process_alive(pid)]
            return real(pids, **kwargs)

        with mock.patch.object(apply_runner, "_kill_survivors", look):
            done, log = self.supervise(self.factory(student="kill_the_driver_then_die"))
        self.assertEqual((done.stop, log.calls), (apply_runner.STOP_CHILD_DIED, 0))
        self.assertTrue(before.get("alive"), "every browser process was already gone when the parent began to kill: nothing proves the kill by pid")
        for pid in done.pids:
            self.assertFalse(apply_runner.process_alive(pid), f"process {pid} of the run is still alive")
        self.assertTrue(done.closed_confirmed)
        self.assertEqual(self.claim_row(self.token)["state"], "claimed")

    def test_a_process_that_cannot_be_killed_makes_the_close_unconfirmed(self):
        with mock.patch.object(apply_runner, "VERIFY_S", 1.0), mock.patch.object(apply_runner, "process_alive", lambda pid: True), \
                mock.patch.object(apply_runner, "_kill_pid", lambda pid: None):
            done, _log = self.supervise(self.factory(student="die_in_the_turn"))
        self.assertFalse(done.closed_confirmed)
        self.assertTrue(done.pids)
        # Clean up what the patch pretended not to kill: only a process that is still the one first seen at its pid (a freed pid may belong
        # to a stranger by now), and never by a bare pid.
        for pid in done.pids:
            kill_if_same_process(pid, done.started.get(pid))   # every process of the run is in done.pids, so no tree walk is needed


@requires_chromium
class ProcessBoundaryTests(unittest.TestCase):
    """The agent in its own spawned process, supervised the way the runner does it."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.dir = Path(self.tempdir.name)

    def job(self, sources):
        return AgentJob(
            run_id="run-" + "b" * 32, mode="rehearse", page_url=JOB_URL, plan=fakes.draft_plan(sources), schema=fakes.fixture_schema(),
            files={"resume": fakes.resume_payload()}, lookup=None, screenshot_dir=str(self.dir),
        )

    def supervise(self, factory, sources, *, deadline_s):
        progress = []
        handlers = apply_runner.SupervisorHandlers(progress=lambda step, text: progress.append(step), replan=fakes.fixture_replan(sources))
        done = apply_runner.supervise(factory, self.job(sources), deadline_s=deadline_s, handlers=handlers, cancel=threading.Event(), poll_s=0.1)
        return done, progress

    def test_a_rehearsal_in_a_child_process_finishes_and_the_fake_saw_no_post(self):
        record = self.dir / "record.json"
        sources = fakes.full_sources()
        done, progress = self.supervise(fakes.BrowserAgentFactory("confirm", record_path=str(record)), sources, deadline_s=90)
        self.assertEqual((done.stop, done.error), ("", ""))
        self.assertEqual(done.result.outcome, "rehearsed", done.result.reasons)
        self.assertEqual(progress, ["open", "read", "fill", "check", "picture"])
        seen = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual((seen["non_get"], seen["submit_path_hit"], seen["forbidden_clicks"]), ([], False, 0))
        self.assertTrue(Path(done.result.screenshots[0]["path"]).exists())

    def test_a_page_that_hangs_the_browser_is_killed_at_the_deadline_with_nothing_left_behind(self):
        sources = fakes.full_sources()
        done, _progress = self.supervise(fakes.BrowserAgentFactory("hang_evaluate"), sources, deadline_s=30)
        self.assertEqual(done.stop, "deadline")
        self.assertIsNone(done.result)
        self.assertGreaterEqual(len(done.killed_pids), 2, "the child and the browser it started were both targeted, on Windows as well as POSIX")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and any(apply_runner.process_alive(pid) for pid in done.killed_pids):
            time.sleep(0.2)
        for pid in done.killed_pids:
            self.assertFalse(apply_runner.process_alive(pid), f"process {pid} of the run is still alive")


class RequiredBrowserTests(unittest.TestCase):
    def test_chromium_is_reported_one_way_or_the_other(self):
        # CI sets PIPELINE_REQUIRE_BROWSER_TESTS=1, and there a missing Chromium errors every class above instead of skipping it.
        self.assertIsInstance(browser_support.chromium_available(), bool)


if __name__ == "__main__":
    unittest.main()
