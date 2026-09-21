"""`posted_at_utc`: the derived key that makes date ordering chronological.

`posted_at` is stored exactly as each source API sent it, and the boards
disagree on spelling -- `Z`, `.000Z`, and offsets like `-04:00` all occur.
Ordered as text, `...17Z` lands after `...17.999999+00:00` despite being nearly
a second earlier, so `newest` ordered cards by spelling rather than by date.

These tests cover the upgrade path specifically. A freshly created database
populates the column during ingest and would never exercise the backfill that
existing databases actually take, so the fixtures here stop at migration 0019
and then apply the new one.
"""

from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opportunity_app import schema
from opportunity_app.schema import (
    backfill_posted_at_utc,
    connect_product,
    ensure_product_schema,
)
from opportunity_app.timestamps import canonical_utc
from pipeline_core import OpportunityFilters, OpportunityRepository

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


def schema_at_0019(path: Path) -> sqlite3.Connection:
    """A database as it stood before this migration, ready to be upgraded."""

    conn = connect_product(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for migration in sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        if migration.name >= "0020":
            break
        conn.executescript(migration.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO schema_migrations(name, applied_at) VALUES(?, '2026-09-01T00:00:00.000000+00:00')",
            (migration.name,),
        )
    conn.commit()
    return conn


def seed(conn: sqlite3.Connection, rows: list[tuple[str, str | None]], *, score: int = 50) -> None:
    """Insert opportunities with the given (id, raw posted_at), plus a source each."""

    conn.execute(
        "INSERT OR IGNORE INTO users(id, email, display_name, role, created_at, updated_at) "
        "VALUES('local-user', NULL, 'Test', 'student', '2026-09-01T00:00:00.000000+00:00', "
        "'2026-09-01T00:00:00.000000+00:00')"
    )
    for opportunity_id, posted_at in rows:
        conn.execute(
            """
            INSERT INTO opportunities(
                id, company, title, location, region, role_type, url, description,
                posted_at, deadline_at, first_seen_at, last_seen_at, active,
                fingerprint, content_fingerprint, duplicate_of, created_at, updated_at
            ) VALUES(?, 'Acme', 'Intern', 'Austin, TX', 'Austin', 'internship',
                     ?, 'desc', ?, NULL,
                     '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', 1,
                     ?, ?, NULL, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')
            """,
            (opportunity_id, f"https://example.com/{opportunity_id}", posted_at, opportunity_id, opportunity_id),
        )
        conn.execute(
            "INSERT INTO opportunity_sources(opportunity_id, source_key, source_name, external_id, "
            "source_url, first_seen_at, last_seen_at) VALUES(?, 'greenhouse:acme', 'Acme', ?, ?, "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            (opportunity_id, opportunity_id, f"https://example.com/{opportunity_id}"),
        )
        conn.execute(
            "INSERT INTO fit_scores(opportunity_id, user_id, ruleset_version, score, explanation_json, created_at) "
            "VALUES(?, 'local-user', 'legacy-v1', ?, '[]', '2026-09-01T00:00:00.000000+00:00')",
            (opportunity_id, score),
        )
    conn.commit()


class CanonicalUtcTests(unittest.TestCase):
    def test_every_spelling_the_boards_actually_send_is_normalised(self):
        for raw, expected in (
            ("2026-07-18T09:14:00Z", "2026-07-18T09:14:00.000000+00:00"),       # Adzuna
            ("2026-05-01T00:00:00.000Z", "2026-05-01T00:00:00.000000+00:00"),   # Ashby
            ("2026-06-01T00:00:00Z", "2026-06-01T00:00:00.000000+00:00"),       # USAJOBS
            ("2026-08-08T00:00:00-04:00", "2026-08-08T04:00:00.000000+00:00"),  # Greenhouse
        ):
            with self.subTest(raw=raw):
                self.assertEqual(canonical_utc(raw), expected)

    def test_a_timestamp_without_a_zone_is_recorded_as_not_stated(self):
        """Reading it as UTC would invent an offset the source never gave.

        Python parses a naive string in the *machine's* zone, so calling it UTC
        makes a laptop that travels reorder its own cards.
        """

        for raw in ("2026-09-21T01:49:17", "2026-09-21", "", "   ", None, "not a date"):
            with self.subTest(raw=raw):
                self.assertIsNone(canonical_utc(raw))

    def test_canonical_text_order_is_chronological_order(self):
        raw = [
            "2026-09-21T01:49:17.999999+00:00",
            "2026-09-21T01:49:17Z",
            "2026-09-21T01:49:18+00:00",
            "2026-09-21T01:49:17-05:00",
        ]
        chronological = sorted(
            raw, key=lambda v: datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(timezone.utc)
        )
        self.assertEqual(sorted(raw, key=canonical_utc), chronological)
        # ...and the raw text order is not, which is the whole reason for this.
        self.assertNotEqual(sorted(raw), chronological)


class UpgradeBackfillTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "platform.db"

    def test_the_backfill_covers_every_shape_an_existing_row_can_hold(self):
        conn = schema_at_0019(self.path)
        self.addCleanup(conn.close)
        seed(conn, [
            ("valid-z", "2026-07-18T09:14:00Z"),
            ("fractional", "2026-05-01T00:00:00.000Z"),
            ("offset", "2026-08-08T00:00:00-04:00"),
            ("naive", "2026-06-01T00:00:00"),
            ("empty", ""),
            ("null", None),
            ("invalid", "not a date"),
        ])
        ensure_product_schema(conn)

        stored = {
            str(row["id"]): row["posted_at_utc"]
            for row in conn.execute("SELECT id, posted_at_utc FROM opportunities")
        }
        self.assertEqual(stored["valid-z"], "2026-07-18T09:14:00.000000+00:00")
        self.assertEqual(stored["fractional"], "2026-05-01T00:00:00.000000+00:00")
        self.assertEqual(stored["offset"], "2026-08-08T04:00:00.000000+00:00")
        for unresolvable in ("naive", "empty", "null", "invalid"):
            with self.subTest(row=unresolvable):
                self.assertIsNone(stored[unresolvable])

    def test_the_raw_posted_at_is_never_rewritten(self):
        """The source's own words are what the product promises to preserve."""

        originals = [
            ("valid-z", "2026-07-18T09:14:00Z"),
            ("fractional", "2026-05-01T00:00:00.000Z"),
            ("offset", "2026-08-08T00:00:00-04:00"),
            ("invalid", "not a date"),
        ]
        conn = schema_at_0019(self.path)
        self.addCleanup(conn.close)
        seed(conn, originals)
        ensure_product_schema(conn)
        after = {str(r["id"]): r["posted_at"] for r in conn.execute("SELECT id, posted_at FROM opportunities")}
        self.assertEqual(after, dict(originals))

    def test_the_view_exposes_the_derived_column(self):
        conn = schema_at_0019(self.path)
        self.addCleanup(conn.close)
        seed(conn, [("a", "2026-07-18T09:14:00Z")])
        ensure_product_schema(conn)
        row = conn.execute("SELECT * FROM opportunity_read_model WHERE id='a'").fetchone()
        self.assertEqual(row["posted_at_utc"], "2026-07-18T09:14:00.000000+00:00")

    # --- restart safety -----------------------------------------------------

    def test_a_crash_after_the_column_is_added_still_upgrades_cleanly(self):
        """DDL commits on its own, so the column can outlive the marker.

        Without a guard, the retry fails on a duplicate column and the database
        can never finish migrating.
        """

        conn = schema_at_0019(self.path)
        self.addCleanup(conn.close)
        seed(conn, [("a", "2026-07-18T09:14:00Z")])
        # Exactly what a crash between the ALTER and the marker leaves behind.
        conn.execute("ALTER TABLE opportunities ADD COLUMN posted_at_utc TEXT")
        conn.commit()

        ensure_product_schema(conn)
        self.assertEqual(
            conn.execute("SELECT posted_at_utc FROM opportunities WHERE id='a'").fetchone()[0],
            "2026-07-18T09:14:00.000000+00:00",
        )
        applied = {str(r[0]) for r in conn.execute("SELECT name FROM schema_migrations")}
        self.assertIn("0020_posted_at_utc.sql", applied)

    def test_a_crash_midway_through_the_backfill_is_recovered_by_rerunning(self):
        conn = schema_at_0019(self.path)
        self.addCleanup(conn.close)
        seed(conn, [(f"row-{i}", "2026-07-18T09:14:00Z") for i in range(6)])
        conn.execute("ALTER TABLE opportunities ADD COLUMN posted_at_utc TEXT")
        # Half-finished work, as an interrupted backfill would leave it.
        conn.execute("UPDATE opportunities SET posted_at_utc=? WHERE id IN ('row-0','row-1')",
                     ("2026-07-18T09:14:00.000000+00:00",))
        conn.commit()

        ensure_product_schema(conn)
        values = {r["posted_at_utc"] for r in conn.execute("SELECT posted_at_utc FROM opportunities")}
        self.assertEqual(values, {"2026-07-18T09:14:00.000000+00:00"})

    def test_the_backfill_is_idempotent(self):
        conn = schema_at_0019(self.path)
        self.addCleanup(conn.close)
        seed(conn, [("a", "2026-07-18T09:14:00Z"), ("b", "2026-06-01T00:00:00")])
        ensure_product_schema(conn)
        first = dict(conn.execute("SELECT id, posted_at_utc FROM opportunities").fetchall())
        backfill_posted_at_utc(conn)
        backfill_posted_at_utc(conn)
        conn.commit()
        self.assertEqual(dict(conn.execute("SELECT id, posted_at_utc FROM opportunities").fetchall()), first)


class RankingTests(unittest.TestCase):
    """The ordering this migration exists to correct, on both repository paths."""

    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "platform.db"
        self.conn = schema_at_0019(self.path)
        self.addCleanup(self.conn.close)

    def ordered(self, sort: str, user_id):
        repo = OpportunityRepository(self.conn, user_id=user_id)
        items, _ = repo.list(OpportunityFilters(sort=sort, limit=50))
        return [item["id"] for item in items]

    def test_newest_orders_equal_instants_written_differently_by_time(self):
        # 'zulu' is nearly a second EARLIER than 'fraction', but sorts after it
        # as raw text because 'Z' > '.'.
        seed(self.conn, [
            ("zulu", "2026-09-21T01:49:17Z"),
            ("fraction", "2026-09-21T01:49:17.999999+00:00"),
            ("later", "2026-09-21T01:49:18+00:00"),
        ])
        ensure_product_schema(self.conn)
        for user_id in (None, "local-user"):
            with self.subTest(path="tenant" if user_id else "cli"):
                self.assertEqual(self.ordered("newest", user_id), ["later", "fraction", "zulu"])

    def test_an_offset_timestamp_is_placed_by_its_real_instant(self):
        # 14:00-05:00 is 19:00Z -- later than 18:00Z, though '1' < '8' as text.
        seed(self.conn, [
            ("utc-evening", "2026-09-21T18:00:00Z"),
            ("central-afternoon", "2026-09-21T14:00:00-05:00"),
        ])
        ensure_product_schema(self.conn)
        for user_id in (None, "local-user"):
            with self.subTest(path="tenant" if user_id else "cli"):
                self.assertEqual(
                    self.ordered("newest", user_id), ["central-afternoon", "utc-evening"]
                )

    def test_both_repository_paths_agree_on_every_sort(self):
        seed(self.conn, [
            ("a", "2026-09-21T01:49:17Z"),
            ("b", "2026-09-21T01:49:17.999999+00:00"),
            ("c", "2026-08-08T00:00:00-04:00"),
            ("d", None),
            ("e", "2026-06-01T00:00:00"),
        ])
        ensure_product_schema(self.conn)
        for sort in ("score", "newest", "discovered", "company", "deadline"):
            with self.subTest(sort=sort):
                self.assertEqual(self.ordered(sort, None), self.ordered(sort, "local-user"))

    def test_a_source_that_stated_no_zone_falls_back_to_when_it_was_seen(self):
        """A named, deliberate change.

        A naive posted_at used to be read in the machine's local zone and used
        as the sort key. It is now NULL, so `newest` falls back to
        first_seen_at, the same fallback a missing date has always taken.
        """

        seed(self.conn, [("naive", "2026-06-01T00:00:00"), ("absent", None)])
        ensure_product_schema(self.conn)
        rows = dict(self.conn.execute("SELECT id, posted_at_utc FROM opportunities").fetchall())
        self.assertIsNone(rows["naive"])
        self.assertIsNone(rows["absent"])
        # Identical first_seen_at, so they tie and the id breaks it -- proving
        # both took the fallback rather than one sorting by a parsed local time.
        self.assertEqual(self.ordered("newest", "local-user"), ["absent", "naive"])


class SyncRefreshTests(unittest.TestCase):
    """The sync rewrites posted_at on every run; the derived value must follow."""

    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)

    def sync(self, posted_at):
        """Run a legacy->product migration with one posting's posted_at."""

        legacy = self.root / "pipeline.db"
        legacy.unlink(missing_ok=True)
        conn = sqlite3.connect(legacy)
        from helpers_platform import LEGACY_SCHEMA, build_profile
        conn.executescript(LEGACY_SCHEMA)
        conn.execute(
            "INSERT INTO jobs VALUES(" + ",".join("?" * 23) + ")",
            ("job-a", "greenhouse:acme", "Acme", "a-1", "Acme", "Intern", "Austin, TX",
             "internship", "https://example.com/a", "desc", posted_at,
             "2026-08-08T01:00:00+00:00", "2026-08-09T01:00:00+00:00", 1, "fp", "cfp",
             None, 50, "[]", "discovered", "", None, None),
        )
        conn.commit(); conn.close()
        schema.migrate_legacy_database(legacy, self.root / "platform.db", build_profile(self.root))
        # closing(), not `with sqlite3.connect(...)`: the latter manages the
        # transaction and leaves the connection open, which holds the file and
        # breaks the temporary directory's cleanup on Windows.
        with closing(sqlite3.connect(self.root / "platform.db")) as target:
            target.row_factory = sqlite3.Row
            return target.execute(
                "SELECT posted_at, posted_at_utc FROM opportunities WHERE id='job-a'"
            ).fetchone()

    def test_the_derived_value_tracks_every_change_to_the_raw_one(self):
        for label, raw, expected in (
            ("valid", "2026-07-18T09:14:00Z", "2026-07-18T09:14:00.000000+00:00"),
            ("valid -> different valid", "2026-08-08T00:00:00-04:00", "2026-08-08T04:00:00.000000+00:00"),
            ("valid -> invalid", "not a date", None),
            ("invalid -> valid", "2026-06-01T00:00:00Z", "2026-06-01T00:00:00.000000+00:00"),
            ("valid -> missing", None, None),
        ):
            with self.subTest(change=label):
                row = self.sync(raw)
                self.assertEqual(row["posted_at"], raw)
                self.assertEqual(row["posted_at_utc"], expected)


if __name__ == "__main__":
    unittest.main()
