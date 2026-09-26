"""The cold outreach pipeline, driven through the real Outreach tab.

Every external reach (the draft model, company websites, DNS, the deep search)
is answered by tests/ui/outreach_fakes.py, so these tests exercise the product's
own rendering, event handlers, and approval gating end to end, offline.
"""

from __future__ import annotations

import re
from contextlib import closing
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from conftest import OWNER_TOKEN
from outreach_fakes import COMPOSE_ACCOUNT
from opportunity_app.schema import connect_product

BEARER = {"Authorization": f"Bearer {OWNER_TOKEN}"}


def seed_target(page, base_url, **overrides):
    body = {
        "company": "Bovi",
        "channel": "Local accelerators",
        "priority": "P1",
        "website": "https://bovi.example",
        "summary": "Dairy robotics for small farms",
        "source_urls": ["https://bovi.example/"],
        **overrides,
    }
    response = page.request.post(f"{base_url}/api/v1/outreach", headers=BEARER, data=body)
    assert response.status == 201, response.text()
    return response.json()


def wait_for_results(page):
    page.wait_for_function("() => document.getElementById('results')?.getAttribute('aria-busy') === 'false'")


def open_tab(page, subtab):
    """Pick an outreach subtab from the rail, e.g. "awaiting" or "deep-search"."""
    button = page.locator(f'#subnav [data-subtab="{subtab}"]')
    button.click()
    expect(button).to_have_attribute("aria-current", "true")
    wait_for_results(page)


def open_outreach(page, subtab=None):
    page.click("#outreach-nav")
    expect(page.locator("#outreach-nav")).to_have_class("nav-item is-active")
    wait_for_results(page)
    if subtab:
        open_tab(page, subtab)


def row_for(page, company):
    name = page.locator(".outreach-row-company", has_text=re.compile(f"^{re.escape(company)}$"))
    return page.locator(".outreach-row", has=name)


def card_for(page, company):
    """The split view's pane for a company, picking its row in the list first."""
    row = row_for(page, company)
    if row.count() and row.get_attribute("aria-pressed") != "true":
        row.click()
    return page.locator(".outreach-pane", has=page.get_by_role("heading", name=company, exact=True))


def open_details(card, tab=None):
    """The pane's form area, switched to one of its tabs when named."""
    if tab:
        card.get_by_role("tab", name=tab, exact=True).click()
    return card


def test_find_a_contact_draft_approve_and_hand_off_to_gmail(owner_page, base_url):
    seed_target(owner_page, base_url)
    open_outreach(owner_page)
    card = card_for(owner_page, "Bovi")
    expect(card).to_contain_text("No contact yet")
    details = open_details(card)

    # Contacts come from the company's own site, each with its evidence.
    details.get_by_role("button", name="Find contacts").click()
    candidates = details.locator(".outreach-candidate")
    expect(details.locator(".outreach-contacts .form-status")).to_contain_text("Found")
    jane = candidates.filter(has_text="jane@bovi.example")
    expect(jane).to_contain_text("Published on their site")
    expect(jane).to_contain_text("Contact confirmed")
    expect(candidates.filter(has_text="sam@bovi.example")).to_contain_text("Contact unverified")
    jane.get_by_role("button", name="Use jane@bovi.example as the contact").click()

    card = card_for(owner_page, "Bovi")
    expect(card.locator(".application-facts").first).to_contain_text("Jane Doe, Co-Founder & CTO")
    expect(card.locator(".application-facts").first).to_contain_text("Contact confirmed")

    # A generated draft waits for review; nothing opens an email yet.
    details = open_details(card)
    details.get_by_role("button", name="Generate draft").click()
    card = card_for(owner_page, "Bovi")
    expect(card).to_contain_text("Draft needs review")
    expect(card.get_by_role("button", name="Copy draft")).to_have_count(0)
    expect(card.get_by_role("link", name=re.compile("Open in Gmail"))).to_have_count(0)
    details = open_details(card)
    expect(details.locator('textarea[name="email_body"]')).to_have_value(re.compile(r"^Hi Jane,"))
    claims = details.locator(".outreach-claims")
    assert claims.evaluate("node => node.firstElementChild === node.querySelector(':scope > summary')")
    claims.locator(":scope > summary").click()
    expect(claims.locator(":scope > .outreach-note")).to_be_visible()
    expect(claims.locator(":scope > .outreach-note")).to_have_text("The model's own citations, not checked sentence by sentence.")
    expect(claims).to_contain_text("your profile: name")
    expect(details.locator('.outreach-draft-assistant[data-draft-kind="initial"]')).to_contain_text("Written by Anthropic")
    expect(details.locator('.outreach-draft-assistant[data-draft-kind="initial"]')).to_contain_text("inference, not from your profile or research")

    details.get_by_role("button", name="Approve draft").click()
    card = card_for(owner_page, "Bovi")
    expect(card).to_contain_text("Draft approved")
    expect(card.get_by_role("button", name="Copy draft")).to_be_visible()
    compose = card.get_by_role("link", name=f"Open in Gmail ({COMPOSE_ACCOUNT}) ↗")
    expect(compose).to_be_visible()
    href = compose.get_attribute("href")
    parts = urlsplit(href)
    query = parse_qs(parts.query)
    assert parts.netloc == "mail.google.com"
    assert query["authuser"] == [COMPOSE_ACCOUNT]
    assert query["to"] == ["jane@bovi.example"]
    assert query["su"] == ["Internship question for Bovi"]
    assert query["body"][0].startswith("Hi Jane,")
    expect(compose).to_have_attribute("target", "_blank")


def test_connected_gmail_creates_the_draft_with_the_attachment(owner_page, base_url):
    """With Gmail connected, the button asks the server for a Gmail draft and opens it.

    The live server has no Google client configured, so the listing is patched to
    report a connection and the draft call is answered here; the server side is
    covered by tests/test_outreach_gmail.py.
    """
    target = seed_target(
        owner_page, base_url, contact_email="jane@bovi.example", contact_name="Jane Doe",
        email_subject="Internship question", email_body="Hi Jane,\n\nWould you be open to a call?\n\nTest Student",
    )
    approved = owner_page.request.post(
        f"{base_url}/api/v1/outreach/{target['id']}/approve", headers=BEARER,
        data={"fingerprint": target["draft_fingerprint"]},
    )
    assert approved.ok, approved.text()
    gmail ={"configured": True, "connected": True, "needs_reconnect": False, "account": COMPOSE_ACCOUNT,
             "attachment": "resume.pdf", "attachment_problem": ""}

    def listing(route):
        response = route.fetch()
        route.fulfill(response=response, json={**response.json(), "gmail_drafts": gmail})

    draft_requests = []

    def create_draft(route):
        draft_requests.append(route.request.post_data_json)
        route.fulfill(json={"draft_id": "r-1", "message_id": "18c1", "attachment": "resume.pdf", "reused": False,
                            "url": f"https://mail.google.com/mail/?authuser={COMPOSE_ACCOUNT}#drafts?compose=18c1"})

    owner_page.route(re.compile(r".*/api/v1/outreach(\?.*)?$"), listing)
    owner_page.route(f"**/api/v1/outreach/{target['id']}/gmail-draft", create_draft)
    owner_page.context.route("https://mail.google.com/**", lambda route: route.fulfill(body="gmail"))
    open_outreach(owner_page)
    card = card_for(owner_page, "Bovi")
    expect(card.get_by_role("link", name=re.compile("Open in Gmail"))).to_have_count(0)
    button = card.get_by_role("button", name="Open in Gmail with resume.pdf ↗")
    with owner_page.context.expect_page() as popup:
        button.click()
    popup.value.wait_for_url(re.compile(r"^https://mail\.google\.com/"))
    assert popup.value.url.endswith("#drafts?compose=18c1")
    assert draft_requests == [{"kind": "initial"}]
    expect(owner_page.locator("#action-status")).to_contain_text("Created the Gmail draft for Bovi with resume.pdf")


def test_send_from_gmail_asks_for_a_second_click_naming_the_recipient(owner_page, base_url):
    """A sent email cannot be taken back, so the first click on Send only asks.

    The route is mocked; the server's send path is covered by tests/test_outreach_gmail.py.
    """
    target = seed_target(
        owner_page, base_url, contact_email="jane@bovi.example", contact_name="Jane Doe",
        email_subject="Internship question", email_body="Hi Jane,\n\nWould you be open to a call?\n\nTest Student",
    )
    approved = owner_page.request.post(
        f"{base_url}/api/v1/outreach/{target['id']}/approve", headers=BEARER,
        data={"fingerprint": target["draft_fingerprint"]},
    )
    assert approved.ok, approved.text()
    gmail = {"configured": True, "connected": True, "needs_reconnect": False, "account": COMPOSE_ACCOUNT,
             "attachment": "resume.pdf", "attachment_problem": ""}

    def listing(route):
        response = route.fetch()
        route.fulfill(response=response, json={**response.json(), "gmail_drafts": gmail})

    send_requests = []

    def send(route):
        send_requests.append(route.request.post_data_json)
        route.fulfill(json={"kind": "initial", "to": "jane@bovi.example", "cc": "", "account": COMPOSE_ACCOUNT,
                            "attachment": "resume.pdf", "status": "sent", "follow_up_at": "2026-10-02",
                            "message_id": "sent-1", "thread_id": "thread-1", "fingerprint": target["draft_fingerprint"]})

    owner_page.route(re.compile(r".*/api/v1/outreach(\?.*)?$"), listing)
    owner_page.route(f"**/api/v1/outreach/{target['id']}/gmail-send", send)
    open_outreach(owner_page)
    card = card_for(owner_page, "Bovi")
    expect(card.get_by_role("button", name="Open in Gmail with resume.pdf ↗")).to_be_visible()
    button = card.locator("button.outreach-send")
    expect(button).to_have_text("Send with resume.pdf")
    button.click()
    expect(button).to_have_text("Send to jane@bovi.example?")
    assert send_requests == [], "the first click only asks"

    owner_page.keyboard.press("Escape")
    expect(button).to_have_text("Send with resume.pdf")
    button.click()
    button.click()
    expect(owner_page.locator("#action-status")).to_contain_text("Sent to jane@bovi.example")
    assert send_requests == [{"kind": "initial", "fingerprint": target["draft_fingerprint"]}]
    assert len(owner_page.context.pages) == 1, "no Gmail tab opens"


@pytest.mark.allow_page_errors  # the 428 and 502 answers are the point of the test
def test_send_that_gmail_may_already_have_asks_for_a_look_and_vouches_once(owner_page, base_url):
    """A 428 names what to check in Gmail; the next confirmed click sends that check, and only that click.

    The route is mocked; the server's side is covered by tests/test_outreach_gmail.py.
    """
    target = seed_target(
        owner_page, base_url, contact_email="jane@bovi.example", contact_name="Jane Doe",
        email_subject="Internship question", email_body="Hi Jane,\n\nWould you be open to a call?\n\nTest Student",
    )
    approved = owner_page.request.post(
        f"{base_url}/api/v1/outreach/{target['id']}/approve", headers=BEARER,
        data={"fingerprint": target["draft_fingerprint"]},
    )
    assert approved.ok, approved.text()
    gmail = {"configured": True, "connected": True, "needs_reconnect": False, "account": COMPOSE_ACCOUNT,
             "attachment": "resume.pdf", "attachment_problem": ""}

    def listing(route):
        response = route.fetch()
        route.fulfill(response=response, json={**response.json(), "gmail_drafts": gmail})

    check = "a" * 32
    send_requests = []

    def send(route):
        send_requests.append(route.request.post_data_json)
        route.fulfill(status=428, json={"detail": {
            "msg": "Gmail may already have sent this email. Check your Gmail Sent folder: if it went out, "
                   "use \"I sent it\"; if not, press Send again.",
            "check": check,
        }})

    owner_page.route(re.compile(r".*/api/v1/outreach(\?.*)?$"), listing)
    owner_page.route(f"**/api/v1/outreach/{target['id']}/gmail-send", send)
    open_outreach(owner_page)
    card = card_for(owner_page, "Bovi")
    button = card.locator("button.outreach-send")
    button.click()
    button.click()
    expect(owner_page.locator("#error-banner")).to_contain_text("Check your Gmail Sent folder")
    expect(button).to_have_text("Checked Gmail — send again with resume.pdf")
    assert send_requests == [{"kind": "initial", "fingerprint": target["draft_fingerprint"]}]

    button.click()
    expect(button).to_have_text("Send to jane@bovi.example?")
    assert len(send_requests) == 1, "the checked send still asks for the second click"
    button.click()
    expect(button).to_have_text("Checked Gmail — send again with resume.pdf")
    assert send_requests[1] == {"kind": "initial", "fingerprint": target["draft_fingerprint"], "sent_folder_check": check}

    # The check was used by that attempt; the relabel above came from the new 428.
    owner_page.unroute(f"**/api/v1/outreach/{target['id']}/gmail-send")
    owner_page.route(f"**/api/v1/outreach/{target['id']}/gmail-send", lambda route: (
        send_requests.append(route.request.post_data_json), route.fulfill(status=502, json={"detail": "Gmail did not answer"})))
    button.click()
    button.click()
    expect(button).to_have_text("Send with resume.pdf")
    assert send_requests[2] == {"kind": "initial", "fingerprint": target["draft_fingerprint"], "sent_folder_check": check}
    button.click()
    button.click()
    expect(owner_page.locator("#error-banner")).to_contain_text("Gmail did not answer")
    assert len(send_requests) == 4
    assert "sent_folder_check" not in send_requests[3], "a check vouches for one attempt only"


def test_unsaved_edits_stop_every_hand_off_of_the_approved_draft(owner_page, base_url):
    """Gmail, the compose link, and Copy all send the saved draft, so unsaved text must stop them."""
    target = seed_target(
        owner_page, base_url, contact_email="jane@bovi.example", contact_name="Jane Doe",
        email_subject="Internship question", email_body="Hi Jane,\n\nWould you be open to a call?\n\nTest Student",
    )
    approved = owner_page.request.post(
        f"{base_url}/api/v1/outreach/{target['id']}/approve", headers=BEARER,
        data={"fingerprint": target["draft_fingerprint"]},
    )
    assert approved.ok, approved.text()
    gmail = {"configured": True, "connected": True, "needs_reconnect": False, "account": COMPOSE_ACCOUNT,
             "attachment": "resume.pdf", "attachment_problem": ""}

    def listing(route):
        response = route.fetch()
        route.fulfill(response=response, json={**response.json(), "gmail_drafts": gmail})

    draft_requests = []
    owner_page.route(re.compile(r".*/api/v1/outreach(\?.*)?$"), listing)
    owner_page.route(f"**/api/v1/outreach/{target['id']}/gmail-draft", lambda route: draft_requests.append(route.request) or route.abort())
    owner_page.route(f"**/api/v1/outreach/{target['id']}/gmail-send", lambda route: draft_requests.append(route.request) or route.abort())
    open_outreach(owner_page)
    card = card_for(owner_page, "Bovi")
    details = open_details(card)
    details.locator('textarea[name="email_body"]').fill("Hi Jane,\n\nA different draft I pasted in.\n\nTest Student")

    card.get_by_role("button", name="Open in Gmail with resume.pdf ↗").click()
    expect(owner_page.locator("#error-banner")).to_contain_text("unsaved edits")
    assert draft_requests == [], "no Gmail draft is made from the stale approved text"

    send = card.locator("button.outreach-send")
    send.click()
    send.click()
    expect(send).to_have_text("Send with resume.pdf")
    assert draft_requests == [], "nothing is sent from the stale approved text"
    assert len(owner_page.context.pages) == 1, "no Gmail tab opens"

    owner_page.evaluate("() => { window.__copied = null; navigator.clipboard.writeText = async (text) => { window.__copied = text; }; }")
    card.get_by_role("button", name="Copy draft").click()
    assert owner_page.evaluate("() => window.__copied") is None


def test_unsaved_edits_stop_the_compose_link(owner_page, base_url):
    target = seed_target(
        owner_page, base_url, contact_email="jane@bovi.example",
        email_subject="Internship question", email_body="Hi Jane,\n\nWould you be open to a call?\n\nTest Student",
    )
    owner_page.request.post(
        f"{base_url}/api/v1/outreach/{target['id']}/approve", headers=BEARER,
        data={"fingerprint": target["draft_fingerprint"]},
    )
    owner_page.context.route("https://mail.google.com/**", lambda route: route.fulfill(body="gmail"))
    open_outreach(owner_page)
    card = card_for(owner_page, "Bovi")
    open_details(card).locator('input[name="email_subject"]').fill("A new subject")
    card.get_by_role("link", name=re.compile("Open in Gmail")).click()
    expect(owner_page.locator("#error-banner")).to_contain_text("unsaved edits")
    assert len(owner_page.context.pages) == 1


def test_editing_an_approved_draft_withdraws_the_approval(owner_page, base_url):
    target = seed_target(
        owner_page, base_url, contact_email="jane@bovi.example", contact_name="Jane Doe",
        email_subject="Internship question", email_body="Hi Jane,\n\nWould you be open to a call?\n\nTest Student",
    )
    approved = owner_page.request.post(
        f"{base_url}/api/v1/outreach/{target['id']}/approve", headers=BEARER,
        data={"fingerprint": target["draft_fingerprint"]},
    )
    assert approved.ok, approved.text()
    open_outreach(owner_page)
    card = card_for(owner_page, "Bovi")
    expect(card.get_by_role("link", name=re.compile("Open in Gmail"))).to_be_visible()

    details = open_details(card)
    body = details.locator('textarea[name="email_body"]')
    body.fill("Hi Jane,\n\nWould you be open to a quick call this month?\n\nTest Student")
    details.get_by_role("button", name="Save changes").click()

    card = card_for(owner_page, "Bovi")
    expect(card).to_contain_text("Draft needs review")
    expect(card.get_by_role("link", name=re.compile("Open in Gmail"))).to_have_count(0)
    details = open_details(card)
    expect(details.locator(".outreach-timeline")).to_contain_text("Approval withdrawn")


def test_regenerate_with_comments_then_step_back_to_the_earlier_draft(owner_page, base_url):
    from test_accessibility import _assert_accessible

    seed_target(owner_page, base_url, contact_email="jane@bovi.example", contact_name="Jane Doe")
    open_outreach(owner_page)
    details = open_details(card_for(owner_page, "Bovi"))
    expect(details.get_by_label("Comments for the next draft")).to_have_count(0)
    details.get_by_role("button", name="Generate draft").click()

    details = open_details(card_for(owner_page, "Bovi"))
    body = details.locator('textarea[name="email_body"]')
    expect(body).to_have_value(re.compile("is why I am writing"))
    expect(details.locator(".outreach-draft-history")).to_have_count(0)
    details.get_by_label("Comments for the next draft").fill("Make it shorter.")
    details.get_by_role("button", name="Regenerate draft").click()

    details = open_details(card_for(owner_page, "Bovi"))
    body = details.locator('textarea[name="email_body"]')
    expect(body).not_to_have_value(re.compile("is why I am writing"))
    expect(details.get_by_label("Comments for the next draft")).to_have_value("")
    history = details.locator(".outreach-draft-history")
    history.locator(":scope > summary").click()
    expect(history.locator(":scope > summary")).to_have_text("Earlier drafts (1)")
    expect(history).to_contain_text("Draft 1 of 2")
    expect(history.locator(".outreach-draft-version-body")).to_contain_text("is why I am writing")
    expect(history.get_by_role("button", name="‹ Older")).to_be_disabled()

    history.get_by_role("button", name="Newer ›").click()
    expect(history).to_contain_text("Draft 2 of 2 · in the editor now")
    expect(history).to_contain_text("Asked for: Make it shorter.")
    expect(history.get_by_role("button", name="Use this draft")).to_be_hidden()
    expect(history.get_by_role("button", name="‹ Older")).to_be_focused()
    _assert_accessible(owner_page, "the earlier drafts viewer")

    owner_page.keyboard.press("Enter")
    history.get_by_role("button", name="Use this draft").click()
    card = card_for(owner_page, "Bovi")
    expect(card).to_contain_text("Draft needs review")
    details = open_details(card)
    expect(details.locator('textarea[name="email_body"]')).to_have_value(re.compile("is why I am writing"))
    expect(details.locator(".outreach-draft-history > summary")).to_have_text("Earlier drafts (1)")
    expect(details.locator(".outreach-timeline")).to_contain_text("Earlier draft restored")


def test_approving_saves_the_words_it_approves(owner_page, base_url):
    """Approve is one press. It saves what is in the box, then approves exactly that."""
    target = seed_target(owner_page, base_url, contact_email="jane@bovi.example", email_subject="Hi", email_body="Hi Jane, hello")
    open_outreach(owner_page)
    details = open_details(card_for(owner_page, "Bovi"))
    details.locator('textarea[name="email_body"]').fill("Hi Jane, something else")
    details.get_by_role("button", name="Approve draft").click()

    card = card_for(owner_page, "Bovi")
    expect(card).to_contain_text("Draft approved")
    details = open_details(card)
    expect(details.locator('textarea[name="email_body"]')).to_have_value("Hi Jane, something else")
    expect(details.locator(".outreach-timeline")).to_contain_text("Draft edited")
    stored = owner_page.request.get(f"{base_url}/api/v1/outreach/{target['id']}", headers=BEARER).json()
    assert stored["email_body"] == "Hi Jane, something else", "the approved draft is the edited one"
    assert stored["draft_status"] == "approved"


def test_regenerating_over_unsaved_edits_shows_the_new_draft(owner_page, base_url):
    """The other half of the promise: once the student agrees to replace their words, they are gone."""
    seed_target(owner_page, base_url, contact_name="Jane Doe", contact_email="jane@bovi.example",
                email_subject="Hi", email_body="Hi Jane, hello")
    open_outreach(owner_page)
    details = open_details(card_for(owner_page, "Bovi"))
    details.locator('textarea[name="email_body"]').fill("Hi Jane, half a thought")

    owner_page.once("dialog", lambda dialog: dialog.accept())
    details.get_by_role("button", name="Regenerate draft").click()
    body = open_details(card_for(owner_page, "Bovi")).locator('textarea[name="email_body"]')
    expect(body).to_have_value(re.compile("15 minute call"))
    expect(body).not_to_have_value("Hi Jane, half a thought")


@pytest.mark.allow_page_errors  # the refused approval is a 422 by design
def test_declining_the_warnings_still_says_where_the_edits_went(owner_page, base_url, live_server):
    """Approve saves before it asks. Backing out of the warnings has to say the words are saved."""
    target = seed_target(owner_page, base_url, contact_email="jane@bovi.example", email_subject="Hi", email_body="Hi Jane, hello")
    with closing(connect_product(live_server.live_path)) as conn:
        conn.execute("UPDATE outreach_targets SET research_confidence='unverified' WHERE id=?", (target["id"],))
        conn.commit()
    open_outreach(owner_page)
    details = open_details(card_for(owner_page, "Bovi"))
    details.locator('textarea[name="email_body"]').fill("Hi Jane, something else")

    owner_page.once("dialog", lambda dialog: dialog.dismiss())
    details.get_by_role("button", name="Approve draft").click()
    expect(details.locator(".outreach-draft-assistant .form-status")).to_have_text("Your edits are saved. The draft is not approved.")
    stored = owner_page.request.get(f"{base_url}/api/v1/outreach/{target['id']}", headers=BEARER).json()
    assert stored["email_body"] == "Hi Jane, something else"
    assert stored["draft_status"] == "generated"


def test_an_action_beside_the_draft_keeps_unsaved_edits(owner_page, base_url, live_server):
    """Confirm research reloads the pane; it must not take the half-written draft with it."""
    target = seed_target(owner_page, base_url, contact_email="jane@bovi.example", email_subject="Hi", email_body="Hi Jane, hello")
    with closing(connect_product(live_server.live_path)) as conn:
        conn.execute("UPDATE outreach_targets SET research_confidence='unverified' WHERE id=?", (target["id"],))
        conn.commit()
    open_outreach(owner_page)
    card = card_for(owner_page, "Bovi")
    expect(card).to_contain_text("Research unverified")
    open_details(card).locator('textarea[name="email_body"]').fill("Hi Jane, half a thought")

    card.get_by_role("button", name="Confirm research").click()
    card = card_for(owner_page, "Bovi")
    expect(card).not_to_contain_text("Research unverified")
    details = open_details(card)
    expect(details.locator('textarea[name="email_body"]')).to_have_value("Hi Jane, half a thought")
    # Still unsaved, so the word count follows the box and the hand-off stays shut.
    expect(details.locator(".outreach-checks").first).to_contain_text("5 words")
    stored = owner_page.request.get(f"{base_url}/api/v1/outreach/{target['id']}", headers=BEARER).json()
    assert stored["email_body"] == "Hi Jane, hello", "nothing is saved behind the student's back"


def test_marking_sent_then_logging_a_reply_moves_the_status(owner_page, base_url):
    target = seed_target(
        owner_page, base_url, contact_email="jane@bovi.example", contact_name="Jane Doe",
        email_subject="Internship question", email_body="Hi Jane,\n\nWould you be open to a call?\n\nTest Student",
    )
    owner_page.request.post(
        f"{base_url}/api/v1/outreach/{target['id']}/approve", headers=BEARER,
        data={"fingerprint": target["draft_fingerprint"]},
    )
    open_outreach(owner_page)
    card_for(owner_page, "Bovi").get_by_role("button", name="I sent it").click()

    card = card_for(owner_page, "Bovi")
    expect(card.locator(".application-heading")).to_contain_text("Sent")
    expect(card).to_contain_text(re.compile(r"Follow up \w{3} \d{1,2}"))
    expect(card.get_by_role("button", name="I sent it")).to_have_count(0)

    details = open_details(card)
    expect(details.locator("legend", has_text="Follow-up draft")).to_be_visible()
    details = open_details(card, "Replies and history")
    details.get_by_label("Paste their reply").fill("Thanks for reaching out! Could we set up a call next week?")
    details.get_by_role("button", name="Log reply").click()
    expect(details.locator(".outreach-reply-result")).to_contain_text("proposes a call")
    details.get_by_role("button", name="Mark Call scheduled").click()

    card = card_for(owner_page, "Bovi")
    expect(card.locator(".application-heading")).to_contain_text("Call scheduled")
    expect(card).not_to_contain_text("Follow-up due")


def test_a_follow_up_is_drafted_approved_and_opened_as_a_reply(owner_page, base_url):
    target = seed_target(
        owner_page, base_url, contact_email="jane@bovi.example", contact_name="Jane Doe", status="sent",
        email_subject="Internship question", email_body="Hi Jane,\n\nWould you be open to a call?\n\nTest Student",
    )
    assert target["follow_up_at"]
    open_outreach(owner_page, "awaiting")
    details = open_details(card_for(owner_page, "Bovi"))
    details.get_by_role("button", name="Generate follow-up").click()
    card = card_for(owner_page, "Bovi")
    expect(card).to_contain_text("Follow-up needs review")
    follow_details = open_details(card)
    expect(follow_details.locator('.outreach-draft-assistant[data-draft-kind="follow_up"]')).to_contain_text("Written by Anthropic")
    expect(card.get_by_role("button", name="Copy follow-up")).to_have_count(0)
    follow_details.get_by_role("button", name="Approve follow-up").click()

    card = card_for(owner_page, "Bovi")
    expect(card.get_by_role("button", name="Copy follow-up")).to_be_visible()
    compose = card.get_by_role("link", name=f"Open follow-up in Gmail ({COMPOSE_ACCOUNT}) ↗")
    query = parse_qs(urlsplit(compose.get_attribute("href")).query)
    assert query["su"] == ["Re: Internship question"]
    card.get_by_role("button", name="I sent the follow-up").click()
    followed = card_for(owner_page, "Bovi")
    expect(followed.locator(".application-heading")).to_contain_text("Followed up")
    expect(followed).to_contain_text(re.compile(r"Followed up \w{3} \d{1,2}"))
    expect(followed).not_to_contain_text("Follow-up due")


def test_the_deep_search_adds_a_verified_company_with_its_published_contact(owner_page):
    open_outreach(owner_page, "deep-search")
    panel = owner_page.locator("details.outreach-deep-search")
    expect(panel.locator("summary")).to_have_text("Deep search: not run yet")
    expect(panel.get_by_role("checkbox", name="US startups in your field")).to_be_checked()
    panel.get_by_role("button", name="Run deep search now").click()

    # Watching the search lands on what it added once it finishes.
    card = card_for(owner_page, "Kestrel Robotics")
    expect(card).to_be_visible(timeout=20_000)
    expect(card).to_contain_text("From deep search")
    # Where it is based comes from its own site, linked, and the filing is shown with its amount.
    location = card.locator(".outreach-location")
    expect(location).to_contain_text("Based in San Carlos, CA")
    expect(location.get_by_role("link", name="the company's site ↗")).to_have_attribute(
        "href", "https://kestrel-robotics.example/about"
    )
    expect(card.locator(".outreach-form-d")).to_contain_text("SEC Form D: $3.2M sold of $5.0M offered")
    expect(card).to_contain_text("Research unverified")
    expect(card).to_contain_text("Summarized by the deep search")
    expect(card).to_contain_text("US startups")
    # The site names Rita Moreno and publishes only a shared inbox, so the draft
    # goes to a guessed address for her with careers@ in Cc, labelled unverified.
    facts = card.locator(".application-facts").first
    expect(facts).to_contain_text("Rita Moreno")
    expect(facts).to_contain_text("Contact unverified")
    expect(facts).not_to_contain_text("Contact confirmed")
    expect(card.locator(".outreach-guess")).to_contain_text(
        "rita@kestrel-robotics.example is a guessed address, not confirmed. careers@kestrel-robotics.example is in Cc"
    )
    expect(card).to_contain_text("Draft needs review")
    expect(owner_page.locator('#subnav [data-subtab="from-search"]')).to_have_attribute("aria-current", "true")

    card.get_by_role("button", name="Confirm research").click()
    card = card_for(owner_page, "Kestrel Robotics")
    expect(card).not_to_contain_text("Research unverified")
    expect(card).not_to_contain_text("Summarized by the deep search")

    open_tab(owner_page, "to-contact")
    expect(owner_page.locator(".outreach-row")).to_have_count(1)
    open_tab(owner_page, "deep-search")
    expect(owner_page.locator("details.outreach-deep-search summary")).to_contain_text("1 company added")


def _expanded_outreach_card(page, base_url):
    target = seed_target(
        page, base_url, contact_email="jane@bovi.example", contact_name="Jane Doe", status="sent",
        email_subject="Internship question", email_body="Hi Jane,\n\nWould you be open to a call?\n\nTest Student",
    )
    page.request.post(
        f"{base_url}/api/v1/outreach/{target['id']}/approve", headers=BEARER,
        data={"fingerprint": target["draft_fingerprint"]},
    )
    open_outreach(page, "awaiting")
    details = open_details(card_for(page, "Bovi"), "Contact")
    details.get_by_role("button", name="Find contacts").click()
    expect(details.locator(".outreach-candidate").first).to_be_visible()
    return details


def test_find_people_reports_first_and_changes_only_the_ticked_contacts(owner_page, base_url):
    bovi = seed_target(owner_page, base_url, contact_email="hello@bovi.example", contact_confidence="confirmed")
    wren = seed_target(owner_page, base_url, company="Wren Motion", website="https://wren-motion.example",
                       source_urls=["https://wren-motion.example/"],
                       contact_email="hello@wren-motion.example", contact_confidence="confirmed")
    open_outreach(owner_page)
    expect(owner_page.locator('#subnav [data-subtab="find-people"]')).to_contain_text("2")
    open_tab(owner_page, "find-people")
    panel = owner_page.locator("section.outreach-recontact")
    expect(panel).to_contain_text("2 companies you have not written to yet have only a shared inbox")
    panel.get_by_role("button", name="Search now").click()

    rows = panel.locator(".outreach-recontact-row")
    expect(rows).to_have_count(2, timeout=20_000)
    confirmed = rows.filter(has_text="Bovi")
    guess = rows.filter(has_text="Wren Motion")
    expect(confirmed).to_contain_text("hello@bovi.example → jane@bovi.example")
    expect(confirmed).to_contain_text("Confirmed on their site")
    # A guess says so, and starts unticked so the student opts in.
    expect(guess).to_contain_text("Weak guess (not confirmed)")
    expect(guess.get_by_role("checkbox")).not_to_be_checked()
    expect(confirmed.get_by_role("checkbox")).to_be_checked()
    from test_accessibility import _assert_accessible

    _assert_accessible(owner_page, "the find people report")

    # Nothing changed yet: the report only reports.
    target = owner_page.request.get(f"{base_url}/api/v1/outreach/{bovi['id']}", headers=BEARER).json()
    assert target["contact_email"] == "hello@bovi.example"

    panel.get_by_role("button", name="Use ticked contacts").click()
    expect(panel).to_contain_text("Updated 1 contact", timeout=20_000)
    target = owner_page.request.get(f"{base_url}/api/v1/outreach/{bovi['id']}", headers=BEARER).json()
    assert target["contact_email"] == "jane@bovi.example"
    untouched = owner_page.request.get(f"{base_url}/api/v1/outreach/{wren['id']}", headers=BEARER).json()
    assert untouched["contact_email"] == "hello@wren-motion.example"
    expect(owner_page.locator('#subnav [data-subtab="find-people"]')).to_contain_text("1")


@pytest.mark.allow_page_errors
def test_a_failed_load_does_not_keep_the_previous_views_counts(owner_page):
    """A server without the outreach routes left "842 to review" beside the error."""
    expect(owner_page.locator("#result-count")).to_contain_text("to review")
    owner_page.route(
        re.compile(r"/api/v1/outreach(\?|$)"),
        lambda route: route.fulfill(status=404, content_type="application/json", body='{"detail": "Not Found"}'),
    )
    open_outreach(owner_page)

    expect(owner_page.locator("#error-banner")).to_have_text("Not Found")
    expect(owner_page.locator("#result-count")).to_have_text("Couldn't load this view")
    expect(owner_page.locator("#page-status")).to_have_text("")


def test_an_expanded_outreach_card_is_accessible(owner_page, base_url):
    from test_accessibility import _assert_accessible

    _expanded_outreach_card(owner_page, base_url)
    _assert_accessible(owner_page, "an expanded outreach card with contacts")
    open_tab(owner_page, "deep-search")
    _assert_accessible(owner_page, "the deep search panel")
    open_tab(owner_page, "add")
    _assert_accessible(owner_page, "the add, import, and export tools")


def test_an_expanded_outreach_card_fits_a_phone(owner_page, base_url):
    owner_page.set_viewport_size({"width": 375, "height": 812})
    _expanded_outreach_card(owner_page, base_url)
    overflow = owner_page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"the outreach view scrolls sideways by {overflow}px at 375px wide"


def test_focus_follows_the_draft_workflow_and_replies_wait_for_a_sent_email(owner_page, base_url):
    seed_target(owner_page, base_url, contact_email="jane@bovi.example", contact_name="Jane Doe")
    open_outreach(owner_page)
    details = open_details(card_for(owner_page, "Bovi"))
    expect(details.get_by_label("Paste their reply")).to_have_count(0)

    details.get_by_role("button", name="Generate draft").click()
    expect(card_for(owner_page, "Bovi").get_by_role("button", name="Approve draft")).to_be_focused()
    owner_page.keyboard.press("Enter")
    expect(card_for(owner_page, "Bovi").get_by_role("link", name=re.compile("Open in Gmail"))).to_be_focused()


@pytest.mark.allow_page_errors
def test_stale_tab_approval_reloads_and_keeps_the_changed_message(owner_page, base_url):
    seed_target(
        owner_page, base_url, contact_email="jane@bovi.example", email_subject="Hi", email_body="Reviewed body",
    )
    open_outreach(owner_page)
    first_details = open_details(card_for(owner_page, "Bovi"))
    second = owner_page.context.new_page()
    try:
        second.goto(base_url)
        open_outreach(second)
        second_details = open_details(card_for(second, "Bovi"))
        second_details.locator('textarea[name="email_body"]').fill("Changed in the other tab")
        second_details.get_by_role("button", name="Save changes").click()
        expect(second_details.locator('textarea[name="email_body"]')).to_have_value("Changed in the other tab")

        first_details.get_by_role("button", name="Approve draft").click()
        refreshed = open_details(card_for(owner_page, "Bovi"))
        expect(refreshed.locator('.outreach-draft-assistant[data-draft-kind="initial"] .form-status')).to_have_text(
            "This draft changed since you opened it. Reload and review it again."
        )
        expect(card_for(owner_page, "Bovi")).to_contain_text("Draft needs review")
    finally:
        second.close()


@pytest.mark.allow_page_errors
def test_unverified_claim_approval_dialog_can_be_dismissed(owner_page):
    open_outreach(owner_page, "deep-search")
    panel = owner_page.locator("details.outreach-deep-search")
    panel.get_by_role("button", name="Run deep search now").click()
    card = card_for(owner_page, "Kestrel Robotics")
    expect(card).to_be_visible(timeout=20_000)
    details = open_details(card)
    messages = []

    def dismiss(dialog):
        messages.append(dialog.message)
        dialog.dismiss()

    owner_page.once("dialog", dismiss)
    details.get_by_role("button", name="Approve draft").click()
    expect(details.get_by_role("button", name="Approve draft")).to_be_enabled()
    assert messages and "research is unverified" in messages[0]
    expect(card_for(owner_page, "Kestrel Robotics")).to_contain_text("Draft needs review")


def test_template_and_manual_draft_provenance_are_labeled(owner_page, base_url, live_server):
    template = seed_target(
        owner_page, base_url, company="Template Co", contact_email="x@template.example",
        email_subject="Template", email_body="Template body",
    )
    manual = seed_target(
        owner_page, base_url, company="Manual Co", contact_email="x@manual.example",
        email_subject="Manual", email_body="Manual body",
    )
    with closing(connect_product(live_server.live_path)) as conn:
        conn.execute(
            "UPDATE outreach_targets SET draft_generated_by='template', draft_claims_json='[{\"text\":\"name\",\"basis\":\"profile:name\"}]' WHERE id=?",
            (template["id"],),
        )
        conn.commit()
    open_outreach(owner_page)
    template_panel = open_details(card_for(owner_page, "Template Co")).locator('.outreach-draft-assistant[data-draft-kind="initial"]')
    expect(template_panel).to_contain_text("Deterministic template built from your saved profile and outreach record")
    expect(template_panel.locator("summary")).to_contain_text("What this draft is based on")
    expect(template_panel).not_to_contain_text(re.compile("model", re.IGNORECASE))
    manual_panel = open_details(card_for(owner_page, "Manual Co")).locator('.outreach-draft-assistant[data-draft-kind="initial"]')
    expect(manual_panel).to_contain_text("Draft origin not recorded")


def test_unknown_contact_and_no_contact_have_distinct_labels(owner_page, base_url):
    seed_target(owner_page, base_url, company="Known Unknown", contact_email="person@bovi.example", contact_confidence="unknown")
    seed_target(owner_page, base_url, company="Nobody", contact_confidence="unknown")
    open_outreach(owner_page)
    known = card_for(owner_page, "Known Unknown")
    expect(known).to_contain_text("Contact not confirmed")
    expect(known).not_to_contain_text("No contact yet")
    details = open_details(known)
    expect(details.get_by_label("Confidence")).to_have_value("unknown")
    expect(details.get_by_label("Confidence").locator("option:checked")).to_have_text("Contact not confirmed")
    expect(card_for(owner_page, "Nobody")).to_contain_text("No contact yet")


def test_unsafe_legacy_research_links_are_not_rendered(owner_page, base_url, live_server):
    target = seed_target(owner_page, base_url, website="", source_urls=[])
    with closing(connect_product(live_server.live_path)) as conn:
        conn.execute(
            "UPDATE outreach_targets SET website='javascript:alert(1)', source_urls_json='[\"http://127.0.0.1:9000\"]' WHERE id=?",
            (target["id"],),
        )
        conn.commit()
    open_outreach(owner_page)
    expect(card_for(owner_page, "Bovi").get_by_role("link", name="Research source ↗")).to_have_count(0)


def test_sent_import_style_drafts_do_not_show_as_needing_review(owner_page, base_url):
    seed_target(owner_page, base_url, company="Sent Draft", status="sent", email_subject="Hi", email_body="Body")
    seed_target(owner_page, base_url, company="Followed Draft", status="followed_up", follow_up_subject="Re", follow_up_body="Body")
    open_outreach(owner_page, "all")
    expect(card_for(owner_page, "Sent Draft")).not_to_contain_text("Draft needs review")
    expect(card_for(owner_page, "Followed Draft")).not_to_contain_text("Follow-up needs review")
    open_tab(owner_page, "needs-review")
    expect(owner_page.locator(".outreach-row")).to_have_count(0)


@pytest.mark.allow_page_errors
def test_failed_status_change_restores_the_previous_selection(owner_page, base_url):
    seed_target(owner_page, base_url)
    open_outreach(owner_page)
    card = card_for(owner_page, "Bovi")
    select = card.locator(".application-controls select")
    owner_page.route(
        re.compile(r"/api/v1/outreach/[^/]+$"),
        lambda route: route.fulfill(status=500, content_type="application/json", body='{"detail":"failed"}')
        if route.request.method == "PATCH" else route.continue_(),
    )
    select.select_option("sent")
    expect(select).to_have_value("not_started")


def test_contacted_companies_leave_the_to_contact_tab(owner_page, base_url):
    seed_target(owner_page, base_url, company="Fresh Lead")
    seed_target(owner_page, base_url, company="Already Emailed", status="sent", contact_email="a@emailed.example")
    open_outreach(owner_page)

    expect(owner_page.locator('#subnav [data-subtab="to-contact"]')).to_have_attribute("aria-current", "true")
    expect(card_for(owner_page, "Fresh Lead")).to_be_visible()
    expect(row_for(owner_page, "Already Emailed")).to_have_count(0)
    expect(owner_page.locator('#subnav [data-subtab="awaiting"] .subnav-count')).to_have_text("1")

    open_tab(owner_page, "awaiting")
    expect(card_for(owner_page, "Already Emailed")).to_be_visible()
    expect(row_for(owner_page, "Fresh Lead")).to_have_count(0)


def test_a_search_shows_each_tab_count_as_shown_of_total(owner_page, base_url):
    seed_target(owner_page, base_url, company="Drone Works", status="sent", contact_email="a@drone.example")
    seed_target(owner_page, base_url, company="Farm Bots", status="sent", contact_email="b@farm.example")
    open_outreach(owner_page, "awaiting")
    count = owner_page.locator('#subnav [data-subtab="awaiting"] .subnav-count')
    expect(count).to_have_text("2")

    owner_page.locator("#outreach-search-input").fill("drone")
    expect(count).to_have_text("1 of 2")
    expect(owner_page.locator(".outreach-row")).to_have_count(1)

    owner_page.locator("#outreach-search-input").fill("")
    expect(count).to_have_text("2")


def test_a_card_marked_sent_stays_until_you_leave_the_tab(owner_page, base_url):
    seed_target(
        owner_page, base_url, contact_email="jane@bovi.example", contact_confidence="confirmed",
        email_subject="Internship question", email_body="Hi Jane,\n\nWould you be open to a call?\n\nTest Student",
    )
    open_outreach(owner_page)
    card = card_for(owner_page, "Bovi")
    card.locator(".application-controls select").select_option("sent")

    moved = card_for(owner_page, "Bovi")
    expect(moved).to_contain_text("Now in Awaiting reply")
    open_tab(owner_page, "awaiting")
    open_tab(owner_page, "to-contact")
    expect(row_for(owner_page, "Bovi")).to_have_count(0)


def test_confirmed_emails_come_first_and_search_narrows_the_tab(owner_page, base_url):
    seed_target(owner_page, base_url, company="No Address", priority="P1")
    seed_target(owner_page, base_url, company="Guessed", priority="P1", contact_email="g@guessed.example", contact_confidence="unverified")
    seed_target(owner_page, base_url, company="Confirmed Co", priority="P3", contact_email="c@confirmed.example", contact_confidence="confirmed")
    open_outreach(owner_page)
    headings = owner_page.locator(".outreach-row-company")
    expect(headings).to_have_text(["Confirmed Co", "Guessed", "No Address"])

    owner_page.get_by_role("searchbox", name="Search outreach").fill("guessed")
    expect(headings).to_have_text(["Guessed"])
    expect(owner_page.get_by_role("searchbox", name="Search outreach")).to_be_focused()


def test_the_split_view_shows_one_company_with_its_next_step(owner_page, base_url):
    seed_target(owner_page, base_url, company="Needs Contact", website="https://needs.example")
    seed_target(
        owner_page, base_url, company="Has Draft", contact_email="jane@draft.example", contact_confidence="confirmed",
        email_subject="Hi", email_body="Hi Jane, would you be open to a call?",
    )
    open_outreach(owner_page)
    expect(owner_page.locator(".outreach-row")).to_have_count(2)
    expect(owner_page.locator(".outreach-pane")).to_have_count(1)

    # A confirmed address leads the list, so it opens first, on its draft.
    pane = owner_page.locator(".outreach-pane")
    expect(pane.get_by_role("heading", level=3)).to_have_text("Has Draft")
    expect(pane.locator(".outreach-next")).to_contain_text("Next: Review the draft")
    expect(pane.get_by_role("tab", name="Draft", exact=True)).to_have_attribute("aria-selected", "true")
    expect(pane.locator('.outreach-step[aria-current="step"]')).to_contain_text("Approve")

    # A company with no contact opens where that work happens.
    card = card_for(owner_page, "Needs Contact")
    expect(card.locator(".outreach-next")).to_contain_text("Next: Find a contact")
    expect(card.get_by_role("tab", name="Contact", exact=True)).to_have_attribute("aria-selected", "true")
    expect(card.get_by_role("button", name="Find contacts")).to_be_visible()

    card.get_by_role("tab", name="Contact", exact=True).focus()
    owner_page.keyboard.press("ArrowRight")
    expect(card.get_by_role("tab", name="Timing", exact=True)).to_be_focused()
    expect(card.get_by_role("tab", name="Timing", exact=True)).to_have_attribute("aria-selected", "true")
    expect(card.get_by_label("Confirmed deadline")).to_be_visible()


def test_switching_companies_asks_before_dropping_unsaved_edits(owner_page, base_url):
    seed_target(owner_page, base_url, company="First Co", contact_email="a@first.example", contact_confidence="confirmed")
    seed_target(owner_page, base_url, company="Second Co")
    open_outreach(owner_page)
    card = card_for(owner_page, "First Co")
    card.locator('input[name="email_subject"]').fill("Half written")

    owner_page.once("dialog", lambda dialog: dialog.dismiss())
    row_for(owner_page, "Second Co").click()
    expect(owner_page.locator(".outreach-pane h3")).to_have_text("First Co")
    expect(card.locator('input[name="email_subject"]')).to_have_value("Half written")

    owner_page.once("dialog", lambda dialog: dialog.accept())
    row_for(owner_page, "Second Co").click()
    expect(owner_page.locator(".outreach-pane h3")).to_have_text("Second Co")


def test_a_paused_company_comes_back_on_its_revisit_date(owner_page, base_url):
    seed_target(owner_page, base_url, company="Westmag", status="paused", contact_email="david@westmag.example")
    open_outreach(owner_page, "closed")
    card = card_for(owner_page, "Westmag")
    expect(card.locator(".outreach-next-text strong")).to_have_text("Next: Paused")

    # A date already past, so it is due the moment it is saved.
    card.locator(".outreach-revisit-date").fill("2026-01-05")
    expect(owner_page.locator('#subnav [data-subtab="revisits-due"] .subnav-count')).to_have_text("1")
    expect(owner_page.locator('#subnav [data-subtab="follow-ups-due"] .subnav-count')).not_to_have_text("1")

    open_tab(owner_page, "revisits-due")
    card = card_for(owner_page, "Westmag")
    expect(card.locator(".outreach-next-text strong")).to_have_text("Next: Get back in touch")
    expect(card.locator(".outreach-revisit-date")).to_have_value("2026-01-05")
    expect(card.get_by_text(re.compile(r"^Revisit due "))).to_be_visible()


def test_a_place_the_site_only_names_waits_for_the_student_to_confirm_it(owner_page, base_url):
    seed_target(
        owner_page, base_url, company="Wren Motion", website="https://wren-motion.example",
        source_urls=["https://wren-motion.example/"],
    )
    open_outreach(owner_page, "needs-location")
    card = card_for(owner_page, "Wren Motion")
    expect(card.locator(".outreach-location")).to_have_text("Location not recorded. Add it under Research.")

    # Reading the site for contacts also finds the one place it names.
    open_details(card).get_by_role("button", name="Find contacts").click()
    location = card.locator(".outreach-location")
    expect(location).to_contain_text("Based in Austin, TX")
    expect(location).to_contain_text("the only place it names, not yet checked")
    expect(location.get_by_role("link", name="the company's site \u2197")).to_have_attribute(
        "href", "https://wren-motion.example/about"
    )

    location.get_by_role("button", name="Confirm location").click()
    expect(owner_page.locator('#subnav [data-subtab="needs-location"]')).to_contain_text("0")
    open_tab(owner_page, "all")
    confirmed = card_for(owner_page, "Wren Motion").locator(".outreach-location")
    expect(confirmed).to_contain_text("Based in Austin, TX")
    expect(confirmed).not_to_contain_text("not yet checked")
    expect(confirmed.get_by_role("button", name="Confirm location")).to_have_count(0)


def test_an_imported_location_says_a_file_said_so_and_nothing_checked_it(owner_page, base_url):
    """The API suite cannot see this line: it is rendered entirely in app.js.

    An imported location has no basis, and the caveat used to live inside the
    "did we find a label for the basis" branch — so a location nobody had
    checked rendered as a bare, confirmed-looking "Based in Cedar Park, TX".
    """
    response = owner_page.request.post(
        f"{base_url}/api/v1/outreach/import",
        headers=BEARER,
        multipart={"upload": {
            "name": "targets.csv",
            "mimeType": "text/csv",
            "buffer": b"company,location,location_basis\nNova Aero,\"Cedar Park, TX\",company_site\n",
        }},
    )
    assert response.status == 200, response.text()

    open_outreach(owner_page, "needs-location")
    location = card_for(owner_page, "Nova Aero").locator(".outreach-location")
    expect(location).to_contain_text("Based in Cedar Park, TX")
    expect(location).to_contain_text("from an import file, not yet checked")
    # The file named a page, but this app never opened it, so there is nothing
    # to offer as a source.
    expect(location.get_by_role("link")).to_have_count(0)
    expect(location).not_to_contain_text("your entry")

    # Confirming sends the place on screen, not a bare flag.
    sent = []
    owner_page.on("request", lambda request: sent.append(request.post_data)
                  if request.method == "PATCH" else None)
    location.get_by_role("button", name="Confirm location").click()
    open_tab(owner_page, "all")
    confirmed = card_for(owner_page, "Nova Aero").locator(".outreach-location")
    expect(confirmed).to_contain_text("from your entry")
    expect(confirmed).not_to_contain_text("not yet checked")
    assert any("Cedar Park, TX" in (body or "") for body in sent), sent
