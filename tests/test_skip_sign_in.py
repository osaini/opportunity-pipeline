"""PIPELINE_SKIP_SIGN_IN=1: this computer's browser opens the app signed in, and nothing else does."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR
from opportunity_app.api import create_app
from opportunity_app.web import context
from opportunity_app.core.schema import LOCAL_USER_ID

from helpers_platform import build_and_migrate

OWNER = "skip-sign-in-owner-token"
LOOPBACK = list(context.LOOPBACK_HOSTS)
THIS_COMPUTER = ("127.0.0.1", 50000)
ORIGIN = "http://127.0.0.1:8765"
# Set every variable explicitly so a .env beside the checkout cannot fill one in.
SKIP_ON = {"PIPELINE_SKIP_SIGN_IN": "1", "PIPELINE_ENV": "development"}


def owner_cookie(response):
    return next((value for value in response.headers.get_list("set-cookie") if value.startswith("pipeline_session=")), None)


class SkipSignInTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        _, self.platform_path = build_and_migrate(Path(self.tempdir.name))

    def tearDown(self):
        self.tempdir.cleanup()

    def build(self, environment, allowed_hosts=LOOPBACK):
        with mock.patch.dict(os.environ, environment):
            return create_app(
                db_path=self.platform_path, access_token=OWNER, employer_token="employer-token",
                static_dir=STATIC_DIR, allowed_hosts=allowed_hosts,
            )

    def browser(self, app, client=THIS_COMPUTER, base_url=ORIGIN):
        return TestClient(app, base_url=base_url, client=client)

    def test_opening_a_page_signs_this_computer_in_for_thirty_days(self):
        app = self.build(SKIP_ON)
        with self.browser(app) as browser:
            self.assertEqual(browser.get("/api/v1/session").status_code, 401)
            page = browser.get("/")
            self.assertEqual(page.status_code, 200)
            self.assertIn(f"Max-Age={context.LAUNCH_SESSION_SECONDS}", owner_cookie(page))
            session = browser.get("/api/v1/session")
            self.assertEqual(session.status_code, 200, session.text)
            self.assertEqual(session.json()["user_id"], LOCAL_USER_ID)

    def test_a_deep_link_signs_in_too(self):
        app = self.build(SKIP_ON)
        with self.browser(app) as browser:
            self.assertIsNotNone(owner_cookie(browser.get("/opportunities/anything")))
            self.assertIsNotNone(owner_cookie(browser.get("/outreach")))

    def test_writes_still_need_the_csrf_header(self):
        app = self.build(SKIP_ON)
        with self.browser(app) as browser:
            browser.get("/")
            body = {"updates": {"skills": ["CAD"]}, "confirmed_fields": []}
            refused = browser.put("/api/v1/profile", json=body, headers={"Origin": ORIGIN})
            self.assertEqual(refused.status_code, 403)
            accepted = browser.put(
                "/api/v1/profile", json=body,
                headers={"Origin": ORIGIN, "X-CSRF-Token": browser.cookies.get("pipeline_csrf")},
            )
            self.assertEqual(accepted.status_code, 200, accepted.text)

    def test_off_unless_turned_on(self):
        for value in ("", "0", "true"):
            with self.subTest(value=value):
                app = self.build({"PIPELINE_SKIP_SIGN_IN": value, "PIPELINE_ENV": "development"})
                with self.browser(app) as browser:
                    self.assertIsNone(owner_cookie(browser.get("/")))
                    self.assertEqual(browser.get("/api/v1/session").status_code, 401)

    def test_ignored_by_a_server_that_answers_to_other_names_or_runs_in_production(self):
        cases = {
            "no host allowlist": (SKIP_ON, None),
            "a non-loopback name": (SKIP_ON, LOOPBACK + ["pipeline.example"]),
            "production": ({"PIPELINE_SKIP_SIGN_IN": "1", "PIPELINE_ENV": "production"}, LOOPBACK),
        }
        for name, (environment, allowed_hosts) in cases.items():
            with self.subTest(name):
                app = self.build(environment, allowed_hosts=allowed_hosts)
                self.assertFalse(app.state.ctx.config.skip_sign_in)
                with self.browser(app) as browser:
                    self.assertIsNone(owner_cookie(browser.get("/")))

    def test_another_machine_is_not_signed_in(self):
        app = self.build(SKIP_ON)
        with self.browser(app, client=("192.168.1.20", 50000)) as browser:
            self.assertIsNone(owner_cookie(browser.get("/")))
            self.assertEqual(browser.get("/api/v1/session").status_code, 401)

    def test_a_rebound_dns_name_is_not_signed_in(self):
        app = self.build(SKIP_ON)
        with self.browser(app, base_url="http://attacker.example:8765") as browser:
            page = browser.get("/")
            self.assertEqual(page.status_code, 400)
            self.assertIsNone(owner_cookie(page))

    def test_a_student_signed_in_to_their_own_account_keeps_it(self):
        app = self.build(SKIP_ON)
        with self.browser(app) as browser:
            browser.cookies.set("pipeline_user_session", "a-student-session")
            page = browser.get("/")
            self.assertIsNone(owner_cookie(page))
            self.assertEqual(browser.cookies.get("pipeline_user_session"), "a-student-session")


if __name__ == "__main__":
    unittest.main()
