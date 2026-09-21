"""Unit tests for the OAuth lifecycle against a mocked provider token endpoint."""

import asyncio
import base64
import hashlib
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cryptography.fernet import Fernet

from opportunity_app import connections
from opportunity_app.schema import connect_product

from opportunity_app.schema import LOCAL_USER_ID

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

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, data=None):
        type(self).captured = {"url": url, "data": data}
        return type(self).response


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
            },
        )
        self.env_patcher.start()

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
