from __future__ import annotations

import json
import hashlib
from io import BytesIO
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from helpers_platform import build_and_migrate
from opportunity_app import STATIC_DIR
from opportunity_app.api import create_app
from opportunity_app.schema import connect_product
from opportunity_app.document_artifacts import _render_pdf
from pypdf import PdfReader


ORIGIN = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
OTHER_ORIGIN = "chrome-extension://ponmlkjihgfedcbaponmlkjihgfedcba"


class ExtensionApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        _, self.database = build_and_migrate(self.root)
        self.app = create_app(
            db_path=self.database,
            access_token="extension-owner",
            static_dir=STATIC_DIR,
            resume_storage=self.root / "resumes",
            capture_storage=self.root / "captures",
            interview_storage=self.root / "interviews",
            rate_limit_per_minute=10_000,
        )
        self.client_context = TestClient(self.app)
        self.client = self.client_context.__enter__()
        self.owner = {"Authorization": "Bearer extension-owner"}

    def tearDown(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.temporary.cleanup()

    def pair(self) -> tuple[str, str]:
        created = self.client.post("/api/v1/extension/pairings", headers=self.owner)
        self.assertEqual(created.status_code, 201, created.text)
        redeemed = self.client.post(
            "/api/v1/extension/pairings/redeem",
            headers={"Origin": ORIGIN},
            json={"code": created.json()["code"], "device_name": "Test Chrome"},
        )
        self.assertEqual(redeemed.status_code, 200, redeemed.text)
        return redeemed.json()["device_token"], created.json()["code"]

    def extension_headers(self, token: str, origin: str = ORIGIN) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}", "Origin": origin}

    def test_pairing_is_single_use_origin_bound_revocable_and_route_scoped(self) -> None:
        token, code = self.pair()
        replay = self.client.post(
            "/api/v1/extension/pairings/redeem",
            headers={"Origin": ORIGIN},
            json={"code": code},
        )
        self.assertEqual(replay.status_code, 401)
        self.assertEqual(
            self.client.get("/api/v1/profile", headers=self.extension_headers(token)).status_code,
            401,
            "an extension-only token must never authenticate a general student route",
        )
        self.assertEqual(
            self.client.get(
                "/api/v1/extension/application-candidates",
                headers=self.extension_headers(token, OTHER_ORIGIN),
                params={"page_url": "https://jobs.example.com/apply/1"},
            ).status_code,
            401,
        )
        devices = self.client.get("/api/v1/extension/devices", headers=self.owner).json()["items"]
        self.assertEqual(len(devices), 1)
        self.assertNotIn("token_hash", devices[0])
        revoked = self.client.delete(
            f"/api/v1/extension/devices/{devices[0]['id']}", headers=self.owner
        )
        self.assertEqual(revoked.status_code, 204)
        rejected = self.client.get(
            "/api/v1/extension/application-candidates",
            headers=self.extension_headers(token),
            params={"page_url": "https://jobs.example.com/apply/1"},
        )
        self.assertEqual(rejected.status_code, 401)

    def test_pairing_expiry_fails_closed(self) -> None:
        created = self.client.post("/api/v1/extension/pairings", headers=self.owner)
        with closing(connect_product(self.database)) as conn, conn:
            conn.execute(
                "UPDATE extension_pairing_challenges SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                (created.json()["pairing_id"],),
            )
        response = self.client.post(
            "/api/v1/extension/pairings/redeem",
            headers={"Origin": ORIGIN},
            json={"code": created.json()["code"]},
        )
        self.assertEqual(response.status_code, 401)

    def test_pairing_code_guessing_is_rate_limited_without_storing_guesses(self) -> None:
        created = self.client.post("/api/v1/extension/pairings", headers=self.owner).json()
        for index in range(10):
            response = self.client.post(
                "/api/v1/extension/pairings/redeem",
                headers={"Origin": ORIGIN},
                json={"code": f"invalid-pairing-code-{index:02d}"},
            )
            self.assertEqual(response.status_code, 401)
        limited = self.client.post(
            "/api/v1/extension/pairings/redeem",
            headers={"Origin": ORIGIN},
            json={"code": created["code"]},
        )
        self.assertEqual(limited.status_code, 401)
        self.assertIn("Too many", limited.json()["detail"])
        with closing(connect_product(self.database)) as conn:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(extension_pairing_redemption_attempts)")]
            self.assertNotIn("code", columns)

    def test_context_returns_only_confirmed_profile_and_explicit_candidates(self) -> None:
        token, _ = self.pair()
        headers = self.extension_headers(token)
        with closing(connect_product(self.database)) as conn, conn:
            source_url = conn.execute(
                "SELECT o.url FROM applications a JOIN opportunities o ON o.id=a.opportunity_id WHERE a.id='app-job-b'"
            ).fetchone()[0]
            conn.execute(
                "UPDATE profiles SET profile_json=? WHERE user_id='local-user'",
                (json.dumps({"name": "Confirmed Student", "summary": "unconfirmed private draft"}),),
            )
            conn.execute(
                "DELETE FROM profile_facts WHERE user_id='local-user' AND field_path='summary'"
            )
        candidates = self.client.get(
            "/api/v1/extension/application-candidates",
            headers=headers,
            params={"page_url": f"{source_url}?utm_source=test#apply"},
        )
        self.assertEqual(candidates.status_code, 200, candidates.text)
        exact = [item for item in candidates.json()["items"] if item["application_id"] == "app-job-b"]
        self.assertEqual(exact[0]["match_kind"], "exact_url")
        context = self.client.get(
            "/api/v1/extension/apply-context",
            headers=headers,
            params={"application_id": "app-job-b"},
        )
        self.assertEqual(context.status_code, 200, context.text)
        self.assertNotIn("summary", context.json()["confirmed_profile"])
        self.assertFalse(any("citizen" in item["question"].lower() for item in context.json()["answers"]))

    def test_value_free_steps_and_explicit_submission_transition(self) -> None:
        token, _ = self.pair()
        headers = self.extension_headers(token)
        with closing(connect_product(self.database)) as conn, conn:
            conn.execute(
                "UPDATE applications SET stage='applying', applied_at=NULL WHERE id='app-job-b'"
            )
        session = self.client.put(
            "/api/v1/extension/sessions/session-one",
            headers=headers,
            json={
                "application_id": "app-job-b",
                "page_url": "https://jobs.example.com/apply/1",
                "ats_type": "generic",
                "fields": [
                    {
                        "key": "email",
                        "label": "Email",
                        "type": "email",
                        "proposed_value": "must-not-persist@example.com",
                        "filled": True,
                    }
                ],
                "status": "reviewed",
            },
        )
        self.assertEqual(session.status_code, 200, session.text)
        self.assertNotIn("proposed_value", session.json()["fields"][0])
        step = self.client.put(
            "/api/v1/extension/sessions/session-one/steps/contact",
            headers=headers,
            json={
                "page_url": "https://jobs.example.com/apply/1",
                "ats_type": "generic",
                "fields": session.json()["fields"],
                "summary": {"filled": 1, "failed": 0, "manual": 0, "required_unresolved": 0},
                "status": "filled",
            },
        )
        self.assertEqual(step.status_code, 200, step.text)
        before = self.client.get("/api/v1/applications/app-job-b", headers=self.owner).json()
        self.assertNotEqual(before["stage"], "applied")
        confirmed = self.client.post(
            "/api/v1/extension/sessions/session-one/confirm-submitted", headers=headers
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)
        self.assertFalse(confirmed.json()["inferred"])
        after = self.client.get("/api/v1/applications/app-job-b", headers=self.owner).json()
        self.assertEqual(after["stage"], "applied")

    def test_only_approved_opportunity_bound_artifacts_can_be_downloaded(self) -> None:
        token, _ = self.pair()
        headers = self.extension_headers(token)
        draft = self.client.post(
            "/api/v1/preparation/documents",
            headers=self.owner,
            json={"opportunity_id": "job-b", "document_type": "resume"},
        )
        self.assertEqual(draft.status_code, 201, draft.text)
        context_before = self.client.get(
            "/api/v1/extension/apply-context",
            headers=headers,
            params={"application_id": "app-job-b"},
        ).json()
        self.assertFalse(any(item.get("source_kind") == "approved_generated" for item in context_before["documents"]))

        approved = self.client.post(
            f"/api/v1/preparation/documents/{draft.json()['id']}/approve", headers=self.owner
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        artifact = approved.json()["artifact"]
        downloaded = self.client.get(
            f"/api/v1/extension/artifacts/{artifact['id']}/file",
            headers=headers,
            params={"application_id": "app-job-b"},
        )
        self.assertEqual(downloaded.status_code, 200, downloaded.text)
        self.assertEqual(downloaded.headers["content-type"], "application/pdf")
        self.assertEqual(hashlib.sha256(downloaded.content).hexdigest(), artifact["sha256"])
        self.assertEqual(downloaded.headers["x-artifact-sha256"], artifact["sha256"])
        extracted = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(downloaded.content)).pages)
        self.assertIn("Test Student", extracted)

        with closing(connect_product(self.database)) as conn:
            stored_name = conn.execute(
                "SELECT storage_path FROM generated_document_artifacts WHERE id=?", (artifact["id"],)
            ).fetchone()[0]
        artifact_path = self.root / "resumes" / "generated" / stored_name
        self.assertTrue(artifact_path.exists())
        removed = self.client.delete(
            f"/api/v1/preparation/documents/{draft.json()['id']}", headers=self.owner
        )
        self.assertEqual(removed.status_code, 204)
        self.assertFalse(artifact_path.exists())
        unavailable = self.client.get(
            f"/api/v1/extension/artifacts/{artifact['id']}/file",
            headers=headers,
            params={"application_id": "app-job-b"},
        )
        self.assertEqual(unavailable.status_code, 422)

        other = self.client.post(
            "/api/v1/preparation/documents",
            headers=self.owner,
            json={"opportunity_id": "job-a", "document_type": "cover_letter"},
        )
        other_approved = self.client.post(
            f"/api/v1/preparation/documents/{other.json()['id']}/approve", headers=self.owner
        ).json()["artifact"]
        wrong_opportunity = self.client.get(
            f"/api/v1/extension/artifacts/{other_approved['id']}/file",
            headers=headers,
            params={"application_id": "app-job-b"},
        )
        self.assertEqual(wrong_opportunity.status_code, 422)
        self.assertIn("different opportunity", wrong_opportunity.json()["detail"])

    def test_generated_pdf_export_is_deterministic(self) -> None:
        first = self.root / "first.pdf"
        second = self.root / "second.pdf"
        content = "# Confirmed Student\n\n- University of Texas\n- Mechanical Engineering"
        _render_pdf(content, first)
        _render_pdf(content, second)
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_extension_answer_endpoint_rejects_sensitive_reuse(self) -> None:
        token, _ = self.pair()
        headers = self.extension_headers(token)
        sensitive = self.client.post(
            "/api/v1/extension/answers",
            headers=headers,
            json={"question": "What is your disability status?", "answer": "Prefer not to say"},
        )
        self.assertEqual(sensitive.status_code, 422)
        saved = self.client.post(
            "/api/v1/extension/answers",
            headers=headers,
            json={"question": "Describe a prototype project", "answer": "I designed and tested a fixture."},
        )
        self.assertEqual(saved.status_code, 201, saved.text)

    def test_account_deletion_revokes_device_and_removes_pairing_state(self) -> None:
        token, _ = self.pair()
        deleted = self.client.delete(
            "/api/v1/account",
            headers={**self.owner, "X-Confirm-Delete": "DELETE"},
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        rejected = self.client.get(
            "/api/v1/extension/application-candidates",
            headers=self.extension_headers(token),
            params={"page_url": "https://jobs.example.com/apply"},
        )
        self.assertEqual(rejected.status_code, 401)
        with closing(connect_product(self.database)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM extension_devices").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM extension_pairing_challenges").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
