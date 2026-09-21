"""Cold outreach tracker: API, lifecycle side effects, import, export, tenancy."""

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR
from opportunity_app import outreach as outreach_module
from opportunity_app.api import create_app
from opportunity_app.connections import update_preferences
from opportunity_app.outreach import (
    CLAIM_DETAIL_LIMIT,
    _claim_detail,
    _is_unique_violation,
    create_target,
    draft_checks,
    export_csv,
    get_target,
    import_targets,
    list_targets,
    local_today,
    location_usable,
    outreach_summary,
    parse_import,
    update_target,
)
from opportunity_app import outreach_profile as profile_module
from opportunity_app.outreach_profile import apply_location
from opportunity_app.schema import connect_product, ensure_product_schema, utc_now

from helpers_platform import build_and_migrate

AUTH = {"Authorization": "Bearer outreach-owner"}
USER = "local-user"


class OutreachApiTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        _, self.platform_path = build_and_migrate(root)
        app = create_app(
            db_path=self.platform_path,
            access_token="outreach-owner",
            admin_token="outreach-admin",
            static_dir=STATIC_DIR,
            resume_storage=root / "resumes",
            capture_storage=root / "captures",
            interview_storage=root / "interviews",
        )
        self.client = TestClient(app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tempdir.cleanup()

    def create(self, **overrides):
        body = {
            "company": "iDvera",
            "channel": "ATI",
            "priority": "P1",
            "contact_name": "Greg Steinberg",
            "contact_email": "info@idvera.com",
            "contact_confidence": "confirmed",
            "source_urls": ["https://idvera.com/"],
            "researched_at": "2026-09-16",
            **overrides,
        }
        response = self.client.post("/api/v1/outreach", headers=AUTH, json=body)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def test_create_list_and_page_route(self):
        created = self.create()
        self.assertEqual(created["status"], "not_started")
        self.assertEqual(created["source_urls"], ["https://idvera.com/"])
        listing = self.client.get("/api/v1/outreach", headers=AUTH).json()
        self.assertEqual(listing["total"], 1)
        self.assertEqual(listing["summary"]["by_status"]["not_started"], 1)
        self.assertEqual(listing["summary"]["unverified_contacts"], 0)
        page = self.client.get("/outreach")
        self.assertEqual(page.status_code, 200)
        self.assertIn('id="outreach-nav"', page.text)

    def test_requires_auth(self):
        self.assertEqual(self.client.get("/api/v1/outreach").status_code, 401)

    def test_marking_sent_sets_sent_date_and_follow_up(self):
        created = self.create()
        with mock.patch("opportunity_app.outreach.local_today", return_value=date(2026, 9, 16)):
            sent = self.client.patch(f"/api/v1/outreach/{created['id']}", headers=AUTH, json={"status": "sent"}).json()
        self.assertEqual(sent["sent_at"], "2026-09-16")
        self.assertEqual(sent["follow_up_at"], "2026-09-23")
        self.assertIsNotNone(sent["follow_up_at"])
        replied = self.client.patch(f"/api/v1/outreach/{created['id']}", headers=AUTH, json={"status": "replied"}).json()
        self.assertIsNone(replied["follow_up_at"])
        self.assertEqual(replied["sent_at"], sent["sent_at"])
        detail = self.client.get(f"/api/v1/outreach/{created['id']}", headers=AUTH).json()
        transitions = [(e["from_status"], e["to_status"]) for e in detail["events"] if e["event_type"] == "status"]
        self.assertIn(("not_started", "sent"), transitions)
        self.assertIn(("sent", "replied"), transitions)

    def test_explicit_follow_up_wins_and_partial_patch_keeps_other_fields(self):
        created = self.create(notes="keep me")
        updated = self.client.patch(
            f"/api/v1/outreach/{created['id']}", headers=AUTH, json={"status": "sent", "follow_up_at": "2026-10-01"}
        ).json()
        self.assertEqual(updated["follow_up_at"], "2026-10-01")
        self.assertEqual(updated["notes"], "keep me")
        self.assertEqual(updated["contact_name"], "Greg Steinberg")

    def test_validation(self):
        self.assertEqual(self.client.post("/api/v1/outreach", headers=AUTH, json={"company": "  "}).status_code, 422)
        self.assertEqual(self.client.post("/api/v1/outreach", headers=AUTH, json={"company": "X", "deadline_date": "Rolling"}).status_code, 422)
        self.assertEqual(self.client.post("/api/v1/outreach", headers=AUTH, json={"company": "X", "contact_email": "nope"}).status_code, 422)
        self.assertEqual(self.client.post("/api/v1/outreach", headers=AUTH, json={"company": "X", "source_urls": ["javascript:alert(1)"]}).status_code, 422)
        self.create()
        duplicate = self.client.post("/api/v1/outreach", headers=AUTH, json={"company": "iDvera"})
        self.assertEqual(duplicate.status_code, 422)
        self.assertIn("already", duplicate.json()["detail"])
        self.assertEqual(self.client.get("/api/v1/outreach?status=bogus", headers=AUTH).status_code, 422)
        self.assertEqual(self.client.patch("/api/v1/outreach/missing", headers=AUTH, json={"notes": "x"}).status_code, 404)

    def test_links_reject_non_web_credentials_and_local_hosts(self):
        for value in (
            "javascript:alert(1)", "https://trusted.example@evil.example", "https://",
            "http://127.0.0.1:9000", "http://localhost:8799",
        ):
            with self.subTest(value=value):
                response = self.client.post("/api/v1/outreach", headers=AUTH, json={"company": value, "website": value})
                self.assertEqual(response.status_code, 422, response.text)

    def test_import_adds_new_and_never_overwrites_existing(self):
        existing = self.create(notes="my manual note")
        payload = {
            "items": [
                {"company": "iDvera", "notes": "imported note should not win"},
                {"company": "Inductive Robotics", "priority": "P1", "contact_name": "David Alspaugh"},
                {"company": "Bad Row", "deadline_date": "soon"},
            ]
        }
        response = self.client.post(
            "/api/v1/outreach/import",
            headers=AUTH,
            files={"upload": ("seed.json", json.dumps(payload).encode(), "application/json")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual((result["imported"], result["skipped"], len(result["errors"])), (1, 1, 1))
        kept = self.client.get(f"/api/v1/outreach/{existing['id']}", headers=AUTH).json()
        self.assertEqual(kept["notes"], "my manual note")

    def test_csv_round_trip(self):
        self.create(source_urls=["https://idvera.com/", "https://incubator.example.edu/companies"])
        exported = self.client.get("/api/v1/outreach/export?format=csv", headers=AUTH)
        self.assertEqual(exported.status_code, 200)
        self.assertIn("contact_confidence", exported.text.splitlines()[0])
        self.client.delete(f"/api/v1/outreach/{self.client.get('/api/v1/outreach', headers=AUTH).json()['items'][0]['id']}", headers=AUTH)
        reimported = self.client.post(
            "/api/v1/outreach/import",
            headers=AUTH,
            files={"upload": ("outreach.csv", exported.content, "text/csv")},
        ).json()
        self.assertEqual(reimported["imported"], 1, reimported)
        item = self.client.get("/api/v1/outreach", headers=AUTH).json()["items"][0]
        self.assertEqual(len(item["source_urls"]), 2)

    def test_export_import_keeps_unverified_research(self):
        with closing(connect_product(self.platform_path)) as conn:
            target = create_target(conn, {"company": "Deep Result"}, user_id="local-user", origin="discovery")
        exported = self.client.get("/api/v1/outreach/export?format=json", headers=AUTH)
        self.assertEqual(exported.status_code, 200)
        self.client.delete(f"/api/v1/outreach/{target['id']}", headers=AUTH)
        imported = self.client.post(
            "/api/v1/outreach/import", headers=AUTH,
            files={"upload": ("outreach.json", exported.content, "application/json")},
        ).json()
        self.assertEqual(imported["imported"], 1)
        item = self.client.get("/api/v1/outreach", headers=AUTH).json()["items"][0]
        self.assertEqual(item["research_confidence"], "unverified")

    def test_account_export_and_delete_include_outreach(self):
        created = self.create()
        self.client.patch(f"/api/v1/outreach/{created['id']}", headers=AUTH, json={"status": "drafted"})
        exported = self.client.get("/api/v1/account/export", headers=AUTH).json()
        self.assertEqual(len(exported["outreach_targets"]), 1)
        self.assertTrue(exported["outreach_events"])

    def test_outreach_never_touches_opportunities_or_applications(self):
        with closing(connect_product(self.platform_path)) as conn:
            before = (
                conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0],
            )
        created = self.create()
        self.client.patch(f"/api/v1/outreach/{created['id']}", headers=AUTH, json={"status": "sent"})
        with closing(connect_product(self.platform_path)) as conn:
            after = (
                conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0],
            )
        self.assertEqual(before, after)


class OutreachTenancyTests(unittest.TestCase):
    def test_targets_are_scoped_to_their_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, platform_path = build_and_migrate(Path(tmp))
            with closing(connect_product(platform_path)) as conn:
                ensure_product_schema(conn)
                conn.execute(
                    "INSERT INTO users(id, email, display_name, role, created_at, updated_at) VALUES('other', 'o@example.com', 'Other', 'student', 'x', 'x')"
                )
                conn.commit()
                mine = create_target(conn, {"company": "Align Engineering"}, user_id="local-user")
                create_target(conn, {"company": "Align Engineering"}, user_id="other")
                self.assertEqual(len(list_targets(conn, user_id="other")), 1)
                with self.assertRaises(LookupError):
                    update_target(conn, mine["id"], {"notes": "x"}, user_id="other")

    def test_confirmed_email_leads_the_list_ahead_of_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, platform_path = build_and_migrate(Path(tmp))
            with closing(connect_product(platform_path)) as conn:
                ensure_product_schema(conn)
                create_target(conn, {"company": "No Contact", "priority": "P1"}, user_id="local-user")
                create_target(conn, {
                    "company": "Unverified", "priority": "P1",
                    "contact_email": "maybe@unverified.example", "contact_confidence": "unverified",
                }, user_id="local-user")
                create_target(conn, {
                    "company": "Confirmed", "priority": "P3",
                    "contact_email": "jane@confirmed.example", "contact_confidence": "confirmed",
                }, user_id="local-user")
                order = [item["company"] for item in list_targets(conn, user_id="local-user")]
                self.assertEqual(order, ["Confirmed", "Unverified", "No Contact"])


class DraftCheckTests(unittest.TestCase):
    def test_flags_dashes_placeholders_and_length(self):
        checks = draft_checks("", "Hi [Name],\n\nI build drones \u2014 fast. " + "word " * 210)
        self.assertEqual(checks["dash_count"], 1)
        self.assertEqual(checks["placeholders"], ["[Name]"])
        self.assertEqual(len(checks["warnings"]), 4)

    def test_follow_up_due_only_while_awaiting_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, platform_path = build_and_migrate(Path(tmp))
            with closing(connect_product(platform_path)) as conn:
                ensure_product_schema(conn)
                target = create_target(conn, {"company": "Bovi", "status": "sent"}, user_id="local-user", today=date(2026, 9, 1))
                self.assertEqual(target["follow_up_at"], "2026-09-08")
                due = list_targets(conn, user_id="local-user", today=date(2026, 9, 9))[0]
                self.assertTrue(due["follow_up_due"])
                update_target(conn, target["id"], {"status": "declined"}, user_id="local-user")
                self.assertFalse(list_targets(conn, user_id="local-user", today=date(2026, 9, 9))[0]["follow_up_due"])

    def test_paused_or_replied_keeps_a_revisit_date_that_comes_due(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, platform_path = build_and_migrate(Path(tmp))
            with closing(connect_product(platform_path)) as conn:
                ensure_product_schema(conn)
                target = create_target(conn, {"company": "Westmag", "status": "sent"}, user_id="local-user", today=date(2026, 9, 18))
                paused = update_target(conn, target["id"], {"status": "paused"}, user_id="local-user")
                self.assertIsNone(paused["follow_up_at"], "the Sent follow-up date is not a revisit date")
                update_target(conn, target["id"], {"follow_up_at": "2027-01-05"}, user_id="local-user")
                replied = update_target(conn, target["id"], {"status": "replied"}, user_id="local-user")
                self.assertEqual(replied["follow_up_at"], "2027-01-05", "the date survives Paused to Replied")
                before = list_targets(conn, user_id="local-user", today=date(2027, 1, 4))
                self.assertFalse(before[0]["revisit_due"])
                items = list_targets(conn, user_id="local-user", today=date(2027, 1, 5))
                self.assertTrue(items[0]["revisit_due"])
                self.assertFalse(items[0]["follow_up_due"], "a revisit is not a follow-up email")
                self.assertEqual(outreach_summary(items)["revisits_due"], 1)
                self.assertEqual(outreach_summary(items)["follow_ups_due"], 0)
                declined = update_target(conn, target["id"], {"status": "declined"}, user_id="local-user")
                self.assertIsNone(declined["follow_up_at"])

    def test_csv_formula_codec_round_trips_apostrophes_and_formulas(self):
        values = ["=x", "'=x", "'plain", "''=x", "'''", "-5", "plain"]
        items = [{field: "" for field in __import__("opportunity_app.outreach", fromlist=["EXPORT_FIELDS"]).EXPORT_FIELDS} for _ in values]
        for item, value in zip(items, values):
            item.update(company=value, source_urls=[])
        encoded = export_csv(items)
        self.assertIn("'=x", encoded)
        decoded = parse_import(encoded.encode(), "outreach.csv")
        self.assertEqual([row["company"] for row in decoded], values)

    def test_sent_drafts_are_not_counted_as_waiting_for_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, platform_path = build_and_migrate(Path(tmp))
            with closing(connect_product(platform_path)) as conn:
                sent = create_target(conn, {
                    "company": "Sent", "status": "sent", "email_subject": "Hi", "email_body": "Body",
                }, user_id="local-user", today=date(2026, 9, 1))
                followed = create_target(conn, {
                    "company": "Followed", "status": "followed_up", "follow_up_subject": "Re", "follow_up_body": "Body",
                }, user_id="local-user", today=date(2026, 9, 2))
                self.assertEqual((sent["draft_status"], followed["follow_up_status"]), ("generated", "generated"))
                self.assertEqual(outreach_summary([sent, followed])["drafts_awaiting_approval"], 0)

    def test_postgres_unique_signals_are_recognized(self):
        with_sqlstate = type("PgError", (Exception,), {"sqlstate": "23505"})()
        fallback = type("UniqueViolation", (Exception,), {})()
        self.assertTrue(_is_unique_violation(with_sqlstate))
        self.assertTrue(_is_unique_violation(fallback))
        self.assertFalse(_is_unique_violation(RuntimeError("other")))

    def test_create_and_update_translate_postgres_unique_errors_only(self):
        class RaisingConnection:
            def __init__(self, error):
                self.error = error

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, _sql, _params=()):
                raise self.error

        sqlstate_error = type("PgError", (Exception,), {"sqlstate": "23505"})()
        with mock.patch.object(outreach_module, "local_today", return_value=date(2026, 9, 17)):
            with self.assertRaisesRegex(ValueError, "already in your outreach list"):
                create_target(RaisingConnection(sqlstate_error), {"company": "Duplicate"}, user_id="local-user")

        previous = {
            "company": "Existing", "status": "not_started", "contact_email": "",
            "email_subject": "", "email_body": "", "draft_status": "none", "draft_approved_at": None,
            "follow_up_subject": "", "follow_up_body": "", "follow_up_status": "none",
        }
        class UniqueViolation(Exception):
            pass
        with mock.patch.object(outreach_module, "get_target", return_value=previous), \
             mock.patch.object(outreach_module, "local_today", return_value=date(2026, 9, 17)):
            with self.assertRaisesRegex(ValueError, "already in your outreach list"):
                update_target(RaisingConnection(UniqueViolation()), "target", {"company": "Duplicate"}, user_id="local-user")
        unrelated = RuntimeError("database unavailable")
        with mock.patch.object(outreach_module, "local_today", return_value=date(2026, 9, 17)):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                create_target(RaisingConnection(unrelated), {"company": "Other"}, user_id="local-user")

    def test_0011_backfills_saved_timezone_and_discovery_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "through-0010.db"
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            try:
                for migration in sorted(Path("migrations").glob("00*.sql")):
                    if migration.name > "0010_outreach_pipeline.sql":
                        break
                    conn.executescript(migration.read_text(encoding="utf-8"))
                conn.execute("INSERT INTO users(id, email, display_name, role, created_at, updated_at) VALUES('local-user', 'a@b.test', 'Local', 'student', 'x', 'x')")
                conn.execute("INSERT INTO notification_preferences(user_id, timezone, updated_at) VALUES('local-user', 'America/Chicago', 'x')")
                conn.execute("INSERT INTO outreach_targets(id, user_id, company, origin, created_at, updated_at) VALUES('manual', 'local-user', 'Manual', 'manual', 'x', 'x')")
                conn.execute("INSERT INTO outreach_targets(id, user_id, company, origin, created_at, updated_at) VALUES('found', 'local-user', 'Found', 'discovery', 'x', 'x')")
                conn.executescript(Path("migrations/0011_outreach_provenance.sql").read_text(encoding="utf-8"))
                self.assertEqual(conn.execute("SELECT timezone_explicit FROM notification_preferences").fetchone()[0], 1)
                confidence = dict(conn.execute("SELECT id, research_confidence FROM outreach_targets").fetchall())
                self.assertEqual(confidence, {"manual": "confirmed", "found": "unverified"})
                instant = __import__("datetime").datetime(2026, 9, 17, 3, 55, tzinfo=__import__("datetime").timezone.utc)
                self.assertEqual(local_today(conn, "local-user", instant), date(2026, 9, 16))
            finally:
                conn.close()

    def test_saving_even_utc_marks_the_timezone_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, platform_path = build_and_migrate(Path(tmp))
            with closing(connect_product(platform_path)) as conn:
                update_preferences(conn, {"timezone": "UTC"}, user_id="local-user")
                self.assertEqual(conn.execute(
                    "SELECT timezone_explicit FROM notification_preferences WHERE user_id='local-user'"
                ).fetchone()[0], 1)
                update_preferences(conn, {"quiet_start": "21:00"}, user_id="local-user")
                self.assertEqual(conn.execute(
                    "SELECT timezone_explicit FROM notification_preferences WHERE user_id='local-user'"
                ).fetchone()[0], 1)


class ImportedLocationProvenanceTests(unittest.TestCase):
    """An import file's word must never be recorded as the student's own.

    create_target used to stamp location_basis='manual' for every origin that
    was not 'discovery', so a location that arrived in a file was shown as
    "your entry", counted as verified, relied on by drafts, and — because
    'manual' outranks every source — never correctable by the company's site.
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        _, self.platform_path = build_and_migrate(Path(self.tempdir.name))
        self.conn = connect_product(self.platform_path)
        self.addCleanup(self.tempdir.cleanup)
        self.addCleanup(self.conn.close)

    def events(self, target_id, event_type):
        rows = self.conn.execute(
            "SELECT detail FROM outreach_events WHERE target_id=? AND event_type=? ORDER BY created_at",
            (target_id, event_type),
        ).fetchall()
        return [row[0] for row in rows]

    def imported(self, **claim):
        record = {"company": "Nova Aero", "location": "Cedar Park, TX", **claim}
        result = import_targets(self.conn, [record], user_id=USER)
        self.assertEqual(result["errors"], [])
        return get_target(self.conn, result["created_ids"][0], user_id=USER)

    def test_a_round_trip_through_export_and_import_cannot_launder_provenance(self):
        searched = create_target(
            self.conn, {"company": "Nova Aero", "location": "Cedar Park, TX"},
            user_id=USER, origin="discovery",
        )
        self.assertEqual((searched["location_basis"], searched["location_verified"]), ("research", False))

        exported = export_csv([searched])
        self.assertIn("location_inferred", exported.splitlines()[0], "the file must not hide an inference")
        records = parse_import(exported.replace("Nova Aero", "Nova Aero Two").encode("utf-8"), "targets.csv")
        self.assertEqual(records[0]["location_basis"], "research", "the file still carries the claim")

        target = self.imported(**{k: v for k, v in records[0].items() if k != "company"}, company="Nova Aero Two")
        self.assertEqual(
            (target["location"], target["location_basis"], target["location_verified"]),
            ("Cedar Park, TX", "", False),
            "an imported location is nobody's entry and nothing has checked it",
        )
        self.assertEqual(target["location_source_url"], "")

    def test_the_company_site_can_correct_an_imported_location(self):
        """The 'kept' regression: 'manual' outranks every source, so a
        fabricated provenance froze the wrong city in place permanently."""
        target = self.imported()
        outcome = apply_location(
            self.conn, target["id"], user_id=USER, location="Austin, TX",
            basis="company_site", source_url="https://novaaero.example/about",
        )
        self.assertEqual(outcome, "recorded")
        corrected = get_target(self.conn, target["id"], user_id=USER)
        self.assertEqual((corrected["location"], corrected["location_basis"]), ("Austin, TX", "company_site"))

    def test_confirming_an_imported_location_makes_it_the_students(self):
        target = self.imported()
        confirmed = update_target(
            self.conn, target["id"], {"confirm_location": "Cedar Park, TX"}, user_id=USER,
        )
        self.assertEqual(
            (confirmed["location_basis"], confirmed["location_verified"]), ("manual", True),
        )
        self.assertEqual(self.events(target["id"], "location_confirmed"), ["You confirmed Cedar Park, TX"])

    def test_an_unverified_basis_is_never_read_as_confirmed(self):
        """The allowlist fails closed: the old test was `basis != "research"`,
        so a blank or unrecognised basis read as verified."""
        for basis in ("", "a_basis_from_the_future"):
            with self.subTest(basis=basis):
                self.assertFalse(location_usable(
                    {"location": "Austin, TX", "location_basis": basis, "location_inferred": 0}
                ))
        self.assertTrue(location_usable(
            {"location": "Austin, TX", "location_basis": "company_site", "location_inferred": 0}
        ))
        self.assertFalse(location_usable(
            {"location": "Austin, TX", "location_basis": "company_site", "location_inferred": 1}
        ))

    def test_a_typed_location_is_recorded_as_the_students_own_word(self):
        target = self.imported()
        update_target(self.conn, target["id"], {"location": "Georgetown, TX"}, user_id=USER)
        self.assertEqual(self.events(target["id"], "location_entered"), ["You entered Georgetown, TX"])
        self.assertEqual(self.events(target["id"], "location_recorded"), [], "a source did not establish it")



class ConfirmLocationRaceTests(unittest.TestCase):
    """Confirming vouches for the place on screen, not for whatever is in the row.

    A locator or profile pass can change a location between the page rendering
    and the click, and "manual" outranks every source permanently, so a bare
    flag would let the student's name be attached to a place they never saw.
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        _, self.platform_path = build_and_migrate(root)
        app = create_app(
            db_path=self.platform_path, access_token="outreach-owner", admin_token="outreach-admin",
            static_dir=STATIC_DIR, resume_storage=root / "resumes",
            capture_storage=root / "captures", interview_storage=root / "interviews",
        )
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.tempdir.cleanup)
        self.addCleanup(self.client.__exit__, None, None, None)
        self.conn = connect_product(self.platform_path)
        self.addCleanup(self.conn.close)

    def target(self):
        result = import_targets(self.conn, [{"company": "Nova Aero", "location": "Cedar Park, TX"}], user_id=USER)
        return get_target(self.conn, result["created_ids"][0], user_id=USER)

    def patch(self, target_id, body):
        return self.client.patch(f"/api/v1/outreach/{target_id}", headers=AUTH, json=body)

    def test_confirming_a_location_that_moved_is_a_409_not_a_422(self):
        target = self.target()
        self.conn.execute("UPDATE outreach_targets SET location='Reno, NV' WHERE id=?", (target["id"],))
        self.conn.commit()
        response = self.patch(target["id"], {"confirm_location": "Cedar Park, TX"})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn("Reno, NV", response.json()["detail"])
        after = get_target(self.conn, target["id"], user_id=USER)
        self.assertEqual((after["location"], after["location_basis"]), ("Reno, NV", ""))

    def test_a_bare_flag_cannot_say_which_place_is_being_vouched_for(self):
        target = self.target()
        response = self.patch(target["id"], {"confirm_location": True})
        self.assertEqual(response.status_code, 422, response.text)
        self.assertFalse(get_target(self.conn, target["id"], user_id=USER)["location_verified"])

    def test_confirming_the_shown_location_still_works_through_the_route(self):
        target = self.target()
        response = self.patch(target["id"], {"confirm_location": "Cedar Park, TX"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["location_basis"], response.json()["location_verified"]), ("manual", True))

    def test_a_stronger_basis_arriving_mid_confirmation_is_not_demoted(self):
        """The guard covers basis and inferred, not just the location text.

        Guarding on the text alone still matches when enrichment attaches
        company_site and a source URL to the *same* place — and the stale
        confirmation would overwrite it with "manual" while keeping the
        company's URL, rendering "your entry" linked to a page the student
        never vouched for.
        """
        target = self.target()
        real = outreach_module.get_target
        state = {"raced": False}

        def race(conn, target_id, **kwargs):
            row = real(conn, target_id, **kwargs)
            if not state["raced"] and target_id == target["id"]:
                state["raced"] = True
                conn.execute(
                    "UPDATE outreach_targets SET location_basis='company_site', "
                    "location_source_url='https://novaaero.example/about' WHERE id=?", (target["id"],))
                conn.commit()
            return row

        with mock.patch.object(outreach_module, "get_target", race):
            confirmed = update_target(self.conn, target["id"], {"confirm_location": "Cedar Park, TX"}, user_id=USER)
        self.assertTrue(state["raced"], "the test did not actually interleave")
        self.assertEqual(
            (confirmed["location_basis"], confirmed["location_source_url"]),
            ("company_site", "https://novaaero.example/about"),
            "a page-checked basis must not be demoted to the student's entry",
        )
        self.assertTrue(confirmed["location_verified"])


class ImportClaimRecordTests(unittest.TestCase):
    """The refused claim is the only surviving record of what the file said."""

    def detail(self, **claim):
        return json.loads(_claim_detail(claim))["unverified_import_claim"]

    def test_a_csv_false_is_recorded_as_the_word_the_file_used(self):
        """A CSV round trip turns False into the string "False", which is true
        under ordinary truthiness — interpreting it would record its opposite."""
        self.assertEqual(self.detail(location_inferred="False")["location_inferred"], "False")
        self.assertEqual(self.detail(location_inferred=False)["location_inferred"], "False")
        self.assertEqual(self.detail(location_inferred=True)["location_inferred"], "True")
        self.assertEqual(self.detail()["location_inferred"], "")

    def test_a_hand_edited_file_cannot_break_the_record(self):
        for value in (1, 0, None, {"nested": ["deep"]}, ["a", "b"], 3.5):
            with self.subTest(value=value):
                detail = _claim_detail({"location": "Austin, TX", "location_basis": value})
                self.assertEqual(json.loads(detail)["unverified_import_claim"]["location"], "Austin, TX")

    def test_the_record_stays_valid_json_within_its_bound(self):
        """Truncating the finished JSON would both invalidate it and, under
        sorted keys, chop the source URL off first."""
        detail = _claim_detail({
            "location": 'A"' + "\x00\x01" * 400 + " ",
            "location_basis": "x" * 500,
            "location_inferred": "y" * 500,
            "location_source_url": "https://example.com/" + "z" * 900,
        })
        self.assertLessEqual(len(detail), CLAIM_DETAIL_LIMIT)
        claim = json.loads(detail)["unverified_import_claim"]
        self.assertEqual(sorted(claim), ["location", "location_basis", "location_inferred", "location_source_url"])
        self.assertTrue(claim["location_source_url"].endswith("…"), "the URL is clipped, not dropped")
        self.assertTrue(claim["location_source_url"].startswith("https://example.com/"))

    def test_the_claim_reads_as_an_import_claim_and_not_as_a_recording(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        _, platform_path = build_and_migrate(Path(tempdir.name))
        with closing(connect_product(platform_path)) as conn:
            result = import_targets(conn, [{
                "company": "Nova Aero", "location": "Cedar Park, TX",
                "location_basis": "company_site", "location_source_url": "https://novaaero.example/about",
            }], user_id=USER)
            target_id = result["created_ids"][0]
            rows = conn.execute(
                "SELECT event_type, detail FROM outreach_events WHERE target_id=?", (target_id,)).fetchall()
            by_type = {row[0]: row[1] for row in rows}
            self.assertIn("location_import_claim", by_type, "the refused claim has to be answerable later")
            self.assertNotIn("location_recorded", by_type, "nothing established this location")
            claim = json.loads(by_type["location_import_claim"])["unverified_import_claim"]
            self.assertEqual(claim["location_basis"], "company_site", "what the file claimed is kept verbatim")
            self.assertEqual(claim["location_source_url"], "https://novaaero.example/about")
            self.assertEqual(
                get_target(conn, target_id, user_id=USER)["location_basis"], "",
                "and is not honoured on the row",
            )



class ApplyLocationRaceTests(unittest.TestCase):
    """apply_location decides from a row read outside its write.

    Two enrichment passes can both read the weak state, and without a guard the
    loser's weaker result lands last — overwriting a page-checked basis with
    one that ranks below it.
    """

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        _, platform_path = build_and_migrate(Path(self.tempdir.name))
        self.conn = connect_product(platform_path)
        self.addCleanup(self.tempdir.cleanup)
        self.addCleanup(self.conn.close)
        result = import_targets(
            self.conn, [{"company": "Nova Aero", "location": "Cedar Park, TX"}], user_id=USER)
        self.target_id = result["created_ids"][0]

    def events(self, event_type):
        return [row[0] for row in self.conn.execute(
            "SELECT detail FROM outreach_events WHERE target_id=? AND event_type=?",
            (self.target_id, event_type)).fetchall()]

    def test_a_weaker_writer_cannot_land_on_top_of_a_stronger_one(self):
        real = profile_module.get_target
        state = {"raced": False}

        def race(conn, target_id, **kwargs):
            row = real(conn, target_id, **kwargs)
            if not state["raced"] and target_id == self.target_id:
                # The company's own site lands while the search still holds a
                # decision made against the blank basis it read.
                state["raced"] = True
                conn.execute(
                    "UPDATE outreach_targets SET location='Austin, TX', location_basis='company_site', "
                    "location_source_url='https://novaaero.example/about' WHERE id=?", (self.target_id,))
                conn.commit()
            return row

        with mock.patch.object(profile_module, "get_target", race):
            outcome = apply_location(
                self.conn, self.target_id, user_id=USER, location="Reno, NV",
                basis="web_search", source_url="https://directory.example/nova")
        self.assertTrue(state["raced"], "the test did not actually interleave")
        self.assertEqual(outcome, "kept")
        target = get_target(self.conn, self.target_id, user_id=USER)
        self.assertEqual(
            (target["location"], target["location_basis"]), ("Austin, TX", "company_site"),
            "the company's own site outranks a directory and must survive",
        )
        self.assertEqual(self.events("location_recorded"), [], "a write that never landed must not be logged")


class ImportBackfillMigrationTests(unittest.TestCase):
    """0019 repairs the rows the bug already laundered.

    A 'manual' row is location_verified, and the UI shows the Confirm button
    only when it is not — so a row left fabricated has no remedy the student can
    even see. Absence of evidence demotes; only a location_confirmed event keeps.
    """

    def rows(self, conn):
        return {
            row[0]: (row[1], row[2]) for row in conn.execute(
                "SELECT company, location_basis, location_source_url FROM outreach_targets").fetchall()
        }

    def test_an_imported_manual_location_is_demoted_unless_it_was_confirmed(self):
        with tempfile.TemporaryDirectory() as directory:
            _, platform_path = build_and_migrate(Path(directory))
            with closing(connect_product(platform_path)) as conn:
                shapes = [
                    ("Untouched", "import", "manual", False),
                    ("EditedSince", "import", "manual", False),
                    ("Confirmed", "import", "manual", True),
                    ("TypedHere", "manual", "manual", False),
                    ("DeepSearch", "discovery", "research", False),
                ]
                for company, origin, basis, confirmed in shapes:
                    target = create_target(
                        conn, {"company": company, "location": "Cedar Park, TX"}, user_id=USER, origin=origin)
                    # Put the row back into the state the bug produced.
                    conn.execute(
                        "UPDATE outreach_targets SET location_basis=?, location_source_url=? WHERE id=?",
                        (basis, "https://stale.example/about" if basis == "manual" else "", target["id"]))
                    if company == "EditedSince":
                        conn.execute("UPDATE outreach_targets SET notes='called them' WHERE id=?", (target["id"],))
                    if confirmed:
                        conn.execute(
                            "INSERT INTO outreach_events(id, target_id, user_id, event_type, detail, created_at) "
                            "VALUES(?, ?, ?, 'location_confirmed', 'You confirmed Cedar Park, TX', ?)",
                            (f"event-{company}", target["id"], USER, utc_now()))
                conn.commit()
                before = self.rows(conn)
                self.assertTrue(all(basis == "manual" for basis, _ in list(before.values())[:4]))

                # Re-apply 0019 alone against those rows.
                conn.execute(
                    "DELETE FROM schema_migrations WHERE name='0019_outreach_import_location_basis.sql'")
                conn.commit()
                ensure_product_schema(conn)

                after = self.rows(conn)
                self.assertEqual(after["Untouched"], ("", ""), "nobody typed it")
                self.assertEqual(after["EditedSince"], ("", ""),
                                 "an unrelated edit is not evidence the student vouched for the location")
                self.assertEqual(after["Confirmed"], ("manual", "https://stale.example/about"),
                                 "an explicit confirmation is the evidence that keeps it")
                self.assertEqual(after["TypedHere"], ("manual", "https://stale.example/about"),
                                 "a target created here is untouched")
                self.assertEqual(after["DeepSearch"][0], "research")

    def test_the_backfill_is_safe_to_apply_twice(self):
        with tempfile.TemporaryDirectory() as directory:
            _, platform_path = build_and_migrate(Path(directory))
            with closing(connect_product(platform_path)) as conn:
                import_targets(conn, [{"company": "Nova Aero", "location": "Cedar Park, TX"}], user_id=USER)
                for _ in range(2):
                    conn.execute(
                        "DELETE FROM schema_migrations WHERE name='0019_outreach_import_location_basis.sql'")
                    conn.commit()
                    ensure_product_schema(conn)
                self.assertEqual(self.rows(conn)["Nova Aero"], ("", ""))


if __name__ == "__main__":
    unittest.main()
