"""The Urgent action queue, student-entered deadlines, and the shared timezone rule."""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR
from opportunity_app import urgent
from opportunity_app.actions import add_application_task, application_analytics
from opportunity_app.api import create_app
from opportunity_app.operations import delete_account, export_account
from opportunity_app.outreach import local_today
from opportunity_app.purge import purge_expired_opportunities
from opportunity_app.schema import LOCAL_USER_ID, connect_product
from opportunity_app.student_agent import decide_proposal
from opportunity_app.user_time import SYSTEM_LOCAL, UserTimezone, user_timezone
from pipeline_core import OpportunityFilters, OpportunityRepository
from tests.helpers_platform import build_and_migrate

CHICAGO = "America/Chicago"
# Noon in Chicago on Thursday 2026-09-17.
NOW = datetime(2026, 9, 17, 17, 0, tzinfo=timezone.utc)
TODAY = date(2026, 9, 17)
STAMP = "2026-09-01T00:00:00+00:00"
OTHER = "student-b"


class UrgentFixture(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        _, self.platform_path = build_and_migrate(self.root)
        self.conn = connect_product(self.platform_path)
        self.conn.execute(
            "INSERT INTO users(id, email, display_name, role, created_at, updated_at) VALUES(?, ?, ?, 'student', ?, ?)",
            (OTHER, "b@example.com", "Student B", STAMP, STAMP),
        )
        # The shared fixture saves job-a (listed deadline Sept 1) and applies to
        # job-b (follow-up Aug 16); every test here seeds its own dated records.
        for statement in (
            "DELETE FROM reminders",
            "DELETE FROM application_events",
            "DELETE FROM application_tasks",
            "DELETE FROM applications",
            "DELETE FROM opportunity_interactions",
            "UPDATE opportunities SET deadline_at = NULL",
        ):
            self.conn.execute(statement)
        self.set_timezone(LOCAL_USER_ID, CHICAGO)
        self.set_timezone(OTHER, CHICAGO)
        self.conn.commit()
        self._outreach = 0
        env = mock.patch.dict(os.environ, {"PIPELINE_TIMEZONE": ""})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    # -- seeding helpers -------------------------------------------------

    def set_timezone(self, user_id: str, name: str | None) -> None:
        self.conn.execute("DELETE FROM notification_preferences WHERE user_id=?", (user_id,))
        if name:
            self.conn.execute(
                """INSERT INTO notification_preferences(user_id, timezone, timezone_explicit, updated_at)
                   VALUES(?, ?, 1, ?)""",
                (user_id, name, STAMP),
            )
        self.conn.commit()

    def application(self, opportunity_id: str, stage: str = "applying", *, user_id: str = LOCAL_USER_ID,
                    follow_up_at: str | None = None) -> str:
        application_id = f"app-{user_id}-{opportunity_id}"
        self.conn.execute(
            """INSERT INTO applications(id, opportunity_id, user_id, stage, follow_up_at, created_at, updated_at)
               VALUES(?, ?, ?, ?, ?, ?, ?)""",
            (application_id, opportunity_id, user_id, stage, follow_up_at, STAMP, STAMP),
        )
        self.conn.commit()
        return application_id

    def task(self, application_id: str, due_at: str, *, user_id: str = LOCAL_USER_ID, status: str = "open") -> str:
        task_id = f"task-{application_id}-{due_at}"
        self.conn.execute(
            """INSERT INTO application_tasks(id, application_id, user_id, title, due_at, status, created_at, updated_at)
               VALUES(?, ?, ?, 'Send transcript', ?, ?, ?, ?)""",
            (task_id, application_id, user_id, due_at, status, STAMP, STAMP),
        )
        self.conn.commit()
        return task_id

    def outreach(self, *, deadline: date | None = None, follow_up: date | None = None, status: str = "not_started",
                 user_id: str = LOCAL_USER_ID) -> str:
        self._outreach += 1
        target_id = f"outreach-{self._outreach}"
        self.conn.execute(
            """INSERT INTO outreach_targets(id, user_id, company, status, deadline_date, follow_up_at, created_at, updated_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?)""",
            (target_id, user_id, f"Startup {self._outreach}", status,
             deadline.isoformat() if deadline else None, follow_up.isoformat() if follow_up else None, STAMP, STAMP),
        )
        self.conn.commit()
        return target_id

    def listed_deadline(self, opportunity_id: str, value: str) -> None:
        self.conn.execute("UPDATE opportunities SET deadline_at=? WHERE id=?", (value, opportunity_id))
        self.conn.commit()

    def intent(self, opportunity_id: str, action: str, *, user_id: str = LOCAL_USER_ID) -> None:
        self.conn.execute(
            "INSERT INTO opportunity_interactions(opportunity_id, user_id, action, created_at) VALUES(?, ?, ?, ?)",
            (opportunity_id, user_id, action, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    def posting(self, opportunity_id: str, *, deadline: str | None = None) -> None:
        self.conn.execute(
            """INSERT INTO opportunities(id, company, title, location, region, role_type, url, description,
                   posted_at, deadline_at, first_seen_at, last_seen_at, active, fingerprint, content_fingerprint,
                   duplicate_of, created_at, updated_at)
               VALUES(?, 'Private Co', 'Captured role', '', '', 'internship', 'https://example.com/job', '',
                   NULL, ?, ?, ?, 1, ?, ?, NULL, ?, ?)""",
            (opportunity_id, deadline, STAMP, STAMP, opportunity_id, opportunity_id, STAMP, STAMP),
        )
        self.conn.commit()

    def capture(self, opportunity_id: str, *, owner: str, capture_id: str, deadline: str | None = None) -> None:
        """Seed a confirmed manual capture the way captures.py stores one."""
        self.posting(opportunity_id, deadline=deadline)
        self.conn.execute(
            """INSERT INTO opportunity_sources(opportunity_id, source_key, source_name, external_id, source_url,
                   first_seen_at, last_seen_at)
               VALUES(?, 'manual:capture', 'Manual capture', ?, 'https://example.com/job', ?, ?)""",
            (opportunity_id, capture_id, STAMP, STAMP),
        )
        application_id = self.application(opportunity_id, user_id=owner)
        self.conn.execute(
            """INSERT INTO opportunity_captures(id, user_id, source_type, source_url, status, application_id, created_at)
               VALUES(?, ?, 'url', 'https://example.com/job', 'confirmed', ?, ?)""",
            (capture_id, owner, application_id, STAMP),
        )
        self.conn.commit()

    def queue(self, *, user_id: str = LOCAL_USER_ID, days: int = 14, now: datetime = NOW) -> dict:
        return urgent.urgent_queue(self.conn, user_id=user_id, days=days, now=now)

    @staticmethod
    def keys(result: dict) -> list[str]:
        return [item["key"] for item in result["items"]]


class UrgentWindowTests(UrgentFixture):
    def test_window_is_exactly_days_long_and_overdue_reaches_back_sixty_days(self):
        offsets = [-61, -60, -59, -1, 0, 1, 6, 7, 13, 14]
        ids = {offset: self.outreach(deadline=TODAY + timedelta(days=offset)) for offset in offsets}
        result = self.queue()
        shown = {item["outreach_target_id"]: item for item in result["items"]}
        for offset in (-60, -59, -1, 0, 1, 6, 7, 13):
            self.assertIn(ids[offset], shown, f"offset {offset} should be in the queue")
            self.assertEqual(shown[ids[offset]]["days_until"], offset)
            self.assertEqual(shown[ids[offset]]["overdue"], offset < 0)
        self.assertNotIn(ids[14], shown, "today + 14 is outside a 14-day window")
        self.assertNotIn(ids[-61], shown)
        self.assertEqual(result["older_overdue"], 1)
        self.assertEqual(result["window_days"], 14)
        self.assertEqual(result["counts"], {"overdue": 3, "upcoming": 5, "attention": 5})

    def test_attention_counts_overdue_and_the_next_two_days_without_double_counting(self):
        for offset in (-3, -1, 0, 2, 3):
            self.outreach(deadline=TODAY + timedelta(days=offset))
        counts = self.queue()["counts"]
        self.assertEqual(counts["overdue"], 2)
        self.assertEqual(counts["attention"], 4, "overdue (-3, -1) plus days 0 and 2; day 3 is not near")

    def test_window_crosses_the_year_boundary(self):
        new_years = datetime(2026, 12, 30, 18, 0, tzinfo=timezone.utc)
        target = self.outreach(deadline=date(2027, 1, 2))
        result = self.queue(now=new_years)
        self.assertEqual(result["today"], "2026-12-30")
        self.assertEqual([item["outreach_target_id"] for item in result["items"]], [target])
        self.assertEqual(result["items"][0]["days_until"], 3)

    def test_days_outside_one_to_sixty_is_rejected(self):
        for days in (0, 61):
            with self.subTest(days=days), self.assertRaises(ValueError):
                self.queue(days=days)

    def test_sort_puts_overdue_first_then_date_then_deadlines_before_tasks(self):
        app = self.application("job-a", follow_up_at="2026-09-20T09:00:00-05:00")
        self.task(app, "2026-09-20T08:00:00-05:00")
        self.outreach(deadline=date(2026, 9, 20))
        overdue = self.outreach(deadline=date(2026, 9, 10))
        kinds = [item["kind"] for item in self.queue()["items"]]
        self.assertEqual(self.queue()["items"][0]["outreach_target_id"], overdue)
        self.assertEqual(kinds[1:], ["outreach_deadline", "task", "application_follow_up"])


class UrgentTimezoneTests(UrgentFixture):
    def test_posting_deadline_stays_on_its_calendar_date(self):
        self.listed_deadline("job-b", "2026-09-20T00:00:00+00:00")
        item = next(item for item in self.queue()["items"] if item["kind"] == "posting_deadline")
        self.assertEqual(item["date"], "2026-09-20", "midnight UTC must not become Sept 19 in Chicago")
        self.assertEqual(item["date_source"], "Stated in posting text")
        self.assertEqual(item["source_name"], "Orbit Lever")

    def test_instants_land_on_the_students_local_date(self):
        app = self.application("job-a", follow_up_at="2026-09-18T04:30:00+00:00")
        self.task(app, "2026-09-17T23:30")
        dates = {item["kind"]: item["date"] for item in self.queue()["items"]}
        self.assertEqual(dates["task"], "2026-09-17", "a naive 23:30 is wall-clock time in Chicago")
        self.assertEqual(dates["application_follow_up"], "2026-09-17", "04:30Z is 23:30 the previous evening")

    def test_dst_ambiguous_and_nonexistent_wall_times(self):
        zone = UserTimezone(CHICAGO, ZoneInfo(CHICAGO))
        ambiguous = zone.normalize_instant("2026-11-01T01:30")
        self.assertTrue(ambiguous.endswith("-05:00"), f"fold=0 picks the earlier (CDT) instant: {ambiguous}")
        nonexistent = zone.normalize_instant("2026-03-08T02:30")
        self.assertTrue(nonexistent.endswith("-06:00"), f"keeps the pre-transition offset: {nonexistent}")
        self.assertEqual(zone.calendar_date("2026-11-01T01:30"), date(2026, 11, 1))
        self.assertEqual(zone.calendar_date("2026-03-08T02:30"), date(2026, 3, 8))

    def test_system_local_fallback_is_named_honestly(self):
        self.set_timezone(LOCAL_USER_ID, None)
        result = self.queue()
        self.assertEqual(result["timezone"], SYSTEM_LOCAL)
        self.assertRegex(result["utc_offset"], r"^[+-]\d{2}:\d{2}$")
        self.assertEqual(result["today"], NOW.astimezone().date().isoformat())

    def test_named_zone_reports_its_offset(self):
        result = self.queue()
        self.assertEqual(result["timezone"], CHICAGO)
        self.assertEqual(result["utc_offset"], "-05:00")

    def test_outreach_and_urgent_agree_on_today(self):
        late_evening = datetime(2026, 9, 18, 4, 30, tzinfo=timezone.utc)
        self.assertEqual(local_today(self.conn, LOCAL_USER_ID, late_evening).isoformat(), self.queue(now=late_evening)["today"])
        self.assertEqual(self.queue(now=late_evening)["today"], "2026-09-17")

    def test_new_tasks_store_an_aware_instant(self):
        app = self.application("job-a")
        with_zone = add_application_task(self.conn, app, title="Browser", due_at="2026-09-20T09:00",
                                         timezone_name=CHICAGO, user_id=LOCAL_USER_ID)
        self.assertEqual(with_zone["due_at"], "2026-09-20T09:00:00-05:00")
        without_zone = add_application_task(self.conn, app, title="Agent", due_at="2026-09-20T09:00", user_id=LOCAL_USER_ID)
        self.assertEqual(without_zone["due_at"], "2026-09-20T09:00:00-05:00", "falls back to the student's zone, not UTC")
        self.set_timezone(LOCAL_USER_ID, None)
        system = add_application_task(self.conn, app, title="System", due_at="2026-09-20T09:00", user_id=LOCAL_USER_ID)
        self.assertIsNotNone(datetime.fromisoformat(system["due_at"]).tzinfo)

    def test_agent_approved_task_uses_the_students_timezone(self):
        app = self.application("job-a")
        self.conn.execute(
            "INSERT INTO agent_threads(id, user_id, title, created_at, updated_at) VALUES('thread-1', ?, 'T', ?, ?)",
            (LOCAL_USER_ID, STAMP, STAMP),
        )
        self.conn.execute(
            """INSERT INTO agent_proposed_actions(id, thread_id, user_id, action_type, scope, input_json,
                   expected_effect, created_at)
               VALUES('proposal-1', 'thread-1', ?, 'add_application_task', 'application', ?, 'Adds a task', ?)""",
            (LOCAL_USER_ID, json.dumps({"application_id": app, "title": "Agent task", "due_at": "2026-09-20T09:00"}), STAMP),
        )
        self.conn.commit()
        decide_proposal(self.conn, "proposal-1", "approve", user_id=LOCAL_USER_ID)
        stored = self.conn.execute("SELECT due_at FROM application_tasks WHERE title='Agent task'").fetchone()
        self.assertEqual(stored["due_at"], "2026-09-20T09:00:00-05:00")


class UrgentRuleTests(UrgentFixture):
    def test_submitted_applications_hide_deadlines_but_applying_keeps_them(self):
        self.listed_deadline("job-a", "2026-09-20T00:00:00+00:00")
        urgent.set_user_deadline(self.conn, "job-b", user_id=LOCAL_USER_ID, deadline_on="2026-09-21")
        self.application("job-a", "applying")
        self.application("job-b", "applied")
        kinds = {(item["kind"], item["opportunity_id"]) for item in self.queue()["items"]}
        self.assertIn(("posting_deadline", "job-a"), kinds)
        self.assertNotIn(("your_deadline", "job-b"), kinds)

    def test_closed_applications_hide_tasks_and_follow_ups_but_offers_keep_them(self):
        closed = self.application("job-a", "rejected", follow_up_at="2026-09-18T09:00:00-05:00")
        self.task(closed, "2026-09-18T09:00:00-05:00")
        offer = self.application("job-b", "offer", follow_up_at="2026-09-19T09:00:00-05:00")
        self.task(offer, "2026-09-19T09:00:00-05:00")
        items = self.queue()["items"]
        self.assertEqual({item["application_id"] for item in items}, {offer})
        self.assertEqual({item["kind"] for item in items}, {"task", "application_follow_up"})

    def test_done_tasks_and_closed_outreach_are_ignored(self):
        app = self.application("job-a")
        self.task(app, "2026-09-18T09:00:00-05:00", status="done")
        self.outreach(deadline=date(2026, 9, 18), status="replied")
        self.outreach(follow_up=date(2026, 9, 18), status="drafted")
        sent = self.outreach(follow_up=date(2026, 9, 18), status="sent")
        items = self.queue()["items"]
        self.assertEqual([(item["kind"], item["outreach_target_id"]) for item in items], [("outreach_follow_up", sent)])
        self.assertEqual(items[0]["date_source"], "Follow-up scheduled")

    def test_paused_and_replied_outreach_surface_only_their_revisit_date(self):
        paused = self.outreach(deadline=date(2026, 9, 18), follow_up=date(2026, 9, 19), status="paused")
        self.outreach(status="replied")
        self.outreach(follow_up=date(2026, 9, 19), status="declined")
        items = self.queue()["items"]
        self.assertEqual([(item["kind"], item["outreach_target_id"]) for item in items], [("outreach_revisit", paused)])
        self.assertEqual(items[0]["date_source"], "Revisit date you set")

    def test_passed_roles_hide_their_deadlines(self):
        self.listed_deadline("job-a", "2026-09-20T00:00:00+00:00")
        urgent.set_user_deadline(self.conn, "job-a", user_id=LOCAL_USER_ID, deadline_on="2026-09-21")
        self.intent("job-a", "passed")
        self.assertEqual(self.queue()["items"], [])
        self.intent("job-a", "undo")
        self.intent("job-a", "saved")
        items = self.queue()["items"]
        self.assertEqual(len(items), 2)
        self.assertTrue(all(item["saved"] for item in items))

    def test_inactive_and_duplicate_postings_are_hidden(self):
        self.listed_deadline("job-a", "2026-09-20T00:00:00+00:00")
        self.listed_deadline("job-b", "2026-09-20T00:00:00+00:00")
        self.conn.execute("UPDATE opportunities SET active=0 WHERE id='job-a'")
        self.conn.execute("UPDATE opportunities SET duplicate_of='job-a' WHERE id='job-b'")
        self.conn.commit()
        self.assertEqual(self.queue()["items"], [])

    def test_two_claims_on_one_posting_are_two_rows(self):
        self.listed_deadline("job-a", "2026-09-20T00:00:00+00:00")
        urgent.set_user_deadline(self.conn, "job-a", user_id=LOCAL_USER_ID, deadline_on="2026-09-25", note="careers page")
        items = self.queue()["items"]
        self.assertEqual([(item["kind"], item["date"]) for item in items],
                         [("posting_deadline", "2026-09-20"), ("your_deadline", "2026-09-25")])
        self.assertEqual(items[1]["date_source"], "You entered")

    def test_unparseable_dates_are_reported_and_logged_once(self):
        app = self.application("job-a")
        self.task(app, "next tuesday")
        with self.assertLogs("opportunity_app.urgent", level="WARNING") as logs:
            first = self.queue()
            second = self.queue()
        self.assertEqual(len(logs.records), 1)
        for result in (first, second):
            self.assertEqual(result["skipped_count"], 1)
            skipped = result["skipped"][0]
            self.assertEqual(skipped["kind"], "task")
            self.assertEqual(skipped["company"], "Acme Robotics")
            self.assertIn("unparseable", skipped["reason"])


class UrgentTenancyTests(UrgentFixture):
    def test_shared_posting_deadlines_are_shared_and_everything_else_is_private(self):
        self.listed_deadline("job-a", "2026-09-20T00:00:00+00:00")
        urgent.set_user_deadline(self.conn, "job-b", user_id=LOCAL_USER_ID, deadline_on="2026-09-21")
        urgent.set_user_deadline(self.conn, "job-b", user_id=OTHER, deadline_on="2026-09-22")
        mine = self.application("job-a", follow_up_at="2026-09-19T09:00:00-05:00")
        self.task(mine, "2026-09-19T08:00:00-05:00")
        self.outreach(deadline=date(2026, 9, 19))
        own = {(item["kind"], item["date"]) for item in self.queue()["items"]}
        other = {(item["kind"], item["date"]) for item in self.queue(user_id=OTHER)["items"]}
        self.assertIn(("posting_deadline", "2026-09-20"), own)
        self.assertEqual(other, {("posting_deadline", "2026-09-20"), ("your_deadline", "2026-09-22")})
        self.assertIn(("your_deadline", "2026-09-21"), own)

    def test_a_capture_is_visible_only_to_its_owner_by_provenance(self):
        self.capture("manual-private", owner=LOCAL_USER_ID, capture_id="capture-1", deadline="2026-09-20T00:00:00+00:00")
        self.assertIn("posting_deadline:manual-private", self.keys(self.queue()))
        self.assertTrue(urgent.visible_opportunity(self.conn, LOCAL_USER_ID, "manual-private"))
        # The hostile path: another student opens their own application on the leaked capture.
        self.application("manual-private", user_id=OTHER)
        self.assertEqual(self.queue(user_id=OTHER)["items"], [])
        self.assertFalse(urgent.visible_opportunity(self.conn, OTHER, "manual-private"))
        with self.assertRaises(urgent.DeadlineNotFoundError):
            urgent.set_user_deadline(self.conn, "manual-private", user_id=OTHER, deadline_on="2026-09-21")

    def test_an_orphaned_capture_is_visible_to_no_one(self):
        self.capture("manual-orphan", owner=LOCAL_USER_ID, capture_id="capture-2", deadline="2026-09-20T00:00:00+00:00")
        self.conn.execute("DELETE FROM opportunity_captures WHERE id='capture-2'")
        self.conn.commit()
        self.assertEqual(self.queue()["items"], [])
        self.assertFalse(urgent.visible_opportunity(self.conn, LOCAL_USER_ID, "manual-orphan"))

    def test_after_the_owner_is_deleted_their_capture_shows_to_no_one(self):
        self.capture("manual-owned", owner=OTHER, capture_id="capture-3", deadline="2026-09-20T00:00:00+00:00")
        roots = [self.root / "r", self.root / "c", self.root / "i"]
        for path in roots:
            path.mkdir()
        delete_account(self.conn, roots, user_id=OTHER)
        self.assertEqual(self.queue()["items"], [])

    def test_a_manual_prefix_without_capture_provenance_is_ordinary_inventory(self):
        self.posting("manual-lookalike", deadline="2026-09-20T00:00:00+00:00")
        self.assertIn("posting_deadline:manual-lookalike", self.keys(self.queue(user_id=OTHER)))


class AnalyticsOverdueTests(UrgentFixture):
    def test_overdue_uses_calendar_dates_across_offsets_and_skips_closed_stages(self):
        active = self.application("job-a", follow_up_at="2026-09-16T23:00:00-05:00")
        self.task(active, "2026-09-16T22:00")  # naive, yesterday evening in Chicago
        self.task(active, "2026-09-17T08:00:00-05:00")  # earlier today: due today, not overdue
        self.task(active, "2026-09-17T03:00:00+00:00")  # 22:00 on the 16th in Chicago: overdue
        closed = self.application("job-b", "withdrawn", follow_up_at="2026-09-01T09:00:00-05:00")
        self.task(closed, "2026-09-01T09:00:00-05:00")
        with mock.patch("opportunity_app.actions.datetime") as fake:
            fake.now.return_value = NOW
            fake.fromisoformat = datetime.fromisoformat
            analytics = application_analytics(self.conn, user_id=LOCAL_USER_ID)
        self.assertEqual(analytics["overdue_tasks"], 2)
        from opportunity_app.actions import list_applications
        with mock.patch("opportunity_app.user_time.datetime") as clock:
            clock.now.return_value = NOW
            clock.fromisoformat = datetime.fromisoformat
            listed = {item["id"]: item for item in list_applications(self.conn, user_id=LOCAL_USER_ID)}
        self.assertEqual(listed[active]["follow_up_on"], "2026-09-16", "23:00 CDT stays on the 16th")
        self.assertTrue(listed[active]["follow_up_overdue"])
        self.assertFalse(listed[closed]["follow_up_overdue"], "closed applications are never overdue")
        self.assertEqual(analytics["overdue_follow_ups"], 1)
        self.assertEqual(analytics["open_tasks"], 4)


class DeadlineApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        _, self.platform_path = build_and_migrate(root)
        self.app = create_app(
            db_path=self.platform_path,
            access_token="urgent-owner-token",
            static_dir=STATIC_DIR,
            resume_storage=root / "resumes",
            capture_storage=root / "captures",
            interview_storage=root / "interviews",
        )
        self.client_context = TestClient(self.app)
        self.client = self.client_context.__enter__()
        self.headers = {"Authorization": "Bearer urgent-owner-token"}

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.tempdir.cleanup()

    def put(self, opportunity_id: str, deadline_on: str, note: str = ""):
        return self.client.put(
            f"/api/v1/opportunities/{opportunity_id}/deadline",
            headers=self.headers, json={"deadline_on": deadline_on, "note": note},
        )

    def test_put_get_and_re_put_preserve_created_at(self):
        first = self.put("job-a", "2026-10-01", "careers page")
        self.assertEqual(first.status_code, 200, first.text)
        second = self.put("job-a", "2026-10-03")
        self.assertEqual(second.json()["deadline_on"], "2026-10-03")
        self.assertEqual(second.json()["created_at"], first.json()["created_at"])
        detail = self.client.get("/api/v1/opportunities/job-a", headers=self.headers).json()
        self.assertEqual(detail["user_deadline"]["deadline_on"], "2026-10-03")
        self.assertTrue(detail["can_set_user_deadline"])
        listed = self.client.get("/api/v1/opportunities", headers=self.headers).json()["items"]
        self.assertEqual({item["id"]: item["user_deadline_on"] for item in listed},
                         {"job-a": "2026-10-03", "job-b": None})

    def test_strict_date_and_note_validation(self):
        for value in ("20260828", " 2026-08-28", "2026-02-30", "2019-12-31", "2101-01-01", "2026-8-28", "28/08/2026"):
            with self.subTest(value=value):
                self.assertEqual(self.put("job-a", value).status_code, 422)
        self.assertEqual(self.put("job-a", "2026-10-01", "x" * 201).status_code, 422)
        self.assertEqual(self.put("job-a", "2026-10-01", "x" * 200).status_code, 200)

    def test_not_found_for_missing_inactive_and_duplicate_postings(self):
        self.assertEqual(self.put("job-missing", "2026-10-01").status_code, 404)
        with closing(connect_product(self.platform_path)) as conn:
            conn.execute("UPDATE opportunities SET active=0 WHERE id='job-a'")
            conn.execute("UPDATE opportunities SET duplicate_of='job-a' WHERE id='job-b'")
            conn.commit()
        self.assertEqual(self.put("job-a", "2026-10-01").status_code, 404)
        self.assertEqual(self.put("job-b", "2026-10-01").status_code, 404)

    def test_visibility_is_part_of_the_write(self):
        """No separate check-then-insert: a posting retired before the write gets no row."""
        with closing(connect_product(self.platform_path)) as conn:
            conn.execute("UPDATE opportunities SET active=0 WHERE id='job-a'")
            conn.commit()
            with self.assertRaises(urgent.DeadlineNotFoundError):
                urgent.set_user_deadline(conn, "job-a", user_id=LOCAL_USER_ID, deadline_on="2026-10-01")
            count = conn.execute("SELECT COUNT(*) AS n FROM opportunity_deadlines").fetchone()["n"]
        self.assertEqual(count, 0)

    def test_a_posting_deleted_mid_write_is_a_404_not_a_500(self):
        import sqlite3

        original = urgent.capture_visible_sql

        def failing_sql(alias="o"):
            raise sqlite3.IntegrityError("FOREIGN KEY constraint failed")

        with mock.patch("opportunity_app.urgent.capture_visible_sql", side_effect=failing_sql):
            response = self.put("job-a", "2026-10-01")
        self.assertEqual(response.status_code, 404, response.text)
        self.assertIs(urgent.capture_visible_sql, original)

    def test_delete_is_idempotent_and_works_after_the_posting_goes_inactive(self):
        self.assertEqual(self.put("job-a", "2026-10-01").status_code, 200)
        with closing(connect_product(self.platform_path)) as conn:
            conn.execute("UPDATE opportunities SET active=0 WHERE id='job-a'")
            conn.commit()
        detail = self.client.get("/api/v1/opportunities/job-a", headers=self.headers).json()
        self.assertFalse(detail["can_set_user_deadline"])
        self.assertIsNotNone(detail["user_deadline"])
        for _ in range(2):
            response = self.client.delete("/api/v1/opportunities/job-a/deadline", headers=self.headers)
            self.assertEqual(response.status_code, 204)
        detail = self.client.get("/api/v1/opportunities/job-a", headers=self.headers).json()
        self.assertIsNone(detail["user_deadline"])

    def test_purge_and_export(self):
        self.assertEqual(self.put("job-a", "2026-10-01").status_code, 200)
        with closing(connect_product(self.platform_path)) as conn:
            exported = export_account(conn, user_id=LOCAL_USER_ID)
            self.assertEqual([row["opportunity_id"] for row in exported["opportunity_deadlines"]], ["job-a"])
            conn.execute("UPDATE opportunities SET active=0 WHERE id='job-a'")
            conn.commit()
            purge_expired_opportunities(conn, backup=False)
            remaining = conn.execute("SELECT COUNT(*) AS n FROM opportunity_deadlines").fetchone()["n"]
        self.assertEqual(remaining, 0, "a user deadline cascades away with its purged posting")

    def test_a_user_deadline_never_marks_a_posting_expired(self):
        with closing(connect_product(self.platform_path)) as conn:
            conn.execute("UPDATE opportunities SET deadline_at = NULL WHERE id='job-a'")
            conn.commit()
        self.assertEqual(self.put("job-a", "2020-01-02").status_code, 200)
        with closing(connect_product(self.platform_path)) as conn:
            result = purge_expired_opportunities(conn, backup=False, today="2026-09-17")
        self.assertEqual(result["deleted"], 0)

    def test_urgent_endpoint_validates_days_and_serves_the_page(self):
        self.assertEqual(self.client.get("/api/v1/urgent?days=0", headers=self.headers).status_code, 422)
        self.assertEqual(self.client.get("/api/v1/urgent?days=61", headers=self.headers).status_code, 422)
        body = self.client.get("/api/v1/urgent", headers=self.headers).json()
        self.assertEqual(body["window_days"], 14)
        self.assertRegex(body["today"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(self.client.get("/urgent").status_code, 200)

    def test_task_create_accepts_the_browser_timezone(self):
        with closing(connect_product(self.platform_path)) as conn:
            conn.execute(
                """INSERT INTO applications(id, opportunity_id, user_id, stage, created_at, updated_at)
                   VALUES('app-a', 'job-a', ?, 'applying', ?, ?)""",
                (LOCAL_USER_ID, STAMP, STAMP),
            )
            conn.commit()
        response = self.client.post(
            "/api/v1/applications/app-a/tasks", headers=self.headers,
            json={"title": "Email recruiter", "due_at": "2026-09-20T09:00", "timezone": "America/New_York"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(response.json()["due_at"], "2026-09-20T09:00:00-04:00")
        bad = self.client.post(
            "/api/v1/applications/app-a/tasks", headers=self.headers,
            json={"title": "x", "due_at": "2026-09-20T09:00", "timezone": "Mars/Olympus"},
        )
        self.assertEqual(bad.status_code, 422)

    def test_the_agent_reports_entered_deadlines_as_the_students_own(self):
        from opportunity_app import student_agent

        with closing(connect_product(self.platform_path)) as conn:
            conn.execute("UPDATE opportunities SET deadline_at='2026-09-20T00:00:00+00:00' WHERE id='job-b'")
            conn.commit()
        self.assertEqual(self.put("job-a", "2026-09-18").status_code, 200)
        with mock.patch.object(student_agent, "_today_utc", return_value="2026-09-17"):
            thread = self.client.post("/api/v1/agent/threads", headers=self.headers, json={"provider": "legacy"}).json()["id"]
            reply = self.client.post(
                f"/api/v1/agent/threads/{thread}/messages", headers=self.headers, json={"content": "What closes soon?"}
            ).json()["message"]
        self.assertEqual(
            reply["content"].splitlines(),
            ["Sep 18, 2026: Mechanical Engineering Intern at Acme Robotics (you entered)",
             "Sep 20, 2026: Controls Co-op at Orbit Systems"],
        )
        self.assertEqual([(c["id"], c["field"]) for c in reply["citations"]],
                         [("job-a", "user_deadline"), ("job-b", "deadline_at")])

    def test_unscoped_repository_keys_are_unchanged(self):
        with closing(connect_product(self.platform_path, read_only=True)) as conn:
            items, _ = OpportunityRepository(conn).list(OpportunityFilters())
        self.assertTrue(items)
        self.assertNotIn("user_deadline_on", items[0])
        self.assertNotIn("user_deadline", items[0])


class DeadlineTenancyApiTests(unittest.TestCase):
    """Two signed-up students each see only their own deadline on a shared posting."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        for name in ("resumes", "captures", "interviews"):
            (root / name).mkdir()
        _, self.platform_path = build_and_migrate(root)
        app = create_app(
            db_path=self.platform_path,
            access_token="owner-token",
            admin_token="admin-token",
            static_dir=STATIC_DIR,
            resume_storage=root / "resumes",
            capture_storage=root / "captures",
            interview_storage=root / "interviews",
        )
        self.client = TestClient(app)
        with TestClient(app):
            self.client.put(
                "/api/v1/admin/feature-flags/allow_public_signup",
                headers={"Authorization": "Bearer admin-token"},
                json={"enabled": True, "description": "test"},
            )
        self.a = self.register("a@example.com")
        self.b = self.register("b@example.com")

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def register(self, email: str) -> dict:
        response = self.client.post(
            "/api/v1/auth/register", json={"email": email, "password": "Password123!", "display_name": email},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return {"Authorization": f"Bearer {response.json()['api_token']}"}

    def test_each_student_sees_only_their_own_deadline(self):
        soon = (date.today() + timedelta(days=5)).isoformat()
        later = (date.today() + timedelta(days=9)).isoformat()
        for headers, value in ((self.a, soon), (self.b, later)):
            response = self.client.put("/api/v1/opportunities/job-a/deadline", headers=headers,
                                       json={"deadline_on": value, "note": ""})
            self.assertEqual(response.status_code, 200, response.text)
        for headers, value in ((self.a, soon), (self.b, later)):
            detail = self.client.get("/api/v1/opportunities/job-a", headers=headers).json()
            self.assertEqual(detail["user_deadline"]["deadline_on"], value)
            listed = self.client.get("/api/v1/opportunities", headers=headers).json()["items"]
            self.assertEqual(next(item for item in listed if item["id"] == "job-a")["user_deadline_on"], value)
            urgent_items = self.client.get("/api/v1/urgent?days=60", headers=headers).json()["items"]
            self.assertEqual([item["date"] for item in urgent_items if item["kind"] == "your_deadline"], [value])

    def test_an_empty_page_skips_the_deadline_query(self):
        response = self.client.get("/api/v1/opportunities?offset=500", headers=self.a)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"], [])


if __name__ == "__main__":
    unittest.main()
