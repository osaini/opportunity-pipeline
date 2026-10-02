"""What the Outreach page says about sending must match what can actually go out.

The page status and each draft's note used to promise "Nothing sends from here" /
"Nothing sends on its own" whatever was switched on. With Gmail connected, approving a
draft unlocks Send, and Resend automatically after a bounce, Send a thank-you when someone
declines and Send through contact forms each send without a click. The student can approve
a draft trusting that promise, so every sentence about sending is built from the live
switches. Switches are written straight into the throwaway database (a real switch may
refuse to turn on without Jev or a sending address, which this page does not care about).
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from opportunity_app.automation import ledger as automation
from opportunity_app.core.timestamps import utc_now
from ui_helpers import card_for, db, gmail_listing, open_details, open_outreach, open_tab, seed_target

USER = "local-user"
SWITCHES = {
    "bounce_auto_resend": "after a bounce",
    "decline_thank_you": "thank-you",
    "form_submission": "contact form",
}


def switch_on(live_server, *keys):
    with db(live_server) as conn, conn:
        for key in keys:
            conn.execute(
                "INSERT INTO user_settings(user_id, key, value, updated_at) VALUES(?, ?, 'on', ?) "
                "ON CONFLICT(user_id, key) DO UPDATE SET value='on'", (USER, key, utc_now()))


def seed_drafted(owner_page, base_url):
    return seed_target(owner_page, base_url, contact_email="jane@bovi.example", contact_name="Jane Doe",
                       email_subject="Internship question", email_body="Hi Jane,\n\nWould you be open to a call?\n\nTest Student")


def note_text(card):
    return card.locator(".outreach-note").filter(has_text=re.compile("Approving a draft")).first


def test_with_gmail_connected_the_page_does_not_promise_that_nothing_sends(owner_page, base_url):
    seed_drafted(owner_page, base_url)
    gmail_listing(owner_page)
    open_outreach(owner_page)
    status = owner_page.locator("#page-status")
    expect(status).not_to_contain_text("Nothing sends from here")
    expect(status).to_contain_text("press Send")
    expect(status).to_contain_text("confirm the recipient")
    owner_page.unroute_all(behavior="ignoreErrors")


@pytest.mark.parametrize("key,words", list(SWITCHES.items()))
def test_an_automatic_sending_switch_is_named_on_the_page(owner_page, live_server, base_url, key, words):
    seed_drafted(owner_page, base_url)
    switch_on(live_server, key)
    gmail_listing(owner_page)
    open_outreach(owner_page)
    status = owner_page.locator("#page-status")
    expect(status).not_to_contain_text("Nothing sends")
    expect(status).to_contain_text(words)
    expect(status).to_contain_text("without a click")
    owner_page.unroute_all(behavior="ignoreErrors")


def test_the_page_names_the_switch_even_before_gmail_is_connected(owner_page, live_server, base_url):
    """Send through contact forms needs no Gmail, so "nothing sends from here" is wrong with Gmail off too."""
    seed_drafted(owner_page, base_url)
    switch_on(live_server, "form_submission")
    open_outreach(owner_page)
    status = owner_page.locator("#page-status")
    expect(status).not_to_contain_text("Nothing sends")
    expect(status).to_contain_text("contact form")


def test_without_any_switch_the_page_still_says_nothing_sends_on_its_own(owner_page, base_url):
    """The reassurance stays when it is true."""
    seed_drafted(owner_page, base_url)
    open_outreach(owner_page)
    expect(owner_page.locator("#page-status")).to_contain_text("Nothing sends from here")


@pytest.mark.parametrize("key,words", [("bounce_auto_resend", "after a bounce"), ("decline_thank_you", "thank-you")])
def test_a_draft_note_with_gmail_does_not_say_nothing_sends_on_its_own(owner_page, live_server, base_url, key, words):
    seed_drafted(owner_page, base_url)
    switch_on(live_server, key)
    gmail_listing(owner_page)
    open_outreach(owner_page)
    note = note_text(open_details(card_for(owner_page, "Bovi")))
    expect(note).not_to_contain_text("Nothing sends on its own")
    expect(note).to_contain_text(words)
    expect(note).to_contain_text("without a click")
    owner_page.unroute_all(behavior="ignoreErrors")


def test_a_draft_note_with_gmail_and_no_switch_keeps_its_promise(owner_page, base_url):
    seed_drafted(owner_page, base_url)
    gmail_listing(owner_page)
    open_outreach(owner_page)
    expect(note_text(open_details(card_for(owner_page, "Bovi")))).to_contain_text("Nothing sends on its own")
    owner_page.unroute_all(behavior="ignoreErrors")


def test_a_draft_note_without_gmail_names_an_automatic_switch(owner_page, live_server, base_url):
    seed_drafted(owner_page, base_url)
    switch_on(live_server, "bounce_auto_resend")
    open_outreach(owner_page)
    note = note_text(open_details(card_for(owner_page, "Bovi")))
    expect(note).not_to_contain_text("Nothing sends from here")
    expect(note).to_contain_text("after a bounce")


def test_a_pause_is_reflected_in_the_draft_note(owner_page, live_server, base_url):
    seed_drafted(owner_page, base_url)
    switch_on(live_server, "decline_thank_you")
    with db(live_server) as conn:
        automation.set_paused(conn, USER, True)
    gmail_listing(owner_page)
    owner_page.reload()
    open_outreach(owner_page)
    note = note_text(open_details(card_for(owner_page, "Bovi")))
    expect(note).to_contain_text("paused")
    expect(note).not_to_contain_text("Nothing sends on its own")
    owner_page.unroute_all(behavior="ignoreErrors")


def test_the_connect_gmail_panel_keeps_its_promise_with_no_switch_on(owner_page, base_url):
    seed_drafted(owner_page, base_url)
    gmail_listing(owner_page, connected=False)
    open_outreach(owner_page)
    expect(owner_page.locator(".outreach-gmail-connect .profile-help")).to_contain_text("Nothing sends until you press Send")
    owner_page.unroute_all(behavior="ignoreErrors")


def test_the_connect_gmail_panel_does_not_promise_that_nothing_sends_with_a_switch_on(owner_page, live_server, base_url):
    seed_drafted(owner_page, base_url)
    switch_on(live_server, "decline_thank_you")
    gmail_listing(owner_page, connected=False)
    open_outreach(owner_page)
    panel = owner_page.locator(".outreach-gmail-connect .profile-help")
    expect(panel).not_to_contain_text("Nothing sends")
    expect(panel).to_contain_text("a thank-you when someone declines")
    owner_page.unroute_all(behavior="ignoreErrors")

def test_the_connect_gmail_panel_follows_pause_and_resume_while_it_is_open(owner_page, live_server, base_url):
    """The panel's sentence is built through pauseWords, so a pause or resume rewrites it in place."""
    seed_drafted(owner_page, base_url)
    switch_on(live_server, "decline_thank_you")
    gmail_listing(owner_page, connected=False)
    open_outreach(owner_page)
    panel = owner_page.locator(".outreach-gmail-connect .profile-help")
    expect(panel).to_contain_text("These go out without a click")
    expect(panel).not_to_contain_text("paused")
    with db(live_server) as conn:
        automation.set_paused(conn, USER, True)
    owner_page.evaluate("() => window.OpportunityApp.refreshAutomationStatus()")
    banner = owner_page.locator("#automation-banner")
    expect(banner).to_contain_text("Automation is paused.")
    expect(panel).to_contain_text("paused")
    expect(panel).to_contain_text("none of it goes out until you resume")
    expect(panel).not_to_contain_text("These go out without a click")
    banner.get_by_role("button", name="Resume").click()
    expect(banner).to_be_hidden()
    expect(panel).to_contain_text("These go out without a click")
    expect(panel).not_to_contain_text("paused")
    owner_page.unroute_all(behavior="ignoreErrors")


def test_turning_a_sending_switch_on_in_settings_updates_the_page_status(owner_page, base_url):
    seed_drafted(owner_page, base_url)
    open_outreach(owner_page)
    open_tab(owner_page, "settings")
    status = owner_page.locator("#page-status")
    expect(status).to_contain_text("Nothing sends from here")
    owner_page.locator("#settings-automation-form_submission").check()
    expect(status).not_to_contain_text("Nothing sends")
    expect(status).to_contain_text("contact forms")
    owner_page.locator("#settings-automation-form_submission").uncheck()
    expect(status).to_contain_text("Nothing sends from here")
