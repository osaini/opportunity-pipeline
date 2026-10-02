"""The suite-wide guard against opening the real data/*.db (tests/realdata_guard.py), and proof that it bites.

Importing this module installs the guard, which is what protects a plain `python -m unittest discover -s tests` run (pytest gets
it from tests/conftest.py). The tests below never touch a real database: they assert the open is refused before it happens.
"""

import ast
import contextlib
import io
import os
import shutil
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
_AUDITED_AT_IMPORT = realdata_guard._audit["prefixes"]

from pipeline_core import paths, store
from opportunity_app import DEFAULT_LEGACY_DB, DEFAULT_PLATFORM_DB
from opportunity_app.core.schema import ensure_product_schema
from opportunity_app.core.database import connect_product

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"


class GuardIsInstalledTests(unittest.TestCase):
    def test_sqlite_connect_is_the_guarded_one_in_this_process(self):
        self.assertEqual(sqlite3.connect.__name__, "guarded_connect")

    def test_installing_twice_does_not_stack_wrappers(self):
        before = sqlite3.connect
        realdata_guard.install()
        self.assertIs(sqlite3.connect, before)


class OpeningRealDataFailsLoudlyTests(unittest.TestCase):
    """Opening a file in a guarded data directory fails before anything is opened or created.

    These tests never make a filesystem call into the checkout's real data/. Each one guards a temp directory named data/ in the
    sqlite wrapper the way the real ones are guarded (the real ones stay guarded too) and probes inside it. The audit hook is
    left off for it: code such as connect_product creates its parent directory first, and the hook would refuse that mkdir
    before the wrapper is reached, which is not what these tests are about (RealDataFilesAreRefusedTests covers the hook). What the real directory would refuse is checked with pure functions: is_real_data_path judges a path without
    opening it, so the real names, the real defaults and the real relative spellings are all asserted that way."""

    NAMES = ("platform.db", "pipeline.db", "platform.db-wal", "platform.db-shm", "old.sqlite", "backup.sqlite3",
             "platform.db.pre-0047-backup", "platform.db.pre-0029-backup-wal", "platform.db.bak-pre-x", "notes.txt")

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="guard-data-")).resolve()
        self.data = self.root / "data"
        self.data.mkdir()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.addCleanup(realdata_guard.guard_extra_dir(self.data, audit=False))

    def listing(self):
        return sorted(os.listdir(self.data))

    def refuse(self, call):
        before = self.listing()
        with self.assertRaises(realdata_guard.RealDataAccessError):
            call()
        self.assertEqual(self.listing(), before, "a refused open must not create or touch the file")
        self.assertEqual(before, [], "nothing in the guarded directory before or after")

    def test_the_probe_directory_is_guarded_like_a_real_data_directory(self):
        self.assertIn(self.data, realdata_guard._extra_dirs)
        self.assertEqual(realdata_guard._audit["prefixes"], _AUDITED_AT_IMPORT, "the audit hook's prefixes are untouched")
        for directory in realdata_guard.real_data_dirs():
            self.assertNotIn(directory, realdata_guard._extra_dirs)
        self.doCleanups()
        self.assertNotIn(self.data, realdata_guard._extra_dirs)
        self.assertEqual(realdata_guard._audit["prefixes"], _AUDITED_AT_IMPORT)
        self.assertFalse(self.root.exists())

    def test_the_real_names_are_refused_in_the_real_directory_by_the_same_judge(self):
        for name in self.NAMES:
            with self.subTest(name=name):
                for target in (DATA / name, str(DATA / name)):
                    self.assertTrue(realdata_guard.is_real_data_path(target))
                self.assertTrue(realdata_guard.is_real_data_path(f"{DATA.joinpath(name).as_uri()}?mode=ro", uri=True))
                self.assertTrue(realdata_guard.is_real_data_path(str(DATA / name), uri=True))

    def test_a_direct_open_of_either_database_is_refused(self):
        for name in ("platform.db", "pipeline.db"):
            with self.subTest(name=name):
                self.refuse(lambda: sqlite3.connect(self.data / name))
                self.refuse(lambda: sqlite3.connect(str(self.data / name)))

    def test_the_journal_files_and_other_sqlite_suffixes_are_refused_too(self):
        for name in ("platform.db-wal", "platform.db-shm", "old.sqlite", "backup.sqlite3"):
            with self.subTest(name=name):
                self.refuse(lambda: sqlite3.connect(self.data / name))

    def test_dated_backups_and_any_other_name_in_the_data_directory_are_refused(self):
        # The real directory holds platform.db.pre-0047-backup, platform.db.bak-pre-callprep-regen-2026-09-29 and similar.
        for name in ("platform.db.pre-0047-backup", "platform.db.pre-0029-backup-wal", "platform.db.bak-pre-x", "notes.txt"):
            with self.subTest(name=name):
                self.refuse(lambda: sqlite3.connect(self.data / name))

    def test_a_relative_path_and_dot_dot_segments_are_resolved_before_the_check(self):
        # The real process cwd moves to the temp root, so the file sqlite would open and the file the guard judges are the same:
        # were the wrapper to let one through, it would land in the temp data/, never in the checkout's.
        (self.root / "tests").mkdir()
        with contextlib.chdir(self.root):
            self.refuse(lambda: sqlite3.connect("data/platform.db"))
            self.refuse(lambda: sqlite3.connect("tests/../data/platform.db"))
        with mock.patch("os.getcwd", return_value=str(ROOT)), mock.patch.object(Path, "cwd", return_value=ROOT):
            self.assertTrue(realdata_guard.is_real_data_path("data/platform.db"))
            self.assertTrue(realdata_guard.is_real_data_path("tests/../data/platform.db"))
            self.assertFalse(realdata_guard.is_real_data_path("tests/data/platform.db"))

    def test_uri_forms_are_refused(self):
        uri = self.data.joinpath("platform.db").as_uri()
        self.refuse(lambda: sqlite3.connect(f"{uri}?mode=ro", uri=True))
        self.refuse(lambda: sqlite3.connect(uri, uri=True))

    def test_an_ordinary_path_is_refused_even_when_uri_is_true(self):
        # With uri=True sqlite still opens a name that does not start with "file:" as an ordinary filename.
        for name in ("platform.db", "pipeline.db", "platform.db.pre-0047-backup"):
            with self.subTest(name=name):
                self.refuse(lambda: sqlite3.connect(str(self.data / name), uri=True))
                self.refuse(lambda: sqlite3.connect(self.data / name, uri=True))
        with contextlib.chdir(self.root):
            self.refuse(lambda: sqlite3.connect("data/platform.db", uri=True))

    def test_memory_mode_is_the_exact_query_parameter_not_a_substring(self):
        uri = self.data.joinpath("platform.db").as_uri()
        self.refuse(lambda: sqlite3.connect(f"{uri}?mode=ro&unused=mode=memory", uri=True))
        self.refuse(lambda: sqlite3.connect(f"{uri}?mode=ro&note=memory", uri=True))
        real_uri = DATA.joinpath("platform.db").as_uri()
        self.assertTrue(realdata_guard.is_real_data_path(f"{real_uri}?mode=ro&unused=mode=memory", uri=True))
        self.assertTrue(realdata_guard.is_real_data_path(f"{real_uri}?mode=ro&note=memory", uri=True))
        self.assertFalse(realdata_guard.is_real_data_path(f"{real_uri}?mode=memory", uri=True))
        self.assertFalse(realdata_guard.is_real_data_path(f"{real_uri}?cache=shared&mode=memory", uri=True))

    def test_the_defaults_the_code_uses_are_real_data_paths(self):
        # Judged by pure functions: opening the defaults would be the very thing this guard exists to stop.
        self.assertTrue(realdata_guard.is_real_data_path(DEFAULT_PLATFORM_DB))
        self.assertTrue(realdata_guard.is_real_data_path(DEFAULT_LEGACY_DB))
        self.assertTrue(realdata_guard.is_real_data_path(paths.ROOT / "data" / "pipeline.db"), "the default of paths.DB_PATH")
        self.assertEqual(DEFAULT_PLATFORM_DB.name, "platform.db")
        self.assertEqual(DEFAULT_LEGACY_DB.name, "pipeline.db")
        self.assertEqual(connect_product.__defaults__, (DEFAULT_PLATFORM_DB,), "connect_product's default path")

    def test_connect_product_with_its_default_path_is_refused_by_the_sqlite_wrapper(self):
        # The default is swapped for a file under the guarded probe directory, whose parent exists, so the refusal can only
        # come from the sqlite wrapper (a missing parent would be created first and refused by the audit hook instead).
        default = self.data / "platform.db"
        with mock.patch.object(connect_product, "__defaults__", (default,)):
            with self.assertRaisesRegex(realdata_guard.RealDataAccessError, "tried to open a real data file"):
                connect_product()
            with self.assertRaisesRegex(realdata_guard.RealDataAccessError, "tried to open a real data file"):
                connect_product(read_only=True)
        with self.assertRaisesRegex(realdata_guard.RealDataAccessError, "tried to open a real data file"):
            connect_product(default, read_only=True)
        self.assertEqual(self.listing(), [])

    def test_a_legacy_test_that_forgot_to_patch_db_path_is_caught(self):
        # paths.DB_PATH is the one global about thirty tests patch. Unpatched, store.connect() would open the real file;
        # pointed at the guarded probe directory it shows the wrapper refuses it, with the parent already there.
        with mock.patch.object(paths, "DB_PATH", self.data / "pipeline.db"):
            with self.assertRaisesRegex(realdata_guard.RealDataAccessError, "tried to open a real data file"):
                store.connect()
        self.assertEqual(self.listing(), [])

    def test_code_that_catches_exceptions_cannot_swallow_it(self):
        def swallowing():
            try:
                sqlite3.connect(self.data / "platform.db")
            except Exception:  # noqa: BLE001 - the point: a broad handler must not hide this
                return "swallowed"

        with self.assertRaises(realdata_guard.RealDataAccessError):
            swallowing()
        with self.assertRaises(realdata_guard.RealDataAccessError):
            try:
                sqlite3.connect(self.data / "platform.db")
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
    comes before the call, so a refused create leaves nothing behind (and a file that does appear fails the test).

    These tests never make a filesystem call into a real data/ directory themselves. Each one adds a temp directory to the
    audited prefixes (the real ones stay audited) and probes inside it, which exercises the same hook; what the real
    directories would refuse is checked with pure functions (is_real_data_path, audit_refuses(..., dirs=real_data_dirs()))."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="guard-probe-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        # Cleanups run last in, first out: the prefixes are restored before the temp directory is removed, which the
        # hook would otherwise refuse.
        self.addCleanup(realdata_guard._audit.update, dict(realdata_guard._audit))
        realdata_guard._audit["prefixes"] = realdata_guard._prefixes([*realdata_guard.real_data_dirs(), self.root])
        realdata_guard._audit["on"] = True
        self.name = f"guard-probe-{uuid.uuid4().hex}"
        self.target = self.root / f"{self.name}.txt"

    def refuse(self, call):
        with self.assertRaises(realdata_guard.RealDataAccessError):
            call()
        self.assertFalse(self.target.exists() or (self.root / self.name).exists(), "a refused call must create nothing")

    def test_the_probe_directory_is_audited_and_the_real_ones_stay_audited(self):
        prefixes = realdata_guard._audit["prefixes"]
        self.assertIn(os.path.normcase(str(self.root)), prefixes)
        for directory in realdata_guard.real_data_dirs():
            self.assertIn(os.path.normcase(os.path.abspath(str(directory))), prefixes)
        self.assertFalse(realdata_guard.is_real_data_path(self.target), "the probe directory is not a real data directory")

    def test_the_prefixes_are_restored_when_a_test_ends(self):
        self.assertIn(os.path.normcase(str(self.root)), realdata_guard._audit["prefixes"])
        self.doCleanups()
        self.assertNotIn(os.path.normcase(str(self.root)), realdata_guard._audit["prefixes"])
        self.assertEqual(realdata_guard._audit["prefixes"], _AUDITED_AT_IMPORT)
        self.assertFalse(self.root.exists(), "the probe directory was removed, which the restored hook let through")

    def test_creating_writing_appending_or_updating_a_file_is_refused(self):
        for mode in ("w", "a", "x", "wb", "ab", "r+", "rb+"):
            with self.subTest(mode=mode):
                self.refuse(lambda: open(self.target, mode))
        for flags in (os.O_CREAT | os.O_WRONLY, os.O_RDWR, os.O_WRONLY | os.O_APPEND, os.O_CREAT | os.O_EXCL | os.O_WRONLY):
            with self.subTest(flags=flags):
                self.refuse(lambda: os.open(self.target, flags))
        self.refuse(lambda: self.target.touch())
        self.refuse(lambda: self.target.write_text("x", encoding="utf-8"))
        self.refuse(lambda: open(str(self.root / ".." / self.root.name / self.target.name), "w"))

    def test_making_removing_or_renaming_inside_the_directory_is_refused(self):
        self.refuse(lambda: os.mkdir(self.root / self.name))
        self.refuse(lambda: (self.root / self.name).mkdir(parents=True, exist_ok=True))
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.txt"
            source.write_text("x", encoding="utf-8")
            self.refuse(lambda: os.rename(source, self.target))
            self.assertTrue(source.exists())
            self.refuse(lambda: os.replace(source, self.target))
        self.refuse(lambda: os.remove(self.target))

    def test_the_deep_search_lock_default_is_inside_the_protected_directory_and_refused(self):
        """The test that started this: _RunLock(REPORT_DIR / ...) on the default REPORT_DIR created a real lock in data/.

        The real default is judged by pure functions; a lock under the probe directory shows the hook stops _RunLock itself."""
        from opportunity_app.outreach import discovery

        lock = discovery.REPORT_DIR / "outreach-discovery.lock"
        self.assertTrue(realdata_guard.is_real_data_path(lock))
        real = realdata_guard.real_data_dirs()
        for event, args in (("os.mkdir", (str(lock.parent), 0o777)),
                            ("open", (str(lock), None, os.O_CREAT | os.O_EXCL | os.O_WRONLY)),
                            ("os.remove", (str(lock),))):
            with self.subTest(event=event):
                self.assertTrue(realdata_guard.audit_refuses(event, args, dirs=real))
        held = None
        try:
            with self.assertRaises(realdata_guard.RealDataAccessError):
                held = discovery._RunLock(self.root / "outreach-discovery.lock").__enter__()
        finally:
            if held is not None:
                held.__exit__()
        self.assertFalse((self.root / "outreach-discovery.lock").exists())

    def test_temp_folders_and_look_alike_names_are_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            for folder in (Path(tmp), Path(tmp) / "data"):
                folder.mkdir(exist_ok=True)
                (folder / "ok.txt").write_text("fine", encoding="utf-8")
                self.assertEqual((folder / "ok.txt").read_text(encoding="utf-8"), "fine")
                (folder / "ok.txt").unlink()
        self.assertFalse(realdata_guard.is_real_data_path(ROOT / "data-not-really" / "x.txt"))
        self.assertFalse(realdata_guard.is_real_data_path(ROOT / "tests" / "data" / "x.txt"))

    def test_reading_other_names_is_let_through_because_tracked_files_live_there(self):
        self.assertFalse(realdata_guard.audit_refuses("open", (str(self.target), "r", 0)))
        self.assertFalse(realdata_guard.audit_refuses("open", (str(self.target), "rb", os.O_RDONLY)))
        self.assertFalse(realdata_guard.audit_refuses("open", (self.target, None, os.O_RDONLY)))
        real = realdata_guard.real_data_dirs()
        self.assertFalse(realdata_guard.audit_refuses("open", (str(real[0] / "x.csv"), "r", 0), dirs=real))
        self.assertFalse(realdata_guard.audit_refuses("open", (real[0] / "x.csv", None, os.O_RDONLY), dirs=real))
        with self.assertRaises(FileNotFoundError):
            open(self.target, "r")
        with self.assertRaises(FileNotFoundError):
            open(self.target, "rb")

    def test_reading_a_database_or_a_backup_of_one_is_refused(self):
        """A raw read_bytes() or copy of a real database goes round the sqlite wrapper, so the hook refuses database names."""
        real = realdata_guard.real_data_dirs()
        for name in ("platform.db", "pipeline.db", "platform.db-wal", "platform.db.pre-0047-backup", "old.sqlite", "b.sqlite3"):
            with self.subTest(name=name):
                self.assertTrue(realdata_guard.audit_refuses("open", (str(real[0] / name), "rb", os.O_RDONLY), dirs=real))
                self.refuse(lambda: (self.root / name).read_bytes())
                self.refuse(lambda: shutil.copyfile(self.root / name, Path(tempfile.gettempdir()) / f"{self.name}.copy"))
        self.assertFalse(realdata_guard.audit_refuses("open", (str(real[0] / "manual_jobs.csv"), "r", 0), dirs=real))

    def test_truncating_a_file_by_name_is_refused(self):
        real = realdata_guard.real_data_dirs()
        self.assertTrue(realdata_guard.audit_refuses("os.truncate", (str(real[0] / "platform.db"), 0), dirs=real))
        self.assertFalse(realdata_guard.audit_refuses("os.truncate", (3, 0), dirs=real), "a descriptor names no path")
        self.refuse(lambda: os.truncate(self.target, 0))

    def test_a_path_through_a_link_to_a_data_directory_is_judged_by_where_it_leads(self):
        with tempfile.TemporaryDirectory() as tmp:
            target, link = Path(tmp).resolve() / "elsewhere", Path(tmp).resolve() / "linked-data"
            target.mkdir()
            try:
                if os.name == "nt":
                    import _winapi
                    _winapi.CreateJunction(str(target), str(link))
                else:
                    os.symlink(target, link, target_is_directory=True)
            except (OSError, AttributeError) as error:
                self.skipTest(f"cannot make a directory link here: {error}")
            for dirs, spelled in (([target], link / "platform.db"), ([link], target / "platform.db")):
                with self.subTest(guarded=dirs[0].name, spelled=spelled.parent.name):
                    self.assertTrue(realdata_guard.audit_refuses("open", (str(spelled), "w", 0), dirs=dirs))
                    self.assertTrue(realdata_guard.audit_refuses("os.truncate", (str(spelled), 0), dirs=dirs))
            os.rmdir(link) if os.name == "nt" else link.unlink()

    def test_a_name_relative_to_a_directory_descriptor_is_judged_in_that_directory(self):
        """shutil.rmtree on Linux and macOS removes os.rmdir("data", dir_fd=<parent>): "data" is not the checkout's data/."""
        real = realdata_guard.real_data_dirs()
        elsewhere = Path(tempfile.gettempdir()).resolve() / "somewhere"
        with mock.patch.object(realdata_guard, "_fd_directory", return_value=str(elsewhere)):
            self.assertFalse(realdata_guard.audit_refuses("os.rmdir", ("data", 7), dirs=real))
            self.assertFalse(realdata_guard.audit_refuses("os.remove", ("platform.db", 7), dirs=real))
            self.assertTrue(realdata_guard.audit_refuses("os.rmdir", ("data", 7), dirs=[elsewhere / "data"]))
            self.assertTrue(realdata_guard.audit_refuses("os.rename", ("a", "data/x", None, 7), dirs=[elsewhere / "data"]))
        with mock.patch.object(realdata_guard, "_fd_directory", return_value=str(real[0].parent)):
            self.assertTrue(realdata_guard.audit_refuses("os.remove", ("data/platform.db", 7), dirs=real))
            self.assertTrue(realdata_guard.audit_refuses("os.mkdir", ("data", 0o777, 7), dirs=real))
        absolute = str(real[0] / "platform.db")
        self.assertTrue(realdata_guard.audit_refuses("os.remove", (absolute, 7), dirs=real), "an absolute path ignores dir_fd")

    def test_removing_a_temp_tree_that_holds_a_data_folder_is_let_through_from_the_checkout(self):
        with contextlib.chdir(ROOT), tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "data" / "nested").mkdir(parents=True)
            (Path(tmp) / "data" / "platform.db").write_bytes(b"")
            shutil.rmtree(Path(tmp) / "data")
            self.assertFalse((Path(tmp) / "data").exists())

    @unittest.skipIf(os.name == "nt", "Windows cannot open a directory descriptor")
    def test_a_real_directory_descriptor_is_resolved(self):
        fd = os.open(self.root, os.O_RDONLY)
        try:
            self.assertEqual(os.path.realpath(realdata_guard._fd_directory(fd)), os.path.realpath(self.root))
            self.assertTrue(realdata_guard.audit_refuses("os.mkdir", (self.name, 0o777, fd)))
            self.refuse(lambda: os.mkdir(self.name, dir_fd=fd))
        finally:
            os.close(fd)

    def test_the_hook_judges_only_events_that_name_a_path(self):
        refuse = realdata_guard.audit_refuses
        real = realdata_guard.real_data_dirs()
        for label, dirs, target in (("probe", None, self.target), ("real", real, real[0] / f"{self.name}.txt")):
            with self.subTest(directories=label):
                self.assertTrue(refuse("open", (str(target), "w", 0), dirs=dirs))
                self.assertTrue(refuse("open", (target, None, os.O_CREAT), dirs=dirs))
                self.assertTrue(refuse("os.rename", ("somewhere", str(target), None, None), dirs=dirs))
                self.assertFalse(refuse("open", (3, "w", 0), dirs=dirs), "an already-open descriptor names no path")
                self.assertFalse(refuse("open", (None, "w", 0), dirs=dirs))
                self.assertFalse(refuse("socket.connect", (object(), str(target)), dirs=dirs))


# Calls that look at a path without opening it for writing: they are a live filesystem call into a directory too.
_PROBES = (
    *((os, f"os.{name}", name) for name in ("stat", "lstat", "listdir", "scandir", "access")),
    *((os.path, f"os.path.{name}", name) for name in ("exists", "lexists", "isdir", "isfile")),
    *((Path, f"Path.{name}", name) for name in ("exists", "is_dir", "is_file", "stat", "lstat", "iterdir")),
)


@contextlib.contextmanager
def recording_calls_into(prefixes, seen):
    """Record into `seen` every filesystem call a test makes into `prefixes`, a refused write included.

    Two kinds are recorded: the audit events (open, mkdir, remove, rename; judged by realdata_guard.audit_refuses, which the
    audit hook calls for every event in the process) and the probes that raise no audit event: stat, lstat, exists, is_dir,
    is_file, listdir, scandir, access, iterdir. A probe made while the guard judges a path (is_real_data_path, real_data_dirs,
    _prefixes and audit_refuses resolve the path they are given, and on Linux and macOS os.path.realpath lstats each part) is the guard
    reading the path string, not a test touching the directory, so it is left out; a test's own call is not. The recorder's
    own check counts as judging too, or its realpath would record itself without end.
    """
    judging = [0]
    judge = realdata_guard.audit_refuses

    def judged(function):
        def wrapper(*args, **kwargs):
            judging[0] += 1
            try:
                return function(*args, **kwargs)
            finally:
                judging[0] -= 1
        return wrapper

    inside = judged(realdata_guard._inside_prefixes)
    # shutil.rmtree on Linux and macOS opens each folder as os.open("data", dir_fd=<its parent>), and the "open" audit event
    # carries no dir_fd, so a temp tree holding a data/ folder would read as the checkout's. While a tree outside the prefixes
    # is removed its events are left out; removing a tree inside them is recorded as such.
    cleaning = [0]
    real_rmtree = shutil.rmtree

    def rmtree(path, *args, **kwargs):
        if inside(path, prefixes):
            seen.append(("shutil.rmtree", path))
            return real_rmtree(path, *args, **kwargs)
        cleaning[0] += 1
        try:
            return real_rmtree(path, *args, **kwargs)
        finally:
            cleaning[0] -= 1

    @judged
    def recording_event(event, args, dirs=None):
        # The path arguments only, each joined to its dir_fd as the hook joins it: shutil.rmtree on Linux and macOS removes
        # os.rmdir("data", dir_fd=<a temp parent>), which is not the checkout's data/.
        paths = [realdata_guard._event_path(event, args, index) for index in realdata_guard._PATH_EVENTS.get(event, ()) if index < len(args)]
        if dirs is None and not cleaning[0] and any(inside(path, prefixes) for path in paths):
            seen.append((event, args[0]))
        return judge(event, args, dirs)

    def probing(name, function):
        def wrapper(*args, **kwargs):
            if not judging[0] and args and inside(args[0], prefixes):
                seen.append((name, args[0]))
            return function(*args, **kwargs)
        return wrapper

    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(realdata_guard, "audit_refuses", recording_event))
        stack.enter_context(mock.patch.object(shutil, "rmtree", rmtree))
        for name in ("is_real_data_path", "real_data_dirs", "_prefixes"):
            stack.enter_context(mock.patch.object(realdata_guard, name, judged(getattr(realdata_guard, name))))
        for owner, label, name in _PROBES:
            if hasattr(owner, name):
                stack.enter_context(mock.patch.object(owner, name, probing(label, getattr(owner, name))))
        yield


class NoGuardTestTouchesARealDataDirectoryTests(unittest.TestCase):
    """The tests that prove a test cannot touch data/ must not do the very thing they forbid: not a write, and not a stat either."""

    def run_cases(self, *cases, prefixes=None):
        real = realdata_guard._prefixes(realdata_guard.real_data_dirs()) if prefixes is None else prefixes
        seen = []
        stream = io.StringIO()
        suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(case) for case in cases)
        with recording_calls_into(real, seen):
            result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)
        return result, stream.getvalue(), seen

    def test_running_the_guard_tests_makes_no_filesystem_call_into_a_real_data_directory(self):
        result, output, seen = self.run_cases(RealDataFilesAreRefusedTests, OpeningRealDataFailsLoudlyTests)
        self.assertTrue(result.wasSuccessful(), output)
        self.assertGreater(result.testsRun, 15)
        self.assertEqual(seen, [], "these tests must probe a temp directory, not data/")

    def test_the_recorder_sees_a_write_and_a_stat_alike(self):
        """Verifies the instrument: the same recorder, pointed at a temp directory standing in for data/, sees every kind of call."""
        with tempfile.TemporaryDirectory() as tmp:
            stand_in = Path(tmp).resolve() / "data"
            stand_in.mkdir()
            target = stand_in / "platform.db"

            class Probing(unittest.TestCase):
                def test_it(self):
                    target.exists()
                    os.path.exists(target)
                    os.stat(stand_in)
                    os.listdir(stand_in)
                    Path(target).is_file()
                    open(target, "w").close()
                    os.mkdir(stand_in / "sub")

            result, output, seen = self.run_cases(Probing, prefixes=realdata_guard._prefixes([stand_in]))
        self.assertTrue(result.wasSuccessful(), output)
        self.assertGreaterEqual({event for event, _ in seen},
                                {"Path.exists", "os.path.exists", "os.stat", "os.listdir", "Path.is_file", "open", "os.mkdir"})


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
