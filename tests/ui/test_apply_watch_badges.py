"""Apply for me's badges on the application card (10.5), the two answers for an attempt that may have been sent, Mark as
applied, and the statistics in the settings.

The server runs in this process, so each test seeds the claims the runner and the watch would have written straight into
the live database. Nothing reaches Greenhouse and no browser is opened by the app. Every company here is invented.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from axe_core_python.sync_playwright import Axe
from playwright.sync_api import expect

from conftest import wait_for_results
from opportunity_app.core.timestamps import utc_now
from ui_helpers import AXE_OPTIONS, USER, db


def iso(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def seed(live_server, key, company, *, state="submitted", verification="", stage="applying", policy="record", mode="one_click",
         after_click=1, detail=None, resolved_by="", note="", watch_until=True, title="Controls Intern"):
    """One opportunity, its application and one Apply for me claim on it, as a stopped server or the watch would leave it."""
    now = datetime.now(timezone.utc)
    handed = iso(now - timedelta(minutes=30)) if after_click else None
    stamp = utc_now()
    submitted = iso(now - timedelta(minutes=29)) if state == "submitted" else None
    until = iso(now + timedelta(hours=23)) if state == "submitted" and verification == "awaiting_email" and watch_until else None
    with db(live_server) as conn, conn:
        conn.execute(
            "INSERT INTO opportunities(id, company, title, url, first_seen_at, last_seen_at, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
            (f"op-{key}", company, title, f"https://boards.example.test/{key}", stamp, stamp, stamp, stamp))
        conn.execute(
            "INSERT INTO applications(id, opportunity_id, user_id, stage, applied_at, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (f"app-op-{key}", f"op-{key}", USER, stage, submitted if stage == "applied" else None, stamp, stamp))
        conn.execute(
            "INSERT INTO application_submit_claims(token, application_id, user_id, opportunity_id, instance, mode, state, after_click, ats, board_token, "
            "job_ref, company_key, stage_policy, plan_hash, handed_over_at, heartbeat_at, verification, watch_until, submitted_at, verified_at, "
            "stage_recorded, resolved_by, note, detail_json, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, 'another-process', ?, ?, ?, 'greenhouse', 'ui', ?, ?, ?, '', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"tok-{key}", f"app-op-{key}", USER, f"op-{key}", mode, state, after_click, f"ui/{key}", company.lower(), policy, handed, handed or stamp,
             verification, until, submitted, submitted, 1 if stage == "applied" else 0, resolved_by, note, json.dumps(detail or {}), stamp, stamp))


def card(page, key):
    return page.locator(f'.application-card[data-application-id="app-op-{key}"]')


def open_applications(page):
    page.click("#applications-nav")
    wait_for_results(page)


@pytest.fixture
def badges(live_server, owner_page):
    """One card for every state the badge can show."""
    soon = iso(datetime.now(timezone.utc) - timedelta(minutes=20))
    seed(live_server, "watching", "Bluefin Robotics", verification="awaiting_email", stage="applied", resolved_by="page")
    seed(live_server, "paused", "Cobalt Robotics", verification="awaiting_email", stage="applied", resolved_by="page",
         detail={"watch_paused_since": soon, "watch_paused": "Gmail needs reconnecting"})
    seed(live_server, "confirmed", "Delta Robotics", verification="email_confirmed", stage="applied", resolved_by="email",
         detail={"email_received_at": soon})
    seed(live_server, "silent", "Ember Robotics", verification="no_email_24h", stage="applied", resolved_by="page")
    seed(live_server, "unwatched", "Fjord Robotics", verification="not_watched", stage="applying", policy="ask", mode="handoff", resolved_by="page",
         detail={"possible_email_at": soon})
    seed(live_server, "unsure", "Gamma Robotics", state="unconfirmed", mode="handoff", policy="ask")
    seed(live_server, "maybe", "Iris Robotics", state="unconfirmed", mode="handoff", policy="ask", detail={"possible_email_at": soon})
    seed(live_server, "stopped", "Helio Robotics", state="needs_you", after_click=0, mode="handoff", policy="ask",
         note="Add your phone number first")
    owner_page.reload()
    wait_for_results(owner_page)
    open_applications(owner_page)
    return owner_page


def test_every_badge_says_what_the_card_knows_and_no_more(badges):
    page = badges
    texts = {key: card(page, key).locator(".apply-badge").inner_text() for key in
             ("watching", "paused", "confirmed", "silent", "unwatched", "unsure", "stopped")}
    assert "Applied with Apply for me" in texts["watching"]
    assert "Greenhouse showed its confirmation page" in texts["watching"]
    assert "Looking for its email until" in texts["watching"]
    assert "Looking for its email: paused, Gmail needs reconnecting" in texts["paused"]
    assert "Greenhouse's confirmation email arrived" in texts["confirmed"]
    assert "No confirmation email yet" in texts["silent"]
    assert "Some employers don't send one" in texts["silent"]
    assert "The app isn't checking for a confirmation email" in texts["unwatched"]
    assert "Submitted with Apply for me" in texts["unwatched"], "the stage is still applying, so it is not called applied"
    assert "An email from Fjord Robotics arrived on" in texts["unwatched"] and "it may be for this application" in texts["unwatched"]
    assert "May have been sent. Check your email or the Greenhouse portal" in texts["unsure"]
    assert "Applied" not in texts["unsure"], "an attempt that may have reached Greenhouse is never called applied"
    assert "it may be for this application" not in texts["unsure"], "no weak email, no hint"
    maybe = card(page, "maybe").locator(".apply-badge").inner_text()
    assert "May have been sent" in maybe
    assert "An email from Iris Robotics arrived on" in maybe and "it may be for this application" in maybe,         "a weak match is shown on a card that may have been sent too (6.16, 10.5)"
    assert "Add your phone number first" in texts["stopped"]
    expect(card(page, "unsure").get_by_role("button", name="It went through")).to_be_enabled()
    expect(card(page, "unsure").get_by_role("button", name="It didn't go through")).to_be_enabled()
    expect(card(page, "unwatched").get_by_role("button", name="Mark as applied?")).to_be_visible()
    expect(card(page, "watching").get_by_role("button", name="Mark as applied?")).to_have_count(0)
    # The seeded Orbit application was never touched by Apply for me: no badge.
    expect(page.locator('.application-card[data-application-id="app-job-b"] .apply-badge')).to_have_count(0)


def test_it_went_through_settles_the_attempt_and_the_card_follows(badges, live_server):
    page = badges
    unsure = card(page, "unsure")
    unsure.get_by_role("button", name="It went through").click()
    expect(unsure.locator(".apply-badge")).to_contain_text("Submitted with Apply for me")
    expect(unsure.locator(".apply-badge")).to_contain_text("You said it went through")
    expect(unsure.get_by_role("button", name="Mark as applied?")).to_be_visible()
    with db(live_server) as conn:
        row = conn.execute("SELECT state, resolved_by, verification, stage_recorded FROM application_submit_claims WHERE token='tok-unsure'").fetchone()
        stage = conn.execute("SELECT stage FROM applications WHERE id='app-op-unsure'").fetchone()[0]
    assert tuple(row)[:2] == ("submitted", "student")
    assert stage == "applying", "a Finish in browser claim never moves the stage by itself"


def test_it_did_not_go_through_releases_it_and_the_badge_goes(badges, live_server):
    page = badges
    unsure = card(page, "unsure")
    unsure.get_by_role("button", name="It didn't go through").click()
    expect(unsure.locator(".apply-badge")).to_have_count(0)
    with db(live_server) as conn:
        state = conn.execute("SELECT state FROM application_submit_claims WHERE token='tok-unsure'").fetchone()[0]
        events = [row[0] for row in conn.execute("SELECT event_type FROM application_events WHERE application_id='app-op-unsure'")]
    assert state == "released"
    assert "apply_agent_resolved" in events


def test_mark_as_applied_moves_the_stage_once(badges, live_server):
    page = badges
    unwatched = card(page, "unwatched")
    unwatched.get_by_role("button", name="Mark as applied?").click()
    expect(unwatched.get_by_role("button", name="Mark as applied?")).to_have_count(0)
    expect(unwatched.locator(".apply-badge")).to_contain_text("Applied with Apply for me")
    with db(live_server) as conn:
        stage = conn.execute("SELECT stage FROM applications WHERE id='app-op-unwatched'").fetchone()[0]
        recorded = conn.execute("SELECT stage_recorded FROM application_submit_claims WHERE token='tok-unwatched'").fetchone()[0]
    assert (stage, recorded) == ("applied", 1)


def test_the_timeline_names_apply_for_me_events_in_words(badges, live_server):
    with db(live_server) as conn, conn:
        conn.execute(
            "INSERT INTO application_events(application_id, event_type, detail_json, created_at) VALUES('app-op-confirmed', 'apply_agent_verification', ?, ?)",
            (json.dumps({"source": "apply_agent:confirmation_email", "verification": "email_confirmed"}), utc_now()))
    badges.reload()
    wait_for_results(badges)
    open_applications(badges)
    confirmed = card(badges, "confirmed")
    confirmed.locator("summary", has_text="Tasks, contacts, and timeline").click()
    timeline = confirmed.locator(".timeline-list")
    expect(timeline).to_contain_text("Confirmation email watch")
    expect(timeline).to_contain_text("The confirmation email")


def test_the_settings_show_the_statistics_in_plain_sentences(badges):
    page = badges
    page.click("#profile-nav")
    wait_for_results(page)
    block = page.locator(".automation-apply-agent")
    expect(block.get_by_role("heading", name="How Apply for me has gone")).to_be_visible()
    stats = block.locator(".apply-stats")
    expect(stats).to_contain_text("Greenhouse: 7 applications handed over, 5 submitted.")
    expect(stats).to_contain_text("Confirmation emails: 1 arrived, 1 didn't come within 24 hours, 2 still being looked for.")
    expect(stats).to_contain_text("Security codes: Greenhouse asked for a code 0 times; the app typed it 0 times.")


@pytest.mark.parametrize("width", (1280, 390))
def test_the_badges_are_accessible_and_do_not_overflow(badges, width):
    page = badges
    page.set_viewport_size({"width": width, "height": 900})
    expect(card(page, "unsure").locator(".apply-badge")).to_be_visible()
    violations = Axe().run(page, context="#results", options=AXE_OPTIONS).get("violations", [])
    assert not violations, [(item["id"], item["help"]) for item in violations]
    assert page.evaluate("() => document.documentElement.scrollWidth <= document.documentElement.clientWidth")
