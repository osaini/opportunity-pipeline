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
import time
import unittest
from contextlib import closing
from datetime import datetime
from pathlib import Path
from unittest import mock

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
        """legacy_sync.py lets a profile.json newer than profiles.updated_at overwrite the database profile, so a copy must not
        hand out a profile.json that is newer than its stored stamp.

        A real migration stamps updated_at from Python's clock right after profile.json was written, and on Windows the
        file system's clock can read a microsecond or so later (seen on the CI runner: .324160 vs .324159), so the fresh
        case is not strictly older either. Allow that skew, but not a file written after the template was built: the
        warm-up and the pause below make a copy that forgot to carry the template's mtime over at least 0.2 s too new.
        """
        skew = 0.01
        self.make("warm", helpers.build_and_migrate)
        time.sleep(0.2)
        for name, builder in (("fresh", helpers.build_and_migrate_fresh), ("cached", helpers.build_and_migrate)):
            with self.subTest(name):
                legacy, platform = self.make(name, builder)
                with closing(sqlite3.connect(platform)) as conn:
                    updated_at = conn.execute("SELECT updated_at FROM profiles").fetchone()[0]
                newer_by = (legacy.parent / "profile.json").stat().st_mtime - datetime.fromisoformat(updated_at).timestamp()
                self.assertLess(newer_by, skew, f"profile.json is {newer_by:.6f} s newer than profiles.updated_at {updated_at}")

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
        # Wrapping the real migration shows it ran: a cached copy never calls it, so a broken hatch would leave `calls` empty.
        previous = os.environ.get("PIPELINE_TEST_FRESH_DB")
        os.environ["PIPELINE_TEST_FRESH_DB"] = "1"
        try:
            with mock.patch.object(helpers, "migrate_legacy_database", wraps=helpers.migrate_legacy_database) as migrate:
                legacy, platform = self.make("hatch", helpers.build_and_migrate)
        finally:
            if previous is None:
                os.environ.pop("PIPELINE_TEST_FRESH_DB")
            else:
                os.environ["PIPELINE_TEST_FRESH_DB"] = previous
        migrate.assert_called_once()
        self.assertEqual(migrate.call_args.args[:2], (legacy, platform))
        with closing(sqlite3.connect(platform)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0], 2)

    def test_a_cached_copy_does_not_run_the_migration_again(self):
        self.make("warm", helpers.build_and_migrate)  # builds the template when this is the first call in the process
        with mock.patch.object(helpers, "migrate_legacy_database") as migrate:
            self.make("cold", helpers.build_and_migrate)
        migrate.assert_not_called()

    def test_committed_changes_still_in_the_legacy_wal_reach_the_migration(self):
        root = self.root / "wal"
        root.mkdir()
        legacy, platform = root / "pipeline.db", root / "platform.db"
        helpers._write_legacy_database(legacy)
        writer = sqlite3.connect(legacy)
        self.addCleanup(writer.close)
        # Held open, with auto-checkpointing off: the committed change stays in pipeline.db-wal, not in pipeline.db itself.
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE jobs SET title='Only in the legacy WAL' WHERE id='job-a'")
        writer.commit()
        self.assertTrue(legacy.with_name("pipeline.db-wal").stat().st_size, "the change must be uncheckpointed for this test to mean anything")
        helpers.migrate_cached(legacy, platform, helpers.build_profile(root))
        with closing(sqlite3.connect(platform)) as conn:
            titles = [row[0] for row in conn.execute("SELECT title FROM opportunities")]
        self.assertIn("Only in the legacy WAL", titles)

    def test_production_connections_keep_the_default_durability(self):
        """Only builds inside fast_throwaway_databases() skip the fsync."""
        from opportunity_app.core import database

        with closing(database.connect_product(self.root / "prod.db")) as conn:
            self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 2, "FULL")
        with helpers.fast_throwaway_databases():
            with closing(database.connect_product(self.root / "fast.db")) as conn:
                self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 0, "OFF")
        with closing(database.connect_product(self.root / "after.db")) as conn:
            self.assertEqual(conn.execute("PRAGMA synchronous").fetchone()[0], 2, "FULL again")


    def test_the_migration_a_build_runs_opens_its_target_without_the_fsync_too(self):
        """legacy_sync.migrate_legacy_database looks connect_product up in its own namespace, so the patch must reach it there."""
        from opportunity_app.opportunities import legacy_sync

        class Stop(Exception):
            pass

        seen = []

        def spy(conn):
            seen.append(conn.execute("PRAGMA synchronous").fetchone()[0])
            raise Stop

        legacy = self.root / "pipeline.db"
        helpers._write_legacy_database(legacy)
        profile = helpers.build_profile(self.root)
        for fast, expected in ((False, 2), (True, 0)):
            seen.clear()
            with mock.patch.object(legacy_sync, "ensure_product_schema", spy):
                with self.assertRaises(Stop):
                    if fast:
                        with helpers.fast_throwaway_databases():
                            legacy_sync.migrate_legacy_database(legacy, self.root / "fast.db", profile)
                    else:
                        legacy_sync.migrate_legacy_database(legacy, self.root / "plain.db", profile)
            self.assertEqual(seen, [expected], "fast" if fast else "plain")


if __name__ == "__main__":
    unittest.main()
