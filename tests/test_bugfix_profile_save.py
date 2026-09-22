"""PUT /api/v1/profile validates types, and a refused or failed save stores nothing.

Before this fix a save of ``regions: 1`` or ``skills: [null]`` was merged into
the stored profile first and crashed scoring afterwards, leaving the profile
row, the facts and the owner's config/profile.json out of step with each other
and with the scores. A first save before any GET also provisioned a profile
row even when the save itself was refused.
"""

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR
from opportunity_app import profile as profile_module
from opportunity_app.api import create_app
from opportunity_app.schema import LOCAL_USER_ID

from helpers_platform import build_and_migrate

OWNER = {"Authorization": "Bearer profile-owner"}
ADMIN = {"Authorization": "Bearer profile-admin"}


class ProfileSaveTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        _, self.platform_path = build_and_migrate(root)
        self.profile_path = root / "profile.json"
        app = create_app(
            db_path=self.platform_path,
            access_token="profile-owner",
            admin_token="profile-admin",
            static_dir=STATIC_DIR,
            resume_storage=root / "resumes",
            capture_storage=root / "captures",
            interview_storage=root / "mock-interviews",
            profile_file=self.profile_path,
            rate_limit_per_minute=10**6,
        )
        self.client = TestClient(app, raise_server_exceptions=False)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        # Establish the owner's profile row and scores before each test.
        self.assertEqual(self.client.get("/api/v1/profile", headers=OWNER).status_code, 200)

    def tearDown(self):
        self.tempdir.cleanup()

    def put(self, updates, confirmed=None, headers=OWNER):
        return self.client.put(
            "/api/v1/profile",
            json={"updates": updates, "confirmed_fields": list(confirmed if confirmed is not None else updates)},
            headers=headers,
        )

    def snapshot(self, user_id=LOCAL_USER_ID):
        with closing(sqlite3.connect(self.platform_path)) as conn:
            profile = conn.execute(
                "SELECT profile_json, confirmed_at, updated_at FROM profiles WHERE user_id=?", (user_id,)
            ).fetchone()
            facts = conn.execute(
                "SELECT field_path, value_json, source, confirmed, updated_at FROM profile_facts "
                "WHERE user_id=? ORDER BY field_path",
                (user_id,),
            ).fetchall()
            scores = conn.execute(
                "SELECT opportunity_id, score, explanation_json, created_at FROM fit_scores "
                "WHERE user_id=? ORDER BY opportunity_id",
                (user_id,),
            ).fetchall()
        file_bytes = self.profile_path.read_bytes() if self.profile_path.exists() else None
        return {"profile": profile, "facts": facts, "scores": scores, "file": file_bytes}

    def fact_paths(self, response):
        return {fact["field_path"] for fact in response.json()["facts"]}

    # -- first-time save --------------------------------------------------

    def register_student(self):
        enabled = self.client.put(
            "/api/v1/admin/feature-flags/allow_public_signup",
            headers=ADMIN,
            json={"enabled": True, "description": "profile save tests"},
        )
        self.assertEqual(enabled.status_code, 200, enabled.text)
        registered = self.client.post(
            "/api/v1/auth/register",
            json={"email": "new@example.com", "password": "PasswordNew123", "display_name": "New"},
        )
        self.assertEqual(registered.status_code, 201, registered.text)
        body = registered.json()
        # Registration provisions a profile; remove it so this is a first save.
        with closing(sqlite3.connect(self.platform_path)) as conn, conn:
            conn.execute("DELETE FROM fit_scores WHERE user_id=?", (body["user_id"],))
            conn.execute("DELETE FROM profile_facts WHERE user_id=?", (body["user_id"],))
            conn.execute("DELETE FROM profiles WHERE user_id=?", (body["user_id"],))
        return body["user_id"], {"Authorization": f"Bearer {body['api_token']}"}

    def test_invalid_first_save_before_any_get_stores_no_profile_row_or_scores(self):
        user_id, headers = self.register_student()
        refused = self.put({"regions": 1}, headers=headers)
        self.assertEqual(refused.status_code, 422, refused.text)
        after = self.snapshot(user_id)
        self.assertIsNone(after["profile"])
        self.assertEqual(after["facts"], [])
        self.assertEqual(after["scores"], [])

    def test_valid_first_save_before_any_get_creates_the_profile_row(self):
        user_id, headers = self.register_student()
        owner_file = self.snapshot()["file"]
        saved = self.put({"skills": ["Python"]}, headers=headers)
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["profile"], {"skills": ["Python"]})
        after = self.snapshot(user_id)
        self.assertIsNotNone(after["profile"])
        self.assertEqual([row[0] for row in after["facts"]], ["skills"])
        self.assertTrue(after["scores"])
        self.assertEqual(self.snapshot()["file"], owner_file, "another student's save never touches the owner file")

    # -- type validation --------------------------------------------------

    def test_nulls_save_as_unanswered_with_no_facts(self):
        saved = self.put(
            {
                "max_years_experience": None,
                "out_of_region_penalty": None,
                "skills": None,
                "available_terms": None,
            }
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertFalse(
            self.fact_paths(saved)
            & {"max_years_experience", "out_of_region_penalty", "skills", "available_terms"}
        )
        self.assertTrue(self.snapshot()["scores"])

    def test_fractional_numbers_are_refused(self):
        for updates in (
            {"max_years_experience": 1.9},
            {"regions": [{"name": "Austin", "bonus": 2.5, "state_markers": [], "places": ["austin"]}]},
        ):
            with self.subTest(updates=updates):
                before = self.snapshot()
                refused = self.put(updates)
                self.assertEqual(refused.status_code, 422, refused.text)
                self.assertIn("whole number", refused.json()["detail"])
                self.assertEqual(self.snapshot(), before)

    def test_wrong_types_are_refused_and_nothing_changes(self):
        cases = (
            ({"regions": 1}, "regions must be a list"),
            ({"preferred_role_types": [{}]}, "preferred_role_types[0] must be text"),
            ({"skills": [None]}, "skills[0] must be text"),
            ({"regions": [{"name": "Austin", "bonus": "bad"}]}, "regions[0].bonus must be a whole number"),
            ({"hours_per_week": True}, "hours_per_week must be a whole number"),
            ({"remote_ok": "yes"}, "remote_ok must be true, false, or not answered"),
            ({"regions": [{"name": "Austin", "state_markers": None}]}, "regions[0].state_markers"),
            ({"regions": ["Austin"]}, "regions[0] must be an object"),
        )
        for updates, message in cases:
            with self.subTest(updates=updates):
                before = self.snapshot()
                refused = self.put(updates)
                self.assertEqual(refused.status_code, 422, refused.text)
                self.assertIsInstance(refused.json()["detail"], str)
                self.assertIn(message, refused.json()["detail"])
                self.assertEqual(self.snapshot(), before)

    def test_partially_answered_form_save_succeeds_without_facts_for_nulls(self):
        # The shape app.js submits from the profile form, with most answers
        # left as "Not answered".
        updates = {
            "name": "Test Student",
            "school": "",
            "degree": "",
            "graduation_year": None,
            "skills": ["SolidWorks"],
            "interest_keywords": [],
            "available_terms": [],
            "regions": [
                {
                    "name": "Atlanta",
                    "radius": "selected",
                    "bonus": 10,
                    "state_markers": [],
                    "aliases": [],
                    "places": ["atlanta"],
                }
            ],
            "preferred_locations": ["Atlanta"],
            "break_location": "",
            "hours_per_week": None,
            "work_authorized_us": None,
            "us_citizen": None,
            "requires_sponsorship": None,
            "willing_to_relocate": None,
            "compensation_preferences": {"paid_only": None, "minimum_hourly": None, "currency": "USD"},
        }
        confirmed = ["name", "skills", "regions", "preferred_locations"]
        saved = self.put(updates, confirmed)
        self.assertEqual(saved.status_code, 200, saved.text)
        facts = self.fact_paths(saved)
        self.assertTrue(set(confirmed) <= facts)
        self.assertFalse(facts & (set(updates) - set(confirmed)), "no facts for unanswered fields")
        stored = saved.json()["profile"]
        self.assertEqual(stored["regions"][0]["state_markers"], [], "a region with no markers is kept")
        written = json.loads(self.profile_path.read_text(encoding="utf-8"))
        self.assertEqual(written["regions"][0]["name"], "Atlanta")
        self.assertIsNone(written["hours_per_week"])

    def test_explicit_zero_years_is_kept(self):
        saved = self.put({"max_years_experience": 0})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["profile"]["max_years_experience"], 0)
        self.assertIn("max_years_experience", self.fact_paths(saved))

    # -- atomicity ----------------------------------------------------------

    def test_database_failure_after_the_file_write_restores_the_file(self):
        before = self.snapshot()
        self.assertIsNotNone(before["file"])
        with mock.patch.object(
            profile_module, "_write_scores", side_effect=sqlite3.OperationalError("disk I/O error")
        ):
            failed = self.put({"skills": ["Aspen Plus"]})
        self.assertEqual(failed.status_code, 500)
        self.assertEqual(self.snapshot(), before)

    def test_database_failure_removes_a_file_that_did_not_exist(self):
        self.profile_path.unlink()
        before = self.snapshot()
        with mock.patch.object(
            profile_module, "_write_scores", side_effect=sqlite3.OperationalError("disk I/O error")
        ):
            failed = self.put({"skills": ["Aspen Plus"]})
        self.assertEqual(failed.status_code, 500)
        self.assertFalse(self.profile_path.exists())
        self.assertEqual(self.snapshot(), before)

    def test_scoring_failure_leaves_database_and_file_unchanged(self):
        before = self.snapshot()
        with mock.patch.object(profile_module, "score_job", side_effect=RuntimeError("scoring broke")):
            failed = self.put({"skills": ["Aspen Plus"]})
        self.assertEqual(failed.status_code, 500)
        self.assertEqual(self.snapshot(), before)

    def test_successful_save_moves_file_database_and_scores_together(self):
        saved = self.put({"skills": ["Aspen Plus"]})
        self.assertEqual(saved.status_code, 200, saved.text)
        after = self.snapshot()
        self.assertEqual(json.loads(after["profile"][0])["skills"], ["Aspen Plus"])
        self.assertEqual(json.loads(after["file"])["skills"], ["Aspen Plus"])
        self.assertEqual(json.loads(after["file"])["regions"][0]["name"], "Austin")
        self.assertTrue(after["scores"])


if __name__ == "__main__":
    unittest.main()
