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

from opportunity_app import STATIC_DIR, apply_runs, apply_schema_client, apply_sensitive, automation
from opportunity_app.api import create_app
from opportunity_app.apply_schema_client import GreenhouseSchemaClient, SchemaUnavailable
from opportunity_app.student.profile import update_profile
from opportunity_app.core.database import connect_product
from opportunity_app.core.timestamps import utc_now

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
        self.app = app
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

    def test_the_check_names_the_posting_it_read_and_flags_a_form_that_is_another_companys(self):
        self.greenhouse_role()
        self.turn_on()
        posting = self.check().json()["posting"]
        # The sandbox's fake board answers every job with Example Robotics' listing, so the saved Acme role does not match it.
        self.assertEqual((posting["title"], posting["company"]), ("Robotics Software Intern", "Example Robotics"))
        self.assertTrue(posting["differs"])
        self.assertIn("Example Robotics, not Acme Robotics", posting["difference"])
        self.assertTrue(posting["url"].endswith("/examplerobotics/jobs/4000000001"))
        with self.conn:
            self.conn.execute("UPDATE opportunities SET company='Example Robotics, Inc.', title='Robotics Intern' WHERE id=?", (ACME,))
        posting = self.check().json()["posting"]
        self.assertFalse(posting["differs"], "the employer's own words match, and the titles share a word besides intern")
        with self.conn:
            self.conn.execute("UPDATE opportunities SET title='Marketing Intern' WHERE id=?", (ACME,))
        self.assertIn("not Marketing Intern", self.check().json()["posting"]["difference"])

    def test_a_form_that_looks_like_another_role_waits_for_the_students_word_before_an_answer_is_saved(self):
        self.greenhouse_role()
        self.turn_on()
        url = f"/api/v1/apply-agent/opportunities/{ACME}/answers"
        refused = self.post(url, {"key": "question_4000000101", "answer": "I build robot arms"})
        self.assertEqual(refused.status_code, 422, refused.text)
        self.assertIn("Confirm it is the right posting", refused.json()["detail"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM answer_library").fetchone()[0], 0)
        self.assertEqual(self.post(url, {"key": "question_4000000101", "answer": "I build robot arms", "posting_confirmed": True}).status_code, 200)

    def test_questions_the_app_leaves_to_the_student_are_counted_apart(self):
        self.greenhouse_role()
        self.turn_on()
        first = self.check().json()
        self.assertEqual(first["counts"]["needs_answer"], 3)
        self.assertGreaterEqual(first["counts"]["left_for_you"], 1)
        self.assertRegex(first["message"], r"^3 questions need an answer first\. \d+ more are left for you to answer on the Greenhouse form$")
        url = f"/api/v1/apply-agent/opportunities/{ACME}/answers"
        for key, answer in (("question_4000000101", "I build robot arms"), ("question_4000000103", "Controls"), ("question_4000000111", "No")):
            last = self.post(url, {"key": key, "answer": answer, "posting_confirmed": True}).json()["check"]
        self.assertEqual(last["counts"]["needs_answer"], 0)
        self.assertRegex(last["message"], r"^The app has everything it can fill\. \d+ questions are yours to answer on the Greenhouse form$")
        self.assertNotIn("answer first", last["message"])

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
            response = self.post(f"/api/v1/apply-agent/opportunities/{ACME}/answers", {"key": "question_4000000103", "answer": "Controls", "posting_confirmed": True})
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
        response = self.post(self.url(), {"key": "question_4000000101", "answer": "I build robot arms", "reusable": False, "posting_confirmed": True})
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
            ({"key": "question_4000000111", "answer": "No", "reusable": True}, "this company only"),
            ({"key": "question_4000000101", "answer": "I build robot arms", "reusable": True}, "this company only"),
            ({"key": "question_1", "answer": "x"}, "no longer asks"),
            ({"key": "question_4000000101", "answer": "   "}, "Type an answer"),
        ):
            with self.subTest(body=body):
                response = self.post(self.url(), {**body, "posting_confirmed": True})
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


class SensitiveApiCase(ApplyApiCase):
    """The sensitive-answers store over HTTP. Every route needs the student's own browser session (spec 4.6), so the
    tests sign in with a cookie and send the CSRF header, as the page does; the owner's bearer token is refused."""

    ROUTES = (
        ("GET", "/api/v1/apply-agent/sensitive-answers", None),
        ("PUT", "/api/v1/apply-agent/sensitive-categories", {"categories": []}),
        ("POST", "/api/v1/apply-agent/sensitive-answers", {"category": "work_authorization", "question": "Are you legally authorized to work?", "answer": "Yes", "consent": True}),
        ("DELETE", "/api/v1/apply-agent/sensitive-answers/sens-none", None),
        ("POST", f"/api/v1/apply-agent/opportunities/{ACME}/sensitive-answers", {"key": "question_4000000105", "answer": "Yes", "consent": True, "posting_confirmed": True}),
    )
    BASE = "/api/v1/apply-agent"

    def setUp(self):
        super().setUp()
        self.greenhouse_role()
        self.turn_on()
        self.browser = self.enterContext(TestClient(self.app))
        signed = self.browser.post("/api/v1/session", json={"token": TOKEN})
        self.assertEqual(signed.status_code, 200, signed.text)
        self.csrf = {"X-CSRF-Token": self.browser.cookies.get("pipeline_csrf")}

    def send(self, method, path, body=None):
        return self.browser.request(method, path, json=body, headers=self.csrf)

    def allow(self, *categories):
        response = self.send("PUT", f"{self.BASE}/sensitive-categories", {"categories": list(categories)})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def entries(self):
        return self.send("GET", f"{self.BASE}/sensitive-answers").json()["entries"]

    def needs(self, key, answer, **extra):
        body = {"key": key, "answer": answer, "consent": True, "posting_confirmed": True, **extra}
        return self.send("POST", f"{self.BASE}/opportunities/{ACME}/sensitive-answers", body)

    def problem(self, key):
        return {item["key"]: item for item in self.check().json()["problems"]}.get(key)

    def rows(self):
        return [dict(row) for row in self.conn.execute("SELECT * FROM apply_sensitive_answers ORDER BY created_at, id").fetchall()]


class SensitiveSessionTests(SensitiveApiCase):
    def test_the_owner_access_token_is_refused_on_every_route_with_or_without_a_cookie(self):
        for method, path, body in self.ROUTES:
            with self.subTest(route=f"{method} {path}"):
                bearer = self.client.request(method, path, json=body, headers=AUTH)
                self.assertEqual(bearer.status_code, 403, bearer.text)
                self.assertIn("browser", bearer.json()["detail"])
                # A cookie next to the header does not make a script the student.
                both = self.browser.request(method, path, json=body, headers={**AUTH, **self.csrf})
                self.assertEqual(both.status_code, 403, both.text)
                self.assertEqual(self.client.request(method, path, json=body).status_code, 401, "and no sign-in at all is 401")
        self.assertEqual(self.rows(), [])

    def test_a_cookie_write_without_the_csrf_header_is_refused_even_with_no_origin_header(self):
        for method, path, body in self.ROUTES:
            if method == "GET":
                continue
            with self.subTest(route=f"{method} {path}"):
                bare = self.browser.request(method, path, json=body)
                self.assertEqual((bare.status_code, bare.json()["detail"]), (403, "CSRF validation failed"))
                wrong = self.browser.request(method, path, json=body, headers={"X-CSRF-Token": "not-the-token"})
                self.assertEqual(wrong.status_code, 403, wrong.text)
        self.assertEqual(self.allow("work_authorization")["groups"][0]["on"], True, "and with the header the same write is allowed")

    def test_reading_needs_the_browser_session_but_not_a_csrf_header(self):
        self.assertEqual(self.browser.get(f"{self.BASE}/sensitive-answers").status_code, 200)
        self.assertEqual(self.client.get(f"{self.BASE}/sensitive-answers", headers=AUTH).status_code, 403)

    def test_the_routes_are_in_the_openapi_schema(self):
        paths = self.client.get("/openapi.json").json()["paths"]
        for method, path, _body in self.ROUTES:
            template = path.replace(ACME, "{opportunity_id}").replace("sens-none", "{entry_id}")
            self.assertIn(method.lower(), paths[template], template)


class SensitiveSettingsTests(SensitiveApiCase):
    def test_nothing_is_switched_on_or_stored_at_first_and_the_consent_wording_is_shown(self):
        body = self.send("GET", f"{self.BASE}/sensitive-answers").json()
        self.assertEqual(body["entries"], [])
        self.assertFalse(any(group["on"] for group in body["groups"]))
        self.assertIn("only to fill in application forms", body["consent_text"])
        self.assertNotIn("export_control", [item["category"] for item in body["categories"]])
        self.assertNotIn("salary", [item["category"] for item in body["categories"]])
        refused = self.send("POST", f"{self.BASE}/sensitive-answers", {"category": "work_authorization", "question": "Are you legally authorized to work?", "answer": "Yes", "consent": True})
        self.assertEqual(refused.status_code, 422)
        self.assertIn("Allow answers about work authorization first", refused.json()["detail"])
        self.assertEqual(self.rows(), [])

    def test_kinds_are_switched_on_and_off_and_export_control_and_salary_cannot_be(self):
        groups = {item["key"]: item for item in self.allow("work_authorization", "eeo_gender", "eeo_hispanic", "eeo_race", "eeo_veteran", "eeo_disability")["groups"]}
        self.assertEqual({key: item["on"] for key, item in groups.items()}, {"work_authorization": True, "sponsorship": False, "age_18": False, "eeo": True, "acknowledgment": False, "consent": False})
        for category in ("export_control", "salary", "uncategorized", "nonsense"):
            response = self.send("PUT", f"{self.BASE}/sensitive-categories", {"categories": ["age_18", category]})
            self.assertEqual(response.status_code, 422, category)
        self.assertTrue(self.send("GET", f"{self.BASE}/sensitive-answers").json()["groups"][0]["on"], "a refused change changed nothing")
        self.assertFalse(any(group["on"] for group in self.allow()["groups"]))

    def test_an_entry_needs_the_consent_tick_and_records_when_and_for_what(self):
        self.allow("work_authorization")
        body = {"category": "work_authorization", "question": "Are you legally authorized to work in the United States?", "answer": "Yes"}
        for consent in (False, None):
            response = self.send("POST", f"{self.BASE}/sensitive-answers", {**body, "consent": consent} if consent is not None else body)
            self.assertEqual((response.status_code, "Tick the box" in response.json()["detail"]), (422, True))
        self.assertEqual(self.rows(), [])
        saved = self.send("POST", f"{self.BASE}/sensitive-answers", {**body, "consent": True})
        self.assertEqual(saved.status_code, 200, saved.text)
        row = self.rows()[0]
        self.assertEqual((row["consent_scope"], row["answer_kind"], row["company_key"]), ("confirmed", "option", ""))
        self.assertTrue(row["consented_at"].startswith(utc_now()[:10]))
        listed = self.entries()
        self.assertEqual((listed[0]["answer"], listed[0]["any_company"], listed[0]["switched_on"]), ("Yes", True, True))

    def test_refusals_are_422_with_a_reason_and_store_nothing(self):
        self.allow(*apply_sensitive.STORABLE)
        for body, needle in (
            ({"category": "export_control", "question": "Are you a U.S. person?", "answer": "Yes"}, "never answers export control"),
            ({"category": "salary", "question": "What are your salary expectations?", "answer": "90000"}, "never answers salary"),
            ({"category": "work_authorization", "question": "Are you a U.S. citizen or authorized to work in the U.S.?", "answer": "Yes"}, "export control"),
            ({"category": "age_18", "question": "What is your age?", "answer": "21"}, "personal question"),
            ({"category": "eeo_gender", "question": "Gender", "answer": "Male"}, "only a decline"),
            ({"category": "eeo_veteran", "question": "Veteran Status", "answer": "I am not a protected veteran"}, "only a decline"),
            ({"category": "eeo_race", "question": "Race", "answer": "Decline To Self Identify", "answer_kind": "text"}, "only a decline"),
            ({"category": "acknowledgment", "question": "I have read the Example Robotics privacy notice", "answer": "checked"}, "never for any company"),
            ({"category": "acknowledgment", "question": "I certify that this is true", "answer": "checked", "links": ["https://example.test/n"]}, "never for any company"),
            ({"category": "acknowledgment", "question": "I certify that this is true", "answer": "checked", "links": ["javascript:alert(1)"]}, "http or https"),
            ({"category": "consent", "question": "I agree", "answer": "checked"}, "whole statement"),
        ):
            with self.subTest(body=body):
                response = self.send("POST", f"{self.BASE}/sensitive-answers", {**body, "consent": True})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertIn(needle, response.json()["detail"])
        self.assertEqual(self.rows(), [])
        for body in ({}, {"category": "age_18"}, {"category": "age_18", "question": "x", "links": ["a"] * 9}):
            self.assertEqual(self.send("POST", f"{self.BASE}/sensitive-answers", body).status_code, 422, body)

    def test_a_question_that_depends_on_its_company_or_reads_as_another_kind_is_refused_by_the_route(self):
        self.allow("sponsorship", "work_authorization")
        prior = "Has this company previously filed an H-1B petition on your behalf?"
        for body, needle in (
            ({"category": "sponsorship", "question": prior, "answer": "Yes"}, "this company only"),
            ({"category": "work_authorization", "question": "What is your race?", "answer": "Asian"}, "voluntary self-identification"),
            ({"category": "work_authorization", "question": "Gender", "answer": "Female"}, "voluntary self-identification"),
        ):
            with self.subTest(body=body):
                response = self.send("POST", f"{self.BASE}/sensitive-answers", {**body, "consent": True})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertIn(needle, response.json()["detail"])
        self.assertEqual(self.rows(), [], "no row, so nothing in the account export either")
        kept = self.send("POST", f"{self.BASE}/sensitive-answers", {"category": "sponsorship", "question": prior, "answer": "Yes", "company": "Example Robotics", "consent": True})
        self.assertEqual(kept.status_code, 200, kept.text)

    def test_an_eeo_decline_and_a_statement_for_one_company_are_stored(self):
        self.allow("eeo_gender", "acknowledgment")
        self.assertEqual(self.send("POST", f"{self.BASE}/sensitive-answers", {"category": "eeo_gender", "question": "Gender", "answer": "Decline To Self Identify", "consent": True}).status_code, 200)
        saved = self.send("POST", f"{self.BASE}/sensitive-answers", {
            "category": "acknowledgment", "question": "I have read the Example Robotics privacy notice", "answer": "checked", "company": "Example Robotics",
            "links": ["https://example-robotics.test/privacy"], "consent": True})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual((saved.json()["company"], saved.json()["links"], saved.json()["any_company"]), ("Example Robotics", ["https://example-robotics.test/privacy"], False))

    def test_an_entry_is_deleted_at_once_and_a_second_delete_is_404(self):
        self.allow("age_18")
        entry = self.send("POST", f"{self.BASE}/sensitive-answers", {"category": "age_18", "question": "Are you at least 18 years of age?", "answer": "Yes", "consent": True}).json()
        self.assertEqual(self.send("DELETE", f"{self.BASE}/sensitive-answers/{entry['id']}").status_code, 204)
        self.assertEqual(self.entries(), [])
        self.assertEqual(self.send("DELETE", f"{self.BASE}/sensitive-answers/{entry['id']}").status_code, 404)


class NeedsYouTests(SensitiveApiCase):
    """The form on a role: the category, the wording and the options come from the form the app read, not from the browser."""

    def test_a_kind_that_is_not_switched_on_is_left_for_the_student_and_says_it_can_be_allowed(self):
        problem = self.problem("question_4000000105")
        self.assertEqual((problem["kind"], problem["action"]["type"], problem["action"]["allowable"]), ("sensitive_not_allowed", "manual", True))
        refused = self.needs("question_4000000105", "Yes")
        self.assertEqual((refused.status_code, refused.json()["detail"]), (422, "The app can't store an answer to this question"))
        self.assertEqual(self.rows(), [])

    def test_a_switched_on_question_offers_its_own_options_and_the_consent_wording(self):
        self.allow("work_authorization", "acknowledgment")
        action = self.problem("question_4000000105")["action"]
        self.assertEqual((action["type"], action["control"], action["options"], action["category"], action["decline_only"]), ("sensitive", "select", ["Yes", "No"], "work_authorization", False))
        self.assertIn("only to fill in application forms", action["consent_text"])
        self.assertFalse(action["company_only"], "work authorization is true whoever asks")

    def test_the_answer_is_saved_with_the_consent_and_the_next_check_fills_it_from_the_store(self):
        self.allow("work_authorization")
        no_tick = self.send("POST", f"{self.BASE}/opportunities/{ACME}/sensitive-answers", {"key": "question_4000000105", "answer": "Yes", "posting_confirmed": True})
        self.assertEqual((no_tick.status_code, "Tick the box" in no_tick.json()["detail"]), (422, True))
        maybe = self.needs("question_4000000105", "Maybe")
        self.assertEqual(maybe.status_code, 422)
        self.assertIn("not one of the form's options", maybe.json()["detail"])
        self.assertEqual(self.rows(), [])
        unconfirmed = self.send("POST", f"{self.BASE}/opportunities/{ACME}/sensitive-answers", {"key": "question_4000000105", "answer": "Yes", "consent": True})
        self.assertIn("Confirm it is the right posting", unconfirmed.json()["detail"])
        saved = self.needs("question_4000000105", "Yes")
        self.assertEqual(saved.status_code, 200, saved.text)
        row = self.rows()[0]
        self.assertEqual((row["category"], row["answer"], row["company_key"], row["consent_scope"], row["question_text"]),
                         ("work_authorization", "Yes", "acme robotics", "confirmed", "Are you legally authorized to work in the United States?"))
        check = saved.json()["check"]
        self.assertNotIn("question_4000000105", [item["key"] for item in check["problems"]])
        field = next(item for item in check["fields"] if item["key"] == "question_4000000105")
        self.assertEqual(field["disposition"], "fill")
        self.assertRegex(field["source"], r"^Sensitive answer you added \d{4}-\d\d-\d\d$")
        self.assertNotIn("Yes", json.dumps({**check, "problems": [], "fields": [], "posting": {}}), "the check names sources, never a value")

    def reword(self, key, *, label=None, values=None, type=None):
        """Serve the fictional listing with one question changed, as another employer's form might word it."""
        real = self.schema.fetch

        def changed(board, job):
            listing = real(board, job)
            for question in listing["questions"]:
                for field in question["fields"]:
                    if field["name"] == key:
                        question["label"] = label or question["label"]
                        field["values"] = [{"label": text, "value": number} for number, text in enumerate(values, 1)] if values else field["values"]
                        field["type"] = type or field["type"]
            return listing

        self.schema.fetch = changed

    def test_a_question_whose_kind_comes_from_its_options_is_stored_under_the_kind_the_plan_gave_it(self):
        # The wording reads as work authorization; the visa option makes the plan file it as sponsorship. The form must save.
        self.allow("sponsorship", "work_authorization")
        self.reword("question_4000000105", label="What is your current work authorization status?",
                    values=("Authorized to work, no sponsorship needed", "Will need H-1B sponsorship"))
        problem = self.problem("question_4000000105")
        self.assertEqual((problem["kind"], problem["action"]["type"]), ("sensitive_missing", "sensitive"))
        saved = self.needs("question_4000000105", "Authorized to work, no sponsorship needed")
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual([row["category"] for row in self.rows()], [problem["action"]["category"]])
        self.assertIsNone(next((item for item in saved.json()["check"]["problems"] if item["key"] == "question_4000000105"), None))

    def test_a_work_authorization_box_with_a_confirming_option_is_stored_and_ticked_through_the_form(self):
        self.allow("work_authorization")
        self.reword("question_4000000105", values=("Yes, I confirm this applies to me today",), type="multi_value_multi_select")
        action = self.problem("question_4000000105")["action"]
        self.assertEqual((action["type"], action["control"]), ("sensitive", "checkbox"))
        self.assertEqual(self.needs("question_4000000105", True).status_code, 200)
        field = next(item for item in self.check().json()["fields"] if item["key"] == "question_4000000105")
        self.assertEqual(field["disposition"], "fill")

    def test_the_any_company_tick_is_honoured_for_work_authorization_and_refused_where_the_wording_depends_on_a_company(self):
        self.allow("work_authorization", "acknowledgment")
        self.assertEqual(self.needs("question_4000000105", "Yes", any_company=True).status_code, 200)
        self.assertEqual(self.rows()[0]["company_key"], "")
        privacy = self.problem("question_4000000109")["action"]
        self.assertEqual((privacy["type"], privacy["control"], privacy["statement"], privacy["company_only"]),
                         ("sensitive", "checkbox", "I have read the Example Robotics privacy notice", True))
        refused = self.needs("question_4000000109", True, any_company=True)
        self.assertEqual((refused.status_code, refused.json()["detail"]), (422, "This answer is saved for this company only"))
        self.assertEqual(len(self.rows()), 1)

    def test_a_privacy_acknowledgment_is_saved_word_for_word_for_this_company_and_ticked_from_the_store(self):
        self.allow("acknowledgment")
        self.assertEqual(self.needs("question_4000000109", "no").status_code, 422)
        saved = self.needs("question_4000000109", True)
        self.assertEqual(saved.status_code, 200, saved.text)
        row = self.rows()[0]
        self.assertEqual((row["category"], row["answer_kind"], row["answer"], row["company_key"], row["question_text"]),
                         ("acknowledgment", "checkbox", "checked", "acme robotics", "I have read the Example Robotics privacy notice"))
        field = next(item for item in saved.json()["check"]["fields"] if item["key"] == "question_4000000109")
        self.assertEqual((field["disposition"], field["source"]), ("fill", "Your acknowledgment for Acme Robotics"))
        # Every statement is kept for one company, even one that reads no document (D9 B, spec 5.4 "As built").
        plain = self.needs("question_4000000110", True, any_company=True)
        self.assertEqual((plain.status_code, plain.json()["detail"]), (422, "This answer is saved for this company only"))
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.needs("question_4000000110", True).status_code, 200)
        self.assertEqual(self.rows()[-1]["company_key"], "acme robotics")

    def test_an_eeo_question_offers_only_the_forms_decline_option_and_never_stores_anything_else(self):
        self.allow("eeo_gender", "eeo_hispanic", "eeo_race", "eeo_veteran", "eeo_disability")
        offered = {item["key"]: item for item in self.check().json()["optional_sensitive"]}
        self.assertEqual(set(offered), {"gender", "hispanic_ethnicity", "veteran_status", "disability_status"})
        self.assertEqual(offered["gender"]["action"]["options"], ["Decline To Self Identify"])
        self.assertEqual(offered["veteran_status"]["action"]["options"], ["I don't wish to answer"])
        self.assertTrue(all(item["action"]["decline_only"] for item in offered.values()))
        for label in ("Male", "Female", "Yes", "No"):
            refused = self.needs("gender", label)
            self.assertEqual(refused.status_code, 422, label)
            self.assertIn("not one of the form's options", refused.json()["detail"])
        self.assertEqual(self.rows(), [])
        saved = self.needs("gender", "Decline To Self Identify")
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertNotIn("gender", [item["key"] for item in saved.json()["check"]["optional_sensitive"]])
        field = next(item for item in saved.json()["check"]["fields"] if item["key"] == "gender")
        self.assertEqual(field["disposition"], "fill")
        self.assertEqual([row["answer"] for row in self.rows()], ["Decline To Self Identify"])

    def test_questions_that_are_never_stored_have_no_form_even_when_every_kind_is_switched_on(self):
        self.allow(*apply_sensitive.STORABLE)
        check = self.check().json()
        offered = {item["key"] for item in check["optional_sensitive"]}
        self.assertNotIn("question_4000000107", offered, "salary")
        self.assertNotIn("question_4000000114", offered, "a demographic-section question has no field name, so it is never filled")
        for key in ("question_4000000107", "question_4000000114", "gdpr_consent_given", "question_4000000111"):
            refused = self.needs(key, "Decline To Self Identify")
            self.assertEqual(refused.status_code, 422, key)
        self.assertEqual(self.rows(), [])
        gdpr = next((item for item in check["problems"] if item["key"] == "gdpr_consent_given"), None)
        self.assertEqual(gdpr["action"]["type"], "manual", "the statement is on the page only")

    def test_a_decline_typed_on_the_settings_page_as_the_form_shows_it_fills_the_eeoc_field(self):
        self.allow("eeo_gender")
        saved = self.send("POST", f"{self.BASE}/sensitive-answers", {"category": "eeo_gender", "question": "Gender", "answer": "Decline To Self Identify", "consent": True})
        self.assertEqual(saved.status_code, 200, saved.text)
        check = self.check().json()
        field = next(item for item in check["fields"] if item["key"] == "gender")
        self.assertEqual(field["disposition"], "fill", "the key is the field's own label, not the question above it")
        self.assertNotIn("gender", [item["key"] for item in check["optional_sensitive"]])

    def test_an_answer_that_does_not_fit_this_form_is_a_mismatch_and_this_companys_entry_wins_over_it(self):
        self.allow("eeo_veteran")
        self.assertEqual(self.send("POST", f"{self.BASE}/sensitive-answers", {"category": "eeo_veteran", "question": "Veteran Status", "answer": "Decline To Self Identify", "consent": True}).status_code, 200)
        offered = {item["key"]: item for item in self.check().json()["optional_sensitive"]}
        # The key matches (the field's own label, at any company); what does not fit is the decline this form words differently.
        self.assertIn("veteran_status", offered, "the any-company answer is not this form's decline label")
        self.assertEqual(self.problem("veteran_status")["kind"], "sensitive_mismatch", "a miss on the decline's label, not on the key")
        self.assertTrue(offered["veteran_status"]["action"]["company_only"])
        self.assertEqual(self.needs("veteran_status", "I don't wish to answer", any_company=True).status_code, 422)
        self.assertEqual(self.needs("veteran_status", "I don't wish to answer").status_code, 200)
        self.assertEqual(sorted(row["company_key"] for row in self.rows()), ["", "acme robotics"], "the other entry is untouched")

    def test_the_route_needs_the_switch_the_role_and_a_real_question(self):
        self.allow("work_authorization")
        self.assertEqual(self.send("POST", f"{self.BASE}/opportunities/no-such-role/sensitive-answers", {"key": "k", "answer": "x", "consent": True}).status_code, 404)
        self.assertEqual(self.needs("question_1", "Yes").status_code, 422)
        self.assertEqual(self.needs("question_4000000101", "Yes").status_code, 422, "an ordinary question is the answer library's")
        self.assertEqual(self.send("POST", f"{self.BASE}/opportunities/job-b/sensitive-answers", {"key": "k", "answer": "x", "consent": True}).status_code, 422, "not a Greenhouse role")
        for body in ({}, {"key": "", "answer": "x"}, {"key": "k"}):
            self.assertEqual(self.send("POST", f"{self.BASE}/opportunities/{ACME}/sensitive-answers", body).status_code, 422, body)
        with self.conn:
            self.conn.execute("UPDATE user_settings SET value='off' WHERE key='apply_agent' AND user_id=?", (USER,))
        self.assertEqual(self.needs("question_4000000105", "Yes").status_code, 409)
        self.assertEqual(self.rows(), [])

    def test_opening_the_check_writes_nothing_even_with_answers_stored(self):
        self.allow("work_authorization", "acknowledgment", "eeo_gender")
        self.needs("question_4000000105", "Yes")
        self.needs("question_4000000109", True)
        self.conn.commit()
        before = (self.rows(), CheckRouteTests.snapshot(self))
        for _ in range(3):
            self.assertEqual(self.check().status_code, 200)
        self.assertEqual((self.rows(), CheckRouteTests.snapshot(self)), before, "no last_used_at, no run, no application, no event")

    def test_no_socket_is_opened_by_the_store_routes(self):
        boom = AssertionError("the store reached for the network")
        with mock.patch("socket.socket.connect", side_effect=boom), mock.patch("socket.create_connection", side_effect=boom), \
                mock.patch("urllib.request.urlopen", side_effect=boom):
            self.allow("work_authorization")
            self.assertEqual(self.needs("question_4000000105", "Yes").status_code, 200)
            self.assertEqual(self.send("GET", f"{self.BASE}/sensitive-answers").status_code, 200)
            entry = self.entries()[0]
            self.assertEqual(self.send("DELETE", f"{self.BASE}/sensitive-answers/{entry['id']}").status_code, 204)


class StoreNeverLeavesTests(SensitiveApiCase):
    """12.6: a stored answer is in no response the extension, the answer library or the tracker gives."""

    PLANTED_ANSWER = "ZZ-planted-answer-7731"
    PLANTED_QUESTION = "Are you legally authorized to work in the ZZ-planted-region-4412?"

    def test_the_planted_entry_is_in_no_extension_or_library_response(self):
        self.allow("work_authorization")
        saved = self.send("POST", f"{self.BASE}/sensitive-answers", {"category": "work_authorization", "question": self.PLANTED_QUESTION, "answer": self.PLANTED_ANSWER, "consent": True})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertIn("ZZ-planted-answer-7731", json.dumps(self.entries()), "the owner's own settings page does show it")
        from opportunity_app.applications.extension import apply_context

        context = apply_context(self.conn, "app-job-b", user_id=USER)
        self.assertNotIn("ZZ-planted", json.dumps(context, default=str))
        origin = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"
        created = self.client.post("/api/v1/extension/pairings", headers=AUTH)
        self.assertEqual(created.status_code, 201, created.text)
        redeemed = self.client.post("/api/v1/extension/pairings/redeem", headers={"Origin": origin}, json={"code": created.json()["code"], "device_name": "Test Chrome"})
        self.assertEqual(redeemed.status_code, 200, redeemed.text)
        device = {"Authorization": f"Bearer {redeemed.json()['device_token']}", "Origin": origin}
        responses = [
            self.client.get("/api/v1/extension/apply-context", headers=device, params={"application_id": "app-job-b"}),
            self.client.get("/api/v1/extension/application-candidates", headers=device, params={"page_url": "https://example.com/jobs/b"}),
            self.client.get("/api/v1/preparation/answers", headers=AUTH),
            self.client.get("/api/v1/applications", headers=AUTH),
            self.client.get("/api/v1/opportunities", headers=AUTH),
            self.client.get("/api/v1/profile", headers=AUTH),
            self.client.get("/api/v1/apply-agent/settings", headers=AUTH),
            self.get(f"/api/v1/apply-agent/opportunities/{ACME}/check"),
        ]
        for response in responses:
            self.assertEqual(response.status_code, 200, response.request.url)
            self.assertNotIn("ZZ-planted", response.text, str(response.request.url))

    def test_the_sensitive_question_is_not_offered_to_the_extension_as_a_saved_answer(self):
        # The extension saves and reads the answer library. Nothing the store holds is ever copied into it.
        self.allow("work_authorization")
        self.needs("question_4000000105", "Yes")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM answer_library").fetchone()[0], 0)


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
        from opportunity_app.apply_greenhouse import API_HOST, schema_url

        self.assertEqual(schema_url("a", "1").split("/")[2], API_HOST)


if __name__ == "__main__":
    unittest.main()
