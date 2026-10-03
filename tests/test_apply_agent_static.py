"""The apply agent, read as text and as a Python object, with no browser (the default unit suite).

The agent may change a page in five places and nowhere else, and it may launch Chromium in exactly one way. Both are
promises a browser test cannot keep: a browser test sees what a run did, not what a later edit could make it do. So the
guards here read the source of every Apply for me module, at any depth (tests/helpers_source.py), and fail when a call
that clicks, types, ticks, focuses, drags or attaches a file appears anywhere but inside ``_type``, ``_tick``, ``_choose``,
``_attach`` or ``_click``, or when a string handed to the page could submit a form. The scan is a list of spellings, so
it cannot be complete: ``MutationTests`` below put each spelling it claims to catch into a copy of the agent and check
the scan fails on it, which is what keeps the claim honest.

What the agent does with a real page, a real request and a real Chromium is in tests/test_apply_agent_browser.py.
"""

import ast
import inspect
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import realdata_guard

realdata_guard.install()

import apply_fake_ats
import helpers_source
from helpers_apply import FakePlan, planned

from opportunity_app import ROOT
from opportunity_app.apply import agent as apply_agent
from opportunity_app.apply import agent_types as apply_agent_types
from opportunity_app.apply import checks as apply_checks
from opportunity_app.apply import policy as apply_policy
from opportunity_app.apply.agent import (
    CLICK_PURPOSES,
    DENYLIST,
    ENGINE_FILES,
    ENGINE_SOURCE,
    NOT_BUILT,
    NOT_BOARD,
    ApplyAgent,
    DefaultApplyAgentFactory,
    GreenhouseAdapter,
    bind_endpoints,
    board_token,
)
from opportunity_app.apply.agent_types import BUILT_MODES, ApplyTimeouts
from opportunity_app.apply.checks import REQUIRED_CHECK_SCRIPT, Endpoint
from opportunity_app.apply.greenhouse import BOARD_HOSTS

AGENT_PATH = "apply/agent.py"
MUTATORS = ("_type", "_tick", "_choose", "_attach", "_click")
# The ways to change or move a page that Playwright's Locator, Frame and Page offer, as they would be written in Python.
# (``dblclick`` and ``uncheck`` are listed beside ``click`` and ``check``: a dot must come right before the name.)
CHANGERS = re.compile(
    r"\.(?:dbl)?click\s*\(|\.(?:un)?check\s*\(|\.set_checked\s*\(|\.select_option\s*\(|\.fill\s*\(|\.press\s*\(|\.tap\s*\(|\.dispatch_event\s*\(|keyboard\.|mouse\."
    r"|set_input_files\s*\(|\.type\s*\(|press_sequentially\s*\(|\.drag_to\s*\(|drag_and_drop\s*\(|\.hover\s*\(|\.focus\s*\(|\.blur\s*\(|\.select_text\s*\(|\.clear\s*\("
    # Moving a page, or putting a script or content into it, changes it as surely as typing does, and skips ``_begin``'s phase switch.
    r"|\.goto\s*\(|\.reload\s*\(|\.set_content\s*\(|\.add_script_tag\s*\(|\.add_style_tag\s*\(|\.go_back\s*\(|\.go_forward\s*\("
)
# Receivers that are not Playwright objects, by "module: the line's text". Each has the reason it is allowed.
NOT_PLAYWRIGHT = {
    ("web/routers/apply_agent.py", "apply_preflight.check("): "the read-only preflight of a saved role, a Python function that opens no browser",
}
# The one navigation an agent makes: to the posting's own address, which ``route_decision`` then judges. Line text, by module.
SANCTIONED = {
    ("apply/agent.py", "page.goto(page_url, wait_until=\"domcontentloaded\""): "_open: the Greenhouse posting the run was asked to open",
}
FORBIDDEN_IN_STRINGS = (
    "press(\"Enter\")", "keyboard.press", "requestSubmit", ".submit(", "new MouseEvent", "new PointerEvent", "dispatchEvent", "KeyboardEvent",
    "SubmitEvent", "new Event('submit'", 'new Event("submit"', ".prototype.submit", "form.submit",
    # A script of the page's own that sends something or moves the page (the route handler sees a fetch, but a script handed to the page must
    # still never be one that sends a value, and a beacon or a navigation is not the agent's to start).
    "sendBeacon", "XMLHttpRequest", "location.assign", "location.replace", "location.href =", "window.open",
)
# Spellings that need a pattern: a space before a bracket, and a script that writes a value, a tick or a file into the page. A script handed
# to the page may read it and scroll it; it never types, ticks, clicks or sets anything, which would skip the five helpers and their phase switch.
FORBIDDEN_PATTERNS = (
    re.compile(r"\.click\b|\[\s*['\"]click['\"]\s*\]"),   # e.click (), e['click'](), HTMLElement.prototype.click.call(e)
    # A fetch of the page's own. Prose such as "the schema fetch (apply_schema_client)" is not one.
    re.compile(r"(?<![\w.])fetch\(|(?<![\w.])fetch\s+\(\s*['\"`]"),
    re.compile(r"\.(?:value|checked|selectedIndex|selected|files|innerHTML|outerHTML|innerText|textContent)\s*(?:=(?!=)|\+=)"),
    re.compile(r"\bsetAttribute\s*\(|\bsetRangeText\s*\(|\bsetSelectionRange\s*\(|\bexecCommand\s*\(|\.stepUp\s*\(|\.stepDown\s*\("),
    re.compile(r"\.(?:focus|select|blur)\s*\(\s*\)"),
)
EVALUATE_METHODS = frozenset({"evaluate", "evaluate_all", "evaluate_handle"})
FIXTURE = apply_fake_ats.FIXTURES / "endpoints.json"


def unauthorised_changers(modules: dict[str, str]) -> tuple[list[str], int]:
    """(where a page could be changed outside the five helpers, how many changing calls were seen in all)."""
    allowed = mutator_lines(modules[AGENT_PATH])
    found: list[str] = []
    seen = 0
    for relative, text in modules.items():
        for number, line in enumerate(text.splitlines(), 1):
            if not CHANGERS.search(line):
                continue
            seen += 1
            if relative == AGENT_PATH and number in allowed:
                continue
            if any(module == relative and needle in line for (module, needle) in NOT_PLAYWRIGHT):
                continue
            if any(module == relative and needle in line for (module, needle) in SANCTIONED):
                continue
            found.append(f"{relative}:{number} {line.strip()}")
    return found, seen


def forbidden_strings(modules: dict[str, str]) -> list[str]:
    """Every string constant of every Apply for me module (and the required-field check script) that holds a way to submit or press."""
    strings = [REQUIRED_CHECK_SCRIPT]
    for text in modules.values():
        strings.extend(node.value for node in ast.walk(ast.parse(text)) if isinstance(node, ast.Constant) and isinstance(node.value, str))
    found = [f"{needle!r} in {value[:60]!r}" for needle in FORBIDDEN_IN_STRINGS for value in strings if needle in value and not _blocker_mention(needle, value)]
    found.extend(f"{pattern.pattern!r} in {value[:60]!r}" for pattern in FORBIDDEN_PATTERNS for value in strings if pattern.search(value))
    return found


# The init script removes beacons: the one string that may name sendBeacon, and only to replace it with a function that returns false.
BEACON_BLOCKER = "'sendBeacon', function () { return false; }"


def _blocker_mention(needle: str, value: str) -> bool:
    """Whether ``needle`` in ``value`` is only the init script naming the beacon it replaces (and nothing in it sends one)."""
    return needle == "sendBeacon" and value == apply_agent.NO_SIDE_CHANNELS and value.count("sendBeacon") == 1 and BEACON_BLOCKER in value


def _is_constant_script(node: ast.AST) -> bool:
    """A script handed to the page: a string written in the source, or a module constant (an all-capitals name, as _SCAN and ENGINE_SOURCE are)."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    return isinstance(node, ast.Name) and re.fullmatch(r"_?[A-Z][A-Z0-9_]*", node.id) is not None


def _is_empty_scan_input(node: ast.AST) -> bool:
    """The only dict the agent hands the page: {"profile": {}, "answers": []}, the scan's structure-only input."""
    if not isinstance(node, ast.Dict) or len(node.keys) != 2:
        return False
    items = {key.value: value for key, value in zip(node.keys, node.values) if isinstance(key, ast.Constant)}
    return (
        set(items) == {"profile", "answers"}
        and isinstance(items["profile"], ast.Dict) and not items["profile"].keys
        and isinstance(items["answers"], ast.List) and not items["answers"].elts
    )


def _is_element_handle(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "element_handle" and not node.args


def unsafe_evaluate_calls(modules: dict[str, str]) -> tuple[list[str], int]:
    """(every ``evaluate`` call whose script is not a constant or whose argument could carry a student value, how many were seen).

    The page's JavaScript gets the script, and at most an element the agent found or the scan's empty profile and answers. A value
    handed over (``locator.evaluate(script, value)``) would put a student's text in the page without ``_begin`` ever moving the phase.
    """
    found: list[str] = []
    seen = 0
    for relative, text in modules.items():
        for node in ast.walk(ast.parse(text)):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in EVALUATE_METHODS):
                continue
            seen += 1
            problems = []
            if not node.args or not _is_constant_script(node.args[0]):
                problems.append("its script is not a constant")
            for argument in [*node.args[1:], *(keyword.value for keyword in node.keywords)]:
                if not (isinstance(argument, ast.Constant) or _is_empty_scan_input(argument) or _is_element_handle(argument)):
                    problems.append("it hands the page something that may be a value")
            if problems:
                found.append(f"{relative}:{node.lineno} " + "; ".join(problems))
    return found, seen


# Playwright can send traffic that ``context.route`` never sees (``page.request`` and its kin are the driver's own HTTP client, and
# ``route.fetch`` is too), rewrite a request after ``route_decision`` approved it (``continue_`` with a URL, a body or headers), or put a value
# on every later request (``set_extra_http_headers``). None of them is on the list of page-changing calls above, so each is its own rule here,
# read from the syntax tree: a regular expression would not see ``getattr(page, "request")`` or a space before the bracket.
FORBIDDEN_DRIVER_ATTRS = frozenset({
    "fetch", "set_extra_http_headers", "expose_function", "expose_binding", "route_from_har", "fulfill", "fallback",
})
# Who may use a name from that list, by (module, name, receiver). Each has its reason.
ALLOWED_DRIVER_ATTRS = {
    ("apply/preflight.py", "fetch", "client"): "the schema client's read of Greenhouse's public listing: Python's urllib, no browser",
}
# ``.request`` on a receiver that is not Playwright, by (module, receiver name): each sends from Python, never from the browser.
ALLOWED_REQUEST_RECEIVERS = {
    ("apply/security_code.py", "gmail"): "the Gmail REST client (integrations.gmail_client over httpx) reading Greenhouse's security-code email",
}
# The one function of the agent that may call each of Playwright's ways to start a browser or a browser context.
LAUNCHERS = {"launch": "_launch_browser", "new_context": "_new_context", "launch_persistent_context": "", "connect_over_cdp": "", "new_browser_context": ""}
# ``getattr`` with a name that is not written in the source, by (module, enclosing function): reading a plan or a schema field by attribute.
DYNAMIC_GETATTR = {
    ("apply/agent.py", "_attr"): "reads a plan entry or a schema field by name",
    ("apply/checks.py", "_get"): "reads a plan entry, a schema field or a run by name",
    ("apply/policy.py", "with_page_labels"): "reads a scan control by name",
}
PAGE_CALL_NAMES = frozenset({
    "click", "dblclick", "fill", "type", "press", "check", "uncheck", "set_checked", "select_option", "tap", "dispatch_event", "hover", "focus", "blur", "clear",
    "drag_to", "set_input_files", "press_sequentially", "select_text", "goto", "reload", "set_content", "add_script_tag", "add_style_tag", "go_back", "go_forward",
    "request", "add_init_script", "route", "route_web_socket",
}) | FORBIDDEN_DRIVER_ATTRS


def _enclosing_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    return ""


def unsafe_driver_calls(modules: dict[str, str]) -> tuple[list[str], int]:
    """(every use of Playwright that could send a request ``route_decision`` did not judge or change one after it did, how many uses were seen)."""
    found: list[str] = []
    seen = 0
    init_scripts = 0
    for relative, text in modules.items():
        tree = ast.parse(text)
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            where = f"{relative}:{getattr(node, 'lineno', 0)}"
            if isinstance(node, ast.Attribute):
                receiver = node.value.id if isinstance(node.value, ast.Name) else ""
                name = node.attr
                if name == "request":
                    seen += 1
                    if receiver not in ("route", "urllib") and (relative, receiver) not in ALLOWED_REQUEST_RECEIVERS:
                        found.append(f"{where} .request on something that is not the route's own request: an APIRequestContext sends from the driver")
                elif name in FORBIDDEN_DRIVER_ATTRS:
                    seen += 1
                    if (relative, name, receiver) not in ALLOWED_DRIVER_ATTRS:
                        found.append(f"{where} .{name}: sends, answers or changes a request outside route_decision")
                elif name in LAUNCHERS:
                    seen += 1
                    if relative != AGENT_PATH or _enclosing_function(node, parents) != LAUNCHERS[name]:
                        found.append(f"{where} .{name} outside {LAUNCHERS[name] or 'any function'}")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                attr = node.func.attr
                if attr == "continue_":
                    seen += 1
                    if node.args or node.keywords:
                        found.append(f"{where} continue_ with arguments rewrites a request after route_decision approved it")
                elif attr == "add_init_script":
                    seen += 1
                    init_scripts += 1
                    only = len(node.args) == 1 and not node.keywords and isinstance(node.args[0], ast.Name) and node.args[0].id == "NO_SIDE_CHANNELS"
                    if not only or relative != AGENT_PATH:
                        found.append(f"{where} add_init_script of anything but NO_SIDE_CHANNELS")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) >= 2:
                seen += 1
                name = node.args[1]
                if isinstance(name, ast.Constant) and isinstance(name.value, str):
                    if name.value in PAGE_CALL_NAMES:
                        found.append(f"{where} getattr(..., {name.value!r}) reaches a call the scans above look for by its spelling")
                elif (relative, _enclosing_function(node, parents)) not in DYNAMIC_GETATTR:
                    found.append(f"{where} getattr with a name that is not written in the source")
    if init_scripts > 1:
        found.append(f"{init_scripts} add_init_script calls: there is one, and it is NO_SIDE_CHANNELS")
    return found, seen


def forbidden_top_imports(text: str) -> list[str]:
    """Imports at the top of a module that name the policy, the preflight or the web app, however they are spelled."""
    found = []
    for node in ast.parse(text).body:
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            names = [base, *(f"{base}.{alias.name}" for alias in node.names)]
        else:
            continue
        for name in names:
            if {"policy", "preflight", "web"} & set(name.split(".")):
                found.append(name)
    return found


def mutate_agent(modules: dict[str, str], snippet: str, *, as_code: bool) -> dict[str, str]:
    """A copy of the modules with ``snippet`` added to the agent: as a statement in a new method, or as a string constant in it."""
    body = snippet if as_code else f"return {snippet!r}"
    mutated = modules[AGENT_PATH].replace("    def _closed(self) -> bool:", f"    def _sneak(self):\n        {body}\n\n    def _closed(self) -> bool:", 1)
    assert mutated != modules[AGENT_PATH]
    return {**modules, AGENT_PATH: mutated}


def mutator_lines(text: str) -> set[int]:
    """The line numbers inside the bodies of ApplyAgent's five mutation helpers."""
    tree = ast.parse(text)
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ApplyAgent":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name in MUTATORS:
                    lines.update(range(item.lineno, item.end_lineno + 1))
    return lines


class OnlyFiveHelpersChangeAPage(unittest.TestCase):
    def test_every_click_keystroke_and_file_input_is_inside_one_of_the_five_helpers(self):
        modules = helpers_source.apply_modules()
        self.assertIn(AGENT_PATH, modules)
        self.assertTrue(mutator_lines(modules[AGENT_PATH]), "the five helpers were not found in apply/agent.py")
        found, seen = unauthorised_changers(modules)
        self.assertEqual(found, [], "a call that can change a page, outside the five helpers")
        self.assertGreaterEqual(seen, 6, "the scan found nothing to scan")

    def test_the_five_helpers_are_methods_of_the_agent_and_no_other_function_in_any_apply_module_has_their_names(self):
        for name in MUTATORS:
            self.assertTrue(callable(getattr(ApplyAgent, name)), name)
        owners: dict[str, list[str]] = {name: [] for name in MUTATORS}
        for relative, text in helpers_source.apply_modules().items():
            tree = ast.parse(text)
            parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in owners:
                    parent = parents.get(node)
                    owner = parent.name if isinstance(parent, ast.ClassDef) else "<module or function>"
                    owners[node.name].append(f"{relative}:{owner}")
        self.assertEqual(owners, {name: ["apply/agent.py:ApplyAgent"] for name in MUTATORS})

    def test_no_apply_module_sends_a_request_route_decision_did_not_judge_or_changes_one_after_it_did(self):
        found, seen = unsafe_driver_calls(helpers_source.apply_modules())
        self.assertEqual(found, [], "traffic the route handler never sees, or a request rewritten after it was approved")
        self.assertGreaterEqual(seen, 8, "the scan found nothing to scan")

    def test_the_guard_s_own_allowances_are_all_in_use(self):
        modules = helpers_source.apply_modules()
        for (relative, name, receiver) in ALLOWED_DRIVER_ATTRS:
            self.assertRegex(modules[relative], rf"\b{receiver}\.{name}\b", f"{relative} no longer uses {receiver}.{name}: drop its entry")
        for (relative, function) in DYNAMIC_GETATTR:
            self.assertRegex(modules[relative], rf"def {function}\b", f"{relative} has no {function}: drop its entry")

    def test_nothing_hands_the_page_a_value_and_every_script_it_runs_is_written_in_the_source(self):
        found, seen = unsafe_evaluate_calls(helpers_source.apply_modules())
        self.assertEqual(found, [])
        self.assertGreaterEqual(seen, 8, "the scan found nothing to scan")

    def test_the_scan_is_given_an_empty_profile_and_no_answers_and_nothing_else(self):
        agent = ApplyAgent(mode="rehearse", adapter=GreenhouseAdapter())
        frame = mock.Mock()
        frame.evaluate.return_value = {"fields": [{"name": "first_name"}]}
        self.assertEqual(agent._scan(frame), [{"name": "first_name"}])
        (script, argument), _ = frame.evaluate.call_args
        self.assertEqual((script, argument), (apply_agent._SCAN, {"profile": {}, "answers": []}))

    def test_no_string_of_any_apply_module_or_the_check_script_can_submit_or_press_anything(self):
        modules = helpers_source.apply_modules()
        self.assertGreater(len(modules), 8, "every Apply for me module is scanned, not the agent alone")
        self.assertEqual(forbidden_strings(modules), [])

    def test_the_engine_the_agent_injects_never_submits_or_dispatches_a_pointer_event(self):
        for needle in ("requestSubmit", ".submit(", "new MouseEvent", "new PointerEvent", ".click("):
            self.assertNotIn(needle, ENGINE_SOURCE)

    def test_the_captcha_and_submit_purposes_cannot_be_clicked_in_these_modes(self):
        agent = ApplyAgent(mode="rehearse", adapter=GreenhouseAdapter())
        for purpose in ("captcha_checkbox", "submit", "anything_else"):
            with self.subTest(purpose=purpose):
                with self.assertRaises(apply_agent.ClickRefused):
                    agent._click(mock.Mock(), purpose, "first_name")


class MutationTests(unittest.TestCase):
    """The scan, run on the agent with one forbidden spelling added: it must fail on each, or its docstring claims too much."""

    CODE = (
        "locator.click()", "locator.dblclick()", "locator.type('x')", "locator.press_sequentially('x')", "locator.uncheck()", "locator.check()",
        "locator.drag_to(other)", "locator.hover()", "locator.focus()", "locator.clear()", "locator.select_text()", "locator.fill('x')",
        "locator.press('Enter')", "locator.tap()", "page.keyboard.press('Enter')", "page.mouse.click(1, 1)", "locator.dispatch_event('click')",
        "locator.set_input_files([])", "locator.select_option(label='x')", "locator.set_checked(True)", "page.drag_and_drop('a', 'b')",
        "locator.blur()", "page.goto('https://job-boards.greenhouse.io/x/jobs/1')", "page.reload()", "page.set_content('<p>x</p>')",
        "page.add_script_tag(content='x')", "page.add_style_tag(content='x')", "page.go_back()", "page.go_forward()",
    )
    # A script that types, ticks or clicks in the page's own JavaScript, and a call that hands the page a value.
    EVALUATE_STRINGS = (
        "(e, v) => { e.value = v; }", "(e) => { e.checked = true; }", "(e) => e.setAttribute('value', 'x')", "(e) => e.click ()",
        "(e) => { e.files = null; }", "(e) => { e.selectedIndex = 1; }", "(e) => { e.innerHTML += 'x'; }", "(e) => e.setRangeText('x')",
        "(e) => document.execCommand('insertText', false, 'x')", "(e) => e.focus()",
    )
    EVALUATE_CODE = (
        "locator.evaluate('(e, v) => { e.value = v; }', value)", "locator.evaluate('(e, v) => 1', self._plan)", "locator.evaluate(script)",
        "frame.evaluate('() => 1', {'profile': {'email': value}, 'answers': []})", "frame.evaluate('() => 1', value=text)",
        "locator.evaluate_all('(els, v) => 1', value)", "page.evaluate_handle(source)",
    )
    STRINGS = (
        "(e) => e.form.dispatchEvent(new Event('submit'))", "(e) => HTMLFormElement.prototype.submit.call(e.form)",
        "(e) => e.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter'}))", "(e) => e.form.requestSubmit()", "(e) => e.form.submit()",
        "(e) => e.dispatchEvent(new SubmitEvent('submit'))", "(e) => e.click()", "(e) => e.dispatchEvent(new MouseEvent('click'))",
    )

    def test_each_changing_call_outside_the_five_helpers_is_found(self):
        modules = helpers_source.apply_modules()
        for snippet in self.CODE:
            with self.subTest(snippet=snippet):
                found, _seen = unauthorised_changers(mutate_agent(modules, snippet, as_code=True))
                self.assertEqual(len(found), 1, found)
                self.assertIn(snippet, found[0])

    def test_a_script_that_writes_into_the_page_is_found_in_any_spelling(self):
        modules = helpers_source.apply_modules()
        for snippet in self.EVALUATE_STRINGS:
            with self.subTest(snippet=snippet):
                self.assertTrue(forbidden_strings(mutate_agent(modules, snippet, as_code=False)), snippet)

    def test_a_call_that_hands_the_page_a_value_or_a_script_it_did_not_write_is_found(self):
        modules = helpers_source.apply_modules()
        before, _seen = unsafe_evaluate_calls(modules)
        self.assertEqual(before, [])
        for snippet in self.EVALUATE_CODE:
            with self.subTest(snippet=snippet):
                found, _seen = unsafe_evaluate_calls(mutate_agent(modules, snippet, as_code=True))
                self.assertEqual(len(found), 1, found)

    DRIVER_CODE = (
        "self._page.request.post('https://x.example/', data=value)", "self._context.request.get('https://x.example/')", "playwright.request.new_context()",
        "ctx = self._context\n        ctx.request.post('https://x.example/', data=value)",
        "route.continue_(post_data=value)", "route.continue_(url='https://x.example/')", "handled.continue_(headers={'x': value})", "route.continue_({})",
        "route.fetch(url='https://x.example/')", "route.fetch()", "route.fulfill(body=value)", "route.fallback()",
        "self._context.set_extra_http_headers({'x': value})", "self._page.set_extra_http_headers({'x': value})",
        "self._context.expose_function('send', print)", "self._page.expose_binding('send', print)", "self._context.route_from_har('x.har')",
        "self._context.add_init_script('window.sneak = 1')", "self._context.add_init_script(value)", "self._context.add_init_script(path='x.js')",
        "self._context.add_init_script(NO_SIDE_CHANNELS + value)",
        "getattr(locator, 'click')()", "getattr(page, 'request')", "getattr(locator, name)()", "getattr(self._page, 'set_extra_http_headers')({})",
        "self._playwright.chromium.launch()", "self._browser.new_context()", "self._playwright.chromium.launch_persistent_context('x')",
        "self._playwright.chromium.connect_over_cdp('http://x')",
    )

    def test_each_way_to_send_or_rewrite_a_request_outside_route_decision_is_found(self):
        modules = helpers_source.apply_modules()
        before, _seen = unsafe_driver_calls(modules)
        self.assertEqual(before, [])
        for snippet in self.DRIVER_CODE:
            with self.subTest(snippet=snippet):
                found, _seen = unsafe_driver_calls(mutate_agent(modules, snippet, as_code=True))
                self.assertTrue(found, snippet)

    def test_the_driver_calls_the_agent_really_makes_are_not_flagged(self):
        real = (
            "def _route(self, route):\n    request = route.request\n    route.abort('blockedbyclient')\n    (hook or (lambda handled: handled.continue_()))(route)\n",
            "def _start(self):\n    self._context.add_init_script(NO_SIDE_CHANNELS)\n",
            "def _launch_browser(playwright, **options):\n    return playwright.chromium.launch(**options)\n",
            "def _new_context(browser, **options):\n    return browser.new_context(**options)\n",
            "def _attr(item, name, default=None):\n    return getattr(item, name, default)\n",
            "def f():\n    return getattr(subprocess, 'CREATE_NO_WINDOW', 0)\n",
        )
        for source in real:
            with self.subTest(source=source.splitlines()[0]):
                text = {AGENT_PATH: source}
                found, _seen = unsafe_driver_calls(text)
                self.assertEqual(found, [])

    def test_a_spelling_with_a_space_before_the_bracket_is_found_in_python_and_in_a_page_script(self):
        modules = helpers_source.apply_modules()
        found, _seen = unauthorised_changers(mutate_agent(modules, "locator.click (timeout=1)", as_code=True))
        self.assertEqual(len(found), 1, found)
        for snippet in ("(e) => e['click']()", "(e) => e[\"click\"]()", "(e) => HTMLElement.prototype.click.call(e)", "(e) => fetch('/x', {method: 'POST'})",
                        "(e) => navigator.sendBeacon('/x', e.value)", "(e) => location.assign('https://x.example/')", "(e) => window.open('https://x.example/')",
                        "(e) => { const r = new XMLHttpRequest(); r.open('GET', '/x'); }"):
            with self.subTest(snippet=snippet):
                self.assertTrue(forbidden_strings(mutate_agent(modules, snippet, as_code=False)), snippet)

    def test_the_calls_the_agent_really_makes_are_not_flagged(self):
        for snippet in ("frame.evaluate(REQUIRED_CHECK_SCRIPT)", "control.evaluate(_NATIVE_OPTIONS)", "container.evaluate(_CONTAINS, locator.element_handle())",
                        "frame.evaluate(_SCAN, {'profile': {}, 'answers': []})", "page.evaluate('() => window.scrollTo(0, 0)')"):
            with self.subTest(snippet=snippet):
                self.assertEqual(unsafe_evaluate_calls({"x.py": f"def f():\n    {snippet}\n"})[0], [])

    def test_the_same_calls_inside_a_helper_are_allowed(self):
        modules = helpers_source.apply_modules()
        before = "        self._begin(key)\n        try:\n            locator.set_checked("
        text = modules[AGENT_PATH].replace(before, before.replace("        try:", "        locator.dblclick()\n        try:", 1), 1)
        self.assertNotEqual(text, modules[AGENT_PATH])
        self.assertEqual(unauthorised_changers({**modules, AGENT_PATH: text})[0], [], "_tick is one of the five helpers")

    def test_each_string_that_could_submit_is_found(self):
        modules = helpers_source.apply_modules()
        for snippet in self.STRINGS:
            with self.subTest(snippet=snippet):
                self.assertTrue(forbidden_strings(mutate_agent(modules, snippet, as_code=False)), snippet)

    def test_a_string_in_another_module_is_found_too(self):
        modules = dict(helpers_source.apply_modules())
        other = next(name for name in modules if name != AGENT_PATH and name.startswith("apply/") or name == "apply/policy.py")
        modules[other] = modules[other] + "\n_SNEAK = \"(e) => e.form.requestSubmit()\"\n"
        self.assertTrue(forbidden_strings(modules))

    def test_each_spelling_of_an_import_of_the_policy_the_preflight_or_the_web_app_is_found(self):
        for line in (
            "from . import policy as apply_policy", "from .policy import plan_entries", "from opportunity_app.apply import policy",
            "from opportunity_app.apply.policy import plan_entries", "import opportunity_app.web.app", "from ..web import app",
            "from . import preflight", "from .preflight import check",
        ):
            with self.subTest(line=line):
                self.assertTrue(forbidden_top_imports(line + "\n"), line)
        self.assertEqual(forbidden_top_imports("from .checks import Problem\nfrom ..integrations.web_fetch import close_browser\n"), [])


# Every global of the page that sends bytes without a request the route handler sees (or lets a script start one that way): WebRTC, fetchLater,
# FedCM, WebTransport, WebSocketStream and the two kinds of worker (an init script cannot reach into a worker, so none may be made).
SIDE_CHANNELS = (
    "RTCPeerConnection", "webkitRTCPeerConnection", "RTCDataChannel", "RTCSessionDescription", "RTCIceCandidate", "fetchLater", "FetchLaterResult",
    "IdentityCredential", "IdentityProvider", "WebTransport", "WebTransportBidirectionalStream", "WebTransportDatagramDuplexStream", "WebSocketStream",
    "Worker", "SharedWorker", "sharedStorage", "SharedStorage", "SharedStorageWorklet",
    # Navigator methods (Protected Audience): the browser looks up and calls the owner's host.
    "joinAdInterestGroup", "leaveAdInterestGroup", "runAdAuction", "updateAdInterestGroups", "createAuctionNonce",
)


class LaunchIsPlain(unittest.TestCase):
    # The switches that remove features a page could carry a value out through, and nothing else: none changes how the browser presents itself.
    # The resolver rule leaves the browser able to look up only these names (Greenhouse's boards, lookups, static files, fonts and CAPTCHA),
    # written out here so that adding a host is a decision someone reads. "s?-recruiting" stands for the numbered logo and banner shards.
    RESOLVABLE = [
        "api-geocode-earth-proxy.greenhouse.io", "boards.greenhouse.io", "fonts.googleapis.com", "fonts.gstatic.com", "job-boards.cdn.greenhouse.io",
        "job-boards.greenhouse.io", "recruiting.cdn.greenhouse.io", "s?-recruiting.cdn.greenhouse.io", "s??-recruiting.cdn.greenhouse.io",
        "s???-recruiting.cdn.greenhouse.io", "www.gstatic.com", "www.recaptcha.net",
    ]
    # What a closing page can send without the route handler being asked is limited by this list alone, so it holds only what a rehearsal
    # needs before Submit: not Greenhouse's analytics collector or my.greenhouse.io, and no CAPTCHA service a Greenhouse form was not seen to use.
    LEFT_OUT = ["c.spl.greenhouse.io", "my.greenhouse.io", "www.google.com", "hcaptcha.com", "api.hcaptcha.com", "challenges.cloudflare.com"]
    ARGS = [
        "--disable-blink-features=FetchLaterAPI,WebSocketStream", "--disable-features=FedCm",
        "--host-resolver-rules=MAP * ~NOTFOUND , " + " , ".join(f"EXCLUDE {host}" for host in RESOLVABLE),
    ]

    def test_the_resolver_rule_refuses_every_name_but_the_listed_ones(self):
        self.assertEqual(list(apply_agent.RESOLVABLE_HOSTS), self.RESOLVABLE)
        self.assertTrue(self.ARGS[2].startswith("--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE "))
        self.assertNotIn("*.", " ".join(self.RESOLVABLE), "no wildcard host: a name a script makes up (a value in a subdomain) must not resolve")
        with_loopback = apply_agent.resolver_rule(("127.0.0.1",))
        self.assertTrue(with_loopback.endswith(" , EXCLUDE 127.0.0.1"))
        for endpoint in apply_checks.GREENHOUSE_LOOKUP_ENDPOINTS:
            self.assertIn(endpoint.host, self.RESOLVABLE)
        for host in apply_checks.CONFIRMED_CAPTCHA_HOSTS:
            self.assertIn(host, self.RESOLVABLE)
        for host in self.LEFT_OUT:
            self.assertNotIn(host, self.RESOLVABLE)
        self.assertEqual(
            {endpoint.host for endpoint in apply_checks.CAPTCHA_ENDPOINTS} - set(self.RESOLVABLE), {"www.google.com", "hcaptcha.com", "api.hcaptcha.com", "challenges.cloudflare.com"},
            "the policy still allows the unconfirmed CAPTCHA hosts; only their names no longer resolve")
        for host in (*apply_checks.STATIC_ASSET_HOSTS, *BOARD_HOSTS):
            self.assertIn(host, self.RESOLVABLE)

    def test_launch_and_context_options_are_exactly_these(self):
        self.assertEqual(ApplyAgent.launch_options(False), {"headless": False, "args": self.ARGS})
        self.assertEqual(ApplyAgent.launch_options(True), {"headless": True, "args": self.ARGS})
        self.assertEqual(list(apply_agent.LAUNCH_ARGS), self.ARGS)
        self.assertEqual(
            ApplyAgent.context_options(),
            {"service_workers": "block", "accept_downloads": False, "permissions": [], "no_viewport": True},
        )

    def test_nothing_in_the_file_disguises_the_browser(self):
        text = helpers_source.apply_modules()[AGENT_PATH]
        for needle in ("AutomationControlled", "stealth", "webdriver", "user_agent", "locale=", "timezone_id", "geolocation", "channel=", "ignore_default_args", "extra_http_headers"):
            with self.subTest(needle=needle):
                self.assertNotIn(needle, text)

    def test_start_calls_the_launch_wrappers_with_those_options_only(self):
        calls = {}
        class Context:
            def on(self, *_a): pass
            def route(self, *_a): pass
            def route_web_socket(self, *_a): pass
            def add_init_script(self, script): calls["init_script"] = script
            def new_page(self): return "page"
        class Browser:
            def new_context(self, **options): calls["context"] = options; return Context()
        class Playwright:
            def start(self): return self
        fake_module = mock.MagicMock()
        fake_module.sync_playwright.return_value = Playwright()
        agent = ApplyAgent(mode="rehearse", adapter=GreenhouseAdapter(), headless=True)

        def launch(playwright, **options):
            calls["launch"] = options
            return Browser()

        with mock.patch.dict(sys.modules, {"playwright": mock.MagicMock(), "playwright.sync_api": fake_module}), \
                mock.patch.object(apply_agent, "_launch_browser", launch):
            agent._start()
        self.assertEqual(calls["launch"], ApplyAgent.launch_options(True))
        self.assertEqual(calls["context"], ApplyAgent.context_options())
        self.assertEqual(calls["init_script"], apply_agent.NO_SIDE_CHANNELS, "the channels that skip the route handler are removed in every frame before a page script runs")
        for name in SIDE_CHANNELS:
            self.assertIn(f"'{name}'", apply_agent.NO_SIDE_CHANNELS)
        self.assertIn("options.identity", apply_agent.NO_SIDE_CHANNELS, "a FedCM request through navigator.credentials is refused")
        for needle in ("speculationrules", "prerender", "MutationObserver", "attachShadow"):
            self.assertIn(needle, apply_agent.NO_SIDE_CHANNELS, "speculation rules are taken out of every document and shadow root")
        for needle in ("'sendBeacon'", "keepalive", "'pagehide'", "'unload'", "'visibilitychange'", "stopImmediatePropagation"):
            self.assertIn(needle, apply_agent.NO_SIDE_CHANNELS, "a request made as the page closes is not routed, so a script must not be able to make one")

    def test_the_init_script_uses_only_copies_of_the_built_ins_it_took_before_any_page_script_ran(self):
        # A page that overwrites Element.prototype.remove, querySelectorAll, NodeList.prototype.forEach or Function.prototype.call must not be
        # able to turn the sweep into a no-op: after the opening lines, nothing calls a method of the page's objects through the live prototype.
        script = apply_agent.NO_SIDE_CHANNELS
        self.assertNotRegex(script, r"\.(?:forEach|remove|querySelectorAll|apply|call|observe|stopImmediatePropagation|addEventListener)\s*\(", "a live method of the page's objects")
        self.assertIn("Function.prototype.call.bind(Function.prototype.call)", script)
        for saved in ("Element.prototype.remove", "NodeList.prototype.item", "Document.prototype.querySelectorAll", "DocumentFragment.prototype.querySelectorAll", "MutationObserver.prototype.observe"):
            self.assertIn(saved, script)


class RouteHandlerResolvesLast(unittest.TestCase):
    """The route handler asks the policy first, and a resolver is asked only about a host a request may reach (a hostname can carry a value)."""

    def agent(self, asked):
        def resolve(host):
            asked.append(host)
            return ["93.184.216.34"]

        agent = ApplyAgent(mode="rehearse", adapter=GreenhouseAdapter(), resolve=resolve)
        agent._state.values = {"last_name": "Rivera", "email": "sam.rivera@example.test"}
        return agent

    @staticmethod
    def route(url, *, method="GET", resource_type="image", navigation=True, frame_error=False):
        request = mock.Mock()
        request.url, request.method, request.resource_type = url, method, resource_type
        request.is_navigation_request.return_value = navigation
        request.post_data_buffer = None
        request.headers = {}
        if frame_error:
            type(request).frame = mock.PropertyMock(side_effect=RuntimeError("Frame for this navigation request is not available"))
        else:
            request.frame.parent_frame = None if navigation else object()
        route = mock.Mock()
        route.request = request
        return route

    def test_before_the_first_input_an_allowed_host_is_resolved_and_let_through(self):
        asked = []
        agent = self.agent(asked)
        route = self.route("https://job-boards.greenhouse.io/x/jobs/1")
        agent._route(route)
        self.assertEqual(asked, ["job-boards.greenhouse.io"])
        route.continue_.assert_called_once()

    def test_after_the_first_input_a_host_that_is_refused_anyway_is_never_resolved(self):
        asked = []
        agent = self.agent(asked)
        agent._phase = apply_checks.PHASE_AFTER_INPUT
        hostile = (
            "https://tracker.example-robotics.test/x.png",                 # not a Greenhouse host: refused by the phase
            "https://73616d2e726976657261.rivera.exfil.test/x.png",       # carries a planned value in its name: refused by the guard
            "https://rivera.collector.example/p.gif",
            "https://evil.example/p.gif?v=sam.rivera%40example.test",
        )
        for url in hostile:
            route = self.route(url, navigation=False)
            agent._route(route)
            route.abort.assert_called_once()
            route.continue_.assert_not_called()
        self.assertEqual(asked, [], "a refused request's hostname went to the resolver")
        rules = {record["rule"] for record in agent._refused}
        self.assertEqual(rules, {"after_first_input", "value_guard"})
        carried = [record for record in agent._refused if record["rule"] == "value_guard"]
        self.assertTrue(all("rivera" not in record["host"].lower() for record in carried), "a refused record holds no value")

    def test_after_the_first_input_a_static_greenhouse_asset_is_still_resolved_and_a_private_answer_is_refused(self):
        asked = []
        agent = self.agent(asked)
        agent._phase = apply_checks.PHASE_AFTER_INPUT
        agent._route(self.route("https://job-boards.cdn.greenhouse.io/static/app.js", resource_type="script", navigation=False))
        self.assertEqual(asked, ["job-boards.cdn.greenhouse.io"])
        agent = ApplyAgent(mode="rehearse", adapter=GreenhouseAdapter(), resolve=lambda host: ["10.0.0.5"])
        route = self.route("https://job-boards.greenhouse.io/x/jobs/1")
        agent._route(route)
        route.abort.assert_called_once()
        self.assertEqual([record["rule"] for record in agent._refused], ["non_public_address"])

    def test_a_navigation_whose_frame_is_not_known_yet_is_treated_as_a_main_frame_navigation(self):
        asked = []
        agent = self.agent(asked)
        route = self.route("https://careers.example-robotics.test/apply", frame_error=True)
        agent._route(route)
        route.abort.assert_called_once()
        self.assertEqual([record["rule"] for record in agent._refused], ["offsite_navigation"])
        self.assertEqual(asked, [])


class OnlyThePagesOwnNavigationIsThePosting(unittest.TestCase):
    """A refused navigation of the run's own page says "this posting sends applicants to" a host; a popup's does not."""

    def agent(self):
        agent = ApplyAgent(mode="rehearse", adapter=GreenhouseAdapter(), resolve=lambda host: ["93.184.216.34"])
        agent._page = mock.Mock()
        return agent

    def refuse(self, agent, url, *, own_page):
        request = mock.Mock()
        request.url, request.method, request.resource_type = url, "GET", "document"
        request.is_navigation_request.return_value = True
        request.post_data_buffer = None
        request.headers = {}
        request.frame.parent_frame = None
        request.frame = agent._page.main_frame if own_page else mock.Mock(parent_frame=None)
        route = mock.Mock()
        route.request = request
        agent._route(route)
        route.abort.assert_called_once()

    def test_a_refused_navigation_of_the_run_s_own_page_is_the_postings_and_names_its_host(self):
        agent = self.agent()
        agent._page.main_frame.parent_frame = None
        self.refuse(agent, "https://careers.example-robotics.test/apply", own_page=True)
        self.assertEqual(agent._offsite_host(), "careers.example-robotics.test")
        self.assertFalse(agent._popup_refused())
        self.assertNotIn("popup", agent._refused[0])

    def test_a_refused_popup_is_not_the_posting_and_names_nothing(self):
        agent = self.agent()
        self.refuse(agent, "https://tracker.example/x", own_page=False)
        self.assertEqual(agent._offsite_host(), "")
        self.assertTrue(agent._popup_refused())
        self.assertEqual((agent._refused[0]["rule"], agent._refused[0]["popup"]), ("offsite_navigation", True))
        self.assertNotIn("sends applicants", apply_agent.POPUP)

    def test_a_check_after_the_page_loaded_stops_the_run_on_the_postings_own_navigation_only(self):
        agent = self.agent()
        agent._page.main_frame.parent_frame = None
        agent._check_left_site()
        self.refuse(agent, "https://tracker.example/x", own_page=False)
        agent._check_left_site()
        self.refuse(agent, "https://careers.example-robotics.test/apply", own_page=True)
        with self.assertRaises(apply_agent._Stop) as caught:
            agent._check_left_site()
        self.assertEqual((caught.exception.outcome, caught.exception.reason), ("needs_you", apply_agent.OFFSITE.format(host="careers.example-robotics.test")))


class TheRefusedListIsCapped(unittest.TestCase):
    """The list of refused requests stops growing at MAX_REFUSED; what ends a run by rule 1 is not read from it."""

    def full(self):
        agent = OnlyThePagesOwnNavigationIsThePosting().agent()
        agent._page.main_frame.parent_frame = None
        for _ in range(apply_agent.MAX_REFUSED):
            agent._refuse({"method": "POST", "host": "telemetry.example-robotics.test", "rule": "after_first_input"})
        return agent

    def test_an_offsite_navigation_after_five_hundred_refusals_still_ends_the_run(self):
        agent = self.full()
        OnlyThePagesOwnNavigationIsThePosting().refuse(agent, "https://careers.example-robotics.test/apply", own_page=True)
        self.assertEqual(len(agent._refused), apply_agent.MAX_REFUSED, "the record itself does not fit")
        self.assertEqual(agent._refused_total, apply_agent.MAX_REFUSED + 1)
        self.assertEqual(agent._offsite_host(), "careers.example-robotics.test")
        with self.assertRaises(apply_agent._Stop) as caught:
            agent._check_left_site()
        self.assertEqual(caught.exception.reason, apply_agent.OFFSITE.format(host="careers.example-robotics.test"))

    def test_a_popup_after_five_hundred_refusals_is_still_seen(self):
        agent = self.full()
        OnlyThePagesOwnNavigationIsThePosting().refuse(agent, "https://tracker.example/x", own_page=False)
        self.assertTrue(agent._popup_refused())
        self.assertEqual(agent._offsite_host(), "")


class LookupEvidenceIsMeasured(unittest.TestCase):
    """"typed" says whether what was typed went with a lookup. It is read from the request that went out, never only from the kind of list."""

    ENDPOINT = Endpoint("boards.greenhouse.io", "/v1/boards/examplerobotics/education/degrees", "degree")

    def agent(self, planned_value="Bachelor's Degree"):
        agent = ApplyAgent(mode="rehearse", adapter=GreenhouseAdapter(), resolve=lambda host: ["93.184.216.34"], lookup_endpoints=[self.ENDPOINT])
        agent._phase = apply_checks.PHASE_AFTER_INPUT
        agent._state.lookup_endpoints = (self.ENDPOINT,)
        agent._state.values = {"degree": planned_value}
        agent._state.typing_key, agent._state.typing_lookup = "degree", "degree"
        return agent

    def lookup(self, agent, query):
        request = mock.Mock()
        request.url = f"https://boards.greenhouse.io/v1/boards/examplerobotics/education/degrees{query}"
        request.method, request.resource_type = "GET", "fetch"
        request.is_navigation_request.return_value = False
        request.post_data_buffer = None
        request.headers = {}
        route = mock.Mock()
        route.request = request
        agent._route(route)
        return route

    def typed(self, agent):
        return [item["typed"] for item in agent._evidence()["lookups"]]

    def test_a_list_fetched_whole_is_not_said_to_have_been_sent_what_was_typed(self):
        agent = self.agent()
        route = self.lookup(agent, "")
        route.continue_.assert_called_once()
        self.assertEqual(self.typed(agent), [False])

    def test_a_degree_lookup_that_carried_the_typed_text_is_said_to_have_been_sent_it(self):
        agent = self.agent()
        route = self.lookup(agent, "?term=Bachelor%27s%20Degree")
        route.continue_.assert_called_once()   # the field's own lookup may carry its own text
        self.assertEqual(self.typed(agent), [True])
        # Once it carried the text it stays said, whatever came after.
        self.lookup(agent, "")
        self.assertEqual(self.typed(agent), [True])

    def test_a_degree_lookup_that_carried_the_search_text_typed_into_the_box_is_said_to_have_been_sent_it(self):
        agent = self.agent()
        agent._typed_texts["degree"] = {"Bachelor"}
        agent._refresh_values()
        self.lookup(agent, "?q=Bachelor")
        self.assertEqual(self.typed(agent), [True])

    def test_the_kinds_of_list_searched_by_the_text_are_still_said_to_be_sent_it(self):
        agent = self.agent()
        agent._state.typing_lookup = "location"
        agent._state.lookup_endpoints = (Endpoint("boards.greenhouse.io", "/v1/boards/examplerobotics/education/degrees", "location"),)
        agent._lookups_sent["degree"] = "location"
        self.assertEqual(self.typed(agent), [True], "a city shorter than the guarded length is not seen in a request, and is still sent")

    def test_the_search_text_typed_into_a_box_is_guarded_like_a_planned_value(self):
        agent = self.agent("Springfield, Example State, United States")
        agent._phase = apply_checks.PHASE_BEFORE_INPUT
        locator = mock.Mock()
        agent._type(locator, "Springfield", "degree", search=True)
        self.assertIn("Springfield", agent._state.values["degree"])
        agent._release()
        # A beacon carrying the city alone, to a Greenhouse static address, is refused now.
        request = mock.Mock()
        request.url, request.method, request.resource_type = "https://job-boards.greenhouse.io/x.gif?c=Springfield", "GET", "image"
        request.is_navigation_request.return_value = False
        request.post_data_buffer = None
        request.headers = {}
        route = mock.Mock()
        route.request = request
        agent._route(route)
        route.abort.assert_called_once()
        self.assertEqual([item["rule"] for item in agent._refused], ["value_guard"])


class ScreenshotNeverRecreatesTheFolder(unittest.TestCase):
    def test_a_picture_is_written_only_into_the_folder_the_run_started_with(self):
        # Deleting an account removes the student's folder; a run that outlived it must not make the folder again.
        with tempfile.TemporaryDirectory() as root:
            folder = Path(root) / "student" / "role"
            agent = ApplyAgent(mode="rehearse", adapter=GreenhouseAdapter(), run_id="run-1", screenshot_dir=folder)
            page = mock.Mock()
            page.screenshot.return_value = b"a picture"
            agent._page = page
            agent._screenshot("filled")
            self.assertFalse(folder.exists())
            self.assertFalse(folder.parent.exists(), "the student's folder was made again")
            self.assertEqual(agent._screenshots, [])
            folder.mkdir(parents=True)
            agent._screenshot("filled")
            self.assertEqual([item.name for item in folder.iterdir()], ["run-1-filled.png"])
            self.assertEqual(len(agent._screenshots), 1)


class OneCopyOfTheStopSentence(unittest.TestCase):
    """The runner's summary relies on the stop sentence ending in "No application was sent."; a second copy could drift from it."""

    def test_the_sentence_is_written_once_and_everything_else_uses_that_one(self):
        from opportunity_app.apply import runner as apply_runner

        self.assertEqual(apply_agent_types.STOPPED, "You stopped this run. No application was sent.")
        self.assertIs(apply_agent.STOPPED, apply_agent_types.STOPPED)
        self.assertIs(apply_runner.STOPPED, apply_agent_types.STOPPED)
        self.assertIs(apply_fake_ats.STOPPED_TEXT, apply_agent_types.STOPPED)
        copies = [relative for relative, text in helpers_source.apply_modules().items() if "You stopped this run" in text]
        self.assertEqual(copies, ["apply/agent_types.py"])

    def test_the_runner_uses_the_shared_rollback_and_has_none_of_its_own(self):
        text = helpers_source.apply_modules()["apply/runner.py"]
        self.assertNotIn("def _rollback", text)
        self.assertIn("rollback_quietly", text)


class FactoryAddsNothing(unittest.TestCase):
    def test_the_default_factory_passes_no_test_only_argument(self):
        tree = ast.parse(inspect.getsource(DefaultApplyAgentFactory))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "ApplyAgent"]
        self.assertEqual(len(calls), 1)
        passed = {keyword.arg for keyword in calls[0].keywords}
        for name in ("route_hook", "student_hook", "headless", "resolve", "lookup_endpoints"):
            self.assertNotIn(name, passed)
        self.assertEqual(passed, {"mode", "adapter", "run_id", "screenshot_dir", "timeouts", "on_progress", "heartbeat"})

    def test_the_agent_it_builds_has_the_test_hooks_unset_and_a_visible_window(self):
        agent = DefaultApplyAgentFactory()(
            mode="rehearse", run_id="run-1", screenshot_dir=None, timeouts=ApplyTimeouts(), on_progress=lambda *_: None, heartbeat=lambda: None,
        )
        self.assertIsNone(agent._route_hook)
        self.assertIsNone(agent._student_hook)
        self.assertIsNone(agent._lookup_endpoints_override)
        self.assertFalse(agent.headless)

    def test_it_runs_in_a_process_and_pickles_whole(self):
        import pickle

        factory = DefaultApplyAgentFactory()
        self.assertEqual(factory.isolation, "process")
        self.assertIsInstance(pickle.loads(pickle.dumps(factory)), DefaultApplyAgentFactory)


class PinnedRules(unittest.TestCase):
    def test_the_denylist_and_the_click_purposes_are_pinned(self):
        self.assertEqual(DENYLIST, (
            "autofill my application", "apply with seek", "apply with linkedin", "locate me", "dropbox", "google drive", "enter manually",
        ))
        self.assertEqual(CLICK_PURPOSES, ("select_open", "select_option", "select_close", "submit", "captcha_checkbox"))
        self.assertFalse(apply_agent.LEGACY_ENABLED)
        self.assertEqual(apply_agent.SCREENSHOT_MASK_COLOR, "#000000")

    def test_only_lookup_and_rehearse_are_built(self):
        self.assertEqual(BUILT_MODES, ("lookup", "rehearse"))

    def test_the_engine_source_is_the_three_files_in_order(self):
        self.assertEqual(ENGINE_FILES, ("adapters.js", "field-engine.js", "apply-engine.js"))
        position = -1
        for name in ENGINE_FILES:
            text = (ROOT / "apps" / "extension" / name).read_text(encoding="utf-8")
            self.assertIn(text, ENGINE_SOURCE)
            self.assertGreater(ENGINE_SOURCE.index(text), position)
            position = ENGINE_SOURCE.index(text)


class PinnedEndpoints(unittest.TestCase):
    def setUp(self):
        self.pinned = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_the_code_equals_the_fixture(self):
        self.assertEqual(
            apply_checks.GREENHOUSE_LOOKUP_ENDPOINTS,
            tuple(Endpoint(item["host"], item["path_prefix"], item["kind"]) for item in self.pinned["lookup_endpoints"]),
        )
        self.assertEqual(
            apply_checks.CAPTCHA_ENDPOINTS,
            tuple(Endpoint(item["host"], item["path_prefix"]) for item in self.pinned["captcha_endpoints"]),
        )

    def test_the_static_asset_hosts_equal_the_fixture(self):
        self.assertEqual(apply_checks.STATIC_ASSET_HOSTS, tuple(self.pinned["static_asset_hosts"]))
        self.assertEqual(apply_checks.STATIC_ASSET_SHARD_PATTERN, self.pinned["static_asset_shard_pattern"])
        for host in (*self.pinned["static_asset_hosts"], "s2-recruiting.cdn.greenhouse.io", "s7-recruiting.cdn.greenhouse.io"):
            self.assertTrue(apply_checks.is_static_asset_host(host), host)
        for host in ("c.spl.greenhouse.io", "boards.greenhouse.io", "job-boards.greenhouse.io", "greenhouse.io", "recruiting.cdn.greenhouse.io.example.test"):
            self.assertFalse(apply_checks.is_static_asset_host(host), host)

    def test_every_lookup_kind_is_a_list_the_policy_confirms_labels_for(self):
        for item in self.pinned["lookup_endpoints"]:
            with self.subTest(kind=item["kind"]):
                self.assertIn(item["kind"], apply_policy.ALLOWED_ATS_LABEL_FIELDS)
                self.assertIn(item["kind"], ("location", "school", "degree", "discipline"))
                self.assertTrue(item["receives"])

    def test_the_fixture_names_only_greenhouse_and_captcha_hosts_and_no_posting(self):
        allowed = re.compile(r"^(?:[a-z0-9-]+\.)?greenhouse\.io$|^www\.(?:recaptcha\.net|gstatic\.com|google\.com)$|^(?:api\.)?hcaptcha\.com$|^challenges\.cloudflare\.com$")
        for item in [*self.pinned["lookup_endpoints"], *self.pinned["captcha_endpoints"]]:
            with self.subTest(host=item["host"]):
                self.assertRegex(item["host"], allowed)
                self.assertNotIn("/jobs/", item["path_prefix"])
        self.assertNotRegex(FIXTURE.read_text(encoding="utf-8"), r"/jobs/\d")

    def test_a_lookup_prefix_names_the_board_only_through_its_token_placeholder(self):
        bound = bind_endpoints(apply_checks.GREENHOUSE_LOOKUP_ENDPOINTS, "examplerobotics")
        self.assertEqual([endpoint.kind for endpoint in bound], ["location", "school", "degree", "discipline"])
        self.assertTrue(all("{token}" not in endpoint.path_prefix for endpoint in bound))
        self.assertIn(Endpoint("boards.greenhouse.io", "/v1/boards/examplerobotics/education/schools", "school"), bound)
        # With no token the three board-scoped endpoints are dropped rather than left unmatchable by accident.
        self.assertEqual([endpoint.kind for endpoint in bind_endpoints(apply_checks.GREENHOUSE_LOOKUP_ENDPOINTS, "")], ["location"])

    def test_the_board_token_comes_from_the_posting_address_only(self):
        self.assertEqual(board_token("https://job-boards.greenhouse.io/examplerobotics/jobs/4000000001"), "examplerobotics")
        self.assertEqual(board_token("https://boards.greenhouse.io/embed/job_app?for=examplerobotics&token=4000000001"), "examplerobotics")
        for url in ("https://job-boards.greenhouse.io/", "https://job-boards.greenhouse.io/a/b/jobs/1", "not a url"):
            self.assertEqual(board_token(url), "")


class LoaderPaths(unittest.TestCase):
    def test_the_fixture_form_gives_both_paths(self):
        html = apply_fake_ats.fixture_text("new_form.html")
        self.assertEqual(GreenhouseAdapter.loader_paths(html), (apply_fake_ats.JOB_PATH, apply_fake_ats.CONFIRMATION_PATH))

    def test_a_page_without_the_loader_gives_neither(self):
        self.assertEqual(GreenhouseAdapter.loader_paths("<html><body>no loader</body></html>"), ("", ""))
        self.assertEqual(GreenhouseAdapter.loader_paths(""), ("", ""))

    def test_a_full_address_on_the_submit_host_is_read_as_its_path_and_one_elsewhere_is_not(self):
        live = '{"submitPath":"https:\\u002F\\u002Fboards.greenhouse.io\\u002Fexamplerobotics\\u002Fjobs\\u002F4000000001","confirmationPath":"\\/examplerobotics\\/jobs\\/4000000001\\/confirmation"}'
        self.assertEqual(GreenhouseAdapter.loader_paths(live), ("/examplerobotics/jobs/4000000001", "/examplerobotics/jobs/4000000001/confirmation"))
        elsewhere = '{"submitPath":"https://collect.example.test/apply","confirmationPath":"/x/jobs/1/confirmation"}'
        self.assertEqual(GreenhouseAdapter.loader_paths(elsewhere), ("", "/x/jobs/1/confirmation"))


class UnbuiltAndRefusedRunsOpenNoBrowser(unittest.TestCase):
    def run_agent(self, mode, url=apply_fake_ats.JOB_URL):
        agent = ApplyAgent(mode=mode, adapter=GreenhouseAdapter())
        with mock.patch.object(ApplyAgent, "_start", side_effect=AssertionError("a browser was started")):
            return agent.run(FakePlan([planned("first_name", "First Name", "Sam")]), page_url=url, schema=[], files={})

    def test_submit_and_handoff_fail_without_starting_a_browser(self):
        for mode in ("submit", "handoff"):
            with self.subTest(mode=mode):
                result = self.run_agent(mode)
                self.assertEqual((result.outcome, result.reasons), ("failed", [NOT_BUILT]))
                self.assertFalse(result.handed_over)
                self.assertEqual(result.requests, [])

    def test_a_page_that_is_not_a_greenhouse_board_is_never_opened(self):
        for url in ("https://careers.example.test/apply", "https://my.greenhouse.io/jobs/1", "http://127.0.0.1:8799/x"):
            with self.subTest(url=url):
                result = self.run_agent("rehearse", url)
                self.assertEqual((result.outcome, result.reasons), ("failed", [NOT_BOARD]))

    def test_a_missing_playwright_is_the_install_sentence(self):
        agent = ApplyAgent(mode="rehearse", adapter=GreenhouseAdapter())
        with mock.patch.object(ApplyAgent, "_start", side_effect=ModuleNotFoundError("playwright")):
            result = agent.run(FakePlan([]), page_url=apply_fake_ats.JOB_URL, schema=[], files={})
        self.assertEqual((result.outcome, result.reasons), ("failed", [apply_agent.INSTALL]))
        self.assertIn("python -m playwright install chromium", result.reasons[0])

    def test_importing_the_agent_does_not_import_playwright(self):
        text = helpers_source.apply_modules()[AGENT_PATH]
        top = [node for node in ast.parse(text).body if isinstance(node, (ast.Import, ast.ImportFrom))]
        for node in top:
            names = [alias.name for alias in node.names] + [getattr(node, "module", "") or ""]
            self.assertFalse(any(str(name).startswith("playwright") for name in names), "playwright is imported at the top of apply/agent.py")

    def test_the_agent_never_imports_the_policy_or_the_web_app_at_the_top(self):
        text = helpers_source.apply_modules()[AGENT_PATH]
        self.assertEqual(forbidden_top_imports(text), [], "the plan arrives pickled and is read by attribute")


if __name__ == "__main__":
    unittest.main()
