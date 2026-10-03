"""Applied directly: the card's button files a company under its own tab and out of the working tabs, still in All companies."""

from __future__ import annotations

from playwright.sync_api import expect

from ui_helpers import card_for, open_outreach, open_tab, row_for, seed_target


def test_applied_directly_files_a_company_under_its_own_tab_and_moving_back_returns_it(owner_page, base_url):
    seed_target(owner_page, base_url)
    seed_target(owner_page, base_url, company="Kiva", website="https://kiva.example", source_urls=["https://kiva.example/"])
    open_outreach(owner_page, "all")
    card = card_for(owner_page, "Bovi")
    card.get_by_role("button", name="Applied directly", exact=True).click()
    card = card_for(owner_page, "Bovi")
    expect(card.get_by_role("button", name="Move back to outreach")).to_be_focused()
    expect(card.get_by_role("button", name="Remove company")).to_be_hidden()
    expect(card.get_by_role("button", name="Not interested", exact=True)).to_have_count(0)

    # Out of the working tabs, but All companies still lists it.
    open_tab(owner_page, "to-contact")
    expect(row_for(owner_page, "Bovi")).to_have_count(0)
    expect(row_for(owner_page, "Kiva")).to_have_count(1)
    open_tab(owner_page, "all")
    expect(row_for(owner_page, "Bovi")).to_have_count(1)
    expect(owner_page.locator('#subnav [data-subtab="applied-directly"]')).to_contain_text("1")
    expect(owner_page.locator('#subnav [data-subtab="not-interested"]')).not_to_contain_text("1")

    open_tab(owner_page, "applied-directly")
    expect(row_for(owner_page, "Kiva")).to_have_count(0)
    card = card_for(owner_page, "Bovi")
    expect(card).to_contain_text("Next: Applied directly")
    expect(card).to_contain_text("Applied directly, marked")

    card.get_by_role("button", name="Move back to outreach").click()
    open_tab(owner_page, "to-contact")
    expect(row_for(owner_page, "Bovi")).to_have_count(1)
    open_tab(owner_page, "applied-directly")
    expect(owner_page.locator(".empty-state")).to_contain_text("Mark a company Applied directly on its card")
