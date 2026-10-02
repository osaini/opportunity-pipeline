"""Unit tests for the OAuth lifecycle against a mocked provider token endpoint."""

import asyncio
import base64
import hashlib
import json
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
from cryptography.fernet import Fernet

from opportunity_app import connections
from opportunity_app.core.database import connect_product

from opportunity_app.core.schema import LOCAL_USER_ID

from helpers_platform import build_and_migrate


def _fake_response(status_code=200, payload=None):
    response = mock.Mock()
    response.status_code = status_code
    response.json = lambda: payload or {}
    return response


def state_from_url(url: str) -> str:
    return parse_qs(urlparse(url).query)["state"][0]


def challenge_from_url(url: str) -> str:
    return parse_qs(urlparse(url).query)["code_challenge"][0]


class FakeAsyncClient:
    """Replaces httpx.AsyncClient inside connections.complete_oauth."""

    response = None
    captured = None
    # What users.getProfile answers, or an exception to raise instead; None is a signed-in student@school.example.
    profile = None
    profile_calls = []

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, data=None):
        type(self).captured = {"url": url, "data": data}
        return type(self).response

    async def get(self, url, headers=None):
        type(self).profile_calls.append({"url": url, "headers": headers})
        answer = type(self).profile
        if isinstance(answer, BaseException):
            raise answer
        return answer if answer is not None else _fake_response(payload={"emailAddress": "student@school.example"})


class OAuthLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        _, self.platform_path = build_and_migrate(Path(self.tempdir.name))
        self.encryption_key = Fernet.generate_key().decode()
        self.env_patcher = mock.patch.dict(
            "os.environ",
            {
                "GOOGLE_OAUTH_CLIENT_ID": "test-client-id",
                "GOOGLE_OAUTH_CLIENT_SECRET": "test-client-secret",
                # A developer's own .env must not decide whether a connection is refused as another account.
                "PIPELINE_OUTREACH_ACCOUNT": "",
            },
        )
        self.env_patcher.start()
        FakeAsyncClient.profile = None
        FakeAsyncClient.profile_calls = []

    def tearDown(self):
        self.env_patcher.stop()
        self.tempdir.cleanup()

    def test_begin_oauth_requires_configured_client_id_and_known_provider(self):
        with closing(connect_product(self.platform_path)) as conn:
            with mock.patch.dict("os.environ", {"GOOGLE_OAUTH_CLIENT_ID": ""}, clear=False):
                with self.assertRaises(ValueError):
                    connections.begin_oauth(conn, "google", "https://localhost:8765/callback", user_id=LOCAL_USER_ID)
            with self.assertRaises(ValueError):
                connections.begin_oauth(conn, "unknown-provider", "https://localhost:8765/callback", user_id=LOCAL_USER_ID)

    def test_begin_oauth_builds_pkce_url_and_persists_state(self):
        with closing(connect_product(self.platform_path)) as conn:
            result = connections.begin_oauth(conn, "google", "https://localhost:8765/callback", user_id=LOCAL_USER_ID)
            url = result["authorization_url"]
            self.assertIn("accounts.google.com", url)
            self.assertIn("client_id=test-client-id", url)
            row = conn.execute("SELECT * FROM oauth_states").fetchone()
            self.assertEqual(row["provider"], "google")
            self.assertIsNone(row["consumed_at"])
            # The stored verifier must hash to the challenge embedded in the URL.
            derived = (
                base64.urlsafe_b64encode(hashlib.sha256(row["code_verifier"].encode()).digest())
                .decode()
                .rstrip("=")
            )
            self.assertEqual(challenge_from_url(url), derived)

    def test_complete_oauth_exchanges_code_stores_encrypted_tokens_consumes_state(self):
        FakeAsyncClient.response = _fake_response(payload={"access_token": "at-secret", "refresh_token": "rt-secret"})
        FakeAsyncClient.captured = None
        with closing(connect_product(self.platform_path)) as conn:
            begin = connections.begin_oauth(conn, "google", "https://localhost:8765/callback", user_id=LOCAL_USER_ID)
            state = state_from_url(begin["authorization_url"])
            with mock.patch.object(connections.httpx, "AsyncClient", FakeAsyncClient):
                record = asyncio.run(
                    connections.complete_oauth(conn, "google", state, "auth-code-1", self.encryption_key, user_id=LOCAL_USER_ID)
                )
            self.assertEqual(record["provider"], "google")
            self.assertEqual(record["status"], "connected")
            row = conn.execute("SELECT * FROM connector_accounts WHERE id=?", (record["id"],)).fetchone()
            fernet = Fernet(self.encryption_key.encode())
            self.assertEqual(fernet.decrypt(row["encrypted_access_token"].encode()).decode(), "at-secret")
            self.assertEqual(fernet.decrypt(row["encrypted_refresh_token"].encode()).decode(), "rt-secret")
            self.assertEqual(FakeAsyncClient.captured["data"]["code"], "auth-code-1")
            self.assertIn("code_verifier", FakeAsyncClient.captured["data"])
            consumed = conn.execute("SELECT consumed_at FROM oauth_states").fetchone()["consumed_at"]
            self.assertIsNotNone(consumed)
            # State is single-use.
            with mock.patch.object(connections.httpx, "AsyncClient", FakeAsyncClient):
                with self.assertRaises(ValueError):
                    asyncio.run(
                        connections.complete_oauth(conn, "google", state, "auth-code-2", self.encryption_key, user_id=LOCAL_USER_ID)
                    )

    def test_complete_oauth_rejects_provider_error_without_storing_tokens(self):
        FakeAsyncClient.response = _fake_response(status_code=400, payload={"error": "bad_grant"})
        with closing(connect_product(self.platform_path)) as conn:
            begin = connections.begin_oauth(conn, "google", "https://localhost:8765/callback", user_id=LOCAL_USER_ID)
            state = state_from_url(begin["authorization_url"])
            with mock.patch.object(connections.httpx, "AsyncClient", FakeAsyncClient):
                with self.assertRaises(ValueError):
                    asyncio.run(connections.complete_oauth(conn, "google", state, "code", self.encryption_key, user_id=LOCAL_USER_ID))
            stored = conn.execute("SELECT COUNT(*) FROM connector_accounts").fetchone()[0]
            self.assertEqual(stored, 0)

    def test_reconnect_preserves_refresh_token_when_provider_omits_a_new_one(self):
        fernet = Fernet(self.encryption_key.encode())
        with closing(connect_product(self.platform_path)) as conn:
            FakeAsyncClient.response = _fake_response(
                payload={"access_token": "first-access", "refresh_token": "durable-refresh"}
            )
            first = connections.begin_oauth(
                conn, "google", "https://localhost:8765/callback", user_id=LOCAL_USER_ID
            )
            with mock.patch.object(connections.httpx, "AsyncClient", FakeAsyncClient):
                asyncio.run(
                    connections.complete_oauth(
                        conn, "google", state_from_url(first["authorization_url"]), "code-1",
                        self.encryption_key, user_id=LOCAL_USER_ID,
                    )
                )

            FakeAsyncClient.response = _fake_response(payload={"access_token": "second-access"})
            second = connections.begin_oauth(
                conn, "google", "https://localhost:8765/callback", user_id=LOCAL_USER_ID
            )
            with mock.patch.object(connections.httpx, "AsyncClient", FakeAsyncClient):
                asyncio.run(
                    connections.complete_oauth(
                        conn, "google", state_from_url(second["authorization_url"]), "code-2",
                        self.encryption_key, user_id=LOCAL_USER_ID,
                    )
                )

            row = conn.execute(
                "SELECT encrypted_access_token, encrypted_refresh_token FROM connector_accounts WHERE user_id=? AND provider='google'",
                (LOCAL_USER_ID,),
            ).fetchone()
            self.assertEqual(fernet.decrypt(row["encrypted_access_token"].encode()).decode(), "second-access")
            self.assertEqual(fernet.decrypt(row["encrypted_refresh_token"].encode()).decode(), "durable-refresh")

    def complete(self, conn, provider, payload, code):
        FakeAsyncClient.response = _fake_response(payload=payload)
        begin = connections.begin_oauth(conn, provider, "https://localhost:8765/callback", user_id=LOCAL_USER_ID)
        with mock.patch.object(connections.httpx, "AsyncClient", FakeAsyncClient):
            return asyncio.run(connections.complete_oauth(
                conn, provider, state_from_url(begin["authorization_url"]), code, self.encryption_key, user_id=LOCAL_USER_ID,
            ))

    def test_the_grant_time_follows_the_refresh_token(self):
        with closing(connect_product(self.platform_path)) as conn:
            def granted():
                return conn.execute(
                    "SELECT token_granted_at FROM connector_accounts WHERE user_id=? AND provider='gmail_drafts'", (LOCAL_USER_ID,),
                ).fetchone()[0]

            record = self.complete(conn, "gmail_drafts", {"access_token": "a1", "refresh_token": "r1"}, "code-1")
            self.assertTrue(granted(), "a new refresh token is a new grant")
            with conn:
                conn.execute(
                    "UPDATE connector_accounts SET token_granted_at='2026-09-01T00:00:00+00:00', status='error', "
                    "last_error='Google refused to renew the connection'"
                )
            self.complete(conn, "gmail_drafts", {"access_token": "a2"}, "code-2")
            self.assertEqual(granted(), "2026-09-01T00:00:00+00:00", "the refresh token was kept, so its grant time is too")
            row = conn.execute("SELECT status, last_error FROM connector_accounts WHERE id=?", (record["id"],)).fetchone()
            self.assertEqual((row["status"], row["last_error"]), ("connected", ""), "reconnecting clears the old error")
            self.complete(conn, "gmail_drafts", {"access_token": "a3", "refresh_token": "r2"}, "code-3")
            self.assertNotEqual(granted(), "2026-09-01T00:00:00+00:00")
            connections.disconnect_provider(conn, record["id"], user_id=LOCAL_USER_ID)
            self.assertIsNone(granted())

    def gmail_row(self, conn):
        return conn.execute(
            "SELECT scopes_json, account_email FROM connector_accounts WHERE user_id=? AND provider='gmail_drafts'", (LOCAL_USER_ID,),
        ).fetchone()

    def test_a_gmail_connection_stores_the_account_it_signed_into_and_the_scopes_google_granted(self):
        granted = "https://www.googleapis.com/auth/gmail.readonly https://www.googleapis.com/auth/gmail.modify"
        with closing(connect_product(self.platform_path)) as conn:
            with mock.patch.dict("os.environ", {"PIPELINE_OUTREACH_ACCOUNT": "Student@School.example"}):
                self.complete(conn, "gmail_drafts", {"access_token": "a1", "refresh_token": "r1", "scope": granted}, "code-1")
            row = self.gmail_row(conn)
            self.assertEqual(row["account_email"], "student@school.example")
            self.assertEqual(json.loads(row["scopes_json"]), sorted(granted.split()), "what Google granted, not what was asked")
            call = FakeAsyncClient.profile_calls[0]
            self.assertEqual(call["url"], connections.GMAIL_PROFILE_URL)
            self.assertEqual(call["headers"], {"Authorization": "Bearer a1"})
            # A token response that names no scopes falls back to the scopes asked for.
            self.complete(conn, "gmail_drafts", {"access_token": "a2"}, "code-2")
            self.assertEqual(json.loads(self.gmail_row(conn)["scopes_json"]), connections.OAUTH_PROVIDERS["gmail_drafts"]["scopes"])

    def test_gmail_asks_for_the_modify_scope_as_well(self):
        scopes = connections.OAUTH_PROVIDERS["gmail_drafts"]["scopes"]
        for name in ("gmail.compose", "gmail.readonly", "gmail.modify"):
            self.assertIn(f"https://www.googleapis.com/auth/{name}", scopes)

    def test_a_connection_from_another_google_account_is_refused_before_anything_is_saved(self):
        FakeAsyncClient.profile = _fake_response(payload={"emailAddress": "someone.else@elsewhere.example"})
        with closing(connect_product(self.platform_path)) as conn:
            with mock.patch.dict("os.environ", {"PIPELINE_OUTREACH_ACCOUNT": "student@school.example"}):
                with self.assertRaises(ValueError) as caught:
                    self.complete(conn, "gmail_drafts", {"access_token": "a1", "refresh_token": "r1"}, "code-1")
            self.assertEqual(
                str(caught.exception),
                "Google signed in as someone.else@elsewhere.example, but this pipeline's mailbox is student@school.example. "
                "Connect again and choose student@school.example.",
            )
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM connector_accounts").fetchone()[0], 0)
            self.assertIsNone(conn.execute("SELECT consumed_at FROM oauth_states").fetchone()["consumed_at"], "the state is not spent")

    def test_a_connection_whose_account_cannot_be_confirmed_is_not_saved(self):
        unreadable = _fake_response(payload={})
        unreadable.json = mock.Mock(side_effect=ValueError("not json"))
        cases = {
            "403": (_fake_response(status_code=403, payload={"error": {"message": "Insufficient Permission"}}),
                    "Google did not grant the Gmail permissions; connect again and tick every box on Google's screen"),
            "500": (_fake_response(status_code=500), "Could not confirm which Gmail account connected; try connecting again"),
            "no address": (_fake_response(payload={"historyId": "1"}), "Could not confirm which Gmail account connected; try connecting again"),
            "not json": (unreadable, "Could not confirm which Gmail account connected; try connecting again"),
            "unreachable": (httpx.ConnectError("no route"), "Could not confirm which Gmail account connected; try connecting again"),
        }
        with closing(connect_product(self.platform_path)) as conn:
            for name, (answer, sentence) in cases.items():
                with self.subTest(name):
                    FakeAsyncClient.profile = answer
                    with self.assertRaises(ValueError) as caught:
                        self.complete(conn, "gmail_drafts", {"access_token": "a1", "refresh_token": "r1"}, f"code-{name}")
                    self.assertEqual(str(caught.exception), sentence)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM connector_accounts").fetchone()[0], 0)

    def test_a_403_says_why_when_google_gave_a_reason(self):
        api_off = "Enable the Gmail API in your Google Cloud project (README, Gmail drafts setup), then connect again"
        try_again = "Could not confirm which Gmail account connected; try connecting again"
        tick = "Google did not grant the Gmail permissions; connect again and tick every box on Google's screen"
        cases = {
            "api off (errors)": ({"error": {"status": "PERMISSION_DENIED", "errors": [{"reason": "accessNotConfigured"}]}}, api_off),
            "api off (details)": ({"error": {"status": "PERMISSION_DENIED", "details": [{"reason": "SERVICE_DISABLED"}]}}, api_off),
            "rate limit": ({"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}, try_again),
            "quota": ({"error": {"status": "RESOURCE_EXHAUSTED"}}, try_again),
            "insufficient scope": ({"error": {"status": "PERMISSION_DENIED", "errors": [{"reason": "insufficientPermissions"}]}}, tick),
            "error that is not an object": ({"error": "forbidden"}, tick),
            "reasons that are not lists of objects": ({"error": {"errors": ["accessNotConfigured"], "details": None}}, tick),
        }
        with closing(connect_product(self.platform_path)) as conn:
            for name, (payload, sentence) in cases.items():
                with self.subTest(name):
                    FakeAsyncClient.profile = _fake_response(status_code=403, payload=payload)
                    with self.assertRaises(ValueError) as caught:
                        self.complete(conn, "gmail_drafts", {"access_token": "a1", "refresh_token": "r1"}, f"code-{name}")
                    self.assertEqual(str(caught.exception), sentence)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM connector_accounts").fetchone()[0], 0)

    def test_only_a_gmail_connection_is_asked_which_account_it_is(self):
        # Microsoft's answer carries no address either; neither provider may be asked, and neither stores one.
        ids = {"MICROSOFT_OAUTH_CLIENT_ID": "ms-id", "MICROSOFT_OAUTH_CLIENT_SECRET": "ms-secret"}
        with closing(connect_product(self.platform_path)) as conn, mock.patch.dict("os.environ", ids):
            for provider in ("google", "microsoft"):
                with self.subTest(provider):
                    self.complete(conn, provider, {"access_token": "a1", "refresh_token": "r1"}, f"code-{provider}")
                    self.assertEqual(FakeAsyncClient.profile_calls, [])
                    row = conn.execute("SELECT account_email FROM connector_accounts WHERE provider=?", (provider,)).fetchone()
                    self.assertEqual(row["account_email"], "")

    def test_complete_oauth_rejects_malformed_encryption_key_before_writes(self):
        FakeAsyncClient.response = _fake_response(payload={"access_token": "at-secret"})
        with closing(connect_product(self.platform_path)) as conn:
            with mock.patch.dict(
                "os.environ",
                {"MICROSOFT_OAUTH_CLIENT_ID": "ms-id", "MICROSOFT_OAUTH_CLIENT_SECRET": "ms-secret"},
            ):
                begin = connections.begin_oauth(conn, "microsoft", "https://localhost:8765/callback", user_id=LOCAL_USER_ID)
                state = state_from_url(begin["authorization_url"])
                with mock.patch.object(connections.httpx, "AsyncClient", FakeAsyncClient):
                    with self.assertRaises(ValueError):
                        asyncio.run(
                            connections.complete_oauth(conn, "microsoft", state, "code", "not-a-fernet-key", user_id=LOCAL_USER_ID)
                        )
                stored = conn.execute("SELECT COUNT(*) FROM connector_accounts").fetchone()[0]
                self.assertEqual(stored, 0)


if __name__ == "__main__":
    unittest.main()
