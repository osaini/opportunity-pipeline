"""Apply for me's read-only check and settings over HTTP (api.py /api/v1/apply-agent/*), and the client behind the check.

No browser and no network: the check is served a fictional listing from memory (tests/apply_fake_ats.py), and a
test proves that no socket is opened while it runs, with the sandbox's own wiring too.
"""

import gzip
import importlib.util
import io
import json
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
from contextlib import closing
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR, apply_runs, apply_schema_client, automation
from opportunity_app.api import create_app
from opportunity_app.apply_schema_client import GreenhouseSchemaClient, SchemaUnavailable
from opportunity_app.profile import update_profile
from opportunity_app.schema import connect_product, utc_now

from apply_fake_ats import FakeApplyAgentFactory, FakeSchemaClient, JOB_URL
from helpers_platform import build_and_migrate

USER = "local-user"
TOKEN = "apply-api-owner"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
REPO = Path(__file__).resolve().parent.parent
ACME = "job-a"


def seed_student(conn, resumes):
    """A confirmed name for applications, email and résumé (fictional), so the switch may be turned on."""
    update_profile(conn, {"name_parts": {"first": "Sam", "last": "Rivera", "preferred": ""}, "contact": {"email": "sam.rivera@example.test"}},
                   ["name_parts", "contact"], user_id=USER)
    resumes.mkdir(parents=True, exist_ok=True)
    data = b"%PDF-1.4 a fictional resume"
    (resumes / "resume-file-1.pdf").write_bytes(data)
    stamp = utc_now()
    with conn:
        conn.execute(
            "INSERT INTO resume_files(id, user_id, original_name, media_type, byte_size, sha256, storage_path, created_at) VALUES('resume-file-1', ?, 'Resume.pdf', 'application/pdf', ?, ?, 'resume-file-1.pdf', ?)",
            (USER, len(data), __import__("hashlib").sha256(data).hexdigest(), stamp))
        conn.execute("INSERT INTO resume_versions(id, resume_file_id, user_id, extracted_text, status, created_at, confirmed_at) VALUES('resume-1', 'resume-file-1', ?, 't', 'confirmed', ?, ?)", (USER, stamp, stamp))


class ApplyApiCase(unittest.TestCase):
    with_factories = True

    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.root = Path(tempdir.name)
        _, self.path = build_and_migrate(self.root)
        self.addCleanup(apply_runs.configure_agent_factory, None)
        self.schema = FakeSchemaClient(any_job=True)
        kwargs = {"apply_schema_client_factory": lambda: self.schema, "apply_agent_factory": FakeApplyAgentFactory()} if self.with_factories else {}
        app = create_app(db_path=self.path, access_token=TOKEN, static_dir=STATIC_DIR, resume_storage=self.root / "resumes", **kwargs)
        self.client = self.enterContext(TestClient(app))
        self.conn = connect_product(self.path)
        self.addCleanup(self.conn.close)

    def get(self, path, **kwargs):
        return self.client.get(path, headers=AUTH, **kwargs)

    def post(self, path, body):
        return self.client.post(path, headers=AUTH, json=body)

    def greenhouse_role(self):
        with self.conn:
            self.conn.execute("UPDATE opportunities SET url=? WHERE id=?", (JOB_URL, ACME))

    def turn_on(self):
        seed_student(self.conn, self.root / "resumes")
        response = self.client.put("/api/v1/automation/settings", headers=AUTH, json={"modes": {"apply_agent": "on"}})
        self.assertEqual(response.status_code, 200, response.text)

    def check(self, opportunity_id=ACME):
        return self.get(f"/api/v1/apply-agent/opportunities/{opportunity_id}/check")


class NoFactoryTests(ApplyApiCase):
    with_factories = False

    def test_without_a_schema_client_and_agent_the_check_and_the_answer_routes_answer_503_at_once(self):
        self.greenhouse_role()
        for response in (self.check(), self.post(f"/api/v1/apply-agent/opportunities/{ACME}/answers", {"key": "question_4000000101", "answer": "x"})):
            self.assertEqual(response.status_code, 503, response.text)
            self.assertEqual(response.json()["detail"], "Apply for me runs only in your own app, with Playwright installed.")
        self.assertEqual(self.schema.calls, [], "no request was made")

    def test_the_switch_says_the_same_thing_and_cannot_be_turned_on(self):
        seed_student(self.conn, self.root / "resumes")
        settings = self.get("/api/v1/apply-agent/settings").json()
        self.assertEqual((settings["mode"], settings["requirement"]), ("off", apply_runs.NOT_HERE))
        response = self.client.put("/api/v1/automation/settings", headers=AUTH, json={"modes": {"apply_agent": "on"}})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(automation.mode(self.conn, USER, "apply_agent"), "off")


class CheckRouteTests(ApplyApiCase):
    def test_it_is_off_by_default_and_the_route_says_what_it_needs(self):
        self.greenhouse_role()
        response = self.check()
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], apply_runs.setup_requirement(self.conn, USER), "the sentence for the first thing still missing")
        self.assertTrue(response.json()["detail"])
        seed_student(self.conn, self.root / "resumes")
        self.assertEqual(self.check().json()["detail"], "Apply for me is off. Turn it on under Automation")
        self.assertEqual(self.schema.calls, [], "nothing was fetched while it was off")

    def test_the_fake_factory_turns_the_switch_on_even_on_a_linux_box_with_no_display(self):
        # The display is the factory's to answer (12.6): CI runs the browserless suites headless on Linux.
        self.greenhouse_role()
        with mock.patch.object(apply_runs.sys, "platform", "linux"), mock.patch.dict("os.environ", {"DISPLAY": "", "WAYLAND_DISPLAY": ""}):
            self.turn_on()
            self.assertEqual(self.check().status_code, 200)
            self.assertEqual(apply_runs.setup_requirement(self.conn, USER), "")

    def test_it_needs_a_sign_in(self):
        self.assertEqual(self.client.get(f"/api/v1/apply-agent/opportunities/{ACME}/check").status_code, 401)
        self.assertEqual(self.client.get("/api/v1/apply-agent/settings").status_code, 401)

    def test_a_saved_greenhouse_role_shows_what_is_missing(self):
        self.greenhouse_role()
        self.turn_on()
        response = self.check()
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual((payload["ats"], payload["board_token"], payload["job_id"]), ("greenhouse", "examplerobotics", "4000000001"))
        self.assertEqual(payload["status"], "needs_you")
        keys = {item["key"]: item for item in payload["problems"]}
        self.assertEqual(keys["question_4000000101"]["action"]["type"], "answer")
        self.assertEqual(keys["question_4000000103"]["action"]["options"], ["Perception", "Controls", "Firmware"])
        self.assertEqual(keys["question_4000000105"]["action"]["type"], "manual")
        self.assertNotIn("question_4000000112", keys, "an optional follow-up is not a problem")
        self.assertNotIn("Sam", json.dumps(payload))

    def test_a_role_that_is_not_greenhouse_says_so_and_a_role_that_does_not_exist_is_404(self):
        self.turn_on()
        payload = self.check("job-b").json()
        self.assertEqual((payload["status"], payload["message"]), ("unavailable", "Apply for me works with Greenhouse postings only, for now"))
        self.assertEqual(self.check("no-such-role").status_code, 404)

    def test_a_closed_posting_and_greenhouse_being_down_are_told_apart(self):
        self.greenhouse_role()
        self.turn_on()
        self.schema.closed = True
        self.assertEqual(self.check().json()["message"], "The app couldn't find this posting on Greenhouse. It may be closed")
        self.schema.closed = False

        def down(board, job):
            raise SchemaUnavailable("down")

        self.schema.fetch = down
        self.assertEqual(self.check().json()["message"], "Greenhouse did not answer. Try again later")

    def test_opening_the_section_writes_nothing_and_asks_greenhouse_once_an_hour(self):
        self.greenhouse_role()
        self.turn_on()
        self.conn.commit()
        before = self.snapshot()
        for _ in range(5):
            self.assertEqual(self.check().status_code, 200)
        self.assertEqual(self.snapshot(), before, "no application, no event, no interaction, no run")
        self.assertEqual(len(self.schema.calls), 1)

    def snapshot(self):
        with closing(sqlite3.connect(self.path)) as other:
            tables = [name for (name,) in other.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            # request logs and rate windows are the server's own, not the student's data
            skip = {"request_traces", "audit_events"}
            return {name: other.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in tables if name not in skip}

    def test_no_socket_is_opened_by_the_check_or_by_saving_an_answer(self):
        self.greenhouse_role()
        self.turn_on()
        boom = AssertionError("the check reached for the network")
        with mock.patch("socket.socket.connect", side_effect=boom), mock.patch("socket.create_connection", side_effect=boom), \
                mock.patch("urllib.request.urlopen", side_effect=boom):
            self.assertEqual(self.check().status_code, 200)
            response = self.post(f"/api/v1/apply-agent/opportunities/{ACME}/answers", {"key": "question_4000000103", "answer": "Controls"})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.get("/api/v1/apply-agent/settings").status_code, 200)


class AnswerRouteTests(ApplyApiCase):
    def setUp(self):
        super().setUp()
        self.greenhouse_role()
        self.turn_on()

    def url(self):
        return f"/api/v1/apply-agent/opportunities/{ACME}/answers"

    def test_an_answer_is_saved_for_this_company_and_the_fresh_check_comes_back(self):
        response = self.post(self.url(), {"key": "question_4000000101", "answer": "I build robot arms", "reusable": False})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertNotIn("question_4000000101", [item["key"] for item in body["check"]["problems"]])
        row = self.conn.execute("SELECT question, answer, company, tags_json FROM answer_library WHERE id=?", (body["answer_id"],)).fetchone()
        self.assertEqual((row["question"], row["answer"], row["company"], row["tags_json"]),
                         ("Why do you want to work at Example Robotics?", "I build robot arms", "Acme Robotics", "[]"))

    def test_refusals_are_422_with_a_reason(self):
        for body, needle in (
            ({"key": "question_4000000103", "answer": "Hardware"}, "not one of the form's options"),
            ({"key": "question_4000000105", "answer": "Yes"}, "kind of question"),
            ({"key": "question_4000000111", "answer": "No", "reusable": True}, "depends on the company"),
            ({"key": "question_1", "answer": "x"}, "no longer asks"),
            ({"key": "question_4000000101", "answer": "   "}, "Type an answer"),
        ):
            with self.subTest(body=body):
                response = self.post(self.url(), body)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertIn(needle, response.json()["detail"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM answer_library").fetchone()[0], 0)

    def test_the_body_is_validated(self):
        for body in ({}, {"key": "", "answer": "x"}, {"key": "k"}, {"key": "k", "answer": 5}):
            self.assertEqual(self.post(self.url(), body).status_code, 422, body)

    def test_an_unknown_role_is_404(self):
        self.assertEqual(self.post("/api/v1/apply-agent/opportunities/nope/answers", {"key": "k", "answer": "x"}).status_code, 404)


class SettingsRouteTests(ApplyApiCase):
    def test_the_settings_show_the_limits_in_force_and_which_are_the_students_own(self):
        settings = self.get("/api/v1/apply-agent/settings").json()
        limits = {item["key"]: item for item in settings["limits"]}
        self.assertEqual((limits["spacing_minutes"]["value"], limits["daily_cap"]["value"], limits["company_days"]["value"],
                          limits["rehearsals_per_day"]["value"], limits["rehearsals_before_submit"]["value"]), (10, 5, 30, 20, 3))
        self.assertFalse(any(item["overridden"] for item in settings["limits"]))
        with self.conn:
            self.conn.execute("INSERT INTO profiles(user_id, profile_json, created_at, updated_at) VALUES('local-user', ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET profile_json=excluded.profile_json",
                              (json.dumps({"apply_agent": {"daily_cap": 2}}), utc_now(), utc_now()))
        limits = {item["key"]: item for item in self.get("/api/v1/apply-agent/settings").json()["limits"]}
        self.assertEqual((limits["daily_cap"]["value"], limits["daily_cap"]["overridden"], limits["company_days"]["overridden"]), (2, True, False))
        self.assertEqual(settings["evidence_days"], 90)
        self.assertIn("school", settings["label_fields"])

    def test_an_option_label_is_saved_listed_replaced_and_deleted(self):
        put = self.client.put("/api/v1/apply-agent/ats-labels/school", headers=AUTH, json={"label": "University of Example - City"})
        self.assertEqual((put.status_code, put.json()["label"]), (200, "University of Example - City"))
        self.client.put("/api/v1/apply-agent/ats-labels/school", headers=AUTH, json={"label": "The University of Example at City"})
        self.assertEqual(self.get("/api/v1/apply-agent/settings").json()["ats_labels"]["school"]["label"], "The University of Example at City")
        self.assertEqual(self.client.delete("/api/v1/apply-agent/ats-labels/school", headers=AUTH).status_code, 204)
        self.assertEqual(self.client.delete("/api/v1/apply-agent/ats-labels/school", headers=AUTH).status_code, 404)
        self.assertEqual(self.get("/api/v1/apply-agent/settings").json()["ats_labels"], {})

    def test_an_unknown_list_or_an_empty_label_is_422(self):
        self.assertEqual(self.client.put("/api/v1/apply-agent/ats-labels/favorite", headers=AUTH, json={"label": "x"}).status_code, 422)
        self.assertEqual(self.client.put("/api/v1/apply-agent/ats-labels/school", headers=AUTH, json={"label": ""}).status_code, 422)

    def test_the_routes_are_in_the_openapi_schema(self):
        paths = self.client.get("/openapi.json").json()["paths"]
        for path, method in (("/api/v1/apply-agent/opportunities/{opportunity_id}/check", "get"),
                             ("/api/v1/apply-agent/opportunities/{opportunity_id}/answers", "post"),
                             ("/api/v1/apply-agent/settings", "get"), ("/api/v1/apply-agent/ats-labels/{field}", "put"),
                             ("/api/v1/apply-agent/ats-labels/{field}", "delete")):
            self.assertIn(method, paths[path])


class SandboxWiringTests(unittest.TestCase):
    """PIPELINE_SANDBOX_FAKE_APPLY: the sandbox shows the missing-answers view for Acme Robotics, with no network and no browser."""

    def test_the_seeded_role_becomes_a_greenhouse_role_and_the_switch_can_be_on(self):
        spec = importlib.util.spec_from_file_location("serve_for_testing", REPO / "scripts" / "serve_for_testing.py")
        module = importlib.util.module_from_spec(spec)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            _, path = build_and_migrate(root)
            with mock.patch.dict("os.environ", {module_flag(): "1"}):
                spec.loader.exec_module(module)
                self.assertTrue(module.fake_apply_enabled())
            module.seed_fake_apply(path, root / "resumes")
            self.addCleanup(apply_runs.configure_agent_factory, None)
            app = create_app(db_path=path, access_token=TOKEN, static_dir=STATIC_DIR, resume_storage=root / "resumes",
                             apply_schema_client_factory=lambda: FakeSchemaClient(any_job=True), apply_agent_factory=FakeApplyAgentFactory())
            with TestClient(app) as client:
                conn = connect_product(path)
                try:
                    self.assertEqual(automation.set_mode(conn, USER, "apply_agent", "on"), "on")
                finally:
                    conn.close()
                boom = AssertionError("the sandbox reached for the network")
                with mock.patch("socket.socket.connect", side_effect=boom), mock.patch("urllib.request.urlopen", side_effect=boom):
                    payload = client.get(f"/api/v1/apply-agent/opportunities/{ACME}/check", headers=AUTH).json()
                    other = client.get("/api/v1/apply-agent/opportunities/job-b/check", headers=AUTH).json()
        self.assertEqual(payload["ats"], "greenhouse")
        self.assertEqual(payload["status"], "needs_you")
        self.assertTrue(payload["problems"])
        self.assertEqual(other["status"], "unavailable", "Orbit Systems stays a non-Greenhouse role")

    def test_the_flag_is_off_unless_it_is_exactly_one(self):
        spec = importlib.util.spec_from_file_location("serve_for_testing", REPO / "scripts" / "serve_for_testing.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for value, expected in (("", False), ("0", False), ("true", False), ("1", True)):
            with mock.patch.dict("os.environ", {module.FAKE_APPLY_ENV: value}):
                self.assertEqual(module.fake_apply_enabled(), expected, value)


def module_flag():
    return "PIPELINE_SANDBOX_FAKE_APPLY"


class SchemaClientTests(unittest.TestCase):
    """The real client, with urlopen replaced: one GET to the public listing, TLS as urllib does it, 20 seconds."""

    class Response:
        def __init__(self, body, headers=None):
            self._body = body
            self.headers = headers or {}

        def read(self, limit=-1):
            return self._body[:limit] if limit and limit > 0 else self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fetch(self, opened):
        with mock.patch("urllib.request.urlopen", side_effect=opened if isinstance(opened, Exception) else None, return_value=None if isinstance(opened, Exception) else opened) as urlopen:
            try:
                return GreenhouseSchemaClient().fetch("examplerobotics", "4000000001"), urlopen
            except SchemaUnavailable as exc:
                return exc, urlopen

    def test_it_makes_one_keyless_get_to_the_listing_with_the_pipelines_user_agent_and_a_timeout(self):
        listing, urlopen = self.fetch(self.Response(json.dumps({"id": 1, "questions": []}).encode()))
        self.assertEqual(listing, {"id": 1, "questions": []})
        (request,), kwargs = urlopen.call_args
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.full_url, "https://boards-api.greenhouse.io/v1/boards/examplerobotics/jobs/4000000001?questions=true")
        self.assertEqual(kwargs["timeout"], 20)
        self.assertIsNone(request.data, "no body")
        self.assertNotIn("Authorization", {key.title() for key in request.headers})
        self.assertIn("Opportunity-Pipeline", request.get_header("User-agent"))
        self.assertEqual(urlopen.call_count, 1)

    def test_a_gzipped_listing_is_read(self):
        body = gzip.compress(json.dumps({"id": 2}).encode())
        listing, _ = self.fetch(self.Response(body, {"Content-Encoding": "gzip"}))
        self.assertEqual(listing, {"id": 2})

    def test_a_404_is_none_and_everything_else_is_unavailable(self):
        error = urllib.error.HTTPError("https://x", 404, "Not Found", {}, io.BytesIO(b"{}"))
        self.assertIsNone(self.fetch(error)[0])
        for problem in (urllib.error.HTTPError("https://x", 500, "Server Error", {}, io.BytesIO(b"")), urllib.error.URLError("no route"), TimeoutError("slow"),
                        self.Response(b"<html>not json</html>"), self.Response(b"[1, 2]"), self.Response(b"\xff\xfe"),
                        self.Response(b"x" * (apply_schema_client.MAX_BYTES + 10))):
            with self.subTest(problem=type(problem).__name__):
                self.assertIsInstance(self.fetch(problem)[0], SchemaUnavailable)

    def test_the_default_factory_gives_the_live_client_and_it_reaches_only_the_public_api_host(self):
        self.assertIsInstance(apply_schema_client.default_schema_client_factory(), GreenhouseSchemaClient)
        from opportunity_app.apply_policy import schema_url

        self.assertEqual(schema_url("a", "1").split("/")[2], apply_schema_client.API_HOST)


if __name__ == "__main__":
    unittest.main()
