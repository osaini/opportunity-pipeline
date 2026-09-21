"""The per-page subtab rail and the color theme switch."""

from __future__ import annotations

from playwright.sync_api import expect


def wait_for_results(page):
    page.wait_for_function("() => document.getElementById('results')?.getAttribute('aria-busy') === 'false'")


def test_every_page_fills_the_rail_with_its_own_subtabs(owner_page):
    for view, first in (
        ("discover", "all"), ("urgent", "all"), ("saved", "all"), ("applications", "all"),
        ("outreach", "to-contact"), ("prepare", "all"), ("agent", "all"), ("profile", "all"),
    ):
        owner_page.click(f"#{view}-nav")
        wait_for_results(owner_page)
        expect(owner_page.locator(f'#subnav [data-subtab="{first}"]')).to_have_attribute("aria-current", "true")
        assert owner_page.locator("#subnav .subnav-item").count() >= 2, f"{view} has no subtabs"


def test_a_profile_subtab_shows_only_that_section(owner_page):
    owner_page.click("#profile-nav")
    wait_for_results(owner_page)
    owner_page.locator('#subnav [data-subtab="resumes"]').click()
    expect(owner_page.locator("#results > [data-subtab='resumes']")).to_be_visible()
    expect(owner_page.locator("#results > [data-subtab='profile']")).to_be_hidden()
    owner_page.locator('#subnav [data-subtab="all"]').click()
    expect(owner_page.locator("#results > [data-subtab='profile']")).to_be_visible()


def test_a_discover_quick_tab_narrows_by_work_mode(owner_page):
    owner_page.click("#discover-nav")
    wait_for_results(owner_page)
    owner_page.locator('#subnav [data-subtab="remote"]').click()
    wait_for_results(owner_page)
    expect(owner_page.locator('#subnav [data-subtab="remote"]')).to_have_attribute("aria-current", "true")
    expect(owner_page.locator('#subnav [data-subtab="remote"] .subnav-count')).to_be_visible()


def test_the_theme_switch_cycles_and_survives_a_reload(owner_page):
    toggle = owner_page.locator("#theme-toggle")
    expect(toggle).to_have_text("Theme: System")
    toggle.click()
    expect(owner_page.locator("html")).to_have_attribute("data-theme", "light")
    toggle.click()
    expect(owner_page.locator("html")).to_have_attribute("data-theme", "dark")
    background = owner_page.evaluate("getComputedStyle(document.body).backgroundColor")
    assert background == "rgb(18, 23, 20)", background

    owner_page.reload()
    expect(owner_page.locator("html")).to_have_attribute("data-theme", "dark")
    expect(owner_page.locator("#theme-toggle")).to_have_text("Theme: Dark")
    owner_page.locator("#theme-toggle").click()
    expect(owner_page.locator("html")).not_to_have_attribute("data-theme", "dark")
