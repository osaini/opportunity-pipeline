"""The cross-platform launcher's scheduling files and log redaction."""

import plistlib
import tempfile
import unittest
from pathlib import Path

from opportunity_app import launch


class SchedulingFileTests(unittest.TestCase):
    ROOT = Path("/Users/student/My Pipeline")

    def test_launchd_agents_are_valid_plists_for_each_job(self):
        for job in ("autostart", "daily", "outreach"):
            with self.subTest(job=job):
                command = ["/Users/student/My Pipeline/.venv/bin/python", "-m", "opportunity_app.daily"]
                parsed = plistlib.loads(launch.launchd_plist(job, command, self.ROOT).encode())
                self.assertEqual(parsed["Label"], f"com.opportunity-pipeline.{job}")
                self.assertEqual(parsed["ProgramArguments"], command)
                self.assertEqual(parsed["WorkingDirectory"], str(self.ROOT))
        autostart = plistlib.loads(launch.launchd_plist("autostart", ["python"], self.ROOT).encode())
        self.assertTrue(autostart["KeepAlive"])
        daily = plistlib.loads(launch.launchd_plist("daily", ["python"], self.ROOT).encode())
        self.assertEqual(daily["StartInterval"], 1800)
        outreach = plistlib.loads(launch.launchd_plist("outreach", ["python"], self.ROOT).encode())
        self.assertEqual([entry["Weekday"] for entry in outreach["StartCalendarInterval"]], [1, 4])

    def test_a_path_with_markup_characters_is_escaped(self):
        root = Path("/Users/a&b/<pipeline>")
        parsed = plistlib.loads(launch.launchd_plist("daily", ["python"], root).encode())
        self.assertEqual(parsed["WorkingDirectory"], str(root))

    def test_systemd_units(self):
        service = launch.systemd_units("autostart", ["python", "-m", "opportunity_app.launch", "serve"], self.ROOT)
        self.assertEqual(list(service), ["opportunity-pipeline-autostart.service"])
        self.assertIn("Restart=on-failure", service["opportunity-pipeline-autostart.service"])
        daily = launch.systemd_units("daily", ["/opt/my env/python", "-m", "opportunity_app.daily"], self.ROOT)
        self.assertIn("OnCalendar=*:0/30", daily["opportunity-pipeline-daily.timer"])
        self.assertIn('ExecStart="/opt/my env/python" -m opportunity_app.daily', daily["opportunity-pipeline-daily.service"])
        outreach = launch.systemd_units("outreach", ["python"], self.ROOT)
        self.assertIn("OnCalendar=Mon,Thu 07:00", outreach["opportunity-pipeline-outreach.timer"])

    def test_every_job_runs_the_current_interpreter(self):
        for job in ("autostart", "daily", "outreach"):
            self.assertEqual(launch.job_command(job)[0], launch.sys.executable)
        self.assertIn("--scheduled", launch.job_command("daily"))


class RedactingLogTests(unittest.TestCase):
    def test_credential_lines_never_reach_the_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "web.log"
            stream = launch._RedactingLog(path)
            stream.write("Opportunity app: http://127.0.0.1:8765\nAccess token: s3cret\n")
            stream.write("Employer API token: e-s3cret\nINFO: started")
            stream.write("\nLocal web access token: s3cret\n")
            stream._handle.close()
            text = path.read_text(encoding="utf-8")
        self.assertIn("Opportunity app", text)
        self.assertIn("INFO: started", text)
        self.assertNotIn("s3cret", text)


if __name__ == "__main__":
    unittest.main()
