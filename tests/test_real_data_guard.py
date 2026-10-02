"""The suite-wide guard against opening the real data/*.db (tests/realdata_guard.py), and proof that it bites.

Importing this module installs the guard, which is what protects a plain `python -m unittest discover -s tests` run (pytest gets
it from tests/conftest.py). The tests below never touch a real database: they assert the open is refused before it happens.
"""

import ast
import os
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import realdata_guard

realdata_guard.install()

from pipeline_core import paths, store
from opportunity_app import DEFAULT_LEGACY_DB, DEFAULT_PLATFORM_DB
from opportunity_app.core.schema import ensure_product_schema
from opportunity_app.core.database import connect_product

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


def leftovers(path):
    """The database file and its journal files that exist, so a refused open can be shown to have created nothing."""
    return [candidate for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm"), Path(f"{path}-journal")) if candidate.exists()]


class GuardIsInstalledTests(unittest.TestCase):
    def test_sqlite_connect_is_the_guarded_one_in_this_process(self):
        self.assertEqual(sqlite3.connect.__name__, "guarded_connect")

    def test_installing_twice_does_not_stack_wrappers(self):
        before = sqlite3.connect
        realdata_guard.install()
        self.assertIs(sqlite3.connect, before)


class OpeningRealDataFailsLoudlyTests(unittest.TestCase):
    def refuse(self, call):
        before = {name: leftovers(DATA / name) for name in ("platform.db", "pipeline.db")}
        with self.assertRaises(realdata_guard.RealDataAccessError):
            call()
        self.assertEqual({name: leftovers(DATA / name) for name in before}, before, "a refused open must not create or touch the file")

    def test_a_direct_open_of_either_real_database_is_refused(self):
        for name in ("platform.db", "pipeline.db"):
            with self.subTest(name=name):
                self.refuse(lambda: sqlite3.connect(DATA / name))
                self.refuse(lambda: sqlite3.connect(str(DATA / name)))

    def test_the_journal_files_and_other_sqlite_suffixes_are_refused_too(self):
        for name in ("platform.db-wal", "platform.db-shm", "old.sqlite", "backup.sqlite3"):
            with self.subTest(name=name):
                self.refuse(lambda: sqlite3.connect(DATA / name))

    def test_dated_backups_and_any_other_name_in_the_data_directory_are_refused(self):
        # The real directory holds platform.db.pre-0047-backup, platform.db.bak-pre-callprep-regen-2026-09-29 and similar.
        for name in ("platform.db.pre-0047-backup", "platform.db.pre-0029-backup-wal", "platform.db.bak-pre-x", "notes.txt"):
            with self.subTest(name=name):
                self.refuse(lambda: sqlite3.connect(DATA / name))

    def test_a_relative_path_and_dot_dot_segments_are_resolved_before_the_check(self):
        with mock.patch("os.getcwd", return_value=str(ROOT)), mock.patch.object(Path, "cwd", return_value=ROOT):
            self.refuse(lambda: sqlite3.connect("data/platform.db"))
            self.refuse(lambda: sqlite3.connect("tests/../data/platform.db"))

    def test_uri_forms_are_refused(self):
        uri = DATA.joinpath("platform.db").as_uri()
        self.refuse(lambda: sqlite3.connect(f"{uri}?mode=ro", uri=True))
        self.refuse(lambda: sqlite3.connect(uri, uri=True))

    def test_an_ordinary_path_is_refused_even_when_uri_is_true(self):
        # With uri=True sqlite still opens a name that does not start with "file:" as an ordinary filename.
        for name in ("platform.db", "pipeline.db", "platform.db.pre-0047-backup"):
            with self.subTest(name=name):
                self.refuse(lambda: sqlite3.connect(str(DATA / name), uri=True))
                self.refuse(lambda: sqlite3.connect(DATA / name, uri=True))
        with mock.patch("os.getcwd", return_value=str(ROOT)), mock.patch.object(Path, "cwd", return_value=ROOT):
            self.refuse(lambda: sqlite3.connect("data/platform.db", uri=True))

    def test_memory_mode_is_the_exact_query_parameter_not_a_substring(self):
        uri = DATA.joinpath("platform.db").as_uri()
        self.refuse(lambda: sqlite3.connect(f"{uri}?mode=ro&unused=mode=memory", uri=True))
        self.refuse(lambda: sqlite3.connect(f"{uri}?mode=ro&note=memory", uri=True))
        self.assertFalse(realdata_guard.is_real_data_path(f"{uri}?mode=memory", uri=True))
        self.assertFalse(realdata_guard.is_real_data_path(f"{uri}?cache=shared&mode=memory", uri=True))

    def test_the_defaults_the_code_uses_are_refused(self):
        self.assertEqual(DEFAULT_PLATFORM_DB.resolve(), (DATA / "platform.db").resolve())
        self.refuse(lambda: connect_product())
        self.refuse(lambda: connect_product(DEFAULT_PLATFORM_DB, read_only=True))
        self.refuse(lambda: sqlite3.connect(DEFAULT_LEGACY_DB))

    def test_a_legacy_test_that_forgot_to_patch_db_path_is_caught(self):
        # paths.DB_PATH is the one global about thirty tests patch. Unpatched, store.connect() would open the real file.
        with mock.patch.object(paths, "DB_PATH", DATA / "pipeline.db"):
            self.refuse(store.connect)

    def test_code_that_catches_exceptions_cannot_swallow_it(self):
        def swallowing():
            try:
                sqlite3.connect(DATA / "platform.db")
            except Exception:  # noqa: BLE001 - the point: a broad handler must not hide this
                return "swallowed"

        with self.assertRaises(realdata_guard.RealDataAccessError):
            swallowing()
        with self.assertRaises(realdata_guard.RealDataAccessError):
            try:
                sqlite3.connect(DATA / "platform.db")
            except sqlite3.Error:
                self.fail("sqlite3.Error handlers must not see it")

    def test_a_main_checkout_data_directory_is_protected_from_a_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = Path(tmp).resolve() / "main"
            worktree = Path(tmp).resolve() / "main" / ".claude" / "worktrees" / "w1"
            (main / ".git" / "worktrees" / "w1").mkdir(parents=True)
            worktree.mkdir(parents=True)
            (worktree / ".git").write_text(f"gitdir: {main / '.git' / 'worktrees' / 'w1'}\n", encoding="utf-8")
            dirs = realdata_guard.real_data_dirs(worktree)
            self.assertEqual(dirs, (worktree / "data", main / "data"))
            self.assertTrue(realdata_guard.is_real_data_path(main / "data" / "platform.db", dirs=dirs))
            self.assertTrue(realdata_guard.is_real_data_path(worktree / "data" / "pipeline.db", dirs=dirs))


class RealDataFilesAreRefusedTests(unittest.TestCase):
    """The same guard covers plain files: no test may open, create, remove or rename anything under a real data directory.

    The sqlite wrapper cannot see a lock file, a report or a backup; an audit hook can, whatever opens it. The refusal
    comes before the call, so a refused create leaves nothing behind (and a file that does appear fails the test)."""

    def setUp(self):
        self.name = f"guard-probe-{uuid.uuid4().hex}"
        self.target = DATA / f"{self.name}.txt"
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for leftover in (self.target, DATA / self.name):
            if leftover.is_dir():
                leftover.rmdir()
            elif leftover.exists():
                leftover.unlink()

    def refuse(self, call):
        with self.assertRaises(realdata_guard.RealDataAccessError):
            call()
        self.assertFalse(self.target.exists() or (DATA / self.name).exists(), "a refused call must create nothing")

    def test_creating_writing_appending_or_updating_a_file_is_refused(self):
        for mode in ("w", "a", "x", "wb", "ab", "r+", "rb+"):
            with self.subTest(mode=mode):
                self.refuse(lambda: open(self.target, mode))
        for flags in (os.O_CREAT | os.O_WRONLY, os.O_RDWR, os.O_WRONLY | os.O_APPEND, os.O_CREAT | os.O_EXCL | os.O_WRONLY):
            with self.subTest(flags=flags):
                self.refuse(lambda: os.open(self.target, flags))
        self.refuse(lambda: self.target.touch())
        self.refuse(lambda: self.target.write_text("x", encoding="utf-8"))
        self.refuse(lambda: open(str(DATA / ".." / "data" / self.target.name), "w"))

    def test_making_removing_or_renaming_inside_the_directory_is_refused(self):
        self.refuse(lambda: os.mkdir(DATA / self.name))
        self.refuse(lambda: (DATA / self.name).mkdir(parents=True, exist_ok=True))
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.txt"
            source.write_text("x", encoding="utf-8")
            self.refuse(lambda: os.rename(source, self.target))
            self.assertTrue(source.exists())
            self.refuse(lambda: os.replace(source, self.target))
        self.refuse(lambda: os.remove(self.target))

    def test_the_deep_search_lock_default_is_inside_the_protected_directory_and_refused(self):
        """The test that started this: _RunLock(REPORT_DIR / ...) on the default REPORT_DIR created a real lock in data/."""
        from opportunity_app.outreach import discovery

        lock = discovery.REPORT_DIR / "outreach-discovery.lock"
        self.assertTrue(realdata_guard.is_real_data_path(lock))
        held = None
        try:
            with self.assertRaises(realdata_guard.RealDataAccessError):
                held = discovery._RunLock(lock).__enter__()
        finally:
            if held is not None:
                held.__exit__()

    def test_temp_folders_and_look_alike_names_are_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            for folder in (Path(tmp), Path(tmp) / "data"):
                folder.mkdir(exist_ok=True)
                (folder / "ok.txt").write_text("fine", encoding="utf-8")
                self.assertEqual((folder / "ok.txt").read_text(encoding="utf-8"), "fine")
                (folder / "ok.txt").unlink()
        self.assertFalse(realdata_guard.is_real_data_path(ROOT / "data-not-really" / "x.txt"))
        self.assertFalse(realdata_guard.is_real_data_path(ROOT / "tests" / "data" / "x.txt"))

    def test_reading_is_let_through_because_tracked_files_live_there(self):
        self.assertFalse(realdata_guard.audit_refuses("open", (str(self.target), "r", 0)))
        self.assertFalse(realdata_guard.audit_refuses("open", (str(self.target), "rb", os.O_RDONLY)))
        self.assertFalse(realdata_guard.audit_refuses("open", (self.target, None, os.O_RDONLY)))
        with self.assertRaises(FileNotFoundError):
            open(self.target, "r")
        with self.assertRaises(FileNotFoundError):
            open(self.target, "rb")

    def test_the_hook_judges_only_events_that_name_a_path(self):
        refuse = realdata_guard.audit_refuses
        self.assertTrue(refuse("open", (str(self.target), "w", 0)))
        self.assertTrue(refuse("open", (self.target, None, os.O_CREAT)))
        self.assertTrue(refuse("os.rename", ("somewhere", str(self.target), None, None)))
        self.assertFalse(refuse("open", (3, "w", 0)), "an already-open descriptor names no path")
        self.assertFalse(refuse("open", (None, "w", 0)))
        self.assertFalse(refuse("socket.connect", (object(), str(self.target))))


class NormalOpensStillWorkTests(unittest.TestCase):
    def test_a_temp_database_and_memory_databases_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            for target in (Path(tmp) / "platform.db", str(Path(tmp) / "pipeline.db"), ":memory:"):
                with self.subTest(target=str(target)):
                    conn = sqlite3.connect(target)
                    conn.execute("CREATE TABLE t (x)")
                    conn.close()
            uri = (Path(tmp) / "uri.db").as_uri()
            sqlite3.connect(f"{uri}?mode=rwc", uri=True).close()

    def test_a_temp_product_database_migrates(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = connect_product(Path(tmp) / "platform.db")
            try:
                ensure_product_schema(conn)
            finally:
                conn.close()

    def test_paths_outside_the_data_directory_and_memory_targets_are_not_refused(self):
        self.assertFalse(realdata_guard.is_real_data_path(ROOT / "tests" / "fixtures" / "anything.db"))
        self.assertFalse(realdata_guard.is_real_data_path(ROOT / "data-not-really" / "platform.db"))
        self.assertFalse(realdata_guard.is_real_data_path(":memory:"))
        self.assertFalse(realdata_guard.is_real_data_path("file::memory:?cache=shared", uri=True))


class CodexConfigIsolationTests(unittest.TestCase):
    """No test reads the developer's own Codex settings: the model and effort come from ~/.codex/config.toml when .env names none."""

    CONFIG = 'model = "developers-own-model"\nmodel_reasoning_effort = "xhigh"\n'

    def test_the_suite_has_an_empty_codex_home_and_no_model_override(self):
        import os

        from opportunity_app.integrations.agent_providers import codex_model_settings

        home = Path(os.environ["CODEX_HOME"])
        self.assertTrue(home.is_dir())
        self.assertEqual(list(home.iterdir()), [], "an empty directory, so there is no config.toml to read")
        self.assertEqual((os.environ.get("PIPELINE_CODEX_MODEL"), os.environ.get("PIPELINE_CODEX_REASONING_EFFORT")), ("", ""))
        self.assertEqual(codex_model_settings(), ("", ""))

    def test_a_real_config_toml_in_the_users_home_is_not_read(self):
        """The developer's file lives at ~/.codex/config.toml; pretend it holds a model and the settings still come back empty."""
        from opportunity_app.integrations.agent_providers import codex_model_settings

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / ".codex").mkdir()
            (Path(tmp) / ".codex" / "config.toml").write_text(self.CONFIG, encoding="utf-8")
            with mock.patch.object(Path, "home", return_value=Path(tmp)):
                self.assertEqual(codex_model_settings(), ("", ""))

    def test_install_isolates_a_process_that_starts_with_a_real_codex_home_and_model_settings(self):
        """What a single-module `python -m unittest tests.test_x` relies on: importing the guard is enough."""
        import os
        import subprocess

        probe = (
            "import sys; sys.path.insert(0, 'tests'); import realdata_guard; realdata_guard.install(); "
            "from opportunity_app.integrations.agent_providers import codex_model_settings; "
            "print(repr(codex_model_settings()))"
        )
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "config.toml").write_text(self.CONFIG, encoding="utf-8")
            env = {**os.environ, "CODEX_HOME": tmp, "PIPELINE_CODEX_MODEL": "env-model", "PIPELINE_CODEX_REASONING_EFFORT": "high"}
            ran = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        self.assertEqual(ran.returncode, 0, ran.stderr)
        self.assertEqual(ran.stdout.strip(), "('', '')")


class EveryTestModuleIsGuardedTests(unittest.TestCase):
    """`python -m unittest tests.test_x` runs one module alone, so each module must install the guard through an import of its own."""

    # Modules that import one of these install the guard as a side effect (each of these imports helpers_platform or the guard).
    INSTALLERS = ("realdata_guard", "helpers_platform", "helpers_apply", "helpers_gmail")

    @staticmethod
    def _module_level(body):
        """Statements that run when the module is imported: the top level, and inside top-level try/if blocks."""
        for node in body:
            yield node
            if isinstance(node, ast.Try):
                for block in (node.body, *(handler.body for handler in node.handlers), node.orelse, node.finalbody):
                    yield from EveryTestModuleIsGuardedTests._module_level(block)
            elif isinstance(node, ast.If):
                yield from EveryTestModuleIsGuardedTests._module_level(node.body)
                yield from EveryTestModuleIsGuardedTests._module_level(node.orelse)

    @classmethod
    def _imported_names(cls, tree):
        """Last components of every module imported at import time, plus names pulled in by `from tests import x`."""
        names = set()
        for node in cls._module_level(tree.body):
            if isinstance(node, ast.Import):
                names.update(alias.name.rsplit(".", 1)[-1] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.rsplit(".", 1)[-1])
                if node.module == "tests":
                    names.update(alias.name for alias in node.names)
        return names

    def test_every_test_module_imports_something_that_installs_the_guard(self):
        modules = sorted((ROOT / "tests").glob("test_*.py"))
        self.assertGreater(len(modules), 50)
        unguarded = [path.name for path in modules
                     if not self._imported_names(ast.parse(path.read_text(encoding="utf-8"))) & set(self.INSTALLERS)]
        self.assertEqual(unguarded, [], "import realdata_guard (or a helper that installs it) at module level in these modules; "
                                        "a mention in a string or an import inside a function does not run on import")

    def test_local_helper_imports_resolve_when_a_module_runs_alone(self):
        """`python -m unittest tests.test_x` does not put tests/ on sys.path, so a bare `import helpers_x` needs one first."""
        local = {path.stem for path in (ROOT / "tests").glob("*.py") if not path.stem.startswith("test_")}
        broken = []
        for path in sorted((ROOT / "tests").glob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            path_added = False
            for node in tree.body:
                if isinstance(node, ast.Expr) and "sys.path.insert" in ast.unparse(node):
                    path_added = True
                elif isinstance(node, ast.Try):
                    continue  # the try/except ImportError fallback to `from tests import ...` handles both spellings
                elif isinstance(node, (ast.Import, ast.ImportFrom)) and not path_added:
                    targets = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                    if any(target.split(".")[0] in local for target in targets):
                        broken.append(f"{path.name}:{node.lineno}")
        self.assertEqual(broken, [], "insert tests/ on sys.path before these imports, or use the try/except tests.<module> fallback")

    def test_the_installing_helpers_really_import_it(self):
        tests = ROOT / "tests"
        self.assertIn("realdata_guard.install()", (tests / "helpers_platform.py").read_text(encoding="utf-8"))
        for name in ("helpers_apply", "helpers_gmail"):
            self.assertIn("from helpers_platform import", (tests / f"{name}.py").read_text(encoding="utf-8"), name)


if __name__ == "__main__":
    unittest.main()
