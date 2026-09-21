"""Render company pages that build their text with JavaScript, for the location check.

Some startup sites send an empty shell and fill it in with JavaScript, so the
plain fetcher in outreach_contacts.py reads nothing from them. This loads such a
page in headless Chromium through Playwright, which is optional: without it, or
without its browser, the location check just has fewer pages to read.

    pip install -r requirements-optional.txt && python -m playwright install chromium

The browser keeps the plain fetcher's rules. Every request a page makes,
including its scripts, frames, and redirects, must be http(s) to a host that
resolves only to public addresses; anything else is aborted, so a page cannot
reach the dashboard or another service on this machine or network. WebSockets,
service workers, and downloads are refused, and images, media, fonts, and styles
are never loaded. The browser starts on the first page that needs it.
"""

from __future__ import annotations

import ipaddress
from typing import Any
from urllib.parse import urlsplit

from .outreach_contacts import MAX_PAGE_BYTES, USER_AGENT, Resolver, _resolve_host, public_web_url_error

SKIPPED_RESOURCES = {"image", "media", "font", "stylesheet"}
NAVIGATION_TIMEOUT_MS = 20_000
# How long a page may keep fetching after its HTML arrives before it is read.
SETTLE_TIMEOUT_MS = 6_000


def request_allowed(url: str, resolve: Resolver, cache: dict[str, bool]) -> bool:
    """Whether the browser may make this request: http(s) to a host with only public addresses."""
    if public_web_url_error(url):
        return False
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    if host not in cache:
        try:
            addresses = resolve(host)
            cache[host] = bool(addresses) and all(ipaddress.ip_address(address).is_global for address in addresses)
        except Exception:  # noqa: BLE001 - an unresolvable host is simply not allowed
            cache[host] = False
    return cache[host]


class PlaywrightRenderer:
    """Headless Chromium behind a request guard. Use as a context manager on one thread."""

    def __init__(self, *, resolve: Resolver = _resolve_host, launch_args: list[str] | None = None) -> None:
        self._resolve = resolve
        self._launch_args = list(launch_args or [])
        self._allowed: dict[str, bool] = {}
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self.unavailable = ""
        self.blocked: list[str] = []

    def __enter__(self) -> "PlaywrightRenderer":
        return self

    def __exit__(self, *_exc: Any) -> None:
        for closer in (
            lambda: self._context and self._context.close(),
            lambda: self._browser and self._browser.close(),
            lambda: self._playwright and self._playwright.stop(),
        ):
            try:
                closer()
            except Exception:  # noqa: BLE001 - shutting down; nothing left to protect
                pass
        self._playwright = self._browser = self._context = None

    def _start(self) -> bool:
        if self._context is not None:
            return True
        if self.unavailable:
            return False
        try:
            from playwright.sync_api import sync_playwright

            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(args=self._launch_args)
            self._context = self._browser.new_context(
                user_agent=USER_AGENT, service_workers="block", accept_downloads=False, java_script_enabled=True,
            )
            self._context.route("**/*", self._guard)
            if hasattr(self._context, "route_web_socket"):
                self._context.route_web_socket("**/*", lambda socket: socket.close())
        except Exception as exc:  # noqa: BLE001 - missing package, missing browser, or no display
            self.unavailable = f"{type(exc).__name__}: {exc}"[:300]
            self.__exit__(None, None, None)
            return False
        return True

    def _guard(self, route: Any) -> None:
        request = route.request
        if request.resource_type in SKIPPED_RESOURCES:
            route.abort()
            return
        if not request_allowed(request.url, self._resolve, self._allowed):
            self.blocked.append(request.url)
            route.abort("blockedbyclient")
            return
        route.continue_()

    def render(self, url: str) -> tuple[str, str] | None:
        """The final URL and the HTML after scripts ran, or None when the page did not load."""
        if not request_allowed(url, self._resolve, self._allowed) or not self._start():
            return None
        page = self._context.new_page()
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
            if response is None or response.status >= 400:
                return None
            try:
                page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
            except Exception:  # noqa: BLE001 - a page that never goes idle is read as it stands
                pass
            return page.url, page.content()[:MAX_PAGE_BYTES]
        except Exception:  # noqa: BLE001 - timeouts and aborted navigations count as not loaded
            return None
        finally:
            page.close()


def default_renderer() -> PlaywrightRenderer | None:
    """A renderer when Playwright is installed, else None. The browser starts only when used."""
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return None
    return PlaywrightRenderer()
