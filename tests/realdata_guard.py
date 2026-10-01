"""A suite-wide guard: no test may open the real database files under data/.

AGENTS.md hard rule 1: never touch data/platform.db (real application history) or data/pipeline.db. Tests isolate themselves by
patching `pipeline.DB_PATH` (about 30 sites) or by passing a temp path to create_app and connect_product. A refactor that moves
one of those globals, or a new test that forgets the patch, would quietly open the real file: sqlite creates and migrates a
missing database and opens an existing one, and nothing fails.

`install()` wraps `sqlite3.connect` for the whole test process so that opening any database file inside a real data directory
raises RealDataAccessError before the file is opened or created. Every module in the project opens SQLite through
`sqlite3.connect`, so this sits below `pipeline.connect`, `schema.connect_product`, the read model and every helper.

It lives in test code: nothing under opportunity_app/, pipeline.py or pipeline_core/ knows about it, and outside a test process
(nothing imports this file) production behaviour is untouched. It is installed from tests/conftest.py (pytest: the unit suite
and tests/ui), from tests/helpers_platform.py (imported by nearly every unittest module, so `python -m unittest tests.test_x`
is covered too) and from tests/test_real_data_guard.py (so `unittest discover` is covered even for a module that imports
neither). Installing twice is harmless.

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
# A database file, or the journal files sqlite keeps beside one.
_DATABASE_FILE = re.compile(r"\.(?:db|sqlite3?)(?:-wal|-shm|-journal)?$", re.IGNORECASE)


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


def _target_path(database, uri):
    """The filesystem path a sqlite3.connect(database) call would open, or None for an in-memory or unparseable target."""
    try:
        text = os.fsdecode(os.fspath(database))
    except TypeError:
        return None
    if not text or text == ":memory:":
        return None
    if uri or text.startswith("file:"):
        if not text.startswith("file:"):
            return None
        parsed = urllib.parse.urlparse(text)
        raw = urllib.parse.unquote(parsed.path or parsed.netloc)
        if re.match(r"^/[A-Za-z]:", raw):  # file:///C:/x/y.db
            raw = raw[1:]
        if raw in ("", ":memory:") or "mode=memory" in parsed.query:
            return None
        text = raw
    return Path(text).expanduser()


def is_real_data_path(database, *, uri=False, dirs=None):
    """Whether opening `database` would open a database file inside a real data directory."""
    path = _target_path(database, uri)
    if path is None or not _DATABASE_FILE.search(path.name):
        return False
    try:
        resolved = (path if path.is_absolute() else Path.cwd() / path).resolve()
    except OSError:
        return False
    return any(resolved.is_relative_to(directory) for directory in (dirs if dirs is not None else real_data_dirs()))


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
