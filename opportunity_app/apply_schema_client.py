"""The read-only client for Greenhouse's public Job Board API, which the Apply for me check uses.

One GET, with no key: ``boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}?questions=true`` lists the
fields an application form asks. TLS verification stays on, the timeout is 20 seconds, and the pipeline's usual
user agent goes with it. It never writes, never follows a link the listing names, and reaches no other host.

The app is given a client through ``create_app(apply_schema_client_factory=...)``. Only the real product
database gets this one by default; a test, the fuzz sandbox and the sandbox server get None (the check then
answers 503 without a request) or the fake in tests/apply_fake_ats.py, which makes no request at all.
"""

from __future__ import annotations

import gzip
import json
import urllib.error
import urllib.request
import zlib
from typing import Any, Callable, Protocol

from pipeline import USER_AGENT

from .apply_policy import schema_url

TIMEOUT_SECONDS = 20
# A listing is a few hundred kilobytes at most; anything larger is not one.
MAX_BYTES = 4 * 1024 * 1024
API_HOST = "boards-api.greenhouse.io"


class SchemaUnavailable(Exception):
    """Greenhouse did not give a usable listing (any answer but 200 or 404: a timeout, a 5xx, a page that is not JSON)."""


class SchemaClient(Protocol):
    def fetch(self, board_token: str, job_id: str) -> dict[str, Any] | None:
        """The parsed listing, None when Greenhouse answers 404 (the posting is closed or unknown), or SchemaUnavailable."""


class GreenhouseSchemaClient:
    """Reads a listing from the live Job Board API. One request per call, GET only."""

    def fetch(self, board_token: str, job_id: str) -> dict[str, Any] | None:
        url = schema_url(board_token, job_id)
        request = urllib.request.Request(
            url, method="GET", headers={"Accept": "application/json", "Accept-Encoding": "gzip", "User-Agent": USER_AGENT},
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                body = _decoded(response, response.read(MAX_BYTES + 1))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise SchemaUnavailable(f"Greenhouse answered HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise SchemaUnavailable(f"Greenhouse did not answer ({type(exc).__name__})") from exc
        if len(body) > MAX_BYTES:
            raise SchemaUnavailable("Greenhouse's listing was larger than expected")
        try:
            listing = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise SchemaUnavailable("Greenhouse's listing was not JSON") from exc
        if not isinstance(listing, dict):
            raise SchemaUnavailable("Greenhouse's listing was not an object")
        return listing


def _decoded(response: Any, body: bytes) -> bytes:
    if str(response.headers.get("Content-Encoding", "")).lower() == "gzip":
        try:
            return gzip.decompress(body)
        except (OSError, EOFError, zlib.error) as exc:
            raise SchemaUnavailable("Greenhouse's listing could not be read") from exc
    return body


def default_schema_client_factory() -> SchemaClient:
    return GreenhouseSchemaClient()


SchemaClientFactory = Callable[[], SchemaClient]
