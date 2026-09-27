"""Automation in the browser: the Profile section, the app-wide banner, the badge, and timeline attribution.

The server runs in this process, so a test can register a feature of its own in
automation.FEATURES and seed the ledger straight into the live database, the
same way the automatic steps would.
"""

from __future__ import annotations

import re
from contextlib import closing
from uuid import uuid4

import pytest
from playwright.sync_api import expect

from conftest import OWNER_TOKEN, wait_for_results
from opportunity_app import automation
from opportunity_app.actions import update_application
from opportunity_app.automation import OFF_SHADOW_ON, Feature
from opportunity_app.schema import connect_product, utc_now

BEARER = {"Authorization": f"Bearer {OWNER_TOKEN}"}
USER = "local-user"
SWITCH = Feature("ui_test_switch", "Stage mover", "Moves an application when an email says so", "applications", "internal")
SHADOWED = Feature("ui_test_shadow", "Careful stage mover", "Moves an application after a trial in shadow", "applications", "internal", OFF_SHADOW_ON)


@pytest.fixture
def test_features():
    for feature in (SWITCH, SHADOWED):
        automation.register(feature)
    yield
    for feature in (SWITCH, SHADOWED):
        automation.FEATURES.pop(feature.key, None)


def perform(live_server, *, feature=SWITCH.key, mode="on", after=None, auto=True, evidence=None, summary="Moved Orbit Systems to interview"):
    """One automatic stage change on the seeded Orbit application (stage 'applied')."""
    with closing(connect_product(live_server.live_path)) as conn:
        automation.set_mode(conn, USER, feature, mode)
        return automation.perform(
            conn, user_id=USER, feature=feature, action_type="application.stage", subject_kind="application",
            subject_id="app-job-b", after=after or {"stage": "interview"},
            evidence=evidence if evidence is not None else {"subject": "Interview invitation from Orbit Systems"},
            summary=summary, basis="rule:ui-test", confidence=0.9, idempotency_key=f"ui:{uuid4().hex}", auto=auto,
        )


def stage(live_server):
    with closing(connect_product(live_server.live_path)) as conn:
        return conn.execute("SELECT stage FROM applications WHERE id='app-job-b'").fetchone()[0]


def seed_in_flight(live_server):
    """An email already handed to Gmail: past stopping, so a pause must say so."""
    now = utc_now()
    with closing(connect_product(live_server.live_path)) as conn:
        with conn:
            conn.execute(
                "INSERT INTO outreach_targets(id, user_id, company, created_at, updated_at) VALUES('t-ui', ?, 'Bovi Robotics', ?, ?)",
                (USER, now, now),
            )
            conn.execute(
                "INSERT INTO outreach_scheduled_sends(target_id, user_id, kind, fingerprint, send_at, timezone, label, state, created_at, updated_at) "
                "VALUES('t-ui', ?, 'initial', 'f', ?, 'UTC', 'Mon, Sep 28, 9:12 AM CDT', 'transmitting', ?, ?)",
                (USER, now, now, now),
            )


def open_profile(page):
    page.click("#profile-nav")
    wait_for_results(page)
    expect(page.locator(".automation-section")).to_be_visible()
    return page.locator(".automation-section")


def test_the_profile_page_shows_what_the_app_does_on_its_own(owner_page):
    banner = owner_page.locator("#automation-banner")
    expect(banner).to_have_attribute("hidden", "")
    section = open_profile(owner_page)
    expect(section.get_by_role("heading", name="What the app does on its own")).to_be_visible()
    expect(section).to_contain_text("Nothing here is on until you turn it on.")
    assert section.locator(".automation-group > h4").all_inner_texts() == ["Outreach", "Applications", "Notifications"], \
        "a group with no features (Discovery) is left out"
    expect(section.locator("#automation-mode-auto_drafts")).not_to_be_checked()
    expect(section.locator('[data-automation-feature="scheduled_sending"] .chip')).to_have_text("External")
    expect(section.locator('[data-automation-feature="auto_drafts"] .chip')).to_have_count(0)
    expect(section.locator("#automation-pause")).to_have_text("Pause all automation")
    expect(section.locator(".automation-waiting")).to_contain_text("Nothing is waiting for you.")
    expect(section.locator(".automation-recent")).to_contain_text("Nothing has happened automatically yet.")
    expect(section.locator("#automation-notices-heading")).to_have_text("Notices")
    expect(section).to_contain_text("No unread notices.")
    labels = owner_page.locator("#subnav .subnav-item .subnav-label").all_inner_texts()
    assert labels[:3] == ["All sections", "Career profile", "Automation"], labels
    expect(banner).to_have_attribute("hidden", "")
    expect(owner_page.locator("#profile-badge")).to_be_hidden()
    expect(owner_page.locator("#profile-nav")).to_have_accessible_name("Profile")


def test_a_switch_saves_at_once_and_is_still_on_after_a_reload(owner_page, base_url):
    open_profile(owner_page)
    owner_page.locator("#automation-mode-auto_drafts").check()
    status = owner_page.locator(".automation-group", has=owner_page.locator("#automation-mode-auto_drafts")).locator(".form-status")
    expect(status).to_have_text("Write drafts automatically: on.")
    # Outreach settings reads the same switch.
    assert owner_page.request.get(f"{base_url}/api/v1/outreach/automation", headers=BEARER).json()["auto_drafts"] is True

    owner_page.reload()
    wait_for_results(owner_page)
    expect(owner_page.locator("#automation-mode-auto_drafts")).to_be_checked()


def test_the_desktop_pop_up_switch_and_its_automation_twin_stay_in_step(owner_page):
    open_profile(owner_page)
    desktop = owner_page.locator("#notification-desktop-popups")
    expect(desktop).to_have_accessible_description(re.compile("never include email text or links"))
    desktop.check()
    expect(owner_page.locator(".automation-desktop-setting .form-status")).to_have_text("Show automation notices as desktop pop-ups: on.")
    expect(owner_page.locator("#automation-mode-desktop_notifications")).to_be_checked()
    owner_page.locator("#automation-mode-desktop_notifications").uncheck()
    expect(desktop).not_to_be_checked()


@pytest.mark.allow_page_errors  # the refusal is a 409 by design
def test_a_refused_switch_says_why_and_goes_back(owner_page):
    open_profile(owner_page)
    owner_page.route("**/api/v1/automation/settings", lambda route: route.fulfill(
        status=409, content_type="application/json", body='{"detail": "Needs 48 hours in shadow first (3 hours so far)"}',
    ))
    box = owner_page.locator("#automation-mode-bounce_recovery")
    box.check()
    status = owner_page.locator(".automation-group", has=box).locator(".form-status")
    expect(status).to_have_text("Needs 48 hours in shadow first (3 hours so far)")
    expect(box).not_to_be_checked()


def test_a_shadow_switch_offers_on_only_once_it_has_earned_it(owner_page, test_features):
    open_profile(owner_page)
    select = owner_page.locator("#automation-mode-ui_test_shadow")
    expect(select).to_have_value("off")
    expect(select.locator('option[value="shadow"]')).to_have_text("Shadow (log what it would do)")
    expect(select.locator('option[value="on"]')).to_be_disabled()
    reason = owner_page.locator("#automation-mode-ui_test_shadow-reason")
    expect(reason).to_contain_text("Run it in shadow first")
    select.select_option("shadow")
    status = owner_page.locator(".automation-group", has=select).locator(".form-status")
    expect(status).to_have_text("Careful stage mover: shadow, logging what it would do.")
    expect(reason).to_contain_text("Needs 48 hours in shadow first")
    expect(select.locator('option[value="on"]')).to_be_disabled()

    owner_page.reload()
    wait_for_results(owner_page)
    expect(owner_page.locator("#automation-mode-ui_test_shadow")).to_have_value("shadow")


def test_pausing_shows_the_banner_and_resuming_from_it_hides_it(owner_page, live_server):
    seed_in_flight(live_server)
    open_profile(owner_page)
    owner_page.click("#automation-pause")
    status = owner_page.locator("#automation-pause-status")
    expect(status).to_contain_text("Paused. Nothing is sent or changed on its own until you resume.")
    expect(status).to_contain_text(re.compile(r"1 email to Bovi Robotics was already on its way \(sent to Gmail at .+\) and can't be stopped\."))
    expect(owner_page.locator("#automation-pause")).to_have_text("Resume automation")
    banner = owner_page.locator("#automation-banner")
    expect(banner).to_be_visible()
    expect(banner).to_contain_text("Automation is paused. Nothing is sent or changed on its own.")
    expect(owner_page.locator(".automation-health")).to_contain_text("Email to Bovi Robotics: sent to Gmail at")

    # The banner is app-wide.
    owner_page.click("#discover-nav")
    wait_for_results(owner_page)
    expect(banner).to_be_visible()
    banner.get_by_role("button", name="Resume").click()
    expect(banner).to_be_hidden()
    expect(owner_page.locator("#action-status")).to_have_text("Automation resumed.")
    expect(owner_page.locator("#page-title")).to_be_focused()
    open_profile(owner_page)
    expect(owner_page.locator("#automation-pause")).to_have_text("Pause all automation")


def test_resuming_from_the_banner_updates_the_pause_button_on_the_profile(owner_page):
    open_profile(owner_page)
    owner_page.click("#automation-pause")
    expect(owner_page.locator("#automation-banner")).to_be_visible()
    owner_page.locator("#automation-banner").get_by_role("button", name="Resume").click()
    expect(owner_page.locator("#automation-banner")).to_be_hidden()
    expect(owner_page.locator("#automation-pause")).to_have_text("Pause all automation")
    expect(owner_page.locator("#automation-pause-status")).to_have_text("Running. Each switch below decides what the app does on its own.")


def test_gmail_needing_reconnecting_points_to_outreach(owner_page, live_server):
    now = utc_now()
    with closing(connect_product(live_server.live_path)) as conn:
        with conn:
            conn.execute(
                "INSERT INTO connector_accounts(id, user_id, provider, status, created_at, updated_at) "
                "VALUES('connector-gmail-ui', ?, 'gmail_drafts', 'error', ?, ?)",
                (USER, now, now),
            )
    owner_page.reload()
    wait_for_results(owner_page)
    banner = owner_page.locator("#automation-banner")
    expect(banner).to_contain_text("Gmail needs reconnecting. Reply and bounce checks have stopped.")
    expect(banner.locator(".automation-banner-item")).to_have_class(re.compile("is-problem"))
    banner.get_by_role("button", name="Open Outreach").click()
    expect(owner_page.locator("#outreach-nav")).to_have_attribute("aria-current", "page")


def test_undo_in_recent_activity_takes_the_change_back(owner_page, live_server, test_features):
    perform(live_server)
    assert stage(live_server) == "interview"
    open_profile(owner_page)
    recent = owner_page.locator(".automation-recent")
    row = recent.locator(".automation-action", has_text="Moved Orbit Systems to interview")
    expect(row.locator(".chip")).to_have_text("Applied")
    row.get_by_role("button", name=re.compile("^Undo")).click()
    expect(recent.locator(".form-status")).to_have_text("Undone: Moved Orbit Systems to interview.")
    expect(recent.locator(".automation-action .chip")).to_have_text("Undone")
    expect(recent.get_by_role("button", name=re.compile("^Undo"))).to_have_count(0)
    expect(owner_page.locator("#automation-recent-heading")).to_be_focused()
    assert stage(live_server) == "applied"


def test_two_undos_turn_the_switch_off_and_say_so(owner_page, live_server, test_features):
    perform(live_server, after={"stage": "interview"})
    perform(live_server, after={"stage": "offer"}, summary="Moved Orbit Systems to offer")
    open_profile(owner_page)
    recent = owner_page.locator(".automation-recent")
    expect(owner_page.locator("#automation-mode-ui_test_switch")).to_be_checked()
    recent.get_by_role("button", name="Undo: Moved Orbit Systems to offer").click()
    expect(recent.locator(".form-status")).to_have_text("Undone: Moved Orbit Systems to offer.")
    recent.get_by_role("button", name="Undo: Moved Orbit Systems to interview").click()
    expect(recent.locator(".form-status")).to_have_text("Turned Stage mover off: you undid 2 of its last 5 actions.")
    expect(owner_page.locator("#automation-mode-ui_test_switch")).not_to_be_checked()
    expect(owner_page.locator(".automation-notices")).to_contain_text("Turned off Stage mover")
    expect(owner_page.locator("#profile-badge")).to_have_text("1")
    expect(owner_page.locator("#profile-nav")).to_have_accessible_name("Profile, 1 unread notice")
    owner_page.click("#automation-notices-read")
    expect(owner_page.locator(".automation-section")).to_contain_text("No unread notices.")
    expect(owner_page.locator("#profile-badge")).to_be_hidden()


def test_waiting_and_shadow_actions_are_decided_from_the_profile(owner_page, live_server, test_features):
    perform(live_server, auto=False)
    perform(live_server, feature=SHADOWED.key, mode="shadow", after={"stage": "offer"}, summary="Would move Orbit Systems to offer",
            evidence={"excerpt": "We would like to <b>extend an offer</b>", "subject": "Offer"})
    section = open_profile(owner_page)
    expect(owner_page.locator("#profile-badge")).to_have_text("2")
    expect(owner_page.locator("#profile-nav")).to_have_accessible_name("Profile, 2 waiting for you")

    waiting = section.locator(".automation-waiting")
    expect(waiting).to_contain_text("Moved Orbit Systems to interview")
    expect(waiting).to_contain_text("Evidence: Interview invitation from Orbit Systems")
    expect(waiting).to_contain_text("Basis: rule:ui-test")
    expect(waiting).to_contain_text("90% confidence")

    shadow = section.locator(".automation-shadow")
    # Evidence is text, never markup.
    expect(shadow.locator(".automation-evidence")).to_have_text("Evidence: We would like to <b>extend an offer</b>")
    expect(shadow.locator(".automation-evidence b")).to_have_count(0)
    shadow.get_by_role("button", name="Right call").click()
    expect(shadow.locator(".form-status")).to_have_text("Marked as the right call.")
    expect(shadow.locator(".automation-verdict")).to_have_text("You marked this the right call.")
    expect(shadow.locator(".automation-verdict")).to_be_focused()
    assert stage(live_server) == "applied", "a shadow action changes nothing"

    waiting.get_by_role("button", name="Approve").click()
    expect(waiting.locator(".form-status")).to_have_text("Approved: Moved Orbit Systems to interview.")
    expect(waiting).to_contain_text("Nothing is waiting for you.")
    expect(section.locator(".automation-recent .chip")).to_have_text("Applied")
    assert stage(live_server) == "interview"
    expect(owner_page.locator("#profile-badge")).to_be_hidden()


@pytest.mark.allow_page_errors  # the superseded approval is a 409 by design
def test_an_approval_after_the_stage_changed_says_so_and_refreshes(owner_page, live_server, test_features):
    perform(live_server, auto=False)
    section = open_profile(owner_page)
    with closing(connect_product(live_server.live_path)) as conn:
        update_application(conn, "app-job-b", stage="offer", user_id=USER)
    waiting = section.locator(".automation-waiting")
    waiting.get_by_role("button", name="Approve").click()
    expect(waiting.locator(".form-status")).to_have_text("The stage changed after this was proposed, so it was not applied")
    expect(waiting).to_contain_text("Nothing is waiting for you.")
    expect(section.locator(".automation-recent .chip")).to_have_text("Left as it was")
    assert stage(live_server) == "offer"


def test_the_timeline_says_who_made_each_change(owner_page, live_server, test_features):
    with closing(connect_product(live_server.live_path)) as conn:
        update_application(conn, "app-job-b", notes="Talked to the recruiter", user_id=USER)
    perform(live_server)
    owner_page.click("#applications-nav")
    wait_for_results(owner_page)
    card = owner_page.locator('.application-card[data-application-id="app-job-b"]')
    card.locator("summary", has_text="Tasks, contacts, and timeline").click()
    timeline = card.locator(".timeline-list")
    automatic = timeline.locator("li", has=owner_page.locator(".timeline-author", has_text=re.compile("^Automatic$")))
    expect(automatic).to_have_count(1)
    expect(automatic).to_contain_text("stage changed")
    expect(automatic).to_contain_text("applied → interview")
    expect(timeline.locator(".timeline-author", has_text=re.compile("^You$")).first).to_be_visible()

    automatic.get_by_role("button", name=re.compile("^Undo this automatic change")).click()
    card = owner_page.locator('.application-card[data-application-id="app-job-b"]')
    expect(card.locator(".tracker-fields .form-status")).to_have_text("Undid the automatic change.")
    expect(card.locator(".application-controls select")).to_have_value("applied")
    card.locator("summary", has_text="Tasks, contacts, and timeline").click()
    undone = card.locator(".timeline-list li", has=owner_page.locator(".timeline-author", has_text=re.compile("^Undone by you$")))
    expect(undone).to_contain_text("interview → applied")
    expect(card.get_by_role("button", name=re.compile("^Undo this automatic change"))).to_have_count(0)
    assert stage(live_server) == "applied"


def test_a_busy_automation_section_is_accessible_in_both_themes(owner_page, live_server, test_features):
    from test_accessibility import _assert_accessible

    perform(live_server, auto=False)
    perform(live_server, feature=SHADOWED.key, mode="shadow", after={"stage": "offer"}, summary="Would move Orbit Systems to offer")
    perform(live_server, after={"stage": "rejected"}, summary="Moved Orbit Systems to rejected")
    seed_in_flight(live_server)
    with closing(connect_product(live_server.live_path)) as conn:
        automation.notice(conn, USER, event_key="ui:warning", level="warning", title="Gmail will likely ask you to reconnect soon")
        automation.notice(conn, USER, event_key="ui:problem", level="problem", title="Reply checks stopped", body="Reconnect Gmail under Outreach.")
        automation.record_health(conn, USER, "inbox.replies", ok=False, error="Gmail refused the saved sign-in")
        automation.set_paused(conn, USER, True)
    owner_page.reload()
    wait_for_results(owner_page)
    open_profile(owner_page)
    expect(owner_page.locator("#automation-banner")).to_be_visible()
    expect(owner_page.locator(".automation-health")).to_contain_text("Gmail refused the saved sign-in")
    _assert_accessible(owner_page, "the busy automation section")
    owner_page.evaluate("document.documentElement.dataset.theme = 'dark'")
    _assert_accessible(owner_page, "the busy automation section in dark mode")
