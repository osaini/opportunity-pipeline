"""The legacy pipeline's HTTP client: retries, a per-host rate limiter and the curl fallback.

``_HOST_LIMITER`` is the one process-wide limiter that every request here goes through.
"""

from __future__ import annotations

import email.utils
import gzip
import json
import random
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime, timezone
from typing import Any


USER_AGENT = "Opportunity-Pipeline/1.0 (personal research tool)"


class TransientFetchError(RuntimeError):
    """A request that failed for reasons outside the posting: no network, a
    timeout, a throttle or a server error. Typical right after the laptop wakes,
    before Wi-Fi reconnects, and worth retrying where a 404 is not."""


def _is_transient(error: Exception | None) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        return error.code == 429 or error.code >= 500
    return isinstance(error, (urllib.error.URLError, TimeoutError, ConnectionError))


# Minimum gap between two requests to the same host, applied across threads.
# These are unauthenticated public boards with no published rate limit, so the
# figure is judgement, not documentation.
FETCH_HOST_INTERVAL_SECONDS = 0.25

_SOURCE_HOSTS = {
    "greenhouse": "boards-api.greenhouse.io",
    "lever": "api.lever.co",
    "ashby": "api.ashbyhq.com",
    "smartrecruiters": "api.smartrecruiters.com",
    "usajobs": "data.usajobs.gov",
    "adzuna": "api.adzuna.com",
}


def _source_host(source: dict[str, Any]) -> str:
    """The hostname a source's requests land on, for rate limiting.

    Workday is per-tenant -- each employer has its own subdomain -- so those
    sources do not contend with each other. Everything else shares one vendor
    API host. An unrecognised kind groups under its own name, which throttles
    it rather than exempting it.
    """

    kind = source.get("kind", "")
    if kind == "workday":
        return f'{source.get("tenant", "")}.{source.get("datacenter", "")}.myworkdayjobs.com'
    return _SOURCE_HOSTS.get(kind, f"kind:{kind}")


class _HostRateLimiter:
    """Spaces out requests per host, and backs every thread off on a 429.

    Jitter alone is not a rate limiter: four threads on one host can still
    burst, and if they are all throttled at once they retry in lockstep. This
    keeps one next-allowed time per host so a Retry-After from any thread
    delays all of them.
    """

    def __init__(self, interval: float = FETCH_HOST_INTERVAL_SECONDS):
        self._interval = interval
        self._next_allowed: dict[str, float] = {}
        self._lock = threading.Lock()

    def acquire(self, host: str) -> None:
        if not host:
            return
        while True:
            with self._lock:
                now = time.monotonic()
                ready = self._next_allowed.get(host, 0.0)
                if now >= ready:
                    # A little jitter so threads released together do not
                    # re-collide on the next request.
                    self._next_allowed[host] = now + self._interval + random.uniform(0, self._interval)
                    return
                delay = ready - now
            time.sleep(min(delay, 5.0))

    def penalise(self, host: str, seconds: float) -> None:
        """Hold every thread off this host for at least `seconds`."""

        if not host or seconds <= 0:
            return
        with self._lock:
            target = time.monotonic() + seconds
            self._next_allowed[host] = max(self._next_allowed.get(host, 0.0), target)


_HOST_LIMITER = _HostRateLimiter()

def _request_host(url: str) -> str:
    """Throttling key for a URL.

    Taken from the URL itself rather than from a thread-local set by the fetch
    workers: the liveness pass calls these helpers straight from the main
    thread, where a thread-local would be empty and every acquire and penalise
    would quietly do nothing.
    """

    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _retry_after_seconds(error: Exception | None) -> float:
    """Seconds requested by a Retry-After header, if the server sent one."""

    headers = getattr(error, "headers", None)
    if headers is None:
        return 0.0
    raw = headers.get("Retry-After")
    if not raw:
        return 0.0
    try:
        return max(0.0, float(str(raw).strip()))
    except ValueError:
        pass
    # The HTTP-date form. email.utils is stdlib, so honouring it costs nothing
    # and retrying earlier than a server explicitly asked is not acceptable.
    try:
        when = email.utils.parsedate_to_datetime(str(raw).strip())
    except (TypeError, ValueError):
        return 0.0
    if when is None:
        return 0.0
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _backoff_delay(attempt: int) -> float:
    """Randomised exponential backoff, so throttled threads do not re-collide."""

    return random.uniform(0.0, min(8.0, 1.5 * (2**attempt)))


def _decoded_body(response: Any) -> bytes:
    """Read a response, undoing Content-Encoding if the server used one.

    urllib does not decompress. It only sends Accept-Encoding when it is going
    to handle the result itself, so asking for gzip by hand means owning the
    decode -- and a server is free to ignore the request and reply identity,
    which is why this dispatches on what came back rather than on what was
    asked for.

    A body that claims to be gzip and is not raises, which the caller's retry
    and error handling already treat as a failed request.
    """

    raw = response.read()
    encoding = (response.headers.get("Content-Encoding") or "").strip().lower()
    if encoding in ("gzip", "x-gzip"):
        return gzip.decompress(raw)
    if encoding == "deflate":
        return zlib.decompress(raw)
    return raw


def _http_json(
    url: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    retries: int = 2,
) -> Any:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request_headers = {
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
        "User-Agent": USER_AGENT,
    }
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, data=body, method=method, headers=request_headers)
    last_error: Exception | None = None
    host = _request_host(url)
    for attempt in range(retries + 1):
        _HOST_LIMITER.acquire(host)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(_decoded_body(response).decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            # A throttle applies to the host, not to this thread: hold every
            # worker off it, for as long as the server asked if it said.
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                _HOST_LIMITER.penalise(host, _retry_after_seconds(exc) or _backoff_delay(attempt))
            # Some managed Macs have a system trust chain that Python cannot
            # see. curl uses macOS SecureTransport and still verifies TLS.
            if "CERTIFICATE_VERIFY_FAILED" in str(exc):
                try:
                    curl_cmd = [
                        "curl",
                        "--fail",
                        "--compressed",
                        "--silent",
                        "--show-error",
                        "--location",
                        "--max-time",
                        "30",
                        "--request",
                        method,
                    ]
                    for key, value in request_headers.items():
                        curl_cmd += ["--header", f"{key}: {value}"]
                    if body is not None:
                        curl_cmd += ["--data", body.decode("utf-8")]
                    curl_cmd.append(url)
                    result = subprocess.run(
                        curl_cmd,
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=35,
                    )
                    return json.loads(result.stdout)
                except (FileNotFoundError, subprocess.SubprocessError, json.JSONDecodeError) as curl_exc:
                    last_error = curl_exc
            if attempt < retries:
                time.sleep(_backoff_delay(attempt))
    error_type = TransientFetchError if _is_transient(last_error) else RuntimeError
    raise error_type(f"Request failed for {url}: {last_error}")


def request_json(url: str, retries: int = 2) -> Any:
    return _http_json(url, retries=retries)


def request_json_post(url: str, payload: dict[str, Any], retries: int = 2) -> Any:
    return _http_json(url, method="POST", payload=payload, retries=retries)


def request_text(url: str, retries: int = 2) -> tuple[int, str, str]:
    """Fetch a page as text: `(status, final_url, body)`.

    Unlike `request_json` this never raises on an HTTP error status. 404, 403
    and 5xx are exactly the signals `classify_liveness` reads, so they have to
    reach the caller as data rather than as an exception. The final URL matters
    too -- a dead permalink that redirects to a search page is only detectable
    by comparing it against the URL that was requested.
    """
    request = urllib.request.Request(
        url,
        method="GET",
        headers={
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Encoding": "gzip",
            "User-Agent": USER_AGENT,
        },
    )
    last_error: Exception | None = None
    host = _request_host(url)
    for attempt in range(retries + 1):
        _HOST_LIMITER.acquire(host)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.status, response.url, _decoded_body(response).decode(charset, "replace")
        except urllib.error.HTTPError as exc:
            # 4xx/5xx are data for classify_liveness, but a 429 still means
            # back off before the next source touches this host.
            if exc.code == 429:
                _HOST_LIMITER.penalise(host, _retry_after_seconds(exc) or _backoff_delay(attempt))
            body = ""
            try:
                charset = exc.headers.get_content_charset() or "utf-8"
                body = _decoded_body(exc).decode(charset, "replace")
            except (OSError, ValueError, zlib.error):
                # classify_liveness reads this body; an unreadable one must
                # leave it empty rather than abort the check.
                pass
            return exc.code, exc.url or url, body
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if "CERTIFICATE_VERIFY_FAILED" in str(exc):
                try:
                    result = subprocess.run(
                        [
                            "curl",
                            "--compressed",
                            "--silent",
                            "--show-error",
                            "--location",
                            "--max-time",
                            "30",
                            "--user-agent",
                            USER_AGENT,
                            # Status and final URL are appended after the body so
                            # a failing status still returns its page, which is
                            # what carries the closure banner.
                            "--write-out",
                            "\n%{http_code}\t%{url_effective}",
                            url,
                        ],
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=35,
                    )
                    body, _, tail = result.stdout.rpartition("\n")
                    code_text, _, final_url = tail.partition("\t")
                    return int(code_text or 0), final_url or url, body
                except (FileNotFoundError, subprocess.SubprocessError, ValueError) as curl_exc:
                    last_error = curl_exc
            if attempt < retries:
                time.sleep(_backoff_delay(attempt))
    raise RuntimeError(f"Request failed for {url}: {last_error}")
