"""End-to-end smoke: signup → match explanation → save → apply-session → digest."""

import json
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR
from opportunity_app.api import create_app
from opportunity_app.operations import enqueue_job, run_next_job
from opportunity_app.schema import connect_product

from helpers_platform import build_and_migrate


RUN_AT = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)


class FakeLiveProvider:
    live = True

    def __init__(self):
        self.calls = []

    def deliver(self, channel, recipient, subject, body):
        self.calls.append({"channel": channel, "recipient": recipient, "subject": subject})
        return {"delivered": True, "detail": "fake"}


class EndToEndSmokeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        _, self.platform_path = build_and_migrate(Path(self.tempdir.name))
        self.app = create_app(
            db_path=self.platform_path,
            access_token="e2e-owner",
            admin_token="e2e-admin",
            static_dir=STATIC_DIR,
        )
        self.client = TestClient(self.app)
        self.client.put(
            "/api/v1/admin/feature-flags/allow_public_signup",
            headers={"Authorization": "Bearer e2e-admin"},
            json={"enabled": True},
        )
        registered = self.client.post(
            "/api/v1/auth/register",
            json={"email": "smoke@example.com", "password": "SmokeTest123", "display_name": "Smoke"},
        )
        self.assertEqual(registered.status_code, 201, registered.text)
        self.token = registered.json()["api_token"]
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def tearDown(self):
        self.client.close()
        self.tempdir.cleanup()

    def test_full_student_journey(self):
        # 1. Session identity resolves to the newly created user.
        session = self.client.get("/api/v1/session", headers=self.headers)
        self.assertEqual(session.status_code, 200)
        self.assertEqual(session.json()["user_id"], registered_user_id(self.platform_path, "smoke@example.com"))

        # 2. Onboarding: scores are per-student, so a blank profile has nothing
        # to explain beyond the base score. Confirm skills and preferences.
        onboarded = self.client.put(
            "/api/v1/profile",
            headers=self.headers,
            json={
                "updates": {"skills": ["SolidWorks"], "preferred_role_types": ["internship"]},
                "confirmed_fields": ["skills", "preferred_role_types"],
            },
        )
        self.assertEqual(onboarded.status_code, 200, onboarded.text)

        # 3. Match explanation: the top opportunity carries reasons + evidence.
        listing = self.client.get("/api/v1/opportunities", headers=self.headers, params={"limit": 5})
        self.assertEqual(listing.status_code, 200)
        top = listing.json()["items"][0]
        self.assertGreater(top["score"], 0)
        self.assertTrue(top["reasons"], "every match must explain itself")

        # 4. Plan/save the role.
        saved = self.client.post(
            f"/api/v1/opportunities/{top['id']}/actions",
            headers=self.headers,
            json={"action": "saved"},
        )
        self.assertIn(saved.status_code, {200, 201})
        detail = self.client.get(f"/api/v1/opportunities/{top['id']}", headers=self.headers)
        self.assertEqual(detail.json()["intent_state"], "saved")

        # 5. Apply-session sync from the extension.
        synced = self.client.post(
            "/api/v1/apply-sessions",
            headers=self.headers,
            json={
                "session_id": "e2e-smoke-session",
                "application_id": None,
                "page_url": "https://jobs.example.com/apply/smoke",
                "ats_type": "generic",
                "fields": [
                    {"key": "field-0", "label": "Email", "type": "email", "provenance": "confirmed_profile.contact.email",
                     "confidence": 0.95, "requires_review": False, "filled": True, "reason": ""}
                ],
                "status": "reviewed",
            },
        )
        self.assertEqual(synced.status_code, 200)

        # 5b. Apply for real: the tracker records the application.
        applied = self.client.post(
            f"/api/v1/opportunities/{top['id']}/actions",
            headers=self.headers,
            json={"action": "apply_opened"},
        )
        self.assertIn(applied.status_code, {200, 201})
        applications = self.client.get("/api/v1/applications", headers=self.headers)
        self.assertEqual(applications.json()["total"], 1)
        application_id = applications.json()["items"][0]["id"]

        # 5c. A monitored update arrives through a signed webhook.
        connected = self.client.post(
            "/api/v1/connections", headers=self.headers, json={"provider": "sandbox"}
        )
        self.assertEqual(connected.status_code, 201)
        connector_id = connected.json()["id"]
        import hashlib
        import hmac as hmac_mod

        payload = json.dumps({
            "connector_id": connector_id,
            "external_id": "smoke-msg-1",
            "subject": "Application confirmation",
            "body": "Your application was received.",
        })
        signature = "sha256=" + hmac_mod.new(b"e2e-webhook-secret", payload.encode(), hashlib.sha256).hexdigest()
        with mock.patch.dict("os.environ", {"PIPELINE_WEBHOOK_SECRET": "e2e-webhook-secret"}, clear=False):
            event = self.client.post(
                "/api/v1/connections/webhook",
                content=payload.encode(),
                headers={**self.headers, "Content-Type": "application/json", "X-Webhook-Signature": signature},
            )
        self.assertEqual(event.status_code, 201, event.text)
        confirmed = self.client.post(
            f"/api/v1/monitored-events/{event.json()['id']}/decision",
            headers=self.headers,
            json={"decision": "confirm", "application_id": application_id},
        )
        self.assertEqual(confirmed.status_code, 200, confirmed.text)

        # 6. Digest delivers through a live-capable provider.
        from opportunity_app.connections import ensure_preferences, queue_notification

        provider = FakeLiveProvider()
        user_id = session.json()["user_id"]
        with closing(connect_product(self.platform_path)) as conn:
            ensure_preferences(conn, user_id=user_id)
            with conn:
                conn.execute(
                    "UPDATE notification_preferences SET email_enabled=1 WHERE user_id=?", (user_id,)
                )
                conn.execute("UPDATE users SET email='smoke@example.com' WHERE id=?", (user_id,))
            queue_notification(
                conn, "email", f"digest-smoke:{application_id}",
                {"subject": "Weekly application digest"}, user_id=user_id,
            )
            enqueue_job(conn, "notification_digest", {}, "e2e-digest")
            with mock.patch("opportunity_app.notifications.build_provider", return_value=provider):
                record = run_next_job(conn, {
                    "notification_digest": lambda payload: _digest(conn, payload),
                })
            self.assertEqual(record["state"], "succeeded")
            statuses = {
                row["event_key"]: row["status"]
                for row in conn.execute("SELECT event_key, status FROM notification_outbox").fetchall()
            }
        self.assertTrue(provider.calls, "the email digest must reach the provider")
        self.assertEqual(statuses[f"digest-smoke:{application_id}"], "sent")

        # 7. No-submit guarantee holds on the synced session payload.
        self.assertFalse(synced.json()["final_submit_available"])


def _digest(conn, payload):
    from opportunity_app.notifications import run_notification_digest

    return run_notification_digest(conn, payload, now=RUN_AT)


def registered_user_id(platform_path: Path, email: str) -> str:
    with closing(connect_product(platform_path, read_only=True)) as conn:
        row = conn.execute("SELECT id FROM users WHERE lower(email)=?", (email,)).fetchone()
    return str(row["id"])


if __name__ == "__main__":
    unittest.main()
