"""Every route renders without a runtime failure.

These are the cheapest tests in the suite and the ones most likely to catch a
regression: ``node --check`` in CI only parses ``app.js``, so until now nothing
proved the 110KB bundle actually executes against the real API responses.

The ``page_is_clean`` autouse fixture in conftest fails any test whose page threw,
logged a console error, or received a 4xx/5xx, so most cases here assert little
beyond "the expected content appeared".
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

import httpx

from conftest import AUTHENTICATED_VIEWS, OWNER_TOKEN, sign_in_as_owner, wait_for_results

VIEW_TITLES = {
    "discover": "Find the roles worth your time.",
    "urgent": "What needs doing next.",
    "saved": "Return to the roles you chose.",
    "applications": "Keep every application moving.",
    "outreach": "Reach the startups before they post.",
    "prepare": "Prepare without inventing a thing.",
    "agent": "Ask your pipeline, then decide.",
    "profile": "Build the profile behind every match.",
}


def test_unauthenticated_root_shows_the_auth_gate(page):
    page.goto("/")
    expect(page.locator("#auth-gate")).to_have_class("auth-gate is-visible")
    expect(page.locator("#auth-title")).to_have_text("Open your pipeline")
    # The workspace must not leak behind the gate before a session exists.
    expect(page.locator("#user-chip")).to_be_hidden()


def test_owner_token_opens_the_workspace(page):
    page.goto("/")
    sign_in_as_owner(page)
    wait_for_results(page)
    expect(page.locator("#page-title")).to_have_text("Find the roles worth your time.")
    expect(page.locator("#results")).to_have_attribute("aria-busy", "false")
    expect(page.locator(".opportunity-card").first).to_be_visible()


def _launch_ticket(base_url: str) -> str:
    minted = httpx.post(
        f"{base_url}/api/v1/auth/launch-ticket", headers={"Authorization": f"Bearer {OWNER_TOKEN}"}
    )
    minted.raise_for_status()
    return minted.json()["ticket"]


def test_a_launch_link_signs_in_without_typing_a_token(page, base_url, pristine_database):
    """What the launcher opens: no gate, the requested view, and no ticket left in the URL."""
    page.goto(f"/saved#launch={_launch_ticket(base_url)}")
    wait_for_results(page)
    expect(page.locator("#saved-nav")).to_have_class("nav-item is-active")
    expect(page.locator("#auth-gate")).not_to_have_class("auth-gate is-visible")
    assert "launch=" not in page.url


@pytest.mark.allow_page_errors
def test_a_used_launch_link_falls_back_to_the_gate(page, base_url, pristine_database, defects):
    ticket = _launch_ticket(base_url)
    httpx.post(f"{base_url}/api/v1/session", json={"launch_ticket": ticket}).raise_for_status()
    page.goto(f"/#launch={ticket}")
    expect(page.locator("#auth-gate")).to_have_class("auth-gate is-visible")
    expect(page.locator("#auth-error")).to_contain_text("already used or expired")
    assert "launch=" not in page.url
    assert not defects.exceptions, defects.report()


@pytest.mark.allow_page_errors
def test_rejected_token_reports_an_error_and_keeps_the_gate_up(page, defects):
    page.goto("/")
    page.wait_for_selector("#auth-gate.is-visible")
    page.fill("#token-input", "not-the-owner-token")
    page.click("#auth-submit")
    expect(page.locator("#auth-error")).not_to_be_empty()
    expect(page.locator("#auth-gate")).to_have_class("auth-gate is-visible")
    # A rejected credential is a 401, never a crash.
    assert not defects.exceptions, defects.report()
    assert not defects.server_errors, defects.report()


@pytest.mark.parametrize("view", AUTHENTICATED_VIEWS)
def test_each_authenticated_view_renders(owner_page, view):
    owner_page.click(f"#{view}-nav")
    wait_for_results(owner_page)
    expect(owner_page.locator(f"#{view}-nav")).to_have_class("nav-item is-active")
    expect(owner_page.locator("#page-title")).to_have_text(VIEW_TITLES[view])
    expect(owner_page.locator("#error-banner")).to_be_hidden()


@pytest.mark.parametrize("view", AUTHENTICATED_VIEWS)
def test_deep_link_rehydrates_for_an_existing_session(owner_page, view):
    """A bookmarked view must come back on reload: initialize() reads location.pathname."""
    path = "/" if view == "discover" else f"/{view}"
    owner_page.goto(path)
    wait_for_results(owner_page)
    expect(owner_page.locator(f"#{view}-nav")).to_have_class("nav-item is-active")


@pytest.mark.parametrize("view", ["saved", "applications", "profile"])
def test_sign_in_preserves_the_requested_destination(page, view):
    page.goto(f"/{view}")
    sign_in_as_owner(page)
    wait_for_results(page)
    expect(page.locator(f"#{view}-nav")).to_have_class("nav-item is-active")


def test_public_market_page_renders_without_a_session(page):
    page.goto("/market")
    expect(page).to_have_title("Weekly Opportunity Market")
    expect(page.locator("#market-issues")).not_to_be_empty()


@pytest.mark.parametrize("path", ["/admin", "/employer"])
def test_role_workspace_pages_render(page, path):
    page.goto(path)
    expect(page.locator("body")).not_to_be_empty()


@pytest.mark.allow_page_errors
@pytest.mark.parametrize("provider", ["google", "microsoft"])
def test_oauth_callback_page_does_not_crash_without_parameters(page, defects, provider):
    """Opened with no code/state, the callback must fail closed, not throw."""
    page.goto(f"/connections/oauth/{provider}/callback")
    expect(page.locator("body")).not_to_be_empty()
    assert not defects.exceptions, defects.report()
    assert not defects.server_errors, defects.report()


def test_unknown_opportunity_id_returns_404_not_a_server_error(owner_page):
    """A stale bookmark should degrade gracefully rather than 500."""
    response = owner_page.request.get("/api/v1/opportunities/does-not-exist")
    assert response.status == 404, f"expected 404, got {response.status}: {response.text()}"


def test_unknown_opportunity_id_is_not_enumerable_without_a_session(page):
    """Auth must be checked before existence, or the 404/401 split leaks valid IDs."""
    page.goto("/")
    response = page.request.get("/api/v1/opportunities/does-not-exist")
    assert response.status == 401, f"expected 401 before authentication, got {response.status}"
