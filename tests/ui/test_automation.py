"""Automation in the browser: the Profile section, the app-wide banner, the badge, and timeline attribution.

The server runs in this process, so a test can register a feature of its own in
automation.FEATURES and seed the ledger straight into the live database, the
same way the automatic steps would.
"""

from __future__ import annotations

import re
from contextlib import closing
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from playwright.sync_api import expect

from conftest import OWNER_TOKEN, wait_for_results
from ui_helpers import assert_accessible, card_for, gmail_listing, open_outreach, seed_target
from opportunity_app import automation
from opportunity_app.actions import add_application_task, update_application
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
    assert section.locator(".automation-group > h4").all_inner_texts() == ["Outreach", "Applications", "Discovery", "Notifications"], \
        "every group with a feature, in order"
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
    # What Pause does, under the button, in the words of the pause itself.
    expect(owner_page.locator("#automation-pause-help")).to_have_text(
        "Stops everything the app does on its own. Replies and bounces are still recorded, and notices still appear.")
    expect(owner_page.locator("#automation-pause")).to_have_accessible_description(re.compile("^Stops everything the app does on its own"))
    owner_page.click("#automation-pause")
    status = owner_page.locator("#automation-pause-status")
    expect(status).to_contain_text(
        "Paused. Nothing is sent and no switch acts on its own until you resume. Replies and bounces are still recorded.")
    expect(status).to_contain_text(re.compile(r"1 email to Bovi Robotics was already on its way \(handed to Gmail at .+\) and can't be stopped\."))
    expect(owner_page.locator("#automation-pause")).to_have_text("Resume automation")
    banner = owner_page.locator("#automation-banner")
    expect(banner).to_be_visible()
    expect(banner.locator("p")).to_have_text(
        "Automation is paused. Nothing is sent and no switch acts on its own. Replies and bounces are still recorded. "
        "1 email was already handed to Gmail and can't be stopped.")
    expect(owner_page.locator(".automation-health")).to_contain_text("An email to Bovi Robotics: handed to Gmail at")

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
    # The server's own notice, with the counts it used: 2 undone of the 2 actions it has.
    expect(recent.locator(".form-status")).to_have_text(
        "Turned off Stage mover: you undid or rejected 2 of its last 2 actions. Turn it back on under Automation when you want it again.")
    expect(owner_page.locator("#automation-mode-ui_test_switch")).not_to_be_checked()
    expect(owner_page.locator(".automation-notices")).to_contain_text("Turned off Stage mover")
    expect(owner_page.locator("#profile-badge")).to_have_text("1")
    expect(owner_page.locator("#profile-nav")).to_have_accessible_name("Profile, 1 unread notice")
    owner_page.click("#automation-notices-read")
    expect(owner_page.locator(".automation-section")).to_contain_text("No unread notices.")
    expect(owner_page.locator("#profile-badge")).to_be_hidden()
    # Health still tells a switch the breaker turned off from one never turned on.
    expect(owner_page.locator(".automation-health")).to_contain_text(
        "Stage mover was turned off by the app")


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
    # The undo records decided_by 'student', but the change it took back was still the app's own.
    expect(card.locator(".timeline-list li", has_text="applied → interview").locator(".timeline-author")).to_have_text("Automatic")
    assert stage(live_server) == "applied"


def test_a_busy_automation_section_is_accessible_in_both_themes(owner_page, live_server, test_features):
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
    assert_accessible(owner_page, "the busy automation section")
    owner_page.evaluate("document.documentElement.dataset.theme = 'dark'")
    assert_accessible(owner_page, "the busy automation section in dark mode")


# --- Phase 0 review fixes ------------------------------------------------------------------


def seed_actions(live_server, *, status, count, feature=SWITCH.key, start, reviewed=(), summary="Action"):
    """Ledger rows straight into the database, one second apart from ``start``, oldest first.

    ``reviewed`` holds the indexes marked 'right'. Nothing is applied: these
    only fill the lists, the way a busy shadow period would.
    """
    with closing(connect_product(live_server.live_path)) as conn:
        with conn:
            for index in range(count):
                conn.execute(
                    "INSERT INTO automation_actions(id, user_id, feature, action_type, subject_kind, subject_id, status, summary, "
                    "idempotency_key, created_at, review) VALUES(?, ?, ?, 'application.stage', 'application', 'app-job-b', ?, ?, ?, ?, ?)",
                    (f"ui-{uuid4().hex}", USER, feature, status, f"{summary} {index}", f"ui:{uuid4().hex}",
                     (start + timedelta(seconds=index)).isoformat(timespec="microseconds"), "right" if index in reviewed else ""),
                )


def wait_until(page, condition, what):
    """Let Playwright run route handlers until ``condition()`` holds (sync handlers run inside Playwright calls)."""
    for _ in range(50):
        if condition():
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"Timed out waiting for {what}")


def test_each_list_is_fetched_on_its_own_so_nothing_waiting_is_crowded_out(owner_page, live_server, test_features):
    """F4, F13: one proposal older than 205 shadow rows is still in Waiting, and every list says how many it holds."""
    start = datetime.now(timezone.utc) - timedelta(hours=2)
    seed_actions(live_server, status="proposed", count=1, start=start, summary="Old proposal")
    # The newest ten are reviewed; unreviewed ones come first in the list all the same.
    seed_actions(live_server, status="shadow", count=205, feature=SHADOWED.key, start=start + timedelta(minutes=1),
                 reviewed=range(195, 205), summary="Shadow move")
    queries = []
    owner_page.on("request", lambda request: queries.append(request.url.split("?", 1)[1])
                  if "/api/v1/automation/actions?" in request.url else None)
    section = open_profile(owner_page)

    waiting = section.locator(".automation-waiting")
    expect(waiting).to_contain_text("Old proposal 0")
    expect(waiting.get_by_role("button", name="Approve")).to_be_enabled()
    expect(section.locator("#automation-waiting-heading")).to_have_text("Waiting for you (1)")
    expect(waiting.locator(".automation-more")).to_have_count(0)

    shadow = section.locator(".automation-shadow")
    expect(section.locator("#automation-shadow-heading")).to_have_text("Would have done (205)")
    rows = shadow.locator(".automation-action")
    expect(rows).to_have_count(200)
    expect(shadow.locator(".automation-more")).to_have_text("Showing the newest 200 of 205.")
    # Unreviewed first (newest of those first), then the ten already reviewed.
    expect(rows.first).to_contain_text("Shadow move 194")
    expect(rows.first.get_by_role("button", name="Right call")).to_be_visible()
    expect(rows.nth(189).get_by_role("button", name="Right call")).to_be_visible()
    expect(rows.nth(190).locator(".automation-verdict")).to_have_text("You marked this the right call.")
    expect(rows.last.locator(".automation-verdict")).to_be_visible()
    expect(section.locator(".automation-recent")).to_contain_text("Nothing has happened automatically yet.")

    assert sorted(queries) == sorted([
        "status=proposed&limit=200",
        "status=shadow&limit=200",
        "status=applied,undone,superseded,rejected,failed,expired&limit=50",
    ]), queries


def test_mark_all_read_marks_every_unread_notice_not_only_those_shown(owner_page, live_server):
    """F32: the overview sends at most 20 notices; Mark all read clears all 25."""
    with closing(connect_product(live_server.live_path)) as conn:
        for index in range(25):
            automation.notice(conn, USER, event_key=f"ui:bulk:{index}", level="info", title=f"Notice {index}")
    owner_page.reload()
    wait_for_results(owner_page)
    bodies = []
    owner_page.on("request", lambda request: bodies.append(request.post_data_json)
                  if request.url.endswith("/api/v1/automation/notices/read") else None)
    section = open_profile(owner_page)
    expect(section.locator(".automation-notice")).to_have_count(20)
    expect(owner_page.locator("#profile-badge")).to_have_text("25")
    owner_page.click("#automation-notices-read")
    expect(section.locator("#automation-notices-read ~ .form-status")).to_have_text("Marked 25 notices read.")
    expect(section).to_contain_text("No unread notices.")
    expect(owner_page.locator("#profile-badge")).to_be_hidden()
    assert bodies == [{"all": True}]


def test_in_flight_work_is_named_by_what_it_is(owner_page, live_server):
    """F29, F39: an email handed to Gmail and a contact form being pressed, each in its own words."""
    seed_in_flight(live_server)
    now = utc_now()
    with closing(connect_product(live_server.live_path)) as conn:
        with conn:
            conn.execute(
                "INSERT INTO outreach_targets(id, user_id, company, created_at, updated_at) VALUES('t-form', ?, 'Orbit Forms', ?, ?)",
                (USER, now, now),
            )
            conn.execute(
                "INSERT INTO outreach_send_claims(target_id, user_id, kind, token, state, action, instance, claimed_at) "
                "VALUES('t-form', ?, 'initial', 'tok', ?, 'form', 'ui', ?)",
                (USER, automation.FORM_HANDED_OVER, now),
            )
    section = open_profile(owner_page)
    health = section.locator(".automation-health")
    expect(health).to_contain_text("An email to Bovi Robotics: handed to Gmail at")
    expect(health).to_contain_text("A contact form to Orbit Forms: submission started at")
    owner_page.click("#automation-pause")
    expect(owner_page.locator("#automation-pause-status")).to_contain_text(re.compile(
        r"1 email and 1 contact form were already on their way and can't be stopped: "
        r"an email to Bovi Robotics \(handed to Gmail at .+\); a contact form to Orbit Forms \(submission started at .+\)\."))


def test_health_lists_unconfirmed_sends_and_the_gmail_error(owner_page, live_server):
    """F16, F43: a send nobody could confirm says where to look; Gmail's last problem is shown."""
    now = utc_now()
    earlier = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(timespec="microseconds")
    with closing(connect_product(live_server.live_path)) as conn:
        with conn:
            for target, company in (("t-mail", "Acme Mail"), ("t-page", "Orbit Page"), ("t-draft", "Kestrel Draft")):
                conn.execute(
                    "INSERT INTO outreach_targets(id, user_id, company, created_at, updated_at) VALUES(?, ?, ?, ?, ?)",
                    (target, USER, company, now, now),
                )
            conn.execute(
                "INSERT INTO outreach_send_claims(target_id, user_id, kind, token, state, action, instance, claimed_at) "
                "VALUES('t-mail', ?, 'initial', 'a', 'unconfirmed', 'send', 'ui', ?)", (USER, now),
            )
            conn.execute(
                "INSERT INTO outreach_send_claims(target_id, user_id, kind, token, state, action, instance, claimed_at) "
                "VALUES('t-page', ?, 'initial', 'b', 'unconfirmed', 'form', 'ui', ?)", (USER, now),
            )
            # A Gmail draft made but not recorded: unconfirmed too, and it sent nothing.
            conn.execute(
                "INSERT INTO outreach_send_claims(target_id, user_id, kind, token, state, action, instance, claimed_at) "
                "VALUES('t-draft', ?, 'initial', 'c', 'unconfirmed', 'draft', 'ui', ?)", (USER, now),
            )
            conn.execute(
                "INSERT INTO connector_accounts(id, user_id, provider, status, created_at, updated_at, last_ok_at, last_error) "
                "VALUES('connector-gmail-ui', ?, 'gmail_drafts', 'connected', ?, ?, ?, 'Gmail could not be reached')",
                (USER, now, now, earlier),
            )
    health = open_profile(owner_page).locator(".automation-health")
    expect(health).to_contain_text("The email to Acme Mail may or may not have gone out")
    expect(health).to_contain_text("Check your Gmail Sent folder.")
    expect(health).to_contain_text("The contact form message to Orbit Page may or may not have gone out")
    expect(health).to_contain_text("Check the company's page.")
    draft = health.locator("li", has_text="Kestrel Draft")
    expect(draft).to_contain_text("A Gmail draft for Kestrel Draft may have been saved without the app recording it")
    expect(draft).not_to_contain_text("gone out")
    expect(health.locator("li", has=owner_page.locator("strong", has_text=re.compile("^Gmail$")))).to_contain_text(
        "Last problem: Gmail could not be reached.")


def test_outreach_switches_show_their_longer_help_in_the_automation_section(owner_page):
    """F36: turning scheduled sending off does not cancel what is already scheduled, and the panel says so."""
    section = open_profile(owner_page)
    field = section.locator('[data-automation-feature="scheduled_sending"]')
    expect(field).to_contain_text("Turning this off does not cancel emails already scheduled")
    expect(section.locator("#automation-mode-scheduled_sending")).to_have_accessible_description(
        re.compile("Turning this off does not cancel emails already scheduled"))


def open_timeline(page):
    page.click("#applications-nav")
    wait_for_results(page)
    card = page.locator('.application-card[data-application-id="app-job-b"]')
    card.locator("summary", has_text="Tasks, contacts, and timeline").click()
    return card.locator(".timeline-list")


def test_an_approved_change_and_an_agent_task_are_labelled_in_the_timeline(owner_page, live_server, base_url, test_features):
    """F28, F34: 'Automatic, approved by you' for an approval, and 'Agent (you approved)' for the agent's task."""
    proposal = perform(live_server, auto=False)
    approved = owner_page.request.post(f"{base_url}/api/v1/automation/actions/{proposal['id']}/approve", headers=BEARER)
    assert approved.ok, approved.text()
    with closing(connect_product(live_server.live_path)) as conn:
        add_application_task(conn, "app-job-b", title="Email the recruiter Friday", user_id=USER,
                             origin="agent", origin_ref="proposal-ui", source="agent_proposal:proposal-ui")
    lookups = []
    owner_page.on("request", lambda request: lookups.append(request.url) if "/api/v1/automation/actions" in request.url else None)
    timeline = open_timeline(owner_page)
    expect(timeline.locator("li", has_text="applied → interview").locator(".timeline-author")).to_have_text("Automatic, approved by you")
    expect(timeline.locator("li", has_text="task added").locator(".timeline-author")).to_have_text("Agent (you approved)")
    expect(timeline.get_by_role("button", name=re.compile("^Undo this automatic change"))).to_be_visible()
    assert len(lookups) == 1, lookups


def test_several_automatic_changes_are_looked_up_in_one_request(owner_page, live_server, base_url, test_features):
    """F34: a change the app made and one the student approved, labelled apart, for one list request."""
    perform(live_server, after={"stage": "interview"}, summary="Moved Orbit Systems to interview")
    proposal = perform(live_server, auto=False, after={"stage": "offer"}, summary="Move Orbit Systems to offer")
    approved = owner_page.request.post(f"{base_url}/api/v1/automation/actions/{proposal['id']}/approve", headers=BEARER)
    assert approved.ok, approved.text()
    lookups = []
    owner_page.on("request", lambda request: lookups.append(request.url) if "/api/v1/automation/actions" in request.url else None)
    timeline = open_timeline(owner_page)
    expect(timeline.locator("li", has_text="interview → offer").locator(".timeline-author")).to_have_text("Automatic, approved by you")
    expect(timeline.locator("li", has_text="applied → interview").locator(".timeline-author")).to_have_text("Automatic")
    assert len(lookups) == 1 and "status=applied,undone,superseded" in lookups[0], lookups


@pytest.mark.allow_page_errors  # B is refused as superseded once A has moved the stage (a 409, by design)
def test_a_decision_still_pending_keeps_its_buttons_disabled_through_another_rows_repaint(owner_page, live_server, test_features):
    """F31: approving A repaints every list while B's approval is still on its way; B's buttons stay disabled."""
    perform(live_server, auto=False, after={"stage": "interview"}, summary="Proposal A")
    second = perform(live_server, auto=False, after={"stage": "offer"}, summary="Proposal B")
    held = []
    owner_page.route(f"**/api/v1/automation/actions/{second['id']}/approve", lambda route: held.append(route))
    section = open_profile(owner_page)
    waiting = section.locator(".automation-waiting")
    waiting.locator(".automation-action", has_text="Proposal B").get_by_role("button", name="Approve").click()
    wait_until(owner_page, lambda: held, "B's approval to be sent")

    waiting.locator(".automation-action", has_text="Proposal A").get_by_role("button", name="Approve").click()
    expect(waiting.locator(".form-status")).to_have_text("Approved: Proposal A.")
    expect(section.locator(".automation-recent")).to_contain_text("Proposal A")
    # The repaint drew B again; its decision is still on its way, so nothing is offered twice.
    row_b = waiting.locator(".automation-action", has_text="Proposal B")
    expect(row_b.get_by_role("button", name="Approve")).to_be_disabled()
    expect(row_b.get_by_role("button", name="Reject")).to_be_disabled()

    held[0].continue_()
    # A moved the stage first, so B is refused as superseded, and says so.
    expect(waiting.locator(".form-status")).to_have_text("The stage changed after this was proposed, so it was not applied")
    expect(waiting).to_contain_text("Nothing is waiting for you.")


@pytest.mark.allow_page_errors  # the lost connection is the point
def test_a_failed_decision_and_a_failed_reload_leave_the_buttons_usable(owner_page, live_server, test_features):
    """F31: a decision that never reached the server can be tried again, even when the lists cannot be refreshed."""
    perform(live_server, auto=False)
    section = open_profile(owner_page)
    owner_page.route("**/api/v1/automation/actions/*/approve", lambda route: route.abort())
    owner_page.route(re.compile(r".*/api/v1/automation(/actions\?.*)?$"), lambda route: route.abort())
    waiting = section.locator(".automation-waiting")
    waiting.get_by_role("button", name="Approve").click()
    expect(waiting.locator(".form-status")).to_have_text(
        "Connection lost. The lists could not be refreshed, so they may be out of date.")
    expect(owner_page.locator("#error-banner")).to_contain_text("Connection lost.")
    expect(waiting.get_by_role("button", name="Approve")).to_be_enabled()
    expect(waiting.get_by_role("button", name="Reject")).to_be_enabled()
    assert stage(live_server) == "applied"


@pytest.mark.allow_page_errors  # the lists' reload is made to fail on purpose
def test_a_decision_that_went_through_keeps_its_buttons_disabled_when_the_reload_fails(owner_page, live_server, test_features):
    """F31: the approval is made, the reload is lost; the row is not offered a second approval."""
    perform(live_server, auto=False)
    section = open_profile(owner_page)
    owner_page.route(re.compile(r".*/api/v1/automation(/actions\?.*)?$"), lambda route: route.abort())
    waiting = section.locator(".automation-waiting")
    waiting.get_by_role("button", name="Approve").click()
    expect(waiting.locator(".form-status")).to_have_text(
        "Approved: Moved Orbit Systems to interview. The lists could not be refreshed, so they may be out of date.")
    expect(waiting.get_by_role("button", name="Approve")).to_be_disabled()
    expect(waiting.get_by_role("button", name="Reject")).to_be_disabled()
    assert stage(live_server) == "interview"


def test_an_older_automation_read_never_undoes_a_newer_resume(owner_page, live_server):
    """F24, F30: Profile's read of the paused state lands after Resume's own answer, and is dropped."""
    with closing(connect_product(live_server.live_path)) as conn:
        automation.set_paused(conn, USER, True)
    owner_page.reload()
    wait_for_results(owner_page)
    banner = owner_page.locator("#automation-banner")
    expect(banner).to_contain_text("Automation is paused.")

    held = []
    # Read now (paused), delivered later: exactly the answer that used to win.
    owner_page.route(re.compile(r".*/api/v1/automation$"), lambda route: held.append((route, route.fetch())))
    owner_page.click("#profile-nav")
    wait_until(owner_page, lambda: held, "Profile to ask for the automation state")
    assert held[0][1].json()["settings"]["paused"] is True
    banner.get_by_role("button", name="Resume").click()
    expect(banner).to_be_hidden()

    route, response = held[0]
    route.fulfill(response=response)
    wait_for_results(owner_page)
    expect(owner_page.locator("#automation-pause")).to_have_text("Pause all automation")
    expect(owner_page.locator("#automation-pause-status")).to_have_text("Running. Each switch below decides what the app does on its own.")
    expect(banner).to_be_hidden()
    owner_page.unroute_all(behavior="ignoreErrors")


def hold_first_settings_answer(page):
    """Let the first settings PUT reach the server, and hold its answer until the test releases it."""
    held = []

    def handler(route):
        if held:
            route.continue_()
        else:
            held.append((route, route.fetch()))

    page.route("**/api/v1/automation/settings", handler)
    return held


def test_an_older_switch_answer_never_undoes_a_newer_pause(owner_page, live_server, test_features):
    """F24, F30: a switch's answer, read before Pause committed, lands after Pause's own answer, and is dropped."""
    section = open_profile(owner_page)
    held = hold_first_settings_answer(owner_page)
    drafts = owner_page.locator("#automation-mode-auto_drafts")
    drafts.check()
    wait_until(owner_page, lambda: held, "the switch save to reach the server")
    assert held[0][1].json()["health"]["paused"] is False, "the held answer is the pre-pause state"
    owner_page.click("#automation-pause")
    banner = owner_page.locator("#automation-banner")
    expect(banner).to_contain_text("Automation is paused.")

    # The fresh read that follows two writes at once is held too, so what is
    # on screen until it lands is down to the older answer being dropped.
    reads = []
    owner_page.route(re.compile(r".*/api/v1/automation$"), lambda route: reads.append(route))
    route, response = held[0]
    route.fulfill(response=response)
    # The switch's own words still come from its own answer.
    drafts_status = section.locator(".automation-group", has=drafts).locator(".form-status")
    expect(drafts_status).to_have_text("Write drafts automatically: on.")
    wait_until(owner_page, lambda: reads, "the read after both writes")
    with closing(connect_product(live_server.live_path)) as conn:
        assert automation.paused(conn, USER) is True
    expect(banner).to_contain_text("Automation is paused.")
    expect(owner_page.locator("#automation-pause")).to_have_text("Resume automation")
    expect(drafts).to_be_checked()

    reads[0].continue_()
    owner_page.wait_for_timeout(300)
    expect(banner).to_contain_text("Automation is paused.")
    expect(owner_page.locator("#automation-pause")).to_have_text("Resume automation")
    expect(drafts).to_be_checked()
    owner_page.unroute_all(behavior="ignoreErrors")


def test_two_switch_answers_in_any_order_end_on_what_the_server_saved(owner_page, live_server, test_features):
    """F24, F30: two switch saves at once; whichever answer lands last, both switches end as the server has them."""
    section = open_profile(owner_page)
    drafts = owner_page.locator("#automation-mode-auto_drafts")
    mover = owner_page.locator(f"#automation-mode-{SWITCH.key}")

    # 1. The first save commits first, but its answer lands last.
    held = hold_first_settings_answer(owner_page)
    drafts.check()
    wait_until(owner_page, lambda: held, "the first save to reach the server")
    mover.check()
    expect(section.locator(".automation-group", has=mover).locator(".form-status")).to_have_text("Stage mover: on.")
    route, response = held[0]
    route.fulfill(response=response)
    expect(section.locator(".automation-group", has=drafts).locator(".form-status")).to_have_text("Write drafts automatically: on.")
    owner_page.wait_for_timeout(300)
    expect(drafts).to_be_checked()
    expect(mover).to_be_checked()
    owner_page.unroute_all(behavior="ignoreErrors")

    # 2. The first save is held before the server sees it, so the second
    # commits first and its answer, which the page shows, still has the
    # first switch on. The first's answer then lands; it started earlier, so
    # it is dropped. Only the read after both is right about both.
    reads = []
    owner_page.route(re.compile(r".*/api/v1/automation$"), lambda route: (reads.append(route.request.url), route.continue_()))
    unsent = []
    owner_page.route("**/api/v1/automation/settings", lambda route: route.continue_() if unsent else unsent.append(route))
    drafts.uncheck()
    wait_until(owner_page, lambda: unsent, "the first save to be held")
    mover.uncheck()
    expect(section.locator(".automation-group", has=mover).locator(".form-status")).to_have_text("Stage mover: off.")
    unsent[0].continue_()
    expect(section.locator(".automation-group", has=drafts).locator(".form-status")).to_have_text("Write drafts automatically: off.")
    wait_until(owner_page, lambda: reads, "the read after both saves")
    with closing(connect_product(live_server.live_path)) as conn:
        assert (automation.mode(conn, USER, "auto_drafts"), automation.mode(conn, USER, SWITCH.key)) == ("off", "off")
    expect(drafts).not_to_be_checked()
    expect(mover).not_to_be_checked()
    owner_page.unroute_all(behavior="ignoreErrors")


def test_a_proposal_approved_in_waiting_can_be_undone_in_recent_at_once(owner_page, live_server, test_features):
    """F31: deciding in Waiting closes the Waiting row only; the Applied row's Undo is a new decision."""
    perform(live_server, auto=False)
    section = open_profile(owner_page)
    waiting = section.locator(".automation-waiting")
    waiting.get_by_role("button", name="Approve").click()
    expect(waiting.locator(".form-status")).to_have_text("Approved: Moved Orbit Systems to interview.")
    recent = section.locator(".automation-recent")
    expect(recent.locator(".automation-action .chip")).to_have_text("Applied")
    undo = recent.get_by_role("button", name="Undo: Moved Orbit Systems to interview")
    expect(undo).to_be_enabled()
    assert stage(live_server) == "interview"
    undo.click()
    expect(recent.locator(".form-status")).to_have_text("Undone: Moved Orbit Systems to interview.")
    expect(recent.locator(".automation-action .chip")).to_have_text("Undone")
    assert stage(live_server) == "applied"


# The Outreach tab reads the pause and Gmail's expiry estimate too. The live
# server has no Google client, so these patch the listing's gmail_drafts the
# way tests/ui/test_outreach_journey.py does; the server side is covered by
# tests/test_outreach_gmail.py and tests/test_outreach_schedule.py.


def test_a_scheduled_send_reads_paused_while_automation_is_paused(owner_page, live_server, base_url):
    """F7, F14: the card says the email waits for Resume, and says its time again the moment Resume is clicked."""
    target = seed_target(
        owner_page, base_url, contact_email="jane@bovi.example", contact_name="Jane Doe",
        email_subject="Internship question", email_body="Hi Jane,\n\nWould you be open to a call?\n\nTest Student",
    )
    approved = owner_page.request.post(
        f"{base_url}/api/v1/outreach/{target['id']}/approve", headers=BEARER, data={"fingerprint": target["draft_fingerprint"]},
    )
    assert approved.ok, approved.text()
    label = "Mon, Sep 28, 9:12 AM CDT (their time, from Austin, TX)"
    now = utc_now()
    with closing(connect_product(live_server.live_path)) as conn:
        with conn:
            conn.execute(
                "INSERT INTO outreach_scheduled_sends(target_id, user_id, kind, fingerprint, send_at, timezone, label, state, created_at, updated_at) "
                "VALUES(?, ?, 'initial', ?, ?, 'America/Chicago', ?, 'scheduled', ?, ?)",
                (target["id"], USER, target["draft_fingerprint"], now, label, now, now),
            )
        automation.set_paused(conn, USER, True)
    gmail_listing(owner_page)
    owner_page.reload()
    wait_for_results(owner_page)
    banner = owner_page.locator("#automation-banner")
    expect(banner).to_contain_text("Automation is paused.")

    open_outreach(owner_page, "scheduled")
    card = card_for(owner_page, "Bovi")
    expect(card.locator(".chip", has_text="Goes out")).to_have_text(f"Paused. Goes out after you resume (was {label})")
    expect(card.locator(".outreach-next-text")).to_contain_text(
        f"Paused. Goes out after you resume (was {label}). Cancel it or send it now below.")

    banner.get_by_role("button", name="Resume").click()
    expect(banner).to_be_hidden()
    # Rewritten in place, without reloading the card.
    expect(card.locator(".chip", has_text="Goes out")).to_have_text(f"Goes out {label}")
    expect(card.locator(".outreach-next-text")).to_contain_text(f"Goes out {label}. Cancel it or send it now below.")
    owner_page.unroute_all(behavior="ignoreErrors")


def test_outreach_offers_reconnect_gmail_before_google_asks(owner_page):
    """F8: a working connection near its estimated expiry gets Reconnect Gmail, with the date as an estimate."""
    soon = (datetime.now(timezone.utc) + timedelta(hours=20)).isoformat(timespec="seconds")
    gmail_listing(owner_page, bounce_check=True, expiring_soon=True, likely_expires_at=soon)
    open_outreach(owner_page)
    panel = owner_page.locator(".outreach-gmail-connect")
    expect(panel.locator(".profile-help")).to_have_text(
        re.compile(r"^Google will likely ask again by \w+, \w+ \d{1,2}; reconnecting now avoids a gap\.$"))
    expect(panel.get_by_role("button", name="Reconnect Gmail")).to_be_visible()
    owner_page.unroute_all(behavior="ignoreErrors")


def test_a_healthy_gmail_connection_offers_no_reconnect(owner_page):
    """F8: not near expiry, nothing to reconnect."""
    gmail_listing(owner_page, bounce_check=True, expiring_soon=False, likely_expires_at=None)
    open_outreach(owner_page)
    expect(owner_page.locator(".outreach-gmail-connect")).to_have_count(0)
    owner_page.unroute_all(behavior="ignoreErrors")
