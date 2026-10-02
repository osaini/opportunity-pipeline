"""A suite-wide guard: no test may open the real database files under data/.

AGENTS.md hard rule 1: never touch data/platform.db (real application history) or data/pipeline.db. Tests isolate themselves by
patching `pipeline_core.paths.DB_PATH` (about 30 sites) or by passing a temp path to create_app and connect_product. A refactor that moves
one of those globals, or a new test that forgets the patch, would quietly open the real file: sqlite creates and migrates a
missing database and opens an existing one, and nothing fails.

`install()` wraps `sqlite3.connect` for the whole test process so that opening any file inside a real data directory
raises RealDataAccessError before the file is opened or created. Every module in the project opens SQLite through
`sqlite3.connect`, so this sits below `pipeline_core.store.connect`, `database.connect_product`, the read model and every helper.

It lives in test code: nothing under opportunity_app/, pipeline.py or pipeline_core/ knows about it, and outside a test process
(nothing imports this file) production behaviour is untouched. It is installed from tests/conftest.py (pytest: the unit suite
and tests/ui) and by every tests/test_*.py module, directly or through helpers_platform, helpers_apply or helpers_gmail, so a
single-module run (`python -m unittest tests.test_pipeline`) is covered too. tests/test_real_data_guard.py fails when a module
is added without one of those imports. Installing twice is harmless.

`sqlite3.connect` is not the only way into data/: the deep search's lock file, its reports, the logs and dated backups are plain
files. The same install therefore adds one audit hook (sys.addaudithook, cheap: one dict lookup for every event it does not
care about) that refuses every write there, before the call is made, with the same RealDataAccessError: an open that can
create or change a file (write, append, create, truncate or read-write), mkdir, remove, rmdir and rename. Reading is left alone,
because a few tests read the tracked files in data/ (the sample jobs CSV); the databases are protected from reads by the sqlite
wrapper above, where a read is what a migration or a lock would follow. A test that needs a folder of reports points its code at a temp directory (patch
discovery.REPORT_DIR and the like), as it already does for the database. An audit hook cannot be removed, so uninstall() only
turns it off.

The same install also gives the process an empty CODEX_HOME and no Codex model or effort variable (isolate_codex_config), so no
test reads the developer's own ~/.codex/config.toml. It is here because every test module already imports this file one way or
another, which is what makes it hold for a single-module `unittest` run too.

The exception derives from BaseException on purpose: code under test that wraps a database open in `except Exception` or
`except sqlite3.Error` to degrade gracefully must not be able to hide an open of the real file.

Not covered: a child process a test starts. The sandbox servers those tests launch are pointed at temp databases by their own
arguments.

Nothing here is named test*, so pytest never collects it.
"""

import atexit
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class RealDataAccessError(BaseException):
    """A test tried to open a database file in a real data directory."""


def real_data_dirs(root=ROOT):
    """The data directories holding real history: this checkout's, and the main checkout's when this is a git worktree.

    A worktree's `.git` is a file ("gitdir: <main>/.git/worktrees/<name>"), and the app's real data lives in the main checkout.
    """
    root = Path(root)
    found = [root / "data"]
    marker = root / ".git"
    try:
        if marker.is_file():
            text = marker.read_text(encoding="utf-8").strip()
            if text.startswith("gitdir:"):
                gitdir = Path(text.split(":", 1)[1].strip())
                if not gitdir.is_absolute():
                    gitdir = root / gitdir
                gitdir = gitdir.resolve()
                if gitdir.parent.name == "worktrees" and gitdir.parent.parent.name == ".git":
                    found.append(gitdir.parent.parent.parent / "data")
    except (OSError, ValueError):
        pass
    resolved = []
    for directory in found:
        try:
            resolved.append(directory.resolve())
        except OSError:
            resolved.append(directory)
    return tuple(dict.fromkeys(resolved))


def _target_paths(database, uri):
    """The filesystem paths a sqlite3.connect(database, uri=uri) call could open; empty for an in-memory or unparseable target.

    More than one when the call is ambiguous: a "file:" name is a URI with uri=True and a literal file name otherwise, and the
    guard refuses either reading. A plain path is checked whatever `uri` says, since sqlite opens it as an ordinary filename.
    """
    try:
        text = os.fsdecode(os.fspath(database))
    except TypeError:
        return []
    if not text or text == ":memory:":
        return []
    paths = [Path(text).expanduser()]
    if text.startswith("file:"):
        parsed = urllib.parse.urlparse(text)
        raw = urllib.parse.unquote(parsed.path or parsed.netloc)
        if re.match(r"^/[A-Za-z]:", raw):  # file:///C:/x/y.db
            raw = raw[1:]
        in_memory = "memory" in urllib.parse.parse_qs(parsed.query).get("mode", [])  # the exact parameter, not a substring
        if uri and (raw in ("", ":memory:") or in_memory):
            return []
        if raw not in ("", ":memory:") and not in_memory:
            paths.append(Path(raw).expanduser())
    return paths


def is_real_data_path(database, *, uri=False, dirs=None):
    """Whether opening `database` would open anything inside a real data directory.

    Any file, whatever its name: the real directory holds the databases, their -wal/-shm/-journal files and dated backups
    (platform.db.pre-0047-backup and the like), and no test has a reason to open a database anywhere under it.
    """
    directories = tuple(dirs if dirs is not None else real_data_dirs())
    for path in _target_paths(database, uri):
        try:
            resolved = (path if path.is_absolute() else Path.cwd() / path).resolve()
        except OSError:
            continue
        if any(resolved.is_relative_to(directory) for directory in directories):
            return True
    return False


# Audit events that name a path, and which of their arguments are paths.
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
_PATH_EVENTS = {"open": (0,), "os.mkdir": (0,), "os.remove": (0,), "os.rmdir": (0,), "os.rename": (0, 1)}
_audit = {"hooked": False, "on": False, "prefixes": ()}


def _inside_prefixes(value, prefixes):
    """Whether a path argument of an audit event (a str, bytes or path object; a file descriptor or None names no path) is in one."""
    if not isinstance(value, (str, bytes, os.PathLike)):
        return False
    try:
        text = os.path.normcase(os.path.abspath(os.fsdecode(value)))
    except (OSError, ValueError, TypeError):
        return False
    return any(text == prefix or text.startswith(prefix + os.sep) for prefix in prefixes)


def _prefixes(dirs):
    return tuple(dict.fromkeys(os.path.normcase(os.path.abspath(str(directory))) for directory in dirs))


def audit_refuses(event, args, dirs=None):
    """Whether this audit event is a write to a file or folder inside a real data directory (see the module docstring)."""
    indexes = _PATH_EVENTS.get(event)
    if indexes is None:
        return False
    if event == "open":  # (path, mode, flags): a read-only open is let through
        mode, flags = (tuple(args) + (None, None))[1:3]
        if not ((isinstance(mode, str) and any(char in mode for char in "wax+")) or (isinstance(flags, int) and flags & _WRITE_FLAGS)):
            return False
    prefixes = _audit["prefixes"] if dirs is None else _prefixes(dirs)
    return any(index < len(args) and _inside_prefixes(args[index], prefixes) for index in indexes)


def _audit_hook(event, args):
    if _audit["on"] and audit_refuses(event, args):
        raise RealDataAccessError(
            f"a test tried to use a file in a real data directory ({event} {args[0]!r}); AGENTS.md hard rule 1. Point the code "
            "under test at a temp directory (patch discovery.REPORT_DIR and the like) and never at data/."
        )


def _install_audit_hook(dirs):
    _audit["prefixes"] = _prefixes(dirs)
    _audit["on"] = True
    if not _audit["hooked"]:
        _audit["hooked"] = True
        sys.addaudithook(_audit_hook)


_GUARD_NAME = "guarded_connect"
_CODEX_ENV = {"PIPELINE_CODEX_MODEL": "", "PIPELINE_CODEX_REASONING_EFFORT": ""}
_codex_home = None


def isolate_codex_config():
    """Point CODEX_HOME at an empty temp directory and clear the two Codex setting variables, for the whole process.

    codex_model_settings() reads the student's model and reasoning effort from PIPELINE_CODEX_MODEL and
    PIPELINE_CODEX_REASONING_EFFORT, and otherwise from $CODEX_HOME/config.toml (~/.codex/config.toml by default). A test that
    builds a Codex command would then pick up the developer's own model, and its expected argv would differ from machine to
    machine. With an empty CODEX_HOME there is no file to read, so a test sets the variables itself when it wants a model.
    A test that does so with mock.patch.dict restores these values, not the developer's. Idempotent.
    """
    global _codex_home
    if _codex_home is None:
        _codex_home = tempfile.mkdtemp(prefix="codex-home-for-tests-")
        atexit.register(shutil.rmtree, _codex_home, ignore_errors=True)
    os.environ["CODEX_HOME"] = _codex_home
    os.environ.update(_CODEX_ENV)


def install():
    """Wrap sqlite3.connect and add the file audit hook once for this process, and isolate the Codex settings. Idempotent."""
    isolate_codex_config()
    dirs = real_data_dirs()
    _install_audit_hook(dirs)
    if getattr(sqlite3.connect, "__name__", "") == _GUARD_NAME:
        return
    real_connect = sqlite3.connect

    def guarded_connect(database=None, *args, **kwargs):
        # `uri` is the eighth positional parameter of the old signature and keyword-only after that.
        uri = kwargs.get("uri", args[6] if len(args) > 6 else False)
        if database is not None and is_real_data_path(database, uri=bool(uri), dirs=dirs):
            raise RealDataAccessError(
                f"a test tried to open a real data file ({database!r}); AGENTS.md hard rule 1. Point it at a temp copy "
                "(tests/helpers_platform.build_and_migrate, or patch pipeline_core.paths.DB_PATH) and never at data/*.db."
            )
        return real_connect(database, *args, **kwargs)

    guarded_connect.__name__ = _GUARD_NAME
    guarded_connect.__wrapped__ = real_connect
    sqlite3.connect = guarded_connect


def uninstall():
    """Restore sqlite3.connect and turn the audit hook off (used only by this guard's own tests)."""
    _audit["on"] = False
    current = sqlite3.connect
    if getattr(current, "__name__", "") == _GUARD_NAME:
        sqlite3.connect = current.__wrapped__
