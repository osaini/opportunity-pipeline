"""The status panel: scheduled jobs, the daily run, and which boards stopped answering."""

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR
from opportunity_app.api import create_app
from opportunity_app.system_status import SystemStatus, daily_run, source_health

from helpers_platform import build_and_migrate

NOW = datetime(2026, 9, 21, 16, 0, tzinfo=timezone.utc)
SOURCES = {
    "ats_sources": [
        {"kind": "greenhouse", "company": "Acme", "token": "acme"},
        {"kind": "greenhouse", "company": "Broken Co", "token": "broken"},
        {"kind": "lever", "company": "Quiet Co", "site": "quiet"},
        {"kind": "ashby", "company": "New Co", "board": "newco"},
        {"kind": "greenhouse", "company": "Off Co", "token": "off", "enabled": False},
    ],
    "discovery_title_terms": [],
}


class FakeScheduler:
    def __init__(self, installed=("daily",)):
        self.installed = set(installed)
        self.calls = []

    def jobs(self):
        self.calls.append("jobs")
        return {
            job: {"installed": job in self.installed, "state": "Ready" if job in self.installed else "",
                  "last_run_at": "2026-09-21T13:00:00Z" if job in self.installed else None,
                  "next_run_at": None, "last_result": 0 if job in self.installed else None}
            for job in ("daily", "outreach", "autostart")
        }

    def install(self, job):
        self.calls.append(f"install:{job}")
        self.installed.add(job)
        return 0


class StatusCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.sources = self.root / "sources.json"
        self.sources.write_text(json.dumps(SOURCES), encoding="utf-8")
        self.legacy = self.root / "pipeline.db"
        with closing(sqlite3.connect(self.legacy)) as conn:
            conn.execute(
                "CREATE TABLE fetch_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, source_key TEXT NOT NULL, started_at TEXT NOT NULL,"
                " finished_at TEXT, outcome TEXT NOT NULL, fetched_count INTEGER NOT NULL DEFAULT 0, error TEXT)"
            )
            runs = [
                ("greenhouse:acme", "2026-09-20T13:00:00+00:00", "success", 12, None),
                ("greenhouse:acme", "2026-09-21T13:00:00+00:00", "success", 14, None),
                ("greenhouse:broken", "2026-09-10T13:00:00+00:00", "success", 3, None),
                ("greenhouse:broken", "2026-09-20T13:00:00+00:00", "error", 0, "HTTP 404 from boards-api"),
                ("greenhouse:broken", "2026-09-21T13:00:00+00:00", "error", 0, "HTTP 404 from boards-api"),
                ("lever:quiet", "2026-09-01T13:00:00+00:00", "success", 0, None),
                # An interrupted run leaves a running row; it is neither a success nor a failure.
                ("lever:quiet", "2026-09-21T13:00:00+00:00", "running", 0, None),
            ]
            conn.executemany(
                "INSERT INTO fetch_runs(source_key, started_at, finished_at, outcome, fetched_count, error) VALUES (?, ?, ?, ?, ?, ?)",
                [(key, stamp, stamp, outcome, count, error) for key, stamp, outcome, count, error in runs],
            )
            conn.commit()
        self.daily_state = self.root / "daily-run.json"

    def tearDown(self):
        self.tmp.cleanup()


class SourceHealthTests(StatusCase):
    def test_failing_boards_come_first_with_their_streak_and_error(self):
        health = source_health(self.legacy, self.sources, now=NOW)
        self.assertEqual((health["enabled"], health["failing"], health["stale"]), (4, 1, 1))
        by_name = {item["name"]: item for item in health["items"]}
        self.assertEqual([item["name"] for item in health["items"]], ["Broken Co", "Quiet Co", "New Co", "Acme"])
        broken = by_name["Broken Co"]
        self.assertEqual((broken["health"], broken["failures_in_a_row"]), ("failing", 2))
        self.assertEqual(broken["last_success_at"], "2026-09-10T13:00:00+00:00")
        self.assertIn("404", broken["last_error"])
        self.assertEqual(by_name["Quiet Co"]["health"], "stale")
        self.assertEqual(by_name["New Co"]["health"], "never")
        self.assertEqual((by_name["Acme"]["health"], by_name["Acme"]["last_fetched"]), ("ok", 14))
        self.assertNotIn("Off Co", by_name)

    def test_a_missing_database_is_reported_as_unavailable(self):
        health = source_health(self.root / "absent.db", self.sources, now=NOW)
        self.assertEqual((health["available"], health["enabled"], health["items"]), (False, 4, []))


class DailyRunTests(StatusCase):
    def write(self, **state):
        self.daily_state.write_text(json.dumps(state), encoding="utf-8")

    def test_no_state_means_it_never_ran(self):
        self.assertEqual((daily_run(self.daily_state, now=NOW)["ran"], daily_run(self.daily_state, now=NOW)["overdue"]), (False, True))

    def test_a_run_over_a_day_and_a_half_ago_is_overdue(self):
        self.write(runDate="2026-09-19", startedAt="2026-09-19T13:00:00Z", finishedAt="2026-09-19T13:04:00Z", exitCode=0)
        self.assertTrue(daily_run(self.daily_state, now=NOW)["overdue"])
        self.write(runDate="2026-09-21", startedAt="2026-09-21T13:00:00Z", finishedAt="2026-09-21T13:04:00Z", exitCode=0)
        self.assertFalse(daily_run(self.daily_state, now=NOW)["overdue"])

    def test_an_unfinished_run_is_in_progress(self):
        self.write(runDate="2026-09-21", startedAt="2026-09-21T13:00:00Z", finishedAt=None, exitCode=None)
        self.assertTrue(daily_run(self.daily_state, now=NOW)["in_progress"])


class StatusApiTests(StatusCase):
    def app(self, system_status=None):
        _, platform_path = build_and_migrate(self.root / "app")
        return create_app(
            db_path=platform_path, access_token="status-owner", static_dir=STATIC_DIR,
            resume_storage=self.root / "resumes", capture_storage=self.root / "captures",
            interview_storage=self.root / "interviews", system_status=system_status,
        )

    def test_the_owner_sees_problems_and_installs_a_missing_schedule(self):
        (self.root / "app").mkdir()
        self.daily_state.write_text(json.dumps({
            "runDate": "2026-09-21", "startedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "finishedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "exitCode": 0,
        }), encoding="utf-8")
        scheduler = FakeScheduler()
        status_source = SystemStatus(
            legacy_path=self.legacy, sources_path=self.sources, daily_state_path=self.daily_state, scheduler=scheduler,
        )
        headers = {"Authorization": "Bearer status-owner"}
        with TestClient(self.app(status_source)) as client:
            report = client.get("/api/v1/system/status", headers=headers).json()
            self.assertEqual(report["problems"], ["Outreach deep search is not scheduled", "1 job board failing"])
            self.assertEqual([job["job"] for job in report["jobs"]], ["daily", "outreach", "autostart"])
            self.assertEqual(report["sources"]["items"][0]["name"], "Broken Co")

            installed = client.post("/api/v1/system/schedules/outreach/install", headers=headers)
            self.assertEqual(installed.status_code, 200, installed.text)
            self.assertEqual(installed.json()["problems"], ["1 job board failing"])
            self.assertIn("install:outreach", scheduler.calls)
            # Installing the sign-in job would start a second server beside this one.
            self.assertEqual(client.post("/api/v1/system/schedules/autostart/install", headers=headers).status_code, 422)
            self.assertEqual(client.get("/api/v1/system/status").status_code, 401)

    def test_a_scratch_database_has_no_status_to_show(self):
        (self.root / "app").mkdir()
        with TestClient(self.app()) as client:
            headers = {"Authorization": "Bearer status-owner"}
            self.assertEqual(client.get("/api/v1/system/status", headers=headers).json()["available"], False)
            self.assertEqual(client.post("/api/v1/system/schedules/daily/install", headers=headers).status_code, 409)

    def test_the_scheduler_is_asked_at_most_once_per_cache_window(self):
        scheduler = FakeScheduler()
        now = [0.0]
        status_source = SystemStatus(
            legacy_path=self.legacy, sources_path=self.sources, daily_state_path=self.daily_state,
            scheduler=scheduler, cache_seconds=30, clock=lambda: now[0],
        )
        status_source.status()
        now[0] = 29.0
        status_source.status()
        self.assertEqual(scheduler.calls, ["jobs"])
        now[0] = 31.0
        status_source.status()
        self.assertEqual(scheduler.calls, ["jobs", "jobs"])


if __name__ == "__main__":
    unittest.main()
