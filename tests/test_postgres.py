"""Hosted database contract; skipped unless a disposable PostgreSQL URL is supplied."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR, automation, outreach_schedule, schema
from opportunity_app.actions import record_intent, update_application
from opportunity_app.api import create_app
from opportunity_app.automation import Feature
from opportunity_app.schema import MIGRATIONS_DIR, connect_product, ensure_product_schema, migrate_legacy_database, utc_now
from pipeline_core import OpportunityFilters, OpportunityRepository
from helpers_platform import JOBS, LEGACY_SCHEMA, build_profile

AUTOMATION_USER = "local-user"
AUTOMATION_SWITCH = Feature("test_pg_switch", "Test PostgreSQL switch", "Moves an application on its own", "applications", "internal")
# The columns 0037 adds to existing tables, each by a guarded Python step.
AUTOMATION_COLUMNS = [
    ("opportunity_interactions", "source"), ("application_tasks", "origin"), ("application_tasks", "origin_ref"),
    ("connector_accounts", "last_ok_at"), ("connector_accounts", "last_error"), ("connector_accounts", "token_granted_at"),
    ("connector_accounts", "backoff_until"),
]


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
                with self.subTest(path="tenant" if user_id else "cli", per_company=5):
                    capped = {}
                    for label, target in (("sqlite", sqlite_target), ("postgres", POSTGRES_TEST_URL)):
                        with closing(connect_product(target, read_only=(label == "sqlite"))) as conn:
                            items, total = OpportunityRepository(conn, user_id=user_id).list(
                                OpportunityFilters(per_company=5, limit=4, offset=4)
                            )
                            capped[label] = ([(item["id"], item["company_total"]) for item in items], total)
                    self.assertEqual(capped["postgres"], capped["sqlite"])
                    self.assertEqual(capped["sqlite"], ([("row-04", 11)], 5))

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


@unittest.skipUnless(POSTGRES_TEST_URL, "POSTGRES_TEST_URL is not configured")
class PostgresAutomationContractTests(unittest.TestCase):
    """The automation ledger, the pause, and Health on PostgreSQL (migration 0037, automation.py).

    Their PostgreSQL-only paths (pause_guard's FOR SHARE, the handlers' FOR
    UPDATE, IS NOT DISTINCT FROM, the Python migration step) run nowhere
    else. Rows are read by column name: a PostgreSQL row is a mapping, so
    tuple(row) would give its column names.
    """

    def setUp(self):
        import psycopg

        # A fresh schema per test, as PostgresContractTests does, for the same reason.
        with psycopg.connect(POSTGRES_TEST_URL, autocommit=True) as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        legacy = root / "pipeline.db"
        with closing(sqlite3.connect(legacy)) as conn:
            conn.executescript(LEGACY_SCHEMA)
            conn.executemany("INSERT INTO jobs VALUES(" + ",".join("?" * 23) + ")", JOBS)
            conn.commit()
        migrate_legacy_database(legacy, POSTGRES_TEST_URL, build_profile(root))
        self.conn = connect_product(POSTGRES_TEST_URL)
        self.addCleanup(self.conn.close)
        automation.register(AUTOMATION_SWITCH)
        self.addCleanup(automation.FEATURES.pop, AUTOMATION_SWITCH.key, None)

    def other_connection(self, *, lock_timeout_ms=None):
        conn = connect_product(POSTGRES_TEST_URL)
        self.addCleanup(conn.close)
        if lock_timeout_ms is not None:
            # A session setting, so it outlives the transaction this statement opens.
            conn.execute(f"SET lock_timeout = '{int(lock_timeout_ms)}ms'")
            conn.commit()
        return conn

    def stage_change(self, application_id, stage, *, key, conn=None):
        return automation.perform(
            conn or self.conn, user_id=AUTOMATION_USER, feature=AUTOMATION_SWITCH.key, action_type="application.stage",
            subject_kind="application", subject_id=application_id, after={"stage": stage}, evidence={"subject": "Interview"},
            summary="Moved the application", basis="rule:test", confidence=0.9, idempotency_key=key, auto=True,
        )

    def application(self, application_id):
        row = self.conn.execute("SELECT stage, applied_at FROM applications WHERE id=?", (application_id,)).fetchone()
        self.conn.commit()
        return row["stage"], row["applied_at"]

    def outreach_target(self, target_id, company):
        now = utc_now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO outreach_targets(id, user_id, company, created_at, updated_at) VALUES(?, ?, ?, ?, ?)",
                (target_id, AUTOMATION_USER, company, now, now),
            )

    def claim(self, target_id, state, action, claimed_at=None):
        with self.conn:
            self.conn.execute(
                "INSERT INTO outreach_send_claims(target_id, user_id, kind, token, state, action, instance, claimed_at) "
                "VALUES(?, ?, 'initial', 'tok', ?, ?, 'i', ?)", (target_id, AUTOMATION_USER, state, action, claimed_at or utc_now()),
            )

    def test_migration_0037_applies_and_a_rerun_repairs_a_half_applied_upgrade(self):
        for table, column in AUTOMATION_COLUMNS:
            self.assertTrue(schema._has_column(self.conn, table, column), f"{table}.{column}")
        stamp = utc_now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO users(id, email, display_name, role, created_at, updated_at) VALUES('student-2', NULL, 'S', 'student', ?, ?)",
                (stamp, stamp),
            )
        automation.set_paused(self.conn, AUTOMATION_USER, True)
        # What a crash between the ALTERs and the marker leaves: some columns gone, and no marker.
        with self.conn:
            self.conn.execute("ALTER TABLE connector_accounts DROP COLUMN backoff_until")
            self.conn.execute("ALTER TABLE application_tasks DROP COLUMN origin_ref")
            self.conn.execute("DELETE FROM schema_migrations WHERE name='0037_automation.sql'")
        ensure_product_schema(self.conn)
        for table, column in AUTOMATION_COLUMNS:
            self.assertTrue(schema._has_column(self.conn, table, column), f"{table}.{column} after the rerun")
        marker = self.conn.execute("SELECT 1 FROM schema_migrations WHERE name='0037_automation.sql'").fetchone()
        self.assertIsNotNone(marker)
        seeded = self.conn.execute("SELECT value, updated_at FROM user_settings WHERE user_id='student-2' AND key='automation_paused'").fetchone()
        self.conn.commit()
        self.assertEqual((seeded["value"], seeded["updated_at"]), ("off", schema.PAUSE_NEVER_CHANGED), "an existing student starts unpaused")
        self.assertTrue(automation.paused(self.conn, AUTOMATION_USER), "the seed never overwrites a student's pause")
        # Running the step itself again is harmless.
        schema._apply_automation(self.conn, (MIGRATIONS_DIR / "0037_automation.sql").read_text(encoding="utf-8"))
        self.conn.commit()

    def test_migration_0038_repairs_a_half_applied_upgrade_and_application_mail_runs(self):
        from opportunity_app import application_inbox

        for table, column in (("application_tasks", "link"), ("monitored_events", "decided_by")):
            self.assertTrue(schema._has_column(self.conn, table, column), f"{table}.{column}")
        with self.conn:
            self.conn.execute("ALTER TABLE monitored_events DROP COLUMN decided_by")
            self.conn.execute("DELETE FROM schema_migrations WHERE name='0038_application_mail.sql'")
        ensure_product_schema(self.conn)
        self.assertTrue(schema._has_column(self.conn, "monitored_events", "decided_by"))
        with self.conn:
            self.conn.execute(
                "INSERT INTO user_settings(user_id, key, value, updated_at) VALUES(?, 'application_mail', 'on', ?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET value='on'",
                (AUTOMATION_USER, utc_now()),
            )
        row = automation.perform(
            self.conn, user_id=AUTOMATION_USER, feature="application_mail", action_type="application.deadline", subject_kind="application",
            subject_id="app-job-b", after={"deadline": {"deadline_on": "2026-10-10", "quote": "by October 10", "sender_domain": "lever.co",
                                                        "gmail_id": "pg-1", "received_at": utc_now()}},
            evidence={"gmail_id": "pg-1"}, summary="A deadline from an email", basis="rule:test", confidence=0.9,
            idempotency_key="gmail:pg-1:app-job-b:application.deadline", auto=True,
        )
        self.assertEqual(row["status"], "applied")
        # An email's actions are found by their key's prefix (LIKE with an escape, portable to both backends).
        self.assertEqual([action["id"] for action in application_inbox._message_actions(self.conn, AUTOMATION_USER, "pg-1")], [row["id"]])
        automation.undo(self.conn, row["id"], AUTOMATION_USER)
        remaining = self.conn.execute("SELECT COUNT(*) AS n FROM email_deadlines").fetchone()["n"]
        self.conn.commit()
        self.assertEqual(remaining, 0)

    def test_a_job_email_reopens_only_the_automatic_archive(self):
        from opportunity_app import application_inbox, internal_automation

        with self.conn:
            for key in ("application_mail", "archive_silent_applications"):
                self.conn.execute(
                    "INSERT INTO user_settings(user_id, key, value, updated_at) VALUES(?, ?, 'on', ?) "
                    "ON CONFLICT(user_id, key) DO UPDATE SET value='on'",
                    (AUTOMATION_USER, key, utc_now()),
                )
        email = {"feature": "application_mail", "action_type": "application.stage", "subject_kind": "application",
                 "subject_id": "app-job-b", "evidence": {"gmail_id": "pg-2"}, "summary": "From an email", "basis": "rule:test",
                 "confidence": 0.9, "auto": True}
        proposal = automation.perform(self.conn, user_id=AUTOMATION_USER, **{**email, "auto": False}, after={"stage": "interview"},
                                      idempotency_key="gmail:pg-2:app-job-b:application.stage")
        self.assertTrue(internal_automation.pending_email_news(self.conn, AUTOMATION_USER, "app-job-b"))
        self.conn.commit()
        automation.reject(self.conn, proposal["id"], AUTOMATION_USER)
        archive = automation.perform(
            self.conn, user_id=AUTOMATION_USER, feature="archive_silent_applications", action_type="application.stage",
            subject_kind="application", subject_id="app-job-b", after={"stage": "archived", "only_from": "applied"}, evidence={},
            summary="Archived", basis="silence:60d", confidence=None, idempotency_key="archive-silent:pg", auto=True,
            guard=lambda conn, _before: not internal_automation.pending_email_news(conn, AUTOMATION_USER, "app-job-b"),
        )
        self.assertEqual(archive["status"], "applied")
        self.assertEqual(internal_automation.automatic_archive(self.conn, "app-job-b")["from_stage"], "applied")
        self.conn.commit()
        reopened = automation.perform(self.conn, user_id=AUTOMATION_USER, **email, after={"stage": "interview"},
                                      idempotency_key="gmail:pg-3:app-job-b:application.stage",
                                      guard=application_inbox._reopen_guard("app-job-b"))
        self.assertEqual((reopened["status"], self.application("app-job-b")[0]), ("applied", "interview"))
        self.assertFalse(internal_automation.automation_archived(self.conn, "app-job-b"))
        self.conn.commit()
        # The student archives it: an email's reopen is refused inside the transaction.
        from opportunity_app.actions import update_application

        update_application(self.conn, "app-job-b", stage="archived", user_id=AUTOMATION_USER)
        refused = automation.perform(self.conn, user_id=AUTOMATION_USER, **email, after={"stage": "rejected"},
                                     idempotency_key="gmail:pg-4:app-job-b:application.stage",
                                     guard=application_inbox._reopen_guard("app-job-b"))
        self.assertIsNone(refused)
        self.assertEqual(self.application("app-job-b")[0], "archived")

    def test_perform_undo_and_superseded_on_an_application(self):
        automation.set_mode(self.conn, AUTOMATION_USER, AUTOMATION_SWITCH.key, "on")
        row = self.stage_change("app-job-b", "interview", key="pg-1")
        self.assertEqual((row["status"], row["before"]["stage"], row["decided_by"]), ("applied", "applied", "system"))
        self.assertEqual(self.application("app-job-b")[0], "interview")
        undone = automation.undo(self.conn, row["id"], AUTOMATION_USER)
        self.assertEqual((undone["status"], undone["feature_paused"], undone["breaker_notice"]), ("undone", False, None))
        self.assertEqual(self.application("app-job-b")[0], "applied")
        again = self.stage_change("app-job-b", "interview", key="pg-2")
        update_application(self.conn, "app-job-b", stage="offer", user_id=AUTOMATION_USER)
        with self.assertRaises(automation.Superseded):
            automation.undo(self.conn, again["id"], AUTOMATION_USER)
        self.assertEqual(self.application("app-job-b")[0], "offer")
        self.assertEqual(automation.list_actions(self.conn, AUTOMATION_USER, status="superseded")[0]["id"], again["id"])
        # A NULL applied date is compared as equal to NULL (IS NOT DISTINCT FROM), so this undo is not refused.
        applying = record_intent(self.conn, "job-a", "apply_opened", user_id=AUTOMATION_USER)["application_id"]
        self.assertEqual(self.application(applying), ("applying", None))
        moved = self.stage_change(applying, "interview", key="pg-3")
        self.assertEqual(self.application(applying), ("interview", None))
        self.assertEqual(automation.undo(self.conn, moved["id"], AUTOMATION_USER)["status"], "undone")
        self.assertEqual(self.application(applying), ("applying", None))

    def test_the_ledger_row_and_the_change_roll_back_together(self):
        automation.set_mode(self.conn, AUTOMATION_USER, AUTOMATION_SWITCH.key, "on")
        before = self.application("app-job-b")
        events = self.conn.execute("SELECT COUNT(*) AS n FROM application_events WHERE application_id='app-job-b'").fetchone()["n"]
        seen = {}

        def fail(conn, values):
            seen["during"] = conn.execute("SELECT stage FROM applications WHERE id='app-job-b'").fetchone()["stage"]
            raise RuntimeError("disk full")

        with mock.patch.object(automation, "_insert_action", fail), self.assertRaisesRegex(RuntimeError, "disk full"):
            self.stage_change("app-job-b", "interview", key="pg-rollback")
        self.assertEqual(seen["during"], "interview", "the instrument saw the change inside the transaction")
        self.assertEqual(self.application("app-job-b"), before)
        after = self.conn.execute("SELECT COUNT(*) AS n FROM application_events WHERE application_id='app-job-b'").fetchone()["n"]
        self.assertEqual(after, events)
        self.assertEqual(automation.list_actions(self.conn, AUTOMATION_USER), [])

    def test_pause_guard_and_the_hand_over_see_the_pause(self):
        with self.conn:
            self.assertFalse(automation.pause_guard(self.conn, AUTOMATION_USER))
        automation.set_paused(self.conn, AUTOMATION_USER, True)
        with self.conn:
            self.assertTrue(automation.pause_guard(self.conn, AUTOMATION_USER))
        self.outreach_target("t-1", "Bovi")
        now = utc_now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO outreach_scheduled_sends(target_id, user_id, kind, fingerprint, send_at, timezone, label, state, created_at, updated_at) "
                "VALUES('t-1', ?, 'initial', 'f', ?, 'UTC', 'Mon, Sep 28, 9:12 AM CDT', 'sending', ?, ?)",
                (AUTOMATION_USER, now, now, now),
            )
        row = self.conn.execute("SELECT * FROM outreach_scheduled_sends WHERE target_id='t-1'").fetchone()
        self.conn.commit()
        self.assertEqual(outreach_schedule._hand_over(self.conn, row), "paused")
        stored = self.conn.execute("SELECT state, attempts, error FROM outreach_scheduled_sends WHERE target_id='t-1'").fetchone()
        self.conn.commit()
        self.assertEqual((stored["state"], stored["attempts"], stored["error"]), ("scheduled", 0, outreach_schedule.PAUSED_NOTE))
        automation.set_paused(self.conn, AUTOMATION_USER, False)
        with self.conn:
            self.conn.execute("UPDATE outreach_scheduled_sends SET state='sending' WHERE target_id='t-1'")
        self.assertEqual(outreach_schedule._hand_over(self.conn, row), "handed_over")
        [flight] = automation.set_paused(self.conn, AUTOMATION_USER, True)["in_flight"]
        self.assertEqual((flight["source"], flight["company"], flight["action"]), ("scheduled_send", "Bovi", "send"))

    def test_a_pause_waits_for_a_hand_over_that_holds_the_pause_row(self):
        import psycopg

        # The hand-over's transaction has taken the pause row (FOR SHARE) and not yet committed.
        automation.pause_guard(self.conn, AUTOMATION_USER)
        other = self.other_connection(lock_timeout_ms=200)
        with self.assertRaises(psycopg.errors.LockNotAvailable):
            automation.set_paused(other, AUTOMATION_USER, True)
        self.conn.commit()
        self.assertTrue(automation.set_paused(other, AUTOMATION_USER, True)["paused"], "once the hand-over commits, the pause lands")

    def test_in_flight_lists_sends_and_forms_being_clicked_but_not_drafts(self):
        old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(timespec="microseconds")
        for number, company in enumerate(("Bovi", "Kiva", "Orbit", "Acme"), start=1):
            self.outreach_target(f"t-{number}", company)
        self.claim("t-1", "sending", "send")
        self.claim("t-2", "clicking", "form")
        self.claim("t-3", "drafting", "draft")
        self.claim("t-4", "sending", "send", old)
        flights = automation.in_flight(self.conn, AUTOMATION_USER)
        self.conn.commit()
        self.assertEqual([(item["source"], item["company"], item["action"]) for item in flights],
                         [("send_claim", "Bovi", "send"), ("form_claim", "Kiva", "form")])

    def test_health_summary(self):
        automation.set_mode(self.conn, AUTOMATION_USER, AUTOMATION_SWITCH.key, "on")
        rows = [self.stage_change("app-job-b", stage, key=f"pg-h-{stage}") for stage in ("interview", "offer")]
        automation.undo(self.conn, rows[1]["id"], AUTOMATION_USER)
        self.assertTrue(automation.undo(self.conn, rows[0]["id"], AUTOMATION_USER)["feature_paused"])
        self.outreach_target("t-1", "Bovi")
        self.claim("t-1", "unconfirmed", "send")
        automation.record_health(self.conn, AUTOMATION_USER, "inbox.replies", ok=True, detail={"read": 2})
        stamp = utc_now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO connector_accounts(id, user_id, provider, status, token_granted_at, created_at, updated_at) "
                "VALUES('connector-gmail', ?, 'gmail_drafts', 'connected', '2026-09-20T12:00:00+00:00', ?, ?)",
                (AUTOMATION_USER, stamp, stamp),
            )
        automation.set_paused(self.conn, AUTOMATION_USER, True)
        with mock.patch.dict("os.environ", {"PIPELINE_TIMEZONE": "America/Chicago", "PIPELINE_GMAIL_TOKEN_DAYS": "7"}):
            summary = automation.health_summary(self.conn, AUTOMATION_USER, now=datetime(2026, 9, 27, 0, 0, tzinfo=timezone.utc))
        self.conn.commit()
        self.assertEqual([item["key"] for item in summary["banner"]], ["paused", "gmail_expiring"])
        self.assertEqual(summary["banner"][0]["text"], automation.PAUSED_BANNER)
        self.assertEqual([(item["target_id"], item["company"]) for item in summary["unconfirmed"]], [("t-1", "Bovi")])
        self.assertEqual([item["feature"] for item in summary["breaker_off"]], [AUTOMATION_SWITCH.key])
        self.assertEqual(summary["counts"], {"proposed": 0, "shadow_unreviewed": 0, "applied_last_24h": 2})
        self.assertEqual(summary["unread_notices"], 1)
        [component] = summary["components"]
        self.assertEqual((component["component"], component["detail"]), ("inbox.replies", {"read": 2}))
