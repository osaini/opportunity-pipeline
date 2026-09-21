"""A preparation document downloads as a real PDF, rendered by the app's own Chromium."""

from __future__ import annotations

from playwright.sync_api import expect

from conftest import OWNER_TOKEN, wait_for_results

BEARER = {"Authorization": f"Bearer {OWNER_TOKEN}"}


def test_a_cover_letter_downloads_as_a_pdf(owner_page, base_url):
    created = owner_page.request.post(
        f"{base_url}/api/v1/preparation/documents", headers=BEARER,
        data={"opportunity_id": "job-a", "document_type": "cover_letter"},
    )
    assert created.status == 201, created.text()
    owner_page.click("#prepare-nav")
    wait_for_results(owner_page)
    card = owner_page.locator(".preparation-item").first
    button = card.get_by_role("button", name="Download PDF")
    expect(button).to_be_visible()
    with owner_page.expect_download(timeout=60_000) as downloaded:
        button.click()
    download = downloaded.value
    assert download.suggested_filename.endswith(".pdf")
    with open(download.path(), "rb") as handle:
        assert handle.read(5) == b"%PDF-"
    expect(card.locator(".form-status")).to_have_text("PDF downloaded. Check it before you send it.")
