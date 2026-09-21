"""Urgent queue, student-entered deadlines, calendar export, and keyboard triage.

Dates are seeded relative to today through the API, and expected counts come
from ``/api/v1/urgent`` itself, so these tests do not rot as the fixture's
fixed dates age.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone

import pytest
from playwright.sync_api import expect

from conftest import OWNER_TOKEN, sign_in_as_owner, wait_for_results

BEARER = {"Authorization": f"Bearer {OWNER_TOKEN}"}


def iso(days: int) -> str:
    return (date.today() + timedelta(days=days)).isoformat()


def put_deadline(page, base_url: str, opportunity_id: str, days: int, note: str = "") -> None:
    response = page.request.put(
        f"{base_url}/api/v1/opportunities/{opportunity_id}/deadline",
        headers=BEARER, data={"deadline_on": iso(days), "note": note},
    )
    assert response.status == 200, response.text()


def attention(page, base_url: str) -> int:
    response = page.request.get(f"{base_url}/api/v1/urgent", headers=BEARER)
    assert response.status == 200, response.text()
    return response.json()["counts"]["attention"]


def open_urgent(page) -> None:
    page.click("#urgent-nav")
    wait_for_results(page)
    expect(page.locator("#page-title")).to_have_text("What needs doing next.")


def expect_badge(page, count: int) -> None:
    badge = page.locator("#urgent-badge")
    if count:
        expect(badge).to_have_text(str(count))
        expect(page.locator("#urgent-nav")).to_have_attribute(
            "aria-label", f"Urgent, {count} need{'s' if count == 1 else ''} attention"
        )
    else:
        expect(badge).to_be_hidden()
        expect(page.locator("#urgent-nav")).to_have_attribute("aria-label", "Urgent")


def test_a_deadline_entered_in_detail_shows_in_urgent_as_yours(owner_page, base_url):
    before = attention(owner_page, base_url)
    owner_page.click("#saved-nav")
    wait_for_results(owner_page)
    owner_page.locator(".card-button").first.click()
    section = owner_page.locator(".user-deadline")
    expect(section).to_contain_text("No deadline entered")
    section.locator("input[type=date]").fill(iso(1))
    section.get_by_label("Where you saw it (optional)").fill("Careers page, fall cycle")
    section.get_by_role("button", name="Save deadline").click()
    expect(section.locator(".form-status")).to_contain_text("appears in Urgent")
    expect(section.locator(".user-deadline-current")).to_contain_text("Careers page, fall cycle")
    expect_badge(owner_page, before + 1)

    owner_page.locator("#detail-close").click()
    open_urgent(owner_page)
    row = owner_page.locator(".urgent-row").filter(
        has=owner_page.locator(".urgent-source", has_text=re.compile(r"^You entered$"))
    )
    expect(row).to_have_count(1)
    expect(row).to_contain_text("Tomorrow")
    expect(owner_page.locator(".urgent-group.is-week")).to_contain_text("You entered")
    # The same posting also states a deadline in its text: two rows, linked by a hint.
    expect(row.locator(".urgent-also")).to_contain_text("stated in posting text")


def test_badge_follows_pass_undo_and_clears_on_sign_out(owner_page, base_url):
    put_deadline(owner_page, base_url, "job-a", 0)
    owner_page.reload()
    wait_for_results(owner_page)
    expect_badge(owner_page, attention(owner_page, base_url))

    response = owner_page.request.post(
        f"{base_url}/api/v1/opportunities/job-a/actions", headers=BEARER, data={"action": "passed"},
    )
    assert response.status == 200
    owner_page.click("#saved-nav")  # any view load re-reads the badge
    wait_for_results(owner_page)
    expect_badge(owner_page, attention(owner_page, base_url))

    owner_page.click("#logout-button")
    owner_page.wait_for_selector("#auth-gate.is-visible")
    expect(owner_page.locator("#urgent-badge")).to_be_hidden()
    expect(owner_page.locator(".urgent-row")).to_have_count(0)


@pytest.mark.allow_page_errors
def test_a_failed_badge_refresh_hides_the_count_but_keeps_the_mutation(owner_page, base_url):
    put_deadline(owner_page, base_url, "job-a", 0)
    owner_page.reload()
    wait_for_results(owner_page)
    expect(owner_page.locator("#urgent-badge")).to_be_visible()
    owner_page.route("**/api/v1/urgent", lambda route: route.fulfill(status=500, body="{}"))
    owner_page.click("#saved-nav")
    wait_for_results(owner_page)
    owner_page.locator(".card-actions .card-action").first.click()  # Saved ✓ → undo
    expect(owner_page.locator("#action-status")).to_contain_text("Undid your last choice")
    expect_badge(owner_page, 0)


def test_urgent_routing_and_aria_current(owner_page):
    open_urgent(owner_page)
    expect(owner_page).to_have_url(re.compile(r"/urgent$"))
    expect(owner_page.locator("#urgent-nav")).to_have_attribute("aria-current", "page")
    expect(owner_page.locator("#discover-nav")).not_to_have_attribute("aria-current", "page")
    owner_page.click("#saved-nav")
    wait_for_results(owner_page)
    owner_page.go_back()
    wait_for_results(owner_page)
    expect(owner_page.locator("#urgent-nav")).to_have_class("nav-item is-active")
    expect(owner_page.locator("#urgent-nav")).to_have_attribute("aria-current", "page")
    owner_page.go_forward()
    wait_for_results(owner_page)
    expect(owner_page.locator("#saved-nav")).to_have_attribute("aria-current", "page")


def test_urgent_explains_itself_when_nothing_is_due(owner_page, base_url):
    owner_page.route("**/api/v1/urgent*", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({
            "today": iso(0), "timezone": "America/Chicago", "utc_offset": "-05:00", "window_days": 14,
            "counts": {"overdue": 0, "upcoming": 0, "attention": 0},
            "older_overdue": 0, "skipped_count": 0, "skipped": [], "items": [],
        }),
    ))
    open_urgent(owner_page)
    expect(owner_page.locator(".urgent-empty")).to_contain_text("Most postings list no deadline")
    expect(owner_page.get_by_role("button", name="Add to calendar (.ics)")).to_be_disabled()


def _unfold(text: str) -> list[str]:
    return text.replace("\r\n ", "").split("\r\n")


def test_calendar_export_is_valid_and_stable_across_edits(owner_page, base_url, tmp_path):
    company = "Café, Inc; Ünïcode Robotics 🚀 with a deliberately long name to force folding"
    response = owner_page.request.post(
        f"{base_url}/api/v1/outreach", headers=BEARER,
        data={"company": company, "priority": "P1", "deadline_date": iso(3)},
    )
    assert response.status == 201, response.text()
    put_deadline(owner_page, base_url, "job-a", 2)
    open_urgent(owner_page)

    def export(name: str) -> bytes:
        with owner_page.expect_download() as download_info:
            owner_page.get_by_role("button", name="Add to calendar (.ics)").click()
        target = tmp_path / name
        download_info.value.save_as(target)
        return target.read_bytes()

    first = export("first.ics")
    assert first.endswith(b"\r\n")
    for physical in first.split(b"\r\n"):
        assert len(physical) <= 75, f"line longer than 75 octets: {physical!r}"
    text = first.decode("utf-8")
    assert "\n" not in text.replace("\r\n", ""), "bare LF found"
    lines = _unfold(text)
    for required in ("BEGIN:VCALENDAR", "VERSION:2.0", "CALSCALE:GREGORIAN", "END:VCALENDAR"):
        assert required in lines
    assert any(line.startswith("PRODID:") for line in lines)
    events = text.count("BEGIN:VEVENT")
    assert events == text.count("END:VEVENT") and events >= 2
    assert f"DTSTART;VALUE=DATE:{iso(3).replace('-', '')}" in lines
    summary = next(line for line in lines if line.startswith("SUMMARY:") and "Café" in line)
    assert "Café\\, Inc\\; Ünïcode Robotics 🚀" in summary
    uids = sorted(line for line in lines if line.startswith("UID:"))
    assert all(line.endswith("@opportunity-pipeline.local") for line in uids)

    put_deadline(owner_page, base_url, "job-a", 5)
    owner_page.click("#saved-nav")
    open_urgent(owner_page)
    second = export("second.ics").decode("utf-8")
    assert sorted(line for line in _unfold(second) if line.startswith("UID:")) == uids
    assert f"DTSTART;VALUE=DATE:{iso(5).replace('-', '')}" in _unfold(second)


def _undo_saved_job_a(page, base_url) -> None:
    response = page.request.post(
        f"{base_url}/api/v1/opportunities/job-a/actions", headers=BEARER, data={"action": "undo"},
    )
    assert response.status == 200


def _focused_card_id(page) -> str | None:
    return page.evaluate("document.activeElement.closest('.opportunity-card')?.dataset.opportunityId || null")


def test_keyboard_triage_moves_saves_and_opens(owner_page, base_url):
    _undo_saved_job_a(owner_page, base_url)
    owner_page.reload()
    wait_for_results(owner_page)
    expect(owner_page.locator("#keyboard-hint")).to_be_visible()
    cards = owner_page.locator(".opportunity-card .card-button")
    expect(cards).to_have_count(2)
    cards.first.focus()
    first_id = _focused_card_id(owner_page)
    owner_page.keyboard.press("j")
    second_id = _focused_card_id(owner_page)
    assert second_id and second_id != first_id
    owner_page.keyboard.press("k")
    assert _focused_card_id(owner_page) == first_id

    owner_page.keyboard.press("s")
    expect(owner_page.locator("#action-status")).to_contain_text("Saved")

    detail_requests = []
    owner_page.on("request", lambda request: detail_requests.append(request.url)
                  if "/api/v1/opportunities/" in request.url and request.url.rstrip("/").split("/")[-1] in {"job-a", "job-b"} else None)
    owner_page.locator(".opportunity-card .card-button").first.focus()
    owner_page.keyboard.press("Enter")
    expect(owner_page.locator("#detail-panel")).to_be_visible()
    owner_page.wait_for_timeout(300)
    assert len(detail_requests) == 1, f"Enter opened detail {len(detail_requests)} times"
    # Shortcuts are inert behind the open panel.
    owner_page.keyboard.press("p")
    owner_page.keyboard.press("Escape")
    expect(owner_page.locator("#detail-panel")).to_be_hidden()

    owner_page.locator(".opportunity-card .card-button").first.focus()
    owner_page.keyboard.press("o")
    expect(owner_page.locator("#detail-panel")).to_be_visible()


def test_keyboard_triage_ignores_fields_and_modifiers(owner_page, base_url):
    _undo_saved_job_a(owner_page, base_url)
    owner_page.reload()
    wait_for_results(owner_page)
    owner_page.fill("#search-input", "")
    owner_page.focus("#search-input")
    owner_page.keyboard.type("sp")
    expect(owner_page.locator("#search-input")).to_have_value("sp")
    owner_page.fill("#search-input", "")
    wait_for_results(owner_page)
    owner_page.locator(".opportunity-card .card-button").first.focus()
    owner_page.keyboard.press("Alt+s")
    owner_page.wait_for_timeout(300)
    intents = owner_page.request.get(f"{base_url}/api/v1/opportunities", headers=BEARER).json()["items"]
    assert all(item["intent_state"] == "" for item in intents), "a modifier or typed text triggered triage"


def test_pass_by_keyboard_moves_focus_to_the_next_card(owner_page, base_url):
    _undo_saved_job_a(owner_page, base_url)
    owner_page.reload()
    wait_for_results(owner_page)
    owner_page.locator(".opportunity-card .card-button").first.focus()
    passed = _focused_card_id(owner_page)
    owner_page.keyboard.press("p")
    expect(owner_page.locator("#action-status")).to_contain_text("Passed on")
    remaining = _focused_card_id(owner_page)
    assert remaining and remaining != passed


@pytest.mark.parametrize(("days", "soon"), [(13, True), (14, True), (15, False)])
def test_soon_means_fourteen_days(owner_page, base_url, days, soon):
    put_deadline(owner_page, base_url, "job-a", days)
    owner_page.click("#saved-nav")
    wait_for_results(owner_page)
    chip = owner_page.locator(".chip", has_text="Your deadline")
    expect(chip).to_have_count(1)
    if soon:
        expect(chip).to_have_class("chip is-soon")
    else:
        expect(chip).to_have_class("chip")


@pytest.mark.parametrize(("hours", "new"), [(47, True), (48, True), (49, False), (-1, False)])
def test_new_marker_boundaries(owner_page, hours, new):
    # Freeze the browser clock so "exactly 48 hours" really is exactly 48 hours.
    frozen = datetime.now(timezone.utc).replace(microsecond=0)
    owner_page.clock.set_fixed_time(frozen)
    seen = (frozen - timedelta(hours=hours)).isoformat()

    def rewrite(route):
        response = route.fetch()
        body = response.json()
        for item in body["items"]:
            item["first_seen_at"] = seen
        route.fulfill(response=response, json=body)

    owner_page.route("**/api/v1/opportunities?*", rewrite)
    owner_page.click("#saved-nav")
    wait_for_results(owner_page)
    marker = owner_page.locator(".opportunity-card .chip.is-new")
    expect(marker).to_have_count(1 if new else 0)


def test_application_cards_dedupe_chips_and_flag_missed_follow_ups(owner_page):
    owner_page.click("#applications-nav")
    wait_for_results(owner_page)
    card = owner_page.locator(".application-card").first
    labels = [text.strip().casefold() for text in card.locator(".application-facts .chip").all_inner_texts()]
    assert len(labels) == len(set(labels)), f"duplicate chips: {labels}"
    # The fixture's follow-up date (Aug 16, 2026) is in the past.
    expect(card.locator(".chip.is-warning", has_text="Overdue follow-up")).to_have_count(1)
    expect(owner_page.locator(".tracker-summary .chip", has_text="overdue")).not_to_have_text("0 overdue")


def test_urgent_blocks_span_the_whole_results_area(owner_page, base_url):
    """#results is the deck's two-column grid; Urgent's blocks must not land in one column."""
    put_deadline(owner_page, base_url, "job-a", 1)
    owner_page.set_viewport_size({"width": 1280, "height": 900})
    open_urgent(owner_page)
    widths = owner_page.evaluate("""() => {
      const results = document.getElementById('results').getBoundingClientRect().width;
      return [...document.querySelectorAll('.urgent-toolbar, .urgent-group')]
        .map((node) => node.getBoundingClientRect().width / results);
    }""")
    assert widths and all(ratio > 0.98 for ratio in widths), widths


def _urgent_requests(page) -> list[str]:
    seen: list[str] = []
    page.on("request", lambda request: seen.append(request.url) if "/api/v1/urgent" in request.url else None)
    return seen


def test_a_mutation_alone_refreshes_the_badge(owner_page, base_url):
    """No navigation: only the api() hook can update the badge here."""
    _undo_saved_job_a(owner_page, base_url)
    put_deadline(owner_page, base_url, "job-a", 0)
    owner_page.reload()
    wait_for_results(owner_page)
    expect_badge(owner_page, attention(owner_page, base_url))
    requests = _urgent_requests(owner_page)
    card = owner_page.locator('.opportunity-card[data-opportunity-id="job-a"]')
    card.get_by_role("button", name="Pass").click()
    expect(owner_page.locator("#action-status")).to_contain_text("Passed on")
    expect_badge(owner_page, attention(owner_page, base_url))
    assert requests, "passing a role never re-read the Urgent badge"


def test_a_stale_in_flight_badge_response_is_discarded(owner_page, base_url):
    put_deadline(owner_page, base_url, "job-a", 0)
    held = []

    def hold_first(route):
        # Hold only the first request in flight; later ones reach the server.
        if not held:
            held.append(route)
        else:
            route.continue_()

    owner_page.route("**/api/v1/urgent", hold_first)
    owner_page.click("#saved-nav")  # request 1 is held in flight
    for _ in range(50):
        if held:
            break
        owner_page.wait_for_timeout(50)
    assert held, "the badge request never started"
    owner_page.click("#discover-nav")  # invalidated while in flight: one trailing refresh
    wait_for_results(owner_page)
    stale = {"counts": {"attention": 99, "overdue": 0, "upcoming": 0}}
    held[0].fulfill(status=200, content_type="application/json", body=json.dumps(stale))
    expect_badge(owner_page, attention(owner_page, base_url))
    expect(owner_page.locator("#urgent-badge")).not_to_have_text("99")


@pytest.mark.allow_page_errors
def test_a_failed_badge_retries_exactly_once_after_thirty_seconds(page, base_url):
    page.clock.install()
    page.goto("/")
    sign_in_as_owner(page)
    wait_for_results(page)
    hits = []

    def fail(route):
        hits.append(route.request.url)
        route.fulfill(status=500, body="{}")

    page.route("**/api/v1/urgent", fail)
    page.click("#saved-nav")
    wait_for_results(page)
    expect_badge(page, 0)
    before = len(hits)
    assert before >= 1
    page.clock.run_for(29_000)
    assert len(hits) == before, "retried before 30 seconds"
    page.clock.run_for(2_000)
    page.wait_for_timeout(100)
    assert len(hits) == before + 1, f"expected exactly one retry, saw {len(hits) - before}"


def test_open_application_lands_on_that_application(owner_page, base_url):
    response = owner_page.request.post(
        f"{base_url}/api/v1/applications/app-job-b/tasks", headers=BEARER,
        data={"title": "Send transcript", "due_at": f"{iso(1)}T09:00"},
    )
    assert response.status == 201, response.text()
    open_urgent(owner_page)
    row = owner_page.locator(".urgent-row", has_text="Send transcript")
    row.get_by_role("button").click()
    wait_for_results(owner_page)
    expect(owner_page.locator("#applications-nav")).to_have_attribute("aria-current", "page")
    focused = owner_page.evaluate("document.activeElement.dataset.applicationId || null")
    assert focused == "app-job-b"
    expect(owner_page.locator(".application-card.is-requested")).to_have_count(1)


def test_clearing_a_deadline_on_a_retired_posting_keeps_focus_in_the_panel(owner_page, base_url):
    put_deadline(owner_page, base_url, "job-a", 3)

    def retired(route):
        response = route.fetch()
        body = response.json()
        body["can_set_user_deadline"] = False
        route.fulfill(response=response, json=body)

    owner_page.route("**/api/v1/opportunities/job-a", retired)
    owner_page.click("#saved-nav")
    wait_for_results(owner_page)
    owner_page.locator(".card-button").first.click()
    section = owner_page.locator(".user-deadline")
    expect(section.locator("form")).to_be_hidden()
    section.get_by_role("button", name="Clear deadline").click()
    expect(section.locator(".form-status")).to_have_text("Cleared.")
    assert owner_page.evaluate("document.activeElement.classList.contains('user-deadline-current')")


@pytest.mark.allow_page_errors
def test_a_successful_urgent_load_cancels_a_pending_badge_retry(page, base_url):
    """The badge fetch has no query string; the Urgent view asks for ?days=14."""
    page.clock.install()
    page.goto("/")
    sign_in_as_owner(page)
    wait_for_results(page)
    put_deadline(page, base_url, "job-a", 0)
    hits = []

    def fail_badge(route):
        hits.append(route.request.url)
        route.fulfill(status=500, body="{}")

    page.route("**/api/v1/urgent", fail_badge)
    page.click("#saved-nav")
    wait_for_results(page)
    expect_badge(page, 0)
    before = len(hits)
    open_urgent(page)  # succeeds, and must cancel the scheduled retry
    expect_badge(page, attention(page, base_url))
    page.clock.run_for(31_000)
    page.wait_for_timeout(100)
    assert len(hits) == before, "a stale retry fired after a successful Urgent load"
    expect_badge(page, attention(page, base_url))


def test_open_outreach_lands_on_that_company(owner_page, base_url):
    response = owner_page.request.post(
        f"{base_url}/api/v1/outreach", headers=BEARER,
        data={"company": "Bovi Robotics", "priority": "P1", "deadline_date": iso(4)},
    )
    assert response.status == 201, response.text()
    target_id = response.json()["id"]
    open_urgent(owner_page)
    owner_page.locator(".urgent-row", has_text="Bovi Robotics").get_by_role("button").click()
    wait_for_results(owner_page)
    expect(owner_page.locator("#outreach-nav")).to_have_attribute("aria-current", "page")
    assert owner_page.evaluate("document.activeElement.dataset.outreachId || null") == target_id
