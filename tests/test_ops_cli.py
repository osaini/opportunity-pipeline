"""Unit tests for the encrypted backup/restore/drill CLI (previously untested)."""

import ast
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cryptography.fernet import Fernet

from opportunity_app import ops_cli
from opportunity_app.operations import enqueue_job
from opportunity_app.schema import connect_product

from helpers_platform import build_and_migrate


class OpsCliTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        _, self.platform_path = build_and_migrate(root)
        self.key = Fernet.generate_key().decode()
        self.backup_path = root / "backup.bin"

    def tearDown(self):
        self.tempdir.cleanup()

    def _run(self, *argv: str) -> str:
        buffer = StringIO()
        env = {"PIPELINE_BACKUP_KEY": self.key}
        with mock.patch.object(ops_cli.os, "environ", {**ops_cli.os.environ, **env}):
            with mock.patch("sys.argv", ["ops_cli", *argv]), redirect_stdout(buffer):
                code = ops_cli.main()
        self.assertEqual(code, 0)
        return buffer.getvalue()

    def _result(self, output: str) -> dict:
        return ast.literal_eval(output.strip().splitlines()[-1])

    def test_missing_key_is_rejected_before_touching_files(self):
        destination = Path(self.tempdir.name) / "never-written.bin"
        with mock.patch.dict("os.environ", {"PIPELINE_BACKUP_KEY": ""}, clear=False):
            with mock.patch("sys.argv", ["ops_cli", "backup", str(destination), "--db", str(self.platform_path)]):
                with self.assertRaises(SystemExit):
                    ops_cli.main()
        self.assertFalse(destination.exists())

    def test_backup_then_restore_round_trip_preserves_data(self):
        with closing(connect_product(self.platform_path)) as conn:
            enqueue_job(conn, "retention", {}, "ops-cli-seed")

        self._run("backup", str(self.backup_path), "--db", str(self.platform_path))
        self.assertTrue(self.backup_path.is_file())
        # Fernet ciphertext must not leak plaintext table names.
        self.assertNotIn(b"job_queue", self.backup_path.read_bytes()[:4096])

        restored = Path(self.tempdir.name) / "restored.db"
        restored_result = self._result(self._run("restore", str(self.backup_path), str(restored)))
        self.assertEqual(restored_result["path"], str(restored.resolve()))
        self.assertEqual(restored_result["integrity"], "ok")
        with closing(sqlite3.connect(restored)) as conn:
            jobs = conn.execute(
                "SELECT COUNT(*) FROM job_queue WHERE idempotency_key='ops-cli-seed'"
            ).fetchone()[0]
        self.assertEqual(jobs, 1)

    def test_drill_restores_into_disposable_location_without_touching_source(self):
        self._run("backup", str(self.backup_path), "--db", str(self.platform_path))
        # Without --target the drill restores into a private temp directory that
        # ops_cli removes afterwards; only its report survives.
        drill_result = self._result(self._run("drill", str(self.backup_path), "--target", ""))
        self.assertTrue(drill_result["restored"])
        self.assertEqual(drill_result["integrity"], "ok")
        self.assertNotEqual(Path(drill_result["path"]).resolve(), self.platform_path.resolve())
        with closing(connect_product(self.platform_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)

    def test_drill_with_explicit_target_restores_to_that_database(self):
        self._run("backup", str(self.backup_path), "--db", str(self.platform_path))
        target = Path(self.tempdir.name) / "drill-target.db"
        drill_result = self._result(self._run("drill", str(self.backup_path), "--target", str(target)))
        self.assertEqual(drill_result["path"], str(target.resolve()))
        with closing(sqlite3.connect(target)) as conn:
            self.assertGreaterEqual(conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
