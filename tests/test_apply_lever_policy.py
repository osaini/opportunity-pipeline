"""Lever's request policy as pure rules: one test per cell of docs/phase5-lever-handoff-spec.md section 7, and one per condition of the resume POST.

No browser and no network. ``route_decision`` answers for a request in a phase with Lever's ``RoutePolicy``; the hand-over interception that acts on
the answer (asking the parent before ``route.continue_()``) is built later, so here only the decision is tested. Every company, posting, person and
address is fictional.
"""

import fnmatch
import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import realdata_guard

realdata_guard.install()

from opportunity_app.apply import agent as apply_agent, ats as apply_ats, checks, lever
from opportunity_app.apply.checks import (
    PHASE_AFTER_HAND_OVER, PHASE_FILL, PHASE_STUDENT, Abort, Allow, RouteRequest, RouteState, route_decision, student_submit_elsewhere,
)

POLICY = checks.LEVER_ROUTE_POLICY
SITE, JOB = "tidewatergames", "6a1f0c52-9b3e-4d17-8c40-2e5d7a91b0f3"
HOST, EU = "jobs.lever.co", "jobs.eu.lever.co"
APPLY_PATH = f"/{SITE}/{JOB}/apply"
ACCOUNT = "0b7c1e24-55d1-4f0a-9a8e-3c6d2f4b7a10"
VALUES = {"email": "sam.rivera@example.test", "first_name": "Samantha", "city": "Springfield, Example State", "org": "Tidewater Games"}
RESUME = b"%PDF-1.4\nSamantha Rivera, sam.rivera@example.test, Tidewater Games intern, Springfield, Example State\n%%EOF"
RESUME_SHA = hashlib.sha256(RESUME).hexdigest()
BOUNDARY = "----LeverBoundary7Qx"
FILL, STUDENT, AFTER = PHASE_FILL, PHASE_STUDENT, PHASE_AFTER_HAND_OVER


def multipart(*parts, boundary=BOUNDARY):
    """A multipart body as a browser writes it. Each part is (name, file name or None, content type or "", bytes)."""
    out = b""
    for name, filename, content_type, data in parts:
        head = f'Content-Disposition: form-data; name="{name}"' + (f'; filename="{filename}"' if filename is not None else "")
        head += f"\r\nContent-Type: {content_type}" if content_type else ""
        out += b"--" + boundary.encode() + b"\r\n" + head.encode() + b"\r\n\r\n" + data + b"\r\n"
    return out + b"--" + boundary.encode() + b"--\r\n"


def form_headers(boundary=BOUNDARY, **more):
    return {"Content-Type": f"multipart/form-data; boundary={boundary}", **more}


def resume_body(data=RESUME, account=ACCOUNT, filename="Samantha_Rivera_Resume.pdf"):
    return multipart(("resume", filename, "application/pdf", data), ("accountId", None, "", account.encode()))


def request(method, host, path, *, query="", body=None, headers=None, kind="fetch", nav=False, socket=False, public=True):
    return RouteRequest(
        method=method, url=f"https://{host}{path}" + (f"?{query}" if query else ""), resource_type=kind, is_navigation=nav, is_websocket=socket,
        public=public, headers=headers or {}, body=body,
    )


def resume_request(*, host=HOST, path=lever.PARSE_RESUME_PATH, body=None, headers=None, query=""):
    return request("POST", host, path, body=resume_body() if body is None else body, headers=form_headers() if headers is None else headers, query=query)


def neutral_request(**more):
    """A well-formed file read whose file holds none of the student's values, so only the address and the shape decide."""
    return resume_request(body=resume_body(b"%PDF-1.4 a file with nothing of the student in it", filename="cv.pdf"), **more)


def apply_request(*, host=HOST, path=APPLY_PATH, headers=None, body=None):
    return request("POST", host, path, body=multipart(("name", None, "", b"Samantha Rivera"), ("email", None, "", VALUES["email"].encode())) if body is None else body,
                   headers=form_headers() if headers is None else headers)


def state(**fields):
    base = dict(submit_path=APPLY_PATH, board_host=HOST, values=dict(VALUES), page_account_id=ACCOUNT, resume_upload_allowed=True, resume_sha256=RESUME_SHA)
    base.update(fields)
    return RouteState(**base)


def decide(phase, facts, st=None, mode="handoff"):
    return route_decision(mode, phase, facts, st if st is not None else state(), POLICY)


class Cases(unittest.TestCase):
    def assertAllowed(self, decision, rule=None, **flags):
        self.assertIsInstance(decision, Allow, getattr(decision, "reason", ""))
        if rule is not None:
            self.assertEqual(decision.rule, rule)
        for name, expected in flags.items():
            self.assertEqual(getattr(decision, name), expected, name)

    def assertAborted(self, decision, rule=None):
        self.assertIsInstance(decision, Abort, "allowed: " + repr(decision))
        if rule is not None:
            self.assertEqual(decision.rule, rule, decision.reason)


# --- What the policy holds ---------------------------------------------------------------------------------------------

class PolicyValuesTests(Cases):
    def test_the_hosts_and_the_one_lookup(self):
        self.assertEqual(set(POLICY.navigation_hosts), {HOST, EU})
        self.assertEqual(set(POLICY.submit_hosts), {HOST, EU})
        self.assertEqual(set(POLICY.form_post_hosts), {HOST, EU})
        self.assertEqual({(e.host, e.path_prefix, e.kind) for e in POLICY.lookup_endpoints}, {(HOST, "/searchLocations", "location"), (EU, "/searchLocations", "location")})
        self.assertEqual(POLICY.storage_upload_suffixes, ())
        self.assertTrue(POLICY.static_asset_host(HOST) and POLICY.static_asset_host(EU))
        self.assertFalse(POLICY.static_asset_host("cdn.example-games.test") or POLICY.static_asset_host("lever.co"))

    def test_the_captcha_endpoints_are_the_three_hcaptcha_hosts_section_7_names_and_the_recording_extends_that_tuple(self):
        self.assertEqual({(e.host, e.path_prefix) for e in POLICY.captcha_endpoints}, {("js.hcaptcha.com", "/"), ("hcaptcha.com", "/"), ("api.hcaptcha.com", "/")})
        self.assertIs(POLICY.captcha_endpoints, checks.LEVER_CAPTCHA_ENDPOINTS)
        self.assertEqual(set(checks.LEVER_CAPTCHA_RESOLVABLE_HOSTS), {"js.hcaptcha.com", "hcaptcha.com", "api.hcaptcha.com"})

    def test_the_other_lists_and_flags(self):
        self.assertEqual(POLICY.challenge_path_prefixes, ("/cdn-cgi/challenge-platform/",))
        self.assertEqual(POLICY.resume_post_path, "/parseResume")
        self.assertEqual(POLICY.submit_content_types, ("multipart/form-data",))
        self.assertTrue(POLICY.bind_submit_host)
        self.assertEqual(set(POLICY.telemetry_hosts), {"googletagmanager.com", "google-analytics.com", "bugsnag.com"})
        self.assertIs(POLICY.outcome_table, checks.lever_outcome)

    def test_greenhouse_has_none_of_it(self):
        greenhouse = checks.GREENHOUSE_ROUTE_POLICY
        self.assertEqual((greenhouse.challenge_path_prefixes, greenhouse.resume_post_path, greenhouse.submit_content_types, greenhouse.bind_submit_host, greenhouse.outcome_table),
                         ((), "", (), False, None))
        self.assertIs(type(greenhouse.telemetry_hosts), frozenset)
        self.assertFalse(greenhouse.is_challenge_request("boards.greenhouse.io", "/cdn-cgi/challenge-platform/x"))

    def test_a_domain_set_holds_the_domain_and_every_subdomain_and_nothing_that_only_ends_the_same(self):
        telemetry = POLICY.telemetry_hosts
        for host in ("googletagmanager.com", "www.googletagmanager.com", "www.google-analytics.com", "region1.google-analytics.com", "notify.bugsnag.com", "sessions.bugsnag.com"):
            with self.subTest(host=host):
                self.assertIn(host, telemetry)
        for host in ("notgoogletagmanager.com", "googletagmanager.com.example.test", "bugsnag.com.evil.test", "lever.co", HOST, ""):
            with self.subTest(host=host):
                self.assertNotIn(host, telemetry)
        self.assertEqual(telemetry, frozenset(checks.LEVER_TELEMETRY_DOMAINS), "iterating and comparing see the names written down")

    def test_the_submit_matcher_needs_the_posting_own_host_and_the_exact_path(self):
        self.assertTrue(POLICY.is_submit_request(HOST, APPLY_PATH, APPLY_PATH, HOST))
        self.assertTrue(POLICY.is_submit_request(EU, APPLY_PATH, APPLY_PATH, EU))
        self.assertFalse(POLICY.is_submit_request(EU, APPLY_PATH, APPLY_PATH, HOST), "the same path on the other Lever host")
        self.assertFalse(POLICY.is_submit_request(HOST, APPLY_PATH, APPLY_PATH, ""), "no host bound, no submit POST")
        self.assertFalse(POLICY.is_submit_request(HOST, APPLY_PATH, APPLY_PATH), "no host bound, no submit POST")
        self.assertFalse(POLICY.is_submit_request(HOST, APPLY_PATH + "/", APPLY_PATH, HOST))
        self.assertFalse(POLICY.is_submit_request(HOST, f"/{SITE}/{JOB}/thanks", APPLY_PATH, HOST))
        self.assertFalse(POLICY.is_submit_request(HOST, APPLY_PATH, "", HOST), "no submit path, no submit POST")
        self.assertFalse(POLICY.is_submit_request("example-games.test", APPLY_PATH, APPLY_PATH, "example-games.test"))

    def test_the_apply_post_needs_a_multipart_body(self):
        for good in ("multipart/form-data; boundary=x", "multipart/form-data"):
            self.assertTrue(POLICY.accepts_submit_body(good))
        for bad in ("", "application/json", "application/x-www-form-urlencoded", "text/plain", "multipart/mixed; boundary=x"):
            self.assertFalse(POLICY.accepts_submit_body(bad), bad)

    def test_the_loader_paths_are_the_pages_own_apply_and_thanks_paths_on_the_host_it_was_loaded_from(self):
        self.assertEqual(checks.lever_loader_paths(f"https://{HOST}/{SITE}/{JOB}/apply"), (HOST, APPLY_PATH, f"/{SITE}/{JOB}/thanks"))
        self.assertEqual(checks.lever_loader_paths(f"https://{EU}/{SITE}/{JOB}?lever-source=x"), (EU, APPLY_PATH, f"/{SITE}/{JOB}/thanks"))
        for url in ("", "https://boards.greenhouse.io/acme/jobs/1", f"https://example-games.test/{SITE}/{JOB}/apply", f"https://{HOST}/{SITE}/not-a-uuid/apply", f"https://{HOST}:8443/{SITE}/{JOB}"):
            with self.subTest(url=url):
                self.assertEqual(checks.lever_loader_paths(url), ("", "", ""))


# --- The rules for every mode, in order (section 7, rules 1 to 4) ---------------------------------------------------

class EveryModeRulesTests(Cases):
    PHASES = (FILL, STUDENT, AFTER)

    def test_rule_1_a_main_frame_navigation_goes_to_a_lever_host_only(self):
        for phase in self.PHASES:
            for host in (HOST, EU):
                with self.subTest(phase=phase, host=host):
                    self.assertAllowed(decide(phase, request("GET", host, APPLY_PATH, kind="document", nav=True)))
            for host in ("careers.example-games.test", "lever.co", "hire.lever.co", "jobs.lever.co.example.test", "boards.greenhouse.io", "hcaptcha.com"):
                with self.subTest(phase=phase, host=host):
                    self.assertAborted(decide(phase, request("GET", host, "/", kind="document", nav=True)), "offsite_navigation")

    def test_rule_2_websockets_are_refused_on_every_host_and_phase(self):
        for phase in self.PHASES:
            for host in (HOST, EU, "example-games.test", "js.hcaptcha.com"):
                with self.subTest(phase=phase, host=host):
                    self.assertAborted(decide(phase, request("GET", host, "/socket", socket=True)), "websocket")

    def test_rule_3_only_public_addresses(self):
        for phase in self.PHASES:
            for public in (None, False):
                with self.subTest(phase=phase, public=public):
                    self.assertAborted(decide(phase, request("GET", HOST, APPLY_PATH, public=public)), "non_public_address")

    def test_rule_4_a_planned_value_in_a_get_is_refused_in_the_url_and_in_a_header_on_every_host(self):
        for phase in self.PHASES:
            for host in (HOST, EU, "example-games.test", "js.hcaptcha.com", "www.googletagmanager.com"):
                with self.subTest(phase=phase, host=host, where="query"):
                    decision = decide(phase, request("GET", host, "/pixel", query="e=sam.rivera%40example.test"))
                    self.assertAborted(decision, "value_guard")
                    self.assertEqual(decision.field_key, "email")
                with self.subTest(phase=phase, host=host, where="header"):
                    self.assertAborted(decide(phase, request("GET", host, "/pixel", headers={"X-Note": "Samantha"})), "value_guard")

    def test_rule_4_a_planned_value_in_a_post_body_to_a_captcha_host_or_cloudflare_is_refused(self):
        for phase in (FILL, STUDENT, AFTER):
            for host, path in (("hcaptcha.com", "/checkcaptcha"), ("api.hcaptcha.com", "/getcaptcha"), ("js.hcaptcha.com", "/1/api.js"), (HOST, "/cdn-cgi/challenge-platform/h/b/jsd/oneshot/x")):
                with self.subTest(phase=phase, host=host):
                    self.assertAborted(decide(phase, request("POST", host, path, body=b"note=Samantha")), "value_guard")

    def test_the_value_guard_exception_the_text_typed_into_a_field_may_go_to_the_lookup_of_that_field_and_nowhere_else(self):
        typing = dict(typing_key="city", typing_lookup="location")
        for phase in (FILL, STUDENT):
            for host in (HOST, EU):
                with self.subTest(phase=phase, host=host):
                    self.assertAllowed(decide(phase, request("GET", host, "/searchLocations", query="text=Springfield%2C+Example+State"), state(**typing)))
                    other = decide(phase, request("GET", host, "/searchLocations", query="text=Springfield%2C+Example+State&n=Samantha"), state(**typing))
                    self.assertAborted(other, "value_guard")
                    self.assertEqual(other.field_key, "first_name", "the typed field's text and nothing else")
            with self.subTest(phase=phase, where="another path"):
                self.assertAborted(decide(phase, request("GET", HOST, "/other", query="text=Springfield%2C+Example+State"), state(**typing)), "value_guard")
            with self.subTest(phase=phase, where="no field being typed"):
                self.assertAborted(decide(phase, request("GET", HOST, "/searchLocations", query="text=Springfield%2C+Example+State"), state()), "value_guard")
            with self.subTest(phase=phase, where="another host"):
                self.assertAborted(decide(phase, request("GET", "example-games.test", "/searchLocations", query="text=Springfield%2C+Example+State"), state(**typing)), "value_guard")

    def test_the_value_guard_exception_the_hand_over_post_carries_the_application(self):
        self.assertAllowed(decide(STUDENT, apply_request()), "hand_over", requires_hand_over=True, submit_post=True)
        self.assertAllowed(decide(AFTER, apply_request()), "submit", submit_post=True)

    def test_the_value_guard_exception_the_resume_post_carries_the_resume(self):
        self.assertAllowed(decide(FILL, resume_request()), "resume_upload", resume_post=True, digest=RESUME_SHA)

    def test_an_unknown_mode_or_phase_is_refused(self):
        self.assertAborted(decide("nowhere", request("GET", HOST, APPLY_PATH)), "unknown_phase")
        self.assertAborted(decide(FILL, request("GET", HOST, APPLY_PATH), mode="unattended"), "unknown_phase")


# --- The table (section 7) --------------------------------------------------------------------------------------------------

class BeforeHandOverCellTests(Cases):
    """Row 1: ``handoff``, before hand-over. The fill and the student's turn both."""

    PHASES = (FILL, STUDENT)

    def test_get_head_and_options_are_allowed(self):
        for phase in self.PHASES:
            for method in ("GET", "HEAD", "OPTIONS"):
                for host, path in ((HOST, APPLY_PATH), (EU, APPLY_PATH), (HOST, "/js/parseResume.js"), ("js.hcaptcha.com", "/1/api.js"), (HOST, "/cdn-cgi/challenge-platform/scripts/jsd/main.js")):
                    with self.subTest(phase=phase, method=method, host=host, path=path):
                        self.assertAllowed(decide(phase, request(method, host, path)))

    def test_the_lookup_get_for_the_field_being_typed_is_allowed(self):
        for phase in self.PHASES:
            self.assertAllowed(decide(phase, request("GET", HOST, "/searchLocations", query="text=Spring"), state(typing_key="city", typing_lookup="location")))

    def test_a_write_to_a_captcha_endpoint_is_allowed(self):
        for phase in self.PHASES:
            for host, path in (("hcaptcha.com", "/checkcaptcha/x"), ("api.hcaptcha.com", "/getcaptcha/y"), ("js.hcaptcha.com", "/1/z")):
                for method in ("POST", "PUT"):
                    with self.subTest(phase=phase, host=host, method=method):
                        self.assertAllowed(decide(phase, request(method, host, path, body=b"{}")), "captcha")

    def test_a_write_to_cloudflares_challenge_path_is_allowed_on_either_lever_host(self):
        for phase in self.PHASES:
            for host in (HOST, EU):
                with self.subTest(phase=phase, host=host):
                    self.assertAllowed(decide(phase, request("POST", host, "/cdn-cgi/challenge-platform/h/b/jsd/oneshot/a1", body=b"\x00\x01")), "challenge")

    def test_a_write_to_another_path_under_cdn_cgi_or_to_that_path_on_another_host_is_refused(self):
        for phase in self.PHASES:
            for host, path in ((HOST, "/cdn-cgi/rum"), (HOST, "/cdn-cgi/"), (HOST, "/cdn-cgi/challenge-platformx"), ("example-games.test", "/cdn-cgi/challenge-platform/h/b"), ("lever.co", "/cdn-cgi/challenge-platform/h/b")):
                with self.subTest(phase=phase, host=host, path=path):
                    self.assertAborted(decide(phase, request("POST", host, path, body=b"\x00")), "non_get_before_hand_over")

    def test_the_resume_post_is_allowed_once_in_the_fill_when_the_student_allowed_it(self):
        self.assertAllowed(decide(FILL, resume_request()), "resume_upload", resume_post=True)

    def test_the_resume_post_is_allowed_in_the_students_turn_for_any_file_without_the_setting_or_a_count(self):
        st = state(resume_upload_allowed=False, resume_sha256="", resume_posts_passed=3)
        self.assertAllowed(decide(STUDENT, resume_request(body=resume_body(b"%PDF a different file the student chose")), st), "resume_upload", resume_post=True)

    def test_every_other_write_is_refused_on_any_host_and_every_other_upload_too(self):
        cases = [
            ("POST", HOST, f"/{SITE}/{JOB}/other"), ("PUT", HOST, APPLY_PATH), ("DELETE", HOST, "/anything"), ("PATCH", EU, "/anything"),
            ("POST", HOST, "/parseResumeX"), ("POST", HOST, "/searchLocations"), ("POST", "example-games.test", "/collect"),
            ("POST", "bucket.s3.amazonaws.com", "/upload"), ("PUT", "uploads.example-games.test", "/file"), ("POST", "lever.co", "/"),
            ("POST", "hire.lever.co", "/"), ("POST", "hcaptcha.com.example.test", "/"),
        ]
        for phase in self.PHASES:
            for method, host, path in cases:
                with self.subTest(phase=phase, method=method, host=host, path=path):
                    self.assertAborted(decide(phase, request(method, host, path, body=multipart(("resume", "cv.pdf", "application/pdf", b"%PDF"))
                                                           , headers=form_headers())), "non_get_before_hand_over")

    def test_a_second_resume_post_in_the_fill_is_refused_and_the_students_turn_has_no_such_limit(self):
        st = state()
        first = decide(FILL, resume_request(), st)
        self.assertAllowed(first)
        st.record(first)
        self.assertEqual(st.resume_posts_passed, 1)
        self.assertAborted(decide(FILL, resume_request(), st), "resume_post_second")
        self.assertAllowed(decide(STUDENT, resume_request(), st))

    def test_a_refused_resume_post_is_not_counted(self):
        st = state()
        self.assertAborted(decide(FILL, resume_request(body=resume_body(b"another file")), st), "resume_post_file")
        self.assertEqual(st.resume_posts_passed, 0)
        self.assertAllowed(decide(FILL, resume_request(), st))

    def test_telemetry_is_refused_for_every_method_silently_and_never_counted_as_a_send(self):
        for phase in self.PHASES:
            for host in ("www.googletagmanager.com", "googletagmanager.com", "www.google-analytics.com", "region1.google-analytics.com", "notify.bugsnag.com", "sessions.bugsnag.com"):
                for method in ("GET", "HEAD", "OPTIONS", "POST", "PUT"):
                    with self.subTest(phase=phase, host=host, method=method):
                        facts = request(method, host, "/collect", body=b"v=1" if method in ("POST", "PUT") else None)
                        decision = decide(phase, facts)
                        self.assertAborted(decision, "telemetry")
                        self.assertEqual(decision.record(method)["rule"], "telemetry")
                        self.assertFalse(student_submit_elsewhere(facts, state(), POLICY))
                        self.assertFalse(checks.looks_like_a_send(facts, state(last_press_at=checks.time.monotonic()), POLICY))

    def test_the_apply_post_before_the_student_is_in_charge_is_refused(self):
        decision = decide(FILL, apply_request())
        self.assertAborted(decision, "before_hand_over")

    def test_a_write_a_cloudflare_beacon_makes_is_never_the_turn_ending_elsewhere(self):
        beacon = request("POST", HOST, "/cdn-cgi/challenge-platform/h/b/jsd/oneshot/a1", body=b"{}", headers={"Content-Type": "application/json"})
        self.assertFalse(student_submit_elsewhere(beacon, state(), POLICY))
        self.assertFalse(student_submit_elsewhere(request("POST", EU, "/cdn-cgi/challenge-platform/h/b", kind="document"), state(), POLICY))
        self.assertTrue(student_submit_elsewhere(request("POST", HOST, "/cdn-cgi/rum", body=b"{}"), state(), POLICY), "another path on a form host still is")
        self.assertTrue(student_submit_elsewhere(request("POST", HOST, APPLY_PATH, body=b"{}", headers={"Content-Type": "application/json"}), state(), POLICY))
        self.assertFalse(student_submit_elsewhere(request("POST", "js.hcaptcha.com", "/1/x", body=b"{}"), state(), POLICY))


class HandOverCellTests(Cases):
    """Row 2: the student's first POST to the apply URL."""

    def test_it_asks_the_parent_for_the_hand_over(self):
        decision = decide(STUDENT, apply_request())
        self.assertAllowed(decision, "hand_over", requires_hand_over=True, submit_post=True)

    def test_it_asks_on_the_eu_host_too(self):
        self.assertAllowed(decide(STUDENT, apply_request(host=EU), state(board_host=EU)), "hand_over", requires_hand_over=True)

    def test_what_is_not_that_post_is_refused(self):
        cases = {
            "the other Lever host's path": apply_request(host=EU),
            "another posting's apply path": apply_request(path=f"/{SITE}/ffffffff-0000-4000-8000-000000000000/apply"),
            "the thanks path": apply_request(path=f"/{SITE}/{JOB}/thanks"),
            "a trailing slash": apply_request(path=APPLY_PATH + "/"),
            "a query on another path": apply_request(path=f"/{SITE}/{JOB}"),
            "a json body": apply_request(headers={"Content-Type": "application/json"}, body=b"{}"),
            "a urlencoded body": apply_request(headers={"Content-Type": "application/x-www-form-urlencoded"}, body=b"a=b"),
            "no content type": apply_request(headers={}, body=b"x"),
            "a put": request("PUT", HOST, APPLY_PATH, body=b"x", headers=form_headers()),
        }
        for label, facts in cases.items():
            with self.subTest(label):
                self.assertAborted(decide(STUDENT, facts, state()))

    def test_it_is_refused_when_no_host_is_bound(self):
        self.assertAborted(decide(STUDENT, apply_request(), state(board_host="")))

    def test_it_is_refused_when_the_loader_gave_no_submit_path(self):
        self.assertAborted(decide(STUDENT, apply_request(), state(submit_path="")))

    def test_a_planned_value_in_a_refused_look_alike_is_the_value_guards_to_refuse(self):
        decision = decide(STUDENT, apply_request(headers={"Content-Type": "application/json"}, body=b'{"email": "sam.rivera@example.test"}'))
        self.assertAborted(decision, "value_guard")

    def test_a_refusal_of_the_look_alike_ends_the_turn_as_the_form_posting_elsewhere(self):
        facts = apply_request(headers={"Content-Type": "application/json"}, body=b"{}")
        self.assertAborted(decide(STUDENT, facts))
        self.assertTrue(student_submit_elsewhere(facts, state(), POLICY))


class AfterHandOverCellTests(Cases):
    """Row 3: after hand-over."""

    def test_one_post_to_the_apply_url_per_attempt(self):
        st = state()
        first = decide(AFTER, apply_request(), st)
        self.assertAllowed(first, "submit", submit_post=True)
        st.record(first)
        self.assertAborted(decide(AFTER, apply_request(), st), "second_submit_post")

    def test_a_second_press_after_the_first_passed_is_refused_in_the_students_turn_too(self):
        self.assertAborted(decide(STUDENT, apply_request(), state(submit_posts_passed=1)), "second_submit_post")

    def test_a_write_to_a_captcha_endpoint_and_to_cloudflares_challenge_path_is_allowed(self):
        self.assertAllowed(decide(AFTER, request("POST", "api.hcaptcha.com", "/checkcaptcha/x", body=b"{}")), "captcha")
        self.assertAllowed(decide(AFTER, request("POST", HOST, "/cdn-cgi/challenge-platform/h/b/x", body=b"{}")), "challenge")

    def test_gets_are_allowed(self):
        for phase_state in (state(), state(submit_posts_passed=1)):
            for host, path in ((HOST, f"/{SITE}/{JOB}/thanks"), (EU, "/js/application.js"), ("js.hcaptcha.com", "/1/api.js")):
                with self.subTest(host=host):
                    self.assertAllowed(decide(AFTER, request("GET", host, path), phase_state))

    def test_after_the_post_a_get_to_a_lever_host_may_carry_a_value_but_a_get_to_any_other_host_may_not(self):
        st = state(submit_posts_passed=1)
        self.assertAllowed(decide(AFTER, request("GET", HOST, f"/{SITE}/{JOB}/thanks", query="e=sam.rivera%40example.test"), st))
        self.assertAborted(decide(AFTER, request("GET", "js.hcaptcha.com", "/1/api.js", query="e=sam.rivera%40example.test"), st), "value_guard")

    def test_every_other_write_is_refused(self):
        cases = [
            ("POST", HOST, "/parseResume"), ("POST", HOST, "/anything"), ("PUT", HOST, APPLY_PATH + "/"), ("POST", EU, "/searchLocations"), ("POST", "example-games.test", "/c"),
            ("POST", "bucket.s3.amazonaws.com", "/u"), ("POST", HOST, "/cdn-cgi/rum"),
        ]
        for method, host, path in cases:
            with self.subTest(method=method, host=host, path=path):
                body = multipart(("resume", "cv.pdf", "application/pdf", b"%PDF-1.4 neutral"))
                self.assertAborted(decide(AFTER, request(method, host, path, body=body, headers=form_headers())), "other_non_get")

    def test_the_resume_post_is_refused_after_hand_over(self):
        self.assertAborted(decide(AFTER, neutral_request()), "other_non_get")
        self.assertAborted(decide(AFTER, resume_request()), "value_guard")

    def test_telemetry_is_refused_after_hand_over_too(self):
        for method in ("GET", "POST"):
            self.assertAborted(decide(AFTER, request(method, "www.googletagmanager.com", "/gtm.js")), "telemetry")


# --- The resume POST: one test per condition -----------------------------------------------------------------------------

class ResumePostConditionTests(Cases):
    def test_all_of_them_hold_in_the_fill(self):
        decision = decide(FILL, resume_request())
        self.assertAllowed(decision, "resume_upload", resume_post=True, digest=RESUME_SHA, requires_hand_over=False, submit_post=False)

    def test_the_setting_is_off(self):
        self.assertAborted(decide(FILL, resume_request(), state(resume_upload_allowed=False)), "resume_post_off")

    def test_the_setting_is_on_but_no_file_was_planned(self):
        self.assertAborted(decide(FILL, resume_request(), state(resume_sha256="")), "resume_post_file")

    def test_a_second_one(self):
        self.assertAborted(decide(FILL, resume_request(), state(resume_posts_passed=1)), "resume_post_second")

    def test_the_wrong_host(self):
        for host in (EU, "example-games.test", "lever.co", "jobs.lever.co.example.test"):
            with self.subTest(host=host):
                self.assertAborted(decide(FILL, neutral_request(host=host)), "non_get_before_hand_over")
                self.assertAborted(decide(STUDENT, neutral_request(host=host)), "non_get_before_hand_over")
                self.assertAborted(decide(FILL, resume_request(host=host)), "value_guard")   # off the posting's host the file's own bytes are no longer exempt

    def test_no_host_bound(self):
        self.assertAborted(decide(FILL, neutral_request(), state(board_host="")), "non_get_before_hand_over")
        self.assertAborted(decide(FILL, resume_request(), state(board_host="")), "value_guard")

    def test_the_eu_host_when_that_is_the_postings(self):
        self.assertAllowed(decide(FILL, resume_request(host=EU), state(board_host=EU)), "resume_upload")
        self.assertAborted(decide(FILL, neutral_request(host=HOST), state(board_host=EU)), "non_get_before_hand_over")

    def test_the_wrong_path(self):
        for path in ("/parseresume", "/parseResume/", "/parseResume/x", "/api/parseResume", "/parseResumeX", "/", "//parseResume"):
            with self.subTest(path=path):
                self.assertAborted(decide(FILL, neutral_request(path=path)), "non_get_before_hand_over")
                self.assertAborted(decide(STUDENT, neutral_request(path=path)), "non_get_before_hand_over")
                self.assertAborted(decide(FILL, resume_request(path=path)), "value_guard")

    def test_the_wrong_content_type(self):
        for headers in ({}, {"Content-Type": "application/json"}, {"Content-Type": "application/x-www-form-urlencoded"}, {"Content-Type": "application/octet-stream"},
                        {"Content-Type": "multipart/mixed; boundary=" + BOUNDARY}, {"content-type": "text/plain"}):
            with self.subTest(headers=headers):
                for phase in (FILL, STUDENT):
                    self.assertAborted(decide(phase, resume_request(headers=headers)), "resume_post_content_type")

    def test_the_content_type_is_read_in_any_header_case_and_the_boundary_keeps_its_case(self):
        headers = {"content-TYPE": f"Multipart/Form-Data; boundary={BOUNDARY}"}
        self.assertAllowed(decide(FILL, resume_request(headers=headers)), "resume_upload")
        self.assertAllowed(decide(FILL, resume_request(headers={"Content-Type": f'multipart/form-data; boundary="{BOUNDARY}"'})), "resume_upload")
        self.assertAborted(decide(FILL, resume_request(headers=form_headers(boundary=BOUNDARY.lower()))), "resume_post_parts")

    def test_a_third_part(self):
        body = multipart(("resume", "cv.pdf", "application/pdf", RESUME), ("accountId", None, "", ACCOUNT.encode()), ("note", None, "", b"x"))
        for phase in (FILL, STUDENT):
            self.assertAborted(decide(phase, resume_request(body=body)), "resume_post_parts")

    def test_a_missing_part(self):
        only_file = multipart(("resume", "cv.pdf", "application/pdf", RESUME))
        only_account = multipart(("accountId", None, "", ACCOUNT.encode()))
        for body in (only_file, only_account, multipart()):
            with self.subTest(body=body[:40]):
                self.assertAborted(decide(FILL, resume_request(body=body)), "resume_post_parts")

    def test_two_parts_that_are_not_these_two(self):
        for parts in ((("resume", "cv.pdf", "", RESUME), ("resume", "cv2.pdf", "", RESUME)), (("file", "cv.pdf", "", RESUME), ("accountId", None, "", ACCOUNT.encode())),
                      (("resume", "cv.pdf", "", RESUME), ("accountid", None, "", ACCOUNT.encode())), (("accountId", None, "", ACCOUNT.encode()), ("accountId", None, "", ACCOUNT.encode()))):
            with self.subTest(parts=[part[0] for part in parts]):
                self.assertAborted(decide(FILL, resume_request(body=multipart(*parts))), "resume_post_parts")

    def test_the_parts_may_come_in_either_order(self):
        body = multipart(("accountId", None, "", ACCOUNT.encode()), ("resume", "cv.pdf", "application/pdf", RESUME))
        self.assertAllowed(decide(FILL, resume_request(body=body)), "resume_upload")

    def test_the_resume_part_must_be_a_file_and_the_account_part_must_not(self):
        self.assertAborted(decide(FILL, resume_request(body=multipart(("resume", None, "", RESUME), ("accountId", None, "", ACCOUNT.encode())))), "resume_post_parts")
        self.assertAborted(decide(FILL, resume_request(body=multipart(("resume", "cv.pdf", "", RESUME), ("accountId", "a.txt", "", ACCOUNT.encode())))), "resume_post_parts")

    def test_a_part_with_a_header_a_browser_does_not_write(self):
        smuggled = (b"--" + BOUNDARY.encode() + b'\r\nContent-Disposition: form-data; name="accountId"\r\nX-Note: Samantha\r\n\r\n' + ACCOUNT.encode() + b"\r\n")
        body = b"--" + BOUNDARY.encode() + b'\r\nContent-Disposition: form-data; name="resume"; filename="cv.pdf"\r\n\r\n' + RESUME + b"\r\n" + smuggled + b"--" + BOUNDARY.encode() + b"--\r\n"
        self.assertAborted(decide(FILL, resume_request(body=body)), "resume_post_parts")
        other = (b"--" + BOUNDARY.encode() + b'\r\nContent-Disposition: form-data; name="resume"; filename="cv.pdf"\r\nX-Note: Samantha\r\n\r\n' + RESUME + b"\r\n"
                 + b"--" + BOUNDARY.encode() + b'\r\nContent-Disposition: form-data; name="accountId"\r\n\r\n' + ACCOUNT.encode() + b"\r\n--" + BOUNDARY.encode() + b"--\r\n")
        self.assertAborted(decide(FILL, resume_request(body=other)), "resume_post_parts")

    def test_a_body_that_is_not_a_clean_multipart_body(self):
        good = resume_body()
        marker = b"--" + BOUNDARY.encode()
        bodies = {
            "a preamble": b"Samantha\r\n" + good,
            "text after the closing boundary": good + b"Samantha",
            "no closing boundary": good[:-len(marker) - 4],
            "no body": None,
            "an empty body": b"",
            "a string body": good.decode("latin-1"),
            "a malformed header block": good.replace(b"\r\n\r\n", b"\r\n", 1),
            "a bare line feed": good.replace(b"\r\n", b"\n"),
        }
        for label, body in bodies.items():
            with self.subTest(label):
                decision = decide(FILL, request("POST", HOST, "/parseResume", body=body, headers=form_headers()))
                self.assertAborted(decision)
                self.assertTrue(decision.rule.startswith("resume_post_"), decision.rule)

    def test_the_resume_part_is_a_different_file_in_the_fill(self):
        self.assertAborted(decide(FILL, resume_request(body=resume_body(RESUME + b" "))), "resume_post_file")
        self.assertAborted(decide(FILL, resume_request(body=resume_body(b""))), "resume_post_file")

    def test_the_planned_digest_is_compared_in_any_case(self):
        self.assertAllowed(decide(FILL, resume_request(), state(resume_sha256=RESUME_SHA.upper())), "resume_upload")

    def test_the_resume_part_is_any_file_in_the_students_turn(self):
        for data in (b"%PDF something else", b"", b"x" * 5000):
            with self.subTest(size=len(data)):
                self.assertAllowed(decide(STUDENT, resume_request(body=resume_body(data))), "resume_upload")

    def test_the_account_id_differs_from_the_pages(self):
        for phase in (FILL, STUDENT):
            self.assertAborted(decide(phase, resume_request(body=resume_body(account="11111111-2222-4333-8444-555555555555"))), "resume_post_account")
            self.assertAborted(decide(phase, resume_request(body=resume_body(account=ACCOUNT + " "))), "resume_post_account")
            self.assertAborted(decide(phase, resume_request(body=resume_body(account=ACCOUNT.upper()))), "resume_post_account")

    def test_the_page_carries_no_account_id(self):
        self.assertAborted(decide(FILL, resume_request(body=resume_body(account="")), state(page_account_id="")), "resume_post_account")
        self.assertAborted(decide(FILL, resume_request(), state(page_account_id="")), "resume_post_account")

    def test_a_planned_value_in_the_url(self):
        for query in ("e=sam.rivera%40example.test", "n=Samantha", "who=Tidewater+Games"):
            with self.subTest(query=query):
                for phase in (FILL, STUDENT):
                    self.assertAborted(decide(phase, resume_request(query=query)), "value_guard")

    def test_a_planned_value_in_a_header(self):
        for name, value in (("Referer", "https://jobs.lever.co/x?n=Samantha"), ("X-Note", "Samantha"), ("Cookie", "who=sam.rivera@example.test")):
            with self.subTest(header=name):
                for phase in (FILL, STUDENT):
                    decision = decide(phase, resume_request(headers=form_headers(**{name: value})))
                    self.assertAborted(decision, "value_guard")

    def test_a_planned_value_in_the_boundary(self):
        boundary = "----Samantha7Qx"
        body = multipart(("resume", "cv.pdf", "application/pdf", RESUME), ("accountId", None, "", ACCOUNT.encode()), boundary=boundary)
        self.assertAborted(decide(FILL, resume_request(body=body, headers=form_headers(boundary=boundary))), "value_guard")

    def test_the_resume_part_is_exempt_from_the_body_check_its_bytes_and_its_file_name_hold_the_students_name_and_email(self):
        self.assertIn(VALUES["email"].encode(), RESUME)
        self.assertAllowed(decide(FILL, resume_request(body=resume_body(filename="Samantha Rivera Resume.pdf"))), "resume_upload")

    def test_only_the_resume_part_is_exempt_the_rest_of_the_body_is_checked(self):
        # An account number that is the page's own but is also a planned value: the page's value is not the student's, so this is a planted fixture.
        collides = {"account": ACCOUNT}
        self.assertAborted(decide(FILL, resume_request(), state(values={**VALUES, **collides})), "value_guard")

    def test_the_record_of_a_refusal_carries_the_rule_and_a_field_key_and_never_a_value(self):
        decision = decide(FILL, resume_request(query="n=Samantha"))
        record = decision.record("POST")
        self.assertEqual(record, {"method": "POST", "host": HOST, "rule": "value_guard", "field_key": "first_name"})

    def test_it_is_judged_only_in_a_handoff(self):
        for mode, phase in (("submit", FILL), ("submit", AFTER), ("rehearse", "after_input"), ("lookup", "before_input")):
            with self.subTest(mode=mode, phase=phase):
                self.assertAborted(decide(phase, resume_request(), mode=mode))

    def test_the_decision_is_counted_by_the_state_only_when_it_is_recorded(self):
        st = state()
        decision = decide(FILL, resume_request(), st)
        self.assertEqual(st.resume_posts_passed, 0)
        st.record(decision)
        self.assertEqual((st.resume_posts_passed, st.submit_posts_passed, st.code_posts_passed), (1, 0, 0))

    def test_a_refused_resume_post_in_the_students_turn_is_a_write_to_a_form_host(self):
        # Spec 7 lists no exception: a malformed one is refused like any write to a form address, and the agent's handler decides what that means for the turn.
        facts = resume_request(body=multipart(("resume", "cv.pdf", "", RESUME)))
        self.assertAborted(decide(STUDENT, facts), "resume_post_parts")
        self.assertTrue(student_submit_elsewhere(facts, state(), POLICY))


class MultipartReaderTests(unittest.TestCase):
    def test_it_reads_the_parts_with_their_places_in_the_body(self):
        body = resume_body()
        parts = checks.read_multipart(f"multipart/form-data; boundary={BOUNDARY}", body)
        self.assertEqual([(p.name, p.filename, p.data) for p in parts], [("resume", "Samantha_Rivera_Resume.pdf", RESUME), ("accountId", None, ACCOUNT.encode())])
        self.assertEqual(parts[0].headers, (("content-disposition", 'form-data; name="resume"; filename="Samantha_Rivera_Resume.pdf"'), ("content-type", "application/pdf")))
        self.assertIn(RESUME, body[parts[0].start:parts[0].end])
        self.assertNotIn(RESUME, body[:parts[0].start] + body[parts[0].end:])

    def test_it_keeps_binary_bytes_and_line_breaks_inside_a_part(self):
        data = bytes(range(256)) + b"\r\n\r\n--not-the-boundary\r\n"
        parts = checks.read_multipart(f"multipart/form-data; boundary={BOUNDARY}", multipart(("resume", "a.bin", "application/octet-stream", data), ("accountId", None, "", b"x")))
        self.assertEqual(parts[0].data, data)

    def test_it_refuses_what_it_is_not_sure_of(self):
        body = resume_body()
        for label, content_type, data in (
            ("not multipart", "application/json", body), ("no boundary", "multipart/form-data", body), ("another boundary", "multipart/form-data; boundary=zzz", body),
            ("an overlong boundary", "multipart/form-data; boundary=" + "b" * 71, body), ("a string body", f"multipart/form-data; boundary={BOUNDARY}", body.decode("latin-1")),
            ("a trailing parameter", f"multipart/form-data; boundary={BOUNDARY}; charset=utf-8", body),
        ):
            with self.subTest(label):
                self.assertIsNone(checks.read_multipart(content_type, data))


# --- The names the browser may look up ----------------------------------------------------------------------------------

class ResolvableHostsTests(unittest.TestCase):
    """``RESOLVABLE_HOSTS`` is the union of every registered policy's hosts and the fonts (tests/test_apply_ats_seam.py pins that); these are Lever's cells of it."""

    def covered(self, host):
        return any(fnmatch.fnmatchcase(host, pattern) for pattern in apply_agent.RESOLVABLE_HOSTS)

    def test_every_host_the_lever_policy_must_reach_resolves_except_the_named_captcha_gap(self):
        needed = {*POLICY.navigation_hosts, *POLICY.submit_hosts, *POLICY.form_post_hosts, *(endpoint.host for endpoint in POLICY.lookup_endpoints)}
        needed |= {endpoint.host for endpoint in POLICY.captcha_endpoints}
        gap = {host for host in needed if not self.covered(host)}
        self.assertEqual(gap, set(checks.LEVER_CAPTCHA_RESOLVABLE_HOSTS), "the CAPTCHA hosts are the one thing left for the Lever driver to join to the resolver rule")
        for host in checks.LEVER_CAPTCHA_RESOLVABLE_HOSTS:
            self.assertIn(host, {endpoint.host for endpoint in POLICY.captcha_endpoints})

    def test_every_lever_name_in_the_resolver_rule_is_one_the_policy_needs(self):
        lever_names = set(POLICY.resolvable_hosts)
        self.assertEqual(lever_names, set(lever.LEVER_HOSTS))
        for host in lever_names:
            self.assertTrue(host in POLICY.navigation_hosts and host in POLICY.submit_hosts and POLICY.static_asset_host(host))
            self.assertTrue(self.covered(host))

    def test_the_union_over_every_registered_policy_is_the_rule(self):
        union = {host for spec in apply_ats.REGISTRY for host in spec.route_policy.resolvable_hosts}
        self.assertEqual(set(apply_agent.RESOLVABLE_HOSTS), union | set(apply_agent.FONT_HOSTS))

    def test_usage_reporting_never_resolves(self):
        for domain in checks.LEVER_TELEMETRY_DOMAINS:
            for host in (domain, "www." + domain, "notify." + domain):
                with self.subTest(host=host):
                    self.assertFalse(self.covered(host))


if __name__ == "__main__":
    unittest.main()
