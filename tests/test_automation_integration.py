"""Application mail (applications/inbox.py) and internal automation (automation/internal.py and friends) together.

Both register into the one automation registry (bootstrap.register_all, called as the app starts) and write the one
ledger. These tests pin what they share: the switches and their groups, the action types, that each switch has its own
circuit breaker, counted its own way, and that the startup call is what fills the registries.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opportunity_app.automation import ledger as automation, handlers as automation_handlers
from opportunity_app import bootstrap
from opportunity_app.api import create_app
from opportunity_app.applications.actions import record_intent
from opportunity_app.core.database import connect_product
from opportunity_app.core.timestamps import utc_now

from helpers_platform import build_and_migrate

USER = "local-user"


class RegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        bootstrap.register_all()  # what create_app does as the app starts (tests/helpers_platform.py does it too)

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


class BootstrapTests(unittest.TestCase):
    def test_create_app_fills_the_registries_through_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(bootstrap, "register_all") as register:
            _, platform_path = build_and_migrate(Path(directory))
            app = create_app(db_path=platform_path, access_token="registry-owner", start_inbox_watcher=False, start_automation_worker=False)
        self.assertIsNotNone(app)
        register.assert_called_once_with()

    def test_the_registries_hold_what_each_module_registers_and_nothing_else(self):
        bootstrap.register_all()
        bootstrap.register_all()  # harmless to repeat
        self.assertEqual(sorted(automation.HANDLERS), sorted([
            "application.stage", "opportunity.intent", "application.task", "outreach.status", "outreach.follow_up_draft",
            "resume.pick", "outreach.thank_you", "application.deadline", "application.capture_proposal",
        ]))
        self.assertEqual(sorted(automation.BREAKER_GROUPS), ["application_mail", "decline_thank_you"])
        self.assertEqual(sorted(automation.CORRECTIONS), ["application_mail"])
        unregistered = [key for key, check in automation.REQUIREMENTS.items() if getattr(check, "__qualname__", "").startswith("_unregistered_requirement")]
        self.assertEqual(unregistered, [], "every requirement was filled in at startup")

    def test_a_registry_nobody_filled_fails_loudly(self):
        with mock.patch.dict(automation.HANDLERS, clear=True):
            with self.assertRaisesRegex(ValueError, "Unknown automation action type"):
                automation._handler("application.stage")
            self.assertFalse(automation.undoable("application.stage"), "and nothing claims to be undoable")
        for key in ("auto_save", "auto_pass", "resume_variant_pick", "apply_agent"):
            with self.subTest(key=key), self.assertRaisesRegex(RuntimeError, f"requirement for {key} was never registered"):
                automation._unregistered_requirement(key)(None, USER)

    def test_a_handler_may_extend_the_base_and_keeps_todays_defaults(self):
        class Plain(automation.HandlerBase):
            fields = ("flag",)

        handler = Plain()
        self.assertTrue(handler.undoable)
        after = {"flag": "x"}
        self.assertIs(handler.ledger(after), after, "the ledger keeps all of it unless a handler says otherwise")
        self.assertFalse(automation_handlers.OutreachThankYou.undoable, "only the handlers that set it say no")
        self.assertTrue(automation_handlers.ApplicationStage.undoable)


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
