"""The ATS seam (apply/ats.py), part 1: Greenhouse is the only ATS registered, and nothing it does has changed.

docs/phase5-lever-handoff-spec.md 5.2 and milestone LV1a. Each decision the seam moved behind the registry (``identify``,
``canonical_url``, ``parse_schema``, the confirmation-sender check) is run old against new: the old code is a frozen copy
(tests/frozen_pre_ats_seam.py, taken from the commit before the seam) and the new is reached through the registry, on every
Greenhouse fixture and vector. What the seam added (the registry's own values, the adapter Protocol, ``AgentJob.ats``,
the factory's ``ats`` argument) is pinned directly. No browser and no network.
"""

import copy
import dataclasses
import fnmatch
import inspect
import itertools
import json
import random
import re
import subprocess
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import realdata_guard

realdata_guard.install()

import frozen_pre_ats_seam as old
import frozen_pre_route_policy as old_route
import frozen_pre_sentences as old_words
import helpers_source
import test_apply_runner as runner_tests
from opportunity_app.apply import (
    agent as apply_agent, agent_types, ats as apply_ats, checks as apply_checks, claims as apply_claims, greenhouse as apply_greenhouse, policy as apply_policy,
    preflight as apply_preflight, runner as apply_runner, runs as apply_runs,
)
from opportunity_app.apply.agent_types import AgentJob, ApplyTimeouts, RunResult
from opportunity_app.apply.policy import SchemaField
from opportunity_app.apply.runs import ClaimRefused
from opportunity_app.apply.schema_client import GreenhouseSchemaClient
from opportunity_app.core.timestamps import utc_now
from pipeline_core.identity import employer_key

from apply_fake_ats import FakeApplyAgentFactory, fixture_json, fixture_text
from helpers_apply import USER, ApplyCase, FakePlan, setUpModule, tearDownModule  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
GREENHOUSE = apply_ats.GREENHOUSE


# --- The registry's values are exactly Greenhouse's -------------------------------------------------------------

class RegistryValuesTests(unittest.TestCase):
    def test_greenhouse_and_lever_are_registered_and_greenhouse_has_todays_values(self):
        self.assertEqual(apply_ats.REGISTRY, (GREENHOUSE, apply_ats.LEVER))
        self.assertEqual(apply_ats.keys(), ("greenhouse", "lever"))
        self.assertIs(apply_ats.spec_for("greenhouse"), GREENHOUSE)
        self.assertEqual(GREENHOUSE.key, old.ATS_GREENHOUSE)
        self.assertEqual(GREENHOUSE.display_name, "Greenhouse")
        self.assertEqual(GREENHOUSE.adapter_version, old.ADAPTER_VERSION)
        self.assertEqual(GREENHOUSE.supported_modes, ("lookup", "rehearse", "handoff"))
        self.assertEqual(GREENHOUSE.supported_modes, agent_types.BUILT_MODES)
        self.assertIs(GREENHOUSE.identify, apply_greenhouse.identify)
        self.assertIs(GREENHOUSE.canonical_url, apply_greenhouse.canonical_url)

    def test_an_unknown_ats_is_refused_by_name(self):
        for key in ("ashby", "", "Greenhouse", "greenhouse ", "Lever"):
            with self.subTest(key=key), self.assertRaises(apply_ats.UnknownAts):
                apply_ats.spec_for(key)

    def test_the_schema_client_is_the_greenhouse_job_board_client(self):
        self.assertIsInstance(GREENHOUSE.schema_client(), GreenhouseSchemaClient)

    def test_the_spec_is_frozen(self):
        with self.assertRaises(dataclasses.FrozenInstanceError):
            GREENHOUSE.key = "other"   # type: ignore[misc]

    def test_canonical_urls_equal_the_old_ones(self):
        for token, job in (("examplerobotics", "4000000001"), ("a", "1"), ("Mixed-Case_1", "99")):
            with self.subTest(token=token):
                self.assertEqual(GREENHOUSE.canonical_url(token, job), old.canonical_url(token, job))

    def test_the_confirmation_sender_check_equals_the_old_one(self):
        for domain in ("greenhouse.io", "us.greenhouse-mail.io", "GREENHOUSE.IO.", "mail.greenhouse.io", "greenhouse-mail.io", "evilgreenhouse.io",
                       "greenhouse.io.evil.example.test", "lever.co", "", None, " greenhouse.io", "my.greenhouse.io"):
            with self.subTest(domain=domain):
                self.assertEqual(GREENHOUSE.is_confirmation_sender(domain), old.is_greenhouse_sender(domain))
        self.assertTrue(GREENHOUSE.is_confirmation_sender("us.greenhouse-mail.io"))
        self.assertFalse(GREENHOUSE.is_confirmation_sender("lever.co"))


# --- identify, old against new ------------------------------------------------------------------------------------

class IdentifyParityTests(ApplyCase):
    def role(self, opportunity_id, url, sources=()):
        self.opportunity(opportunity_id)
        with self.conn:
            self.conn.execute("UPDATE opportunities SET url=? WHERE id=?", (url, opportunity_id))
            for key, external_id, source_url in sources:
                self.conn.execute(
                    "INSERT INTO opportunity_sources(opportunity_id, source_key, source_name, external_id, source_url, first_seen_at, last_seen_at) "
                    "VALUES(?, ?, 'x', ?, ?, ?, ?)", (opportunity_id, key, external_id, source_url, utc_now(), utc_now()))
        return opportunity_id

    def same(self, opportunity_id):
        expected = old.identify(self.conn, opportunity_id)
        found = apply_ats.identify(self.conn, opportunity_id)
        self.assertEqual(None if found is None else found[1], expected)
        if found is not None:
            self.assertIs(found[0], GREENHOUSE)
        self.assertEqual(GREENHOUSE.identify(self.conn, opportunity_id), expected)
        return expected

    def test_every_url_vector_gives_what_the_old_function_gave(self):
        found = []
        for index, url in enumerate((
            "https://job-boards.greenhouse.io/examplerobotics/jobs/4000000001",
            "https://boards.greenhouse.io/examplerobotics/jobs/4000000001?gh_src=abc",
            "https://boards.greenhouse.io/embed/job_app?for=examplerobotics&token=4000000001",
            "https://boards.greenhouse.io/embed/job_app/?for=examplerobotics&token=4000000001",
            "https://boards.greenhouse.io/embed/job_app?for=examplerobotics&token=notdigits",
            "https://boards.greenhouse.io/embed/job_app?for=&token=1",
            "https://JOB-BOARDS.GREENHOUSE.IO./examplerobotics/jobs/4000000001/",
            "https://job-boards.greenhouse.io/examplerobotics/jobs/4000000001#apply",
            "http://boards.greenhouse.io/examplerobotics/jobs/4000000001",
            "ftp://boards.greenhouse.io/examplerobotics/jobs/4000000001",
            "https://job-boards.greenhouse.io/examplerobotics/jobs/abc",
            "https://job-boards.greenhouse.io/examplerobotics/",
            "https://job-boards.greenhouse.io/-bad/jobs/1",
            "https://job-boards.greenhouse.io.evil.example.test/acme/jobs/4000000001",
            "https://my.greenhouse.io/acme/jobs/4000000001",
            "https://jobs.lever.co/acme/1",
            "https://careers.example.test/jobs/1?gh_jid=4000000001",
            "", "not a url", "https://[bad",
        )):
            with self.subTest(url=url):
                found.append(self.same(self.role(f"r{index}", url)))
        self.assertIn(("examplerobotics", "4000000001"), found, "the vectors include roles that are Greenhouse's")
        self.assertIn(None, found, "and roles that are not")

    def test_every_source_vector_gives_what_the_old_function_gave(self):
        company = "https://careers.example.test/jobs/"
        cases = [
            [("greenhouse:keytoken", "4000000009", "https://x.example.test")],
            [("greenhouse:acme", "4000000003", "https://boards.greenhouse.io/acmetoken/jobs/4000000003")],
            [("greenhouse:acme", "4000000004", company + "2")],
            [("greenhouse:acme", "a-1", company + "3")],
            [("greenhouse:acme", "", company + "3")],
            [("greenhouse:-bad", "1", "")],
            [("lever:acme", "1", "https://jobs.lever.co/acme/1")],
            [("ashby:acme", "1", "")],
            [("greenhouse:one", "11", ""), ("greenhouse:two", "22", "")],
            [("greenhouse:one", "11", ""), ("other:two", "22", "https://job-boards.greenhouse.io/two/jobs/22")],
        ]
        found = []
        for index, sources in enumerate(cases):
            with self.subTest(sources=sources):
                found.append(self.same(self.role(f"s{index}", company + str(index), sources)))
        self.assertIn(("acmetoken", "4000000003"), found)
        self.assertIn(("acme", "4000000004"), found)
        self.assertIn(None, found)

    def test_a_url_token_wins_over_a_source_key_and_an_unknown_role_is_none(self):
        opportunity = self.role("r1", "https://job-boards.greenhouse.io/urltoken/jobs/4000000002", [("greenhouse:keytoken", "4000000009", "")])
        self.assertEqual(self.same(opportunity), ("urltoken", "4000000002"))
        self.assertIsNone(self.same("no-such-role"))


# --- parse_schema, old against new --------------------------------------------------------------------------------

def _vectors():
    new, legacy = fixture_json("schema_new.json"), fixture_json("schema_legacy.json")
    yield "fixture: new board", new
    yield "fixture: legacy board", legacy
    for name, listing in (("new", new), ("legacy", legacy)):
        for key in list(listing):
            yield f"fixture {name} without {key}", {k: v for k, v in listing.items() if k != key}
            yield f"fixture {name} with only {key}", {key: listing[key]}
    yield "empty", {}
    yield "blocks of the wrong shape", {
        "questions": ["x", None, 3, {"label": "L", "required": True, "fields": [{"name": "resume_text", "type": "textarea"}, "bad", {"type": "input_text"}]}],
        "location_questions": "no",
        "compliance": [None, {"type": "eeoc", "questions": [None, {"label": "Gender", "fields": [
            {"name": "gender", "type": "multi_value_single_select", "values": [{"label": "A"}, "B", {"label": ""}, None]}]}]}],
        "demographic_questions": {"questions": [None, {"id": 0, "label": "Q", "answer_options": [{"label": "Yes"}, "No"]}, {"id": "", "label": "skip"}, {"label": "no id"}]},
        "data_compliance": [None, {"type": "GDPR Consent!", "requires_consent": True}, {"type": "ccpa"}, {"type": "", "requires_consent": True},
                            {"type": "retention", "requires_retention_consent": True}],
    }
    yield "labels that are not strings", {"questions": [
        {"label": 7, "required": 1, "fields": [{"name": "n1", "type": "input_text"}]},
        {"label": True, "fields": [{"name": "question_1", "type": "input_text"}]},
        {"label": "  Spaced   out\nlabel ", "required": "yes", "fields": [{"name": "school", "type": "input_text"}, {"name": "degree_1", "type": "input_text"}]},
    ]}
    yield "hidden fields never become the previous question", {"questions": [
        {"label": "Visible", "fields": [{"name": "question_1", "type": "input_text"}]},
        {"label": "Hidden only", "fields": [{"name": "question_2", "type": "input_hidden"}]},
        {"label": "Follow-up", "fields": [{"name": "question_3", "type": "input_text"}]},
    ]}
    yield "descriptions at and over the limit", {"questions": [
        {"label": "Exact", "description": "x" * 2000, "fields": [{"name": "question_1", "type": "input_text"}]},
        {"label": "Over", "description": "y" * 2001, "fields": [{"name": "question_2", "type": "input_text"}]},
    ], "demographic_questions": {"questions": [{"id": 5, "label": "D", "description": "z" * 2500, "answer_options": []}]}}
    yield "the paste-instead alternatives", {"questions": [
        {"label": "Resume", "required": True, "fields": [{"name": "resume", "type": "input_file"}, {"name": "resume_text", "type": "textarea"}]},
        {"label": "Cover", "required": True, "fields": [{"name": "cover_letter", "type": "input_file"}, {"name": "cover_letter_text", "type": "textarea"}]},
    ]}


class ParseSchemaParityTests(unittest.TestCase):
    def test_every_listing_vector_parses_as_the_old_function_parsed_it(self):
        counts = []
        for name, listing in _vectors():
            with self.subTest(vector=name):
                expected = old.parse_schema(copy.deepcopy(listing))
                found = GREENHOUSE.parse_schema(copy.deepcopy(listing))
                self.assertEqual(found, expected)
                self.assertTrue(all(isinstance(item, SchemaField) for item in found))
                counts.append(len(found))
        self.assertGreater(max(counts), 20, "the fixtures are real forms, so the comparison is not vacuous")
        self.assertIn(0, counts)

    def test_the_registry_does_not_change_the_listing_it_is_given(self):
        listing = fixture_json("schema_new.json")
        before = copy.deepcopy(listing)
        GREENHOUSE.parse_schema(listing)
        self.assertEqual(listing, before)


# --- preflight reads the registry --------------------------------------------------------------------------------

class PreflightUsesTheRegistryTests(runner_tests.RunnerCase):
    """A patch of the registry reaches the check: the decisions really moved, and were not left behind as copies."""

    def inputs(self):
        return apply_preflight.run_inputs(
            self.conn, runner_tests.USER, runner_tests.ACME, client=self.schema, resume_root=self.root / "resumes", apply_root=self.apply_root,
        )

    def test_the_ats_the_check_reports_comes_from_the_registry_spec(self):
        base = self.inputs()
        self.assertEqual(base.result["ats"], "greenhouse")
        self.assertEqual(base.result["canonical_url"], old.canonical_url(base.result["board_token"], base.result["job_id"]))
        changed = dataclasses.replace(GREENHOUSE, adapter_version="greenhouse-9", canonical_url=lambda token, job: f"https://example.test/{token}/{job}")
        with mock.patch.object(apply_ats, "REGISTRY", (changed,)):
            moved = self.inputs()
        self.assertEqual(moved.result["canonical_url"], f"https://example.test/{base.result['board_token']}/{base.result['job_id']}")
        self.assertNotEqual(moved.plan.plan_hash, base.plan.plan_hash, "the plan is fingerprinted with the registry's adapter version and url")

    def test_the_fields_come_from_the_specs_parse_schema(self):
        calls = []
        spy = dataclasses.replace(GREENHOUSE, parse_schema=lambda listing: calls.append(1) or GREENHOUSE.parse_schema(listing))
        with mock.patch.object(apply_ats, "REGISTRY", (spy,)):
            self.inputs()
        self.assertEqual(calls, [1])

    def test_a_role_no_registered_ats_recognises_is_unavailable(self):
        with mock.patch.object(apply_ats, "REGISTRY", ()):
            inputs = self.inputs()
        self.assertEqual(inputs.result["status"], "unavailable")
        self.assertIsNone(inputs.plan)


# --- The adapter Protocol and the agent ---------------------------------------------------------------------------

OPTIONAL = {"fill_react_select", "react_values"}


def protocol_methods():
    return {name for name, value in vars(apply_ats.AtsAdapter).items() if callable(value) and not name.startswith("_")}


class AdapterProtocolTests(unittest.TestCase):
    def test_the_protocol_lists_the_methods_the_agent_calls_and_no_submit(self):
        self.assertEqual(protocol_methods(), {
            "form_frame", "detect_page", "loader_paths", "uploads_on_attach", "reads_on_attach", "posting_ids", "lookup_token", "confirmation_ids",
            "security_code_prompt", "security_code_inputs", "captcha_widget",
            "control", "control_kind", "is_react_select", "field_container", "choices", "fill_location", "read_options",
            "scan", "page_facts", "page_managed", "owns", "is_typeahead", "parse_state", "guessed_fields", "parser_values", "cleared", "refuses",
        })

    def test_the_agent_calls_nothing_on_its_adapter_that_the_protocol_does_not_name(self):
        called = set()
        for text in helpers_source.apply_modules().values():
            called |= set(re.findall(r"\badapter\.(\w+)\(", text))
        self.assertTrue(called, "the scan found the agent's calls")
        self.assertEqual(called - protocol_methods() - OPTIONAL, set())
        self.assertNotIn("submit_control", called, "nothing presses Submit through the adapter, which is why the Protocol drops it")
        self.assertEqual(protocol_methods() - called, set(), "no method is in the Protocol that the agent never calls")

    def test_the_greenhouse_adapter_has_every_method_with_the_same_parameters(self):
        def names(signature):
            return [name for name in signature.parameters if name != "self"]

        for name in sorted(protocol_methods()):
            with self.subTest(method=name):
                self.assertEqual(names(inspect.signature(getattr(apply_agent.GreenhouseAdapter, name))), names(inspect.signature(getattr(apply_ats.AtsAdapter, name))))
        for name in sorted(OPTIONAL):
            self.assertTrue(callable(getattr(apply_agent.GreenhouseAdapter, name)))

    def test_the_agent_is_typed_to_the_protocol_not_to_greenhouse(self):
        self.assertEqual(inspect.signature(apply_agent.ApplyAgent.__init__).parameters["adapter"].annotation, "AtsAdapter")


# --- The factory and the job ---------------------------------------------------------------------------------------

def build(factory, **more):
    return factory(mode="rehearse", run_id="run-1", screenshot_dir=None, timeouts=ApplyTimeouts(), on_progress=lambda *_: None, heartbeat=lambda: None, **more)


class FactoryTests(unittest.TestCase):
    def test_every_registered_ats_whose_driver_is_built_has_an_adapter_and_the_reverse(self):
        self.assertEqual(set(apply_agent.ADAPTERS), {spec.key for spec in apply_ats.REGISTRY if spec.adapter_built})
        self.assertFalse(apply_ats.LEVER.adapter_built, "Lever is read-only until its driver lands (LV3)")

    def test_greenhouse_gets_the_greenhouse_adapter_with_or_without_the_argument(self):
        factory = apply_agent.DefaultApplyAgentFactory()
        for agent in (build(factory), build(factory, ats="greenhouse")):
            self.assertIs(type(agent.adapter), apply_agent.GreenhouseAdapter)

    def test_an_ats_that_is_not_registered_builds_no_agent(self):
        with self.assertRaises(apply_ats.UnknownAts):
            build(apply_agent.DefaultApplyAgentFactory(), ats="ashby")

    def test_an_ats_that_is_registered_but_has_no_driver_builds_no_agent_and_says_so(self):
        with self.assertRaisesRegex(RuntimeError, "no driver for Lever"):
            build(apply_agent.DefaultApplyAgentFactory(), ats="lever")

    def test_the_adapter_is_chosen_through_the_registry(self):
        gone = dataclasses.replace(GREENHOUSE, key="elsewhere")
        with mock.patch.object(apply_ats, "REGISTRY", (gone,)), self.assertRaises(apply_ats.UnknownAts):
            build(apply_agent.DefaultApplyAgentFactory(), ats="greenhouse")

    def test_a_patch_on_the_adapter_class_still_reaches_the_agent_the_factory_builds(self):
        agent = build(apply_agent.DefaultApplyAgentFactory(), ats="greenhouse")
        with mock.patch.object(apply_agent.GreenhouseAdapter, "detect_page", return_value="patched"):
            self.assertEqual(agent.adapter.detect_page(object()), "patched")

    def test_the_ats_picks_the_adapter_and_is_not_an_agent_argument(self):
        self.assertNotIn("ats", inspect.signature(apply_agent.ApplyAgent.__init__).parameters)


class AgentJobTests(unittest.TestCase):
    def test_a_job_built_without_an_ats_is_a_greenhouse_job(self):
        self.assertEqual(runner_tests.job().ats, apply_greenhouse.ATS_GREENHOUSE)
        self.assertEqual(AgentJob.__dataclass_fields__["ats"].default, apply_greenhouse.ATS_GREENHOUSE)

    def test_the_job_carries_the_ats_it_is_given(self):
        self.assertEqual(runner_tests.job(ats="other").ats, "other")


class Recording(FakeApplyAgentFactory):
    def __init__(self):
        super().__init__(step_delay=0)
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(dict(kwargs))
        return super().__call__(**kwargs)


class RunnerPassesTheAtsTests(runner_tests.RunnerCase):
    def test_a_rehearsal_builds_its_agent_for_the_preflight_ats(self):
        factory = Recording()
        self.assertEqual(self.finish(self.start(factory))["ats"], "greenhouse")
        self.assertEqual([call["ats"] for call in factory.calls], ["greenhouse"])

    def test_the_ats_comes_from_the_preflight_and_not_from_a_constant(self):
        factory = Recording()
        renamed = dataclasses.replace(GREENHOUSE, key="greenhouse-renamed")
        with mock.patch.object(apply_ats, "REGISTRY", (renamed,)):
            row = self.finish(self.start(factory))
        self.assertEqual(row["ats"], "greenhouse-renamed")
        self.assertEqual([call["ats"] for call in factory.calls], ["greenhouse-renamed"])


# --- The request policy: route_decision, decide_outcome and confirmation_reached, old against new -------------------------

POLICY = GREENHOUSE.route_policy
TOKEN, JOB = "examplerobotics", "4000000001"
SUBMIT_PATH = f"/{TOKEN}/jobs/{JOB}"
CONFIRMATION_PATH = f"{SUBMIT_PATH}/confirmation"
EMAIL = "sam.rivera@example.test"
VALUES = {"email": EMAIL, "first_name": "Samantha", "work_auth": "Yes", "city": "Springfield, Example State"}
TEST_LOOKUP = apply_checks.Endpoint("boards-api.greenhouse.io", "/fake-lookup/", "location")
BOUND_LOOKUPS = apply_agent.bind_endpoints(apply_checks.GREENHOUSE_LOOKUP_ENDPOINTS, TOKEN)
INF = float("inf")

# Every host the rules name or could be confused with, taken from the pinned endpoints fixture and the old constants.
ENDPOINTS = fixture_json("endpoints.json")
HOSTS = sorted({
    *old_route.BOARD_HOSTS, old_route.SUBMIT_HOST, *old_route.FORM_POST_HOSTS, *old_route.TELEMETRY_HOSTS,
    *(entry["host"] for entry in ENDPOINTS["lookup_endpoints"]), *ENDPOINTS["static_asset_hosts"], *(entry["host"] for entry in ENDPOINTS["captcha_endpoints"]),
    "s1-recruiting.cdn.greenhouse.io", "s123-recruiting.cdn.greenhouse.io", "s1234-recruiting.cdn.greenhouse.io", "xs1-recruiting.cdn.greenhouse.io",
    "my.greenhouse.io", "greenhouse.io", "www.greenhouse.io", "uploads.s3.amazonaws.com", "amazonaws.com", "careers.example-robotics.test",
    "jobs.lever.co", "jobs.eu.lever.co", "example-robotics.test", "xn--rene-dpa.collector.example",
})
PATHS = [
    SUBMIT_PATH, SUBMIT_PATH + "/", "/", "/v1/autocomplete", f"/v1/boards/{TOKEN}/education/schools", "/v1/boards/{token}/education/schools",
    "/recaptcha/enterprise/anchor", "/recaptcha/api2/bframe", "/embed/job_app/confirmation", f"/x?e={EMAIL.replace('@', '%40')}", "/hcaptcha/1/api.js",
]
METHODS = ("GET", "HEAD", "OPTIONS", "POST", "PUT", "DELETE")
BODIES = (None, b"", f"email={EMAIL}".encode(), b'--b\r\nContent-Disposition: form-data; name="resume"; filename="cv.pdf"\r\n\r\n%PDF\r\n--b--',
          b'--b\r\nContent-Disposition: form-data; name="x"\r\n\r\n\r\n--b--')
MODES = ("lookup", "rehearse", "submit", "handoff", "unattended", "")
PHASES = ("before_input", "after_input", "before_hand_over", "student", "after_hand_over", "other")
VALID_PAIRS = tuple((mode, phase) for mode, phases in apply_checks.PHASES.items() for phase in phases)

# (the state's fields, whether the state has the endpoints written out). The old rules had the endpoint lists as defaults of the state;
# the new ones leave them to the policy, so each state is built three ways (see ``states``).
STATE_FIELDS = (
    {},
    {"submit_posts_passed": 1},
    {"submit_path": ""},
    {"typing_key": "city", "typing_lookup": "location"},
    {"typing_key": "city", "typing_lookup": "school"},
    {"typing_key": "school", "typing_lookup": "school", "submit_posts_passed": 1},
    {"submit_posts_passed": 1, "security_code_prompts": 1, "code_press_required": True},
    {"submit_posts_passed": 1, "security_code_prompts": 1, "code_press_required": True, "code_pressed": True},
    {"submit_posts_passed": 1, "security_code_prompts": 2, "code_posts_passed": 1},
    {"code_typing_until": INF},
    {"submit_posts_passed": 1, "code_typing_until": INF, "security_code_prompts": 1},
    {"last_press_at": 1.0},
)


def states(fields, lookups):
    """(the state the old rules read, the state the new rules read with the policy's lists, the same with the lists written out)."""
    base = {"submit_path": SUBMIT_PATH, "values": dict(VALUES), **fields}
    old_lists = {"lookup_endpoints": lookups, "captcha_endpoints": apply_checks.CAPTCHA_ENDPOINTS}
    return (apply_checks.RouteState(**base, **old_lists), apply_checks.RouteState(**base, **({"lookup_endpoints": lookups} if lookups is not POLICY.lookup_endpoints else {})),
            apply_checks.RouteState(**base, **old_lists))


def request(method, host, path, *, nav=False, kind="fetch", body=None, public=True, socket=False, headers=None):
    return apply_checks.RouteRequest(
        method=method, url=f"https://{host}{path}", resource_type=kind, is_navigation=nav, is_websocket=socket, public=public,
        headers=headers or {}, body=body,
    )


class RoutePolicyValuesTests(unittest.TestCase):
    def test_the_greenhouse_policy_holds_exactly_the_old_module_constants(self):
        self.assertIs(POLICY, apply_checks.GREENHOUSE_ROUTE_POLICY)
        self.assertEqual(POLICY.display_name, GREENHOUSE.display_name)
        self.assertEqual(POLICY.navigation_hosts, old_route.BOARD_HOSTS)
        self.assertEqual(POLICY.submit_hosts, frozenset({old_route.SUBMIT_HOST}))
        self.assertEqual(POLICY.form_post_hosts, old_route.FORM_POST_HOSTS)
        self.assertEqual(POLICY.telemetry_hosts, old_route.TELEMETRY_HOSTS)
        self.assertEqual(POLICY.storage_upload_suffixes, (".amazonaws.com",))
        self.assertEqual(POLICY.lookup_endpoints, apply_checks.GREENHOUSE_LOOKUP_ENDPOINTS)
        self.assertEqual(POLICY.captcha_endpoints, apply_checks.CAPTCHA_ENDPOINTS)

    def test_the_static_asset_rule_is_the_old_one_on_every_host(self):
        for host in HOSTS:
            with self.subTest(host=host):
                self.assertEqual(POLICY.static_asset_host(host), old_route.is_static_asset_host(host))

    def test_the_submit_matcher_is_the_old_comparison(self):
        for host, path, submit in itertools.product(HOSTS, PATHS, (SUBMIT_PATH, "", "/other")):
            self.assertEqual(
                POLICY.is_submit_request(host, path, submit), host == old_route.SUBMIT_HOST and bool(submit) and path == submit, (host, path, submit))

    def test_the_policy_is_frozen_and_its_spec_carries_it(self):
        self.assertIs(GREENHOUSE.route_policy, POLICY)
        with self.assertRaises(dataclasses.FrozenInstanceError):
            POLICY.display_name = "Other"   # type: ignore[misc]

    def test_a_state_with_no_lists_uses_the_policys_and_a_state_with_lists_uses_its_own(self):
        none = apply_checks.RouteState(submit_path=SUBMIT_PATH, typing_key="city", typing_lookup="location")
        self.assertIsNone(none.lookup_endpoints)
        self.assertIsNone(none.captcha_endpoints)
        geocode = request("GET", "api-geocode-earth-proxy.greenhouse.io", "/v1/autocomplete?text=x")
        self.assertEqual(apply_checks.route_decision("rehearse", "after_input", geocode, none, POLICY).rule, "lookup")
        own = apply_checks.RouteState(submit_path=SUBMIT_PATH, typing_key="city", typing_lookup="location", lookup_endpoints=())
        self.assertEqual(apply_checks.route_decision("rehearse", "after_input", geocode, own, POLICY).rule, "after_first_input")


class RouteDecisionParityTests(unittest.TestCase):
    def same(self, mode, phase, req, fields, lookups, seen):
        before, after, written = states(fields, lookups)
        expected = old_route.route_decision(mode, phase, req, before)
        found = (
            apply_checks.route_decision(mode, phase, req, after, POLICY),
            apply_checks.route_decision(mode, phase, req, written, POLICY),
        )
        seen.add(type(expected).__name__ + ":" + getattr(expected, "rule", ""))
        if found != (expected, expected):
            return f"{mode}/{phase} {req.method} {req.url} {fields}: old {expected}, new {found}"
        return ""

    def test_every_host_method_and_navigation_in_every_mode_and_phase_gives_the_old_answer(self):
        seen, wrong = set(), []
        for mode, phase in VALID_PAIRS + (("unattended", "before_hand_over"), ("submit", "student"), ("rehearse", "other")):
            for host, method, nav in itertools.product(HOSTS, METHODS, (False, True)):
                for fields in (STATE_FIELDS[0], STATE_FIELDS[1], STATE_FIELDS[6]):
                    req = request(method, host, SUBMIT_PATH, nav=nav, kind="document" if nav else "script", body=b"x" if method == "POST" else None)
                    message = self.same(mode, phase, req, fields, POLICY.lookup_endpoints if method == "GET" else (TEST_LOOKUP,), seen)
                    if message:
                        wrong.append(message)
        self.assertEqual(wrong[:5], [])
        for rule in ("Allow:", "Allow:static_asset", "Abort:offsite_navigation", "Abort:telemetry", "Abort:s3_upload", "Abort:unknown_phase", "Abort:before_hand_over",
                     "Allow:hand_over", "Allow:submit", "Abort:other_non_get", "Abort:non_get_before_hand_over", "Allow:captcha", "Abort:after_first_input"):
            self.assertIn(rule, seen, f"the grid never reached {rule}")

    def test_a_seeded_sample_of_everything_at_once_gives_the_old_answer(self):
        rng, seen, wrong = random.Random(20261008), set(), []
        for _ in range(6000):
            mode, phase = rng.choice(MODES[:4]), rng.choice(PHASES[:5])
            req = request(
                rng.choice(METHODS), rng.choice(HOSTS), rng.choice(PATHS), nav=rng.random() < 0.2, kind=rng.choice(("document", "script", "image", "fetch", "xhr", "font")),
                body=rng.choice(BODIES), public=rng.choice((True, True, True, None, False)), socket=rng.random() < 0.05,
                headers=rng.choice(({}, {"Content-Type": "multipart/form-data; boundary=b"}, {"X-Echo": EMAIL})),
            )
            message = self.same(mode, phase, req, rng.choice(STATE_FIELDS), rng.choice(((TEST_LOOKUP,), BOUND_LOOKUPS, (), POLICY.lookup_endpoints)), seen)
            if message:
                wrong.append(message)
        self.assertEqual(wrong[:5], [])
        self.assertGreaterEqual(len(seen), 12, "the sample reaches many different rules")

    def test_every_pinned_endpoint_of_the_fixture_gives_the_old_answer_as_a_lookup_and_a_captcha_request(self):
        seen, wrong = set(), []
        for entry in ENDPOINTS["lookup_endpoints"]:
            path = entry["path_prefix"].replace("{token}", TOKEN) + "?text=Springfield"
            for kind in ("location", "school", "degree", "discipline", ""):
                for mode, phase in (("lookup", "after_input"), ("rehearse", "after_input"), ("handoff", "before_hand_over"), ("rehearse", "before_input")):
                    message = self.same(mode, phase, request("GET", entry["host"], path), {"typing_key": "city", "typing_lookup": kind}, BOUND_LOOKUPS, seen)
                    wrong += [message] if message else []
        for entry in ENDPOINTS["captcha_endpoints"]:
            for method in ("GET", "POST"):
                for mode, phase in itertools.product(("rehearse", "handoff"), ("before_input", "before_hand_over", "student", "after_hand_over")):
                    message = self.same(mode, phase, request(method, entry["host"], entry["path_prefix"] + "x", body=b"{}"), {"submit_posts_passed": 0}, (), seen)
                    wrong += [message] if message else []
        self.assertEqual(wrong[:5], [])
        self.assertIn("Allow:lookup", seen)
        self.assertIn("Allow:captcha", seen)

    def test_the_loader_paths_of_every_fixture_page_give_the_old_answer_for_the_submit_post(self):
        seen, wrong = set(), []
        for name in ("new_form.html", "legacy_form.html", "new_confirmation.html", "closed.html", "offsite.html"):
            host, submit, _confirmation = apply_agent.GreenhouseAdapter.loader_paths(fixture_text(name))
            for url_host in (host or old_route.SUBMIT_HOST, "job-boards.greenhouse.io"):
                for mode, phase in itertools.product(("submit", "handoff"), ("before_hand_over", "student", "after_hand_over")):
                    for fields in STATE_FIELDS[:2] + STATE_FIELDS[6:9]:
                        before, after, _written = states({**fields, "submit_path": submit}, ())
                        req = request("POST", url_host, submit or "/none", body=b"x")
                        expected = old_route.route_decision(mode, phase, req, before)
                        seen.add(type(expected).__name__ + ":" + getattr(expected, "rule", ""))
                        if apply_checks.route_decision(mode, phase, req, after, POLICY) != expected:
                            wrong.append((name, mode, phase, fields))
        self.assertEqual(wrong[:5], [])
        self.assertTrue({"Allow:hand_over", "Allow:submit", "Allow:security_code", "Abort:second_submit_post", "Abort:code_post_before_press"} <= seen, seen)

    def test_a_policy_with_other_hosts_changes_the_answer_and_nothing_else_does(self):
        other = dataclasses.replace(POLICY, navigation_hosts=frozenset({"jobs.example-robotics.test"}), submit_hosts=frozenset({"jobs.example-robotics.test"}),
                                    telemetry_hosts=frozenset({"beacon.example-robotics.test"}))
        state_ = apply_checks.RouteState(submit_path="/x", lookup_endpoints=())
        nav = request("GET", "jobs.example-robotics.test", "/x", nav=True, kind="document")
        self.assertIsInstance(apply_checks.route_decision("handoff", "before_hand_over", nav, state_, other), apply_checks.Allow)
        self.assertEqual(apply_checks.route_decision("handoff", "before_hand_over", nav, state_, POLICY).rule, "offsite_navigation")
        beacon = request("GET", "beacon.example-robotics.test", "/p")
        self.assertEqual(apply_checks.route_decision("handoff", "before_hand_over", beacon, state_, other).rule, "telemetry")
        post = request("POST", "jobs.example-robotics.test", "/x", body=b"x")
        self.assertEqual(apply_checks.route_decision("handoff", "student", post, state_, other).rule, "hand_over")
        self.assertEqual(apply_checks.route_decision("handoff", "student", post, state_, POLICY).rule, "non_get_before_hand_over")


class ResolvableHostsTests(unittest.TestCase):
    """The names the browser may look up (the Chromium resolver rule) are the union over registered ATSs, and each one is there for a reason."""

    OLD = sorted({
        *old_route.BOARD_HOSTS, *(entry["host"] for entry in ENDPOINTS["lookup_endpoints"]), *ENDPOINTS["static_asset_hosts"],
        "s?-recruiting.cdn.greenhouse.io", "s??-recruiting.cdn.greenhouse.io", "s???-recruiting.cdn.greenhouse.io",
        *(entry["host"] for entry in ENDPOINTS["captcha_endpoints"] if entry["confirmed"]), "fonts.googleapis.com", "fonts.gstatic.com",
    })

    def test_with_greenhouse_alone_the_list_is_the_old_one(self):
        self.assertEqual(list(apply_agent.resolvable_hosts((GREENHOUSE,))), self.OLD)
        self.assertEqual(sorted(POLICY.resolvable_hosts), [host for host in self.OLD if not host.startswith("fonts.")])

    def test_with_lever_registered_the_list_is_the_old_one_and_the_names_lever_needs(self):
        # Its two hosts, hCaptcha's five (the widget is how its Submit works), and the hosts of its fonts and its company's logo.
        self.assertEqual(set(apply_agent.RESOLVABLE_HOSTS) - set(self.OLD), {
            "jobs.lever.co", "jobs.eu.lever.co", "js.hcaptcha.com", "hcaptcha.com", "api.hcaptcha.com", "api2.hcaptcha.com", "newassets.hcaptcha.com",
            "cdn.lever.co", "lever-client-logos.s3.amazonaws.com",
        })
        self.assertEqual(set(self.OLD) - set(apply_agent.RESOLVABLE_HOSTS), set())

    def test_the_list_is_the_union_of_every_registered_policy_and_the_fonts(self):
        union = {host for spec in apply_ats.REGISTRY for host in spec.route_policy.resolvable_hosts}
        self.assertEqual(set(apply_agent.RESOLVABLE_HOSTS), union | set(apply_agent.FONT_HOSTS))
        self.assertEqual(list(apply_agent.RESOLVABLE_HOSTS), sorted(apply_agent.RESOLVABLE_HOSTS))

    def test_every_host_a_registered_policy_names_resolves(self):
        for spec in apply_ats.REGISTRY:
            policy = spec.route_policy
            for host in (*policy.navigation_hosts, *policy.submit_hosts, *(endpoint.host for endpoint in policy.lookup_endpoints)):
                with self.subTest(ats=spec.key, host=host):
                    self.assertTrue(any(fnmatch.fnmatchcase(host, pattern) for pattern in apply_agent.RESOLVABLE_HOSTS))

    def test_every_resolvable_name_belongs_to_a_registered_policy_or_is_a_font_host(self):
        for pattern in apply_agent.RESOLVABLE_HOSTS:
            sample = pattern.replace("?", "1")
            with self.subTest(host=pattern):
                if pattern in apply_agent.FONT_HOSTS:
                    continue
                reasons = [
                    spec.key for spec in apply_ats.REGISTRY if any((
                        sample in spec.route_policy.navigation_hosts, sample in spec.route_policy.submit_hosts,
                        sample in {endpoint.host for endpoint in spec.route_policy.lookup_endpoints},
                        sample in {endpoint.host for endpoint in spec.route_policy.captcha_endpoints},
                        spec.route_policy.static_asset_host(sample),
                    ))
                ]
                self.assertTrue(reasons, "nothing in any registered policy needs this name to resolve")

    def second_ats(self):
        return dataclasses.replace(
            GREENHOUSE, key="second", route_policy=dataclasses.replace(POLICY, resolvable_hosts=("jobs.example-robotics.test",)),
        )

    def test_the_function_gives_the_old_list_for_greenhouse_alone_and_adds_a_second_ats_hosts(self):
        self.assertEqual(list(apply_agent.resolvable_hosts((GREENHOUSE,))), self.OLD)
        both = apply_agent.resolvable_hosts((GREENHOUSE, self.second_ats()))
        self.assertIn("jobs.example-robotics.test", both)
        self.assertEqual(set(both) - set(self.OLD), {"jobs.example-robotics.test"})
        self.assertEqual(list(both), sorted(both))

    def test_the_resolver_rule_built_from_a_second_ats_lets_its_host_through_and_the_font_hosts_stay(self):
        hosts = apply_agent.resolvable_hosts((GREENHOUSE, self.second_ats()))
        rule = apply_agent.resolver_rule(hosts=hosts)
        self.assertIn("EXCLUDE jobs.example-robotics.test", rule)
        self.assertIn("EXCLUDE fonts.gstatic.com", rule)
        self.assertTrue(rule.startswith("--host-resolver-rules=MAP * ~NOTFOUND , "))
        self.assertNotIn("jobs.example-robotics.test", apply_agent.resolver_rule(), "the live rule is the registry's, and the second ATS is not in it")

    def test_the_module_builds_its_list_and_its_launch_switch_from_the_registry_it_imports(self):
        """In a fresh process whose registry holds a second ATS, the agent's own list, rule and launch switch name that ATS's host."""
        code = (
            "import dataclasses, sys\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from opportunity_app.apply import ats\n"
            "second = dataclasses.replace(ats.GREENHOUSE, key='second', route_policy=dataclasses.replace(ats.GREENHOUSE.route_policy, resolvable_hosts=('jobs.example-robotics.test',)))\n"
            "ats.REGISTRY = (ats.GREENHOUSE, second)\n"
            "from opportunity_app.apply import agent\n"
            "assert 'jobs.example-robotics.test' in agent.RESOLVABLE_HOSTS, agent.RESOLVABLE_HOSTS\n"
            "assert 'EXCLUDE jobs.example-robotics.test' in agent.resolver_rule()\n"
            "assert any('EXCLUDE jobs.example-robotics.test' in arg for arg in agent.LAUNCH_ARGS)\n"
            "print('ok')\n"
        )
        done = subprocess.run([sys.executable, "-I", "-c", code], cwd=str(ROOT), capture_output=True, text=True, timeout=120)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, "ok"), done.stderr[-800:])


class SendAndElsewhereParityTests(unittest.TestCase):
    def test_looks_like_a_send_and_student_submit_elsewhere_give_the_old_answer(self):
        now = time.monotonic()
        wrong, true_for = [], set()
        for host, path, method, nav in itertools.product(HOSTS, PATHS[:4] + PATHS[6:7], METHODS, (False, True)):
            for body, headers in ((None, {}), (b"a=b", {"Content-Type": "application/x-www-form-urlencoded"}), (b"{}", {"content-type": "application/json"}),
                                  (b"x", {"Content-Type": "text/plain"})):
                for pressed in (0.0, now, now - 60):
                    req = request(method, host, path, nav=nav, kind="document" if nav else "fetch", body=body, headers=headers)
                    before, after, _written = states({"last_press_at": pressed}, ())
                    for name, expected, found in (
                        ("send", old_route.looks_like_a_send(req, before), apply_checks.looks_like_a_send(req, after, POLICY)),
                        ("elsewhere", old_route.student_submit_elsewhere(req, before), apply_checks.student_submit_elsewhere(req, after, POLICY)),
                    ):
                        if expected != found:
                            wrong.append((name, host, path, method, nav, pressed))
                        if expected:
                            true_for.add(name)
        self.assertEqual(wrong[:5], [])
        self.assertEqual(true_for, {"send", "elsewhere"}, "the grid reaches both answers")


def seen_request(method, host, path, status, passed=True):
    return apply_checks.SeenRequest(method, host, path, status, passed)


SEEN_POOL = [
    seen_request("POST", old_route.SUBMIT_HOST, SUBMIT_PATH, status) for status in (None, 200, 302, 303, 400, 422, 428, 500, 502)
] + [
    seen_request("POST", old_route.SUBMIT_HOST, SUBMIT_PATH, 428, passed=False),
    seen_request("POST", "job-boards.greenhouse.io", SUBMIT_PATH, 200),
    seen_request("post", "BOARDS.GREENHOUSE.IO", SUBMIT_PATH, 200),
    seen_request("POST", old_route.SUBMIT_HOST, "/other", 200),
    seen_request("PUT", "example-uploads.s3.amazonaws.com", "/resume", None, passed=False),
    seen_request("GET", old_route.SUBMIT_HOST, SUBMIT_PATH, 200),
    seen_request("POST", "hcaptcha.com", "/x", 200),
]


class DecideOutcomeParityTests(unittest.TestCase):
    def observations(self):
        turn = itertools.count()
        for count in range(4):
            for chosen in itertools.product(SEEN_POOL, repeat=count):
                for main_path, visible, challenge, present, navigated, error in itertools.product(
                    ("", SUBMIT_PATH, CONFIRMATION_PATH), (False, True), (False, True), (False, True), (False, True), ("", "Email"),
                ):
                    if count == 3 and next(turn) % 7:
                        continue   # three requests: every seventh of the cross product
                    yield apply_checks.Observation(
                        main_path=main_path, form_present=present, requests=chosen, security_code_visible=visible, challenge_frame=challenge,
                        submit_path=SUBMIT_PATH, confirmation_path=CONFIRMATION_PATH, board_token=TOKEN, job_id=JOB, navigated=navigated, first_field_error=error,
                    )

    def test_every_observation_gives_the_old_outcome_and_the_old_new_code_prompt(self):
        count, outcomes, wrong = 0, set(), []
        for obs in self.observations():
            count += 1
            for over in (False, True):
                expected = old_route.decide_outcome(obs, code_wait_over=over)
                outcomes.add(expected.outcome)
                if apply_checks.decide_outcome(obs, POLICY, code_wait_over=over) != expected:
                    wrong.append((obs, over))
            if apply_checks.new_code_prompt(obs, POLICY) != old_route.new_code_prompt(obs):
                wrong.append((obs, "prompt"))
        self.assertEqual(wrong[:2], [])
        self.assertGreater(count, 5000)
        self.assertEqual(outcomes, {"submitted", "unconfirmed", "needs_you", "failed", "waiting"})

    def test_an_observation_with_no_loader_path_gives_the_old_outcome(self):
        for submit_path in ("", "/other"):
            for chosen in (SEEN_POOL[:3], SEEN_POOL[2:6], ()):
                obs = apply_checks.Observation(requests=tuple(chosen), submit_path=submit_path, main_path=CONFIRMATION_PATH, form_present=False)
                self.assertEqual(apply_checks.decide_outcome(obs, POLICY), old_route.decide_outcome(obs))

    def test_the_outcome_follows_the_policys_submit_host_and_confirmation_rule(self):
        post = (seen_request("POST", "apply.example-robotics.test", "/go", 302),)
        obs = apply_checks.Observation(requests=post, submit_path="/go", main_path="/thanks", form_present=False)
        self.assertEqual(apply_checks.decide_outcome(obs, POLICY).outcome, "failed", "Greenhouse's rules do not know this host, so no submit POST passed")
        other = dataclasses.replace(POLICY, submit_hosts=frozenset({"apply.example-robotics.test"}), confirmation_reached=lambda seen: seen.main_path == "/thanks")
        out = apply_checks.decide_outcome(obs, other)
        self.assertEqual((out.outcome, out.resolved_by), ("submitted", "page"))


class ConfirmationParityTests(unittest.TestCase):
    def test_every_path_and_query_gives_the_old_answer(self):
        paths = ("", CONFIRMATION_PATH, CONFIRMATION_PATH + "/", SUBMIT_PATH, "/other/jobs/4000000001/confirmation", f"/{TOKEN}/jobs/1/confirmation",
                 "/embed/job_app/confirmation", "/embed/job_app/confirmation/", "/embed/job_app", f"/{TOKEN.upper()}/jobs/{JOB}/confirmation", "/a.b/jobs/1/confirmation")
        queries = ("", f"?for={TOKEN}&token={JOB}", f"for={TOKEN}&token={JOB}", f"?token={JOB}&for={TOKEN}", f"?for={TOKEN}", f"?for={TOKEN}&token=1",
                   f"?for={TOKEN}&for=x&token={JOB}", f"?for={TOKEN}&token={JOB}&x=1", "?for=examplerobotics&token=4000000001%20")
        loader = ("", CONFIRMATION_PATH, "/other/confirmation", SUBMIT_PATH)
        ids = ((TOKEN, JOB), ("", ""), (TOKEN, ""), ("", JOB), ("a.b", "1"), ("a.b", "1.5"), (".*", ".*"), ("x)", "1"))
        found = set()
        for path, query, confirmation, (token, job) in itertools.product(paths, queries, loader, ids):
            obs = apply_checks.Observation(main_path=path, main_query=query, confirmation_path=confirmation, board_token=token, job_id=job)
            expected = old_route.confirmation_reached(obs)
            found.add(expected)
            self.assertEqual(POLICY.confirmation_reached(obs), expected, (path, query, confirmation, token, job))
        self.assertEqual(found, {True, False})

    def test_nothing_is_left_of_the_module_level_function(self):
        self.assertFalse(hasattr(apply_checks, "confirmation_reached"), "the decision moved onto the policy; no second copy stays behind")


# --- The per-ATS branches of ApplyAgent._run ---------------------------------------------------------------------

class SecondAdapter(apply_agent.GreenhouseAdapter):
    """A stand-in adapter for a second ATS: only what ``_run`` asks before the fill, answering as the test sets it."""

    ats = "second"
    form_page_kind = "second_form"

    def __init__(self, **answers):
        self.answers = {"kind": "second_form", "loader": ("apply.example-robotics.test", "/go", "/thanks"), "uploads": False, "reads": False, **answers}
        self.frame = mock.Mock()

    def form_frame(self, page):
        return self.frame

    def detect_page(self, page):
        return self.answers["kind"]

    def loader_paths(self, html, url=""):
        return self.answers["loader"]

    def uploads_on_attach(self, frame):
        return self.answers["uploads"]

    def reads_on_attach(self, frame):
        return self.answers["reads"]

    @staticmethod
    def posting_ids(url):
        return ("second", url.rstrip("/").rsplit("/", 1)[-1]) if "jobs.example-robotics.test" in url else ("", "")

    @staticmethod
    def lookup_token(url):
        return "tok"

    @staticmethod
    def confirmation_ids(url):
        return ("second", url.rstrip("/").rsplit("/", 1)[-1])


SECOND_LOOKUP = apply_checks.Endpoint("lookup.example-robotics.test", "/v1/{token}/places", "location")
SECOND_POLICY = dataclasses.replace(
    POLICY, display_name="Second", navigation_hosts=frozenset({"jobs.example-robotics.test"}), submit_hosts=frozenset({"apply.example-robotics.test"}),
    lookup_endpoints=(SECOND_LOOKUP,), form_post_hosts=frozenset({"apply.example-robotics.test"}),
)
SECOND = dataclasses.replace(GREENHOUSE, key="second", display_name="Second", route_policy=SECOND_POLICY)
SECOND_URL = "https://jobs.example-robotics.test/acme/9"


class AgentBranchesFollowTheAtsTests(unittest.TestCase):
    """What ``_run`` asked of Greenhouse alone (its hosts, its token, its page kind, its submit host) it now asks of the adapter and the policy."""

    def run_agent(self, adapter=None, *, mode="handoff", url=SECOND_URL, landed=SECOND_URL, filled=None):
        adapter = adapter or SecondAdapter()
        with mock.patch.object(apply_ats, "REGISTRY", (GREENHOUSE, SECOND)):
            agent = apply_agent.ApplyAgent(mode=mode, adapter=adapter)
        page = mock.Mock(url=landed)
        agent._start = lambda: setattr(agent, "_page", page)
        agent._open = lambda page_url: "<html></html>"
        agent._fill_form = lambda frame, replan, uploads: filled if filled is not None else "filled"
        return agent, agent.run(FakePlan([]), page_url=url, schema=[], files={})

    def test_the_agent_reads_its_policy_from_the_adapters_spec(self):
        with mock.patch.object(apply_ats, "REGISTRY", (GREENHOUSE, SECOND)):
            self.assertIs(apply_agent.ApplyAgent(mode="rehearse", adapter=SecondAdapter())._policy, SECOND_POLICY)
        self.assertIs(apply_agent.ApplyAgent(mode="rehearse", adapter=apply_agent.GreenhouseAdapter())._policy, POLICY)

    def test_an_adapter_of_an_ats_no_spec_names_builds_no_agent(self):
        with self.assertRaises(apply_ats.UnknownAts):
            apply_agent.ApplyAgent(mode="rehearse", adapter=SecondAdapter())

    def test_the_navigation_check_uses_the_policys_hosts(self):
        agent, result = self.run_agent()
        self.assertEqual(result, "filled")
        for url in ("https://job-boards.greenhouse.io/acme/jobs/1", "https://careers.example-robotics.test/x"):
            _agent, refused = self.run_agent(url=url)
            self.assertEqual((refused.outcome, refused.reasons), ("failed", [apply_agent.NOT_BOARD.format(ats="Second")]))
        greenhouse = apply_agent.ApplyAgent(mode="rehearse", adapter=apply_agent.GreenhouseAdapter())
        self.assertEqual(greenhouse.run(FakePlan([]), page_url=SECOND_URL, schema=[], files={}).reasons, [apply_agent.NOT_BOARD.format(ats="Greenhouse")])

    def test_the_lookup_endpoints_are_the_policys_with_the_adapters_token(self):
        agent, _result = self.run_agent()
        self.assertEqual(agent._endpoints, (apply_checks.Endpoint("lookup.example-robotics.test", "/v1/tok/places", "location"),))
        self.assertEqual(agent._state.lookup_endpoints, agent._endpoints)

    def test_the_different_posting_check_uses_the_adapters_ids(self):
        _agent, result = self.run_agent(landed="https://jobs.example-robotics.test/acme/10")
        self.assertEqual((result.outcome, result.reasons), ("needs_you", [apply_agent.DIFFERENT_POSTING.format(ats="Second")]))
        _agent, result = self.run_agent(landed="https://jobs.example-robotics.test/acme/9/")
        self.assertEqual(result, "filled")
        _agent, result = self.run_agent(landed="https://elsewhere.example-robotics.test/acme/10")
        self.assertEqual(result, "filled", "a page the adapter cannot read the ids of is not judged, as before")

    def test_the_page_kind_is_the_adapters(self):
        _agent, result = self.run_agent(SecondAdapter(kind="application_form_new"))
        self.assertEqual((result.outcome, result.reasons), ("needs_you", [apply_agent.UNKNOWN_PAGE.format(ats="Second")]), "Greenhouse's name for the form is not this ATS's")
        _agent, result = self.run_agent(SecondAdapter(kind="second_form"))
        self.assertEqual(result, "filled")

    def test_a_handoff_needs_a_submit_host_the_policy_names(self):
        _agent, result = self.run_agent(SecondAdapter(loader=("boards.greenhouse.io", "/go", "/thanks")))
        self.assertEqual((result.outcome, result.reasons), ("needs_you", [agent_types.HANDOFF_NO_LOADER]))
        _agent, result = self.run_agent(SecondAdapter(loader=("", "/go", "/thanks")))
        self.assertEqual(result.reasons, [agent_types.HANDOFF_NO_LOADER])
        _agent, result = self.run_agent(SecondAdapter())
        self.assertEqual(result, "filled")
        _agent, result = self.run_agent(SecondAdapter(loader=("boards.greenhouse.io", "/go", "/thanks")), mode="rehearse")
        self.assertEqual(result, "filled", "a rehearsal never refused for the host")

    def test_a_board_that_uploads_to_storage_is_refused_and_one_that_only_reads_is_not(self):
        _agent, result = self.run_agent(SecondAdapter(uploads=True))
        self.assertEqual((result.outcome, result.reasons), ("needs_you", [agent_types.HANDOFF_S3]))
        agent, result = self.run_agent(SecondAdapter(reads=True))
        self.assertEqual(result, "filled")
        self.assertIs(agent._evidence_bits["reads_on_attach"], True)
        self.assertIs(agent._evidence_bits["uploads_on_attach"], False)
        agent, _result = self.run_agent(SecondAdapter())
        self.assertNotIn("reads_on_attach", agent._evidence_bits, "a page that does not read as it is attached adds nothing to the evidence")

    def test_the_greenhouse_adapters_answers_are_the_old_functions(self):
        adapter = apply_agent.GreenhouseAdapter()
        urls = ("https://job-boards.greenhouse.io/Examplerobotics/jobs/4000000001", "https://boards.greenhouse.io/embed/job_app?for=Examplerobotics&token=4000000001",
                "https://boards.greenhouse.io/embed/job_app?for=examplerobotics&token=x", "https://jobs.lever.co/acme/1", "", "not a url", "https://[bad")
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(adapter.posting_ids(url), apply_agent.posting_ids(url))
                self.assertEqual(adapter.lookup_token(url), apply_agent.board_token(url))
                self.assertEqual(adapter.confirmation_ids(url), (apply_agent.board_token(url), apply_agent._job_id(url)))
        self.assertEqual(adapter.form_page_kind, "application_form_new")
        self.assertEqual(adapter.ats, "greenhouse")
        frame = mock.Mock()
        self.assertIs(adapter.reads_on_attach(frame), False)
        self.assertEqual(frame.mock_calls, [], "it asks the page nothing")


# --- Per-ATS sentences: the words for Greenhouse are the old ones, byte for byte ---------------------------------------------

WORDS = dict(question="Why us?", n=7, host="apply.example-robotics.test")


class SentenceParityTests(unittest.TestCase):
    """Every sentence that said "Greenhouse" now names the ATS it is told, and for Greenhouse says exactly what it said (frozen_pre_sentences)."""

    def test_the_progress_steps_are_the_old_ones_and_the_two_lever_added_and_only_these_name_the_ats(self):
        # Two steps are new, and a Greenhouse run never reports them: the student attached a file in a window whose page reads it at once, and the fields it filled.
        self.assertEqual(set(agent_types.PROGRESS_STEPS), set(old_words.PROGRESS_STEPS) | {"resume_attached", "resume_changed"})
        for step, old_text in old_words.PROGRESS_STEPS.items():
            with self.subTest(step=step):
                self.assertEqual(agent_types.progress_text(step, "Greenhouse", **WORDS), old_text.format(**WORDS))
        naming = {step for step, text in agent_types.PROGRESS_STEPS.items() if "{ats}" in text}
        self.assertEqual(naming, {"open", "submitting", "security_code", "code_yours", "challenge", "resume_attached", "resume_changed"})
        self.assertEqual(agent_types.progress_text("open", "Second"), "Opening the Second form")

    def test_the_window_note_and_the_outcome_notes(self):
        self.assertEqual(agent_types.WINDOW_UNCONFIRMED.format(ats="Greenhouse"), old_words.WINDOW_UNCONFIRMED)
        for name in ("UNCONFIRMED_NOTE", "SECURITY_CODE_NOTE", "CODE_REFUSED_NOTE", "CHALLENGE_NOTE"):
            with self.subTest(note=name):
                self.assertEqual(getattr(apply_checks, name).format(ats="Greenhouse"), getattr(old_route, name))
        self.assertEqual(apply_checks.REFUSED_NOTE.format(ats="Greenhouse", status=422), old_words.refused_form(422))
        self.assertEqual(apply_checks.MARKED_WRONG_NOTE.format(ats="Greenhouse", question="Email"), old_words.marked_wrong("Email"))

    def test_the_agents_sentences(self):
        for name in ("NOT_BOARD", "LEGACY", "UNKNOWN_PAGE", "NO_ENDPOINT", "OPEN_FAILED", "DIFFERENT_POSTING"):
            with self.subTest(sentence=name):
                self.assertEqual(getattr(apply_agent, name).format(ats="Greenhouse"), getattr(old_words, name))
        self.assertEqual(apply_agent.HTTP_STATUS.format(ats="Greenhouse", status=503), old_words.HTTP_STATUS.format(status=503))

    def test_the_agent_names_the_ats_it_is_for(self):
        with mock.patch.object(apply_ats, "REGISTRY", (GREENHOUSE, SECOND)):
            second = apply_agent.ApplyAgent(mode="rehearse", adapter=SecondAdapter())
        first = apply_agent.ApplyAgent(mode="rehearse", adapter=apply_agent.GreenhouseAdapter())
        self.assertEqual(first._say(apply_agent.OPEN_FAILED), old_words.OPEN_FAILED)
        self.assertEqual(second._say(apply_agent.OPEN_FAILED), "The app could not open the Second form")
        self.assertEqual(second._say(apply_agent.HTTP_STATUS, status=404), "Second answered HTTP 404")
        sent = []
        named = apply_agent.ApplyAgent(mode="rehearse", adapter=apply_agent.GreenhouseAdapter(), on_progress=lambda step, text: sent.append((step, text)))
        named._progress("open")
        self.assertEqual(sent, [("open", old_words.PROGRESS_STEPS["open"])])

    def test_the_outcome_notes_name_the_policys_ats(self):
        obs = apply_checks.Observation(requests=(seen_request("POST", "apply.example-robotics.test", "/go", 422),), submit_path="/go", form_present=True,
                                       first_field_error="Email")
        for policy, name in ((POLICY, "Greenhouse"), (SECOND_POLICY, "Second")):
            with self.subTest(ats=name):
                other = dataclasses.replace(policy, submit_hosts=frozenset({"apply.example-robotics.test"}))
                self.assertEqual(apply_checks.decide_outcome(obs, other).note, f'{name} refused the form (HTTP 422). {name} marked "Email" as wrong')
        done = apply_checks.Observation(security_code_visible=True)
        self.assertEqual(apply_checks.decide_outcome(done, SECOND_POLICY, code_wait_over=True).note,
                         "Second asked for the emailed security code, and Submit application was not pressed after it. Look for Second's email")

    def test_the_problems_of_a_join_and_a_check_name_the_ats(self):
        schema = [{"name": "first_name", "label": "First Name", "required": True, "type": "input_text"}]
        missing = apply_checks.join(schema, [], ats_name="Greenhouse")
        self.assertEqual([problem.message for problem in missing], [old_words.listing_mismatch("First Name")])
        scan = [{"name": "q", "id": "q", "question": "Why do you want to join us?", "required_any": False, "visible_css": True, "type": "text", "widget": "native"}]
        reworded = apply_checks.join([{"name": "q", "label": "Why us?", "required": False, "type": "input_text"}], scan, ats_name="Greenhouse")
        self.assertEqual([problem.message for problem in reworded], [old_words.wording_mismatch("Why do you want to join us?")])
        self.assertEqual([problem.message for problem in apply_checks.join(schema, [], ats_name="Second")], ["The form does not match what Second's own listing describes (First Name)"])
        plan = types.SimpleNamespace(fields=[], plan_hash="")
        found = apply_checks.check_required([], plan, [{"name": "email", "label": "Email", "required": True, "type": "input_text"}], ats_name="Greenhouse")
        self.assertEqual([problem.message for problem in found], [old_words.required_not_seen("Email")])

    def test_the_posting_difference_names_the_ats(self):
        listing = {"company_name": "Orbit Systems", "title": "Controls Intern"}
        found = apply_policy.posting_difference("Acme Robotics", "Controls Intern", listing, ats_name="Greenhouse")
        self.assertEqual(found, old_words.posting_other_company("Controls Intern", "Orbit Systems", "Acme Robotics"))
        found = apply_policy.posting_difference("Orbit Systems", "Sales Lead", listing, ats_name="Greenhouse")
        self.assertEqual(found, old_words.posting_other_title("Controls Intern", "Sales Lead"))
        self.assertEqual(apply_policy.posting_difference("Orbit Systems", "Sales Lead", listing, ats_name="Second"), "Second's form is for Controls Intern, not Sales Lead")

    def test_the_checks_sentences(self):
        # The one sentence that names every registered ATS: with Lever registered it says so, and is otherwise the old words.
        self.assertEqual(apply_preflight.not_supported(), old_words.NOT_GREENHOUSE.replace("Greenhouse postings", "Greenhouse and Lever postings"))
        self.assertEqual(apply_preflight.NOT_FOUND.format(ats="Greenhouse"), old_words.NOT_FOUND)
        self.assertEqual(apply_preflight.NO_ANSWER.format(ats="Greenhouse"), old_words.NO_ANSWER)
        self.assertEqual(apply_preflight.DUPLICATE_TICK.format(company="Acme Robotics", ats="Greenhouse", date="April 3"), old_words.duplicate_tick("Acme Robotics", "April 3"))
        for count in (1, 2, 5):
            self.assertEqual(
                apply_preflight.LEFT_FOR_YOU.format(count=count, is_are="is" if count == 1 else "are", ats="Greenhouse"), old_words.left_for_you(count))
            self.assertEqual(
                apply_preflight.YOURS_TO_ANSWER.format(count=count, s_are="s are" if count != 1 else " is", ats="Greenhouse"), old_words.yours_to_answer(count))
        with mock.patch.object(apply_ats, "REGISTRY", (GREENHOUSE, SECOND)):
            self.assertEqual(apply_preflight.not_supported(), "Apply for me works with Greenhouse and Second postings only, for now")
        self.assertEqual(apply_ats.supported_names(), "Greenhouse and Lever")

    def test_the_names_of_the_registered_ats_as_words(self):
        third = dataclasses.replace(GREENHOUSE, key="third", display_name="Third")
        with mock.patch.object(apply_ats, "REGISTRY", (GREENHOUSE, SECOND, third)):
            self.assertEqual(apply_ats.supported_names(), "Greenhouse, Second and Third")
            self.assertEqual(apply_ats.name_of("second"), "Second")
        self.assertEqual(apply_ats.name_of("greenhouse"), "Greenhouse")
        self.assertEqual(apply_ats.name_of("some-ats"), "Some-Ats", "a row of an ATS this build no longer registers is named by its key")
        self.assertEqual(apply_ats.name_of(""), "")

    def test_the_ledger_describes_the_feature_with_the_specs_name(self):
        from opportunity_app.automation import ledger

        self.assertEqual(ledger.FEATURES["apply_agent"].description, old_words.FEATURE_APPLY_AGENT)
        self.assertEqual(GREENHOUSE.display_name, "Greenhouse")


class RunsAndWatchSentenceTests(unittest.TestCase):
    def test_the_runs_sentences_are_the_old_ones(self):
        self.assertEqual(apply_runs.CONFIRMED_BY_EMAIL.format(ats="Greenhouse", day="April 3"), old_words.confirmed_by_email("April 3"))
        self.assertEqual(apply_runs.RELEASED_JOB_ASK.format(ats="Greenhouse", day="April 3"), old_words.released_job_ask("April 3"))
        self.assertEqual(apply_runs.OTHER_COPY.format(ats="Greenhouse", title="Controls Intern"), old_words.other_copy("Controls Intern"))
        self.assertEqual(apply_runs.LATE_CONFIRMATION.format(ats="Greenhouse", company="Acme"), old_words.late_confirmation("Acme"))
        self.assertEqual(apply_runs.RESULT_SUBMITTED.format(ats="Greenhouse", title="Controls Intern", company="Acme"), old_words.result_submitted("Controls Intern", "Acme"))
        self.assertEqual(apply_runs.STOPPED_BEFORE.format(ats="Greenhouse"), old_words.STOPPED_BEFORE)

    def test_what_settled_a_submission_is_said_with_the_claims_ats(self):
        for resolved_by, old_row in (("page", "page"), ("email", "email"), ("student", "student"), ("", "page"), ("other", "page")):
            with self.subTest(resolved_by=resolved_by):
                found = apply_runs._settled_by({"resolved_by": resolved_by, "ats": "greenhouse"})
                self.assertEqual(found, old_words.SETTLED_BY.get(resolved_by, old_words.SETTLED_BY["page"]))
        with mock.patch.object(apply_ats, "REGISTRY", (GREENHOUSE, SECOND)):
            self.assertEqual(apply_runs._settled_by({"resolved_by": "email", "ats": "second"})[2], "Second's confirmation email arrived")
        self.assertEqual(apply_runs._settled_by({"resolved_by": "page", "ats": "gone"})[2], "Gone showed its confirmation page")

    def test_the_watch_sentences_are_the_old_ones(self):
        from opportunity_app.apply import watch as apply_watch

        self.assertEqual(apply_watch.EMAIL_AFTER_RELEASE.format(ats="Greenhouse", company="Acme"), old_words.email_after_release("Acme"))
        self.assertEqual(apply_watch.EMAIL_CONFIRMED.format(ats="Greenhouse", company="Acme"), old_words.email_confirmed("Acme"))
        self.assertFalse(hasattr(apply_watch, "ATS_NAMES"), "the names come from the registry, not a second table")


class RunnerSentenceTests(unittest.TestCase):
    def row(self, **more):
        return {"status": "finished", "kind": "handoff", "outcome": "submitted", "ats": "greenhouse", **more}

    def test_the_summaries_are_the_old_ones(self):
        summary = apply_runner._summary
        for ask in (False, True):
            self.assertEqual(summary(self.row(), [], {}, "", "", [], False, claim={"ask_mark_applied": ask}), old_words.confirmation_shown(ask))
        looked = self.row(kind="lookup", outcome="looked_up")
        for count in (1, 3):
            self.assertEqual(summary(looked, [], {"city": ["x"] * count}, "", "", [], False), old_words.options_listed(f"{count} option{'' if count == 1 else 's'}"))

    def test_the_summaries_name_the_runs_ats(self):
        with mock.patch.object(apply_ats, "REGISTRY", (GREENHOUSE, SECOND)):
            self.assertEqual(apply_runner._summary(self.row(ats="second"), [], {}, "", "", [], False, claim={}), "Second showed its confirmation page.")
            running = {"status": "running", "kind": "handoff", "outcome": "", "ats": "second"}
            self.assertEqual(apply_runner._summary(running, [], {}, "", "", [], False, phase="submitting"), "Submitting to Second…")

    def test_the_rehearsal_measure_is_the_old_one(self):
        row = {"outcome": "rehearsed", "ats": "greenhouse"}
        cases = (
            ({"refused_total": 1, "lookups": []}, "1 request", "", ""),
            ({"refused_total": 4, "lookups": [{"question": "City", "typed": True}, {"question": "Degree", "typed": False}]}, "4 requests", "City", "Degree"),
            ({"refused_total": 2, "lookups": [{"question": "City", "typed": True}, {"key": "school", "typed": True}]}, "2 requests", "City and school", ""),
        )
        for evidence, count_words, typed, listed in cases:
            with self.subTest(evidence=evidence):
                self.assertEqual(apply_runner._measured(row, evidence, []), old_words.measured(count_words, typed, listed))
        self.assertIn("to Second or anywhere else", apply_runner._measured({**row, "ats": "second"}, {"refused_total": 1}, []))

    def test_the_settlement_notes_name_the_ats(self):
        result = RunResult("needs_you", ["x"], handed_over=True, after_click=True)
        settlement = apply_runner.handoff_settlement(
            None, stop="", shutting_down=False, claim_state="clicking", cancel_requested=False, handed_over=True, closed_confirmed=True, minutes=5, ats_name="Second",
        )
        self.assertEqual(settlement.note, "Your application may have been sent, but Second did not show its confirmation page. Look for its email")
        unclosed = apply_runner.handoff_settlement(
            result, stop="", shutting_down=False, claim_state="claimed", cancel_requested=False, handed_over=False, closed_confirmed=False, minutes=5, ats_name="Second",
        )
        self.assertTrue(unclosed.note.endswith("Check your email for a confirmation from Second."), unclosed.note)


# --- The names the pages need ---------------------------------------------------------------------------------------------

class TheNamesThePagesNeedTests(ApplyCase):
    """The page's sentences take the ATS's name from the server's payloads: the check, an attempt's first event, and the automation lists."""

    def test_the_automation_lists_say_which_ats_an_application_item_is_on(self):
        from opportunity_app.automation import ledger as automation

        held = self.raw_claim(state="clicking", mode="handoff", handed_over_at=utc_now(), ats="second")
        apply_claims.RUNNING.add(held)
        self.raw_claim(state="unconfirmed", mode="handoff", handed_over_at=utc_now(), ats="greenhouse")
        self.raw_claim(state="unconfirmed", mode="handoff", handed_over_at=utc_now(), ats="second")
        flights = automation.in_flight(self.conn, USER)
        self.assertEqual([(item["action"], item["ats"]) for item in flights], [("application", "second")])
        unconfirmed = automation.unconfirmed(self.conn, USER)
        self.assertEqual(sorted(item["ats"] for item in unconfirmed), ["greenhouse", "second"])

    def test_the_router_adds_the_display_name_to_those_items_and_only_those(self):
        from opportunity_app.web.routers import automation as automation_router

        items = [
            {"action": "application", "ats": "greenhouse"}, {"action": "application", "ats": "gone"}, {"action": "send", "ats": "greenhouse"},
            {"action": "window"}, {"action": "application"},
        ]
        with mock.patch.object(apply_ats, "REGISTRY", (GREENHOUSE, SECOND)):
            automation_router.name_the_ats(items)
        self.assertEqual([item.get("ats_name") for item in items], ["Greenhouse", "Gone", None, None, None])
        self.assertEqual(automation_router.name_the_ats(None), None)

    def test_an_attempts_first_event_records_the_name_of_its_ats(self):
        self.start("job-1", "handoff")
        [event] = self.conn.execute("SELECT detail_json FROM application_events WHERE event_type='apply_agent_started'").fetchall()
        self.assertEqual(json.loads(event["detail_json"])["ats_name"], "Greenhouse")


class TheCheckNamesItsAtsTests(runner_tests.RunnerCase):
    def test_the_check_says_which_ats_it_read_and_a_role_no_ats_recognises_says_none(self):
        result = apply_preflight.check(self.conn, runner_tests.USER, runner_tests.ACME, client=self.schema, resume_root=self.root / "resumes")
        self.assertEqual((result["ats"], result["ats_name"]), ("greenhouse", "Greenhouse"))
        renamed = dataclasses.replace(GREENHOUSE, key="greenhouse-renamed", display_name="Renamed")
        with mock.patch.object(apply_ats, "REGISTRY", (renamed,)):
            result = apply_preflight.check(self.conn, runner_tests.USER, runner_tests.ACME, client=self.schema, resume_root=self.root / "resumes")
        self.assertEqual((result["ats"], result["ats_name"]), ("greenhouse-renamed", "Renamed"))
        with mock.patch.object(apply_ats, "REGISTRY", ()):
            result = apply_preflight.check(self.conn, runner_tests.USER, runner_tests.ACME, client=self.schema, resume_root=self.root / "resumes")
        self.assertEqual((result["ats"], result["ats_name"], result["status"]), ("", "", "unavailable"))


class ThePagesNameTheAtsFromThePayloadTests(unittest.TestCase):
    """app-ui.js's atsName and the sentences that use it (the browser tests read them rendered; this reads the source)."""

    def test_no_sentence_of_the_three_pages_names_greenhouse_but_through_atsname(self):
        texts = {name: text for name, text in helpers_source.static_scripts().items() if name.rsplit("/", 1)[-1] in ("app-apply.js", "app-applications.js", "app-automation.js")}
        self.assertEqual(len(texts), 3, "the scan found the three pages")
        for name, text in texts.items():
            for number, line in enumerate(text.splitlines(), 1):
                code = line.split("//", 1)[0]
                self.assertNotIn("Greenhouse", code, f"{name}:{number} says Greenhouse in a sentence instead of the payload's name")


    def test_the_loading_line_says_what_it_said_for_greenhouse(self):
        """The one page sentence the payload cannot fill yet (the page asks before the check answers): the fallback name gives the old words."""
        text = next(text for name, text in helpers_source.static_scripts().items() if name.rsplit("/", 1)[-1] == "app-apply.js")
        self.assertIn('element("p", "apply-summary", `Checking the ${atsName(null)} form…`)', text)
        ui = next(text for name, text in helpers_source.static_scripts().items() if name.rsplit("/", 1)[-1] == "app-ui.js")
        self.assertIn('|| "Greenhouse";', ui[ui.index("function atsName"):], "atsName(null) is the old word")


# --- Correction 1: the company limit matches a board within its ATS ---------------------------------------------------

class CompanyLimitIsPerAtsTests(ApplyCase):
    """A Lever site called ``acme`` is not the Greenhouse board called ``acme`` (docs/phase5-lever-handoff-spec.md 5.2 item 4, R7)."""

    def handed(self, *, ats, board, company):
        return self.raw_claim(
            state="submitted", mode="handoff", handed_over_at=self.at(-60 * 24 * 3).isoformat(timespec="microseconds"), ats=ats, board=board, company=company,
        )

    def block(self, ats, board, company="Orbit Systems"):
        return apply_runs.limit_check(self.conn, USER, employer_key(company), ats, board, "handoff", self.at())

    def test_the_same_board_name_on_another_ats_does_not_trip_the_limit(self):
        self.handed(ats="second", board="acme", company="Zed Corporation")
        self.assertIsNone(self.block("greenhouse", "acme"), "a Greenhouse board that shares a name with another ATS's site is another employer")
        self.assertIsNone(self.block("third", "acme"))

    def test_the_same_board_on_the_same_ats_still_does(self):
        self.handed(ats="second", board="acme", company="Zed Corporation")
        block = self.block("second", "acme")
        self.assertEqual((block.kind, block.code), ("ask", apply_runs.ASK_COMPANY_LIMIT))
        self.assertIn("You applied to Zed Corporation with Apply for me 3 days ago", block.message)

    def test_the_same_company_name_still_counts_across_ats(self):
        self.handed(ats="second", board="zed-site", company="Zed Corporation")
        block = self.block("greenhouse", "another-board", company="ZED corporation")
        self.assertEqual(block.code, apply_runs.ASK_COMPANY_LIMIT, "one company is one company whichever ATS it uses")

    def test_a_hand_over_is_not_refused_for_another_atss_site_of_that_name(self):
        self.handed(ats="second", board="bluefin", company="Zed Corporation")
        claim = self.start("job-1", "handoff", board="bluefin", now=self.at(0))
        self.assertEqual(claim["state"], "claimed")

    def test_a_hand_over_is_still_asked_about_for_the_same_atss_board(self):
        self.handed(ats="greenhouse", board="bluefin", company="Zed Corporation")
        with self.assertRaises(ClaimRefused) as caught:
            self.start("job-1", "handoff", board="bluefin", now=self.at(0))
        self.assertEqual((caught.exception.code, caught.exception.ask), (apply_runs.ASK_COMPANY_LIMIT, True))
# --- Correction 2: the schema cache is keyed by the ATS ---------------------------------------------------------------

class SchemaCacheIsPerAtsTests(runner_tests.RunnerCase):
    def test_the_key_holds_the_ats_and_a_listing_is_served_only_to_it(self):
        cache = apply_preflight.SchemaCache()
        cache.put(("greenhouse", "acme", "1"), {"title": "A"})
        self.assertEqual(cache.get(("greenhouse", "acme", "1")), {"title": "A"})
        self.assertIsNone(cache.get(("second", "acme", "1")), "another ATS's site of the same name and job id is another posting")

    def test_a_listing_read_for_one_ats_is_not_served_for_another(self):
        class Client:
            def __init__(self, listing):
                self.listing, self.calls = listing, 0

            def fetch(self, token, job_id):
                self.calls += 1
                return self.listing

        cache = apply_preflight.SchemaCache()
        first, second = Client({"title": "On the first"}), Client({"title": "On the second"})
        self.assertEqual(apply_preflight._listing(first, cache, "greenhouse", "acme", "1"), ({"title": "On the first"}, "", False))
        self.assertEqual(apply_preflight._listing(second, cache, "second", "acme", "1"), ({"title": "On the second"}, "", False))
        self.assertEqual(apply_preflight._listing(first, cache, "greenhouse", "acme", "1"), ({"title": "On the first"}, "", True))
        self.assertEqual((first.calls, second.calls), (1, 1))

    def test_the_check_files_a_listing_under_the_ats_of_the_role(self):
        cache = apply_preflight.SchemaCache()

        def check():
            return apply_preflight.check(self.conn, runner_tests.USER, runner_tests.ACME, client=self.schema, cache=cache, resume_root=self.root / "resumes")

        self.assertFalse(check()["from_cache"])
        self.assertTrue(check()["from_cache"])
        renamed = dataclasses.replace(GREENHOUSE, key="greenhouse-renamed")
        with mock.patch.object(apply_ats, "REGISTRY", (renamed,)):
            self.assertFalse(check()["from_cache"], "the same token and job id on another ATS is a different listing")
            self.assertTrue(check()["from_cache"])


# --- Correction 3: a run is stamped with its own ATS's adapter version -------------------------------------------------

class Replanning(FakeApplyAgentFactory):
    """A factory whose agent asks the runner for a new plan before it does anything else, as the real agent does once it has read the page."""

    def __call__(self, **kwargs):
        agent = super().__call__(**kwargs)
        inner = agent.run

        def run(plan, **more):
            if more.get("replan") is not None:
                more["replan"]([], False)
            return inner(plan, **more)

        agent.run = run
        return agent


class RunnerStampsTheRunsOwnVersionTests(runner_tests.RunnerCase):
    def test_a_runs_row_and_every_plan_it_makes_carry_the_specs_adapter_version(self):
        newer = dataclasses.replace(GREENHOUSE, key="greenhouse-renamed", adapter_version="greenhouse-9")
        with mock.patch.object(apply_ats, "REGISTRY", (newer,)), mock.patch.object(apply_policy, "build_plan", wraps=apply_policy.build_plan) as spy:
            row = self.finish(self.start(Replanning()))
        self.assertEqual((row["ats"], row["adapter_version"]), ("greenhouse-renamed", "greenhouse-9"))
        versions = [call.kwargs["adapter_version"] for call in spy.call_args_list]
        self.assertGreaterEqual(len(versions), 2, "the check's plan and the one the agent asked for")
        self.assertEqual(set(versions), {"greenhouse-9"}, "the page's plan is fingerprinted with the same version the student approved")

    def test_greenhouse_runs_are_still_stamped_greenhouse_1(self):
        row = self.finish(self.start(Replanning()))
        self.assertEqual((row["ats"], row["adapter_version"]), ("greenhouse", old.ADAPTER_VERSION))


if __name__ == "__main__":
    unittest.main()
