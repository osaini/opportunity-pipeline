"""Scheduled-task launchers must stay windowless.

A task registered with powershell.exe as its action shows a blank console each
time it fires, and closing that console kills the run. Both project tasks go
through a wscript.exe shim instead. The static checks run everywhere; the live
check drives the real daily launcher on Windows and watches for any new visible
window while it runs.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"
LAUNCHERS = {
    "run-daily.vbs": "run-daily.ps1",
    "start-web.vbs": "start-web.ps1",
    "run-outreach-discovery.vbs": "run-outreach-discovery.ps1",
}
INSTALLERS = {
    "install-daily-task.ps1": "run-daily.vbs",
    "install-web-task.ps1": "start-web.vbs",
    "install-outreach-task.ps1": "run-outreach-discovery.vbs",
}
CONSOLE_PROCESSES = {
    "powershell.exe", "pwsh.exe", "cmd.exe", "conhost.exe", "openconsole.exe", "windowsterminal.exe",
    "py.exe", "python.exe",
}


class LauncherSourceTests(unittest.TestCase):
    def test_launchers_start_their_script_hidden_and_wait(self):
        for launcher, script in LAUNCHERS.items():
            with self.subTest(launcher=launcher):
                source = (SCRIPTS / launcher).read_text(encoding="utf-8")
                self.assertIn(script, source)
                # Window style 0 hides the child; True waits so the exit code
                # reaches LastTaskResult.
                self.assertRegex(source, r"shell\.Run\(command,\s*0,\s*True\)")

    def test_installers_register_wscript_not_a_console_host(self):
        for installer, launcher in INSTALLERS.items():
            with self.subTest(installer=installer):
                source = (SCRIPTS / installer).read_text(encoding="utf-8")
                self.assertIn(f"'{launcher}'", source)
                self.assertIn("'wscript.exe'", source)
                self.assertNotRegex(source, r"-Execute\s+'?\"?powershell")

    def test_daily_task_runs_on_battery_and_catches_up_missed_runs(self):
        source = (SCRIPTS / "install-daily-task.ps1").read_text(encoding="utf-8")
        for setting in ("-AllowStartIfOnBatteries", "-DontStopIfGoingOnBatteries", "-StartWhenAvailable"):
            with self.subTest(setting=setting):
                self.assertIn(setting, source)

    def test_daily_task_resumes_on_sign_in_unlock_wake_and_a_retry_interval(self):
        source = (SCRIPTS / "install-daily-task.ps1").read_text(encoding="utf-8")
        for fragment in (
            "-AtLogOn",
            "MSFT_TaskSessionStateChangeTrigger",
            "StateChange = [uint32]8",
            "Microsoft-Windows-Power-Troubleshooter",
            "-RepetitionInterval",
            "-Scheduled",
            "-MultipleInstances IgnoreNew",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, source)

    def test_outreach_task_runs_twice_weekly_and_catches_up_missed_runs(self):
        source = (SCRIPTS / "install-outreach-task.ps1").read_text(encoding="utf-8")
        for fragment in (
            "-Weekly", "'Monday', 'Thursday'", "-StartWhenAvailable", "-AllowStartIfOnBatteries",
            "-DontStopIfGoingOnBatteries", "-MultipleInstances IgnoreNew", "-Scheduled",
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, source)

    def test_outreach_launcher_forwards_its_arguments(self):
        source = (SCRIPTS / "run-outreach-discovery.vbs").read_text(encoding="utf-8")
        self.assertIn("WScript.Arguments", source)

    def test_daily_launcher_forwards_its_arguments(self):
        source = (SCRIPTS / "run-daily.vbs").read_text(encoding="utf-8")
        self.assertIn("WScript.Arguments", source)

    def test_docs_never_register_powershell_directly(self):
        for path in (REPO_ROOT / "README.md", SCRIPTS / "run-daily.ps1"):
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertIsNone(
                    re.search(r"New-ScheduledTaskAction\s+-Execute\s+'powershell\.exe'", text),
                    "registering powershell.exe directly brings the console window back",
                )


def visible_windows() -> dict[int, str]:
    """Visible top-level windows on this desktop, keyed by handle, valued by process image name."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    def process_name(pid: int) -> str:
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return ""
        try:
            buffer = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(len(buffer))
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return ""
            return Path(buffer.value).name.lower()
        finally:
            kernel32.CloseHandle(handle)

    found: dict[int, str] = {}
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def collect(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            found[int(hwnd)] = process_name(pid.value)
        return True

    user32.EnumWindows(callback_type(collect), 0)
    return found


def run_watching_for_consoles(command: list[str]) -> tuple[subprocess.Popen, dict[int, str]]:
    """Run to completion, collecting any console-host window that appears meanwhile."""
    before = visible_windows()
    process = subprocess.Popen(command)
    appeared: dict[int, str] = {}
    while process.poll() is None:
        for hwnd, name in visible_windows().items():
            if hwnd not in before and name in CONSOLE_PROCESSES:
                appeared[hwnd] = name
        time.sleep(0.1)
    return process, appeared


@unittest.skipUnless(sys.platform == "win32", "window visibility is Windows-specific")
class DailyLauncherHeadlessTests(unittest.TestCase):
    def test_daily_launcher_shows_no_window_and_passes_exit_code_through(self):
        with tempfile.TemporaryDirectory() as temp:
            # The launcher resolves run-daily.ps1 next to itself, so a copy beside
            # a stub exercises the real launcher without running the pipeline.
            shutil.copy(SCRIPTS / "run-daily.vbs", temp)
            marker = Path(temp) / "ran.txt"
            (Path(temp) / "run-daily.ps1").write_text(
                f"Start-Sleep -Seconds 4\nSet-Content -LiteralPath '{marker}' -Value ok\nexit 7\n",
                encoding="utf-8",
            )
            wscript = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "wscript.exe"
            process, appeared = run_watching_for_consoles(
                [str(wscript), "//nologo", str(Path(temp) / "run-daily.vbs")]
            )

            self.assertEqual(appeared, {}, "the daily launcher must not show any console window")
            self.assertEqual(process.returncode, 7, "the script's exit code must reach Task Scheduler")
            self.assertTrue(marker.exists(), "the stub script never ran")


@unittest.skipUnless(sys.platform == "win32", "run-daily.ps1 targets Windows PowerShell")
class RunDailyScriptTests(unittest.TestCase):
    def test_stderr_output_does_not_abort_the_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "scripts").mkdir()
            shutil.copy(SCRIPTS / "run-daily.ps1", root / "scripts")
            shutil.copy(SCRIPTS / "run-daily.vbs", root / "scripts")
            # Stands in for pipeline.py: warns on stderr mid-run and prints the
            # ellipsis the real fetch output uses. Every command succeeds --
            # this test is about stderr not aborting the script and no console
            # window appearing, so every downstream step has to run. A *failing*
            # fetch deliberately stops the run now and is covered separately by
            # ResumableDailyRunTests; exit codes from later steps are still
            # exercised below by the migrate and purge stubs.
            (root / "pipeline.py").write_text(
                "import sys, time\n"
                "command = sys.argv[1]\n"
                "time.sleep(1)  # long enough for a stray console window to be seen\n"
                "print(f'{command} first line \\u2026', flush=True)\n"
                "sys.stderr.write(f'{command} warning on stderr\\n'); sys.stderr.flush()\n"
                "print(f'{command} after warning', flush=True)\n"
                "sys.exit(0)\n",
                encoding="utf-8",
            )
            # The platform purge only runs when data/platform.db exists; the stub
            # module stands in for opportunity_app.purge and fails to prove a
            # non-zero exit is logged without changing the run's exit code.
            (root / "data").mkdir()
            (root / "data" / "platform.db").write_bytes(b"")
            (root / "opportunity_app").mkdir()
            (root / "opportunity_app" / "__init__.py").write_text("", encoding="utf-8")
            (root / "opportunity_app" / "purge.py").write_text(
                "import sys\nprint('platform purge ran', flush=True)\nsys.exit(4)\n",
                encoding="utf-8",
            )
            (root / "opportunity_app" / "migrate.py").write_text(
                "import sys\nprint('platform sync ran', flush=True)\nsys.exit(1)\n",
                encoding="utf-8",
            )
            wscript = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "wscript.exe"
            # Watched end to end: every Python step the real script starts,
            # purges included, must inherit the hidden console, not open its own.
            completed, appeared = run_watching_for_consoles(
                [str(wscript), "//nologo", str(root / "scripts" / "run-daily.vbs")]
            )
            log = (root / "data" / "run.log").read_text(encoding="utf-8-sig")

        self.assertEqual(appeared, {}, "no step of the daily run may show a console window")
        self.assertEqual(completed.returncode, 0, log)
        for expected in (
            "run start",
            "run first line \u2026",
            "run warning on stderr",
            "run after warning",
            "--- liveness (limit 40) ---",
            "liveness warning on stderr",
            "liveness after warning",
            "--- purge expired ---",
            "purge-expired after warning",
            "--- sync platform database ---",
            "platform sync ran",
            "--- platform sync exited 1 ---",
            "platform purge ran",
            "--- platform purge exited 4 ---",
            "run end (exit 0)",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, log)


# Stands in for pipeline.py and the migrate and purge modules: records every call,
# then exits with the next code queued for that step in behaviour.json. A step
# queued with "hang" writes a marker and sleeps, so a test can kill it mid-step
# the way a shutdown or dead battery would.
RECORDING_STUB = r"""
import json, sys, time
from pathlib import Path
root = Path(__file__).resolve().parent
while not (root / "data").is_dir():
    root = root.parent
step = {"purge.py": "platform-purge", "migrate.py": "platform-sync", "outreach_cli.py": "outreach-remind"}.get(Path(__file__).name) or sys.argv[1]
with open(root / "data" / "calls.txt", "a", encoding="utf-8") as calls:
    calls.write(json.dumps([step, sys.argv[1:]]) + "\n")
behaviour_path = root / "behaviour.json"
behaviour = json.loads(behaviour_path.read_text(encoding="utf-8")) if behaviour_path.exists() else {}
queued = behaviour.get(step, [])
outcome = queued.pop(0) if queued else 0
behaviour_path.write_text(json.dumps(behaviour), encoding="utf-8")
print(f"{step} ran", flush=True)
if outcome == "hang":
    (root / "data" / "hanging.txt").write_text(step, encoding="utf-8")
    time.sleep(120)
sys.exit(outcome)
"""


@unittest.skipUnless(sys.platform == "win32", "run-daily.ps1 targets Windows PowerShell")
class ResumableDailyRunTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "scripts").mkdir()
        shutil.copy(SCRIPTS / "run-daily.ps1", self.root / "scripts")
        shutil.copy(SCRIPTS / "run-daily.vbs", self.root / "scripts")
        (self.root / "data").mkdir()
        (self.root / "data" / "platform.db").write_bytes(b"")
        (self.root / "pipeline.py").write_text(RECORDING_STUB, encoding="utf-8")
        (self.root / "opportunity_app").mkdir()
        (self.root / "opportunity_app" / "__init__.py").write_text("", encoding="utf-8")
        (self.root / "opportunity_app" / "purge.py").write_text(RECORDING_STUB, encoding="utf-8")
        (self.root / "opportunity_app" / "migrate.py").write_text(RECORDING_STUB, encoding="utf-8")
        (self.root / "opportunity_app" / "outreach_cli.py").write_text(RECORDING_STUB, encoding="utf-8")
        self.wscript = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "wscript.exe"

    def behave(self, **steps):
        (self.root / "behaviour.json").write_text(json.dumps(steps), encoding="utf-8")

    def command(self, *arguments: str) -> list[str]:
        return [str(self.wscript), "//nologo", str(self.root / "scripts" / "run-daily.vbs"), *arguments]

    def launch(self, *arguments: str) -> int:
        return subprocess.run(self.command(*arguments), timeout=180).returncode

    def calls(self) -> list[list]:
        path = self.root / "data" / "calls.txt"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def steps(self) -> list[str]:
        return [step for step, _ in self.calls()]

    def state(self) -> dict:
        return json.loads((self.root / "data" / "daily-run.json").read_text(encoding="utf-8-sig"))

    def log(self) -> str:
        return (self.root / "data" / "run.log").read_text(encoding="utf-8-sig")

    def test_a_complete_run_is_checkpointed_as_finished(self):
        self.assertEqual(self.launch(), 0)
        self.assertEqual(self.steps(), ["run", "liveness", "purge-expired", "platform-sync", "platform-purge", "outreach-remind"])
        state = self.state()
        self.assertEqual(state["completed"], ["run", "liveness", "purge-expired", "platform-sync", "platform-purge", "outreach-remind"])
        self.assertTrue(state["finishedAt"])
        self.assertEqual(state["attempts"], 1)
        run_arguments = self.calls()[0][1]
        self.assertEqual(run_arguments, ["run", "--resume-since", state["startedAt"]])

    def test_a_run_killed_mid_step_resumes_from_that_step(self):
        self.behave(liveness=["hang"])
        process = subprocess.Popen(self.command())
        hanging = self.root / "data" / "hanging.txt"
        deadline = time.monotonic() + 60
        while not hanging.exists():
            self.assertLess(time.monotonic(), deadline, "the liveness stub never started")
            time.sleep(0.2)
        # What a shutdown or a dead battery does: the whole tree dies at once.
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
        process.wait(timeout=30)
        interrupted = self.state()
        self.assertEqual(interrupted["completed"], ["run"])
        self.assertFalse(interrupted["finishedAt"])

        self.assertEqual(self.launch("-Scheduled"), 0)
        self.assertEqual(self.steps(), ["run", "liveness", "liveness", "purge-expired", "platform-sync", "platform-purge", "outreach-remind"])
        resumed = self.state()
        self.assertEqual(resumed["startedAt"], interrupted["startedAt"], "resuming must not start a new run")
        self.assertEqual(resumed["attempts"], 2)
        self.assertTrue(resumed["finishedAt"])
        self.assertIn("run resume (attempt 2", self.log())

    def test_a_fetch_with_unreachable_sources_pauses_and_retries_only_the_fetch(self):
        self.behave(run=[75, 0])
        self.assertEqual(self.launch(), 75)
        self.assertEqual(self.steps(), ["run"], "nothing after the fetch runs while it is incomplete")
        paused = self.state()
        self.assertFalse(paused["finishedAt"])
        self.assertIn("run paused", self.log())

        self.assertEqual(self.launch("-Scheduled"), 0)
        self.assertEqual(self.steps(), ["run", "run", "liveness", "purge-expired", "platform-sync", "platform-purge", "outreach-remind"])
        first, second = self.calls()[0][1], self.calls()[1][1]
        self.assertEqual(first, second, "the retry resumes the same run, skipping fetched sources")
        self.assertTrue(self.state()["finishedAt"])

    def test_a_fatal_fetch_failure_stops_the_run_instead_of_recording_a_finished_day(self):
        """Exit 1 is not exit 75, and must not be treated like a good day.

        A fatal failure means the database is unusable, not that an employer is
        down. Only exit 75 used to be handled, so any other non-zero exit
        completed the fetch step, ran the sync and purge on top of an aborted
        fetch, and set finishedAt -- after which -Scheduled skipped the rest of
        the day. Nothing downstream may run, and the day must stay resumable.
        """

        self.behave(run=[1, 0])
        self.assertEqual(self.launch(), 1)
        self.assertEqual(self.steps(), ["run"], "downstream steps ran on top of an aborted fetch")
        failed = self.state()
        self.assertFalse(failed["finishedAt"], "a fatal failure was recorded as a finished day")
        self.assertNotIn("run", failed["completed"], "the aborted fetch was marked complete")
        self.assertIn("run failed (exit 1)", self.log())

        # The next trigger resumes the same run rather than skipping the day.
        self.assertEqual(self.launch("-Scheduled"), 0)
        self.assertEqual(
            self.steps(),
            ["run", "run", "liveness", "purge-expired", "platform-sync", "platform-purge", "outreach-remind"],
        )
        self.assertTrue(self.state()["finishedAt"])

    def test_a_fatal_failure_is_never_recorded_as_a_finished_day(self):
        """Not even on the last attempt.

        Setting finishedAt would send every later -Scheduled trigger down the
        same-day no-op path, silently skipping a day whose fetch aborted and
        whose sync never ran -- the exact state the fatal branch exists to
        prevent. The 20-hour stale-run window is what abandons it instead.
        """

        self.behave(run=[1, 1])
        self.assertEqual(self.launch("-MaxAttempts", "1"), 1)
        self.assertEqual(self.steps(), ["run"])
        self.assertFalse(
            self.state()["finishedAt"],
            "an aborted fetch was recorded as a finished day, so -Scheduled will skip it",
        )
        self.assertNotIn("run", self.state()["completed"])
        self.assertIn("left unfinished", self.log())

    def test_retries_give_up_after_max_attempts_and_keep_what_arrived(self):
        self.behave(run=[75, 75])
        self.assertEqual(self.launch("-MaxAttempts", "2"), 75)
        self.assertEqual(self.launch("-MaxAttempts", "2"), 75)
        self.assertEqual(self.steps(), ["run", "run", "liveness", "purge-expired", "platform-sync", "platform-purge", "outreach-remind"])
        self.assertTrue(self.state()["finishedAt"])
        self.assertIn("still unreachable after 2 attempts", self.log())

    def test_scheduled_starts_after_todays_run_are_silent_no_ops(self):
        self.assertEqual(self.launch(), 0)
        log_before = self.log()
        for _ in range(2):
            self.assertEqual(self.launch("-Scheduled"), 0)
        self.assertEqual(len(self.calls()), 6, "a finished day must not run again")
        self.assertEqual(self.log(), log_before, "no-op starts must not grow the log")

    def test_scheduled_start_before_the_daily_time_waits(self):
        self.assertEqual(self.launch("-Scheduled", "-NotBefore", "23:59:59"), 0)
        self.assertEqual(self.calls(), [])
        self.assertFalse((self.root / "data" / "daily-run.json").exists())

    def test_a_stale_unfinished_run_is_abandoned_for_a_fresh_one(self):
        (self.root / "data" / "daily-run.json").write_text(json.dumps({
            "runDate": "2000-01-01", "startedAt": "2000-01-01T08:00:00Z", "attempts": 1,
            "completed": ["run"], "finishedAt": None, "exitCode": 0,
        }), encoding="utf-8")
        self.assertEqual(self.launch(), 0)
        self.assertEqual(self.steps()[0], "run")
        self.assertNotEqual(self.state()["startedAt"], "2000-01-01T08:00:00Z")
        self.assertIn("abandoning unfinished run", self.log())


if __name__ == "__main__":
    unittest.main()
