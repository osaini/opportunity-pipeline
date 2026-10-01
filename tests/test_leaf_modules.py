"""The shared leaf modules of Phase 3: what each may import, and what each does that the copies it replaced did.

A leaf is a small module many others import. It stays a leaf by importing only the standard library and, where listed,
other leaves; the moment it imports schema, automation or outreach, the layering the refactor built is gone and an
import cycle is one edit away. One table below (LEAVES) names every leaf and what it may import; one helper
(module_imports) reads a file's imports. Each workstream then keeps its own sections of behaviour tests, so the
workstreams can add to this file without touching each other's.
"""

import ast
import json
import logging
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
from pathlib import Path
from typing import get_args
from unittest import mock
from zoneinfo import ZoneInfo

try:
    import realdata_guard
except ImportError:  # imported as tests.<module>, with tests/ not on sys.path
    from tests import realdata_guard
# Guards this module's own run against opening data/*.db (AGENTS.md hard rule 1); see tests/realdata_guard.py.
realdata_guard.install()

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

APP = ROOT / "opportunity_app"


PACKAGE = "opportunity_app"


def module_imports(path: Path) -> tuple[set[str], set[str]]:
    """(imports that run when the module is imported, imports inside a function) of a source file, as dotted names.

    ``from .x import y`` in opportunity_app/m.py is opportunity_app.x; ``from . import x`` is opportunity_app.x as
    well; ``import a.b`` is a.b and ``from a.b import c`` is a.b. A file in pipeline_core/ resolves relative imports
    against pipeline_core.
    """
    package = path.parent.name
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    top: set[str] = set()
    lazy: set[str] = set()

    def record(node: ast.AST, depth: int) -> None:
        found: set[str] = set()
        if isinstance(node, ast.Import):
            found = {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                found = {f"{package}.{node.module}"} if node.module else {f"{package}.{alias.name}" for alias in node.names}
            else:
                found = {node.module or ""}
        (top if depth == 0 else lazy).update(found)

    def walk(node: ast.AST, depth: int) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                record(child, depth)
            walk(child, depth + 1 if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else depth)

    walk(tree, 0)
    return top, lazy


def all_imports(path: Path) -> set[str]:
    """Every module a file imports, at any depth."""
    top, lazy = module_imports(path)
    return top | lazy


def stdlib_only(names: set[str], also: set[str] = frozenset()) -> set[str]:
    """What is left of ``names`` once the standard library and ``also`` are taken out.

    ``also`` may name a package (``psycopg``), which then allows its submodules (``psycopg.rows``) too.
    """
    return {
        name for name in names
        if name.split(".")[0] not in sys.stdlib_module_names and name not in also and name.split(".")[0] not in also and name != "__future__"
    }


def dotted(*names: str) -> set[str]:
    return {f"{PACKAGE}.{name}" for name in names}


# Leaf (path from the repository root) -> (what it may import when imported, what it may import inside a function).
# Everything else must be the standard library. Not every entry is a pure leaf; each one that is not says why.
LEAVES: dict[str, tuple[set[str], set[str]]] = {
    # Workstream T: time, database, settings, JSON, stored profile
    "opportunity_app/timestamps.py": (set(), set()),
    # PostgreSQL support imports psycopg inside PostgresConnection, so SQLite-only installs never need it.
    "opportunity_app/database.py": (set(), {"psycopg"}),
    "opportunity_app/settings_store.py": (set(), set()),
    "opportunity_app/json_values.py": (set(), set()),
    "opportunity_app/profile_store.py": (dotted("json_values"), set()),
    "opportunity_app/user_time.py": (set(), set()),
    # Workstream I: identity and the legacy boundary
    "pipeline_core/identity.py": (set(), set()),
    "pipeline_core/regions.py": ({"pipeline_core.identity"}, set()),
    "pipeline_core/env.py": (set(), set()),
    "opportunity_app/storage_paths.py": (set(), set()),
    # Workstream M: the mail leaves. mail_message is stdlib-only so scripts/pipeline_mailbox.py can use it without httpx.
    "opportunity_app/mail_message.py": (set(), set()),
    "opportunity_app/gmail_client.py": ({"httpx"}, set()),
    # Workstream B: workers, the AI CLI runner, outreach leaves
    # Not a pure leaf: automation (record_health) is imported where used, so importing background loads neither it
    # nor the mail reader. mail_message and timestamps are stdlib-only leaves.
    "opportunity_app/background.py": (dotted("mail_message", "timestamps"), dotted("automation")),
    "opportunity_app/web_fetch.py": ({"httpx", "httpcore"}, set()),
    "opportunity_app/outreach_config.py": (dotted("agent_providers"), set()),
    "opportunity_app/outreach_batch.py": (dotted("agent_providers"), set()),
    "opportunity_app/daily_lock.py": ({f"{PACKAGE}.ROOT"}, set()),
    # The two API SDKs are imported where a provider is built, so a missing one fails only that provider.
    "opportunity_app/agent_providers.py": (set(), {"openai", "anthropic"}),
    "opportunity_app/__init__.py": (set(), set()),
    # Storage over outreach, not a pure leaf: it may import only outreach and the clock.
    "opportunity_app/outreach_versions.py": (dotted("outreach", "timestamps"), set()),
}

# Leaves that load nothing late: no function-level import at all, not even of the standard library.
NO_LAZY_IMPORTS = ("pipeline_core/identity.py", "pipeline_core/regions.py", "pipeline_core/env.py", "opportunity_app/storage_paths.py")

# Leaves that must not open a database connection when imported.
NO_CONNECTION_AT_IMPORT = ("timestamps", "database", "settings_store", "json_values", "profile_store", "user_time")


class LeavesImportOnlyWhatTheyMayTests(unittest.TestCase):
    """One table, one reader of imports, for the leaves of all four workstreams."""

    def test_each_leaf_imports_only_what_it_is_allowed_to(self):
        for relative_path, (allowed, lazy_allowed) in LEAVES.items():
            with self.subTest(leaf=relative_path):
                path = ROOT / relative_path
                self.assertTrue(path.is_file(), f"{relative_path} is listed as a leaf but does not exist")
                top, lazy = module_imports(path)
                self.assertEqual(stdlib_only(top, allowed), set(), f"{relative_path} imports outside its allowlist at import time")
                self.assertEqual(stdlib_only(lazy, allowed | lazy_allowed), set(), f"{relative_path} imports outside its allowlist in a function")

    def test_the_leaves_that_load_nothing_late_have_no_function_level_import(self):
        for relative_path in NO_LAZY_IMPORTS:
            with self.subTest(leaf=relative_path):
                self.assertEqual(module_imports(ROOT / relative_path)[1], set(), f"{relative_path} has a function-level import")

    def test_the_time_and_storage_leaves_open_no_connection_when_imported(self):
        for name in NO_CONNECTION_AT_IMPORT:
            tree = ast.parse((APP / f"{name}.py").read_text(encoding="utf-8"))
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)):
                    continue
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Attribute) and inner.attr == "connect":
                        self.fail(f"{name}.py opens a connection at import (line {inner.lineno})")


# --- Workstream T: time, database, settings, JSON, stored profile -------------------------------------------------------


def legacy_parse(value):
    """The body that automation, apply_runs, system_status and outreach_gmail each carried before parse_app_instant."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def legacy_stamp(value):
    """outreach_inbox._stamp and _moment: no falsy guard, TypeError caught as well."""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def legacy_when(stamp):
    """outreach_research._when: no Z replacement (Python 3.11 reads Z itself)."""
    try:
        when = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


STAMPS = [
    None, "", " ", 0, False, "junk", "2026-09-30", "2026-09-30T12:00:00", "2026-09-30T12:00:00Z",
    "2026-09-30T12:00:00+00:00", "2026-09-30T12:00:00.123456+00:00", "2026-09-30T12:00:00-04:00",
    "2026-09-30 12:00:00+05:30", "2026-09-30T12:00:00z", "  2026-09-30T12:00:00Z", "2026-13-45T00:00:00",
    datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc), datetime(2026, 9, 30, 12, 0),
]


class ParseAppInstantTests(unittest.TestCase):
    def test_it_agrees_with_every_copy_it_replaced(self):
        from opportunity_app.timestamps import parse_app_instant

        for stamp in STAMPS:
            with self.subTest(stamp=repr(stamp)):
                got = parse_app_instant(stamp)
                self.assertEqual(got, legacy_parse(stamp))
                self.assertEqual(got, legacy_stamp(stamp))
                self.assertEqual(got, legacy_when(stamp))
                if got is not None:
                    self.assertEqual(got.utcoffset(), legacy_parse(stamp).utcoffset())

    def test_a_naive_stamp_the_app_wrote_is_utc_and_a_non_utc_offset_is_kept(self):
        from opportunity_app.timestamps import parse_app_instant

        self.assertEqual(parse_app_instant("2026-09-30T12:00:00").utcoffset(), timedelta(0))
        self.assertEqual(parse_app_instant("2026-09-30T12:00:00-04:00").utcoffset(), timedelta(hours=-4))
        self.assertEqual(parse_app_instant("2026-09-30T12:00:00-04:00").isoformat(), "2026-09-30T12:00:00-04:00")

    def test_a_source_date_without_an_offset_stays_unknown(self):
        # The opposite rule, on purpose: canonical_utc is for dates a source sent, and a naive one is "did not say when".
        from opportunity_app.timestamps import canonical_utc, parse_app_instant

        self.assertIsNone(canonical_utc("2026-09-30T12:00:00"))
        self.assertIsNotNone(parse_app_instant("2026-09-30T12:00:00"))


class UtcNowTests(unittest.TestCase):
    def test_stamps_never_repeat_even_when_the_clock_does(self):
        from opportunity_app import timestamps

        frozen = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)

        class CoarseClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen

        with mock.patch.object(timestamps, "datetime", CoarseClock), \
                mock.patch.object(timestamps, "_LAST_NOW", datetime.min.replace(tzinfo=timezone.utc)):
            stamps = [timestamps.utc_now() for _ in range(20)]
        self.assertEqual(stamps, sorted(set(stamps)))
        self.assertEqual(stamps[0], "2026-09-30T12:00:00.000000+00:00")
        self.assertEqual(stamps[1], "2026-09-30T12:00:00.000001+00:00")

    def test_there_is_one_clock(self):
        from opportunity_app import company_tags, schema, timestamps

        self.assertIs(schema.utc_now, timestamps.utc_now)
        self.assertIs(company_tags.utc_now, timestamps.utc_now)


class LocalTimeHelperTests(unittest.TestCase):
    def test_to_local_uses_the_zone_or_the_machines_own(self):
        from opportunity_app.user_time import UserTimezone, to_local

        instant = datetime(2026, 7, 1, 16, 0, tzinfo=timezone.utc)
        zone = ZoneInfo("America/Chicago")
        self.assertEqual(to_local(instant, zone).isoformat(), "2026-07-01T11:00:00-05:00")
        self.assertEqual(to_local(instant, None), instant.astimezone())
        self.assertEqual(UserTimezone("x", zone).to_local(instant), to_local(instant, zone))
        self.assertEqual(UserTimezone("system-local").to_local(instant), to_local(instant, None))

    def test_at_wall_clock_reads_a_naive_time_in_the_zone(self):
        from opportunity_app.user_time import at_wall_clock

        naive = datetime(2026, 7, 1, 9, 0)
        self.assertEqual(at_wall_clock(naive, ZoneInfo("America/Chicago")).isoformat(), "2026-07-01T09:00:00-05:00")
        self.assertEqual(at_wall_clock(naive, None), naive.astimezone())


class RollbackQuietlyTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE t(x)")
        self.conn.commit()
        self.logger = logging.getLogger("leaf-test")

    def tearDown(self):
        self.conn.close()

    def test_an_open_transaction_is_rolled_back(self):
        from opportunity_app.database import rollback_quietly

        self.conn.execute("INSERT INTO t VALUES (1)")
        self.assertTrue(self.conn.in_transaction)
        rollback_quietly(self.conn, self.logger, "a step failed")
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM t").fetchone()[0], 0)

    def test_a_connection_with_nothing_open_is_left_alone(self):
        from opportunity_app.database import rollback_quietly

        conn = mock.Mock()
        conn.in_transaction = False
        rollback_quietly(conn, self.logger, "a step failed")
        conn.rollback.assert_not_called()

    def test_a_failed_rollback_is_logged_under_the_callers_logger_and_never_raised(self):
        from opportunity_app.database import rollback_quietly

        conn = mock.Mock()
        conn.in_transaction = True
        conn.rollback.side_effect = RuntimeError("connection lost")
        with self.assertLogs("leaf-test", "WARNING") as logged:
            rollback_quietly(conn, self.logger, "an inbox step failed")
        self.assertEqual(logged.records[0].getMessage(), "Could not roll back after an inbox step failed")
        self.assertIsNotNone(logged.records[0].exc_info)


class SettingsStoreTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE user_settings(user_id TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL, "
            "updated_at TEXT NOT NULL, PRIMARY KEY(user_id, key))"
        )

    def tearDown(self):
        self.conn.close()

    def test_a_missing_row_reads_as_none(self):
        from opportunity_app.settings_store import get_setting, setting_updated_at

        self.assertIsNone(get_setting(self.conn, "u", "k"))
        self.assertIsNone(setting_updated_at(self.conn, "u", "k"))

    def test_put_inserts_then_updates_the_value_and_the_stamp(self):
        from opportunity_app.settings_store import get_setting, put_setting, setting_updated_at

        put_setting(self.conn, "u", "k", "on", "2026-09-30T10:00:00+00:00")
        self.assertEqual(get_setting(self.conn, "u", "k"), "on")
        put_setting(self.conn, "u", "k", "off", "2026-09-30T11:00:00+00:00")
        self.assertEqual(get_setting(self.conn, "u", "k"), "off")
        self.assertEqual(setting_updated_at(self.conn, "u", "k"), "2026-09-30T11:00:00+00:00")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM user_settings").fetchone()[0], 1)

    def test_settings_are_per_student_and_per_key(self):
        from opportunity_app.settings_store import get_setting, put_setting

        put_setting(self.conn, "u", "k", "1", "s")
        put_setting(self.conn, "v", "k", "2", "s")
        put_setting(self.conn, "u", "j", "3", "s")
        self.assertEqual([get_setting(self.conn, "u", "k"), get_setting(self.conn, "v", "k"), get_setting(self.conn, "u", "j")],
                         ["1", "2", "3"])

    def test_put_opens_no_transaction_of_its_own_commit_is_the_callers(self):
        from opportunity_app.settings_store import get_setting, put_setting

        put_setting(self.conn, "u", "k", "on", "s")
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertIsNone(get_setting(self.conn, "u", "k"))


class JsonValuesTests(unittest.TestCase):
    def test_json_dict_gives_an_object_or_nothing(self):
        from opportunity_app.json_values import json_dict

        self.assertEqual(json_dict('{"a": 1}'), {"a": 1})
        for text in (None, "", "{", "[1]", '"x"', "3", "null", b"\xff", 5):
            with self.subTest(text=text):
                self.assertEqual(json_dict(text), {})

    def test_json_as_requires_the_type_of_the_default(self):
        from opportunity_app.json_values import json_as

        self.assertEqual(json_as("[1, 2]", []), [1, 2])
        self.assertEqual(json_as('{"a": 1}', {}), {"a": 1})
        self.assertEqual(json_as('{"a": 1}', []), [])
        self.assertEqual(json_as("[1]", {}), {})
        self.assertEqual(json_as(None, []), [])
        self.assertEqual(json_as("", {"d": 1}), {"d": 1})
        self.assertEqual(json_as("not json", []), [])

    def test_it_agrees_with_the_copies_it_replaced(self):
        import json

        from opportunity_app.json_values import json_as, json_dict

        def old_dict(text):
            try:
                value = json.loads(text or "{}")
            except (TypeError, ValueError):
                return {}
            return value if isinstance(value, dict) else {}

        def old_loads(text, default):
            try:
                value = json.loads(text or "")
            except (TypeError, ValueError):
                return default
            return value if isinstance(value, type(default)) else default

        for text in (None, "", "{}", '{"a": [1]}', "[]", "[1]", "junk", "null", "0", '{"a":', " "):
            with self.subTest(text=text):
                self.assertEqual(json_dict(text), old_dict(text))
                for default in ({}, [], ""):
                    self.assertEqual(json_as(text, default), old_loads(text, default))


class ReadStoredProfileTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE profiles(user_id TEXT PRIMARY KEY, profile_json TEXT)")

    def tearDown(self):
        self.conn.close()

    def test_no_row_gives_an_empty_profile_and_creates_none(self):
        from opportunity_app.profile_store import read_stored_profile

        self.assertEqual(read_stored_profile(self.conn, "u"), {})
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0], 0)

    def test_the_stored_object_is_returned_and_anything_else_is_empty(self):
        from opportunity_app.profile_store import read_stored_profile

        for stored, expected in (('{"school": "X"}', {"school": "X"}), ("", {}), (None, {}), ("[1]", {}), ("junk", {}), ("null", {})):
            with self.subTest(stored=stored):
                self.conn.execute("DELETE FROM profiles")
                self.conn.execute("INSERT INTO profiles VALUES ('u', ?)", (stored,))
                self.assertEqual(read_stored_profile(self.conn, "u"), expected)


class LogApplicationEventTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute(
            "CREATE TABLE application_events(application_id TEXT, event_type TEXT, from_stage TEXT, to_stage TEXT, "
            "detail_json TEXT, created_at TEXT)"
        )

    def tearDown(self):
        self.conn.close()

    def rows(self):
        return [dict(row) for row in self.conn.execute("SELECT * FROM application_events")]

    def test_the_columns_default_to_null_stages_and_json_dumps_detail(self):
        from opportunity_app.actions import log_application_event

        log_application_event(self.conn, "a1", "task_added", {"task_id": "t", "title": "x"}, "2026-09-30T10:00:00+00:00")
        self.assertEqual(self.rows(), [{
            "application_id": "a1", "event_type": "task_added", "from_stage": None, "to_stage": None,
            "detail_json": '{"task_id": "t", "title": "x"}', "created_at": "2026-09-30T10:00:00+00:00",
        }])

    def test_stages_and_pre_encoded_detail_are_stored_as_given(self):
        from opportunity_app.actions import log_application_event

        log_application_event(self.conn, "a1", "stage_changed", {"b": 1, "a": 2}, "s", from_stage="applied", to_stage="interview")
        log_application_event(self.conn, "a1", "apply_agent_submitted", None, "s", encoded='{"a": 2, "b": 1}')
        first, second = self.rows()
        self.assertEqual((first["from_stage"], first["to_stage"], first["detail_json"]), ("applied", "interview", '{"b": 1, "a": 2}'))
        self.assertEqual(second["detail_json"], '{"a": 2, "b": 1}')



# --- Workstream I: identity and the legacy boundary ---------------------------------------------------------------------

class IdentityAndLegacyWorkstreamTests(unittest.TestCase):
    """Workstream I: pipeline_core/identity.py, opportunity_app/legacy.py and the constants collapsed into one place."""

    def test_employer_key_output_is_pinned_because_apply_stores_persist_it(self):
        from pipeline_core.identity import employer_key

        # A change to the identity rule orphans rows in apply_runs, apply_sensitive_answers and employer_domains.
        for name, expected in (
            ("Acme Robotics Inc.", "acme robotics"),
            ("ACME robotics", "acme robotics"),
            ("The Acme Group, LLC", "acme"),
            ("Robotics Acme", "acme robotics"),
            ("Bluefin Labs", "bluefin labs"),
            ("", ""),
        ):
            with self.subTest(name=name):
                self.assertEqual(employer_key(name), expected)

    def test_employer_key_still_refuses_none_because_an_empty_key_means_any_company(self):
        from pipeline_core.identity import employer_key

        with self.assertRaises(AttributeError):
            employer_key(None)  # type: ignore[arg-type]

    def test_mail_trust_company_key_still_reads_none_as_empty(self):
        from opportunity_app import mail_trust
        from pipeline_core.identity import employer_key

        self.assertEqual(mail_trust.company_key(None), "")  # type: ignore[arg-type]
        self.assertEqual(mail_trust.company_key("Acme Robotics Inc."), employer_key("Acme Robotics Inc."))

    def test_the_apply_stores_and_policy_use_the_one_employer_key(self):
        from opportunity_app import apply_policy, apply_runs, apply_sensitive

        for module in (apply_runs, apply_sensitive, apply_policy):
            self.assertFalse(hasattr(module, "company_key"), f"{module.__name__} defines its own company_key again")
        self.assertNotIn("company_key", apply_sensitive.__all__)

    def test_sort_key_is_the_casefold_the_stored_columns_use(self):
        from pipeline_core.identity import sort_key

        self.assertEqual(sort_key("Straße GmbH"), "strasse gmbh")
        self.assertEqual(sort_key(None), "")
        self.assertEqual(sort_key(""), "")
        self.assertEqual(sort_key("ACME"), "acme")

    def test_schema_read_model_and_tags_share_that_one_sort_key(self):
        from opportunity_app import company_tags, schema
        from pipeline_core import identity, read_model

        self.assertIs(schema.sort_key, identity.sort_key)
        self.assertIs(company_tags.sort_key, identity.sort_key)
        self.assertIs(read_model.sort_key, identity.sort_key)

    def test_outreach_company_key_is_a_different_rule_and_stays_separate(self):
        from opportunity_app import outreach
        from pipeline_core.identity import employer_key, sort_key

        # NFKC, "&" becomes "and", a leading "The" and trailing legal words dropped, word order kept.
        self.assertEqual(outreach.company_key("The Smith & Sons Holdings Group"), "smith and sons holdings group")
        self.assertNotEqual(outreach.company_key("Robotics Acme"), employer_key("Robotics Acme"))
        self.assertNotEqual(outreach.company_key("Acme Robotics Inc"), sort_key("Acme Robotics Inc"))

    def test_normalized_text_reads_none_as_empty_but_not_zero_or_false(self):
        from pipeline_core.identity import normalized, normalized_text

        self.assertEqual(normalized_text(None), "")
        self.assertEqual(normalized_text(0), "0")
        self.assertEqual(normalized_text(False), "false")
        self.assertEqual(normalized_text("Are you 18+?"), normalized("Are you 18+?"))

    def test_schema_no_longer_loads_the_legacy_pipeline_for_a_region_label(self):
        # A fresh interpreter, since every other test in the run has imported pipeline already.
        import subprocess

        code = (
            "import sys; sys.path.insert(0, %r); import opportunity_app.schema; "
            "print('pipeline' in sys.modules)" % str(ROOT)
        )
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, check=True)
        self.assertEqual(done.stdout.strip(), "False")

    def test_only_the_legacy_adapter_imports_pipeline(self):
        offenders = []
        for path in sorted((ROOT / "opportunity_app").rglob("*.py")):
            if path.name == "legacy.py":
                continue
            if "pipeline" in all_imports(path):
                offenders.append(path.name)
        self.assertEqual(offenders, [], "web modules must import pipeline names through opportunity_app.legacy")

    def test_the_regions_moved_to_pipeline_core_still_bucket_locations(self):
        from pipeline_core.regions import match_region, region_label

        profile = {"regions": [{"name": "Austin", "state_markers": ["TX"], "places": ["Austin"], "aliases": []}]}
        self.assertEqual(region_label("Austin, TX", profile), "Austin")
        self.assertEqual(region_label("3 Locations", profile), "Unknown")
        self.assertEqual(region_label("Remote - US", profile), "Remote")
        self.assertEqual(region_label("Seattle, WA", profile), "Other")
        self.assertIsNone(match_region("Austin, MN", profile["regions"]))

    def test_env_lines_share_one_rule_and_differ_only_in_how_the_caller_treats_repeats(self):
        import os

        import pipeline
        from opportunity_app import setup
        from pipeline_core.env import iter_env_pairs

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "# comment\n\nA=one\nB = \"two\" \nnot a pair\nA=again\n=orphan\nC='mixed\"\nD=\n", encoding="utf-8",
            )
            self.assertEqual(
                iter_env_pairs(path),
                [("A", "one"), ("B", "two"), ("A", "again"), ("", "orphan"), ("C", "'mixed\""), ("D", "")],
            )
            # setup.read_env keeps the last line of a repeated key and an empty key ...
            self.assertEqual(setup.read_env(path), {"A": "again", "B": "two", "": "orphan", "C": "'mixed\"", "D": ""})
            # ... pipeline.load_env_file keeps the first and drops an empty key, and never overrides a real variable.
            names = ("A", "B", "C", "D")
            saved = {name: os.environ.pop(name, None) for name in names}
            try:
                os.environ["B"] = "from the shell"
                pipeline.load_env_file(path)
                self.assertEqual([os.environ.get(name) for name in names], ["one", "from the shell", "'mixed\"", ""])
                self.assertNotIn("", os.environ)
            finally:
                for name, value in saved.items():
                    os.environ.pop(name, None)
                    if value is not None:
                        os.environ[name] = value
            self.assertEqual(iter_env_pairs(Path(tmp) / "absent.env"), [])

    def test_imported_posting_ids_keep_linkedin_ids_except_for_the_manual_csv(self):
        import hashlib

        import pipeline

        url = "https://www.linkedin.com/jobs/view/3912345678/?trackingId=abc"
        hashed = hashlib.sha256(pipeline.canonical_url(url).encode("utf-8")).hexdigest()[:20]
        self.assertEqual(pipeline.url_external_id(url, linkedin_ids=True), "3912345678")
        self.assertEqual(pipeline.url_external_id(url, linkedin_ids=False), hashed)
        other = "https://boards.example.test/jobs/1"
        self.assertEqual(
            pipeline.url_external_id(other, linkedin_ids=True), pipeline.url_external_id(other, linkedin_ids=False),
        )

    def test_source_key_keeps_its_keyerror_that_system_status_relies_on(self):
        import pipeline

        self.assertEqual(pipeline.source_key({"kind": "greenhouse", "token": "acme"}), "greenhouse:acme")
        self.assertEqual(pipeline.source_key({"kind": "workday", "tenant": "t", "site": "s"}), "workday:t:s")
        with self.assertRaises(KeyError):
            pipeline.source_key({"token": "acme"})
        # The merge key is the same string lowercased; its .get("kind", "") never helps, since source_identity needs kind too.
        self.assertEqual(pipeline._source_merge_key({"kind": "Greenhouse", "token": "ACME"}), "greenhouse:acme")
        with self.assertRaises(KeyError):
            pipeline._source_merge_key({"token": "acme"})

    def test_pipeline_connect_takes_a_path_and_defaults_to_the_module_path_read_at_call_time(self):
        import sqlite3

        import pipeline

        with tempfile.TemporaryDirectory() as tmp:
            explicit = Path(tmp) / "nested" / "explicit.db"
            pipeline.connect(explicit).close()
            self.assertTrue(explicit.is_file())
            default = Path(tmp) / "default.db"
            with mock.patch.object(pipeline, "DB_PATH", default):
                pipeline.connect().close()
            self.assertTrue(default.is_file())
            with closing(sqlite3.connect(explicit)) as conn:
                tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("jobs", tables)

    def test_legacy_create_database_does_not_move_the_global_db_path(self):
        import pipeline
        from opportunity_app import legacy

        before = pipeline.DB_PATH
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "data" / "pipeline.db"
            legacy.create_database(target)
            self.assertTrue(target.is_file())
        self.assertEqual(pipeline.DB_PATH, before)

    def test_one_ruleset_version_constant_backs_every_fit_score_read_and_write(self):
        from opportunity_app import schema
        from pipeline_core.read_model import RULESET_VERSION

        self.assertEqual(RULESET_VERSION, "legacy-v1")  # the SQL views in migrations/0001, 0020 and 0021 bake this in
        self.assertFalse(hasattr(schema, "RULESET_VERSION") and schema.RULESET_VERSION is not RULESET_VERSION)
        # The migration_runs key is a different concept that happens to read the same today.
        self.assertEqual(schema.LEGACY_MIGRATION_KEY, "legacy-v1")
        for relative in ("actions.py", "extension_apply.py", "profile.py"):
            with self.subTest(module=relative):
                text = (ROOT / "opportunity_app" / relative).read_text(encoding="utf-8")
                self.assertNotIn("legacy-v1", text)

    def test_the_closed_application_stages_are_defined_once(self):
        from opportunity_app import actions, urgent

        self.assertIs(urgent.CLOSED_APPLICATION_STAGES, actions.CLOSED_APPLICATION_STAGES)
        self.assertEqual(tuple(actions.CLOSED_APPLICATION_STAGES), ("rejected", "withdrawn", "archived"))

    def test_the_job_types_the_api_accepts_are_the_job_types_the_worker_handles(self):
        from helpers_platform import build_and_migrate
        from opportunity_app import api, worker

        accepted = set(get_args(api.JobCreateRequest.model_fields["job_type"].annotation))
        seen: dict[str, object] = {}

        def capture(conn, handlers, **kwargs):
            seen.update(handlers)
            return None

        with tempfile.TemporaryDirectory() as tmp:
            _, platform_path = build_and_migrate(Path(tmp))
            with mock.patch.object(worker, "run_next_job", capture):
                worker.run_once(platform_path)
        self.assertEqual(accepted, set(seen))

    def test_confined_path_accepts_a_direct_child_and_refuses_everything_else(self):
        from opportunity_app.storage_paths import confined_path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "store"
            root.mkdir()
            self.assertEqual(confined_path(root, "a.pdf"), (root / "a.pdf").resolve())
            for name in ("../a.pdf", "sub/a.pdf", "..", str(Path(tmp) / "elsewhere.pdf"), ""):
                with self.subTest(name=name):
                    self.assertIsNone(confined_path(root, name))

    def test_each_store_still_raises_its_own_error_for_a_name_outside_its_folder(self):
        from opportunity_app import captures, preparation, resumes

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(captures.CaptureValidationError):
                captures._safe_path(root, "../x")
            with self.assertRaises(resumes.ResumeValidationError):
                resumes._safe_storage_path(root, "../x")
            self.assertEqual(captures._safe_path(root, "x"), (root.resolve() / "x"))
            self.assertEqual(resumes._safe_storage_path(root, "x"), (root.resolve() / "x"))
            self.assertTrue(hasattr(preparation, "PreparationNotFoundError"))


# --- Workstream M: the mail leaves --------------------------------------------------------------------------------------


def parsed(raw: str):
    return BytesParser(policy=policy.default).parsebytes(raw.encode("utf-8"))


class MailMessageLeafTests(unittest.TestCase):
    def test_the_header_map_keeps_the_last_of_a_repeated_header_and_folds_names(self):
        from opportunity_app.mail_message import header_map

        message = {"payload": {"headers": [
            {"name": "Subject", "value": "first"}, {"name": "SUBJECT", "value": "second"}, {"name": "To", "value": "a@b.example"},
        ]}}
        self.assertEqual(header_map(message), {"subject": "second", "to": "a@b.example"})
        self.assertEqual(header_map({}), {})
        self.assertEqual(header_map({"payload": {"headers": None}}), {})

    def test_the_guarded_header_map_skips_what_is_not_a_header_and_the_plain_one_does_not(self):
        from opportunity_app.mail_message import header_map

        message = {"payload": {"headers": [None, {"name": "From", "value": "a@b.example"}, "junk"]}}
        with self.assertRaises(AttributeError):
            header_map(message)
        self.assertEqual(header_map(message, guarded=True), {"from": "a@b.example"})

    def test_base64url_gets_its_padding_back_and_refuses_what_is_not_base64(self):
        from opportunity_app.mail_message import decode_base64url

        self.assertEqual(decode_base64url("aGVsbG8"), b"hello")
        self.assertEqual(decode_base64url("aGVsbG8="), b"hello")
        self.assertEqual(decode_base64url(""), b"")
        with self.assertRaises(ValueError):
            decode_base64url("not base64 ☃")

    def test_the_two_html_readers_keep_their_different_text_retention(self):
        from opportunity_app.mail_message import html_text_reply, html_text_spaced

        markup = "<p>Hi <b>there</b></p><style>x{}</style><blockquote>quoted words</blockquote>after"
        self.assertEqual(html_text_spaced(markup), " Hi  there \n quoted words after")
        self.assertEqual(html_text_reply(markup), "Hi there\n")

    def test_the_two_bulk_checks_differ_only_in_the_auto_submitted_rule(self):
        from opportunity_app.mail_message import has_list_headers, is_bulk_or_generated

        generated = parsed("From: a@b.example\nAuto-Submitted: auto-generated\n\nbody\n")
        listed = parsed("From: a@b.example\nList-Id: <news.b.example>\n\nbody\n")
        bulk = parsed("From: a@b.example\nPrecedence: Bulk\n\nbody\n")
        replied = parsed("From: a@b.example\nAuto-Submitted: auto-replied\n\nbody\n")
        self.assertTrue(is_bulk_or_generated(generated))
        self.assertFalse(has_list_headers(generated))
        for message in (listed, bulk):
            self.assertTrue(is_bulk_or_generated(message))
            self.assertTrue(has_list_headers(message))
        self.assertFalse(is_bulk_or_generated(replied))
        self.assertFalse(has_list_headers(replied))

    def test_the_two_received_times_fall_back_differently(self):
        from opportunity_app.mail_message import received_or_epoch, received_or_none

        dated = parsed("From: a@b.example\nDate: Tue, 01 Sep 2026 10:00:00 +0000\n\nbody\n")
        undated = parsed("From: a@b.example\n\nbody\n")
        self.assertEqual(received_or_epoch({}), datetime(1970, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(received_or_epoch({"internalDate": "1000"}), datetime(1970, 1, 1, 0, 0, 1, tzinfo=timezone.utc))
        self.assertEqual(received_or_none({"internalDate": "1000"}, undated), datetime(1970, 1, 1, 0, 0, 1, tzinfo=timezone.utc))
        self.assertEqual(received_or_none({}, dated), datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc))
        self.assertIsNone(received_or_none({}, undated))

    def test_a_link_is_cut_to_its_host_and_a_query_is_never_kept(self):
        from opportunity_app.mail_message import clean_url, host_of, strip_queries

        self.assertEqual(clean_url("https://Acme.com/a?x=1&amp;y=2)."), "https://Acme.com/a?x=1&y=2")
        self.assertEqual(host_of("https://Careers.Acme.com./jobs?id=1"), "careers.acme.com")
        self.assertEqual(host_of("not a url"), "")
        self.assertEqual(host_of(None), "")
        self.assertEqual(strip_queries("failed for https://acme.com/a/b?token=secret#frag and more"), "failed for https://acme.com/a/b and more")

    def test_link_hosts_fail_closed_where_hosts_in_does_not(self):
        from opportunity_app.mail_message import LINK_HOST_LIMIT, hosts_in, link_hosts_or_none

        many = " ".join(f"https://site{number}.example/" for number in range(LINK_HOST_LIMIT + 1))
        crowded = parsed(f"From: a@b.example\nContent-Type: text/plain\n\n{many}\n")
        self.assertIsNone(link_hosts_or_none(crowded))
        self.assertEqual(len(hosts_in(many)), LINK_HOST_LIMIT + 1)
        self.assertEqual(link_hosts_or_none(parsed("From: a@b.example\n\nsee https://www.acme.com/x?y=1.\n")), ["www.acme.com"])

    def test_a_mailbox_folds_its_tag_and_a_gmail_address_its_dots(self):
        from opportunity_app.mail_message import mailbox_key

        self.assertEqual(mailbox_key(" Dana.Reyes+jobs@Gmail.com "), "danareyes@gmail.com")
        self.assertEqual(mailbox_key("d.reyes@googlemail.com"), "dreyes@gmail.com")
        self.assertEqual(mailbox_key("dana.reyes+x@school.example"), "dana.reyes@school.example")
        self.assertEqual(mailbox_key("+tag@school.example"), "+tag@school.example")
        self.assertEqual(mailbox_key("no address"), "no address")


class GmailClientLeafTests(unittest.TestCase):
    def test_a_connection_is_connected_not_connected_or_needing_a_reconnect(self):
        from opportunity_app.gmail_client import connection_state

        self.assertEqual(connection_state(None), "not_connected")
        self.assertEqual(connection_state({"status": "disconnected"}), "not_connected")
        self.assertEqual(connection_state({"status": "connected"}), "connected")
        self.assertEqual(connection_state({"status": "error"}), "needs_reconnect")
        self.assertEqual(connection_state({"status": "anything else"}), "needs_reconnect")

    def test_granted_scopes_read_a_list_and_nothing_else(self):
        from opportunity_app.gmail_client import MODIFY_SCOPE, READ_SCOPE, can_read_mail, granted_scopes

        self.assertEqual(granted_scopes(f'["{READ_SCOPE}", 7]'), [READ_SCOPE, "7"])
        for unreadable in (None, "", "not json", "null", '"a string"', '{"a": 1}', 5):
            with self.subTest(value=unreadable):
                self.assertEqual(granted_scopes(unreadable), [])
        self.assertTrue(can_read_mail([READ_SCOPE]))
        self.assertTrue(can_read_mail([MODIFY_SCOPE]))
        self.assertFalse(can_read_mail(["https://www.googleapis.com/auth/gmail.compose"]))

    def test_a_throttle_is_a_429_or_a_403_that_names_a_rate_limit(self):
        import httpx

        from opportunity_app.gmail_client import error_reasons, is_throttle

        def answer(status, body=None):
            return httpx.Response(status, json=body) if body is not None else httpx.Response(status, text="not json")

        named = {"error": {"status": "RESOURCE_EXHAUSTED", "errors": [{"reason": "rateLimitExceeded"}, "junk", {"reason": 3}]}}
        self.assertTrue(is_throttle(answer(429)))
        self.assertTrue(is_throttle(answer(403, named)))
        self.assertTrue(is_throttle(answer(403, {"error": {"errors": [{"reason": "userRateLimitExceeded"}]}})))
        self.assertFalse(is_throttle(answer(403, {"error": {"status": "PERMISSION_DENIED", "errors": [{"reason": "forbidden"}]}})))
        self.assertFalse(is_throttle(answer(403)))
        self.assertFalse(is_throttle(answer(403, {"error": "denied"})))
        self.assertFalse(is_throttle(answer(500, named)))
        self.assertEqual(error_reasons(answer(403, named)), ["RESOURCE_EXHAUSTED", "rateLimitExceeded", 3])
        self.assertEqual(error_reasons(answer(403, [1])), [])
        self.assertEqual(error_reasons(answer(403)), [])

    def test_a_throttle_is_read_as_could_not_reach_gmail_and_never_as_a_refused_connection(self):
        import httpx

        from opportunity_app.gmail_client import GmailAuthError, GmailNeedsReadScope, GmailThrottled

        self.assertIsInstance(GmailThrottled("slow down"), httpx.HTTPError)
        self.assertNotIsInstance(GmailAuthError("refused"), httpx.HTTPError)
        self.assertNotIsInstance(GmailNeedsReadScope(), (httpx.HTTPError, GmailAuthError))

    def test_a_look_schedule_spaces_looks_and_forgets_a_failed_one(self):
        import threading
        from datetime import timedelta

        from opportunity_app.gmail_client import LookSchedule

        last: dict = {}
        schedule = LookSchedule(
            last, threading.Lock(), interval=lambda age: timedelta(minutes=10) if age < timedelta(hours=1) else timedelta(hours=1),
            key=lambda item: item["id"], started=lambda item: item["at"],
        )
        now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        fresh, old = {"id": "a", "at": now - timedelta(minutes=5)}, {"id": "b", "at": now - timedelta(days=2)}
        self.assertEqual(schedule.take_due("u", [fresh, old], now), [fresh, old])
        self.assertEqual(schedule.take_due("u", [fresh, old], now + timedelta(minutes=9)), [])
        self.assertEqual(schedule.take_due("u", [fresh, old], now + timedelta(minutes=11)), [fresh])
        self.assertEqual(schedule.take_due("u", [fresh, old], now + timedelta(hours=2)), [fresh, old])
        self.assertEqual(schedule.take_due("someone else", [fresh], now), [fresh], "kept per student")
        schedule.forget("u", [fresh])
        self.assertEqual(schedule.take_due("u", [fresh, old], now + timedelta(hours=2, minutes=1)), [fresh])
        self.assertIs(schedule.last, last)

    def test_the_send_watchers_look_key_is_forgotten_as_text_and_remembered_as_stored(self):
        # Kept as it was (gmail-11): remembered under the id as stored, forgotten under str() of it. Harmless while
        # a draft id is always text; this pins that nobody evens the two out without meaning to.
        import threading
        from datetime import timedelta

        from opportunity_app.gmail_client import LookSchedule

        schedule = LookSchedule(
            {}, threading.Lock(), interval=lambda age: timedelta(hours=1), key=lambda item: item["id"], started=lambda item: item["at"],
            forget_key=str,
        )
        now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        item = {"id": 5, "at": now}
        schedule.take_due("u", [item], now)
        schedule.forget("u", [item])
        self.assertEqual(schedule.take_due("u", [item], now), [], "the int key was not forgotten: forget looked for '5'")
        text = {"id": "d-1", "at": now}
        schedule.take_due("u", [text], now)
        schedule.forget("u", [text])
        self.assertEqual(schedule.take_due("u", [text], now), [text])

    def test_each_watcher_keeps_its_own_look_state_under_its_own_name(self):
        from opportunity_app import outreach_delivery, outreach_gmail_sends

        self.assertIs(outreach_delivery._LOOKS.last, outreach_delivery._LAST_LOOK)
        self.assertIs(outreach_gmail_sends._LOOKS.last, outreach_gmail_sends._LAST_LOOK)
        self.assertIsNot(outreach_delivery._LAST_LOOK, outreach_gmail_sends._LAST_LOOK)
        self.assertIs(outreach_delivery._LOOKS.lock, outreach_delivery._LOOK_LOCK, "which also guards _READ_NOTICES")


class OutreachIdentityTests(unittest.TestCase):
    def test_company_identity_does_not_load_the_mail_readers(self):
        heavy = dotted("outreach_inbox", "application_inbox", "outreach_labels", "outreach_delivery", "automation", "api")
        self.assertEqual(all_imports(APP / "outreach_identity.py") & heavy, set())

    def test_the_interviewer_and_research_take_identity_from_it_not_from_the_mail_reader(self):
        for module in ("outreach_interviewer.py", "outreach_research.py"):
            with self.subTest(module=module):
                self.assertNotIn(f"{PACKAGE}.outreach_inbox", all_imports(APP / module))
                self.assertIn(f"{PACKAGE}.outreach_identity", all_imports(APP / module))


# --- Workstream B: background workers, the AI CLI runner, outreach leaves ---------------------------------------------

from opportunity_app import agent_providers, background, inbox_watcher, outreach_batch, outreach_config, outreach_review, web_fetch  # noqa: E402
from opportunity_app.outreach import create_target, get_target, latest_event_stamp, log_event, withdraw_auto_approval  # noqa: E402
from opportunity_app.schema import connect_product  # noqa: E402

from helpers_platform import build_and_migrate  # noqa: E402

USER = "local-user"


class QuietWorker(background.PollingWorker):
    thread_name = "test-quiet-worker"
    failure_message = "The quiet worker's pass failed"
    logger = logging.getLogger("test_leaf_modules.quiet")

    def __init__(self, interval_seconds, fail=False):
        super().__init__(interval_seconds)
        self.passes = 0
        self.started = []
        self.fail = fail
        self.ran = threading.Event()

    def before_start(self):
        self.started.append(self._stop.is_set())

    def _run_pass(self):
        self.passes += 1
        self.ran.set()
        if self.fail:
            raise RuntimeError("boom")


class WorkstreamBBackgroundTests(unittest.TestCase):
    def test_a_polling_worker_runs_a_pass_then_waits_for_the_interval_or_a_wake(self):
        worker = QuietWorker(60)
        try:
            worker.start()
            self.assertTrue(worker.ran.wait(5), "the first pass runs at once")
            self.assertEqual(worker.started, [False], "before_start runs once, after the stop flag was cleared")
            worker.ran.clear()
            self.assertFalse(worker.ran.wait(0.3), "it then sleeps for the interval")
            worker.wake()
            self.assertTrue(worker.ran.wait(5), "wake() ends the sleep")
        finally:
            worker.stop()
        self.assertEqual(worker.passes, 2)
        self.assertFalse(worker._thread.is_alive())

    def test_start_while_running_is_a_no_op_and_stop_ends_the_thread_quickly(self):
        worker = QuietWorker(60)
        worker.start()
        first = worker._thread
        worker.start()
        self.assertIs(worker._thread, first)
        self.assertEqual(len(worker.started), 1)
        began = time.monotonic()
        worker.stop()
        self.assertLess(time.monotonic() - began, 4, "stop() wakes the sleeping thread rather than waiting out the interval")
        worker.stop()

    def test_a_failed_pass_is_logged_where_the_worker_says_and_the_thread_goes_on(self):
        worker = QuietWorker(0.01, fail=True)
        try:
            with self.assertLogs("test_leaf_modules.quiet", "ERROR") as logged:
                worker.start()
                deadline = time.monotonic() + 5
                while worker.passes < 2 and time.monotonic() < deadline:
                    time.sleep(0.01)
        finally:
            worker.stop()
        self.assertGreaterEqual(worker.passes, 2, "one bad pass does not end the thread")
        self.assertIn("The quiet worker's pass failed", logged.output[0])

    def test_the_inbox_watcher_waits_before_its_first_pass_and_stop_ends_that_wait(self):
        watcher = inbox_watcher.InboxWatcher(
            "unused", client_factory=lambda: None, decisions_for=lambda conn, user_id: None, interval_seconds=3600,
        )
        with mock.patch.object(watcher, "run_once") as run_once:
            watcher.start()
            time.sleep(0.2)
            began = time.monotonic()
            watcher.stop()
            self.assertLess(time.monotonic() - began, 4)
        run_once.assert_not_called()

    def test_a_single_flight_manager_runs_one_job_and_reports_its_status(self):
        class Busy(RuntimeError):
            pass

        class Manager(background.SingleFlightManager):
            busy_error = Busy
            busy_message = "A test job is already running"
            idle_extra = {"mode": None}

        manager = Manager()
        idle = manager.status()
        self.assertEqual((idle["state"], idle["mode"], idle["result"]), ("idle", None, None))
        release = threading.Event()
        running = manager._launch("test-job", lambda: (release.wait(5), {"done": 1})[1], mode="report")
        self.assertEqual((running["state"], running["mode"]), ("running", "report"))
        with self.assertRaises(Busy) as raised:
            manager._launch("test-job-2", lambda: None, mode="apply")
        self.assertEqual(str(raised.exception), "A test job is already running")
        running["state"] = "tampered"
        self.assertEqual(manager.status()["state"], "running", "status() is a copy")
        release.set()
        manager.wait(5)
        done = manager.status()
        self.assertEqual((done["state"], done["result"], done["error"], done["mode"]), ("succeeded", {"done": 1}, None, "report"))
        self.assertTrue(done["finished_at"])

    def test_a_job_that_raises_is_failed_with_a_trimmed_message_and_the_next_job_may_start(self):
        manager = background.SingleFlightManager()

        def fail():
            raise ValueError("x" * 2_000)

        manager._launch("test-failing", fail)
        manager.wait(5)
        failed = manager.status()
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(len(failed["error"]), 1_000)
        self.assertIsNone(failed["result"])
        manager._launch("test-after", lambda: "fine")
        manager.wait(5)
        self.assertEqual(manager.status()["result"], "fine")

    def test_step_error_names_the_failure_and_never_an_address_or_a_query_string(self):
        error = background.step_error(RuntimeError("could not read the draft to greg@bovi.example via https://x.test/a?token=abc&b=1"))
        self.assertEqual(error, "RuntimeError: could not read the draft to [address] via https://x.test/a")
        self.assertEqual(len(background.step_error(ValueError("y" * 500))), len("ValueError: ") + 200)


class WorkstreamBCliRunnerTests(unittest.TestCase):
    def test_run_headless_sends_the_prompt_on_stdin_with_the_shared_flags(self):
        done = subprocess.CompletedProcess(["claude"], 0, "out", "")
        with mock.patch.object(agent_providers.subprocess, "run", return_value=done) as run:
            result = agent_providers.run_headless(["claude", "-p"], "the prompt", timeout=12.5, cwd="somewhere")
        self.assertIs(result, done)
        run.assert_called_once_with(
            ["claude", "-p"], input="the prompt", capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=12.5, cwd="somewhere", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    def test_run_headless_lets_a_timeout_and_a_missing_binary_propagate(self):
        for failure in (subprocess.TimeoutExpired("claude", 1), FileNotFoundError("claude")):
            with self.subTest(failure=type(failure).__name__), mock.patch.object(agent_providers.subprocess, "run", side_effect=failure):
                with self.assertRaises(type(failure)):
                    agent_providers.run_headless(["claude"], "p", timeout=1, cwd=".")

    def test_failure_detail_prefers_stderr_and_falls_back_to_stdout(self):
        self.assertEqual(agent_providers.failure_detail(subprocess.CompletedProcess([], 1, "out ", " err\n")), "err")
        self.assertEqual(agent_providers.failure_detail(subprocess.CompletedProcess([], 1, " out\n", "")), "out")
        self.assertEqual(agent_providers.failure_detail(subprocess.CompletedProcess([], 1, "", "")), "")

    def test_every_headless_call_uses_the_shared_no_tools_and_read_only_flags(self):
        self.assertEqual(agent_providers.CLAUDE_NO_TOOLS, ["-p", "--output-format", "text", "--tools", "", "--strict-mcp-config"])
        self.assertEqual(agent_providers.CODEX_READ_ONLY, ["exec", "--skip-git-repo-check", "--sandbox", "read-only"])
        seen = []

        def fake(command, prompt, **kwargs):
            seen.append(command)
            return subprocess.CompletedProcess(command, 0, "answer", "")

        with mock.patch.dict("os.environ", {"PIPELINE_OUTREACH_REVIEW_PROVIDER": "claude-code"}), \
                mock.patch.object(agent_providers, "provider_catalog", return_value=[
                    {"id": "claude-code", "display_name": "Claude Code", "model": "m", "configured": True, "setup_hint": ""},
                ]), mock.patch.object(outreach_review, "run_headless", fake):
            name, run = outreach_review.review_runner()
            run("review this")
        self.assertEqual(name, "claude-code")
        self.assertEqual(seen[0][1:], agent_providers.CLAUDE_NO_TOOLS)

    def test_the_cli_provider_command_is_the_shared_flags(self):
        claude = agent_providers.CliAgentProvider("claude-code", "m")
        codex = agent_providers.CliAgentProvider("codex-cli", "m")
        self.assertEqual(claude._command()[1:], agent_providers.CLAUDE_NO_TOOLS)
        self.assertEqual(codex._command()[1:], [*agent_providers.CODEX_READ_ONLY, "-"])

    def test_the_binary_name_and_availability_are_public(self):
        with mock.patch.dict("os.environ", {"PIPELINE_CODEX_BIN": "/opt/codex"}):
            self.assertEqual(agent_providers.cli_binary("codex-cli"), "/opt/codex")
        with mock.patch.object(agent_providers.shutil, "which", return_value=None):
            self.assertFalse(agent_providers.cli_available("claude"))
        with mock.patch.object(agent_providers.shutil, "which", return_value="/bin/claude"):
            self.assertTrue(agent_providers.cli_available("claude"))


class WorkstreamBOutreachLeafTests(unittest.TestCase):
    def test_gmail_web_url_opens_the_outreach_account_or_the_first_signed_in(self):
        self.assertEqual(outreach_config.gmail_web_url("all/abc", "me+x@gmail.com"), "https://mail.google.com/mail/?authuser=me%2Bx%40gmail.com#all/abc")
        self.assertEqual(outreach_config.gmail_web_url("all/abc", ""), "https://mail.google.com/mail/?authuser=0#all/abc")
        with mock.patch.dict("os.environ", {"PIPELINE_OUTREACH_ACCOUNT": " me@gmail.com "}):
            self.assertEqual(outreach_config.gmail_web_url("drafts"), "https://mail.google.com/mail/?authuser=me%40gmail.com#drafts")
        with mock.patch.dict("os.environ", {"PIPELINE_OUTREACH_ACCOUNT": ""}):
            self.assertEqual(outreach_config.gmail_web_url("drafts"), "https://mail.google.com/mail/?authuser=0#drafts")

    def test_the_sending_address_and_the_discovery_provider_read_the_environment_each_time(self):
        with mock.patch.dict("os.environ", {"PIPELINE_OUTREACH_ACCOUNT": "  a@b.test "}):
            self.assertEqual(outreach_config.sender_account(), "a@b.test")
        with mock.patch.dict("os.environ", {"PIPELINE_OUTREACH_DISCOVERY_PROVIDER": ""}):
            self.assertEqual(outreach_config.discovery_provider(), "claude-code")
        with mock.patch.dict("os.environ", {"PIPELINE_OUTREACH_DISCOVERY_PROVIDER": "codex-cli"}):
            self.assertEqual(outreach_config.discovery_provider(), "codex-cli")

    def test_the_writer_settings_names_are_the_ones_the_settings_page_writes(self):
        self.assertEqual(outreach_config.PURPOSE_ENV, {
            "follow_up": "PIPELINE_OUTREACH_FOLLOW_UP_PROVIDER",
            "call_prep": "PIPELINE_OUTREACH_CALL_PREP_PROVIDER",
            "thank_you": "PIPELINE_OUTREACH_THANK_YOU_PROVIDER",
        })

    def test_answers_are_matched_to_targets_by_name_and_the_unanswered_come_back_in_batch_order(self):
        targets = [{"id": "1", "company": "Acme Robotics"}, {"id": "2", "company": "Bovi"}, {"id": "3", "company": "Cyclo"}]
        reply = json.dumps({"companies": [
            {"company": "  bovi  "}, "not an object", {"company": "Nobody"}, {"company": "BOVI"}, {"company": "acme   robotics"},
        ]})
        matched, unanswered = outreach_batch.answers_by_target(reply, targets, "location search")
        self.assertEqual([(answer["company"], target["id"]) for answer, target in matched], [("  bovi  ", "2"), ("acme   robotics", "1")])
        self.assertEqual([target["id"] for target in unanswered], ["3"])

    def test_a_stored_name_with_a_double_space_is_never_matched(self):
        targets = [{"id": "1", "company": "Acme  Robotics"}]
        matched, unanswered = outreach_batch.answers_by_target(json.dumps({"companies": [{"company": "Acme Robotics"}]}), targets, "x")
        self.assertEqual((matched, [t["id"] for t in unanswered]), ([], ["1"]))

    def test_a_reply_without_a_companies_list_names_the_search_in_its_error(self):
        with self.assertRaisesRegex(ValueError, "The email search reply had no companies list"):
            outreach_batch.answers_by_target(json.dumps({"companies": "none"}), [], "email search")

    def test_close_browser_closes_in_order_swallows_errors_and_leaves_nothing_held(self):
        order = []

        class Owner:
            pass

        owner = Owner()
        owner._context = mock.Mock(close=lambda: order.append("context") or (_ for _ in ()).throw(RuntimeError("dead")))
        owner._browser = mock.Mock(close=lambda: order.append("browser"))
        owner._playwright = mock.Mock(stop=lambda: order.append("playwright"))
        web_fetch.close_browser(owner)
        self.assertEqual(order, ["context", "browser", "playwright"])
        self.assertEqual((owner._context, owner._browser, owner._playwright), (None, None, None))
        web_fetch.close_browser(owner)

    def test_ask_reviewer_fails_closed_and_each_caller_names_what_it_catches(self):
        held = {"send": False, "reviewer": "r"}
        good = json.dumps({"send": True, "problems": []})

        def raises(error):
            def run(prompt):
                raise error
            return run

        answer, hold = outreach_review.ask_reviewer(lambda prompt: good, "p", held, catch=(RuntimeError,))
        self.assertEqual((answer["send"], hold), (True, None))
        _, hold = outreach_review.ask_reviewer(raises(RuntimeError("down")), "p", held, catch=(RuntimeError,))
        self.assertEqual(hold, {**held, "problems": ["The reviewer could not run: down"]})
        with self.assertRaises(KeyError):
            outreach_review.ask_reviewer(raises(KeyError("odd")), "p", held, catch=(RuntimeError,))
        _, hold = outreach_review.ask_reviewer(raises(KeyError("odd")), "p", held, catch=(Exception,))
        self.assertTrue(hold["problems"][0].startswith("The reviewer could not run: "))
        for bad in ("not json", json.dumps({"send": "yes", "problems": []}), json.dumps({"send": True, "problems": [1]}),
                    json.dumps({"send": True, "problems": "none"}), json.dumps([1])):
            with self.subTest(reply=bad):
                answer, hold = outreach_review.ask_reviewer(lambda prompt, bad=bad: bad, "p", held, catch=(Exception,))
                self.assertEqual((answer, hold), (None, {**held, "problems": ["The reviewer's answer could not be read"]}))

    def test_review_log_detail_says_who_passed_or_held_it_and_is_trimmed(self):
        self.assertEqual(outreach_review.review_log_detail("codex-cli", {"send": True, "problems": []}), "Passed by codex-cli")
        self.assertEqual(outreach_review.review_log_detail("codex-cli", {"send": False, "problems": ["a", "b"]}), "Held by codex-cli: a; b")
        self.assertEqual(len(outreach_review.review_log_detail("r", {"send": False, "problems": ["z" * 3_000]})), 1_000)


class WorkstreamBOutreachStorageTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        _, self.path = build_and_migrate(Path(self.root.name))
        self.conn = connect_product(self.path)
        self.addCleanup(self.conn.close)
        self.target = create_target(self.conn, {"company": "Bovi", "contact_email": "greg@bovi.example", "website": "https://bovi.example"}, user_id=USER)

    def test_latest_event_stamp_is_the_newest_stored_stamp_of_that_type_or_none(self):
        self.assertIsNone(latest_event_stamp(self.conn, self.target["id"], USER, "bounced"))
        with self.conn:
            for stamp in ("2026-01-01T10:00:00+00:00", "2026-03-01T10:00:00+00:00", "2026-02-01T10:00:00+00:00"):
                self.conn.execute(
                    "INSERT INTO outreach_events(id, target_id, user_id, event_type, detail, created_at) VALUES(?, ?, ?, 'bounced', '', ?)",
                    (f"event-{stamp}", self.target["id"], USER, stamp),
                )
        self.assertEqual(latest_event_stamp(self.conn, self.target["id"], USER, "bounced"), "2026-03-01T10:00:00+00:00")
        self.assertIsNone(latest_event_stamp(self.conn, self.target["id"], USER, "sent"))
        self.assertIsNone(latest_event_stamp(self.conn, self.target["id"], "someone-else", "bounced"))

    def test_withdraw_auto_approval_touches_only_an_approved_draft_and_says_why(self):
        target_id = self.target["id"]
        with self.conn:
            withdraw_auto_approval(self.conn, target_id, USER, "not approved, so nothing happens")
        before = get_target(self.conn, target_id, user_id=USER, include_events=True)["events"]
        self.assertEqual([event for event in before if event["event_type"] == "approval_withdrawn"], [])
        with self.conn:
            self.conn.execute("UPDATE outreach_targets SET draft_status='approved' WHERE id=?", (target_id,))
            withdraw_auto_approval(self.conn, target_id, USER, "the automatic resend could not be queued")
        after = get_target(self.conn, target_id, user_id=USER, include_events=True)
        self.assertEqual(after["draft_status"], "generated")
        withdrawn = [event for event in after["events"] if event["event_type"] == "approval_withdrawn"]
        self.assertEqual([event["detail"] for event in withdrawn], ["the automatic resend could not be queued"])

    def test_log_event_is_the_public_name_for_writing_history(self):
        with self.conn:
            log_event(self.conn, self.target["id"], USER, "note", detail="hello")
        events = get_target(self.conn, self.target["id"], user_id=USER, include_events=True)["events"]
        self.assertIn(("note", "hello"), [(event["event_type"], event["detail"]) for event in events])


if __name__ == "__main__":
    unittest.main()
