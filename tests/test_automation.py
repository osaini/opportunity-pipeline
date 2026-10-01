"""The automation core: switches and the pause, the ledger and its undo, the breaker, notices, and health."""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opportunity_app import actions as actions_module, automation, schema
from opportunity_app.operations import ACCOUNT_QUERIES, delete_account, export_account
from opportunity_app.student_agent import decide_proposal
from opportunity_app.actions import (
    add_application_task,
    import_applications,
    record_intent,
    update_application,
    update_application_task,
    add_application_task_tx,
    record_intent_tx,
    update_application_tx,
)
from opportunity_app.automation import OFF_SHADOW_ON, AutomationGateError, Feature, Superseded
from opportunity_app.outreach_automation import SETTINGS, settings, update_settings
from opportunity_app.schema import connect_product, ensure_product_schema, utc_now

from helpers_platform import build_and_migrate

USER = "local-user"
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
LEGACY_OUTREACH_SETTINGS = {
    "auto_drafts": "Write a draft for every company with a contact and a location",
    "bounce_recovery": "After a bounce, find another contact and fix the greeting",
    "bounce_auto_resend": "After a bounce, send the approved email again to the new contact when only the greeting changed",
    "scheduled_sending": "Send approved emails on the recipient's next weekday morning",
    "follow_up_review": "Have a second model check each follow-up before it goes out",
    "form_submission": "Send approved first messages through the company's contact form when it has no email",
}
# A fixed clock for everything that takes ``now``.
T0 = datetime(2026, 9, 20, 12, 0, tzinfo=timezone.utc)


class FlagHandler:
    """A test-only action type: one per-subject value kept in user_settings."""

    fields = ("flag",)

    def key(self, subject_id):
        return f"test.flag.{subject_id}"

    def read(self, conn, user_id, subject_id):
        row = conn.execute("SELECT value FROM user_settings WHERE user_id=? AND key=?", (user_id, self.key(subject_id))).fetchone()
        return {"flag": row[0] if row else None}

    def apply(self, conn, user_id, subject_id, after, *, source, timestamp):
        conn.execute(
            "INSERT INTO user_settings(user_id, key, value, updated_at) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (user_id, self.key(subject_id), after["flag"], timestamp),
        )
        return {"source": source}

    def undo(self, conn, user_id, subject_id, before, after, *, source, timestamp):
        if self.read(conn, user_id, subject_id)["flag"] != after["flag"]:
            raise Superseded("The flag changed since")
        if before["flag"] is None:
            conn.execute("DELETE FROM user_settings WHERE user_id=? AND key=?", (user_id, self.key(subject_id)))
        else:
            conn.execute("UPDATE user_settings SET value=? WHERE user_id=? AND key=?", (before["flag"], user_id, self.key(subject_id)))


SWITCH = Feature("test_switch", "Test switch", "Raises a test flag on its own", "applications", "internal")
SHADOWED = Feature("test_shadow", "Test shadow", "Raises a test flag on its own, after a trial in shadow", "applications", "internal", OFF_SHADOW_ON)


class AutomationCase(unittest.TestCase):
    """A throwaway database, a test-only feature of each kind, and the test flag handler."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        _, self.platform_path = build_and_migrate(Path(self.tempdir.name))
        self.conn = connect_product(self.platform_path)
        self.addCleanup(self.conn.close)
        for feature in (SWITCH, SHADOWED):
            automation.register(feature)
            self.addCleanup(automation.FEATURES.pop, feature.key, None)
        handlers = mock.patch.dict(automation.HANDLERS, {"test.flag": FlagHandler()})
        handlers.start()
        self.addCleanup(handlers.stop)

    def act(self, *, feature="test_switch", action_type="test.flag", subject_id="s1", subject_kind="test",
            after=None, key=None, auto=True, conn=None):
        return automation.perform(
            conn or self.conn, user_id=USER, feature=feature, action_type=action_type, subject_kind=subject_kind,
            subject_id=subject_id, after={"flag": "up"} if after is None else after, evidence={"message_id": "m-1"},
            summary="Raised the test flag", basis="rule:test", confidence=0.9, idempotency_key=key or f"test:{uuid4().hex}",
            auto=auto,
        )

    def flag(self, subject_id="s1"):
        return FlagHandler().read(self.conn, USER, subject_id)["flag"]

    def stage(self, application_id):
        row = self.conn.execute("SELECT stage, applied_at FROM applications WHERE id=?", (application_id,)).fetchone()
        return row["stage"], row["applied_at"]

    def events(self, application_id, event_type=None):
        rows = self.conn.execute(
            "SELECT event_type, from_stage, to_stage, detail_json FROM application_events WHERE application_id=? ORDER BY id",
            (application_id,),
        ).fetchall()
        return [(row["event_type"], row["from_stage"], row["to_stage"], json.loads(row["detail_json"]))
                for row in rows if event_type is None or row["event_type"] == event_type]

    def applying(self):
        """job-a as an application still being written (stage 'applying')."""
        return record_intent(self.conn, "job-a", "apply_opened", user_id=USER)["application_id"]

    def other_connection(self):
        conn = connect_product(self.platform_path)
        self.addCleanup(conn.close)
        return conn

    def impatient_connection(self):
        """Another connection that gives up on a held write lock at once, instead of after five seconds."""
        conn = self.other_connection()
        conn.execute("PRAGMA busy_timeout = 50")
        return conn

    def meanwhile(self, owner, method, *writes):
        """Patch owner.method so each write runs once, on another connection, the first time this test's connection calls it.

        Returns the patch and what each write returned, or the error it raised.
        """
        real = getattr(owner, method)
        seen = {}

        def patched(handler, conn, *args, **kwargs):
            if conn is self.conn and "in_transaction" not in seen:
                seen["in_transaction"] = conn.in_transaction
                other = self.impatient_connection()
                for name, write in writes:
                    try:
                        seen[name] = write(other)
                    except sqlite3.OperationalError as exc:
                        seen[name] = str(exc)
            return real(handler, conn, *args, **kwargs)

        return mock.patch.object(owner, method, patched), seen


# --- The registry and the switches ----------------------------------------------------------


class RegistryTests(AutomationCase):
    def test_the_registry_holds_the_outreach_switches_with_the_same_meaning(self):
        self.assertEqual(SETTINGS, LEGACY_OUTREACH_SETTINGS, "the legacy view keeps its keys and words")
        for key in LEGACY_OUTREACH_SETTINGS:
            feature = automation.FEATURES[key]
            self.assertEqual((feature.group, feature.modes), ("outreach", ("off", "on")))
        self.assertEqual({key for key, f in automation.FEATURES.items() if f.risk == "external"},
                         {"scheduled_sending", "bounce_auto_resend", "form_submission", "decline_thank_you", "apply_agent"})
        self.assertEqual(automation.FEATURES["jev_inbox_suggestions"].group, "applications")
        self.assertEqual(automation.FEATURES["desktop_notifications"].group, "notifications")
        with self.assertRaises(ValueError):
            Feature("bad", "Bad", "Nothing", "elsewhere", "internal")

    def test_the_legacy_settings_keep_their_shapes(self):
        off = dict.fromkeys(LEGACY_OUTREACH_SETTINGS, False)
        self.assertEqual(settings(self.conn, user_id=USER), off)
        self.assertEqual(update_settings(self.conn, {"auto_drafts": 1, "form_submission": True}, user_id=USER),
                         {**off, "auto_drafts": True, "form_submission": True})
        stored = dict(self.conn.execute("SELECT key, value FROM user_settings WHERE key IN ('auto_drafts', 'form_submission')").fetchall())
        self.assertEqual(stored, {"auto_drafts": "on", "form_submission": "on"}, "stored as on and off, as before")
        self.assertEqual(update_settings(self.conn, {"auto_drafts": False}, user_id=USER)["auto_drafts"], False)
        with self.assertRaisesRegex(ValueError, "Unknown automation settings: autopilot"):
            update_settings(self.conn, {"autopilot": True}, user_id=USER)

    def test_the_legacy_view_ignores_pause_but_the_worker_check_does_not(self):
        update_settings(self.conn, {"bounce_recovery": True}, user_id=USER)
        automation.set_paused(self.conn, USER, True)
        self.assertTrue(settings(self.conn, user_id=USER)["bounce_recovery"], "the switch as the student set it")
        self.assertFalse(automation.is_enabled(self.conn, USER, "bounce_recovery"))
        automation.set_paused(self.conn, USER, False)
        self.assertTrue(automation.is_enabled(self.conn, USER, "bounce_recovery"))

    def test_modes_are_checked_against_what_each_feature_takes(self):
        self.assertEqual(automation.mode(self.conn, USER, "auto_drafts"), "off", "no row means off")
        with self.assertRaises(ValueError):
            automation.set_mode(self.conn, USER, "auto_drafts", "shadow")
        with self.assertRaises(ValueError):
            automation.set_mode(self.conn, USER, "auto_drafts", "maybe")
        with self.assertRaisesRegex(ValueError, "Unknown automation feature"):
            automation.set_mode(self.conn, USER, "autopilot", "on")
        self.assertEqual(automation.set_mode(self.conn, USER, "auto_drafts", "on"), "on")
        self.assertEqual(automation.set_mode(self.conn, USER, "test_shadow", "shadow"), "shadow")
        self.assertTrue(automation.is_shadow(self.conn, USER, "test_shadow"))
        with self.conn:
            self.conn.execute("UPDATE user_settings SET value='shadow' WHERE key='auto_drafts'")
        self.assertEqual(automation.mode(self.conn, USER, "auto_drafts"), "off", "a value the feature does not take is off")

    def test_the_settings_payload_lists_every_feature(self):
        payload = automation.settings_payload(self.conn, USER)
        self.assertFalse(payload["paused"])
        by_key = {item["key"]: item for item in payload["features"]}
        self.assertEqual(set(by_key), set(automation.FEATURES))
        self.assertEqual(by_key["form_submission"]["risk"], "external")
        self.assertEqual((by_key["test_shadow"]["can_turn_on"], by_key["test_shadow"]["modes"]), (False, ["off", "shadow", "on"]))
        self.assertIn("shadow", by_key["test_shadow"]["can_turn_on_reason"])
        self.assertEqual((by_key["auto_drafts"]["can_turn_on"], by_key["auto_drafts"]["can_turn_on_reason"]), (True, ""))


class ShadowGateTests(AutomationCase):
    """A shadow-capable feature acts on its own only after 48 hours and 5 reviewed actions in shadow, none wrong."""

    def start_shadow(self):
        # An hour ago, so the shadow rows written now fall after it.
        self.since = datetime.now(timezone.utc) - timedelta(hours=1)
        automation.set_mode(self.conn, USER, "test_shadow", "shadow", now=self.since)

    def shadow_rows(self, count, *, review="right"):
        rows = [self.act(feature="test_shadow", subject_id=f"s{n}") for n in range(count)]
        for row in rows:
            self.assertEqual(row["status"], "shadow")
            if review:
                automation.review(self.conn, row["id"], USER, review)
        return rows

    def gate(self, hours):
        return automation.can_turn_on(self.conn, USER, "test_shadow", now=self.since + timedelta(hours=hours))

    def test_a_feature_that_never_ran_in_shadow_cannot_be_turned_on(self):
        allowed, reason = automation.can_turn_on(self.conn, USER, "test_shadow")
        self.assertFalse(allowed)
        self.assertIn("shadow first", reason)
        with self.assertRaises(AutomationGateError):
            automation.set_mode(self.conn, USER, "test_shadow", "on")
        self.assertEqual(automation.can_turn_on(self.conn, USER, "auto_drafts"), (True, ""), "a feature with no shadow has no gate")

    def test_it_needs_48_hours(self):
        self.start_shadow()
        self.shadow_rows(5)
        self.assertEqual(self.gate(31), (False, "Needs 48 hours in shadow first (31 hours so far)"))
        self.assertEqual(self.gate(48), (True, ""))

    def test_it_needs_five_actions(self):
        self.start_shadow()
        self.shadow_rows(3)
        self.assertEqual(self.gate(49), (False, "Needs at least 5 actions in shadow first (3 so far)"))

    def test_every_action_must_be_reviewed(self):
        self.start_shadow()
        self.shadow_rows(3)
        self.shadow_rows(2, review="")
        self.assertEqual(self.gate(49), (False, "Review every shadow action first (2 not reviewed yet)"))

    def test_one_wrong_review_blocks_it_until_shadow_starts_again(self):
        self.start_shadow()
        rows = self.shadow_rows(5)
        automation.review(self.conn, rows[2]["id"], USER, "wrong")
        allowed, reason = self.gate(49)
        self.assertFalse(allowed)
        self.assertIn("wrong", reason)
        with self.assertRaises(AutomationGateError):
            automation.set_mode(self.conn, USER, "test_shadow", "on", now=self.since + timedelta(hours=49))
        # Setting shadow again while in shadow does not restart the clock.
        automation.set_mode(self.conn, USER, "test_shadow", "shadow", now=self.since + timedelta(hours=50))
        self.assertEqual(automation.settings_payload(self.conn, USER)["features"][-1]["shadow_since"],
                         self.since.isoformat(timespec="microseconds"))

    def test_with_everything_in_place_it_turns_on(self):
        self.start_shadow()
        self.shadow_rows(5)
        self.assertEqual(automation.set_mode(self.conn, USER, "test_shadow", "on", now=self.since + timedelta(hours=49)), "on")
        self.assertTrue(automation.is_enabled(self.conn, USER, "test_shadow"))

    def test_shadow_actions_from_before_shadow_restarted_do_not_count(self):
        self.start_shadow()
        self.shadow_rows(5)
        automation.set_mode(self.conn, USER, "test_shadow", "off")
        automation.set_mode(self.conn, USER, "test_shadow", "shadow", now=datetime.now(timezone.utc) + timedelta(minutes=1))
        allowed, reason = automation.can_turn_on(self.conn, USER, "test_shadow", now=datetime.now(timezone.utc) + timedelta(hours=60))
        self.assertEqual((allowed, reason), (False, "Needs at least 5 actions in shadow first (0 so far)"))

    def test_a_review_is_right_or_wrong_and_only_for_shadow_rows(self):
        automation.set_mode(self.conn, USER, "test_switch", "on")
        applied = self.act()
        with self.assertRaises(ValueError):
            automation.review(self.conn, applied["id"], USER, "right")
        automation.set_mode(self.conn, USER, "test_shadow", "shadow")
        shadow = self.act(feature="test_shadow", subject_id="s2")
        with self.assertRaises(ValueError):
            automation.review(self.conn, shadow["id"], USER, "maybe")
        self.assertEqual(automation.review(self.conn, shadow["id"], USER, "right")["review"], "right")


# --- The pause ----------------------------------------------------------------------------


class PauseTests(AutomationCase):
    def test_pause_and_resume(self):
        self.assertFalse(automation.paused(self.conn, USER))
        self.assertEqual(automation.set_paused(self.conn, USER, True), {"paused": True, "in_flight": []})
        self.assertTrue(automation.paused(self.conn, USER))
        self.assertTrue(automation.settings_payload(self.conn, USER)["paused"])
        self.assertEqual(automation.set_paused(self.conn, USER, False)["paused"], False)
        self.assertFalse(automation.paused(self.conn, USER))

    def test_the_pause_row_records_when_it_last_changed(self):
        automation.set_paused(self.conn, USER, True)
        first = self.conn.execute("SELECT updated_at FROM user_settings WHERE key='automation_paused'").fetchone()[0]
        automation.set_paused(self.conn, USER, True)
        again = self.conn.execute("SELECT updated_at FROM user_settings WHERE key='automation_paused'").fetchone()[0]
        self.assertEqual(first, again, "pausing twice is one pause")

    def test_only_a_real_pause_or_resume_moves_the_pause_time(self):
        def stamp():
            return self.conn.execute("SELECT updated_at FROM user_settings WHERE key='automation_paused'").fetchone()[0]

        with self.conn:
            automation.pause_guard(self.conn, USER)
        self.assertEqual(stamp(), schema.PAUSE_NEVER_CHANGED, "a row the guard made was never paused")
        automation.set_paused(self.conn, USER, False)
        self.assertEqual(stamp(), schema.PAUSE_NEVER_CHANGED, "resuming what was never paused changes nothing")
        with self.conn:
            self.conn.execute("DELETE FROM user_settings WHERE key='automation_paused'")
        automation.set_paused(self.conn, USER, False)
        self.assertEqual(stamp(), schema.PAUSE_NEVER_CHANGED, "nor does a resume that has to make the row")
        automation.set_paused(self.conn, USER, True)
        paused_at = stamp()
        self.assertGreater(datetime.fromisoformat(paused_at), T0)
        automation.set_paused(self.conn, USER, False)
        self.assertGreater(stamp(), paused_at, "a real resume is when the pause ended")

    def test_the_guard_makes_the_row_and_reads_it(self):
        with self.conn:
            self.assertFalse(automation.pause_guard(self.conn, USER))
        self.assertEqual(self.conn.execute("SELECT value FROM user_settings WHERE key='automation_paused'").fetchone()[0], "off")
        automation.set_paused(self.conn, USER, True)
        with self.conn:
            self.assertTrue(automation.pause_guard(self.conn, USER))

    def targets(self, *companies):
        now = utc_now()
        with self.conn:
            for number, company in enumerate(companies, start=1):
                self.conn.execute(
                    "INSERT INTO outreach_targets(id, user_id, company, created_at, updated_at) VALUES(?, ?, ?, ?, ?)",
                    (f"t-{number}", USER, company, now, now),
                )

    def claim(self, target_id, state, action, claimed_at=None, kind="initial"):
        with self.conn:
            self.conn.execute(
                "INSERT INTO outreach_send_claims(target_id, user_id, kind, token, state, action, instance, claimed_at) "
                "VALUES(?, ?, ?, 'tok', ?, ?, 'i', ?)", (target_id, USER, kind, state, action, claimed_at or utc_now()),
            )

    def test_in_flight_lists_what_is_past_stopping(self):
        now = utc_now()
        old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(timespec="microseconds")
        self.targets("Bovi", "Kiva", "Orbit", "Acme", "Zeta")
        with self.conn:
            self.conn.execute(
                "INSERT INTO outreach_scheduled_sends(target_id, user_id, kind, fingerprint, send_at, timezone, label, state, created_at, updated_at) "
                "VALUES('t-1', ?, 'initial', 'f', ?, 'UTC', 'Mon, Sep 28, 9:12 AM CDT', 'transmitting', ?, ?)",
                (USER, now, now, now),
            )
        self.claim("t-1", "sending", "send")
        self.claim("t-2", "clicking", "form")
        self.claim("t-3", "sending", "send", old)
        self.claim("t-4", "drafting", "draft")
        self.claim("t-5", "drafting", "form")
        flights = automation.set_paused(self.conn, USER, True)["in_flight"]
        self.assertEqual([(item["source"], item["company"], item["action"]) for item in flights],
                         [("scheduled_send", "Bovi", "send"), ("form_claim", "Kiva", "form")],
                         "one email handed over is listed once, a form being clicked is listed, a claim left long ago is not, "
                         "a Gmail draft being saved is not a send, and a form still being filled can still be stopped")
        self.assertEqual(flights[0]["label"], "Mon, Sep 28, 9:12 AM CDT")

    def test_the_paused_banner_names_what_could_not_be_stopped(self):
        self.targets("Bovi", "Kiva", "Orbit")
        automation.set_paused(self.conn, USER, True)

        def banner():
            [item] = [entry for entry in automation.health_summary(self.conn, USER)["banner"] if entry["key"] == "paused"]
            return item["text"]

        base = "Automation is paused. Nothing is sent and no switch acts on its own. Replies and bounces are still recorded."
        self.assertEqual(banner(), base)
        self.claim("t-1", "sending", "send")
        self.assertEqual(banner(), f"{base} 1 email was already handed to Gmail and can't be stopped.")
        self.claim("t-2", "sending", "send")
        self.assertEqual(banner(), f"{base} 2 emails were already handed to Gmail and can't be stopped.")
        self.claim("t-3", "clicking", "form")
        self.assertEqual(banner(), f"{base} 2 emails were already handed to Gmail and 1 contact form was already being sent, "
                                   "and neither can be stopped.")
        with self.conn:
            self.conn.execute("DELETE FROM outreach_send_claims WHERE action='send'")
        self.assertEqual(banner(), f"{base} 1 contact form was already being sent and can't be stopped.")
        self.claim("t-1", "drafting", "draft")
        self.assertEqual(banner(), f"{base} 1 contact form was already being sent and can't be stopped.", "a draft is not a send")

    def test_unconfirmed_claims_are_listed_until_the_student_settles_them(self):
        self.targets("Bovi", "Kiva", "Orbit", "Acme")
        old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(timespec="microseconds")
        self.claim("t-1", "unconfirmed", "send", kind="follow_up")
        self.claim("t-2", "unconfirmed", "form")
        self.claim("t-3", "clicking", "form", old)  # the app stopped while pressing the button
        self.claim("t-4", "sending", "send")  # still going: in flight, not unconfirmed
        with self.conn:
            self.conn.execute("UPDATE outreach_targets SET status='sent' WHERE id='t-1'")
        listed = automation.health_summary(self.conn, USER)["unconfirmed"]
        self.assertEqual([(item["target_id"], item["company"], item["kind"], item["action"]) for item in listed],
                         [("t-3", "Orbit", "initial", "form"), ("t-1", "Bovi", "follow_up", "send"), ("t-2", "Kiva", "initial", "form")],
                         "oldest first")
        self.assertTrue(all(item["at"] for item in listed))
        with self.conn:
            # "I sent it" on the form, and the company replied after the follow-up.
            self.conn.execute("UPDATE outreach_targets SET status='sent', sent_at=? WHERE id='t-2'", (utc_now(),))
            self.conn.execute("UPDATE outreach_targets SET status='replied' WHERE id='t-1'")
        self.assertEqual([item["target_id"] for item in automation.health_summary(self.conn, USER)["unconfirmed"]], ["t-3"])

    def test_settings_are_checked_and_written_in_one_transaction(self):
        with self.assertRaises(AutomationGateError):
            automation.apply_settings(self.conn, USER, modes={"auto_drafts": "on", "test_shadow": "on"}, paused=True)
        self.assertFalse(automation.paused(self.conn, USER), "a refused switch leaves no pause behind")
        self.assertEqual(automation.mode(self.conn, USER, "auto_drafts"), "off")
        real = automation.can_turn_on
        seen = []

        def gate(conn, *args, **kwargs):
            seen.append(conn.in_transaction)
            return real(conn, *args, **kwargs)

        automation.set_mode(self.conn, USER, "test_shadow", "shadow")
        with mock.patch.object(automation, "can_turn_on", gate), self.assertRaises(AutomationGateError):
            automation.apply_settings(self.conn, USER, modes={"test_shadow": "on"}, paused=True)
        self.assertEqual(seen, [True], "the gate is checked inside the transaction that would write, so nothing changes in between")
        applied = automation.apply_settings(self.conn, USER, modes={"auto_drafts": "on"}, paused=True)
        self.assertEqual(applied, {"paused": True, "in_flight": []})
        self.assertEqual((automation.paused(self.conn, USER), automation.mode(self.conn, USER, "auto_drafts")), (True, "on"))
        self.assertEqual(automation.apply_settings(self.conn, USER, modes={"auto_drafts": "off"}, paused=None),
                         {"paused": True, "in_flight": None}, "in_flight is read only when the pause was asked about")
        with self.assertRaisesRegex(ValueError, "Unknown automation feature"):
            automation.apply_settings(self.conn, USER, modes={"autopilot": "on"}, paused=False)
        self.assertTrue(automation.paused(self.conn, USER))


# --- perform --------------------------------------------------------------------------------


class PerformTests(AutomationCase):
    def test_off_records_nothing(self):
        self.assertIsNone(self.act())
        self.assertIsNone(self.flag())
        self.assertEqual(automation.list_actions(self.conn, USER), [])

    def test_paused_records_nothing(self):
        automation.set_mode(self.conn, USER, "test_switch", "on")
        automation.set_paused(self.conn, USER, True)
        self.assertIsNone(self.act())
        self.assertIsNone(self.flag())

    def test_on_and_automatic_applies_and_records(self):
        automation.set_mode(self.conn, USER, "test_switch", "on")
        row = self.act()
        self.assertEqual((row["status"], row["decided_by"], row["feature"]), ("applied", "system", "test_switch"))
        self.assertTrue(row["applied_at"])
        self.assertEqual(self.flag(), "up")
        self.assertEqual((row["fields"], row["before"]), (["flag"], {"flag": None}))
        self.assertEqual(row["after"], {"flag": "up", "_result": {"source": f"automation:{row['id']}"}},
                         "the change names the action that made it")
        self.assertEqual((row["evidence"], row["basis"], row["confidence"]), ({"message_id": "m-1"}, "rule:test", 0.9))

    def test_not_automatic_proposes(self):
        automation.set_mode(self.conn, USER, "test_switch", "on")
        row = self.act(auto=False)
        self.assertEqual((row["status"], row["decided_by"], row["applied_at"]), ("proposed", "", None))
        self.assertIsNone(self.flag())

    def test_shadow_records_what_it_would_do_and_changes_nothing(self):
        automation.set_mode(self.conn, USER, "test_shadow", "shadow")
        row = self.act(feature="test_shadow")
        self.assertEqual((row["status"], row["after"]), ("shadow", {"flag": "up"}))
        self.assertIsNone(self.flag())

    def test_the_same_key_returns_the_first_row_whatever_became_of_it(self):
        automation.set_mode(self.conn, USER, "test_switch", "on")
        first = self.act(key="gmail:msg-1:s1:test.flag")
        automation.undo(self.conn, first["id"], USER)
        again = self.act(key="gmail:msg-1:s1:test.flag", after={"flag": "down"})
        self.assertEqual((again["id"], again["status"]), (first["id"], "undone"))
        self.assertIsNone(self.flag(), "a re-read of the same evidence never acts twice")
        self.assertEqual(len(automation.list_actions(self.conn, USER)), 1)

    def test_a_change_already_in_place_records_nothing(self):
        automation.set_mode(self.conn, USER, "test_switch", "on")
        self.act()
        self.assertIsNone(self.act())
        self.assertEqual(len(automation.list_actions(self.conn, USER)), 1)

    def test_two_passes_racing_on_one_key_do_not_crash(self):
        automation.set_mode(self.conn, USER, "test_switch", "on")
        first = self.act(key="race")
        real = automation._by_key
        calls = []

        def racing(conn, user_id, key):
            # The other pass had not committed when this one looked.
            calls.append(key)
            return None if len(calls) == 1 else real(conn, user_id, key)

        with mock.patch.object(automation, "_by_key", racing):
            second = self.act(key="race", subject_id="s2")
        self.assertEqual(second["id"], first["id"])
        self.assertIsNone(self.flag("s2"), "the losing pass's change was rolled back with its row")

    def test_a_pause_or_an_edit_cannot_land_between_its_reads_and_the_change(self):
        automation.set_mode(self.conn, USER, "test_switch", "on")
        patch, seen = self.meanwhile(
            automation.ApplicationStage, "read",
            ("edit", lambda other: update_application(other, "app-job-b", stage="offer", user_id=USER)["stage"]),
            ("pause", lambda other: automation.set_paused(other, USER, True)),
        )
        with patch:
            row = self.act(action_type="application.stage", subject_kind="application", subject_id="app-job-b", after={"stage": "interview"})
        self.assertTrue(seen["in_transaction"], "the fields are read inside the transaction that changes them")
        self.assertEqual((seen["edit"], seen["pause"]), ("database is locked", "database is locked"),
                         "the student's edit and pause wait for the change instead of landing between the read and the write")
        self.assertEqual((row["status"], row["before"]["stage"]), ("applied", "applied"), "before is what the change replaced")
        # Made now, the edit lands after the change, and an undo then leaves it alone.
        update_application(self.conn, "app-job-b", stage="offer", user_id=USER)
        with self.assertRaises(Superseded):
            automation.undo(self.conn, row["id"], USER)
        self.assertEqual(self.stage("app-job-b")[0], "offer")

    def test_unknown_features_and_action_types_are_refused(self):
        with self.assertRaises(ValueError):
            self.act(feature="autopilot")
        with self.assertRaises(ValueError):
            self.act(action_type="sources.add_board")

    def test_list_actions_filters_and_orders_newest_first(self):
        automation.set_mode(self.conn, USER, "test_switch", "on")
        first = self.act(subject_id="s1")
        second = self.act(subject_id="s2", auto=False)
        self.assertEqual([row["id"] for row in automation.list_actions(self.conn, USER)], [second["id"], first["id"]])
        self.assertEqual([row["id"] for row in automation.list_actions(self.conn, USER, status="proposed")], [second["id"]])
        self.assertEqual(automation.list_actions(self.conn, USER, feature="auto_drafts"), [])
        with self.assertRaises(ValueError):
            automation.list_actions(self.conn, USER, status="done")

    def test_list_actions_takes_several_statuses(self):
        automation.set_mode(self.conn, USER, "test_switch", "on")
        automation.set_mode(self.conn, USER, "test_shadow", "shadow")
        applied = self.act(subject_id="s1")
        proposed = self.act(subject_id="s2", auto=False)
        shadow = self.act(feature="test_shadow", subject_id="s3")
        for status in (["applied", "shadow"], ("applied", "shadow"), "applied,shadow", " applied , shadow ,"):
            with self.subTest(status=status):
                self.assertEqual([row["id"] for row in automation.list_actions(self.conn, USER, status=status)], [shadow["id"], applied["id"]])
                self.assertEqual(automation.count_actions(self.conn, USER, status=status), 2)
        self.assertEqual([row["id"] for row in automation.list_actions(self.conn, USER, status="proposed")], [proposed["id"]])
        self.assertEqual(automation.count_actions(self.conn, USER), 3)
        for bad in ("applied,done", ",", []):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                automation.list_actions(self.conn, USER, status=bad)


class AtomicityTests(AutomationCase):
    """The change and its ledger row are one transaction: a failed ledger insert takes the change back."""

    def failing_insert(self, look):
        seen = {}

        def fail(conn, values):
            seen["during"] = look()  # the change was made, inside the transaction
            raise RuntimeError("disk full")

        return mock.patch.object(automation, "_insert_action", fail), seen

    def test_a_stage_change_is_rolled_back(self):
        automation.set_mode(self.conn, USER, "test_switch", "on")
        before = self.stage("app-job-b")
        events = len(self.events("app-job-b"))
        patch, seen = self.failing_insert(lambda: self.stage("app-job-b"))
        with patch, self.assertRaisesRegex(RuntimeError, "disk full"):
            self.act(action_type="application.stage", subject_kind="application", subject_id="app-job-b", after={"stage": "interview"})
        self.assertEqual(seen["during"][0], "interview", "the instrument saw the change before the failure")
        self.assertEqual(self.stage("app-job-b"), before)
        self.assertEqual(len(self.events("app-job-b")), events)
        self.assertEqual(automation.list_actions(self.conn, USER), [])

    def test_an_intent_is_rolled_back(self):
        automation.set_mode(self.conn, USER, "test_switch", "on")
        count = lambda: self.conn.execute("SELECT COUNT(*) FROM opportunity_interactions").fetchone()[0]  # noqa: E731
        interactions = count()
        patch, seen = self.failing_insert(count)
        with patch, self.assertRaises(RuntimeError):
            self.act(action_type="opportunity.intent", subject_kind="opportunity", subject_id="job-b", after={"intent": "saved"})
        self.assertEqual(seen["during"], interactions + 1)
        self.assertEqual(count(), interactions)


# --- The transaction refactor in actions.py -----------------------------------------------------


class ActionsTests(AutomationCase):
    def test_the_public_functions_still_commit_on_their_own(self):
        other = self.other_connection()
        response = record_intent(self.conn, "job-b", "passed", user_id=USER, idempotency_key="k-1")
        self.assertEqual(set(response), {"opportunity_id", "action", "application_id", "created_at", "replayed", "unchanged"})
        self.assertEqual(other.execute("SELECT action FROM opportunity_interactions WHERE opportunity_id='job-b'").fetchone()[0], "passed")
        self.assertTrue(record_intent(self.conn, "job-b", "passed", user_id=USER, idempotency_key="k-1")["replayed"])
        self.assertTrue(record_intent(self.conn, "job-b", "passed", user_id=USER)["unchanged"])
        update_application(self.conn, "app-job-b", stage="interview", notes="Phone screen", user_id=USER)
        self.assertEqual(tuple(other.execute("SELECT stage, notes FROM applications WHERE id='app-job-b'").fetchone()), ("interview", "Phone screen"))
        task = add_application_task(self.conn, "app-job-b", title="Send thanks", due_at="2026-10-01T09:00", user_id=USER, timezone_name="America/Chicago")
        self.assertEqual(tuple(other.execute("SELECT title, due_at FROM application_tasks WHERE id=?", (task["id"],)).fetchone()),
                         ("Send thanks", "2026-10-01T09:00:00-05:00"))

    def test_the_tx_functions_leave_the_commit_to_the_caller(self):
        record_intent_tx(self.conn, "job-b", "saved", user_id=USER)
        update_application_tx(self.conn, "app-job-b", stage="offer", user_id=USER)
        add_application_task_tx(self.conn, "app-job-b", title="Reply to offer", due_at=None, user_id=USER)
        self.assertTrue(self.conn.in_transaction)
        self.conn.rollback()
        self.assertEqual(self.stage("app-job-b")[0], "applied")
        self.assertIsNone(self.conn.execute("SELECT 1 FROM application_tasks").fetchone())
        self.assertIsNone(self.conn.execute("SELECT 1 FROM opportunity_interactions WHERE opportunity_id='job-b'").fetchone())

    def test_import_applications_behaves_as_before(self):
        result = import_applications(self.conn, [
            {"opportunity_id": "job-a", "stage": "applied", "notes": "Imported"},
            {"opportunity_id": "job-b", "stage": "interview", "follow_up_at": "2026-10-02"},
            {"opportunity_id": "nope", "stage": "applied"},
        ], user_id=USER)
        self.assertEqual((result["imported"], result["skipped"]), (2, 1))
        self.assertEqual(result["errors"], [{"row": 3, "detail": "Opportunity not found: nope"}])
        other = self.other_connection()
        self.assertEqual(tuple(other.execute("SELECT stage, notes FROM applications WHERE id='app-job-a'").fetchone()), ("applied", "Imported"))
        self.assertTrue(other.execute("SELECT applied_at FROM applications WHERE id='app-job-a'").fetchone()[0])
        sources = {event[3].get("source") for event in self.events("app-job-a", "stage_changed")}
        self.assertEqual(sources, {"application_import"})

    def test_an_earlier_applied_date_wins_and_a_later_one_is_ignored(self):
        stored = self.stage("app-job-b")[1]
        update_application(self.conn, "app-job-b", stage="interview", applied_at="2026-08-20T00:00:00Z", user_id=USER)
        self.assertEqual(self.stage("app-job-b"), ("interview", stored), "a later date does not replace an earlier one")
        update_application(self.conn, "app-job-b", applied_at="2026-08-01T10:00:00-05:00", user_id=USER)
        self.assertEqual(self.stage("app-job-b")[1], "2026-08-01T15:00:00.000000+00:00")

    def test_an_applied_date_counts_only_once_applied_and_must_be_aware(self):
        application = self.applying()
        update_application(self.conn, application, notes="Draft", applied_at="2026-08-01T10:00:00Z", user_id=USER)
        self.assertEqual(self.stage(application), ("applying", None), "not applied yet, so no applied date")
        for bad in ("yesterday", "2026-08-01T10:00:00", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                update_application(self.conn, application, stage="applied", applied_at=bad, user_id=USER)
        self.assertEqual(self.stage(application), ("applying", None), "a refused value changes nothing")
        update_application(self.conn, application, stage="applied", applied_at="2026-08-01T10:00:00Z", user_id=USER)
        self.assertEqual(self.stage(application), ("applied", "2026-08-01T10:00:00.000000+00:00"))

    def test_without_an_applied_date_reaching_applied_records_now(self):
        application = self.applying()
        before = datetime.now(timezone.utc)
        update_application(self.conn, application, stage="applied", user_id=USER)
        self.assertGreaterEqual(datetime.fromisoformat(self.stage(application)[1]), before)

    def test_intent_source_and_task_origin_are_stored(self):
        record_intent(self.conn, "job-b", "saved", user_id=USER)
        record_intent(self.conn, "job-b", "passed", user_id=USER, source="automation:auto-1")
        rows = self.conn.execute("SELECT action, source FROM opportunity_interactions WHERE opportunity_id='job-b' ORDER BY id").fetchall()
        self.assertEqual([tuple(row) for row in rows], [("saved", "user"), ("passed", "automation:auto-1")])
        mine = add_application_task(self.conn, "app-job-b", title="Mine", user_id=USER)
        theirs = add_application_task(self.conn, "app-job-b", title="From mail", user_id=USER, origin="application_mail", origin_ref="gmail:abc")
        self.assertEqual((mine["origin"], mine["origin_ref"]), ("user", ""))
        self.assertEqual((theirs["origin"], theirs["origin_ref"]), ("application_mail", "gmail:abc"))
        self.assertEqual(self.events("app-job-b", "task_added")[0][3], {"task_id": mine["id"], "title": "Mine"}, "the student's own event is unchanged")

    def test_a_new_applied_date_alone_is_on_the_timeline_with_its_source(self):
        automation.set_mode(self.conn, USER, "test_switch", "on")
        row = self.act(action_type="application.stage", subject_kind="application", subject_id="app-job-b",
                       after={"stage": "applied", "applied_at": "2026-01-01T00:00:00Z"})
        self.assertEqual(self.stage("app-job-b"), ("applied", "2026-01-01T00:00:00.000000+00:00"))
        self.assertEqual(self.events("app-job-b", "application_updated"),
                         [("application_updated", None, None, {"source": f"automation:{row['id']}", "fields": ["applied_at"]})],
                         "the automatic backdating is attributed, as its undo is")
        automation.undo(self.conn, row["id"], USER)
        self.assertEqual([event[3] for event in self.events("app-job-b", "application_updated")][-1],
                         {"source": f"automation-undo:{row['id']}", "fields": ["applied_at"]})
        update_application(self.conn, "app-job-b", notes="Same stage", applied_at="2025-12-01T00:00:00Z", user_id=USER)
        self.assertEqual(self.events("app-job-b", "application_updated")[-1][3], {"source": "user", "fields": ["notes", "applied_at"]})
        stage_changes = len(self.events("app-job-b", "stage_changed"))
        update_application(self.conn, "app-job-b", stage="interview", applied_at="2025-11-01T00:00:00Z", user_id=USER)
        self.assertEqual(len(self.events("app-job-b", "stage_changed")), stage_changes + 1)
        self.assertEqual(len(self.events("app-job-b", "application_updated")), 3, "a stage change's own event covers its date")

    def test_a_students_edit_reads_the_row_under_the_write_lock(self):
        """F22: a notes-only edit cannot write back a stage read before an automatic change committed."""
        automation.set_mode(self.conn, USER, "test_switch", "on")
        application = self.applying()
        real = actions_module.applied_at_for_stage
        seen = {}

        def between_read_and_write(*args, **kwargs):
            # Called after the student's transaction read the row, before it writes.
            if "perform" not in seen:
                seen["in_transaction"] = self.conn.in_transaction
                try:
                    seen["perform"] = self.act(action_type="application.stage", subject_kind="application",
                                               subject_id=application, after={"stage": "interview"},
                                               key="race", conn=self.impatient_connection())["status"]
                except sqlite3.OperationalError as exc:
                    seen["perform"] = str(exc)
            return real(*args, **kwargs)

        with mock.patch.object(actions_module, "applied_at_for_stage", between_read_and_write):
            update_application(self.conn, application, notes="Called the recruiter", user_id=USER)
        self.assertTrue(seen["in_transaction"], "the row is read inside the transaction that writes it")
        self.assertEqual(seen["perform"], "database is locked", "the automatic change waits instead of landing in between")
        row = self.act(action_type="application.stage", subject_kind="application", subject_id=application,
                       after={"stage": "interview"}, key="race")
        self.assertEqual(row["status"], "applied")
        detail = self.conn.execute("SELECT stage, notes FROM applications WHERE id=?", (application,)).fetchone()
        self.assertEqual((detail["stage"], detail["notes"]), ("interview", "Called the recruiter"), "neither change is lost")

    def test_a_task_from_an_approved_agent_proposal_is_labelled_as_the_agents(self):
        stamp = utc_now()
        with self.conn:
            self.conn.execute("INSERT INTO agent_threads(id, user_id, title, created_at, updated_at) VALUES('thread-1', ?, 'T', ?, ?)",
                              (USER, stamp, stamp))
            self.conn.execute(
                """INSERT INTO agent_proposed_actions(id, thread_id, user_id, action_type, scope, input_json, expected_effect, created_at)
                   VALUES('proposal-1', 'thread-1', ?, 'add_application_task', 'application', ?, 'Adds a task', ?)""",
                (USER, json.dumps({"application_id": "app-job-b", "title": "Email the recruiter Friday"}), stamp),
            )
        self.assertEqual(decide_proposal(self.conn, "proposal-1", "approve", user_id=USER)["status"], "approved")
        task = self.conn.execute("SELECT origin, origin_ref FROM application_tasks WHERE title='Email the recruiter Friday'").fetchone()
        self.assertEqual((task["origin"], task["origin_ref"]), ("agent", "proposal-1"))
        self.assertEqual(self.events("app-job-b", "task_added")[-1][3]["source"], "agent_proposal:proposal-1",
                         "shown as the agent's, like its stage changes, not as the student's own")


# --- Undo, approve, reject -------------------------------------------------------------------


class UndoTests(AutomationCase):
    def setUp(self):
        super().setUp()
        automation.set_mode(self.conn, USER, "test_switch", "on")

    def stage_action(self, application_id, **after):
        return self.act(action_type="application.stage", subject_kind="application", subject_id=application_id, after=after)

    def test_a_stage_change_is_undone_with_its_applied_date(self):
        application = self.applying()
        row = self.stage_action(application, stage="applied")
        self.assertEqual(self.stage(application)[0], "applied")
        self.assertEqual(row["after"]["applied_at"], self.stage(application)[1], "the ledger holds exactly what was written")
        undone = automation.undo(self.conn, row["id"], USER)
        self.assertEqual((undone["status"], undone["decided_by"], undone["feature_paused"]), ("undone", "student", False))
        self.assertEqual(self.stage(application), ("applying", None))
        changes = [(event[1], event[2], event[3]["source"]) for event in self.events(application, "stage_changed")]
        self.assertEqual(changes, [("applying", "applied", f"automation:{row['id']}"), ("applied", "applying", f"automation-undo:{row['id']}")])

    def test_a_stage_the_student_changed_since_is_not_undone(self):
        row = self.stage_action("app-job-b", stage="interview")
        update_application(self.conn, "app-job-b", stage="offer", user_id=USER)
        with self.assertRaises(Superseded) as raised:
            automation.undo(self.conn, row["id"], USER)
        self.assertIn("The stage changed", str(raised.exception))
        self.assertEqual(self.stage("app-job-b")[0], "offer")
        stored = automation.list_actions(self.conn, USER)[0]
        self.assertEqual(stored["status"], "superseded")
        self.assertIn("stage", stored["note"])
        with self.assertRaises(ValueError):
            automation.undo(self.conn, row["id"], USER)

    def test_notes_written_since_do_not_block_an_undo(self):
        row = self.stage_action("app-job-b", stage="interview")
        update_application(self.conn, "app-job-b", notes="Talked to the recruiter", user_id=USER)
        automation.undo(self.conn, row["id"], USER)
        detail = self.conn.execute("SELECT stage, notes FROM applications WHERE id='app-job-b'").fetchone()
        self.assertEqual(tuple(detail), ("applied", "Talked to the recruiter"))

    def intent(self, value, opportunity="job-b"):
        return self.act(action_type="opportunity.intent", subject_kind="opportunity", subject_id=opportunity, after={"intent": value})

    def current_intent(self, opportunity="job-b"):
        return automation.HANDLERS["opportunity.intent"].read(self.conn, USER, opportunity)["intent"]

    def test_an_intent_is_undone_by_restoring_the_earlier_choice(self):
        row = self.intent("saved")
        source = self.conn.execute("SELECT source FROM opportunity_interactions WHERE id=?", (row["after"]["_result"]["interaction_id"],)).fetchone()[0]
        self.assertEqual(source, f"automation:{row['id']}")
        automation.undo(self.conn, row["id"], USER)
        self.assertEqual(self.current_intent(), "")
        latest = self.conn.execute("SELECT action, source FROM opportunity_interactions WHERE opportunity_id='job-b' ORDER BY id DESC").fetchone()
        self.assertEqual(tuple(latest), ("undo", f"automation-undo:{row['id']}"))

    def test_an_intent_the_student_changed_since_is_not_undone(self):
        row = self.intent("saved")
        record_intent(self.conn, "job-b", "passed", user_id=USER)
        with self.assertRaises(Superseded):
            automation.undo(self.conn, row["id"], USER)
        self.assertEqual(self.current_intent(), "passed")

    def test_the_student_acting_on_another_opportunity_does_not_block_an_undo(self):
        row = self.intent("passed")
        record_intent(self.conn, "job-a", "passed", user_id=USER)
        automation.undo(self.conn, row["id"], USER)
        self.assertEqual((self.current_intent(), self.current_intent("job-a")), ("", "passed"))

    def task(self, **task):
        return self.act(action_type="application.task", subject_kind="application", subject_id="app-job-b", after={"task": {
            "title": "Send a thank-you note", "due_at": "2026-10-01T09:00:00-05:00", "origin": "application_mail",
            "origin_ref": "gmail:abc", **task,
        }})

    def test_a_task_is_undone_by_deleting_it(self):
        row = self.task()
        created = self.conn.execute("SELECT * FROM application_tasks WHERE id=?", (row["after"]["_result"]["task_id"],)).fetchone()
        self.assertEqual((created["origin"], created["origin_ref"], created["status"]), ("application_mail", "gmail:abc", "open"))
        self.assertEqual(self.events("app-job-b", "task_added")[0][3]["source"], f"automation:{row['id']}")
        automation.undo(self.conn, row["id"], USER)
        self.assertIsNone(self.conn.execute("SELECT 1 FROM application_tasks WHERE id=?", (created["id"],)).fetchone())
        removed = self.events("app-job-b", "task_removed")[0][3]
        self.assertEqual((removed["task_id"], removed["source"]), (created["id"], f"automation-undo:{row['id']}"))

    def test_a_task_the_student_finished_or_edited_stays(self):
        done = self.task(title="Finished")
        update_application_task(self.conn, done["after"]["_result"]["task_id"], status="done", user_id=USER)
        with self.assertRaisesRegex(Superseded, "marked done"):
            automation.undo(self.conn, done["id"], USER)
        edited = self.task(title="Edited")
        with self.conn:
            self.conn.execute("UPDATE application_tasks SET title='Edited by me' WHERE id=?", (edited["after"]["_result"]["task_id"],))
        with self.assertRaisesRegex(Superseded, "edited"):
            automation.undo(self.conn, edited["id"], USER)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM application_tasks").fetchone()[0], 2)

    def test_a_task_undo_ignores_unrelated_changes(self):
        row = self.task()
        update_application(self.conn, "app-job-b", notes="Unrelated", user_id=USER)
        self.assertEqual(automation.undo(self.conn, row["id"], USER)["status"], "undone")

    def test_a_second_undo_while_the_first_is_under_way_is_refused_plainly(self):
        row = self.stage_action("app-job-b", stage="interview")
        patch, seen = self.meanwhile(
            automation.ApplicationStage, "undo", ("undo", lambda other: automation.undo(other, row["id"], USER)["status"]),
        )
        with patch:
            undone = automation.undo(self.conn, row["id"], USER)
        self.assertEqual((seen["undo"], undone["status"]), ("database is locked", "undone"), "the second waits for the first")
        with self.assertRaisesRegex(ValueError, "this one is undone"):
            automation.undo(self.conn, row["id"], USER)
        self.assertEqual(self.stage("app-job-b")[0], "applied")

    def reminder(self, application_id="app-job-b"):
        row = self.conn.execute(
            "SELECT status, due_at, updated_at FROM reminders WHERE application_id=? AND reminder_type='follow_up'", (application_id,),
        ).fetchone()
        return None if row is None else (row["status"], row["due_at"])

    def with_follow_up(self, when):
        update_application(self.conn, "app-job-b", follow_up_at=when.isoformat(), user_id=USER)
        self.assertEqual(self.reminder()[0], "scheduled")
        row = self.stage_action("app-job-b", stage="rejected")
        self.assertEqual(self.reminder()[0], "cancelled", "the instrument: a move to a closed stage cancels the reminder")
        return row

    def test_undoing_a_move_to_a_closed_stage_schedules_the_follow_up_reminder_again(self):
        soon = datetime.now(timezone.utc) + timedelta(days=5)
        row = self.with_follow_up(soon)
        undone = automation.undo(self.conn, row["id"], USER)
        self.assertEqual(self.stage("app-job-b")[0], "applied")
        self.assertEqual(self.reminder(), ("scheduled", soon.isoformat()))
        self.assertEqual((undone["reminder_restored"], undone["undo_note"]), (True, "Your follow-up reminder is scheduled again."))

    def test_a_follow_up_date_that_has_passed_is_not_restored_and_the_undo_says_so(self):
        row = self.with_follow_up(datetime.now(timezone.utc) + timedelta(days=5))
        with self.conn:
            self.conn.execute("UPDATE applications SET follow_up_at=? WHERE id='app-job-b'",
                              ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),))
        undone = automation.undo(self.conn, row["id"], USER)
        self.assertEqual((undone["status"], undone["reminder_restored"]), ("undone", False))
        self.assertIn("its date has passed", undone["undo_note"])
        self.assertEqual(self.reminder()[0], "cancelled")

    def test_a_reminder_changed_since_is_left_alone_and_the_undo_says_so(self):
        row = self.with_follow_up(datetime.now(timezone.utc) + timedelta(days=5))
        with self.conn:
            self.conn.execute("UPDATE reminders SET updated_at=? WHERE application_id='app-job-b'", (utc_now(),))
        undone = automation.undo(self.conn, row["id"], USER)
        self.assertEqual(undone["reminder_restored"], False)
        self.assertIn("changed since", undone["undo_note"])
        self.assertEqual(self.reminder()[0], "cancelled")
        # A move that cancelled no reminder says nothing about one.
        other = self.stage_action("app-job-b", stage="interview")
        self.assertNotIn("undo_note", automation.undo(self.conn, other["id"], USER))

    def test_only_an_applied_action_can_be_undone(self):
        proposed = self.act(auto=False)
        with self.assertRaises(ValueError):
            automation.undo(self.conn, proposed["id"], USER)
        with self.assertRaises(LookupError):
            automation.undo(self.conn, "auto-missing", USER)


class ApproveRejectTests(AutomationCase):
    def setUp(self):
        super().setUp()
        automation.set_mode(self.conn, USER, "test_switch", "on")

    def propose(self, stage="interview"):
        return self.act(action_type="application.stage", subject_kind="application", subject_id="app-job-b",
                        after={"stage": stage}, auto=False)

    def test_approve_applies_as_the_student(self):
        row = self.propose()
        self.assertEqual(self.stage("app-job-b")[0], "applied", "a proposal changes nothing")
        approved = automation.approve(self.conn, row["id"], USER)
        self.assertEqual((approved["status"], approved["decided_by"]), ("applied", "student"))
        self.assertTrue(approved["decided_at"] and approved["applied_at"])
        self.assertEqual(self.stage("app-job-b")[0], "interview")
        self.assertEqual(self.events("app-job-b", "stage_changed")[-1][3], {"source": f"automation:{row['id']}"})
        automation.undo(self.conn, row["id"], USER)
        self.assertEqual(self.stage("app-job-b")[0], "applied", "an approved action can be undone like any other")

    def test_approve_refuses_when_the_fields_changed_after_the_proposal(self):
        row = self.propose()
        update_application(self.conn, "app-job-b", stage="offer", user_id=USER)
        with self.assertRaisesRegex(Superseded, "proposed"):
            automation.approve(self.conn, row["id"], USER)
        self.assertEqual(self.stage("app-job-b")[0], "offer")
        self.assertEqual(automation.list_actions(self.conn, USER)[0]["status"], "superseded")

    def test_a_second_approve_while_the_first_is_under_way_adds_nothing(self):
        row = self.act(action_type="application.task", subject_kind="application", subject_id="app-job-b",
                       after={"task": {"title": "Reply"}}, auto=False)
        patch, seen = self.meanwhile(
            automation.ApplicationTask, "read", ("approve", lambda other: automation.approve(other, row["id"], USER)["status"]),
        )
        with patch:
            approved = automation.approve(self.conn, row["id"], USER)
        self.assertEqual(seen["approve"], "database is locked", "a double click waits for the first approve")
        tasks = self.conn.execute("SELECT id FROM application_tasks WHERE application_id='app-job-b' AND title='Reply'").fetchall()
        self.assertEqual([task["id"] for task in tasks], [approved["after"]["_result"]["task_id"]], "one task, the one undo removes")
        with self.assertRaisesRegex(ValueError, "this one is applied"):
            automation.approve(self.conn, row["id"], USER)

    def test_a_reject_while_approve_is_under_way_waits_and_then_finds_it_decided(self):
        row = self.propose()
        patch, seen = self.meanwhile(
            automation.ApplicationStage, "read", ("reject", lambda other: automation.reject(other, row["id"], USER)["status"]),
        )
        with patch:
            approved = automation.approve(self.conn, row["id"], USER)
        self.assertEqual(seen["reject"], "database is locked")
        self.assertEqual((approved["status"], self.stage("app-job-b")[0]), ("applied", "interview"))
        with self.assertRaisesRegex(ValueError, "this one is applied"):
            automation.reject(self.conn, row["id"], USER)

    def test_an_approve_that_finds_the_action_decided_at_the_end_takes_its_change_back(self):
        row = self.propose()
        real = automation.ApplicationStage.apply

        def apply(handler, conn, *args, **kwargs):
            result = real(handler, conn, *args, **kwargs)
            # What a decision that slipped past the claim would leave: the action no longer proposed.
            conn.execute("UPDATE automation_actions SET status='rejected' WHERE id=?", (row["id"],))
            return result

        with mock.patch.object(automation.ApplicationStage, "apply", apply), self.assertRaisesRegex(ValueError, "decided somewhere else"):
            automation.approve(self.conn, row["id"], USER)
        self.assertEqual(self.stage("app-job-b")[0], "applied", "the change went back with the ledger write")
        self.assertEqual(automation.list_actions(self.conn, USER)[0]["status"], "proposed")

    def test_reject_changes_nothing(self):
        row = self.propose()
        rejected = automation.reject(self.conn, row["id"], USER)
        self.assertEqual((rejected["status"], rejected["decided_by"], rejected["feature_paused"]), ("rejected", "student", False))
        self.assertEqual(self.stage("app-job-b")[0], "applied")
        with self.assertRaises(ValueError):
            automation.approve(self.conn, row["id"], USER)
        with self.assertRaises(ValueError):
            automation.reject(self.conn, row["id"], USER)


class BreakerTests(AutomationCase):
    def notices(self):
        return automation.list_notices(self.conn, USER)

    def test_two_undos_in_the_last_five_turn_the_feature_off(self):
        automation.set_mode(self.conn, USER, "test_switch", "on")
        rows = [self.act(subject_id=f"s{n}") for n in range(5)]
        self.assertFalse(automation.undo(self.conn, rows[0]["id"], USER)["feature_paused"])
        self.assertEqual(automation.mode(self.conn, USER, "test_switch"), "on")
        second = automation.undo(self.conn, rows[3]["id"], USER)
        self.assertTrue(second["feature_paused"])
        self.assertEqual(automation.mode(self.conn, USER, "test_switch"), "off")
        [notice] = self.notices()
        self.assertEqual((notice["level"], notice["event_key"]), ("warning", f"breaker:test_switch:{rows[3]['id']}"))
        self.assertEqual(notice["title"], "Turned off Test switch: you undid or rejected 2 of its last 5 actions")
        self.assertEqual(second["breaker_notice"], {"title": notice["title"], "body": notice["body"]},
                         "the result carries the notice's own words, so the app need not guess them")
        self.assertIsNone(self.act(subject_id="s9"), "off now, so nothing more happens")

    def test_the_result_says_no_notice_when_the_breaker_did_not_trip(self):
        automation.set_mode(self.conn, USER, "test_switch", "on")
        applied = self.act(subject_id="s1")
        proposed = self.act(subject_id="s2", auto=False)
        self.assertIsNone(automation.undo(self.conn, applied["id"], USER)["breaker_notice"])
        rejected = automation.reject(self.conn, proposed["id"], USER)
        self.assertTrue(rejected["feature_paused"])
        self.assertEqual(rejected["breaker_notice"]["title"], "Turned off Test switch: you undid or rejected 2 of its last 2 actions")

    def test_only_the_last_five_actions_count(self):
        automation.set_mode(self.conn, USER, "test_switch", "on")
        rows = [self.act(subject_id=f"s{n}") for n in range(7)]
        # Oldest first: rows[0] and rows[1] are outside the newest five.
        self.assertIsNone(automation.undo(self.conn, rows[0]["id"], USER)["breaker_notice"])
        undone = automation.undo(self.conn, rows[6]["id"], USER)
        self.assertEqual((undone["feature_paused"], automation.mode(self.conn, USER, "test_switch")), (False, "on"),
                         "an undo of an action that has fallen out of the window does not count")
        self.assertEqual(self.notices(), [])

    def test_a_superseded_undo_is_not_the_student_taking_it_back(self):
        automation.set_mode(self.conn, USER, "test_switch", "on")
        first, second = self.act(subject_id="s1"), self.act(subject_id="s2")
        with self.conn:
            self.conn.execute("UPDATE user_settings SET value='changed by hand' WHERE key='test.flag.s1'")
        with self.assertRaises(Superseded):
            automation.undo(self.conn, first["id"], USER)
        self.assertFalse(automation.undo(self.conn, second["id"], USER)["feature_paused"])
        self.assertEqual(automation.mode(self.conn, USER, "test_switch"), "on")
        with self.conn:
            self.conn.execute("UPDATE automation_actions SET decided_by='system' WHERE id=?", (second["id"],))
        third = self.act(subject_id="s3")
        self.assertFalse(automation.undo(self.conn, third["id"], USER)["feature_paused"],
                         "an action not decided by the student does not count either")

    def test_two_rejects_trip_it_too(self):
        automation.set_mode(self.conn, USER, "test_switch", "on")
        rows = [self.act(subject_id=f"s{n}", auto=False) for n in range(3)]
        automation.reject(self.conn, rows[0]["id"], USER)
        self.assertTrue(automation.reject(self.conn, rows[1]["id"], USER)["feature_paused"])
        self.assertEqual(automation.mode(self.conn, USER, "test_switch"), "off")

    def test_shadow_reviews_never_trip_it(self):
        automation.set_mode(self.conn, USER, "test_shadow", "shadow")
        for n in range(5):
            automation.review(self.conn, self.act(feature="test_shadow", subject_id=f"s{n}")["id"], USER, "wrong")
        self.assertEqual(automation.mode(self.conn, USER, "test_shadow"), "shadow")
        self.assertEqual(self.notices(), [])


# --- Notices and health ----------------------------------------------------------------------


class NoticeTests(AutomationCase):
    def test_a_notice_is_left_once_per_event(self):
        self.assertTrue(automation.notice(self.conn, USER, event_key="gmail-expired:1", level="problem", title="Gmail needs reconnecting"))
        self.assertFalse(automation.notice(self.conn, USER, event_key="gmail-expired:1", level="problem", title="Gmail needs reconnecting"))
        automation.notice(self.conn, USER, event_key="other", level="info", title="Something else")
        with self.assertRaises(ValueError):
            automation.notice(self.conn, USER, event_key="bad", level="urgent", title="No")
        notices = automation.list_notices(self.conn, USER)
        self.assertEqual(len(notices), 2)
        self.assertEqual(automation.mark_notices_read(self.conn, USER, [notices[0]["id"]]), 1)
        self.assertEqual([n["id"] for n in automation.list_notices(self.conn, USER, unread_only=True)], [notices[1]["id"]])
        self.assertEqual(automation.health_summary(self.conn, USER)["unread_notices"], 1)

    def test_mark_all_read_reaches_past_the_page_that_was_shown(self):
        for n in range(25):
            automation.notice(self.conn, USER, event_key=f"many:{n}", level="info", title=f"Notice {n}")
        self.assertEqual(len(automation.list_notices(self.conn, USER, unread_only=True)), 20, "a page shows 20")
        self.assertEqual(automation.mark_notices_read(self.conn, USER, all_unread=True), 25)
        self.assertEqual(automation.health_summary(self.conn, USER)["unread_notices"], 0)
        self.assertEqual(automation.mark_notices_read(self.conn, USER, all_unread=True), 0)
        self.assertEqual(automation.mark_notices_read(self.conn, USER), 0, "no ids and not all: nothing")


class HealthTests(AutomationCase):
    GRANTED = "2026-09-20T12:00:00+00:00"

    def setUp(self):
        super().setUp()
        env = mock.patch.dict("os.environ", {"PIPELINE_TIMEZONE": "America/Chicago"})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("PIPELINE_GMAIL_TOKEN_DAYS", None)  # restored with the rest when the patch stops

    def connector(self, status="connected", **columns):
        values = {"id": "connector-gmail", "user_id": USER, "provider": "gmail_drafts", "status": status,
                  "created_at": utc_now(), "updated_at": utc_now(), **columns}
        with self.conn:
            self.conn.execute("DELETE FROM connector_accounts WHERE provider='gmail_drafts'")
            self.conn.execute(f"INSERT INTO connector_accounts({', '.join(values)}) VALUES({', '.join('?' for _ in values)})", tuple(values.values()))

    def summary(self, now=T0, **env):
        with mock.patch.dict("os.environ", env):
            return automation.health_summary(self.conn, USER, now=now)

    def test_record_health_keeps_the_last_success_and_the_last_error(self):
        automation.record_health(self.conn, USER, "inbox.replies", ok=True, detail={"read": 3})
        automation.record_health(self.conn, USER, "inbox.replies", ok=False, error="x" * 400)
        [component] = automation.health_summary(self.conn, USER)["components"]
        self.assertTrue(component["last_ok_at"] and component["last_error_at"])
        self.assertEqual(len(component["last_error"]), 300)
        self.assertEqual(component["detail"], {"read": 3}, "an error without detail keeps the last detail")

    def test_each_gmail_state(self):
        self.assertEqual(self.summary()["gmail"]["state"], "not_connected")
        cases = (
            ({"status": "connected"}, "connected"),
            ({"status": "connected", "backoff_until": (T0 + timedelta(minutes=5)).isoformat()}, "throttled"),
            ({"status": "connected", "backoff_until": (T0 - timedelta(minutes=5)).isoformat()}, "connected"),
            ({"status": "error", "backoff_until": (T0 + timedelta(minutes=5)).isoformat()}, "needs_reconnect"),
            ({"status": "disconnected"}, "disconnected"),
        )
        for columns, state in cases:
            with self.subTest(columns=columns):
                self.connector(**columns)
                self.assertEqual(self.summary()["gmail"]["state"], state)

    def test_the_expected_expiry_follows_the_configured_days(self):
        self.connector(token_granted_at=self.GRANTED)
        self.assertEqual(self.summary()["gmail"]["likely_expires_at"], "2026-09-27T12:00:00+00:00", "7 days by default")
        self.assertEqual(self.summary(PIPELINE_GMAIL_TOKEN_DAYS="7")["gmail"]["likely_expires_at"], "2026-09-27T12:00:00+00:00")
        self.assertEqual(self.summary(PIPELINE_GMAIL_TOKEN_DAYS="3")["gmail"]["likely_expires_at"], "2026-09-23T12:00:00+00:00")
        for value in ("0", "soon", "-2"):
            with self.subTest(value=value):
                gmail = self.summary(PIPELINE_GMAIL_TOKEN_DAYS=value)["gmail"]
                self.assertEqual((gmail["likely_expires_at"], gmail["expiring_soon"]), (None, False))
        self.connector()
        self.assertIsNone(self.summary()["gmail"]["likely_expires_at"], "no grant time, no estimate")

    def test_expiring_soon_starts_exactly_24_hours_before(self):
        self.connector(token_granted_at=self.GRANTED)
        boundary = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
        self.assertFalse(self.summary(now=boundary - timedelta(seconds=1))["gmail"]["expiring_soon"])
        at = self.summary(now=boundary)
        self.assertTrue(at["gmail"]["expiring_soon"])
        self.assertEqual(at["banner"], [{"level": "warning", "key": "gmail_expiring", "text": (
            "Gmail will likely ask you to reconnect by Sun, Sep 27. Testing-mode connections last about 7 days."
        )}])
        self.connector(status="error", token_granted_at=self.GRANTED)
        self.assertFalse(self.summary(now=boundary)["gmail"]["expiring_soon"], "only a working connection is about to expire")

    def test_an_estimate_gmail_outlived_is_retired(self):
        # Expected 2026-09-27 12:00; Gmail answered after that, so the 7-day estimate was wrong (a published app, say).
        later = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
        self.connector(token_granted_at=self.GRANTED, last_ok_at="2026-10-20T11:00:00+00:00")
        summary = self.summary(now=later)
        self.assertEqual({key: summary["gmail"][key] for key in ("likely_expires_at", "expiring_soon", "estimate_passed")},
                         {"likely_expires_at": None, "expiring_soon": False, "estimate_passed": True})
        self.assertEqual(summary["banner"], [], "no past-dated warning on a connection that works")
        self.assertFalse(self.summary(now=datetime(2026, 9, 25, 12, 0, tzinfo=timezone.utc))["gmail"]["estimate_passed"])

    def test_a_passed_estimate_with_no_answer_since_says_soon_not_a_past_date(self):
        self.connector(token_granted_at=self.GRANTED, last_ok_at="2026-09-27T11:00:00+00:00")
        summary = self.summary(now=datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc))
        self.assertEqual((summary["gmail"]["expiring_soon"], summary["gmail"]["estimate_passed"]), (True, True))
        self.assertEqual(summary["banner"], [{"level": "warning", "key": "gmail_expiring", "text": "Gmail may ask you to reconnect soon."}])
        before = self.summary(now=datetime(2026, 9, 27, 0, 0, tzinfo=timezone.utc))
        self.assertIn("by Sun, Sep 27", before["banner"][0]["text"], "before the date, the date is given")
        self.assertFalse(before["gmail"]["estimate_passed"])

    def test_a_switch_right_after_the_breaker_is_the_students_even_on_a_coarse_clock(self):
        # Windows before Python 3.13 ticks every ~15 ms: the breaker's write and the
        # student's next switch must still get different stamps, or breaker_off
        # would keep listing a feature the student set themselves.
        frozen = datetime.now(timezone.utc)

        class CoarseClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen

        with mock.patch.object(schema, "datetime", CoarseClock), mock.patch.object(automation, "datetime", CoarseClock):
            automation.set_mode(self.conn, USER, "test_switch", "on")
            rows = [self.act(subject_id=f"s{n}") for n in range(2)]
            automation.undo(self.conn, rows[0]["id"], USER)
            # The breaker's write is the first in a new tick, so its stamp is the clock's own value.
            schema._LAST_NOW = datetime.min.replace(tzinfo=timezone.utc)
            self.assertTrue(automation.undo(self.conn, rows[1]["id"], USER)["feature_paused"])
            self.assertEqual(len(automation.health_summary(self.conn, USER)["breaker_off"]), 1)
            automation.set_mode(self.conn, USER, "test_switch", "on")
            automation.set_mode(self.conn, USER, "test_switch", "off")
            self.assertEqual(automation.health_summary(self.conn, USER)["breaker_off"], [], "off by the student's own choice")

    def test_features_the_breaker_turned_off_are_listed_while_they_stay_off(self):
        automation.set_mode(self.conn, USER, "test_switch", "on")
        rows = [self.act(subject_id=f"s{n}") for n in range(2)]
        automation.undo(self.conn, rows[0]["id"], USER)
        self.assertTrue(automation.undo(self.conn, rows[1]["id"], USER)["feature_paused"])
        automation.mark_notices_read(self.conn, USER, all_unread=True)
        [item] = automation.health_summary(self.conn, USER)["breaker_off"]
        self.assertEqual((item["feature"], item["label"]), ("test_switch", "Test switch"), "still listed once the notice is read")
        self.assertTrue(item["at"])
        automation.set_mode(self.conn, USER, "test_switch", "on")
        self.assertEqual(automation.health_summary(self.conn, USER)["breaker_off"], [], "turned back on: no longer the breaker's")
        automation.set_mode(self.conn, USER, "test_switch", "off")
        self.assertEqual(automation.health_summary(self.conn, USER)["breaker_off"], [], "off by the student's own choice")
        with self.conn:
            self.conn.execute("UPDATE user_settings SET updated_at=? WHERE key='test_switch'", (item["at"],))
        self.assertEqual(len(automation.health_summary(self.conn, USER)["breaker_off"]), 1, "the instrument: the same row counts again")
        self.assertEqual(automation.health_summary(self.conn, USER, now=datetime.now(timezone.utc) + timedelta(days=31))["breaker_off"], [],
                         "only the last 30 days")

    def test_the_banner_order(self):
        automation.set_paused(self.conn, USER, True)
        self.connector(status="error")
        self.assertEqual([item["key"] for item in self.summary()["banner"]], ["paused", "gmail_needs_reconnect"])
        self.connector(token_granted_at=self.GRANTED)
        self.assertEqual([item["key"] for item in self.summary(now=datetime(2026, 9, 27, 0, 0, tzinfo=timezone.utc))["banner"]],
                         ["paused", "gmail_expiring"])
        self.connector(backoff_until="2026-09-20T12:10:00+00:00")
        banner = self.summary()["banner"]
        self.assertEqual([item["key"] for item in banner], ["paused", "gmail_throttled"])
        self.assertEqual(banner[0]["text"], automation.PAUSED_BANNER)
        self.assertEqual(banner[1]["text"], "Gmail asked the app to slow down. Checks resume after 7:10 AM.")
        automation.set_paused(self.conn, USER, False)
        self.connector(status="error")
        self.assertEqual(self.summary()["banner"][0]["text"], "Gmail needs reconnecting. Reply and bounce checks have stopped.")

    def test_counts(self):
        automation.set_mode(self.conn, USER, "test_switch", "on")
        automation.set_mode(self.conn, USER, "test_shadow", "shadow")
        self.act(subject_id="s1")
        self.act(subject_id="s2", auto=False)
        self.act(feature="test_shadow", subject_id="s3")
        counts = automation.health_summary(self.conn, USER)["counts"]
        self.assertEqual(counts, {"proposed": 1, "shadow_unreviewed": 1, "applied_last_24h": 1})


class AccountTests(AutomationCase):
    def test_the_account_export_holds_the_ledger_its_notices_and_health(self):
        automation.set_mode(self.conn, USER, "test_switch", "on")
        row = self.act()
        automation.notice(self.conn, USER, event_key="export:1", level="info", title="Exported notice")
        automation.record_health(self.conn, USER, "inbox.replies", ok=False, error="boom")
        exported = export_account(self.conn, user_id=USER)
        self.assertEqual([item["id"] for item in exported["automation_actions"]], [row["id"]])
        self.assertEqual(json.loads(exported["automation_actions"][0]["evidence_json"]), {"message_id": "m-1"},
                         "the evidence it acted on goes with it")
        self.assertEqual([item["title"] for item in exported["automation_notices"]], ["Exported notice"])
        self.assertEqual([(item["component"], item["last_error"]) for item in exported["automation_health"]], [("inbox.replies", "boom")])

    def test_deleting_the_account_removes_them(self):
        automation.set_mode(self.conn, USER, "test_switch", "on")
        self.act()
        automation.notice(self.conn, USER, event_key="gone:1", level="info", title="Gone")
        automation.record_health(self.conn, USER, "inbox.replies", ok=True)
        root = Path(self.tempdir.name)
        delete_account(self.conn, [root / "resumes", root / "captures", root / "interviews"], user_id=USER)
        for table in ("automation_actions", "automation_notices", "automation_health"):
            with self.subTest(table=table):
                self.assertIn(table, ACCOUNT_QUERIES)
                self.assertEqual(self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, "cascaded with the account")


# --- The migration ----------------------------------------------------------------------------

NEW_COLUMNS = [
    ("opportunity_interactions", "source"), ("application_tasks", "origin"), ("application_tasks", "origin_ref"),
    ("connector_accounts", "last_ok_at"), ("connector_accounts", "last_error"), ("connector_accounts", "token_granted_at"),
    ("connector_accounts", "backoff_until"),
]


def schema_before_0037(path):
    """A database as it stood before this migration, built the way ensure_product_schema builds one."""
    conn = connect_product(path)
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    for migration in sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        if migration.name >= "0037":
            break
        sql = migration.read_text(encoding="utf-8")
        step = schema._MIGRATION_STEPS.get(migration.name)
        step(conn, sql) if step else conn.executescript(sql)
        conn.execute("INSERT INTO schema_migrations(name, applied_at) VALUES(?, ?)", (migration.name, utc_now()))
    conn.execute(
        "INSERT INTO users(id, email, display_name, role, created_at, updated_at) VALUES('student-1', NULL, 'S', 'student', ?, ?)",
        (utc_now(), utc_now()),
    )
    conn.commit()
    return conn


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)

    def assert_migrated(self, conn, user_id):
        for table, column in NEW_COLUMNS:
            self.assertTrue(schema._has_column(conn, table, column), f"{table}.{column}")
        for table in ("automation_actions", "automation_health", "automation_notices"):
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        self.assertIn("0037_automation.sql", {row[0] for row in conn.execute("SELECT name FROM schema_migrations")})
        return conn.execute("SELECT value FROM user_settings WHERE user_id=? AND key='automation_paused'", (user_id,)).fetchone()[0]

    def test_a_crash_after_some_columns_were_added_still_upgrades(self):
        conn = schema_before_0037(Path(self.tempdir.name) / "platform.db")
        self.addCleanup(conn.close)
        # What a crash between ALTERs and the marker leaves behind.
        for table, column in NEW_COLUMNS[::2]:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
        conn.commit()
        ensure_product_schema(conn)
        self.assertEqual(self.assert_migrated(conn, "student-1"), "off", "every existing student starts unpaused")
        seeded = conn.execute("SELECT updated_at FROM user_settings WHERE user_id='student-1' AND key='automation_paused'").fetchone()[0]
        self.assertEqual(seeded, schema.PAUSE_NEVER_CHANGED, "nobody paused, so the seed is not a pause time")

    def test_running_it_again_after_the_marker_was_lost_succeeds(self):
        _, path = build_and_migrate(Path(self.tempdir.name))
        conn = connect_product(path)
        self.addCleanup(conn.close)
        automation.set_paused(conn, USER, True)
        with conn:
            conn.execute("DELETE FROM schema_migrations WHERE name='0037_automation.sql'")
        ensure_product_schema(conn)
        self.assertEqual(self.assert_migrated(conn, USER), "on", "the seed never overwrites a student's pause")
        schema._apply_automation(conn, (MIGRATIONS / "0037_automation.sql").read_text(encoding="utf-8"))
        conn.commit()


if __name__ == "__main__":
    unittest.main()


class MonotonicClockTests(unittest.TestCase):
    """utc_now never repeats a stamp, even on a clock that ticks only every 15 ms."""

    def test_a_coarse_clock_still_gives_strictly_later_stamps(self):
        from datetime import datetime as real_datetime, timezone as real_timezone
        from unittest import mock

        frozen = real_datetime(2026, 9, 28, 12, 0, tzinfo=real_timezone.utc)

        class CoarseClock(real_datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen

        with mock.patch.object(schema, "datetime", CoarseClock), mock.patch.object(schema, "_LAST_NOW", real_datetime.min.replace(tzinfo=real_timezone.utc)):
            stamps = [utc_now() for _ in range(50)]
        self.assertEqual(stamps, sorted(stamps))
        self.assertEqual(len(set(stamps)), 50)
        self.assertEqual(stamps[0], frozen.isoformat(timespec="microseconds"))
