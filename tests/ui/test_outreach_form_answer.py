"""The card asks the student whether a contact form they sent in Finish in browser went, and only their click answers.

After the student's own press of a form's send button in Finish in browser, the app does not judge what the page
said (outreach/forms.py): the form waits as 'unconfirmed' with FORM_PRESSED_NOTE, and the card asks. Yes marks the
company sent through the page's own signed-in request (the browser session and its CSRF header, which an API test
never sends). No opens Finish in browser again, with the student's confirmation first, and is never an automatic
send. The form's row is written straight into the throwaway database, and No's request is answered here, since the
live server has no browser to open.
"""

from __future__ import annotations

import json

from playwright.sync_api import expect

from opportunity_app.outreach.targets import FORM_PRESSED_NOTE
from ui_helpers import BEARER, card_for, db, open_outreach, seed_target

QUESTION = "Did their page say your message was sent?"


def seed_asking(owner_page, live_server, base_url):
    target = seed_target(owner_page, base_url, email_subject="Internship question",
                         email_body="Hi Bovi team,\n\nWould you be open to a call?\n\nTest Student")
    put = owner_page.request.put(f"{base_url}/api/v1/outreach/{target['id']}/contact-form", headers=BEARER,
                                 data={"page_url": "https://bovi.example/contact"})
    assert put.ok, put.text()
    with db(live_server) as conn, conn:
        conn.execute("UPDATE outreach_targets SET draft_status='approved' WHERE id=?", (target["id"],))
        conn.execute("UPDATE outreach_contact_forms SET state='unconfirmed', note=? WHERE target_id=?", (FORM_PRESSED_NOTE, target["id"]))
    return target


def test_yes_marks_the_company_sent_through_the_signed_in_page(owner_page, live_server, base_url):
    target = seed_asking(owner_page, live_server, base_url)
    open_outreach(owner_page)
    card = card_for(owner_page, "Bovi")
    expect(card).to_contain_text(QUESTION)
    expect(card).to_contain_text("Answer No only if their page showed an error or nothing")
    expect(card.get_by_role("button", name="It arrived")).to_have_count(0)
    with owner_page.expect_response(lambda response: response.request.method == "PATCH" and target["id"] in response.url) as answered:
        card.get_by_role("button", name="Yes, it was sent").click()
    assert answered.value.ok, answered.value.text()
    assert answered.value.request.headers.get("x-csrf-token"), "the browser's own session, with its CSRF header"
    stored = owner_page.request.get(f"{base_url}/api/v1/outreach/{target['id']}", headers=BEARER).json()
    assert stored["status"] == "sent"


def test_no_opens_finish_in_browser_again_only_after_the_students_confirmation(owner_page, live_server, base_url):
    target = seed_asking(owner_page, live_server, base_url)
    asked = []

    def finish_in_browser(route):
        asked.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(json={"outcome": "needs_you", "note": "You closed the window before the form was sent. Nothing was sent",
                            "confirmation": "", "page_url": "https://bovi.example/contact", "filled": [], "marked": True,
                            "target": owner_page.request.get(f"{base_url}/api/v1/outreach/{target['id']}", headers=BEARER).json()})

    owner_page.route(f"**/api/v1/outreach/{target['id']}/form-submit", finish_in_browser)
    open_outreach(owner_page)
    card = card_for(owner_page, "Bovi")
    no = card.get_by_role("button", name="No, it was not sent")
    no.click()
    expect(owner_page.locator("#action-status")).to_contain_text("if it did go, pressing its send button would send it twice")
    assert asked == [], "the first press only asks"
    card.get_by_role("button", name="Open bovi.example's form again?").click()
    for _ in range(50):
        if asked:
            break
        owner_page.wait_for_timeout(100)
    assert asked and asked[0]["in_browser"] is True and asked[0]["retry_unconfirmed"] is True, asked
    owner_page.unroute_all(behavior="ignoreErrors")
