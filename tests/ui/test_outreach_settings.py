"""The Outreach Settings tab: .env values a student would otherwise edit by hand."""

from __future__ import annotations

import os
from unittest import mock

import pytest
from playwright.sync_api import expect

import outreach_fakes
from conftest import OWNER_TOKEN
from test_outreach_journey import open_outreach

BEARER = {"Authorization": f"Bearer {OWNER_TOKEN}"}


@pytest.fixture
def restored_environment():
    # The server runs in this process, so its settings are this process's environment.
    with mock.patch.dict(os.environ, {}):
        yield
    if outreach_fakes.SETTINGS_ENV and outreach_fakes.SETTINGS_ENV.exists():
        outreach_fakes.SETTINGS_ENV.unlink()


def test_settings_save_at_once_and_attach_a_resume_under_its_own_name(owner_page, base_url, restored_environment):
    import test_platform

    uploaded = owner_page.request.post(
        f"{base_url}/api/v1/resumes", headers=BEARER,
        multipart={"resume": {"name": "Student Resume.docx", "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                              "buffer": test_platform.PlatformTests.sample_docx()}},
    )
    assert uploaded.status == 201, uploaded.text()

    open_outreach(owner_page, "settings")
    panel = owner_page.locator("section.outreach-settings")
    expect(panel.get_by_label("Who writes drafts")).to_be_visible()

    panel.get_by_label("Who writes drafts").select_option("legacy")
    expect(panel.locator(".form-status")).to_have_text("Draft writer saved.")
    assert os.environ["PIPELINE_OUTREACH_PROVIDER"] == "legacy"

    attach = panel.get_by_label("Attach to Gmail drafts")
    option = attach.locator("option", has_text="Student Resume.docx")
    attach.select_option(value=option.get_attribute("value"))
    expect(panel.locator(".form-status")).to_have_text("Attachment saved.")
    expect(attach.locator("option:checked")).to_have_text("Current: Student Resume.docx")
    assert os.environ["PIPELINE_OUTREACH_ATTACHMENT"].endswith("Student Resume.docx")

    env_text = outreach_fakes.SETTINGS_ENV.read_text(encoding="utf-8")
    assert "PIPELINE_OUTREACH_PROVIDER=legacy" in env_text

    from test_accessibility import _assert_accessible

    _assert_accessible(owner_page, "the outreach settings tab")

    attach.select_option("")
    expect(panel.locator(".form-status")).to_have_text("Attachment saved.")
    assert os.environ["PIPELINE_OUTREACH_ATTACHMENT"] == ""
