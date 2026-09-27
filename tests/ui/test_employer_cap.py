"""Best fit shows each employer's top postings, and "+N more" opens the rest.

The seeded sandbox has one posting per employer, so each test adds one large
employer to the freshly rewound database before signing in.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing

from playwright.sync_api import expect

from conftest import sign_in_as_owner, wait_for_results
from opportunity_app.schema import LOCAL_USER_ID, sort_key
from pipeline_core import RANKED_VIEW_PER_COMPANY
from test_accessibility import _assert_accessible

COMPANY = "Capstone Dynamics"
POSTINGS = RANKED_VIEW_PER_COMPANY + 2
STAMP = "2026-08-10T00:00:00+00:00"


def _seed_large_employer(path) -> None:
    with closing(sqlite3.connect(path)) as conn:
        for index in range(POSTINGS):
            opportunity_id = f"capstone-{index}"
            title = f"Controls Intern {index}"
            conn.execute(
                "INSERT INTO opportunities(id, company, title, company_sort_key, title_sort_key, location, "
                "region, role_type, url, description, first_seen_at, last_seen_at, active, fingerprint, "
                "content_fingerprint, created_at, updated_at) "
                "VALUES(?, ?, ?, ?, ?, 'Remote', 'Remote', 'internship', ?, 'Controls work.', ?, ?, 1, ?, ?, ?, ?)",
                (opportunity_id, COMPANY, title, sort_key(COMPANY), sort_key(title),
                 f"https://example.com/{opportunity_id}", STAMP, STAMP, opportunity_id, opportunity_id,
                 STAMP, STAMP),
            )
            conn.execute(
                "INSERT INTO opportunity_sources(opportunity_id, source_key, source_name, external_id, "
                "source_url, first_seen_at, last_seen_at) VALUES(?, 'greenhouse:capstone', 'Capstone Board', "
                "?, ?, ?, ?)",
                (opportunity_id, opportunity_id, f"https://example.com/{opportunity_id}", STAMP, STAMP),
            )
            conn.execute(
                "INSERT INTO fit_scores(opportunity_id, user_id, ruleset_version, score, explanation_json, "
                "created_at) VALUES(?, ?, 'legacy-v1', ?, '[]', ?)",
                (opportunity_id, LOCAL_USER_ID, 99 - index, STAMP),
            )
        conn.commit()


def _open_deck(page, live_server) -> None:
    _seed_large_employer(live_server.live_path)
    page.goto("/")
    sign_in_as_owner(page)
    wait_for_results(page)


def _employer_cards(page):
    return page.locator(".opportunity-card").filter(has=page.locator(".company-name", has_text=COMPANY))


def test_best_fit_shows_an_employers_top_postings_and_says_how_many_more(page, live_server):
    _open_deck(page, live_server)
    expect(_employer_cards(page)).to_have_count(RANKED_VIEW_PER_COMPANY)
    more = page.get_by_role("button", name=f"+2 more from {COMPANY}")
    expect(more).to_have_count(1)
    # On the employer's last shown card, not floating in the list.
    expect(_employer_cards(page).last.locator(".company-more")).to_have_count(1)
    _assert_accessible(page, "discover with a capped employer")


def test_more_opens_every_posting_from_that_employer_and_can_be_undone(page, live_server):
    _open_deck(page, live_server)
    page.get_by_role("button", name=f"+2 more from {COMPANY}").click()
    wait_for_results(page)
    expect(page.locator("#company-filter")).to_be_visible()
    expect(page.locator("#company-filter-name")).to_have_text(COMPANY)
    expect(page.locator("#company-filter")).to_be_focused()
    expect(_employer_cards(page)).to_have_count(POSTINGS)
    expect(page.locator(".opportunity-card")).to_have_count(POSTINGS)
    expect(page.locator(".company-more")).to_have_count(0)
    _assert_accessible(page, "discover filtered to one employer")

    page.get_by_role("button", name="Show every employer").click()
    wait_for_results(page)
    expect(page.locator("#company-filter")).to_be_hidden()
    expect(_employer_cards(page)).to_have_count(RANKED_VIEW_PER_COMPANY)


def test_other_sorts_are_not_capped(page, live_server):
    _open_deck(page, live_server)
    page.select_option("#sort-filter", "newest")
    wait_for_results(page)
    expect(_employer_cards(page)).to_have_count(POSTINGS)
    expect(page.locator(".company-more")).to_have_count(0)
