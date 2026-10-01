"""Fetch public web pages safely: the SSRF-guarded layer every outreach crawler, searcher and renderer shares.

SafeFetcher checks the URL's shape and every address its host resolves to before it asks, follows redirects
one checked hop at a time, bounds the body, and can bound the whole fetch by a deadline that also covers the
socket reads of a slow server. Standard library plus httpx and httpcore only: it imports nothing from outreach.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpcore
import httpx

USER_AGENT = "Mozilla/5.0 (compatible; internship-pipeline-outreach/1.0; one student's research)"
MAX_PAGE_BYTES = 2 * 1024 * 1024

Resolver = Callable[[str], list[str]]


@dataclass(frozen=True)
class FetchResult:
    url: str
    status: int
    text: str
    error: str | None = None
    content_type: str = ""


def public_web_url_error(url: str) -> str | None:
    """Validate the URL shape before DNS is consulted."""
    try:
        parsed = urlsplit(str(url or "").strip())
        host = parsed.hostname
        if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
            return "invalid"
        lowered = host.rstrip(".").lower()
        if lowered == "localhost" or lowered.endswith(".localhost"):
            return "private"
        try:
            ipaddress.ip_address(lowered)
        except ValueError:
            return None
        return "private"
    except ValueError:
        return "invalid"


def resolve_host(host: str) -> list[str]:
    return sorted({item[4][0] for item in socket.getaddrinfo(host, None)})


class _FetchClock(threading.local):
    """When the fetch this thread is running must be over (time.monotonic), or None for no limit."""

    until: float | None = None


_FETCH_CLOCK = _FetchClock()


def _within_deadline(timeout: float | None, error: type[Exception]) -> float | None:
    """A socket wait cut down to what is left of the fetch's deadline; ``error`` once none is left."""
    until = _FETCH_CLOCK.until
    if until is None:
        return timeout
    left = until - time.monotonic()
    if left <= 0:
        raise error("the fetch's deadline passed")
    return left if timeout is None else min(timeout, left)


class _DeadlineStream(httpcore.NetworkStream):
    """A connection whose every read and write gives up when the fetch's deadline does.

    httpx's timeout limits each wait on the socket, so a server that sends one
    byte of its status line or headers every few seconds never trips it, and
    ``iter_bytes`` is not reached until the headers are in.
    """

    def __init__(self, inner: httpcore.NetworkStream) -> None:
        self._inner = inner

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._inner.read(max_bytes, _within_deadline(timeout, httpcore.ReadTimeout))

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._inner.write(buffer, _within_deadline(timeout, httpcore.WriteTimeout))

    def close(self) -> None:
        self._inner.close()

    def start_tls(self, ssl_context: Any, server_hostname: str | None = None, timeout: float | None = None) -> httpcore.NetworkStream:
        return _DeadlineStream(self._inner.start_tls(ssl_context, server_hostname, _within_deadline(timeout, httpcore.ConnectTimeout)))

    def get_extra_info(self, info: str) -> Any:
        return self._inner.get_extra_info(info)


class _DeadlineBackend(httpcore.NetworkBackend):
    def __init__(self, inner: httpcore.NetworkBackend) -> None:
        self._inner = inner

    def connect_tcp(self, host: str, port: int, timeout: float | None = None, local_address: str | None = None, socket_options: Any = None) -> httpcore.NetworkStream:
        stream = self._inner.connect_tcp(host, port, _within_deadline(timeout, httpcore.ConnectTimeout), local_address, socket_options)
        return _DeadlineStream(stream)

    def connect_unix_socket(self, path: str, timeout: float | None = None, socket_options: Any = None) -> httpcore.NetworkStream:
        return _DeadlineStream(self._inner.connect_unix_socket(path, _within_deadline(timeout, httpcore.ConnectTimeout), socket_options))

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


def _bound_by_deadline(client: httpx.Client) -> None:
    """Make every real connection of ``client`` obey the fetch deadline (see _DeadlineStream).

    A transport that opens no sockets (a test's MockTransport) needs nothing. A
    real one that cannot be wrapped is an error, not a silent loss of the limit.
    """
    for transport in (client._transport, *client._mounts.values()):
        if not isinstance(transport, httpx.HTTPTransport):
            continue
        pool = getattr(transport, "_pool", None)
        backend = getattr(pool, "_network_backend", None)
        if backend is None:
            raise RuntimeError("This httpx version's connection pool cannot be bounded by a fetch deadline")
        if not isinstance(backend, _DeadlineBackend):
            pool._network_backend = _DeadlineBackend(backend)


class SafeFetcher:
    """Fetch public web pages with bounded bodies and checked redirect hops."""

    def __init__(
        self, client: httpx.Client, resolve: Resolver = resolve_host, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.resolve = resolve
        self.clock = clock
        _bound_by_deadline(client)

    def __enter__(self) -> "SafeFetcher":
        self.client.__enter__()
        return self

    def __exit__(self, *args: Any) -> None:
        self.client.__exit__(*args)

    def fetch(
        self, url: str, *, same_host_only: bool, hop_check: Callable[[str], str | None] | None = None,
        deadline_seconds: float | None = None,
    ) -> FetchResult:
        """hop_check, when given, is asked about every URL before it is requested, redirects included.

        The client's timeout limits each wait for the server, not the whole
        fetch: a server that sends a byte every few seconds never trips it.
        ``deadline_seconds`` bounds the whole fetch, redirects, headers, and body
        together (looking the host up is the operating system's own wait).
        """
        start_host = (urlsplit(url).hostname or "").lower().removeprefix("www.")
        current = url
        stop_at = self.clock() + deadline_seconds if deadline_seconds is not None else None
        for redirects in range(6):
            if stop_at is not None and self.clock() >= stop_at:
                return FetchResult(current, 0, "", "timeout")
            policy = public_web_url_error(current)
            if policy:
                return FetchResult(current, 0, "", policy)
            refused = hop_check(current) if hop_check is not None else None
            if refused:
                return FetchResult(current, 0, "", refused)
            host = (urlsplit(current).hostname or "").lower()
            if same_host_only and host.removeprefix("www.") != start_host:
                return FetchResult(current, 0, "", "left site")
            try:
                addresses = self.resolve(host)
                if not addresses:
                    return FetchResult(current, 0, "", "dns")
                if any(not ipaddress.ip_address(address).is_global for address in addresses):
                    return FetchResult(current, 0, "", "private")
            except Exception:
                return FetchResult(current, 0, "", "dns")
            _FETCH_CLOCK.until = time.monotonic() + (stop_at - self.clock()) if stop_at is not None else None
            try:
                with self.client.stream("GET", current) as response:
                    status = response.status_code
                    if status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            return FetchResult(str(response.url), status, "", "redirect")
                        if redirects == 5:
                            return FetchResult(str(response.url), status, "", "redirect")
                        current = urljoin(str(response.url), location)
                        continue
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > MAX_PAGE_BYTES:
                            return FetchResult(str(response.url), status, "", "too_large")
                        if stop_at is not None and self.clock() >= stop_at:
                            return FetchResult(str(response.url), status, "", "timeout")
                        chunks.append(chunk)
                    encoding = response.encoding or "utf-8"
                    return FetchResult(
                        str(response.url), status, b"".join(chunks).decode(encoding, errors="replace"),
                        content_type=response.headers.get("content-type", ""),
                    )
            except Exception:
                return FetchResult(current, 0, "", "timeout" if stop_at is not None and self.clock() >= stop_at else "network")
            finally:
                _FETCH_CLOCK.until = None
        return FetchResult(current, 0, "", "redirect")


def default_client() -> httpx.Client:
    return httpx.Client(
        timeout=10.0,
        follow_redirects=False,
        verify=True,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
    )


def default_fetcher() -> SafeFetcher:
    return SafeFetcher(default_client())


def close_browser(owner: Any) -> None:
    """Shut down the Playwright browser an owner holds in _context, _browser and _playwright, in that order.

    Shared by the renderer and the form submitter, which start their browsers differently but stop them the same way:
    each step swallows its own error, since there is nothing left to protect, and the owner is left holding none.
    """
    for closer in (
        lambda: owner._context and owner._context.close(),
        lambda: owner._browser and owner._browser.close(),
        lambda: owner._playwright and owner._playwright.stop(),
    ):
        try:
            closer()
        except Exception:  # noqa: BLE001 - shutting down
            pass
    owner._playwright = owner._browser = owner._context = None


def same_site(url: str, domain: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == domain or host.endswith(f".{domain}")


def fetch_site_robots(start: str, fetcher: SafeFetcher) -> tuple[RobotFileParser, bool]:
    """The site's robots.txt rules, and whether the answer is settled enough to reuse.

    A missing or unreadable file allows everything. The flag is True for a file that was read (200) or is
    plainly absent (404, 410), and False for a failed fetch or any other status, which a later try may answer
    differently.
    """
    robots = RobotFileParser()
    response = fetcher.fetch(urljoin(start, "/robots.txt"), same_host_only=True)
    read = not response.error and response.status == 200
    robots.parse(response.text.splitlines() if read else [])
    return robots, read or (not response.error and response.status in (404, 410))


def site_robots(start: str, fetcher: SafeFetcher) -> RobotFileParser:
    """The site's robots.txt rules; a missing or unreadable file allows everything."""
    return fetch_site_robots(start, fetcher)[0]
