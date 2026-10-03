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
import socket
import sys
import tempfile
import threading
import time
import unittest
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
from helpers_apply import FakePlan, answer, planned

from opportunity_app.apply import agent as apply_agent
from opportunity_app.apply import checks as apply_checks
from opportunity_app.apply import policy as apply_policy
from opportunity_app.apply import runner as apply_runner
from opportunity_app.apply.agent import ApplyAgent, GreenhouseAdapter
from opportunity_app.apply import agent_types as apply_agent_types
from opportunity_app.apply.agent_types import AgentJob, ApplyTimeouts, LookupRequest

EMAIL = "sam.rivera@example.test"
CHECKBOX_KEYS = {"question_4000000109", "question_4000000110", "question_4000000113", "gdpr_consent_given"}
GUARDED = {"last_name": "Rivera", "email": EMAIL, "question_4000000101": fakes.WHY, "question_4000000102": fakes.PORTFOLIO}
STATE_JS = """() => {
  const one = (id) => document.getElementById(id);
  const mirror = (name) => { const e = document.querySelector('input[name="' + name + '"][aria-hidden]'); return e ? e.value : null; };
  const shown = (name) => Array.from(one(name).closest('.rs').querySelectorAll('.select__single-value, .select__multi-value__label')).map((n) => n.textContent.trim());
  const file = one('resume').files[0];
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
           cancelled=None, lookup_endpoints=fakes.FIXTURE_LOOKUP, inspect=None, on_progress=None, headless=True, timeouts=None):
        sources = sources or fakes.full_sources()
        schema = fakes.fixture_schema() if schema is None else schema
        steps, beats, holder = [], [], {}

        def progress(step, text):
            steps.append(step)
            if on_progress:
                on_progress(holder["agent"], step)

        agent = fakes.RecordingAgent(
            fake=FakeGreenhouse(scenario), mode=mode, adapter=GreenhouseAdapter(), run_id="run-test", screenshot_dir=self.dir,
            timeouts=timeouts or fakes.TEST_TIMEOUTS, lookup_endpoints=lookup_endpoints, on_progress=progress, heartbeat=lambda: beats.append(1),
        )
        agent.headless = headless
        holder["agent"] = agent
        with agent:
            result = agent.run(
                fakes.draft_plan(sources, schema=schema) if plan is None else plan, page_url=JOB_URL, schema=schema,
                files={"resume": fakes.resume_payload()} if files == "default" else files, lookup=lookup,
                replan=replan or fakes.fixture_replan(sources, schema=schema), cancelled=cancelled,
            )
            seen = inspect(agent._page, result) if inspect else None
            record = agent.record()
        # The promise of this milestone, whatever the test was about.
        self.assertEqual(record["non_get"], [], "a request that could carry an application reached the fake")
        self.assertFalse(record["submit_path_hit"])
        # -1 means the run never opened a page, so there was nothing to click.
        self.assertIn(record["forbidden_clicks"], (0, -1) if not record["requests"] else (0,), "the agent pressed a button it must never press")
        self.assertEqual([entry for entry in record["requests"] if entry["method"] not in ("GET", "HEAD", "OPTIONS")], [])
        return SimpleNamespace(result=result, record=record, steps=steps, beats=beats, seen=seen, agent=agent)

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
        self.assertLessEqual({entry["key"] for entry in result.join_problems}, CHECKBOX_KEYS)
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
        # The only join problems are the fictional form's four bare consent boxes (the wording defect in docs/known-defects.md): none is for the field the page lacks.
        self.assertEqual({entry["key"] for entry in result.join_problems} - CHECKBOX_KEYS, set(), "an optional question the page does not draw is not a disagreement with the listing (6.5, 9.2)")
        field = next(entry for entry in result.plan if entry["key"] == "question_4000000102")
        self.assertEqual(field["disposition"], "blank")
        self.assertEqual(field["problem"], apply_checks.OPTIONAL_NOT_DRAWN_MESSAGE.format(question=field["question"]), "it still shows as left blank, and says why")
        self.assertNotIn("question_4000000102", result.evidence["filled_keys"])
        for key in ("first_name", "last_name", "email", "question_4000000101"):
            self.assertIn(key, result.evidence["filled_keys"], f"{key} was filled and read back")
        self.assertTrue(apply_checks.clean_rehearsal({
            "outcome": result.outcome, "check_problems": result.check_problems,
            "plan": [entry for entry in result.plan if entry["key"] not in CHECKBOX_KEYS],
            "join_problems": [entry for entry in result.join_problems if entry["key"] not in CHECKBOX_KEYS],
        }), "an optional field left blank does not make a rehearsal unclean (9.2): nothing but those four consent boxes (the known wording defect) is in the way")

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

    def test_a_required_cover_letter_is_said_to_be_left_for_the_student_and_not_called_unreadable(self):
        entries = [planned("cover_letter", "Cover Letter", None, control="file", source="cover_letter", file_sha256="c" * 64)]
        plan = FakePlan(entries)
        letter = apply_agent_types.FilePayload(name="letter.pdf", mime_type="application/pdf", buffer=b"%PDF-1.4 a letter", sha256="c" * 64)
        run = self.go(plan=plan, replan=lambda scan, uploads: plan, files={"cover_letter": letter}, inspect=lambda page, _result: page.evaluate(
            "() => document.getElementById('cover_letter').files.length"))
        self.assertEqual(run.result.outcome, "rehearsed")
        self.assertEqual(run.seen, 0, "nothing was attached")
        letters = [problem for problem in run.result.check_problems if problem["key"] == "cover_letter"]
        self.assertEqual([(problem["kind"], problem["message"]) for problem in letters],
                         [("file", apply_agent.NO_COVER_LETTER.format(question="Cover Letter"))])
        self.assertNotIn("could not read", letters[0]["message"])
        self.assertNotIn("cover_letter", run.result.evidence["filled_keys"])
        # A missing résumé keeps its own sentence.
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
      const stream = 'ws://127.0.0.1:%(stream)d', worker = 'ws://127.0.0.1:%(worker)d';
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
    </script></body></html>"""

    def __enter__(self):
        self.http_paths, self.tcp_lines, self.udp_count = [], [], 0
        self.tcp = {}
        for name in ("stream", "worker"):
            self.tcp[name] = socket.socket()
            self.tcp[name].bind(("127.0.0.1", 0))
            self.tcp[name].listen(16)
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind(("127.0.0.1", 0))
        self.udp.settimeout(0.2)
        listeners = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def handle_any(self):
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                listeners.http_paths.append((self.command, self.path, body.decode("utf-8", "replace")))
                page = (Listeners.PAGE % {
                    "stream": listeners.tcp["stream"].getsockname()[1], "worker": listeners.tcp["worker"].getsockname()[1], "udp": listeners.udp.getsockname()[1],
                    "port": listeners.port,
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
        for target in (self.server.serve_forever, self.read_udp):
            threading.Thread(target=target, daemon=True).start()
        for listener in self.tcp.values():
            threading.Thread(target=self.accept_tcp, args=(listener,), daemon=True).start()
        return self

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

    def __exit__(self, *_exc):
        self.stopping = True
        self.server.shutdown()
        self.server.server_close()
        for listener in self.tcp.values():
            listener.close()
        self.udp.close()

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
        }
        return {name for name, seen in reached.items() if seen}


@requires_chromium
class SideChannelTests(unittest.TestCase):
    """Chromium sends some bytes without any request the route handler sees: a script that reads a typed value could carry it out.

    The page here is served by a loopback listener, so nothing about the page's origin or local-network rules is in the way; what is
    measured is whether the listeners hear anything. The control browser has none of the agent's protections and must be heard on every
    channel, which is what makes silence under the agent's own launch options and init script mean something.
    """

    ALL = {"fetchLater", "fedcm", "stream", "worker", "webtransport", "speculation", "prerender", "cross_site"}

    def attempt(self, *, args=(), init=None):
        from playwright.sync_api import sync_playwright

        with Listeners() as listeners, sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, args=list(args))
            context = browser.new_context(**ApplyAgent.context_options())
            if init:
                context.add_init_script(init)
            page = context.new_page()
            page.goto(f"http://127.0.0.1:{listeners.port}/")
            time.sleep(2.5)
            page.close()   # fetchLater also fires when the page goes away
            time.sleep(1.5)
            context.close()
            browser.close()
            return listeners.channels()

    def test_the_control_is_heard_on_every_channel(self):
        heard = self.attempt()
        # QUIC to a loopback port can be filtered by a sandbox; the four that go over TCP and HTTP must be heard.
        self.assertGreaterEqual(heard, {"fetchLater", "fedcm", "stream", "worker", "speculation", "prerender", "cross_site"}, heard)

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
        # prefetch to another site and a script's request to one go nowhere. Workers, WebTransport and a prerender of the page's own site
        # have no switch: the init script's alone.
        self.assertEqual(heard & {"fetchLater", "fedcm", "stream", "speculation", "cross_site"}, set())
        self.assertIn("worker", heard, "what no switch closes is still open without the init script")

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
