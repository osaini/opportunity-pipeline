"""Finish in browser on a saved Lever role, in the browser (docs/phase5-lever-handoff-spec.md, milestone LV4): the start, the student's turn, the result.

The server runs in this process with the fictional Lever page (tests/fixtures/apply/lever/) and an agent that opens no browser
(tests/apply_fake_ats.py): a Finish in browser run answers with a canned handoff, the student's own press of Submit application is the
agent's pause of ``CANNED["handoff"]["wait"]`` seconds, and nothing reaches Lever. Lever's window is not built yet, so a fixture here marks
it built in this process, the way the sandbox does (scripts/serve_for_testing.py), and puts everything back after each test. Every
company, site and posting is fictional.
"""

from __future__ import annotations

import dataclasses
import time

import httpx
import pytest
from axe_core_python.sync_playwright import Axe
from playwright.sync_api import expect

import apply_fake_ats
from apply_fake_ats import LEVER_COMPANY
from conftest import OWNER_TOKEN, wait_for_results
from opportunity_app.apply import ats as apply_ats
from ui_helpers import AXE_OPTIONS, db, open_saved_role

BEARER = {"Authorization": f"Bearer {OWNER_TOKEN}"}
API = "/api/v1/apply-agent"
IDLE_WITHIN_S = 60
BY_APP = "The app will attach your résumé. Lever reads it as soon as it is attached, so it is sent to Lever before you press Submit."
BY_YOU = "Your résumé is left for you: attach it in the window. Lever reads it as soon as it is attached, so it is sent to Lever before you press Submit."
SENT = "Your résumé was sent to Lever when it was attached."
NOT_SENT_LEVER_HAS_IT = "You didn't submit it in the window. Your application was not sent. Lever received your résumé."
NOT_SENT = "You didn't submit it in the window. Your application was not sent."


@pytest.fixture
def lever_window(monkeypatch):
    """Lever's Finish in browser counts as built, in this process (the fake agent answers for the driver)."""
    built = tuple(dataclasses.replace(spec, adapter_built=True) if spec.key == apply_ats.LEVER.key else spec for spec in apply_ats.REGISTRY)
    monkeypatch.setattr(apply_ats, "REGISTRY", built)


@pytest.fixture
def canned_agent(lever_window, lever_ready, live_server, base_url):
    """The canned agent's knobs back to what they were, and the app's run slot free, before the next test rewinds the database."""
    original = dict(apply_fake_ats.CANNED)
    yield apply_fake_ats.CANNED
    apply_fake_ats.CANNED.clear()
    apply_fake_ats.CANNED.update(original)
    deadline = time.monotonic() + IDLE_WITHIN_S
    while time.monotonic() < deadline:
        try:
            listed = httpx.get(f"{base_url}{API}/opportunities/{apply_fake_ats.LEVER_ROLE_ID}/runs", headers=BEARER)
        except Exception:
            return
        if listed.status_code != 200 or not listed.json().get("busy"):
            return
        time.sleep(0.2)
    pytest.fail("a run was still going after the test ended")


def handoff(canned, *, wait=6.0, outcome="submitted"):
    # A new dict each time: the fixture restores the old one by reference.
    canned["handoff"] = {"wait": wait, "outcome": outcome}


def resume_upload(base_url, mode):
    response = httpx.put(f"{base_url}/api/v1/automation/settings", headers=BEARER, json={"modes": {"apply_lever_resume_upload": mode}})
    assert response.status_code == 200, response.text


def open_lever(page):
    open_saved_role(page, LEVER_COMPANY)
    section = page.locator(".apply-for-me")
    expect(section).to_be_visible()
    return section


def finish_button(section):
    return section.get_by_role("button", name="Finish in browser")


def tracker(live_server):
    with db(live_server) as conn:
        return [tuple(row) for row in conn.execute("SELECT opportunity_id, stage FROM applications ORDER BY opportunity_id")]


def test_a_lever_role_offers_finish_in_browser_as_its_only_action_and_says_what_happens_to_the_resume(canned_agent, owner_page):
    section = open_lever(owner_page)
    expect(finish_button(section)).to_be_visible()
    for name in ("Rehearse in a window", "Look up options", "Submit application", "Submit"):
        expect(section.get_by_role("button", name=name)).to_have_count(0)
    expect(section.locator("[data-apply-not-offered]")).to_have_count(0)
    # The setting starts off, so the résumé is the student's to attach, and the start says so before the window opens.
    expect(section.locator("[data-apply-resume-start]")).to_have_text(BY_YOU)
    expect(section.locator(".apply-handoff-start")).to_contain_text("You complete what is left and press Submit application yourself. Your application is not sent until you do.")
    expect(section.locator(".apply-note")).to_have_text(
        "Opening this only reads the form: it changes nothing in your tracker, and nothing is filled or sent. "
        "Finish in browser fills the form in a window, and you press Submit application yourself."
    )
    violations = Axe().run(owner_page, context=".apply-for-me", options=AXE_OPTIONS).get("violations", [])
    assert not violations, [(item["id"], item["help"]) for item in violations]


def test_with_the_resume_choice_on_the_start_says_the_app_attaches_it_and_it_goes_to_lever_before_submit(canned_agent, owner_page, base_url):
    resume_upload(base_url, "on")
    owner_page.reload()
    wait_for_results(owner_page)
    section = open_lever(owner_page)
    expect(section.locator("[data-apply-resume-start]")).to_have_text(BY_APP)
    expect(section.locator(".apply-ats-note")).to_contain_text("the app attaches it itself, because you let it in Apply agent settings")


def test_a_run_with_the_resume_choice_on_shows_the_resume_was_sent_and_a_stop_says_lever_received_it(canned_agent, owner_page, base_url, live_server):
    resume_upload(base_url, "on")
    owner_page.reload()
    wait_for_results(owner_page)
    handoff(canned_agent, wait=60.0)
    before = tracker(live_server)
    section = open_lever(owner_page)
    finish_button(section).click()
    turn = section.locator(".apply-turn")
    expect(turn).to_be_visible(timeout=30_000)
    expect(turn.locator(".apply-resume-sent")).to_have_text(SENT)
    expect(turn.locator(".apply-turn-until")).to_contain_text("if you haven't pressed Submit application.")
    # Nothing before the student's press says "nothing was sent": the file is already with Lever.
    assert "nothing was sent" not in turn.inner_text().lower()
    turn.get_by_role("button", name="Stop").click()
    result = section.locator(".apply-result")
    expect(result.locator(".apply-result-title")).to_have_text(NOT_SENT_LEVER_HAS_IT, timeout=30_000)
    expect(result.locator(".apply-resume-sent")).to_have_count(0)    # said once, in the title
    assert "Nothing was sent" not in result.inner_text()
    # The result offers Finish in browser again, and it still says what happens to the résumé.
    expect(result.get_by_role("button", name="Finish in browser")).to_be_visible()
    expect(result.locator("[data-apply-resume-start]")).to_have_text(BY_APP)
    with db(live_server) as conn:
        claim = conn.execute("SELECT ats, state, after_click, note FROM application_submit_claims").fetchone()
    assert (claim["ats"], claim["state"], claim["after_click"]) == ("lever", "needs_you", 0)
    assert claim["note"] == NOT_SENT_LEVER_HAS_IT
    assert [row for row in tracker(live_server) if row[1] == "applied"] == [row for row in before if row[1] == "applied"], "a stop moves nothing in the tracker"


def test_a_run_with_the_resume_choice_off_sends_none_and_a_stop_keeps_the_old_words(canned_agent, owner_page, live_server):
    handoff(canned_agent, wait=60.0)
    section = open_lever(owner_page)
    finish_button(section).click()
    turn = section.locator(".apply-turn")
    expect(turn).to_be_visible(timeout=30_000)
    expect(turn.locator(".apply-resume-sent")).to_be_hidden()
    turn.get_by_role("button", name="Stop").click()
    result = section.locator(".apply-result")
    expect(result.locator(".apply-result-title")).to_have_text(NOT_SENT, timeout=30_000)
    assert "résumé" not in result.locator(".apply-result-title").inner_text()


def test_a_submitted_lever_run_says_lever_showed_its_confirmation_and_that_the_resume_went_with_it(canned_agent, owner_page, base_url, live_server):
    resume_upload(base_url, "on")
    owner_page.reload()
    wait_for_results(owner_page)
    handoff(canned_agent, wait=1.0)
    section = open_lever(owner_page)
    finish_button(section).click()
    result = section.locator(".apply-result")
    expect(result.locator(".apply-result-title")).to_have_text("Lever showed its confirmation page. Mark as applied?", timeout=40_000)
    expect(result.locator(".apply-resume-sent")).to_have_text(SENT)
    mark = result.get_by_role("button", name="Mark as applied?")
    expect(mark).to_be_visible()
    with db(live_server) as conn:
        assert conn.execute("SELECT stage FROM applications WHERE opportunity_id=?", (apply_fake_ats.LEVER_ROLE_ID,)).fetchone()[0] == "applying", \
            "the app never moves the tracker by itself"
    mark.click()
    expect(result.get_by_role("button", name="Mark as applied?")).to_have_count(0, timeout=15_000)
    with db(live_server) as conn:
        assert conn.execute("SELECT stage FROM applications WHERE opportunity_id=?", (apply_fake_ats.LEVER_ROLE_ID,)).fetchone()[0] == "applied"
    violations = Axe().run(owner_page, context=".apply-for-me", options=AXE_OPTIONS).get("violations", [])
    assert not violations, [(item["id"], item["help"]) for item in violations]


def test_a_refused_lever_form_names_lever_and_the_resume_went_with_it(canned_agent, owner_page, base_url):
    resume_upload(base_url, "on")
    owner_page.reload()
    wait_for_results(owner_page)
    handoff(canned_agent, wait=1.0, outcome="failed_4xx")
    section = open_lever(owner_page)
    finish_button(section).click()
    result = section.locator(".apply-result")
    expect(result.locator(".apply-result-title")).to_contain_text("Lever refused the form (HTTP 422)", timeout=40_000)
    expect(result.locator(".apply-resume-sent")).to_have_text(SENT)
    expect(result).not_to_contain_text("Greenhouse")


def test_the_greenhouse_role_beside_it_has_no_resume_sentence(canned_agent, owner_page):
    open_saved_role(owner_page)
    section = owner_page.locator(".apply-for-me")
    expect(section).to_be_visible()
    expect(finish_button(section)).to_be_visible()
    expect(section.get_by_role("button", name="Rehearse in a window")).to_be_visible()
    expect(section.locator("[data-apply-resume-start]")).to_be_hidden()


def test_the_settings_say_the_window_is_there_once_it_is(canned_agent, owner_page):
    owner_page.click("#profile-nav")
    wait_for_results(owner_page)
    lever = owner_page.locator(".automation-apply-agent .apply-lever-settings")
    expect(lever).to_contain_text("and Finish in browser opens its form in a window for you to finish.")
    expect(lever).not_to_contain_text("is not available yet")
    expect(lever).to_contain_text("With this off, you attach it yourself in the window.")
