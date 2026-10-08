"""Shared fixtures for platform-side unit tests."""

import atexit
import gc
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import zipfile
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

try:
    import realdata_guard
except ImportError:  # imported as tests.helpers_platform, with tests/ not on sys.path
    from tests import realdata_guard
# Every test that builds a fixture database imports this module, so a run of any one test file is guarded against opening
# data/*.db (AGENTS.md hard rule 1). See tests/realdata_guard.py.
realdata_guard.install()

PROFILE_REGIONS_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "profile_regions.json"

from opportunity_app import bootstrap
from opportunity_app.opportunities import legacy_sync
from opportunity_app.core import database, schema, timestamps
from opportunity_app.opportunities.legacy_sync import migrate_legacy_database

# What create_app (and the worker and CLI entry points) do as a process starts: fill the automation, scheduler and callback
# registries. A test that calls the ledger, the scheduler or a record's callbacks without building an app needs it too, and
# every test that builds a fixture database comes through this module. Calling it again is harmless.
bootstrap.register_all()

LEGACY_SCHEMA = """
CREATE TABLE jobs (
    id TEXT PRIMARY KEY,
    source_key TEXT NOT NULL,
    source_name TEXT NOT NULL,
    external_id TEXT NOT NULL,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    role_type TEXT NOT NULL DEFAULT 'other',
    url TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    posted_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    fingerprint TEXT NOT NULL,
    content_fingerprint TEXT NOT NULL DEFAULT '',
    duplicate_of TEXT,
    score INTEGER NOT NULL DEFAULT 0,
    score_explanation TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'discovered',
    notes TEXT NOT NULL DEFAULT '',
    applied_at TEXT,
    follow_up_at TEXT
);
"""

# The rows below were written for this day, and the tests that read them rely on the ages their dates had then: postings seven weeks
# old (between scoring's "+5 updated within 21 days" and "-5 posting timestamp over 60 days old", so a blank profile's matches carry
# no reason at all), an application 47 days old (short of the 60-day silent-application archive), follow-ups six weeks overdue, and
# a deadline three weeks past. Written as fixed dates they aged across those lines on 2026-10-07, so every date in them is moved
# forward by whole days to keep the distance from today it had from FIXTURE_AS_OF. Whole days keep each time of day.
FIXTURE_AS_OF = date(2026, 9, 25)


def fixture_shift(today: date | None = None) -> timedelta:
    """How far the fixture's dates move: from FIXTURE_AS_OF to today (UTC), in whole days."""
    return (today or datetime.now(timezone.utc).date()) - FIXTURE_AS_OF


def as_of_today(value: str, today: date | None = None) -> str:
    """A fixture date or ISO timestamp, written for FIXTURE_AS_OF, moved to the same distance from today (and in the same form)."""
    if "T" in value:
        return (datetime.fromisoformat(value) + fixture_shift(today)).isoformat()
    return (date.fromisoformat(value) + fixture_shift(today)).isoformat()


def as_of_today_text(value: str, today: date | None = None) -> str:
    """as_of_today for a date written out in a posting's text ("September 1, 2026")."""
    moved = date.fromisoformat(as_of_today(value, today))
    return f"{moved:%B} {moved.day}, {moved.year}"


JOBS = [
    (
        "job-a",
        "greenhouse:acme",
        "Acme Greenhouse",
        "a-1",
        "Acme Robotics",
        "Mechanical Engineering Intern",
        "Austin, TX",
        "internship",
        "https://example.com/jobs/a",
        f"Design mechanisms using SolidWorks. Apply by {as_of_today_text('2026-09-01')}.",
        as_of_today("2026-08-08T00:00:00+00:00"),
        as_of_today("2026-08-08T01:00:00+00:00"),
        as_of_today("2026-08-09T01:00:00+00:00"),
        1,
        "fp-a",
        "cfp-a",
        None,
        91,
        '["35 base", "SolidWorks matches profile skills", "Austin target region"]',
        "shortlisted",
        "Strong fit",
        None,
        as_of_today("2026-08-14"),
    ),
    (
        "job-b",
        "lever:orbit",
        "Orbit Lever",
        "b-1",
        "Orbit Systems",
        "Controls Co-op",
        "Remote",
        "co-op",
        "https://example.com/jobs/b",
        "Summer 2027 controls role with Python.",
        as_of_today("2026-08-07T00:00:00+00:00"),
        as_of_today("2026-08-07T01:00:00+00:00"),
        as_of_today("2026-08-09T02:00:00+00:00"),
        1,
        "fp-b",
        "cfp-b",
        None,
        76,
        '["35 base"]',
        "applied",
        "Applied on employer site",
        as_of_today("2026-08-09T03:00:00+00:00"),
        as_of_today("2026-08-16"),
    ),
]


def build_profile(root: Path) -> Path:
    profile_path = root / "profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "name": "Test Student",
                "regions": [
                    {
                        "name": "Austin",
                        "radius": "close",
                        "bonus": 15,
                        "state_markers": ["tx", "texas"],
                        "aliases": ["greater austin"],
                        "places": ["austin", "round rock"],
                    }
                ],
                "remote_ok": True,
            }
        ),
        encoding="utf-8",
    )
    return profile_path


def _write_legacy_database(legacy_path: Path) -> None:
    conn = sqlite3.connect(legacy_path)
    try:
        conn.executescript(LEGACY_SCHEMA)
        conn.executemany(
            "INSERT INTO jobs VALUES(" + ",".join("?" * 23) + ")",
            JOBS,
        )
        conn.commit()
    finally:
        conn.close()


@contextmanager
def fast_throwaway_databases():
    """Open every product database made inside the block with ``synchronous=OFF``.

    A test database is deleted with its temp directory, so surviving a crash is
    worth nothing there, and each migration commits (and so fsyncs) once: turning
    the fsync off cuts a full migration roughly fourfold. This patches
    ``connect_product`` where the builders look it up (``database`` for this module's own
    calls, ``legacy_sync`` for the migration it runs) only for the duration of the block
    and only from test code; production connections keep SQLite's default
    ``synchronous=FULL``.
    """
    real_connect = database.connect_product

    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        if isinstance(conn, sqlite3.Connection):
            conn.execute("PRAGMA synchronous = OFF")
        return conn

    with mock.patch.object(database, "connect_product", connect), mock.patch.object(legacy_sync, "connect_product", connect):
        yield


def build_and_migrate_fresh(root: Path) -> tuple[Path, Path]:
    """Create a minimal legacy pipeline database and really migrate it to the product schema.

    Use this (rather than ``build_and_migrate``) when a test patches anything the
    migration reads (``schema.MIGRATIONS_DIR``, the company-tag rules, the legacy
    fixture rows), asserts on template-time stamps, or runs in a process that
    builds a database once (the UI suite's server, the sandbox server, the
    fuzzer): such a process gains nothing from a cache and must not leave a temp
    directory behind when it is terminated without running its exit handlers.
    """
    legacy_path = root / "pipeline.db"
    platform_path = root / "platform.db"
    _write_legacy_database(legacy_path)
    with fast_throwaway_databases():
        migrate_legacy_database(legacy_path, platform_path, build_profile(root))
    return legacy_path, platform_path


# --- process-wide migrated templates ------------------------------------------------
#
# Replaying every migration takes ~1 s and used to run in nearly every test's setUp.
# A template is migrated once per process, into a private temp directory that is
# removed at exit, and each caller gets a copy. Templates are keyed by the content of
# their inputs, so a caller with different rows or a different profile gets its own.
#
# Nothing may patch what a migration reads around the first call that builds a
# template (use ``build_and_migrate_fresh`` for that): the template would bake it in.
# Set PIPELINE_TEST_FRESH_DB=1 to bypass every template and migrate for real each time.

_TEMPLATE_LOCK = threading.Lock()
_TEMPLATE_ROOT: Path | None = None
_TEMPLATES: dict[tuple, "_Template"] = {}


class _Template:
    def __init__(self, directory: Path):
        self.directory = directory
        self.legacy = directory / "pipeline.db"
        self.platform = directory / "platform.db"
        self.profile = directory / "profile.json"


def _fresh_requested() -> bool:
    return os.environ.get("PIPELINE_TEST_FRESH_DB", "") not in ("", "0")


def _template_root() -> Path:
    global _TEMPLATE_ROOT
    if _TEMPLATE_ROOT is None:
        root = Path(tempfile.mkdtemp(prefix="pipeline-test-template-"))
        atexit.register(shutil.rmtree, root, ignore_errors=True)
        _TEMPLATE_ROOT = root
    return _TEMPLATE_ROOT


def _legacy_fingerprint(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        return hashlib.sha256("\n".join(conn.iterdump()).encode("utf-8")).hexdigest()
    finally:
        conn.close()


def _checkpoint_and_close(db_path: Path) -> None:
    """Fold the WAL into the main file so the file alone is a whole database."""
    gc.collect()  # a connection dropped without close() would keep the WAL open
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
    finally:
        conn.close()
    wal = db_path.with_name(db_path.name + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise RuntimeError(f"{wal} still holds committed pages; copying {db_path} alone would lose them")


def _template_for(key: tuple, build) -> _Template:
    with _TEMPLATE_LOCK:
        template = _TEMPLATES.get(key)
        if template is None:
            directory = Path(tempfile.mkdtemp(prefix="t-", dir=_template_root()))
            template = _Template(directory)
            build(template)
            _checkpoint_and_close(template.platform)
            _TEMPLATES[key] = template
        return template


def _restamp(platform_path: Path, legacy_path: Path | None) -> None:
    """Make a copied template read as if it had just been migrated from ``legacy_path``.

    ``migration_runs.source_path`` names the legacy database that was migrated,
    and ``schema_migrations.applied_at`` is read as "when this feature was
    switched on" (outreach_inbox._activated_at), so both must describe the copy,
    not the template.
    """
    conn = sqlite3.connect(platform_path)
    try:
        conn.execute("PRAGMA synchronous = OFF")
        if legacy_path is not None:
            conn.execute("UPDATE migration_runs SET source_path=?", (str(legacy_path.resolve()),))
        conn.execute("UPDATE schema_migrations SET applied_at=?", (timestamps.utc_now(),))
        conn.commit()
    finally:
        conn.close()


def migrate_cached(legacy_path: Path, platform_path: Path, profile_path: Path) -> None:
    """``migrate_legacy_database(legacy_path, platform_path, profile_path)`` from a copied template.

    For callers that start with no ``platform_path`` and ignore the returned
    MigrationResult. A caller that asserts on that result, or migrates a second
    time to test re-migration, should call ``migrate_legacy_database`` itself.
    ``profile_path`` is given the modification time the template's profile had,
    as on a real run (the file is older than ``profiles.updated_at``), so a later
    migration resolves file-versus-database profile precedence the same way.
    """
    if _fresh_requested() or platform_path.exists():
        with fast_throwaway_databases():
            migrate_legacy_database(legacy_path, platform_path, profile_path)
        return
    profile_bytes = profile_path.read_bytes()
    key = ("legacy", _legacy_fingerprint(legacy_path), hashlib.sha256(profile_bytes).hexdigest())

    def build(template: _Template) -> None:
        # The backup API reads through the legacy database's WAL, as the fingerprint above did; copying the file alone would
        # leave out committed changes that are not yet checkpointed.
        source, snapshot = sqlite3.connect(legacy_path), sqlite3.connect(template.legacy)
        try:
            source.backup(snapshot)
        finally:
            snapshot.close()
            source.close()
        template.profile.write_bytes(profile_bytes)
        with fast_throwaway_databases():
            migrate_legacy_database(template.legacy, template.platform, template.profile)

    template = _template_for(key, build)
    shutil.copyfile(template.platform, platform_path)
    stat = template.profile.stat()
    os.utime(profile_path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    _restamp(platform_path, legacy_path)


def build_and_migrate(root: Path) -> tuple[Path, Path]:
    """Create a minimal legacy pipeline database and migrate it to the product schema.

    Leaves the same files at the same paths as a real migration (``pipeline.db``,
    ``platform.db`` and ``profile.json`` in ``root``), but the migration itself
    runs once per process and is copied; see ``migrate_cached``. Use
    ``build_and_migrate_fresh`` for a real migration every time.
    """
    legacy_path = root / "pipeline.db"
    platform_path = root / "platform.db"
    _write_legacy_database(legacy_path)
    migrate_cached(legacy_path, platform_path, build_profile(root))
    return legacy_path, platform_path


def migrated_empty_db(path: Path) -> None:
    """Create ``path`` as an empty database with every migration applied, from a copied template.

    For tests that open a fresh file with ``connect_product`` and call
    ``ensure_product_schema`` themselves. Call it before any connection to
    ``path`` opens. A test that patches migrations or builds a partial schema
    must keep running the real ``ensure_product_schema``.
    """
    if _fresh_requested() or path.exists():
        with fast_throwaway_databases():
            conn = database.connect_product(path)
            try:
                schema.ensure_product_schema(conn)
            finally:
                conn.close()
        return

    def build(template: _Template) -> None:
        with fast_throwaway_databases():
            conn = database.connect_product(template.platform)
            try:
                schema.ensure_product_schema(conn)
            finally:
                conn.close()

    template = _template_for(("empty",), build)
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template.platform, path)
    _restamp(path, None)


def use_profile_regions(case, path: Path = PROFILE_REGIONS_FIXTURE) -> None:
    """Point outreach's region lookup at a test profile for the rest of ``case``.

    Outreach reads regions only from the student's config/profile.json, so an
    unpatched test would read whatever profile the machine running it has.
    """
    patcher = mock.patch("opportunity_app.outreach.location.PROFILE_PATH", path)
    patcher.start()
    case.addCleanup(patcher.stop)


def sample_docx(extra_members=None):
    """A minimal résumé .docx, optionally with extra zip members (a macro project, say)."""
    paragraphs = [
        "Test Student",
        "test@example.com | (512) 555-0123 | https://github.com/test",
        "EDUCATION",
        "The University of Texas at Austin — B.S. Mechanical Engineering — 2030",
        "EXPERIENCE",
        "Prototype Lab — Engineering Intern",
        "Built and tested a robotic fixture using SolidWorks.",
        "SKILLS",
        "CAD: SolidWorks, Fusion 360; Software: Python, MATLAB",
    ]
    body = "".join(
        f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in paragraphs
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body></w:document>"
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", document)
        for member_name, member_data in (extra_members or {}).items():
            archive.writestr(member_name, member_data)
    return output.getvalue()
