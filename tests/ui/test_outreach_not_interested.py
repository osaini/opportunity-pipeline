"""Not interested: the card's button files a company under its own tab, out of every other one, and back again."""

from __future__ import annotations

from playwright.sync_api import expect

from ui_helpers import card_for, open_outreach, open_tab, row_for, seed_target


def test_not_interested_files_a_company_under_its_own_tab_and_moving_back_returns_it(owner_page, base_url):
    seed_target(owner_page, base_url)
    seed_target(owner_page, base_url, company="Kiva", website="https://kiva.example", source_urls=["https://kiva.example/"])
    open_outreach(owner_page, "all")
    card = card_for(owner_page, "Bovi")
    card.get_by_role("button", name="Not interested", exact=True).click()
    # The card stays under the pointer and says where it went; kept means it offers no Remove.
    card = card_for(owner_page, "Bovi")
    expect(card).to_contain_text("Now in Not interested")
    expect(card.get_by_role("button", name="Move back to outreach")).to_be_focused()
    expect(card.get_by_role("button", name="Remove company")).to_be_hidden()

    for tab in ("to-contact", "all"):
        open_tab(owner_page, tab)
        expect(row_for(owner_page, "Bovi")).to_have_count(0)
        expect(row_for(owner_page, "Kiva")).to_have_count(1)
    expect(owner_page.locator('#subnav [data-subtab="not-interested"]')).to_contain_text("1")

    open_tab(owner_page, "not-interested")
    expect(row_for(owner_page, "Kiva")).to_have_count(0)
    card = card_for(owner_page, "Bovi")
    expect(card).to_contain_text("Next: Nothing while not interested")
    expect(card).to_contain_text("Not interested since")

    card.get_by_role("button", name="Move back to outreach").click()
    expect(card_for(owner_page, "Bovi")).to_contain_text("Now in To contact")
    open_tab(owner_page, "to-contact")
    expect(row_for(owner_page, "Bovi")).to_have_count(1)
    open_tab(owner_page, "not-interested")
    expect(owner_page.locator(".empty-state")).to_contain_text("Mark a company Not interested on its card")
