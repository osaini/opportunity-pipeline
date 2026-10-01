"""A suite-wide guard: no test may open the real database files under data/.

AGENTS.md hard rule 1: never touch data/platform.db (real application history) or data/pipeline.db. Tests isolate themselves by
patching `pipeline.DB_PATH` (about 30 sites) or by passing a temp path to create_app and connect_product. A refactor that moves
one of those globals, or a new test that forgets the patch, would quietly open the real file: sqlite creates and migrates a
missing database and opens an existing one, and nothing fails.

`install()` wraps `sqlite3.connect` for the whole test process so that opening any file inside a real data directory
raises RealDataAccessError before the file is opened or created. Every module in the project opens SQLite through
`sqlite3.connect`, so this sits below `pipeline.connect`, `database.connect_product`, the read model and every helper.

It lives in test code: nothing under opportunity_app/, pipeline.py or pipeline_core/ knows about it, and outside a test process
(nothing imports this file) production behaviour is untouched. It is installed from tests/conftest.py (pytest: the unit suite
and tests/ui) and by every tests/test_*.py module, directly or through helpers_platform, helpers_apply or helpers_gmail, so a
single-module run (`python -m unittest tests.test_pipeline`) is covered too. tests/test_real_data_guard.py fails when a module
is added without one of those imports. Installing twice is harmless.

The exception derives from BaseException on purpose: code under test that wraps a database open in `except Exception` or
`except sqlite3.Error` to degrade gracefully must not be able to hide an open of the real file.

Not covered: a child process a test starts. The sandbox servers those tests launch are pointed at temp databases by their own
arguments.

Nothing here is named test*, so pytest never collects it.
"""

import os
import re
import sqlite3
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


_GUARD_NAME = "guarded_connect"


def install():
    """Wrap sqlite3.connect once for this process. Idempotent."""
    if getattr(sqlite3.connect, "__name__", "") == _GUARD_NAME:
        return
    dirs = real_data_dirs()
    real_connect = sqlite3.connect

    def guarded_connect(database=None, *args, **kwargs):
        # `uri` is the eighth positional parameter of the old signature and keyword-only after that.
        uri = kwargs.get("uri", args[6] if len(args) > 6 else False)
        if database is not None and is_real_data_path(database, uri=bool(uri), dirs=dirs):
            raise RealDataAccessError(
                f"a test tried to open a real data file ({database!r}); AGENTS.md hard rule 1. Point it at a temp copy "
                "(tests/helpers_platform.build_and_migrate, or patch pipeline.DB_PATH) and never at data/*.db."
            )
        return real_connect(database, *args, **kwargs)

    guarded_connect.__name__ = _GUARD_NAME
    guarded_connect.__wrapped__ = real_connect
    sqlite3.connect = guarded_connect


def uninstall():
    """Restore sqlite3.connect (used only by this guard's own tests)."""
    current = sqlite3.connect
    if getattr(current, "__name__", "") == _GUARD_NAME:
        sqlite3.connect = current.__wrapped__
