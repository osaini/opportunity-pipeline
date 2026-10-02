"""The sensitive-answers store in the browser: its settings, and the Needs you form on a saved Greenhouse role.

The server runs in this process with the fictional listing (tests/apply_fake_ats.py), so nothing here reaches
Greenhouse, and every answer, company and statement is made up. The store keeps only what the student chose to let the
app type into a form, so these tests check what the page refuses as much as what it keeps.
"""

from __future__ import annotations

import re

import pytest
from axe_core_python.sync_playwright import Axe
from playwright.sync_api import expect

from conftest import wait_for_results
from ui_helpers import AXE_OPTIONS, USER, confirm_posting, db, open_saved_role

CONSENT = re.compile("only to fill in application forms")


def allow(live_server, *categories):
    """The student switched these kinds of sensitive answer on, before the page loads."""
    from opportunity_app.apply import sensitive as apply_sensitive

    with db(live_server) as conn:
        apply_sensitive.set_allowed_categories(conn, USER, categories)


def stored(live_server):
    with db(live_server) as conn:
        return [dict(row) for row in conn.execute("SELECT * FROM apply_sensitive_answers ORDER BY created_at, id").fetchall()]


def test_a_sensitive_question_says_it_can_be_allowed_and_offers_no_form_until_it_is(apply_ready, owner_page, live_server):
    open_saved_role(owner_page)
    section = owner_page.locator(".apply-for-me")
    expect(section).to_be_visible()
    row = section.locator('[data-apply-key="question_4000000105"]')
    expect(row).to_contain_text("The app doesn't answer this kind of question for you")
    expect(row).to_contain_text("You can let the app answer this kind of question")
    expect(row.locator("textarea, select, input")).to_have_count(0)
    assert stored(live_server) == []


def test_the_settings_switch_a_kind_on_keep_the_answer_the_student_adds_and_remove_it(apply_ready, owner_page, live_server):
    owner_page.click("#profile-nav")
    wait_for_results(owner_page)
    block = owner_page.locator(".apply-sensitive-settings")
    expect(block).to_contain_text("The app never answers these on its own")
    expect(block).to_contain_text("It never answers export control, citizenship, security clearance or salary questions")
    expect(block).to_contain_text("No answers added yet.")
    expect(block.get_by_role("button", name="Save this answer")).to_have_count(0)
    expect(block.get_by_label(re.compile("Export control|Salary"))).to_have_count(0)
    block.get_by_label("Work authorization").check()
    expect(block.get_by_role("heading", name="Add an answer")).to_be_visible()
    block.get_by_label("Question, or the statement word for word").fill("Are you legally authorized to work in the United States?")
    block.get_by_label("Answer", exact=True).fill("Yes")
    block.get_by_role("button", name="Save this answer").click()
    expect(block.locator(".form-status")).to_have_text("Tick the box that says how the app may use this answer.")
    assert stored(live_server) == []
    block.get_by_label(CONSENT).check()
    block.get_by_role("button", name="Save this answer").click()
    expect(block.locator(".apply-sensitive-entry")).to_contain_text("Are you legally authorized to work in the United States?")
    expect(block.locator(".apply-sensitive-entry")).to_contain_text("For any company. You agreed to its use on")
    rows = stored(live_server)
    assert [(row["category"], row["answer"], row["consent_scope"], row["company_key"]) for row in rows] == [("work_authorization", "Yes", "confirmed", "")]
    violations = Axe().run(owner_page, context=".apply-sensitive-settings", options=AXE_OPTIONS).get("violations", [])
    assert not violations, [(item["id"], item["help"]) for item in violations]
    block.get_by_role("button", name="Remove the stored answer for Are you legally authorized to work in the United States?").click()
    expect(block).to_contain_text("No answers added yet.")
    assert stored(live_server) == []


@pytest.mark.allow_page_errors  # the two refusals are 422s by design
def test_an_eeo_answer_can_only_be_a_decline_in_the_settings_and_a_statement_needs_its_company(apply_ready, owner_page, live_server):
    owner_page.click("#profile-nav")
    wait_for_results(owner_page)
    block = owner_page.locator(".apply-sensitive-settings")
    block.get_by_label(re.compile("Voluntary self-identification")).check()
    block.get_by_label("Kind").select_option("eeo_gender")
    block.get_by_label("Question, or the statement word for word").fill("Gender")
    block.get_by_label("Answer", exact=True).fill("Male")
    block.get_by_label(CONSENT).check()
    block.get_by_role("button", name="Save this answer").click()
    expect(block.locator(".form-status")).to_contain_text("only a decline answer")
    assert stored(live_server) == []
    block.get_by_label("Answer", exact=True).fill("Decline To Self Identify")
    block.get_by_role("button", name="Save this answer").click()
    expect(block.locator(".apply-sensitive-entry")).to_contain_text("Decline To Self Identify")
    assert [row["answer"] for row in stored(live_server)] == ["Decline To Self Identify"]
    block.get_by_label("Legal acknowledgments, word for word").check()
    block.get_by_label("Kind").select_option("acknowledgment")
    expect(block.get_by_label("Answer", exact=True)).to_be_hidden()
    block.get_by_label("Question, or the statement word for word").fill("I have read the Example Robotics privacy notice")
    block.get_by_label(CONSENT).check()
    block.get_by_role("button", name="Save this answer").click()
    expect(block.locator(".form-status")).to_contain_text("never for any company")
    block.get_by_label(re.compile("^Company")).fill("Example Robotics")
    block.get_by_role("button", name="Save this answer").click()
    expect(block.locator(".apply-sensitive-entry", has_text="privacy notice")).to_contain_text("For Example Robotics")


def test_a_switched_on_question_has_a_form_with_the_consent_and_is_then_filled_from_the_store(apply_ready, owner_page, live_server):
    allow(live_server, "work_authorization", "acknowledgment", "eeo_gender", "eeo_veteran")
    open_saved_role(owner_page)
    section = owner_page.locator(".apply-for-me")
    expect(section).to_be_visible()
    confirm_posting(section)
    work = section.locator('[data-apply-key="question_4000000105"]')
    expect(work.get_by_label("Yes", exact=True)).to_be_visible()
    expect(work).to_contain_text("Use for any company")
    work.get_by_label("Yes", exact=True).check()
    work.get_by_role("button", name="Save this answer").click()
    expect(work.locator(".form-status")).to_have_text("Tick the box that says how the app may use this answer.")
    work.get_by_label(CONSENT).check()
    work.get_by_role("button", name="Save this answer").click()
    expect(section.locator('[data-apply-key="question_4000000105"]')).to_have_count(0)
    expect(section.locator(".apply-summary")).to_contain_text("Saved.")
    rows = stored(live_server)
    assert [(row["category"], row["answer"], row["company_key"], row["consent_scope"]) for row in rows] == [("work_authorization", "Yes", "acme robotics", "confirmed")]
    section.locator("summary", has_text="What the app would do with each").click()
    expect(section.locator(".apply-fields li", has_text="Are you legally authorized")).to_contain_text("from sensitive answer you added")


def test_a_privacy_statement_shows_its_words_is_ticked_for_this_company_only_and_the_field_list_says_so(apply_ready, owner_page, live_server):
    allow(live_server, "acknowledgment")
    open_saved_role(owner_page)
    section = owner_page.locator(".apply-for-me")
    expect(section).to_be_visible()
    confirm_posting(section)
    privacy = section.locator('[data-apply-key="question_4000000109"]')
    expect(privacy.locator(".apply-statement")).to_have_text("I have read the Example Robotics privacy notice")
    expect(privacy).to_contain_text("This is saved for Acme Robotics only.")
    expect(privacy.get_by_label("Use for any company")).to_have_count(0)
    privacy.get_by_label("Yes, tick this statement for me on the form").check()
    privacy.get_by_label(CONSENT).check()
    privacy.get_by_role("button", name="Save this answer").click()
    expect(section.locator('[data-apply-key="question_4000000109"]')).to_have_count(0)
    section.locator("summary", has_text="What the app would do with each").click()
    expect(section.locator(".apply-fields li", has_text="I have read the Example Robotics privacy notice")).to_contain_text("from your acknowledgment for Acme Robotics")
    assert [(row["answer_kind"], row["question_text"]) for row in stored(live_server)] == [("checkbox", "I have read the Example Robotics privacy notice")]


def test_optional_eeo_questions_offer_only_the_forms_decline_option(apply_ready, owner_page, live_server):
    allow(live_server, "eeo_gender", "eeo_veteran")
    open_saved_role(owner_page)
    section = owner_page.locator(".apply-for-me")
    expect(section).to_be_visible()
    confirm_posting(section)
    optional = section.locator(".apply-optional")
    expect(optional.locator("summary")).to_have_text("2 optional questions the app could answer for you")
    optional.locator("summary").click()
    gender = optional.locator('[data-apply-key="gender"]')
    expect(gender.locator('input[type="radio"]')).to_have_count(1)
    expect(gender.get_by_label("Decline To Self Identify")).to_be_visible()
    expect(gender.get_by_label("Male")).to_have_count(0)
    expect(optional.locator('[data-apply-key="veteran_status"]').get_by_label("I don't wish to answer")).to_be_visible()
    gender.get_by_label("Decline To Self Identify").check()
    gender.get_by_label(CONSENT).check()
    gender.get_by_role("button", name="Save this answer").click()
    expect(section.locator('.apply-optional [data-apply-key="gender"]')).to_have_count(0)
    expect(section.locator(".apply-optional summary")).to_have_text("1 optional question the app could answer for you")
    assert [row["answer"] for row in stored(live_server)] == ["Decline To Self Identify"]


@pytest.mark.parametrize("width", (1280, 390))
def test_the_sensitive_forms_are_accessible_and_do_not_overflow(apply_ready, owner_page, live_server, width):
    allow(live_server, "work_authorization", "acknowledgment", "eeo_gender")
    owner_page.set_viewport_size({"width": width, "height": 900})
    open_saved_role(owner_page)
    section = owner_page.locator(".apply-for-me")
    expect(section).to_be_visible()
    section.locator(".apply-optional summary").click()
    expect(section.locator('[data-apply-key="question_4000000105"] form')).to_be_visible()
    violations = Axe().run(owner_page, context=".apply-for-me", options=AXE_OPTIONS).get("violations", [])
    assert not violations, [(item["id"], item["help"]) for item in violations]
    assert owner_page.evaluate("() => document.documentElement.scrollWidth <= document.documentElement.clientWidth")


TERMS_FIELD = "question_4000000301"
TERMS_LINK = "https://careers.example-robotics.test/candidate-terms"


@pytest.fixture
def yes_no_terms(monkeypatch):
    """The fictional listing also asks a Yes/No agreement question whose description holds the statement and a link."""
    from apply_fake_ats import FakeSchemaClient
    from opportunity_app.apply import preflight as apply_preflight

    original = FakeSchemaClient.fetch

    def fetch(self, board_token, job_id):
        listing = original(self, board_token, job_id)
        if listing is not None:
            listing["questions"].append({
                "description": f'<p>By applying you agree to the <a href="{TERMS_LINK}">Candidate Terms</a>, including binding arbitration of any employment dispute and a waiver of class actions.</p>',
                "label": "Do you accept the candidate terms?", "required": True,
                "fields": [{"name": TERMS_FIELD, "type": "multi_value_single_select", "values": [{"label": "Yes", "value": 1}, {"label": "No", "value": 0}]}],
            })
        return listing

    monkeypatch.setattr(FakeSchemaClient, "fetch", fetch)
    monkeypatch.setattr(FakeSchemaClient, "__call__", fetch)
    # The server keeps listings for an hour, and other tests share it: read nothing from that cache and leave nothing in it.
    monkeypatch.setattr(apply_preflight.SchemaCache, "get", lambda self, key: None)
    monkeypatch.setattr(apply_preflight.SchemaCache, "put", lambda self, key, listing: None)


def test_a_yes_no_agreement_shows_its_statement_and_links_before_it_can_be_ticked(apply_ready, yes_no_terms, owner_page, live_server):
    allow(live_server, "acknowledgment")
    open_saved_role(owner_page)
    section = owner_page.locator(".apply-for-me")
    expect(section).to_be_visible()
    confirm_posting(section)
    terms = section.locator(f'[data-apply-key="{TERMS_FIELD}"]')
    # The words the student agrees to, and the document they point to, are on screen before the tick and the Save.
    expect(terms.locator(".apply-statement")).to_contain_text("binding arbitration of any employment dispute and a waiver of class actions")
    expect(terms.locator(f'a[href="{TERMS_LINK}"]')).to_be_visible()
    # One tick, never a Yes/No choice that could not be saved for No.
    expect(terms.locator('input[type="radio"]')).to_have_count(0)
    expect(terms.get_by_label("Yes, choose Yes for me on the form")).to_be_visible()
    terms.get_by_role("button", name="Save this answer").click()
    expect(terms.locator(".form-status")).to_have_text("Tick the statement first.")
    assert stored(live_server) == []
    terms.get_by_label("Yes, choose Yes for me on the form").check()
    terms.get_by_label(CONSENT).check()
    terms.get_by_role("button", name="Save this answer").click()
    expect(section.locator(f'[data-apply-key="{TERMS_FIELD}"]')).to_have_count(0)
    rows = stored(live_server)
    assert [(row["answer_kind"], row["answer"], row["company_key"]) for row in rows] == [("checkbox", "checked", "acme robotics")]
    assert "binding arbitration" in rows[0]["question_text"] and TERMS_LINK in rows[0]["statement_links_json"]


def test_switching_two_kinds_quickly_keeps_both_changes(apply_ready, owner_page, live_server):
    from opportunity_app.apply import sensitive as apply_sensitive

    allow(live_server, *apply_sensitive.STORABLE)
    owner_page.click("#profile-nav")
    wait_for_results(owner_page)
    block = owner_page.locator(".apply-sensitive-settings")
    expect(block.locator('.apply-kinds input[type="checkbox"]:checked')).to_have_count(6)
    # Two clicks before the first request is back, as a quick student or a screen reader's double activation would.
    block.evaluate("""(host) => {
        for (const key of ["age_18", "acknowledgment"]) host.querySelector(`[data-focus="kind-${key}"]`).click();
    }""")
    owner_page.wait_for_timeout(1000)
    expect(block.locator('.apply-kinds input[type="checkbox"]:checked')).to_have_count(4)
    expect(block.get_by_label("18 or older")).not_to_be_checked()
    expect(block.get_by_label("Legal acknowledgments, word for word")).not_to_be_checked()
    with db(live_server) as conn:
        allowed = apply_sensitive.allowed_categories(conn, USER)
    assert "age_18" not in allowed and "acknowledgment" not in allowed
    assert {"work_authorization", "sponsorship", "consent"} <= allowed


def test_a_change_in_the_settings_keeps_keyboard_focus_and_what_was_typed(apply_ready, owner_page, live_server):
    from opportunity_app.apply import sensitive as apply_sensitive

    allow(live_server, "work_authorization")
    with db(live_server) as conn:
        apply_sensitive.add_entry(conn, USER, category="work_authorization", question="Are you legally authorized to work in the United States?",
                                  answer="Yes", consent=True)
    owner_page.click("#profile-nav")
    wait_for_results(owner_page)
    block = owner_page.locator(".apply-sensitive-settings")
    block.get_by_label("Question, or the statement word for word").fill("Half-typed question I was writing")
    block.get_by_label(CONSENT).check()
    block.get_by_label("Visa sponsorship and immigration status").focus()
    owner_page.keyboard.press("Space")
    expect(block.get_by_label("Kind").locator("option")).to_have_count(2)  # the repaint has happened
    expect(block.get_by_label("Visa sponsorship and immigration status")).to_be_checked()
    assert owner_page.evaluate("() => document.activeElement.dataset.focus") == "kind-sponsorship"
    expect(block.get_by_label("Question, or the statement word for word")).to_have_value("Half-typed question I was writing")
    expect(block.get_by_label(CONSENT)).to_be_checked()
    # Removing an answer leaves focus in the block, on the list's heading, not at the top of the page.
    block.get_by_role("button", name=re.compile("^Remove the stored answer")).focus()
    owner_page.keyboard.press("Enter")
    expect(block).to_contain_text("No answers added yet.")
    assert owner_page.evaluate("() => document.activeElement.textContent") == "Answers you added"
    expect(block.get_by_label("Question, or the statement word for word")).to_have_value("Half-typed question I was writing")


@pytest.mark.allow_page_errors  # the refused save is a 422 by design
def test_a_refused_needs_you_save_keeps_focus_on_the_save_button(apply_ready, owner_page, live_server):
    allow(live_server, "work_authorization")
    open_saved_role(owner_page)
    section = owner_page.locator(".apply-for-me")
    expect(section).to_be_visible()
    # The posting is not confirmed, so the server refuses the save.
    work = section.locator('[data-apply-key="question_4000000105"]')
    work.get_by_label("Yes", exact=True).check()
    work.get_by_label(CONSENT).check()
    work.get_by_role("button", name="Save this answer").focus()
    owner_page.keyboard.press("Enter")
    expect(work.locator(".form-status")).to_contain_text("Confirm it is the right posting")
    assert owner_page.evaluate("() => document.activeElement.textContent") == "Save this answer"
    assert owner_page.evaluate("() => !!document.activeElement.closest('#detail-panel')")


def test_the_list_shows_the_company_as_typed_and_says_when_no_role_has_it(apply_ready, owner_page, live_server):
    allow(live_server, "work_authorization")
    owner_page.click("#profile-nav")
    wait_for_results(owner_page)
    block = owner_page.locator(".apply-sensitive-settings")
    block.get_by_label("Question, or the statement word for word").fill("Are you legally authorized to work in the United States?")
    block.get_by_label("Answer", exact=True).fill("Yes")
    block.get_by_label(re.compile("^Company")).fill("Zeta Alpha Labs, Inc.")
    block.get_by_label(CONSENT).check()
    block.get_by_role("button", name="Save this answer").click()
    entry = block.locator(".apply-sensitive-entry")
    expect(entry).to_contain_text("For Zeta Alpha Labs, Inc.")
    expect(entry).not_to_contain_text("Alpha Labs Zeta")
    expect(entry).to_contain_text("None of your roles is at a company with this name")
    expect(block.locator(".form-status")).to_contain_text("Saved. None of your roles")


AGE_FIELD = "question_4000000302"
AGE_STATEMENT = "Are you 18 years of age or older? Yes, I am 18 or older"


@pytest.fixture
def age_box(monkeypatch):
    """The fictional listing also asks a tick box that states the student's age."""
    from apply_fake_ats import FakeSchemaClient
    from opportunity_app.apply import preflight as apply_preflight

    original = FakeSchemaClient.fetch

    def fetch(self, board_token, job_id):
        listing = original(self, board_token, job_id)
        if listing is not None:
            listing["questions"].append({
                "description": "", "label": "Are you 18 years of age or older?", "required": True,
                "fields": [{"name": AGE_FIELD, "type": "multi_value_multi_select", "values": [{"label": "Yes, I am 18 or older", "value": 1}]}],
            })
        return listing

    monkeypatch.setattr(FakeSchemaClient, "fetch", fetch)
    monkeypatch.setattr(FakeSchemaClient, "__call__", fetch)
    monkeypatch.setattr(apply_preflight.SchemaCache, "get", lambda self, key: None)
    monkeypatch.setattr(apply_preflight.SchemaCache, "put", lambda self, key, listing: None)


@pytest.mark.allow_page_errors  # the save with no company is a 422 by design
def test_a_tick_box_answer_added_in_the_settings_is_stored_as_ticked_and_used_on_the_form(apply_ready, age_box, owner_page, live_server):
    allow(live_server, "age_18")
    owner_page.click("#profile-nav")
    wait_for_results(owner_page)
    block = owner_page.locator(".apply-sensitive-settings")
    tick = block.get_by_label(re.compile("^The form shows this as a tick box"))
    expect(tick).to_be_visible()
    block.get_by_label("Question, or the statement word for word").fill(AGE_STATEMENT)
    tick.check()
    expect(block.get_by_label("Answer", exact=True)).to_be_hidden()
    block.get_by_label(CONSENT).check()
    # A tick box is kept for one company only: with no company the save is refused, and with one it is stored for that company.
    block.get_by_role("button", name="Save this answer").click()
    expect(block.locator(".form-status")).to_contain_text("never for any company")
    assert stored(live_server) == []
    block.get_by_label(re.compile("^Company")).fill("Acme Robotics")
    block.get_by_role("button", name="Save this answer").click()
    expect(block.locator(".apply-sensitive-entry")).to_contain_text("Ticked")
    assert [(row["category"], row["answer_kind"], row["answer"], row["company_key"]) for row in stored(live_server)] == [("age_18", "checkbox", "checked", "acme robotics")]
    open_saved_role(owner_page)
    section = owner_page.locator(".apply-for-me")
    expect(section).to_be_visible()
    confirm_posting(section)
    # The box is filled from what was stored: it is not listed as needing an answer, and no mismatch is reported.
    expect(section.locator(f'[data-apply-key="{AGE_FIELD}"]')).to_have_count(0)
    expect(section).not_to_contain_text("doesn't fit this form")


def test_remove_buttons_say_which_company_and_the_options_say_which_question(apply_ready, owner_page, live_server):
    from opportunity_app.apply import sensitive as apply_sensitive

    question = "Are you legally authorized to work in the United States?"
    allow(live_server, "work_authorization")
    with db(live_server) as conn:
        apply_sensitive.add_entry(conn, USER, category="work_authorization", question=question, answer="Yes", consent=True)
        apply_sensitive.add_entry(conn, USER, category="work_authorization", question=question, answer="No", consent=True, company="Alpha Labs")
    owner_page.click("#profile-nav")
    wait_for_results(owner_page)
    block = owner_page.locator(".apply-sensitive-settings")
    expect(block.get_by_role("button", name=re.compile(r"^Remove the stored answer for .*, for any company$"))).to_have_count(1)
    expect(block.get_by_role("button", name=re.compile(r"^Remove the stored answer for .*, for Alpha Labs$"))).to_have_count(1)
    with db(live_server) as conn:
        conn.execute("DELETE FROM apply_sensitive_answers")
        conn.commit()
    open_saved_role(owner_page)
    section = owner_page.locator(".apply-for-me")
    expect(section).to_be_visible()
    work = section.locator('[data-apply-key="question_4000000105"]')
    expect(work.get_by_role("group", name=f"Your answer: {question}")).to_be_visible()
