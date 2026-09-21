"""Unit tests for auth lifecycle: registration, login, sandbox recovery."""

import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from opportunity_app import auth
from opportunity_app.schema import connect_product

from helpers_platform import build_and_migrate

SECRET = "unit-test-secret"


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        _, self.platform_path = build_and_migrate(Path(self.tempdir.name))

    def tearDown(self):
        self.tempdir.cleanup()

    def test_register_owner_and_authenticate(self):
        with closing(connect_product(self.platform_path)) as conn:
            result = auth.register_owner(conn, "Owner@Example.com ", "CorrectHorse1", "Test Owner")
            self.assertTrue(result["registered"])
            self.assertEqual(result["email"], "owner@example.com")
            self.assertTrue(auth.authenticate_password(conn, "owner@example.com", "CorrectHorse1"))
            self.assertFalse(auth.authenticate_password(conn, "owner@example.com", "WrongPassword1"))
            # Unknown accounts take the same expensive path and simply fail.
            self.assertFalse(auth.authenticate_password(conn, "nobody@example.com", "Whatever123"))

    def test_register_rejects_weak_passwords_and_duplicates(self):
        with closing(connect_product(self.platform_path)) as conn:
            for password in ("short1A", "alllowercase1", "ALLUPPERCASE1", "NoDigitsHere"):
                with self.subTest(password=password):
                    with self.assertRaises(ValueError):
                        auth.register_owner(conn, "owner@example.com", password, "Owner")
            auth.register_owner(conn, "owner@example.com", "CorrectHorse1", "Owner")
            with self.assertRaises(ValueError):
                auth.register_owner(conn, "owner@example.com", "AnotherPass1", "Owner")

    def test_recovery_round_trip(self):
        with closing(connect_product(self.platform_path)) as conn:
            auth.register_owner(conn, "owner@example.com", "OriginalPass1", "Owner")
            challenge = auth.request_recovery(conn, "owner@example.com", SECRET, expose_code=True)
            self.assertEqual(challenge["delivery"], "sandbox_suppressed")
            code = challenge["sandbox_code"]
            auth.complete_recovery(conn, challenge["challenge_id"], code, "ReplacementPass1", SECRET)
            self.assertTrue(auth.authenticate_password(conn, "owner@example.com", "ReplacementPass1"))
            self.assertFalse(auth.authenticate_password(conn, "owner@example.com", "OriginalPass1"))
            row = conn.execute(
                "SELECT status FROM recovery_challenges WHERE id=?", (challenge["challenge_id"],)
            ).fetchone()
            self.assertEqual(row["status"], "used")

    def test_recovery_is_indistinguishable_for_unknown_email(self):
        with closing(connect_product(self.platform_path)) as conn:
            response = auth.request_recovery(conn, "nobody@example.com", SECRET, expose_code=True)
            self.assertEqual(response, {"accepted": True, "delivery": "sandbox_suppressed"})
            count = conn.execute("SELECT COUNT(*) FROM recovery_challenges").fetchone()[0]
            self.assertEqual(count, 0)

    def test_recovery_rejects_wrong_code_and_counts_attempts(self):
        with closing(connect_product(self.platform_path)) as conn:
            auth.register_owner(conn, "owner@example.com", "OriginalPass1", "Owner")
            challenge = auth.request_recovery(conn, "owner@example.com", SECRET, expose_code=True)
            attempts_before = conn.execute(
                "SELECT attempts FROM recovery_challenges WHERE id=?", (challenge["challenge_id"],)
            ).fetchone()["attempts"]
            with self.assertRaises(ValueError):
                auth.complete_recovery(conn, challenge["challenge_id"], "000000", "ReplacementPass1", SECRET)
            attempts_after = conn.execute(
                "SELECT attempts FROM recovery_challenges WHERE id=?", (challenge["challenge_id"],)
            ).fetchone()["attempts"]
            self.assertEqual(attempts_after, attempts_before + 1)
            self.assertFalse(auth.authenticate_password(conn, "owner@example.com", "ReplacementPass1"))

    def test_expired_challenge_is_marked_expired(self):
        with closing(connect_product(self.platform_path)) as conn:
            auth.register_owner(conn, "owner@example.com", "OriginalPass1", "Owner")
            challenge = auth.request_recovery(conn, "owner@example.com", SECRET, expose_code=True)
            conn.execute(
                "UPDATE recovery_challenges SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
                (challenge["challenge_id"],),
            )
            with self.assertRaises(ValueError):
                auth.complete_recovery(
                    conn, challenge["challenge_id"], challenge["sandbox_code"], "ReplacementPass1", SECRET
                )
            row = conn.execute(
                "SELECT status FROM recovery_challenges WHERE id=?", (challenge["challenge_id"],)
            ).fetchone()
            self.assertEqual(row["status"], "expired")


if __name__ == "__main__":
    unittest.main()
