"""Apply for me's "what's missing" view on a saved Greenhouse role, its settings, and the name written on an application.

The server runs in this process with the fictional listing (tests/apply_fake_ats.py) and an agent that only says a
window could open, so nothing here reaches Greenhouse and no browser is opened by the app. A test seeds the live
database directly, the way the switches and uploads would.
"""

from __future__ import annotations

import json

import pytest
from axe_core_python.sync_playwright import Axe
from playwright.sync_api import expect

from apply_fake_ats import JOB_URL
from conftest import OWNER_TOKEN, wait_for_results
from opportunity_app.applications import actions
from ui_helpers import AXE_OPTIONS, USER, confirm_posting, db, fact, open_saved_role, prepare

BEARER = {"Authorization": f"Bearer {OWNER_TOKEN}"}


def tracker_rows(live_server):
    with db(live_server) as conn:
        return [tuple(row) for row in conn.execute("SELECT opportunity_id, stage FROM applications ORDER BY opportunity_id")], \
            conn.execute("SELECT COUNT(*) FROM opportunity_interactions").fetchone()[0], conn.execute("SELECT COUNT(*) FROM apply_runs").fetchone()[0]


def test_a_saved_greenhouse_role_shows_what_is_missing_and_changes_nothing_in_the_tracker(apply_ready, owner_page, live_server):
    before = tracker_rows(live_server)
    open_saved_role(owner_page)
    section = owner_page.locator(".apply-for-me")
    expect(section).to_be_visible()
    expect(section.locator(".apply-summary")).to_contain_text("3 questions need an answer first")
    expect(section.locator(".apply-summary")).to_contain_text("left for you to answer on the Greenhouse form")
    # Which posting was read is on screen, linked to it, and the form of another company is flagged.
    source = section.locator(".apply-source")
    expect(source).to_contain_text("Read from Robotics Software Intern at Example Robotics on Greenhouse")
    expect(source.locator("a")).to_have_attribute("href", JOB_URL)
    expect(section.locator(".apply-mismatch")).to_contain_text("This may not be your role")
    expect(section.locator('[data-apply-key="question_4000000101"] strong')).to_have_text("Why do you want to work at Example Robotics?")
    expect(section.locator('[data-apply-key="question_4000000103"] select option')).to_have_text(["Choose an option", "Perception", "Controls", "Firmware"])
    # A sensitive question is named, and gets no answer box: the app never answers these for the student.
    expect(section.locator(".apply-group")).to_contain_text("Left for you")
    sensitive = section.locator('[data-apply-key="question_4000000105"]')
    expect(sensitive).to_contain_text("The app doesn't answer this kind of question for you")
    expect(sensitive.locator("textarea, select, input")).to_have_count(0)
    expect(section.locator(".apply-note")).to_contain_text("A rehearsal changes nothing in your tracker")
    assert tracker_rows(live_server) == before, "opening the section wrote nothing"


def test_a_text_and_a_select_question_are_answered_once_and_the_answer_carries_over(apply_ready, owner_page, live_server):
    open_saved_role(owner_page)
    section = owner_page.locator(".apply-for-me")
    expect(section).to_be_visible()
    confirm_posting(section)
    text = section.locator('[data-apply-key="question_4000000101"]')
    text.locator("textarea").fill("I build small robot arms")
    text.get_by_role("button", name="Save and use for this question").click()
    expect(section.locator('[data-apply-key="question_4000000101"]')).to_have_count(0)
    expect(section.locator(".apply-summary")).to_contain_text("Saved for Acme Robotics.")
    team = section.locator('[data-apply-key="question_4000000103"]')
    team.locator("select").select_option("Controls")
    expect(team.get_by_label("Use for any company")).to_have_count(0)
    team.get_by_role("button", name="Save and use for this question").click()
    expect(section.locator('[data-apply-key="question_4000000103"]')).to_have_count(0)
    # The saved answers are id-free rows for this company, and neither is tagged reusable: Apply for me carries no answer to another company.
    with db(live_server) as conn:
        rows = {row["question"]: (row["answer"], row["company"], row["tags_json"]) for row in conn.execute("SELECT question, answer, company, tags_json FROM answer_library")}
    assert rows["Why do you want to work at Example Robotics?"] == ("I build small robot arms", "Acme Robotics", "[]")
    assert rows["Which team are you most interested in?"] == ("Controls", "Acme Robotics", "[]")
    # Closed and opened again, the two are no longer missing.
    owner_page.locator("#detail-close").click()
    open_saved_role(owner_page)
    expect(owner_page.locator(".apply-for-me")).to_be_visible()
    expect(owner_page.locator('.apply-for-me [data-apply-key="question_4000000101"]')).to_have_count(0)
    expect(owner_page.locator('.apply-for-me [data-apply-key="question_4000000103"]')).to_have_count(0)
    expect(owner_page.locator('.apply-for-me [data-apply-key="question_4000000111"]')).to_have_count(1)


def test_saving_one_answer_keeps_what_is_typed_into_the_others(apply_ready, owner_page, live_server):
    open_saved_role(owner_page)
    section = owner_page.locator(".apply-for-me")
    expect(section).to_be_visible()
    confirm_posting(section)
    essay = "A long essay I spent ten minutes writing about why I want this role."
    section.locator('[data-apply-key="question_4000000101"] textarea').fill(essay)
    team = section.locator('[data-apply-key="question_4000000103"]')
    team.locator("select").select_option("Controls")
    team.get_by_role("button", name="Save and use for this question").click()
    expect(section.locator('[data-apply-key="question_4000000103"]')).to_have_count(0)
    expect(section.locator(".apply-summary")).to_contain_text("Saved for Acme Robotics.")
    expect(section.locator('[data-apply-key="question_4000000101"] textarea')).to_have_value(essay)
    with db(live_server) as conn:
        assert conn.execute("SELECT COUNT(*) FROM answer_library WHERE question LIKE 'Why do you want%'").fetchone()[0] == 0, "the essay was kept on screen, not saved"


def test_the_profile_button_closes_the_role_and_lands_on_the_missing_email(apply_ready, owner_page, live_server):
    with db(live_server) as conn, conn:
        conn.execute("DELETE FROM profile_facts WHERE user_id=? AND field_path='contact'", (USER,))
    open_saved_role(owner_page)
    section = owner_page.locator(".apply-for-me")
    expect(section).to_be_visible()
    section.locator(".apply-problem", has_text="Add your email to your profile").get_by_role("button", name="Open your profile").click()
    expect(owner_page.locator("#detail-panel.is-open")).to_have_count(0)
    expect(owner_page.locator('.profile-form input[name="contact_email"]')).to_be_focused()
    assert owner_page.evaluate("() => window.location.pathname") == "/profile"


def test_the_profile_button_for_a_missing_name_lands_on_the_first_name_box(apply_ready, owner_page, live_server):
    with db(live_server) as conn, conn:
        # Half a name is not one the app will type into a form.
        fact(conn, "name_parts", {"first": "Ana", "last": "", "preferred": ""})
    open_saved_role(owner_page)
    section = owner_page.locator(".apply-for-me")
    expect(section).to_be_visible()
    section.get_by_role("button", name="Add your name for applications").click()
    expect(owner_page.locator("#detail-panel.is-open")).to_have_count(0)
    expect(owner_page.locator('.profile-form input[name="name_parts_first"]')).to_be_focused()


def test_the_profile_keeps_the_email_and_phone_an_application_is_filled_with(owner_page, live_server):
    from opportunity_app.student.profile import update_profile

    with db(live_server) as conn:
        update_profile(conn, {"contact": {"linkedin": "https://example.test/in/sam"}}, ["contact"], user_id=USER)
    owner_page.click("#profile-nav")
    wait_for_results(owner_page)
    owner_page.get_by_label("Email for applications").fill("sam.rivera@example.test")
    owner_page.get_by_label("Phone for applications (optional)").fill("555-0100")
    owner_page.get_by_role("button", name="Save and confirm profile").click()
    expect(owner_page.locator(".form-status", has_text="Profile saved and confirmed.")).to_be_visible()
    with db(live_server) as conn:
        stored = json.loads(conn.execute("SELECT value_json FROM profile_facts WHERE field_path='contact' AND confirmed=1").fetchone()[0])
    assert stored == {"linkedin": "https://example.test/in/sam", "email": "sam.rivera@example.test", "phone": "555-0100"}, "what a résumé saved there is kept"
    owner_page.reload()
    wait_for_results(owner_page)
    owner_page.click("#profile-nav")
    wait_for_results(owner_page)
    expect(owner_page.get_by_label("Email for applications")).to_have_value("sam.rivera@example.test")


def test_the_profile_keeps_a_mailing_address_for_contact_forms_and_lets_it_be_cleared(owner_page, live_server):
    from opportunity_app.student.profile import update_profile

    with db(live_server) as conn:
        update_profile(conn, {"contact": {"linkedin": "https://example.test/in/sam"}}, ["contact"], user_id=USER)
    owner_page.click("#profile-nav")
    wait_for_results(owner_page)
    fieldset = owner_page.locator(".profile-fieldset", has_text="Mailing address (optional)")
    expect(fieldset).to_contain_text("only then")
    expect(fieldset).to_contain_text("a form that requires only a country, state, city or ZIP gets only that")
    entries = {"Address line 1": "12 Example Lane", "City": "Riverton", "State or province": "Oregon", "ZIP or postal code": "97000",
               "Country": "United States"}
    for label, value in entries.items():
        fieldset.get_by_label(label, exact=True).fill(value)
    owner_page.get_by_role("button", name="Save and confirm profile").click()
    expect(owner_page.locator(".form-status", has_text="Profile saved and confirmed.")).to_be_visible()

    def stored():
        with db(live_server) as conn:
            row = conn.execute("SELECT value_json FROM profile_facts WHERE field_path='contact' AND confirmed=1").fetchone()
        return json.loads(row[0]) if row else None

    assert stored() == {"linkedin": "https://example.test/in/sam", "address_line1": "12 Example Lane", "city": "Riverton",
                        "state": "Oregon", "postal_code": "97000", "country": "United States"}
    owner_page.reload()
    wait_for_results(owner_page)
    owner_page.click("#profile-nav")
    wait_for_results(owner_page)
    fieldset = owner_page.locator(".profile-fieldset", has_text="Mailing address (optional)")
    for label, value in entries.items():
        expect(fieldset.get_by_label(label, exact=True)).to_have_value(value)
    for label in entries:
        fieldset.get_by_label(label, exact=True).fill("")
    owner_page.get_by_role("button", name="Save and confirm profile").click()
    expect(owner_page.locator(".form-status", has_text="Profile saved and confirmed.")).to_be_visible()
    assert stored() == {"linkedin": "https://example.test/in/sam"}, "a cleared address is removed, and what a résumé saved is kept"


def test_no_missing_answer_offers_use_for_any_company(apply_ready, owner_page):
    open_saved_role(owner_page)
    section = owner_page.locator(".apply-for-me")
    expect(section).to_be_visible()
    worked = section.locator('[data-apply-key="question_4000000111"]')
    expect(worked).to_contain_text("saved for this company only")
    expect(worked.get_by_label("Use for any company")).to_have_count(0)
    expect(section.locator('[data-apply-key="question_4000000101"]')).to_contain_text("saved for this company only")
    expect(section.locator('[data-apply-key="question_4000000101"]').get_by_label("Use for any company")).to_have_count(0)
    expect(section.get_by_label("Use for any company")).to_have_count(0)


def test_two_saved_answers_that_disagree_offer_a_way_to_the_answer_library(apply_ready, owner_page, live_server):
    from opportunity_app.student import preparation

    with db(live_server) as conn:
        for answer in ("Controls", "Perception"):
            preparation.save_answer(conn, "Which team are you most interested in?", answer, "Acme Robotics", [], user_id=USER)
    open_saved_role(owner_page)
    section = owner_page.locator(".apply-for-me")
    expect(section).to_be_visible()
    team = section.locator('[data-apply-key="question_4000000103"]')
    expect(team).to_contain_text("two different saved answers")
    team.get_by_role("button", name="Open your saved answers").click()
    expect(owner_page.locator("h3", has_text="Answer library")).to_be_focused()


def test_a_wrong_option_is_refused_in_words_and_nothing_is_saved(apply_ready, owner_page, live_server):
    open_saved_role(owner_page)
    section = owner_page.locator(".apply-for-me")
    expect(section).to_be_visible()
    team = section.locator('[data-apply-key="question_4000000103"]')
    team.get_by_role("button", name="Save and use for this question").click()
    expect(team.locator(".form-status")).to_have_text("Give an answer first.")
    with db(live_server) as conn:
        assert conn.execute("SELECT COUNT(*) FROM answer_library").fetchone()[0] == 0


def test_a_saved_role_that_is_not_on_greenhouse_says_so(apply_ready, owner_page, live_server):
    with db(live_server) as conn:
        actions.record_intent(conn, "job-b", "saved", user_id=USER)
    open_saved_role(owner_page, "Orbit Systems")
    expect(owner_page.locator(".apply-for-me .apply-summary")).to_have_text("Apply for me works with Greenhouse and Lever postings only, for now")
    expect(owner_page.locator(".apply-for-me button")).to_have_count(0)


def test_nothing_is_asked_and_nothing_shows_until_the_switch_is_on(owner_page, live_server):
    prepare(live_server)
    asked = []
    owner_page.on("request", lambda request: asked.append(request.url) if "/apply-agent/" in request.url else None)
    open_saved_role(owner_page)
    owner_page.wait_for_timeout(700)
    assert asked == []
    expect(owner_page.locator(".apply-for-me")).to_have_count(0)


@pytest.mark.parametrize("width", (1280, 390))
def test_the_section_and_its_forms_are_accessible_and_do_not_overflow(apply_ready, owner_page, width):
    owner_page.set_viewport_size({"width": width, "height": 900})
    open_saved_role(owner_page)
    expect(owner_page.locator(".apply-for-me")).to_be_visible()
    violations = Axe().run(owner_page, context=".apply-for-me", options=AXE_OPTIONS).get("violations", [])
    assert not violations, [(item["id"], item["help"]) for item in violations]
    assert owner_page.evaluate("() => document.documentElement.scrollWidth <= document.documentElement.clientWidth")


def test_the_settings_show_the_limits_and_keep_an_exact_option_the_student_saves(apply_ready, owner_page, live_server):
    owner_page.click("#profile-nav")
    wait_for_results(owner_page)
    block = owner_page.locator(".automation-apply-agent")
    expect(block).to_contain_text("Applications a day: 5")
    expect(block).to_contain_text("Days before applying again to the same company: 30 days")
    expect(block).to_contain_text("No options saved yet.")
    block.get_by_label("List").select_option("school")
    block.get_by_label("Exact option").fill("University of Example - City")
    block.get_by_role("button", name="Save this option").click()
    expect(block).to_contain_text("School: University of Example - City")
    with db(live_server) as conn:
        assert conn.execute("SELECT label FROM apply_ats_labels WHERE field='school'").fetchone()[0] == "University of Example - City"
    block.get_by_role("button", name="Remove the saved School option").click()
    expect(block).to_contain_text("No options saved yet.")
    violations = Axe().run(owner_page, context=".automation-apply-agent", options=AXE_OPTIONS).get("violations", [])
    assert not violations, [(item["id"], item["help"]) for item in violations]


def test_the_profile_keeps_the_name_written_on_an_application(owner_page, live_server):
    owner_page.click("#profile-nav")
    wait_for_results(owner_page)
    fields = owner_page.locator(".profile-fieldset", has_text="Name for applications")
    expect(fields.locator("legend")).to_have_text("Name for applications")
    fields.get_by_label("First name").fill("Ana María")
    fields.get_by_label("Last name").fill("de la Cruz")
    fields.get_by_label("Preferred name (optional)").fill("Ana")
    owner_page.get_by_role("button", name="Save and confirm profile").click()
    expect(owner_page.locator(".form-status", has_text="Profile saved and confirmed.")).to_be_visible()
    with db(live_server) as conn:
        stored = json.loads(conn.execute("SELECT value_json FROM profile_facts WHERE field_path='name_parts' AND confirmed=1").fetchone()[0])
    assert stored == {"first": "Ana María", "last": "de la Cruz", "preferred": "Ana"}
    owner_page.reload()
    wait_for_results(owner_page)
    owner_page.click("#profile-nav")
    wait_for_results(owner_page)
    expect(owner_page.locator(".profile-fieldset").get_by_label("Last name")).to_have_value("de la Cruz")
