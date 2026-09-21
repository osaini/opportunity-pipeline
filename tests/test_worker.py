"""Unit tests for the durable worker loop (previously untested)."""

import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opportunity_app.operations import enqueue_job, queue_status
from opportunity_app.schema import connect_product, utc_now
from opportunity_app.worker import run_once

from helpers_platform import build_and_migrate


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        _, self.platform_path = build_and_migrate(Path(self.tempdir.name))

    def tearDown(self):
        self.tempdir.cleanup()

    def _enqueue(self, job_type: str, payload: dict, key: str, **kwargs):
        with closing(connect_product(self.platform_path)) as conn:
            return enqueue_job(conn, job_type, payload, key, **kwargs)

    def test_run_once_executes_retention_job(self):
        record = self._enqueue("retention", {}, "retention-once")
        result = run_once(self.platform_path)
        self.assertIsNotNone(result)
        self.assertEqual(result["id"], record["id"])
        self.assertEqual(result["state"], "succeeded")
        with closing(connect_product(self.platform_path)) as conn:
            status = queue_status(conn)
        self.assertEqual(status["states"]["succeeded"], 1)

    def test_run_once_returns_none_when_queue_is_empty(self):
        self.assertIsNone(run_once(self.platform_path))

    def test_noop_handlers_are_registered_and_succeed(self):
        # Documents current scaffolding behavior: these job types are accepted
        # and complete. Phase R-D replaces them with real implementations.
        for job_type, key in (
            ("connector_health", "health-once"),
            ("notification_digest", "digest-once"),
        ):
            with self.subTest(job_type=job_type):
                record = self._enqueue(job_type, {}, key)
                result = run_once(self.platform_path)
                self.assertEqual(record["state"], "queued")
                self.assertEqual(result["state"], "succeeded")
                self.assertEqual(result["job_type"], job_type)

    def test_unknown_job_type_retries_with_backoff_then_dies(self):
        self._enqueue("mystery", {}, "mystery-retry", max_attempts=3)
        first = run_once(self.platform_path)
        self.assertEqual(first["state"], "retry")
        self.assertIn("mystery", first["last_error"])
        # Backoff must push the next attempt into the future.
        self.assertGreater(first["next_attempt_at"], utc_now())
        self.assertIsNone(run_once(self.platform_path), "backoff should prevent immediate re-run")

        with closing(connect_product(self.platform_path)) as conn:
            with conn:
                conn.execute(
                    "UPDATE job_queue SET next_attempt_at=?, max_attempts=1 WHERE idempotency_key=?",
                    (utc_now(), "mystery-retry"),
                )
        second = run_once(self.platform_path)
        self.assertEqual(second["attempts"], 2)
        self.assertEqual(second["state"], "dead")

    def test_idempotency_key_prevents_duplicate_enqueue(self):
        first = self._enqueue("retention", {}, "same-key")
        second = self._enqueue("retention", {}, "same-key")
        self.assertEqual(first["id"], second["id"])
        result = run_once(self.platform_path)
        self.assertEqual(result["state"], "succeeded")
        self.assertIsNone(run_once(self.platform_path))


if __name__ == "__main__":
    unittest.main()
