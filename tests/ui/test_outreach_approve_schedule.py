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
                "VALUES(?, ?, 'initial', ?, ?, 'America/Chicago', ?, 'scheduled', ?, ?)",
                (target_id, USER, body["fingerprint"], now, LABEL, now, now))
        route.fulfill(json={"kind": "initial", "send_at": now, "label": LABEL, "state": "scheduled"})

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
