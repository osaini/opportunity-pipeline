"""The fictional Greenhouse fixtures, the fake that serves them, and the checks that read them.

The first half needs no browser: the fixtures parse, agree with each other, and
name no real employer; FakeGreenhouse and FakeSchemaClient answer as documented;
browser_support fails loudly when Chromium is required and broken. The second half
runs real Chromium against the fake (skipped without it, required in the CI job
`browser-python`). It proves that REQUIRED_CHECK_SCRIPT reads the page the way
check_required expects, that what a real browser reports fits `Observation`, and that
`route_decision` refuses in a real browser what its table says it refuses. The agent
itself (apply_agent.py) is M5a: nothing here fills a form except the test.
"""

import io
import json
import os
import re
import sys
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apply_fake_ats
import browser_support
from helpers_apply import FakePlan, planned
from apply_fake_ats import (
    API_HOST,
    CONFIRMATION_PATH,
    CONFIRMATION_URL,
    DATA_COMPLIANCE_CONTROLS,
    DEMOGRAPHIC_CONTROL,
    JOB_HOST,
    JOB_ID,
    JOB_PATH,
    JOB_URL,
    LEGACY_JOB_ID,
    OFFSITE_HOST,
    SCENARIOS,
    SUBMIT_HOST,
    FakeGreenhouse,
    FakeSchemaClient,
    choose,
    complete_form,
    fixture_json,
    fixture_text,
    form_values,
    press_submit,
    type_security_code,
)
from browser_support import requires_chromium

from opportunity_app.apply_checks import (
    PHASE_AFTER_HAND_OVER,
    PHASE_AFTER_INPUT,
    PHASE_BEFORE_INPUT,
    PHASE_FILL,
    REQUIRED_CHECK_SCRIPT,
    Abort,
    Endpoint,
    Observation,
    RouteRequest,
    RouteState,
    SeenRequest,
    check_required,
    decide_outcome,
    question_key,
    route_decision,
)

EMAIL = "sam.rivera@example.test"
LOOKUP = Endpoint(API_HOST, "/fake-lookup/", "location")
LOOKUP_LOCATION = Endpoint(API_HOST, "/fake-lookup/location", "location")
LOOKUP_SCHOOL = Endpoint(API_HOST, "/fake-lookup/school", "school")
FIXTURE_NAMES = (
    "new_form.html", "new_confirmation.html", "closed.html", "offsite.html", "text_only_thanks.html",
    "legacy_form.html", "legacy_confirmation.html", "schema_new.json", "schema_legacy.json", "security_code_428.json",
)


def schema_fields(schema):
    """Every field of a Job Board API listing as (name, label, required, type), in every section."""
    found = []
    for block in schema.get("questions", []) + schema.get("location_questions", []):
        for entry in block["fields"]:
            found.append((entry["name"], block["label"], block["required"], entry["type"]))
    for block in schema.get("compliance", []):
        for question in block["questions"]:
            for entry in question["fields"]:
                found.append((entry["name"], question["label"], question["required"], entry["type"]))
    # The live listing gives these two no control name (a demographic question has an id, a
    # data_compliance entry only its type and flags), so the names come from the fake's constants.
    for question in (schema.get("demographic_questions") or {}).get("questions", []):
        found.append((DEMOGRAPHIC_CONTROL.format(id=question["id"]), question["label"], question["required"], question["type"]))
    for block in schema.get("data_compliance", []):
        if block.get("requires_consent"):
            name, label = DATA_COMPLIANCE_CONTROLS[block["type"]]
            found.append((name, label, True, "multi_value_multi_select"))
    return found


class DomIndex(HTMLParser):
    """The controls a static page names and the label text next to each, without running its script."""

    def __init__(self):
        super().__init__()
        self.controls = set()
        self.labels = {}
        self.legends = {}
        self._label = None
        self._legend = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("input", "select", "textarea"):
            for attribute in ("name", "id"):
                if a.get(attribute):
                    self.controls.add(a[attribute])
            if self._label is not None and a.get("name"):
                self._label["controls"].append(a["name"])
        elif tag == "div" and "rs" in (a.get("class") or "").split():
            self.controls.add(a["data-name"])
        elif tag == "label":
            self._label = {"for": a.get("for"), "text": [], "controls": []}
        elif tag == "legend":
            self._legend = {"id": a.get("id"), "text": []}

    def handle_data(self, data):
        for open_tag in (self._label, self._legend):
            if open_tag is not None:
                open_tag["text"].append(data)

    def handle_endtag(self, tag):
        if tag == "label" and self._label is not None:
            text = " ".join("".join(self._label["text"]).split())
            for key in [self._label["for"], *self._label["controls"]]:
                if key:
                    self.labels[key] = text
            self._label = None
        elif tag == "legend" and self._legend is not None:
            self.legends[self._legend["id"]] = " ".join("".join(self._legend["text"]).split())
            self._legend = None


class FixtureTests(unittest.TestCase):
    def test_every_fixture_exists_and_the_json_ones_parse(self):
        for name in FIXTURE_NAMES:
            with self.subTest(name=name):
                text = fixture_text(name)
                self.assertTrue(text.strip())
                if name.endswith(".json"):
                    json.loads(text)

    def test_the_fixtures_name_only_greenhouse_hosts_and_reserved_test_domains(self):
        allowed = re.compile(r"^(?:[a-z0-9-]+\.)?greenhouse\.io$|^(?:[a-z0-9.-]+\.)?(?:example|examplerobotics|example-robotics)\.test$|^[a-z0-9-]+\.s3\.amazonaws\.com$")
        for name in FIXTURE_NAMES:
            for host in re.findall(r"(?:https?|wss)://([^/\"'\s<>?#]+)", fixture_text(name)):
                with self.subTest(name=name, host=host):
                    self.assertRegex(host, allowed)

    def test_ids_and_the_company_are_fictional(self):
        for name in FIXTURE_NAMES:
            text = fixture_text(name)
            for job_id in re.findall(r"/jobs/(\d+)", text) + re.findall(r"question_(\d+)", text):
                with self.subTest(name=name, job_id=job_id):
                    self.assertTrue(job_id.startswith("4000000"), job_id)
        for name in FIXTURE_NAMES:
            if name.endswith(".html"):
                self.assertIn("FICTIONAL", fixture_text(name))

    def test_the_form_has_the_structure_the_spec_lists(self):
        html = fixture_text("new_form.html")
        for needle in (
            'form id="application-form"', "Submit application", 'id="resume"', 'class="visually-hidden"', 'data-allow-s3="false"',
            "Autofill my application", "Locate me", "window.__forbiddenClicks", '"submitPath"', '"confirmationPath"',
            'name="website_url"', 'class="trap"', "select__single-value", "select__multi-value__label", "IMITATION",
            'name="gender"', 'name="hispanic_ethnicity"', 'name="veteran_status"', 'name="disability_status"',
            "question_4000000101", "question_4000000109", "<!--FAKE_SCENARIO-->",
        ):
            with self.subTest(needle=needle):
                self.assertIn(needle, html)

    def test_the_form_carries_every_kind_of_required_marker(self):
        html = fixture_text("new_form.html")
        self.assertRegex(html, r'<input id="last_name"[^>]* required')                       # the required attribute
        self.assertRegex(html, r'<input id="email"[^>]*aria-required="true"')                # aria-required
        self.assertRegex(html, r'Why do you want to work at Example Robotics\? \*</label>')  # an asterisk in the label
        self.assertIn('<span class="required"></span>', html)                                # span.required in the upload group
        self.assertIn('aria-hidden="true" tabindex="-1" class="visually-hidden" value=""', html)   # the hidden required mirror
        self.assertIn('role="group" aria-required="true"', html)                             # aria-required on a radio group

    def test_the_loader_paths_read_the_way_the_adapter_will_read_them(self):
        for name, submit in (("new_form.html", JOB_PATH), ("text_only_thanks.html", JOB_PATH)):
            html = fixture_text(name)
            self.assertEqual(re.search(r'"submitPath":"([^"]*)"', html).group(1), submit)
            self.assertEqual(re.search(r'"confirmationPath":"([^"]*)"', html).group(1), CONFIRMATION_PATH)

    def test_the_form_and_its_listing_agree_on_every_field_and_question(self):
        page = DomIndex()
        page.feed(fixture_text("new_form.html"))
        page.labels["question_4000000111"] = page.legends["q111-label"]
        listing = schema_fields(fixture_json("schema_new.json"))
        for name, label, required, kind in listing:
            with self.subTest(name=name):
                if kind == "input_hidden":
                    self.assertIn(name, page.controls)
                    continue
                if name in page.controls:
                    self.assertEqual(question_key(page.labels[name]), question_key(label))
                else:
                    # (resume_text is the paste-instead alternative of the required upload)
                    self.assertTrue(name.endswith("_text") or not required, f"{name} is required but the form has no control for it")
        listed = {name for name, *_ in listing}
        for control in page.controls - {"website_url"}:
            if control.startswith(("select_", "security-input")) or control in ("security_code", "locate-me"):
                continue
            self.assertIn(control, listed, f"the form has a control the listing does not mention: {control}")

    def test_the_listing_covers_every_section_with_the_spec_named_fields(self):
        schema = fixture_json("schema_new.json")
        sections = ("questions", "location_questions", "compliance", "demographic_questions", "data_compliance")
        for section in sections:
            self.assertIn(section, schema)
        types = {kind for *_, kind in schema_fields(schema)}
        self.assertEqual(types, {"input_text", "input_file", "textarea", "input_hidden", "multi_value_single_select", "multi_value_multi_select"})
        names = [name for name, *_ in schema_fields(schema)]
        self.assertEqual(len(names), len(set(names)))
        for name in ("gender", "hispanic_ethnicity", "veteran_status", "disability_status", "location_city", "question_4000000105", "question_4000000106"):
            self.assertIn(name, names)

    def test_the_listing_keeps_to_the_keys_the_live_job_board_api_returns(self):
        # M4's parse_schema must not come to depend on a key a real board never sends.
        schema = fixture_json("schema_new.json")
        for block in schema["data_compliance"]:
            self.assertLessEqual(set(block), {"type", "requires_consent", "requires_processing_consent", "requires_retention_consent",
                                              "retention_period", "demographic_data_consent_applies"})
        for question in schema["demographic_questions"]["questions"]:
            self.assertLessEqual(set(question), {"id", "label", "required", "type", "answer_options"})
        # What the listing does not say is derived, from the fake's documented constants.
        names = {name for name, *_ in schema_fields(schema)}
        self.assertIn("gdpr_consent_given", names)
        self.assertIn("question_4000000114", names)

    def test_the_legacy_listing_and_page_agree_on_the_standard_fields(self):
        listing = {name: label for name, label, *_ in schema_fields(fixture_json("schema_legacy.json"))}
        html = fixture_text("legacy_form.html")
        for name in ("first_name", "last_name", "email", "phone", "resume"):
            self.assertIn(name, listing)
            self.assertIn(f"job_application[{name}]", html)
        self.assertIn('form id="application_form"', html)
        self.assertIn('id="submit_app"', html)
        self.assertIn('class="asterisk"', html)

    def test_only_the_fixtures_the_confirmation_pages_lack_a_form(self):
        for name in ("new_confirmation.html", "legacy_confirmation.html", "closed.html"):
            self.assertNotIn("<form", fixture_text(name))
        self.assertIn("Thank you for applying", fixture_text("text_only_thanks.html"))
        self.assertIn('form id="application-form"', fixture_text("text_only_thanks.html"))

    def test_the_security_code_answer_is_a_428_captcha_failed(self):
        body = fixture_json("security_code_428.json")
        self.assertEqual((body["status"], body["error"]), (428, "captcha-failed"))


class FakeGreenhouseTests(unittest.TestCase):
    """The fake without a browser: what it answers to each request, and what it records."""

    def test_the_job_page_is_the_fixture_and_the_schema_is_served_on_the_api_host(self):
        fake = FakeGreenhouse()
        page = fake.answer("GET", JOB_URL)
        self.assertEqual(page.status, 200)
        self.assertIn('form id="application-form"', page.body)
        self.assertNotIn("FAKE_SCENARIO", page.body)      # the marker is replaced, even when there is no scenario script
        schema = fake.answer("GET", f"https://{API_HOST}/v1/boards/examplerobotics/jobs/{JOB_ID}?questions=true")
        self.assertEqual((schema.status, json.loads(schema.body)["id"]), (200, int(JOB_ID)))
        self.assertEqual(fake.answer("GET", f"https://{API_HOST}/v1/boards/examplerobotics/jobs/{JOB_ID}").status, 404)  # no questions=true
        self.assertEqual(fake.answer("GET", f"https://{API_HOST}/v1/boards/examplerobotics/jobs/9").status, 404)
        self.assertEqual(fake.answer("GET", CONFIRMATION_URL).body, fixture_text("new_confirmation.html"))

    def test_the_lookup_answers_with_options_containing_what_was_typed(self):
        fake = FakeGreenhouse()
        found = json.loads(fake.answer("GET", f"https://{API_HOST}{apply_fake_ats.LOOKUP_PATH}?q=Spring").body)
        self.assertEqual(found, list(apply_fake_ats.LOOKUP_OPTIONS))
        self.assertEqual(json.loads(fake.answer("GET", f"https://{API_HOST}{apply_fake_ats.LOOKUP_PATH}?q=zzz").body), [])

    def test_the_submit_answers_by_scenario(self):
        submit = f"https://{SUBMIT_HOST}{JOB_PATH}"
        expected = {"confirm": 200, "server_500": 500, "validation_422": 422, "security_code": 428, "text_only_thanks": 200, "double_submit": 200}
        for scenario, status in expected.items():
            with self.subTest(scenario=scenario):
                fake = FakeGreenhouse(scenario)
                reply = fake.answer("POST", submit, "x")
                self.assertEqual(reply.status, status)
                self.assertTrue(fake.submit_path_hit)
        self.assertIsNone(FakeGreenhouse("hang").answer("POST", submit, "x"))

    def test_no_answer_is_a_redirect_because_the_hop_after_one_would_leave_the_fake(self):
        for scenario in SCENARIOS:
            fake = FakeGreenhouse(scenario)
            for method, url in (("GET", JOB_URL), ("POST", f"https://{SUBMIT_HOST}{JOB_PATH}"), ("GET", CONFIRMATION_URL)):
                reply = fake.answer(method, url, "x", resource_type="document")
                if reply is not None:
                    with self.subTest(scenario=scenario, url=url):
                        self.assertNotIn("location", reply.headers)
                        self.assertFalse(300 <= reply.status < 400)

    def test_the_security_code_scenario_confirms_only_a_post_that_carries_a_code(self):
        fake = FakeGreenhouse("security_code")
        submit = f"https://{SUBMIT_HOST}{JOB_PATH}"
        empty = '--b\r\nContent-Disposition: form-data; name="security_code"\r\n\r\n\r\n--b--'
        typed = '--b\r\nContent-Disposition: form-data; name="security_code"\r\n\r\n1\r\n--b\r\nContent-Disposition: form-data; name="security_code"\r\n\r\n2\r\n--b--'
        self.assertEqual(fake.answer("POST", submit, empty).status, 428)
        self.assertEqual(json.loads(fake.answer("POST", submit, empty).body)["error"], "captcha-failed")
        self.assertEqual(fake.answer("POST", submit, typed).status, 200)
        self.assertEqual(form_values(typed), {"security_code": ["1", "2"]})

    def test_the_closed_offsite_and_text_only_scenarios_serve_their_pages(self):
        self.assertEqual(FakeGreenhouse("closed").answer("GET", JOB_URL).body, fixture_text("closed.html"))
        closed_api = FakeGreenhouse("closed").answer("GET", f"https://{API_HOST}/v1/boards/examplerobotics/jobs/{JOB_ID}?questions=true")
        self.assertEqual(closed_api.status, 404)
        self.assertIn(f"https://{OFFSITE_HOST}/apply", FakeGreenhouse("redirect_offsite").answer("GET", JOB_URL).body)
        self.assertEqual(FakeGreenhouse().answer("GET", f"https://{OFFSITE_HOST}/apply").body, fixture_text("offsite.html"))
        fake = FakeGreenhouse("text_only_thanks")
        self.assertIn('form id="application-form"', fake.answer("GET", JOB_URL).body)
        self.assertNotIn("Thank you", fake.answer("GET", JOB_URL).body)
        fake.answer("POST", f"https://{SUBMIT_HOST}{JOB_PATH}", "x")
        self.assertEqual(fake.answer("GET", JOB_URL).body, fixture_text("text_only_thanks.html"))

    def test_the_loader_and_s3_scenarios_change_the_page(self):
        self.assertNotIn('"submitPath"', FakeGreenhouse("loader_missing").answer("GET", JOB_URL).body)
        self.assertIn('"confirmationPath"', FakeGreenhouse("loader_missing").answer("GET", JOB_URL).body)
        self.assertIn('data-allow-s3="true"', FakeGreenhouse("s3_upload").answer("GET", JOB_URL).body)
        self.assertIn('data-allow-s3="false"', FakeGreenhouse().answer("GET", JOB_URL).body)

    def test_every_scenario_is_known_and_the_ones_with_a_page_script_carry_it(self):
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario):
                body = FakeGreenhouse(scenario).answer("GET", JOB_URL).body
                if scenario in apply_fake_ats._SCRIPTS:
                    self.assertIn(apply_fake_ats._SCRIPTS[scenario].strip()[:40], body)
        with self.assertRaises(ValueError):
            FakeGreenhouse("nonsense")

    def test_it_records_every_request_that_reached_it(self):
        fake = FakeGreenhouse()
        fake.answer("GET", JOB_URL, resource_type="document")
        fake.answer("POST", "https://analytics.example-robotics.test/collect", "v=1")
        fake.answer("POST", f"https://{SUBMIT_HOST}{JOB_PATH}", "x")
        self.assertEqual([(seen.method, seen.host) for seen in fake.requests],
                         [("GET", JOB_HOST), ("POST", "analytics.example-robotics.test"), ("POST", SUBMIT_HOST)])
        self.assertEqual([seen.path for seen in fake.non_get_requests()], ["/collect", JOB_PATH])
        self.assertEqual(len(fake.submit_posts()), 1)
        self.assertEqual(len(fake.requests_to(SUBMIT_HOST)), 1)
        self.assertFalse(FakeGreenhouse().submit_path_hit)

    def test_a_fake_route_fulfils_and_a_hung_submit_is_left_pending(self):
        class Request:
            method, url, post_data_buffer, resource_type = "GET", JOB_URL, None, "document"

        class Route:
            request = Request()
            fulfilled = None

            def fulfill(self, **kwargs):
                self.fulfilled = kwargs

        route = Route()
        FakeGreenhouse().route(route)
        self.assertEqual(route.fulfilled["status"], 200)
        Request.method, Request.url = "POST", f"https://{SUBMIT_HOST}{JOB_PATH}"
        hung = Route()
        FakeGreenhouse("hang").route(hung)
        self.assertIsNone(hung.fulfilled)


    def test_a_binary_upload_in_the_body_does_not_hide_the_text_fields_from_the_fake(self):
        # Playwright's post_data raises on a real PDF's bytes; the security_code scenario reads the code from the body.
        submit = f"https://{SUBMIT_HOST}{JOB_PATH}"
        pdf = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n"
        with_code = (b'--b\r\nContent-Disposition: form-data; name="resume"; filename="r.pdf"\r\nContent-Type: application/pdf\r\n\r\n' + pdf
                     + b'\r\n--b\r\nContent-Disposition: form-data; name="security_code"\r\n\r\n7\r\n--b--')
        without = with_code.replace(b'name="security_code"\r\n\r\n7', b'name="security_code"\r\n\r\n')
        self.assertRaises(UnicodeDecodeError, with_code.decode)

        class Request:
            method, url, resource_type = "POST", submit, "fetch"
            post_data_buffer = with_code

            @property
            def post_data(self):
                return with_code.decode("utf-8")       # what Playwright does: strict, and it raises

        class Route:
            request = Request()
            fulfilled = None

            def fulfill(self, **kwargs):
                self.fulfilled = kwargs

        for body, status in ((with_code, 200), (without, 428)):
            with self.subTest(status=status):
                Request.post_data_buffer = body
                route = Route()
                FakeGreenhouse("security_code").route(route)
                self.assertEqual(route.fulfilled["status"], status)


class FakeSchemaClientTests(unittest.TestCase):
    def test_it_serves_the_fixture_listing_without_a_network(self):
        client = FakeSchemaClient()
        listing = client.fetch("examplerobotics", JOB_ID)
        self.assertEqual(listing["title"], "Robotics Software Intern")
        self.assertEqual(client.calls, [("examplerobotics", JOB_ID)])
        listing["title"] = "changed"
        self.assertEqual(client.fetch("examplerobotics", JOB_ID)["title"], "Robotics Software Intern")   # a copy each time
        self.assertEqual(client("examplerobotics", JOB_ID)["id"], int(JOB_ID))

    def test_an_unknown_or_closed_job_is_a_404(self):
        client = FakeSchemaClient()
        self.assertIsNone(client.fetch("examplerobotics", "1"))
        self.assertIsNone(client.fetch("someoneelse", JOB_ID))
        self.assertIsNone(FakeSchemaClient(closed=True).fetch("examplerobotics", JOB_ID))

    def test_the_legacy_job_and_the_url_form(self):
        self.assertEqual(FakeSchemaClient().fetch("examplerobotics", LEGACY_JOB_ID)["title"], "Robotics Hardware Intern")
        self.assertEqual(FakeSchemaClient(legacy=True).fetch("examplerobotics", JOB_ID)["title"], "Robotics Hardware Intern")
        url = f"https://{API_HOST}/v1/boards/examplerobotics/jobs/{JOB_ID}?questions=true"
        self.assertEqual(FakeSchemaClient().fetch_url(url)["id"], int(JOB_ID))
        self.assertIsNone(FakeSchemaClient().fetch_url("https://boards-api.greenhouse.io/v1/boards/examplerobotics"))


class BrowserSupportTests(unittest.TestCase):
    """The helper that decides skip or fail: a required-but-broken Chromium must never skip."""

    def decorated(self):
        class Probe(unittest.TestCase):
            def test_it(self):
                pass

        return browser_support.requires_chromium(Probe)

    def test_with_chromium_the_class_is_left_alone(self):
        with mock.patch.object(browser_support, "chromium_launch_error", return_value=""):
            cls = self.decorated()
        self.assertFalse(getattr(cls, "__unittest_skip__", False))

    def test_without_chromium_and_not_required_the_class_skips(self):
        with mock.patch.dict(os.environ, {browser_support.REQUIRE_ENV: ""}), \
                mock.patch.object(browser_support, "chromium_launch_error", return_value="ImportError: no playwright"):
            cls = self.decorated()
        self.assertTrue(cls.__unittest_skip__)
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(unittest.defaultTestLoader.loadTestsFromTestCase(cls))
        self.assertEqual((result.testsRun, len(result.skipped), len(result.errors)), (1, 1, 0))

    def test_without_chromium_and_required_every_test_errors_with_the_launch_exception(self):
        with mock.patch.dict(os.environ, {browser_support.REQUIRE_ENV: "1"}), \
                mock.patch.object(browser_support, "chromium_launch_error", return_value="Error: launch failed: no display"):
            cls = self.decorated()
        self.assertFalse(getattr(cls, "__unittest_skip__", False))
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(unittest.defaultTestLoader.loadTestsFromTestCase(cls))
        self.assertEqual(len(result.skipped), 0)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("launch failed: no display", result.errors[0][1])
        self.assertIn("PIPELINE_REQUIRE_BROWSER_TESTS=1", result.errors[0][1])

    def test_the_switches_read_the_environment(self):
        with mock.patch.dict(os.environ, {browser_support.REQUIRE_ENV: "1", browser_support.HEADED_ENV: "1"}):
            self.assertTrue(browser_support.browser_tests_required())
            self.assertTrue(browser_support.headed_tests_enabled())
        with mock.patch.dict(os.environ, {browser_support.REQUIRE_ENV: "0", browser_support.HEADED_ENV: ""}):
            self.assertFalse(browser_support.browser_tests_required())
            self.assertFalse(browser_support.headed_tests_enabled())

    def test_a_missing_package_is_reported_as_a_launch_error_not_raised(self):
        browser_support.chromium_launch_error.cache_clear()
        try:
            with mock.patch.dict(sys.modules, {"playwright": None, "playwright.sync_api": None}):
                self.assertIn("ModuleNotFoundError", browser_support.chromium_launch_error())
        finally:
            browser_support.chromium_launch_error.cache_clear()


# --- in a real browser ------------------------------------------------------------------------------

class PolicyRoute:
    """What the apply agent's route handler will do (M5a): gather the request facts, ask route_decision, apply the answer.

    Only what the policy lets through reaches the fake, as with ``route_hook``. The
    handler here is the test's stand-in; the decision under test is route_decision's.
    """

    def __init__(self, fake, mode, phase, **state):
        self.fake, self.mode, self.phase = fake, mode, phase
        self.state = RouteState(submit_path=JOB_PATH, **state)
        self.refused = []
        # The handler's own record of what it aborted, by request. Observation.requests reads this, never requestfailed.
        self.aborted = []
        # A test's switch: let the submit POST through to the fake, then lose the connection before any answer.
        self.drop_submit = False

    def install(self, context):
        context.route("**/*", self)
        if hasattr(context, "route_web_socket"):
            context.route_web_socket("**/*", self.websocket)

    def facts(self, request):
        try:
            body = request.post_data_buffer or request.post_data
        except Exception:  # noqa: BLE001 - no body
            body = None
        return RouteRequest(
            method=request.method, url=request.url, resource_type=request.resource_type, headers=request.headers, body=body,
            is_navigation=request.is_navigation_request() and request.frame.parent_frame is None,
            # The real handler takes this from outreach_render.request_allowed; every host here is a fake one, served in-process.
            public=True,
        )

    def __call__(self, route):
        request = route.request
        decision = route_decision(self.mode, self.phase, self.facts(request), self.state)
        if isinstance(decision, Abort):
            self.refused.append(decision.record(request.method) | {"path": urlsplit(request.url).path})
            self.aborted.append(request)
            route.abort("blockedbyclient")
            return
        self.state.record(decision)
        if self.drop_submit and decision.submit_post:
            buffer = request.post_data_buffer
            self.fake.answer(request.method, request.url, buffer.decode("utf-8", errors="replace") if buffer else "")
            route.abort("connectionreset")      # the connection drops: requestfailed fires, though the route let the POST through
            return
        self.fake.route(route)

    def websocket(self, ws):
        decision = route_decision(self.mode, self.phase, RouteRequest("GET", ws.url, is_websocket=True, public=True), self.state)
        self.refused.append(decision.record("GET"))
        # Refused by never calling connect_to_server(): the page holds a socket that goes nowhere.
        # (ws.close() from inside the handler deadlocks Playwright's sync API, seen with 1.5x.)

    def rules(self):
        return [entry["rule"] for entry in self.refused]


class Observer:
    """Gathers what `Observation` holds from a real page, the way the agent's watch loop will.

    ``passed`` comes from the route handler's own record (``policy.aborted``), never
    from ``requestfailed``: that event also fires for a request the route let
    through whose connection then failed, and calling such a submit "refused" would
    make ``decide_outcome`` say "Nothing was sent". Pass the ``policy`` whenever the
    page sits behind a ``PolicyRoute``; with none, nothing was ever aborted.
    """

    def __init__(self, page, policy=None):
        self.page, self.policy = page, policy
        self.seen = []
        self.navigated = False
        page.on("request", self._request)
        page.on("response", self._response)
        page.on("framenavigated", self._navigated)

    def _request(self, request):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            parts = urlsplit(request.url)
            self.seen.append({"request": request, "method": request.method, "host": parts.hostname, "path": parts.path, "status": None})

    def _response(self, response):
        for entry in self.seen:
            if entry["request"] is response.request:
                entry["status"] = response.status

    def _passed(self, entry):
        return self.policy is None or not any(entry["request"] is aborted for aborted in self.policy.aborted)

    def _navigated(self, frame):
        if frame == self.page.main_frame:
            self.navigated = True

    def observation(self):
        page = self.page
        error = page.locator("#form-error")
        parts = urlsplit(page.url)
        return Observation(
            main_path=parts.path, main_query=parts.query, form_present=page.locator("form#application-form").count() > 0,
            requests=tuple(SeenRequest(e["method"], e["host"], e["path"], e["status"], self._passed(e)) for e in self.seen),
            security_code_visible=page.locator("#security-input-0").is_visible(),
            challenge_frame=page.locator("iframe[src*='bframe']").count() > 0, submit_path=JOB_PATH, confirmation_path=CONFIRMATION_PATH,
            board_token="examplerobotics", job_id=JOB_ID, navigated=self.navigated,
            first_field_error=error.inner_text() if error.is_visible() else "",
        )


RESUME = {"name": "Sam Rivera Resume.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.4 fictional resume for tests"}
# Bytes that are not valid UTF-8, as a real PDF's are.
BINARY_RESUME = {"name": "Sam Rivera Resume.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n"}


@requires_chromium
class BrowserFixtureTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright

        cls._playwright = sync_playwright().start()
        cls._browser = cls._playwright.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls._browser.close()
        cls._playwright.stop()

    def open(self, scenario="confirm", policy=None, **policy_state):
        """A fresh page on the fake. With ``policy`` = (mode, phase) the agent's request policy sits in front of it."""
        fake = FakeGreenhouse(scenario)
        context = self._browser.new_context(service_workers="block")
        self.addCleanup(context.close)
        if policy:
            router = PolicyRoute(fake, *policy, **policy_state)
            router.install(context)
        else:
            router = None
            fake.install(context)
        page = context.new_page()
        page.set_default_timeout(8_000)
        return fake, page, router

    def load(self, scenario="confirm", policy=None, **policy_state):
        fake, page, router = self.open(scenario, policy, **policy_state)
        page.goto(JOB_URL, wait_until="domcontentloaded")
        return fake, page, router


class RequiredCheckScriptBrowserTests(BrowserFixtureTestCase):
    def read(self, page):
        return page.evaluate(REQUIRED_CHECK_SCRIPT)

    def test_it_finds_every_required_field_by_every_marker_kind_and_only_those(self):
        _fake, page, _router = self.load()
        scan = self.read(page)
        self.assertTrue(scan["form"])
        markers = {entry["key"]: entry["markers"] for entry in scan["items"]}
        self.assertEqual(markers, {
            "first_name": ["attr", "asterisk"],
            "last_name": ["attr"],
            "email": ["aria"],
            "resume": ["span_required"],
            "question_4000000101": ["asterisk"],
            "question_4000000103": ["hidden_required_sibling"],
            "question_4000000105": ["hidden_required_sibling"],
            "question_4000000106": ["hidden_required_sibling"],
            "question_4000000109": ["attr"],
            "question_4000000110": ["attr"],
            "question_4000000111": ["aria"],
            "gdpr_consent_given": ["attr"],
        })
        self.assertTrue(all(entry["empty"] for entry in scan["items"]))

    def test_its_items_agree_with_the_listing_on_key_and_question(self):
        _fake, page, _router = self.load()
        labels = {name: label for name, label, *_ in schema_fields(fixture_json("schema_new.json"))}
        required = {name for name, _label, is_required, kind in schema_fields(fixture_json("schema_new.json")) if is_required and kind != "input_hidden" and not name.endswith("_text")}
        items = self.read(page)["items"]
        self.assertEqual({entry["key"] for entry in items}, required)
        for entry in items:
            with self.subTest(key=entry["key"]):
                self.assertEqual(question_key(entry["question"]), question_key(labels[entry["key"]]))

    def test_it_reports_each_kind_of_value_as_the_page_holds_it(self):
        _fake, page, _router = self.load()
        complete_form(page, resume=RESUME)
        by_key = {entry["key"]: entry for entry in self.read(page)["items"]}
        self.assertEqual(by_key["first_name"]["value_text"], "Sam")
        self.assertEqual(by_key["question_4000000103"]["value_text"], "Controls")          # the react-select's own display, not the mirror's "v2"
        self.assertEqual(by_key["question_4000000103"]["kind"], "combobox")
        self.assertEqual(by_key["resume"]["value_text"], "Sam Rivera Resume.pdf")
        self.assertEqual(by_key["question_4000000111"]["value_text"], ["No"])
        self.assertEqual(by_key["question_4000000109"]["value_text"], ["I have read the Example Robotics privacy notice"])
        self.assertEqual([key for key, entry in by_key.items() if entry["empty"]], [])

    def test_it_reads_a_multi_select_and_a_native_select_and_changes_nothing(self):
        _fake, page, _router = self.load()
        choose(page, "question_4000000104", "Python")
        choose(page, "question_4000000104", "Rust")
        before = page.content()
        controls = {c["key"]: c for c in self.read(page)["controls"] if not c["mirror"]}
        self.assertEqual(controls["question_4000000104"]["value_text"], ["Python", "Rust"])
        self.assertEqual(controls["question_4000000108"]["value_text"], "Summer 2027")
        self.assertTrue(controls["question_4000000113"]["checked"])
        self.assertEqual(page.content(), before)

    def test_a_typed_but_unchosen_combobox_holds_no_value(self):
        _fake, page, _router = self.load()
        page.fill("#question_4000000103", "Contr")
        items = {entry["key"]: entry for entry in self.read(page)["items"]}
        self.assertTrue(items["question_4000000103"]["empty"])

    def test_it_reports_what_the_form_flags_as_invalid(self):
        _fake, page, _router = self.load()
        press_submit(page)     # the fixture's own validation shows its error and sends nothing
        invalid = self.read(page)["invalid"]
        self.assertIn("Please complete the required fields.", [entry["reason"] for entry in invalid])
        self.assertTrue(any(entry["key"] == "first_name" for entry in invalid))   # native :invalid on an empty required input

    def test_it_reads_the_older_form_too(self):
        fake, page, _router = self.open()
        page.goto(apply_fake_ats.LEGACY_JOB_URL, wait_until="domcontentloaded")
        scan = self.read(page)
        self.assertTrue(scan["legacy"])
        self.assertEqual({entry["key"] for entry in scan["items"]}, {"job_application[first_name]", "job_application[last_name]", "job_application[email]", "job_application[resume]"})

    def test_a_native_invalid_reason_is_fixed_wording_never_the_browsers_message_that_quotes_the_typed_text(self):
        _fake, page, _router = self.load()
        typed = "sam.rivera.example.test"
        page.fill("#email", typed)
        # Chromium's own message for this control quotes what was typed.
        self.assertIn(typed, page.evaluate("() => document.getElementById('email').validationMessage"))
        invalid = [entry for entry in self.read(page)["invalid"] if entry["key"] == "email"]
        self.assertEqual([entry["reason"] for entry in invalid], ["not the kind of value the field expects"])
        for entry in self.read(page)["invalid"]:
            self.assertNotIn(typed, entry["reason"])

    def test_a_page_without_the_form_reports_none(self):
        _fake, page, _router = self.open("closed")
        page.goto(JOB_URL, wait_until="domcontentloaded")
        self.assertEqual(self.read(page), {"form": False, "legacy": False, "items": [], "controls": [], "invalid": []})


def filled_plan():
    fields = [
        planned("first_name", "First Name", "Sam"),
        planned("last_name", "Last Name", "Rivera"),
        planned("email", "Email", EMAIL),
        planned("resume", "Resume/CV", "", control="file", source="resume", file_name="Sam Rivera Resume.pdf"),
        planned("question_4000000101", "Why?", "I build small robot arms and would like to learn from the team."),
        planned("question_4000000103", "Team", "Controls", control="react_select"),
        planned("question_4000000105", "Work authorization", "Yes", control="react_select", source="sensitive"),
        planned("question_4000000106", "Sponsorship", "No", control="react_select", source="sensitive"),
        planned("question_4000000109", "Privacy", True, control="checkbox", source="sensitive"),
        planned("question_4000000110", "Accurate", True, control="checkbox", source="sensitive"),
        planned("question_4000000111", "Worked here", "No", control="radio"),
        planned("gdpr_consent_given", "Data", True, control="checkbox", source="sensitive"),
    ]
    return FakePlan(fields)


class CheckRequiredBrowserTests(BrowserFixtureTestCase):
    """check_required against what the script reads from a real page."""

    def setUp(self):
        self.schema = [{"name": name, "label": label, "required": required, "type": kind} for name, label, required, kind in schema_fields(fixture_json("schema_new.json"))]

    def snapshot(self, page):
        scan = page.evaluate(REQUIRED_CHECK_SCRIPT)
        initial = {}
        for entry in scan["controls"]:
            if entry["mirror"]:
                continue
            if entry["kind"] in ("checkbox", "radio"):
                if entry["checked"]:
                    initial.setdefault(entry["key"], []).append(entry["value_text"])
            elif entry["value_text"]:
                initial[entry["key"]] = entry["value_text"]
        return initial

    def problems(self, page, initial, plan=None):
        scan = page.evaluate(REQUIRED_CHECK_SCRIPT)
        return check_required(scan["items"], plan or filled_plan(), self.schema, initial, controls=scan["controls"], invalid=scan["invalid"])

    def ready(self):
        _fake, page, _router = self.load()
        initial = self.snapshot(page)
        complete_form(page, resume=RESUME)
        return page, initial

    def test_the_page_defaults_are_seen_as_the_pages_own(self):
        _fake, page, _router = self.load()
        initial = self.snapshot(page)
        self.assertEqual(initial["question_4000000108"], "Summer 2027")
        self.assertEqual(initial["question_4000000113"], ["Keep me informed about future openings at Example Robotics"])
        hidden = {c["key"]: c for c in page.evaluate(REQUIRED_CHECK_SCRIPT)["controls"] if c["mirror"] and c["value_text"]}
        self.assertEqual(hidden["mapped_url_token"]["value_text"], "fixture-token-0001")   # Greenhouse's own hidden input

    def test_a_completed_form_that_matches_its_plan_passes(self):
        page, initial = self.ready()
        self.assertEqual(self.problems(page, initial), [])

    def test_a_field_changed_since_the_plan_is_caught(self):
        page, initial = self.ready()
        page.fill("#first_name", "Samuel")
        self.assertEqual([(p.kind, p.key) for p in self.problems(page, initial)], [("value_mismatch", "first_name")])

    def test_a_required_field_left_empty_is_caught_including_asterisk_only_and_mirror_only_fields(self):
        page, initial = self.ready()
        page.fill("#question_4000000101", "")
        page.evaluate("() => { const mirror = document.querySelector('input[name=question_4000000103]'); mirror.value = ''; document.querySelector('#question_4000000103').closest('.rs').querySelector('.select__values').innerHTML = ''; }")
        found = {(p.kind, p.key) for p in self.problems(page, initial)}
        self.assertIn(("empty", "question_4000000101"), found)
        self.assertIn(("empty", "question_4000000103"), found)

    def test_a_value_a_page_script_put_in_an_optional_field_is_caught(self):
        page, initial = self.ready()
        page.evaluate("() => { document.getElementById('question_4000000102').value = 'https://example.test/sneaky'; }")
        problems = self.problems(page, initial)
        self.assertEqual([(p.kind, p.key, p.required) for p in problems], [("unplanned_value", "question_4000000102", False)])

    def test_a_value_in_the_hidden_spam_trap_is_caught(self):
        page, initial = self.ready()
        page.evaluate("() => { document.querySelector('[name=website_url]').value = 'bot'; }")
        self.assertEqual([(p.kind, p.key) for p in self.problems(page, initial)], [("unplanned_value", "website_url")])

    def test_a_page_set_default_that_a_script_changed_is_caught_but_the_default_itself_is_not(self):
        page, initial = self.ready()
        self.assertEqual(self.problems(page, initial), [])
        page.evaluate("() => { document.getElementById('question_4000000108').value = '2'; }")
        self.assertEqual([(p.kind, p.key) for p in self.problems(page, initial)], [("unplanned_value", "question_4000000108")])

    def test_a_sensitive_answer_typed_into_the_page_in_a_rehearsal_is_caught(self):
        page, initial = self.ready()
        plan = filled_plan()
        plan.fields[6]["disposition"] = "deferred"       # work authorization: known, checked, never put in the page
        self.assertEqual([(p.kind, p.key) for p in self.problems(page, initial, plan)], [("unplanned_value", "question_4000000105")])

    def test_a_field_the_form_flags_is_caught(self):
        page, initial = self.ready()
        page.evaluate("() => { document.getElementById('email').setAttribute('aria-invalid', 'true'); }")
        self.assertEqual([p.kind for p in self.problems(page, initial)], ["invalid"])

    def test_a_deferred_required_field_left_empty_does_not_fail_the_form(self):
        _fake, page, _router = self.load()
        initial = self.snapshot(page)
        complete_form(page, resume=RESUME)
        page.evaluate("() => { const box = document.querySelector('#question_4000000105').closest('.rs'); box.querySelector('.select__values').innerHTML = ''; box.querySelector('input[aria-hidden]').value = ''; }")
        plan = filled_plan()
        plan.fields[6]["disposition"] = "deferred"
        self.assertEqual(self.problems(page, initial, plan), [])


class OutcomeBrowserTests(BrowserFixtureTestCase):
    """What a real browser reports after Submit, read into an Observation and decided."""

    def submit(self, scenario, *, complete=True, wait_ms=900):
        fake, page, _router = self.load(scenario)
        observer = Observer(page)
        if complete:
            complete_form(page, resume=RESUME)
        press_submit(page)
        page.wait_for_timeout(wait_ms)
        return fake, page, observer

    def test_a_303_and_the_confirmation_page_is_submitted(self):
        fake, page, observer = self.submit("confirm")
        outcome = decide_outcome(observer.observation())
        self.assertEqual((outcome.outcome, outcome.after_click, outcome.resolved_by), ("submitted", 1, "page"))
        self.assertEqual(len(fake.submit_posts()), 1)
        self.assertEqual(fake.forbidden_clicks(page) if page.locator("form").count() else 0, 0)

    def test_a_server_error_is_unconfirmed(self):
        _fake, _page, observer = self.submit("server_500")
        self.assertEqual(decide_outcome(observer.observation()).outcome, "unconfirmed")

    def test_a_refusal_with_the_form_still_there_is_failed_after_the_click_with_the_pages_error(self):
        _fake, _page, observer = self.submit("validation_422")
        outcome = decide_outcome(observer.observation())
        self.assertEqual((outcome.outcome, outcome.after_click), ("failed", 1))
        self.assertIn("HTTP 422", outcome.note)
        self.assertIn("Greenhouse could not accept the application (422).", outcome.note)

    def test_a_thank_you_at_the_jobs_own_address_with_the_form_still_there_is_unconfirmed(self):
        _fake, page, observer = self.submit("text_only_thanks")
        self.assertIn("Thank you for applying", page.inner_text("body"))
        self.assertEqual(decide_outcome(observer.observation()).outcome, "unconfirmed")

    def test_a_confirmation_page_reached_without_a_post_is_unconfirmed(self):
        fake, _page, observer = self.submit("confirmation_without_post")
        self.assertEqual(fake.submit_posts(), [])
        self.assertEqual(decide_outcome(observer.observation()).outcome, "unconfirmed")

    def test_a_post_that_never_answers_is_unconfirmed(self):
        _fake, page, observer = self.submit("hang", wait_ms=600)
        outcome = decide_outcome(observer.observation())
        self.assertEqual(outcome.outcome, "unconfirmed")
        self.assertIsNone(observer.observation().requests[0].status)
        page.context.unroute_all(behavior="ignoreErrors")     # the pending POST is left behind on purpose

    def test_a_form_that_stopped_itself_sent_nothing(self):
        fake, _page, observer = self.submit("confirm", complete=False, wait_ms=300)
        outcome = decide_outcome(observer.observation())
        self.assertEqual((outcome.outcome, outcome.after_click), ("failed", 0))
        self.assertIn("Please complete the required fields.", outcome.note)
        self.assertEqual(fake.non_get_requests(), [])

    def test_the_security_code_waits_then_the_students_code_confirms(self):
        fake, page, observer = self.submit("security_code")
        waiting = decide_outcome(observer.observation())
        self.assertEqual((waiting.outcome, waiting.detail), ("waiting", {"waiting": "security_code"}))
        type_security_code(page)
        press_submit(page)
        page.wait_for_timeout(900)
        outcome = decide_outcome(observer.observation(), code_wait_over=True)
        self.assertEqual((outcome.outcome, outcome.detail), ("submitted", {"security_code": True}))
        self.assertEqual(len(fake.submit_posts()), 2)

    def test_the_security_code_still_confirms_when_the_resume_is_a_real_binary_pdf(self):
        # Playwright's post_data raises on bytes that are not UTF-8; the fake used to read that body as empty, so the code was never seen.
        fake, page, _router = self.load("security_code")
        observer = Observer(page)
        complete_form(page, resume=BINARY_RESUME)
        press_submit(page)
        page.wait_for_timeout(900)
        self.assertEqual(decide_outcome(observer.observation()).outcome, "waiting")
        type_security_code(page)
        press_submit(page)
        page.wait_for_timeout(900)
        outcome = decide_outcome(observer.observation(), code_wait_over=True)
        self.assertEqual((outcome.outcome, outcome.detail), ("submitted", {"security_code": True}))
        self.assertEqual(len(fake.submit_posts()), 2)
        self.assertIn("%PDF-1.7", fake.submit_posts()[0].post_data)

    def test_the_closed_posting_has_no_form(self):
        _fake, page, _router = self.load("closed")
        self.assertEqual(page.locator("form#application-form").count(), 0)
        self.assertIn("no longer open", page.inner_text("body"))


class RoutePolicyBrowserTests(BrowserFixtureTestCase):
    """route_decision against a real browser: what each hostile page script tries, and what reaches the fake."""

    VALUES = {"first_name": "Samantha", "email": EMAIL, "location_city": "Springfield, Example State, United States"}

    def typing(self, scenario, mode="rehearse"):
        fake, page, router = self.load(scenario, (mode, PHASE_BEFORE_INPUT), values=self.VALUES, lookup_endpoints=(LOOKUP,))
        router.phase = PHASE_AFTER_INPUT
        return fake, page, router

    def test_a_rehearsal_refuses_a_page_script_that_posts_on_every_keystroke(self):
        fake, page, router = self.typing("eager_script")
        page.fill("#first_name", "Samantha")
        page.wait_for_timeout(300)
        self.assertEqual(fake.non_get_requests(), [])
        self.assertEqual(router.rules(), ["value_guard"])
        self.assertEqual(router.refused[0]["field_key"], "first_name")

    def test_a_rehearsal_refuses_a_get_beacon_carrying_a_value_on_any_host(self):
        for scenario, beacon in (("eager_get", "/p.gif"), ("eager_get_greenhouse", "/pixel.gif")):
            with self.subTest(scenario=scenario):
                fake, page, router = self.typing(scenario)
                page.fill("#first_name", "Samantha")
                page.wait_for_timeout(300)
                self.assertEqual([seen for seen in fake.requests if seen.path == beacon], [])
                self.assertIn("value_guard", router.rules())

    def test_a_beacon_the_page_re_encodes_is_still_refused(self):
        # An unencoded "+" (Chromium encodes the spaces and leaves the "+"), and a JSON-wrapped multi-line answer.
        phone, essay = "+1 512 555 0100", "I build small robot arms.\nAnd I would like to learn from the team."
        fake, page, router = self.load("eager_get_greenhouse", ("rehearse", PHASE_BEFORE_INPUT), values={"phone": phone, "essay": essay})
        router.phase = PHASE_AFTER_INPUT
        page.fill("#phone", phone)
        page.fill("#question_4000000101", essay)
        page.wait_for_timeout(400)
        self.assertEqual([seen for seen in fake.requests if seen.path == "/pixel.gif"], [])
        self.assertEqual(sorted({(entry["rule"], entry.get("field_key")) for entry in router.refused if entry["path"] == "/pixel.gif"}),
                         [("value_guard", "essay"), ("value_guard", "phone")])
        # Three beacons per input, all refused: the encoded one, the unencoded one and the JSON one.
        self.assertEqual(len([entry for entry in router.refused if entry["path"] == "/pixel.gif"]), 6)

    def test_a_lookup_carrying_another_fields_value_is_refused_and_a_clean_one_is_served(self):
        fake, page, router = self.typing("lookup_leak")
        page.fill("#email", EMAIL)
        router.state.typing_key = "location_city"
        router.state.typing_lookup = "location"
        page.fill("#location_city", "Spring")
        page.wait_for_timeout(300)
        self.assertEqual(fake.requests_to(API_HOST), [])
        self.assertEqual([(entry["rule"], entry["field_key"]) for entry in router.refused if entry["host"] == API_HOST], [("value_guard", "email")])
        self.assertEqual(page.locator(".select__option").count(), 0)
        # The same typing on a clean page reaches the lookup and gets its options.
        fake, page, router = self.typing("confirm")
        router.state.typing_key = "location_city"
        router.state.typing_lookup = "location"
        page.fill("#location_city", "Spring")
        page.wait_for_selector(".select__option")
        self.assertEqual(page.locator(".select__option").count(), 2)
        self.assertEqual([seen.path for seen in fake.requests_to(API_HOST) if seen.path != f"/v1/boards/examplerobotics/jobs/{JOB_ID}"], [apply_fake_ats.LOOKUP_PATH])

    def stray(self, *, typing_key, typing_lookup):
        """The page sends a non-lookup GET and two lookups on every input; the agent types a field of its own."""
        fake, page, router = self.load(
            "stray_get", ("rehearse", PHASE_BEFORE_INPUT), values={"first_name": "Samantha"},
            lookup_endpoints=(LOOKUP_LOCATION, LOOKUP_SCHOOL), typing_key=typing_key, typing_lookup=typing_lookup,
        )
        router.phase = PHASE_AFTER_INPUT
        first_input = len(fake.requests)
        page.fill("#last_name", "Rivera")
        page.wait_for_timeout(400)
        return fake, router, first_input

    def test_after_the_first_input_only_the_typed_fields_own_lookup_reaches_greenhouse(self):
        fake, router, first_input = self.stray(typing_key="location_city", typing_lookup="location")
        self.assertEqual([(seen.method, seen.host, seen.path) for seen in fake.requests[first_input:]], [("GET", API_HOST, "/fake-lookup/location")])
        # The non-lookup GET and the other field's lookup were refused by the after-first-input rule, not by luck.
        self.assertEqual(sorted((entry["host"], entry["path"], entry["rule"]) for entry in router.refused),
                         [(API_HOST, "/fake-lookup/school", "after_first_input"), (JOB_HOST, "/track", "after_first_input")])

    def test_after_the_first_input_a_field_with_no_typeahead_reaches_no_lookup(self):
        fake, router, first_input = self.stray(typing_key="last_name", typing_lookup="")
        self.assertEqual(fake.requests[first_input:], [])
        self.assertEqual(sorted((entry["path"], entry["rule"]) for entry in router.refused),
                         [("/fake-lookup/location", "after_first_input"), ("/fake-lookup/school", "after_first_input"), ("/track", "after_first_input")])

    def test_typing_a_field_with_a_typeahead_reaches_only_that_endpoint_kind(self):
        fake, router, first_input = self.stray(typing_key="school", typing_lookup="school")
        self.assertEqual([seen.path for seen in fake.requests[first_input:]], ["/fake-lookup/school"])
        self.assertEqual(sorted(entry["path"] for entry in router.refused), ["/fake-lookup/location", "/track"])

    def test_a_captcha_endpoint_never_receives_a_field_value(self):
        fake, page, router = self.load("captcha_body_leak", ("submit", PHASE_FILL), values=self.VALUES)
        page.fill("#first_name", "Samantha")
        page.wait_for_timeout(300)
        self.assertEqual(fake.non_get_requests(), [])
        self.assertEqual(router.rules(), ["value_guard"])

    def test_a_websocket_is_refused(self):
        fake, page, router = self.load("websocket", ("rehearse", PHASE_BEFORE_INPUT))
        page.wait_for_timeout(500)
        self.assertEqual(fake.websockets, [])
        self.assertIn("websocket", router.rules())

    def test_the_fake_installed_on_a_whole_context_refuses_a_websocket_without_hanging(self):
        # install() used to call ws.close() inside the handler, which deadlocks Playwright's sync API.
        import faulthandler

        faulthandler.dump_traceback_later(60, exit=True)
        try:
            fake, page, _router = self.open("websocket")
            page.goto(JOB_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(500)
        finally:
            faulthandler.cancel_dump_traceback_later()
        self.assertEqual(fake.websockets, ["wss://socket.example-robotics.test/live"])
        self.assertEqual(page.locator("form#application-form").count(), 1)

    def test_an_upload_to_an_s3_host_is_refused_in_a_rehearsal(self):
        fake, page, router = self.typing("s3_upload")
        page.set_input_files("#resume", RESUME)
        page.wait_for_timeout(300)
        self.assertEqual(fake.non_get_requests(), [])
        self.assertEqual(router.rules(), ["s3_upload"])

    def test_a_posting_that_sends_applicants_to_another_site_never_reaches_it(self):
        fake, page, router = self.open("redirect_offsite", ("rehearse", PHASE_BEFORE_INPUT))
        page.goto(JOB_URL, wait_until="domcontentloaded")      # the page then tries to send the applicant away
        page.wait_for_timeout(500)
        self.assertNotIn(OFFSITE_HOST, page.url)      # the navigation was aborted (Chromium shows its error page)
        self.assertEqual(fake.requests_to(OFFSITE_HOST), [])
        self.assertEqual([(entry["rule"], entry["host"]) for entry in router.refused], [("offsite_navigation", OFFSITE_HOST)])

    def test_a_request_submit_from_a_page_script_while_the_agent_fills_reaches_nothing(self):
        for mode in ("submit", "handoff"):
            with self.subTest(mode=mode):
                fake, page, router = self.load("request_submit_during_fill", (mode, PHASE_FILL))
                page.fill("#first_name", "Sam")
                page.wait_for_timeout(500)
                self.assertFalse(fake.submit_path_hit)
                self.assertEqual(fake.non_get_requests(), [])
                self.assertEqual(router.rules(), ["before_hand_over"])

    def test_after_hand_over_one_submit_passes_and_a_page_script_cannot_send_a_second(self):
        fake, page, router = self.load("double_submit", ("submit", PHASE_FILL))
        complete_form(page, resume=RESUME)
        router.phase = PHASE_AFTER_HAND_OVER
        observer = Observer(page, router)
        press_submit(page)
        page.wait_for_timeout(900)
        self.assertEqual(len(fake.submit_posts()), 1)
        self.assertEqual(router.rules(), ["second_submit_post"])
        outcome = decide_outcome(observer.observation())
        self.assertEqual(outcome.outcome, "submitted")

    def test_after_hand_over_a_post_to_another_path_is_refused_and_says_nothing_was_sent(self):
        fake, page, router = self.load("other_path_post", ("submit", PHASE_FILL))
        complete_form(page, resume=RESUME)
        router.phase = PHASE_AFTER_HAND_OVER
        observer = Observer(page, router)
        press_submit(page)
        page.wait_for_timeout(900)
        self.assertEqual(fake.submit_posts(), [])
        self.assertEqual(fake.non_get_requests(), [])
        self.assertEqual(router.rules(), ["other_non_get"])
        outcome = decide_outcome(observer.observation())
        self.assertEqual((outcome.outcome, outcome.after_click), ("failed", 0))
        self.assertIn("address the app doesn't recognize", outcome.note)

    def test_a_submit_the_route_let_through_whose_connection_then_dropped_is_unconfirmed_not_nothing_sent(self):
        fake, page, router = self.load("confirm", ("submit", PHASE_FILL))
        complete_form(page, resume=RESUME)
        router.phase = PHASE_AFTER_HAND_OVER
        router.drop_submit = True
        observer = Observer(page, router)
        press_submit(page)
        page.wait_for_timeout(900)
        # The submit POST reached Greenhouse (the fake) and the route let it through; only the answer was lost.
        self.assertEqual(len(fake.submit_posts()), 1)
        self.assertEqual(router.state.submit_posts_passed, 1)
        self.assertEqual(router.refused, [])
        seen = observer.observation().requests
        self.assertEqual([(entry.method, entry.host, entry.path, entry.status, entry.passed) for entry in seen],
                         [("POST", SUBMIT_HOST, JOB_PATH, None, True)])
        outcome = decide_outcome(observer.observation())
        self.assertEqual((outcome.outcome, outcome.after_click), ("unconfirmed", 1))
        self.assertNotIn("Nothing was sent", outcome.note)

    def test_the_agent_never_presses_the_buttons_it_must_avoid_and_the_page_counts_them(self):
        fake, page, _router = self.load("confirm", ("rehearse", PHASE_BEFORE_INPUT))
        self.assertEqual(fake.forbidden_clicks(page), 0)
        page.click("#autofill-mygreenhouse")      # the test itself pressing one proves the counter counts
        page.click("#locate-me")
        self.assertEqual(fake.forbidden_clicks(page), 2)


if __name__ == "__main__":
    unittest.main()
