"""The Gmail reply label: its Outreach settings field, and the Reconnect Gmail copy that asks for its permission.

The field talks to the live server's own route, so a save is checked through
the same GET a reload uses. The mailbox line and the connect panel depend on a
Gmail connection the live server does not have, so those patch the response the
way tests/ui/test_automation.py does; the server side is covered by
tests/test_outreach_labels.py and tests/test_outreach_gmail.py.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from conftest import OWNER_TOKEN
from outreach_fakes import COMPOSE_ACCOUNT
from test_automation import gmail_listing
from test_outreach_journey import open_outreach

BEARER = {"Authorization": f"Bearer {OWNER_TOKEN}"}
ROUTE = "**/api/v1/outreach/gmail-label"
OTHER_ACCOUNT = "someone@other.example"


def saved_setting(page, base_url):
    response = page.request.get(f"{base_url}/api/v1/outreach/gmail-label", headers=BEARER)
    assert response.ok, response.text()
    return response.json()


def settings_field(page):
    open_outreach(page, "settings")
    return page.locator("section.outreach-settings .gmail-label-setting")


def patch_setting(page, *, value="opportunities", connected=False, connected_as="", expected=COMPOSE_ACCOUNT, permission=False):
    """Answer the field's GET with a mailbox the live server cannot have."""
    body = {"value": value, "default": "opportunities", "search": value.lower().replace(" ", "-"),
            "mailbox": {"connected": connected, "connected_as": connected_as, "expected": expected},
            "permission": permission}
    page.route(ROUTE, lambda route: route.fulfill(json=body) if route.request.method == "GET" else route.fallback())


def test_the_label_field_starts_on_the_default_and_says_what_it_does(owner_page, base_url):
    field = settings_field(owner_page)
    box = field.get_by_label("Gmail label for replies")
    expect(box).to_have_value("opportunities")
    expect(box).to_have_attribute("placeholder", "Empty keeps labelling off; the usual name is opportunities")
    expect(box).to_be_enabled()
    expect(field).to_contain_text("Every thread where someone at a company replied gets this label in Gmail")
    expect(field).to_contain_text("Leave it empty to stop labelling; pausing automation pauses it too")
    expect(field).to_contain_text("threads keep the old label too")
    assert saved_setting(owner_page, base_url)["value"] == "opportunities"


def test_renaming_the_label_saves_at_once_and_survives_a_reload(owner_page, base_url):
    field = settings_field(owner_page)
    box = field.get_by_label("Gmail label for replies")
    box.fill("Job replies")
    box.press("Enter")
    box.blur()
    expect(field.locator(".gmail-label-status")).to_have_text("Replies will be labelled “Job replies”.")
    saved = saved_setting(owner_page, base_url)
    assert saved["value"] == "Job replies"
    assert saved["search"] == "job-replies"

    owner_page.reload()
    field = settings_field(owner_page)
    expect(field.get_by_label("Gmail label for replies")).to_have_value("Job replies")


def test_an_empty_label_turns_labelling_off_and_stays_off(owner_page, base_url):
    field = settings_field(owner_page)
    box = field.get_by_label("Gmail label for replies")
    box.fill("")
    box.blur()
    expect(field.locator(".gmail-label-status")).to_have_text("Labelling is off.")
    assert saved_setting(owner_page, base_url)["value"] == ""

    owner_page.reload()
    field = settings_field(owner_page)
    expect(field.get_by_label("Gmail label for replies")).to_have_value("")
    # The placeholder says an empty field means off and names the usual label, so it is not mistaken for a saved value.
    expect(field.get_by_label("Gmail label for replies")).to_have_attribute(
        "placeholder", "Empty keeps labelling off; the usual name is opportunities")


@pytest.mark.allow_page_errors  # the refusal is a 422 by design
def test_a_name_gmail_reserves_is_refused_in_words_and_not_saved(owner_page, base_url):
    field = settings_field(owner_page)
    box = field.get_by_label("Gmail label for replies")
    box.fill("INBOX")
    box.press("Enter")
    status = field.locator(".gmail-label-status")
    expect(status).to_have_class(re.compile(r"\bform-error\b"))
    expect(status).to_have_text("Gmail keeps the name INBOX for itself; choose another label name")
    expect(box).to_be_enabled()
    # Saving with Enter must leave the student in the field, told (to a screen reader too) what is wrong with it.
    expect(box).to_be_focused()
    expect(box).to_have_attribute("aria-invalid", "true")
    expect(box).to_have_attribute("aria-describedby", "settings-gmail-label-status")
    assert saved_setting(owner_page, base_url)["value"] == "opportunities"


def test_the_mailbox_line_says_when_gmail_is_not_connected(owner_page):
    patch_setting(owner_page, connected=False)
    field = settings_field(owner_page)
    expect(field.locator(".gmail-label-mailbox")).to_have_text("Pipeline mailbox: not connected")
    expect(field.locator(".gmail-label-permission")).to_have_text("")
    owner_page.unroute_all(behavior="ignoreErrors")


def test_the_mailbox_line_names_the_account_gmail_is_connected_as(owner_page):
    patch_setting(owner_page, connected=True, connected_as=COMPOSE_ACCOUNT, permission=True)
    field = settings_field(owner_page)
    expect(field.locator(".gmail-label-mailbox")).to_have_text(f"Pipeline mailbox: {COMPOSE_ACCOUNT}")
    expect(field.locator(".gmail-label-mailbox")).not_to_have_class(re.compile(r"\bform-error\b"))
    expect(field.locator(".gmail-label-permission")).to_have_text("")
    owner_page.unroute_all(behavior="ignoreErrors")


def test_the_mailbox_line_admits_it_does_not_know_the_account_yet(owner_page):
    patch_setting(owner_page, connected=True, connected_as="", permission=True)
    field = settings_field(owner_page)
    expect(field.locator(".gmail-label-mailbox")).to_have_text("Pipeline mailbox: checking which account Gmail is connected as…")
    owner_page.unroute_all(behavior="ignoreErrors")


def test_the_mailbox_line_warns_when_gmail_is_a_different_account(owner_page):
    patch_setting(owner_page, connected=True, connected_as=OTHER_ACCOUNT, permission=True)
    field = settings_field(owner_page)
    line = field.locator(".gmail-label-mailbox")
    expect(line).to_have_class(re.compile(r"\bform-error\b"))
    expect(line).to_contain_text(f"Pipeline mailbox: {OTHER_ACCOUNT}")
    expect(line).to_contain_text(f"Your outreach address is {COMPOSE_ACCOUNT}")
    owner_page.unroute_all(behavior="ignoreErrors")


def test_the_field_says_labelling_waits_for_the_permission_only_when_it_is_on_and_connected(owner_page):
    patch_setting(owner_page, connected=True, connected_as=COMPOSE_ACCOUNT, permission=False)
    field = settings_field(owner_page)
    expect(field.locator(".gmail-label-permission")).to_have_text("Labelling waits until you reconnect Gmail on the Outreach tab.")
    owner_page.unroute_all(behavior="ignoreErrors")

    # Off, nothing waits; not connected, the mailbox line already says so.
    patch_setting(owner_page, value="", connected=True, connected_as=COMPOSE_ACCOUNT, permission=False)
    field = settings_field(owner_page)
    expect(field.locator(".gmail-label-permission")).to_have_text("")
    owner_page.unroute_all(behavior="ignoreErrors")
    patch_setting(owner_page, connected=False, permission=False)
    field = settings_field(owner_page)
    expect(field.locator(".gmail-label-permission")).to_have_text("")
    owner_page.unroute_all(behavior="ignoreErrors")


def test_reconnect_gmail_asks_for_the_label_permission_once(owner_page):
    gmail_listing(owner_page, bounce_check=True, label="opportunities", label_check=False)
    open_outreach(owner_page)
    panel = owner_page.locator(".outreach-gmail-connect")
    expect(panel.locator(".profile-help")).to_contain_text("add your “opportunities” label to every thread where someone at a company replied")
    expect(panel.locator(".profile-help")).to_contain_text("“Read, compose, and send emails”")
    expect(panel.locator(".profile-help")).to_contain_text("it never deletes, archives, moves or marks mail as read")
    expect(panel.locator(".profile-help")).to_contain_text("leave the label empty in Outreach settings")
    expect(panel.get_by_role("button", name="Reconnect Gmail")).to_be_visible()
    owner_page.unroute_all(behavior="ignoreErrors")


def test_reconnect_gmail_names_both_permissions_when_the_label_is_on_and_only_one_when_it_is_off(owner_page):
    gmail_listing(owner_page, bounce_check=False, label="opportunities", label_check=False)
    open_outreach(owner_page)
    text = owner_page.locator(".outreach-gmail-connect .profile-help")
    expect(text).to_contain_text("It asks for permission to read mail;")
    expect(text).to_contain_text("“Read, compose, and send emails”")
    expect(text).to_contain_text("add your “opportunities” label to threads where a company replied")
    expect(text).to_contain_text("tick both boxes")
    expect(text).not_to_contain_text("one more permission")
    owner_page.unroute_all(behavior="ignoreErrors")

    gmail_listing(owner_page, bounce_check=False, label="", label_check=False)
    open_outreach(owner_page)
    text = owner_page.locator(".outreach-gmail-connect .profile-help")
    expect(text).to_contain_text("It asks for permission to read mail;")
    expect(text).not_to_contain_text("Read, compose, and send emails")
    expect(text).not_to_contain_text("tick both boxes")
    owner_page.unroute_all(behavior="ignoreErrors")


def test_the_connect_panel_names_the_students_own_label(owner_page):
    gmail_listing(owner_page, bounce_check=True, label="Job replies", label_check=False)
    open_outreach(owner_page)
    expect(owner_page.locator(".outreach-gmail-connect .profile-help")).to_contain_text("add your “Job replies” label")
    owner_page.unroute_all(behavior="ignoreErrors")


def test_no_label_reconnect_when_the_permission_is_there_or_labelling_is_off(owner_page):
    gmail_listing(owner_page, bounce_check=True, label="opportunities", label_check=True)
    open_outreach(owner_page)
    expect(owner_page.locator(".outreach-gmail-connect")).to_have_count(0)
    owner_page.unroute_all(behavior="ignoreErrors")

    gmail_listing(owner_page, bounce_check=True, label="", label_check=False)
    open_outreach(owner_page)
    expect(owner_page.locator(".outreach-gmail-connect")).to_have_count(0)
    owner_page.unroute_all(behavior="ignoreErrors")


def test_a_wrong_account_connection_is_offered_a_reconnect_that_names_both_addresses(owner_page):
    # Even with every permission, and with the label permission missing too: the account is the first problem.
    gmail_listing(owner_page, bounce_check=True, label="opportunities", label_check=False,
                  connected_as=OTHER_ACCOUNT, wrong_account=True)
    open_outreach(owner_page)
    panel = owner_page.locator(".outreach-gmail-connect")
    expect(panel.locator(".profile-help")).to_have_text(
        f"Gmail is connected as {OTHER_ACCOUNT}, but your outreach address is {COMPOSE_ACCOUNT}. "
        f"Reconnect Gmail and choose {COMPOSE_ACCOUNT}.")
    expect(panel.get_by_role("button", name="Reconnect Gmail")).to_be_visible()
    owner_page.unroute_all(behavior="ignoreErrors")
