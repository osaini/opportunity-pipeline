"""Apply for me's rehearsal and option lookup, in the browser: the Rehearse button, the Running state, the result, the review marks.

The server runs in this process with the fictional listing and an agent that opens no browser (tests/apply_fake_ats.py):
a rehearsal or a lookup answers with a canned result after a few seconds, and nothing reaches Greenhouse. The canned agent's
knobs are in ``apply_fake_ats.CANNED``; a fixture here puts them back after each test and waits until the app's one run
slot is free, so the next test's database rewind does not pull the rows from under a run that is still finishing.
"""

from __future__ import annotations

import re
import time

import httpx
import pytest
from axe_core_python.sync_playwright import Axe
from playwright.sync_api import expect

import apply_fake_ats
from conftest import OWNER_TOKEN
from ui_helpers import AXE_OPTIONS, confirm_posting, db, open_saved_role

BEARER = {"Authorization": f"Bearer {OWNER_TOKEN}"}
API = "/api/v1/apply-agent"
IDLE_WITHIN_S = 40


def opportunity_id(live_server):
    with db(live_server) as conn:
        return conn.execute("SELECT id FROM opportunities WHERE company='Acme Robotics'").fetchone()[0]


def tracker_rows(live_server):
    """What a rehearsal must not change, and the one thing it adds: the tracker's rows, its interactions, and the runs."""
    with db(live_server) as conn:
        return (
            [tuple(row) for row in conn.execute("SELECT opportunity_id, stage FROM applications ORDER BY opportunity_id")],
            conn.execute("SELECT COUNT(*) FROM opportunity_interactions").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM application_events").fetchone()[0],
        ), conn.execute("SELECT COUNT(*) FROM apply_runs").fetchone()[0]


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


def open_section(page):
    open_saved_role(page)
    section = page.locator(".apply-for-me")
    expect(section).to_be_visible()
    return section


def rehearse(section):
    # The fictional board answers every role with Example Robotics' listing, so the student says it is the right posting first.
    confirm_posting(section)
    section.get_by_role("button", name="Rehearse in a window").click()


def finished_rehearsal(page, live_server):
    """A rehearsal started and run to its result, the section open on it."""
    section = open_section(page)
    rehearse(section)
    expect(section.locator(".apply-result")).to_be_visible(timeout=20_000)
    return section


def test_a_rehearsal_runs_in_front_of_the_student_and_ends_in_a_result_that_changes_nothing(apply_ready, owner_page, live_server, canned_agent):
    canned_agent["step_delay"] = 0.6
    before, runs_before = tracker_rows(live_server)
    section = open_section(owner_page)
    rehearsal = section.locator(".apply-rehearse")
    expect(rehearsal).to_contain_text("Opens a Chromium window and fills this form to check it")
    expect(rehearsal).to_contain_text("Nothing is sent")
    rehearse(section)
    # Running: the step it is on, the window it opened, and a way to stop it.
    run = section.locator(".apply-run")
    expect(run).to_be_visible()
    expect(run.locator(".apply-run-step")).to_have_attribute("role", "status")
    expect(run.locator(".apply-run-step")).not_to_be_empty()
    expect(run).to_contain_text("A Chromium window is open. You can watch, but please don't type in it.")
    expect(run.get_by_role("button", name="Stop")).to_be_visible()
    expect(section.get_by_role("button", name="Rehearse in a window")).to_have_count(0)
    # Result: the sentence, what was measured, the table of what it did, and the picture of the filled form.
    result = section.locator(".apply-result")
    expect(result).to_be_visible(timeout=20_000)
    expect(run).to_have_count(0)
    expect(result.locator(".apply-result-title")).to_contain_text("Here is what the app would send to")
    expect(result.locator(".apply-result-title")).to_contain_text("Your application has not been submitted.")
    expect(result.locator(".apply-measured")).to_contain_text("blocked 1 request")
    expect(result.locator("table.apply-plan caption")).to_have_text("What the rehearsal did with each field")
    expect(result.locator("table.apply-plan thead th")).to_have_text(["Question", "What the rehearsal did", "From"])
    assert result.locator("table.apply-plan tbody tr").count() >= 1
    picture = result.locator(".apply-shot img")
    picture.scroll_into_view_if_needed()
    owner_page.wait_for_function("() => { const i = document.querySelector('.apply-shot img'); return Boolean(i && i.complete && i.naturalWidth > 0); }")
    expect(picture).to_have_attribute("alt", "The filled form, with sensitive fields covered")
    expect(result.locator(".apply-shot a")).to_have_attribute("target", "_blank")
    expect(result.get_by_role("button", name="Rehearse again")).to_be_visible()
    after, runs_after = tracker_rows(live_server)
    assert after == before, "a rehearsal wrote nothing to the tracker"
    assert runs_after == runs_before + 1


def test_the_result_comes_back_when_the_role_is_opened_again(apply_ready, owner_page, live_server):
    section = finished_rehearsal(owner_page, live_server)
    expect(section.get_by_role("button", name="Right")).to_be_visible()
    section.get_by_role("button", name="Right").click()
    mark = section.locator(".apply-review-mark")
    expect(mark).to_have_text("You marked this rehearsal right.")
    with db(live_server) as conn:
        assert conn.execute("SELECT review FROM apply_runs").fetchone()[0] == "right"
    # The role's address is a deep link: the page opens the role again by itself, so there is nothing to click (its scrim would cover it).
    owner_page.reload()
    owner_page.wait_for_selector("#detail-panel.is-open")
    section = owner_page.locator(".apply-for-me")
    expect(section).to_be_visible()
    expect(section.locator(".apply-result .apply-result-title")).to_contain_text("Here is what the app would send to")
    expect(section.locator(".apply-review-mark")).to_have_text("You marked this rehearsal right.")
    expect(section.get_by_role("button", name="Rehearse again")).to_be_visible()


def test_something_is_wrong_takes_a_note_and_marks_the_rehearsal_wrong(apply_ready, owner_page, live_server):
    section = finished_rehearsal(owner_page, live_server)
    expect(section.locator(".apply-review-note")).to_be_hidden()
    section.get_by_role("button", name="Something's wrong").click()
    note = section.get_by_label("What was wrong? (optional)")
    expect(note).to_be_focused()
    expect(note).to_have_attribute("maxlength", "500")
    note.fill("It put my name in the wrong box")
    section.get_by_role("button", name="Send").click()
    expect(section.locator(".apply-review-mark")).to_have_text("You marked this rehearsal wrong.")
    expect(section.locator(".apply-review")).to_contain_text("It put my name in the wrong box")
    with db(live_server) as conn:
        assert tuple(conn.execute("SELECT review, review_note FROM apply_runs").fetchone()) == ("wrong", "It put my name in the wrong box")


def test_stop_ends_a_rehearsal_that_is_running(apply_ready, owner_page, live_server, canned_agent):
    canned_agent["hang"] = True
    section = open_section(owner_page)
    rehearse(section)
    stop = section.locator(".apply-run").get_by_role("button", name="Stop")
    expect(stop).to_be_visible()
    stop.click()
    expect(stop).to_have_text("Stopping…")
    result = section.locator(".apply-result")
    expect(result.locator(".apply-result-title")).to_contain_text("You stopped this run", timeout=20_000)
    expect(section.locator(".apply-run")).to_have_count(0)
    # A stopped run is not a rehearsal to mark, and the student can go again.
    expect(section.get_by_role("button", name="Right")).to_have_count(0)
    expect(result.get_by_role("button", name="Rehearse again")).to_be_visible()


def test_a_rehearsal_still_running_is_picked_up_when_the_role_is_opened_again(apply_ready, owner_page, live_server, canned_agent):
    canned_agent["step_delay"] = 1.0
    section = open_section(owner_page)
    rehearse(section)
    expect(section.locator(".apply-run")).to_be_visible()
    owner_page.locator("#detail-close").click()
    section = open_section(owner_page)
    expect(section.locator(".apply-run, .apply-result").first).to_be_visible()
    expect(section.locator(".apply-result")).to_be_visible(timeout=20_000)


@pytest.mark.allow_page_errors
def test_a_refused_start_says_why_in_the_block_and_starts_nothing(apply_ready, owner_page, live_server, canned_agent):
    canned_agent["hang"] = True
    section = open_section(owner_page)
    confirm_posting(section)
    # The same role open on a second page of the same app, its Rehearse button pressed after the first page took the one slot.
    other = owner_page.context.new_page()
    try:
        other.goto(owner_page.url)
        other.wait_for_selector("#detail-panel.is-open")
        other_section = other.locator(".apply-for-me")
        expect(other_section).to_be_visible()
        confirm_posting(other_section)
        starter = other_section.get_by_role("button", name="Rehearse in a window")
        expect(starter).to_be_visible()
        section.get_by_role("button", name="Rehearse in a window").click()
        expect(section.locator(".apply-run")).to_be_visible()
        starter.click()
        expect(other_section.locator(".apply-rehearse-start .form-status")).to_have_text("Another application is being filled. Wait for it to finish.")
        assert tracker_rows(live_server)[1] == 1, "the refused start wrote no row"
    finally:
        other.close()
    section.locator(".apply-run").get_by_role("button", name="Stop").click()
    expect(section.locator(".apply-result")).to_be_visible(timeout=20_000)


@pytest.mark.allow_page_errors
def test_a_form_that_does_not_look_like_the_saved_role_waits_for_the_students_tick_before_a_rehearsal(apply_ready, owner_page, live_server):
    section = open_section(owner_page)
    expect(section.locator(".apply-mismatch")).to_contain_text("This may not be your role")
    section.get_by_role("button", name="Rehearse in a window").click()
    status = section.locator(".apply-rehearse-start .form-status")
    expect(status).to_contain_text("Check the posting first. Greenhouse's form is for Robotics Software Intern at Example Robotics, not Acme Robotics")
    expect(section.locator(".apply-run")).to_have_count(0)
    assert tracker_rows(live_server)[1] == 0, "nothing was started"
    # With the tick, the same button starts it.
    section.get_by_label("This is the right posting").check()
    section.get_by_role("button", name="Rehearse in a window").click()
    expect(section.locator(".apply-result")).to_be_visible(timeout=20_000)


def csrf(page):
    return next((cookie["value"] for cookie in page.context.cookies() if cookie["name"] == "pipeline_csrf"), "")


def location_problem(live_server):
    """The check with one more question on it: a required Location, a list only Greenhouse knows (it is optional on the fictional form)."""
    return {
        "key": "location_city", "question": "Location (City)", "required": True,
        "message": "The form asks for this from a list the app has not seen you confirm.",
        "action": {"type": "ats_label", "field": "location", "suggestion": ""},
    }


def check_with_location_problem(page, live_server):
    """The page's check answers carry the Location question until a Location option is saved."""
    saved = {"label": False}

    def answer(route):
        response = route.fetch()
        body = response.json()
        if not saved["label"] and body.get("problems") is not None:
            body["problems"] = [*body["problems"], location_problem(live_server)]
        route.fulfill(response=response, json=body)

    page.route(re.compile(rf".*{API}/opportunities/[^/]+/check$"), answer)
    page.on("response", lambda response: saved.update(label=True) if "/ats-labels/location" in response.url and response.request.method == "PUT" and response.ok else None)


def test_look_up_options_offers_what_greenhouse_lists_and_saves_the_one_picked(apply_ready, owner_page, live_server):
    check_with_location_problem(owner_page, live_server)
    section = open_section(owner_page)
    row = section.locator('[data-apply-key="location_city"]')
    expect(row).to_contain_text("Location (City)")
    expect(row).to_contain_text("This sends what you typed to Greenhouse's lookup service.")
    find = row.get_by_role("button", name="Look up options")
    find.click()
    expect(row.locator(".apply-lookup-status")).to_have_text("Type a few letters of the option first.")
    row.get_by_label("Exact option, as the form lists it").fill("Spring")
    find.click()
    options = row.locator('fieldset.apply-options input[type="radio"]')
    expect(options).to_have_count(len(apply_fake_ats.LOOKUP_OPTIONS), timeout=20_000)
    expect(row.locator("fieldset.apply-options legend")).to_have_text("Options the form lists")
    expect(row.locator(".apply-lookup-status")).to_contain_text("Greenhouse listed 2 options for what you typed. Pick the one that is yours.")
    # Nothing is saved until one is picked and confirmed.
    row.get_by_role("button", name="Use this one from now on").click()
    expect(row.locator(".apply-lookup-status")).to_have_text("Pick one of the options first.")
    with db(live_server) as conn:
        assert conn.execute("SELECT COUNT(*) FROM apply_ats_labels").fetchone()[0] == 0
    picked = apply_fake_ats.LOOKUP_OPTIONS[1]
    row.get_by_label(picked).check()
    row.get_by_role("button", name="Use this one from now on").click()
    expect(section.locator(".apply-summary")).to_contain_text("Saved.")
    expect(section.locator('[data-apply-key="location_city"]')).to_have_count(0)
    with db(live_server) as conn:
        assert conn.execute("SELECT label FROM apply_ats_labels WHERE field='location'").fetchone()[0] == picked


def test_a_lookup_that_finds_nothing_says_so_and_offers_no_options(apply_ready, owner_page, live_server):
    check_with_location_problem(owner_page, live_server)
    section = open_section(owner_page)
    row = section.locator('[data-apply-key="location_city"]')
    row.get_by_label("Exact option, as the form lists it").fill("zzzz")
    row.get_by_role("button", name="Look up options").click()
    expect(row.locator(".apply-lookup-status")).to_have_text(
        "No options came back for what you typed. Try fewer letters, or check the spelling.", timeout=20_000)
    expect(row.locator("fieldset.apply-options")).to_have_count(0)


@pytest.mark.allow_page_errors
def test_signing_out_ends_the_polling_without_touching_the_sign_in_gate(apply_ready, owner_page, live_server, canned_agent):
    canned_agent["step_delay"] = 1.0
    section = open_section(owner_page)
    rehearse(section)
    expect(section.locator(".apply-run")).to_be_visible()
    with owner_page.expect_response(lambda response: response.url.endswith("/api/v1/session") and response.request.method == "DELETE") as signed_out:
        # The open role's scrim covers the button, so it is pressed from the page: the student's session ends while the role is open.
        owner_page.evaluate("() => document.getElementById('logout-button').click()")
    assert signed_out.value.ok
    owner_page.wait_for_selector("#auth-gate.is-visible")
    owner_page.fill("#token-input", "not-the-token")
    owner_page.click("#auth-submit")
    expect(owner_page.locator("#auth-error")).to_have_text("That token was not accepted.")
    owner_page.focus("#token-input")
    polled = []
    owner_page.on("request", lambda request: polled.append(request.url) if "/apply-agent/runs/" in request.url else None)
    owner_page.wait_for_timeout(3500)
    assert polled == [], f"the poll kept going after sign-out: {polled}"
    expect(owner_page.locator("#auth-error")).to_have_text("That token was not accepted.")
    assert owner_page.evaluate("document.activeElement && document.activeElement.id") == "token-input"


@pytest.mark.parametrize("width", (1280, 390))
def test_the_result_is_accessible_and_does_not_overflow_the_page(apply_ready, owner_page, live_server, width):
    owner_page.set_viewport_size({"width": width, "height": 900})
    section = finished_rehearsal(owner_page, live_server)
    owner_page.wait_for_function("() => { const i = document.querySelector('.apply-shot img'); return Boolean(i && i.complete && i.naturalWidth > 0); }")
    violations = Axe().run(owner_page, context=".apply-for-me", options=AXE_OPTIONS).get("violations", [])
    assert not violations, [(item["id"], item["help"]) for item in violations]
    assert owner_page.evaluate("() => document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    expect(section.locator(".apply-result")).to_be_visible()


def test_the_running_state_is_accessible(apply_ready, owner_page, live_server, canned_agent):
    canned_agent["step_delay"] = 1.5
    section = open_section(owner_page)
    rehearse(section)
    expect(section.locator(".apply-run")).to_be_visible()
    violations = Axe().run(owner_page, context=".apply-for-me", options=AXE_OPTIONS).get("violations", [])
    assert not violations, [(item["id"], item["help"]) for item in violations]
    expect(section.locator(".apply-result")).to_be_visible(timeout=30_000)
