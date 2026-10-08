"""The apply agent's pure decisions: the outcome table, the request policy, the joins and the pre-submit check.

No browser and no database: this runs in the default suite. The browser tests
(test_apply_fixtures here, test_apply_agent_browser later) only prove that the
observations these functions read are gathered correctly.
"""

import base64
import json
import re
import sys
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from opportunity_app.apply import checks as apply_checks
from opportunity_app.apply.checks import (
    CAPTCHA_ENDPOINTS,
    PHASE_AFTER_HAND_OVER,
    PHASE_AFTER_INPUT,
    PHASE_BEFORE_INPUT,
    PHASE_FILL,
    PHASE_STUDENT,
    REQUIRED_CHECK_SCRIPT,
    Abort,
    Allow,
    Endpoint,
    Observation,
    Problem,
    RouteRequest,
    RouteState,
    SeenRequest,
    check_required,
    clean_rehearsal,
    decide_outcome,
    is_upload,
    join,
    leaked_field,
    question_key,
    route_decision,
    student_submit_elsewhere,
)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers_apply import FakePlan, planned  # noqa: E402  (also installs tests/realdata_guard.py)

TOKEN, JOB = "examplerobotics", "4000000001"
SUBMIT_PATH = f"/{TOKEN}/jobs/{JOB}"
CONFIRMATION_PATH = f"{SUBMIT_PATH}/confirmation"
LOOKUP = Endpoint("boards-api.greenhouse.io", "/fake-lookup/", "location")
LOOKUP_LOCATION = Endpoint("boards-api.greenhouse.io", "/fake-lookup/location", "location")
LOOKUP_SCHOOL = Endpoint("boards-api.greenhouse.io", "/fake-lookup/school", "school")
EMAIL = "sam.rivera@example.test"


# --- decide_outcome (spec 6.14) ---------------------------------------------------------------

def post(status, *, path=SUBMIT_PATH, host="boards.greenhouse.io", passed=True, method="POST"):
    return SeenRequest(method, host, path, status, passed)


def seen(*requests, **overrides):
    values = dict(main_path=SUBMIT_PATH, form_present=True, requests=tuple(requests), submit_path=SUBMIT_PATH,
                  confirmation_path=CONFIRMATION_PATH, board_token=TOKEN, job_id=JOB)
    values.update(overrides)
    return Observation(**values)


class DecideOutcomeTests(unittest.TestCase):
    def confirmed(self, status=303, **overrides):
        return seen(post(status), main_path=CONFIRMATION_PATH, form_present=False, navigated=True, **overrides)

    def test_a_303_then_the_confirmation_path_with_the_form_gone_is_submitted(self):
        outcome = decide_outcome(self.confirmed())
        self.assertEqual((outcome.outcome, outcome.after_click, outcome.resolved_by), ("submitted", 1, "page"))
        self.assertTrue(outcome.settled)
        self.assertEqual(outcome.evidence["submit_status"], 303)
        self.assertTrue(outcome.evidence["form_absent"])
        self.assertEqual(outcome.detail, {})

    def test_a_2xx_answer_counts_the_same_as_a_redirect(self):
        self.assertEqual(decide_outcome(self.confirmed(200)).outcome, "submitted")

    def test_the_boards_own_confirmation_path_matches_even_when_the_loader_named_another(self):
        for path in (f"/{TOKEN}/jobs/{JOB}/confirmation", f"/{TOKEN}/jobs/{JOB}/confirmation/"):
            with self.subTest(path=path):
                obs = seen(post(303), main_path=path, form_present=False, confirmation_path="/somewhere/else")
                self.assertEqual(decide_outcome(obs).outcome, "submitted")

    def test_the_embed_confirmation_needs_the_same_board_and_job(self):
        good = seen(post(303), main_path="/embed/job_app/confirmation", main_query=f"for={TOKEN}&token={JOB}", form_present=False,
                    confirmation_path="")
        self.assertEqual(decide_outcome(good).outcome, "submitted")
        for query in (f"for=other&token={JOB}", f"for={TOKEN}&token=1", ""):
            with self.subTest(query=query):
                bad = seen(post(303), main_path="/embed/job_app/confirmation", main_query=query, form_present=False, confirmation_path="")
                self.assertEqual(decide_outcome(bad).outcome, "unconfirmed")

    def test_the_confirmation_path_with_the_form_still_present_is_unconfirmed(self):
        obs = seen(post(303), main_path=CONFIRMATION_PATH, form_present=True, navigated=True)
        outcome = decide_outcome(obs)
        self.assertEqual((outcome.outcome, outcome.after_click), ("unconfirmed", 1))
        self.assertIn("Look for its email", outcome.note)

    def test_a_thank_you_page_at_the_same_address_with_the_form_present_is_unconfirmed(self):
        # The employer edits that page's wording, so the words prove nothing: the path and the form decide.
        obs = seen(post(200), main_path=SUBMIT_PATH, form_present=True, navigated=True)
        self.assertEqual(decide_outcome(obs).outcome, "unconfirmed")

    def test_a_server_error_is_unconfirmed(self):
        outcome = decide_outcome(seen(post(500)))
        self.assertEqual((outcome.outcome, outcome.after_click), ("unconfirmed", 1))
        self.assertFalse(outcome.settled)

    def test_a_post_that_never_answered_is_unconfirmed(self):
        self.assertEqual(decide_outcome(seen(post(None))).outcome, "unconfirmed")

    def test_a_redirect_without_a_confirmation_page_is_unconfirmed(self):
        self.assertEqual(decide_outcome(seen(post(303), main_path=SUBMIT_PATH, navigated=True)).outcome, "unconfirmed")

    def test_a_navigation_to_the_confirmation_path_without_a_submit_post_is_unconfirmed(self):
        obs = seen(main_path=CONFIRMATION_PATH, form_present=False, navigated=True)
        outcome = decide_outcome(obs)
        self.assertEqual((outcome.outcome, outcome.after_click), ("unconfirmed", 1))

    def test_a_post_to_the_submit_path_on_another_host_is_not_the_submit(self):
        obs = seen(post(303, host="example.test"), main_path=CONFIRMATION_PATH, form_present=False, navigated=True)
        self.assertEqual(decide_outcome(obs).outcome, "unconfirmed")

    def test_a_post_the_route_aborted_is_not_a_submit_that_passed(self):
        obs = seen(post(None, passed=False), main_path=CONFIRMATION_PATH, form_present=False, navigated=True)
        self.assertEqual(decide_outcome(obs).outcome, "unconfirmed")

    def test_without_a_loader_submit_path_nothing_can_be_the_submit(self):
        obs = seen(post(303), main_path=CONFIRMATION_PATH, form_present=False, submit_path="")
        self.assertNotEqual(decide_outcome(obs).outcome, "submitted")

    def test_no_submit_post_and_no_navigation_is_failed_with_nothing_sent(self):
        outcome = decide_outcome(seen(first_field_error="Email"))
        self.assertEqual((outcome.outcome, outcome.after_click), ("failed", 0))
        self.assertIn("nothing was sent", outcome.note.lower())
        self.assertEqual(outcome.note, 'The form did not send, so nothing was sent. Greenhouse marked "Email" as wrong')
        self.assertFalse(outcome.settled)
        self.assertEqual(decide_outcome(seen()).outcome, "failed")

    def test_a_post_to_another_path_that_the_route_aborted_says_so(self):
        other = post(None, path=f"{SUBMIT_PATH}/apply-v2", passed=False)
        outcome = decide_outcome(seen(other))
        self.assertEqual((outcome.outcome, outcome.after_click), ("failed", 0))
        self.assertIn("address the app doesn't recognize", outcome.note)
        self.assertIn("Nothing was sent", outcome.note)

    def test_an_aborted_upload_to_another_host_says_the_same(self):
        outcome = decide_outcome(seen(post(None, method="PUT", host="example-uploads.s3.amazonaws.com", path="/resume", passed=False)))
        self.assertEqual(outcome.after_click, 0)
        self.assertIn("address the app doesn't recognize", outcome.note)

    def test_an_aborted_second_submit_does_not_spoil_a_submission(self):
        obs = seen(post(303), post(None, passed=False), main_path=CONFIRMATION_PATH, form_present=False)
        self.assertEqual(decide_outcome(obs).outcome, "submitted")

    def test_the_security_code_waits_for_the_student(self):
        for obs in (seen(post(428)), seen(security_code_visible=True), seen(post(428), security_code_visible=True)):
            with self.subTest(obs=repr(obs)[:80]):
                outcome = decide_outcome(obs)
                self.assertEqual((outcome.outcome, outcome.after_click), ("waiting", 1))
                self.assertEqual(outcome.detail, {"waiting": "security_code"})

    def test_a_code_never_entered_is_needs_you_once_the_wait_is_over(self):
        outcome = decide_outcome(seen(post(428), security_code_visible=True), code_wait_over=True)
        self.assertEqual((outcome.outcome, outcome.after_click), ("needs_you", 1))

    def test_a_confirmation_after_the_code_is_submitted_with_the_code_recorded(self):
        obs = seen(post(428), post(303), main_path=CONFIRMATION_PATH, form_present=False, navigated=True)
        outcome = decide_outcome(obs, code_wait_over=True)
        self.assertEqual((outcome.outcome, outcome.detail), ("submitted", {"security_code": True}))

    def test_a_second_answer_that_is_not_a_prompt_ends_the_wait(self):
        # 428, the student's code, then a 500: no longer waiting for a code.
        self.assertEqual(decide_outcome(seen(post(428), post(500))).outcome, "unconfirmed")

    def test_a_code_post_that_has_no_answer_is_not_a_second_prompt(self):
        # The boxes stay on the page while the code POST is on its way: one answer, never two prompts.
        in_flight = seen(post(428), post(None), security_code_visible=True)
        self.assertEqual(decide_outcome(in_flight).outcome, "waiting")
        self.assertFalse(apply_checks.new_code_prompt(in_flight))
        accepted = seen(post(428), post(200), security_code_visible=True)
        self.assertEqual(decide_outcome(accepted).outcome, "waiting")
        self.assertFalse(apply_checks.new_code_prompt(accepted), "an accepted code is the confirmation page's to come, not a prompt")
        self.assertFalse(apply_checks.new_code_prompt(seen()), "no submit POST, no prompt")
        self.assertFalse(apply_checks.new_code_prompt(seen(post(428), security_code_visible=True)), "the first 428 is the prompt being answered")

    def test_a_new_prompt_is_a_new_428_or_the_boxes_again_after_a_refusal(self):
        self.assertTrue(apply_checks.new_code_prompt(seen(post(428), post(428), security_code_visible=True)))
        self.assertTrue(apply_checks.new_code_prompt(seen(post(428), post(428))))
        self.assertTrue(apply_checks.new_code_prompt(seen(post(428), post(422), security_code_visible=True)))
        self.assertFalse(apply_checks.new_code_prompt(seen(post(428), post(422))), "a refusal with no boxes is not a prompt")
        self.assertFalse(apply_checks.new_code_prompt(seen(post(428), post(None, passed=False), security_code_visible=True)),
                         "a POST the route refused has no answer to read")

    def test_a_code_post_with_no_answer_when_the_wait_is_over_may_have_been_sent(self):
        for last in (None, 200, 303):
            with self.subTest(status=last):
                outcome = decide_outcome(seen(post(428), post(last), security_code_visible=True), code_wait_over=True)
                self.assertEqual((outcome.outcome, outcome.after_click, outcome.note), ("unconfirmed", 1, apply_checks.UNCONFIRMED_NOTE))
        # A second 428 is a real prompt the student did not answer, and a code never pressed is the note that says so.
        for obs in (seen(post(428), post(428), security_code_visible=True), seen(post(428), security_code_visible=True)):
            self.assertEqual(decide_outcome(obs, code_wait_over=True).outcome, "needs_you")

    def test_a_5xx_answer_to_the_code_post_is_never_a_new_prompt_and_may_have_been_sent(self):
        # An edge proxy may answer 502, 503 or 504 after the origin took the code. The boxes stay on the page after a 5xx.
        for status in (500, 502, 503, 504):
            for boxes in (True, False):
                obs = seen(post(428), post(status), security_code_visible=boxes)
                with self.subTest(status=status, boxes=boxes):
                    self.assertFalse(apply_checks.new_code_prompt(obs), "a second application POST would be allowed after one that may have been received")
                    for over in (False, True):
                        outcome = decide_outcome(obs, code_wait_over=over)
                        self.assertEqual((outcome.outcome, outcome.after_click, outcome.note), ("unconfirmed", 1, apply_checks.UNCONFIRMED_NOTE))

    def test_a_refused_code_is_called_refused_not_unpressed(self):
        for last in (428, 422):
            with self.subTest(status=last):
                outcome = decide_outcome(seen(post(428), post(last), security_code_visible=True), code_wait_over=True)
                self.assertEqual((outcome.outcome, outcome.note), ("needs_you", apply_checks.CODE_REFUSED_NOTE))
                self.assertNotIn("was not pressed after it", outcome.note)
        # The first prompt, never answered: Submit really was not pressed after it.
        self.assertEqual(decide_outcome(seen(post(428), security_code_visible=True), code_wait_over=True).note, apply_checks.SECURITY_CODE_NOTE)

    def test_a_challenge_frame_is_needs_you_with_the_click_made(self):
        outcome = decide_outcome(seen(challenge_frame=True))
        self.assertEqual((outcome.outcome, outcome.after_click), ("needs_you", 1))

    def test_a_refusal_with_the_form_still_present_is_failed_after_the_click(self):
        outcome = decide_outcome(seen(post(422), first_field_error="Email"))
        self.assertEqual((outcome.outcome, outcome.after_click), ("failed", 1))
        self.assertEqual(outcome.note, 'Greenhouse refused the form (HTTP 422). Greenhouse marked "Email" as wrong')
        self.assertTrue(outcome.settled)
        # No named field, no clause: the note never carries text from the page.
        self.assertEqual(decide_outcome(seen(post(422))).note, "Greenhouse refused the form (HTTP 422)")

    def test_the_two_notes_that_wait_on_the_student_say_what_was_not_done(self):
        self.assertEqual(
            apply_checks.SECURITY_CODE_NOTE,
            "Greenhouse asked for the emailed security code, and Submit application was not pressed after it. Look for Greenhouse's email",
        )
        self.assertEqual(apply_checks.CHALLENGE_NOTE, "Greenhouse showed a check that wasn't finished. Look for Greenhouse's email")
        self.assertEqual(decide_outcome(seen(post(428), security_code_visible=True), code_wait_over=True).note, apply_checks.SECURITY_CODE_NOTE)
        self.assertEqual(decide_outcome(seen(challenge_frame=True)).note, apply_checks.CHALLENGE_NOTE)

    def test_a_4xx_with_the_form_gone_is_not_called_a_refusal(self):
        self.assertEqual(decide_outcome(seen(post(422), form_present=False, navigated=True)).outcome, "unconfirmed")

    def test_the_first_matching_row_wins(self):
        # A confirmation beats a challenge frame that is still in the DOM.
        obs = self.confirmed(challenge_frame=True)
        self.assertEqual(decide_outcome(obs).outcome, "submitted")
        # The code prompt beats the challenge frame.
        self.assertEqual(decide_outcome(seen(post(428), challenge_frame=True)).outcome, "waiting")

    def test_an_outcome_carries_no_page_wording_of_its_own(self):
        outcome = decide_outcome(seen(post(200), main_path=SUBMIT_PATH, form_present=True))
        self.assertNotIn("Thank you", outcome.note)


# --- route_decision (spec 4.3) ------------------------------------------------------------------

def request(method="GET", url="https://job-boards.greenhouse.io/examplerobotics/jobs/4000000001", **kwargs):
    # The handler passes public=True after outreach_render.request_allowed; a request with no such fact is refused.
    kwargs.setdefault("public", True)
    return RouteRequest(method=method, url=url, **kwargs)


def state(**kwargs):
    kwargs.setdefault("submit_path", SUBMIT_PATH)
    kwargs.setdefault("values", {"email": EMAIL, "first_name": "Samantha", "work_auth": "Yes", "city": "Springfield, Example State"})
    kwargs.setdefault("lookup_endpoints", (LOOKUP,))
    return RouteState(**kwargs)


SUBMIT_URL = f"https://boards.greenhouse.io{SUBMIT_PATH}"
BEACON = "https://pixel.example-robotics.test/p.gif?v="


class RouteDecisionRulesTests(unittest.TestCase):
    """The rules for every mode, in order: navigation, WebSockets, public addresses, the value guard."""

    def test_a_main_frame_navigation_may_go_only_to_the_two_board_hosts(self):
        for host, allowed in (("job-boards.greenhouse.io", True), ("boards.greenhouse.io", True), ("my.greenhouse.io", False),
                              ("boards-api.greenhouse.io", False), ("careers.example-robotics.test", False), ("greenhouse.io", False)):
            for mode, phase in (("rehearse", PHASE_BEFORE_INPUT), ("submit", PHASE_FILL), ("handoff", PHASE_STUDENT)):
                with self.subTest(host=host, mode=mode):
                    decision = route_decision(mode, phase, request(url=f"https://{host}/x", is_navigation=True, resource_type="document"), state())
                    if allowed:
                        self.assertIsInstance(decision, Allow)
                    else:
                        self.assertEqual(decision.rule, "offsite_navigation")
                        self.assertEqual(decision.reason, f"This posting sends applicants to {host}")

    def test_only_a_main_frame_navigation_is_held_to_the_board_hosts(self):
        # A script or an image from another host is not a navigation; before the first input it may load.
        self.assertIsInstance(route_decision("rehearse", PHASE_BEFORE_INPUT, request(url="https://cdn.example-robotics.test/a.js", resource_type="script"), state()), Allow)

    def test_websockets_are_refused_in_every_mode_and_phase(self):
        for mode, phases in apply_checks.PHASES.items():
            for phase in phases:
                with self.subTest(mode=mode, phase=phase):
                    decision = route_decision(mode, phase, request(url="wss://socket.example-robotics.test/live", is_websocket=True), state())
                    self.assertEqual(decision.rule, "websocket")

    def test_a_non_public_address_is_refused(self):
        decision = route_decision("rehearse", PHASE_BEFORE_INPUT, request(url="http://127.0.0.1:8000/", public=False), state())
        self.assertEqual(decision.rule, "non_public_address")

    def test_a_request_with_no_public_fact_is_refused_so_the_rule_fails_closed(self):
        for mode, phase in (("rehearse", PHASE_BEFORE_INPUT), ("submit", PHASE_FILL), ("handoff", PHASE_STUDENT)):
            with self.subTest(mode=mode):
                unset = RouteRequest(method="GET", url="http://127.0.0.1:8000/api/v1/applications")
                self.assertIsNone(unset.public)
                self.assertEqual(route_decision(mode, phase, unset, state()).rule, "non_public_address")
                self.assertEqual(route_decision(mode, phase, RouteRequest("GET", "https://job-boards.greenhouse.io/x"), state()).rule, "non_public_address")
                self.assertIsInstance(route_decision(mode, phase, RouteRequest("GET", "https://job-boards.greenhouse.io/x", public=True), state()), Allow)

    def test_an_unknown_mode_or_phase_is_refused(self):
        self.assertEqual(route_decision("unattended", PHASE_FILL, request(), state()).rule, "unknown_phase")
        self.assertEqual(route_decision("submit", PHASE_BEFORE_INPUT, request(), state()).rule, "unknown_phase")
        self.assertEqual(route_decision("rehearse", PHASE_STUDENT, request(), state()).rule, "unknown_phase")
        self.assertEqual(route_decision("submit", PHASE_STUDENT, request("POST", SUBMIT_URL), state()).rule, "unknown_phase")

    def test_the_abort_record_names_the_method_host_rule_and_field_never_the_url_or_body(self):
        decision = route_decision("rehearse", PHASE_AFTER_INPUT, request(url=f"{BEACON}{EMAIL}", resource_type="image"), state())
        self.assertEqual(decision.rule, "value_guard")
        self.assertEqual(decision.record("get"), {"method": "GET", "host": "pixel.example-robotics.test", "rule": "value_guard", "field_key": "email"})
        self.assertNotIn(EMAIL, repr(decision.record("get")))
        self.assertNotIn("field_key", Abort("non_get", "x", "example.test").record("POST"))


class ValueGuardTests(unittest.TestCase):
    def guard(self, req, mode="rehearse", phase=PHASE_AFTER_INPUT, **state_args):
        return route_decision(mode, phase, req, state(**state_args))

    def test_a_planned_value_is_found_raw_encoded_and_case_folded(self):
        for label, url in (
            ("raw", f"{BEACON}{EMAIL}"),
            ("url-encoded", f"{BEACON}sam.rivera%40example.test"),
            ("upper case", f"{BEACON}SAM.RIVERA@EXAMPLE.TEST"),
            ("encoded upper case", f"{BEACON}SAM.RIVERA%40EXAMPLE.TEST"),
            ("plus for a space", f"{BEACON}Springfield%2C+Example+State"),
            ("plus for a space, raw", f"{BEACON}Springfield, Example State"),
        ):
            with self.subTest(label):
                decision = self.guard(request(url=url, resource_type="image"))
                self.assertIsInstance(decision, Abort)
                self.assertEqual(decision.rule, "value_guard")

    def test_a_refused_record_never_holds_a_value_even_when_the_value_is_in_the_host(self):
        values = {"last_name": "Rivera", "email": EMAIL}
        for host in ("sam-rivera.collector.example", "73616d2e726976657261.rivera.exfil.test", "RIVERA.example"):
            with self.subTest(host=host):
                decision = self.guard(request(url=f"https://{host}/p.gif", resource_type="image"), values=values)
                self.assertEqual((decision.rule, decision.field_key), ("value_guard", "last_name"))
                self.assertEqual(decision.host, apply_checks.HOST_WITHHELD)
                self.assertEqual(decision.record("get"), {"method": "GET", "host": "[host withheld]", "rule": "value_guard", "field_key": "last_name"})
        # A host with no value in it is recorded as it is, and so is a refusal for another reason.
        clear = self.guard(request(url=f"https://pixel.example-robotics.test/p.gif?v={EMAIL}", resource_type="image"), values=values)
        self.assertEqual(clear.host, "pixel.example-robotics.test")
        other = self.guard(request(url="https://rivera.example/page", resource_type="image", is_navigation=True), values=values)
        self.assertEqual((other.rule, other.host), ("offsite_navigation", apply_checks.HOST_WITHHELD))

    def test_a_host_that_holds_a_non_ascii_value_as_punycode_is_withheld_too(self):
        # Chromium writes an international host name as punycode before Playwright reports the request.
        self.assertEqual(apply_checks.safe_host("xn--rene-dpa.collector.example", {"first_name": "Ren\u00e9e"}), apply_checks.HOST_WITHHELD)
        # Any xn-- host is withheld while a planned value is not plain ASCII (the value may be in it in a form no search finds).
        self.assertEqual(apply_checks.safe_host("xn--zo-ija.evil.example", {"first_name": "Zo\u00eb Quinn"}), apply_checks.HOST_WITHHELD)
        # Nothing to withhold: an ordinary host, a punycode host with only ASCII values, no values at all.
        self.assertEqual(apply_checks.safe_host("collector.example", {"first_name": "Ren\u00e9e"}), "collector.example")
        self.assertEqual(apply_checks.safe_host("xn--zo-ija.evil.example", {"first_name": "Sam Rivera"}), "xn--zo-ija.evil.example")
        self.assertEqual(apply_checks.safe_host("xn--zo-ija.evil.example", {}), "xn--zo-ija.evil.example")
        decision = route_decision("rehearse", PHASE_AFTER_INPUT, request(url="https://xn--rene-dpa.collector.example/p.gif", resource_type="image", is_navigation=True),
                                  state(values={"first_name": "Ren\u00e9e"}))
        self.assertEqual((decision.rule, decision.host), ("offsite_navigation", apply_checks.HOST_WITHHELD))

    def test_a_planned_value_in_a_header_or_a_body_is_found(self):
        by_header = self.guard(request("GET", "https://job-boards.greenhouse.io/x", headers={"x-note": f"hello {EMAIL}"}, resource_type="fetch"))
        self.assertEqual((by_header.rule, by_header.field_key), ("value_guard", "email"))
        for body in (f'{{"e": "{EMAIL}"}}', f"e={EMAIL}".encode(), b"\xff\xfe" + EMAIL.encode()):
            with self.subTest(body=body):
                decision = self.guard(request("POST", "https://analytics.example-robotics.test/collect", body=body), mode="submit", phase=PHASE_FILL)
                self.assertEqual(decision.rule, "value_guard")

    def test_a_value_the_page_re_encodes_is_still_found(self):
        phone, essay = "+1 512 555 0100", 'I build "robot" arms.\nAnd I like the team.'
        site = "https://example.test/portfolio"
        values = {"phone": phone, "essay": essay, "site": site}
        for label, req in (
            # A script that concatenates without encodeURIComponent: Chromium encodes the spaces and leaves the "+".
            ("unencoded plus", request(url="https://job-boards.greenhouse.io/pixel.gif?v=+1%20512%20555%200100", resource_type="image")),
            ("json body", request("POST", "https://www.google.com/recaptcha/api2/reload", body=json.dumps({"v": essay}))),
            ("json body, non-ascii escaped off", request("POST", "https://www.google.com/recaptcha/api2/reload", body=json.dumps({"v": essay}, ensure_ascii=False))),
            ("json in a url", request(url="https://job-boards.greenhouse.io/x.png?d=" + quote(json.dumps({"v": essay})), resource_type="image")),
            ("json with escaped slashes", request("POST", "https://www.google.com/recaptcha/api2/reload", body=json.dumps({"v": site}).replace("/", "\\/"))),
            ("crlf body", request("POST", "https://www.google.com/recaptcha/api2/reload", body=essay.replace("\n", "\r\n"))),
        ):
            with self.subTest(label):
                found = route_decision("rehearse" if req.method == "GET" else "submit", PHASE_AFTER_INPUT if req.method == "GET" else PHASE_FILL, req, state(values=values))
                self.assertEqual(found.rule, "value_guard")
        # The two probes that got through before, by name.
        self.assertEqual(leaked_field(request("POST", "https://www.google.com/recaptcha/api2/reload", body=json.dumps({"v": essay})), {"essay": essay}), "essay")
        self.assertEqual(leaked_field(request(url="https://job-boards.greenhouse.io/pixel.gif?v=+1%20512%20555%200100"), {"phone": phone}), "phone")

    def test_a_short_value_is_not_searched_for(self):
        # "Yes" would match almost anything, so values under four characters are not guarded.
        decision = self.guard(request(url="https://job-boards.cdn.greenhouse.io/logo.png?yes=Yes", resource_type="image"))
        self.assertIsInstance(decision, Allow)

    def test_a_value_an_analytics_beacon_base64_encodes_is_found_at_any_offset(self):
        # Snowplow's GET transport sends its event, JSON with the field values in it, as base64 in the query string.
        email, name = EMAIL, "Mu\u00f1oz-Garc\u00eda"
        for document in ('{"e":"%s"}', 'xx{"e":"%s"}', 'x{"e":"%s","z":1}', "%s"):
            for value in (email, name):
                payload = (document % value).encode("utf-8")
                for encoder in (base64.b64encode, base64.urlsafe_b64encode):
                    with self.subTest(document=document, value=value, encoder=encoder.__name__):
                        url = "https://c.spl.greenhouse.io/i?e=ue&ue_px=" + encoder(payload).decode("ascii")
                        self.assertNotEqual(leaked_field(request(url=url, resource_type="image"), {"email": email, "last_name": name}), "")
        unrelated = base64.b64encode(b'{"e":"nothing of the student"}').decode("ascii")
        self.assertEqual(leaked_field(request(url="https://c.spl.greenhouse.io/i?ue_px=" + unrelated), {"email": email}), "")

    def test_a_value_sent_as_latin_1_or_percent_u_is_found(self):
        values = {"last_name": "Mu\u00f1oz-Garc\u00eda"}
        for label, url in (
            ("latin-1 percent", "https://x.example/?n=Mu%F1oz-Garc%EDa"),
            ("latin-1 percent, lower case", "https://x.example/?n=mu%f1oz-garc%eda"),
            ("percent-u (escape())", "https://x.example/?n=Mu%u00F1oz-Garc%u00EDa"),
            ("utf-8 percent", "https://x.example/?n=Mu%C3%B1oz-Garc%C3%ADa"),
        ):
            with self.subTest(label):
                self.assertEqual(leaked_field(request(url=url), values), "last_name")

    def test_it_applies_on_greenhouse_hosts_and_captcha_endpoints_too(self):
        for url in ("https://job-boards.greenhouse.io/pixel.gif?v=" + EMAIL, "https://www.google.com/recaptcha/api2/reload?v=" + EMAIL):
            with self.subTest(url=url):
                decision = self.guard(request("POST", url, body=EMAIL), mode="submit", phase=PHASE_FILL)
                self.assertEqual(decision.rule, "value_guard")

    def test_it_runs_before_the_table_so_a_static_asset_carrying_a_value_is_refused(self):
        decision = self.guard(request(url=f"https://job-boards.greenhouse.io/x.png?v={EMAIL}", resource_type="image"))
        self.assertEqual(decision.rule, "value_guard")

    def test_the_submit_post_is_exempt(self):
        req = request("POST", SUBMIT_URL, body=f"email={EMAIL}", resource_type="fetch")
        decision = self.guard(req, mode="submit", phase=PHASE_AFTER_HAND_OVER)
        self.assertEqual(decision, Allow("submit", submit_post=True))

    def test_the_lookup_for_the_field_being_typed_may_carry_that_fields_own_text(self):
        typing = "Springfield, Example State"
        url = f"https://boards-api.greenhouse.io/fake-lookup/location?q={typing.replace(' ', '%20')}"
        decision = self.guard(request(url=url, resource_type="fetch"), typing_key="city", typing_lookup="location")
        self.assertEqual(decision, Allow("lookup"))

    def test_a_lookup_carrying_another_fields_value_is_refused_and_names_that_field(self):
        url = f"https://boards-api.greenhouse.io/fake-lookup/location?q=Spring&e={EMAIL}"
        decision = self.guard(request(url=url, resource_type="fetch"), typing_key="city", typing_lookup="location")
        self.assertEqual((decision.rule, decision.field_key), ("value_guard", "email"))

    def test_a_lookup_with_no_field_being_typed_is_not_a_lookup(self):
        url = "https://boards-api.greenhouse.io/fake-lookup/location?q=Spring"
        self.assertEqual(self.guard(request(url=url, resource_type="fetch"), typing_key="").rule, "after_first_input")

    def test_a_get_is_exempt_only_once_the_submit_post_has_passed(self):
        beacon = request(url=f"https://job-boards.greenhouse.io/x?v={EMAIL}", resource_type="fetch")
        self.assertEqual(self.guard(beacon, mode="submit", phase=PHASE_AFTER_HAND_OVER).rule, "value_guard")
        self.assertIsInstance(self.guard(beacon, mode="submit", phase=PHASE_AFTER_HAND_OVER, submit_posts_passed=1), Allow)

    def test_in_a_handoff_a_get_to_another_host_is_guarded_after_the_press_too(self):
        # The form stays on the page through a security code or a challenge: a script there may send what was typed in a GET.
        values = {"race": "Hispanic or Latino", "email": EMAIL}
        for label, url in (
            ("analytics pixel", "https://www.google-analytics.com/collect?el=Hispanic%20or%20Latino"),
            ("an embedded third party", f"https://widget.example-robotics.test/p.gif?v={EMAIL}"),
            ("a Greenhouse host that is not a board", f"https://boards-api.greenhouse.io/v1/x?v={EMAIL}"),
        ):
            with self.subTest(label):
                beacon = request(url=url, resource_type="image")
                for phase in (PHASE_STUDENT, PHASE_AFTER_HAND_OVER):
                    decision = self.guard(beacon, mode="handoff", phase=phase, submit_posts_passed=1, values=values)
                    self.assertEqual(decision.rule, "value_guard", f"{phase}: a value left in a GET after the press")
        # Greenhouse's own board hosts still load the confirmation page and its parts, and submit mode keeps its exemption.
        board = request(url=f"https://job-boards.greenhouse.io/x?v={EMAIL}", resource_type="fetch")
        self.assertIsInstance(self.guard(board, mode="handoff", phase=PHASE_AFTER_HAND_OVER, submit_posts_passed=1, values=values), Allow)
        other = request(url=f"https://widget.example-robotics.test/p.gif?v={EMAIL}", resource_type="image")
        self.assertIsInstance(self.guard(other, mode="submit", phase=PHASE_AFTER_HAND_OVER, submit_posts_passed=1, values=values), Allow)

    def test_a_multi_value_answer_is_guarded_by_each_of_its_parts(self):
        decision = self.guard(request(url=f"{BEACON}Rust", resource_type="image"), values={"langs": ["Python", "Rust"]})
        self.assertEqual((decision.rule, decision.field_key), ("value_guard", "langs"))

    def test_no_values_means_nothing_to_guard(self):
        self.assertIsInstance(self.guard(request(url=f"{BEACON}{EMAIL}"), phase=PHASE_BEFORE_INPUT, values={}), Allow)


class LookupAndRehearseTableTests(unittest.TestCase):
    """Spec 4.3, rows one and two: lookup and rehearse."""

    def test_before_the_first_input_only_get_head_and_options_pass(self):
        for mode in ("lookup", "rehearse"):
            for method in ("GET", "HEAD", "OPTIONS"):
                with self.subTest(mode=mode, method=method):
                    self.assertIsInstance(route_decision(mode, PHASE_BEFORE_INPUT, request(method), state()), Allow)
            for method in ("POST", "PUT", "PATCH", "DELETE"):
                with self.subTest(mode=mode, method=method):
                    decision = route_decision(mode, PHASE_BEFORE_INPUT, request(method, "https://analytics.example-robotics.test/collect"), state())
                    self.assertEqual(decision.rule, "non_get")

    def test_a_rehearsal_refuses_captcha_endpoints_that_a_submit_would_allow(self):
        url = "https://www.google.com/recaptcha/api2/reload?k=fixture"
        for phase in (PHASE_BEFORE_INPUT, PHASE_AFTER_INPUT):
            self.assertIsInstance(route_decision("rehearse", phase, request("POST", url), state()), Abort)
        self.assertEqual(route_decision("submit", PHASE_FILL, request("POST", url), state()), Allow("captcha"))

    def test_after_the_first_input_static_assets_on_greenhouses_static_hosts_pass(self):
        for host in ("job-boards.cdn.greenhouse.io", "recruiting.cdn.greenhouse.io", "s2-recruiting.cdn.greenhouse.io", "s7-recruiting.cdn.greenhouse.io"):
            for resource in ("image", "font", "stylesheet", "script", "media"):
                with self.subTest(host=host, resource=resource):
                    decision = route_decision("rehearse", PHASE_AFTER_INPUT, request(url=f"https://{host}/assets/a.bin", resource_type=resource), state())
                    self.assertEqual(decision, Allow("static_asset"))

    def test_a_static_asset_request_to_any_other_greenhouse_host_is_refused(self):
        # c.spl.greenhouse.io is Greenhouse's analytics collector: an image request to it is an event, not a logo.
        for host in ("c.spl.greenhouse.io", "job-boards.greenhouse.io", "boards.greenhouse.io", "my.greenhouse.io", "cdn.greenhouse.io",
                     "job-boards.cdn.greenhouse.io.example.test", "sx-recruiting.cdn.greenhouse.io", "s1234-recruiting.cdn.greenhouse.io"):
            with self.subTest(host=host):
                url = f"https://{host}/i?e=ue&ue_px=" + base64.b64encode(f'{{"e":"{EMAIL}"}}'.encode()).decode()
                decision = route_decision("rehearse", PHASE_AFTER_INPUT, request(url=url, resource_type="image"), state(values={"email": EMAIL}))
                self.assertIsInstance(decision, Abort)
                clear = route_decision("rehearse", PHASE_AFTER_INPUT, request(url=f"https://{host}/i.gif", resource_type="image"), state())
                self.assertEqual(clear.rule, "after_first_input")

    def test_after_the_first_input_every_other_get_is_refused(self):
        cases = (
            ("document", "https://job-boards.greenhouse.io/other", "document"),
            ("xhr", "https://job-boards.greenhouse.io/api/x", "xhr"),
            ("fetch", "https://job-boards.greenhouse.io/api/x", "fetch"),
            ("ping", "https://job-boards.greenhouse.io/ping", "ping"),
            ("other", "https://job-boards.greenhouse.io/other", "other"),
            ("image on another host", "https://cdn.example-robotics.test/a.png", "image"),
            ("script on a lookalike host", "https://greenhouse.io.example.test/a.js", "script"),
            ("api host, not the typed field's lookup", "https://boards-api.greenhouse.io/v1/other", "fetch"),
        )
        for label, url, resource in cases:
            with self.subTest(label):
                decision = route_decision("rehearse", PHASE_AFTER_INPUT, request(url=url, resource_type=resource), state(typing_key="city", typing_lookup="location"))
                self.assertEqual(decision.rule, "after_first_input")

    def test_after_the_first_input_every_other_method_is_refused_on_any_host(self):
        for url in ("https://boards.greenhouse.io" + SUBMIT_PATH, "https://job-boards.greenhouse.io/x", "https://www.google.com/recaptcha/api2/reload"):
            for method in ("POST", "PUT", "HEAD", "OPTIONS"):
                with self.subTest(url=url, method=method):
                    self.assertIsInstance(route_decision("rehearse", PHASE_AFTER_INPUT, request(method, url), state()), Abort)

    def test_a_rehearsal_never_lets_a_submit_post_through(self):
        for phase in (PHASE_BEFORE_INPUT, PHASE_AFTER_INPUT):
            decision = route_decision("rehearse", phase, request("POST", SUBMIT_URL), state())
            self.assertIsInstance(decision, Abort)

    def test_the_lookup_pass_needs_an_endpoint_that_is_pinned(self):
        url = "https://boards-api.greenhouse.io/fake-lookup/location?q=Spr"
        req = request(url=url, resource_type="fetch")
        self.assertEqual(route_decision("lookup", PHASE_AFTER_INPUT, req, state(typing_key="city", typing_lookup="location")), Allow("lookup"))
        self.assertEqual(route_decision("lookup", PHASE_AFTER_INPUT, req, state(typing_key="city", typing_lookup="location", lookup_endpoints=())).rule, "after_first_input")
        # The shipped list names Greenhouse's own lookup hosts, not the fictional fixture endpoint, so the fake's path is not let through by default.
        self.assertTrue(apply_checks.GREENHOUSE_LOOKUP_ENDPOINTS)
        self.assertEqual(route_decision("lookup", PHASE_AFTER_INPUT, req, RouteState(submit_path=SUBMIT_PATH, typing_key="city", typing_lookup="location")).rule, "after_first_input")

    def test_a_lookup_endpoint_is_an_exact_host_and_a_path_prefix(self):
        for url, allowed in (
            ("https://boards-api.greenhouse.io/fake-lookup/location?q=a", True),
            ("https://boards-api.greenhouse.io/fake-lookup/location/extra?q=a", True),
            ("https://boards-api.greenhouse.io/fake-lookup/school?q=a", False),      # pinned, but it serves another field
            ("https://boards-api.greenhouse.io/fake-lookup-other?q=a", False),
            ("https://job-boards.greenhouse.io/fake-lookup/location?q=a", False),
            ("https://boards-api.greenhouse.io.example.test/fake-lookup/location?q=a", False),
        ):
            with self.subTest(url=url):
                decision = route_decision("rehearse", PHASE_AFTER_INPUT, request(url=url, resource_type="fetch"),
                                          state(typing_key="city", typing_lookup="location", lookup_endpoints=(LOOKUP_LOCATION, LOOKUP_SCHOOL)))
                self.assertEqual(isinstance(decision, Allow), allowed)

    def test_a_lookup_passes_only_for_the_kind_the_typed_field_uses(self):
        endpoints = (LOOKUP_LOCATION, LOOKUP_SCHOOL)
        location = request(url="https://boards-api.greenhouse.io/fake-lookup/location?q=a", resource_type="fetch")
        school = request(url="https://boards-api.greenhouse.io/fake-lookup/school?q=a", resource_type="fetch")
        for typing_lookup, allowed in (("location", location), ("school", school)):
            for label, req in (("location", location), ("school", school)):
                with self.subTest(typing_lookup=typing_lookup, request=label):
                    decision = route_decision("rehearse", PHASE_AFTER_INPUT, req, state(typing_key="field", typing_lookup=typing_lookup, lookup_endpoints=endpoints))
                    if req is allowed:
                        self.assertEqual(decision, Allow("lookup"))
                    else:
                        self.assertEqual(decision.rule, "after_first_input")

    def test_typing_a_field_with_no_typeahead_never_allows_a_lookup(self):
        # typing_key is set (M5a sets it on every type), but the field has no lookup kind.
        endpoints = (LOOKUP_LOCATION, LOOKUP_SCHOOL)
        for url in ("https://boards-api.greenhouse.io/fake-lookup/location?q=a", "https://boards-api.greenhouse.io/fake-lookup/school?q=a"):
            for mode in ("lookup", "rehearse"):
                with self.subTest(url=url, mode=mode):
                    decision = route_decision(mode, PHASE_AFTER_INPUT, request(url=url, resource_type="fetch"),
                                              state(typing_key="first_name", typing_lookup="", lookup_endpoints=endpoints))
                    self.assertEqual(decision.rule, "after_first_input")

    def test_an_endpoint_with_no_kind_serves_no_field(self):
        anonymous = Endpoint("boards-api.greenhouse.io", "/fake-lookup/", "")
        req = request(url="https://boards-api.greenhouse.io/fake-lookup/location?q=a", resource_type="fetch")
        for typing_lookup in ("", "location"):
            with self.subTest(typing_lookup=typing_lookup):
                decision = route_decision("rehearse", PHASE_AFTER_INPUT, req, state(typing_key="city", typing_lookup=typing_lookup, lookup_endpoints=(anonymous,)))
                self.assertEqual(decision.rule, "after_first_input")

    def test_a_field_typed_with_another_kinds_endpoint_loses_the_value_guard_exemption(self):
        # first_name is being typed and its (non-)lookup is not the location endpoint, so its text is guarded on that endpoint too.
        url = "https://boards-api.greenhouse.io/fake-lookup/location?q=Samantha"
        decision = route_decision("rehearse", PHASE_AFTER_INPUT, request(url=url, resource_type="fetch"),
                                  state(typing_key="first_name", typing_lookup="", lookup_endpoints=(LOOKUP_LOCATION, LOOKUP_SCHOOL)))
        self.assertEqual((decision.rule, decision.field_key), ("value_guard", "first_name"))


class SubmitAndHandoffTableTests(unittest.TestCase):
    """Spec 4.3, rows three to five: submit and handoff, before and after hand-over."""

    def test_before_hand_over_gets_pass_and_nothing_that_could_carry_the_application_does(self):
        for mode in ("submit", "handoff"):
            self.assertIsInstance(route_decision(mode, PHASE_FILL, request("GET"), state()), Allow)
            for method, url in (
                ("POST", SUBMIT_URL),                                            # a requestSubmit, an Enter key in a typeahead
                ("POST", "https://boards.greenhouse.io/examplerobotics/jobs/1/other"),
                ("POST", "https://analytics.example-robotics.test/collect"),
                ("PUT", "https://example-robotics-uploads.s3.amazonaws.com/resume"),  # an upload counts
                ("PATCH", "https://job-boards.greenhouse.io/x"),
            ):
                with self.subTest(mode=mode, method=method, url=url):
                    self.assertIsInstance(route_decision(mode, PHASE_FILL, request(method, url), state()), Abort)

    def test_the_submit_post_before_hand_over_is_refused_by_name(self):
        for mode in ("submit", "handoff"):
            decision = route_decision(mode, PHASE_FILL, request("POST", SUBMIT_URL), state())
            self.assertEqual(decision.rule, "before_hand_over")

    def test_before_hand_over_a_captcha_endpoint_passes_only_without_a_planned_value(self):
        url = "https://www.google.com/recaptcha/api2/reload?k=fixture"
        for mode in ("submit", "handoff"):
            self.assertEqual(route_decision(mode, PHASE_FILL, request("POST", url, body="c=abc"), state()), Allow("captcha"))
            self.assertEqual(route_decision(mode, PHASE_FILL, request("POST", url, body=EMAIL), state()).rule, "value_guard")

    def test_the_captcha_endpoint_list_is_exact_hosts_and_prefixes(self):
        self.assertTrue(CAPTCHA_ENDPOINTS)
        for endpoint in CAPTCHA_ENDPOINTS:
            with self.subTest(endpoint=endpoint.host):
                decision = route_decision("submit", PHASE_FILL, request("POST", f"https://{endpoint.host}{endpoint.path_prefix}x"), state())
                self.assertEqual(decision, Allow("captcha"))
        for url in ("https://www.google.com/maps", "https://www.google.com.example.test/recaptcha/x", "https://evil-recaptcha.example.test/recaptcha/x"):
            with self.subTest(url=url):
                self.assertIsInstance(route_decision("submit", PHASE_FILL, request("POST", url), state()), Abort)

    def test_a_handoff_students_first_submit_asks_for_the_hand_over(self):
        decision = route_decision("handoff", PHASE_STUDENT, request("POST", SUBMIT_URL, body="x=1"), state())
        self.assertEqual(decision, Allow("hand_over", requires_hand_over=True, submit_post=True))

    def test_in_the_students_turn_everything_else_is_still_held_back(self):
        for method, url in (("POST", "https://boards.greenhouse.io/examplerobotics/jobs/1/other"), ("POST", "https://analytics.example-robotics.test/c"),
                            ("PUT", "https://example-robotics-uploads.s3.amazonaws.com/resume")):
            with self.subTest(url=url):
                self.assertIsInstance(route_decision("handoff", PHASE_STUDENT, request(method, url), state()), Abort)
        # Only the submit path on the submit host counts.
        wrong_host = route_decision("handoff", PHASE_STUDENT, request("POST", f"https://job-boards.greenhouse.io{SUBMIT_PATH}"), state())
        self.assertIsInstance(wrong_host, Abort)

    def test_a_second_press_of_submit_after_the_first_passed_is_refused(self):
        decision = route_decision("handoff", PHASE_STUDENT, request("POST", SUBMIT_URL), state(submit_posts_passed=1))
        self.assertEqual(decision.rule, "second_submit_post")

    def test_with_no_loader_submit_path_no_request_is_the_submit_post(self):
        for phase in (PHASE_STUDENT, PHASE_AFTER_HAND_OVER):
            decision = route_decision("handoff", phase, request("POST", SUBMIT_URL), state(submit_path=""))
            self.assertIsInstance(decision, Abort)

    def test_after_hand_over_exactly_one_submit_post_passes(self):
        for mode in ("submit", "handoff"):
            run = state()
            first = route_decision(mode, PHASE_AFTER_HAND_OVER, request("POST", SUBMIT_URL), run)
            self.assertEqual(first, Allow("submit", submit_post=True))
            run.record(first)
            second = route_decision(mode, PHASE_AFTER_HAND_OVER, request("POST", SUBMIT_URL), run)
            self.assertEqual(second.rule, "second_submit_post")

    def test_one_more_post_passes_only_after_a_security_code_prompt_and_once_per_prompt(self):
        run = state()
        run.record(route_decision("submit", PHASE_AFTER_HAND_OVER, request("POST", SUBMIT_URL), run))
        self.assertEqual(route_decision("submit", PHASE_AFTER_HAND_OVER, request("POST", SUBMIT_URL), run).rule, "second_submit_post")
        run.note_security_code_prompt()                # Greenhouse answered 428
        code = route_decision("submit", PHASE_AFTER_HAND_OVER, request("POST", SUBMIT_URL), run)
        self.assertEqual(code, Allow("security_code", code_post=True))
        run.record(code)
        self.assertEqual(route_decision("submit", PHASE_AFTER_HAND_OVER, request("POST", SUBMIT_URL), run).rule, "second_submit_post")
        run.note_security_code_prompt()                # a second prompt allows a second code
        self.assertIsInstance(route_decision("submit", PHASE_AFTER_HAND_OVER, request("POST", SUBMIT_URL), run), Allow)

    def test_after_hand_over_every_other_non_get_is_refused_and_a_captcha_post_passes(self):
        for method, url in (("POST", "https://boards.greenhouse.io/examplerobotics/jobs/4000000001/apply-v2"), ("POST", "https://analytics.example-robotics.test/collect"),
                            ("PUT", "https://example-robotics-uploads.s3.amazonaws.com/resume"), ("DELETE", "https://job-boards.greenhouse.io/x")):
            with self.subTest(url=url):
                self.assertIsInstance(route_decision("submit", PHASE_AFTER_HAND_OVER, request(method, url), state(submit_posts_passed=1)), Abort)
        self.assertEqual(route_decision("submit", PHASE_AFTER_HAND_OVER, request("POST", "https://www.google.com/recaptcha/api2/reload"), state()),
                         Allow("captcha"))
        self.assertEqual(route_decision("submit", PHASE_AFTER_HAND_OVER, request("POST", f"https://boards.greenhouse.io{SUBMIT_PATH}/apply-v2"), state()).rule, "other_non_get")

    def test_gets_pass_after_hand_over(self):
        self.assertIsInstance(route_decision("submit", PHASE_AFTER_HAND_OVER, request("GET", CONFIRMATION_PATH_URL), state()), Allow)

    def test_s3_uploads_are_refused_in_every_phase_while_the_flag_is_off(self):
        self.assertIs(apply_checks.S3_UPLOAD_ENABLED, False)
        url = "https://example-robotics-uploads.s3.amazonaws.com/resume"
        for mode, phases in apply_checks.PHASES.items():
            for phase in phases:
                with self.subTest(mode=mode, phase=phase):
                    decision = route_decision(mode, phase, request("PUT", url), state(submit_posts_passed=1))
                    self.assertEqual(decision.rule, "s3_upload")

    def test_record_counts_only_what_passed(self):
        run = state()
        run.record(Allow())
        run.record(Allow("captcha"))
        self.assertEqual((run.submit_posts_passed, run.code_posts_passed), (0, 0))
        run.record(Allow("submit", submit_post=True))
        run.record(Allow("security_code", code_post=True))
        self.assertEqual((run.submit_posts_passed, run.code_posts_passed), (1, 1))


CONFIRMATION_PATH_URL = f"https://job-boards.greenhouse.io{CONFIRMATION_PATH}"
TELEMETRY_URL = "https://c.spl.greenhouse.io/com.snowplowanalytics.snowplow/tp2"
S3_URL = "https://example-robotics-uploads.s3.amazonaws.com/resume"
MULTIPART = {"content-type": "multipart/form-data; boundary=----x"}
WITH_FILE = '------x\r\nContent-Disposition: form-data; name="resume"; filename="Sam Rivera Resume.pdf"\r\nContent-Type: application/pdf\r\n\r\n%PDF\r\n------x--'
WITHOUT_FILE = '------x\r\nContent-Disposition: form-data; name="first_name"\r\n\r\nSam\r\n------x\r\nContent-Disposition: form-data; name="cover_letter"; filename=""\r\nContent-Type: application/octet-stream\r\n\r\n\r\n------x--'


class TelemetryAndCodeGuardTests(unittest.TestCase):
    """What a handoff does about Greenhouse's usage reporting, and about a code widget that submits by itself."""

    def test_every_method_to_a_telemetry_host_is_refused_by_name_in_submit_and_handoff(self):
        self.assertEqual(apply_checks.TELEMETRY_HOSTS, frozenset({"c.spl.greenhouse.io"}))
        for mode, phases in (("submit", (PHASE_FILL, PHASE_AFTER_HAND_OVER)), ("handoff", (PHASE_FILL, PHASE_STUDENT, PHASE_AFTER_HAND_OVER))):
            for phase in phases:
                for method in ("GET", "POST", "PUT", "OPTIONS"):
                    with self.subTest(mode=mode, phase=phase, method=method):
                        decision = route_decision(mode, phase, request(method, TELEMETRY_URL + "?e=pv"), state(submit_posts_passed=1))
                        self.assertEqual(decision.rule, "telemetry")
                        self.assertEqual(decision.host, "c.spl.greenhouse.io")
                        self.assertEqual(decision.record(method), {"method": method, "host": "c.spl.greenhouse.io", "rule": "telemetry"})

    def test_a_telemetry_host_is_the_exact_host_only(self):
        for url in ("https://spl.greenhouse.io/x", "https://c.spl.greenhouse.io.example.test/x", "https://greenhouse.io/x"):
            with self.subTest(url=url):
                self.assertNotEqual(getattr(route_decision("handoff", PHASE_STUDENT, request("GET", url), state()), "rule", ""), "telemetry")

    def test_a_submit_post_while_the_app_types_the_code_is_refused_without_spending_the_prompt(self):
        run = state()
        run.record(route_decision("handoff", PHASE_AFTER_HAND_OVER, request("POST", SUBMIT_URL), run))
        run.note_security_code_prompt()
        run.code_typing_until = float("inf")
        self.assertTrue(run.code_typing)
        refused = route_decision("handoff", PHASE_AFTER_HAND_OVER, request("POST", SUBMIT_URL), run)
        self.assertEqual(refused.rule, "code_post_while_typing")
        self.assertEqual((run.code_posts_passed, run.security_code_prompts), (0, 1))
        # A refusal is not recorded as a pass. With the guard over and no code typed by the app, the next POST is the prompt's one.
        run.code_typing_until = 0.0
        self.assertFalse(run.code_typing)
        allowed = route_decision("handoff", PHASE_AFTER_HAND_OVER, request("POST", SUBMIT_URL), run)
        self.assertEqual(allowed, Allow("security_code", code_post=True))
        run.record(allowed)
        self.assertEqual(run.code_posts_passed, 1)

    def test_once_the_app_typed_the_code_the_code_post_waits_for_the_students_press_however_long_it_takes(self):
        run = state()
        run.record(route_decision("handoff", PHASE_AFTER_HAND_OVER, request("POST", SUBMIT_URL), run))
        run.note_security_code_prompt()
        run.code_typing_until = float("inf")
        run.note_student_press()                              # a press while the app types is not the press that counts
        self.assertFalse(run.code_pressed)
        run.code_typing_until = 0.0
        run.require_code_press()
        for _ in range(3):                                    # a widget that retries: refused each time, nothing spent
            refused = route_decision("handoff", PHASE_AFTER_HAND_OVER, request("POST", SUBMIT_URL), run)
            self.assertEqual(refused.rule, "code_post_before_press")
            self.assertEqual((run.code_posts_passed, run.security_code_prompts), (0, 1))
        run.note_student_press()
        allowed = route_decision("handoff", PHASE_AFTER_HAND_OVER, request("POST", SUBMIT_URL), run)
        self.assertEqual(allowed, Allow("security_code", code_post=True))
        run.record(allowed)
        self.assertEqual((run.code_press_required, run.code_pressed), (False, False), "the press was used by the POST it let through")
        self.assertEqual(route_decision("handoff", PHASE_AFTER_HAND_OVER, request("POST", SUBMIT_URL), run).rule, "second_submit_post")

    def test_a_press_is_forgotten_when_the_app_starts_typing_again_and_does_not_count_before_the_app_typed(self):
        run = state()
        run.note_student_press()
        self.assertFalse(run.code_pressed, "no code was typed, so there is no press to wait for")
        run.require_code_press()
        run.note_student_press()
        self.assertTrue(run.code_pressed)
        run.require_code_press()
        self.assertFalse(run.code_pressed)

    def test_a_code_the_student_typed_needs_no_press_from_the_guard(self):
        run = state()
        run.record(route_decision("handoff", PHASE_AFTER_HAND_OVER, request("POST", SUBMIT_URL), run))
        run.note_security_code_prompt()
        self.assertEqual(route_decision("handoff", PHASE_AFTER_HAND_OVER, request("POST", SUBMIT_URL), run), Allow("security_code", code_post=True))

    def test_the_guard_covers_only_the_submit_post(self):
        run = state(code_typing_until=float("inf"))
        self.assertIsInstance(route_decision("handoff", PHASE_AFTER_HAND_OVER, request("GET"), run), Allow)
        self.assertEqual(route_decision("handoff", PHASE_AFTER_HAND_OVER, request("POST", "https://boards.greenhouse.io/x/other"), run).rule, "other_non_get")

    def test_the_code_guard_is_off_unless_the_agent_turned_it_on(self):
        self.assertFalse(RouteState().code_typing)
        self.assertEqual(RouteState().code_typing_until, 0.0)


class StudentSubmitElsewhereAndUploadTests(unittest.TestCase):
    def test_student_submit_elsewhere_table(self):
        cases = (
            (request("POST", "https://boards.greenhouse.io/examplerobotics/jobs/4000000001/apply-v2"), True),
            (request("PUT", "https://boards-api.greenhouse.io/v1/boards/x/apply"), True),
            (request("POST", "https://job-boards.greenhouse.io/x", resource_type="fetch"), True),
            (request("POST", TELEMETRY_URL), False),
            (request("POST", "https://analytics.example-robotics.test/collect"), False),
            (request("POST", "https://www.google.com/recaptcha/api2/reload"), False),
            (request("POST", "https://hcaptcha.com/checkcaptcha"), False),
            (request("POST", "https://careers.example-robotics.test/apply", resource_type="document"), True),
            (request("GET", "https://boards.greenhouse.io/x"), False),
            (request("HEAD", "https://boards.greenhouse.io/x", resource_type="document"), False),
        )
        for asked, expected in cases:
            with self.subTest(method=asked.method, url=asked.url):
                self.assertEqual(student_submit_elsewhere(asked, state()), expected)

    def test_is_upload_table(self):
        cases = (
            (request("PUT", S3_URL), True),
            (request("POST", "https://example-uploads.s3.amazonaws.com/"), True),
            (request("POST", "https://example.test/u", headers=MULTIPART, body=WITH_FILE), True),
            (request("POST", "https://example.test/u", headers=MULTIPART, body=WITH_FILE.encode()), True),
            (request("POST", "https://example.test/u", headers=MULTIPART), True),          # a body that cannot be read could hold a file
            (request("PUT", "https://example.test/u", headers={"Content-Type": "application/octet-stream"}, body=b"x"), True),
            (request("POST", SUBMIT_URL, headers=MULTIPART, body=WITHOUT_FILE), False),     # the form's own fields: empty file parts
            (request("POST", SUBMIT_URL, headers={"content-type": "application/json"}, body="{}"), False),
            (request("POST", "https://analytics.example-robotics.test/collect", body="{}"), False),
            (request("GET", S3_URL), False),
            (request("GET", "https://example.test/u", headers=MULTIPART), False),
        )
        for asked, expected in cases:
            with self.subTest(method=asked.method, url=asked.url, headers=dict(asked.headers)):
                self.assertEqual(is_upload(asked), expected)


class Base64ValueGuardTests(unittest.TestCase):
    """A script that wraps a field in base64 (Snowplow's ue_px and cx) at an offset the value's own base64 would miss."""

    VALUE = "sam.rivera@example.test"

    def payloads(self, urlsafe):
        encode = base64.urlsafe_b64encode if urlsafe else base64.b64encode
        for before in range(0, 6):
            for after in range(0, 4):
                yield before, after, encode(("x" * before + self.VALUE + "y" * after).encode()).decode()

    def test_a_planned_value_inside_a_base64_payload_is_caught_at_every_alignment(self):
        for urlsafe in (False, True):
            for before, after, payload in self.payloads(urlsafe):
                for place in ("body", "query", "header"):
                    with self.subTest(urlsafe=urlsafe, before=before, after=after, place=place):
                        asked = (request("POST", TELEMETRY_URL, body="ue_px=" + payload) if place == "body"
                                 else request("GET", "https://pixel.example.test/p?cx=" + quote(payload, safe="")) if place == "query"
                                 else request("GET", "https://pixel.example.test/p", headers={"x-payload": payload}))
                        self.assertEqual(leaked_field(asked, {"email": self.VALUE}), "email")

    def test_a_json_document_holding_the_value_then_base64d_is_caught(self):
        document = json.dumps({"schema": "iglu:com.snowplowanalytics/unstruct_event", "data": {"field": "email", "value": self.VALUE}})
        for payload in (base64.b64encode(document.encode()).decode(), base64.urlsafe_b64encode(document.encode()).decode().rstrip("=")):
            self.assertEqual(leaked_field(request("POST", TELEMETRY_URL, body='{"ue_px":"%s"}' % payload), {"email": self.VALUE}), "email")

    def test_other_text_and_short_values_are_not_caught(self):
        blob = base64.urlsafe_b64encode(b"nothing here but the page's own address and a counter 123456").decode()
        self.assertEqual(leaked_field(request("POST", TELEMETRY_URL, body="ue_px=" + blob), {"email": self.VALUE}), "")
        # "Yes" is three characters: its base64 is everywhere, so it is never searched for.
        self.assertEqual(leaked_field(request("POST", TELEMETRY_URL, body="ue_px=" + base64.b64encode(b"Yes please").decode()), {"answer": "Yes"}), "")

    def test_a_core_is_never_shorter_than_the_floor(self):
        for value in ("abcd", "Sam1", "Rivera", self.VALUE, "x" * 40):
            for urlsafe in (False, True):
                for core in apply_checks._stable_base64(value.encode(), urlsafe):
                    self.assertGreaterEqual(len(core), apply_checks.MIN_GUARDED_VALUE)


class TheFormHostsAreTheSubmitHostsFamily(unittest.TestCase):
    def test_form_post_hosts(self):
        self.assertEqual(apply_checks.FORM_POST_HOSTS, frozenset({"job-boards.greenhouse.io", "boards.greenhouse.io", "boards-api.greenhouse.io"}))


# The handoff phases belong to the table tests above: a student's Submit during a code typing is a refusal, never a hand-over.
class StudentPhaseAndCodeTypingTests(unittest.TestCase):
    def test_a_submit_in_the_students_turn_is_still_a_hand_over_when_no_code_is_being_typed(self):
        decision = route_decision("handoff", PHASE_STUDENT, request("POST", SUBMIT_URL), state())
        self.assertEqual(decision, Allow("hand_over", requires_hand_over=True, submit_post=True))


# --- join (spec 6.5) --------------------------------------------------------------------------

def field_of(name, label, *, required=True, type="input_text"):
    return {"name": name, "label": label, "required": required, "type": type}


def scan_of(name, question, *, required_any=True, visible=True, type="text", widget="native", **extra):
    return {"name": name, "id": name, "question": question, "required_any": required_any, "visible_css": visible,
            "type": type, "widget": widget, **extra}


class JoinTests(unittest.TestCase):
    def kinds(self, schema, scans):
        return [(problem.kind, problem.key) for problem in join(schema, scans)]

    def test_a_form_that_agrees_with_the_listing_has_no_problem(self):
        schema = [field_of("first_name", "First Name"), field_of("phone", "Phone", required=False)]
        scans = [scan_of("first_name", "First Name"), scan_of("phone", "Phone", required_any=False)]
        self.assertEqual(join(schema, scans), [])

    def test_a_required_field_with_no_control_is_a_mismatch_in_the_students_words(self):
        problems = join([field_of("first_name", "First Name")], [])
        self.assertEqual([(p.kind, p.key) for p in problems], [("listing_mismatch", "first_name")])
        self.assertEqual(problems[0].message, "The form does not match what Greenhouse's own listing describes (First Name)")
        self.assertTrue(problems[0].required)

    def test_a_required_field_with_two_controls_is_a_mismatch(self):
        scans = [scan_of("email", "Email"), scan_of("email", "Email")]
        self.assertEqual(self.kinds([field_of("email", "Email")], scans), [("listing_mismatch", "email")])

    def test_an_optional_field_with_no_control_is_not_a_problem_and_with_two_is(self):
        schema = [field_of("phone", "Phone", required=False)]
        self.assertEqual(join(schema, []), [])
        problems = join(schema, [scan_of("phone", "Phone", required_any=False), scan_of("phone", "Phone", required_any=False)])
        self.assertEqual([(p.kind, p.required) for p in problems], [("listing_mismatch", False)])

    def test_an_optional_field_the_page_does_not_draw_is_not_a_disagreement_with_the_listing(self):
        # Not a join problem (the agent copies only those, and any of them makes a rehearsal unclean): spec 6.5 and 9.2.
        self.assertNotIn(apply_checks.OPTIONAL_NOT_DRAWN, {"listing_mismatch", "wording_mismatch", "hidden_control", "unlisted_required"})

    def test_an_optional_field_the_plan_would_fill_but_the_page_does_not_draw_is_reported_so_the_plan_can_blank_it(self):
        schema = [field_of("portfolio", "Portfolio or project link", required=False)]
        problems = join(schema, [], ["portfolio"])
        self.assertEqual([(p.kind, p.key, p.required) for p in problems], [("optional_not_drawn", "portfolio", False)])
        self.assertEqual(problems[0].message, 'The form does not show the optional field "Portfolio or project link", so the app left it blank')
        self.assertEqual(join(schema, [], ["job_application[portfolio]"]), problems, "the key is read the way the page spells it")
        # Nothing to fill into it, or no plan yet: an optional field with no control is no problem.
        self.assertEqual(join(schema, [], []), [])
        self.assertEqual(join(schema, [], ["something_else"]), [])
        self.assertEqual(join(schema, []), [])
        # Drawn once, it agrees; the plan's own key list makes no difference then.
        self.assertEqual(join(schema, [scan_of("portfolio", "Portfolio or project link", required_any=False)], ["portfolio"]), [])

    def test_a_radio_or_checkbox_group_counts_as_one_control(self):
        schema = [field_of("question_1", "Have you worked here?", type="multi_value_single_select")]
        scans = [scan_of("question_1", "Have you worked here?", type="radio"), scan_of("question_1", "Have you worked here?", type="radio")]
        self.assertEqual(join(schema, scans), [])

    def test_a_control_matches_by_name_or_id_and_is_counted_once(self):
        schema = [field_of("question_9", "Team")]
        by_id = {"name": "", "id": "question_9", "question": "Team", "required_any": True, "visible_css": True}
        self.assertEqual(join(schema, [by_id]), [])
        self.assertEqual(join(schema, [scan_of("question_9", "Team")]), [])   # name and id both match: still one control

    def test_the_wording_must_normalize_to_the_same_question_key(self):
        problems = join([field_of("q", "Why us?")], [scan_of("q", "Why do you want to join us?")])
        self.assertEqual([(p.kind, p.message) for p in problems],
                         [("wording_mismatch", "The form's wording differs from Greenhouse's listing (Why do you want to join us?)")])
        # Case, punctuation, spacing and a trailing asterisk do not matter.
        for wording in ("why  us", "WHY US?*", "Why us? (required)".replace(" (required)", "")):
            with self.subTest(wording=wording):
                self.assertEqual(join([field_of("q", "Why us?")], [scan_of("q", wording)]), [])

    def test_a_lone_checkbox_with_no_question_of_its_own_is_not_a_wording_mismatch(self):
        # The engine reports "" for a checkbox (or radio) with no fieldset legend, such as a consent box wrapped in its own label.
        # Nothing was heard, so there is nothing to disagree with.
        consent = field_of("gdpr_consent_given", "I consent to Example Robotics storing my application data for 365 days", type="multi_value_multi_select")
        for kind in ("checkbox", "radio"):
            with self.subTest(kind=kind):
                self.assertEqual(join([consent], [scan_of("gdpr_consent_given", "", type=kind)]), [])
        # A control that is not a choice still needs its wording, and a choice that does report a different question still disagrees.
        text = field_of("q", "Why us?")
        self.assertEqual([p.kind for p in join([text], [scan_of("q", "")])], ["wording_mismatch"])
        self.assertEqual([p.kind for p in join([consent], [scan_of("gdpr_consent_given", "Something else", type="checkbox")])], ["wording_mismatch"])

    def test_a_required_control_the_listing_does_not_mention_is_a_problem(self):
        problems = join([field_of("first_name", "First Name")], [scan_of("first_name", "First Name"), scan_of("surprise", "Extra question")])
        self.assertEqual([(p.kind, p.message) for p in problems],
                         [("unlisted_required", "The form has a required field the listing does not mention (Extra question)")])

    def test_an_unlisted_optional_control_is_left_alone(self):
        problems = join([field_of("first_name", "First Name")], [scan_of("first_name", "First Name"), scan_of("website_url", "", required_any=False)])
        self.assertEqual(problems, [])

    def test_greenhouses_own_hidden_inputs_never_need_a_control(self):
        schema = [field_of("mapped_url_token", "Referral token", required=False, type="input_hidden")]
        self.assertEqual(join(schema, []), [])
        # ... and a required control that carries such a name is listed, not "unlisted".
        self.assertEqual(join(schema, [scan_of("mapped_url_token", "", type="hidden")]), [])

    def test_the_paste_instead_alternative_of_an_upload_is_neither_required_nor_missing(self):
        # Greenhouse lists resume_text beside resume in the same required block; the page shows it only after "Enter manually".
        schema = [field_of("resume", "Resume/CV", type="input_file"), field_of("resume_text", "Resume/CV", type="textarea"),
                  field_of("cover_letter_text", "Cover Letter", required=False, type="textarea")]
        self.assertEqual(join(schema, [scan_of("resume", "Resume/CV", type="file", widget="file_group")]), [])

    def test_a_hidden_control_is_a_problem_when_the_plan_would_fill_it_or_no_plan_is_given(self):
        schema = [field_of("website", "Website", required=False)]
        scans = [scan_of("website", "Website", required_any=False, visible=False)]
        self.assertEqual([(p.kind, p.key) for p in join(schema, scans)], [("hidden_control", "website")])            # no plan yet: the cautious reading
        self.assertEqual([(p.kind, p.key) for p in join(schema, scans, ["website"])], [("hidden_control", "website")])
        self.assertEqual([(p.kind, p.key) for p in join(schema, scans, ["job_application[website]"])], [("hidden_control", "website")])

    def test_an_optional_hidden_control_the_plan_leaves_blank_is_not_a_problem(self):
        # An optional sub-question the page shows only after its parent is answered.
        schema = [field_of("explain", "If yes, please explain", required=False)]
        scans = [scan_of("explain", "If yes, please explain", required_any=False, visible=False)]
        self.assertEqual(join(schema, scans, []), [])
        self.assertEqual(join(schema, scans, ["first_name"]), [])
        # A required hidden control can never be filled, so it stays a problem whatever the plan says.
        required = [field_of("explain", "If yes, please explain", required=True)]
        self.assertEqual([(p.kind, p.key) for p in join(required, scans, [])], [("hidden_control", "explain")])

    def test_an_unknown_visibility_is_not_treated_as_hidden(self):
        scan = scan_of("website", "Website", required_any=False)
        scan["visible_css"] = None
        self.assertEqual(join([field_of("website", "Website", required=False)], [scan]), [])

    def test_a_file_input_inside_a_visible_upload_group_may_be_visually_hidden(self):
        schema = [field_of("resume", "Resume/CV", type="input_file")]
        self.assertEqual(join(schema, [scan_of("resume", "Resume/CV", visible=False, type="file", widget="file_group")]), [])
        self.assertEqual(self.kinds(schema, [scan_of("resume", "Resume/CV", visible=False, type="file", widget="file_group", group_visible=False)]),
                         [("hidden_control", "resume")])
        # A hidden file input that is not an upload group gets no such pass.
        self.assertEqual(self.kinds(schema, [scan_of("resume", "Resume/CV", visible=False, type="file", widget="native")]), [("hidden_control", "resume")])

    def test_the_older_forms_job_application_names_are_unwrapped(self):
        schema = [field_of("first_name", "First Name")]
        self.assertEqual(join(schema, [scan_of("job_application[first_name]", "First Name")]), [])

    def test_problems_are_plain_data(self):
        problem = join([field_of("first_name", "First Name")], [])[0]
        self.assertIsInstance(problem, Problem)
        with self.assertRaises(Exception):
            problem.kind = "x"   # frozen


class QuestionKeyTests(unittest.TestCase):
    def test_the_key_is_lowercase_with_runs_of_other_characters_as_one_space(self):
        for text, key in (("First Name *", "first name"), ("  Why  us? ", "why us"), ("What's your name?", "what's your name"),
                          ("Are you 18+?", "are you 18"), ("Résumé", "r sum"), ("", ""), (None, "")):
            with self.subTest(text=text):
                self.assertEqual(question_key(text), key)


# --- REQUIRED_CHECK_SCRIPT ---------------------------------------------------------------------

class RequiredCheckScriptTests(unittest.TestCase):
    def test_it_looks_at_both_form_shapes(self):
        self.assertIn("form#application-form", REQUIRED_CHECK_SCRIPT)
        self.assertIn("#application_form", REQUIRED_CHECK_SCRIPT)

    def test_it_holds_no_way_to_click_type_or_submit(self):
        forbidden = (r"\.click\(", r"\.check\(", r"\.set_checked\(", r"\.select_option\(", r"\.fill\(", r"\.press\(", r"\.tap\(",
                     r"dispatch_event\(", r"dispatchEvent", r"keyboard\.", r"mouse\.", r"set_input_files\(", r"requestSubmit", r"\.submit\(",
                     r"new MouseEvent", r"new PointerEvent", r"\.focus\(", r"\.value\s*=[^=]", r"\.checked\s*=[^=]", r"\.files\s*=[^=]",
                     r"\.innerHTML\s*=", r"\.remove\(", r"\.setAttribute\(", r"\bfetch\(", r"XMLHttpRequest")
        for pattern in forbidden:
            with self.subTest(pattern=pattern):
                self.assertIsNone(re.search(pattern, REQUIRED_CHECK_SCRIPT))

    def test_it_reports_a_fixed_reason_and_never_the_browsers_own_message(self):
        # validationMessage quotes what was typed ("'sam...' is missing an '@'").
        self.assertNotIn("validationMessage", REQUIRED_CHECK_SCRIPT)
        for state_name in ("valueMissing", "typeMismatch", "patternMismatch", "tooShort", "tooLong", "rangeUnderflow", "rangeOverflow", "stepMismatch", "badInput"):
            self.assertIn(f"v.{state_name}", REQUIRED_CHECK_SCRIPT)

    def test_it_shares_no_selectors_with_the_apply_engine_by_construction(self):
        # The engine's own attribute for tagged controls is never used here.
        self.assertNotIn("data-opportunity-field", REQUIRED_CHECK_SCRIPT)
        self.assertNotIn("OpportunityApplyEngine", REQUIRED_CHECK_SCRIPT)


# --- check_required (spec 6.10) ------------------------------------------------------------------


def item(key, question, value, *, kind="text", markers=("attr",), empty=None):
    empty = (not value) if empty is None else empty
    return {"key": key, "question": question, "markers": list(markers), "kind": kind, "value_text": value, "empty": empty}


def control(key, value="", *, kind="text", checked=False, mirror=False, required=False):
    return {"key": key, "name": key, "id": key, "kind": kind, "value_text": value, "checked": checked, "mirror": mirror, "required": required}


SCHEMA = [
    field_of("first_name", "First Name"),
    field_of("email", "Email"),
    field_of("phone", "Phone", required=False),
    field_of("team", "Team", type="multi_value_single_select"),
    field_of("resume", "Resume/CV", type="input_file"),
    field_of("work_auth", "Are you legally authorized to work in the United States?", type="multi_value_single_select"),
    field_of("mapped_url_token", "Referral token", required=False, type="input_hidden"),
]


def good_plan():
    return FakePlan([
        planned("first_name", "First Name", "Sam"),
        planned("email", "Email", EMAIL),
        planned("team", "Team", "Controls", control="react_select"),
        planned("resume", "Resume/CV", "", control="file", source="resume", file_name="Sam Rivera Resume.pdf"),
        planned("work_auth", "Are you legally authorized to work in the United States?", "Yes", disposition="deferred", source="sensitive"),
    ])


def good_items():
    return [
        item("first_name", "First Name", "Sam"),
        item("email", "Email", EMAIL, kind="email", markers=("aria",)),
        item("team", "Team", "Controls", kind="combobox", markers=("hidden_required_sibling",)),
        item("resume", "Resume/CV", "Sam Rivera Resume.pdf", kind="file", markers=("span_required",)),
        item("work_auth", "Are you legally authorized to work in the United States?", "", kind="combobox", markers=("hidden_required_sibling",)),
    ]


class CheckRequiredTests(unittest.TestCase):
    def check(self, items=None, plan=None, schema=SCHEMA, initial=None, **kwargs):
        return check_required(good_items() if items is None else items, good_plan() if plan is None else plan, schema, initial or {}, **kwargs)

    def kinds(self, problems):
        return sorted((p.kind, p.key) for p in problems)

    def test_a_filled_form_that_matches_its_plan_passes(self):
        self.assertEqual(self.check(), [])

    # 1. every item is non-empty
    def test_an_empty_required_item_is_a_problem_in_the_students_words(self):
        items = good_items()
        items[0] = item("first_name", "First Name", "")
        problems = self.check(items)
        self.assertEqual([(p.kind, p.message) for p in problems], [("empty", 'The required field "First Name" is empty')])

    def test_deferred_left_for_you_and_blank_keys_are_skipped_for_the_first_three_checks(self):
        for disposition in ("deferred", "left_for_you", "blank"):
            with self.subTest(disposition=disposition):
                plan = good_plan()
                plan.fields[0] = planned("first_name", "First Name", "Sam", disposition=disposition)
                items = good_items()
                items[0] = item("first_name", "First Name", "")
                self.assertEqual(self.check(items, plan), [])
        # ... including the schema-required check: a skipped key need not appear among the items.
        items = [entry for entry in good_items() if entry["key"] != "work_auth"]
        self.assertEqual(self.check(items), [])

    # 2. every item is in the plan, with a source, and holds what the plan put there
    def test_an_item_the_plan_does_not_have_is_a_problem(self):
        items = good_items() + [item("surprise", "A surprise question", "x")]
        self.assertEqual(self.kinds(self.check(items)), [("not_planned", "surprise")])

    def test_an_item_whose_plan_has_no_source_is_a_problem(self):
        plan = good_plan()
        plan.fields[0] = planned("first_name", "First Name", "Sam", source="none")
        self.assertEqual(self.kinds(self.check(plan=plan)), [("no_source", "first_name")])
        plan.fields[0] = {"key": "first_name", "question": "First Name", "value": "Sam", "required": True, "disposition": "fill"}
        self.assertEqual(self.kinds(self.check(plan=plan)), [("no_source", "first_name")])

    def test_an_item_holding_something_else_than_the_plan_put_there_is_a_problem_that_never_shows_the_value(self):
        items = good_items()
        items[0] = item("first_name", "First Name", "Samuel")
        problems = self.check(items)
        self.assertEqual(self.kinds(problems), [("value_mismatch", "first_name")])
        self.assertNotIn("Samuel", problems[0].message)
        self.assertNotIn("Sam", problems[0].message.replace("Samuel", ""))

    def test_a_text_value_may_differ_only_by_the_crlf_a_browser_makes(self):
        plan = good_plan()
        plan.fields[0] = planned("first_name", "First Name", "line one\n\nline two", control="textarea")
        ok = good_items()
        ok[0] = item("first_name", "First Name", "line one\r\n\r\nline two", kind="textarea")
        self.assertEqual(self.check(ok, plan), [])
        bad = good_items()
        bad[0] = item("first_name", "First Name", "line one\nline two", kind="textarea")
        self.assertEqual(self.kinds(self.check(bad, plan)), [("value_mismatch", "first_name")])
        # Case is a difference in text, unlike a chosen option's label.
        bad[0] = item("first_name", "First Name", "Line one\n\nline two", kind="textarea")
        self.assertEqual(self.kinds(self.check(bad, plan)), [("value_mismatch", "first_name")])

    def test_a_chosen_option_is_compared_by_its_label_after_normalization(self):
        items = good_items()
        items[2] = item("team", "Team", "  controls ", kind="combobox", markers=("hidden_required_sibling",))
        self.assertEqual(self.check(items), [])
        items[2] = item("team", "Team", "Firmware", kind="combobox", markers=("hidden_required_sibling",))
        self.assertEqual(self.kinds(self.check(items)), [("value_mismatch", "team")])

    def test_a_file_is_compared_by_its_original_name(self):
        items = good_items()
        items[3] = item("resume", "Resume/CV", "resume-file-3f2a.pdf", kind="file", markers=("span_required",))
        self.assertEqual(self.kinds(self.check(items)), [("value_mismatch", "resume")])

    def test_a_multi_select_and_a_checkbox_group_match_in_any_order(self):
        plan = good_plan()
        plan.fields.append(planned("langs", "Languages", ["Python", "Rust"], control="react_select"))
        items = good_items() + [item("langs", "Languages", ["rust", "Python"], kind="combobox", markers=("aria",))]
        self.assertEqual(self.check(items, plan), [])
        items[-1] = item("langs", "Languages", ["Rust"], kind="combobox", markers=("aria",))
        self.assertEqual(self.kinds(self.check(items, plan)), [("value_mismatch", "langs")])

    def test_a_planned_tick_matches_a_checked_box_whatever_its_label(self):
        plan = good_plan()
        plan.fields.append(planned("privacy", "I have read the privacy notice", True, control="checkbox", source="sensitive"))
        plan.fields.append(planned("statement", "I certify it is accurate", "I certify that it is accurate", control="checkbox", source="sensitive"))
        items = good_items() + [
            item("privacy", "I have read the privacy notice", ["I have read the privacy notice"], kind="checkbox"),
            item("statement", "I certify it is accurate", ["I certify that it is accurate"], kind="checkbox"),
        ]
        self.assertEqual(self.check(items, plan), [])
        items[-1] = item("statement", "I certify it is accurate", ["Something else"], kind="checkbox")
        self.assertEqual(self.kinds(self.check(items, plan)), [("value_mismatch", "statement")])

    def test_a_planned_label_is_never_satisfied_by_another_checked_label(self):
        # The plan says "Yes" (or "1", "On", "true"); the page holds the opposite choice. The failure used to run one way only.
        for planned_label, held, kind in (
            ("Yes", ["No"], "radio"), ("No", ["Yes"], "radio"), ("1", ["5"], "radio"), ("On", ["Off"], "checkbox"),
            ("true", ["false"], "checkbox"), ("Yes", ["Maybe", "No"], "checkbox"),
        ):
            with self.subTest(planned=planned_label, held=held, kind=kind):
                plan = good_plan()
                plan.fields.append(planned("prior", "Have you worked here before?", planned_label, control=kind))
                schema = SCHEMA + [field_of("prior", "Have you worked here before?", type="multi_value_single_select")]
                items = good_items() + [item("prior", "Have you worked here before?", held, kind=kind, markers=("aria",))]
                # check 2, the item held against the plan
                self.assertEqual(self.kinds(self.check(items, plan, schema)), [("value_mismatch", "prior")])
                # check 4, the control held against the plan, with the item out of the way
                controls = [control("prior", held[0], kind=kind, checked=True)]
                optional = [field_of("prior", "Have you worked here before?", required=False, type="multi_value_single_select")]
                self.assertEqual(self.kinds(self.check(good_items(), plan, SCHEMA + optional, controls=controls)), [("unplanned_value", "prior")])
        # The same label, chosen, is fine, in any case and spacing.
        plan = good_plan()
        plan.fields.append(planned("prior", "Have you worked here before?", "Yes", control="radio"))
        items = good_items() + [item("prior", "Have you worked here before?", ["  yes "], kind="radio", markers=("aria",))]
        self.assertEqual(self.check(items, plan, SCHEMA + [field_of("prior", "Have you worked here before?", type="multi_value_single_select")]), [])

    def test_the_fixtures_own_yes_no_radio_holds_only_the_planned_answer(self):
        # question_4000000111: "Have you previously worked at Example Robotics?" (options Yes and No)
        plan = good_plan()
        plan.fields.append(planned("question_4000000111", "Have you previously worked at Example Robotics?", "Yes", control="radio"))
        schema = SCHEMA + [field_of("question_4000000111", "Have you previously worked at Example Robotics?", type="multi_value_single_select")]
        no = [item("question_4000000111", "Have you previously worked at Example Robotics?", ["No"], kind="radio", markers=("aria",))]
        yes = [item("question_4000000111", "Have you previously worked at Example Robotics?", ["Yes"], kind="radio", markers=("aria",))]
        self.assertEqual(self.kinds(self.check(good_items() + no, plan, schema)), [("value_mismatch", "question_4000000111")])
        self.assertEqual(self.check(good_items() + yes, plan, schema), [])

    # 3. every schema-required field appears among the items
    def test_a_schema_required_field_the_scanner_did_not_report_is_a_problem(self):
        items = [entry for entry in good_items() if entry["key"] != "email"]
        problems = self.check(items)
        self.assertEqual(self.kinds(problems), [("required_not_seen", "email")])
        self.assertIn("Email", problems[0].message)

    def test_the_paste_instead_alternative_of_an_upload_needs_no_item(self):
        schema = SCHEMA + [field_of("resume_text", "Resume/CV", type="textarea")]
        self.assertEqual(self.check(schema=schema), [])

    def test_optional_and_hidden_schema_fields_need_no_item(self):
        self.assertEqual(self.check(schema=SCHEMA + [field_of("cover_letter", "Cover Letter", required=False, type="input_file")]), [])

    # 4. no control holds a value the plan did not put there
    def test_an_optional_field_holding_a_value_the_plan_did_not_set_is_a_problem(self):
        controls = [control("phone", "555-0100")]
        problems = self.check(controls=controls)
        self.assertEqual(self.kinds(problems), [("unplanned_value", "phone")])
        self.assertFalse(problems[0].required)
        self.assertNotIn("555", problems[0].message)

    def test_a_required_field_holding_a_value_the_plan_did_not_set_is_always_a_problem(self):
        plan = good_plan()
        plan.fields[0] = planned("first_name", "First Name", "Sam", disposition="left_for_you")
        controls = [control("first_name", "Sam", required=True)]
        # Even when it equals the snapshot: autofill and page scripts fill required fields too.
        self.assertEqual(self.kinds(self.check(plan=plan, controls=controls, initial={"first_name": "Sam"})), [("unplanned_value", "first_name")])

    def test_an_optional_field_still_holding_the_pages_own_default_is_allowed(self):
        controls = [control("term", "Summer 2027", kind="select"), control("keep_informed", "Keep me informed", kind="checkbox", checked=True)]
        initial = {"term": "Summer 2027", "keep_informed": ["Keep me informed"]}
        self.assertEqual(self.check(controls=controls, initial=initial), [])
        # Changed by a script since the snapshot: a problem.
        controls[0] = control("term", "Fall 2027", kind="select")
        self.assertEqual(self.kinds(self.check(controls=controls, initial=initial)), [("unplanned_value", "term")])

    def test_greenhouses_own_hidden_inputs_the_mirrors_and_the_captcha_token_are_not_problems(self):
        controls = [
            control("mapped_url_token", "fixture-token", kind="hidden", mirror=True),
            control("team", "v2", mirror=True),                       # the mirror of a control that is also on the page
            control("g-recaptcha-response", "token" * 12, kind="textarea"),
            control("team", "Controls", kind="combobox"),
        ]
        self.assertEqual(self.check(controls=controls), [])

    def test_a_mirror_with_a_value_and_no_control_of_its_own_is_a_problem(self):
        self.assertEqual(self.kinds(self.check(controls=[control("ghost", "v1", mirror=True)])), [("unplanned_value", "ghost")])

    def test_a_deferred_field_that_holds_a_value_anyway_is_a_problem(self):
        # Sensitive answers are not typed into the page in a rehearsal: the page must not hold one.
        controls = [control("work_auth", "Yes", kind="combobox")]
        self.assertEqual(self.kinds(self.check(controls=controls)), [("unplanned_value", "work_auth")])

    def test_a_planned_field_holding_a_different_value_is_reported_once(self):
        items = good_items()
        items[0] = item("first_name", "First Name", "Samuel")
        controls = [control("first_name", "Samuel")]
        self.assertEqual(self.kinds(self.check(items, controls=controls)), [("value_mismatch", "first_name")])

    def test_boxes_and_radios_of_one_group_are_judged_together(self):
        plan = good_plan()
        plan.fields.append(planned("langs", "Languages", ["Python", "Rust"], required=False, control="checkbox"))
        controls = [control("langs", "Python", kind="checkbox", checked=True), control("langs", "C++", kind="checkbox"),
                    control("langs", "Rust", kind="checkbox", checked=True)]
        self.assertEqual(self.check(plan=plan, controls=controls), [])
        controls[1] = control("langs", "C++", kind="checkbox", checked=True)
        self.assertEqual(self.kinds(self.check(plan=plan, controls=controls)), [("unplanned_value", "langs")])

    def test_an_unchecked_box_and_an_empty_control_hold_nothing(self):
        controls = [control("phone", ""), control("newsletter", "Send me the newsletter", kind="checkbox", checked=False),
                    control("langs", [], kind="select_multiple")]
        self.assertEqual(self.check(controls=controls), [])

    # 5. nothing invalid
    def test_anything_the_form_flags_as_invalid_is_a_problem(self):
        problems = self.check(invalid=[{"key": "email", "question": "Email", "reason": "Please include an '@' in the email address."}])
        self.assertEqual([(p.kind, p.key) for p in problems], [("invalid", "email")])
        self.assertIn("Please include", problems[0].message)
        unattributed = self.check(invalid=[{"key": "", "question": "", "reason": "Something went wrong"}])
        self.assertEqual([p.kind for p in unattributed], ["invalid"])

    def test_a_problem_never_quotes_a_value_the_form_or_the_plan_holds(self):
        typed = "sam.rivera.example.test"
        items = good_items()
        items[1] = item("email", "Email", typed, kind="email", markers=("aria",))
        controls = [control("email", typed, kind="email")]
        for reason in (f"Please include an '@' in the email address. '{typed}' is missing an '@'.", f"Sorry, {EMAIL.upper()} is taken", f"{typed}"):
            with self.subTest(reason=reason):
                problems = self.check(items, invalid=[{"key": "email", "question": "Email", "reason": reason}], controls=controls)
                invalid = [p for p in problems if p.kind == "invalid"]
                self.assertEqual(len(invalid), 1)
                for held in (typed, EMAIL, EMAIL.upper()):
                    self.assertNotIn(held, invalid[0].message)
                    self.assertNotIn(held.lower(), invalid[0].message.lower())
        # What is not a value stays, so the student still learns what the form said.
        problems = self.check(invalid=[{"key": "email", "question": "Email", "reason": "required and empty"}])
        self.assertIn("required and empty", problems[0].message)

    def test_what_the_form_flags_on_a_deferred_field_is_not_held_against_the_form(self):
        # A required field left empty on purpose (a rehearsal defers every sensitive answer) is :invalid by nature.
        problems = self.check(invalid=[{"key": "work_auth", "question": "Work authorization", "reason": "Please fill out this field."}])
        self.assertEqual(problems, [])

    # 6. the plan is the one the student confirmed
    def test_a_submit_run_needs_the_confirmed_plan_hash(self):
        self.assertEqual(self.check(confirmed_plan_hash="hash-1"), [])
        problems = self.check(confirmed_plan_hash="hash-0")
        self.assertEqual([p.kind for p in problems], ["plan_changed"])
        self.assertEqual(problems[0].message, "The form or your answers changed since you confirmed. Look at the new plan")

    def test_without_a_confirmed_hash_the_plan_is_not_compared(self):
        self.assertEqual(self.check(confirmed_plan_hash=None), [])

    def test_a_plan_may_be_a_bare_list_of_fields(self):
        self.assertEqual(check_required(good_items(), good_plan().fields, SCHEMA, {}), [])

    def test_an_item_with_no_key_cannot_be_matched_to_the_plan(self):
        items = good_items() + [item("", "An unnamed control", "x")]
        self.assertEqual(self.kinds(self.check(items)), [("not_planned", "")])


# --- clean_rehearsal (spec 9.2) --------------------------------------------------------------------

def entry(key="first_name", *, required=True, disposition="fill", problem="", control="text", source="profile", **extra):
    return {"key": key, "question": key, "required": required, "disposition": disposition, "problem": problem,
            "control": control, "source": {"kind": source}, **extra}


class CleanRehearsalTests(unittest.TestCase):
    def run_of(self, *plan, **overrides):
        return {"outcome": "rehearsed", "plan": list(plan), "join_problems": [], "check_problems": [], **overrides}

    def test_a_rehearsal_with_everything_resolved_is_clean(self):
        self.assertTrue(clean_rehearsal(self.run_of(entry(), entry("resume", control="file", source="resume", file_sha256="ab"))))

    def test_a_deferred_resume_is_not_clean(self):
        # A board that uploads as you attach: the résumé stays deferred, and the gate does not count it.
        deferred = entry("resume", disposition="deferred", control="file", source="resume", file_sha256="ab")
        self.assertFalse(clean_rehearsal(self.run_of(entry(), deferred)))
        by_source = entry("resume", disposition="deferred", control="text", source="resume")
        self.assertFalse(clean_rehearsal(self.run_of(by_source)))
        self.assertFalse(clean_rehearsal(self.run_of(entry("cover_letter", required=False, disposition="deferred", control="file", source="cover_letter"))))

    def test_optional_fields_left_blank_do_not_matter(self):
        self.assertTrue(clean_rehearsal(self.run_of(entry(), entry("phone", required=False, disposition="blank", source="none"),
                                                    entry("salary", required=False, disposition="blank", source="none", problem="No saved answer"))))

    def test_deferred_sensitive_fields_do_not_matter(self):
        self.assertTrue(clean_rehearsal(self.run_of(entry(), entry("work_auth", disposition="deferred", source="sensitive"),
                                                    entry("gender", required=False, disposition="deferred", source="sensitive"))))

    def test_a_problem_on_a_required_field_is_not_clean(self):
        self.assertFalse(clean_rehearsal(self.run_of(entry(problem="No saved answer for this company"))))
        self.assertFalse(clean_rehearsal(self.run_of(entry(disposition="blank", source="none"))))
        self.assertFalse(clean_rehearsal(self.run_of(entry(disposition="left_for_you", source="none"))))

    def test_any_join_problem_or_failed_check_is_not_clean(self):
        problem = Problem("listing_mismatch", "x", "The form does not match")
        self.assertFalse(clean_rehearsal(self.run_of(entry(), join_problems=[problem])))
        self.assertFalse(clean_rehearsal(self.run_of(entry(), check_problems=[problem])))

    def test_only_a_rehearsal_that_ended_as_rehearsed_can_be_clean(self):
        for outcome in ("needs_you", "failed", "looked_up", ""):
            with self.subTest(outcome=outcome):
                self.assertFalse(clean_rehearsal(self.run_of(entry(), outcome=outcome)))

    def test_a_run_that_does_not_carry_what_the_gate_reads_is_never_clean(self):
        # A stored apply_runs row keeps these under other names (plan_json, reasons_json): it must not read as clean.
        complete = self.run_of(entry())
        self.assertTrue(clean_rehearsal(complete))
        for missing in ("outcome", "plan", "join_problems", "check_problems"):
            with self.subTest(missing=missing):
                partial = {name: value for name, value in complete.items() if name != missing}
                self.assertFalse(clean_rehearsal(partial))
                self.assertFalse(clean_rehearsal({**complete, missing: None}))
        self.assertFalse(clean_rehearsal({"plan": []}))
        self.assertFalse(clean_rehearsal({"outcome": "rehearsed", "plan_json": "[]", "clean": True}))
        self.assertFalse(clean_rehearsal(object()))

    def test_a_run_may_be_an_object_and_its_plan_entries_objects(self):
        @dataclass
        class Source:
            kind: str = "profile"

        @dataclass
        class Entry:
            key: str = "first_name"
            required: bool = True
            disposition: str = "fill"
            problem: str = ""
            control: str = "text"
            source: Source = field(default_factory=Source)

        @dataclass
        class Run:
            plan: list = field(default_factory=lambda: [Entry()])
            outcome: str = "rehearsed"
            join_problems: list = field(default_factory=list)
            check_problems: list = field(default_factory=list)

        @dataclass
        class Unchecked:
            plan: list = field(default_factory=lambda: [Entry()])
            outcome: str = "rehearsed"

        self.assertTrue(clean_rehearsal(Run()))
        self.assertFalse(clean_rehearsal(Run(plan=[Entry(problem="x")])))
        self.assertFalse(clean_rehearsal(Unchecked()))


if __name__ == "__main__":
    unittest.main()
