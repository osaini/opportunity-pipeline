"""The shared leaf modules of Phase 3: what each may import, and that each does what the copies it replaced did.

A leaf is a small module many others import. It stays a leaf by importing only the standard library and, where listed,
other leaves; the moment it imports schema, automation or outreach, the layering the refactor built is gone and an
import cycle is one edit away. Each section below belongs to one workstream, so the workstreams can add to this file
without touching each other's tests.
"""

import ast
import logging
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
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

APP = ROOT / "opportunity_app"


def imports_of(path: Path) -> tuple[set[str], set[str]]:
    """(absolute top-level module names, relative module names) imported anywhere in a file, lazy imports included."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    absolute: set[str] = set()
    relative: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            absolute.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative.add(node.module or "")
                if not node.module:
                    relative.update(alias.name for alias in node.names)
            elif node.module:
                absolute.add(node.module.split(".")[0])
    return absolute, relative


def module_level_imports(path: Path) -> set[str]:
    """Top-level names imported by statements that run when the module is imported (not inside a function or class)."""
    names: set[str] = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        for inner in ast.walk(node) if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) else ():
            if isinstance(inner, ast.Import):
                names.update(alias.name.split(".")[0] for alias in inner.names)
            elif isinstance(inner, ast.ImportFrom) and inner.module and not inner.level:
                names.add(inner.module.split(".")[0])
    return names


def assert_leaf(case: unittest.TestCase, name: str, allowed_relative: set[str], lazy_third_party: frozenset[str] = frozenset()) -> None:
    """``lazy_third_party`` names packages the leaf may import, but only inside a function, never when it is imported."""
    path = APP / f"{name}.py"
    absolute, relative = imports_of(path)
    outside = {module for module in absolute if module not in sys.stdlib_module_names and module != "__future__"}
    case.assertEqual(outside - lazy_third_party, set(), f"{name}.py imports beyond the standard library")
    case.assertEqual(module_level_imports(path) & lazy_third_party, set(), f"{name}.py imports a third-party package at import time")
    case.assertLessEqual(relative, allowed_relative, f"{name}.py imports modules a leaf may not")


# --- Workstream T: time, database, settings, JSON, stored profile -------------------------------------------------------


class WorkstreamTLeafImportTests(unittest.TestCase):
    """timestamps, database, settings_store, json_values and profile_store import nothing but the standard library
    (profile_store also imports json_values), so any module can import them without a cycle or a database."""

    def test_timestamps_is_standard_library_only(self):
        assert_leaf(self, "timestamps", set())

    def test_database_is_standard_library_only_apart_from_a_lazy_psycopg(self):
        # PostgreSQL support imports psycopg inside PostgresConnection, so SQLite-only installs never need it.
        assert_leaf(self, "database", set(), lazy_third_party=frozenset({"psycopg"}))

    def test_settings_store_is_standard_library_only(self):
        assert_leaf(self, "settings_store", set())

    def test_json_values_is_standard_library_only(self):
        assert_leaf(self, "json_values", set())

    def test_profile_store_imports_only_json_values(self):
        assert_leaf(self, "profile_store", {"json_values"})

    def test_user_time_is_standard_library_only(self):
        assert_leaf(self, "user_time", set())

    def test_the_leaves_open_no_connection_when_imported(self):
        for name in ("timestamps", "database", "settings_store", "json_values", "profile_store", "user_time"):
            tree = ast.parse((APP / f"{name}.py").read_text(encoding="utf-8"))
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)):
                    continue
                for inner in ast.walk(node):
                    if isinstance(inner, ast.Attribute) and inner.attr == "connect":
                        self.fail(f"{name}.py opens a connection at import (line {inner.lineno})")


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


if __name__ == "__main__":
    unittest.main()
