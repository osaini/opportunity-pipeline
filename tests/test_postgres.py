"""Hosted database contract; skipped unless a disposable PostgreSQL URL is supplied."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR
from opportunity_app.api import create_app
from opportunity_app.schema import connect_product, migrate_legacy_database
from pipeline_core import OpportunityFilters, OpportunityRepository
from helpers_platform import LEGACY_SCHEMA


POSTGRES_TEST_URL = os.environ.get("POSTGRES_TEST_URL", "")


@unittest.skipUnless(POSTGRES_TEST_URL, "POSTGRES_TEST_URL is not configured")
class PostgresContractTests(unittest.TestCase):
    def setUp(self):
        # Per test, not per class: these tests migrate into the same `public`
        # schema, and a row one test inserts (the 'manual-pg' capture) was
        # showing up in the next test's totals and pages. Each test now starts
        # from an empty schema. Also why this file stays out of the parallel
        # runner -- workers would still share the one database.
        import psycopg
        with psycopg.connect(POSTGRES_TEST_URL, autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")

    def test_every_sort_matches_sqlite_on_the_same_data(self):
        """The read model's ordering must not depend on the backend.

        database.py translates SQL by string substitution -- it rewrites `?` and
        deletes one exact spelling of COLLATE NOCASE -- so nothing guarantees
        that a query ordering one way on SQLite orders the same way on
        PostgreSQL. NULL ordering differs between them by default, and text
        collation differs too. The same rows go into both and the resulting ID
        order must be identical for every sort.
        """

        rows = [
            # id, company, posted_at, score, deadline prose -- chosen so each sort
            # produces a different order, and so NULLs, offsets, fractional
            # seconds and a non-ASCII name are all exercised.
            ("a", "Straße", "2026-05-01T00:00:00.000Z", 70, "Apply by March 1, 2026."),
            ("b", "Strasse", "2026-05-01T00:00:00Z", 70, ""),
            ("c", "acme", "2026-08-08T00:00:00-04:00", 90, "Apply by March 1, 2026."),
            ("d", "Borealis", None, 90, "Apply by September 1, 2026."),
            ("e", "Ørsted", "2026-01-01T00:00:00Z", 10, ""),
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "legacy.db"
            profile = root / "profile.json"
            profile.write_text(json.dumps({"name": "S", "regions": [], "remote_ok": True}), encoding="utf-8")
            with closing(sqlite3.connect(legacy)) as conn:
                conn.executescript(LEGACY_SCHEMA)
                for index, (job_id, company, posted_at, score, deadline_prose) in enumerate(rows):
                    conn.execute(
                        "INSERT INTO jobs VALUES(" + ",".join("?" * 23) + ")",
                        (job_id, "test:pg", "Postgres Test", job_id, company, "Mechanical Intern",
                         "Remote", "internship", f"https://example.com/{job_id}", f"CAD work. {deadline_prose}", posted_at,
                         f"2026-08-{10 + index:02d}T00:00:00+00:00", f"2026-08-{10 + index:02d}T00:00:00+00:00",
                         1, f"fp-{job_id}", f"cfp-{job_id}", None, score, "[]", "discovered", "", None, None),
                    )
                conn.commit()

            sqlite_target = root / "platform.db"
            migrate_legacy_database(legacy, sqlite_target, profile)
            migrate_legacy_database(legacy, POSTGRES_TEST_URL, profile)

            for sort in ("score", "newest", "discovered", "company", "deadline"):
                for user_id in (None, "local-user"):
                    with self.subTest(sort=sort, path="tenant" if user_id else "cli"):
                        with closing(connect_product(sqlite_target, read_only=True)) as lite:
                            expected = [
                                item["id"]
                                for item in OpportunityRepository(lite, user_id=user_id).list(
                                    OpportunityFilters(sort=sort, limit=200)
                                )[0]
                            ]
                        with closing(connect_product(POSTGRES_TEST_URL)) as postgres:
                            actual = [
                                item["id"]
                                for item in OpportunityRepository(postgres, user_id=user_id).list(
                                    OpportunityFilters(sort=sort, limit=200)
                                )[0]
                            ]
                        self.assertEqual(actual, expected, f"{sort} ordered differently on PostgreSQL")

    def test_paging_and_totals_match_sqlite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "legacy.db"
            profile = root / "profile.json"
            profile.write_text(json.dumps({"name": "S", "regions": [], "remote_ok": True}), encoding="utf-8")
            with closing(sqlite3.connect(legacy)) as conn:
                conn.executescript(LEGACY_SCHEMA)
                for index in range(11):
                    conn.execute(
                        "INSERT INTO jobs VALUES(" + ",".join("?" * 23) + ")",
                        (f"row-{index:02d}", "test:pg", "Postgres Test", str(index), "Acme",
                         "Mechanical Intern", "Remote", "internship", f"https://example.com/{index}",
                         "CAD", None, "2026-08-10T00:00:00+00:00", "2026-08-10T00:00:00+00:00",
                         1, f"fp-{index}", f"cfp-{index}", None, 100 - index, "[]", "discovered", "", None, None),
                    )
                conn.commit()
            sqlite_target = root / "platform.db"
            migrate_legacy_database(legacy, sqlite_target, profile)
            migrate_legacy_database(legacy, POSTGRES_TEST_URL, profile)
            for user_id in (None, "local-user"):
                with self.subTest(path="tenant" if user_id else "cli"):
                    pages = {}
                    for label, target in (("sqlite", sqlite_target), ("postgres", POSTGRES_TEST_URL)):
                        with closing(connect_product(target, read_only=(label == "sqlite"))) as conn:
                            repo = OpportunityRepository(conn, user_id=user_id)
                            collected, total = [], None
                            for offset in (0, 4, 8):
                                items, total = repo.list(OpportunityFilters(limit=4, offset=offset))
                                collected.extend(item["id"] for item in items)
                            pages[label] = (collected, total)
                    self.assertEqual(pages["postgres"], pages["sqlite"])
                    self.assertEqual(pages["sqlite"][1], 11)

    def test_migration_read_model_and_authenticated_api(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "legacy.db"
            profile = root / "profile.json"
            profile.write_text(json.dumps({"name": "Postgres Student", "regions": [], "remote_ok": True}), encoding="utf-8")
            with closing(sqlite3.connect(legacy)) as conn:
                conn.executescript("""CREATE TABLE jobs (
                    id TEXT PRIMARY KEY, source_key TEXT NOT NULL, source_name TEXT NOT NULL, external_id TEXT NOT NULL,
                    company TEXT NOT NULL, title TEXT NOT NULL, location TEXT NOT NULL DEFAULT '', role_type TEXT NOT NULL DEFAULT 'other',
                    url TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', posted_at TEXT, first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, fingerprint TEXT NOT NULL,
                    content_fingerprint TEXT NOT NULL DEFAULT '', duplicate_of TEXT, score INTEGER NOT NULL DEFAULT 0,
                    score_explanation TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL DEFAULT 'discovered', notes TEXT NOT NULL DEFAULT '',
                    applied_at TEXT, follow_up_at TEXT);""")
                conn.execute("""INSERT INTO jobs VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
                    "pg-job", "test:pg", "Postgres Test", "1", "Acme", "Mechanical Intern", "Remote", "internship",
                    "https://example.com/pg", "Test fixtures with CAD", None, "2026-08-10T00:00:00+00:00", "2026-08-10T00:00:00+00:00",
                    1, "fp", "cfp", None, 80, '["CAD match"]', "discovered", "", None, None,
                ))
                conn.commit()
            result = migrate_legacy_database(legacy, POSTGRES_TEST_URL, profile)
            self.assertTrue(result.top_ids_match)
            app = create_app(database_url=POSTGRES_TEST_URL, access_token="pg-secret", static_dir=STATIC_DIR)
            with TestClient(app) as client:
                response = client.get("/api/v1/opportunities", headers={"Authorization": "Bearer pg-secret"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["items"][0]["id"], "pg-job")
                self._deadlines_and_urgent(client)

    def _deadlines_and_urgent(self, client):
        """Deadline UPSERT, Urgent aggregation, capture ownership and cascades on PostgreSQL."""
        from datetime import date, timedelta

        from opportunity_app import urgent
        from opportunity_app.schema import LOCAL_USER_ID, connect_product

        auth = {"Authorization": "Bearer pg-secret"}
        soon = (date.today() + timedelta(days=3)).isoformat()
        later = (date.today() + timedelta(days=5)).isoformat()
        stamp = "2026-09-01T00:00:00+00:00"

        first = client.put("/api/v1/opportunities/pg-job/deadline", headers=auth, json={"deadline_on": soon, "note": "careers"})
        self.assertEqual(first.status_code, 200, first.text)
        second = client.put("/api/v1/opportunities/pg-job/deadline", headers=auth, json={"deadline_on": later, "note": ""})
        self.assertEqual(second.json()["created_at"], first.json()["created_at"])
        detail = client.get("/api/v1/opportunities/pg-job", headers=auth).json()
        self.assertEqual(detail["user_deadline"]["deadline_on"], later)
        self.assertTrue(detail["can_set_user_deadline"])
        listed = client.get("/api/v1/opportunities", headers=auth).json()["items"]
        self.assertEqual(listed[0]["user_deadline_on"], later)
        empty = client.get("/api/v1/opportunities?offset=500", headers=auth)
        self.assertEqual(empty.status_code, 200)
        self.assertEqual(empty.json()["items"], [])
        queue = client.get("/api/v1/urgent", headers=auth).json()
        self.assertEqual([(item["kind"], item["date"]) for item in queue["items"]], [("your_deadline", later)])
        self.assertEqual(client.put("/api/v1/opportunities/pg-missing/deadline", headers=auth,
                                    json={"deadline_on": soon}).status_code, 404)

        with closing(connect_product(POSTGRES_TEST_URL)) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO users(id, email, display_name, role, created_at, updated_at) VALUES('pg-other', 'o@example.com', 'Other', 'student', ?, ?)",
                    (stamp, stamp),
                )
                # A confirmed capture owned by the local student.
                conn.execute(
                    """INSERT INTO opportunities(id, company, title, location, region, role_type, url, description,
                           posted_at, deadline_at, first_seen_at, last_seen_at, active, fingerprint, content_fingerprint,
                           duplicate_of, created_at, updated_at)
                       VALUES('manual-pg', 'Private', 'Captured', '', '', 'internship', 'https://example.com/c', '',
                           NULL, ?, ?, ?, 1, 'fp-c', 'cfp-c', NULL, ?, ?)""",
                    (f"{soon}T00:00:00+00:00", stamp, stamp, stamp, stamp),
                )
                conn.execute(
                    """INSERT INTO opportunity_sources(opportunity_id, source_key, source_name, external_id, source_url, first_seen_at, last_seen_at)
                       VALUES('manual-pg', 'manual:capture', 'Manual capture', 'capture-pg', 'https://example.com/c', ?, ?)""",
                    (stamp, stamp),
                )
                for app_id, user_id in (("app-owner", LOCAL_USER_ID), ("app-hostile", "pg-other")):
                    conn.execute(
                        "INSERT INTO applications(id, opportunity_id, user_id, stage, created_at, updated_at) VALUES(?, 'manual-pg', ?, 'applying', ?, ?)",
                        (app_id, user_id, stamp, stamp),
                    )
                conn.execute(
                    """INSERT INTO opportunity_captures(id, user_id, source_type, source_url, status, application_id, created_at)
                       VALUES('capture-pg', ?, 'url', 'https://example.com/c', 'confirmed', 'app-owner', ?)""",
                    (LOCAL_USER_ID, stamp),
                )
            owner = urgent.urgent_queue(conn, user_id=LOCAL_USER_ID)
            self.assertIn("posting_deadline:manual-pg", [item["key"] for item in owner["items"]])
            hostile = urgent.urgent_queue(conn, user_id="pg-other")
            self.assertEqual(hostile["items"], [], "an application on a leaked capture is not ownership")
            self.assertFalse(urgent.visible_opportunity(conn, "pg-other", "manual-pg"))
            with conn:
                conn.execute("DELETE FROM opportunity_captures WHERE id='capture-pg'")
            self.assertNotIn("posting_deadline:manual-pg", [item["key"] for item in urgent.urgent_queue(conn, user_id=LOCAL_USER_ID)["items"]])

        self.assertEqual(client.delete("/api/v1/opportunities/pg-job/deadline", headers=auth).status_code, 204)
        self.assertEqual(client.delete("/api/v1/opportunities/pg-job/deadline", headers=auth).status_code, 204)
        self.assertEqual(client.put("/api/v1/opportunities/pg-job/deadline", headers=auth,
                                    json={"deadline_on": soon}).status_code, 200)
        with closing(connect_product(POSTGRES_TEST_URL)) as conn:
            with conn:
                conn.execute("DELETE FROM opportunity_interactions WHERE opportunity_id='pg-job'")
                conn.execute("DELETE FROM opportunities WHERE id='pg-job'")
            remaining = conn.execute("SELECT COUNT(*) AS n FROM opportunity_deadlines").fetchone()["n"]
        self.assertEqual(remaining, 0, "deadlines cascade away with their posting")
