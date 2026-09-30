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
from test_apply_for_me import AXE_OPTIONS, USER, apply_ready, confirm_posting, db, open_saved_role  # noqa: F401 (apply_ready is a fixture)

CONSENT = re.compile("only to fill in application forms")


def allow(live_server, *categories):
    """The student switched these kinds of sensitive answer on, before the page loads."""
    from opportunity_app import apply_sensitive

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
