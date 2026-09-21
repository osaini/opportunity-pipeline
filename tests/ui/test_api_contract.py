"""API-level regressions found by the schemathesis fuzz run.

These are kept as ordinary tests rather than left to the fuzzer so each one names
a specific defect, reproduces in under a second, and turns green the moment it is
fixed. Run the fuzzer itself with `py -3 scripts/run_api_fuzz.py` to look for new
ones.

They use Playwright's APIRequestContext purely because it is already wired to the
live server; nothing here needs a browser.
"""

from __future__ import annotations

import pytest

# hmac.compare_digest raises TypeError on any str containing a non-ASCII
# character. Every credential comparison in api.py passes user-controlled text
# straight into it, so a non-ASCII token turns what should be a 401 into an
# unhandled exception and a 500. Encoding both sides to bytes before comparing
# fixes all call sites at once.
NON_ASCII_TOKEN = "梵.²"

def test_non_ascii_session_token_is_rejected_not_crashed(page, base_url):
    response = page.request.post("/api/v1/session", data={"token": NON_ASCII_TOKEN})
    assert response.status == 401, f"expected 401, got {response.status}"


def test_non_ascii_invite_token_is_rejected_not_crashed(page, base_url):
    response = page.request.post(
        "/api/v1/auth/register",
        data={
            "invite_token": NON_ASCII_TOKEN,
            "display_name": "Fuzz",
            "email": "fuzz@example.com",
            "password": "FuzzTesting123",
        },
    )
    assert response.status < 500, f"expected a client error, got {response.status}"


def test_ascii_garbage_token_is_rejected_cleanly(page, base_url):
    """The control case: an ASCII credential of the wrong value must 401, not 500."""
    response = page.request.post("/api/v1/session", data={"token": "definitely-not-the-token"})
    assert response.status == 401, f"expected 401, got {response.status}"


def test_health_endpoint_needs_no_credentials(page, base_url):
    response = page.request.get("/api/v1/health")
    assert response.ok, f"health check returned {response.status}"
