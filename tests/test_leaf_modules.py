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
import tempfile
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


# --- Workstream I: identity and the legacy boundary ---------------------------------------------------------------------

# Leaf module path -> first-party modules it may import (everything else must be standard library).
# Dotted names are absolute; "." names are relative imports within the leaf's own package.
IDENTITY_LEAVES: dict[str, set[str]] = {
    # Workstream I: identity and the legacy boundary
    "pipeline_core/identity.py": set(),
    "pipeline_core/regions.py": {".identity"},
    "pipeline_core/env.py": set(),
    "opportunity_app/storage_paths.py": set(),
}


def first_party_imports(path: Path) -> tuple[set[str], set[str]]:
    """(standard-library-or-other top-level modules imported, relative modules imported) for every import in a file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    absolute: set[str] = set()
    relative: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            absolute.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                relative.add("." * node.level + (node.module or ""))
            elif node.module:
                absolute.add(node.module)
    return absolute, relative


def imports_outside_the_stdlib(path: Path) -> set[str]:
    absolute, relative = first_party_imports(path)
    return {name for name in absolute if name.split(".")[0] not in sys.stdlib_module_names and name != "__future__"} | relative


class LeafModulesStayLeavesTests(unittest.TestCase):
    def test_each_leaf_imports_only_the_standard_library_and_the_leaves_it_names(self):
        for relative_path, allowed in IDENTITY_LEAVES.items():
            with self.subTest(leaf=relative_path):
                path = ROOT / relative_path
                self.assertTrue(path.is_file(), f"{relative_path} is listed as a leaf but does not exist")
                self.assertEqual(imports_outside_the_stdlib(path) - allowed, set(), f"{relative_path} imports outside its allowlist")

    def test_every_leaf_import_is_at_module_level_so_nothing_loads_late(self):
        for relative_path in IDENTITY_LEAVES:
            with self.subTest(leaf=relative_path):
                tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
                nested = [
                    node for node in ast.walk(tree)
                    if isinstance(node, (ast.Import, ast.ImportFrom)) and node not in tree.body
                ]
                self.assertEqual(nested, [], f"{relative_path} has a function-level import")


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
            absolute, _ = first_party_imports(path)
            if "pipeline" in absolute:
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


def imported_modules(path: Path) -> set[str]:
    """Every module a file imports, at any depth. A relative import is named "." + its module ('.mail_message')."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                names = [alias.name for alias in node.names] if not node.module else [node.module]
                found |= {"." + name.split(".")[0] for name in names}
            else:
                found.add((node.module or "").split(".")[0])
    return found


def outside_allowlist(path: Path, allowed_local: set[str] = frozenset()) -> set[str]:
    """What a leaf imports beyond the standard library and the leaves it may use ('.name' for a sibling module)."""
    return {
        name for name in imported_modules(path)
        if not (name in sys.stdlib_module_names or name == "__future__" or name in allowed_local)
    }


def parsed(raw: str):
    return BytesParser(policy=policy.default).parsebytes(raw.encode("utf-8"))


class MailMessageLeafTests(unittest.TestCase):
    def test_mail_message_imports_only_the_standard_library(self):
        self.assertEqual(outside_allowlist(APP / "mail_message.py"), set())

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
    def test_gmail_client_imports_only_the_standard_library_and_httpx(self):
        self.assertEqual(outside_allowlist(APP / "gmail_client.py", {"httpx"}), set())

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
        heavy = {".outreach_inbox", ".application_inbox", ".outreach_labels", ".outreach_delivery", ".automation", ".api"}
        self.assertEqual(imported_modules(APP / "outreach_identity.py") & heavy, set())

    def test_the_interviewer_and_research_take_identity_from_it_not_from_the_mail_reader(self):
        for module in ("outreach_interviewer.py", "outreach_research.py"):
            with self.subTest(module=module):
                self.assertNotIn(".outreach_inbox", imported_modules(APP / module))
                self.assertIn(".outreach_identity", imported_modules(APP / module))


if __name__ == "__main__":
    unittest.main()
