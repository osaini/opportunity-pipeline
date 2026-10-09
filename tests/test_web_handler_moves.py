"""Behaviour of handler logic that moved into domain modules, pinned at the HTTP level.

Phase 4 moved the CSV/JSON export and import parsing of applications, the outreach JSON export, the preparation-document delete
and the application-session routes out of their handlers. These tests state what the routes answer, byte for byte where the answer
is a file, so they pass before and after the move.
"""

import csv
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR
from opportunity_app.api import create_app
from opportunity_app.outreach.targets import list_targets
from opportunity_app.core.database import connect_product
from opportunity_app.core.schema import LOCAL_USER_ID

from helpers_platform import build_and_migrate

OWNER = "moves-owner-token"
AUTH = {"Authorization": f"Bearer {OWNER}"}
EXPORT_FIELDS = [
    "id", "opportunity_id", "company", "title", "stage", "notes",
    "applied_at", "follow_up_at", "location", "region", "url", "created_at", "updated_at",
]


class HandlerMoveTestCase(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        _, self.platform_path = build_and_migrate(self.root)
        self.app = create_app(
            db_path=self.platform_path, access_token=OWNER, static_dir=STATIC_DIR,
            resume_storage=self.root / "resumes", capture_storage=self.root / "captures",
            interview_storage=self.root / "interviews", apply_storage=self.root / "apply",
            start_call_prep_worker=False, start_inbox_watcher=False, start_automation_worker=False,
        )
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def upload(self, name, content, content_type="application/json"):
        return self.client.post("/api/v1/applications/import", headers=AUTH, files={"upload": (name, content, content_type)})


class ApplicationExportImportTests(HandlerMoveTestCase):
    def test_a_json_list_imports_and_exports_as_json_and_csv(self):
        imported = self.upload("applications.json", json.dumps([
            {"opportunity_id": "job-a", "stage": "interview", "notes": "Phone screen, then onsite"},
            {"opportunity_id": "job-b", "stage": "applied", "notes": "Plain note"},
        ]))
        self.assertEqual(imported.status_code, 200, imported.text)
        self.assertEqual((imported.json()["imported"], imported.json()["skipped"]), (2, 0))

        listed = self.client.get("/api/v1/applications", headers=AUTH).json()["items"]
        # The list also says what Apply for me did for each card (10.5); the export is the tracker's own columns only.
        self.assertTrue(all(item.pop("apply") is None for item in listed))
        as_json = self.client.get("/api/v1/applications/export", headers=AUTH, params={"format": "json"})
        self.assertEqual(as_json.headers["content-type"], "application/json")
        self.assertEqual(as_json.headers["content-disposition"], 'attachment; filename="applications.json"')
        self.assertEqual(as_json.text, json.dumps(listed, indent=2, sort_keys=True))

        as_csv = self.client.get("/api/v1/applications/export", headers=AUTH, params={"format": "csv"})
        self.assertEqual(as_csv.headers["content-type"], "text/csv; charset=utf-8")
        self.assertEqual(as_csv.headers["content-disposition"], 'attachment; filename="applications.csv"')
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=EXPORT_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(listed)
        self.assertEqual(as_csv.content.decode("utf-8"), buffer.getvalue())
        self.assertEqual(as_csv.text.splitlines()[0], ",".join(EXPORT_FIELDS))

    def test_a_csv_file_imports_and_the_extension_is_read_in_lower_case(self):
        content = "opportunity_id,stage,notes,follow_up_at\njob-a,interview,From a spreadsheet,2026-12-01\n"
        imported = self.upload("TRACKER.CSV", content.encode("utf-8-sig"), "text/csv")
        self.assertEqual(imported.status_code, 200, imported.text)
        self.assertEqual(imported.json()["imported"], 1)
        (item,) = [row for row in self.client.get("/api/v1/applications", headers=AUTH).json()["items"] if row["opportunity_id"] == "job-a"]
        self.assertEqual((item["stage"], item["notes"]), ("interview", "From a spreadsheet"))

    def test_an_object_with_items_imports_and_an_object_without_them_imports_nothing(self):
        with_items = self.upload("a.json", json.dumps({"items": [{"opportunity_id": "job-a", "stage": "applied"}]}))
        self.assertEqual(with_items.json()["imported"], 1)
        without = self.upload("b.json", json.dumps({"other": 1}))
        self.assertEqual(without.status_code, 200)
        self.assertEqual((without.json()["imported"], without.json()["skipped"]), (0, 0))

    def test_files_that_are_not_application_lists_are_a_422_with_the_parser_message(self):
        cases = [
            ("scalar.json", "5", "Import must be a JSON list or object with an items list"),
            ("items.json", json.dumps({"items": 5}), "Import must contain a list of application objects"),
            ("rows.json", json.dumps([1, 2]), "Import must contain a list of application objects"),
            ("broken.json", "{not json", "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)"),
        ]
        for name, content, detail in cases:
            with self.subTest(name=name):
                response = self.upload(name, content)
                self.assertEqual((response.status_code, response.json()["detail"]), (422, detail))
        bad_bytes = self.upload("latin.json", b"\xff\xfe\x00[")
        self.assertEqual(bad_bytes.status_code, 422)
        self.assertIn("codec can't decode", bad_bytes.json()["detail"])

    def test_an_upload_over_two_megabytes_is_refused_before_it_is_read_as_text(self):
        response = self.upload("big.json", b" " * (2 * 1024 * 1024 + 1))
        self.assertEqual((response.status_code, response.json()["detail"]), (422, "Application imports are limited to 2 MB"))

    def test_a_row_naming_a_missing_opportunity_is_skipped_not_a_422(self):
        response = self.upload("a.json", json.dumps([{"opportunity_id": "no-such-job", "stage": "applied"}]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["skipped"], 1)
        self.assertIn("Opportunity not found", response.json()["errors"][0]["detail"])


class OutreachJsonExportTests(HandlerMoveTestCase):
    DERIVED = (
        "draft_checks", "follow_up_checks", "follow_up_due", "revisit_due", "suggestion",
        "draft_history_count", "follow_up_history_count", "possible_reply_count", "possible_replies", "gmail_reply",
    )

    def test_the_json_export_is_the_list_without_the_derived_fields(self):
        created = self.client.post("/api/v1/outreach", headers=AUTH, json={"company": "Export Robotics", "contact_email": "hi@export.test"})
        self.assertEqual(created.status_code, 201, created.text)
        with closing(connect_product(self.platform_path)) as conn:
            listed = list_targets(conn, user_id=LOCAL_USER_ID)
        exported = self.client.get("/api/v1/outreach/export", headers=AUTH, params={"format": "json"})
        self.assertEqual(exported.headers["content-type"], "application/json")
        self.assertEqual(exported.headers["content-disposition"], 'attachment; filename="outreach.json"')
        body = exported.json()
        self.assertEqual(body["format"], "outreach-targets-v1")
        self.assertEqual(len(body["items"]), len(listed))
        for derived in self.DERIVED:
            self.assertIn(derived, listed[0], f"{derived} is derived on the list, so the export must drop it")
            self.assertNotIn(derived, body["items"][0])
        self.assertEqual(set(listed[0]) - set(self.DERIVED), set(body["items"][0]))
        stripped = [{key: value for key, value in item.items() if key not in self.DERIVED} for item in listed]
        self.assertEqual(exported.text, json.dumps({"format": "outreach-targets-v1", "items": stripped}, indent=2, sort_keys=True))


class PreparationDocumentDeleteTests(HandlerMoveTestCase):
    def tables(self):
        with closing(sqlite3.connect(self.platform_path)) as conn:
            return (
                conn.execute("SELECT COUNT(*) FROM generated_documents").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM generated_document_artifacts").fetchone()[0],
            )

    def test_deleting_a_document_removes_its_row_and_its_artifact(self):
        created = self.client.post("/api/v1/preparation/documents", headers=AUTH, json={"opportunity_id": "job-a", "document_type": "cover_letter"})
        self.assertEqual(created.status_code, 201, created.text)
        document_id = created.json()["id"]
        # Approving a document files its downloadable artifact.
        self.assertEqual(self.client.post(f"/api/v1/preparation/documents/{document_id}/approve", headers=AUTH).status_code, 200)
        self.assertEqual(self.tables(), (1, 1))
        stored = [path for path in (self.root / "resumes" / "generated").rglob("*") if path.is_file()]
        self.assertEqual(len(stored), 1)

        deleted = self.client.delete(f"/api/v1/preparation/documents/{document_id}", headers=AUTH)
        self.assertEqual((deleted.status_code, deleted.content), (204, b""))
        self.assertEqual(self.tables(), (0, 0))
        self.assertFalse(stored[0].exists())
        self.assertEqual(self.client.get(f"/api/v1/preparation/documents/{document_id}", headers=AUTH).status_code, 404)

    def test_deleting_a_missing_document_is_a_404_and_touches_nothing(self):
        created = self.client.post("/api/v1/preparation/documents", headers=AUTH, json={"opportunity_id": "job-a", "document_type": "resume"})
        self.assertEqual(created.status_code, 201, created.text)
        missing = self.client.delete("/api/v1/preparation/documents/not-a-document", headers=AUTH)
        self.assertEqual((missing.status_code, missing.json()["detail"]), (404, "Document not found"))
        self.assertEqual(self.tables()[0], 1)


class ApplySessionRouteTests(HandlerMoveTestCase):
    def sync(self, **fields):
        body = {"session_id": "s-1", "page_url": "https://jobs.example.com/apply/1", "fields": [], **fields}
        return self.client.post("/api/v1/apply-sessions", headers=AUTH, json=body)

    def test_a_session_is_stored_with_only_the_allowed_field_keys_and_listed_newest_first(self):
        first = self.sync(fields=[{"key": "k", "label": "Email", "type": "email", "filled": True, "proposed_value": "secret@example.com"}])
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()["fields"], [{"key": "k", "label": "Email", "type": "email", "filled": True}])
        self.assertIs(first.json()["final_submit_available"], False)
        self.sync(session_id="s-2", status="reviewed")
        listed = self.client.get("/api/v1/apply-sessions", headers=AUTH).json()
        self.assertEqual((listed["total"], [item["id"] for item in listed["items"]]), (2, ["s-2", "s-1"]))
        self.assertNotIn("fields_json", listed["items"][0])
        self.assertEqual(listed["items"][1]["fields"], first.json()["fields"])

    def test_the_url_the_owner_and_the_final_submit_controls_are_checked_in_that_order(self):
        bad_url = self.sync(page_url="ftp://jobs.example.com/apply")
        self.assertEqual((bad_url.status_code, bad_url.json()["detail"]), (422, "Apply sessions require an HTTP or HTTPS page URL"))
        unowned = self.sync(application_id="not-mine", fields=[{"type": "submit", "label": "Go"}])
        self.assertEqual((unowned.status_code, unowned.json()["detail"]), (404, "Application not found"))
        for field in ({"type": "submit"}, {"type": "BUTTON"}, {"type": "image"}, {"type": "text", "label": "Please Submit Application now"}):
            with self.subTest(field=field):
                refused = self.sync(fields=[{"key": "x", **field}])
                self.assertEqual(
                    (refused.status_code, refused.json()["detail"]),
                    (422, "Final-submit controls are prohibited from Apply Mode sessions"),
                )
        with closing(sqlite3.connect(self.platform_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM application_form_sessions").fetchone()[0], 0)

    def test_a_session_id_belonging_to_another_user_is_a_409_and_is_not_overwritten(self):
        with closing(sqlite3.connect(self.platform_path)) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO users(id, email, display_name, role, created_at, updated_at)"
                    " VALUES('other', 'other@example.com', 'Other', 'student', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
                )
                conn.execute(
                    "INSERT INTO application_form_sessions(id, user_id, application_id, page_url, ats_type, fields_json, status, created_at, updated_at)"
                    " VALUES('theirs', 'other', NULL, 'https://jobs.example.com/apply/9', 'generic', '[]', 'draft',"
                    " '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')"
                )
        taken = self.sync(session_id="theirs")
        self.assertEqual((taken.status_code, taken.json()["detail"]), (409, "Apply session belongs to another user"))
        with closing(sqlite3.connect(self.platform_path)) as conn:
            self.assertEqual(conn.execute("SELECT user_id, page_url FROM application_form_sessions WHERE id='theirs'").fetchone(), ("other", "https://jobs.example.com/apply/9"))

    def test_syncing_a_session_for_an_application_records_an_event_and_a_resync_updates_in_place(self):
        imported = self.upload_application()
        first = self.sync(application_id=imported, status="draft")
        self.assertEqual(first.status_code, 200, first.text)
        again = self.sync(application_id=imported, status="completed", ats_type="greenhouse")
        self.assertEqual((again.json()["status"], again.json()["ats_type"]), ("completed", "greenhouse"))
        with closing(sqlite3.connect(self.platform_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM application_form_sessions").fetchone()[0], 1)
            events = conn.execute(
                "SELECT COUNT(*) FROM application_events WHERE application_id=? AND event_type='apply_session_synced'", (imported,)
            ).fetchone()[0]
        self.assertEqual(events, 2)

    def upload_application(self):
        response = self.client.post("/api/v1/applications/import", headers=AUTH, files={"upload": ("a.json", json.dumps([{"opportunity_id": "job-a", "stage": "applying"}]), "application/json")})
        self.assertEqual(response.status_code, 200, response.text)
        (item,) = [row for row in self.client.get("/api/v1/applications", headers=AUTH).json()["items"] if row["opportunity_id"] == "job-a"]
        return item["id"]


if __name__ == "__main__":
    unittest.main()
