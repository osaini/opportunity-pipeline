"""The Outreach Settings tab: .env values a student would otherwise edit by hand."""

from __future__ import annotations

import os

from playwright.sync_api import expect

import outreach_fakes
from conftest import OWNER_TOKEN
from helpers_platform import sample_docx
from ui_helpers import assert_accessible, card_for, open_details, open_outreach, seed_target

BEARER = {"Authorization": f"Bearer {OWNER_TOKEN}"}


def test_settings_save_at_once_and_attach_a_resume_under_its_own_name(owner_page, base_url, restored_environment):
    uploaded = owner_page.request.post(
        f"{base_url}/api/v1/resumes", headers=BEARER,
        multipart={"resume": {"name": "Student Resume.docx", "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                              "buffer": sample_docx()}},
    )
    assert uploaded.status == 201, uploaded.text()

    open_outreach(owner_page, "settings")
    panel = owner_page.locator("section.outreach-settings")
    expect(panel.get_by_label("Who writes first-email drafts")).to_be_visible()

    panel.get_by_label("Who writes first-email drafts").select_option("legacy")
    expect(panel.locator(".form-status")).to_have_text("Draft writer saved.")
    assert os.environ["PIPELINE_OUTREACH_PROVIDER"] == "legacy"

    # Follow-ups and call prep can have their own writer, and default to the first-email one.
    follow = panel.get_by_label("Who writes follow-ups")
    expect(follow).to_have_value("")
    follow.select_option("legacy")
    expect(panel.locator(".form-status")).to_have_text("Follow-up writer saved.")
    assert os.environ["PIPELINE_OUTREACH_FOLLOW_UP_PROVIDER"] == "legacy"
    follow.select_option("")
    expect(panel.locator(".form-status")).to_have_text("Follow-up writer saved.")
    assert os.environ["PIPELINE_OUTREACH_FOLLOW_UP_PROVIDER"] == ""
    expect(panel.get_by_label("Who writes call prep")).to_have_value("")
    reviewer = panel.get_by_label("Who reviews follow-ups and thank-yous")
    expect(reviewer).to_have_value("")
    expect(reviewer.locator("option").first).to_contain_text("Automatic")

    attach = panel.get_by_label("Attach to Gmail drafts")
    option = attach.locator("option", has_text="Student Resume.docx")
    attach.select_option(value=option.get_attribute("value"))
    expect(panel.locator(".form-status")).to_have_text("Attachment saved.")
    expect(attach.locator("option:checked")).to_have_text("Current: Student Resume.docx")
    assert os.environ["PIPELINE_OUTREACH_ATTACHMENT"].endswith("Student Resume.docx")

    env_text = outreach_fakes.SETTINGS_ENV.read_text(encoding="utf-8")
    assert "PIPELINE_OUTREACH_PROVIDER=legacy" in env_text

    assert_accessible(owner_page, "the outreach settings tab")

    attach.select_option("")
    expect(panel.locator(".form-status")).to_have_text("Attachment saved.")
    assert os.environ["PIPELINE_OUTREACH_ATTACHMENT"] == ""


def test_jev_inbox_suggestions_are_off_until_the_student_turns_them_on(owner_page, base_url):
    seed_target(owner_page, base_url, status="sent")
    open_outreach(owner_page, "settings")
    panel = owner_page.locator("section.outreach-settings")
    choice = panel.get_by_label("Who suggests reply and email outcomes")
    expect(choice).to_be_enabled()
    expect(choice).to_have_value("rules")
    expect(panel.locator(".jev-inbox-setting")).to_contain_text("With Jev, these go to TypeSafe")
    # Application emails the app reads count too, and the one switch that may act on a Jev answer says when.
    expect(panel.locator(".jev-inbox-setting")).to_contain_text("when Jev and the keyword rules agree")

    def log_reply():
        open_outreach(owner_page, "awaiting")
        details = open_details(card_for(owner_page, "Bovi"), "Replies and history")
        details.get_by_label("Paste their reply").fill("Thanks for reaching out! Could we set up a call next week?")
        details.get_by_role("button", name="Log reply").click()
        return details.locator(".outreach-reply-result")

    result = log_reply()
    expect(result).to_contain_text("proposes a call")
    expect(result).not_to_contain_text("Jev")

    open_outreach(owner_page, "settings")
    choice = owner_page.locator("section.outreach-settings").get_by_label("Who suggests reply and email outcomes")
    choice.select_option("jev")
    expect(owner_page.locator(".jev-inbox-status")).to_have_text("Jev inbox suggestions on.")
    # The fake Jev answers the first option, so the suggestion is visibly Jev's.
    expect(log_reply()).to_contain_text("Jev suggestion, 94% sure")


def test_a_codex_that_is_installed_but_not_opted_in_says_what_it_needs_not_that_it_is_not_set_up(owner_page, restored_environment):
    """Codex is on this computer; only the .env opt-in is missing. The picker must not read as if it were not installed."""
    import json
    import re

    reason = "needs the .env opt-in"
    hint = "Codex cannot be limited to web search, so it reads the web only when PIPELINE_OUTREACH_RESEARCH_ALLOW_CODEX=1 is set in .env."

    def blocked(route):
        response = route.fetch()
        body = response.json()
        for field in ("research_agent", "company_research_agent"):
            body[field]["options"] = [
                {"id": "claude-code", "label": "Claude Code", "available": True, "hint": ""},
                {"id": "codex-cli", "label": "Codex", "available": False, "reason": reason, "hint": hint},
            ]
        route.fulfill(response=response, body=json.dumps(body))

    owner_page.route(re.compile(r"/api/v1/outreach/settings$"), blocked)
    open_outreach(owner_page, "settings")
    panel = owner_page.locator("section.outreach-settings")
    for label in ("Who does the web research", "Who researches a company for call prep"):
        option = panel.get_by_label(label).locator("option", has_text="Codex")
        expect(option).to_have_text(f"Codex ({reason})")
    panel.get_by_label("Who does the web research").select_option("codex-cli")
    expect(panel.get_by_text(hint).first).to_be_visible()
    # The save the change made goes through the same route; let it finish before the test ends.
    expect(panel.locator(".form-status")).to_have_text("Research agent saved.")

