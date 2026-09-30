"""Application mail (application_inbox.py) and internal automation (internal_automation.py and friends) together.

Both register into the one automation registry and write the one ledger. These
tests pin what they share: the switches and their groups, the action types,
and that each switch has its own circuit breaker, counted its own way.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opportunity_app import api  # noqa: F401  (imports every module that registers a switch or a handler)
from opportunity_app import automation
from opportunity_app.actions import record_intent
from opportunity_app.schema import connect_product, utc_now

from helpers_platform import build_and_migrate

USER = "local-user"


class RegistryTests(unittest.TestCase):
    def test_both_phases_share_one_registry(self):
        groups = {key: feature.group for key, feature in automation.FEATURES.items()}
        self.assertEqual(groups, {
            "auto_drafts": "outreach", "bounce_recovery": "outreach", "bounce_auto_resend": "outreach", "scheduled_sending": "outreach",
            "follow_up_review": "outreach", "form_submission": "outreach", "outreach_auto_close": "outreach",
            "auto_follow_up_drafts": "outreach", "decline_thank_you": "outreach",
            "jev_inbox_suggestions": "applications", "application_mail": "applications", "resume_variant_pick": "applications",
            "application_silence": "applications", "archive_silent_applications": "applications", "apply_agent": "applications",
            "auto_save": "discovery", "auto_pass": "discovery",
            "desktop_notifications": "notifications",
        })
        self.assertEqual(automation.FEATURES["application_mail"].modes, automation.OFF_SHADOW_ON, "the one that must earn On in shadow")
        self.assertEqual(set(automation.HANDLERS), {
            "application.stage", "application.task", "opportunity.intent",  # Phase 0
            "application.deadline", "application.capture_proposal",  # application mail
            "outreach.status", "outreach.follow_up_draft", "resume.pick",  # internal automation
            "outreach.thank_you",  # the thank-you after a decline
        })
        self.assertEqual({key for key in automation.HANDLERS if not automation.undoable(key)},
                         {"application.capture_proposal", "outreach.thank_you"}, "an email that went cannot be taken back")
        self.assertEqual(set(automation.BREAKER_GROUPS), {"application_mail", "decline_thank_you"},
                         "only one email's changes (a job email's, or a decline's) are counted together")
        self.assertEqual(set(automation.CORRECTIONS), {"application_mail"})


class BreakerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        _, platform_path = build_and_migrate(Path(self.tempdir.name))
        self.conn = connect_product(platform_path)
        self.addCleanup(self.conn.close)
        self.acme = record_intent(self.conn, "job-a", "apply_opened", user_id=USER)["application_id"]
        with self.conn:  # application_mail earns On in shadow; its own tests cover that gate
            self.conn.execute(
                "INSERT INTO user_settings(user_id, key, value, updated_at) VALUES(?, 'application_mail', 'on', ?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET value='on'",
                (USER, utc_now()),
            )
        automation.set_mode(self.conn, USER, "archive_silent_applications", "on")

    def email_change(self, gmail_id, action_type, application_id, after):
        return automation.perform(
            self.conn, user_id=USER, feature="application_mail", action_type=action_type, subject_kind="application",
            subject_id=application_id, after=after, evidence={"gmail_id": gmail_id}, summary=f"From {gmail_id}", basis="test",
            confidence=0.9, idempotency_key=f"gmail:{gmail_id}:{application_id}:{action_type}", auto=True,
        )

    def archive(self, application_id):
        return automation.perform(
            self.conn, user_id=USER, feature="archive_silent_applications", action_type="application.stage",
            subject_kind="application", subject_id=application_id, after={"stage": "archived", "only_from": "applied"},
            evidence={}, summary=f"Archived {application_id}", basis="silence:60d", confidence=None,
            idempotency_key=f"archive-silent:{application_id}:test", auto=True,
        )

    def test_each_switch_has_its_own_breaker_counted_its_own_way(self):
        # One email, two changes, both taken back: application mail counts that once.
        stage = self.email_change("m-1", "application.stage", self.acme, {"stage": "applied"})
        task = self.email_change("m-1", "application.task", self.acme, {"task": {"title": "Complete the assessment", "origin": "email"}})
        automation.undo(self.conn, task["id"], USER)
        automation.undo(self.conn, stage["id"], USER)
        self.assertEqual(automation.mode(self.conn, USER, "application_mail"), "on", "one email taken back is one, not two")
        # The archive switch counts action by action, and nothing application mail did counts toward it.
        first = self.archive("app-job-b")
        automation.undo(self.conn, first["id"], USER)
        self.assertEqual(automation.mode(self.conn, USER, "archive_silent_applications"), "on")
        self.assertEqual(automation.mode(self.conn, USER, "application_mail"), "on")
        second = automation.perform(
            self.conn, user_id=USER, feature="archive_silent_applications", action_type="application.stage",
            subject_kind="application", subject_id="app-job-b", after={"stage": "archived", "only_from": "applied"},
            evidence={}, summary="Archived again", basis="silence:60d", confidence=None,
            idempotency_key="archive-silent:app-job-b:again", auto=True,
        )
        result = automation.undo(self.conn, second["id"], USER)
        self.assertTrue(result["feature_paused"])
        self.assertEqual(automation.mode(self.conn, USER, "archive_silent_applications"), "off")
        self.assertEqual(automation.mode(self.conn, USER, "application_mail"), "on", "another switch's breaker leaves this one alone")

    def test_an_archive_an_email_moved_on_is_left_as_it_is_and_never_counted(self):
        archived = self.archive("app-job-b")
        self.email_change("m-2", "application.stage", "app-job-b", {"stage": "interview"})
        with self.assertRaises(automation.Superseded):
            automation.undo(self.conn, archived["id"], USER)
        self.assertEqual(self.conn.execute("SELECT stage FROM applications WHERE id='app-job-b'").fetchone()[0], "interview")
        self.assertEqual(automation.mode(self.conn, USER, "archive_silent_applications"), "on", "left as it was is not taken back")


if __name__ == "__main__":
    unittest.main()
