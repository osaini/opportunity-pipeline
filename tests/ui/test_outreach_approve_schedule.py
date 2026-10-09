"""One press that confirms the research, approves the draft and schedules it for the recipient's morning.

It sits beside the separate buttons, which stay. The live server has no Google
client, so the listing is patched to say Gmail is connected (as in
tests/ui/test_automation.py) and the schedule request is answered here, after
writing the row the real route would; confirming research and approving go to
the real server. The scheduling switch is written straight into the throwaway
database.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import expect

from opportunity_app.core.timestamps import utc_now
from ui_helpers import card_for, db, gmail_listing, open_details, open_outreach, seed_target

USER = "local-user"
LABEL = "Mon, Oct 5, 9:12 AM CDT (their time, from Austin, TX)"
BUTTON = "Confirm research, approve and schedule"


def seed_unverified(owner_page, live_server, base_url, *, scheduling=True):
    target = seed_target(owner_page, base_url, contact_email="jane@bovi.example", contact_name="Jane Doe",
                         email_subject="Internship question", email_body="Hi Jane,\n\nWould you be open to a call?\n\nTest Student")
    with db(live_server) as conn, conn:
        conn.execute("UPDATE outreach_targets SET research_confidence='unverified' WHERE id=?", (target["id"],))
        if scheduling:
            conn.execute(
                "INSERT INTO user_settings(user_id, key, value, updated_at) VALUES(?, 'scheduled_sending', 'on', ?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET value='on'", (USER, utc_now()))
    gmail_listing(owner_page, bounce_check=True)
    return target


def stored(live_server, target_id):
    with db(live_server) as conn:
        return tuple(conn.execute(
            "SELECT research_confidence, draft_status FROM outreach_targets WHERE id=?", (target_id,)).fetchone())


def answer_schedule(owner_page, live_server, requests):
    """Write the scheduled row the real route would, then answer as it does."""
    def schedule(route):
        body = json.loads(route.request.post_data)
        requests.append(body)
        now = utc_now()
        target_id = route.request.url.split("/outreach/")[1].split("/")[0]
        with db(live_server) as conn, conn:
            conn.execute(
                "INSERT INTO outreach_scheduled_sends(target_id, user_id, kind, fingerprint, send_at, timezone, label, state, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, 'America/Chicago', ?, 'scheduled', ?, ?)",
                (target_id, USER, body["kind"], body["fingerprint"], now, LABEL, now, now))
        route.fulfill(json={"kind": body["kind"], "send_at": now, "label": LABEL, "state": "scheduled"})

    owner_page.route("**/api/v1/outreach/*/schedule", schedule)


def test_one_press_confirms_the_research_approves_and_schedules(owner_page, live_server, base_url):
    target = seed_unverified(owner_page, live_server, base_url)
    requests = []
    answer_schedule(owner_page, live_server, requests)
    open_outreach(owner_page)
    card = card_for(owner_page, "Bovi")
    # By class: pressing it once renames it.
    button = card.locator(".outreach-approve-schedule")
    expect(button).to_have_text(BUTTON)
    # The separate buttons stay.
    expect(card.get_by_role("button", name="Confirm research", exact=True)).to_be_visible()
    expect(open_details(card, "Draft").get_by_role("button", name="Approve draft")).to_be_visible()

    button.click()
    expect(button).to_have_text("Schedule to jane@bovi.example?")
    assert stored(live_server, target["id"]) == ("unverified", "generated"), "the first press only arms it"
    button.click()

    expect(card.locator(".chip", has_text="Goes out")).to_have_text(f"Goes out {LABEL}")
    assert stored(live_server, target["id"]) == ("confirmed", "approved")
    # Approving leaves the words alone, so the schedule carries the fingerprint the student reviewed.
    assert requests == [{"kind": "initial", "fingerprint": target["draft_fingerprint"]}]
    expect(card.get_by_role("button", name=BUTTON)).to_have_count(0)
    expect(card.get_by_role("button", name="Confirm research", exact=True)).to_have_count(0)
    owner_page.unroute_all(behavior="ignoreErrors")


def test_without_scheduled_sending_only_the_separate_buttons_show(owner_page, live_server, base_url):
    seed_unverified(owner_page, live_server, base_url, scheduling=False)
    open_outreach(owner_page)
    card = card_for(owner_page, "Bovi")
    expect(card.get_by_role("button", name="Confirm research", exact=True)).to_be_visible()
    expect(card.get_by_role("button", name=BUTTON)).to_have_count(0)
    owner_page.unroute_all(behavior="ignoreErrors")


@pytest.mark.allow_page_errors  # the refused schedule is a 422 by design
def test_a_refused_schedule_says_what_was_done_and_leaves_the_schedule_button(owner_page, live_server, base_url):
    target = seed_unverified(owner_page, live_server, base_url)
    owner_page.route("**/api/v1/outreach/*/schedule", lambda route: route.fulfill(
        status=422, content_type="application/json", body='{"detail": "Reconnect Gmail once before scheduling"}'))
    open_outreach(owner_page)
    card = card_for(owner_page, "Bovi")
    button = card.locator(".outreach-approve-schedule")
    button.click()
    button.click()

    expect(owner_page.locator("#error-banner")).to_have_text(
        "Confirmed the research and approved the draft for Bovi, but it is not scheduled: Reconnect Gmail once before scheduling")
    assert stored(live_server, target["id"]) == ("confirmed", "approved")
    expect(card.get_by_role("button", name="Schedule for their morning")).to_be_visible()
    owner_page.unroute_all(behavior="ignoreErrors")


def turn_on_scheduling(live_server):
    with db(live_server) as conn, conn:
        conn.execute(
            "INSERT INTO user_settings(user_id, key, value, updated_at) VALUES(?, 'scheduled_sending', 'on', ?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value='on'", (USER, utc_now()))


DUE_FOLLOW_UP = {
    "contact_email": "jane@bovi.example", "contact_name": "Jane Doe", "status": "sent", "follow_up_at": "2026-01-05",
    "email_subject": "Internship question", "email_body": "Hi Jane,\n\nWould you be open to a call?\n\nTest Student",
    "follow_up_subject": "Re: Internship question", "follow_up_body": "Hi Jane,\n\nJust following up on my note.\n\nTest Student",
}


def follow_up_status(live_server, target_id):
    with db(live_server) as conn:
        return conn.execute("SELECT follow_up_status FROM outreach_targets WHERE id=?", (target_id,)).fetchone()[0]


def test_a_due_follow_up_is_approved_and_scheduled_for_their_morning_in_one_press(owner_page, live_server, base_url):
    target = seed_target(owner_page, base_url, **DUE_FOLLOW_UP)
    turn_on_scheduling(live_server)
    gmail_listing(owner_page, bounce_check=True)
    requests = []
    answer_schedule(owner_page, live_server, requests)
    open_outreach(owner_page, "follow-ups-due")
    card = card_for(owner_page, "Bovi")
    button = card.locator("[data-follow-up-approve-schedule]")
    expect(button).to_have_text("Approve and schedule for their morning")
    # Sending at once stays beside it, as Send now does beside Schedule.
    expect(card.locator("[data-follow-up-approve-send]")).to_have_text("Approve and send now")

    button.click()
    expect(button).to_have_text("Schedule to jane@bovi.example?")
    assert follow_up_status(live_server, target["id"]) == "generated", "the first press only arms it"
    button.click()

    next_step = card.locator(".outreach-next")
    expect(next_step.locator(".outreach-next-text strong")).to_have_text("Next: Follow-up scheduled")
    expect(next_step.locator(".chip", has_text="Goes out")).to_have_text(f"Goes out {LABEL}")
    expect(next_step.get_by_role("button", name="Cancel")).to_be_visible()
    assert follow_up_status(live_server, target["id"]) == "approved"
    assert requests == [{"kind": "follow_up", "fingerprint": target["follow_up_fingerprint"]}]
    expect(card.locator("[data-follow-up-approve-schedule]")).to_have_count(0)
    owner_page.unroute_all(behavior="ignoreErrors")


def test_without_scheduled_sending_a_follow_up_offers_only_approve_and_send(owner_page, live_server, base_url):
    seed_target(owner_page, base_url, **DUE_FOLLOW_UP)
    gmail_listing(owner_page, bounce_check=True)
    open_outreach(owner_page, "follow-ups-due")
    card = card_for(owner_page, "Bovi")
    expect(card.locator("[data-follow-up-approve-send]")).to_have_text("Approve and send")
    expect(card.locator("[data-follow-up-approve-schedule]")).to_have_count(0)
    owner_page.unroute_all(behavior="ignoreErrors")


@pytest.mark.allow_page_errors  # the refused schedule is a 422 by design
def test_a_follow_up_schedule_that_is_refused_after_approving_says_so(owner_page, live_server, base_url):
    target = seed_target(owner_page, base_url, **DUE_FOLLOW_UP)
    turn_on_scheduling(live_server)
    gmail_listing(owner_page, bounce_check=True)
    owner_page.route("**/api/v1/outreach/*/schedule", lambda route: route.fulfill(
        status=422, content_type="application/json", body='{"detail": "Reconnect Gmail once before scheduling"}'))
    open_outreach(owner_page, "follow-ups-due")
    card = card_for(owner_page, "Bovi")
    button = card.locator("[data-follow-up-approve-schedule]")
    button.click()
    button.click()
    expect(owner_page.locator("#error-banner")).to_have_text(
        "Approved the Bovi follow-up, but it is not scheduled: Reconnect Gmail once before scheduling")
    assert follow_up_status(live_server, target["id"]) == "approved"
    # The bar's own schedule button can try again.
    expect(card.get_by_role("button", name="Schedule follow-up for their morning")).to_be_visible()
    owner_page.unroute_all(behavior="ignoreErrors")
