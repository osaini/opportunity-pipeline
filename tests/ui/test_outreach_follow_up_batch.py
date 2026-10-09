"""Ticking follow-ups under Follow-ups due and queuing or sending them together.

The live server has no Google client, so the listing is patched to say Gmail is
connected (ui_helpers.gmail_listing) and the schedule and send requests are
answered here; approving goes to the real server. The scheduling switch is
written straight into the throwaway database.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import expect

from opportunity_app.core.timestamps import utc_now
from ui_helpers import BEARER, card_for, db, gmail_listing, open_outreach, open_tab, row_for, seed_target

USER = "local-user"
LABEL = "Mon, Oct 5, 9:12 AM CDT (their time, from Austin, TX)"


def due(company, **overrides):
    """A company written to a while ago whose follow-up is written and due."""
    slug = company.lower().replace(" ", "")
    return {
        "company": company, "website": f"https://{slug}.example", "contact_email": f"jane@{slug}.example",
        "contact_name": "Jane Doe", "status": "sent", "follow_up_at": "2026-01-05",
        "email_subject": "Internship question", "email_body": "Hi Jane,\n\nWould you be open to a call?\n\nTest Student",
        "follow_up_subject": "Re: Internship question", "follow_up_body": "Hi Jane,\n\nJust following up on my note.\n\nTest Student",
        **overrides,
    }


def turn_on_scheduling(live_server):
    with db(live_server) as conn, conn:
        conn.execute(
            "INSERT INTO user_settings(user_id, key, value, updated_at) VALUES(?, 'scheduled_sending', 'on', ?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value='on'", (USER, utc_now()))


def follow_up_status(live_server, target_id):
    with db(live_server) as conn:
        return conn.execute("SELECT follow_up_status FROM outreach_targets WHERE id=?", (target_id,)).fetchone()[0]


def answer_schedule(page, live_server, requests):
    """Write the scheduled row the real route would, then answer as it does."""
    def schedule(route):
        body = json.loads(route.request.post_data)
        target_id = route.request.url.split("/outreach/")[1].split("/")[0]
        requests.append((target_id, body))
        now = utc_now()
        with db(live_server) as conn, conn:
            conn.execute(
                "INSERT INTO outreach_scheduled_sends(target_id, user_id, kind, fingerprint, send_at, timezone, label, state, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, 'America/Chicago', ?, 'scheduled', ?, ?)",
                (target_id, USER, body["kind"], body["fingerprint"], now, LABEL, now, now))
        route.fulfill(json={"kind": body["kind"], "send_at": now, "label": LABEL, "state": "scheduled"})

    page.route("**/api/v1/outreach/*/schedule", schedule)


def answer_send(page, requests):
    def send(route):
        body = json.loads(route.request.post_data)
        target_id = route.request.url.split("/outreach/")[1].split("/")[0]
        requests.append((target_id, body))
        route.fulfill(json={"kind": "follow_up", "to": "jane@example.test", "cc": "", "account": "student@school.example",
                            "attachment": "", "status": "followed_up", "follow_up_at": None, "marked": True,
                            "message_id": f"sent-{target_id}", "thread_id": "thread-1", "fingerprint": body["fingerprint"]})

    page.route("**/api/v1/outreach/*/gmail-send", send)


def pick(page, company):
    return row_for(page, company).locator("xpath=..").locator(".outreach-pick")


def test_rows_under_follow_ups_due_are_ticked_one_by_one_a_run_at_a_time_or_all(owner_page, live_server, base_url):
    for company in ("Alpha Co", "Bravo Co", "Charlie Co"):
        seed_target(owner_page, base_url, **due(company))
    seed_target(owner_page, base_url, **due("Later Co", follow_up_at="2099-01-05"))
    turn_on_scheduling(live_server)
    gmail_listing(owner_page, bounce_check=True)
    open_outreach(owner_page, "follow-ups-due")
    bar = owner_page.locator(".outreach-batch")
    queue = bar.locator(".outreach-batch-queue")
    send = bar.locator(".outreach-batch-send")
    select_all = bar.get_by_label("Select all")
    expect(owner_page.locator(".outreach-pick")).to_have_count(3)
    expect(bar.locator(".outreach-batch-count")).to_have_text("0 of 3 selected")
    expect(queue).to_have_text("Queue for their morning")
    expect(queue).to_be_disabled()
    expect(send).to_have_text("Send now")
    expect(send).to_be_disabled()

    # A tick never opens the company; the pane stays where it was.
    opened = owner_page.locator(".outreach-pane h3").text_content()
    pick(owner_page, "Charlie Co").check()
    expect(owner_page.locator(".outreach-pane h3")).to_have_text(opened)
    expect(bar.locator(".outreach-batch-count")).to_have_text("1 of 3 selected")
    expect(queue).to_have_text("Queue 1 for their morning")
    expect(send).to_have_text("Send 1 now")
    expect(queue).to_be_enabled()
    assert select_all.evaluate("box => box.indeterminate")

    select_all.check()
    expect(owner_page.locator(".outreach-pick:checked")).to_have_count(3)
    expect(queue).to_have_text("Queue all for their morning")
    expect(send).to_have_text("Send all now")
    select_all.uncheck()
    expect(owner_page.locator(".outreach-pick:checked")).to_have_count(0)

    # Shift ticks the run since the last tick, as in Gmail.
    boxes = owner_page.locator(".outreach-pick")
    boxes.nth(0).click()
    boxes.nth(2).click(modifiers=["Shift"])
    expect(owner_page.locator(".outreach-pick:checked")).to_have_count(3)

    # Ticks survive a reload, and a new pick of the rail tab starts again.
    boxes.nth(1).click()
    owner_page.locator(".outreach-refresh").click()
    expect(owner_page.locator(".outreach-pick:checked")).to_have_count(2)
    open_tab(owner_page, "follow-ups-due")
    expect(owner_page.locator(".outreach-pick:checked")).to_have_count(0)

    # Only Follow-ups due takes batch actions.
    open_tab(owner_page, "awaiting")
    expect(owner_page.locator(".outreach-pick")).to_have_count(0)
    expect(owner_page.locator(".outreach-batch")).to_have_count(0)
    owner_page.unroute_all(behavior="ignoreErrors")


def test_queue_approves_and_schedules_each_ticked_follow_up_and_names_any_left_out(owner_page, live_server, base_url):
    alpha = seed_target(owner_page, base_url, **due("Alpha Co"))
    bravo = seed_target(owner_page, base_url, **due("Bravo Co"))
    approved = owner_page.request.post(f"{base_url}/api/v1/outreach/{bravo['id']}/approve", headers=BEARER,
                                       data={"kind": "follow_up", "fingerprint": bravo["follow_up_fingerprint"]})
    assert approved.ok, approved.text()
    seed_target(owner_page, base_url, **due("Charlie Co", follow_up_subject="", follow_up_body=""))
    turn_on_scheduling(live_server)
    gmail_listing(owner_page, bounce_check=True)
    requests = []
    answer_schedule(owner_page, live_server, requests)
    open_outreach(owner_page, "follow-ups-due")
    bar = owner_page.locator(".outreach-batch")
    bar.get_by_label("Select all").check()
    queue = bar.locator(".outreach-batch-queue")
    queue.click()
    expect(queue).to_have_text("Queue 3 follow-ups?")
    assert requests == [] and follow_up_status(live_server, alpha["id"]) == "generated", "the first press only asks"
    queue.click()

    status = owner_page.locator(".outreach-batch-status")
    expect(status).to_contain_text("Queued 2 follow-ups, each for its recipient's next weekday morning. 1 company was left out:")
    expect(status.locator("li")).to_have_text(["Charlie Co: no follow-up is written yet"])
    assert follow_up_status(live_server, alpha["id"]) == "approved"
    assert sorted(requests) == sorted([
        (alpha["id"], {"kind": "follow_up", "fingerprint": alpha["follow_up_fingerprint"]}),
        (bravo["id"], {"kind": "follow_up", "fingerprint": bravo["follow_up_fingerprint"]}),
    ])
    # A queued follow-up is handled, so it leaves Follow-ups due for Scheduled; what was left out stays, still ticked.
    expect(owner_page.locator(".outreach-row-company")).to_have_text(["Charlie Co"])
    expect(pick(owner_page, "Charlie Co")).to_be_checked()
    expect(owner_page.locator('#subnav [data-subtab="follow-ups-due"] .subnav-count')).to_have_text("1")
    open_tab(owner_page, "scheduled")
    expect(owner_page.locator(".outreach-row-company")).to_have_text(["Alpha Co", "Bravo Co"])
    expect(card_for(owner_page, "Alpha Co").locator(".outreach-next-text strong")).to_have_text("Next: Follow-up scheduled")
    owner_page.unroute_all(behavior="ignoreErrors")


@pytest.mark.allow_page_errors  # the approval that asks about warnings is a 422 by design
def test_send_now_sends_each_ticked_follow_up_and_asks_once_about_warnings(owner_page, live_server, base_url):
    alpha = seed_target(owner_page, base_url, **due("Alpha Co"))
    # A guessed address is a warning Approve asks about.
    guessed = seed_target(owner_page, base_url, **due("Guess Co", contact_confidence="unverified"))
    gmail_listing(owner_page)
    requests = []
    answer_send(owner_page, requests)
    open_outreach(owner_page, "follow-ups-due")
    bar = owner_page.locator(".outreach-batch")
    # Without scheduled sending, queuing says why it is off; Send now works.
    expect(bar.locator(".outreach-batch-queue")).to_be_disabled()
    expect(bar.locator(".outreach-batch-why")).to_contain_text("Turn on Send on their weekday morning")
    bar.get_by_label("Select all").check()
    send = bar.locator(".outreach-batch-send")

    questions = []

    def decline(dialog):
        questions.append(dialog.message)
        dialog.dismiss()

    owner_page.on("dialog", decline)
    send.click()
    expect(send).to_have_text("Send 2 follow-ups now?")
    send.click()

    status = owner_page.locator(".outreach-batch-status")
    expect(status).to_contain_text("Sent 1 follow-up. 1 company was left out:")
    expect(status.locator("li")).to_have_text(["Guess Co: its follow-up has warnings to review, so it is not approved"])
    assert requests == [(alpha["id"], {"kind": "follow_up", "fingerprint": alpha["follow_up_fingerprint"]})]
    assert len(questions) == 1 and "Guess Co" in questions[0] and "guessed address" in questions[0]
    assert follow_up_status(live_server, guessed["id"]) == "generated"

    # Saying yes approves it with its warnings accepted and sends it.
    owner_page.remove_listener("dialog", decline)
    owner_page.once("dialog", lambda dialog: dialog.accept())
    pick(owner_page, "Guess Co").check()
    send.click()
    send.click()
    expect(status).to_contain_text("Sent 1 follow-up.")
    assert requests[-1][0] == guessed["id"]
    assert follow_up_status(live_server, guessed["id"]) == "approved"
    owner_page.unroute_all(behavior="ignoreErrors")


@pytest.mark.allow_page_errors  # the refusal is shown as an error on purpose
def test_unsaved_edits_in_the_open_follow_up_stop_a_batch(owner_page, live_server, base_url):
    seed_target(owner_page, base_url, **due("Alpha Co"))
    gmail_listing(owner_page)
    requests = []
    answer_send(owner_page, requests)
    open_outreach(owner_page, "follow-ups-due")
    card = card_for(owner_page, "Alpha Co")
    card.locator('textarea[name="follow_up_body"]').fill("Hi Jane,\n\nA different follow-up.\n\nTest Student")
    pick(owner_page, "Alpha Co").check()
    send = owner_page.locator(".outreach-batch-send")
    send.click()
    expect(owner_page.locator("#error-banner")).to_contain_text("The Alpha Co follow-up has unsaved edits")
    expect(send).to_have_text("Send all now")
    assert requests == []
    owner_page.unroute_all(behavior="ignoreErrors")
