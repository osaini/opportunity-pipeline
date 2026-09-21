"""The cross-platform daily run (opportunity_app.daily), with a scripted runner."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from opportunity_app import daily

CENTRAL = timezone(timedelta(hours=-5))


class ScriptedRunner:
    def __init__(self, exits=None):
        self.calls = []
        self.exits = dict(exits or {})

    def __call__(self, arguments):
        self.calls.append(arguments)
        # arguments[1] is the pipeline.py command or the module after -m.
        return self.exits.get(arguments[1], 0)

    def steps(self):
        return [call[1] for call in self.calls]


class DailyRunTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.state_path = root / "daily-run.json"
        self.platform_db = root / "platform.db"
        self.lines = []
        self.clock = datetime(2026, 9, 21, 9, 0, tzinfo=CENTRAL)

    def tearDown(self):
        self.tempdir.cleanup()

    def run_daily(self, runner, **kwargs):
        options = {
            "runner": runner,
            "now": lambda: self.clock,
            "network": lambda: True,
            "state_path": self.state_path,
            "platform_db": self.platform_db,
            "log": self.lines.append,
        }
        options.update(kwargs)
        return daily.run_daily(**options)

    def state(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def test_a_full_run_takes_every_step_in_order_and_records_the_day(self):
        self.platform_db.touch()
        runner = ScriptedRunner()
        self.assertEqual(self.run_daily(runner), 0)
        self.assertEqual(
            runner.steps(),
            ["run", "liveness", "purge-expired", "opportunity_app.migrate", "opportunity_app.purge", "opportunity_app.outreach_cli"],
        )
        self.assertEqual(runner.calls[0][2:], ["--resume-since", "2026-09-21T14:00:00Z"])
        state = self.state()
        self.assertEqual(state["runDate"], "2026-09-21")
        self.assertEqual(state["finishedAt"], "2026-09-21T14:00:00Z")
        self.assertEqual(
            state["completed"],
            ["run", "liveness", "purge-expired", "platform-sync", "platform-purge", "outreach-remind"],
        )

    def test_without_the_product_database_only_the_pipeline_steps_run(self):
        runner = ScriptedRunner()
        self.run_daily(runner)
        self.assertEqual(runner.steps(), ["run", "liveness", "purge-expired"])

    def test_unreachable_sources_pause_the_run_and_the_next_trigger_resumes_it(self):
        paused = ScriptedRunner({"run": daily.TEMPFAIL_EXIT})
        self.assertEqual(self.run_daily(paused), daily.TEMPFAIL_EXIT)
        self.assertEqual(paused.steps(), ["run"])
        self.assertEqual(self.state()["completed"], [])
        self.assertIsNone(self.state()["finishedAt"])

        self.clock += timedelta(minutes=30)
        resumed = ScriptedRunner()
        self.assertEqual(self.run_daily(resumed, scheduled=True), 0)
        # The resumed fetch skips what the first attempt already fetched.
        self.assertEqual(resumed.calls[0][2:], ["--resume-since", "2026-09-21T14:00:00Z"])
        self.assertEqual(self.state()["attempts"], 2)
        self.assertIsNotNone(self.state()["finishedAt"])
        self.assertTrue(any("run resume (attempt 2" in line for line in self.lines))

    def test_the_last_attempt_keeps_what_arrived(self):
        runner = ScriptedRunner({"run": daily.TEMPFAIL_EXIT})
        self.assertEqual(self.run_daily(runner, max_attempts=1), daily.TEMPFAIL_EXIT)
        self.assertEqual(runner.steps(), ["run", "liveness", "purge-expired"])
        self.assertIsNotNone(self.state()["finishedAt"])

    def test_a_fatal_fetch_skips_everything_downstream_and_leaves_the_day_open(self):
        runner = ScriptedRunner({"run": 1})
        self.assertEqual(self.run_daily(runner), 1)
        self.assertEqual(runner.steps(), ["run"])
        self.assertIsNone(self.state()["finishedAt"])

    def test_a_failed_later_step_is_logged_and_the_run_carries_on(self):
        runner = ScriptedRunner({"liveness": 2})
        self.assertEqual(self.run_daily(runner), 0)
        self.assertEqual(runner.steps(), ["run", "liveness", "purge-expired"])
        self.assertIn("--- liveness exited 2 ---", self.lines)

    def test_scheduled_mode_waits_for_not_before_and_runs_once_a_day(self):
        early = datetime(2026, 9, 21, 7, 0, tzinfo=CENTRAL)
        runner = ScriptedRunner()
        self.assertEqual(self.run_daily(runner, scheduled=True, now=lambda: early), 0)
        self.assertEqual(runner.calls, [])
        self.assertFalse(self.state_path.exists())

        self.run_daily(runner, scheduled=True)
        self.assertEqual(runner.steps(), ["run", "liveness", "purge-expired"])
        again = ScriptedRunner()
        self.clock += timedelta(hours=2)
        self.run_daily(again, scheduled=True)
        self.assertEqual(again.calls, [], "today's run already finished")

    def test_a_stale_unfinished_run_is_abandoned_for_a_fresh_one(self):
        self.run_daily(ScriptedRunner({"run": 1}))
        self.clock += timedelta(hours=daily.RESUME_WINDOW_HOURS + 1)
        runner = ScriptedRunner()
        self.run_daily(runner)
        self.assertTrue(any("abandoning unfinished run" in line for line in self.lines))
        self.assertEqual(self.state()["attempts"], 1)

    def test_no_network_defers_without_touching_state(self):
        runner = ScriptedRunner()
        self.assertEqual(self.run_daily(runner, scheduled=True, network=lambda: False), 0)
        self.assertEqual(runner.calls, [])
        self.assertFalse(self.state_path.exists())

    def test_reads_the_state_file_the_powershell_script_writes(self):
        # Windows PowerShell 5.1 writes UTF-8 with a byte order mark.
        self.state_path.write_bytes(
            b"\xef\xbb\xbf"
            + json.dumps({
                "runDate": "2026-09-21", "startedAt": "2026-09-21T13:30:00Z", "attempts": 1,
                "completed": ["run"], "finishedAt": None, "exitCode": 0,
            }).encode()
        )
        runner = ScriptedRunner()
        self.run_daily(runner)
        self.assertEqual(runner.steps(), ["liveness", "purge-expired"])


if __name__ == "__main__":
    unittest.main()
