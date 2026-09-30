"""One place that decides whether a test may use a real Chromium, and what happens when it cannot.

Browser tests skip when Playwright's Chromium is not installed, so the default
unit job (which has no Playwright) and a student's machine stay green. The CI
job `browser-python` sets PIPELINE_REQUIRE_BROWSER_TESTS=1, and there a missing
package or a broken launch is an error that shows the launch exception, never a
silent skip: a job that skipped every browser test would be green and prove
nothing.

    from browser_support import requires_chromium

    @requires_chromium
    class SomeBrowserTests(unittest.TestCase): ...

Tests launch headless. The one headed launch test (apply agent) runs only when
PIPELINE_HEADED_TESTS=1, which only the CI job sets, under xvfb, so a plain
`python -m unittest discover` on the student's machine never opens a window.
"""

from __future__ import annotations

import os
import traceback
import unittest
from functools import lru_cache
from typing import Any

REQUIRE_ENV = "PIPELINE_REQUIRE_BROWSER_TESTS"
HEADED_ENV = "PIPELINE_HEADED_TESTS"
INSTALL_HINT = "Install Playwright and Chromium: python -m playwright install chromium"


def browser_tests_required() -> bool:
    return os.environ.get(REQUIRE_ENV) == "1"


def headed_tests_enabled() -> bool:
    return os.environ.get(HEADED_ENV) == "1"


@lru_cache(maxsize=1)
def chromium_launch_error() -> str:
    """"" when a headless Chromium starts and stops, else why it did not (the exception and its traceback)."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as playwright:
            playwright.chromium.launch().close()
        return ""
    except BaseException as exc:  # noqa: BLE001 - no package, no browser, or a broken launch: all mean "not available"
        return f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"


def chromium_available() -> bool:
    return not chromium_launch_error()


def requires_chromium(cls: Any) -> Any:
    """Class decorator: run the tests with Chromium, skip them without it, and fail them when it is required.

    With PIPELINE_REQUIRE_BROWSER_TESTS=1 a Chromium that will not launch makes
    every test in the class error with the launch exception, so nothing is skipped.
    """
    problem = chromium_launch_error()
    if not problem:
        return cls
    if not browser_tests_required():
        return unittest.skip(f"Playwright's Chromium is not installed ({INSTALL_HINT})")(cls)

    def setUpClass(klass: Any) -> None:
        raise RuntimeError(f"{REQUIRE_ENV}=1 but Chromium could not be launched. {INSTALL_HINT}\n{problem}")

    cls.setUpClass = classmethod(setUpClass)
    return cls


requires_headed = unittest.skipUnless(
    headed_tests_enabled(), f"headed browser tests run only with {HEADED_ENV}=1 (the CI job sets it under xvfb)",
)
