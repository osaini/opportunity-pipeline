"""Regression guards for defects found in the 2026-08-22 bug search.

All defects originally pinned here are fixed. Their tests remain live so a
regression fails the build directly; there are no ``expectedFailure`` markers.

Status:
  * ReadOnlyTokenResolutionTests — FIXED 2026-08-23 (auth.py:147 now catches
    broadly); the tests are live regression guards.
  * CliProviderErrorBoundaryTests — FIXED 2026-08-23; the test is a live
    regression guard.
  * LoneSurrogateRequestBodyTests and the lone-surrogate case in
    UnicodeCredentialComparisonTests — FIXED 2026-09-14 (found during the
    2026-09 refactor review); live regression guards.
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from psycopg.errors import ReadOnlySqlTransaction

sys.path.insert(0, str(Path(__file__).resolve().parent))


class ReadOnlyPostgresLike:
    """A read-only PostgreSQL connection, as PostgresConnection presents one.

    `SET TRANSACTION READ ONLY` leaves reads working and makes writes raise, so
    the SELECT must succeed here and only the UPDATE may fail — that is the
    sequence resolve_user_token actually performs. The context-manager behaviour
    mirrors PostgresConnection.__exit__: roll back, then let it propagate.
    """

    backend = "postgresql"

    def __init__(self, user_id="student-user"):
        self._user_id = user_id
        self.attempted_write = False
        self.rolled_back = False

    def execute(self, sql, params=()):
        if sql.strip().upper().startswith("SELECT"):
            return _SingleRow({"user_id": self._user_id})
        self.attempted_write = True
        raise ReadOnlySqlTransaction("cannot execute UPDATE in a read-only transaction")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.rolled_back = exc_type is not None
        return False


class _SingleRow:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class ReadOnlyTokenResolutionTests(unittest.TestCase):
    """Regression: a read-only PostgreSQL connection must not break auth.

    require_auth resolves bearer tokens over a read-only connection, and
    resolve_user_token then performs a best-effort `UPDATE ... last_used_at`.
    That write raises ReadOnlySqlTransaction (SQLSTATE 25006) on PostgreSQL,
    which is not a sqlite3.Error — so the original `except sqlite3.Error` let it
    escape and every request carrying an issued student token returned 500.

    Nothing else in the suite covers this: SQLite silently permits the write, and
    tests/test_postgres.py authenticates with the static owner token, which
    short-circuits in require_auth before reaching this code.
    """

    def test_resolve_user_token_survives_a_read_only_postgres_write(self):
        from opportunity_app import auth

        conn = ReadOnlyPostgresLike()
        resolved = auth.resolve_user_token(conn, "student-token")

        self.assertEqual(resolved, "student-user")
        # Guard the test itself: if the UPDATE were never attempted, this would
        # pass no matter how narrow the exception handling became.
        self.assertTrue(conn.attempted_write, "the usage-tracking UPDATE was never attempted")

    def test_the_narrow_catch_that_caused_the_outage_cannot_come_back(self):
        self.assertFalse(
            issubclass(ReadOnlySqlTransaction, sqlite3.Error),
            "psycopg's read-only error is not a sqlite3.Error, so `except sqlite3.Error` "
            "cannot catch it",
        )

    def test_student_token_authenticates_end_to_end_over_a_read_only_connection(self):
        """The user-visible half: a real request must return 200, not 500."""
        from fastapi.testclient import TestClient

        from helpers_platform import build_and_migrate
        from opportunity_app import STATIC_DIR
        from opportunity_app import api as api_module
        from opportunity_app.api import create_app
        from opportunity_app.auth import resolve_user_token

        with tempfile.TemporaryDirectory() as tempdir:
            _, platform_path = build_and_migrate(Path(tempdir))
            app = create_app(
                db_path=platform_path,
                access_token="pins-owner",
                admin_token="pins-admin",
                static_dir=STATIC_DIR,
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                client.put(
                    "/api/v1/admin/feature-flags/allow_public_signup",
                    headers={"Authorization": "Bearer pins-admin"},
                    json={"enabled": True},
                )
                registered = client.post(
                    "/api/v1/auth/register",
                    json={
                        "email": "pins@example.com",
                        "password": "PinsPassword123",
                        "display_name": "Pins",
                    },
                )
                self.assertEqual(registered.status_code, 201, registered.text)
                student_token = registered.json()["api_token"]
                headers = {"Authorization": f"Bearer {student_token}"}

                self.assertEqual(client.get("/api/v1/opportunities", headers=headers).status_code, 200)

                # Same request, but token resolution now runs against a read-only
                # PostgreSQL connection instead of SQLite.
                def resolve_over_postgres(_conn, token):
                    return resolve_user_token(ReadOnlyPostgresLike(), token)

                with mock.patch.object(api_module, "resolve_user_token", resolve_over_postgres):
                    for path in ("/api/v1/opportunities", "/api/v1/stats"):
                        response = client.get(path, headers=headers)
                        self.assertNotEqual(
                            response.status_code, 500,
                            f"GET {path} returned 500: the read-only write escaped require_auth",
                        )
                        self.assertEqual(response.status_code, 200, response.text)


class UnicodeCredentialComparisonTests(unittest.TestCase):
    def test_arbitrary_unicode_credentials_compare_without_type_errors(self):
        from opportunity_app.auth import constant_time_equal

        self.assertTrue(constant_time_equal("梵.²", "梵.²"))
        self.assertFalse(constant_time_equal("梵.²", "owner-token"))

    def test_lone_surrogate_credentials_compare_without_encode_errors(self):
        """A JSON "\\ud800" escape decodes to an unpaired surrogate, which strict
        UTF-8 encoding refuses. The comparison must answer, not raise."""
        from opportunity_app.auth import constant_time_equal

        self.assertFalse(constant_time_equal("x\ud800y", "owner-token"))
        self.assertTrue(constant_time_equal("x\ud800y", "x\ud800y"))


class LoneSurrogateRequestBodyTests(unittest.TestCase):
    """Regression: an unpaired surrogate in a JSON credential returned 500.

    json.loads accepts "\\ud800" and yields a str that cannot be encoded as UTF-8,
    so the failure surfaced in whichever layer encoded first — the credential
    compare, scrypt, or the SQLite driver — on every route below. The API must
    reject such a body as invalid input (422) before any of those layers run.
    """

    SURROGATE = "\\ud800"

    def test_credential_routes_reject_lone_surrogates_instead_of_500(self):
        from fastapi.testclient import TestClient

        from helpers_platform import build_and_migrate
        from opportunity_app import STATIC_DIR
        from opportunity_app.api import create_app

        s = self.SURROGATE
        cases = [
            ("/api/v1/session", '{"token": "x%sy"}' % s),
            ("/api/v1/session", '{"email": "a%s@example.com", "password": "CorrectHorse1"}' % s),
            ("/api/v1/session", '{"email": "pins@example.com", "password": "CorrectHorse1%s"}' % s),
            ("/api/v1/auth/register", '{"invite_token": "t%s", "email": "s@example.com", "password": "CorrectHorse1", "display_name": "S"}' % s),
            ("/api/v1/auth/register", '{"email": "s2@example.com", "password": "CorrectHorse1%s", "display_name": "S"}' % s),
            ("/api/v1/auth/recovery", '{"email": "o%s@example.com"}' % s),
            ("/api/v1/auth/recovery/complete", '{"challenge_id": "c%s", "code": "123456", "new_password": "CorrectHorse1"}' % s),
            ("/api/v1/extension/pairings/redeem", '{"code": "abcdefghijklmnopqrstu%s"}' % s),
        ]
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            _, platform_path = build_and_migrate(root)
            app = create_app(
                db_path=platform_path,
                access_token="pins-owner",
                admin_token="pins-admin",
                static_dir=STATIC_DIR,
                resume_storage=root / "resumes",
                capture_storage=root / "captures",
                interview_storage=root / "interviews",
            )
            with TestClient(app, raise_server_exceptions=False) as client:
                registered = client.post(
                    "/api/v1/auth/register",
                    json={
                        "invite_token": "pins-owner",
                        "email": "pins@example.com",
                        "password": "PinsPassword123",
                        "display_name": "Pins",
                    },
                )
                self.assertEqual(registered.status_code, 201, registered.text)
                for path, body in cases:
                    with self.subTest(path=path, body=body):
                        response = client.post(
                            path,
                            content=body.encode("ascii"),
                            headers={"Content-Type": "application/json", "Origin": "chrome-extension://pins"},
                        )
                        self.assertEqual(response.status_code, 422, response.text)

                # Guard the test itself: a surrogate *pair* is valid Unicode and must
                # still reach the route, so the rejection cannot be a blanket escape ban.
                paired = client.post(
                    "/api/v1/session",
                    content=b'{"token": "\\ud83d\\ude00"}',
                    headers={"Content-Type": "application/json"},
                )
                self.assertEqual(paired.status_code, 401, paired.text)


class CliProviderErrorBoundaryTests(unittest.TestCase):
    def test_cli_provider_converts_subprocess_failures_to_runtime_error(self):
        """BUG: long conversations embed the full transcript into argv; on
        Windows a command line over ~32k characters makes subprocess.run
        raise OSError [WinError 206], which escapes create() as a raw
        OSError/500 instead of the provider boundary's honest RuntimeError
        that the agent turn recorder can attribute."""
        from opportunity_app.agent_providers import CliAgentProvider

        provider = CliAgentProvider("claude-code", "subscription")
        with mock.patch(
            "opportunity_app.agent_providers.subprocess.run",
            side_effect=OSError("[WinError 206] The filename or extension is too long"),
        ):
            with self.assertRaises(RuntimeError):
                provider.create(
                    instructions="t",
                    messages=[{"role": "user", "content": "x" * 40000}],
                    tools=[],
                    max_output_tokens=10,
                )


if __name__ == "__main__":
    unittest.main()
