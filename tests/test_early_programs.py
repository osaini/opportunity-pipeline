"""The Programs tab: each student's private, researched program list plus their status."""

from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR
from opportunity_app.api import create_app
from opportunity_app.early_programs import early_programs, load_programs, set_program_status
from opportunity_app.operations import export_account
from opportunity_app.schema import LOCAL_USER_ID, connect_product
from opportunity_app.setup import Paths, programs_report
from opportunity_app.urgent import urgent_queue
from tests.helpers_platform import build_and_migrate


def program(program_id: str, **fields) -> dict:
    return {
        "id": program_id,
        "name": f"{program_id} internship",
        "host": "Example Aerospace",
        "url": f"https://careers.example.com/{program_id}",
        "evidence": "explicit",
        **fields,
    }


def write_programs(path: Path, programs: list, checked_on: str = "2026-09-21", **document) -> Path:
    path.write_text(json.dumps({"checked_on": checked_on, **document, "programs": programs}), encoding="utf-8")
    return path


class LoadProgramsTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_a_missing_file_is_unconfigured_not_an_error(self):
        result = load_programs(self.root / "absent.json")
        self.assertEqual((result["configured"], result["programs"], result["error"]), (False, [], ""))
        self.assertFalse(load_programs(None)["configured"])

    def test_malformed_json_is_reported_without_raising(self):
        path = self.root / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        result = load_programs(path)
        self.assertTrue(result["configured"])
        self.assertIn("could not be read", result["error"])

    def test_invalid_and_duplicate_entries_are_skipped_and_counted(self):
        path = write_programs(self.root / "programs.json", [
            program("good"),
            program("good"),
            program("Bad ID"),
            program("no-url", url="javascript:alert(1)"),
            program("weak-claim", evidence="probably"),
            program("bad-date", deadline_on="Dec 13"),
            program("no-host", host=""),
            "not an object",
        ])
        result = load_programs(path)
        self.assertEqual([item["id"] for item in result["programs"]], ["good"])
        self.assertEqual(result["skipped"], 7)

    def three_levels(self, **document) -> dict:
        path = write_programs(self.root / "programs.json", [
            program("a", evidence="explicit"),
            program("b", evidence="not_named"),
            program("c", evidence="unverified"),
        ], **document)
        return load_programs(path)

    def test_labels_come_from_the_students_own_audience(self):
        result = self.three_levels(label="First-year", audience="first-years")
        self.assertEqual(result["label"], "First-year")
        self.assertEqual({item["id"]: item["evidence_label"] for item in result["programs"]}, {
            "a": "Names first-years",
            "b": "No class-year limit stated",
            "c": "Eligibility for first-years unverified",
        })

    def test_without_an_audience_nothing_about_a_class_year_is_assumed(self):
        result = self.three_levels()
        self.assertEqual((result["label"], result["audience"]), ("Programs", ""))
        self.assertEqual({item["id"]: item["evidence_label"] for item in result["programs"]}, {
            "a": "Names your class year",
            "b": "No class-year limit stated",
            "c": "Class-year eligibility unverified",
        })


class SetupProgramsCheckTests(unittest.TestCase):
    def test_setup_reports_each_problem_by_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = Paths(Path(directory))
            self.assertEqual(programs_report(paths)["exists"], False)
            paths.programs.parent.mkdir(parents=True)
            write_programs(paths.programs, [program("good"), program("late", deadline_on="soon")],
                           label="Sophomore", audience="sophomores")
            report = programs_report(paths)
        self.assertEqual((report["exists"], report["ok"], report["count"]), (True, False, 1))
        self.assertEqual(report["label"], "Sophomore")
        self.assertEqual(report["problems"], [{"entry": "late", "error": "dates must be YYYY-MM-DD"}])


class EarlyProgramQueueTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        _, self.platform_path = build_and_migrate(root)
        self.path = root / "programs.json"
        # 17:00 UTC is still 2026-09-21 in every US time zone.
        self.now = datetime(2026, 9, 21, 17, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.tempdir.cleanup()

    def queue(self, programs: list) -> dict:
        write_programs(self.path, programs)
        with closing(connect_product(self.platform_path)) as conn:
            return early_programs(conn, user_id=LOCAL_USER_ID, path=self.path, now=self.now)

    def test_buckets_and_deadline_ordering(self):
        payload = self.queue([
            program("later", deadline_on="2026-12-13"),
            program("rolling"),
            program("soon", deadline_on="2026-09-25"),
            program("opens-later", opens_on="2026-10-10", deadline_on="2027-01-10"),
            program("passed", deadline_on="2026-09-14"),
            program("filled", closed_note="Already filled for 2027"),
        ])
        buckets = {item["id"]: item["bucket"] for item in payload["items"]}
        self.assertEqual(buckets, {
            "passed": "closed", "soon": "open", "later": "open",
            "opens-later": "upcoming", "rolling": "open", "filled": "closed",
        })
        # Soonest deadline first, undated entries last.
        self.assertEqual([item["id"] for item in payload["items"]][:4], ["passed", "soon", "later", "opens-later"])
        days = {item["id"]: item["days_left"] for item in payload["items"]}
        self.assertEqual((days["soon"], days["passed"], days["rolling"]), (4, -7, None))
        self.assertEqual(payload["counts"], {"open": 3, "upcoming": 1, "done": 0, "closed": 2})
        self.assertEqual(payload["today"], "2026-09-21")

    def test_a_status_moves_a_program_to_done_and_todo_clears_it(self):
        self.queue([program("spacex", deadline_on="2026-12-13")])
        with closing(connect_product(self.platform_path)) as conn:
            set_program_status(conn, "spacex", user_id=LOCAL_USER_ID, status="applied", path=self.path)
            item = early_programs(conn, user_id=LOCAL_USER_ID, path=self.path, now=self.now)["items"][0]
            self.assertEqual((item["status"], item["bucket"]), ("applied", "done"))
            self.assertEqual(len(export_account(conn, user_id=LOCAL_USER_ID)["early_program_status"]), 1)
            set_program_status(conn, "spacex", user_id=LOCAL_USER_ID, status="todo", path=self.path)
            item = early_programs(conn, user_id=LOCAL_USER_ID, path=self.path, now=self.now)["items"][0]
            self.assertEqual((item["status"], item["bucket"]), ("todo", "open"))
            rows = conn.execute("SELECT COUNT(*) FROM early_program_status").fetchone()[0]
            self.assertEqual(rows, 0)


class UrgentProgramTests(unittest.TestCase):
    """Program deadlines within Urgent's window join the queue; nothing else does."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        _, self.platform_path = build_and_migrate(root)
        self.path = root / "programs.json"
        self.now = datetime(2026, 9, 21, 17, 0, tzinfo=timezone.utc)
        write_programs(self.path, [
            program("today", deadline_on="2026-09-21"),
            program("in-13-days", deadline_on="2026-10-04", source_note="Official posting"),
            program("in-14-days", deadline_on="2026-10-05"),
            program("passed", deadline_on="2026-09-20"),
            program("rolling"),
            program("filled", deadline_on="2026-09-25", closed_note="Already filled"),
            program("applied", deadline_on="2026-09-24"),
            program("opens-soon", opens_on="2026-09-23", deadline_on="2026-09-30"),
        ])

    def tearDown(self):
        self.tempdir.cleanup()

    def queue(self, **kwargs):
        with closing(connect_product(self.platform_path)) as conn:
            set_program_status(conn, "applied", user_id=LOCAL_USER_ID, status="applied", path=self.path)
            return urgent_queue(conn, user_id=LOCAL_USER_ID, now=self.now, **kwargs)

    def test_only_open_deadlines_in_the_next_two_weeks_are_urgent(self):
        payload = self.queue(programs_path=self.path)
        programs = [item for item in payload["items"] if item["kind"] == "program_deadline"]
        self.assertEqual([item["program_id"] for item in programs], ["today", "opens-soon", "in-13-days"])
        first = programs[0]
        self.assertEqual((first["date"], first["days_until"], first["overdue"]), ("2026-09-21", 0, False))
        self.assertEqual(first["date_source"], "From your program research")
        self.assertEqual((first["title"], first["company"]), ("today internship", "Example Aerospace"))
        self.assertEqual(programs[2]["source_name"], "Official posting")
        # Due today counts toward the nav badge, like any deadline within two days.
        self.assertGreaterEqual(payload["counts"]["attention"], 1)

    def test_without_a_program_list_urgent_is_unchanged(self):
        kinds = {item["kind"] for item in self.queue()["items"]}
        self.assertNotIn("program_deadline", kinds)
        missing = self.queue(programs_path=self.path.with_name("absent.json"))
        self.assertNotIn("program_deadline", {item["kind"] for item in missing["items"]})


class EarlyProgramApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        _, platform_path = build_and_migrate(root)
        soon = (date.today() + timedelta(days=3)).isoformat()
        self.path = write_programs(root / "programs.json", [program("nreip", deadline_on=soon)])
        self.app = create_app(
            db_path=platform_path,
            access_token="first-year-token",
            admin_token="admin-token",
            static_dir=STATIC_DIR,
            resume_storage=root / "resumes",
            capture_storage=root / "captures",
            interview_storage=root / "interviews",
            early_programs_file=self.path,
        )
        self.client_context = TestClient(self.app)
        self.client = self.client_context.__enter__()
        self.headers = {"Authorization": "Bearer first-year-token"}

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.tempdir.cleanup()

    def put(self, program_id: str, status: str, headers: dict | None = None):
        return self.client.put(
            f"/api/v1/early-programs/{program_id}/status", headers=headers or self.headers, json={"status": status},
        )

    def test_list_requires_authentication(self):
        self.assertEqual(self.client.get("/api/v1/early-programs").status_code, 401)

    def test_list_status_round_trip(self):
        listed = self.client.get("/api/v1/early-programs", headers=self.headers)
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json()["items"][0]["days_left"], 3)
        self.assertEqual(self.put("nreip", "skipped").status_code, 200)
        item = self.client.get("/api/v1/early-programs", headers=self.headers).json()["items"][0]
        self.assertEqual((item["status"], item["bucket"]), ("skipped", "done"))

    def test_the_urgent_route_includes_a_program_due_soon(self):
        items = self.client.get("/api/v1/urgent", headers=self.headers).json()["items"]
        self.assertEqual([item["program_id"] for item in items if item["kind"] == "program_deadline"], ["nreip"])
        self.put("nreip", "applied")
        items = self.client.get("/api/v1/urgent", headers=self.headers).json()["items"]
        self.assertEqual([item for item in items if item["kind"] == "program_deadline"], [])

    def test_unknown_program_and_bad_status_are_rejected(self):
        self.assertEqual(self.put("not-in-file", "applied").status_code, 404)
        self.assertEqual(self.put("nreip", "maybe").status_code, 422)

    def test_another_account_gets_the_empty_state_not_the_owners_list(self):
        self.client.put(
            "/api/v1/admin/feature-flags/allow_public_signup",
            headers={"Authorization": "Bearer admin-token"},
            json={"enabled": True, "description": "test"},
        )
        registered = self.client.post(
            "/api/v1/auth/register",
            json={"email": "b@example.com", "password": "Password123!", "display_name": "b"},
        )
        self.assertEqual(registered.status_code, 201, registered.text)
        other = {"Authorization": f"Bearer {registered.json()['api_token']}"}
        self.assertEqual(self.put("nreip", "applied").status_code, 200)
        mine = self.client.get("/api/v1/early-programs", headers=self.headers).json()["items"][0]
        self.assertEqual(mine["status"], "applied")
        # The private list file is researched for the local owner; another
        # account on the same install sees the honest unconfigured state.
        theirs = self.client.get("/api/v1/early-programs", headers=other).json()
        self.assertEqual((theirs["configured"], theirs["items"]), (False, []))
        self.assertEqual(self.put("nreip", "applied", headers=other).status_code, 404)
        urgent = self.client.get("/api/v1/urgent", headers=other).json()["items"]
        self.assertEqual([item for item in urgent if item["kind"] == "program_deadline"], [])

    def test_the_page_route_serves_the_app(self):
        page = self.client.get("/programs")
        self.assertEqual(page.status_code, 200)
        self.assertIn('id="programs-nav"', page.text)

    def test_the_shipped_page_names_no_particular_class_year(self):
        # The tab's name and wording come from each student's file, never the code.
        for name in ("index.html", "app.js"):
            text = (STATIC_DIR / name).read_text(encoding="utf-8").casefold()
            for phrase in ("first-year", "freshm", "sophomore"):
                self.assertNotIn(phrase, text, f"{name} hardcodes {phrase!r}")

    def test_without_a_file_the_tab_is_unconfigured(self):
        self.path.unlink()
        payload = self.client.get("/api/v1/early-programs", headers=self.headers).json()
        self.assertEqual((payload["configured"], payload["items"]), (False, []))


if __name__ == "__main__":
    unittest.main()
