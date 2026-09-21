"""Purging expired opportunities from the product database."""

import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

# Importable on its own as well as through discovery.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from opportunity_app.purge import purge_expired_opportunities
from opportunity_app.schema import connect_product
from helpers_platform import build_and_migrate


class PurgeExpiredOpportunitiesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        _, self.platform_path = build_and_migrate(Path(self._tmp.name))
        # job-a (shortlisted, "Apply by September 1, 2026") and job-b (applied)
        # come from the fixture. Add a retired row and a duplicate of it.
        with closing(connect_product(self.platform_path)) as conn, conn:
            for opportunity_id, active, duplicate_of in (
                ("job-retired", 0, None),
                ("job-dup", 1, "job-retired"),
            ):
                conn.execute(
                    """
                    INSERT INTO opportunities(
                        id, company, title, location, region, role_type, url, description,
                        first_seen_at, last_seen_at, active, fingerprint, content_fingerprint,
                        duplicate_of, created_at, updated_at
                    ) VALUES(?, 'Gone Corp', 'Intern', '', '', 'internship', ?, '',
                             '2026-08-01', '2026-08-01', ?, ?, '', ?, '2026-08-01', '2026-08-01')
                    """,
                    (opportunity_id, f"https://example.com/{opportunity_id}", active, opportunity_id, duplicate_of),
                )
            conn.execute("UPDATE opportunities SET active=0 WHERE id='job-b'")

    def ids(self):
        with closing(connect_product(self.platform_path)) as conn:
            return {row[0] for row in conn.execute("SELECT id FROM opportunities")}

    def test_dry_run_deletes_nothing(self):
        with closing(connect_product(self.platform_path)) as conn:
            result = purge_expired_opportunities(conn, today="2026-09-14", dry_run=True)
        self.assertEqual(result["deleted"], 0)
        self.assertEqual(self.ids(), {"job-a", "job-b", "job-retired", "job-dup"})

    def test_deletes_retired_and_past_deadline_but_keeps_applications(self):
        with closing(connect_product(self.platform_path)) as conn:
            result = purge_expired_opportunities(conn, today="2026-09-14")
            dependents = conn.execute(
                "SELECT COUNT(*) FROM opportunity_sources WHERE opportunity_id IN ('job-a', 'job-retired')"
            ).fetchone()[0]
            duplicate_of = conn.execute("SELECT duplicate_of FROM opportunities WHERE id='job-dup'").fetchone()[0]

        self.assertEqual(result["retired"], 2)
        self.assertEqual(result["past_deadline"], 1)
        self.assertEqual(result["kept_for_applications"], 1)
        self.assertEqual(result["deleted"], 2)
        # job-b is retired but has an application, so it stays.
        self.assertEqual(self.ids(), {"job-b", "job-dup"})
        self.assertEqual(dependents, 0)
        self.assertIsNone(duplicate_of)

    def test_purge_backs_up_first_and_the_backup_keeps_deleted_rows(self):
        with closing(connect_product(self.platform_path)) as conn:
            result = purge_expired_opportunities(conn, today="2026-09-14")

        backup = Path(result["backup"])
        self.assertEqual(backup.parent, Path(self.platform_path).resolve().parent / "backups")
        with closing(sqlite3.connect(backup)) as snapshot:
            saved = {row[0] for row in snapshot.execute("SELECT id FROM opportunities")}
        self.assertEqual(saved, {"job-a", "job-b", "job-retired", "job-dup"})

    def test_dry_run_and_nothing_to_delete_write_no_backup(self):
        backups = Path(self.platform_path).resolve().parent / "backups"
        with closing(connect_product(self.platform_path)) as conn:
            self.assertIsNone(purge_expired_opportunities(conn, today="2026-09-14", dry_run=True)["backup"])
            self.assertFalse(backups.exists(), "a dry run must not write a backup")
            purge_expired_opportunities(conn, today="2026-09-14")
            second = purge_expired_opportunities(conn, today="2026-09-14")
        self.assertEqual(second["deleted"], 0)
        self.assertIsNone(second["backup"])
        self.assertEqual(len(list(backups.glob("platform-*.db"))), 1)

    def test_failed_backup_deletes_nothing(self):
        with closing(connect_product(self.platform_path)) as conn:
            with mock.patch("opportunity_app.purge.backup_sqlite", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    purge_expired_opportunities(conn, today="2026-09-14")
        self.assertEqual(self.ids(), {"job-a", "job-b", "job-retired", "job-dup"})

    def test_deadline_day_itself_is_not_expired(self):
        with closing(connect_product(self.platform_path)) as conn:
            result = purge_expired_opportunities(conn, today="2026-09-01")
        self.assertEqual(result["past_deadline"], 0)
        self.assertIn("job-a", self.ids())


if __name__ == "__main__":
    unittest.main()
