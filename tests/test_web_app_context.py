"""What the web layer must keep true however create_app is assembled.

Phase 4 splits the 4,000-line create_app closure into opportunity_app/web/ (a per-app context, module-level routers built once
per process, a dependency module). These tests pin three things that split could change without failing any route test:

* Apps are isolated. Two apps in one process keep their own tokens, databases, sessions and counters, however many of their
  routes are shared objects.
* A handler's own sqlite3.OperationalError stays a 500. The connection dependencies map only a failure to OPEN the database to
  503 ("Product database unavailable"); a mapping that wrapped the yield as well would turn a locked or broken database inside
  a handler into a 503 that tells the student to run the migration command.
* Importing only opportunity_app.api registers every automation handler and breaker group, because the web layer is what
  imports the modules that register them.
"""

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.routing import Mount

from opportunity_app import STATIC_DIR
from opportunity_app.api import create_app

from helpers_platform import build_and_migrate

ROOT = Path(__file__).resolve().parent.parent


def build(root: Path, platform_path: Path, token: str, **overrides):
    return create_app(
        db_path=platform_path,
        access_token=token,
        employer_token=f"{token}-employer",
        admin_token=f"{token}-admin",
        static_dir=STATIC_DIR,
        resume_storage=root / "resumes",
        capture_storage=root / "captures",
        interview_storage=root / "audio",
        apply_storage=root / "apply",
        start_call_prep_worker=False,
        start_inbox_watcher=False,
        start_automation_worker=False,
        **overrides,
    )


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class TwoAppsInOneProcessTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        base = Path(self.tempdir.name)
        self.roots = []
        self.apps = []
        for name in ("alpha", "beta"):
            root = base / name
            root.mkdir()
            _, platform_path = build_and_migrate(root)
            self.roots.append(root)
            self.apps.append(build(root, platform_path, f"{name}-owner-token"))
        self.alpha, self.beta = self.apps

    def test_each_app_accepts_only_its_own_tokens_however_the_requests_alternate(self):
        with TestClient(self.alpha) as alpha, TestClient(self.beta) as beta:
            for _ in range(2):
                self.assertEqual(alpha.get("/api/v1/session", headers=bearer("alpha-owner-token")).status_code, 200)
                self.assertEqual(beta.get("/api/v1/session", headers=bearer("beta-owner-token")).status_code, 200)
                self.assertEqual(alpha.get("/api/v1/session", headers=bearer("beta-owner-token")).status_code, 401)
                self.assertEqual(beta.get("/api/v1/session", headers=bearer("alpha-owner-token")).status_code, 401)
                self.assertEqual(alpha.get("/api/v1/employer/requisitions", headers=bearer("alpha-owner-token-employer")).status_code, 200)
                self.assertEqual(beta.get("/api/v1/employer/requisitions", headers=bearer("alpha-owner-token-employer")).status_code, 401)
                self.assertEqual(alpha.get("/api/v1/admin/overview", headers=bearer("beta-owner-token-admin")).status_code, 401)
                self.assertEqual(beta.get("/api/v1/admin/overview", headers=bearer("beta-owner-token-admin")).status_code, 200)

    def test_a_session_cookie_signed_in_to_one_app_is_not_a_session_in_the_other(self):
        with TestClient(self.alpha) as alpha, TestClient(self.beta) as beta:
            signed_in = alpha.post("/api/v1/session", json={"token": "alpha-owner-token"})
            self.assertEqual(signed_in.status_code, 200)
            self.assertEqual(alpha.get("/api/v1/session").status_code, 200)
            cookie = alpha.cookies.get("pipeline_session")
            self.assertTrue(cookie)
            # The same cookie value presented to the other app is not valid there.
            beta.cookies.set("pipeline_session", cookie)
            self.assertEqual(beta.get("/api/v1/session").status_code, 401)

    def test_each_app_reads_and_writes_its_own_database(self):
        with TestClient(self.alpha) as alpha, TestClient(self.beta) as beta:
            created = alpha.post(
                "/api/v1/outreach", headers=bearer("alpha-owner-token"), json={"company": "Alpha Only Robotics"},
            )
            self.assertEqual(created.status_code, 201, created.text)
            alpha_companies = [item["company"] for item in alpha.get("/api/v1/outreach", headers=bearer("alpha-owner-token")).json()["items"]]
            beta_companies = [item["company"] for item in beta.get("/api/v1/outreach", headers=bearer("beta-owner-token")).json()["items"]]
            self.assertIn("Alpha Only Robotics", alpha_companies)
            self.assertNotIn("Alpha Only Robotics", beta_companies)

    def test_rate_limits_and_launch_tickets_are_per_app(self):
        first = build(self.roots[0], self.roots[0] / "platform.db", "limited-one-token", rate_limit_per_minute=3)
        second = build(self.roots[1], self.roots[1] / "platform.db", "limited-two-token", rate_limit_per_minute=3)
        with TestClient(first) as one, TestClient(second) as two:
            statuses = [one.get("/api/v1/session", headers=bearer("limited-one-token")).status_code for _ in range(5)]
            self.assertEqual(statuses, [200, 200, 200, 429, 429])
            self.assertEqual(
                [two.get("/api/v1/session", headers=bearer("limited-two-token")).status_code for _ in range(3)], [200] * 3,
                "the other app's request window is its own, though both clients are the same host",
            )
        with TestClient(self.alpha) as alpha, TestClient(self.beta) as beta:
            ticket = alpha.post("/api/v1/auth/launch-ticket", headers=bearer("alpha-owner-token")).json()["ticket"]
            self.assertEqual(beta.post("/api/v1/session", json={"launch_ticket": ticket}).status_code, 401, "a ticket is good only in the app that minted it")
            self.assertEqual(alpha.post("/api/v1/session", json={"launch_ticket": ticket}).status_code, 200)

    def test_each_app_keeps_its_own_request_counters(self):
        with TestClient(self.alpha) as alpha, TestClient(self.beta) as beta:
            for _ in range(5):
                alpha.get("/api/v1/health")
            beta.get("/api/v1/health")

            def requests_seen(client, admin_token):
                overview = client.get("/api/v1/admin/overview", headers=bearer(admin_token)).json()
                return overview["metrics"]["requests"] if "metrics" in overview else overview["service"]["requests"]

            self.assertGreater(requests_seen(alpha, "alpha-owner-token-admin"), requests_seen(beta, "beta-owner-token-admin"))


class SharedRouteTableTests(unittest.TestCase):
    """The route table is built once per process and listed by every app; only the per-app parts differ."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        base = Path(self.tempdir.name)
        self.apps = []
        for name in ("one", "two"):
            root = base / name
            root.mkdir()
            _, platform_path = build_and_migrate(root)
            self.apps.append(build(root, platform_path, f"{name}-token"))

    def test_two_apps_list_the_same_route_objects(self):
        first, second = ([route for route in app.routes if isinstance(route, APIRoute)] for app in self.apps)
        self.assertGreater(len(first), 200)
        self.assertEqual(len(first), len(second))
        self.assertTrue(all(a is b for a, b in zip(first, second)), "a route was rebuilt for the second app")

    def test_a_shared_route_holds_no_reference_to_any_app(self):
        # The sharing depends on this FastAPI behaviour: a route created on a standalone APIRouter has no
        # dependency_overrides_provider, so it cannot serve one app's dependency_overrides to another. If a FastAPI upgrade
        # binds routes to an app, this fails and the shared table has to be revisited.
        for route in self.apps[0].routes:
            if isinstance(route, APIRoute):
                self.assertIsNone(route.dependency_overrides_provider, route.path)

    def test_the_assets_mount_and_the_context_belong_to_one_app(self):
        first, second = self.apps
        mounts = [[route for route in app.routes if isinstance(route, Mount)] for app in self.apps]
        self.assertEqual([len(group) for group in mounts], [1, 1])
        self.assertIsNot(mounts[0][0], mounts[1][0])
        self.assertIsNot(first.state.ctx, second.state.ctx)
        for part in ("launch_tickets", "rate_windows", "metrics", "traces", "open_connections", "asset_versions", "apply_schema_cache"):
            self.assertIsNot(getattr(first.state.ctx.runtime, part), getattr(second.state.ctx.runtime, part), part)
        self.assertNotEqual(first.state.ctx.config.access_token, second.state.ctx.config.access_token)
        self.assertEqual(first.state.access_token, first.state.ctx.config.access_token, "main() and the tests read app.state.access_token")
        self.assertIs(first.state.call_prep_worker, first.state.ctx.services.call_prep_worker)

    def test_an_unknown_keyword_is_still_refused(self):
        with self.assertRaises(TypeError):
            create_app(not_an_option=True)


class AHandlersDatabaseErrorIsNotAMissingDatabaseTests(unittest.TestCase):
    """The 503 means "no database to open". A handler that fails on an open one is a bug, so it stays a 500."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        _, self.platform_path = build_and_migrate(self.root)
        self.app = build(self.root, self.platform_path, "pin-owner-token")

    def drop_applications(self):
        with closing(sqlite3.connect(self.platform_path)) as conn:
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute("DROP TABLE IF EXISTS application_events")
            conn.execute("DROP TABLE applications")
            conn.commit()

    def test_a_missing_table_inside_a_writable_handler_is_a_500_not_a_503(self):
        self.drop_applications()
        with TestClient(self.app, raise_server_exceptions=False) as client:
            response = client.get("/api/v1/applications", headers=bearer("pin-owner-token"))
        self.assertEqual(response.status_code, 500, response.text)
        self.assertNotIn("Product database unavailable", response.text)

    def test_a_missing_table_inside_a_read_only_handler_is_a_500_not_a_503(self):
        self.drop_applications()
        with TestClient(self.app, raise_server_exceptions=False) as client:
            response = client.get("/api/v1/stats", headers=bearer("pin-owner-token"))
        self.assertEqual(response.status_code, 500, response.text)
        self.assertNotIn("Product database unavailable", response.text)

    def test_a_missing_database_file_is_still_the_503_the_read_only_dependency_maps(self):
        missing = build(self.root, self.root / "no-such-platform.db", "pin-owner-token")
        with TestClient(missing, raise_server_exceptions=False) as client:
            self.assertEqual(client.get("/api/v1/opportunities", headers=bearer("pin-owner-token")).status_code, 503)
            self.assertEqual(client.get("/api/v1/health").json()["database"], "missing")


class ImportingTheApiRegistersTheAutomationModulesTests(unittest.TestCase):
    def test_a_fresh_interpreter_that_imports_only_the_api_has_every_handler_and_breaker_group(self):
        code = (
            "import json, sys; sys.path.insert(0, %r); import opportunity_app.api; from opportunity_app import automation; "
            "print('REPORT' + json.dumps({'handlers': sorted(automation.HANDLERS), 'breakers': sorted(automation.BREAKER_GROUPS), "
            "'corrections': sorted(automation.CORRECTIONS)}))" % str(ROOT)
        )
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT, timeout=300)
        lines = [line for line in done.stdout.splitlines() if line.startswith("REPORT")]
        self.assertTrue(lines, f"probe failed:\n{done.stdout}\n{done.stderr}")
        report = json.loads(lines[-1][len("REPORT"):])
        for handler in ("application.deadline", "application.capture_proposal", "outreach.status", "outreach.follow_up_draft",
                        "outreach.thank_you", "resume.pick"):
            self.assertIn(handler, report["handlers"])
        self.assertEqual(report["breakers"], ["application_mail", "decline_thank_you"])
        self.assertEqual(report["corrections"], ["application_mail"])


if __name__ == "__main__":
    unittest.main()
