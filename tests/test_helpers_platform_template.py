"""The shared migrated-database template must be indistinguishable from a real migration.

``helpers_platform.build_and_migrate`` copies a per-process template instead of
replaying every migration, and ~190 test files depend on the copy. These tests
pin what "indistinguishable" means, so a change to the migration or to the
template helper that makes the copy drift fails here and not in some far-off
test.
"""

import os
import re
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import helpers_platform as helpers

TIMESTAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:\+00:00|Z)?")
# Columns that legitimately hold the time of the build or the location of the legacy file.
PATH_TABLE = "migration_runs"


def snapshot(platform_path: Path) -> dict[str, list[tuple]]:
    """Every table's rows with timestamps masked and path-bearing rows left out."""
    with closing(sqlite3.connect(platform_path)) as conn:
        tables = [
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        ]
        result = {}
        for name in tables:
            rows = [
                tuple(TIMESTAMP.sub("<ts>", value) if isinstance(value, str) else value for value in row)
                for row in conn.execute(f'SELECT * FROM "{name}"')
            ]
            result[name] = sorted(rows, key=repr)
        result["__schema__"] = list(conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY type, name"))
        return result


class TemplateCopyTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def make(self, name: str, builder) -> tuple[Path, Path]:
        root = self.root / name
        root.mkdir()
        return builder(root)

    def test_a_copy_has_the_same_files_schema_and_rows_as_a_real_migration(self):
        fresh_legacy, fresh_platform = self.make("fresh", helpers.build_and_migrate_fresh)
        cached_legacy, cached_platform = self.make("cached", helpers.build_and_migrate)
        for root in (fresh_legacy.parent, cached_legacy.parent):
            self.assertEqual(
                sorted(entry.name for entry in root.iterdir()),
                ["pipeline.db", "platform.db", "profile.json"],
                "no -wal or -shm sidecar, and the same files at the same paths",
            )
        fresh, cached = snapshot(fresh_platform), snapshot(cached_platform)
        self.assertEqual(fresh["__schema__"], cached["__schema__"])
        for table, rows in fresh.items():
            if table == PATH_TABLE:
                continue
            self.assertEqual(rows, cached[table], table)

    def test_the_migration_row_names_this_copys_own_legacy_file(self):
        legacy, platform = self.make("a", helpers.build_and_migrate)
        other_legacy, other_platform = self.make("b", helpers.build_and_migrate)
        for legacy_path, platform_path in ((legacy, platform), (other_legacy, other_platform)):
            with closing(sqlite3.connect(platform_path)) as conn:
                rows = conn.execute("SELECT migration_key, source_path FROM migration_runs").fetchall()
            self.assertEqual(rows, [("legacy-v1", str(legacy_path.resolve()))])

    def test_the_profile_file_is_older_than_the_stored_profile_as_on_a_real_run(self):
        """schema.py lets a file that is newer than profiles.updated_at overwrite the database profile."""
        for name, builder in (("fresh", helpers.build_and_migrate_fresh), ("cached", helpers.build_and_migrate)):
            with self.subTest(name):
                legacy, platform = self.make(name, builder)
                with closing(sqlite3.connect(platform)) as conn:
                    updated_at = conn.execute("SELECT updated_at FROM profiles").fetchone()[0]
                stored = datetime.fromisoformat(updated_at).timestamp()
                self.assertLess((legacy.parent / "profile.json").stat().st_mtime, stored)

    def test_the_activation_stamp_is_fresh_not_the_templates(self):
        first_legacy, first_platform = self.make("first", helpers.build_and_migrate)
        second_legacy, second_platform = self.make("second", helpers.build_and_migrate)
        with closing(sqlite3.connect(first_platform)) as a, closing(sqlite3.connect(second_platform)) as b:
            first = a.execute("SELECT MAX(applied_at) FROM schema_migrations").fetchone()[0]
            second = b.execute("SELECT MAX(applied_at) FROM schema_migrations").fetchone()[0]
        self.assertLess(first, second, "applied_at is restamped per copy, in order")

    def test_writing_to_one_copy_never_reaches_another(self):
        _, first = self.make("first", helpers.build_and_migrate)
        _, second = self.make("second", helpers.build_and_migrate)
        with closing(sqlite3.connect(first)) as conn, conn:
            conn.execute("DELETE FROM opportunities")
        with closing(sqlite3.connect(second)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0], 2)
        _, third = self.make("third", helpers.build_and_migrate)
        with closing(sqlite3.connect(third)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0], 2)

    def test_a_different_profile_gets_its_own_template(self):
        root = self.root / "custom"
        root.mkdir()
        legacy, platform = root / "pipeline.db", root / "platform.db"
        helpers._write_legacy_database(legacy)
        profile = legacy.parent / "profile.json"
        profile.write_text('{"name": "Someone Else", "regions": [], "remote_ok": false}', encoding="utf-8")
        helpers.migrate_cached(legacy, platform, profile)
        with closing(sqlite3.connect(platform)) as conn:
            stored = conn.execute("SELECT profile_json FROM profiles").fetchone()[0]
        self.assertIn("Someone Else", stored)

    def test_an_empty_migrated_database_has_every_migration_and_no_rows(self):
        path = self.root / "empty" / "platform.db"
        helpers.migrated_empty_db(path)
        self.assertEqual(sorted(entry.name for entry in path.parent.iterdir()), ["platform.db"])
        migrations = sorted(p.name for p in (Path(__file__).resolve().parent.parent / "migrations").glob("[0-9]*_*.sql"))
        with closing(sqlite3.connect(path)) as conn:
            applied = sorted(row[0] for row in conn.execute("SELECT name FROM schema_migrations"))
            self.assertEqual(applied, migrations)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0], 0)

    def test_the_escape_hatch_migrates_for_real(self):
        previous = os.environ.get("PIPELINE_TEST_FRESH_DB")
        os.environ["PIPELINE_TEST_FRESH_DB"] = "1"
        try:
            legacy, platform = self.make("hatch", helpers.build_and_migrate)
        finally:
            if previous is None:
                os.environ.pop("PIPELINE_TEST_FRESH_DB")
            else:
                os.environ["PIPELINE_TEST_FRESH_DB"] = previous
        with closing(sqlite3.connect(platform)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0], 2)

    def test_production_connections_keep_the_default_durability(self):
        """Only builds inside fast_throwaway_databases() skip the fsync."""
        from opportunity_app import schema

        with closing(schema.connect_product(self.root / "prod.db")) as conn:
            self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 2, "FULL")
        with helpers.fast_throwaway_databases():
            with closing(schema.connect_product(self.root / "fast.db")) as conn:
                self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 0, "OFF")
        with closing(schema.connect_product(self.root / "after.db")) as conn:
            self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 2, "FULL again")


if __name__ == "__main__":
    unittest.main()
