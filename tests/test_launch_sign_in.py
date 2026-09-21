"""One-click sign-in: the local launcher trades the owner token for a ticket."""

import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR, api
from opportunity_app.api import create_app
from opportunity_app.auth import issue_user_token
from opportunity_app.schema import LOCAL_USER_ID, connect_product

from helpers_platform import build_and_migrate

OWNER = "launch-owner-token"


class LaunchTicketTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        _, self.platform_path = build_and_migrate(Path(self.tempdir.name))
        self.app = create_app(
            db_path=self.platform_path, access_token=OWNER, employer_token="employer-token", static_dir=STATIC_DIR
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def mint(self, client, token=OWNER):
        return client.post("/api/v1/auth/launch-ticket", headers={"Authorization": f"Bearer {token}"})

    def test_a_ticket_signs_the_browser_in_once_and_remembers_it(self):
        with TestClient(self.app) as launcher, TestClient(self.app) as browser:
            minted = self.mint(launcher)
            self.assertEqual(minted.status_code, 200, minted.text)
            ticket = minted.json()["ticket"]
            self.assertEqual(minted.json()["expires_in"], api.LAUNCH_TICKET_SECONDS)

            session = browser.post("/api/v1/session", json={"launch_ticket": ticket})
            self.assertEqual(session.status_code, 200, session.text)
            self.assertEqual(session.json()["user_id"], LOCAL_USER_ID)
            cookies = session.headers.get_list("set-cookie")
            owner_cookie = next(value for value in cookies if value.startswith("pipeline_session="))
            csrf_cookie = next(value for value in cookies if value.startswith("pipeline_csrf="))
            self.assertIn(f"Max-Age={api.LAUNCH_SESSION_SECONDS}", owner_cookie)
            self.assertIn(f"Max-Age={api.LAUNCH_SESSION_SECONDS}", csrf_cookie)
            self.assertEqual(browser.get("/api/v1/session").status_code, 200)
            # Cookie-authenticated writes still need the CSRF header.
            write = browser.put(
                "/api/v1/profile",
                json={"updates": {"skills": ["CAD"]}, "confirmed_fields": []},
                headers={"Origin": "http://testserver"},
            )
            self.assertEqual(write.status_code, 403)

            with TestClient(self.app) as other:
                replay = other.post("/api/v1/session", json={"launch_ticket": ticket})
            self.assertEqual(replay.status_code, 401, "a ticket works once")

    def test_a_typed_token_keeps_the_shorter_session(self):
        with TestClient(self.app) as browser:
            session = browser.post("/api/v1/session", json={"token": OWNER})
        owner_cookie = next(
            value for value in session.headers.get_list("set-cookie") if value.startswith("pipeline_session=")
        )
        self.assertIn(f"Max-Age={60 * 60 * 12}", owner_cookie)

    def test_an_expired_ticket_is_refused(self):
        with TestClient(self.app) as client:
            ticket = self.mint(client).json()["ticket"]
            later = api.time.monotonic() + api.LAUNCH_TICKET_SECONDS + 1
            with mock.patch.object(api.time, "monotonic", return_value=later):
                session = client.post("/api/v1/session", json={"launch_ticket": ticket})
        self.assertEqual(session.status_code, 401)

    def test_an_unknown_ticket_is_refused(self):
        with TestClient(self.app) as client:
            self.assertEqual(client.post("/api/v1/session", json={"launch_ticket": "made-up"}).status_code, 401)

    def test_only_the_owner_token_as_a_bearer_can_mint(self):
        with closing(connect_product(self.platform_path)) as conn:
            owner_api_token = issue_user_token(conn, LOCAL_USER_ID)
        with TestClient(self.app) as client:
            self.assertEqual(client.post("/api/v1/auth/launch-ticket").status_code, 401)
            for token in ("wrong", "employer-token", owner_api_token):
                with self.subTest(token=token[:8]):
                    self.assertEqual(self.mint(client, token).status_code, 401)
            # A signed-in browser cannot mint a ticket with its cookie alone.
            client.post("/api/v1/session", json={"token": OWNER})
            self.assertEqual(client.post("/api/v1/auth/launch-ticket").status_code, 401)


class RecoveryWithoutMailTests(unittest.TestCase):
    def test_a_real_copy_never_answers_recovery_with_the_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, platform_path = build_and_migrate(Path(tmp))
            app = create_app(db_path=platform_path, access_token=OWNER, static_dir=STATIC_DIR)
            with mock.patch.dict("os.environ", {"PIPELINE_NOTIFICATIONS_LIVE": ""}), TestClient(app) as client:
                registered = client.post("/api/v1/auth/register", json={
                    "invite_token": OWNER, "email": "owner@example.com",
                    "password": "StrongPassword123", "display_name": "Owner",
                })
                self.assertEqual(registered.status_code, 201, registered.text)
                known = client.post("/api/v1/auth/recovery", json={"email": "owner@example.com"}).json()
                unknown = client.post("/api/v1/auth/recovery", json={"email": "nobody@example.com"}).json()
        self.assertEqual(known, {"accepted": True, "delivery": "unavailable"})
        self.assertEqual(known, unknown)


class HostAllowlistTests(unittest.TestCase):
    def test_a_loopback_server_answers_only_to_loopback_host_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, platform_path = build_and_migrate(Path(tmp))
            app = create_app(
                db_path=platform_path, access_token=OWNER, static_dir=STATIC_DIR,
                allowed_hosts=list(api.LOOPBACK_HOSTS),
            )
            with TestClient(app, base_url="http://127.0.0.1:8765") as client:
                self.assertEqual(client.get("/api/v1/health").status_code, 200)
                self.assertEqual(
                    client.get("/api/v1/health", headers={"Host": "localhost:8765"}).status_code, 200
                )
                rebound = client.get("/api/v1/health", headers={"Host": "attacker.example:8765"})
                self.assertEqual(rebound.status_code, 400)


if __name__ == "__main__":
    unittest.main()
