"""The Lever adapter without a browser: its shape against the Protocol, the constants it keeps, the static scans of spec 10.5, and the proof that
nothing in production starts a Lever browser run yet (docs/phase5-lever-handoff-spec.md 10.5 and 12, LV3). What the adapter does in a real Chromium
is in tests/test_apply_lever_browser.py. Every company, person and address is fictional.
"""

import ast
import inspect
import re
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import realdata_guard

realdata_guard.install()

import apply_fake_ats
import helpers_source
from helpers_apply import FakePlan, planned
from helpers_source import python_modules

from opportunity_app.apply import agent as apply_agent
from opportunity_app.apply import agent_types
from opportunity_app.apply import ats as apply_ats
from opportunity_app.apply import lever, lever_adapter
from opportunity_app.apply.agent import ApplyAgent, GreenhouseAdapter
from opportunity_app.apply.lever_adapter import DENYLIST, PARSER_FIELDS, PARSER_PREFIX, LeverAdapter

ADAPTER_PATH = "apply/lever_adapter.py"
SITE, JOB = "tidewatergames", "6a1f0c52-9b3e-4d17-8c40-2e5d7a91b0f3"


def protocol_methods():
    return {name for name, value in vars(apply_ats.AtsAdapter).items() if callable(value) and not name.startswith("_")}


def protocol_attributes():
    return set(apply_ats.AtsAdapter.__annotations__)


def page_at(url, forms=0):
    page = mock.Mock(url=url)
    page.main_frame.locator.return_value.count.return_value = forms
    return page


class ShapeTests(unittest.TestCase):
    def test_it_has_every_method_the_protocol_names_with_the_same_parameters(self):
        def names(signature):
            return [name for name in signature.parameters if name != "self"]

        for name in sorted(protocol_methods()):
            with self.subTest(method=name):
                self.assertEqual(names(inspect.signature(getattr(LeverAdapter, name))), names(inspect.signature(getattr(apply_ats.AtsAdapter, name))))

    def test_it_and_greenhouses_adapter_have_every_attribute_the_protocol_names(self):
        for adapter in (LeverAdapter, GreenhouseAdapter):
            for name in sorted(protocol_attributes()):
                with self.subTest(adapter=adapter.__name__, attribute=name):
                    self.assertTrue(hasattr(adapter, name))

    def test_it_is_lever_with_the_form_kind_lever_names_and_a_scan_of_its_own(self):
        adapter = LeverAdapter()
        self.assertEqual((adapter.ats, adapter.form_page_kind), ("lever", "application_form"))
        self.assertEqual((adapter.uses_engine, adapter.closed_on_404, adapter.waits_for_challenge, adapter.required_from_load), (False, True, True, True))
        self.assertEqual(adapter.page_sentences, {"confirmation": "This page already says the application was submitted. The app did nothing."})
        self.assertEqual((GreenhouseAdapter.uses_engine, GreenhouseAdapter.closed_on_404, GreenhouseAdapter.waits_for_challenge, GreenhouseAdapter.required_from_load),
                         (True, False, False, False))
        self.assertEqual(GreenhouseAdapter.page_sentences, {})

    def test_it_reads_on_attach_and_never_uploads_on_attach_and_asks_the_page_nothing_to_say_so(self):
        frame = mock.Mock()
        adapter = LeverAdapter()
        self.assertIs(adapter.reads_on_attach(frame), True)
        self.assertIs(adapter.uploads_on_attach(frame), False)
        self.assertIs(adapter.security_code_prompt(frame), False)
        self.assertIsNone(adapter.security_code_inputs(frame))
        self.assertEqual(adapter.captcha_widget(frame), "")
        self.assertIs(adapter.is_react_select(frame, "location"), False)
        self.assertEqual(frame.mock_calls, [])

    def test_the_greenhouse_adapter_does_what_the_base_does_and_asks_nothing_new_of_a_page(self):
        adapter, frame = GreenhouseAdapter(), mock.Mock()
        self.assertEqual((adapter.page_facts(frame), adapter.page_managed(frame), adapter.owns("x"), adapter.is_typeahead(frame, "location")), ({}, {}, False, False))
        self.assertEqual((adapter.parse_state(frame), adapter.guessed_fields(frame), adapter.cleared(frame, "x"), adapter.refuses(mock.Mock())), ("", [], True, False))
        with self.assertRaises(NotImplementedError):
            adapter.scan(frame)
        self.assertEqual(frame.mock_calls, [])

    def test_the_posting_is_read_from_its_address_and_a_greenhouse_address_is_not_one(self):
        adapter = LeverAdapter()
        url = f"https://jobs.lever.co/{SITE}/{JOB}/apply?lever-source=x"
        self.assertEqual(adapter.posting_ids(url), (SITE, JOB))
        self.assertEqual(adapter.posting_ids(f"https://jobs.lever.co/{SITE.upper()}/{JOB}"), (SITE, JOB), "compared in lower case, as Greenhouse's are")
        self.assertEqual(adapter.confirmation_ids(f"https://jobs.eu.lever.co/Mixed-Case/{JOB}/thanks"), ("Mixed-Case", JOB), "the confirmation path is as the address writes it")
        for other in ("", "https://boards.greenhouse.io/acme/jobs/1", f"https://example-games.test/{SITE}/{JOB}", f"https://jobs.lever.co/{SITE}/not-a-uuid"):
            with self.subTest(url=other):
                self.assertEqual((adapter.posting_ids(other), adapter.confirmation_ids(other), adapter.loader_paths("<html></html>", other)), (("", ""), ("", ""), ("", "", "")))
        self.assertEqual(adapter.loader_paths("<html></html>", url), ("jobs.lever.co", f"/{SITE}/{JOB}/apply", f"/{SITE}/{JOB}/thanks"), "the page is not read: the form has no action")
        self.assertEqual(adapter.lookup_token(url), "")

    def test_a_page_is_a_form_a_confirmation_a_check_or_another_site(self):
        adapter = LeverAdapter()
        self.assertEqual(adapter.detect_page(page_at(f"https://jobs.lever.co/{SITE}/{JOB}/apply", forms=1)), "application_form")
        self.assertEqual(adapter.detect_page(page_at(f"https://jobs.eu.lever.co/{SITE}/{JOB}/thanks")), "confirmation")
        self.assertEqual(adapter.detect_page(page_at(f"https://jobs.lever.co/{SITE}/{JOB}/thanks/")), "confirmation")
        self.assertEqual(adapter.detect_page(page_at(f"https://jobs.lever.co/{SITE}/{JOB}/apply")), "challenge", "no form on a 200: Cloudflare's check, or one the app cannot tell from it")
        for url in ("https://careers.example-games.test/apply", "https://hire.lever.co/x", "https://boards.greenhouse.io/acme/jobs/1"):
            with self.subTest(url=url):
                self.assertEqual(adapter.detect_page(page_at(url, forms=1)), "offsite")

    def test_an_element_that_cannot_be_asked_about_is_not_pressed(self):
        adapter = LeverAdapter()
        locator = mock.Mock()
        locator.evaluate.side_effect = RuntimeError("detached")
        self.assertIs(adapter.refuses(locator), True)
        locator.evaluate.side_effect = None
        locator.evaluate.return_value = False
        self.assertIs(adapter.refuses(locator), False)
        locator.evaluate.return_value = True
        self.assertIs(adapter.refuses(locator), True)
        self.assertEqual(locator.evaluate.call_args[0][0], lever_adapter._DENIED)
        self.assertTrue(all(f"#{name}" in lever_adapter._DENIED for name in DENYLIST))

    def test_a_name_the_page_keeps_for_itself_is_one_of_its_hidden_fields_or_a_questions_description(self):
        adapter = LeverAdapter()
        for name in ("accountId", "linkedInData", "origin", "referer", "timezone", "socialReferralKey", "socialSource", "resumeStorageId", "h-captcha-response", "source",
                     "cards[6f1d2c3b-4a59][baseTemplate]", "surveysResponses[5e3f6d44][baseTemplate]", "surveysResponses[5e3f6d44][surveyId]",
                     "surveysResponses[5e3f6d44][candidateSelectedLocation]"):
            with self.subTest(name=name):
                self.assertTrue(adapter.owns(name))
        for name in ("name", "email", "location", "selectedLocation", "org", "urls[LinkedIn]", "cards[6f1d2c3b-4a59][field0]", "eeo[gender]", "resume", "comments"):
            with self.subTest(name=name):
                self.assertFalse(adapter.owns(name))

    def test_the_plans_eeo_names_map_back_to_the_pages_controls(self):
        control = mock.Mock()
        frame = mock.Mock()
        frame.locator.return_value = control
        adapter = LeverAdapter()
        adapter.control(frame, "veteran_status")
        adapter.control(frame, "eeo[disabilitySignature]")
        adapter.control(frame, 'cards[a"b][field0]')
        selectors = [call.args[0] for call in frame.locator.call_args_list]
        self.assertEqual(selectors, [
            'form#application-form [name="eeo[veteran]"]:not([type="hidden"])',
            'form#application-form [name="eeo[disabilitySignature]"]:not([type="hidden"])',
            'form#application-form [name="cards[a\\"b][field0]"]:not([type="hidden"])',
        ])


class ConstantsTests(unittest.TestCase):
    def test_the_fields_the_reader_may_fill_are_the_ones_the_pages_script_fills(self):
        """The list the app clears (spec 6.7, R4) is the reader's own: compared here with the stand-in script, which is written to the observed behaviour."""
        script = (apply_fake_ats.LEVER_FIXTURES / "parseResume.js").read_text(encoding="utf-8")
        listed = re.search(r"var PARSED_FIELDS = \[(.*?)\];", script, re.S)
        self.assertIsNotNone(listed)
        self.assertEqual(tuple(re.findall(r'"([^"]+)"', listed.group(1))), PARSER_FIELDS)
        self.assertEqual(re.search(r'var RESIDENTIAL = "([^"]+)";', script).group(1), PARSER_PREFIX)
        self.assertNotIn("selectedLocation", PARSER_FIELDS, "the page rewrites it on every read and empties it with the location field: it goes with location")

    def test_the_two_submit_controls_are_the_denylist(self):
        self.assertEqual(DENYLIST, ("btn-submit", "hcaptchaSubmitBtn"))

    def test_the_page_sets_resume_storage_id_itself_and_nothing_else_after_a_read(self):
        self.assertEqual(lever_adapter.PAGE_SETS, frozenset({"resumeStorageId"}))


# --- 10.5: static scans, through tests/helpers_source.py (a directory, never one file) ---------------------------------------------------

class StaticScanTests(unittest.TestCase):
    NEEDLES = ("btn-submit", "hcaptchaSubmitBtn")

    def test_the_two_submit_controls_are_named_in_apply_only_in_the_adapters_denylist(self):
        modules = helpers_source.apply_modules()
        self.assertIn(ADAPTER_PATH, modules)
        denylist = next(node for node in ast.walk(ast.parse(modules[ADAPTER_PATH])) if isinstance(node, ast.Assign)
                        and any(isinstance(target, ast.Name) and target.id == "DENYLIST" for target in node.targets))
        allowed = range(denylist.lineno, denylist.end_lineno + 1)
        found = []
        for relative, text in modules.items():
            for number, line in enumerate(text.splitlines(), 1):
                if any(needle in line for needle in self.NEEDLES) and not (relative == ADAPTER_PATH and number in allowed):
                    found.append(f"{relative}:{number}")
        self.assertEqual(found, [], "a Submit control of Lever's page is named outside the adapter's denylist")
        self.assertTrue(all(needle in modules[ADAPTER_PATH] for needle in self.NEEDLES), "the denylist names both")

    def test_the_adapter_presses_nothing_but_an_option_of_the_list_it_typed_into(self):
        tree = ast.parse(helpers_source.apply_modules()[ADAPTER_PATH])
        purposes = [call.args[1].value for call in ast.walk(tree)
                    if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "_click" and len(call.args) > 1]
        self.assertEqual(purposes, ["option_pick"])
        for module, text in helpers_source.apply_modules().items():
            if module != "apply/agent.py":
                self.assertNotIn("_click(", text.replace("ops._click(", ""), f"{module} presses something through another door")

    def test_a_request_continues_only_where_the_request_rules_judged_it(self):
        found = {}
        for relative, text in helpers_source.apply_modules().items():
            tree = ast.parse(text)
            parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
            for node in ast.walk(tree):
                if isinstance(node, ast.Attribute) and node.attr == "continue_":
                    scope = node
                    while scope in parents and not isinstance(scope, ast.FunctionDef):
                        scope = parents[scope]
                    found.setdefault(relative, set()).add(scope.name if isinstance(scope, ast.FunctionDef) else "<module>")
        self.assertEqual(found, {"apply/agent.py": {"_route", "_hand_over_and_continue"}})

    def test_the_adapter_builds_no_path_from_its_own_file(self):
        self.assertNotIn("__file__", helpers_source.apply_modules()[ADAPTER_PATH])

    def test_the_adapter_imports_no_playwright_and_none_of_the_agent(self):
        tree = ast.parse(helpers_source.apply_modules()[ADAPTER_PATH])
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                imported.add(("." * node.level) + (node.module or ""))
        self.assertFalse({name for name in imported if "playwright" in name or name in (".agent", ".runner", ".runs")}, imported)


# --- Reachable only in tests and the sandbox --------------------------------------------------------------------------------------------

class NotReachableInProductionTests(unittest.TestCase):
    def test_no_module_of_the_app_imports_the_lever_driver(self):
        importers = []
        for relative, text in python_modules("*.py").items():
            if relative == ADAPTER_PATH:
                continue
            tree = ast.parse(text)
            for node in ast.walk(tree):
                names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""] + [alias.name for alias in node.names] if isinstance(node, ast.ImportFrom) else []
                if any("lever_adapter" in name or name == "LeverAdapter" for name in names):
                    importers.append(relative)
        self.assertEqual(importers, [], "a module of the app reaches the Lever driver: LV4 adds the one place, and changes this test")

    def test_the_agent_factory_has_no_lever_adapter_and_builds_no_agent_for_it(self):
        self.assertEqual(set(apply_agent.ADAPTERS), {"greenhouse"})
        self.assertFalse(apply_ats.LEVER.adapter_built)
        with self.assertRaisesRegex(RuntimeError, "no driver for Lever"):
            apply_agent.DefaultApplyAgentFactory()(mode="handoff", run_id="r", screenshot_dir=None, timeouts=agent_types.ApplyTimeouts(),
                                                    on_progress=lambda *_: None, heartbeat=lambda: None, ats="lever")

    def test_the_runner_refuses_finish_in_browser_for_lever_before_it_reads_anything(self):
        self.assertEqual(apply_ats.mode_refusal(apply_ats.LEVER, "handoff"), ("ats_not_built", "Finish in browser for Lever postings is not available yet"))

    def test_the_agent_runs_lever_in_finish_in_browser_only(self):
        for mode in ("lookup", "rehearse", "submit"):
            with self.subTest(mode=mode):
                result = ApplyAgent(mode=mode, adapter=LeverAdapter()).run(FakePlan([]), page_url=f"https://jobs.lever.co/{SITE}/{JOB}/apply", schema=[], files={})
                self.assertEqual((result.outcome, result.reasons), ("failed", [apply_agent.NOT_BUILT]))


# --- The names this run's own request rules reach ------------------------------------------------------------------------------------------

class ResolvableForARunTests(unittest.TestCase):
    LEVER_ONLY = ("jobs.lever.co", "jobs.eu.lever.co", "js.hcaptcha.com", "api.hcaptcha.com", "api2.hcaptcha.com", "hcaptcha.com", "newassets.hcaptcha.com",
                  "cdn.lever.co", "lever-client-logos.s3.amazonaws.com")
    GREENHOUSE_ONLY = ("job-boards.greenhouse.io", "boards.greenhouse.io", "api-geocode-earth-proxy.greenhouse.io", "recruiting.cdn.greenhouse.io",
                       "s12-recruiting.cdn.greenhouse.io", "www.gstatic.com", "www.recaptcha.net")

    def test_a_lever_run_reaches_lever_and_its_hcaptcha_and_the_fonts_and_nothing_of_greenhouses(self):
        agent = ApplyAgent(mode="handoff", adapter=LeverAdapter())
        for host in (*self.LEVER_ONLY, "fonts.googleapis.com", "fonts.gstatic.com"):
            with self.subTest(host=host):
                self.assertTrue(agent._resolvable(host))
        for host in (*self.GREENHOUSE_ONLY, "bugs.lever.co", "www.linkedin.com", "lever.co", "shard.w.hcaptcha.com", "challenges.cloudflare.com", "127.0.0.1", ""):
            with self.subTest(host=host):
                self.assertFalse(agent._resolvable(host))

    def test_a_greenhouse_run_reaches_none_of_the_names_only_lever_needs(self):
        agent = ApplyAgent(mode="handoff", adapter=GreenhouseAdapter())
        for host in (*self.GREENHOUSE_ONLY, "fonts.googleapis.com", "fonts.gstatic.com"):
            with self.subTest(host=host):
                self.assertTrue(agent._resolvable(host))
        for host in self.LEVER_ONLY:
            with self.subTest(host=host):
                self.assertFalse(agent._resolvable(host))

    def test_the_browsers_one_resolver_rule_holds_the_names_of_both(self):
        for host in (*self.LEVER_ONLY, *(name for name in self.GREENHOUSE_ONLY if "?" not in name and not name.startswith("s12"))):
            self.assertIn(host, apply_agent.RESOLVABLE_HOSTS)


class ParserValuesTests(unittest.TestCase):
    def test_only_the_readers_fields_and_the_hidden_location_are_kept_and_the_script_takes_nothing_from_the_app(self):
        frame = mock.Mock()
        frame.evaluate.return_value = [
            ["name", "Sam Rivera"], ["org", "A"], ["location", "B"], ["selectedLocation", "{}"], ["urls[GitHub]", "g"], ["residentialLocation[city]", "c"],
            ["comments", "free text"], ["cards[x][field0]", "answer"],
        ]
        found = LeverAdapter().parser_values(frame)
        self.assertEqual(sorted(found), ["location", "name", "org", "residentialLocation[city]", "selectedLocation", "urls[GitHub]"])
        frame.evaluate.assert_called_once_with(lever_adapter.LEVER_VALUES)

    def test_a_page_without_the_form_has_no_values(self):
        frame = mock.Mock()
        frame.evaluate.return_value = []
        self.assertEqual(LeverAdapter().parser_values(frame), {})


# --- The student's choice of a file, as the press listener reports it ---------------------------------------------------------------------------

class StudentFileSignalTests(unittest.TestCase):
    SHA = "ab" * 32

    def agent(self, adapter=None, phase=None):
        agent = ApplyAgent(mode="handoff", adapter=adapter or LeverAdapter())
        if phase:
            agent._phase = phase
        return agent

    @staticmethod
    def call(agent, payload):
        agent._on_binding({"name": apply_agent.PRESS_BINDING, "payload": payload})

    def test_a_choice_in_the_students_turn_lets_one_read_pass_and_its_hash_follows(self):
        agent = self.agent(phase=apply_agent.PHASE_STUDENT)
        self.call(agent, "file")
        self.assertEqual((agent._state.student_files_chosen, agent._chosen_digests), (1, [None]))
        self.call(agent, f"sha:{self.SHA}")
        self.assertEqual(agent._chosen_digests, [self.SHA])
        self.call(agent, "file")
        self.call(agent, "sha:")
        self.assertEqual((agent._state.student_files_chosen, agent._chosen_digests), (2, [self.SHA, ""]))

    def test_a_choice_before_the_turn_or_after_the_hand_over_or_on_another_ats_counts_for_nothing(self):
        for label, agent in (
            ("the app's fill", self.agent(phase=apply_agent.PHASE_FILL)),
            ("after the hand-over", self.agent(phase=apply_agent.PHASE_AFTER_HAND_OVER)),
            ("Greenhouse", self.agent(GreenhouseAdapter(), phase=apply_agent.PHASE_STUDENT)),
        ):
            with self.subTest(case=label):
                self.call(agent, "file")
                self.assertEqual((agent._state.student_files_chosen, agent._chosen_digests), (0, []))

    def test_anything_but_the_listeners_three_words_is_ignored(self):
        agent = self.agent(phase=apply_agent.PHASE_STUDENT)
        for payload in ("", "File", "file ", "sha:xyz", f"sha:{self.SHA.upper()}", f"sha:{self.SHA}0", 1, None, {"file": 1}):
            self.call(agent, payload)
        self.assertEqual((agent._state.student_files_chosen, agent._chosen_digests), (0, []))
        agent._on_binding({"name": "somebodyElse", "payload": "file"})
        self.assertEqual(agent._state.student_files_chosen, 0)

    def test_the_listener_runs_on_both_boards_and_counts_only_a_trusted_change_of_a_file_box_in_the_application_form(self):
        source = apply_agent.PRESS_LISTENER
        for host in (*lever.LEVER_HOSTS, "job-boards.greenhouse.io", "boards.greenhouse.io"):
            self.assertIn(f'"{host}"', source)
        self.assertEqual(source.count("isTrusted"), 2, "each of the two kinds of event is checked for the browser's own mark")
        self.assertIn("box.type === 'file'", source)
        self.assertIn("form#application-form", source)

    def test_an_unasked_read_waits_no_grace_when_it_cannot_be_the_students(self):
        for agent in (self.agent(phase=apply_agent.PHASE_FILL), self.agent(GreenhouseAdapter(), phase=apply_agent.PHASE_STUDENT)):
            started = time.monotonic()
            self.assertFalse(agent._file_choice_arrives())
            self.assertLess(time.monotonic() - started, apply_agent.PRESS_GRACE_S / 2)


# --- The field a refusal names ------------------------------------------------------------------------------------------------------------------

class FirstErrorTests(unittest.TestCase):
    """After a 4xx the run names the first field the PAGE marks invalid, not the first one the browser finds empty (docs/phase5-lever-handoff-spec.md 6.13)."""

    def named(self, invalid):
        agent = ApplyAgent(mode="handoff", adapter=LeverAdapter())
        frame = mock.Mock()
        frame.evaluate.return_value = {"invalid": invalid}
        return agent._first_error_question(frame)

    def test_a_field_the_page_marked_wins_over_an_earlier_one_that_is_only_empty(self):
        found = self.named([
            {"key": "name", "question": "Full name", "reason": "required and empty"},
            {"key": "email", "question": "Email", "reason": "marked invalid by the form"},
        ])
        self.assertEqual(found, "Email")

    def test_with_nothing_marked_the_first_the_browser_finds_invalid_is_named_as_before(self):
        self.assertEqual(self.named([{"key": "name", "question": "Full name", "reason": "required and empty"}]), "Full name")

    def test_nothing_invalid_names_nothing(self):
        self.assertEqual(self.named([]), "")

    def test_the_reason_the_agent_looks_for_is_the_one_the_independent_check_gives(self):
        from opportunity_app.apply import checks

        self.assertIn(f'"{apply_agent.MARKED_BY_PAGE}"', checks.REQUIRED_CHECK_SCRIPT)


class WordsOnceTheFileHasGoneTests(unittest.TestCase):
    def agent(self, sent):
# --- The words once the file has gone ---------------------------------------------------------------------------------------------------------

        agent = ApplyAgent(mode="handoff", adapter=LeverAdapter())
        agent._reads = True
        agent._state.resume_posts_passed = 1 if sent else 0
        return agent

    SENTENCES = [
        agent_types.HANDOFF_NOT_SUBMITTED, agent_types.HANDOFF_UNRECORDED, agent_types.HANDOFF_ELSEWHERE, agent_types.HANDOFF_EARLY, agent_types.HANDOFF_S3,
        agent_types.HANDOFF_UPLOAD, agent_types.HANDOFF_HIDDEN.format(question="Q"), agent_types.HANDOFF_UNPLANNED_FILE, agent_types.HANDOFF_UNPLANNED_SEND,
        agent_types.WINDOW_CLOSED, agent_types.STOPPED, apply_agent.CHALLENGE_UNFINISHED.format(ats="Lever"),
    ]

    def test_with_no_file_sent_every_sentence_is_as_it_was(self):
        agent = self.agent(sent=False)
        for sentence in self.SENTENCES:
            with self.subTest(sentence=sentence):
                self.assertEqual(agent._once_the_file_has_gone(sentence), sentence)

    def test_once_lever_has_the_file_every_sentence_that_said_nothing_was_sent_says_the_application_was_not_sent_and_who_has_the_file(self):
        agent = self.agent(sent=True)
        for sentence in self.SENTENCES:
            with self.subTest(sentence=sentence):
                said = agent._once_the_file_has_gone(sentence)
                self.assertIn("Your application was not sent. Lever received your résumé.", said)
                self.assertNotIn("Nothing was sent", said)
                self.assertNotIn("No application was sent", said)

    def test_a_sentence_already_said_that_way_is_not_said_twice(self):
        agent = self.agent(sent=True)
        once = agent._once_the_file_has_gone(agent_types.HANDOFF_NOT_SUBMITTED)
        self.assertEqual(agent._once_the_file_has_gone(once), once)

    def test_a_sentence_that_makes_no_such_claim_is_left_alone(self):
        agent = self.agent(sent=True)
        for sentence in ("Lever did not finish reading your résumé. Nothing was filled. Lever may still have the file.", apply_agent.FIELD_TOOK.format(question="Q")):
            self.assertEqual(agent._once_the_file_has_gone(sentence), sentence)

    def test_an_agent_for_a_page_that_reads_nothing_never_changes_a_sentence(self):
        agent = ApplyAgent(mode="handoff", adapter=GreenhouseAdapter())
        agent._state.resume_posts_passed = 1
        self.assertEqual(agent._once_the_file_has_gone(agent_types.HANDOFF_ELSEWHERE), agent_types.HANDOFF_ELSEWHERE)


# --- The agent's steps for a page that reads an attached file, with the page stood in for -----------------------------------------------------------

class StandInAdapter(LeverAdapter):
    """The Lever adapter with the page's answers set by the test: what the reader shows, which fields it may have filled, and whether each is empty."""

    def __init__(self, states=("success",), guessed=(), held=()):
        self.states = list(states)
        self.guessed = list(guessed)
        self.held = set(held)       # the keys that hold a value
        self.log = []

    def parse_state(self, frame):
        self.log.append("parse_state")
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]

    def guessed_fields(self, frame):
        return list(self.guessed)

    def cleared(self, frame, key):
        return key not in self.held

    def control(self, frame, key):
        return mock.Mock(first=key)


class AgentStepsTests(unittest.TestCase):
    def agent(self, adapter, plan=None, **timeouts):
        agent = ApplyAgent(mode="handoff", adapter=adapter, timeouts=agent_types.ApplyTimeouts(parse_s=timeouts.get("parse_s", 0.6), heartbeat_s=60))
        agent._plan = FakePlan(plan or [])
        agent._page = mock.Mock()
        agent._page.is_closed.return_value = False
        agent._poll = lambda milliseconds, step: (time.sleep(milliseconds / 1000), True)[1]
        agent._reads = True
        return agent

    def test_the_wait_for_the_read_ends_on_success_failure_or_oversize_and_not_on_working_or_nothing_yet(self):
        for end in ("success", "failure", "oversize"):
            with self.subTest(end=end):
                adapter = StandInAdapter(states=("", "working", "working", end))
                self.assertEqual(self.agent(adapter)._wait_for_parse(mock.Mock()), end)
                self.assertEqual(adapter.log.count("parse_state"), 4)

    def test_a_read_that_does_not_end_in_time_stops_the_run_before_anything_is_touched_and_says_so(self):
        agent = self.agent(StandInAdapter(states=("working",)), parse_s=0.3)
        with self.assertRaises(apply_agent._Stop) as caught:
            agent._wait_for_parse(mock.Mock())
        self.assertEqual((caught.exception.outcome, caught.exception.reason, caught.exception.end),
                         ("needs_you", "Lever did not finish reading your résumé. Nothing was filled. Lever may still have the file.", "parse"))
        self.assertEqual(agent._parse_result, "timeout")

    def test_a_page_that_cannot_answer_for_a_moment_is_still_reading(self):
        class Flaky(StandInAdapter):
            def parse_state(self, frame):
                self.log.append("x")
                if len(self.log) < 3:
                    raise RuntimeError("the page is changing")
                return "success"

        self.assertEqual(self.agent(Flaky())._wait_for_parse(mock.Mock()), "success")

    def test_every_field_the_reader_may_have_filled_that_the_plan_does_not_fill_is_emptied_and_listed_and_the_rest_are_left(self):
        plan = [planned("name", "Full name", "Sam Rivera"), planned("org", "Current company", None, disposition="blank", source="none"),
                planned("phone", "Phone", None, disposition="left_for_you", source="none")]
        adapter = StandInAdapter(guessed=["name", "org", "phone", "urls[Quora]", "email"], held={"name", "org", "phone", "urls[Quora]"})
        agent = self.agent(adapter, plan)
        emptied = []

        def type_empty(locator, value, key, **more):
            emptied.append((key, value))
            adapter.held.discard(key)

        agent._type = type_empty
        agent._between = lambda: None
        agent._clear_guesses(mock.Mock())
        self.assertEqual(emptied, [("org", ""), ("phone", ""), ("urls[Quora]", "")], "the plan's own name is not touched, and an empty email is left alone")
        self.assertEqual(agent._guesses_cleared, ["org", "phone", "urls[Quora]"])
        left = {item["key"]: item["reason"] for item in agent._left_items()}
        self.assertEqual(set(left), {"org", "phone", "urls[Quora]"})
        self.assertTrue(all(reason.startswith("Lever filled this from your résumé") for reason in left.values()))

    def test_a_field_that_will_not_empty_stops_the_run_and_names_it(self):
        adapter = StandInAdapter(guessed=["org"], held={"org"})
        agent = self.agent(adapter, [planned("org", "Current company", None, disposition="blank", source="none")])
        agent._type = lambda locator, value, key, **more: None
        agent._between = lambda: None
        with self.assertRaises(apply_agent._Stop) as caught:
            agent._clear_guesses(mock.Mock())
        self.assertEqual((caught.exception.outcome, caught.exception.reason), ("needs_you", 'The field "Current company" did not take the answer'))
        self.assertEqual(agent._guesses_cleared, [])

    def test_a_question_the_page_stopped_marking_required_once_a_box_was_ticked_is_still_checked_as_required(self):
        agent = self.agent(LeverAdapter())
        agent._items_at_load = [
            {"key": "g1", "question": "Certify", "markers": ["attr"], "kind": "checkbox", "value_text": [], "empty": True},
            {"key": "g2", "question": "Other", "markers": ["attr"], "kind": "checkbox", "value_text": [], "empty": True},
            {"key": "t1", "question": "Why", "markers": ["attr"], "kind": "text", "value_text": "", "empty": True},
        ]
        seen = {
            "items": [{"key": "t1", "question": "Why", "markers": ["attr"], "kind": "text", "value_text": "x", "empty": False}],
            "controls": [
                {"key": "g1", "kind": "checkbox", "checked": True, "value_text": "I certify"},
                {"key": "g1", "kind": "checkbox", "checked": False, "value_text": ""},
                {"key": "g2", "kind": "checkbox", "checked": False, "value_text": "Some"},
            ],
        }
        items = {item["key"]: item for item in agent._items_to_check(seen)}
        self.assertEqual(set(items), {"g1", "g2", "t1"})
        self.assertEqual((items["g1"]["value_text"], items["g1"]["empty"]), (["I certify"], False))
        self.assertEqual((items["g2"]["value_text"], items["g2"]["empty"]), ([], True), "the other group is still required and still empty")
        self.assertEqual(items["t1"]["value_text"], "x", "an item the page still marks is as the page says")
        greenhouse = ApplyAgent(mode="handoff", adapter=GreenhouseAdapter())
        greenhouse._items_at_load = agent._items_at_load
        self.assertEqual([item["key"] for item in greenhouse._items_to_check(seen)], ["t1"], "a page whose boxes keep their marks is read as it is")


if __name__ == "__main__":
    unittest.main()
