"""Regressions from the 2026-09-21 sweep: capture privacy, deadline dates, program provenance.

* A confirmed manual capture is private to the student who captured it on every
  read path: the opportunity list and detail, the assistant's recommendations,
  search, detail and deadline tools, and its save/intent proposals.
* "Deadline before D" includes deadlines on D in the stored timestamp format.
* The assistant's "closes soon" uses the student's local today, not UTC's.
* Urgent does not claim a researched program's date was published by the host.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR, student_agent, urgent
from opportunity_app.agent_providers import ToolCall
from opportunity_app.api import create_app
from opportunity_app.auth import issue_user_token
from opportunity_app.schema import LOCAL_USER_ID, connect_product
from pipeline_core import OpportunityFilters, OpportunityRepository, capture_visible_sql
from tests.helpers_platform import build_and_migrate

OTHER = "student-b"
STAMP = "2026-09-01T00:00:00+00:00"
SECRET = "manual-secret"


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        _, self.db = build_and_migrate(self.root)
        env = mock.patch.dict(os.environ, {"PIPELINE_TIMEZONE": ""})
        env.start()
        self.addCleanup(env.stop)
        with closing(connect_product(self.db)) as conn:
            conn.execute(
                "INSERT INTO users(id, email, display_name, role, created_at, updated_at) "
                "VALUES(?, 'b@example.com', 'Student B', 'student', ?, ?)",
                (OTHER, STAMP, STAMP),
            )
            self.token_b = issue_user_token(conn, OTHER)
            conn.commit()

    def tearDown(self):
        self.tempdir.cleanup()

    def seed_capture(self, owner: str = LOCAL_USER_ID) -> None:
        """A confirmed capture stored the way captures.confirm_capture stores one."""
        with closing(connect_product(self.db)) as conn:
            conn.execute(
                """INSERT INTO opportunities(id, company, title, company_sort_key, title_sort_key, location, region,
                       role_type, url, description, posted_at, posted_at_utc, deadline_at, first_seen_at,
                       last_seen_at, active, fingerprint, content_fingerprint, duplicate_of, created_at, updated_at)
                   VALUES(?, 'Secret Startup', 'Private Capture Role', 'secret startup', 'private capture role',
                       '', 'Unknown', 'other', 'https://secret.example.com/job', 'private text', NULL, NULL,
                       '2099-12-01T00:00:00+00:00', ?, ?, 1, 'fp-secret', 'fp-secret', NULL, ?, ?)""",
                (SECRET, STAMP, STAMP, STAMP, STAMP),
            )
            conn.execute(
                """INSERT INTO opportunity_sources(opportunity_id, source_key, source_name, external_id, source_url,
                       first_seen_at, last_seen_at)
                   VALUES(?, 'manual:capture', 'Manual capture', 'cap-1', 'https://secret.example.com/job', ?, ?)""",
                (SECRET, STAMP, STAMP),
            )
            conn.execute(
                "INSERT INTO applications(id, opportunity_id, user_id, stage, created_at, updated_at) "
                "VALUES('app-secret', ?, ?, 'applying', ?, ?)",
                (SECRET, owner, STAMP, STAMP),
            )
            conn.execute(
                "INSERT INTO opportunity_captures(id, user_id, source_type, source_url, status, application_id, created_at) "
                "VALUES('cap-1', ?, 'url', 'https://secret.example.com/job', 'confirmed', 'app-secret', ?)",
                (owner, STAMP),
            )
            conn.commit()


class CaptureVisibilityTests(_Fixture):
    def setUp(self):
        super().setUp()
        self.seed_capture()
        app = create_app(
            db_path=self.db, access_token="t", admin_token="a", static_dir=STATIC_DIR,
            resume_storage=self.root / "r", capture_storage=self.root / "c",
            interview_storage=self.root / "i", rate_limit_per_minute=10**6,
        )
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.owner = {"Authorization": "Bearer t"}
        self.other = {"Authorization": f"Bearer {self.token_b}"}

    def listed(self, headers) -> list[str]:
        response = self.client.get("/api/v1/opportunities", params={"limit": 100}, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        return [item["id"] for item in response.json()["items"]]

    def test_urgent_reexports_the_single_pipeline_core_predicate(self):
        self.assertIs(urgent.capture_visible_sql, capture_visible_sql)

    def test_api_list_and_detail_hide_another_students_capture(self):
        self.assertIn(SECRET, self.listed(self.owner))
        self.assertEqual(self.client.get(f"/api/v1/opportunities/{SECRET}", headers=self.owner).status_code, 200)
        other_list = self.listed(self.other)
        self.assertNotIn(SECRET, other_list)
        self.assertTrue(other_list, "shared inventory must stay visible to other students")
        self.assertEqual(self.client.get(f"/api/v1/opportunities/{SECRET}", headers=self.other).status_code, 404)

    def test_repository_tenant_paths_apply_the_predicate(self):
        with closing(connect_product(self.db)) as conn:
            other = OpportunityRepository(conn, user_id=OTHER)
            items, total = other.list(OpportunityFilters(limit=100))
            self.assertNotIn(SECRET, [item["id"] for item in items])
            self.assertEqual(total, len(items))
            self.assertIsNone(other.get(SECRET))
            owner = OpportunityRepository(conn, user_id=LOCAL_USER_ID)
            self.assertIsNotNone(owner.get(SECRET))
            # The unscoped CLI path is the owner's own copy and is unchanged.
            self.assertIsNotNone(OpportunityRepository(conn).get(SECRET))

    def run_tool(self, conn, user_id: str, name: str, **arguments):
        thread = student_agent.create_thread(conn, user_id=user_id)["id"]
        return student_agent._execute_agent_tool(
            conn, thread, None, ToolCall(id="call-1", name=name, arguments=arguments), user_id=user_id,
        )

    def test_agent_search_detail_and_intent_tools_refuse_another_students_capture(self):
        with closing(connect_product(self.db)) as conn:
            output, _, _ = self.run_tool(conn, OTHER, "search_opportunities", query="Private Capture", limit=10)
            self.assertEqual(output["total"], 0)
            with self.assertRaises(student_agent.OpportunityNotFoundError):
                self.run_tool(conn, OTHER, "get_opportunity", opportunity_id=SECRET)
            with self.assertRaises(ValueError):
                self.run_tool(conn, OTHER, "propose_opportunity_intent", opportunity_id=SECRET, action="saved")
            with self.assertRaises(ValueError):
                self.run_tool(conn, OTHER, "propose_preparation_document", opportunity_id=SECRET, document_type="resume")
            # The owner still reaches it through every tool.
            output, _, _ = self.run_tool(conn, LOCAL_USER_ID, "search_opportunities", query="Private Capture", limit=10)
            self.assertEqual([item["id"] for item in output["items"]], [SECRET])
            output, _, _ = self.run_tool(conn, LOCAL_USER_ID, "get_opportunity", opportunity_id=SECRET)
            self.assertEqual(output["id"], SECRET)
            _, _, proposal = self.run_tool(conn, LOCAL_USER_ID, "propose_opportunity_intent", opportunity_id=SECRET, action="saved")
            self.assertIsNotNone(proposal)

    def post(self, headers, content: str) -> dict:
        thread = self.client.post("/api/v1/agent/threads", headers=headers, json={"provider": "legacy"})
        self.assertEqual(thread.status_code, 201, thread.text)
        reply = self.client.post(
            f"/api/v1/agent/threads/{thread.json()['id']}/messages", headers=headers, json={"content": content},
        )
        self.assertLess(reply.status_code, 300, reply.text)
        return reply.json()

    def test_legacy_save_proposal_refuses_a_guessed_capture_id(self):
        refused = self.post(self.other, f"save {SECRET}")
        self.assertIn("could not find", refused["message"]["content"])
        self.assertNotIn("Secret Startup", json.dumps(refused))
        accepted = self.post(self.owner, f"save {SECRET}")
        self.assertIn("prepared a save action", accepted["message"]["content"])

    def test_legacy_recommendations_and_deadlines_hide_another_students_capture(self):
        with closing(connect_product(self.db)) as conn:
            # Put the capture at the top of every ranking so a leak cannot hide below the cut.
            conn.execute("UPDATE fit_scores SET score=100 WHERE opportunity_id=?", (SECRET,))
            conn.execute(
                "INSERT INTO fit_scores(opportunity_id, user_id, ruleset_version, score, explanation_json, created_at) "
                "VALUES(?, ?, 'legacy-v1', 100, '[]', ?) ON CONFLICT DO NOTHING",
                (SECRET, OTHER, STAMP),
            )
            conn.commit()
        for content in ("recommend something", "what closes soon?"):
            with self.subTest(content=content):
                self.assertNotIn("Secret Startup", json.dumps(self.post(self.other, content)))
        self.assertIn("Secret Startup", json.dumps(self.post(self.owner, "what closes soon?")))
        with closing(connect_product(self.db)) as conn:
            other = [row["id"] for row in student_agent._upcoming_deadlines(conn, user_id=OTHER, limit=50)]
            owner = [row["id"] for row in student_agent._upcoming_deadlines(conn, user_id=LOCAL_USER_ID, limit=50)]
        self.assertNotIn(SECRET, other)
        self.assertIn(SECRET, owner)


class DeadlineBeforeTests(_Fixture):
    def test_the_chosen_day_is_included_in_the_stored_format(self):
        with closing(connect_product(self.db)) as conn:
            conn.execute("UPDATE opportunities SET deadline_at=NULL")
            conn.execute("UPDATE opportunities SET deadline_at='2026-06-01T00:00:00+00:00' WHERE id='job-a'")
            conn.execute("UPDATE opportunities SET deadline_at='2026-06-02T00:00:00+00:00' WHERE id='job-b'")
            conn.commit()
            for user_id in (None, LOCAL_USER_ID, OTHER):
                with self.subTest(user=user_id):
                    items, _ = OpportunityRepository(conn, user_id=user_id).list(
                        OpportunityFilters(deadline_before="2026-06-01", limit=100)
                    )
                    self.assertEqual([item["id"] for item in items], ["job-a"])


class AgentLocalTodayTests(_Fixture):
    def test_closes_soon_uses_the_students_local_date(self):
        # 21:00 on Sept 21 in Chicago is already Sept 22 in UTC.
        now = datetime(2026, 9, 22, 2, 0, tzinfo=timezone.utc)
        with closing(connect_product(self.db)) as conn:
            conn.execute(
                "INSERT INTO notification_preferences(user_id, timezone, timezone_explicit, updated_at) "
                "VALUES(?, 'America/Chicago', 1, ?)",
                (LOCAL_USER_ID, STAMP),
            )
            conn.execute("UPDATE opportunities SET deadline_at=NULL")
            conn.execute("UPDATE opportunities SET deadline_at='2026-09-21T00:00:00+00:00' WHERE id='job-a'")
            conn.commit()
            with mock.patch.object(student_agent, "utc_now", return_value=now.isoformat()):
                self.assertEqual(student_agent._today_local(conn, LOCAL_USER_ID), "2026-09-21")
                rows = student_agent._upcoming_deadlines(conn, user_id=LOCAL_USER_ID, limit=10)
        self.assertEqual([row["id"] for row in rows], ["job-a"])


class ProgramDeadlineProvenanceTests(_Fixture):
    def test_program_dates_carry_a_neutral_label_and_their_note(self):
        path = self.root / "programs.json"
        path.write_text(json.dumps({"checked_on": "2026-09-21", "programs": [
            {"id": "est", "name": "Estimated program", "host": "Example Host", "url": "https://example.com/p",
             "evidence": "explicit", "deadline_on": "2026-09-25",
             "deadline_note": "Estimated from last year's cycle", "source_note": "Found on a listicle"},
            {"id": "plain", "name": "Plain program", "host": "Example Host", "url": "https://example.com/q",
             "evidence": "unverified", "deadline_on": "2026-09-26"},
        ]}), encoding="utf-8")
        now = datetime(2026, 9, 21, 17, 0, tzinfo=timezone.utc)
        with closing(connect_product(self.db)) as conn:
            payload = urgent.urgent_queue(conn, user_id=LOCAL_USER_ID, now=now, programs_path=path)
        programs = {item["program_id"]: item for item in payload["items"] if item["kind"] == "program_deadline"}
        self.assertEqual(set(programs), {"est", "plain"})
        for item in programs.values():
            self.assertEqual(item["date_source"], "From your program research")
            self.assertNotIn("Published", item["date_source"])
        self.assertEqual(programs["est"]["date_note"], "Estimated from last year's cycle")
        self.assertEqual(programs["est"]["source_name"], "Found on a listicle")
        self.assertIsNone(programs["plain"]["date_note"])
        # Every Urgent row carries the field, so the frontend can read it unconditionally.
        self.assertTrue(all("date_note" in item for item in payload["items"]))


if __name__ == "__main__":
    unittest.main()
