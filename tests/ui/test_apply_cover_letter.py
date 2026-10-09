"""Apply for me and the cover letter (Phase 5 M7, D11 B), in the browser: Draft one, Open the draft, Try again, and the plan preview.

The server runs in this process with the fictional listing, made to require a cover letter, and an agent that opens no browser
(tests/apply_fake_ats.py). A letter is drafted and approved through the app's own routes, the way the student would. Nothing
reaches Greenhouse.
"""

from __future__ import annotations

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


@pytest.fixture(autouse=True)
def canned_agent(live_server, base_url, pristine_database, monkeypatch):
    """The listing requires a cover letter; the knobs go back, and the app's run slot is free, before the next test rewinds the database."""
    original = dict(apply_fake_ats.CANNED)
    apply_fake_ats.CANNED["letter_required"] = True
    from opportunity_app.apply import preflight as apply_preflight

    # The server keeps listings for an hour, and other tests share it: read nothing from that cache and leave nothing in it,
    # or the listing that requires a letter would reach the next test file.
    monkeypatch.setattr(apply_preflight.SchemaCache, "get", lambda self, key: None)
    monkeypatch.setattr(apply_preflight.SchemaCache, "put", lambda self, key, listing: None)
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


def draft(base_url, live_server):
    """A cover letter drafted for the Acme role through the Prepare page's own route."""
    response = httpx.post(f"{base_url}/api/v1/preparation/documents", headers=BEARER, json={"opportunity_id": opportunity_id(live_server), "document_type": "cover_letter"})
    assert response.status_code == 201, response.text
    return response.json()


def approve(base_url, document):
    response = httpx.post(f"{base_url}/api/v1/preparation/documents/{document['id']}/approve", headers=BEARER)
    assert response.status_code == 200, response.text
    return response.json()


def open_section(page):
    open_saved_role(page)
    section = page.locator(".apply-for-me")
    expect(section).to_be_visible()
    return section


def letter_row(section):
    return section.locator('.apply-problem[data-apply-key="cover_letter"]')


def test_a_required_cover_letter_with_none_approved_offers_draft_one_and_it_opens_the_prepare_form_for_the_role(apply_ready, owner_page, live_server):
    section = open_section(owner_page)
    row = letter_row(section)
    expect(row).to_be_visible()
    expect(row).to_contain_text("Cover Letter")
    expect(row).to_contain_text("No cover letter is approved for this role.")
    expect(row.get_by_role("button", name="Draft one")).to_be_visible()
    expect(row.get_by_role("button", name="Open the draft")).to_have_count(0)
    expect(row.get_by_role("button", name="Try again")).to_be_visible()
    violations = Axe().run(owner_page, context=".apply-for-me", options=AXE_OPTIONS).get("violations", [])
    assert not violations, [(item["id"], item["help"]) for item in violations]
    row.get_by_role("button", name="Draft one").click()
    expect(owner_page.locator("#detail-panel.is-open")).to_have_count(0)
    form = owner_page.locator(".preparation-create-form", has=owner_page.locator('select[aria-label="Document type"]'))
    expect(form).to_be_visible()
    expect(form.locator('select[aria-label="Document type"]')).to_have_value("cover_letter")
    expect(form.locator('select[aria-label="Application opportunity"]')).to_have_value(opportunity_id(live_server))
    expect(form.get_by_role("button", name="Generate grounded draft")).to_be_focused()
    with db(live_server) as conn:
        assert conn.execute("SELECT COUNT(*) FROM generated_documents").fetchone()[0] == 0, "the app drafted nothing itself: the student presses the button"


def test_a_draft_is_opened_not_drafted_again_and_try_again_follows_its_approval(apply_ready, owner_page, live_server, base_url):
    document = draft(base_url, live_server)
    section = open_section(owner_page)
    row = letter_row(section)
    expect(row).to_contain_text("Your cover letter for this role is still a draft")
    expect(row.get_by_role("button", name="Draft one")).to_have_count(0)
    # Approved elsewhere while the role stays open: Try again asks the app once more.
    approve(base_url, document)
    row.get_by_role("button", name="Try again").click()
    expect(section.locator('.apply-problem[data-apply-key="cover_letter"]')).to_have_count(0)
    expect(section.locator(".apply-fields summary")).to_be_visible()
    section.locator(".apply-fields summary").click()
    expect(section.locator(".apply-fields")).to_contain_text("Cover Letter: from approved cover letter, version 1")


def test_open_the_draft_lands_on_the_draft_with_its_approve_button(apply_ready, owner_page, live_server, base_url):
    document = draft(base_url, live_server)
    section = open_section(owner_page)
    letter_row(section).get_by_role("button", name="Open the draft").click()
    expect(owner_page.locator("#detail-panel.is-open")).to_have_count(0)
    card = owner_page.locator(f'[data-document-id="{document["id"]}"]')
    expect(card).to_be_visible()
    expect(card.get_by_role("button", name="Approve version")).to_be_focused()
    card.get_by_role("button", name="Approve version").click()
    expect(card.get_by_role("button", name="Approved")).to_be_visible()


def test_a_newer_draft_after_an_approved_version_is_opened_and_the_old_one_is_not_attached(apply_ready, owner_page, live_server, base_url):
    approve(base_url, draft(base_url, live_server))
    newer = draft(base_url, live_server)
    assert newer["version"] == 2
    section = open_section(owner_page)
    row = letter_row(section)
    expect(row).to_contain_text("has a newer draft")
    expect(row.get_by_role("button", name="Open the draft")).to_be_visible()


def test_the_preview_of_a_rehearsal_shows_the_file_name_the_letters_words_and_its_version(apply_ready, owner_page, live_server, base_url):
    document = draft(base_url, live_server)
    approved = approve(base_url, document)
    section = open_section(owner_page)
    confirm_posting(section)
    section.get_by_role("button", name="Rehearse in a window").click()
    result = section.locator(".apply-result")
    expect(result).to_be_visible(timeout=20_000)
    expect(result.locator("table.apply-plan thead th")).to_have_text(["Question", "Answer", "What the rehearsal did", "From"], timeout=15_000)
    row = result.locator("table.apply-plan tbody tr", has=owner_page.get_by_role("rowheader", name="Cover Letter"))
    expect(row).to_have_count(1)
    cells = row.locator("td")
    expect(cells.nth(0)).to_contain_text(approved["artifact"]["filename"])
    letter_text = cells.nth(0).get_by_role("region", name="Text of the cover letter")
    expect(letter_text).to_contain_text("Dear Hiring Team")
    expect(letter_text).to_contain_text("Sincerely")
    expect(cells.nth(1)).to_have_text("Filled in the rehearsal")
    expect(cells.nth(2)).to_have_text("Approved cover letter, version 1")
    violations = Axe().run(owner_page, context=".apply-for-me", options=AXE_OPTIONS).get("violations", [])
    assert not violations, [(item["id"], item["help"]) for item in violations]
    assert owner_page.evaluate("() => document.documentElement.scrollWidth <= document.documentElement.clientWidth")
    # Changing the letter after the rehearsal is noticed: the preview says so rather than showing the old words as current.
    with db(live_server) as conn, conn:
        conn.execute("UPDATE generated_documents SET content=content || ? WHERE id=?", ("\nP.S. A new line.\n", document["id"]))
    owner_page.reload()
    owner_page.wait_for_selector("#detail-panel.is-open")
    again = owner_page.locator(".apply-for-me .apply-result table.apply-plan tbody tr", has=owner_page.get_by_role("rowheader", name="Cover Letter"))
    expect(again.locator("td").nth(0)).to_contain_text("changed since the rehearsal", timeout=15_000)
    expect(again.locator("td").nth(0)).to_contain_text("A new line.")
