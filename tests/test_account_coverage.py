"""The account export and the account delete must cover every table that holds a student's rows.

``operations.ACCOUNT_QUERIES`` lists what the export holds and ``delete_account`` erases an account by
deleting the ``users`` row and relying on ``ON DELETE CASCADE``. Both drifted as tables were added: the
Gmail label tables (0044) have a ``user_id`` column but no foreign key, so a deleted account left its
Gmail searches behind, and seven tables never reached the export. These tests read the migrated schema, so
a table added later fails here until it is exported (or excluded below with a reason) and deleted.
"""

import re
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opportunity_app.operations import ACCOUNT_EXPLICIT_DELETES, ACCOUNT_QUERIES, delete_account, export_account
from opportunity_app.core.database import connect_product

from helpers_platform import build_and_migrate

USER = "local-user"
OTHER = "student-b"

# Tables with a user_id column that the export leaves out on purpose. Each needs a reason. Everything else
# with a user_id column must be in ACCOUNT_QUERIES.
EXPORT_EXCLUDED = {
    "user_credentials": "the password hash and credential secrets; an export file must never carry them",
    "user_api_tokens": "bearer-token hashes; an export file must never carry them",
    "oauth_states": "single-use sign-in handshakes with PKCE verifiers; they expire in minutes and hold no student data",
    "recovery_challenges": "single-use recovery code hashes; they expire in minutes and hold no student data",
    "automation_held": "a waiting proposal's full link, token and all; migration 0038 keeps it out of the export (once approved it is exported as the task's own link)",
    "action_requests": "an idempotency cache of past API responses; what they changed is exported in the tables they wrote",
    "organization_memberships": "an employer organization's roster, not the student's own content",
}

# Tables whose user_id has no ON DELETE CASCADE foreign key, which delete_account has to erase itself.
# (The set lives in operations.ACCOUNT_EXPLICIT_DELETES; this repeats it so a change to it is deliberate.)
EXPECTED_EXPLICIT_DELETES = {"outreach_label_threads", "outreach_label_searches"}


def first_table(query: str) -> str:
    return re.search(r"\bFROM\s+(\w+)", query, re.IGNORECASE).group(1)


class AccountCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        _, platform_path = build_and_migrate(self.root)
        self.conn = connect_product(platform_path)
        self.addCleanup(self.conn.close)
        with self.conn:
            self.conn.execute(
                "INSERT INTO users(id, email, display_name, role, created_at, updated_at) "
                "VALUES(?, 'b@example.com', 'B', 'student', '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00')", (OTHER,))

    def user_tables(self) -> list[str]:
        names = [row[0] for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return [
            name for name in names
            if any(column[1] == "user_id" for column in self.conn.execute(f'PRAGMA table_info("{name}")'))
        ]

    def cascades_from_users(self, table: str) -> bool:
        return any(
            fk["table"] == "users" and fk["from"] == "user_id" and str(fk["on_delete"]).upper() == "CASCADE"
            for fk in self.conn.execute(f'PRAGMA foreign_key_list("{table}")')
        )

    def delete(self, user_id=USER):
        return delete_account(self.conn, [self.root / "resumes", self.root / "captures", self.root / "interviews"], user_id=user_id)


class SchemaCoverageTests(AccountCase):
    def test_every_table_with_a_user_id_is_exported_or_excluded_with_a_reason(self):
        exported = {first_table(query) for query in ACCOUNT_QUERIES.values()}
        tables = self.user_tables()
        self.assertGreater(len(tables), 50, "the schema introspection found the tables")
        missing = [name for name in tables if name not in exported and name not in EXPORT_EXCLUDED]
        self.assertEqual(missing, [], "add each to operations.ACCOUNT_QUERIES, or to EXPORT_EXCLUDED here with a reason")

    def test_the_exclusion_list_holds_no_stale_or_exported_tables(self):
        exported = {first_table(query) for query in ACCOUNT_QUERIES.values()}
        tables = set(self.user_tables())
        self.assertEqual(sorted(set(EXPORT_EXCLUDED) - tables), [], "an excluded table that no longer exists")
        self.assertEqual(sorted(set(EXPORT_EXCLUDED) & exported), [], "an excluded table the export now holds")
        self.assertTrue(all(reason.strip() for reason in EXPORT_EXCLUDED.values()))

    def test_every_exported_query_runs_and_every_exported_table_exists(self):
        result = export_account(self.conn, user_id=USER)
        for key in ACCOUNT_QUERIES:
            self.assertIn(key, result)

    def test_every_table_with_a_user_id_is_erased_with_the_account(self):
        loose = [name for name in self.user_tables() if not self.cascades_from_users(name)]
        self.assertEqual(
            sorted(loose), sorted(ACCOUNT_EXPLICIT_DELETES),
            "a table whose user_id has no ON DELETE CASCADE must be listed in operations.ACCOUNT_EXPLICIT_DELETES",
        )
        self.assertEqual(set(ACCOUNT_EXPLICIT_DELETES), EXPECTED_EXPLICIT_DELETES)


class LabelTablesTests(AccountCase):
    """The two tables that had no foreign key, with real rows for two students."""

    def seed(self, user_id):
        with self.conn:
            self.conn.execute(
                "INSERT INTO outreach_label_threads(user_id, thread_id, target_id, source, label_name, found_at) "
                "VALUES(?, 't-1', 'target-1', 'sent', 'Outreach', '2026-09-01T00:00:00+00:00')", (user_id,))
            self.conn.execute(
                "INSERT INTO outreach_label_searches(user_id, target_id, searched_at, found, query) "
                "VALUES(?, 'target-1', '2026-09-01T00:00:00+00:00', 1, 'to:person@example.com subject:Hello')", (user_id,))

    def count(self, table, user_id):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id=?", (user_id,)).fetchone()[0]

    def test_deleting_the_account_removes_its_gmail_label_rows_and_only_its_own(self):
        self.seed(USER)
        self.seed(OTHER)
        self.delete(USER)
        for table in ("outreach_label_threads", "outreach_label_searches"):
            with self.subTest(table=table):
                self.assertEqual(self.count(table, USER), 0, "the searches hold contact addresses and subjects")
                self.assertEqual(self.count(table, OTHER), 1, "no one else's rows go")

    def test_the_export_holds_the_gmail_label_rows(self):
        self.seed(USER)
        self.seed(OTHER)
        exported = export_account(self.conn, user_id=USER)
        self.assertEqual([row["thread_id"] for row in exported["outreach_label_threads"]], ["t-1"])
        self.assertEqual([row["query"] for row in exported["outreach_label_searches"]], ["to:person@example.com subject:Hello"])
        self.assertEqual({row["user_id"] for row in exported["outreach_label_threads"]}, {USER})


class OtherExportedTablesTests(AccountCase):
    """The four outreach tables that cascaded on delete but never reached the export."""

    def test_the_export_holds_scheduled_sends_contact_forms_dismissals_and_tag_state(self):
        stamp = "2026-09-01T00:00:00+00:00"
        with self.conn:
            self.conn.execute("INSERT INTO outreach_targets(id, user_id, company, created_at, updated_at) VALUES('target-1', ?, 'Acme', ?, ?)", (USER, stamp, stamp))
            self.conn.execute("INSERT INTO outreach_targets(id, user_id, company, created_at, updated_at) VALUES('target-2', ?, 'Other', ?, ?)", (OTHER, stamp, stamp))
            for user_id, target_id in ((USER, "target-1"), (OTHER, "target-2")):
                self.conn.execute(
                    "INSERT INTO outreach_scheduled_sends(target_id, user_id, kind, fingerprint, send_at, timezone, label, state, created_at, updated_at) "
                    "VALUES(?, ?, 'first', 'fp', ?, 'UTC', 'Tomorrow', 'scheduled', ?, ?)", (target_id, user_id, stamp, stamp, stamp))
                self.conn.execute(
                    "INSERT INTO outreach_contact_forms(target_id, user_id, page_url, found_at, updated_at) VALUES(?, ?, 'https://example.com/contact', ?, ?)",
                    (target_id, user_id, stamp, stamp))
                self.conn.execute(
                    "INSERT INTO outreach_dismissed(user_id, company_key, company, dismissed_at) VALUES(?, 'acme', 'Acme', ?)", (user_id, stamp))
                self.conn.execute("INSERT INTO outreach_tag_state(user_id, signature, generated_at) VALUES(?, 'sig', ?)", (user_id, stamp))
        exported = export_account(self.conn, user_id=USER)
        for table in ("outreach_scheduled_sends", "outreach_contact_forms", "outreach_dismissed", "outreach_tag_state"):
            with self.subTest(table=table):
                self.assertTrue(table in exported, f"the export has no {table} section")
                self.assertEqual({row["user_id"] for row in exported[table]}, {USER}, "the student's own rows, and no one else's")
                self.assertEqual(len(exported[table]), 1)


if __name__ == "__main__":
    unittest.main()
