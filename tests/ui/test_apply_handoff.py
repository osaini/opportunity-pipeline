"""Apply for me's Finish in browser, in the browser: the Finish in browser button and its ticks, the filling step, the student's
turn (what is left, what was ticked, Stop, the window button), the result, Mark as applied?, the Answer column, the card badges
and the timeline.

The server runs in this process with the fictional listing and an agent that opens no browser (tests/apply_fake_ats.py): a
Finish in browser run answers with a canned handoff, the student's own press of Submit is the agent's own pause of
``CANNED["handoff"]["wait"]`` seconds, and nothing reaches Greenhouse. A fixture here puts the knobs back after each test and
waits until the app's one run slot is free, so the next test's database rewind does not pull the rows from under a run that is
still finishing.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from axe_core_python.sync_playwright import Axe
from playwright.sync_api import expect

import apply_fake_ats
from conftest import OWNER_TOKEN, wait_for_results
from opportunity_app.applications.actions import record_intent
from opportunity_app.core.timestamps import utc_now
from test_apply_sensitive import CONSENT, TERMS_FIELD, TERMS_LINK, allow, yes_no_terms  # noqa: F401 (the fixture is used by name)
from ui_helpers import AXE_OPTIONS, USER, confirm_posting, db, open_saved_role

BEARER = {"Authorization": f"Bearer {OWNER_TOKEN}"}
API = "/api/v1/apply-agent"
IDLE_WITHIN_S = 60
WINDOW_OPEN = "A Finish in browser window is open. Pausing doesn't stop your own Submit; press Stop to end it."
NOT_SUBMITTED = "You didn't submit it in the window. Your application was not sent."


def opportunity_id(live_server):
    with db(live_server) as conn:
        return conn.execute("SELECT id FROM opportunities WHERE company='Acme Robotics'").fetchone()[0]


def other_stages(live_server):
    """The tracker's rows for every role but Acme Robotics: what Finish in browser on Acme must not touch."""
    with db(live_server) as conn:
        return [tuple(row) for row in conn.execute(
            "SELECT a.opportunity_id, a.stage FROM applications a JOIN opportunities o ON o.id = a.opportunity_id "
            "WHERE o.company <> 'Acme Robotics' ORDER BY a.opportunity_id")]


@pytest.fixture(autouse=True)
def canned_agent(live_server, base_url, pristine_database):
    """The canned agent's knobs back to what they were, and the app's run slot free, before the next test rewinds the database."""
    original = dict(apply_fake_ats.CANNED)
    yield apply_fake_ats.CANNED
    apply_fake_ats.CANNED.clear()
    apply_fake_ats.CANNED.update(original)
    deadline = time.monotonic() + IDLE_WITHIN_S
    while time.monotonic() < deadline:
        try:
            listed = httpx.get(f"{base_url}{API}/opportunities/{opportunity_id(live_server)}/runs", headers=BEARER)
        except Exception:
            return
        if listed.status_code != 200 or not listed.json().get("busy"):
            return
        time.sleep(0.2)
    pytest.fail("a run was still going after the test ended")


def handoff(canned, *, wait=6.0, outcome="submitted"):
    # A new dict each time: the fixture restores the old one by reference.
    canned["handoff"] = {"wait": wait, "outcome": outcome}


def open_section(page):
    open_saved_role(page)
    section = page.locator(".apply-for-me")
    expect(section).to_be_visible()
    return section


def reload_section(page):
    """The page reloaded on the open role: the app opens the role again from its address, so the section is waited for, not clicked to."""
    page.reload()
    page.wait_for_selector("#detail-panel.is-open")
    section = page.locator(".apply-for-me")
    expect(section).to_be_visible()
    return section


def finish_button(section):
    return section.get_by_role("button", name="Finish in browser")


def start_finish(section):
    """The student says the posting is right (the sandbox's fake board is not Acme's) and presses Finish in browser."""
    confirm_posting(section)
    expect(finish_button(section)).to_be_visible()
    finish_button(section).click()


def reach_the_turn(page, canned, **knobs):
    handoff(canned, **knobs)
    section = open_section(page)
    start_finish(section)
    expect(section.locator(".apply-turn")).to_be_visible(timeout=30_000)
    return section


def csrf(page):
    return next((cookie["value"] for cookie in page.context.cookies() if cookie["name"] == "pipeline_csrf"), "")


def seed_claim(live_server, key, company, *, state="claimed", waiting="", mode="handoff", after_click=0, stage="applying", existing=None):
    """One opportunity, its application and one Finish in browser claim on it, as a runner in another process would leave it.

    ``existing`` is (application id, opportunity id) of a seeded role to put the claim on instead of making a new one."""
    now = datetime.now(timezone.utc)
    stamp = utc_now()
    detail = {"waiting": waiting} if waiting else {}
    handed = (now - timedelta(minutes=2)).isoformat(timespec="microseconds") if after_click else None
    application_id, opportunity = existing or (f"app-op-{key}", f"op-{key}")
    with db(live_server) as conn, conn:
        if existing is None:
            conn.execute(
                "INSERT INTO opportunities(id, company, title, url, first_seen_at, last_seen_at, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (opportunity, company, "Controls Intern", f"https://boards.example.test/{key}", stamp, stamp, stamp, stamp))
            conn.execute(
                "INSERT INTO applications(id, opportunity_id, user_id, stage, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?)",
                (application_id, opportunity, USER, stage, stamp, stamp))
        conn.execute(
            "INSERT INTO application_submit_claims(token, application_id, user_id, opportunity_id, instance, mode, state, after_click, ats, board_token, "
            "job_ref, company_key, stage_policy, plan_hash, handed_over_at, heartbeat_at, verification, stage_recorded, resolved_by, note, "
            "detail_json, created_at, updated_at) VALUES(?, ?, ?, ?, 'another-process', ?, ?, ?, 'greenhouse', 'ui', ?, ?, 'ask', '', ?, ?, '', 0, '', '', ?, ?, ?)",
            (f"tok-{key}", application_id, USER, opportunity, mode, state, after_click, f"ui/{key}", company.lower(), handed,
             now.isoformat(timespec="microseconds"), json.dumps(detail), stamp, stamp))


def seed_tombstone(live_server):
    """An attempt on Acme's own job that the student said didn't go through: Greenhouse may still have it, so starting again asks."""
    now = datetime.now(timezone.utc)
    stamp = utc_now()
    handed = (now - timedelta(days=5)).isoformat(timespec="microseconds")
    with db(live_server) as conn, conn:
        opportunity = conn.execute("SELECT id FROM opportunities WHERE company='Acme Robotics'").fetchone()[0]
        conn.execute(
            "INSERT INTO applications(id, opportunity_id, user_id, stage, created_at, updated_at) VALUES('app-tomb', ?, ?, 'applying', ?, ?)",
            (opportunity, USER, stamp, stamp))
        conn.execute(
            "INSERT INTO application_submit_claims(token, application_id, user_id, opportunity_id, instance, mode, state, after_click, ats, board_token, "
            "job_ref, company_key, stage_policy, plan_hash, handed_over_at, heartbeat_at, verification, stage_recorded, resolved_by, note, "
            "detail_json, created_at, updated_at) VALUES('tok-tomb', 'app-tomb', ?, ?, 'another-process', 'handoff', 'released', 1, 'greenhouse', ?, ?, "
            "'acme robotics', 'ask', '', ?, ?, '', 0, 'student', '', '{}', ?, ?)",
            (USER, opportunity, apply_fake_ats.BOARD_TOKEN, f"{apply_fake_ats.BOARD_TOKEN}/{apply_fake_ats.JOB_ID}", handed, handed, stamp, stamp))


# --- Starting ----------------------------------------------------------------------------------------------------------


def test_finish_in_browser_sits_beside_the_rehearsal_and_says_what_it_does(apply_ready, owner_page):
    section = open_section(owner_page)
    rehearsal = section.locator(".apply-rehearse")
    expect(rehearsal.get_by_role("button", name="Rehearse in a window")).to_be_visible()
    expect(finish_button(section)).to_be_visible()
    expect(rehearsal).to_contain_text(
        "Opens a Chromium window and fills the form. You complete what is left and press Submit application yourself. Your application is not sent until you do. "
        "To find options for typeahead fields, the app sends what is typed there to Greenhouse's lookup service.")


def test_a_tick_the_app_asks_for_must_be_ticked_before_the_button_works(apply_ready, owner_page, live_server, canned_agent):
    seed_tombstone(live_server)
    handoff(canned_agent, wait=2.0)
    section = open_section(owner_page)
    confirm_posting(section)
    group = section.locator("fieldset.apply-ticks")
    expect(group).to_be_visible()
    expect(group.locator("legend")).to_have_text("Before you go on")
    expect(group).to_contain_text("didn't go through")
    expect(finish_button(section)).to_have_attribute("aria-disabled", "true")
    finish_button(section).click(force=True)
    expect(section.locator(".apply-turn, .apply-run")).to_have_count(0)
    # The released attempt also counts as a recent application to the company: every tick the app asks for must be ticked.
    boxes = group.get_by_role("checkbox")
    expect(boxes.first).to_be_visible()
    for index in range(boxes.count()):
        expect(finish_button(section)).to_have_attribute("aria-disabled", "true")
        boxes.nth(index).check()
    expect(finish_button(section)).not_to_have_attribute("aria-disabled", "true")
    finish_button(section).click()
    expect(section.locator(".apply-run, .apply-turn").first).to_be_visible(timeout=30_000)
    with db(live_server) as conn:
        asked = conn.execute("SELECT detail_json FROM application_submit_claims WHERE mode='handoff' AND state <> 'released'").fetchall()
    assert asked, "the start made a claim"


def test_a_tick_given_for_one_start_is_not_carried_to_the_next_finish_in_browser_after_a_stop(apply_ready, owner_page, live_server, canned_agent):
    seed_tombstone(live_server)
    handoff(canned_agent, wait=60.0)
    section = open_section(owner_page)
    confirm_posting(section)
    group = section.locator("fieldset.apply-ticks")
    boxes = group.get_by_role("checkbox")
    expect(boxes.first).to_be_visible()
    for index in range(boxes.count()):
        boxes.nth(index).check()
    finish_button(section).click()
    turn = section.locator(".apply-turn")
    expect(turn).to_be_visible(timeout=30_000)
    turn.get_by_role("button", name="Stop").click()
    result = section.locator(".apply-result")
    expect(result.locator(".apply-result-title")).to_contain_text(NOT_SUBMITTED, timeout=30_000)
    again = result.locator("fieldset.apply-ticks").get_by_role("checkbox")
    expect(again.first).to_be_visible()
    for index in range(again.count()):
        expect(again.nth(index)).not_to_be_checked()
    expect(result.get_by_role("button", name="Finish in browser")).to_have_attribute("aria-disabled", "true")


# --- The run: filling, the student's turn, the result ------------------------------------------------------------------


def test_a_run_goes_from_filling_to_the_students_turn_to_a_result_and_mark_as_applied_moves_the_card(apply_ready, owner_page, live_server, canned_agent):
    handoff(canned_agent, wait=10.0)
    canned_agent["step_delay"] = 0.8
    before = other_stages(live_server)
    section = open_section(owner_page)
    start_finish(section)
    # Filling: the step it is on and the window note (the form is being filled, nobody has anything to do yet).
    run = section.locator(".apply-run")
    expect(run).to_be_visible()
    expect(run.locator(".apply-run-step")).to_have_attribute("role", "status")
    expect(run).to_contain_text("A Chromium window is open. You can watch, but please don't type in it.")
    # Your turn.
    turn = section.locator(".apply-turn")
    expect(turn).to_be_visible(timeout=30_000)
    expect(run).to_have_count(0)
    expect(turn.locator(".apply-run-step")).to_have_attribute("role", "status")
    # A live region inserted already holding its words is not read out, so the move to the student's turn is announced once.
    expect(owner_page.locator("#action-status")).to_contain_text(re.compile(r"The form is filled in the Chromium window\. .* press Submit application there\."))
    expect(turn.locator(".apply-turn-until")).to_contain_text(re.compile(r"The window closes at .+ if you haven't pressed Submit application\."))
    expect(turn.get_by_role("button", name="Stop")).to_be_visible()
    expect(turn).to_contain_text("Closes the window. Nothing is sent.")
    expect(turn.get_by_role("button", name="Bring the window forward")).to_be_visible()
    expect(turn).to_contain_text("If it doesn't appear, click Chromium in your taskbar.")
    expect(turn).to_contain_text("If the form shows an error, fix that field in the window and press Submit application again.")
    assert "nothing was sent" not in turn.inner_text().lower(), "no sentence before settle says nothing was sent (I9)"
    # The student's own press, then the result.
    result = section.locator(".apply-result")
    expect(result).to_be_visible(timeout=40_000)
    expect(result.locator(".apply-result-title")).to_contain_text("Greenhouse showed its confirmation page. Mark as applied?")
    mark = result.get_by_role("button", name="Mark as applied?")
    expect(mark).to_be_visible()
    with db(live_server) as conn:
        assert conn.execute("SELECT stage FROM applications WHERE opportunity_id=?", (opportunity_id(live_server),)).fetchone()[0] == "applying", \
            "the app never moves the tracker by itself"
    mark.click()
    expect(result.get_by_role("button", name="Mark as applied?")).to_have_count(0, timeout=15_000)
    with db(live_server) as conn:
        assert conn.execute("SELECT stage FROM applications WHERE opportunity_id=?", (opportunity_id(live_server),)).fetchone()[0] == "applied"
    assert other_stages(live_server) == before, "the other roles' rows were not touched"
    owner_page.locator("#detail-close").click()
    owner_page.click("#applications-nav")
    wait_for_results(owner_page)
    badge = owner_page.locator(".application-card", has_text="Acme Robotics").locator(".apply-badge")
    expect(badge).to_contain_text("Applied with Apply for me")


def store_statements(section):
    """The student stores the two statements the fictional form asks for: a tick box with no address, and a Yes/No agreement that links one."""
    confirm_posting(section)
    privacy = section.locator('[data-apply-key="question_4000000109"]')
    privacy.get_by_label("Yes, tick this statement for me on the form").check()
    privacy.get_by_label(CONSENT).check()
    privacy.get_by_role("button", name="Save this answer").click()
    expect(section.locator('[data-apply-key="question_4000000109"]')).to_have_count(0)
    terms = section.locator(f'[data-apply-key="{TERMS_FIELD}"]')
    terms.get_by_label("Yes, choose Yes for me on the form").check()
    terms.get_by_label(CONSENT).check()
    terms.get_by_role("button", name="Save this answer").click()
    expect(section.locator(f'[data-apply-key="{TERMS_FIELD}"]')).to_have_count(0)


def test_the_turn_lists_what_is_left_and_every_statement_the_app_ticked_with_its_addresses(apply_ready, yes_no_terms, owner_page, live_server, canned_agent):
    allow(live_server, "acknowledgment")
    handoff(canned_agent, wait=20.0)
    section = open_section(owner_page)
    store_statements(section)
    finish_button(section).click()
    turn = section.locator(".apply-turn")
    expect(turn).to_be_visible(timeout=30_000)
    left = turn.locator(".apply-left")
    expect(left.locator("h5")).to_have_text("Left for you")
    expect(left).to_contain_text("Which team are you most interested in?")
    ticked = turn.locator(".apply-ticked")
    expect(ticked.locator("h5")).to_have_text("Ticked for you")
    # A tick box, and a Yes/No agreement answered Yes from a stored statement, each by its question.
    privacy = ticked.locator(".apply-ticked-item", has_text="I have read the Example Robotics privacy notice")
    expect(privacy).to_contain_text("Ticked from your stored statement")
    terms = ticked.locator(".apply-ticked-item", has_text="Do you accept the candidate terms?")
    expect(terms).to_contain_text(f"Answered Yes from your stored statement · links to {TERMS_LINK}")
    expect(ticked.locator(".apply-ticked-item")).to_have_count(2)
    expect(ticked.locator("a")).to_have_count(0)    # an address from the page is shown, never made a link
    # What the app did not tick is not in the list.
    expect(ticked).not_to_contain_text("I certify that the information I have provided is accurate")


def test_the_result_shows_a_statements_address_beside_what_the_app_did_and_never_as_a_link(apply_ready, yes_no_terms, owner_page, live_server, canned_agent):
    allow(live_server, "acknowledgment")
    handoff(canned_agent, wait=1.0)
    section = open_section(owner_page)
    store_statements(section)
    finish_button(section).click()
    result = section.locator(".apply-result")
    expect(result).to_be_visible(timeout=40_000)
    row = result.locator("table.apply-plan tbody tr", has_text="Do you accept the candidate terms?")
    expect(row).to_have_count(1)
    expect(row).to_contain_text("Answered from your stored statement", timeout=15_000)
    expect(row).to_contain_text(TERMS_LINK)
    expect(result.locator("table.apply-plan a")).to_have_count(0)
    expect(result.locator("table.apply-plan tbody tr", has_text="I have read the Example Robotics privacy notice")).to_contain_text("Ticked from your stored statement")


def test_the_rehearsal_preview_shows_the_address_a_stored_statement_would_agree_to(apply_ready, yes_no_terms, owner_page, live_server):
    allow(live_server, "acknowledgment")
    section = open_section(owner_page)
    store_statements(section)
    confirm_posting(section)
    section.get_by_role("button", name="Rehearse in a window").click()
    result = section.locator(".apply-result")
    expect(result).to_be_visible(timeout=30_000)
    row = result.locator("table.apply-plan tbody tr", has_text="Do you accept the candidate terms?")
    # Before the Answer column arrives, the From column carries the address; after it, the Answer cell does.
    expect(row).to_contain_text(TERMS_LINK, timeout=15_000)
    expect(result.locator("table.apply-plan thead th").nth(1)).to_have_text("Answer", timeout=15_000)
    expect(row).to_contain_text(TERMS_LINK)
    expect(result.locator("table.apply-plan a")).to_have_count(0)


def current_run_id(live_server):
    with db(live_server) as conn:
        return conn.execute("SELECT id FROM apply_runs WHERE kind='handoff' ORDER BY started_at DESC LIMIT 1").fetchone()[0]


def test_a_turn_with_nothing_left_shows_no_left_for_you_list_and_says_so_in_the_servers_words(apply_ready, owner_page, live_server, canned_agent):
    # The words the server gives an empty list are pinned in tests/test_apply_handoff.py (YOUR_TURN_NONE_LEFT): this row holds the page to
    # showing the summary it is given, and to drawing no list when there is nothing to list.
    from opportunity_app.apply.agent_types import YOUR_TURN_NONE_LEFT

    def empty_left(route):
        response = route.fetch()
        body = response.json()
        body["left_for_you"] = []
        if body.get("phase") == "your_turn":
            body["summary"] = YOUR_TURN_NONE_LEFT
        route.fulfill(response=response, json=body)

    owner_page.route(re.compile(rf".*{API}/runs/run-[0-9a-f]+$"), empty_left)
    try:
        section = reach_the_turn(owner_page, canned_agent, wait=8.0)
        expect(section.locator(".apply-turn")).to_be_visible()
        expect(section.locator(".apply-turn .apply-run-step")).to_have_text(YOUR_TURN_NONE_LEFT)
        expect(section.locator(".apply-left")).to_have_count(0)
    finally:
        # A poll still in flight when the page closes would fail inside the handler and be blamed on the next test.
        owner_page.unroute_all(behavior="ignoreErrors")


def test_stop_during_the_turn_ends_it_and_offers_finish_in_browser_again(apply_ready, owner_page, live_server, canned_agent):
    section = reach_the_turn(owner_page, canned_agent, wait=60.0)
    stop = section.locator(".apply-turn").get_by_role("button", name="Stop")
    # The busy label is set in the click handler itself, before its first await; read it in the same task as the click,
    # because the canned run can end and replace the turn before a later look.
    assert stop.evaluate("(button) => { button.click(); return button.textContent; }") == "Stopping…"
    result = section.locator(".apply-result")
    expect(result.locator(".apply-result-title")).to_contain_text(NOT_SUBMITTED, timeout=30_000)
    expect(section.locator(".apply-turn")).to_have_count(0)
    expect(result.get_by_role("button", name="Finish in browser")).to_be_visible()
    expect(result.get_by_role("button", name="Mark as applied?")).to_have_count(0)
    with db(live_server) as conn:
        assert conn.execute("SELECT stage FROM applications WHERE opportunity_id=?", (opportunity_id(live_server),)).fetchone()[0] == "applying"


def test_a_run_that_may_have_been_sent_says_so_and_it_went_through_follows_to_the_card(apply_ready, owner_page, live_server, canned_agent):
    handoff(canned_agent, wait=1.5, outcome="unconfirmed")
    section = open_section(owner_page)
    start_finish(section)
    result = section.locator(".apply-result")
    expect(result).to_be_visible(timeout=40_000)
    expect(result.locator(".apply-claim")).to_contain_text("This may have been sent.")
    yes = result.get_by_role("button", name="It went through")
    no = result.get_by_role("button", name="It didn't go through")
    expect(yes).to_be_visible()
    expect(no).to_be_visible()
    expect(result.get_by_role("button", name="Finish in browser")).to_have_count(0)
    yes.click()
    expect(result.get_by_role("button", name="It went through")).to_have_count(0, timeout=15_000)
    owner_page.locator("#detail-close").click()
    owner_page.click("#applications-nav")
    wait_for_results(owner_page)
    badge = owner_page.locator(".application-card", has_text="Acme Robotics").locator(".apply-badge")
    expect(badge).to_contain_text("Submitted with Apply for me")
    expect(badge).to_contain_text("You said it went through")


def test_after_it_didnt_go_through_the_role_can_be_tried_again_also_after_it_is_opened_again(apply_ready, owner_page, live_server, canned_agent):
    handoff(canned_agent, wait=1.0, outcome="unconfirmed")
    section = open_section(owner_page)
    start_finish(section)
    result = section.locator(".apply-result")
    expect(result.get_by_role("button", name="It didn't go through")).to_be_visible(timeout=40_000)
    expect(result.get_by_role("button", name="Finish in browser")).to_have_count(0)
    result.get_by_role("button", name="It didn't go through").click()
    result = section.locator(".apply-result")
    expect(result.get_by_role("button", name="It didn't go through")).to_have_count(0, timeout=15_000)
    expect(result.locator(".apply-claim")).to_contain_text("You said it didn't go through.")
    # Not a dead end: the button is there, and it says why it waits (the app spaces its own submissions apart) instead of vanishing.
    expect(result.get_by_role("button", name="Finish in browser")).to_be_visible()
    expect(finish_button(section)).to_have_attribute("aria-disabled", "true")
    expect(section.locator(".apply-rehearse")).to_contain_text("The next agent submission is allowed at", timeout=15_000)
    # Some days later the attempt is old enough: starting again asks about the released attempt first, and each ask is a box.
    with db(live_server) as conn, conn:
        conn.execute("UPDATE application_submit_claims SET handed_over_at=?", ((datetime.now(timezone.utc) - timedelta(days=5)).isoformat(timespec="microseconds"),))
    section = reload_section(owner_page)
    expect(section.locator(".apply-result .apply-claim")).to_contain_text("You said it didn't go through.")
    expect(section.locator(".apply-result").get_by_role("button", name="Finish in browser")).to_be_visible()
    group = section.locator("fieldset.apply-ticks")
    expect(group).to_contain_text("didn't go through")
    expect(finish_button(section)).to_have_attribute("aria-disabled", "true")
    for box in group.get_by_role("checkbox").all():
        box.check()
    expect(finish_button(section)).not_to_have_attribute("aria-disabled", "true")


def test_what_the_app_asks_is_on_the_page_once_when_the_button_shows_it_as_boxes(apply_ready, owner_page, live_server, canned_agent):
    seed_tombstone(live_server)
    section = open_section(owner_page)
    confirm_posting(section)
    expect(section.locator("fieldset.apply-ticks")).to_contain_text("didn't go through")
    expect(section.locator("p.apply-limit", has_text=re.compile("^Before you go on:"))).to_have_count(0)


def test_the_picture_taken_after_the_press_is_not_called_the_filled_form(apply_ready, owner_page, live_server, canned_agent):
    def with_final_picture(route):
        response = route.fetch()
        body = response.json()
        if body.get("outcome") == "submitted":
            body["screenshots"] = [*body.get("screenshots", []), {"index": 7, "step": "final", "url": f"{API}/runs/{body['id']}/screenshots/7", "masked": [], "available": True}]
        route.fulfill(response=response, json=body)

    owner_page.route(re.compile(rf".*{API}/runs/run-[0-9a-f]+$"), with_final_picture)
    owner_page.route(re.compile(rf".*{API}/runs/run-[0-9a-f]+/screenshots/7$"), lambda route: route.fulfill(content_type="image/png", body=apply_fake_ats.canned_png()))
    try:
        handoff(canned_agent, wait=1.0)
        section = open_section(owner_page)
        start_finish(section)
        result = section.locator(".apply-result")
        expect(result).to_be_visible(timeout=40_000)
        expect(result.locator('img[alt="The page after you pressed Submit application, with sensitive fields covered"]')).to_have_count(1)
        expect(result.locator('img[alt="The filled form, with sensitive fields covered"]')).to_have_count(1)   # the picture taken before the turn
    finally:
        owner_page.unroute_all(behavior="ignoreErrors")


def test_a_role_that_was_rehearsed_still_shows_its_rehearsal_after_many_lookups(apply_ready, owner_page, live_server):
    from opportunity_app.apply import runs as apply_runs

    with db(live_server) as conn:
        opportunity = conn.execute("SELECT id FROM opportunities WHERE company='Acme Robotics'").fetchone()[0]
    with db(live_server) as conn:
        def make(kind, stamp, outcome):
            run_id = apply_runs.create_run(
                conn, user_id=USER, opportunity_id=opportunity, kind=kind, started_by="student", ats="greenhouse", board_token=apply_fake_ats.BOARD_TOKEN,
                page_url=apply_fake_ats.JOB_URL, company="acme robotics", deadline_seconds=300, now=stamp,
            )
            apply_runs.finish_run(conn, run_id, outcome=outcome, clean=False, now=stamp)
            return run_id

        base = datetime.now(timezone.utc) - timedelta(hours=1)
        make("rehearsal", base, "rehearsed")
        for number in range(1, 8):
            make("lookup", base + timedelta(minutes=number), "looked_up")
    section = open_section(owner_page)
    expect(section.locator(".apply-result .apply-result-title")).to_contain_text("Here is what the app would send", timeout=15_000)


def test_the_timeline_does_not_credit_you_with_what_the_app_wrote_on_its_own(apply_ready, owner_page, live_server, canned_agent):
    handoff(canned_agent, wait=1.0, outcome="unconfirmed")
    section = open_section(owner_page)
    start_finish(section)
    expect(section.locator(".apply-result")).to_be_visible(timeout=40_000)
    owner_page.locator("#detail-close").click()
    owner_page.click("#applications-nav")
    wait_for_results(owner_page)
    card = owner_page.locator(".application-card", has_text="Acme Robotics")
    card.locator("summary", has_text="Tasks, contacts, and timeline").click()
    rows = card.locator(".timeline-list li")
    unconfirmed = rows.filter(has_text="may have been sent")
    expect(unconfirmed).to_have_count(1)
    expect(unconfirmed.locator(".timeline-author")).to_have_text("The app, on its own")
    expect(rows.filter(has_text="Finish in browser started").locator(".timeline-author")).to_have_text("You")


def test_a_finished_run_comes_back_when_the_role_is_opened_again(apply_ready, owner_page, live_server, canned_agent):
    handoff(canned_agent, wait=1.0)
    section = open_section(owner_page)
    start_finish(section)
    expect(section.locator(".apply-result")).to_be_visible(timeout=40_000)
    section = reload_section(owner_page)
    expect(section.locator(".apply-result .apply-result-title")).to_contain_text("Greenhouse showed its confirmation page")


def test_a_run_still_going_is_picked_up_again_by_phase(apply_ready, owner_page, live_server, canned_agent):
    handoff(canned_agent, wait=15.0)
    section = open_section(owner_page)
    start_finish(section)
    expect(section.locator(".apply-turn")).to_be_visible(timeout=30_000)
    owner_page.locator("#detail-close").click()
    section = open_section(owner_page)
    expect(section.locator(".apply-turn")).to_be_visible()
    # Nothing was handed over: the sentence above the turn never says the application was submitted.
    expect(section.locator(".apply-summary")).to_contain_text("Finish in browser is already open for this role")
    assert "submitted" not in section.locator(".apply-summary").inner_text()
    section.locator(".apply-turn").get_by_role("button", name="Stop").click()
    result = section.locator(".apply-result")
    expect(result).to_be_visible(timeout=30_000)
    # The check read while the run was going said the claim was live; once it stopped the role can be tried again, without reopening it.
    expect(result.get_by_role("button", name="Finish in browser")).to_be_visible()
    expect(result.get_by_role("button", name="Finish in browser")).not_to_have_attribute("aria-disabled", "true", timeout=15_000)
    expect(section.locator(".apply-summary")).not_to_contain_text("already open for this role", timeout=15_000)
    # A Finish in browser run wrote to the tracker (an application and its events): the section never says nothing in it changed.
    expect(section.locator(".apply-note")).not_to_contain_text("Nothing in your tracker has changed")
    expect(section.locator(".apply-note")).to_contain_text("A rehearsal changes nothing in your tracker")


# --- The plan preview: values, groups --------------------------------------------------------------------------------------


def rehearsal_result(page):
    section = open_section(page)
    confirm_posting(section)       # the fake board always serves Example Robotics' listing, so a rehearsal refuses the saved role until the tick
    section.get_by_role("button", name="Rehearse in a window").click()
    expect(section.locator(".apply-result")).to_be_visible(timeout=30_000)
    return section


def test_the_rehearsal_shows_the_answer_column_then_what_changed_and_not_now_folds_it_back(apply_ready, owner_page, live_server):
    section = rehearsal_result(owner_page)
    table = section.locator("table.apply-plan")
    expect(table.locator("thead th").nth(1)).to_have_text("Answer", timeout=15_000)
    expect(table.locator("thead th")).to_have_text(["Question", "Answer", "What the rehearsal did", "From"])
    expect(table).to_contain_text("Sam")
    expect(section.locator(".apply-plan-changed")).to_have_count(0)
    # The student changes an answer the plan used; the next look at the same rehearsal says so.
    with db(live_server) as conn, conn:
        conn.execute("UPDATE profile_facts SET value_json=? WHERE field_path='name_parts'", (json.dumps({"first": "Alex", "last": "Rivera", "preferred": ""}),))
    section = reload_section(owner_page)
    expect(section.locator(".apply-plan-changed").first).to_have_text("changed since the rehearsal", timeout=15_000)
    expect(section.locator(".apply-values-note")).to_contain_text("Some answers changed since this rehearsal. Rehearse again to see the new plan; Finish in browser uses your current answers.")
    # Not now folds the result back into the starters, with no request.
    sent = []
    # The masked screenshot of the reloaded result may still be loading; an <img> fetch is not a question to the server.
    owner_page.on("request", lambda request: sent.append(request.url) if "/apply-agent/" in request.url and request.resource_type != "image" else None)
    section.get_by_role("button", name="Not now").click()
    expect(section.locator(".apply-result")).to_have_count(0)
    expect(section.get_by_role("button", name="Rehearse in a window")).to_be_visible()
    expect(finish_button(section)).to_be_visible()
    assert sent == [], f"Not now asked the server something: {sent}"


def test_left_blank_shows_its_reasons_in_a_collapsed_group(apply_ready, owner_page, live_server):
    section = rehearsal_result(owner_page)
    blank = section.locator("details.apply-blank")
    # The sandbox student has no phone, portfolio or other optional answer: those fields are left blank, with the reason.
    expect(blank).to_have_count(1)
    expect(blank).not_to_have_attribute("open", "")
    expect(blank.locator("summary")).to_have_text(re.compile(r"^Left blank \(\d+\)$"))
    blank.locator("summary").click()
    expect(blank.locator("li").first).to_be_visible()
    expect(blank.locator("li p.profile-help").first).not_to_be_empty()


def test_a_finished_run_names_its_column_what_the_app_filled_and_warns_about_edits(apply_ready, owner_page, live_server, canned_agent):
    handoff(canned_agent, wait=1.0)
    section = open_section(owner_page)
    start_finish(section)
    result = section.locator(".apply-result")
    expect(result).to_be_visible(timeout=40_000)
    expect(result.locator("table.apply-plan thead th").nth(1)).to_have_text("What the app filled", timeout=15_000)
    expect(result.locator(".apply-values-note")).to_have_text("You may have changed fields in the window before you pressed Submit application.")
    # A value that changed since is not shown: what was sent is not stored.
    with db(live_server) as conn, conn:
        conn.execute("UPDATE profile_facts SET value_json=? WHERE field_path='name_parts'", (json.dumps({"first": "Alex", "last": "Rivera", "preferred": ""}),))
    section = reload_section(owner_page)
    expect(section.locator(".apply-plan-answer", has_text="Changed since this application was filled; what was sent is not stored.").first).to_be_visible(timeout=15_000)
    assert "Alex" not in section.locator("table.apply-plan").inner_text(), "a value the student changed afterwards is never drawn as what was filled"


def test_a_stopped_run_does_not_talk_about_a_press_or_what_was_sent(apply_ready, owner_page, live_server, canned_agent):
    section = reach_the_turn(owner_page, canned_agent, wait=60.0)
    section.locator(".apply-turn").get_by_role("button", name="Stop").click()
    result = section.locator(".apply-result")
    expect(result.locator(".apply-result-title")).to_contain_text(NOT_SUBMITTED, timeout=30_000)
    expect(result.locator("table.apply-plan thead th").nth(1)).to_have_text("What the app filled", timeout=15_000)
    assert "pressed Submit" not in result.locator(".apply-plan-box").inner_text(), "a run that was never submitted talks about a press"
    expect(result.locator(".apply-values-note")).to_have_count(0)
    with db(live_server) as conn, conn:
        conn.execute("UPDATE profile_facts SET value_json=? WHERE field_path='name_parts'", (json.dumps({"first": "Alex", "last": "Rivera", "preferred": ""}),))
    section = reload_section(owner_page)
    cell = section.locator(".apply-plan-answer", has_text="Changed since the app filled the form.").first
    expect(cell).to_be_visible(timeout=15_000)
    assert "what was sent" not in section.locator("table.apply-plan").inner_text()


# --- Cards, the timeline, the pause ------------------------------------------------------------------------------------------


def test_the_card_says_filling_and_your_turn_and_opens_the_role(apply_ready, owner_page, live_server):
    seed_claim(live_server, "fill", "Bluefin Robotics", waiting="")
    with db(live_server) as conn:
        orbit = record_intent(conn, "job-b", "apply_opened", user_id=USER)["application_id"]
    seed_claim(live_server, "turn", "Orbit Systems", waiting="student", existing=(orbit, "job-b"))
    owner_page.reload()
    wait_for_results(owner_page)
    owner_page.click("#applications-nav")
    wait_for_results(owner_page)
    filling = owner_page.locator('.application-card[data-application-id="app-op-fill"] .apply-badge')
    turn = owner_page.locator(f'.application-card[data-application-id="{orbit}"] .apply-badge')
    expect(filling).to_contain_text("Apply for me is filling the form in a window")
    expect(turn).to_contain_text("Your turn: finish the form in the Chromium window and press Submit application")
    turn.get_by_role("button", name="Open").click()
    owner_page.wait_for_selector("#detail-panel.is-open")
    expect(owner_page.locator("#detail-content")).to_contain_text("Orbit Systems")


def test_the_timeline_says_you_submitted_in_the_window_and_see_the_run_opens_that_run(apply_ready, owner_page, live_server, canned_agent):
    # Two runs on the role: the first is stopped, the second is submitted. See the run on the older one must open the older one.
    handoff(canned_agent, wait=60.0)
    section = open_section(owner_page)
    start_finish(section)
    turn = section.locator(".apply-turn")
    expect(turn).to_be_visible(timeout=30_000)
    first = current_run_id(live_server)
    turn.get_by_role("button", name="Stop").click()
    expect(section.locator(".apply-result .apply-result-title")).to_contain_text(NOT_SUBMITTED, timeout=30_000)
    handoff(canned_agent, wait=1.0)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and finish_button(section).get_attribute("aria-disabled") == "true":
        owner_page.wait_for_timeout(200)
    for box in section.locator("fieldset.apply-ticks").get_by_role("checkbox").all():
        box.check()
    finish_button(section).click()
    expect(section.locator(".apply-result .apply-result-title")).to_contain_text("Greenhouse showed its confirmation page", timeout=60_000)
    second = current_run_id(live_server)
    assert first != second
    owner_page.locator("#detail-close").click()
    owner_page.click("#applications-nav")
    wait_for_results(owner_page)
    card = owner_page.locator(".application-card", has_text="Acme Robotics")
    card.locator("summary", has_text="Tasks, contacts, and timeline").click()
    timeline = card.locator(".timeline-list")
    expect(timeline).to_contain_text("You submitted in the window")
    expect(timeline).to_contain_text("Finish in browser started")
    buttons = timeline.get_by_role("button", name="See the run")
    assert buttons.count() >= 3, "the two starts and the submission each carry their run"
    # The timeline is newest first: the last button belongs to the first run.
    buttons.last.click()
    owner_page.wait_for_selector("#detail-panel.is-open")
    result = owner_page.locator(".apply-for-me .apply-result")
    expect(result.locator(".apply-result-title")).to_contain_text(NOT_SUBMITTED, timeout=15_000)
    assert "confirmation page" not in result.inner_text(), "See the run opened the latest run, not the one it names"
    # The first attempt was released by the later start, not by anything the student said.
    assert "You said it didn't go through" not in result.inner_text(), "the page credits the student with a word they never gave"


def test_pausing_during_the_turn_says_the_window_stays_open(apply_ready, owner_page, live_server, canned_agent):
    reach_the_turn(owner_page, canned_agent, wait=12.0)
    owner_page.locator("#detail-close").click()
    owner_page.click("#profile-nav")
    wait_for_results(owner_page)
    owner_page.click("#automation-pause")
    status = owner_page.locator("#automation-pause-status")
    expect(status).to_contain_text(WINDOW_OPEN, timeout=15_000)
    expect(status).not_to_contain_text("already on its way")


# --- Sign-out --------------------------------------------------------------------------------------------------------------------


def sign_out(page):
    with page.expect_response(lambda response: response.url.endswith("/api/v1/session") and response.request.method == "DELETE") as signed_out:
        page.evaluate("() => document.getElementById('logout-button').click()")
    assert signed_out.value.ok
    page.wait_for_selector("#auth-gate.is-visible")


@pytest.mark.allow_page_errors
def test_signing_out_during_the_turn_stops_the_polling(apply_ready, owner_page, live_server, canned_agent):
    reach_the_turn(owner_page, canned_agent, wait=20.0)
    sign_out(owner_page)
    owner_page.fill("#token-input", "not-the-token")
    owner_page.click("#auth-submit")
    expect(owner_page.locator("#auth-error")).to_have_text("That token was not accepted.")
    polled = []
    owner_page.on("request", lambda request: polled.append(request.url) if "/apply-agent/runs/" in request.url else None)
    owner_page.wait_for_timeout(3500)
    assert polled == [], f"the poll kept going after sign-out: {polled}"


@pytest.mark.allow_page_errors
def test_signing_out_takes_the_answer_column_away(apply_ready, owner_page, live_server):
    section = rehearsal_result(owner_page)
    expect(section.locator("table.apply-plan thead th").nth(1)).to_have_text("Answer", timeout=15_000)
    sign_out(owner_page)
    assert owner_page.evaluate("() => [...document.querySelectorAll('table.apply-plan thead th')].map((cell) => cell.textContent)") == [
        "Question", "What the rehearsal did", "From"]
    assert "Sam" not in owner_page.evaluate("() => document.querySelector('table.apply-plan').innerText")


@pytest.mark.allow_page_errors
def test_a_values_answer_that_arrives_after_sign_out_paints_nothing(apply_ready, owner_page, live_server):
    gate = {"release": None}

    def hold(route):
        gate["release"] = route
        # Not answered yet: the page waits on it.

    owner_page.route(re.compile(rf".*{API}/runs/[^/]+/values$"), hold)
    section = open_section(owner_page)
    confirm_posting(section)
    section.get_by_role("button", name="Rehearse in a window").click()
    expect(section.locator(".apply-result")).to_be_visible(timeout=30_000)
    deadline = time.monotonic() + 10
    while gate["release"] is None and time.monotonic() < deadline:
        owner_page.wait_for_timeout(100)
    assert gate["release"] is not None, "the page asked for the values"
    sign_out(owner_page)
    held = gate["release"]
    held.fulfill(json={"values": {"first_name": {"text": "Sam", "changed": False, "available": True, "shown": True}}})
    owner_page.wait_for_timeout(500)
    assert owner_page.evaluate("() => document.querySelectorAll('table.apply-plan thead th').length") in (0, 3)
    assert "Sam" not in (owner_page.evaluate("() => document.querySelector('.apply-for-me')?.innerText || ''") or "")


# --- Accessibility and layout ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("width", (1280, 390))
def test_the_turn_and_the_result_are_accessible_and_do_not_overflow(apply_ready, owner_page, live_server, canned_agent, width):
    owner_page.set_viewport_size({"width": width, "height": 900})
    section = reach_the_turn(owner_page, canned_agent, wait=25.0)
    violations = Axe().run(owner_page, context=".apply-for-me", options=AXE_OPTIONS).get("violations", [])
    assert not violations, [(item["id"], item["help"]) for item in violations]
    assert owner_page.evaluate("() => document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    section.locator(".apply-turn").get_by_role("button", name="Stop").click()
    expect(section.locator(".apply-result")).to_be_visible(timeout=30_000)
    expect(section.locator("table.apply-plan thead th").nth(1)).to_have_text("What the app filled", timeout=15_000)
    violations = Axe().run(owner_page, context=".apply-for-me", options=AXE_OPTIONS).get("violations", [])
    assert not violations, [(item["id"], item["help"]) for item in violations]
    assert owner_page.evaluate("() => document.documentElement.scrollWidth <= document.documentElement.clientWidth")
