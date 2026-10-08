"""The ATS seam (apply/ats.py), part 1: Greenhouse is the only ATS registered, and nothing it does has changed.

docs/phase5-lever-handoff-spec.md 5.2 and milestone LV1a. Each decision the seam moved behind the registry (``identify``,
``canonical_url``, ``parse_schema``, the confirmation-sender check) is run old against new: the old code is a frozen copy
(tests/frozen_pre_ats_seam.py, taken from the commit before the seam) and the new is reached through the registry, on every
Greenhouse fixture and vector. What the seam added (the registry's own values, the adapter Protocol, ``AgentJob.ats``,
the factory's ``ats`` argument) is pinned directly. No browser and no network.
"""

import copy
import dataclasses
import inspect
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import realdata_guard

realdata_guard.install()

import frozen_pre_ats_seam as old
import helpers_source
import test_apply_runner as runner_tests
from opportunity_app.apply import agent as apply_agent, agent_types, ats as apply_ats, greenhouse as apply_greenhouse, preflight as apply_preflight
from opportunity_app.apply.agent_types import AgentJob, ApplyTimeouts
from opportunity_app.apply.policy import SchemaField
from opportunity_app.apply.schema_client import GreenhouseSchemaClient
from opportunity_app.core.timestamps import utc_now

from apply_fake_ats import FakeApplyAgentFactory, fixture_json
from helpers_apply import ApplyCase, setUpModule, tearDownModule  # noqa: F401

GREENHOUSE = apply_ats.GREENHOUSE


# --- The registry's values are exactly Greenhouse's -------------------------------------------------------------

class RegistryValuesTests(unittest.TestCase):
    def test_greenhouse_is_the_only_ats_and_has_todays_values(self):
        self.assertEqual(apply_ats.REGISTRY, (GREENHOUSE,))
        self.assertEqual(apply_ats.keys(), ("greenhouse",))
        self.assertIs(apply_ats.spec_for("greenhouse"), GREENHOUSE)
        self.assertEqual(GREENHOUSE.key, old.ATS_GREENHOUSE)
        self.assertEqual(GREENHOUSE.display_name, "Greenhouse")
        self.assertEqual(GREENHOUSE.adapter_version, old.ADAPTER_VERSION)
        self.assertEqual(GREENHOUSE.supported_modes, ("lookup", "rehearse", "handoff"))
        self.assertEqual(GREENHOUSE.supported_modes, agent_types.BUILT_MODES)
        self.assertIs(GREENHOUSE.identify, apply_greenhouse.identify)
        self.assertIs(GREENHOUSE.canonical_url, apply_greenhouse.canonical_url)

    def test_an_unknown_ats_is_refused_by_name(self):
        for key in ("lever", "", "Greenhouse", "greenhouse "):
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
            "form_frame", "detect_page", "loader_paths", "uploads_on_attach", "security_code_prompt", "security_code_inputs", "captcha_widget",
            "control", "control_kind", "is_react_select", "field_container", "choices", "fill_location", "read_options",
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
    def test_every_registered_ats_has_an_adapter_and_the_reverse(self):
        self.assertEqual(set(apply_agent.ADAPTERS), set(apply_ats.keys()))

    def test_greenhouse_gets_the_greenhouse_adapter_with_or_without_the_argument(self):
        factory = apply_agent.DefaultApplyAgentFactory()
        for agent in (build(factory), build(factory, ats="greenhouse")):
            self.assertIs(type(agent.adapter), apply_agent.GreenhouseAdapter)

    def test_an_ats_that_is_not_registered_builds_no_agent(self):
        with self.assertRaises(apply_ats.UnknownAts):
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


if __name__ == "__main__":
    unittest.main()
