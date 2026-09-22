"""Tests for the notification delivery engine (digest, reminders, health)."""

import sys
import sqlite3
import tempfile
import unittest
from unittest import mock
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cryptography.fernet import Fernet

from opportunity_app import auth
from opportunity_app import notifications as notif
from opportunity_app.connections import ensure_preferences
from opportunity_app.schema import LOCAL_USER_ID, connect_product, utc_now

from helpers_platform import build_and_migrate

RUN_AT = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)


class FakeProvider:
    """In-memory live provider for assertions."""

    live = True

    def __init__(self):
        self.calls = []

    def deliver(self, channel, recipient, subject, body):
        self.calls.append({"channel": channel, "recipient": recipient, "subject": subject, "body": body})
        return {"delivered": True, "detail": "fake"}


class FailingProvider(FakeProvider):
    live = True

    def deliver(self, channel, recipient, subject, body):
        self.calls.append({"channel": channel})
        return {"delivered": False, "detail": "provider outage"}


class QuietHoursTests(unittest.TestCase):
    def _prefs(self, tz="UTC", start="22:00", end="08:00"):
        return {"timezone": tz, "quiet_start": start, "quiet_end": end}

    def test_window_crossing_midnight(self):
        late = datetime(2026, 8, 22, 23, 30, tzinfo=timezone.utc)
        early = datetime(2026, 8, 22, 7, 0, tzinfo=timezone.utc)
        noon = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
        prefs = self._prefs()
        self.assertTrue(notif.in_quiet_hours(prefs, late))
        self.assertTrue(notif.in_quiet_hours(prefs, early))
        self.assertFalse(notif.in_quiet_hours(prefs, noon))

    def test_same_window_respects_timezone(self):
        # 23:30 UTC is 17:30 in Austin (summer): outside 22:00-08:00 local.
        instant = datetime(2026, 8, 22, 23, 30, tzinfo=timezone.utc)
        self.assertFalse(notif.in_quiet_hours(self._prefs(tz="America/Chicago"), instant))
        # 04:00 UTC is 23:00 the previous day in Austin: inside quiet window.
        night_utc = datetime(2026, 8, 22, 4, 0, tzinfo=timezone.utc)
        self.assertTrue(notif.in_quiet_hours(self._prefs(tz="America/Chicago"), night_utc))

    def test_equal_bounds_mean_never_quiet(self):
        instant = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
        self.assertFalse(notif.in_quiet_hours(self._prefs(start="08:00", end="08:00"), instant))


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        _, self.platform_path = build_and_migrate(Path(self.tempdir.name))
        self.provider = FakeProvider()
        # Quiet hours resolve through user_time.user_timezone; pin the
        # fallback so RUN_AT (12:00 UTC) is daytime on every machine.
        env = mock.patch.dict("os.environ", {"PIPELINE_TIMEZONE": "UTC"})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self.tempdir.cleanup()

    def _conn(self):
        return connect_product(self.platform_path)

    def _queue(self, conn, event_key, channel="email", status="sandbox_suppressed", user_id=LOCAL_USER_ID):
        timestamp = utc_now()
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO notification_outbox(id, user_id, channel, event_key, payload_json, status, created_at)"
                " VALUES(?, ?, ?, ?, ?, ?, ?)",
                (f"n-{event_key}-{channel}", user_id, channel, event_key, '{"subject": "Test update"}', status, timestamp),
            )

    def _set_prefs(self, conn, **updates):
        preferences = ensure_preferences(conn, user_id=LOCAL_USER_ID)
        merged = {**preferences, **updates}
        with conn:
            conn.execute(
                "UPDATE notification_preferences SET timezone=?, quiet_start=?, quiet_end=?, digest_frequency=?,"
                " email_enabled=?, in_app_enabled=? WHERE user_id=?",
                (merged["timezone"], merged["quiet_start"], merged["quiet_end"], merged["digest_frequency"],
                 int(merged["email_enabled"]), int(merged["in_app_enabled"]), LOCAL_USER_ID),
            )

    def test_digest_delivers_email_when_enabled_and_live(self):
        with closing(self._conn()) as conn:
            with conn:
                conn.execute("UPDATE users SET email='owner@example.com' WHERE id=?", (LOCAL_USER_ID,))
            self._set_prefs(conn, digest_frequency="immediate", email_enabled=True, in_app_enabled=True)
            self._queue(conn, "evt-1")
            stats = notif.run_notification_digest(conn, provider=self.provider, now=RUN_AT)
            row = conn.execute("SELECT * FROM notification_outbox WHERE event_key='evt-1'").fetchone()
        self.assertEqual(row["status"], "sent")
        self.assertIsNotNone(row["delivered_at"])
        self.assertEqual(stats["messages_delivered"], 1)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(self.provider.calls[0]["recipient"], "owner@example.com")

    def test_digest_holds_during_quiet_hours(self):
        with closing(self._conn()) as conn:
            self._set_prefs(conn, email_enabled=True, quiet_start="00:00", quiet_end="23:59")
            self._queue(conn, "evt-hold")
            stats = notif.run_notification_digest(conn, provider=self.provider, now=RUN_AT)
            row = conn.execute("SELECT * FROM notification_outbox WHERE event_key='evt-hold'").fetchone()
        self.assertEqual(row["status"], "sandbox_suppressed")
        self.assertEqual(stats["held_quiet_hours"], 1)
        self.assertEqual(self.provider.calls, [])

    def test_digest_cancels_when_frequency_off(self):
        with closing(self._conn()) as conn:
            self._set_prefs(conn, digest_frequency="off", email_enabled=True)
            self._queue(conn, "evt-off")
            stats = notif.run_notification_digest(conn, provider=self.provider, now=RUN_AT)
            row = conn.execute("SELECT * FROM notification_outbox WHERE event_key='evt-off'").fetchone()
        self.assertEqual(row["status"], "cancelled")
        self.assertGreaterEqual(stats["cancelled_off"], 1)
        self.assertEqual(self.provider.calls, [])

    def test_sandbox_provider_leaves_rows_suppressed(self):
        with closing(self._conn()) as conn:
            self._set_prefs(conn, email_enabled=True)
            self._queue(conn, "evt-sandbox")
            stats = notif.run_notification_digest(conn, provider=notif.SandboxProvider(), now=RUN_AT)
            row = conn.execute("SELECT * FROM notification_outbox WHERE event_key='evt-sandbox'").fetchone()
        self.assertEqual(row["status"], "sandbox_suppressed")
        self.assertGreaterEqual(stats["left_suppressed"], 1)

    def test_failing_provider_does_not_mark_sent(self):
        with closing(self._conn()) as conn:
            self._set_prefs(conn, email_enabled=True)
            self._queue(conn, "evt-fail")
            notif.run_notification_digest(conn, provider=FailingProvider(), now=RUN_AT)
            row = conn.execute("SELECT * FROM notification_outbox WHERE event_key='evt-fail'").fetchone()
        self.assertEqual(row["status"], "sandbox_suppressed")

    def test_frequency_filter_skips_other_users_batches(self):
        with closing(self._conn()) as conn:
            self._set_prefs(conn, digest_frequency="weekly", email_enabled=True)
            self._queue(conn, "evt-weekly")
            stats = notif.run_notification_digest(
                conn, {"frequency": "daily"}, provider=self.provider, now=RUN_AT
            )
            row = conn.execute("SELECT * FROM notification_outbox WHERE event_key='evt-weekly'").fetchone()
        self.assertEqual(row["status"], "sandbox_suppressed")
        self.assertEqual(stats["users_processed"], 0)

    def test_disabled_channel_cancels_pending_rows_instead_of_delivering_later(self):
        with closing(self._conn()) as conn:
            self._set_prefs(conn, email_enabled=False)
            self._queue(conn, "evt-disabled")
            stats = notif.run_notification_digest(conn, provider=self.provider, now=RUN_AT)
            row = conn.execute(
                "SELECT status FROM notification_outbox WHERE event_key='evt-disabled'"
            ).fetchone()
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(stats["cancelled_off"], 1)
        self.assertEqual(self.provider.calls, [])

    def test_phone_channels_use_only_the_verified_phone_destination(self):
        with closing(self._conn()) as conn:
            ensure_preferences(conn, user_id=LOCAL_USER_ID)
            with conn:
                conn.execute(
                    """UPDATE notification_preferences
                       SET quiet_start='00:00', quiet_end='00:00',
                           sms_enabled=1, voice_enabled=1,
                           phone_e164='+15125550123', phone_verified=1
                       WHERE user_id=?""",
                    (LOCAL_USER_ID,),
                )
                conn.execute(
                    "UPDATE users SET email='must-not-be-used@example.com' WHERE id=?",
                    (LOCAL_USER_ID,),
                )
            self._queue(conn, "evt-sms", channel="sms")
            self._queue(conn, "evt-voice", channel="voice")
            stats = notif.run_notification_digest(conn, provider=self.provider, now=RUN_AT)
        self.assertEqual(stats["messages_delivered"], 2)
        self.assertEqual(
            {(call["channel"], call["recipient"]) for call in self.provider.calls},
            {("sms", "+15125550123"), ("voice", "+15125550123")},
        )

    def test_push_is_not_misrouted_to_email_without_an_explicit_subscription(self):
        with closing(self._conn()) as conn:
            ensure_preferences(conn, user_id=LOCAL_USER_ID)
            with conn:
                conn.execute(
                    """UPDATE notification_preferences
                       SET quiet_start='00:00', quiet_end='00:00', push_enabled=1
                       WHERE user_id=?""",
                    (LOCAL_USER_ID,),
                )
                conn.execute(
                    "UPDATE users SET email='owner@example.com' WHERE id=?",
                    (LOCAL_USER_ID,),
                )
            self._queue(conn, "evt-push", channel="push")
            stats = notif.run_notification_digest(conn, provider=self.provider, now=RUN_AT)
            row = conn.execute(
                "SELECT status FROM notification_outbox WHERE event_key='evt-push'"
            ).fetchone()
        self.assertEqual(row["status"], "sandbox_suppressed")
        self.assertEqual(stats["left_suppressed"], 1)
        self.assertEqual(self.provider.calls, [])


class ReminderDispatchTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        _, self.platform_path = build_and_migrate(Path(self.tempdir.name))
        self.provider = FakeProvider()
        # Quiet hours resolve through user_time.user_timezone; pin the
        # fallback so RUN_AT (12:00 UTC) is daytime on every machine.
        env = mock.patch.dict("os.environ", {"PIPELINE_TIMEZONE": "UTC"})
        env.start()
        self.addCleanup(env.stop)

    def tearDown(self):
        self.tempdir.cleanup()

    def _add_due_reminder(self, conn, due_at, reminder_type="follow_up", app_id="app-job-b"):
        with conn:
            conn.execute(
                "INSERT INTO reminders(id, application_id, user_id, reminder_type, due_at, timezone, status, created_at, updated_at)"
                " VALUES(?, ?, ?, ?, ?, 'UTC', 'scheduled', ?, ?)"
                " ON CONFLICT(application_id, user_id, reminder_type) DO UPDATE SET due_at=excluded.due_at, status='scheduled'",
                (f"reminder-{app_id}-{reminder_type}", app_id, LOCAL_USER_ID, reminder_type, due_at, utc_now(), utc_now()),
            )

    def test_due_reminder_completes_and_records_outbox_entry(self):
        past = (RUN_AT - timedelta(hours=1)).isoformat()
        future = (RUN_AT + timedelta(days=2)).isoformat()
        with closing(connect_product(self.platform_path)) as conn:
            self._add_due_reminder(conn, past)
            self._add_due_reminder(conn, future, reminder_type="deadline")
            stats = notif.send_due_reminders(conn, provider=self.provider, now=RUN_AT)
            done = conn.execute(
                "SELECT status FROM reminders WHERE reminder_type='follow_up' AND user_id=?", (LOCAL_USER_ID,)
            ).fetchone()
            pending = conn.execute(
                "SELECT status FROM reminders WHERE reminder_type='deadline' AND user_id=?", (LOCAL_USER_ID,)
            ).fetchone()
            outbox = conn.execute(
                "SELECT * FROM notification_outbox WHERE event_key LIKE 'reminder:%'"
            ).fetchall()
        self.assertEqual(done["status"], "completed")
        self.assertEqual(pending["status"], "scheduled", "future reminders must stay scheduled")
        self.assertEqual(stats["reminders_fired"], 1)
        self.assertEqual(len(outbox), 1)
        self.assertEqual(outbox[0]["status"], "sent")

    def test_dispatch_is_idempotent(self):
        past = (RUN_AT - timedelta(hours=2)).isoformat()
        with closing(connect_product(self.platform_path)) as conn:
            self._add_due_reminder(conn, past)
            first = notif.send_due_reminders(conn, provider=self.provider, now=RUN_AT)
            second = notif.send_due_reminders(conn, provider=self.provider, now=RUN_AT)
            count = conn.execute("SELECT COUNT(*) FROM notification_outbox WHERE event_key LIKE 'reminder:%'").fetchone()[0]
        self.assertEqual(first["reminders_fired"], 1)
        self.assertEqual(second["reminders_fired"], 0)
        self.assertEqual(count, 1)


class ConnectorHealthTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        _, self.platform_path = build_and_migrate(Path(self.tempdir.name))

    def tearDown(self):
        self.tempdir.cleanup()

    def test_reports_states_per_connector(self):
        key = Fernet.generate_key().decode()
        from cryptography.fernet import Fernet as _F
        fernet = _F(key.encode())
        with closing(connect_product(self.platform_path)) as conn:
            with conn:
                conn.execute(
                    "INSERT INTO connector_accounts(id, user_id, provider, scopes_json, encrypted_access_token,"
                    " encrypted_refresh_token, status, created_at, updated_at)"
                    " VALUES('c-live', ?, 'google', '[]', ?, ?, 'connected', ?, ?)",
                    (LOCAL_USER_ID, fernet.encrypt(b"tok").decode(), fernet.encrypt(b"").decode(), utc_now(), utc_now()),
                )
                conn.execute(
                    "INSERT INTO connector_accounts(id, user_id, provider, scopes_json, status, created_at, updated_at)"
                    " VALUES('c-sandbox', ?, 'sandbox', '[]', 'connected', ?, ?)",
                    (LOCAL_USER_ID, utc_now(), utc_now()),
                )
            report = notif.connector_health(conn)
        states = {item["connector_id"]: item["state"] for item in report["items"]}
        self.assertEqual(states["c-live"], "connected")
        self.assertEqual(states["c-sandbox"], "unverified_sandbox")


class RecoveryAndEmployerDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        _, self.platform_path = build_and_migrate(root)
        self.provider = FakeProvider()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_recovery_uses_live_delivery_without_leaking_code(self):
        secret = "recovery-secret"
        with closing(connect_product(self.platform_path)) as conn:
            auth.register_owner(conn, "owner@example.com", "OriginalPass1", "Owner")
            response = auth.request_recovery(
                conn, "owner@example.com", secret,
                deliver_code=lambda recipient, code: self.provider.deliver("email", recipient, "code", code),
            )
            self.assertEqual(response["delivery"], "sent")
            self.assertNotIn("sandbox_code", response)
            challenge = response["challenge_id"]
            used_code = self.provider.calls[-1]["body"]
            result = auth.complete_recovery(conn, challenge, used_code, "ReplacementPass1", secret)
            self.assertTrue(result["recovered"])

    def test_recovery_without_provider_never_hands_out_the_code(self):
        # With no mail provider, the code used to come back in the HTTP response,
        # so anyone who could reach the port and knew the owner's email could
        # reset the password. It also told them whether the email was registered.
        with closing(connect_product(self.platform_path)) as conn:
            auth.register_owner(conn, "owner@example.com", "OriginalPass1", "Owner")
            known = auth.request_recovery(conn, "owner@example.com", "recovery-secret")
            unknown = auth.request_recovery(conn, "nobody@example.com", "recovery-secret")
            challenges = conn.execute("SELECT COUNT(*) FROM recovery_challenges").fetchone()[0]
        self.assertEqual(known, {"accepted": True, "delivery": "unavailable"})
        self.assertEqual(known, unknown)
        self.assertEqual(challenges, 0, "a code nobody can receive is never created")

    def test_recovery_sandbox_returns_the_code_only_when_opted_in(self):
        with closing(connect_product(self.platform_path)) as conn:
            auth.register_owner(conn, "owner@example.com", "OriginalPass1", "Owner")
            response = auth.request_recovery(conn, "owner@example.com", "recovery-secret", expose_code=True)
        self.assertEqual(response["delivery"], "sandbox_suppressed")
        self.assertIn("sandbox_code", response)


if __name__ == "__main__":
    unittest.main()
