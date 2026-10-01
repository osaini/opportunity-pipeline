"""last_heard reads only the ledger rows of the emails it is deciding about, and answers what a full ledger read did.

The ledger (automation_actions) grows with every job email ever read. last_heard used to load all of an
application_mail's rows on every Urgent read and inside every archive candidate's write transaction. With few
emails it now asks for each one's own key range on UNIQUE(user_id, idempotency_key); the parse of each key is still
the filter, so the answer is the same. The reference below is last_heard itself with the old full read swapped in.
"""

import random
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opportunity_app import internal_automation
from opportunity_app.database import connect_product
from opportunity_app.timestamps import utc_now

from helpers_platform import build_and_migrate

USER = "local-user"
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


def full_ledger_read(conn, user_id, _gmail_ids):
    """What last_heard read before: every application_mail row the student has."""
    return conn.execute(
        "SELECT idempotency_key, status, review FROM automation_actions WHERE user_id=? AND feature='application_mail'", (user_id,),
    ).fetchall()


# Gmail ids with characters that are special to LIKE (% and _) or to the key format (:), and one that differs
# from another only by case.
ODD_IDS = ("g%x", "g_y", "g:z", "G-UP", "g-up", "plain")


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        _, platform_path = build_and_migrate(Path(self.tmp.name))
        self.conn = connect_product(platform_path)
        self.addCleanup(self.conn.close)

    def seed(self, apps, per_app, noise, seed=7):
        rng = random.Random(seed)
        stamp = utc_now()
        kinds = ["interview", "rejected", "assessment", "application_confirmation", "newsletter", "offer"]
        with self.conn:
            self.conn.execute(
                "INSERT INTO user_settings(user_id, key, value, updated_at) VALUES(?, 'application_mail', 'on', ?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET value='on'", (USER, stamp),
            )
            for index in range(apps):
                application = f"app-{index}"
                for number in range(per_app):
                    gmail_id = f"{rng.choice(ODD_IDS)}-{index}-{number}" if rng.random() < 0.3 else f"g{index}-{number}"
                    self.conn.execute(
                        "INSERT INTO application_mail_messages(user_id, gmail_id, thread_id, application_id, kind, matched_by, state, "
                        "origin, subject, sender_domain, received_at, recorded_at) VALUES(?, ?, 't', ?, ?, 'x', ?, 'live', 's', 'd', ?, ?)",
                        (USER, gmail_id, application, rng.choice(kinds), rng.choice(["done", "done", "awaiting_resume", "skipped"]),
                         (NOW - timedelta(days=rng.randint(0, 100))).isoformat(), stamp),
                    )
                    for extra in range(rng.randint(0, 3)):
                        self.conn.execute(
                            "INSERT INTO automation_actions(id, user_id, feature, action_type, subject_kind, subject_id, status, "
                            "idempotency_key, review, created_at) VALUES(?, ?, 'application_mail', 'application.stage', 'application', ?, ?, ?, ?, ?)",
                            (f"a-{index}-{number}-{extra}", USER, application,
                             rng.choice(["applied", "rejected", "expired", "shadow", "proposed", "undone"]),
                             f"gmail:{gmail_id}:{application}:application.stage.{extra}", rng.choice(["", "", "wrong", "right"]), stamp),
                        )
            # Keys that are not gmail ones, malformed ones, and other emails' rows, which must never be counted.
            for number, key in enumerate(("archive-silent:app-0:x", "gmail:", "gmail:g0-0", "gmail", "other:g0-0:app-0:x")):
                self.conn.execute(
                    "INSERT INTO automation_actions(id, user_id, feature, action_type, subject_kind, subject_id, status, "
                    "idempotency_key, created_at) VALUES(?, ?, 'application_mail', 'application.stage', 'application', 'app-0', 'rejected', ?, ?)",
                    (f"junk-{number}", USER, key, stamp),
                )
            for number in range(noise):
                self.conn.execute(
                    "INSERT INTO automation_actions(id, user_id, feature, action_type, subject_kind, subject_id, status, "
                    "idempotency_key, created_at) VALUES(?, ?, 'application_mail', 'application.stage', 'application', 'app-x', 'rejected', ?, ?)",
                    (f"noise-{number}", USER, f"gmail:old{number}:app-x:application.stage", stamp),
                )

    def answers(self, apps):
        return {None: internal_automation.last_heard(self.conn, USER),
                **{f"app-{index}": internal_automation.last_heard(self.conn, USER, f"app-{index}") for index in range(apps)}}

    def reference(self, apps):
        with mock.patch.object(internal_automation, "_ledger_rows", full_ledger_read):
            return self.answers(apps)


class LastHeardParityTests(Case):
    def test_few_emails_answer_what_a_full_ledger_read_did(self):
        for seed in range(5):
            with self.subTest(seed=seed):
                self.setUp()
                self.seed(apps=12, per_app=2, noise=200, seed=seed)
                got = self.answers(12)
                self.assertTrue(got[None], "the data has replies to compare")
                self.assertEqual(got, self.reference(12))

    def test_many_emails_answer_what_a_full_ledger_read_did(self):
        self.seed(apps=60, per_app=2, noise=50)
        self.assertEqual(self.answers(60), self.reference(60))

    def test_turned_down_emails_are_still_left_out(self):
        self.seed(apps=3, per_app=1, noise=0)
        self.conn.execute("DELETE FROM automation_actions")
        self.conn.execute("UPDATE application_mail_messages SET kind='interview', state='done', received_at='2026-09-01T00:00:00+00:00'")
        row = self.conn.execute("SELECT gmail_id, application_id FROM application_mail_messages ORDER BY gmail_id LIMIT 1").fetchone()
        before = internal_automation.last_heard(self.conn, USER, row["application_id"])
        self.assertEqual(list(before), [row["application_id"]])
        self.conn.execute(
            "INSERT INTO automation_actions(id, user_id, feature, action_type, subject_kind, subject_id, status, idempotency_key, created_at) "
            "VALUES('x', ?, 'application_mail', 'application.stage', 'application', ?, 'rejected', ?, 't')",
            (USER, row["application_id"], f"gmail:{row['gmail_id']}:{row['application_id']}:application.stage"),
        )
        self.conn.commit()
        self.assertEqual(internal_automation.last_heard(self.conn, USER, row["application_id"]), {})

    def test_with_the_switch_off_or_no_emails_it_is_empty(self):
        self.assertEqual(internal_automation.last_heard(self.conn, USER), {})
        self.seed(apps=2, per_app=1, noise=0)
        self.conn.execute("UPDATE user_settings SET value='off' WHERE key='application_mail'")
        self.conn.commit()
        self.assertEqual(internal_automation.last_heard(self.conn, USER), {})


class LedgerReadTests(Case):
    def statements(self, call):
        seen = []
        self.conn.set_trace_callback(seen.append)
        try:
            call()
        finally:
            self.conn.set_trace_callback(None)
        return [text for text in seen if "FROM automation_actions" in text]

    def test_one_application_reads_only_its_own_key_range(self):
        self.seed(apps=5, per_app=2, noise=500)
        reads = self.statements(lambda: internal_automation.last_heard(self.conn, USER, "app-1"))
        self.assertTrue(reads)
        self.assertTrue(all("idempotency_key >=" in text and "idempotency_key <" in text for text in reads), reads)
        self.assertEqual(len(reads), 2, "one read for each of the application's two emails")
        plan = self.conn.execute(
            "EXPLAIN QUERY PLAN SELECT idempotency_key, status, review FROM automation_actions "
            "WHERE user_id=? AND feature='application_mail' AND idempotency_key >= ? AND idempotency_key < ?",
            (USER, "gmail:g1-0:", "gmail:g1-0;"),
        ).fetchall()
        self.assertTrue(any("SEARCH" in str(tuple(row)) and "idempotency_key" in str(tuple(row)) for row in plan), plan)

    def test_more_than_fifty_emails_read_the_ledger_once(self):
        self.seed(apps=60, per_app=1, noise=10)
        self.conn.execute("UPDATE application_mail_messages SET kind='interview', state='done'")
        self.conn.commit()
        reads = self.statements(lambda: internal_automation.last_heard(self.conn, USER))
        self.assertEqual(len(reads), 1)
        self.assertNotIn("idempotency_key >=", reads[0])

    def test_postgresql_always_reads_the_whole_ledger(self):
        """A PostgreSQL collation need not order the key bounds as SQLite's BINARY one does."""

        class Postgres:
            backend = "postgresql"

            def __init__(self, conn):
                self.conn, self.sql = conn, []

            def execute(self, sql, params=()):
                self.sql.append(sql)
                return self.conn.execute(sql, params)

        self.seed(apps=3, per_app=1, noise=0)
        proxy = Postgres(self.conn)
        rows = internal_automation._ledger_rows(proxy, USER, {"g0-0"})
        self.assertEqual(len(proxy.sql), 1)
        self.assertNotIn("idempotency_key >=", proxy.sql[0])
        self.assertEqual(rows, full_ledger_read(self.conn, USER, set()))

    def test_no_emails_reads_the_whole_ledger_as_before(self):
        self.seed(apps=1, per_app=1, noise=3)
        self.assertEqual(internal_automation._ledger_rows(self.conn, USER, set()), full_ledger_read(self.conn, USER, set()))

    def test_the_range_ends_just_after_the_colon(self):
        self.assertEqual(ord(":") + 1, ord(";"))


if __name__ == "__main__":
    unittest.main()
