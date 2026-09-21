"""Fixtures for the browser-driven UI/UX suite.

The suite boots the real FastAPI application against a seeded temporary database
and drives it with Playwright. Nothing here touches the developer's own
``data/platform.db``: every run migrates a throwaway copy of the legacy fixture
data already used by the unittest suite, so results are deterministic.

This directory deliberately has no ``__init__.py``. ``python -m unittest discover
-s tests`` only recurses into importable packages, so the stdlib suite keeps
ignoring these pytest-only modules.
"""

from __future__ import annotations

import os
import re
import shutil
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
import uvicorn

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
# The unittest suite keeps its shared fixture builders in tests/helpers_platform.py.
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

from opportunity_app import STATIC_DIR  # noqa: E402
from opportunity_app.api import create_app  # noqa: E402

from helpers_platform import build_and_migrate  # noqa: E402

import outreach_fakes  # noqa: E402

OWNER_TOKEN = "ui-suite-owner-token"
EMPLOYER_TOKEN = "ui-suite-employer-token"
ADMIN_TOKEN = "ui-suite-admin-token"

# Routes the application serves as HTML. The smoke, accessibility, and responsive
# suites all read these, so a new page only has to be registered in one place.
PUBLIC_ROUTES = ("/market",)
AUTHENTICATED_VIEWS = ("discover", "urgent", "saved", "applications", "outreach", "prepare", "agent", "profile")

# A 401 on the unauthenticated bootstrap is the auth gate working as designed:
# app.js probes /api/v1/session on load and renders the sign-in card on rejection.
EXPECTED_STATUS_BY_PATH = {"/api/v1/session": {401}}
IGNORED_REQUEST_SUFFIXES = ("/favicon.ico",)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--update-visual-baselines",
        action="store_true",
        default=False,
        help="Rewrite screenshot baselines for this platform instead of comparing against them.",
    )


# Populated by the accessibility and responsive suites when they skip over a
# known defect, and printed at the end of the run. Quarantined debt that nobody
# ever sees again is just deleted debt.
QUARANTINED: dict[str, set[str]] = {}


def record_quarantined(rule: str, where: str) -> None:
    QUARANTINED.setdefault(rule, set()).add(where)


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    if not QUARANTINED:
        return
    terminalreporter.write_sep("=", "known UI defects (not failing the build)")
    for rule in sorted(QUARANTINED):
        terminalreporter.write_line(f"  {rule}: {', '.join(sorted(QUARANTINED[rule]))}")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass(frozen=True)
class LiveServer:
    """A running app plus the means to rewind its database.

    One uvicorn instance serves the whole session, because pytest-playwright's
    ``browser_context_args`` is session-scoped and so ``base_url`` must be too.
    Isolation comes from ``reset``: the app opens and closes a SQLite connection
    per request, so between tests the file is free to be replaced with a pristine
    copy taken immediately after migration. That keeps every test starting from
    identical data without this fixture having to know the schema.
    """

    url: str
    live_path: Path
    pristine_path: Path

    def reset(self) -> None:
        for sidecar in ("-wal", "-shm"):
            # A stale write-ahead log would be replayed over the pristine copy.
            self._remove(Path(str(self.live_path) + sidecar))
        shutil.copyfile(self.pristine_path, self.live_path)

    @staticmethod
    def _remove(path: Path, attempts: int = 25) -> None:
        """Delete a sidecar, waiting briefly for the server to let go of it.

        The app closes its connection in a request's `finally`, but the response
        reaches the browser first, so the next test can begin resetting while
        that close is still landing. Windows then refuses the unlink with
        WinError 32 and the *next* test errors in setup rather than this one
        failing -- which reads as an unrelated flake. Retrying for up to a
        second costs nothing on the normal path, where the first attempt works.
        """

        for attempt in range(attempts):
            try:
                path.unlink(missing_ok=True)
                return
            except PermissionError:
                if attempt == attempts - 1:
                    raise
                time.sleep(0.04)


@pytest.fixture(scope="session")
def live_server(tmp_path_factory: pytest.TempPathFactory):
    """Serve the real app over HTTP against a seeded temporary database."""
    root = tmp_path_factory.mktemp("ui-platform")
    _, platform_path = build_and_migrate(root)
    pristine_path = root / "pristine.db"
    shutil.copyfile(platform_path, pristine_path)
    # Approved outreach drafts open a Gmail compose link for this account. Set
    # before the app loads .env, which never overrides a variable already set.
    os.environ["PIPELINE_OUTREACH_COMPOSE"] = "gmail"
    os.environ["PIPELINE_OUTREACH_ACCOUNT"] = outreach_fakes.COMPOSE_ACCOUNT

    app = create_app(
        db_path=platform_path,
        access_token=OWNER_TOKEN,
        employer_token=EMPLOYER_TOKEN,
        admin_token=ADMIN_TOKEN,
        static_dir=STATIC_DIR,
        # Uploads must land in the temp tree, never in the repo's data/ directory.
        resume_storage=root / "resumes",
        capture_storage=root / "captures",
        interview_storage=root / "mock-interviews",
        # Every test shares 127.0.0.1, so the per-IP sliding window sees the whole
        # suite as one client and starts returning 429 partway through. The limiter
        # is covered by the API-level unittest suite; here it only adds flakiness.
        rate_limit_per_minute=1_000_000,
        # Outreach never reaches a model, a company website, or a web search here.
        outreach_provider_factory=outreach_fakes.provider_factory,
        outreach_draft_provider="anthropic",
        outreach_contact_client_factory=outreach_fakes.contact_client,
        outreach_contact_delay=0,
        outreach_discovery_manager=outreach_fakes.discovery_manager(platform_path, root / "outreach-reports"),
        outreach_recontact_manager=outreach_fakes.recontact_manager(platform_path),
        system_status=outreach_fakes.system_status(root / "system-status"),
        board_tracker=outreach_fakes.board_tracker(root / "boards"),
        outreach_settings=outreach_fakes.outreach_settings(root),
        typesafe_client_factory=outreach_fakes.FakeTypeSafeClient,
    )
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 30
    while not server.started:
        if time.monotonic() > deadline:
            server.should_exit = True
            raise RuntimeError("uvicorn did not start within 30s")
        if not thread.is_alive():
            raise RuntimeError("uvicorn thread exited before the server started")
        time.sleep(0.05)

    try:
        yield LiveServer(f"http://127.0.0.1:{port}", platform_path, pristine_path)
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture(scope="session")
def base_url(live_server: LiveServer) -> str:
    """Override pytest-playwright's base_url so page.goto('/') hits our server."""
    return live_server.url


@pytest.fixture(autouse=True)
def pristine_database(live_server: LiveServer) -> None:
    """Rewind the database before every test so saves and passes cannot leak."""
    live_server.reset()


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict) -> dict:
    """Pin a desktop viewport so layout assertions do not drift with the runner."""
    return {**browser_context_args, "viewport": {"width": 1280, "height": 900}}


class PageDefects:
    """Collects the runtime failures a page reports while a test drives it."""

    def __init__(self) -> None:
        self.console: list[str] = []
        self.exceptions: list[str] = []
        self.failed_requests: list[str] = []
        self.server_errors: list[str] = []

    @property
    def all(self) -> list[str]:
        return self.exceptions + self.server_errors + self.failed_requests + self.console

    def report(self) -> str:
        sections = (
            ("Uncaught page exceptions", self.exceptions),
            ("Server errors (5xx)", self.server_errors),
            ("Failed requests", self.failed_requests),
            ("Console errors", self.console),
        )
        lines: list[str] = []
        for heading, entries in sections:
            if entries:
                lines.append(f"{heading}:")
                lines.extend(f"  - {entry}" for entry in entries)
        return "\n".join(lines)


def _is_expected(url: str, status: int) -> bool:
    path = url.split("?", 1)[0]
    for suffix, allowed in EXPECTED_STATUS_BY_PATH.items():
        if path.endswith(suffix) and status in allowed:
            return True
    return False


# Chromium also reports a failed fetch as a console error, separately from the
# response event, e.g. "Failed to load resource: the server responded with a
# status of 401 (Unauthorized)". Recognise those so one expected status does not
# have to be allow-listed twice.
_RESOURCE_STATUS = re.compile(r"status of (\d{3})")


def _is_expected_console_error(text: str, url: str) -> bool:
    match = _RESOURCE_STATUS.search(text)
    return bool(match) and _is_expected(url, int(match.group(1)))


@pytest.fixture
def defects(page) -> PageDefects:
    """Attach runtime listeners to the page and expose what they captured.

    Requesting this fixture only records; ``page_is_clean`` is what turns a
    recording into a failure. A test that intentionally provokes an error can
    request ``defects`` and assert on it under ``@pytest.mark.allow_page_errors``.
    """
    collected = PageDefects()

    def on_console(message) -> None:
        if message.type != "error":
            return
        url = message.location.get("url", "")
        if url.endswith(IGNORED_REQUEST_SUFFIXES) or _is_expected_console_error(message.text, url):
            return
        collected.console.append(f"{message.text} ({url or 'unknown location'})")

    def on_request_failed(request) -> None:
        if request.url.endswith(IGNORED_REQUEST_SUFFIXES):
            return
        failure = request.failure or "unknown failure"
        # A reload or navigation cancels in-flight fetches. That is the browser
        # doing its job, not the application failing, and treating it as a defect
        # makes every test that navigates during a request intermittently red.
        if "ERR_ABORTED" in failure:
            return
        collected.failed_requests.append(f"{request.method} {request.url} -> {failure}")

    def on_response(response) -> None:
        if response.status < 400 or response.url.endswith(IGNORED_REQUEST_SUFFIXES):
            return
        if _is_expected(response.url, response.status):
            return
        entry = f"{response.status} {response.request.method} {response.url}"
        if response.status >= 500:
            collected.server_errors.append(entry)
        else:
            collected.failed_requests.append(entry)

    page.on("console", on_console)
    page.on("pageerror", lambda error: collected.exceptions.append(str(error)))
    page.on("requestfailed", on_request_failed)
    page.on("response", on_response)
    return collected


@pytest.fixture(autouse=True)
def page_is_clean(request: pytest.FixtureRequest):
    """Fail any browser test whose page logged an error, threw, or got a 4xx/5xx.

    Opt out with ``@pytest.mark.allow_page_errors`` when a test deliberately
    exercises a failure path.
    """
    if "page" not in request.fixturenames:
        yield
        return
    collected = request.getfixturevalue("defects")
    yield
    if "allow_page_errors" in request.keywords:
        return
    if collected.all:
        pytest.fail(f"The page reported runtime failures:\n{collected.report()}", pytrace=False)


def sign_in_as_owner(page, token: str = OWNER_TOKEN) -> None:
    """Complete the owner-invitation sign-in and wait for the gate to close."""
    page.wait_for_selector("#auth-gate.is-visible")
    page.fill("#token-input", token)
    page.click("#auth-submit")
    page.wait_for_selector("#auth-gate.is-visible", state="detached", timeout=15_000)


def wait_for_results(page) -> None:
    """Wait until the opportunity deck has finished its first render."""
    page.wait_for_function(
        "() => document.getElementById('results')?.getAttribute('aria-busy') !== 'true'",
        timeout=15_000,
    )


@pytest.fixture
def owner_page(page, base_url: str, pristine_database):
    """A page signed in as the local owner, parked on the Discover view.

    ``pristine_database`` is named explicitly rather than left to autouse ordering
    so the rewind is guaranteed to happen before the first navigation.
    """
    page.goto("/")
    sign_in_as_owner(page)
    wait_for_results(page)
    return page
