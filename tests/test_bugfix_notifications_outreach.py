"""Regression tests for the 2026-09-21 notification and outreach fixes (PLAN items 9, 10, 13-16)."""

import json
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opportunity_app import notifications as notif
from opportunity_app import outreach
from opportunity_app.connections import update_preferences
from opportunity_app.outreach import create_target, get_target, list_targets, location_region, user_regions
from opportunity_app.outreach_drafting import INSTRUCTIONS, location_line, validate_draft
from opportunity_app.schema import LOCAL_USER_ID, connect_product, ensure_product_schema, utc_now

from helpers_platform import build_and_migrate, use_profile_regions


class LiveProvider:
    live = True

    def __init__(self):
        self.calls = []

    def deliver(self, channel, recipient, subject, body):
        self.calls.append({"channel": channel, "recipient": recipient})
        return {"delivered": True, "detail": "fake"}


class _DbCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        _, self.platform_path = build_and_migrate(Path(self.tempdir.name))

    def env(self, **values):
        patcher = mock.patch.dict("os.environ", values)
        patcher.start()
        self.addCleanup(patcher.stop)


class ReminderInstantTests(_DbCase):
    """Item 9: due_at carries the student's offset; compare instants, not strings."""

    def setUp(self):
        super().setUp()
        self.env(PIPELINE_TIMEZONE="UTC")

    def _fire(self, due_at, now):
        with closing(connect_product(self.platform_path)) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO reminders(id, application_id, user_id, reminder_type, due_at, timezone, status, created_at, updated_at)"
                    " VALUES('r1', 'app-job-b', ?, 'follow_up', ?, 'UTC', 'scheduled', ?, ?)",
                    (LOCAL_USER_ID, due_at, utc_now(), utc_now()),
                )
            stats = notif.send_due_reminders(conn, provider=notif.SandboxProvider(), now=now)
            status = conn.execute("SELECT status FROM reminders WHERE id='r1'").fetchone()[0]
        return stats, status

    def test_chicago_morning_is_not_due_before_it_happens(self):
        # 09:00 in Chicago is 14:00 UTC; at 10:00 UTC it is still four hours away.
        stats, status = self._fire("2026-09-22T09:00:00-05:00", datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(stats["reminders_fired"], 0)
        self.assertEqual(status, "scheduled")

    def test_tokyo_morning_is_due_once_it_has_passed(self):
        # 09:00 in Tokyo is 00:00 UTC; at 05:00 UTC it is five hours overdue.
        stats, status = self._fire("2026-09-22T09:00:00+09:00", datetime(2026, 9, 22, 5, 0, tzinfo=timezone.utc))
        self.assertEqual(stats["reminders_fired"], 1)
        self.assertEqual(status, "completed")

    def test_a_naive_due_at_is_read_as_utc(self):
        stats, _ = self._fire("2026-09-22T04:00:00", datetime(2026, 9, 22, 5, 0, tzinfo=timezone.utc))
        self.assertEqual(stats["reminders_fired"], 1)


class QuietHoursResolverTests(_DbCase):
    """Item 10: quiet hours use the same timezone resolver as Urgent, not the 'UTC' column default."""

    def test_quiet_hours_follow_the_resolved_timezone(self):
        # No explicit preference; PIPELINE_TIMEZONE says Chicago. 10:00 UTC is
        # 05:00 in Chicago, inside the default 22:00-08:00 window, though it is
        # outside that window in UTC.
        self.env(PIPELINE_TIMEZONE="America/Chicago")
        with closing(connect_product(self.platform_path)) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO reminders(id, application_id, user_id, reminder_type, due_at, timezone, status, created_at, updated_at)"
                    " VALUES('r1', 'app-job-b', ?, 'follow_up', '2026-09-22T09:00:00+00:00', 'UTC', 'scheduled', ?, ?)",
                    (LOCAL_USER_ID, utc_now(), utc_now()),
                )
            stats = notif.send_due_reminders(
                conn, provider=notif.SandboxProvider(), now=datetime(2026, 9, 22, 10, 0, tzinfo=timezone.utc),
            )
        self.assertEqual(stats["held_quiet_hours"], 1)
        self.assertEqual(stats["messages_delivered"], 0)


class ReminderEmailRecipientTests(_DbCase):
    """Item 13: no email delivery attempt for a user with no email address."""

    def test_no_address_means_no_email_delivery(self):
        self.env(PIPELINE_TIMEZONE="UTC")
        provider = LiveProvider()
        with closing(connect_product(self.platform_path)) as conn:
            update_preferences(conn, {"email_enabled": True}, user_id=LOCAL_USER_ID)
            with conn:
                conn.execute("UPDATE users SET email=NULL WHERE id=?", (LOCAL_USER_ID,))
                conn.execute(
                    "INSERT INTO reminders(id, application_id, user_id, reminder_type, due_at, timezone, status, created_at, updated_at)"
                    " VALUES('r1', 'app-job-b', ?, 'follow_up', '2026-09-22T09:00:00+00:00', 'UTC', 'scheduled', ?, ?)",
                    (LOCAL_USER_ID, utc_now(), utc_now()),
                )
            stats = notif.send_due_reminders(conn, provider=provider, now=datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(stats["reminders_fired"], 1)
        self.assertEqual([call for call in provider.calls if call["channel"] == "email"], [])


NORCAL = [{
    "name": "NorCal", "phrase": "Northern California",
    "state_markers": ["ca"], "aliases": ["northern california"], "places": ["oakland"],
}]


class LocationPhraseValidationTests(unittest.TestCase):
    """Item 14: location_line writes the region's phrase; validation must accept it."""

    def test_a_phrase_that_omits_the_region_name_passes(self):
        with mock.patch.object(outreach, "_profile_regions", return_value=NORCAL):
            facts = {"break_location": "Oakland, CA", "school": "Somewhere U"}
            target = {"location": "Oakland, CA", "location_basis": "company_site"}
            line = location_line(facts, target)
            self.assertEqual(line, "I'm based in Northern California during breaks and summers.")
            body = "Hi Sam,\n\nI'm a student at Somewhere U. " + line + "\n\nThanks,\nMe"
            raw = json.dumps({"subject": "Hello", "body": body, "claims": [{"text": "x", "basis": "profile:school"}]})
            inputs = {
                "student": facts, "company_research": {}, "unverified_research": {},
                "source_urls": [], "location_line": line, "primary_experience": "",
            }
            _, problems = validate_draft(raw, inputs, "initial")
        self.assertFalse([p for p in problems if "location_line" in p], problems)

    def test_a_missing_location_is_still_sent_back(self):
        with mock.patch.object(outreach, "_profile_regions", return_value=NORCAL):
            body = "Hi Sam,\n\nI'm a student at Somewhere U.\n\nThanks,\nMe"
            raw = json.dumps({"subject": "Hello", "body": body, "claims": [{"text": "x", "basis": "profile:school"}]})
            inputs = {
                "student": {"break_location": "Oakland, CA", "school": "Somewhere U"}, "company_research": {},
                "unverified_research": {}, "source_urls": [], "primary_experience": "",
                "location_line": "I'm based in Northern California during breaks and summers.",
            }
            _, problems = validate_draft(raw, inputs, "initial")
        self.assertTrue(any("leaves out location_line" in p for p in problems), problems)


class DraftingPromptNeutralityTests(unittest.TestCase):
    """Item 15: the drafting prompt presumes no particular discipline."""

    def test_no_hardware_only_contribution_instruction(self):
        lowered = INSTRUCTIONS.casefold()
        for phrase in ("physical verbs", "fusion 360", "soldering", "flight testing", "machine, print, wire"):
            self.assertNotIn(phrase, lowered)

    def test_contribution_verbs_come_from_the_students_own_work(self):
        self.assertIn("primary experience's entry and the student's skills", INSTRUCTIONS)


class PerUserRegionTests(_DbCase):
    """Item 16: another account never inherits the owner's config/profile.json regions."""

    OTHER = "student-b"

    def setUp(self):
        super().setUp()
        use_profile_regions(self)
        self.conn = connect_product(self.platform_path)
        self.addCleanup(self.conn.close)
        ensure_product_schema(self.conn)
        timestamp = utc_now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO users(id, email, display_name, role, created_at, updated_at) VALUES(?, 'b@example.com', 'B', 'student', ?, ?)",
                (self.OTHER, timestamp, timestamp),
            )
            self.conn.execute(
                "INSERT INTO profile_facts(user_id, field_path, value_json, source, confirmed, created_at, updated_at)"
                " VALUES(?, 'regions', ?, 'user', 1, ?, ?)",
                (self.OTHER, json.dumps([{"name": "Front Range", "phrase": "Colorado's Front Range",
                                          "state_markers": ["co"], "places": ["boulder", "denver"]}]), timestamp, timestamp),
            )

    def _target(self, user_id, company, location):
        return create_target(self.conn, {"company": company, "location": location}, user_id=user_id)

    def test_the_other_account_uses_its_own_confirmed_regions(self):
        mine = self._target(self.OTHER, "Peak", "Boulder, CO")
        self.assertEqual(get_target(self.conn, mine["id"], user_id=self.OTHER)["location_region"], "Front Range")
        owners_metro = self._target(self.OTHER, "Harbor", "Oakland, CA")
        self.assertEqual(get_target(self.conn, owners_metro["id"], user_id=self.OTHER)["location_region"], "")
        listed = {item["company"]: item["location_region"] for item in list_targets(self.conn, user_id=self.OTHER)}
        self.assertEqual(listed, {"Peak": "Front Range", "Harbor": ""})

    def test_the_owner_still_uses_the_profile_file(self):
        owned = self._target(LOCAL_USER_ID, "Harbor", "Oakland, CA")
        self.assertEqual(get_target(self.conn, owned["id"], user_id=LOCAL_USER_ID)["location_region"], "Bay Area")

    def test_location_line_for_the_other_account(self):
        regions = user_regions(self.conn, self.OTHER)
        facts = {"break_location": "Denver, CO", "school": "Somewhere U"}
        target = {"location": "Boulder, CO", "location_basis": "manual"}
        self.assertEqual(
            location_line(facts, target, regions), "I'm based in Colorado's Front Range during breaks and summers.",
        )
        self.assertEqual(location_region("Oakland, CA", regions), "")

    def test_the_other_account_gets_the_line_for_its_own_home_city(self):
        regions = user_regions(self.conn, self.OTHER)
        facts = {"break_location": "Houston, TX", "school": "Somewhere U"}
        self.assertEqual(
            location_line(facts, {"location": "Houston, TX", "location_basis": "company_site"}, regions),
            "I'm based in Houston during breaks and summers.",
        )
        self.assertEqual(location_line(facts, {"location": "Oakland, CA", "location_basis": "company_site"}, regions), "")


if __name__ == "__main__":
    unittest.main()
