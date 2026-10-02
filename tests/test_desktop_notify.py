"""Automation notices as desktop pop-ups: the command that shows one, and the pass that decides which to show."""

import base64
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from xml.sax.saxutils import unescape

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opportunity_app import automation, desktop_notify
from opportunity_app.mail.connections import update_preferences
from opportunity_app.outreach_automation import AutomationWorker
from opportunity_app.core.schema import LOCAL_USER_ID
from opportunity_app.core.database import connect_product

from helpers_platform import build_and_migrate

USER = LOCAL_USER_ID
# Quotes, PowerShell's $( ) and backticks, and XML's < and &: none may ever run or break the toast.
TITLE = """It's "done" $(Remove-Item x) `whoami` <b>&amp;"""
BODY = "Body $env:USERNAME 'x' \"y\" <z/>"


def decoded(value):
    return base64.b64decode(value).decode("utf-8")


class CommandTests(unittest.TestCase):
    def test_windows_passes_the_words_only_through_the_environment(self):
        argv, env = desktop_notify.command(TITLE, BODY, platform="win32")
        self.assertTrue(argv[0].lower().endswith("powershell.exe"))
        self.assertEqual(argv[1:4], ["-NoProfile", "-NonInteractive", "-Command"])
        self.assertEqual(len(argv), 5)
        script = argv[4]
        self.assertEqual(script, desktop_notify._WINDOWS_SCRIPT, "the script is the same fixed text whatever the words")
        for piece in ("done", "Remove-Item", "whoami", "USERNAME", "<b>", "<z/>"):
            self.assertNotIn(piece, script)
        self.assertNotIn('"', script, "one argument, so no double quotes to be re-quoted")
        self.assertNotIn("\n", script)
        self.assertIn("ToastText02", script)
        self.assertIn("ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime", script)
        self.assertEqual(env["PIPELINE_TOAST_APP"], desktop_notify.WINDOWS_APP_ID)
        title = decoded(env["PIPELINE_TOAST_TITLE"])
        self.assertNotIn("<", title, "escaped for the toast's XML")
        self.assertEqual(title, "It&apos;s &quot;done&quot; $(Remove-Item x) `whoami` &lt;b&gt;&amp;amp;")
        self.assertEqual(unescape(title, {"&quot;": '"', "&apos;": "'"}), TITLE)
        self.assertEqual(unescape(decoded(env["PIPELINE_TOAST_BODY"]), {"&quot;": '"', "&apos;": "'"}), BODY)

    def test_macos_and_linux_pass_the_words_as_plain_arguments(self):
        argv, env = desktop_notify.command(TITLE, BODY, platform="darwin")
        self.assertEqual(argv[0], "osascript")
        self.assertEqual(argv[-2:], [TITLE, BODY])
        scripts = argv[2:-2:2]
        self.assertEqual(argv[1:-2:2], ["-e"] * 3)
        self.assertIn("on run argv", scripts)
        self.assertTrue(all(TITLE not in script and BODY not in script for script in scripts))
        self.assertEqual(env, {})
        with mock.patch.object(desktop_notify.shutil, "which", return_value="/usr/bin/notify-send"):
            self.assertEqual(desktop_notify.command(TITLE, BODY, platform="linux"), (["/usr/bin/notify-send", "--", TITLE, BODY], {}))
        with mock.patch.object(desktop_notify.shutil, "which", return_value=None):
            self.assertIsNone(desktop_notify.command(TITLE, BODY, platform="linux"), "no notify-send, no pop-up")
        self.assertIsNone(desktop_notify.command(TITLE, BODY, platform="sunos5"))

    def test_show_runs_hidden_with_a_timeout_and_never_raises(self):
        with mock.patch.object(desktop_notify, "_platform", return_value="win32"), \
                mock.patch.object(desktop_notify.subprocess, "run", return_value=mock.Mock(returncode=0)) as run:
            self.assertTrue(desktop_notify.show(TITLE, BODY))
        (argv,), options = run.call_args
        self.assertEqual(argv[4], desktop_notify._WINDOWS_SCRIPT)
        self.assertEqual(options["timeout"], 10)
        self.assertEqual(options["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertIn("PIPELINE_TOAST_TITLE", options["env"])
        self.assertTrue(all(TITLE not in part for part in argv))
        for outcome in (mock.Mock(returncode=1), FileNotFoundError("powershell.exe"), subprocess.TimeoutExpired("powershell.exe", 10)):
            with self.subTest(outcome=type(outcome).__name__), mock.patch.object(desktop_notify, "_platform", return_value="win32"), \
                    mock.patch.object(desktop_notify.subprocess, "run", **(
                        {"side_effect": outcome} if isinstance(outcome, BaseException) else {"return_value": outcome})):
                self.assertFalse(desktop_notify.show(TITLE, BODY))
        with mock.patch.object(desktop_notify, "_platform", return_value="sunos5"), \
                mock.patch.object(desktop_notify.subprocess, "run") as run:
            self.assertFalse(desktop_notify.show(TITLE, BODY))
        run.assert_not_called()


class Notifier:
    """A desktop that records every pop-up it is asked for, and shows them (or not)."""

    def __init__(self, result=True, error=None):
        self.result, self.error, self.calls = result, error, []

    def __call__(self, title, body):
        self.calls.append((title, body))
        if self.error:
            raise self.error
        return self.result


class DeliveryTests(unittest.TestCase):
    RUN_AT = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        _, self.platform_path = build_and_migrate(Path(self.tempdir.name))
        self.conn = connect_product(self.platform_path)
        update_preferences(self.conn, {"timezone": "UTC", "quiet_start": "22:00", "quiet_end": "07:00"}, user_id=USER)
        desktop_notify._FAILED.clear()
        self.addCleanup(desktop_notify._FAILED.clear)

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def notice(self, key, title="Turned off Write drafts automatically", body="Turn it back on under Automation.", *, age=timedelta(hours=1)):
        automation.notice(self.conn, USER, event_key=key, level="warning", title=title, body=body)
        with self.conn:
            self.conn.execute(
                "UPDATE automation_notices SET created_at=? WHERE event_key=?",
                ((self.RUN_AT - age).isoformat(timespec="microseconds"), key),
            )

    def shown_at(self, key):
        return self.conn.execute("SELECT desktop_at FROM automation_notices WHERE event_key=?", (key,)).fetchone()[0]

    def deliver(self, notifier, now=None):
        return desktop_notify.deliver_desktop_notices(self.conn, notifier=notifier, now=now or self.RUN_AT)

    def turn_on(self, *, ago=timedelta(days=3)):
        """Turn pop-ups on, as if the student did it ``ago`` before RUN_AT: notices made before then never pop up."""
        automation.set_mode(self.conn, USER, "desktop_notifications", "on")
        with self.conn:
            self.conn.execute(
                "UPDATE user_settings SET updated_at=? WHERE user_id=? AND key='desktop_notifications'",
                ((self.RUN_AT - ago).isoformat(timespec="microseconds"), USER),
            )

    def test_nothing_pops_up_while_the_switch_is_off(self):
        self.notice("one")
        notifier = Notifier()
        self.assertEqual(self.deliver(notifier), 0)
        automation.set_mode(self.conn, USER, "desktop_notifications", "off")
        self.assertEqual(self.deliver(notifier), 0)
        self.assertEqual(notifier.calls, [])
        self.assertIsNone(self.shown_at("one"))

    def test_a_pause_does_not_hold_pop_ups_back(self):
        # Pause stops what the app does on its own; a notice such as this one only informs.
        self.turn_on()
        automation.set_paused(self.conn, USER, True)
        self.notice("gmail", title="Gmail needs reconnecting", body="Reply and bounce checks have stopped.")
        notifier = Notifier()
        self.assertEqual(self.deliver(notifier), 1)
        self.assertEqual(notifier.calls, [("Gmail needs reconnecting", "Reply and bounce checks have stopped.")])
        self.assertIsNotNone(self.shown_at("gmail"))

    def test_a_notice_already_read_in_the_app_never_pops_up(self):
        self.turn_on()
        self.notice("read")
        self.notice("unread", title="Unread")
        read_id = self.conn.execute("SELECT id FROM automation_notices WHERE event_key='read'").fetchone()[0]
        self.assertEqual(automation.mark_notices_read(self.conn, USER, [read_id]), 1)
        notifier = Notifier()
        self.assertEqual(self.deliver(notifier), 1)
        self.assertEqual([title for title, _body in notifier.calls], ["Unread"])
        self.assertIsNone(self.shown_at("read"), "left as it is, in the app only")

    def test_turning_pop_ups_on_brings_back_no_backlog(self):
        self.notice("before", title="Before", age=timedelta(hours=5))
        self.turn_on(ago=timedelta(hours=3))
        self.notice("after", title="After", age=timedelta(hours=1))
        notifier = Notifier()
        self.assertEqual(self.deliver(notifier), 1)
        self.assertEqual([title for title, _body in notifier.calls], ["After"])
        self.assertIsNone(self.shown_at("before"), "made before the switch was turned on, so it stays in the app only")
        # Off and on again later: what came in while it was off stays in the app too.
        automation.set_mode(self.conn, USER, "desktop_notifications", "off")
        self.notice("while-off", title="While off", age=timedelta(minutes=30))
        self.turn_on(ago=timedelta(minutes=10))
        self.assertEqual(self.deliver(notifier), 0)
        self.assertEqual(len(notifier.calls), 1)

    def test_each_notice_pops_up_once_with_its_title_and_body(self):
        self.turn_on()
        self.notice("first", title="First", body="First body", age=timedelta(hours=2))
        self.notice("second", title="Second", body="", age=timedelta(hours=1))
        notifier = Notifier()
        self.assertEqual(self.deliver(notifier), 2)
        self.assertEqual(notifier.calls, [("First", "First body"), ("Second", "")])
        self.assertEqual(self.shown_at("first"), self.RUN_AT.isoformat(timespec="microseconds"))
        self.assertIsNotNone(self.shown_at("second"))
        self.assertEqual(self.deliver(notifier), 0)
        self.assertEqual(len(notifier.calls), 2)

    def test_quiet_hours_hold_notices_until_they_end_even_across_midnight(self):
        self.turn_on()
        self.notice("late")
        notifier = Notifier()
        for held in (datetime(2026, 9, 27, 23, 30, tzinfo=timezone.utc), datetime(2026, 9, 28, 6, 45, tzinfo=timezone.utc)):
            with self.subTest(at=held.isoformat()):
                self.assertEqual(self.deliver(notifier, now=held), 0)
                self.assertIsNone(self.shown_at("late"))
        self.assertEqual(self.deliver(notifier, now=datetime(2026, 9, 28, 7, 5, tzinfo=timezone.utc)), 1)
        update_preferences(self.conn, {"quiet_start": "11:00", "quiet_end": "13:00"}, user_id=USER)
        self.notice("noon")
        self.assertEqual(self.deliver(notifier), 0, "a window that does not cross midnight holds too")
        self.assertEqual(notifier.calls, [("Turned off Write drafts automatically", "Turn it back on under Automation.")])

    def test_notices_older_than_two_days_never_pop_up(self):
        self.turn_on()
        self.notice("old", age=timedelta(days=2, minutes=1))
        self.notice("recent", age=timedelta(days=1, hours=23))
        notifier = Notifier()
        self.assertEqual(self.deliver(notifier), 1)
        self.assertEqual(len(notifier.calls), 1)
        self.assertIsNone(self.shown_at("old"), "left as it is, in the app only")
        self.assertIsNotNone(self.shown_at("recent"))

    def test_a_notice_that_will_not_show_is_given_up_after_three_passes(self):
        self.turn_on()
        self.notice("stuck")
        for notifier in (Notifier(result=False), Notifier(error=OSError("no desktop session"))):
            with self.subTest(notifier="refuses" if notifier.error is None else "raises"):
                with self.conn:
                    self.conn.execute("UPDATE automation_notices SET desktop_at=NULL")
                for _ in range(2):
                    self.assertEqual(self.deliver(notifier), 0)
                    self.assertIsNone(self.shown_at("stuck"), "tried again next pass")
                with self.assertLogs(desktop_notify.LOGGER, "WARNING") as logged:
                    self.assertEqual(self.deliver(notifier), 0)
                self.assertEqual(len(logged.records), 1)
                self.assertIsNotNone(self.shown_at("stuck"), "marked so it never loops")
                self.deliver(notifier)
                self.assertEqual(len(notifier.calls), 3)

    def test_the_pop_up_carries_no_link_or_address_and_stays_short(self):
        self.turn_on()
        self.notice("leaky", title="Reply from greg@bovi.example", body="See https://mail.example/thread/1 now. " + "word " * 100)
        notifier = Notifier()
        self.deliver(notifier)
        [(title, body)] = notifier.calls
        self.assertEqual(title, "Reply from [address]")
        self.assertTrue(body.startswith("See [link] now. word"))
        self.assertLessEqual(len(body), desktop_notify.MAX_TEXT)
        self.assertNotIn("@", title + body)
        self.assertNotIn("http", body)

    def test_the_automation_worker_shows_them_first_and_a_failure_never_stops_it(self):
        update_preferences(self.conn, {"quiet_start": "00:00", "quiet_end": "00:00"}, user_id=USER)
        self.turn_on()
        automation.notice(self.conn, USER, event_key="now", level="info", title="Now", body="Body")
        worker = AutomationWorker(self.platform_path, fetcher_factory=lambda: None)
        with mock.patch.object(desktop_notify, "show", return_value=True) as show:
            report = worker.run_once()
        show.assert_called_once_with("Now", "Body")
        self.assertEqual(report, {"sent": [], "recovered": [], "drafted": [], "forms": []})
        with closing(connect_product(self.platform_path)) as conn:
            self.assertIsNotNone(conn.execute("SELECT desktop_at FROM automation_notices WHERE event_key='now'").fetchone()[0])
        with mock.patch.object(desktop_notify, "deliver_desktop_notices", side_effect=RuntimeError("no desktop")), \
                self.assertLogs("opportunity_app.outreach_automation", "ERROR"):
            self.assertEqual(worker.run_once(), {"sent": [], "recovered": [], "drafted": [], "forms": []})


if __name__ == "__main__":
    unittest.main()
