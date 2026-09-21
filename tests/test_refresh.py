"""Manual refresh and purge started from the web app."""

import json
import sqlite3
import sys
import threading
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

# Importable on its own as well as through discovery.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from opportunity_app.api import create_app
from opportunity_app.auth import issue_user_token
from opportunity_app.employer import ensure_actor
from opportunity_app.refresh import RefreshBusy, RefreshManager
from opportunity_app.schema import connect_product
from helpers_platform import build_and_migrate, build_profile

OWNER_TOKEN = "refresh-owner-token"


class AlwaysOwned:
    def acquire(self):
        return True

    def release(self):
        pass


class NeverOwned(AlwaysOwned):
    def acquire(self):
        return False


class FakePipeline:
    """Replays the output the real CLI prints, without touching the network."""

    def __init__(self, legacy_path, *, run_exit=0, gate=None):
        self.legacy_path = legacy_path
        self.run_exit = run_exit
        self.gate = gate
        self.calls = []

    def __call__(self, arguments, on_line):
        self.calls.append(arguments)
        command = arguments[0]
        if command == "run":
            if self.gate:
                self.gate.wait(timeout=10)
            # Interleaved the way a concurrent fetch really emits them: both
            # sources start before either finishes.
            for line in ("Fetching Acme (greenhouse)…", "Fetching Orbit (lever)…",
                         "  Done Acme (greenhouse): 3 candidate postings saved",
                         "  Done Orbit (lever): 2 candidate postings saved",
                         "Imported 0 manual postings", "Scored 2 postings"):
                on_line(line)
            return self.run_exit
        if command == "liveness":
            for line in ("Checking 2 posting(s)…", "  ok Acme — Intern", "  x Orbit — Intern: 404 — retired"):
                on_line(line)
            return 0
        if command == "purge-expired":
            # What the legacy purge does to a posting its employer took down.
            with closing(sqlite3.connect(self.legacy_path)) as legacy, legacy:
                legacy.execute("DELETE FROM jobs WHERE id='job-a'")
            on_line("Deleted 1 posting(s): 1 retired, 0 past deadline, 0 kept for applications")
            return 0
        raise AssertionError(f"unexpected command {arguments}")


class RefreshManagerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.legacy_path, self.platform_path = build_and_migrate(self.root)
        self.sources_path = self.root / "sources.json"
        self.sources_path.write_text(json.dumps({"ats_sources": [
            {"company": "Acme"}, {"company": "Orbit"}, {"company": "Off", "enabled": False},
        ]}), encoding="utf-8")

    def manager(self, runner, mutex=AlwaysOwned):
        return RefreshManager(
            self.platform_path,
            legacy_path=self.legacy_path,
            profile_path=build_profile(self.root),
            sources_path=self.sources_path,
            runner=runner,
            mutex_factory=mutex,
        )

    def test_a_refresh_runs_every_step_and_syncs_the_app_database(self):
        pipeline = FakePipeline(self.legacy_path)
        manager = self.manager(pipeline)
        manager.start()
        manager.wait(timeout=30)

        status = manager.status()
        self.assertEqual(status["state"], "succeeded", status)
        self.assertEqual([call[0] for call in pipeline.calls], ["run", "liveness", "purge-expired"])
        steps = {step["key"]: step for step in status["steps"]}
        self.assertTrue(all(step["state"] == "done" for step in steps.values()), steps)
        self.assertEqual((steps["pull"]["done"], steps["pull"]["total"]), (3, 3))
        self.assertEqual(steps["liveness"]["detail"], "Checked 2; retired 1")
        self.assertEqual(steps["purge-legacy"]["detail"], "Deleted 1")
        self.assertIn("retired 1 no longer listed", steps["sync"]["detail"])
        with closing(connect_product(self.platform_path)) as conn:
            remaining = {row[0] for row in conn.execute("SELECT id FROM opportunities")}
        # job-a left the legacy database, was retired by the sync, then purged.
        self.assertNotIn("job-a", remaining)
        self.assertIn("job-b", remaining)

    def test_concurrent_starts_do_not_advance_progress(self):
        """Twelve sources in flight, none finished: the bar stays at zero.

        The fetch runs sources concurrently, so a dozen start lines arrive
        before any source completes. Progress used to advance on each start,
        inferring that the previous source had finished -- which would report
        eleven of twelve done while nothing had been fetched at all.
        """

        self.sources_path.write_text(json.dumps({"ats_sources": [
            {"company": f"S{index}"} for index in range(12)
        ]}), encoding="utf-8")
        progress = []

        class TwelveInFlight(FakePipeline):
            def __call__(self, arguments, on_line):
                self.calls.append(arguments)
                if arguments[0] != "run":
                    return super().__call__(arguments, on_line)
                for index in range(12):
                    on_line(f"Fetching S{index} (greenhouse)…")
                    progress.append(manager.status()["steps"][0]["done"])
                for index in range(12):
                    on_line(f"  Done S{index} (greenhouse): 1 candidate postings saved")
                    progress.append(manager.status()["steps"][0]["done"])
                on_line("Scored 12 postings")
                return 0

        manager = self.manager(TwelveInFlight(self.legacy_path))
        manager.start()
        manager.wait(timeout=30)

        self.assertEqual(progress[:12], [0] * 12, "progress advanced before any source finished")
        self.assertEqual(progress[12:], list(range(1, 13)), "progress did not track completions")

    def test_unreachable_sources_keep_going(self):
        manager = self.manager(FakePipeline(self.legacy_path, run_exit=75))
        manager.start()
        manager.wait(timeout=30)
        status = manager.status()
        self.assertEqual(status["state"], "succeeded")
        self.assertIn("unreachable", status["steps"][0]["detail"])

    def test_a_failed_fetch_skips_the_later_steps(self):
        manager = self.manager(FakePipeline(self.legacy_path, run_exit=2))
        manager.start()
        manager.wait(timeout=30)
        status = manager.status()
        self.assertEqual(status["state"], "failed")
        self.assertIn("exited 2", status["error"])
        self.assertEqual([step["state"] for step in status["steps"]],
                         ["failed", "skipped", "skipped", "skipped", "skipped"])

    def test_only_one_refresh_runs_at_a_time(self):
        gate = threading.Event()
        manager = self.manager(FakePipeline(self.legacy_path, gate=gate))
        manager.start()
        try:
            with self.assertRaises(RefreshBusy):
                manager.start()
        finally:
            gate.set()
            manager.wait(timeout=30)

    def test_a_scheduled_run_in_progress_blocks_a_manual_one(self):
        pipeline = FakePipeline(self.legacy_path)
        manager = self.manager(pipeline, mutex=NeverOwned)
        with self.assertRaises(RefreshBusy):
            manager.start()
        self.assertEqual(pipeline.calls, [])
        self.assertEqual(manager.status()["state"], "idle")


class RefreshApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.legacy_path, self.platform_path = build_and_migrate(self.root)

    def client(self, manager=None):
        app = create_app(
            db_path=self.platform_path,
            access_token=OWNER_TOKEN,
            resume_storage=self.root / "resumes",
            capture_storage=self.root / "captures",
            interview_storage=self.root / "mock-interviews",
            refresh_manager=manager,
        )
        return TestClient(app, headers={"Authorization": f"Bearer {OWNER_TOKEN}"})

    def test_a_temporary_database_never_gets_a_live_refresh(self):
        with self.client() as client:
            status = client.get("/api/v1/refresh").json()
            self.assertFalse(status["available"])
            self.assertEqual(client.post("/api/v1/refresh").status_code, 409)

    def test_owner_starts_a_refresh_and_polls_it_to_completion(self):
        manager = RefreshManager(
            self.platform_path,
            legacy_path=self.legacy_path,
            profile_path=build_profile(self.root),
            runner=FakePipeline(self.legacy_path),
            mutex_factory=AlwaysOwned,
        )
        with self.client(manager) as client:
            started = client.post("/api/v1/refresh")
            self.assertEqual(started.status_code, 202, started.text)
            self.assertTrue(started.json()["available"])
            manager.wait(timeout=30)
            status = client.get("/api/v1/refresh").json()
        self.assertEqual(status["state"], "succeeded", status)
        self.assertEqual(len(status["steps"]), 5)

    def test_students_cannot_refresh(self):
        with closing(connect_product(self.platform_path)) as conn:
            ensure_actor(conn, "student-2", "student")
            token = issue_user_token(conn, "student-2")
        with self.client() as client:
            headers = {"Authorization": f"Bearer {token}"}
            self.assertEqual(client.get("/api/v1/refresh", headers=headers).status_code, 403)
            self.assertEqual(client.post("/api/v1/refresh", headers=headers).status_code, 403)


if __name__ == "__main__":
    unittest.main()
