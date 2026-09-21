"""The Refresh dialog's status panel, and the sidebar marker that points to it."""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

import outreach_fakes
from conftest import wait_for_results


@pytest.fixture
def broken_setup():
    outreach_fakes.SCHEDULER.installed.discard("outreach")
    outreach_fakes.fail_board("lever:orbit-boards", "HTTP 404 from api.lever.co")
    yield
    outreach_fakes.SCHEDULER.installed.add("outreach")
    outreach_fakes.heal_board("lever:orbit-boards")


def test_a_healthy_setup_shows_its_schedule_and_no_marker(owner_page):
    button = owner_page.locator("#refresh-open")
    expect(button).to_be_visible()
    expect(button).not_to_have_class("text-button needs-attention")
    button.click()
    panel = owner_page.locator("#system-status")
    expect(panel).to_be_visible()
    expect(panel.locator(".system-status-job")).to_have_count(3)
    expect(panel.locator(".system-status-problems")).to_have_count(0)
    expect(panel).to_contain_text("2 boards enabled")


def test_a_missing_schedule_and_a_dead_board_are_flagged_and_fixable(owner_page, broken_setup):
    owner_page.reload()
    wait_for_results(owner_page)
    button = owner_page.locator("#refresh-open")
    expect(button).to_have_class("text-button needs-attention")
    expect(button).to_contain_text("needs attention: Outreach deep search is not scheduled; 1 job board failing")

    button.click()
    panel = owner_page.locator("#system-status")
    expect(panel.locator(".system-status-problems")).to_contain_text("1 job board failing")
    boards = panel.locator("details.system-status-sources")
    expect(boards).to_have_attribute("open", "")
    expect(boards).to_contain_text("Orbit Boards")
    expect(boards).to_contain_text("HTTP 404 from api.lever.co")

    from test_accessibility import _assert_accessible

    _assert_accessible(owner_page, "the status panel with problems")

    panel.get_by_role("button", name="Schedule the outreach deep search").click()
    expect(panel.locator(".system-status-problems")).not_to_contain_text("not scheduled")
    expect(panel.get_by_role("button", name="Schedule the outreach deep search")).to_have_count(0)
    assert "outreach" in outreach_fakes.SCHEDULER.installed


def test_a_board_is_tracked_at_once_only_when_it_names_the_company(owner_page):
    owner_page.locator("#refresh-open").click()
    tracker = owner_page.locator(".board-tracker")
    company = tracker.get_by_label("Company name")

    company.fill("Kestrel Robotics")
    tracker.get_by_role("button", name="Find board").click()
    expect(tracker).to_contain_text("Found Kestrel Robotics's Greenhouse board (kestrelrobotics): 2 postings, 1 matching")
    expect(tracker.locator(".board-tracker-titles li").first).to_have_text("Robotics Intern")
    tracker.get_by_role("button", name="Track this board").click()
    expect(tracker).to_contain_text("Tracking Kestrel Robotics. The next refresh fetches its postings.")

    company.fill("Kestrel Robotics")
    tracker.get_by_role("button", name="Find board").click()
    expect(tracker).to_contain_text("Kestrel Robotics is already tracked (Greenhouse: kestrelrobotics).")

    # Ashby does not name the owner, so the student confirms from the postings.
    company.fill("Wren Motion")
    tracker.get_by_role("button", name="Find board").click()
    expect(tracker).to_contain_text("does not say whose board it is")
    track = tracker.get_by_role("button", name="Track this board")
    expect(track).to_be_disabled()
    tracker.get_by_label("These postings are Wren Motion's").check()
    track.click()
    expect(tracker).to_contain_text("Tracking Wren Motion.")

    company.fill("Nowhere Labs")
    tracker.get_by_role("button", name="Find board").click()
    expect(tracker).to_contain_text("No Greenhouse, Ashby or Lever board with postings was found for Nowhere Labs")

    local = outreach_fakes_local_entries()
    assert [(entry["kind"], entry.get("token") or entry.get("board")) for entry in local] == [
        ("greenhouse", "kestrelrobotics"), ("ashby", "wrenmotion"),
    ]


def outreach_fakes_local_entries():
    import json

    path = outreach_fakes.BOARDS_LOCAL
    entries = json.loads(path.read_text(encoding="utf-8"))["ats_sources"]
    path.unlink()
    return entries
