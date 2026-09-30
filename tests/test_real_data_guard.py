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

    def test_a_relative_path_and_dot_dot_segments_are_resolved_before_the_check(self):
        with mock.patch("os.getcwd", return_value=str(ROOT)), mock.patch.object(Path, "cwd", return_value=ROOT):
            self.refuse(lambda: sqlite3.connect("data/platform.db"))
            self.refuse(lambda: sqlite3.connect("tests/../data/platform.db"))

    def test_uri_forms_are_refused(self):
        uri = DATA.joinpath("platform.db").as_uri()
        self.refuse(lambda: sqlite3.connect(f"{uri}?mode=ro", uri=True))
        self.refuse(lambda: sqlite3.connect(uri, uri=True))

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

    def test_a_non_database_file_in_data_is_not_a_database_open(self):
        self.assertFalse(realdata_guard.is_real_data_path(DATA / "manual_jobs.csv"))
        self.assertFalse(realdata_guard.is_real_data_path(DATA / "web.log"))
        self.assertFalse(realdata_guard.is_real_data_path(":memory:"))
        self.assertFalse(realdata_guard.is_real_data_path("file::memory:?cache=shared", uri=True))


if __name__ == "__main__":
    unittest.main()
