"""Phase 2 automation that stays inside the app: résumé variants, silent applications, auto-close,
follow-up drafts, and saving or passing on new roles by score. Every change has an Undo."""

import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR, auto_triage, automation, automation_handlers, automation_health, internal_automation, migrate, outreach_inbox
from opportunity_app.student import resume_variants
from opportunity_app.core import schema
from opportunity_app.actions import record_intent, update_application
from opportunity_app.api import create_app
from opportunity_app.automation import Superseded
from opportunity_app.extension_apply import apply_context
from opportunity_app.outreach import (
    create_target, delete_target, get_target, lifecycle_suggestion, list_targets, log_reply, update_target,
)
from opportunity_app.outreach_automation import AutomationWorker
from opportunity_app.outreach_delivery import record_bounce
from opportunity_app.outreach_versions import draft_versions
from opportunity_app.refresh import RefreshManager
from opportunity_app.student.resumes import ResumeValidationError, confirm_variant, resume_record
from opportunity_app.core.schema import ensure_product_schema
from opportunity_app.core.database import connect_product, has_column
from opportunity_app.core.timestamps import utc_now
from opportunity_app.urgent import urgent_queue
from opportunity_app.core.user_time import user_timezone

from helpers_platform import build_and_migrate

USER = "local-user"
AUTH = {"Authorization": "Bearer internal-owner"}
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


def legacy(_provider, _model):
    raise AssertionError("the template drafter calls no model")


class Case(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.legacy_path, self.platform_path = build_and_migrate(self.root)
        self.conn = connect_product(self.platform_path)
        self.addCleanup(self.conn.close)

    # --- helpers ---

    def on(self, *keys):
        for key in keys:
            automation.set_mode(self.conn, USER, key, "on")

    def set_profile(self, **values):
        row = self.conn.execute("SELECT profile_json FROM profiles WHERE user_id=?", (USER,)).fetchone()
        profile = {**json.loads(row[0] or "{}"), **values}
        with self.conn:
            self.conn.execute("UPDATE profiles SET profile_json=? WHERE user_id=?", (json.dumps(profile), USER))

    def confirm_skills(self, skills):
        now = utc_now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO profile_facts(user_id, field_path, value_json, source, confirmed, created_at, updated_at) "
                "VALUES(?, 'skills', ?, 'user', 1, ?, ?) ON CONFLICT(user_id, field_path) DO UPDATE SET value_json=excluded.value_json",
                (USER, json.dumps(skills), now, now),
            )

    def add_resume(self, *, label="", text="A resume with enough words to be useful.", status="confirmed", name=None, parsed=None):
        file_id, version_id = f"resume-file-{uuid4().hex}", f"resume-{uuid4().hex}"
        now = utc_now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO resume_files(id, user_id, original_name, media_type, byte_size, sha256, storage_path, created_at, variant_label) "
                "VALUES(?, ?, ?, 'application/pdf', 1000, ?, ?, ?, ?)",
                (file_id, USER, name or f"{label or 'main'}.pdf", uuid4().hex, f"{file_id}.pdf", now, label),
            )
            self.conn.execute(
                "INSERT INTO resume_versions(id, resume_file_id, user_id, extracted_text, parsed_json, confirmed_json, status, created_at, confirmed_at) "
                "VALUES(?, ?, ?, ?, ?, '{}', ?, ?, ?)",
                (version_id, file_id, USER, text, json.dumps(parsed or {}), status, now, now if status == "confirmed" else None),
            )
        return {"file_id": file_id, "version_id": version_id}

    def add_opportunity(self, opportunity_id, *, title="Engineering Intern", description="A role.", score=50,
                        first_seen=None, reasons=None):
        now = utc_now()
        first_seen = first_seen or (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(timespec="seconds")
        with self.conn:
            self.conn.execute(
                "INSERT INTO opportunities(id, company, title, location, url, description, first_seen_at, last_seen_at, "
                "active, fingerprint, created_at, updated_at) VALUES(?, 'Nimbus Labs', ?, 'Remote', ?, ?, ?, ?, 1, ?, ?, ?)",
                (opportunity_id, title, f"https://example.com/{opportunity_id}", description, first_seen, first_seen,
                 f"fp-{opportunity_id}", first_seen, now),
            )
            self.conn.execute(
                "INSERT INTO fit_scores(opportunity_id, user_id, ruleset_version, score, explanation_json, created_at) "
                "VALUES(?, ?, 'legacy-v1', ?, ?, ?)",
                (opportunity_id, USER, score, json.dumps(reasons or ["35 base", "+10 skills: Python", "+4 interests: data"]), now),
            )

    def intent(self, opportunity_id):
        row = self.conn.execute(
            "SELECT action FROM opportunity_interactions WHERE opportunity_id=? AND user_id=? ORDER BY id DESC LIMIT 1",
            (opportunity_id, USER),
        ).fetchone()
        return row[0] if row else None

    def client(self):
        app = create_app(
            db_path=self.platform_path, access_token="internal-owner", static_dir=STATIC_DIR,
            resume_storage=self.root / "resumes", capture_storage=self.root / "captures",
            interview_storage=self.root / "interviews", start_automation_worker=False,
        )
        return TestClient(app)


# --- The registry ---------------------------------------------------------------------------


class RegistryTests(Case):
    def test_each_new_switch_is_registered_off_internal_and_two_mode(self):
        expected = {
            "resume_variant_pick": "applications", "application_silence": "applications",
            "archive_silent_applications": "applications", "outreach_auto_close": "outreach",
            "auto_follow_up_drafts": "outreach", "auto_save": "discovery", "auto_pass": "discovery",
        }
        for key, group in expected.items():
            with self.subTest(key=key):
                feature = automation.FEATURES[key]
                self.assertEqual((feature.group, feature.risk, feature.modes), (group, "internal", automation.OFF_ON))
                self.assertTrue(feature.label and feature.description)
                self.assertEqual(automation.mode(self.conn, USER, key), "off")

    def test_triage_switches_need_their_thresholds_and_the_panel_says_why(self):
        payload = {item["key"]: item for item in automation.settings_payload(self.conn, USER)["features"]}
        self.assertFalse(payload["auto_save"]["can_turn_on"])
        self.assertIn("automation.auto_save_at", payload["auto_save"]["can_turn_on_reason"])
        self.assertIn("automation.auto_pass_below", payload["auto_pass"]["requirement"])
        with self.assertRaisesRegex(automation.AutomationGateError, "auto_save_at"):
            automation.set_mode(self.conn, USER, "auto_save", "on")
        self.set_profile(automation={"auto_save_at": 80, "auto_pass_below": 90})
        with self.assertRaisesRegex(automation.AutomationGateError, "above"):
            automation.set_mode(self.conn, USER, "auto_pass", "on")
        self.set_profile(automation={"auto_save_at": 80, "auto_pass_below": 30})
        self.on("auto_save", "auto_pass")
        payload = {item["key"]: item for item in automation.settings_payload(self.conn, USER)["features"]}
        self.assertEqual((payload["auto_save"]["mode"], payload["auto_save"]["requirement"]), ("on", ""))
        self.assertEqual(auto_triage.requirement(self.conn, "student-2", "auto_save")[:6], "Scores", "the owner's scores only")

    def test_turning_a_switch_on_records_when(self):
        self.on("application_silence")
        first = automation.on_since(self.conn, USER, "application_silence")
        self.assertTrue(first)
        automation.set_mode(self.conn, USER, "application_silence", "on")
        self.assertEqual(automation.on_since(self.conn, USER, "application_silence"), first, "saving on again keeps the time")
        automation.set_mode(self.conn, USER, "application_silence", "off")
        self.assertIsNone(automation.on_since(self.conn, USER, "application_silence"))


# --- Résumé variants ------------------------------------------------------------------------

VARIANTS = [
    {"label": "Hardware", "keywords": ["CAD", "SolidWorks", "mechanical", "PCB"]},
    {"label": "Software", "keywords": ["Python", "React", "backend", "C++"]},
]


class ChooseVariantTests(unittest.TestCase):
    def test_a_clear_winner(self):
        choice = resume_variants.choose_variant(
            "Mechanical Engineering Intern", "Design parts in SolidWorks and CAD.", VARIANTS, "Software",
        )
        self.assertEqual((choice["status"], choice["label"]), ("picked", "Hardware"))
        self.assertEqual(choice["matched"], ["CAD", "SolidWorks", "mechanical"])
        self.assertEqual(choice["points"], {"Hardware": 5, "Software": 0})

    def test_a_margin_too_small_is_unsure_and_uses_the_default(self):
        choice = resume_variants.choose_variant("Intern", "CAD and SolidWorks, plus Python and React.", VARIANTS, "software")
        self.assertEqual(choice["points"], {"Hardware": 2, "Software": 2})
        self.assertEqual((choice["status"], choice["label"]), ("unsure", "Software"), "the default, matched case-insensitively")
        self.assertIn("Couldn't tell which variant fits", choice["reason"])
        low = resume_variants.choose_variant("Intern", "Some CAD.", VARIANTS, "")
        self.assertEqual((low["status"], low["label"]), ("unsure", ""), "one point is not enough, and there is no default")
        self.assertIn("no default variant is set", low["reason"])

    def test_title_hits_weigh_three_times(self):
        choice = resume_variants.choose_variant("Mechanical Systems Intern", "Python, React, and backend services.", VARIANTS, "")
        self.assertEqual(choice["points"], {"Hardware": 3, "Software": 3})
        self.assertEqual(choice["status"], "unsure", "a title hit ties three description hits")
        choice = resume_variants.choose_variant("Mechanical PCB Intern", "Python, React, and backend services.", VARIANTS, "")
        self.assertEqual((choice["status"], choice["label"]), ("picked", "Hardware"), "6 beats 3 by 1.5 times")

    def test_whole_words_only_and_punctuated_keywords(self):
        self.assertFalse(resume_variants.mentions("CADENCE tools", "CAD"))
        self.assertTrue(resume_variants.mentions("Modern C++ and Go", "C++"))
        self.assertTrue(resume_variants.mentions("real-time embedded   systems", "embedded systems"))
        self.assertFalse(resume_variants.mentions("C++", "C"))

    def test_no_variants_means_no_pick(self):
        choice = resume_variants.choose_variant("Intern", "CAD", [], "Hardware")
        self.assertEqual((choice["status"], choice["label"]), ("no_variants", ""))

    def test_the_profile_list_is_cleaned(self):
        variants, default = resume_variants.configured_variants({
            "resume_variants": [{"label": "  Hardware "}, {"label": "hardware", "keywords": ["x"]}, {"keywords": ["y"]}, "bad",
                                {"label": "Software", "keywords": ["Python", 3, " "]}],
            "default_variant": " Software ",
        })
        self.assertEqual(variants, [{"label": "Hardware", "keywords": []}, {"label": "Software", "keywords": ["Python"]}])
        self.assertEqual(default, "Software")


class ResumePickTests(Case):
    def setUp(self):
        super().setUp()
        self.hardware = self.add_resume(label="Hardware", text="CAD projects and SolidWorks assemblies.")
        self.software = self.add_resume(label="Software", text="Python services.")
        self.set_profile(resume_variants=VARIANTS, default_variant="Software")

    def pick(self, opportunity_id="job-a"):
        return resume_variants.stored_pick(self.conn, USER, opportunity_id)

    def test_saving_a_role_picks_its_variant_through_the_ledger_when_the_switch_is_on(self):
        record_intent(self.conn, "job-a", "undo", user_id=USER)
        record_intent(self.conn, "job-a", "saved", user_id=USER)
        self.assertIsNone(self.pick(), "off by default")
        self.on("resume_variant_pick")
        record_intent(self.conn, "job-a", "undo", user_id=USER)
        record_intent(self.conn, "job-a", "saved", user_id=USER)
        pick = self.pick()
        self.assertEqual((pick["resume_file_id"], pick["picked_by"], pick["label"]), (self.hardware["file_id"], "automatic", "Hardware"))
        self.assertEqual(pick["matched"], ["SolidWorks", "mechanical"])
        [action] = automation.list_actions(self.conn, USER, feature="resume_variant_pick")
        self.assertEqual((action["action_type"], action["status"]), ("resume.pick", "applied"))
        self.assertIn("Hardware", action["summary"])
        automation.undo(self.conn, action["id"], USER)
        self.assertIsNone(self.pick(), "undo clears the pick")

    def test_an_unsure_role_gets_the_default_with_the_reason(self):
        self.on("resume_variant_pick")
        record_intent(self.conn, "job-b", "saved", user_id=USER)
        pick = self.pick("job-b")
        # "Controls Co-op" / "Summer 2027 controls role with Python." -> Software 1 point: unsure.
        self.assertEqual((pick["label"], pick["status"]), ("Software", "unsure"))
        self.assertIn("Couldn't tell", pick["reason"])

    def test_a_student_pick_sticks(self):
        self.on("resume_variant_pick")
        resume_variants.set_student_pick(self.conn, USER, "job-a", self.software["file_id"])
        record_intent(self.conn, "job-a", "undo", user_id=USER)
        record_intent(self.conn, "job-a", "saved", user_id=USER)
        pick = self.pick()
        self.assertEqual((pick["resume_file_id"], pick["picked_by"]), (self.software["file_id"], "student"))
        self.assertEqual(automation.list_actions(self.conn, USER, feature="resume_variant_pick"), [], "nothing to record")

    def test_undo_refuses_once_the_student_changed_the_pick(self):
        self.on("resume_variant_pick")
        record_intent(self.conn, "job-a", "undo", user_id=USER)
        record_intent(self.conn, "job-a", "saved", user_id=USER)
        [action] = automation.list_actions(self.conn, USER, feature="resume_variant_pick")
        resume_variants.set_student_pick(self.conn, USER, "job-a", self.software["file_id"])
        with self.assertRaisesRegex(Superseded, "résumé choice changed"):
            automation.undo(self.conn, action["id"], USER)
        self.assertEqual(self.pick()["picked_by"], "student")

    def test_no_variants_falls_back_to_the_confirmed_resume(self):
        self.on("resume_variant_pick")
        self.set_profile(resume_variants=[], default_variant="")
        self.assertIn("resume_variants", automation.requirement(self.conn, USER, "resume_variant_pick"),
                      "a switch left on says why it cannot pick")
        record_intent(self.conn, "job-a", "undo", user_id=USER)
        record_intent(self.conn, "job-a", "saved", user_id=USER)
        self.assertIsNone(self.pick())
        view = resume_variants.pick_view(self.conn, USER, "job-a")
        self.assertEqual((view["configured"], view["pick"], view["suggestion"]["status"]), (False, None, "no_variants"))
        self.assertEqual(len(view["options"]), 2, "every confirmed résumé can still be chosen by hand")

    def test_a_variant_needs_a_confirmed_resume_with_its_label(self):
        with self.conn:
            self.conn.execute("UPDATE resume_files SET variant_label='' WHERE user_id=?", (USER,))
        choice = resume_variants.pick_variant(self.conn, USER, "job-a")
        self.assertEqual((choice["status"], choice["resume_file_id"]), ("no_variants", None))
        self.assertIn("confirmed résumé with its label", choice["reason"])

    def test_the_extension_preselects_the_picked_variant_and_keeps_the_order(self):
        with self.conn:
            self.conn.execute("UPDATE applications SET opportunity_id='job-a' WHERE id='app-job-b'")
        before = apply_context(self.conn, "app-job-b", user_id=USER)["documents"]
        self.assertFalse(any(item["preferred"] for item in before), "no pick, nothing preferred")
        resume_variants.set_student_pick(self.conn, USER, "job-a", self.hardware["file_id"])
        after = apply_context(self.conn, "app-job-b", user_id=USER)["documents"]
        self.assertEqual([item["artifact_id"] for item in after], [item["artifact_id"] for item in before], "the order is stable")
        self.assertEqual([item["artifact_id"] for item in after if item["preferred"]], [self.hardware["version_id"]])
        self.assertEqual({item["variant_label"] for item in after}, {"Hardware", "Software"})

    def test_a_role_the_student_cannot_see_is_never_picked(self):
        other = "student-b"
        stamp = "2026-09-01T00:00:00+00:00"
        with self.conn:
            self.conn.execute(
                "INSERT INTO users(id, email, display_name, role, created_at, updated_at) VALUES(?, 'b@example.com', 'B', 'student', ?, ?)",
                (other, stamp, stamp),
            )
            self.conn.execute(
                "INSERT INTO profiles(user_id, profile_json, created_at, updated_at) VALUES(?, ?, ?, ?)",
                (other, json.dumps({"resume_variants": VARIANTS, "default_variant": "Hardware"}), stamp, stamp),
            )
            # The owner's private capture, which only the owner may see.
            self.conn.execute(
                "INSERT INTO opportunities(id, company, title, location, url, description, first_seen_at, last_seen_at, active, "
                "fingerprint, created_at, updated_at) VALUES('manual-secret', 'Secret Startup', 'Private PCB Role', '', "
                "'https://secret.test/job', 'CAD and PCB work.', ?, ?, 1, 'fp-secret', ?, ?)",
                (stamp, stamp, stamp, stamp),
            )
            self.conn.execute(
                "INSERT INTO opportunity_sources(opportunity_id, source_key, source_name, external_id, source_url, first_seen_at, last_seen_at) "
                "VALUES('manual-secret', 'manual:capture', 'Manual capture', 'cap-secret', 'https://secret.test/job', ?, ?)",
                (stamp, stamp),
            )
            self.conn.execute(
                "INSERT INTO applications(id, opportunity_id, user_id, stage, created_at, updated_at) "
                "VALUES('app-secret', 'manual-secret', ?, 'applying', ?, ?)",
                (USER, stamp, stamp),
            )
            self.conn.execute(
                "INSERT INTO opportunity_captures(id, user_id, source_type, source_url, status, application_id, created_at) "
                "VALUES('cap-secret', ?, 'url', 'https://secret.test/job', 'confirmed', 'app-secret', ?)",
                (USER, stamp),
            )
        file_id = f"resume-file-{uuid4().hex}"
        with self.conn:
            self.conn.execute(
                "INSERT INTO resume_files(id, user_id, original_name, media_type, byte_size, sha256, storage_path, created_at, variant_label) "
                "VALUES(?, ?, 'hw.pdf', 'application/pdf', 1000, ?, ?, ?, 'Hardware')",
                (file_id, other, uuid4().hex, f"{file_id}.pdf", stamp),
            )
            self.conn.execute(
                "INSERT INTO resume_versions(id, resume_file_id, user_id, extracted_text, parsed_json, confirmed_json, status, created_at, confirmed_at) "
                "VALUES(?, ?, ?, 'CAD work.', '{}', '{}', 'confirmed', ?, ?)",
                (f"resume-{uuid4().hex}", file_id, other, stamp, stamp),
            )
        automation.set_mode(self.conn, other, "resume_variant_pick", "on")
        record_intent(self.conn, "manual-secret", "saved", user_id=other)
        self.assertIsNone(resume_variants.pick_after_save(self.conn, other, "manual-secret"))
        self.assertEqual(automation.list_actions(self.conn, other, feature="resume_variant_pick"), [],
                         "nothing about another student's capture reaches this student's ledger")
        self.assertIsNone(resume_variants.stored_pick(self.conn, other, "manual-secret"))

    def test_a_student_pick_that_lands_after_the_read_stands_and_no_row_claims_a_pick(self):
        self.on("resume_variant_pick")
        record_intent(self.conn, "job-a", "undo", user_id=USER)
        stamp = utc_now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO opportunity_interactions(opportunity_id, user_id, action, created_at, source) VALUES('job-a', ?, 'saved', ?, 'user')",
                (USER, stamp),
            )
            self.conn.execute(
                "INSERT INTO opportunity_resume_picks(user_id, opportunity_id, resume_file_id, picked_by, matched_json, created_at, updated_at) "
                "VALUES(?, 'job-a', ?, 'student', '{}', ?, ?)",
                (USER, self.software["file_id"], stamp, stamp),
            )
        # As if read before the student's pick committed (PostgreSQL takes no row lock on the role any more).
        with mock.patch.object(automation_handlers.ResumePick, "read", return_value={"resume_pick": None}):
            self.assertIsNone(resume_variants.pick_after_save(self.conn, USER, "job-a"))
        self.assertEqual(automation.list_actions(self.conn, USER, feature="resume_variant_pick"), [])
        self.assertEqual((self.pick()["resume_file_id"], self.pick()["picked_by"]), (self.software["file_id"], "student"))

    def test_the_pick_takes_no_row_lock_on_the_role(self):
        class Recorder:
            backend = "postgresql"

            def __init__(self):
                self.statements = []

            def execute(self, sql, _params=()):
                self.statements.append(" ".join(sql.split()))
                return mock.Mock(fetchone=lambda: (1,) if "FROM opportunities" in sql else None)

        recorder = Recorder()
        automation_handlers.ResumePick().read(recorder, USER, "job-a")
        on_role = [sql for sql in recorder.statements if "FROM opportunities" in sql]
        self.assertTrue(on_role)
        self.assertFalse(any("FOR UPDATE" in sql for sql in on_role), "a running sync holds every role's row until it commits")

    def test_the_pick_view_says_whether_the_role_is_saved_and_whether_automation_is_paused(self):
        self.on("resume_variant_pick")
        view = resume_variants.pick_view(self.conn, USER, "job-a")
        self.assertEqual((view["saved"], view["paused"], view["pick"]), (True, False, None), "saved before the switch: no pick")
        record_intent(self.conn, "job-a", "undo", user_id=USER)
        automation.set_paused(self.conn, USER, True)
        view = resume_variants.pick_view(self.conn, USER, "job-a")
        self.assertEqual((view["saved"], view["paused"], view["enabled"]), (False, True, True))

    def test_the_switch_needs_a_variant_with_a_confirmed_resume(self):
        setup = resume_variants.variant_setup(self.conn, USER)
        self.assertEqual((setup["usable"], setup["unlisted"]), (["Hardware", "Software"], []))
        self.set_profile(resume_variants=[], default_variant="")
        self.assertEqual(resume_variants.variant_setup(self.conn, USER)["unlisted"], ["Hardware", "Software"],
                         "labelled résumés the profile does not list are never picked")
        with self.assertRaisesRegex(automation.AutomationGateError, "resume_variants"):
            automation.set_mode(self.conn, USER, "resume_variant_pick", "on")
        self.set_profile(resume_variants=[{"label": "Firmware", "keywords": ["RTOS"]}])
        with self.assertRaisesRegex(automation.AutomationGateError, "Use as a variant"):
            automation.set_mode(self.conn, USER, "resume_variant_pick", "on")
        self.set_profile(resume_variants=VARIANTS)
        self.on("resume_variant_pick")
        with self.client() as client:
            listed = client.get("/api/v1/resumes", headers=AUTH).json()
        self.assertEqual(listed["variants"]["usable"], ["Hardware", "Software"])

    def test_a_failed_pick_never_fails_the_save(self):
        self.on("resume_variant_pick")
        record_intent(self.conn, "job-a", "undo", user_id=USER)
        with mock.patch.object(resume_variants, "pick_variant", side_effect=RuntimeError("boom")), \
                self.assertLogs("opportunity_app.student.resume_variants", level="ERROR"):
            response = record_intent(self.conn, "job-a", "saved", user_id=USER)
        self.assertFalse(response["unchanged"])
        self.assertEqual(self.intent("job-a"), "saved")


class VariantConfirmTests(Case):
    def test_confirming_a_variant_never_changes_profile_facts(self):
        facts_before = self.conn.execute("SELECT field_path, value_json FROM profile_facts ORDER BY field_path").fetchall()
        profile_before = self.conn.execute("SELECT profile_json FROM profiles WHERE user_id=?", (USER,)).fetchone()[0]
        draft = self.add_resume(status="draft", parsed={"profile_suggestions": {"name": "Someone Else", "skills": ["COBOL"]}})
        record = confirm_variant(self.conn, draft["version_id"], "  Hardware ", user_id=USER)
        self.assertEqual((record["status"], record["variant_label"], record["confirmed"]), ("confirmed", "Hardware", {}))
        self.assertEqual(self.conn.execute("SELECT field_path, value_json FROM profile_facts ORDER BY field_path").fetchall(), facts_before)
        self.assertEqual(self.conn.execute("SELECT profile_json FROM profiles WHERE user_id=?", (USER,)).fetchone()[0], profile_before)
        renamed = confirm_variant(self.conn, draft["version_id"], "Robot arms", user_id=USER)
        self.assertEqual((renamed["variant_label"], renamed["confirmed_at"]), ("Robot arms", record["confirmed_at"]))
        cleared = confirm_variant(self.conn, draft["version_id"], "", user_id=USER)
        self.assertEqual((cleared["status"], cleared["variant_label"]), ("confirmed", ""))

    def test_labels_are_unique_per_student(self):
        self.add_resume(label="Hardware")
        other = self.add_resume(status="draft")
        with self.assertRaisesRegex(ResumeValidationError, "already uses the label"):
            confirm_variant(self.conn, other["version_id"], "hardware ", user_id=USER)
        self.assertEqual(resume_record(self.conn, other["version_id"], user_id=USER)["status"], "draft", "nothing changed")

    def test_the_api_confirms_picks_and_checks(self):
        self.set_profile(resume_variants=VARIANTS, default_variant="Software")
        self.confirm_skills(["SolidWorks", "Python", "Kubernetes"])
        draft = self.add_resume(status="draft", text="My hardware resume mentions CAD only.")
        other = self.add_resume(label="Software", text="Python everywhere.")
        with self.client() as client:
            confirmed = client.post(f"/api/v1/resumes/{draft['version_id']}/variant", headers=AUTH, json={"variant_label": "Hardware"})
            self.assertEqual(confirmed.status_code, 200, confirmed.text)
            self.assertEqual(confirmed.json()["variant_label"], "Hardware")
            listed = client.get("/api/v1/resumes", headers=AUTH).json()["items"]
            self.assertEqual({item["variant_label"] for item in listed}, {"Hardware", "Software"})
            view = client.get("/api/v1/opportunities/job-a/resume-pick", headers=AUTH).json()
            self.assertEqual((view["configured"], view["suggestion"]["label"], view["pick"]), (True, "Hardware", None))
            check = client.get("/api/v1/opportunities/job-a/resume-check", headers=AUTH).json()
            self.assertEqual((check["label"], check["terms"]), ("Hardware", ["SolidWorks"]))
            changed = client.put("/api/v1/opportunities/job-a/resume-pick", headers=AUTH, json={"resume_file_id": other["file_id"]})
            self.assertEqual(changed.status_code, 200, changed.text)
            self.assertEqual((changed.json()["pick"]["label"], changed.json()["pick"]["picked_by"]), ("Software", "student"))
            detail = client.get("/api/v1/opportunities/job-a", headers=AUTH).json()
            self.assertEqual(detail["resume_pick"]["label"], "Software")
            page = client.get("/api/v1/opportunities", headers=AUTH).json()["items"]
            self.assertEqual({item["id"]: (item["resume_pick"] or {}).get("label") for item in page}, {"job-a": "Software", "job-b": None})
            refused = client.put("/api/v1/opportunities/job-a/resume-pick", headers=AUTH, json={"resume_file_id": "resume-file-nope"})
            self.assertEqual(refused.status_code, 422)
            missing = client.get("/api/v1/opportunities/nope/resume-pick", headers=AUTH)
            self.assertEqual(missing.status_code, 404)
            clash = client.post(f"/api/v1/resumes/{other['version_id']}/variant", headers=AUTH, json={"variant_label": "HARDWARE"})
            self.assertEqual(clash.status_code, 422)


class KeywordCheckTests(Case):
    def test_it_never_suggests_a_skill_the_student_has_not_confirmed(self):
        # SolidWorks is in the posting and in the profile, but only as an unconfirmed edit.
        self.set_profile(skills=["SolidWorks", "Python"])
        self.confirm_skills(["Python"])
        self.add_resume(label="", text="Nothing relevant here at all.")
        check = resume_variants.resume_check(self.conn, USER, "job-a")
        self.assertEqual(check["terms"], [], "SolidWorks is not confirmed, and Python is not in the posting")
        self.confirm_skills(["Python", "solidworks"])
        check = resume_variants.resume_check(self.conn, USER, "job-a")
        self.assertEqual(check["terms"], ["solidworks"], "whole word, any case, in the student's own spelling")

    def test_a_resume_that_mentions_the_skill_is_not_flagged(self):
        self.confirm_skills(["SolidWorks"])
        self.add_resume(text="Modelled parts in solidworks for a club.")
        self.assertEqual(resume_variants.resume_check(self.conn, USER, "job-a")["terms"], [])

    def test_with_no_resume_or_no_skills_it_says_so(self):
        self.assertEqual(resume_variants.resume_check(self.conn, USER, "job-a")["note"], "No confirmed résumé to check yet")
        self.add_resume()
        self.assertIn("no confirmed skills", resume_variants.resume_check(self.conn, USER, "job-a")["note"])


def mail_mode(conn, mode):
    """Set "Update applications from job emails" directly (its 48-hour shadow gate is automation's, tested there)."""
    with conn:
        conn.execute(
            "INSERT INTO user_settings(user_id, key, value, updated_at) VALUES(?, 'application_mail', ?, ?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value",
            (USER, mode, utc_now()),
        )


def job_email(conn, days_ago, *, application_id="app-job-b", gmail_id=None, kind="assessment", state="done", received=None):
    """A job email "Update applications from job emails" read and linked to the application, received ``days_ago``.

    The switch is turned on first, unless the test set it already: only while it is on does a job email count as a reply.
    Returns the email's Gmail id.
    """
    received = received or (datetime.now(timezone.utc) - timedelta(days=days_ago)).replace(microsecond=0).isoformat()
    gmail_id = gmail_id or f"m-{uuid4().hex}"
    with conn:
        conn.execute(
            "INSERT INTO user_settings(user_id, key, value, updated_at) VALUES(?, 'application_mail', 'on', ?) "
            "ON CONFLICT(user_id, key) DO NOTHING",
            (USER, utc_now()),
        )
        conn.execute(
            "INSERT INTO application_mail_messages(user_id, gmail_id, thread_id, application_id, kind, matched_by, state, origin, "
            "subject, sender_domain, received_at, recorded_at) VALUES(?, ?, 't', ?, ?, 'company_title', ?, 'live', 'An update', "
            "'hire.lever.co', ?, ?)",
            (USER, gmail_id, application_id, kind, state, received, utc_now()),
        )
    return gmail_id


def email_proposal(conn, gmail_id, after, *, auto=False):
    """A change "Update applications from job emails" made or proposed from that email, keyed as it keys them."""
    return automation.perform(
        conn, user_id=USER, feature="application_mail", action_type="application.stage", subject_kind="application",
        subject_id="app-job-b", after=after, evidence={"gmail_id": gmail_id}, summary="Orbit Systems: from an email",
        basis="test", confidence=0.9, idempotency_key=f"gmail:{gmail_id}:app-job-b:application.stage", auto=auto,
    )


# --- Application silence and archive --------------------------------------------------------


class SilenceTests(Case):
    def applied(self, days_ago):
        # Noon on the student's own calendar day, so the day count cannot slip across midnight.
        zone = user_timezone(self.conn, USER)
        day = zone.today() - timedelta(days=days_ago)
        applied_at = zone.localize(datetime(day.year, day.month, day.day, 12)).isoformat(timespec="microseconds")
        with self.conn:
            self.conn.execute("UPDATE applications SET stage='applied', applied_at=?, follow_up_at=NULL WHERE id='app-job-b'", (applied_at,))
        return applied_at

    def silence(self):
        return [item for item in urgent_queue(self.conn, user_id=USER)["items"] if item["kind"] == "application_silence"]

    def test_the_row_appears_on_the_boundary_day_and_not_before(self):
        self.on("application_silence")
        self.applied(20)
        self.assertEqual(self.silence(), [], "day 20 of 21")
        self.applied(21)
        [row] = self.silence()
        self.assertEqual((row["date_source"], row["days_until"], row["application_id"]), ("No reply yet", 0, "app-job-b"))
        self.assertIn("21 days", row["subtitle"])
        self.set_profile(application_follow_up_days=10)
        self.applied(12)
        [row] = self.silence()
        self.assertEqual((row["days_until"], row["overdue"]), (-2, True))

    def test_no_row_once_the_stage_moved_on_or_with_the_switch_off(self):
        self.applied(30)
        self.assertEqual(self.silence(), [], "off by default")
        self.on("application_silence")
        self.assertEqual(len(self.silence()), 1)
        automation.set_paused(self.conn, USER, True)
        self.assertEqual(len(self.silence()), 1, "a row only informs, so the pause leaves it")
        automation.set_paused(self.conn, USER, False)
        update_application(self.conn, "app-job-b", stage="interview", user_id=USER)
        self.assertEqual(self.silence(), [])

    def test_a_job_email_is_a_reply_so_silence_counts_from_the_latest_one(self):
        self.on("application_silence")
        self.applied(30)
        job_email(self.conn, 5)
        self.assertEqual(self.silence(), [], "the company wrote 5 days ago: not silent")
        job_email(self.conn, 40, gmail_id="m-before")  # before the student applied: silence still counts from applying
        self.assertEqual(self.silence(), [])
        with self.conn:
            self.conn.execute("DELETE FROM application_mail_messages WHERE gmail_id<>'m-before'")
        [row] = self.silence()
        self.assertEqual((row["days_until"], row["subtitle"]), (-9, "No reply 21 days after you applied"))
        job_email(self.conn, 25)
        [row] = self.silence()
        self.assertEqual((row["days_until"], row["subtitle"]), (-4, "No reply 21 days after their last email"))
        job_email(self.conn, 1, application_id="app-other", gmail_id="m-other")
        self.assertEqual(len(self.silence()), 1, "an email about another application is not a reply to this one")
        job_email(self.conn, 1, state="skipped", gmail_id="m-skipped")
        self.assertEqual(len(self.silence()), 1, "only emails linked and kept, as the application's Emails list shows them")

    def test_only_news_from_the_employer_counts_and_only_while_job_emails_are_on(self):
        self.on("application_silence")
        self.applied(30)
        self.assertEqual(len(self.silence()), 1)
        job_email(self.conn, 5, kind="unknown", gmail_id="m-alert")
        [row] = self.silence()
        self.assertEqual(row["subtitle"], "No reply 21 days after you applied", "a job alert or an unclassified receipt is not a reply")
        job_email(self.conn, 5, gmail_id="m-assessment")
        self.assertEqual(self.silence(), [], "an assessment is")
        mail_mode(self.conn, "shadow")
        self.assertEqual(len(self.silence()), 1, "in shadow it only logs what it would do, and changes nothing another switch shows")
        mail_mode(self.conn, "off")
        self.assertEqual(len(self.silence()), 1)
        mail_mode(self.conn, "on")
        self.assertEqual(self.silence(), [])
        proposal = email_proposal(self.conn, "m-assessment", {"stage": "interview"})
        self.assertEqual(self.silence(), [], "waiting for the student, it still came")
        automation.reject(self.conn, proposal["id"], USER)
        [row] = self.silence()
        self.assertEqual(row["subtitle"], "No reply 21 days after you applied", "the student turned down everything it proposed")
        job_email(self.conn, 4, gmail_id="m-ignored")
        ignored = email_proposal(self.conn, "m-ignored", {"stage": "interview"})
        with self.conn:  # what ignoring the email card does to its proposals (application_inbox._expire)
            self.conn.execute("UPDATE automation_actions SET status='expired' WHERE id=?", (ignored["id"],))
        self.assertEqual(len(self.silence()), 1, "nor an email the student ignored")

    def test_a_bad_setting_falls_back_to_21(self):
        self.set_profile(application_follow_up_days="soon")
        self.assertEqual(internal_automation.follow_up_days(self.conn, USER), 21)
        self.set_profile(application_follow_up_days=True)
        self.assertEqual(internal_automation.follow_up_days(self.conn, USER), 21)


class ArchiveTests(Case):
    def applied(self, days_ago):
        # Noon on the student's own calendar day, so a DST change inside the gap cannot shift the day count.
        zone = user_timezone(self.conn, USER)
        day = zone.today() - timedelta(days=days_ago)
        applied_at = zone.localize(datetime(day.year, day.month, day.day, 12)).isoformat(timespec="microseconds")
        with self.conn:
            self.conn.execute("UPDATE applications SET stage='applied', applied_at=? WHERE id='app-job-b'", (applied_at,))
        return applied_at

    def stage(self):
        return self.conn.execute("SELECT stage FROM applications WHERE id='app-job-b'").fetchone()[0]

    def test_a_silent_application_is_archived_and_undo_brings_it_back(self):
        self.applied(59)
        self.on("archive_silent_applications")
        self.assertEqual(internal_automation.archive_silent_applications(self.conn, USER), [])
        self.applied(61)
        self.assertEqual(internal_automation.archive_silent_applications(self.conn, USER), [], "once a day per student")
        [done] = internal_automation.archive_silent_applications(self.conn, USER, force=True)
        self.assertEqual(self.stage(), "archived")
        self.assertTrue(internal_automation.automation_archived(self.conn, "app-job-b"))
        automation.undo(self.conn, done["action_id"], USER)
        self.assertEqual(self.stage(), "applied")
        self.assertFalse(internal_automation.automation_archived(self.conn, "app-job-b"), "undone, so no longer the switch's archive")
        self.assertEqual(internal_automation.archive_silent_applications(self.conn, USER, force=True), [], "an undo sticks")

    def test_a_stage_the_student_moved_on_is_never_archived(self):
        self.applied(61)
        self.on("archive_silent_applications")
        real = internal_automation.archive_due

        def moved_on_meanwhile(*args, **kwargs):
            due = real(*args, **kwargs)
            update_application(self.conn, "app-job-b", stage="interview", user_id=USER)
            return due

        with mock.patch.object(internal_automation, "archive_due", moved_on_meanwhile):
            self.assertEqual(internal_automation.archive_silent_applications(self.conn, USER, force=True), [])
        self.assertEqual(self.stage(), "interview")
        self.assertEqual(automation.list_actions(self.conn, USER, feature="archive_silent_applications"), [])

    def test_an_archive_the_daily_sync_moved_back_no_longer_counts(self):
        self.applied(61)
        self.on("archive_silent_applications")
        internal_automation.archive_silent_applications(self.conn, USER, force=True)
        self.assertTrue(internal_automation.automation_archived(self.conn, "app-job-b"))
        # The sync resets an imported application's stage with no stage_changed event (legacy_sync._migrate_status).
        with self.conn:
            self.conn.execute("UPDATE applications SET stage='applied' WHERE id='app-job-b'")
        self.assertFalse(internal_automation.automation_archived(self.conn, "app-job-b"), "it sits at Applied, not archived")

    def test_the_worker_runs_the_daily_archive(self):
        self.applied(61)
        self.on("archive_silent_applications")
        worker = AutomationWorker(self.platform_path, fetcher_factory=lambda: None)
        report = worker.run_once()
        self.assertEqual([item["application_id"] for item in report["archived"]], ["app-job-b"])
        self.assertEqual(self.stage(), "archived")

    def test_an_application_with_a_job_email_waiting_for_the_student_is_not_archived(self):
        self.applied(61)
        self.on("archive_silent_applications")
        with self.conn:  # application_mail runs in shadow before on; set directly, as its own tests do
            self.conn.execute(
                "INSERT INTO user_settings(user_id, key, value, updated_at) VALUES(?, 'application_mail', 'on', ?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET value='on'",
                (USER, utc_now()),
            )
        proposal = automation.perform(
            self.conn, user_id=USER, feature="application_mail", action_type="application.stage", subject_kind="application",
            subject_id="app-job-b", after={"stage": "interview"}, evidence={"gmail_id": "m-1"}, summary="Orbit Systems: move to interview",
            basis="test", confidence=0.9, idempotency_key="gmail:m-1:app-job-b:application.stage", auto=False,
        )
        self.assertEqual(internal_automation.archive_silent_applications(self.conn, USER, force=True), [],
                         "an email came, so it was not silent, and archiving would leave the approval nothing to apply to")
        self.assertEqual(self.stage(), "applied")
        self.assertEqual(automation.list_actions(self.conn, USER, feature="archive_silent_applications"), [])
        approved = automation.approve(self.conn, proposal["id"], USER)
        self.assertEqual((approved["status"], self.stage()), ("applied", "interview"))

    def test_once_the_waiting_email_is_turned_down_the_archive_goes_ahead(self):
        self.applied(61)
        self.on("archive_silent_applications")
        with self.conn:
            self.conn.execute(
                "INSERT INTO user_settings(user_id, key, value, updated_at) VALUES(?, 'application_mail', 'on', ?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET value='on'",
                (USER, utc_now()),
            )
        proposal = automation.perform(
            self.conn, user_id=USER, feature="application_mail", action_type="application.stage", subject_kind="application",
            subject_id="app-job-b", after={"stage": "interview"}, evidence={"gmail_id": "m-2"}, summary="Orbit Systems: move to interview",
            basis="test", confidence=0.9, idempotency_key="gmail:m-2:app-job-b:application.stage", auto=False,
        )
        self.assertEqual(internal_automation.archive_silent_applications(self.conn, USER, force=True), [])
        automation.reject(self.conn, proposal["id"], USER)
        [done] = internal_automation.archive_silent_applications(self.conn, USER, force=True)
        self.assertEqual((done["application_id"], self.stage()), ("app-job-b", "archived"))

    def test_a_job_email_since_applying_restarts_the_archive_clock(self):
        self.applied(90)
        self.on("archive_silent_applications")
        # An assessment 30 days ago left a task open at Applied: archiving now would hide it.
        job_email(self.conn, 30)
        self.assertEqual(internal_automation.archive_silent_applications(self.conn, USER, force=True), [])
        self.assertEqual(self.stage(), "applied")
        job_email(self.conn, 61, gmail_id="m-older")
        with self.conn:
            self.conn.execute("DELETE FROM application_mail_messages WHERE gmail_id<>'m-older'")
        [done] = internal_automation.archive_silent_applications(self.conn, USER, force=True)
        [action] = automation.list_actions(self.conn, USER, feature="archive_silent_applications")
        self.assertEqual(action["id"], done["action_id"])
        self.assertIn("no reply 61 days after their last email", action["summary"])
        self.assertEqual(action["evidence"]["days"], 61)
        self.assertTrue(action["evidence"]["last_email_on"])

    def test_an_undo_sticks_when_a_job_email_moves_the_applied_date_earlier(self):
        applied_at = self.applied(61)
        self.on("archive_silent_applications")
        [done] = internal_automation.archive_silent_applications(self.conn, USER, force=True)
        automation.undo(self.conn, done["action_id"], USER)
        self.assertEqual(internal_automation.archive_silent_applications(self.conn, USER, force=True), [])
        # A confirmation read late, received an hour before the applied date the student recorded, moves that date earlier.
        earlier = (datetime.fromisoformat(applied_at) - timedelta(hours=1)).astimezone(timezone.utc).isoformat(timespec="seconds")
        gmail_id = job_email(self.conn, 0, kind="application_confirmation", received=earlier)
        moved = email_proposal(self.conn, gmail_id, {"stage": "applied", "applied_at": earlier}, auto=True)
        self.assertEqual(moved["status"], "applied")
        stored = self.conn.execute("SELECT applied_at FROM applications WHERE id='app-job-b'").fetchone()[0]
        self.assertLess(datetime.fromisoformat(stored), datetime.fromisoformat(applied_at))
        self.assertEqual(internal_automation.archive_silent_applications(self.conn, USER, force=True), [], "the undo still stands")
        self.assertEqual(self.stage(), "applied")
        self.assertEqual(len(automation.list_actions(self.conn, USER, feature="archive_silent_applications")), 1)

    def test_moving_it_back_by_hand_sticks_and_a_pass_reports_only_what_it_archived(self):
        self.applied(61)
        self.on("archive_silent_applications")
        [done] = internal_automation.archive_silent_applications(self.conn, USER, force=True)
        update_application(self.conn, "app-job-b", stage="applied", user_id=USER)
        self.assertEqual(internal_automation.archive_silent_applications(self.conn, USER, force=True), [],
                         "not archived again, and the earlier archive is not reported as a new one")
        self.assertEqual(self.stage(), "applied")
        [action] = automation.list_actions(self.conn, USER, feature="archive_silent_applications")
        self.assertEqual(action["id"], done["action_id"])

    def test_a_new_applied_date_after_an_undo_starts_a_new_silence(self):
        self.applied(90)
        self.on("archive_silent_applications")
        [done] = internal_automation.archive_silent_applications(self.conn, USER, force=True)
        automation.undo(self.conn, done["action_id"], USER)
        later = datetime.now(timezone.utc) + timedelta(days=61)
        self.assertEqual(internal_automation.archive_silent_applications(self.conn, USER, now=later, force=True), [],
                         "an undo sticks however long it stays silent")
        # The student recorded a new applied date after the undo, and it went silent for 61 days from then.
        with self.conn:
            self.conn.execute("UPDATE applications SET applied_at=? WHERE id='app-job-b'", (utc_now(),))
        [again] = internal_automation.archive_silent_applications(self.conn, USER, now=later, force=True)
        self.assertNotEqual(again["action_id"], done["action_id"])
        self.assertEqual(self.stage(), "archived")

    def test_a_job_email_decided_after_the_list_was_made_stops_the_archive(self):
        self.applied(61)
        self.on("archive_silent_applications")
        mail_mode(self.conn, "on")
        real = internal_automation.archive_due

        def email_meanwhile(*args, **kwargs):
            due = real(*args, **kwargs)
            job_email(self.conn, 0)  # decided between the list and the archive's transaction
            return due

        with mock.patch.object(internal_automation, "archive_due", email_meanwhile):
            self.assertEqual(internal_automation.archive_silent_applications(self.conn, USER, force=True), [])
        self.assertEqual(self.stage(), "applied")
        self.assertEqual(automation.list_actions(self.conn, USER, feature="archive_silent_applications"), [])

    def test_a_student_archive_is_not_the_automations(self):
        update_application(self.conn, "app-job-b", stage="archived", user_id=USER)
        self.assertFalse(internal_automation.automation_archived(self.conn, "app-job-b"))

    def test_the_daily_run_time_is_kept_per_student(self):
        self.on("archive_silent_applications")
        internal_automation.archive_silent_applications(self.conn, USER)
        stored = self.conn.execute(
            "SELECT value FROM user_settings WHERE user_id=? AND key=?", (USER, internal_automation.ARCHIVE_LAST_RUN_KEY),
        ).fetchone()
        self.assertIsNotNone(stored)
        tomorrow = datetime.now(timezone.utc) + timedelta(days=1, minutes=1)
        self.applied(70)
        self.assertEqual(len(internal_automation.archive_silent_applications(self.conn, USER, now=tomorrow)), 1)


# --- Outreach: auto-close and follow-up drafts ----------------------------------------------


class OutreachCase(Case):
    def target(self, **values):
        return create_target(self.conn, {
            "company": "Bovi", "website": "https://bovi.test", "location": "Austin, TX", "contact_email": "info@bovi.test",
            "email_subject": "Internship question", "email_body": "Hi Bovi team,\n\nA short note.\n\nSam", **values,
        }, user_id=USER)

    def possible(self, owner, *others, gmail_id=None, text="Thanks for writing. Could you send a few times for a call?"):
        """An email reply capture kept as a possible reply: filed under ``owner``, and ``others`` could have sent it too.

        Written the way outreach_inbox writes one (the row, its history events, its notice).
        """
        gmail_id = gmail_id or f"possible-{uuid4().hex[:8]}"
        kept = outreach_inbox._record_possible(
            self.conn, [owner, *others], user_id=USER, gmail_id=gmail_id, sender="careers@bovi.test", received=utc_now(),
            via="domain", reason="ambiguous" if others else "shared_address", thread_id="", subject="Re: Internship question",
            text=text, message_id=f"<{gmail_id}@bovi.test>", from_name="Careers",
        )
        self.assertIsNotNone(kept, "the possible reply was recorded")
        return gmail_id

    def settle(self, target, gmail_id, decision="not_reply"):
        """The student answers a possible reply on ``target``'s card."""
        return outreach_inbox.decide_possible_reply(self.conn, target["id"], gmail_id, decision, user_id=USER)

    def company(self, name, **values):
        slug = name.lower().replace(" ", "")
        return {"company": name, "website": f"https://{slug}.test", "contact_email": f"hi@{slug}.test", **values}


class AutoCloseTests(OutreachCase):
    def setUp(self):
        super().setUp()
        self.connect_gmail()

    def connect_gmail(self, status="connected"):
        with self.conn:
            self.conn.execute(
                "INSERT INTO connector_accounts(id, user_id, provider, scopes_json, status, created_at, updated_at) "
                "VALUES(?, ?, 'gmail_drafts', '[]', ?, ?, ?) ON CONFLICT(id) DO UPDATE SET status=excluded.status",
                (f"connector-gmail_drafts-{USER}", USER, status, utc_now(), utc_now()),
            )

    def status(self, target):
        return get_target(self.conn, target["id"], user_id=USER)["status"]

    def quiet(self, days=20, **values):
        follow_up = (date.today() - timedelta(days=days)).isoformat()
        return self.target(status="followed_up", sent_at=(date.today() - timedelta(days=days + 7)).isoformat(),
                           follow_up_at=follow_up, **values)

    def test_it_holds_when_the_fresh_look_fails(self):
        target = self.quiet()
        self.on("outreach_auto_close")
        with mock.patch("opportunity_app.outreach_review.fresh_look", return_value={"ok": False, "reason": "Gmail could not be reached"}):
            [result] = internal_automation.auto_close(self.conn, USER, client_factory=lambda: None)
        self.assertEqual((result["closed"], result["reason"]), (False, "Gmail could not be reached"))
        self.assertEqual(get_target(self.conn, target["id"], user_id=USER)["status"], "followed_up")
        [result] = internal_automation.auto_close(self.conn, USER, client_factory=None)
        self.assertFalse(result["closed"], "no Gmail at all fails closed too")

    def test_it_closes_after_the_fresh_look_and_undo_restores_the_status_and_date(self):
        target = self.quiet()
        self.on("outreach_auto_close")
        with mock.patch("opportunity_app.outreach_review.fresh_look", return_value={"ok": True, "reason": ""}):
            [result] = internal_automation.auto_close(self.conn, USER, client_factory=lambda: None)
        self.assertTrue(result["closed"])
        closed = get_target(self.conn, target["id"], user_id=USER, include_events=True)
        self.assertEqual((closed["status"], closed["follow_up_at"]), ("no_response", None))
        self.assertEqual(closed["events"][0]["detail"], "Changed automatically")
        [action] = automation.list_actions(self.conn, USER, feature="outreach_auto_close")
        self.assertEqual((action["basis"], action["evidence"]["days_since"]), ("lifecycle:no_reply_14d", 20))
        automation.undo(self.conn, action["id"], USER)
        back = get_target(self.conn, target["id"], user_id=USER)
        self.assertEqual((back["status"], back["follow_up_at"]), ("followed_up", target["follow_up_at"]))

    def test_undo_refuses_once_the_status_moved(self):
        target = self.quiet()
        self.on("outreach_auto_close")
        with mock.patch("opportunity_app.outreach_review.fresh_look", return_value={"ok": True, "reason": ""}):
            internal_automation.auto_close(self.conn, USER, client_factory=lambda: None)
        [action] = automation.list_actions(self.conn, USER, feature="outreach_auto_close")
        update_target(self.conn, target["id"], {"status": "replied"}, user_id=USER)
        with self.assertRaisesRegex(Superseded, "status changed"):
            automation.undo(self.conn, action["id"], USER)
        self.assertEqual(get_target(self.conn, target["id"], user_id=USER)["status"], "replied")

    def test_a_status_that_moved_on_inside_the_write_is_left_as_it_is(self):
        target = self.quiet()
        self.on("outreach_auto_close")
        update_target(self.conn, target["id"], {"status": "replied"}, user_id=USER)
        row = automation.perform(
            self.conn, user_id=USER, feature="outreach_auto_close", action_type="outreach.status", subject_kind="outreach_target",
            subject_id=target["id"], after={"status": "no_response", "only_from": "followed_up"}, evidence={}, summary="x",
            basis="test", confidence=None, idempotency_key="guard:1", auto=True,
        )
        self.assertIsNone(row, "a reply recorded after the check stands")
        self.assertEqual(get_target(self.conn, target["id"], user_id=USER)["status"], "replied")

    def test_a_company_with_a_reply_or_not_yet_due_is_left_alone(self):
        self.quiet(days=10)
        replied = self.quiet(company="Kiva", website="https://kiva.test", contact_email="hi@kiva.test")
        log_reply(self.conn, replied["id"], "Thanks, we will get back to you.", user_id=USER)
        self.assertEqual(internal_automation.auto_close_due(self.conn, USER), [])

    def test_an_undone_close_is_not_tried_again_and_frees_its_slot(self):
        quiet = [self.quiet(company=f"Quiet {index}", website=f"https://q{index}.test", contact_email=f"a@q{index}.test")
                 for index in range(6)]
        self.on("outreach_auto_close")
        with mock.patch("opportunity_app.outreach_review.fresh_look", return_value={"ok": True, "reason": ""}):
            first = internal_automation.auto_close(self.conn, USER, client_factory=lambda: None)
        self.assertEqual(sum(1 for result in first if result["closed"]), 5)
        for action in automation.list_actions(self.conn, USER, feature="outreach_auto_close"):
            automation.undo(self.conn, action["id"], USER)
        self.on("outreach_auto_close")  # the breaker turned it off; the student turns it back on
        with mock.patch("opportunity_app.outreach_review.fresh_look", return_value={"ok": True, "reason": ""}) as look:
            second = internal_automation.auto_close(self.conn, USER, client_factory=lambda: None)
            self.assertEqual(look.call_count, 1, "Gmail is read only for the company never tried")
            self.assertEqual([(result["company"], result["closed"]) for result in second], [("Quiet 5", True)])
            look.reset_mock()
            self.assertEqual(internal_automation.auto_close(self.conn, USER, client_factory=lambda: None), [])
            look.assert_not_called()
        self.assertEqual([self.status(target) for target in quiet], ["followed_up"] * 5 + ["no_response"])

    def test_a_company_gmail_never_searches_is_left_for_the_student(self):
        # A LinkedIn message has no address to search for, and an imported company has no record of when it was written to.
        linkedin = self.quiet(company="Kiva", website="https://kiva.test", contact_email="", channel="LinkedIn")
        imported = self.target(company="Mako", website="https://mako.test", contact_email="x@mako.test", status="followed_up",
                               follow_up_at=(date.today() - timedelta(days=20)).isoformat())
        self.assertEqual({item["id"] for item in internal_automation.auto_close_due(self.conn, USER)}, {linkedin["id"], imported["id"]})
        self.on("outreach_auto_close")

        def no_gmail():
            raise AssertionError("nothing to search, so Gmail is not opened")

        for connector in ("connected", None):
            with self.subTest(connector=connector):
                if connector is None:
                    with self.conn:
                        self.conn.execute("DELETE FROM connector_accounts WHERE user_id=?", (USER,))
                results = internal_automation.auto_close(self.conn, USER, client_factory=no_gmail)
                self.assertEqual({(result["target_id"], result["closed"]) for result in results},
                                 {(linkedin["id"], False), (imported["id"], False)})
                self.assertTrue(all("never searched" in result["reason"] for result in results))
                self.assertEqual((self.status(linkedin), self.status(imported)), ("followed_up", "followed_up"))
                self.assertEqual(automation.list_actions(self.conn, USER, feature="outreach_auto_close"), [])

    def test_it_holds_without_reading_when_gmail_is_not_connected(self):
        target = self.quiet()
        self.on("outreach_auto_close")
        for status, reason in (("error", "Gmail needs to be reconnected"), ("disconnected", "Gmail is not connected")):
            with self.subTest(status=status):
                self.connect_gmail(status)
                with mock.patch("opportunity_app.outreach_review.fresh_look") as look:
                    [result] = internal_automation.auto_close(self.conn, USER, client_factory=lambda: None)
                look.assert_not_called()
                self.assertEqual((result["closed"], result["reason"]), (False, reason))
                self.assertEqual(self.status(target), "followed_up")

    def test_a_close_records_that_gmail_was_searched_for_the_company(self):
        target = self.quiet()
        self.on("outreach_auto_close")
        with mock.patch("opportunity_app.outreach_review.fresh_look", return_value={"ok": True, "reason": ""}) as look:
            internal_automation.auto_close(self.conn, USER, client_factory=lambda: None)
        self.assertEqual(look.call_args.args[1], target["id"])
        [action] = automation.list_actions(self.conn, USER, feature="outreach_auto_close")
        self.assertTrue(action["evidence"]["gmail_checked"])

    def test_a_reply_the_fresh_look_finds_is_handled_as_the_inbox_watcher_would(self):
        self.quiet()
        self.on("outreach_auto_close")
        classifier, hook = object(), mock.Mock()
        report = {}
        with mock.patch("opportunity_app.outreach_review.fresh_look", return_value={"ok": True, "reason": ""}) as look:
            internal_automation.run_for_user(
                self.conn, USER, report, gmail_client_factory=lambda: None, provider_factory=None,
                decisions_for=lambda _conn, _user: classifier, on_reply=hook,
            )
        self.assertIs(look.call_args.kwargs["decisions"], classifier)
        self.assertIs(look.call_args.kwargs["on_reply"], hook)
        self.assertTrue(report["closed"][0]["closed"])

    def test_the_worker_runs_auto_close(self):
        target = self.quiet()
        self.on("outreach_auto_close")
        worker = AutomationWorker(self.platform_path, fetcher_factory=lambda: None, gmail_client_factory=lambda: None)
        with mock.patch("opportunity_app.outreach_review.fresh_look", return_value={"ok": True, "reason": ""}):
            report = worker.run_once()
        self.assertEqual([(item["target_id"], item["closed"]) for item in report["closed"]], [(target["id"], True)])
        self.assertEqual(self.status(target), "no_response")

    def test_at_most_five_per_pass_and_nothing_while_paused(self):
        for index in range(7):
            self.quiet(company=f"Quiet {index}", website=f"https://q{index}.test", contact_email=f"a@q{index}.test")
        self.on("outreach_auto_close")
        automation.set_paused(self.conn, USER, True)
        with mock.patch("opportunity_app.outreach_review.fresh_look", return_value={"ok": True, "reason": ""}) as look:
            self.assertEqual(internal_automation.auto_close(self.conn, USER, client_factory=lambda: None), [])
            look.assert_not_called()
            automation.set_paused(self.conn, USER, False)
            results = internal_automation.auto_close(self.conn, USER, client_factory=lambda: None)
        self.assertEqual(sum(1 for result in results if result["closed"]), 5)

    # --- An email that may be a reply holds the close until the student says ---

    def test_a_possible_reply_waiting_holds_the_company_even_when_it_is_only_a_candidate(self):
        held = self.quiet()
        owner = self.quiet(**self.company("Kiva"))
        candidate = self.quiet(**self.company("Mako"))
        free = self.quiet(**self.company("Tarn"))
        self.possible(held)
        # Filed under Kiva; Mako could have sent it too (candidates_json), so it holds Mako as well.
        self.possible(owner, candidate)
        self.assertEqual([item["id"] for item in internal_automation.auto_close_due(self.conn, USER)], [free["id"]])
        self.on("outreach_auto_close")
        with mock.patch("opportunity_app.outreach_review.fresh_look", return_value={"ok": True, "reason": ""}) as look:
            results = internal_automation.auto_close(self.conn, USER, client_factory=lambda: None)
        self.assertEqual([(result["target_id"], result["closed"]) for result in results], [(free["id"], True)])
        self.assertEqual([call.args[1] for call in look.call_args_list], [free["id"]], "Gmail is not read for a held company")
        self.assertEqual([self.status(target) for target in (held, owner, candidate, free)],
                         ["followed_up", "followed_up", "followed_up", "no_response"])
        self.assertEqual(len(automation.list_actions(self.conn, USER, feature="outreach_auto_close")), 1)

    def test_only_a_possible_reply_holds_not_an_email_set_aside(self):
        target = self.quiet()
        now = utc_now()
        with self.conn:
            for kind in ("dismissed", "automatic", "ignored"):
                self.conn.execute(
                    "INSERT INTO outreach_inbox_messages(user_id, gmail_id, target_id, kind, sender, received_at, recorded_at, "
                    "via, rules, reason, candidates_json) VALUES(?, ?, ?, ?, 'careers@bovi.test', ?, ?, 'domain', 2, 'x', '[]')",
                    (USER, f"{kind}-1", target["id"], kind, now, now),
                )
        self.assertEqual([item["id"] for item in internal_automation.auto_close_due(self.conn, USER)], [target["id"]])

    def test_once_the_student_settles_a_possible_reply_the_hold_lifts_for_every_candidate(self):
        owner = self.quiet(**self.company("Kiva"))
        candidate = self.quiet(**self.company("Mako"))
        dismissed = self.possible(owner, candidate)
        self.assertEqual(internal_automation.auto_close_due(self.conn, USER), [], "both held while it waits")
        self.settle(candidate, dismissed, "not_reply")  # said on the candidate's card
        self.assertEqual({item["id"] for item in internal_automation.auto_close_due(self.conn, USER)}, {owner["id"], candidate["id"]})

        other_owner = self.quiet(**self.company("Orla"))
        other_candidate = self.quiet(**self.company("Pell"))
        confirmed = self.possible(other_owner, other_candidate)
        released = {item["id"] for item in internal_automation.auto_close_due(self.conn, USER)} & {other_owner["id"], other_candidate["id"]}
        self.assertEqual(released, set(), "both held while it waits")
        self.settle(other_candidate, confirmed, "reply")  # Pell's reply: Orla is free, Pell has heard back
        due = {item["id"] for item in internal_automation.auto_close_due(self.conn, USER)}
        self.assertIn(other_owner["id"], due)
        self.assertNotIn(other_candidate["id"], due)
        self.assertEqual(get_target(self.conn, other_candidate["id"], user_id=USER)["reply_count"], 1)

    def test_a_possible_reply_the_fresh_look_finds_holds_the_close(self):
        owner = self.quiet()
        candidate = self.quiet(**self.company("Kiva"))
        self.on("outreach_auto_close")

        def finds_one(conn, target_id, **_kwargs):
            # Found for the first company looked at, and Kiva could have sent it too.
            if not conn.execute("SELECT 1 FROM outreach_inbox_messages WHERE kind='possible'").fetchone():
                self.possible(get_target(conn, target_id, user_id=USER),
                              *[get_target(conn, other, user_id=USER) for other in (owner["id"], candidate["id"]) if other != target_id])
            return {"ok": True, "reason": ""}

        with mock.patch("opportunity_app.outreach_review.fresh_look", side_effect=finds_one):
            results = internal_automation.auto_close(self.conn, USER, client_factory=lambda: None)
        self.assertEqual({(result["target_id"], result["closed"], result["reason"]) for result in results},
                         {(owner["id"], False, "It is no longer waiting on a reply"),
                          (candidate["id"], False, "It is no longer waiting on a reply")})
        self.assertEqual((self.status(owner), self.status(candidate)), ("followed_up", "followed_up"))
        self.assertEqual(automation.list_actions(self.conn, USER, feature="outreach_auto_close"), [])

    def test_the_close_checks_again_inside_its_write(self):
        # Found after the re-read and before the write (another tab, the background check): the guard stops it.
        arrivals = {
            "possible reply": lambda target, other: self.possible(target),
            "possible reply filed under another company": lambda target, other: self.possible(other, target),
            "pasted reply": lambda target, other: log_reply(self.conn, target["id"], "Thanks, we will get back to you.", user_id=USER),
        }
        self.on("outreach_auto_close")
        original = automation.perform
        for index, (name, arrive) in enumerate(arrivals.items()):
            with self.subTest(name):
                target = self.quiet(**self.company(f"Quiet {index}"))
                other = self.target(**self.company(f"Other {index}"), status="sent",
                                    sent_at=(date.today() - timedelta(days=3)).isoformat())

                def arrives_first(*args, target=target, other=other, arrive=arrive, **kwargs):
                    arrive(target, other)
                    return original(*args, **kwargs)

                with mock.patch("opportunity_app.outreach_review.fresh_look", return_value={"ok": True, "reason": ""}), \
                        mock.patch.object(automation, "perform", side_effect=arrives_first) as perform:
                    [result] = internal_automation.auto_close(self.conn, USER, client_factory=lambda: None)
                self.assertEqual(perform.call_count, 1, "the re-read saw nothing, so the write was reached")
                self.assertEqual((result["target_id"], result["closed"], result["reason"]), (target["id"], False, "Nothing was changed"))
                self.assertEqual(self.status(target), "followed_up")
                self.assertEqual(automation.list_actions(self.conn, USER, feature="outreach_auto_close"), [])

    def test_no_no_response_suggestion_while_a_possible_reply_waits(self):
        owner = self.quiet()
        candidate = self.quiet(**self.company("Kiva"))
        today = date.today()

        def suggested():
            listed = {item["id"]: item for item in list_targets(self.conn, user_id=USER, today=today)}
            seen = []
            for target in (owner, candidate):
                card = get_target(self.conn, target["id"], user_id=USER, today=today)
                seen.append(tuple((suggestion or {}).get("status") for suggestion in (
                    lifecycle_suggestion(card, today), card["suggestion"], lifecycle_suggestion(listed[target["id"]], today),
                    listed[target["id"]]["suggestion"],
                )))
            return seen

        self.assertEqual(suggested(), [("no_response",) * 4] * 2)
        gmail_id = self.possible(owner, candidate)
        self.assertEqual(suggested(), [(None,) * 4] * 2, "neither the company it is filed under nor the other candidate")
        self.settle(owner, gmail_id, "not_reply")
        self.assertEqual(suggested(), [("no_response",) * 4] * 2)

    def test_deleting_the_company_a_possible_reply_is_filed_under_keeps_holding_the_other_candidate(self):
        # The email could still be from Kiva: deleting Bovi must not settle it for Kiva without asking.
        owner = self.quiet()
        candidate = self.quiet(**self.company("Kiva"))
        self.possible(owner, candidate)
        self.assertTrue(delete_target(self.conn, owner["id"], user_id=USER))
        card = get_target(self.conn, candidate["id"], user_id=USER)
        self.assertEqual(card["possible_reply_count"], 1, "the email is still asked about on Kiva's card")
        self.assertEqual(internal_automation.auto_close_due(self.conn, USER), [], "and it still holds Kiva")

    def test_pasting_a_possible_reply_on_another_candidates_card_settles_it_for_every_candidate(self):
        text = "Thanks for writing. Could you send a few times for a call?"
        owner = self.quiet()
        candidate = self.quiet(**self.company("Kiva"))
        gmail_id = self.possible(owner, candidate, text=text)
        log_reply(self.conn, candidate["id"], text, user_id=USER)  # Kiva's reply, pasted on Kiva's card
        self.assertEqual(get_target(self.conn, candidate["id"], user_id=USER)["possible_replies"], [], "never asked about again")
        row = self.conn.execute("SELECT kind, target_id FROM outreach_inbox_messages WHERE gmail_id=?", (gmail_id,)).fetchone()
        self.assertEqual((row[0], row[1]), ("reply", candidate["id"]), "settled as Kiva's reply")
        self.assertEqual([item["id"] for item in internal_automation.auto_close_due(self.conn, USER)], [owner["id"]],
                         "Bovi no longer waits on an email that was Kiva's")


class FollowUpDraftTests(OutreachCase):
    def due(self, **values):
        return self.target(status="sent", sent_at=(date.today() - timedelta(days=8)).isoformat(),
                           follow_up_at=(date.today() - timedelta(days=1)).isoformat(), **values)

    def write(self, target, provider_factory=legacy, provider="legacy"):
        fresh = get_target(self.conn, target["id"], user_id=USER)
        return internal_automation.auto_follow_up_draft(self.conn, fresh, user_id=USER, provider_factory=provider_factory,
                                                        draft_provider=provider)

    def test_a_due_follow_up_is_written_and_waits_for_approval(self):
        target = self.due()
        self.on("auto_follow_up_drafts")
        self.assertEqual([item["id"] for item in internal_automation.follow_up_draft_due(self.conn, USER)], [target["id"]])
        result = self.write(target)
        self.assertTrue(result["drafted"], result)
        after = get_target(self.conn, target["id"], user_id=USER)
        self.assertEqual((after["follow_up_status"], after["status"]), ("generated", "sent"), "nothing is sent")
        self.assertTrue(after["follow_up_body"])
        [action] = automation.list_actions(self.conn, USER, feature="auto_follow_up_drafts")
        self.assertEqual((action["action_type"], action["status"]), ("outreach.follow_up_draft", "applied"))
        self.assertEqual(internal_automation.follow_up_draft_due(self.conn, USER), [], "one draft per follow-up date")

    def test_not_when_they_replied_or_it_bounced_or_it_is_not_due(self):
        self.target(company="Later", website="https://later.test", contact_email="a@later.test", status="sent")
        replied = self.due(company="Kiva", website="https://kiva.test", contact_email="hi@kiva.test")
        log_reply(self.conn, replied["id"], "Thanks, we will get back to you.", user_id=USER)
        bounced = self.due(company="Mako", website="https://mako.test", contact_email="x@mako.test")
        record_bounce(self.conn, bounced["id"], user_id=USER, reason="Address not found", source="gmail")
        self.assertEqual(internal_automation.follow_up_draft_due(self.conn, USER), [])

    def test_a_failure_is_logged_and_retried_after_six_hours(self):
        target = self.due()
        self.on("auto_follow_up_drafts")

        def down(_provider, _model):
            raise RuntimeError("The model is unreachable")

        result = self.write(target, provider_factory=down, provider="anthropic")
        self.assertFalse(result["drafted"])
        events = get_target(self.conn, target["id"], user_id=USER, include_events=True)["events"]
        self.assertEqual((events[0]["event_type"], events[0]["detail"]), ("auto_follow_up_draft_failed", "The model is unreachable"))
        self.assertEqual(internal_automation.follow_up_draft_due(self.conn, USER), [])
        later = datetime.now(timezone.utc) + timedelta(hours=7)
        self.assertEqual(len(internal_automation.follow_up_draft_due(self.conn, USER, now=later)), 1)

    def test_undo_discards_an_untouched_draft_and_keeps_it_in_the_history(self):
        target = self.due()
        self.on("auto_follow_up_drafts")
        self.write(target)
        [action] = automation.list_actions(self.conn, USER, feature="auto_follow_up_drafts")
        undone = automation.undo(self.conn, action["id"], USER)
        self.assertIn("history", undone["undo_note"])
        after = get_target(self.conn, target["id"], user_id=USER)
        self.assertEqual((after["follow_up_body"], after["follow_up_status"]), ("", "none"))
        self.assertEqual(len(draft_versions(self.conn, target["id"], user_id=USER, kind="follow_up")), 1)
        self.assertEqual(internal_automation.follow_up_draft_due(self.conn, USER), [], "an undo is not written again")

    def test_undo_is_refused_once_the_draft_was_edited_or_approved(self):
        target = self.due()
        self.on("auto_follow_up_drafts")
        self.write(target)
        [action] = automation.list_actions(self.conn, USER, feature="auto_follow_up_drafts")
        update_target(self.conn, target["id"], {"follow_up_body": "Hi Bovi team,\n\nMy own words.\n\nSam"}, user_id=USER)
        with self.assertRaisesRegex(Superseded, "changed since"):
            automation.undo(self.conn, action["id"], USER)
        self.assertIn("My own words", get_target(self.conn, target["id"], user_id=USER)["follow_up_body"])

    def test_a_follow_up_written_meanwhile_stands(self):
        target = self.due()
        self.on("auto_follow_up_drafts")
        from opportunity_app import outreach_drafting

        original = outreach_drafting.compose_draft

        def student_writes_first(*args, **kwargs):
            prepared = original(*args, **kwargs)
            update_target(self.conn, target["id"], {"follow_up_subject": "Mine", "follow_up_body": "My follow-up"}, user_id=USER)
            return prepared

        with mock.patch.object(outreach_drafting, "compose_draft", student_writes_first):
            result = self.write(target)
        self.assertFalse(result["drafted"])
        self.assertEqual(get_target(self.conn, target["id"], user_id=USER)["follow_up_body"], "My follow-up")
        self.assertEqual(automation.list_actions(self.conn, USER, feature="auto_follow_up_drafts"), [])

    def test_the_worker_writes_at_most_one_per_pass_and_nothing_while_paused(self):
        self.due()
        self.due(company="Kiva", website="https://kiva.test", contact_email="hi@kiva.test")
        self.on("auto_follow_up_drafts")
        worker = AutomationWorker(self.platform_path, fetcher_factory=lambda: None, provider_factory=legacy, draft_provider="legacy")
        automation.set_paused(self.conn, USER, True)
        self.assertEqual(worker.run_once().get("follow_up_drafts", []), [])
        automation.set_paused(self.conn, USER, False)
        report = worker.run_once()
        self.assertEqual(len(report["follow_up_drafts"]), 1)
        self.assertEqual(len(worker.run_once()["follow_up_drafts"]), 1)
        self.assertEqual(internal_automation.follow_up_draft_due(self.conn, USER), [])

    # --- An email that may be a reply holds the follow-up until the student says ---

    def test_not_while_a_possible_reply_waits_even_when_it_is_only_a_candidate(self):
        held = self.due()
        owner = self.due(**self.company("Kiva"))
        candidate = self.due(**self.company("Mako"))
        free = self.due(**self.company("Tarn"))
        self.possible(held)
        self.possible(owner, candidate)
        self.assertEqual([item["id"] for item in internal_automation.follow_up_draft_due(self.conn, USER)], [free["id"]])
        self.on("auto_follow_up_drafts")
        worker = AutomationWorker(self.platform_path, fetcher_factory=lambda: None, provider_factory=legacy, draft_provider="legacy")
        self.assertEqual([item["target_id"] for item in worker.run_once()["follow_up_drafts"]], [free["id"]])
        self.assertEqual(worker.run_once().get("follow_up_drafts", []), [], "nothing else is due")
        self.assertEqual([get_target(self.conn, target["id"], user_id=USER)["follow_up_body"] for target in (held, owner, candidate)],
                         ["", "", ""])

    def test_a_possible_reply_found_while_the_draft_is_written_stops_it_and_is_not_a_failure(self):
        self.on("auto_follow_up_drafts")
        from opportunity_app import outreach_drafting

        original = outreach_drafting.compose_draft
        for index, filed_under_another in enumerate((False, True)):
            with self.subTest(filed_under_another=filed_under_another):
                target = self.due(**self.company(f"Due {index}"))
                other = self.target(**self.company(f"Other {index}"), status="sent",
                                    sent_at=(date.today() - timedelta(days=3)).isoformat())
                found = []

                def found_meanwhile(*args, target=target, other=other, filed_under_another=filed_under_another, found=found, **kwargs):
                    prepared = original(*args, **kwargs)
                    found.append(self.possible(other, target) if filed_under_another else self.possible(target))
                    return prepared

                with mock.patch.object(outreach_drafting, "compose_draft", found_meanwhile):
                    result = self.write(target)
                self.assertEqual(result, {"target_id": target["id"], "drafted": False, "skipped": True,
                                          "error": "The company is no longer waiting on a follow-up, so no draft was saved"})
                after = get_target(self.conn, target["id"], user_id=USER, include_events=True)
                self.assertEqual((after["follow_up_body"], after["follow_up_status"]), ("", "none"))
                self.assertEqual(draft_versions(self.conn, target["id"], user_id=USER, kind="follow_up"), [])
                self.assertNotIn(internal_automation.AUTO_FOLLOW_UP_DRAFT_FAILED, [event["event_type"] for event in after["events"]])
                self.assertEqual(automation.list_actions(self.conn, USER, feature="auto_follow_up_drafts"), [])
                self.assertNotIn(target["id"], [item["id"] for item in internal_automation.follow_up_draft_due(self.conn, USER)])
                # Held, not tried: once the student says it is not a reply, the draft is written on the next pass.
                self.settle(target, found[0], "not_reply")
                self.assertIn(target["id"], [item["id"] for item in internal_automation.follow_up_draft_due(self.conn, USER)])

    def test_the_draft_handler_refuses_while_a_possible_reply_waits(self):
        self.on("auto_follow_up_drafts")
        owner = self.due()
        candidate = self.due(**self.company("Kiva"))
        self.possible(owner, candidate)
        draft = {"kind": "follow_up", "subject": "Following up", "body": "Hi team,\n\nFollowing up on my note.\n\nSam"}
        for target in (owner, candidate):
            with self.subTest(company=target["company"]):
                with self.assertRaisesRegex(automation.NotApplicable, "no longer waiting on a follow-up"):
                    automation.perform(
                        self.conn, user_id=USER, feature="auto_follow_up_drafts", action_type="outreach.follow_up_draft",
                        subject_kind="outreach_target", subject_id=target["id"], after={"draft": draft}, evidence={},
                        summary="x", basis="test", confidence=None, idempotency_key=f"guard:{target['id']}", auto=True,
                    )
                self.assertEqual(get_target(self.conn, target["id"], user_id=USER)["follow_up_body"], "")
        self.assertEqual(automation.list_actions(self.conn, USER, feature="auto_follow_up_drafts"), [])


# --- Auto-save and auto-pass ----------------------------------------------------------------


class TriageTests(Case):
    def setUp(self):
        super().setUp()
        self.set_profile(automation={"auto_save_at": 80, "auto_pass_below": 40})

    def test_the_thresholds_decide_and_the_evidence_holds_the_score_and_top_reasons(self):
        self.on("auto_save", "auto_pass")
        self.add_opportunity("hi", score=80, reasons=["35 base", "+4 interests: data", "+18 preferred role type (internship)",
                                                      "+10 skills: Python", "+2 terms: summer"])
        self.add_opportunity("mid", score=60)
        self.add_opportunity("just-under-save", score=79)
        self.add_opportunity("at-pass", score=40, description="A full description of the role.")
        self.add_opportunity("lo", score=39, description="A full description of the role.")
        report = auto_triage.run_auto_triage(self.conn, user_id=USER)
        self.assertEqual([item["opportunity_id"] for item in report["saved"]], ["hi"])
        self.assertEqual([item["opportunity_id"] for item in report["passed"]], ["lo"])
        self.assertEqual((self.intent("hi"), self.intent("mid"), self.intent("lo")), ("saved", None, "passed"))
        self.assertEqual((self.intent("just-under-save"), self.intent("at-pass")), (None, None),
                         "79 is not at 80, and 40 is not below 40")
        [saved] = automation.list_actions(self.conn, USER, feature="auto_save")
        self.assertEqual(saved["evidence"]["score"], 80)
        self.assertEqual(saved["evidence"]["reasons"], ["+18 preferred role type (internship)", "+10 skills: Python", "+4 interests: data"])
        self.assertEqual(saved["idempotency_key"], "triage:hi:saved")
        source = self.conn.execute("SELECT source FROM opportunity_interactions WHERE opportunity_id='hi'").fetchone()[0]
        self.assertEqual(source, f"automation:{saved['id']}")

    def test_missing_thresholds_mean_no_action(self):
        self.on("auto_save", "auto_pass")
        self.set_profile(automation={})
        self.add_opportunity("hi", score=95)
        report = auto_triage.run_auto_triage(self.conn, user_id=USER)
        self.assertEqual((report["saved"], report["passed"]), ([], []))
        self.assertIn("auto_save_at", report["notes"]["auto_save"])
        self.assertIsNone(self.intent("hi"))

    def test_a_sparse_posting_is_never_auto_passed(self):
        self.on("auto_pass")
        self.add_opportunity("sparse", score=5, description="   ")
        self.assertEqual(auto_triage.run_auto_triage(self.conn, user_id=USER)["passed"], [])
        self.assertIsNone(self.intent("sparse"))

    def test_a_role_already_touched_or_seen_before_the_switch_is_skipped(self):
        self.add_opportunity("old", score=95, first_seen="2026-01-01T00:00:00+00:00")
        self.on("auto_save")
        self.add_opportunity("seen", score=95)
        record_intent(self.conn, "seen", "seen", user_id=USER)
        self.add_opportunity("applied", score=95)
        record_intent(self.conn, "applied", "apply_opened", user_id=USER)
        with self.conn:
            self.conn.execute("DELETE FROM opportunity_interactions WHERE opportunity_id='applied'")
        report = auto_triage.run_auto_triage(self.conn, user_id=USER)
        self.assertEqual(report["saved"], [], "an interaction, an application, or the backlog before the switch: all left alone")
        self.assertEqual(self.intent("old"), None)

    def test_a_choice_the_student_makes_meanwhile_stands(self):
        self.on("auto_pass")
        self.add_opportunity("lo", score=10, description="Real text.")
        real = automation_handlers.OpportunityIntent.apply

        def student_first(handler, conn, *args, **kwargs):
            conn.execute("INSERT INTO opportunity_interactions(opportunity_id, user_id, action, created_at, source) "
                         "VALUES('lo', ?, 'saved', ?, 'user')", (USER, utc_now()))
            return real(handler, conn, *args, **kwargs)

        with mock.patch.object(automation_handlers.OpportunityIntent, "apply", student_first):
            report = auto_triage.run_auto_triage(self.conn, user_id=USER)
        self.assertEqual(report["passed"], [])
        self.assertEqual(automation.list_actions(self.conn, USER, feature="auto_pass"), [])
        self.assertIsNone(self.intent("lo"), "the whole transaction rolled back, the test's insert included")

    def test_re_runs_are_idempotent(self):
        self.on("auto_save", "auto_pass")
        self.add_opportunity("hi", score=90)
        self.add_opportunity("lo", score=10, description="Real text.")
        auto_triage.run_auto_triage(self.conn, user_id=USER)
        again = auto_triage.run_auto_triage(self.conn, user_id=USER)
        self.assertEqual((again["saved"], again["passed"]), ([], []))
        self.assertEqual(automation.count_actions(self.conn, USER), 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM opportunity_interactions WHERE opportunity_id IN ('hi', 'lo')").fetchone()[0], 2)

    def test_restore_undoes_an_auto_pass_and_it_leaves_the_weekly_list(self):
        self.on("auto_pass")
        self.add_opportunity("lo", score=10, description="Real text.")
        auto_triage.run_auto_triage(self.conn, user_id=USER)
        [item] = auto_triage.auto_passed_this_week(self.conn, USER)
        self.assertEqual((item["opportunity_id"], item["score"], item["title"]), ("lo", 10, "Engineering Intern"))
        with self.client() as client:
            listed = client.get("/api/v1/automation/auto-passed", headers=AUTH).json()
            self.assertEqual([row["action_id"] for row in listed["items"]], [item["action_id"]])
            restored = client.post(f"/api/v1/automation/actions/{item['action_id']}/undo", headers=AUTH)
            self.assertEqual(restored.status_code, 200, restored.text)
            self.assertEqual(client.get("/api/v1/automation/auto-passed", headers=AUTH).json()["items"], [])
        self.assertEqual(self.intent("lo"), "undo", "restored to neither saved nor passed")
        self.assertEqual(auto_triage.run_auto_triage(self.conn, user_id=USER)["passed"], [], "a restored role is not passed again")
        old = (datetime.now(timezone.utc) + timedelta(days=8))
        self.assertEqual(auto_triage.auto_passed_this_week(self.conn, USER, now=old), [])

    def test_an_auto_save_picks_the_resume_variant_too(self):
        hardware = self.add_resume(label="Hardware")
        self.set_profile(resume_variants=[{"label": "Hardware", "keywords": ["PCB", "embedded"]}])
        self.on("auto_save", "resume_variant_pick")
        self.add_opportunity("hw", title="Embedded PCB Intern", score=90)
        auto_triage.run_auto_triage(self.conn, user_id=USER)
        self.assertEqual(resume_variants.stored_pick(self.conn, USER, "hw")["resume_file_id"], hardware["file_id"])

    def test_after_a_sync_it_runs_and_never_fails_the_sync(self):
        self.on("auto_save")
        result = auto_triage.triage_after_sync(self.platform_path)
        self.assertEqual(result["saved"], [])
        health = {row["component"]: row for row in automation_health.health_summary(self.conn, USER)["components"]}
        self.assertIsNotNone(health["discovery.auto_triage"]["last_ok_at"])
        with mock.patch.object(auto_triage, "run_auto_triage", side_effect=RuntimeError("boom")), \
                self.assertLogs("opportunity_app.auto_triage", level="ERROR"):
            self.assertIsNone(auto_triage.triage_after_sync(self.platform_path))
        health = {row["component"]: row for row in automation_health.health_summary(self.conn, USER)["components"]}
        self.assertEqual(health["discovery.auto_triage"]["last_error"], "boom")
        with self.assertLogs("opportunity_app.auto_triage", level="ERROR"):
            self.assertIsNone(auto_triage.triage_after_sync(self.root / "missing" / "nowhere.db"))

    def test_the_daily_platform_sync_triages(self):
        self.on("auto_save")
        first_seen = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(timespec="seconds")
        with closing(sqlite3.connect(self.legacy_path)) as legacy, legacy:
            legacy.execute(
                "INSERT INTO jobs(id, source_key, source_name, external_id, company, title, location, role_type, url, description, "
                "posted_at, first_seen_at, last_seen_at, active, fingerprint, content_fingerprint, score, score_explanation, status) "
                "VALUES('job-new', 'greenhouse:acme', 'Acme Greenhouse', 'n-1', 'Nova Labs', 'Robotics Intern', 'Austin, TX', "
                "'internship', 'https://example.com/jobs/new', 'Build robots.', ?, ?, ?, 1, 'fp-new', 'cfp-new', 95, "
                "'[\"35 base\", \"+20 skills: robots\"]', 'discovered')",
                (first_seen, first_seen, first_seen),
            )
        # As daily.py runs it: the target is text, as argparse gives it.
        argv = ["migrate", "--source", str(self.legacy_path), "--target", str(self.platform_path),
                "--profile", str(self.root / "profile.json")]
        with mock.patch("sys.argv", argv), redirect_stdout(io.StringIO()) as out, \
                self.assertNoLogs("opportunity_app.auto_triage", level="ERROR"):
            migrate.main()
        self.assertEqual(self.intent("job-new"), "saved")
        self.assertIn("Automatically saved 1", out.getvalue())

    def test_the_web_refresh_sync_step_triages(self):
        manager = RefreshManager(self.platform_path, legacy_path=self.legacy_path, profile_path=self.root / "profile.json",
                                 runner=lambda arguments, on_line: 0)
        with mock.patch("opportunity_app.refresh.triage_after_sync", return_value={"saved": [1], "passed": []}) as triage:
            manager._state = {"state": "running", "steps": [{"key": "sync"}]}
            manager._sync()
        triage.assert_called_once_with(self.platform_path)
        self.assertIn("automatically saved 1", manager._state["steps"][0]["detail"])


# --- Setup ----------------------------------------------------------------------------------


class SetupValidationTests(unittest.TestCase):
    def test_the_new_profile_fields_are_checked(self):
        from opportunity_app.setup import validate_profile

        good = validate_profile({
            "resume_variants": [{"label": "Hardware", "keywords": ["CAD"]}, {"label": "Software", "keywords": ["Python"]}],
            "default_variant": "software", "application_follow_up_days": 14, "archive_after_days": 45,
            "automation": {"auto_save_at": 85, "auto_pass_below": 30.5},
        })
        self.assertTrue(good["ok"], good)
        self.assertEqual(good["warnings"], [])
        bad = validate_profile({
            "resume_variants": [{"label": "A", "keywords": "CAD"}, {"keywords": []}, {"label": "a", "keywords": []}],
            "default_variant": "Nope", "application_follow_up_days": 0, "archive_after_days": True,
            "automation": {"auto_save_at": 120, "auto_pass_below": 90},
        })
        joined = " ".join(bad["errors"])
        for fragment in ("resume_variants[0].keywords", "resume_variants[1].label", "listed twice", "application_follow_up_days",
                         "archive_after_days", "automation.auto_save_at"):
            self.assertIn(fragment, joined)
        self.assertTrue(any("default_variant" in warning for warning in bad["warnings"]))
        self.assertIn("could be both", " ".join(validate_profile({"automation": {"auto_save_at": 50, "auto_pass_below": 60}})["errors"]))


# --- The migration --------------------------------------------------------------------------


class MigrationTests(Case):
    def test_running_it_again_after_the_marker_was_lost_succeeds(self):
        with self.conn:
            self.conn.execute("DELETE FROM schema_migrations WHERE name='0039_internal_automation.sql'")
        ensure_product_schema(self.conn)
        self.assertTrue(has_column(self.conn, "resume_files", "variant_label"))
        self.conn.execute("SELECT COUNT(*) FROM opportunity_resume_picks").fetchone()
        schema._apply_internal_automation(self.conn, (MIGRATIONS / "0039_internal_automation.sql").read_text(encoding="utf-8"))
        self.conn.commit()

    def test_picks_go_with_the_account_and_the_role(self):
        from opportunity_app.operations import ACCOUNT_QUERIES, export_account

        self.assertIn("resume_picks", ACCOUNT_QUERIES)
        resume = self.add_resume(label="Hardware")
        resume_variants.set_student_pick(self.conn, USER, "job-b", resume["file_id"])
        self.assertEqual(len(export_account(self.conn, user_id=USER)["resume_picks"]), 1)
        with self.conn:
            self.conn.execute("DELETE FROM resume_files WHERE id=?", (resume["file_id"],))
        self.assertIsNone(resume_variants.stored_pick(self.conn, USER, "job-b"), "a deleted résumé takes its picks with it")


if __name__ == "__main__":
    unittest.main()
