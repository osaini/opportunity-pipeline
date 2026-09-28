"""Phase 2 automation that stays inside the app: résumé variants, silent applications, auto-close,
follow-up drafts, and saving or passing on new roles by score. Every change has an Undo."""

import json
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR, auto_triage, automation, internal_automation, resume_variants, schema
from opportunity_app.actions import record_intent, update_application
from opportunity_app.api import create_app
from opportunity_app.automation import Superseded
from opportunity_app.extension_apply import apply_context
from opportunity_app.outreach import create_target, get_target, log_reply, update_target
from opportunity_app.outreach_automation import AutomationWorker
from opportunity_app.outreach_delivery import record_bounce
from opportunity_app.outreach_drafting import draft_versions
from opportunity_app.refresh import RefreshManager
from opportunity_app.resumes import ResumeValidationError, confirm_variant, resume_record
from opportunity_app.schema import connect_product, ensure_product_schema, utc_now
from opportunity_app.urgent import urgent_queue
from opportunity_app.user_time import user_timezone

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
        self.set_profile(resume_variants=[], default_variant="")
        self.on("resume_variant_pick")
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

    def test_a_failed_pick_never_fails_the_save(self):
        self.on("resume_variant_pick")
        record_intent(self.conn, "job-a", "undo", user_id=USER)
        with mock.patch.object(resume_variants, "pick_variant", side_effect=RuntimeError("boom")), \
                self.assertLogs("opportunity_app.resume_variants", level="ERROR"):
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

    def test_a_bad_setting_falls_back_to_21(self):
        self.set_profile(application_follow_up_days="soon")
        self.assertEqual(internal_automation.follow_up_days(self.conn, USER), 21)
        self.set_profile(application_follow_up_days=True)
        self.assertEqual(internal_automation.follow_up_days(self.conn, USER), 21)


class ArchiveTests(Case):
    def applied(self, days_ago):
        applied_at = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(timespec="microseconds")
        with self.conn:
            self.conn.execute("UPDATE applications SET stage='applied', applied_at=? WHERE id='app-job-b'", (applied_at,))

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


class AutoCloseTests(OutreachCase):
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
        self.add_opportunity("lo", score=39, description="A full description of the role.")
        report = auto_triage.run_auto_triage(self.conn, user_id=USER)
        self.assertEqual([item["opportunity_id"] for item in report["saved"]], ["hi"])
        self.assertEqual([item["opportunity_id"] for item in report["passed"]], ["lo"])
        self.assertEqual((self.intent("hi"), self.intent("mid"), self.intent("lo")), ("saved", None, "passed"))
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
        real = automation.OpportunityIntent.apply

        def student_first(handler, conn, *args, **kwargs):
            conn.execute("INSERT INTO opportunity_interactions(opportunity_id, user_id, action, created_at, source) "
                         "VALUES('lo', ?, 'saved', ?, 'user')", (USER, utc_now()))
            return real(handler, conn, *args, **kwargs)

        with mock.patch.object(automation.OpportunityIntent, "apply", student_first):
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
        health = {row["component"]: row for row in automation.health_summary(self.conn, USER)["components"]}
        self.assertIsNotNone(health["discovery.auto_triage"]["last_ok_at"])
        with mock.patch.object(auto_triage, "run_auto_triage", side_effect=RuntimeError("boom")), \
                self.assertLogs("opportunity_app.auto_triage", level="ERROR"):
            self.assertIsNone(auto_triage.triage_after_sync(self.platform_path))
        health = {row["component"]: row for row in automation.health_summary(self.conn, USER)["components"]}
        self.assertEqual(health["discovery.auto_triage"]["last_error"], "boom")
        with self.assertLogs("opportunity_app.auto_triage", level="ERROR"):
            self.assertIsNone(auto_triage.triage_after_sync(self.root / "missing" / "nowhere.db"))

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
        self.assertTrue(schema._has_column(self.conn, "resume_files", "variant_label"))
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
