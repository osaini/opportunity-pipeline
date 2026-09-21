"""Cross-user tenancy tests: two real accounts must never see each other's data."""

import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from opportunity_app.api import create_app
from opportunity_app import STATIC_DIR

from helpers_platform import build_and_migrate


class TenancyTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.resume_storage = root / "resumes"
        self.capture_storage = root / "captures"
        self.interview_storage = root / "mock-interviews"
        self.resume_storage.mkdir()
        self.capture_storage.mkdir()
        self.interview_storage.mkdir()
        _, self.platform_path = build_and_migrate(root)
        app = create_app(
            db_path=self.platform_path,
            access_token="owner-static-token",
            admin_token="admin-tenant",
            static_dir=STATIC_DIR,
            resume_storage=self.resume_storage,
            capture_storage=self.capture_storage,
            interview_storage=self.interview_storage,
        )
        self.client = TestClient(app)
        with TestClient(app):
            # Enable open signup via the admin feature-flag surface.
            enabled = self.client.put(
                "/api/v1/admin/feature-flags/allow_public_signup",
                headers={"Authorization": "Bearer admin-tenant"},
                json={"enabled": True, "description": "test tenancy"},
            )
            self.assertEqual(enabled.status_code, 200)

        self.user_a = self._register("a@example.com", "PasswordA123", "Student A")
        self.user_b = self._register("b@example.com", "PasswordB123", "Student B")

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def _register(self, email: str, password: str, display_name: str) -> dict:
        response = self.client.post(
            "/api/v1/auth/register",
            json={"email": email, "password": password, "display_name": display_name},
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertIn("api_token", body)
        return body

    def _headers(self, user: dict) -> dict:
        return {"Authorization": f"Bearer {user['api_token']}"}

    def test_public_signup_requires_feature_flag(self):
        disabled = self.client.put(
            "/api/v1/admin/feature-flags/allow_public_signup",
            headers={"Authorization": "Bearer admin-tenant"},
            json={"enabled": False},
        )
        self.assertEqual(disabled.status_code, 200)
        blocked = self.client.post(
            "/api/v1/auth/register",
            json={"email": "c@example.com", "password": "PasswordC123", "display_name": "C"},
        )
        self.assertEqual(blocked.status_code, 403)

    def test_password_login_issues_scoped_user_token(self):
        login = self.client.post(
            "/api/v1/session",
            json={"email": "a@example.com", "password": "PasswordA123"},
        )
        self.assertEqual(login.status_code, 200)
        body = login.json()
        self.assertEqual(body["user_id"], self.user_a["user_id"])
        self.assertIsNotNone(body["api_token"])
        session = self.client.get("/api/v1/session", headers={"Authorization": f"Bearer {body['api_token']}"})
        self.assertEqual(session.json()["user_id"], self.user_a["user_id"])

    def test_owner_static_token_still_authenticates_as_local_user(self):
        session = self.client.get("/api/v1/session", headers={"Authorization": "Bearer owner-static-token"})
        self.assertEqual(session.status_code, 200)
        self.assertEqual(session.json()["user_id"], "local-user")

    def test_profiles_are_independent(self):
        updated = self.client.put(
            "/api/v1/profile",
            headers=self._headers(self.user_a),
            json={"updates": {"name": "Student A Renamed"}, "confirmed_fields": ["name"]},
        )
        self.assertIn(updated.status_code, {200, 201})
        other = self.client.get("/api/v1/profile", headers=self._headers(self.user_b))
        self.assertNotEqual(other.json()["profile"].get("name"), "Student A Renamed")
        own = self.client.get("/api/v1/profile", headers=self._headers(self.user_a))
        self.assertEqual(own.json()["profile"].get("name"), "Student A Renamed")

    def test_dossier_items_never_leak_across_users(self):
        created = self.client.post(
            "/api/v1/dossier/items",
            headers=self._headers(self.user_a),
            json={
                "item_type": "user_opinion",
                "field_path": "strengths.cad",
                "value": "Strong SolidWorks CAD skills",
                "evidence": [{"source": "resume", "detail": "CAD section"}],
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        other = self.client.get("/api/v1/dossier", headers=self._headers(self.user_b))
        self.assertEqual(other.json().get("items", []), [])
        own = self.client.get("/api/v1/dossier", headers=self._headers(self.user_a))
        self.assertTrue(any(item["id"] == created.json()["id"] for item in own.json().get("items", [])))

    def test_answer_library_is_per_user(self):
        saved = self.client.post(
            "/api/v1/preparation/answers",
            headers=self._headers(self.user_a),
            json={"question": "Why this company?", "answer": "Because of its robotics mission."},
        )
        self.assertIn(saved.status_code, {200, 201})
        other = self.client.get("/api/v1/preparation/answers", headers=self._headers(self.user_b))
        self.assertEqual(other.json()["items"], [])
        own = self.client.get("/api/v1/preparation/answers", headers=self._headers(self.user_a))
        self.assertGreaterEqual(own.json()["total"], 1)

    def test_mock_interview_recordings_are_private(self):
        interview = self.client.post(
            "/api/v1/preparation/interviews",
            headers=self._headers(self.user_a),
            json={"opportunity_id": "job-b"},
        )
        self.assertEqual(interview.status_code, 201, interview.text)
        question_id = interview.json()["questions"][0]["id"]
        recorded = self.client.post(
            f"/api/v1/preparation/questions/{question_id}/recorded-answers",
            headers=self._headers(self.user_a),
            data={"answer_text": "A reviewed private answer.", "transcript": "A reviewed private answer."},
            files={"audio": ("answer.webm", b"\x1aE\xdf\xa3private", "audio/webm")},
        )
        self.assertEqual(recorded.status_code, 201, recorded.text)
        answer_id = recorded.json()["id"]
        own = self.client.get(
            f"/api/v1/preparation/answers/{answer_id}/audio",
            headers=self._headers(self.user_a),
        )
        self.assertEqual(own.status_code, 200)
        other = self.client.get(
            f"/api/v1/preparation/answers/{answer_id}/audio",
            headers=self._headers(self.user_b),
        )
        self.assertEqual(other.status_code, 404)
        listed = self.client.get("/api/v1/preparation/interviews", headers=self._headers(self.user_b))
        self.assertEqual(listed.json()["items"], [])
        self.assertEqual(
            self.client.get("/api/v1/preparation/interviews", headers=self._headers(self.user_a)).json()["items"][0]["recordings"], 1,
        )

    def test_agent_threads_are_private_and_cross_access_is_404(self):
        thread = self.client.post(
            "/api/v1/agent/threads",
            headers=self._headers(self.user_a),
            json={"title": "A's plan"},
        )
        self.assertIn(thread.status_code, {200, 201})
        thread_id = thread.json()["id"]
        listed_b = self.client.get("/api/v1/agent/threads", headers=self._headers(self.user_b))
        self.assertNotIn(thread_id, [item["id"] for item in listed_b.json()["items"]])
        direct_b = self.client.get(f"/api/v1/agent/threads/{thread_id}", headers=self._headers(self.user_b))
        self.assertEqual(direct_b.status_code, 404)

    def test_apply_sessions_are_isolated_with_issued_tokens(self):
        synced = self.client.post(
            "/api/v1/apply-sessions",
            headers=self._headers(self.user_a),
            json={
                "session_id": f"ext-{self.user_a['user_id']}",
                "page_url": "https://jobs.example.com/apply/1",
                "ats_type": "generic",
                "fields": [],
                "status": "draft",
            },
        )
        self.assertEqual(synced.status_code, 200)
        other = self.client.get("/api/v1/apply-sessions", headers=self._headers(self.user_b))
        self.assertEqual(other.json()["items"], [])
        own = self.client.get("/api/v1/apply-sessions", headers=self._headers(self.user_a))
        self.assertEqual(own.json()["total"], 1)

    def test_student_password_login_gives_a_working_browser_session(self):
        # Start from a signed-in owner browser to prove the student login
        # cannot inherit the owner cookie.
        owner = self.client.post("/api/v1/session", json={"token": "owner-static-token"})
        self.assertEqual(owner.status_code, 200, owner.text)

        login = self.client.post(
            "/api/v1/session", json={"email": "a@example.com", "password": "PasswordA123"}
        )
        self.assertEqual(login.status_code, 200, login.text)
        self.assertNotIn("pipeline_session", self.client.cookies)
        self.assertIn("pipeline_user_session", self.client.cookies)

        session = self.client.get("/api/v1/session")
        self.assertEqual(session.status_code, 200, session.text)
        self.assertEqual(session.json()["user_id"], self.user_a["user_id"])
        self.assertEqual(self.client.get("/api/v1/opportunities").status_code, 200)

        origin = {"Origin": "http://testserver"}
        without_csrf = self.client.post(
            "/api/v1/opportunities/job-b/actions", headers=origin, json={"action": "saved"}
        )
        self.assertEqual(without_csrf.status_code, 403, "cookie writes must require the CSRF token")
        csrf = self.client.cookies.get("pipeline_csrf")
        with_csrf = self.client.post(
            "/api/v1/opportunities/job-b/actions",
            headers={**origin, "X-CSRF-Token": csrf},
            json={"action": "saved"},
        )
        self.assertEqual(with_csrf.status_code, 200, with_csrf.text)
        owner_detail = self.client.get(
            "/api/v1/opportunities/job-b", headers={"Authorization": "Bearer owner-static-token"}
        ).json()
        self.assertNotEqual(owner_detail["intent_state"], "saved", "the save must land on the student")

        session_token = self.client.cookies.get("pipeline_user_session")
        logout = self.client.delete("/api/v1/session")
        self.assertEqual(logout.status_code, 204)
        self.assertEqual(self.client.get("/api/v1/session").status_code, 401)
        replayed = self.client.get(
            "/api/v1/session", headers={"Authorization": f"Bearer {session_token}"}
        )
        self.assertEqual(replayed.status_code, 401, "signing out must revoke the session token")

    def test_blank_profiles_are_reported_as_not_personalized(self):
        listing = self.client.get("/api/v1/opportunities", headers=self._headers(self.user_a))
        self.assertEqual(listing.status_code, 200, listing.text)
        self.assertFalse(listing.json()["personalized"])
        self.assertTrue(all(not item["reasons"] for item in listing.json()["items"]))

        updated = self.client.put(
            "/api/v1/profile",
            headers=self._headers(self.user_a),
            json={"updates": {"skills": ["SolidWorks"]}, "confirmed_fields": ["skills"]},
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        own = self.client.get("/api/v1/opportunities", headers=self._headers(self.user_a)).json()
        other = self.client.get("/api/v1/opportunities", headers=self._headers(self.user_b)).json()
        self.assertTrue(own["personalized"])
        self.assertTrue(any(item["reasons"] for item in own["items"]))
        self.assertFalse(other["personalized"], "one student's profile must not personalize another's")

    def test_opportunity_intent_and_scores_are_scoped_per_user(self):
        saved = self.client.post(
            "/api/v1/opportunities/job-b/actions",
            headers=self._headers(self.user_a),
            json={"action": "saved"},
        )
        self.assertEqual(saved.status_code, 200, saved.text)
        own_detail = self.client.get(
            "/api/v1/opportunities/job-b", headers=self._headers(self.user_a)
        ).json()
        other_detail = self.client.get(
            "/api/v1/opportunities/job-b", headers=self._headers(self.user_b)
        ).json()
        self.assertEqual(own_detail["intent_state"], "saved")
        self.assertEqual(other_detail["intent_state"], "")
        self.assertNotEqual(
            other_detail["score"],
            76,
            "another user must not inherit the migrated owner's fit score",
        )
        self.assertEqual(
            other_detail["score"],
            own_detail["score"],
            "two untouched profiles should receive the same deterministic baseline",
        )
        own_saved = self.client.get(
            "/api/v1/opportunities",
            headers=self._headers(self.user_a),
            params={"intent_state": "saved"},
        ).json()
        other_saved = self.client.get(
            "/api/v1/opportunities",
            headers=self._headers(self.user_b),
            params={"intent_state": "saved"},
        ).json()
        self.assertIn("job-b", [item["id"] for item in own_saved["items"]])
        self.assertNotIn("job-b", [item["id"] for item in other_saved["items"]])
        own_undecided = self.client.get(
            "/api/v1/opportunities",
            headers=self._headers(self.user_a),
            params={"intent_state": "undecided"},
        ).json()
        other_undecided = self.client.get(
            "/api/v1/opportunities",
            headers=self._headers(self.user_b),
            params={"intent_state": "undecided"},
        ).json()
        self.assertNotIn("job-b", [item["id"] for item in own_undecided["items"]])
        self.assertIn("job-b", [item["id"] for item in other_undecided["items"]])

        passed = self.client.post(
            "/api/v1/opportunities/job-b/actions",
            headers=self._headers(self.user_a),
            json={"action": "passed"},
        )
        self.assertEqual(passed.status_code, 200, passed.text)
        discover = self.client.get(
            "/api/v1/opportunities",
            headers=self._headers(self.user_a),
            params={"exclude_passed": True},
        ).json()
        self.assertNotIn("job-b", [item["id"] for item in discover["items"]])

    def test_account_deletion_removes_only_the_authenticated_users_files(self):
        a_resume = self.resume_storage / "a.pdf"
        b_resume = self.resume_storage / "b.pdf"
        a_capture = self.capture_storage / "a.png"
        b_capture = self.capture_storage / "b.png"
        for path in (a_resume, b_resume, a_capture, b_capture):
            path.write_bytes(b"private test data")

        with closing(sqlite3.connect(self.platform_path)) as conn:
            timestamp = "2026-08-23T00:00:00+00:00"
            for owner, suffix in ((self.user_a["user_id"], "a"), (self.user_b["user_id"], "b")):
                conn.execute(
                    """
                    INSERT INTO resume_files(id, user_id, original_name, media_type, byte_size, sha256, storage_path, created_at)
                    VALUES(?, ?, ?, 'application/pdf', 17, ?, ?, ?)
                    """,
                    (f"resume-file-{suffix}", owner, f"{suffix}.pdf", f"digest-{suffix}", f"{suffix}.pdf", timestamp),
                )
                conn.execute(
                    """
                    INSERT INTO opportunity_captures(id, user_id, source_type, original_name, media_type, storage_path, created_at)
                    VALUES(?, ?, 'screenshot', ?, 'image/png', ?, ?)
                    """,
                    (f"capture-{suffix}", owner, f"{suffix}.png", f"{suffix}.png", timestamp),
                )
            conn.commit()

        deleted = self.client.delete(
            "/api/v1/account",
            headers={**self._headers(self.user_a), "X-Confirm-Delete": "DELETE"},
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json()["files_removed"], 2)
        self.assertFalse(a_resume.exists())
        self.assertFalse(a_capture.exists())
        self.assertTrue(b_resume.exists())
        self.assertTrue(b_capture.exists())
        self.assertEqual(
            self.client.get("/api/v1/profile", headers=self._headers(self.user_b)).status_code,
            200,
        )


if __name__ == "__main__":
    unittest.main()
