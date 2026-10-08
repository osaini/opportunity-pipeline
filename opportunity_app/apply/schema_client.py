"""The read-only clients the Apply for me check uses: Greenhouse's public Job Board API, and Lever's application page.

Greenhouse: one GET, with no key: ``boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}?questions=true`` lists the
fields an application form asks. Lever has no such listing (its page is the schema, docs/phase5-lever-handoff-spec.md 3.2),
so ``LeverPageClient`` makes one GET of the posting's own application page and hands back its HTML.
For both, TLS verification stays on, the timeout is 20 seconds, and the pipeline's usual user agent goes with it. They never
write, never follow a link the answer names, and reach no other host (Lever's client refuses a redirect off the two Lever hosts).

The app is given a client through ``create_app(apply_schema_client_factory=...)`` (Greenhouse's) and
``create_app(apply_page_client_factory=...)`` (Lever's). Only the real product database gets the real ones by default; a test,
the fuzz sandbox and the sandbox server get None (the check then answers 503 without a request, or for Lever says it did not
answer) or the fakes in tests/apply_fake_ats.py, which make no request at all.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
import zlib
from typing import Any, Callable, Protocol

from .greenhouse import schema_url
from .lever import DEFAULT_HOST, LEVER_HOSTS, canonical_url as lever_page_url
from .lever_form import parse_lever_form
from ..opportunities.legacy import USER_AGENT

TIMEOUT_SECONDS = 20
# A listing is a few hundred kilobytes at most; anything larger is not one.
MAX_BYTES = 4 * 1024 * 1024


class SchemaUnavailable(Exception):
    """The ATS did not give a usable listing (any answer but 200 or 404: a timeout, a 429 or 5xx, a page that is not what was asked for)."""


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
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
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
    """The answer's bytes, unpacked when it is gzip, and never more than ``MAX_BYTES + 1`` of them.

    The read from the wire is capped, but a small gzip answer can unpack to gigabytes, so the unpacking is capped too: an answer that would
    pass ``MAX_BYTES`` comes back ``MAX_BYTES + 1`` long, which the caller refuses as too large.
    """
    if str(response.headers.get("Content-Encoding", "")).lower() != "gzip":
        return body
    parts: list[bytes] = []
    size = 0
    try:
        while body:  # a gzip answer may be several members, one after another
            unpacker = zlib.decompressobj(16 + zlib.MAX_WBITS)
            part = unpacker.decompress(body, MAX_BYTES + 1 - size)
            parts.append(part)
            size += len(part)
            if size > MAX_BYTES:
                break
            if not unpacker.eof:
                raise EOFError("the answer ended early")
            body = unpacker.unused_data
    except (OSError, EOFError, zlib.error) as exc:
        raise SchemaUnavailable("The answer could not be read") from exc
    return b"".join(parts)


def default_schema_client_factory() -> SchemaClient:
    return GreenhouseSchemaClient()


SchemaClientFactory = Callable[[], SchemaClient]


class PageClient(Protocol):
    def fetch(self, site: str, job_id: str, host: str) -> str | None:
        """The posting's application page as text, None when the ATS answers 404 (the posting is closed or unknown), or SchemaUnavailable."""


class _SameHostRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect is followed only to https on one of Lever's two hosts: a page that sends the app elsewhere is not read."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        parts = urllib.parse.urlsplit(newurl)
        if parts.scheme != "https" or (parts.hostname or "").lower().rstrip(".") not in LEVER_HOSTS or parts.port not in (None, 443):
            raise urllib.error.HTTPError(req.full_url, code, "redirect off Lever", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class LeverPageClient:
    """Reads a Lever posting's application page: one GET, HTML back. TLS on, 20 seconds, at most 4 MB, no cookies, nothing written."""

    def _open(self, request: urllib.request.Request) -> Any:
        return urllib.request.build_opener(_SameHostRedirects).open(request, timeout=TIMEOUT_SECONDS)

    def fetch(self, site: str, job_id: str, host: str) -> str | None:
        if host not in LEVER_HOSTS:
            raise SchemaUnavailable("Lever did not answer (an address that is not Lever's)")
        request = urllib.request.Request(
            lever_page_url(site, job_id, host), method="GET",
            headers={"Accept": "text/html", "Accept-Encoding": "gzip", "User-Agent": USER_AGENT},
        )
        try:
            with self._open(request) as response:
                body = _decoded(response, response.read(MAX_BYTES + 1))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise SchemaUnavailable(f"Lever answered HTTP {exc.code}") from exc
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as exc:
            raise SchemaUnavailable(f"Lever did not answer ({type(exc).__name__})") from exc
        if len(body) > MAX_BYTES:
            raise SchemaUnavailable("Lever's page was larger than expected")
        return body.decode("utf-8", errors="replace")


class LeverListings:
    """Lever's posting as a listing: the page, read by ``parse_lever_form``, kept in a dict beside the page's own title.

    ``fetch`` is the call a Greenhouse listing client has, so the check reads both the same way. A 404 is None (the posting may be
    closed); anything else that is not a Lever form (a timeout, a 429 or 5xx, a Cloudflare page that is a 200 with no form) is
    SchemaUnavailable: "Lever did not answer", never "closed" (spec 6.0 step 4).
    """

    def __init__(self, pages: PageClient, host: str) -> None:
        self._pages = pages
        self._host = host or DEFAULT_HOST

    def fetch(self, site: str, job_id: str) -> dict[str, Any] | None:
        text = self._pages.fetch(site, job_id, self._host)
        if text is None:
            return None
        form = parse_lever_form(text)
        if form is None:
            raise SchemaUnavailable("Lever's page had no application form")
        return {"lever_form": form, "title": form.posting.company_title, "company_name": ""}


def default_page_client_factory() -> PageClient:
    return LeverPageClient()


PageClientFactory = Callable[[], PageClient]
