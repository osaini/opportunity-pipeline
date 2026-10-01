"""The thank-you after a plain decline in the browser: the card, its actions, the pause, the switch, and the writer.

The server runs in this process, so a test seeds the live database directly, the
way outreach_thank_you.plan would. The live server has no Gmail, so the two
actions that reach Gmail (Send it anyway, Edit) are answered by a route that
records what the server would have, the way tests/ui/test_automation.py patches
Gmail's listing; the server side is covered by tests/test_decline_thank_you.py.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone

from playwright.sync_api import expect

import outreach_fakes
from conftest import OWNER_TOKEN, wait_for_results
from ui_helpers import assert_accessible, card_for, db, open_outreach, seed_target
from opportunity_app import automation
from opportunity_app.outreach_gmail import thank_you_fingerprint
from opportunity_app.timestamps import utc_now

BEARER = {"Authorization": f"Bearer {OWNER_TOKEN}"}
USER = "local-user"
LABEL = "Tue, Sep 29, 11:32 AM CDT (their time, from Austin, TX)"
BODY = (
    "Hi Dana,\n\nThank you for getting back to me, and for taking the time to consider it. "
    "I appreciate it, and I wish you and the Acme Robotics team all the best.\n\nBest,\nTest Student"
)
SUBJECT = "Re: Robotics internship question"


def declined_company(page, base_url, live_server, company="Acme Robotics"):
    target = seed_target(page, base_url, company=company, website="https://acme.example", contact_email="dana@acme.example",
                         contact_name="Dana Lee", location="Austin, TX")
    with db(live_server) as conn, conn:
        conn.execute("UPDATE outreach_targets SET status='declined', sent_at='2026-09-21' WHERE id=?", (target["id"],))
    return target


def seed_thank_you(live_server, target_id, *, state="scheduled", send_state="scheduled", note="", generated_by="template", to_name="Dana Lee"):
    fingerprint = thank_you_fingerprint("dana@acme.example", to_name, SUBJECT, BODY, "<d-1@acme.example>", "t-decline")
    now = utc_now()
    send_at = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(timespec="seconds")
    with db(live_server) as conn, conn:
        conn.execute(
            "INSERT INTO outreach_thank_yous(target_id, user_id, reply_gmail_id, reply_message_id, thread_id, to_email, to_name, subject, "
            "body, generated_by, fingerprint, state, note, send_at, label, created_at, updated_at) "
            "VALUES(?, ?, 'd-1', '<d-1@acme.example>', 't-decline', 'dana@acme.example', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (target_id, USER, to_name, SUBJECT, BODY, generated_by, fingerprint, state, note, send_at, LABEL, now, now),
        )
        if send_state:
            conn.execute(
                "INSERT INTO outreach_scheduled_sends(target_id, user_id, kind, fingerprint, send_at, timezone, label, state, error, attempts, "
                "created_at, updated_at) VALUES(?, ?, 'thank_you', ?, ?, 'America/Chicago', ?, ?, '', 0, ?, ?)",
                (target_id, USER, fingerprint, send_at, LABEL, send_state, now, now),
            )
    return fingerprint


def stored(live_server, target_id):
    with db(live_server) as conn:
        return dict(conn.execute("SELECT * FROM outreach_thank_yous WHERE target_id=?", (target_id,)).fetchone())


def thank_you_box(page, company="Acme Robotics"):
    return card_for(page, company).locator(".outreach-thank-you")


def test_a_waiting_thank_you_shows_when_it_goes_and_cancel_stops_it(owner_page, base_url, live_server):
    target = declined_company(owner_page, base_url, live_server)
    seed_thank_you(live_server, target["id"])
    open_outreach(owner_page, "scheduled")
    box = thank_you_box(owner_page)
    expect(box.locator(".outreach-thank-you-when")).to_have_text(f"Thank-you to Dana goes out {LABEL}.")
    box.locator("summary", has_text="Read the thank-you").click()
    expect(box.locator("pre")).to_have_text(BODY)
    expect(box).to_contain_text(f"To Dana Lee <dana@acme.example> · {SUBJECT}")
    expect(box).to_contain_text("Fixed words, with no model.")
    expect(box.get_by_role("button", name="Edit the thank-you to Dana in your Gmail Drafts")).to_be_visible()
    box.get_by_role("button", name="Cancel the thank-you to Dana").click()
    expect(owner_page.locator("#action-status")).to_have_text("Cancelled the thank-you to Dana.")
    open_outreach(owner_page, "closed")
    box = thank_you_box(owner_page)
    expect(box).to_have_text("Thank-you to Dana not sent: You cancelled it")
    assert stored(live_server, target["id"])["state"] == "cancelled"


def test_while_paused_the_card_says_it_goes_after_resume_and_says_its_time_again_on_resume(owner_page, base_url, live_server):
    target = declined_company(owner_page, base_url, live_server)
    seed_thank_you(live_server, target["id"])
    with db(live_server) as conn:
        automation.set_paused(conn, USER, True)
    owner_page.reload()
    wait_for_results(owner_page)
    banner = owner_page.locator("#automation-banner")
    expect(banner).to_contain_text("Automation is paused.")
    open_outreach(owner_page, "closed")
    when = thank_you_box(owner_page).locator(".outreach-thank-you-when")
    expect(when).to_have_text(f"Thank-you to Dana. Paused. Goes out after you resume (was {LABEL}).")
    banner.get_by_role("button", name="Resume").click()
    expect(banner).to_be_hidden()
    expect(when).to_have_text(f"Thank-you to Dana goes out {LABEL}.")


def test_a_held_thank_you_shows_why_and_is_sent_anyway_after_a_confirm(owner_page, base_url, live_server):
    target = declined_company(owner_page, base_url, live_server)
    fingerprint = seed_thank_you(live_server, target["id"], state="held", send_state="cancelled",
                                 note="The reviewer held it: Their reply mentions a call next spring")
    sent = {}

    def send(route):
        sent.update(json.loads(route.request.post_data or "{}"))
        with db(live_server) as conn, conn:
            conn.execute("UPDATE outreach_thank_yous SET state='sent', note='', updated_at=? WHERE target_id=?", (utc_now(), target["id"]))
        route.fulfill(json={"to": "dana@acme.example", "thread_id": "t-decline"})

    owner_page.route(re.compile(r".*/api/v1/outreach/[^/]+/thank-you/send$"), send)
    open_outreach(owner_page, "closed")
    box = thank_you_box(owner_page)
    expect(box.locator(".outreach-research-warning")).to_have_text(
        "Thank-you to Dana held: The reviewer held it: Their reply mentions a call next spring.")
    expect(box.get_by_role("button", name="Send it anyway")).to_be_visible()
    button = box.locator(".outreach-thank-you-actions .primary-button")
    button.click()
    expect(button).to_have_text("Send to dana@acme.example?")
    assert sent == {}, "the first click only asks"
    button.click()
    box = thank_you_box(owner_page)
    expect(box.locator(".outreach-fit")).to_have_text(re.compile(r"^Thank-you sent .+\.$"))
    assert sent == {"fingerprint": fingerprint}
    link = box.get_by_role("link", name="Open the thread in Gmail, with Dana")
    expect(link).to_have_attribute("href", re.compile(r"#all/t-decline$"))
    owner_page.unroute_all(behavior="ignoreErrors")


def test_dismiss_drops_a_held_thank_you(owner_page, base_url, live_server):
    target = declined_company(owner_page, base_url, live_server)
    seed_thank_you(live_server, target["id"], state="failed", send_state="failed", note="Gmail did not confirm it (HTTP 503)")
    open_outreach(owner_page, "closed")
    box = thank_you_box(owner_page)
    # Gmail may have sent it, so the card never says it was not sent.
    expect(box.locator(".outreach-research-warning")).to_have_text("Thank-you to Dana stopped: Gmail did not confirm it (HTTP 503).")
    box.get_by_role("button", name="Dismiss the thank-you to Dana").click()
    expect(thank_you_box(owner_page)).to_have_text("Thank-you to Dana not sent: You dismissed it")
    assert stored(live_server, target["id"])["state"] == "cancelled"


def test_edit_moves_it_to_gmail_drafts(owner_page, base_url, live_server):
    target = declined_company(owner_page, base_url, live_server)
    seed_thank_you(live_server, target["id"])
    called = []

    def edit(route):
        called.append(route.request.method)
        # What the server records once Gmail has made the draft.
        with db(live_server) as conn, conn:
            conn.execute("UPDATE outreach_scheduled_sends SET state='cancelled' WHERE target_id=? AND kind='thank_you'", (target["id"],))
            conn.execute("UPDATE outreach_thank_yous SET state='cancelled', note='Moved to your Gmail Drafts' WHERE target_id=?", (target["id"],))
            conn.execute(
                "INSERT INTO outreach_events(id, target_id, user_id, event_type, detail, created_at) VALUES(?, ?, ?, 'thank_you_draft_created', ?, ?)",
                (f"ev-{target['id']}", target["id"], USER, json.dumps({"draft_id": "r-1", "message_id": "18c1", "thread_id": "t-decline"}), utc_now()),
            )
        route.fulfill(json={"draft": {"url": "https://mail.google.com/"}, "target": {}})

    owner_page.route(re.compile(r".*/api/v1/outreach/[^/]+/thank-you/edit$"), edit)
    open_outreach(owner_page, "closed")
    thank_you_box(owner_page).get_by_role("button", name="Edit the thank-you to Dana in your Gmail Drafts").click()
    box = thank_you_box(owner_page)
    expect(box.locator(".outreach-fit")).to_have_text("Thank-you to Dana is in your Gmail Drafts, to edit and send yourself.")
    expect(box.get_by_role("link", name="Open the draft in Gmail: the thank-you to Dana")).to_have_attribute("href", re.compile(r"drafts\?compose=18c1$"))
    assert called == ["POST"]
    owner_page.unroute_all(behavior="ignoreErrors")


def test_a_waiting_card_says_it_will_be_held_while_its_switch_is_off(owner_page, base_url, live_server):
    target = declined_company(owner_page, base_url, live_server)
    # "Dr." is a title: the card names her as the email greets her.
    seed_thank_you(live_server, target["id"], to_name="Dr. Dana Lee")
    saved = owner_page.request.put(f"{base_url}/api/v1/automation/settings", headers=BEARER, data={"modes": {"jev_inbox_suggestions": "on"}})
    assert saved.ok, saved.text()
    owner_page.reload()
    wait_for_results(owner_page)
    open_outreach(owner_page, "scheduled")
    box = thank_you_box(owner_page)
    expect(box.locator(".outreach-thank-you-when")).to_have_text(f"Thank-you to Dana goes out {LABEL}.")
    hold = box.locator(".outreach-thank-you-hold")
    expect(hold).to_have_text("It will be held at its time, not sent: Send a thank-you when someone declines is off.")
    owner_page.click("#profile-nav")
    wait_for_results(owner_page)
    owner_page.locator("#automation-mode-decline_thank_you").check()
    status = owner_page.locator(".automation-group", has=owner_page.locator("#automation-mode-decline_thank_you")).locator(".form-status")
    expect(status).to_have_text("Send a thank-you when someone declines: on.")
    open_outreach(owner_page, "scheduled")
    expect(thank_you_box(owner_page).locator(".outreach-thank-you-hold")).to_be_hidden()


def test_cancel_on_a_card_a_check_already_stopped_says_so(owner_page, base_url, live_server):
    target = declined_company(owner_page, base_url, live_server)
    seed_thank_you(live_server, target["id"])
    open_outreach(owner_page, "scheduled")
    box = thank_you_box(owner_page)
    expect(box.locator(".outreach-thank-you-when")).to_be_visible()
    # The check before sending stopped it after the page loaded: they wrote again.
    with db(live_server) as conn, conn:
        conn.execute("UPDATE outreach_scheduled_sends SET state='cancelled' WHERE target_id=? AND kind='thank_you'", (target["id"],))
        conn.execute("UPDATE outreach_thank_yous SET state='cancelled', note=? WHERE target_id=?",
                     ("They wrote again, so the thank-you was not sent. Read their reply.", target["id"]))
    box.get_by_role("button", name="Cancel the thank-you to Dana").click()
    expect(owner_page.locator("#action-status")).to_have_text(
        "The thank-you to Dana had already stopped: They wrote again, so the thank-you was not sent. Read their reply.")


def test_a_reply_that_fails_a_rule_says_so_plainly_on_the_card(owner_page, base_url, live_server):
    target = declined_company(owner_page, base_url, live_server)
    # The check before sending found the reply was sent by a system (outreach_reply_senders.thank_you_blockers).
    seed_thank_you(live_server, target["id"], state="cancelled", send_state="cancelled",
                   note="Not thanked automatically: sent by an automated system")
    open_outreach(owner_page, "closed")
    box = thank_you_box(owner_page)
    expect(box).to_have_text("Not thanked automatically: sent by an automated system.")
    expect(box.get_by_role("button")).to_have_count(0)


def test_the_history_names_each_step(owner_page, base_url, live_server):
    target = declined_company(owner_page, base_url, live_server)
    seed_thank_you(live_server, target["id"], state="sent", send_state="sent")
    with db(live_server) as conn, conn:
        for number, (event_type, detail) in enumerate((
            ("thank_you_scheduled", f"A thank-you to dana@acme.example goes out {LABEL}"),
            ("thank_you_reviewed", "Passed by codex-cli"),
            ("thank_you_sent", json.dumps({"message_id": "sent-2", "thread_id": "t-decline"})),
        )):
            conn.execute(
                "INSERT INTO outreach_events(id, target_id, user_id, event_type, detail, created_at) VALUES(?, ?, ?, ?, ?, ?)",
                (f"ev-{number}", target["id"], USER, event_type, detail, f"2026-09-29T1{number}:00:00+00:00"),
            )
    open_outreach(owner_page, "closed")
    card = card_for(owner_page, "Acme Robotics")
    expect(card.locator(".outreach-thank-you .outreach-fit")).to_have_text(re.compile(r"^Thank-you sent "))
    card.get_by_role("tab", name="Replies and history", exact=True).click()
    timeline = card.locator(".outreach-timeline")
    expect(timeline).to_contain_text("Thank-you scheduled")
    expect(timeline).to_contain_text("Thank-you reviewed")
    expect(timeline).to_contain_text("Thank-you sent")
    expect(timeline).not_to_contain_text("sent-2", timeout=1_000)


def test_the_automation_panel_lists_the_switch_and_says_it_needs_jev(owner_page, base_url):
    owner_page.click("#profile-nav")
    wait_for_results(owner_page)
    section = owner_page.locator(".automation-section")
    feature = section.locator('[data-automation-feature="decline_thank_you"]')
    expect(feature).to_contain_text("Send a thank-you when someone declines")
    expect(feature.locator(".chip")).to_have_text("External")
    expect(feature.locator(".automation-reason")).to_contain_text("On is not available yet: it needs Jev inbox suggestions on")
    expect(section.locator("#automation-mode-decline_thank_you")).not_to_be_checked()
    saved = owner_page.request.put(f"{base_url}/api/v1/automation/settings", headers=BEARER,
                                   data={"modes": {"jev_inbox_suggestions": "on"}})
    assert saved.ok, saved.text()
    owner_page.reload()
    wait_for_results(owner_page)
    owner_page.click("#profile-nav")
    wait_for_results(owner_page)
    box = owner_page.locator("#automation-mode-decline_thank_you")
    expect(owner_page.locator('[data-automation-feature="decline_thank_you"] .automation-reason')).to_be_hidden()
    box.check()
    status = owner_page.locator(".automation-group", has=box).locator(".form-status")
    expect(status).to_have_text("Send a thank-you when someone declines: on.")


def test_the_thank_you_writer_is_chosen_in_outreach_settings(owner_page, restored_environment):
    open_outreach(owner_page, "settings")
    panel = owner_page.locator("section.outreach-settings")
    writer = panel.get_by_label("Who writes thank-yous after a decline")
    expect(writer).to_have_value("")
    expect(writer.locator("option").first).to_have_text("Same as first-email drafts")
    writer.select_option("legacy")
    expect(panel.locator(".form-status")).to_have_text("Thank-you writer saved.")
    assert os.environ["PIPELINE_OUTREACH_THANK_YOU_PROVIDER"] == "legacy"
    assert "PIPELINE_OUTREACH_THANK_YOU_PROVIDER=legacy" in outreach_fakes.SETTINGS_ENV.read_text(encoding="utf-8")
    assert_accessible(owner_page, "the outreach settings tab with the thank-you writer")


def test_thank_yous_on_the_cards_are_accessible_in_both_themes(owner_page, base_url, live_server):
    waiting = declined_company(owner_page, base_url, live_server)
    seed_thank_you(live_server, waiting["id"])
    held = declined_company(owner_page, base_url, live_server, company="Bovi")
    seed_thank_you(live_server, held["id"], state="held", send_state="cancelled", note="The reviewer held it: Unsure of the tone",
                   generated_by="anthropic:claude-sonnet-5")
    open_outreach(owner_page, "closed")
    box = thank_you_box(owner_page)
    box.locator("summary").click()
    expect(box.locator("pre")).to_be_visible()
    assert_accessible(owner_page, "a card with a waiting thank-you")
    held_box = thank_you_box(owner_page, "Bovi")
    held_box.locator("summary").click()
    expect(held_box).to_contain_text("Written by Anthropic.")
    assert_accessible(owner_page, "a card with a held thank-you")
    owner_page.evaluate("document.documentElement.dataset.theme = 'dark'")
    assert_accessible(owner_page, "a card with a held thank-you in dark mode")
