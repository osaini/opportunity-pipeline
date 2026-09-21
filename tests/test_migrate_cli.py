"""Unit tests for the migration CLI entry point and its idempotency."""

import io
import json
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opportunity_app.migrate import main
from opportunity_app.schema import connect_product

from helpers_platform import build_and_migrate


class MigrateCliTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.legacy_path, self.platform_path = build_and_migrate(root)
        self.profile_path = root / "profile.json"

    def tearDown(self):
        self.tempdir.cleanup()

    def _run(self) -> tuple[int, dict]:
        argv = [
            "migrate",
            "--source",
            str(self.legacy_path),
            "--target",
            str(self.platform_path),
            "--profile",
            str(self.profile_path),
        ]
        buffer = io.StringIO()
        with mock.patch("sys.argv", argv), redirect_stdout(buffer):
            code = main()
        # main() prints the result JSON followed by human-readable parity lines.
        payload, _ = json.JSONDecoder().raw_decode(buffer.getvalue())
        return code, payload

    def test_remigration_is_idempotent_and_parity_holds(self):
        first_code, first_result = self._run()
        self.assertEqual(first_code, 0)
        self.assertTrue(first_result["top_ids_match"])
        self.assertEqual(first_result["active_unique_source"], first_result["active_unique_target"])
        second_code, second_result = self._run()
        self.assertEqual(second_code, 0)
        self.assertEqual(first_result["active_unique_target"], second_result["active_unique_target"])
        with closing(connect_product(self.platform_path)) as conn:
            opportunities = conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0]
        self.assertEqual(opportunities, second_result["active_unique_target"])


if __name__ == "__main__":
    unittest.main()
