"""Every `opportunity_app.<...>` name a test patches or listens to must resolve to something that exists.

`mock.patch("opportunity_app.x.helper")` raises when `opportunity_app.x` or `helper` is gone, but only when that test runs, and a
patch whose target moved and was left behind can still pass if the target happens to resolve to a different object, or if a
renamed package still imports (an empty subpackage with the old module's name, for example). This file resolves each string once,
up front, so a moved module cannot leave a patch pointing at nothing, and checks the instrument on synthetic sources.

What is read: the first argument of patch(), patch.dict() and patch.multiple() must resolve to an attribute (import the longest
module prefix, then getattr the rest); the first argument of assertLogs(), getLogger(), import_module(), find_spec() and reload()
must name a module, because loggers follow ``__name__`` and a stale logger name silently splits log lines away from a listener.
Strings built at run time (f-strings, constants) are out of reach and are not read.
"""

import ast
import importlib
import importlib.util
import re
import sys
import unittest
from pathlib import Path

try:
    import realdata_guard
except ImportError:  # imported as tests.test_patch_targets, with tests/ not on sys.path
    from tests import realdata_guard
# Guards this module's own run against opening data/*.db (AGENTS.md hard rule 1); see tests/realdata_guard.py.
realdata_guard.install()

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DOTTED = re.compile(r"^opportunity_app(?:\.\w+)+$")
PATCH_CALLS = {"patch"}
PATCH_METHODS = {"dict", "multiple"}
MODULE_CALLS = {"assertLogs", "assertNoLogs", "getLogger", "import_module", "find_spec", "reload"}

# Synthetic module names that tests/test_layers.py invents to exercise its own rules; they are never imported.
SYNTHETIC_FILES = {"tests/test_layers.py"}


def call_name(node: ast.Call) -> str:
    target = node.func
    return target.id if isinstance(target, ast.Name) else target.attr if isinstance(target, ast.Attribute) else ""


def is_patch_call(node: ast.Call) -> bool:
    name = call_name(node)
    if name in PATCH_CALLS:
        return True
    target = node.func
    return name in PATCH_METHODS and isinstance(target, ast.Attribute) and call_name(ast.Call(func=target.value, args=[], keywords=[])) == "patch"


def targets_in(source: str, filename: str = "<test>") -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    """(patch targets, module names) as [(line, dotted name)] for the `opportunity_app.*` strings a test file names."""
    patched: list[tuple[int, str]] = []
    modules: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        first = node.args[0]
        if not (isinstance(first, ast.Constant) and isinstance(first.value, str) and DOTTED.match(first.value)):
            continue
        if is_patch_call(node):
            patched.append((node.lineno, first.value))
        elif call_name(node) in MODULE_CALLS:
            modules.append((node.lineno, first.value))
    return patched, modules


def resolve(dotted: str):
    """The object a dotted name points at: the longest importable module prefix, then attributes."""
    parts = dotted.split(".")
    for end in range(len(parts), 0, -1):
        prefix = ".".join(parts[:end])
        try:
            found = importlib.util.find_spec(prefix)
        except (ImportError, ValueError):
            found = None
        if found is None:
            continue
        obj = importlib.import_module(prefix)
        for attribute in parts[end:]:
            obj = getattr(obj, attribute)
        return obj
    raise ModuleNotFoundError(dotted)


def is_module(dotted: str) -> bool:
    try:
        return importlib.util.find_spec(dotted) is not None
    except (ImportError, ValueError):
        return False


def stale(source: str, filename: str = "<test>") -> list[str]:
    patched, modules = targets_in(source, filename)
    found = []
    for line, name in patched:
        try:
            resolve(name)
        except (ImportError, AttributeError):
            found.append(f"{filename}:{line} patches {name}, which does not resolve")
    for line, name in modules:
        if not is_module(name):
            found.append(f"{filename}:{line} names the module {name}, which does not exist")
    return found


def scanned_files():
    for path in sorted((ROOT / "tests").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        if relative not in SYNTHETIC_FILES:
            yield relative, path.read_text(encoding="utf-8-sig")


class PatchTargetsResolveTests(unittest.TestCase):
    def test_every_patch_target_and_logger_name_in_the_tests_exists(self):
        problems = []
        patched_total = modules_total = 0
        for relative, source in scanned_files():
            patched, modules = targets_in(source, relative)
            patched_total += len(patched)
            modules_total += len(modules)
            problems.extend(stale(source, relative))
        self.assertGreater(patched_total, 30, "the scan found far fewer patch targets than the suite uses: it reads the wrong calls")
        self.assertGreater(modules_total, 5, "the scan found no logger or module names")
        self.assertEqual(problems, [], "a patch or logger name points at a module or attribute that moved or was removed")

    def test_the_browser_suite_is_scanned_too(self):
        self.assertTrue(any(relative.startswith("tests/ui/") for relative, _ in scanned_files()))


class InstrumentBitesTests(unittest.TestCase):
    """The scan is a claim about the tests; these check it on synthetic sources."""

    def test_a_missing_module_attribute_and_logger_are_each_reported(self):
        source = (
            "from unittest import mock\n"
            "mock.patch('opportunity_app.schema.no_such_name')\n"
            "mock.patch('opportunity_app.no_such_module.helper')\n"
            "self.assertLogs('opportunity_app.no_such_module', level='ERROR')\n"
            "logging.getLogger('opportunity_app.no_such_module')\n"
        )
        found = stale(source)
        self.assertEqual(len(found), 4, found)
        self.assertIn("opportunity_app.schema.no_such_name", found[0])

    def test_existing_targets_pass_in_every_spelling(self):
        source = (
            "from unittest import mock\n"
            "import unittest.mock\n"
            "mock.patch('opportunity_app.schema.ensure_product_schema')\n"
            "unittest.mock.patch('opportunity_app.schema.ensure_product_schema')\n"
            "@patch('opportunity_app.schema.ensure_product_schema')\n"
            "def f(): pass\n"
            "mock.patch.dict('opportunity_app.schema.__dict__', {})\n"
            "self.assertLogs('opportunity_app.schema', level='ERROR')\n"
            "mock.patch('os.environ')\n"
            "mock.patch(f'{__name__}.x')\n"
        )
        self.assertEqual(stale(source), [])
        patched, modules = targets_in(source)
        self.assertEqual((len(patched), len(modules)), (4, 1))

    def test_a_package_name_that_exists_but_holds_no_such_attribute_is_reported(self):
        # An empty package still imports, which is how a patch of a moved module would otherwise go unnoticed.
        self.assertEqual(len(stale("mock.patch('opportunity_app.web.no_such_attribute')")), 1)


if __name__ == "__main__":
    unittest.main()
