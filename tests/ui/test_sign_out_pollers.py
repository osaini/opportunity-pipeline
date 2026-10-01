"""Background pollers stop when the session ends.

The Outreach tab polls while a call prep is being written (every 5 seconds) and
looks for bounces after a send (15 s, 45 s, 2 min, 5 min). Before the fix
nothing stopped them on sign-out or session loss: each poll got a 401, and
api() answered every 401 by re-running showAuth(), which blanks the sign-in
error and moves focus to the email field. The student typing a password was
thrown back to the email field every few seconds and never saw the error.

The browser clock is faked so minutes of polling take milliseconds.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from playwright.sync_api import expect

from conftest import OWNER_TOKEN, sign_in_as_owner, wait_for_results
from outreach_fakes import COMPOSE_ACCOUNT

BEARER = {"Authorization": f"Bearer {OWNER_TOKEN}"}
TOKEN_REJECTED = "That token was not accepted."


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


def is_list_url(url):
    return urlsplit(url).path == "/api/v1/outreach"


def sign_out_and_fail_a_sign_in(page):
    """Sign out, then try a wrong token so the gate shows a sign-in error."""
    with page.expect_response(
        lambda response: response.url.endswith("/api/v1/session") and response.request.method == "DELETE"
    ) as signed_out:
        page.click("#logout-button")
    assert signed_out.value.ok, f"sign-out failed: {signed_out.value.status}"
    page.wait_for_selector("#auth-gate.is-visible")
    page.fill("#token-input", "not-the-token")
    page.click("#auth-submit")
    expect(page.locator("#auth-error")).to_have_text(TOKEN_REJECTED)
    # showAuth moves focus on a zero-delay timer, which the fake clock holds.
    page.clock.run_for(100)
    page.focus("#token-input")


def assert_gate_undisturbed(page):
    expect(page.locator("#auth-error")).to_have_text(TOKEN_REJECTED)
    focused = page.evaluate("document.activeElement && document.activeElement.id")
    assert focused == "token-input", f"focus was taken from the token field and given to #{focused}"


def test_a_call_prep_watcher_stops_when_the_student_signs_out(page, base_url):
    page.clock.install()
    page.goto("/")
    sign_in_as_owner(page)
    wait_for_results(page)
    target = seed_target(page, base_url, contact_email="jane@bovi.example", contact_name="Jane Doe")

    signed_out = []
    polls_after_sign_out = []
    polls = []

    def listing(route):
        response = route.fetch()
        payload = response.json()
        for item in payload["items"]:
            item["status"] = "replied"
            item["call_prep_job"] = {"state": "running", "attempts": 1}
        route.fulfill(response=response, json=payload)

    def one_company(route):
        if signed_out:
            polls_after_sign_out.append(route.request.url)
            route.continue_()
            return
        polls.append(route.request.url)
        route.fulfill(json={"id": target["id"], "call_prep_job": {"state": "running", "attempts": 1}})

    page.route(lambda url: is_list_url(url), listing)
    page.route(re.compile(rf".*/api/v1/outreach/{re.escape(target['id'])}$"), one_company)

    page.click("#outreach-nav")
    wait_for_results(page)
    page.locator('#subnav [data-subtab="replied"]').click()
    wait_for_results(page)
    page.clock.run_for(5_100)
    page.wait_for_timeout(200)
    assert polls, "the call prep watcher never polled, so this test proves nothing"

    sign_out_and_fail_a_sign_in(page)
    signed_out.append(True)

    for _ in range(4):
        page.clock.run_for(5_100)
        page.wait_for_timeout(100)
    assert polls_after_sign_out == [], f"the watcher kept polling after sign-out: {polls_after_sign_out}"
    assert_gate_undisturbed(page)
    page.unroute_all(behavior="ignoreErrors")


def test_the_bounce_looks_stop_when_the_student_signs_out(page, base_url):
    page.clock.install()
    page.goto("/")
    sign_in_as_owner(page)
    wait_for_results(page)
    target = seed_target(
        page, base_url, contact_email="jane@bovi.example", contact_name="Jane Doe",
        email_subject="Internship question", email_body="Hi Jane,\n\nWould you be open to a call?\n\nTest Student",
    )
    approved = page.request.post(
        f"{base_url}/api/v1/outreach/{target['id']}/approve", headers=BEARER,
        data={"fingerprint": target["draft_fingerprint"]},
    )
    assert approved.ok, approved.text()
    gmail = {"configured": True, "connected": True, "needs_reconnect": False, "account": COMPOSE_ACCOUNT,
             "attachment": "resume.pdf", "attachment_problem": ""}

    signed_out = []
    looks_before = []
    looks_after_sign_out = []

    def listing(route):
        response = route.fetch()
        route.fulfill(response=response, json={**response.json(), "gmail_drafts": gmail})

    def send(route):
        route.fulfill(json={"kind": "initial", "to": "jane@bovi.example", "cc": "", "account": COMPOSE_ACCOUNT,
                            "attachment": "resume.pdf", "status": "sent", "follow_up_at": "2026-10-02",
                            "message_id": "sent-1", "thread_id": "thread-1", "fingerprint": target["draft_fingerprint"]})

    def inbox_check(route):
        if signed_out:
            looks_after_sign_out.append(route.request.url)
            route.continue_()
            return
        looks_before.append(route.request.url)
        route.fulfill(json={})

    page.route(lambda url: is_list_url(url), listing)
    page.route(f"**/api/v1/outreach/{target['id']}/gmail-send", send)
    page.route("**/api/v1/outreach/inbox-check", inbox_check)

    page.click("#outreach-nav")
    wait_for_results(page)
    button = page.locator("button.outreach-send")
    button.click()
    button.click()
    expect(page.locator("#action-status")).to_contain_text("Sent to jane@bovi.example")
    # The first look is 15 s after the send.
    page.clock.run_for(16_000)
    page.wait_for_timeout(200)
    assert looks_before, "no bounce look ran, so this test proves nothing"

    sign_out_and_fail_a_sign_in(page)
    signed_out.append(True)

    for _ in range(4):
        page.clock.run_for(130_000)
        page.wait_for_timeout(100)
    assert looks_after_sign_out == [], f"bounce looks kept running after sign-out: {looks_after_sign_out}"
    assert_gate_undisturbed(page)
    page.unroute_all(behavior="ignoreErrors")
