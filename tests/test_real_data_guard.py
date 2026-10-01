"""The suite-wide guard against opening the real data/*.db (tests/realdata_guard.py), and proof that it bites.

Importing this module installs the guard, which is what protects a plain `python -m unittest discover -s tests` run (pytest gets
it from tests/conftest.py). The tests below never touch a real database: they assert the open is refused before it happens.
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import realdata_guard

realdata_guard.install()

import pipeline
from opportunity_app import DEFAULT_LEGACY_DB, DEFAULT_PLATFORM_DB
from opportunity_app.schema import connect_product, ensure_product_schema

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
        # pipeline.DB_PATH is the one global about thirty tests patch. Unpatched, pipeline.connect() would open the real file.
        with mock.patch.object(pipeline, "DB_PATH", DATA / "pipeline.db"):
            self.refuse(pipeline.connect)

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


class EveryTestModuleIsGuardedTests(unittest.TestCase):
    """`python -m unittest tests.test_x` runs one module alone, so each module must install the guard through an import of its own."""

    # Modules that import one of these install the guard as a side effect (each of these imports helpers_platform or the guard).
    INSTALLERS = ("realdata_guard", "helpers_platform", "helpers_apply", "helpers_gmail")

    def test_every_test_module_imports_something_that_installs_the_guard(self):
        modules = sorted((ROOT / "tests").glob("test_*.py"))
        self.assertGreater(len(modules), 50)
        unguarded = [path.name for path in modules
                     if not any(name in path.read_text(encoding="utf-8") for name in self.INSTALLERS)]
        self.assertEqual(unguarded, [], "import realdata_guard and call install() in these modules")

    def test_the_installing_helpers_really_import_it(self):
        tests = ROOT / "tests"
        self.assertIn("realdata_guard.install()", (tests / "helpers_platform.py").read_text(encoding="utf-8"))
        for name in ("helpers_apply", "helpers_gmail"):
            self.assertIn("from helpers_platform import", (tests / f"{name}.py").read_text(encoding="utf-8"), name)


if __name__ == "__main__":
    unittest.main()
