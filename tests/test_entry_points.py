"""Every way the project is started from outside must keep resolving.

Launchers (Open Pipeline.vbs/.command), the scheduled-task installers and their run scripts, the Dockerfile and
infra/docker-compose.yml, CI, the git hooks, the README and SETUP all name `python -m opportunity_app.<module>`, a
`scripts/<file>` path, or `uvicorn opportunity_app.api:app`. None of them is imported by a unit test, so a refactor that moves or
renames a module (splitting api.py into routers, moving modules into packages) would break a student's double-click launcher
or scheduled task with every test still green. This file is the contract:

* the `-m` modules and script paths the launchers and docs mention are read from those files, so a new mention is checked
  automatically, and each must resolve;
* the explicit list below is checked too, so deleting the last mention cannot drop a module out of the contract silently;
* each entry point answers `--help` in a subprocess (fast, no network, no database opened), or for the few that would touch real
  mail or need the slow app import, is imported and checked for `main`.

If a module moved on purpose, leave a thin module at the old name (a `python -m` target), or change every launcher and this list
together.
"""

import ast
import importlib
import importlib.util
import re
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    import realdata_guard
except ImportError:  # imported as tests.<module>, with tests/ not on sys.path
    from tests import realdata_guard
# Guards this module's own run against opening data/*.db (AGENTS.md hard rule 1); see tests/realdata_guard.py.
realdata_guard.install()

ROOT = Path(__file__).resolve().parent.parent

# `python -m <module> ...`: module -> runs `--help` in a subprocess. `opportunity_app.api` is checked by import below because
# building the app takes several seconds; pipeline_mailbox is checked by import so a test never goes near the mailbox.
HELP_MODULES = (
    "opportunity_app.daily",
    "opportunity_app.launch",
    "opportunity_app.migrate",
    "opportunity_app.ops_cli",
    "opportunity_app.outreach_cli",
    "opportunity_app.purge",
    "opportunity_app.setup",
    "opportunity_app.worker",
)
IMPORT_ONLY_MODULES = ("opportunity_app.api", "opportunity_app.pipeline_mailbox")
# `python <path> ...`, run from the repository root.
HELP_SCRIPTS = ("pipeline.py", "scripts/check_personal_data.py", "scripts/run_api_fuzz.py")
# Scripts that are parsed, not run: pipeline_mailbox.py resolves the main checkout's mailbox, and serve_for_testing.py takes
# several seconds to import. Both must still parse and define `main` behind a __main__ guard.
PARSE_ONLY_SCRIPTS = ("scripts/pipeline_mailbox.py", "scripts/serve_for_testing.py")
# Things launchers name that are not Python (checked to exist).
OTHER_LAUNCH_FILES = (
    "Open Pipeline.vbs", "Open Pipeline.command", "scripts/open-web.ps1", "scripts/start-web.ps1", "scripts/start-web.vbs",
    "scripts/run-daily.ps1", "scripts/run-daily.vbs", "scripts/run-outreach-discovery.ps1", "scripts/run-outreach-discovery.vbs",
    "scripts/install-web-task.ps1", "scripts/install-daily-task.ps1", "scripts/install-outreach-task.ps1", "scripts/playwright-mcp.mjs",
    "Dockerfile", "infra/docker-compose.yml",
)

# Files whose text names entry points. Globs are relative to the repository root.
REFERENCE_GLOBS = (
    "scripts/*.ps1", "scripts/*.vbs", "scripts/*.py", "Open Pipeline.*", "Dockerfile", "infra/*.yml", "infra/*.yaml",
    ".github/workflows/*.yml", ".githooks/*", ".mcp.json", "package.json", "README.md", "SETUP.md", "CONTRIBUTING.md",
    ".env.example", "opportunity_app/*.py", "pipeline.py", "pipeline_core/*.py",
)
MODULE_REFERENCE = re.compile(r"""-m['", ]+\s*((?:opportunity_app|pipeline_core)(?:\.\w+)+)""")
UVICORN_REFERENCE = re.compile(r"""uvicorn['", ]+\s*([\w.]+):(\w+)""")
SCRIPT_REFERENCE = re.compile(r"""(?<![\w.-])(scripts/[\w-]+\.(?:ps1|vbs|py|mjs|sh))""")
# A sibling script named on its own inside scripts/ (start-web.vbs calls start-web.ps1 through its own folder).
SIBLING_REFERENCE = re.compile(r"""(?<![\w./\\-])([\w-]+\.(?:ps1|vbs))\b""")


def references():
    """(modules, uvicorn targets, script paths) named in the launcher files, each as {name: {files that name it}}."""
    modules, uvicorn, scripts = {}, {}, {}
    for pattern in REFERENCE_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            if not path.is_file():
                continue
            relative = path.relative_to(ROOT).as_posix()
            text = path.read_text(encoding="utf-8", errors="replace").replace("\\", "/")
            text = text.replace("Open Pipeline", "")  # the launchers' own names have a space; what is left of them is not a script
            for match in MODULE_REFERENCE.finditer(text):
                modules.setdefault(match.group(1), set()).add(relative)
            for match in UVICORN_REFERENCE.finditer(text):
                uvicorn.setdefault((match.group(1), match.group(2)), set()).add(relative)
            for match in SCRIPT_REFERENCE.finditer(text):
                scripts.setdefault(match.group(1), set()).add(relative)
            if relative.startswith("scripts/") and path.suffix in (".ps1", ".vbs"):
                for match in SIBLING_REFERENCE.finditer(text):
                    scripts.setdefault(f"scripts/{match.group(1)}", set()).add(relative)
    return modules, uvicorn, scripts


NL = chr(10)


def main_guard_calls_main(source):
    """Whether the module-level `if __name__ == "__main__":` block calls `main(...)`.

    `--help` and an import both succeed when this block is deleted or stops calling main, while `python -m <module>` silently does
    nothing, so the guard itself is checked structurally.
    """
    for node in ast.parse(source).body:
        if not (isinstance(node, ast.If) and isinstance(node.test, ast.Compare) and isinstance(node.test.left, ast.Name)
                and node.test.left.id == "__name__"):
            continue
        for call in (item for statement in node.body for item in ast.walk(statement) if isinstance(item, ast.Call)):
            target = call.func
            if (isinstance(target, ast.Name) and target.id == "main") or (isinstance(target, ast.Attribute) and target.attr == "main"):
                return True
    return False


def module_source(module):
    spec = importlib.util.find_spec(module)
    return Path(spec.origin).read_text(encoding="utf-8")


def run_help(arguments):
    return subprocess.run(
        [sys.executable, *arguments, "--help"], cwd=ROOT, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
    )


class ReferencedEntryPointsResolveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.modules, cls.uvicorn, cls.scripts = references()

    def test_the_scan_found_the_entry_points_it_should(self):
        # A vacuous scan would pass everything: the launchers are known to name these.
        self.assertIn("opportunity_app.launch", self.modules)
        self.assertIn("opportunity_app.api", self.modules)
        self.assertIn(("opportunity_app.api", "app"), self.uvicorn)
        self.assertIn("scripts/open-web.ps1", self.scripts)

    def test_every_python_m_module_the_launchers_and_docs_name_resolves(self):
        for module, files in sorted(self.modules.items()):
            with self.subTest(module=module):
                self.assertIsNotNone(importlib.util.find_spec(module), f"`python -m {module}` (named in {sorted(files)}) no longer resolves")

    def test_every_python_m_module_named_is_in_the_explicit_contract(self):
        known = set(HELP_MODULES) | set(IMPORT_ONLY_MODULES)
        for module, files in sorted(self.modules.items()):
            with self.subTest(module=module):
                self.assertIn(module, known, f"{module} is run by {sorted(files)}: add it to this test's explicit list")

    def test_every_uvicorn_target_resolves_to_an_attribute(self):
        for (module, attribute), files in sorted(self.uvicorn.items()):
            with self.subTest(target=f"{module}:{attribute}"):
                self.assertTrue(hasattr(importlib.import_module(module), attribute), f"uvicorn {module}:{attribute} (named in {sorted(files)}) is gone")

    def test_every_scripts_path_the_launchers_and_docs_name_exists(self):
        for script, files in sorted(self.scripts.items()):
            with self.subTest(script=script):
                self.assertTrue((ROOT / script).is_file(), f"{script} (named in {sorted(files)}) does not exist")

    def test_the_launcher_files_themselves_exist(self):
        for name in OTHER_LAUNCH_FILES + HELP_SCRIPTS + PARSE_ONLY_SCRIPTS:
            with self.subTest(file=name):
                self.assertTrue((ROOT / name).is_file())


class EntryPointsRunTests(unittest.TestCase):
    def test_every_python_m_entry_point_answers_help(self):
        # All at once: several take a second to import, and none of them opens a database to print its usage.
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda module: (module, run_help(["-m", module])), HELP_MODULES))
            scripts = list(pool.map(lambda script: (script, run_help([script])), HELP_SCRIPTS))
        for name, done in results + scripts:
            with self.subTest(entry=name):
                self.assertEqual(done.returncode, 0, done.stderr[-600:])
                self.assertIn("usage", done.stdout.lower())

    def test_the_import_only_modules_expose_main(self):
        for module in IMPORT_ONLY_MODULES:
            with self.subTest(module=module):
                self.assertTrue(callable(getattr(importlib.import_module(module), "main", None)), f"{module} has no main()")

    def test_every_python_m_module_starts_main_from_its_main_guard(self):
        for module in HELP_MODULES + IMPORT_ONLY_MODULES:
            with self.subTest(module=module):
                self.assertTrue(main_guard_calls_main(module_source(module)),
                                f"`python -m {module}` would import and exit without running main()")

    def test_the_main_guard_check_notices_a_missing_or_empty_guard(self):
        head = "def main():" + NL + "    pass" + NL + NL
        guard = 'if __name__ == "__main__":' + NL
        self.assertTrue(main_guard_calls_main(head + guard + "    raise SystemExit(main())" + NL))
        self.assertFalse(main_guard_calls_main(head))
        self.assertFalse(main_guard_calls_main(head + guard + "    pass" + NL))
        self.assertFalse(main_guard_calls_main(head + guard + "    other()" + NL))

    def test_the_api_module_exposes_app_create_app_and_a_parser(self):
        api = importlib.import_module("opportunity_app.api")
        self.assertTrue(callable(api.create_app))
        self.assertEqual(type(api.app).__name__, "FastAPI", "the Dockerfile's `uvicorn opportunity_app.api:app` needs a module-level app")
        self.assertTrue(callable(api.build_parser))
        parsed = api.build_parser().parse_args([])
        self.assertTrue(hasattr(parsed, "host") and hasattr(parsed, "port"))

    def test_the_parse_only_scripts_define_main_behind_a_main_guard(self):
        for name in PARSE_ONLY_SCRIPTS:
            with self.subTest(script=name):
                source = (ROOT / name).read_text(encoding="utf-8")
                tree = ast.parse(source, filename=name)
                self.assertIn("main", {node.name for node in tree.body if isinstance(node, ast.FunctionDef)})
                self.assertTrue(main_guard_calls_main(source), f"{name} has no __main__ guard that calls main()")

    def test_the_sandbox_server_still_finds_the_test_helpers_it_imports(self):
        # scripts/serve_for_testing.py imports helpers out of tests/ and tests/ui/ by bare module name; moving one silently breaks
        # exploratory testing and the browser suite's shared fixtures.
        source = (ROOT / "scripts" / "serve_for_testing.py").read_text(encoding="utf-8")
        tests = ROOT / "tests"
        for match in re.finditer(r"^(?:from|import)\s+(helpers_platform|outreach_fakes|apply_fake_ats|sandbox_app)\b", source, re.MULTILINE):
            name = match.group(1)
            with self.subTest(helper=name):
                self.assertTrue((tests / f"{name}.py").is_file() or (tests / "ui" / f"{name}.py").is_file(), f"{name}.py is not under tests/ or tests/ui/")


if __name__ == "__main__":
    unittest.main()
