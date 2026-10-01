"""Hosted database contract; skipped unless a disposable PostgreSQL URL is supplied."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR, apply_runs, automation, outreach_schedule, schema
from opportunity_app.actions import record_intent, update_application
from opportunity_app.api import create_app
from opportunity_app.automation import Feature
from opportunity_app.schema import MIGRATIONS_DIR, connect_product, ensure_product_schema, migrate_legacy_database
from opportunity_app.timestamps import utc_now
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
# The columns 0041 adds to outreach_inbox_messages, each by a guarded Python step, and its index.
REPLY_RULES_COLUMNS = ("via", "rules", "candidates_json", "thread_id", "message_id", "from_name", "subject", "text", "reason", "in_spam",
                       "decided_at", "meta_json")
REPLY_RULES_INDEX = "idx_outreach_inbox_messages_target"
# The columns 0043 adds (the reply label, and the account a Gmail connection signed into), and its index.
GMAIL_REPLY_LABELS_MIGRATION = "0043_gmail_reply_labels.sql"
GMAIL_REPLY_LABELS_COLUMNS = (
    ("outreach_inbox_messages", "label_name"), ("outreach_inbox_messages", "labeled_at"),
    ("outreach_inbox_messages", "label_note"), ("connector_accounts", "account_email"),
)
GMAIL_REPLY_LABELS_INDEX = "idx_outreach_inbox_messages_labels"
# The tables 0044 adds (the Gmail label on the outreach the student sent), plain SQL with no Python step.
SENT_LABELS_MIGRATION = "0044_outreach_sent_labels.sql"
SENT_LABELS_INDEX = "idx_outreach_label_threads_label"
SENT_LABELS_COLUMNS = {
    "outreach_label_threads": {
        "user_id": ("text", "NO"), "thread_id": ("text", "NO"), "target_id": ("text", "NO"), "source": ("text", "NO"),
        "label_name": ("text", "NO"), "labeled_at": ("text", "YES"), "label_note": ("text", "NO"), "found_at": ("text", "NO"),
    },
    "outreach_label_searches": {
        "user_id": ("text", "NO"), "target_id": ("text", "NO"), "searched_at": ("text", "NO"), "found": ("integer", "NO"), "query": ("text", "NO"),
    },
}
RECEIVED = "2026-09-25T15:00:00+00:00"


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

    def student(self, user_id):
        stamp = utc_now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO users(id, email, display_name, role, created_at, updated_at) VALUES(?, NULL, 'S', 'student', ?, ?)",
                (user_id, stamp, stamp),
            )

    def inbox_message(self, gmail_id, kind, *, user_id=AUTOMATION_USER, target_id="t-1", **columns):
        """A message outreach read. With no other columns, the row the old code wrote: its columns only, the rest defaulted."""
        values = {"user_id": user_id, "gmail_id": gmail_id, "target_id": target_id, "kind": kind, "sender": "dana@bovi.example",
                  "received_at": RECEIVED, "recorded_at": utc_now(), **columns}
        with self.conn:
            self.conn.execute(
                f"INSERT INTO outreach_inbox_messages({', '.join(values)}) VALUES({', '.join('?' * len(values))})", tuple(values.values()),
            )

    def inbox_row(self, gmail_id, user_id=AUTOMATION_USER):
        row = self.conn.execute("SELECT * FROM outreach_inbox_messages WHERE user_id=? AND gmail_id=?", (user_id, gmail_id)).fetchone()
        self.conn.commit()
        return None if row is None else dict(row)

    def index_definition(self, name):
        row = self.conn.execute("SELECT indexdef FROM pg_indexes WHERE schemaname=current_schema() AND indexname=?", (name,)).fetchone()
        self.conn.commit()
        return None if row is None else row["indexdef"]

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

    def test_a_job_email_restarts_the_silence_and_an_undone_archive_stays_undone(self):
        from datetime import datetime, timedelta, timezone

        from opportunity_app import internal_automation

        now = datetime.now(timezone.utc).replace(microsecond=0)
        with self.conn:
            for key in ("application_mail", "archive_silent_applications"):
                self.conn.execute(
                    "INSERT INTO user_settings(user_id, key, value, updated_at) VALUES(?, ?, 'on', ?) "
                    "ON CONFLICT(user_id, key) DO UPDATE SET value='on'",
                    (AUTOMATION_USER, key, utc_now()),
                )
            self.conn.execute("UPDATE applications SET stage='applied', applied_at=? WHERE id='app-job-b'",
                              ((now - timedelta(days=90)).isoformat(),))
            for gmail_id, days_ago in (("pg-heard", 70), ("pg-turned-down", 65)):
                self.conn.execute(
                    "INSERT INTO application_mail_messages(user_id, gmail_id, thread_id, application_id, kind, matched_by, state, "
                    "subject, sender_domain, received_at, recorded_at) VALUES(?, ?, 't', 'app-job-b', 'assessment', 'company_title', "
                    "'done', 'An update', 'hire.lever.co', ?, ?)",
                    (AUTOMATION_USER, gmail_id, (now - timedelta(days=days_ago)).isoformat(), utc_now()),
                )
        proposal = automation.perform(
            self.conn, user_id=AUTOMATION_USER, feature="application_mail", action_type="application.stage", subject_kind="application",
            subject_id="app-job-b", after={"stage": "interview"}, evidence={"gmail_id": "pg-turned-down"}, summary="From an email",
            basis="rule:test", confidence=0.9, idempotency_key="gmail:pg-turned-down:app-job-b:application.stage", auto=False,
        )
        automation.reject(self.conn, proposal["id"], AUTOMATION_USER)
        heard = internal_automation.last_heard(self.conn, AUTOMATION_USER)
        self.conn.commit()
        self.assertEqual(list(heard), ["app-job-b"])
        self.assertEqual(datetime.fromisoformat(heard["app-job-b"]), now - timedelta(days=70), "the email the student turned down is not a reply")
        [done] = internal_automation.archive_silent_applications(self.conn, AUTOMATION_USER, force=True)
        archive = internal_automation.automatic_archive(self.conn, "app-job-b")
        self.conn.commit()
        self.assertEqual(archive["action_id"], done["action_id"])
        self.assertTrue(archive["silent_since"])
        automation.undo(self.conn, done["action_id"], AUTOMATION_USER)
        self.assertEqual(internal_automation.archive_silent_applications(self.conn, AUTOMATION_USER, force=True), [])
        self.assertEqual(self.application("app-job-b")[0], "applied")

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

    def test_migration_0040_and_a_thank_you_scheduled_handed_over_and_settled(self):
        from opportunity_app import outreach, outreach_thank_you
        from opportunity_app.outreach_gmail import thank_you_fingerprint

        # The switch needs the address the student sends from (automation.REQUIREMENTS), as it does in every student's .env.
        sending = mock.patch.dict("os.environ", {"PIPELINE_OUTREACH_ACCOUNT": "student@school.example"})
        sending.start()
        self.addCleanup(sending.stop)
        self.assertTrue(schema._has_column(self.conn, "outreach_events", "detail_json"))
        with self.conn:
            self.conn.execute("ALTER TABLE outreach_events DROP COLUMN detail_json")
            self.conn.execute("DELETE FROM schema_migrations WHERE name='0040_decline_thank_you.sql'")
        ensure_product_schema(self.conn)
        self.assertTrue(schema._has_column(self.conn, "outreach_events", "detail_json"), "a half-applied 0040 is repaired")
        self.conn.commit()
        self.outreach_target("t-1", "Bovi")
        with self.conn:
            self.conn.execute("UPDATE outreach_targets SET status='replied', sent_at='2026-09-20', contact_email='greg@bovi.example', "
                              "location='Austin, TX' WHERE id='t-1'")
            outreach._log(self.conn, "t-1", AUTOMATION_USER, "reply_logged", detail="We're not hiring right now.",
                          data={"source": "gmail", "gmail_id": "g-1", "readings": {"rules": {"status": "declined"}}})
        stored = self.conn.execute("SELECT detail_json FROM outreach_events WHERE target_id='t-1' AND event_type='reply_logged'").fetchone()
        self.conn.commit()
        self.assertEqual(json.loads(stored["detail_json"])["readings"]["rules"]["status"], "declined")
        automation.set_mode(self.conn, AUTOMATION_USER, "jev_inbox_suggestions", "on")
        automation.set_mode(self.conn, AUTOMATION_USER, "decline_thank_you", "on")
        body = "Hi Greg,\n\nThank you for considering it.\n\nBest,\nTest Student"
        fingerprint = thank_you_fingerprint("greg@bovi.example", "Greg", "Re: Hello", body, "<g-1@bovi.example>", "thread-1")
        planned = {
            "reply_gmail_id": "g-1", "reply_message_id": "<g-1@bovi.example>", "thread_id": "thread-1", "to_email": "greg@bovi.example",
            "to_name": "Greg", "subject": "Re: Hello", "body": body, "generated_by": "template", "fingerprint": fingerprint,
            "send_at": utc_now(), "label": "Tue, Sep 29, 11:32 AM CDT (their time, from Austin, TX)", "timezone": "America/Chicago",
        }
        with self.conn:
            marker = automation.perform_in(
                self.conn, user_id=AUTOMATION_USER, feature="decline_thank_you", action_type="outreach.thank_you",
                subject_kind="outreach_target", subject_id="t-1", after={"thank_you": planned}, evidence={"reply_gmail_id": "g-1"},
                summary="Thank-you to Greg at Bovi scheduled for Tue 11:32 AM (their time)", basis="decline:rules+jev",
                confidence=0.9, idempotency_key="thank-you:t-1:g-1", auto=True,
            )
            status = automation.perform_in(
                self.conn, user_id=AUTOMATION_USER, feature="decline_thank_you", action_type="outreach.status",
                subject_kind="outreach_target", subject_id="t-1", after={"status": "declined", "only_from": "replied"},
                evidence={}, summary="Marked Bovi Declined", basis="decline:rules+jev", confidence=0.9,
                idempotency_key="thank-you-status:t-1:g-1", auto=True,
            )
        self.assertEqual((marker["status"], marker["undoable"], status["undoable"]), ("applied", False, True))
        card = outreach.get_target(self.conn, "t-1", user_id=AUTOMATION_USER)
        self.conn.commit()
        self.assertEqual((card["status"], card["thank_you"]["state"], card["thank_you"]["send_state"]), ("declined", "scheduled", "scheduled"))
        self.assertEqual(card["thank_you"]["label"], planned["label"])
        # A second one for the same company is never scheduled: the change is already in place.
        with self.conn:
            again = automation.perform_in(
                self.conn, user_id=AUTOMATION_USER, feature="decline_thank_you", action_type="outreach.thank_you",
                subject_kind="outreach_target", subject_id="t-1", after={"thank_you": planned}, evidence={},
                summary="again", basis="decline:rules+jev", confidence=0.9, idempotency_key="thank-you:t-1:g-2", auto=True,
            )
        self.assertIsNone(again)
        automation.undo(self.conn, status["id"], AUTOMATION_USER)
        with self.assertRaises(ValueError):
            automation.undo(self.conn, marker["id"], AUTOMATION_USER)
        with self.conn:
            self.conn.execute("UPDATE outreach_scheduled_sends SET state='sending' WHERE target_id='t-1' AND kind='thank_you'")
        row = self.conn.execute("SELECT * FROM outreach_scheduled_sends WHERE target_id='t-1' AND kind='thank_you'").fetchone()
        self.conn.commit()
        # 11:00 in Austin on a Tuesday: inside the thank-you's window, whenever this test runs.
        self.assertEqual(outreach_schedule._hand_over(self.conn, row, now=datetime(2026, 9, 29, 16, 0, tzinfo=timezone.utc)), "handed_over")
        state = self.conn.execute("SELECT state FROM outreach_thank_yous WHERE target_id='t-1'").fetchone()["state"]
        self.conn.commit()
        self.assertEqual(state, "transmitting")
        [flight] = automation.in_flight(self.conn, AUTOMATION_USER)
        self.conn.commit()
        self.assertEqual((flight["kind"], flight["action"]), ("thank_you", "send"))
        outreach_schedule._finish(self.conn, row, "failed", "Gmail did not confirm it (HTTP 503)")
        stored = self.conn.execute("SELECT state, note FROM outreach_thank_yous WHERE target_id='t-1'").fetchone()
        self.conn.commit()
        self.assertEqual((stored["state"], stored["note"]), ("failed", "Gmail did not confirm it (HTTP 503)"))
        self.assertEqual([notice["title"] for notice in automation.list_notices(self.conn, AUTOMATION_USER)],
                         ["Thank-you to Bovi stopped: it may have gone out, so check your Gmail Sent folder"])
        self.assertTrue(outreach_thank_you.cancel(self.conn, "t-1", user_id=AUTOMATION_USER, reason="You dismissed it"))
        stored = self.conn.execute("SELECT state FROM outreach_thank_yous WHERE target_id='t-1'").fetchone()
        self.conn.commit()
        self.assertEqual(stored["state"], "cancelled")

    # --- Replies from other addresses (migration 0041, outreach_inbox.py) -------------------------

    def test_migration_0041_applies_and_a_rerun_repairs_a_half_applied_upgrade(self):
        from opportunity_app import outreach_inbox

        self.assertEqual({column for _table, column, _definition in schema._OUTREACH_REPLY_RULES_COLUMNS}, set(REPLY_RULES_COLUMNS),
                         "the table as 0039 left it, below, lacks every column 0041 adds")
        for column in REPLY_RULES_COLUMNS:
            self.assertTrue(schema._has_column(self.conn, "outreach_inbox_messages", column), column)
        self.assertIn("(user_id, target_id, kind)", self.index_definition(REPLY_RULES_INDEX) or "")
        # The table as 0039 left it, holding what the old code read; then a crash after the first two
        # ALTERs and before the marker. SQLite keeps ALTERs a crash interrupts and PostgreSQL rolls them
        # back; the rerun must work either way.
        with self.conn:
            for column in REPLY_RULES_COLUMNS:
                self.conn.execute(f"ALTER TABLE outreach_inbox_messages DROP COLUMN {column}")
            self.conn.execute(f"DROP INDEX {REPLY_RULES_INDEX}")
            self.conn.execute("DELETE FROM schema_migrations WHERE name=?", (outreach_inbox.MIGRATION,))
        self.outreach_target("t-1", "Bovi")
        self.inbox_message("old-ignored", "ignored", target_id="")
        self.inbox_message("old-automatic", "automatic")
        self.inbox_message("old-reply", "reply")
        with self.conn:
            self.conn.execute("ALTER TABLE outreach_inbox_messages ADD COLUMN via TEXT NOT NULL DEFAULT ''")
            self.conn.execute("ALTER TABLE outreach_inbox_messages ADD COLUMN rules INTEGER NOT NULL DEFAULT 0")
        ensure_product_schema(self.conn)
        for column in REPLY_RULES_COLUMNS:
            self.assertTrue(schema._has_column(self.conn, "outreach_inbox_messages", column), f"{column} after the rerun")
        self.assertIn("(user_id, target_id, kind)", self.index_definition(REPLY_RULES_INDEX) or "")
        markers = self.conn.execute("SELECT COUNT(*) AS n FROM schema_migrations WHERE name=?", (outreach_inbox.MIGRATION,)).fetchone()["n"]
        types = {row["column_name"]: (row["data_type"], row["is_nullable"]) for row in self.conn.execute(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name='outreach_inbox_messages'",
        ).fetchall()}
        self.conn.commit()
        self.assertEqual(markers, 1)
        self.assertEqual(types["rules"], ("integer", "NO"), "rules < RULES compares numbers")
        self.assertEqual(types["in_spam"], ("integer", "NO"))
        self.assertEqual(types["decided_at"], ("text", "YES"), "undecided until the student says")
        # Nothing the old code read is lost, and each row reads as judged by the old rules (0).
        old = self.inbox_row("old-ignored")
        self.assertEqual({column: old[column] for column in REPLY_RULES_COLUMNS},
                         {"via": "", "rules": 0, "candidates_json": "[]", "thread_id": "", "message_id": "", "from_name": "",
                          "subject": "", "text": "", "reason": "", "in_spam": 0, "decided_at": None, "meta_json": "{}"})
        self.assertEqual((old["kind"], old["sender"], old["received_at"]), ("ignored", "dana@bovi.example", RECEIVED))
        self.assertEqual((self.inbox_row("old-automatic")["kind"], self.inbox_row("old-reply")["kind"]), ("automatic", "reply"))
        seen = {gmail_id: outreach_inbox._seen(self.conn, AUTOMATION_USER, gmail_id) for gmail_id in ("old-ignored", "old-automatic", "old-reply")}
        self.conn.commit()
        self.assertEqual(seen, {"old-ignored": False, "old-automatic": False, "old-reply": True},
                         "what the old rules set aside or took as automatic is read again")
        # Running the step itself again is harmless.
        schema._apply_outreach_reply_rules(self.conn, (MIGRATIONS_DIR / outreach_inbox.MIGRATION).read_text(encoding="utf-8"))
        self.conn.commit()

    def test_migration_0041_looks_for_its_columns_in_this_schema_only(self):
        """Another schema in the same database already has the columns (a copy, a second app): 0041 still adds them here.

        information_schema.columns lists every schema the user can see. Asked
        without table_schema=current_schema(), _has_column found the other
        copy's column and the ALTER here was skipped, so every write of the new
        columns failed.
        """
        import psycopg

        from opportunity_app import outreach_inbox

        copy = "reply_rules_copy"
        with psycopg.connect(POSTGRES_TEST_URL, autocommit=True) as admin:
            admin.execute(f"DROP SCHEMA IF EXISTS {copy} CASCADE")
            admin.execute(f"CREATE SCHEMA {copy}")
            admin.execute(f"CREATE TABLE {copy}.outreach_inbox_messages ({', '.join(f'{column} TEXT' for column in REPLY_RULES_COLUMNS)})")

        def drop_copy():
            with psycopg.connect(POSTGRES_TEST_URL, autocommit=True) as admin:
                admin.execute(f"DROP SCHEMA IF EXISTS {copy} CASCADE")

        self.addCleanup(drop_copy)
        with self.conn:
            for column in ("text", "decided_at"):
                self.conn.execute(f"ALTER TABLE outreach_inbox_messages DROP COLUMN {column}")
            self.conn.execute("DELETE FROM schema_migrations WHERE name=?", (outreach_inbox.MIGRATION,))
        self.assertFalse(schema._has_column(self.conn, "outreach_inbox_messages", "text"), "the other schema's column is not this one's")
        self.conn.commit()
        ensure_product_schema(self.conn)
        for column in REPLY_RULES_COLUMNS:
            self.assertTrue(schema._has_column(self.conn, "outreach_inbox_messages", column), column)
        self.conn.commit()
        self.outreach_target("t-1", "Bovi")
        with self.conn:
            self.assertTrue(outreach_inbox._remember(self.conn, AUTOMATION_USER, "careers-1", "t-1", outreach_inbox.POSSIBLE,
                                                     "careers@bovi.example", RECEIVED, via="domain", reason="shared_address",
                                                     text="Could you send your availability?"))
        self.assertEqual(self.inbox_row("careers-1")["text"], "Could you send your availability?")

    # --- The reply label (migration 0043, outreach_labels.py) and read-only connections -----------

    def test_migration_0043_applies_and_a_rerun_repairs_a_half_applied_upgrade(self):
        self.assertEqual({(table, column) for table, column, _definition in schema._GMAIL_REPLY_LABELS_COLUMNS},
                         set(GMAIL_REPLY_LABELS_COLUMNS), "the columns dropped below are every column 0043 adds")
        for table, column in GMAIL_REPLY_LABELS_COLUMNS:
            self.assertTrue(schema._has_column(self.conn, table, column), f"{table}.{column}")
        self.assertIn("(user_id, kind, label_name)", self.index_definition(GMAIL_REPLY_LABELS_INDEX) or "")
        # The tables as 0042 left them, holding what the old code read; then a crash after the first ALTER and
        # before the marker. SQLite keeps ALTERs a crash interrupts and PostgreSQL rolls them back; the rerun
        # must work either way.
        with self.conn:
            # The index first: PostgreSQL drops it along with its column, and a second DROP INDEX would then fail.
            self.conn.execute(f"DROP INDEX {GMAIL_REPLY_LABELS_INDEX}")
            for table, column in GMAIL_REPLY_LABELS_COLUMNS:
                self.conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
            self.conn.execute("DELETE FROM schema_migrations WHERE name=?", (GMAIL_REPLY_LABELS_MIGRATION,))
        self.outreach_target("t-1", "Bovi")
        self.inbox_message("old-reply", "reply")
        with self.conn:
            self.conn.execute("ALTER TABLE outreach_inbox_messages ADD COLUMN label_name TEXT NOT NULL DEFAULT ''")
        ensure_product_schema(self.conn)
        for table, column in GMAIL_REPLY_LABELS_COLUMNS:
            self.assertTrue(schema._has_column(self.conn, table, column), f"{table}.{column} after the rerun")
        self.assertIn("(user_id, kind, label_name)", self.index_definition(GMAIL_REPLY_LABELS_INDEX) or "")
        markers = self.conn.execute("SELECT COUNT(*) AS n FROM schema_migrations WHERE name=?", (GMAIL_REPLY_LABELS_MIGRATION,)).fetchone()["n"]
        types = {(row["table_name"], row["column_name"]): (row["data_type"], row["is_nullable"]) for row in self.conn.execute(
            "SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name IN ('outreach_inbox_messages', 'connector_accounts')",
        ).fetchall()}
        self.conn.commit()
        self.assertEqual(markers, 1)
        self.assertEqual(types[("outreach_inbox_messages", "label_name")], ("text", "NO"))
        self.assertEqual(types[("outreach_inbox_messages", "labeled_at")], ("text", "YES"), "unlabelled until the app adds it")
        self.assertEqual(types[("outreach_inbox_messages", "label_note")], ("text", "NO"), "'' until a row is settled without a label")
        self.assertEqual(types[("connector_accounts", "account_email")], ("text", "NO"))
        # A reply the old code logged is waiting to be labelled: no name, no time.
        old = self.inbox_row("old-reply")
        self.assertEqual((old["kind"], old["label_name"], old["labeled_at"], old["label_note"]), ("reply", "", None, ""))
        # Running the step itself again is harmless.
        schema._apply_gmail_reply_labels(self.conn, (MIGRATIONS_DIR / GMAIL_REPLY_LABELS_MIGRATION).read_text(encoding="utf-8"))
        self.conn.commit()

    def test_migration_0043_looks_for_its_columns_in_this_schema_only(self):
        """A copy of the tables in another schema already has the columns: 0043 still adds them here (see 0041's test)."""
        import psycopg

        copy = "gmail_labels_copy"
        with psycopg.connect(POSTGRES_TEST_URL, autocommit=True) as admin:
            admin.execute(f"DROP SCHEMA IF EXISTS {copy} CASCADE")
            admin.execute(f"CREATE SCHEMA {copy}")
            admin.execute(f"CREATE TABLE {copy}.outreach_inbox_messages (label_name TEXT, labeled_at TEXT, label_note TEXT)")
            admin.execute(f"CREATE TABLE {copy}.connector_accounts (account_email TEXT)")

        def drop_copy():
            with psycopg.connect(POSTGRES_TEST_URL, autocommit=True) as admin:
                admin.execute(f"DROP SCHEMA IF EXISTS {copy} CASCADE")

        self.addCleanup(drop_copy)
        with self.conn:
            self.conn.execute("ALTER TABLE connector_accounts DROP COLUMN account_email")
            self.conn.execute("DELETE FROM schema_migrations WHERE name=?", (GMAIL_REPLY_LABELS_MIGRATION,))
        self.assertFalse(schema._has_column(self.conn, "connector_accounts", "account_email"), "the other schema's column is not this one's")
        self.conn.commit()
        ensure_product_schema(self.conn)
        self.assertTrue(schema._has_column(self.conn, "connector_accounts", "account_email"))
        self.conn.commit()

    def test_migration_0044_adds_the_sent_label_tables_and_a_rerun_changes_nothing(self):
        def columns():
            found = {}
            for row in self.conn.execute(
                "SELECT table_name, column_name, data_type, is_nullable FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND table_name IN ('outreach_label_threads', 'outreach_label_searches')",
            ).fetchall():
                found.setdefault(row["table_name"], {})[row["column_name"]] = (row["data_type"], row["is_nullable"])
            self.conn.commit()
            return found

        self.assertEqual(columns(), SENT_LABELS_COLUMNS)
        self.assertIn("(user_id, label_name)", self.index_definition(SENT_LABELS_INDEX) or "")
        keys = {row["table_name"]: row["columns"] for row in self.conn.execute(
            "SELECT tc.table_name, string_agg(kcu.column_name, ',' ORDER BY kcu.ordinal_position) AS columns "
            "FROM information_schema.table_constraints tc JOIN information_schema.key_column_usage kcu "
            "ON kcu.constraint_name=tc.constraint_name AND kcu.table_schema=tc.table_schema "
            "WHERE tc.table_schema=current_schema() AND tc.constraint_type='PRIMARY KEY' "
            "AND tc.table_name IN ('outreach_label_threads', 'outreach_label_searches') GROUP BY tc.table_name",
        ).fetchall()}
        self.conn.commit()
        self.assertEqual(keys, {"outreach_label_threads": "user_id,thread_id", "outreach_label_searches": "user_id,target_id"})
        # No foreign key: a deleted company's threads keep their label.
        with self.conn:
            self.conn.execute(
                "INSERT INTO outreach_label_threads(user_id, thread_id, source, found_at) VALUES(?, 'th-1', 'sent', ?)",
                (AUTOMATION_USER, utc_now()),
            )
            self.conn.execute(
                "INSERT INTO outreach_label_searches(user_id, target_id, searched_at) VALUES(?, 'gone-target', ?)",
                (AUTOMATION_USER, utc_now()),
            )
        row = self.conn.execute("SELECT target_id, label_name, labeled_at, label_note FROM outreach_label_threads WHERE thread_id='th-1'").fetchone()
        found = self.conn.execute("SELECT found FROM outreach_label_searches WHERE target_id='gone-target'").fetchone()["found"]
        self.conn.commit()
        self.assertEqual((row["target_id"], row["label_name"], row["labeled_at"], row["label_note"]), ("", "", None, ""))
        self.assertEqual(found, 0)
        # Applying it again (the marker gone) keeps the tables and their rows.
        with self.conn:
            self.conn.execute("DELETE FROM schema_migrations WHERE name=?", (SENT_LABELS_MIGRATION,))
        ensure_product_schema(self.conn)
        self.assertEqual(columns(), SENT_LABELS_COLUMNS)
        kept = self.conn.execute("SELECT COUNT(*) AS n FROM outreach_label_threads").fetchone()["n"]
        markers = self.conn.execute("SELECT COUNT(*) AS n FROM schema_migrations WHERE name=?", (SENT_LABELS_MIGRATION,)).fetchone()["n"]
        self.conn.commit()
        self.assertEqual((kept, markers), (1, 1))

    def test_migration_0048_adds_three_indexes_and_a_rerun_changes_nothing(self):
        marker = "0048_mail_hot_path_indexes.sql"
        wanted = {
            "idx_outreach_events_user_type": ("outreach_events", "(user_id, event_type, created_at)"),
            "idx_outreach_events_target_type": ("outreach_events", "(target_id, user_id, event_type, created_at DESC)"),
            "idx_opportunities_company_sort_key": ("opportunities", "(company_sort_key)"),
        }
        for name, (table, columns) in wanted.items():
            definition = self.index_definition(name) or ""
            self.assertRegex(definition, rf"ON \S*{table} USING", name)
            self.assertIn(columns, definition, name)
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM schema_migrations WHERE name=?", (marker,)).fetchone())
        rows = self.conn.execute("SELECT COUNT(*) AS n FROM outreach_events").fetchone()["n"]
        self.conn.commit()
        # Applying it again (the marker gone) keeps all the indexes and every row.
        with self.conn:
            self.conn.execute("DELETE FROM schema_migrations WHERE name=?", (marker,))
        ensure_product_schema(self.conn)
        for name, (_table, columns) in wanted.items():
            self.assertIn(columns, self.index_definition(name) or "", name)
        kept = self.conn.execute("SELECT COUNT(*) AS n FROM outreach_events").fetchone()["n"]
        markers = self.conn.execute("SELECT COUNT(*) AS n FROM schema_migrations WHERE name=?", (marker,)).fetchone()["n"]
        self.conn.commit()
        self.assertEqual((kept, markers), (rows, 1))

    def test_a_read_only_connection_stays_read_only_after_a_commit(self):
        """connect_product(read_only=True): SET TRANSACTION covers only the first transaction, so the connection is read-only too."""
        conn = connect_product(POSTGRES_TEST_URL, read_only=True)
        self.addCleanup(conn.close)
        self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM schema_migrations").fetchone()["n"] > 0, True)
        conn.commit()
        for attempt in ("in the transaction after a commit", "in the one after that"):
            with self.subTest(attempt):
                with self.assertRaises(Exception) as caught:
                    conn.execute("INSERT INTO schema_migrations(name, applied_at) VALUES('read-only-check', ?)", (utc_now(),))
                self.assertIn("read-only", str(caught.exception).lower())
                conn.rollback()
        self.assertIsNone(self.conn.execute("SELECT 1 FROM schema_migrations WHERE name='read-only-check'").fetchone())
        self.conn.commit()

    def test_remember_takes_the_place_only_of_a_row_older_rules_set_aside_or_took_as_automatic(self):
        """outreach_inbox._remember's INSERT ... ON CONFLICT DO UPDATE ... WHERE kind IN ('ignored', 'automatic') AND rules < RULES.

        On PostgreSQL: the old code's verdict on an email a person may have
        written is replaced once, returning True; a verdict under these rules,
        or a reply the old code logged, never is, returning False.
        """
        from opportunity_app import outreach_inbox as inbox

        self.outreach_target("t-1", "Bovi")
        self.student("student-2")
        self.inbox_message("careers-1", "ignored", target_id="")  # the old code set it aside: rules 0
        self.inbox_message("careers-1", "ignored", target_id="", user_id="student-2")
        self.inbox_message("rules-1", "ignored", target_id="", rules=1)
        self.inbox_message("old-automatic", "automatic")  # the old code took a sales tool's email as automatic
        self.inbox_message("old-reply", "reply")
        self.inbox_message("news-1", "ignored", target_id="", via="domain", reason="list", rules=inbox.RULES)
        self.inbox_message("away-1", "automatic", via="thread", reason="out_of_office", rules=inbox.RULES)
        with self.conn:
            took = inbox._remember(
                self.conn, AUTOMATION_USER, "careers-1", "t-1", inbox.POSSIBLE, "careers@bovi.example", RECEIVED, via="domain",
                reason="spam", thread_id="th-1", subject="Next steps", text="Could you send your availability?",
                candidates=["t-2"], message_id="<m-1@bovi.example>", from_name="Bovi Careers", in_spam=True,
            )
        self.assertTrue(took, "a row the old rules set aside is judged again and replaced")
        row = self.inbox_row("careers-1")
        self.assertEqual(
            {key: row[key] for key in ("target_id", "kind", "sender", "received_at", "via", "rules", "reason", "thread_id", "subject",
                                       "text", "candidates_json", "message_id", "from_name", "in_spam", "decided_at")},
            {"target_id": "t-1", "kind": "possible", "sender": "careers@bovi.example", "received_at": RECEIVED, "via": "domain",
             "rules": inbox.RULES, "reason": "spam", "thread_id": "th-1", "subject": "Next steps",
             "text": "Could you send your availability?", "candidates_json": '["t-2"]', "message_id": "<m-1@bovi.example>",
             "from_name": "Bovi Careers", "in_spam": 1, "decided_at": None},
        )
        # Judged under these rules now: a second look at it records nothing and changes nothing.
        with self.conn:
            again = inbox._remember(self.conn, AUTOMATION_USER, "careers-1", "", inbox.IGNORED, "careers@bovi.example", RECEIVED,
                                    reason="no_company")
        self.assertFalse(again)
        self.assertEqual(self.inbox_row("careers-1"), row)
        with self.conn:
            outcomes = {
                "rules-1": inbox._remember(self.conn, AUTOMATION_USER, "rules-1", "", inbox.IGNORED, "news@bovi.example", RECEIVED,
                                           via="domain", reason="list", subject="This week at Bovi", message_id="<m-3@bovi.example>"),
                "old-automatic": inbox._remember(self.conn, AUTOMATION_USER, "old-automatic", "t-1", inbox.POSSIBLE, "lee@bovi.example",
                                                 RECEIVED, via="domain", reason="mailing_tool", text="Would you have 15 minutes?"),
                "old-reply": inbox._remember(self.conn, AUTOMATION_USER, "old-reply", "", inbox.IGNORED, "dana@bovi.example", RECEIVED,
                                             reason="no_company"),
                "fresh": inbox._remember(self.conn, AUTOMATION_USER, "fresh", "t-1", inbox.REPLY, "dana@bovi.example", RECEIVED,
                                         via="address", reason="written_to", subject="Re: Hello", text="Sure, let's talk.",
                                         message_id="<m-2@bovi.example>", from_name="Dana"),
            }
        current = {gmail_id: self.inbox_row(gmail_id) for gmail_id in ("news-1", "away-1")}
        with self.conn:
            outcomes["fresh, again"] = inbox._remember(self.conn, AUTOMATION_USER, "fresh", "", inbox.IGNORED, "dana@bovi.example",
                                                       RECEIVED, reason="no_company")
            # Set aside, or taken as automatic, under these rules, whether just now or earlier: never judged again.
            outcomes["rules-1, again"] = inbox._remember(self.conn, AUTOMATION_USER, "rules-1", "t-1", inbox.POSSIBLE, "news@bovi.example",
                                                         RECEIVED, via="domain", reason="mailing_tool", text="This week at Bovi")
            outcomes["news-1"] = inbox._remember(self.conn, AUTOMATION_USER, "news-1", "t-1", inbox.REPLY, "news@bovi.example", RECEIVED,
                                                 via="address", reason="written_to", subject="Hello")
            outcomes["away-1"] = inbox._remember(self.conn, AUTOMATION_USER, "away-1", "t-1", inbox.REPLY, "dana@bovi.example", RECEIVED,
                                                 via="thread", reason="thread", subject="Re: Hello")
        self.assertEqual(outcomes, {"rules-1": True, "old-automatic": True, "old-reply": False, "fresh": True, "fresh, again": False,
                                    "rules-1, again": False, "news-1": False, "away-1": False})
        self.assertEqual({gmail_id: self.inbox_row(gmail_id) for gmail_id in current}, current)
        set_aside = self.inbox_row("rules-1")
        self.assertEqual((set_aside["kind"], set_aside["rules"], set_aside["reason"], set_aside["subject"], set_aside["message_id"]),
                         ("ignored", inbox.RULES, "list", "", ""), "a set-aside email keeps no subject or Message-ID")
        rejudged = self.inbox_row("old-automatic")
        self.assertEqual((rejudged["kind"], rejudged["rules"], rejudged["reason"], rejudged["text"]),
                         ("possible", inbox.RULES, "mailing_tool", "Would you have 15 minutes?"))
        self.assertEqual(self.inbox_row("old-reply")["kind"], "reply", "a reply the old code logged is never replaced")
        fresh = self.inbox_row("fresh")
        self.assertEqual((fresh["kind"], fresh["subject"], fresh["message_id"], fresh["from_name"], fresh["text"], fresh["in_spam"]),
                         ("reply", "Re: Hello", "<m-2@bovi.example>", "Dana", "", 0), "a reply's words are kept in its history, not here")
        theirs = self.inbox_row("careers-1", "student-2")
        self.assertEqual((theirs["kind"], theirs["rules"]), ("ignored", 0), "another student's email with the same id is theirs")
        seen = {gmail_id: inbox._seen(self.conn, AUTOMATION_USER, gmail_id)
                for gmail_id in ("careers-1", "rules-1", "old-automatic", "news-1", "away-1", "old-reply", "fresh", "unread")}
        seen["student-2's"] = inbox._seen(self.conn, "student-2", "careers-1")
        self.conn.commit()
        self.assertEqual(seen, {"careers-1": True, "rules-1": True, "old-automatic": True, "news-1": True, "away-1": True,
                                "old-reply": True, "fresh": True, "unread": False, "student-2's": False})

    def test_two_checks_judging_one_old_row_record_it_once(self):
        """The second check waits on the row the first is replacing, then finds it judged and records nothing.

        PostgreSQL re-reads the row once the first commits and re-tests the
        DO UPDATE's WHERE on it, so the second check's verdict never
        overwrites the first's.
        """
        import psycopg

        from opportunity_app import outreach_inbox as inbox

        self.outreach_target("t-1", "Bovi")
        self.inbox_message("careers-1", "ignored", target_id="")
        other = self.other_connection()
        other_pid = other.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
        other.commit()
        self.assertTrue(inbox._remember(self.conn, AUTOMATION_USER, "careers-1", "t-1", inbox.POSSIBLE, "careers@bovi.example", RECEIVED,
                                        via="domain", reason="shared_address", text="Could you send your availability?"))
        second = {}

        def second_check():
            try:
                with other:
                    second["took"] = inbox._remember(other, AUTOMATION_USER, "careers-1", "", inbox.IGNORED, "careers@bovi.example",
                                                     RECEIVED, reason="no_company")
            except Exception as exc:  # reported below rather than lost in the thread
                second["error"] = exc

        thread = threading.Thread(target=second_check, daemon=True)
        thread.start()
        waited = False
        try:
            # The instrument: the second check is waiting on the row, not merely not started yet.
            with psycopg.connect(POSTGRES_TEST_URL, autocommit=True) as watcher:
                deadline = time.monotonic() + 10
                while not waited and time.monotonic() < deadline:
                    waited = watcher.execute(
                        "SELECT 1 FROM pg_stat_activity WHERE pid=%s AND wait_event_type='Lock'", (other_pid,),
                    ).fetchone() is not None
                    if not waited:
                        time.sleep(0.02)
        finally:
            self.conn.commit()  # the first check's transaction ends whatever happened, so the thread can finish
            thread.join(10)
        self.assertTrue(waited, "the second check never waited on the row")
        self.assertFalse(thread.is_alive())
        self.assertEqual(second, {"took": False}, "the second check found the row already judged")
        row = self.inbox_row("careers-1")
        self.assertEqual((row["kind"], row["target_id"], row["reason"], row["rules"], row["text"]),
                         ("possible", "t-1", "shared_address", inbox.RULES, "Could you send your availability?"))

    def test_the_job_mail_reader_leaves_to_outreach_only_what_outreach_holds(self):
        """application_inbox._outreach_owns and _reclaim on PostgreSQL, through outreach_inbox.owned_sql (plain and aliased)."""
        from opportunity_app import application_inbox

        self.outreach_target("t-1", "Bovi")
        self.student("student-2")
        # gmail_id: (kind, via, reason, rules), and whether outreach holds it.
        rows = {
            "in-thread": (("reply", "thread", "thread", 2), True),
            "written-to": (("reply", "address", "written_to", 2), True),
            "out-of-office": (("automatic", "thread", "out_of_office", 2), True),
            "may-be": (("possible", "domain", "shared_address", 2), True),
            "job-mail": (("possible", "domain", "job_mail", 2), False),
            "domain-reply": (("reply", "domain", "domain_person", 2), False),
            "domain-auto": (("automatic", "domain", "out_of_office", 2), False),
            "confirmed-name": (("reply", "name", "mentions_company", 2), False),
            "set-aside": (("ignored", "domain", "automated_sender", 2), False),
            "dismissed": (("dismissed", "domain", "shared_address", 2), False),
            "old-reply": (("reply", "", "", 0), True),
            "old-automatic": (("automatic", "", "", 0), False),
            "old-ignored": (("ignored", "", "", 0), False),
        }
        for gmail_id, ((kind, via, reason, rules), _held) in rows.items():
            self.inbox_message(gmail_id, kind, via=via, reason=reason, rules=rules)
        self.inbox_message("theirs", "reply", user_id="student-2", target_id="t-9", via="thread", reason="thread", rules=2)
        self.inbox_message("theirs-dismissed", "dismissed", user_id="student-2", target_id="t-9", via="domain", reason="shared_address", rules=2)
        owns = {gmail_id: application_inbox._outreach_owns(self.conn, AUTOMATION_USER, gmail_id) for gmail_id in [*rows, "theirs", "unread"]}
        owns["theirs, for them"] = application_inbox._outreach_owns(self.conn, "student-2", "theirs")
        self.conn.commit()
        self.assertEqual(owns, {**{gmail_id: held for gmail_id, (_row, held) in rows.items()},
                                "theirs": False, "unread": False, "theirs, for them": True})
        now = utc_now()
        with self.conn:
            for user_id, gmail_id in [*((AUTOMATION_USER, gmail_id) for gmail_id in rows), ("student-2", "theirs-dismissed")]:
                self.conn.execute(
                    "INSERT INTO application_mail_messages(user_id, gmail_id, state, recorded_at, received_at) VALUES(?, ?, 'outreach', ?, ?)",
                    (user_id, gmail_id, now, now),
                )
            self.conn.execute(
                "INSERT INTO application_mail_sync(user_id, history_id, pending_ids_json, enabled_at, updated_at) VALUES(?, 'h-1', '[]', 'e-1', ?)",
                (AUTOMATION_USER, now),
            )
            self.conn.execute(
                "INSERT INTO user_settings(user_id, key, value, updated_at) VALUES(?, ?, 'on', ?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET value='on'",
                (AUTOMATION_USER, application_inbox.FEATURE, now),
            )
        self.assertEqual(application_inbox._reclaim(self.conn, AUTOMATION_USER, expect="e-1"), 6,
                         "not the rows the old rules left, until they are read again")
        states = {(row["user_id"], row["gmail_id"]): (row["state"], row["origin"]) for row in self.conn.execute(
            "SELECT user_id, gmail_id, state, origin FROM application_mail_messages",
        ).fetchall()}
        self.conn.commit()
        reclaimed = ("job-mail", "domain-reply", "domain-auto", "confirmed-name", "set-aside", "dismissed")
        self.assertEqual({key: state for key, (state, _origin) in states.items() if state != "outreach"},
                         {(AUTOMATION_USER, gmail_id): "awaiting_resume" for gmail_id in reclaimed}, "decided again, as mail read while paused is")
        self.assertEqual({states[(AUTOMATION_USER, gmail_id)][1] for gmail_id in reclaimed}, {application_inbox.RECLAIMED},
                         "read late: only ever proposed")
        self.assertEqual({key for key, (state, _origin) in states.items() if state == "outreach"},
                         {(AUTOMATION_USER, gmail_id) for gmail_id in rows if gmail_id not in reclaimed} | {("student-2", "theirs-dismissed")},
                         "what outreach still holds, the old rules' rows, and another student's mail stay")
        self.assertEqual(application_inbox._reclaim(self.conn, AUTOMATION_USER, expect="e-1"), 0, "taken back once")

    def test_a_follow_up_is_not_handed_to_gmail_while_a_possible_reply_waits(self):
        """outreach_schedule._hand_over's follow-up check, inside its UPDATE, on PostgreSQL.

        A possible reply waiting on the company, or naming it among the others
        it could be from (candidates_json LIKE '%"<id>"%'), keeps the follow-up
        from Gmail: it is held until the next morning without counting a try.
        The id is matched whole, so t-10's possible reply never holds t-1's.
        """
        from opportunity_app import outreach_inbox as inbox

        now = utc_now()
        for target_id, company in (("t-1", "Bovi"), ("t-10", "Kiva"), ("t-3", "Orbit")):
            self.outreach_target(target_id, company)
        with self.conn:
            self.conn.execute("UPDATE outreach_targets SET status='sent', sent_at=? WHERE user_id=?", (now[:10], AUTOMATION_USER))
            for target_id in ("t-1", "t-10", "t-3"):
                self.conn.execute(
                    "INSERT INTO outreach_scheduled_sends(target_id, user_id, kind, fingerprint, send_at, timezone, label, state, created_at, "
                    "updated_at) VALUES(?, ?, 'follow_up', 'f', ?, 'UTC', 'Mon, Sep 28, 9:12 AM CDT', 'sending', ?, ?)",
                    (target_id, AUTOMATION_USER, now, now, now),
                )
            # From Orbit's domain or Kiva's, the student has not said which: it holds both.
            self.assertTrue(inbox._remember(self.conn, AUTOMATION_USER, "g-1", "t-3", inbox.POSSIBLE, "jobs@orbit.example", RECEIVED,
                                            via="domain", reason="ambiguous", candidates=["t-10"], text="Are you still interested?"))

        def hand_over(target_id):
            row = self.conn.execute("SELECT * FROM outreach_scheduled_sends WHERE target_id=? AND kind='follow_up'", (target_id,)).fetchone()
            self.conn.commit()
            return outreach_schedule._hand_over(self.conn, row)

        outcomes = {target_id: hand_over(target_id) for target_id in ("t-1", "t-10", "t-3")}
        stored = {row["target_id"]: (row["state"], row["attempts"], row["error"].split(" may have replied")[0]) for row in self.conn.execute(
            "SELECT target_id, state, attempts, error FROM outreach_scheduled_sends WHERE user_id=?", (AUTOMATION_USER,),
        ).fetchall()}
        held = [row["target_id"] for row in self.conn.execute(
            "SELECT target_id FROM outreach_events WHERE user_id=? AND event_type='follow_up_held' ORDER BY target_id", (AUTOMATION_USER,),
        ).fetchall()]
        self.conn.commit()
        self.assertEqual(outcomes, {"t-1": "handed_over", "t-10": "held", "t-3": "held"})
        self.assertEqual(stored, {"t-1": ("transmitting", 0, ""), "t-10": ("scheduled", 0, "Held: Kiva"), "t-3": ("scheduled", 0, "Held: Orbit")})
        self.assertEqual(held, ["t-10", "t-3"])
        # Once the student says it is not a reply, the follow-up may go.
        inbox.decide_possible_reply(self.conn, "t-3", "g-1", "not_reply", user_id=AUTOMATION_USER)
        with self.conn:
            self.conn.execute("UPDATE outreach_scheduled_sends SET state='sending' WHERE user_id=? AND target_id IN ('t-10', 't-3')",
                              (AUTOMATION_USER,))
        self.assertEqual({target_id: hand_over(target_id) for target_id in ("t-10", "t-3")}, {"t-10": "handed_over", "t-3": "handed_over"})


# The tables, indexes and columns 0045 adds; the columns by a guarded Python step, as the earlier migrations do.
APPLY_TABLES = ("application_submit_claims", "apply_runs", "apply_sensitive_answers", "apply_ats_labels")
APPLY_COLUMNS = (("application_mail_messages", "sender_verified"),
                 ("generated_document_artifacts", "content_sha256"),
                 ("apply_sensitive_answers", "company_name"))  # 0046
APPLY_LOCKS = ("ux_submit_claims_live_application", "ux_submit_claims_live_job")


@unittest.skipUnless(POSTGRES_TEST_URL, "POSTGRES_TEST_URL is not configured")
class PostgresApplyContractTests(unittest.TestCase):
    """Apply for me's claims, their two partial unique indexes, the hand-over and recovery on PostgreSQL (migration 0045, apply_runs.py).

    The locks (a transaction that starts with an UPDATE of the student's users row, the partial unique
    indexes, FOR UPDATE on the claim) are what make one attempt per application and per job hold across
    threads and server processes; SQLite proves them only for its own single writer.
    """

    def setUp(self):
        import psycopg

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
        apply_runs.RUNNING.clear()
        self.addCleanup(apply_runs.RUNNING.clear)
        self.base = datetime.now(timezone.utc).replace(microsecond=0)

    def at(self, minutes=0):
        return self.base + timedelta(minutes=minutes)

    def opportunity(self, opportunity_id, title="Controls Intern", company="Bluefin Robotics"):
        stamp = utc_now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO opportunities(id, company, title, url, first_seen_at, last_seen_at, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (opportunity_id, company, title, f"https://boards.example.test/{opportunity_id}", stamp, stamp, stamp, stamp),
            )

    def claim(self, opportunity_id, mode="handoff", *, job="bluefin/1001", conn=None, now=None, company="Bluefin Robotics", board="bluefin", **kwargs):
        return apply_runs.claim(
            conn or self.conn, user_id=AUTOMATION_USER, opportunity_id=opportunity_id, mode=mode, ats="greenhouse", board_token=board,
            job_ref=job, company=apply_runs.company_key(company), now=now or self.at(1), **kwargs,
        )

    def state(self, token):
        row = self.conn.execute("SELECT state, after_click FROM application_submit_claims WHERE token=?", (token,)).fetchone()
        self.conn.commit()
        return row["state"], row["after_click"]

    def test_migration_0045_applies_and_a_rerun_repairs_a_half_applied_upgrade(self):
        for table in APPLY_TABLES:
            self.assertEqual(self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"], 0, table)
        for table, column in APPLY_COLUMNS:
            self.assertTrue(schema._has_column(self.conn, table, column), f"{table}.{column}")
        for name in APPLY_LOCKS:
            row = self.conn.execute("SELECT indexdef FROM pg_indexes WHERE schemaname=current_schema() AND indexname=?", (name,)).fetchone()
            self.conn.commit()
            self.assertIn("UNIQUE", row["indexdef"])
            self.assertIn("released", row["indexdef"], "partial: a released row locks nothing")
        # What a crash between the ALTERs and the marker leaves: a column gone, and no marker.
        with self.conn:
            self.conn.execute("ALTER TABLE application_mail_messages DROP COLUMN sender_verified")
            self.conn.execute("DELETE FROM schema_migrations WHERE name='0045_apply_agent.sql'")
        ensure_product_schema(self.conn)
        for table, column in APPLY_COLUMNS:
            self.assertTrue(schema._has_column(self.conn, table, column), f"{table}.{column} after the rerun")
        self.assertIsNotNone(self.conn.execute("SELECT 1 FROM schema_migrations WHERE name='0045_apply_agent.sql'").fetchone())
        schema._apply_apply_agent(self.conn, (MIGRATIONS_DIR / "0045_apply_agent.sql").read_text(encoding="utf-8"))
        self.conn.commit()

    def test_the_partial_unique_indexes_hold_one_live_attempt_per_application_and_per_job(self):
        import psycopg

        self.opportunity("job-1")
        self.opportunity("job-2")
        first = self.claim("job-1")
        row = self.conn.execute("SELECT * FROM application_submit_claims WHERE token=?", (first["token"],)).fetchone()
        self.conn.commit()
        with self.conn:
            self.conn.execute(
                "INSERT INTO applications(id, opportunity_id, user_id, stage, created_at, updated_at) VALUES('app-job-2', 'job-2', ?, 'applying', ?, ?)",
                (AUTOMATION_USER, utc_now(), utc_now()),
            )

        def insert(token, application_id, job_ref, state):
            with self.conn:
                self.conn.execute(
                    "INSERT INTO application_submit_claims(token, application_id, user_id, opportunity_id, instance, mode, state, ats, board_token, "
                    "job_ref, company_key, stage_policy, plan_hash, heartbeat_at, created_at, updated_at) "
                    "VALUES(?, ?, ?, 'x', 'i', 'handoff', ?, 'greenhouse', 'bluefin', ?, 'bluefin robotics', 'ask', '', 't', 't', 't')",
                    (token, application_id, AUTOMATION_USER, state, job_ref),
                )

        with self.assertRaises(psycopg.errors.UniqueViolation):
            insert("dup-application", row["application_id"], "bluefin/other", "claimed")
        with self.assertRaises(psycopg.errors.UniqueViolation):
            insert("dup-job", "app-job-2", row["job_ref"], "claimed")
        insert("tombstone", row["application_id"], row["job_ref"], "released")
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET state='submitted' WHERE token=?", (first["token"],))
        with self.assertRaises(psycopg.errors.UniqueViolation):
            insert("after-submit", "app-job-2", row["job_ref"], "claimed")

    def test_two_connections_claim_one_application_and_exactly_one_gets_it(self):
        self.opportunity("job-1")
        results = []
        barrier = threading.Barrier(2)

        def attempt(number):
            conn = connect_product(POSTGRES_TEST_URL)
            try:
                barrier.wait()
                try:
                    results.append(("claimed", self.claim("job-1", conn=conn, now=self.at(number))["token"]))
                except apply_runs.ClaimRefused as refusal:
                    results.append(("refused", str(refusal)))
            finally:
                conn.close()

        threads = [threading.Thread(target=attempt, args=(number,)) for number in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(kind for kind, _ in results), ["claimed", "refused"])
        self.assertEqual(dict(results)["refused"], apply_runs.LIVE_APPLICATION)
        count = self.conn.execute("SELECT COUNT(*) AS n FROM application_submit_claims").fetchone()["n"]
        self.conn.commit()
        self.assertEqual(count, 1)

    def test_the_job_lock_refuses_a_second_saved_copy_until_the_first_is_released(self):
        self.opportunity("copy-a", "Controls Intern")
        self.opportunity("copy-b", "Controls Intern (repost)")
        first = self.claim("copy-a")
        with self.assertRaises(apply_runs.ClaimRefused) as caught:
            self.claim("copy-b", now=self.at(2))
        self.assertEqual(caught.exception.code, "job")
        with self.conn:
            self.conn.execute("UPDATE application_submit_claims SET state='released' WHERE token=?", (first["token"],))
        self.assertEqual(self.state(self.claim("copy-b", now=self.at(3))["token"]), ("claimed", 0))

    def test_the_hand_over_takes_the_pause_row_and_the_claim_and_a_pause_after_it_reports_the_application(self):
        self.opportunity("job-1")
        # Another company: the company limit (30 days) would refuse a second Bluefin claim, and an unattended claim cannot tick past it.
        self.opportunity("job-2", "Controls Co-op", company="Cobalt Labs")
        token = self.claim("job-1")["token"]
        self.assertTrue(apply_runs.hand_over(self.conn, token, user_id=AUTOMATION_USER, now=self.at(2)))
        self.assertEqual(self.state(token), ("clicking", 1))
        self.assertFalse(apply_runs.hand_over(self.conn, token, user_id=AUTOMATION_USER, now=self.at(3)), "only a claimed attempt is handed over")
        unattended = self.claim("job-2", "unattended", job="cobalt/2002", company="Cobalt Labs", board="cobalt", now=self.at(60))["token"]
        result = automation.set_paused(self.conn, AUTOMATION_USER, True)
        self.assertEqual([(item["action"], item["source"]) for item in result["in_flight"]], [("application", "apply_claim")])
        self.assertFalse(apply_runs.hand_over(self.conn, unattended, user_id=AUTOMATION_USER, now=self.at(61)), "an unattended claim waits for the resume")
        self.assertEqual(self.state(unattended), ("claimed", 0))

    def test_a_pause_and_a_cancel_wait_for_a_hand_over_that_holds_the_locks(self):
        import psycopg

        self.opportunity("job-1")
        token = self.claim("job-1")["token"]
        other = connect_product(POSTGRES_TEST_URL)
        self.addCleanup(other.close)
        other.execute("SET lock_timeout = '200ms'")
        other.commit()
        # The hand-over's transaction has taken the pause row (FOR SHARE) and the student's row, and not yet committed.
        automation.pause_guard(self.conn, AUTOMATION_USER)
        apply_runs.lock_user(self.conn, AUTOMATION_USER)
        with self.assertRaises(psycopg.errors.LockNotAvailable):
            automation.set_paused(other, AUTOMATION_USER, True)
        with self.assertRaises(psycopg.errors.LockNotAvailable):
            apply_runs.request_cancel(other, token, user_id=AUTOMATION_USER)
        self.conn.rollback()
        self.assertTrue(apply_runs.request_cancel(other, token, user_id=AUTOMATION_USER), "once the transaction ends, the cancel lands")

    def test_a_claim_waits_on_the_students_lock_and_then_sees_what_landed_first(self):
        import psycopg

        # Only the lock can make this outcome: no claim exists yet, so the unique indexes have nothing to refuse.
        self.opportunity("job-1")
        record_intent(self.conn, "job-1", "apply_opened", user_id=AUTOMATION_USER)
        application_id = self.conn.execute("SELECT id FROM applications WHERE opportunity_id='job-1'").fetchone()["id"]
        self.conn.commit()
        other = connect_product(POSTGRES_TEST_URL)
        self.addCleanup(other.close)
        other.execute("SET lock_timeout = '200ms'")
        other.commit()
        apply_runs.lock_user(self.conn, AUTOMATION_USER)  # the student's own write holds their row and has not committed
        with self.assertRaises(psycopg.errors.LockNotAvailable):
            self.claim("job-1", conn=other)
        self.assertEqual(apply_runs.RUNNING, set(), "a claim that never started holds nothing")
        other.execute("SET lock_timeout = 0")
        other.commit()
        self.conn.execute("UPDATE applications SET stage='rejected' WHERE id=?", (application_id,))
        outcome = []

        def waiting():
            try:
                outcome.append(("claimed", self.claim("job-1", conn=other, now=self.at(2))["token"]))
            except apply_runs.ClaimRefused as refusal:
                outcome.append((refusal.code, str(refusal)))

        thread = threading.Thread(target=waiting)
        thread.start()
        time.sleep(0.5)
        self.assertEqual(outcome, [], "the claim is still waiting for the lock")
        self.conn.commit()
        thread.join(10)
        self.assertEqual(outcome, [("stage", "This application is already rejected")], "the waiting claim saw the stage that landed first")
        count = self.conn.execute("SELECT COUNT(*) AS n FROM application_submit_claims").fetchone()["n"]
        self.conn.commit()
        self.assertEqual(count, 0)

    def test_recovery_fails_a_stopped_claim_before_hand_over_and_leaves_a_clicking_one_unconfirmed(self):
        self.opportunity("job-1")
        self.opportunity("job-2", "Controls Co-op")
        before = self.claim("job-1")["token"]
        during = self.claim("job-2", job="bluefin/2002", now=self.at(2))["token"]
        apply_runs.hand_over(self.conn, during, user_id=AUTOMATION_USER, now=self.at(3))
        for token in (before, during):
            apply_runs.forget(token)
        counts = apply_runs.recover_stale(self.conn, self.at(10))
        self.assertEqual((counts["failed"], counts["unconfirmed"]), (1, 1))
        self.assertEqual(self.state(before), ("failed", 0))
        self.assertEqual(self.state(during), ("unconfirmed", 1))
        [item] = automation.unconfirmed(self.conn, AUTOMATION_USER, now=self.at(10))
        self.conn.commit()
        self.assertEqual((item["action"], item["company"]), ("application", "Bluefin Robotics"))
