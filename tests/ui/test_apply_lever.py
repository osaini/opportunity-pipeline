"""Apply for me's "what's missing" view on a saved Lever role (docs/phase5-lever-handoff-spec.md, milestone LV2), and its settings.

The server runs in this process with the fictional Lever page (tests/fixtures/apply/lever/) served by a fake page client, so nothing here
reaches Lever and no browser is opened by the app. While Lever's window is not connected (``adapter_built`` False, which a fixture here sets for the
whole test, as it was before milestone LV4) the view says there is no window action, and has no button for one; the view with the window is
tests/ui/test_apply_lever_handoff.py.
"""

from __future__ import annotations

import dataclasses

import httpx
from opportunity_app.apply import ats as apply_ats, preflight as apply_preflight
from axe_core_python.sync_playwright import Axe
from playwright.sync_api import expect

import apply_fake_ats
import pytest
from apply_fake_ats import LEVER_COMPANY, LEVER_ROLE_ID, LEVER_URL
from conftest import OWNER_TOKEN, wait_for_results
from ui_helpers import AXE_OPTIONS, db, open_saved_role

BEARER = {"Authorization": f"Bearer {OWNER_TOKEN}"}


@pytest.fixture
def window_not_built(monkeypatch):
    """Lever's Finish in browser does not count as built, in this process: the view is the read-only one."""
    unbuilt = tuple(dataclasses.replace(spec, adapter_built=False) if spec.key == apply_ats.LEVER.key else spec for spec in apply_ats.REGISTRY)
    monkeypatch.setattr(apply_ats, "REGISTRY", unbuilt)


def tracker_rows(live_server):
    with db(live_server) as conn:
        return (
            [tuple(row) for row in conn.execute("SELECT opportunity_id, stage FROM applications ORDER BY opportunity_id")],
            conn.execute("SELECT COUNT(*) FROM opportunity_interactions").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM apply_runs").fetchone()[0],
            conn.execute("SELECT COUNT(*) FROM application_submit_claims").fetchone()[0],
        )


def test_a_saved_lever_role_shows_what_is_missing_and_offers_no_window_action(window_not_built, lever_ready, owner_page, live_server):
    before = tracker_rows(live_server)
    open_saved_role(owner_page, LEVER_COMPANY)
    section = owner_page.locator(".apply-for-me")
    expect(section).to_be_visible()
    # The student here has no phone number confirmed, and Lever's form asks for one; the current company is the app's to leave.
    expect(section.locator(".apply-summary")).to_have_text("1 question needs an answer first. 1 more is left for you to answer on the Lever form")
    phone = section.locator('[data-apply-key="phone"]')
    expect(phone.locator("strong")).to_have_text("Phone")
    expect(phone).to_contain_text("Add your phone number to your profile")
    expect(phone.get_by_role("button", name="Open your profile")).to_be_visible()
    source = section.locator(".apply-source")
    expect(source).to_contain_text("Read from Harbor Demo Labs - Customer Success Lead on Lever")
    expect(source.locator("a")).to_have_attribute("href", f"{LEVER_URL}/apply")
    expect(section.locator(".apply-mismatch")).to_have_count(0)
    # What the app cannot fill is the student's to type in the window, named with the reason.
    expect(section.locator(".apply-group")).to_have_text("Left for you: the app leaves these to you on the Lever form.")
    company = section.locator('[data-apply-key="org"]')
    expect(company.locator("strong")).to_have_text("Current company")
    expect(company).to_contain_text("The app has no source for your current company. Type it on Lever's application page")
    expect(company.get_by_role("link", name="Open the posting on Lever")).to_have_attribute("href", f"{LEVER_URL}/apply")
    expect(company.locator("textarea, select, input, button")).to_have_count(0)
    # The résumé is the student's to attach, and the sentence says why.
    expect(section.locator(".apply-ats-note")).to_contain_text("you attach it yourself on Lever's application page")
    expect(section.get_by_text("in the window")).to_have_count(0)
    expect(section.locator(".apply-ats-note")).to_contain_text("Lever reads it as soon as it is attached")
    # Plainly: nothing can be started on Lever yet, and there is no button that looks as if it could.
    expect(section.locator("[data-apply-not-offered]")).to_have_text("Finish in browser for Lever postings is not available yet")
    for name in ("Rehearse in a window", "Finish in browser", "Look up options", "Submit application", "Submit"):
        expect(section.get_by_role("button", name=name)).to_have_count(0)
    expect(section.locator(".apply-note")).to_have_text("This only reads the form. Opening it changes nothing in your tracker, and nothing is filled or sent.")
    # What the app would do with each field: the disability question and its signature are left whole to the student.
    section.locator(".apply-fields > summary").click()
    expect(section.locator(".apply-fields")).to_contain_text("Disability status (optional): Answering this makes Lever ask for a typed signature and a date")
    expect(section.locator(".apply-fields")).to_contain_text("Pronouns (optional): The app doesn't answer this kind of question for you")
    expect(section.locator(".apply-fields")).to_contain_text("Full name: from profile")
    violations = Axe().run(owner_page, context=".apply-for-me", options=AXE_OPTIONS).get("violations", [])
    assert not violations, [(item["id"], item["help"]) for item in violations]
    assert tracker_rows(live_server) == before, "opening the section wrote nothing: no application, no interaction, no run, no claim"


@pytest.fixture
def required_location(lever_ready, live_server, monkeypatch):
    """The Lever page whose current location is required (the student has no Lever option for it yet), for a role that names it.

    The server keeps a page for an hour per posting, so the cache is switched off here: an earlier test's page would be served again.
    """
    monkeypatch.setattr(apply_preflight.SchemaCache, "get", lambda self, key: None)
    monkeypatch.setattr(apply_preflight.SchemaCache, "put", lambda self, key, listing: None)
    original = dict(apply_fake_ats.CANNED)
    apply_fake_ats.CANNED["lever_page"] = "many_cards.html"
    with db(live_server) as conn, conn:
        conn.execute("UPDATE opportunities SET company='Orbital Ledger', title='Operations Associate' WHERE id=?", (LEVER_ROLE_ID,))
    yield
    apply_fake_ats.CANNED.clear()
    apply_fake_ats.CANNED.update(original)


def test_a_required_lever_location_is_saved_for_lever_and_never_looked_up(required_location, owner_page, live_server):
    open_saved_role(owner_page, "Orbital Ledger")
    section = owner_page.locator(".apply-for-me")
    expect(section).to_be_visible()
    location = section.locator('[data-apply-key="location"]')
    expect(location).to_contain_text("Choose your current location")
    # Finish in browser is offered here, so the posting is not offered as a second way to fill and send the form outside the app.
    expect(location.get_by_role("link", name="Open the posting on Lever")).to_have_count(0)
    # Lever's list is only read from its own form, which comes later: the type-it-yourself box has no Look up options beside it.
    expect(location.get_by_role("button", name="Save this option")).to_be_visible()
    expect(location.get_by_role("button", name="Look up options")).to_have_count(0)
    expect(section.get_by_role("button", name="Look up options")).to_have_count(0)
    location.get_by_label("Exact option, as the form lists it").fill("Austin, Texas, United States")
    location.get_by_role("button", name="Save this option").click()
    expect(section.locator('[data-apply-key="location"]').get_by_text("Choose your current location")).to_have_count(0)
    with db(live_server) as conn:
        assert [tuple(row) for row in conn.execute("SELECT ats, field, label FROM apply_ats_labels")] == [("lever", "location", "Austin, Texas, United States")]


def test_the_greenhouse_role_beside_it_still_offers_both_window_actions(lever_ready, owner_page):
    open_saved_role(owner_page)
    section = owner_page.locator(".apply-for-me")
    expect(section).to_be_visible()
    expect(section.get_by_role("button", name="Rehearse in a window")).to_be_visible()
    expect(section.get_by_role("button", name="Finish in browser")).to_be_visible()
    expect(section.locator("[data-apply-not-offered]")).to_have_count(0)
    expect(section.locator(".apply-note")).to_contain_text("A rehearsal changes nothing in your tracker")


def test_with_the_lever_switch_off_the_role_says_how_to_turn_it_on(lever_ready, owner_page, base_url):
    response = httpx.put(f"{base_url}/api/v1/automation/settings", headers=BEARER, json={"modes": {"apply_agent_lever": "off"}})
    assert response.status_code == 200, response.text
    owner_page.reload()
    wait_for_results(owner_page)
    open_saved_role(owner_page, LEVER_COMPANY)
    section = owner_page.locator(".apply-for-me")
    expect(section.locator(".apply-summary")).to_have_text("Apply for me on Lever is off. Turn it on in Profile, under Automation, in the Applications list")
    expect(section.get_by_text("Turn it on in Profile")).to_have_count(1)
    expect(section.get_by_role("button")).to_have_count(0)


def test_the_settings_keep_levers_exact_location_apart_and_say_what_the_two_switches_do(lever_ready, owner_page, live_server):
    owner_page.click("#profile-nav")
    wait_for_results(owner_page)
    block = owner_page.locator(".automation-apply-agent")
    expect(block.get_by_role("heading", name="Exact options for lists the Greenhouse form owns")).to_be_visible()
    expect(block.get_by_role("heading", name="Exact options for lists the Lever form owns")).to_be_visible()
    lever = block.locator('form[data-ats="lever"]')
    expect(lever.get_by_label("List").locator("option")).to_have_text(["Location"])
    lever.get_by_label("Exact option").fill("Austin, Texas, United States")
    lever.get_by_role("button", name="Save this option").click()
    expect(block.locator('ul[data-ats="lever"]')).to_contain_text("Location: Austin, Texas, United States")
    with db(live_server) as conn:
        assert [tuple(row) for row in conn.execute("SELECT ats, field, label FROM apply_ats_labels")] == [("lever", "location", "Austin, Texas, United States")]
    expect(block.locator('ul[data-ats="greenhouse"]')).not_to_contain_text("Austin")
    expect(block.locator(".apply-lever-settings")).to_contain_text("Apply for me on Lever is on.")
    expect(block.locator(".apply-lever-settings")).to_contain_text(
        "Let the app attach my résumé on Lever is off. Lever reads a résumé as soon as it is attached, so it is sent to Lever before you press Submit. "
        "With this off, you attach it yourself in the window."
    )
    block.get_by_role("button", name="Remove the saved Lever Location option").click()
    expect(block.locator('ul[data-ats="lever"] li')).to_have_count(0)
    with db(live_server) as conn:
        assert conn.execute("SELECT COUNT(*) FROM apply_ats_labels").fetchone()[0] == 0
    violations = Axe().run(owner_page, context=".automation-apply-agent", options=AXE_OPTIONS).get("violations", [])
    assert not violations, [(item["id"], item["help"]) for item in violations]


def test_each_list_in_the_settings_says_saved_under_its_own_form(lever_ready, owner_page):
    owner_page.click("#profile-nav")
    wait_for_results(owner_page)
    block = owner_page.locator(".automation-apply-agent")
    expect(block.locator('form[data-ats="lever"]')).to_be_visible()
    status = lambda ats: block.locator(f'form[data-ats="{ats}"] + .form-status')
    # An empty box on the Greenhouse form says so under the Greenhouse form, and nothing appears under the Lever one.
    block.locator('form[data-ats="greenhouse"]').get_by_role("button", name="Save this option").click()
    expect(status("greenhouse")).to_have_text("Type the option first.")
    expect(status("lever")).to_have_text("")
    block.locator('form[data-ats="lever"]').get_by_role("button", name="Save this option").click()
    expect(status("lever")).to_have_text("Type the option first.")
    expect(status("greenhouse")).to_have_text("Type the option first.")
    assert block.locator(".apply-settings > .form-status").count() == 2, "one status per list, never one shared"


def _status(block, ats):
    return block.locator(f'form[data-ats="{ats}"] + .form-status')


def test_a_saved_or_removed_option_says_so_under_its_own_form_after_the_block_repaints(lever_ready, owner_page):
    owner_page.click("#profile-nav")
    wait_for_results(owner_page)
    block = owner_page.locator(".automation-apply-agent")
    lever = block.locator('form[data-ats="lever"]')
    lever.get_by_label("Exact option").fill("Austin, Texas, United States")
    lever.get_by_role("button", name="Save this option").click()
    expect(block.locator('ul[data-ats="lever"]')).to_contain_text("Location: Austin")
    # Saving repaints the whole block; the message must be in the element that is on the page afterwards.
    expect(_status(block, "lever")).to_have_text("Saved.")
    expect(_status(block, "greenhouse")).to_have_text("")
    expect(_status(block, "lever")).to_have_attribute("role", "status")
    block.get_by_role("button", name="Remove the saved Lever Location option").click()
    expect(block.locator('ul[data-ats="lever"] li')).to_have_count(0)
    expect(_status(block, "lever")).to_have_text("Removed.")


def test_a_saved_option_says_so_when_greenhouse_is_the_only_list(apply_ready, owner_page):
    owner_page.click("#profile-nav")
    wait_for_results(owner_page)
    block = owner_page.locator(".automation-apply-agent")
    expect(block.locator("form.apply-answer-form")).to_have_count(1)
    form = block.locator("form.apply-answer-form")
    form.get_by_label("List").select_option("school")
    form.get_by_label("Exact option").fill("The University of Example at City")
    form.get_by_role("button", name="Save this option").click()
    expect(block.locator(".apply-labels")).to_contain_text("The University of Example at City")
    expect(block.locator(".apply-settings > .form-status")).to_have_text("Saved.")
    block.get_by_role("button", name="Remove the saved School option").click()
    expect(block.locator(".apply-labels li")).to_have_count(0)
    expect(block.locator(".apply-settings > .form-status")).to_have_text("Removed.")


def test_the_resume_line_says_the_switch_does_nothing_yet_whichever_way_it_is_set(window_not_built, lever_ready, owner_page):
    owner_page.click("#profile-nav")
    wait_for_results(owner_page)
    block = owner_page.locator(".automation-apply-agent")
    resume = owner_page.locator("#automation-mode-apply_lever_resume_upload")
    resume.check()
    line = block.locator(".apply-lever-settings li").nth(1)
    expect(line).to_contain_text("is on.")
    expect(line).to_contain_text("The app cannot attach it on Lever yet")
    expect(line).to_contain_text("you attach it yourself on Lever's application page")
    expect(block).not_to_contain_text("Apply agent settings")


def test_with_the_window_built_the_resume_line_says_the_app_attaches_it_in_the_window_when_the_switch_is_on(lever_ready, owner_page):
    owner_page.click("#profile-nav")
    wait_for_results(owner_page)
    block = owner_page.locator(".automation-apply-agent")
    owner_page.locator("#automation-mode-apply_lever_resume_upload").check()
    line = block.locator(".apply-lever-settings li").nth(1)
    expect(line).to_contain_text("is on.")
    expect(line).to_contain_text("it is sent to Lever before you press Submit")
    expect(line).to_contain_text("you attach it yourself in the window")
    expect(line).not_to_contain_text("cannot attach it on Lever yet")
    expect(block.locator(".apply-lever-settings li").nth(0)).to_contain_text("Finish in browser opens its form in a window")


def test_the_lever_lines_in_the_settings_follow_their_switches_without_a_reload(window_not_built, lever_ready, owner_page):
    owner_page.click("#profile-nav")
    wait_for_results(owner_page)
    block = owner_page.locator(".automation-apply-agent")
    lines = block.locator(".apply-lever-settings")
    expect(lines).to_contain_text("Let the app attach my résumé on Lever is off.")
    # The switch is in the Applications group above the block; turning it on repaints the sentence at once.
    resume = owner_page.locator("#automation-mode-apply_lever_resume_upload")
    resume.check()
    expect(lines).to_contain_text("Let the app attach my résumé on Lever is on.")
    expect(lines).not_to_contain_text("Let the app attach my résumé on Lever is off.")
    resume.uncheck()
    expect(lines).to_contain_text("Let the app attach my résumé on Lever is off.")
    # What the block says about the switches is true whichever way they are set, and the switches are where it says.
    expect(block).to_contain_text("Both are switches under Applications, above. Each is off until you turn it on.")
    group = resume.locator("xpath=ancestor::div[contains(@class, 'automation-group')]")
    expect(group.locator("h4")).to_have_text("Applications")
    expect(block).not_to_contain_text("Apply for me list")
    expect(block).not_to_contain_text("in the window")


def test_the_lever_list_help_names_only_lists_lever_has(lever_ready, owner_page):
    owner_page.click("#profile-nav")
    wait_for_results(owner_page)
    block = owner_page.locator(".automation-apply-agent")
    expect(block.get_by_role("heading", name="Exact options for lists the Lever form owns").locator("xpath=following-sibling::p[1]")).to_have_text(
        "On the Lever form, location is a list whose wording only the form knows. Save the exact option once and the app uses it word for word."
    )
    expect(block.get_by_role("heading", name="Exact options for lists the Greenhouse form owns").locator("xpath=following-sibling::p[1]")).to_contain_text("such as school and location")


def test_every_left_for_you_row_says_where_to_do_it_and_links_to_the_posting(window_not_built, required_location, owner_page):
    open_saved_role(owner_page, "Orbital Ledger")
    section = owner_page.locator(".apply-for-me")
    expect(section).to_be_visible()
    # Finish in browser is not there for Lever, so no row sends the student to it; each one says Lever's own page, and links to it.
    expect(section.get_by_text("Finish in browser leaves")).to_have_count(0)
    manual = section.locator(".apply-problem", has=owner_page.get_by_text("Do it on Lever's application page"))
    assert manual.count() >= 2, "the demo page has several questions the app never answers (sponsorship, consent, language skills)"
    for index in range(manual.count()):
        expect(manual.nth(index).get_by_role("link", name="Open the posting on Lever")).to_have_attribute("href", f"{LEVER_URL}/apply")
    # The résumé is explained once, in the note, and its row is only the instruction.
    resume = section.locator('[data-apply-key="resume"]')
    expect(resume).to_contain_text("Attach your résumé on Lever's application page")
    expect(resume).not_to_contain_text("The app doesn't attach it")
    expect(section.locator(".apply-ats-note")).to_have_text(
        "Your résumé: you attach it yourself on Lever's application page, because Lever reads it as soon as it is attached."
    )


def test_the_not_offered_line_and_the_resume_note_have_room_around_them(window_not_built, lever_ready, owner_page):
    open_saved_role(owner_page, LEVER_COMPANY)
    section = owner_page.locator(".apply-for-me")
    expect(section.locator("[data-apply-not-offered]")).to_be_visible()
    margin = lambda selector, side: section.locator(selector).first.evaluate(f"(node) => parseFloat(getComputedStyle(node).{side})")
    assert margin("[data-apply-not-offered]", "marginBottom") == 0, "the block's own gap spaces it, not an extra margin"
    assert margin(".apply-ats-note", "marginBottom") >= 8, "the note does not sit on the first card's border"
    note, cards = section.locator(".apply-ats-note").bounding_box(), section.locator(".apply-problems").first.bounding_box()
    assert cards["y"] - (note["y"] + note["height"]) >= 8


def test_the_settings_follow_the_lever_switch_without_a_reload(lever_ready, owner_page):
    owner_page.click("#profile-nav")
    wait_for_results(owner_page)
    block = owner_page.locator(".automation-apply-agent")
    lever_lists = block.get_by_role("heading", name="Exact options for lists the Lever form owns")
    expect(lever_lists).to_be_visible()
    owner_page.locator("#automation-mode-apply_agent_lever").uncheck()
    expect(lever_lists).to_have_count(0)
    expect(block.locator('form[data-ats="lever"]')).to_have_count(0)
    owner_page.locator("#automation-mode-apply_agent_lever").check()
    expect(lever_lists).to_be_visible()
    expect(block.locator('form[data-ats="lever"]')).to_be_visible()


def test_the_resume_line_in_the_settings_is_worded_for_the_state_of_the_lever_switch(lever_ready, owner_page):
    owner_page.click("#profile-nav")
    wait_for_results(owner_page)
    block = owner_page.locator(".automation-apply-agent")
    line = block.locator(".apply-lever-settings li").nth(1)
    expect(line).to_contain_text("you attach it yourself in the window")
    owner_page.locator("#automation-mode-apply_agent_lever").uncheck()
    # With the Lever switch off there is no window: the line says when the choice starts to matter instead of sending the student to one.
    expect(block.locator(".apply-lever-settings li").nth(0)).to_contain_text("Apply for me on Lever is off.")
    expect(line).to_contain_text("It takes effect once Apply for me on Lever is on")
    expect(line).not_to_contain_text("With this off, you attach it yourself in the window.")
    owner_page.locator("#automation-mode-apply_agent_lever").check()
    expect(line).to_contain_text("With this off, you attach it yourself in the window.")
