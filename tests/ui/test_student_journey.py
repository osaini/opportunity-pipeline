"""The student journey, driven through the real UI.

tests/test_e2e_smoke.py already covers this path at the API layer with
TestClient. This module covers what that layer cannot see: whether the rendered
interface actually lets a person complete the journey — filtering, opening a
posting, saving it, and finding it again on the Saved view.

The seeded fixture (tests/helpers_platform.py) provides two opportunities:
job-a "Acme Robotics" arrives already saved (legacy status "shortlisted") and
job-b "Orbit Systems" arrives unsaved.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from conftest import sign_in_as_owner, wait_for_results

SAVED_COMPANY = "Acme Robotics"
UNSAVED_COMPANY = "Orbit Systems"

def _card_count(page) -> int:
    return page.locator(".opportunity-card").count()


def _card_for(page, company: str):
    return page.locator(".opportunity-card").filter(has_text=company)


def test_stats_render_real_numbers_not_placeholders(owner_page):
    """The dashes are the pre-load placeholder; leaving them means the fetch failed."""
    for stat in ("stat-active", "stat-total", "stat-tracked", "stat-score"):
        expect(owner_page.locator(f"#{stat}")).not_to_have_text("—")


def test_result_count_agrees_with_the_number_of_cards(owner_page):
    """A heading that disagrees with the deck below it is a trust problem."""
    heading = owner_page.locator("#result-count").inner_text()
    assert "Loading" not in heading, "the result heading never left its loading state"
    assert str(_card_count(owner_page)) in heading, (
        f"heading {heading!r} does not match {_card_count(owner_page)} rendered cards"
    )


def test_every_card_keeps_its_source_attribution(owner_page):
    """Source-linking is the product's core promise; assert it per card."""
    cards = owner_page.locator(".opportunity-card")
    assert cards.count() > 0, "the seeded database produced no cards"
    for index in range(cards.count()):
        card = cards.nth(index)
        expect(card.locator(".source-line")).to_contain_text("checked")
        expect(card.locator(".company-name")).not_to_be_empty()
        apply_link = card.locator("a.is-apply")
        expect(apply_link).to_have_attribute("rel", "noopener noreferrer")
        assert apply_link.get_attribute("href").startswith("http"), (
            "the apply link must point at the original posting"
        )


def test_scores_and_explanations_are_shown_together(owner_page):
    """A ranked list without its reason is the thing this product exists not to be."""
    cards = owner_page.locator(".opportunity-card")
    for index in range(cards.count()):
        card = cards.nth(index)
        expect(card.locator(".fit-score strong")).to_have_text(re.compile(r"^\d+$"))
        expect(card.locator(".top-reason")).not_to_be_empty()


def test_search_narrows_the_deck_and_clearing_restores_it(owner_page):
    baseline = _card_count(owner_page)
    assert baseline > 0

    owner_page.fill("#search-input", "zzzz-no-such-role-zzzz")
    owner_page.wait_for_function(
        "() => document.querySelectorAll('.opportunity-card').length === 0", timeout=10_000
    )
    expect(owner_page.locator(".empty-state")).to_be_visible()

    owner_page.fill("#search-input", "")
    owner_page.wait_for_function(
        "(expected) => document.querySelectorAll('.opportunity-card').length === expected",
        arg=baseline,
        timeout=10_000,
    )


def test_sorting_reorders_the_deck_without_losing_results(owner_page):
    before = owner_page.locator(".opportunity-card .company-name").all_inner_texts()
    owner_page.select_option("#sort-filter", "company")
    wait_for_results(owner_page)
    after = owner_page.locator(".opportunity-card .company-name").all_inner_texts()
    assert after == sorted(after), f"sorting by company did not produce alphabetical order: {after}"
    assert set(after) == set(before), "sorting changed which opportunities are shown"


def test_work_mode_filter_restricts_the_deck(owner_page):
    """Facet filters must actually filter, not just repaint the control."""
    # The work-mode select lives inside the collapsed "More filters" <details>.
    owner_page.locator(".advanced-filters > summary").click()
    owner_page.select_option("#remote-filter", "remote")
    wait_for_results(owner_page)
    companies = owner_page.locator(".opportunity-card .company-name").all_inner_texts()
    assert UNSAVED_COMPANY in companies, "the remote-only role disappeared from a remote filter"
    assert SAVED_COMPANY not in companies, "an on-site role survived a remote-only filter"


def test_opening_a_card_shows_the_scored_explanation(owner_page):
    """The detail panel must explain the ranking, not just repeat the listing."""
    owner_page.locator(".opportunity-card .card-button").first.click()
    owner_page.wait_for_selector("#detail-panel.is-open")
    expect(owner_page.locator("#detail-content")).not_to_contain_text("Loading opportunity…")
    expect(owner_page.locator("#detail-content a.primary-link")).to_have_attribute(
        "rel", "noopener noreferrer"
    )


def test_jev_review_is_explicit_uncertain_and_does_not_replace_the_score(owner_page):
    owner_page.locator(".opportunity-card .card-button").first.click()
    owner_page.wait_for_selector("#detail-panel.is-open")
    original_score = owner_page.locator(".detail-score > strong").inner_text()
    button = owner_page.get_by_role("button", name="Run Jev review")
    expect(button).to_be_enabled()
    expect(owner_page.locator(".jev-intro")).to_contain_text("citizenship")
    expect(owner_page.locator(".jev-intro")).to_contain_text("sponsorship")

    with owner_page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith("/jev-review")
    ) as reviewed:
        button.click()
    assert reviewed.value.ok, reviewed.value.status

    expect(owner_page.locator(".jev-answer")).to_have_count(8)
    expect(owner_page.locator(".jev-meta")).to_contain_text("Unconfirmed AI suggestion")
    expect(owner_page.locator(".jev-meta")).to_contain_text("score unchanged")
    expect(owner_page.locator(".detail-score > strong")).to_have_text(original_score)


def test_seeded_saved_opportunity_appears_on_the_saved_view(owner_page):
    """Saved roles belong to the shortlist, not the Discover review queue."""
    expect(_card_for(owner_page, SAVED_COMPANY)).to_be_hidden()

    owner_page.click("#saved-nav")
    wait_for_results(owner_page)
    companies = owner_page.locator(".opportunity-card .company-name").all_inner_texts()
    assert companies == [SAVED_COMPANY], f"Saved view showed {companies}"


def test_saving_a_card_persists_to_the_saved_view(owner_page):
    card = _card_for(owner_page, UNSAVED_COMPANY)
    card.get_by_role("button", name="Save", exact=True).click()
    expect(card).to_be_hidden()
    expect(owner_page.locator("#result-count")).to_have_text("0 to review")
    expect(owner_page.locator("#error-banner")).to_be_hidden()

    owner_page.click("#saved-nav")
    wait_for_results(owner_page)
    companies = owner_page.locator(".opportunity-card .company-name").all_inner_texts()
    assert UNSAVED_COMPANY in companies

    # Surviving a reload proves the write reached the server, not just the outbox.
    owner_page.reload()
    wait_for_results(owner_page)
    assert UNSAVED_COMPANY in owner_page.locator(".opportunity-card .company-name").all_inner_texts()


def test_passing_a_card_removes_it_from_discover(owner_page):
    card = _card_for(owner_page, UNSAVED_COMPANY)
    # The pass is written asynchronously. Without waiting for the response the
    # reload can race ahead of it and bring the card back.
    with owner_page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith("/actions")
    ) as written:
        card.get_by_role("button", name="Pass", exact=True).click()
    assert written.value.ok, written.value.status
    expect(owner_page.locator("#error-banner")).to_be_hidden()

    owner_page.reload()
    wait_for_results(owner_page)
    assert UNSAVED_COMPANY not in owner_page.locator(".opportunity-card .company-name").all_inner_texts()


@pytest.mark.allow_page_errors
def test_a_rejected_mutation_never_reports_success_in_the_ui(owner_page):
    """A failed write must not leave a success label behind.

    This currently passes: runIntent's catch branch skips the re-render, so the
    card keeps its "Save" label when the 403 comes back. It is here as the
    regression guard for that behaviour, and it stays meaningful after the CSRF
    defect is fixed — any future write path that updates the label optimistically
    before confirming the response will trip it.
    """
    card = _card_for(owner_page, UNSAVED_COMPANY)
    card.get_by_role("button", name="Save", exact=True).click()
    owner_page.wait_for_timeout(1_000)
    banner_visible = owner_page.locator("#error-banner").is_visible()
    claims_saved = card.get_by_role("button", name="Saved ✓").count() > 0
    assert not (banner_visible and claims_saved), (
        "the card reports 'Saved ✓' while the error banner reports a failure"
    )


def test_switching_between_card_and_list_view_keeps_the_results(owner_page):
    cards = _card_count(owner_page)
    owner_page.click("#list-view-button")
    expect(owner_page.locator("#list-view-button")).to_have_attribute("aria-pressed", "true")
    expect(owner_page.locator("#card-view-button")).to_have_attribute("aria-pressed", "false")
    wait_for_results(owner_page)
    assert _card_count(owner_page) == cards, "switching display mode changed the result set"


def test_mock_interview_recording_has_visible_state_and_private_upload(owner_page):
    owner_page.add_init_script(
        """
        Object.defineProperty(navigator, 'mediaDevices', {
          configurable: true,
          value: {getUserMedia: async () => ({getTracks: () => [{stop() { window.__mockTrackStopped = true; }}]})}
        });
        class TestMediaRecorder {
          constructor(stream) { this.stream = stream; this.state = 'inactive'; this.mimeType = 'audio/webm'; this.events = {}; }
          addEventListener(name, callback) { (this.events[name] ||= []).push(callback); }
          emit(name, event = {}) { (this.events[name] || []).forEach((callback) => callback(event)); }
          start() { this.state = 'recording'; }
          stop() {
            this.state = 'inactive';
            this.emit('dataavailable', {data: new Blob([new Uint8Array([0x1a, 0x45, 0xdf, 0xa3, 1])], {type: 'audio/webm'})});
            this.emit('stop');
          }
        }
        window.MediaRecorder = TestMediaRecorder;
        """
    )
    owner_page.reload()
    wait_for_results(owner_page)
    owner_page.click("#prepare-nav")
    expect(owner_page.locator("#result-count")).to_have_text("Preparation workspace")
    owner_page.get_by_role("button", name="Start mock interview").click()
    question = owner_page.locator(".interview-question").first
    expect(question).to_be_visible()
    question.locator("textarea").fill(
        "Situation, task, action, and result: I reviewed this spoken answer before scoring."
    )
    record = question.locator("button[aria-pressed]").nth(1)
    record.click()
    expect(record).to_have_attribute("aria-pressed", "true")
    expect(question.get_by_text("Microphone active", exact=False)).to_be_visible()
    record.click()
    expect(question.get_by_text("Recording ready", exact=False)).to_be_visible()
    with owner_page.expect_response(
        lambda response: "/recorded-answers" in response.url and response.request.method == "POST"
    ) as uploaded:
        question.get_by_role("button", name="Score reviewed answer").click()
    assert uploaded.value.status == 201, uploaded.value.text()
    expect(question.get_by_text("Recording saved privately", exact=False)).to_be_visible()
    player = question.locator(".interview-feedback audio")
    expect(player).to_have_attribute("src", re.compile(r"/api/v1/preparation/answers/[^/]+/audio$"))
    played = owner_page.request.get(player.get_attribute("src"))
    assert played.status == 200 and played.body().startswith(bytes([0x1A, 0x45, 0xDF, 0xA3])), played.status
    owner_page.evaluate("window.__mockTrackStopped = false")
    record.click()
    expect(record).to_have_attribute("aria-pressed", "true")
    owner_page.click("#applications-nav")
    assert owner_page.evaluate("window.__mockTrackStopped") is True


    # The practice is still there after leaving, with the recording playable.
    owner_page.click("#prepare-nav")
    expect(owner_page.locator("#result-count")).to_have_text("Preparation workspace")
    past = owner_page.locator("details.interview-past")
    past.locator("summary").click()
    past.get_by_role("button", name=re.compile("^Open the ")).first.click()
    earlier = owner_page.locator(".interview-question").first.locator("details.interview-earlier")
    expect(earlier.locator("summary")).to_have_text("1 earlier answer")
    earlier.locator("summary").click()
    expect(earlier.locator("audio")).to_have_count(1)


def test_the_app_lets_its_own_pages_ask_for_the_microphone(owner_page):
    # The recording test above stubs getUserMedia, so it could not see a
    # Permissions-Policy of microphone=() blocking the real one on every page.
    assert owner_page.evaluate("document.featurePolicy.allowsFeature('microphone')") is True
    assert owner_page.evaluate("document.featurePolicy.allowsFeature('camera')") is False


def test_signing_out_returns_to_the_gate_and_stays_there(owner_page):
    # Wait for the server to actually drop the session before reloading, or the
    # reload races the DELETE and the assertion proves nothing.
    with owner_page.expect_response(
        lambda response: response.url.endswith("/api/v1/session")
        and response.request.method == "DELETE"
    ) as signed_out:
        owner_page.click("#logout-button")
    assert signed_out.value.ok, f"sign-out failed: {signed_out.value.status}"

    expect(owner_page.locator("#auth-gate")).to_have_class("auth-gate is-visible")
    owner_page.reload()
    expect(owner_page.locator("#auth-gate")).to_have_class("auth-gate is-visible")


# Throwaway credentials for an account that only exists in the per-test database.
BLANK_EMAIL = "blank@example.com"
BLANK_PASSWORD = "BlankStudent123"  # gitleaks:allow


def _signed_in_blank_student(page):
    """Register a brand-new student with an empty profile and open Discover."""
    from conftest import ADMIN_TOKEN

    flag = page.request.put(
        "/api/v1/admin/feature-flags/allow_public_signup",
        headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
        data={"enabled": True},
    )
    assert flag.ok, flag.text()
    registered = page.request.post(
        "/api/v1/auth/register",
        data={"email": BLANK_EMAIL, "password": BLANK_PASSWORD, "display_name": "Blank"},
    )
    assert registered.status == 201, registered.text()
    page.goto("/")
    page.wait_for_selector("#auth-gate.is-visible")
    page.fill("#auth-email", BLANK_EMAIL)
    page.fill("#auth-password", BLANK_PASSWORD)
    page.click("#auth-submit")
    page.wait_for_selector("#auth-gate.is-visible", state="detached", timeout=15_000)
    wait_for_results(page)
    return page


def test_blank_profile_is_prompted_to_personalize_matches(page, base_url):
    page = _signed_in_blank_student(page)
    prompt = page.locator("#personalize-prompt")
    expect(prompt).to_be_visible()
    expect(page.locator(".opportunity-card .top-reason").first).to_contain_text("complete your profile")

    page.click("#personalize-button")
    expect(page).to_have_url(re.compile(r"/profile$"))
    expect(prompt).to_be_hidden()
    # Opening Profile fires a burst of requests. Let them finish so the server
    # is not holding the database open when the next test's reset copies over
    # it, which Windows refuses with a sharing violation.
    page.wait_for_load_state("networkidle")


def test_personalized_owner_sees_no_profile_prompt(owner_page):
    expect(owner_page.locator("#personalize-prompt")).to_be_hidden()


OUTBOX_PREFIX = "opportunity-action-outbox"
ACTIONS_ROUTE = "**/api/v1/opportunities/*/actions"


def _outbox_entries(page) -> dict:
    return page.evaluate(
        """(prefix) => Object.fromEntries(Object.keys(localStorage)
            .filter((key) => key.startsWith(prefix))
            .map((key) => [key, JSON.parse(localStorage.getItem(key))]))""",
        OUTBOX_PREFIX,
    )


@pytest.mark.allow_page_errors
def test_a_server_error_is_shown_as_itself_and_never_queued(owner_page):
    owner_page.route(
        ACTIONS_ROUTE,
        lambda route: route.fulfill(status=500, content_type="application/json", body='{"detail": "Injected failure"}'),
    )
    card = _card_for(owner_page, UNSAVED_COMPANY)
    card.get_by_role("button", name="Save", exact=True).click()

    expect(owner_page.locator("#error-banner")).to_have_text("Injected failure")
    expect(card.get_by_role("button", name="Save", exact=True)).to_be_visible()
    assert _outbox_entries(owner_page) == {}


@pytest.mark.allow_page_errors
def test_an_offline_action_is_queued_for_this_user_and_synced_on_return(owner_page):
    owner_page.route(ACTIONS_ROUTE, lambda route: route.abort())
    _card_for(owner_page, UNSAVED_COMPANY).get_by_role("button", name="Save", exact=True).click()
    expect(owner_page.locator("#error-banner")).to_contain_text("Connection lost")

    entries = _outbox_entries(owner_page)
    assert list(entries) == [f"{OUTBOX_PREFIX}:local-user"], entries
    assert [item["action"] for item in entries[f"{OUTBOX_PREFIX}:local-user"]] == ["saved"]

    owner_page.unroute(ACTIONS_ROUTE)
    owner_page.reload()
    wait_for_results(owner_page)
    expect(owner_page.locator("#action-status")).to_have_text("Synced 1 queued action.")
    assert _outbox_entries(owner_page) == {}
    owner_page.click("#saved-nav")
    expect(_card_for(owner_page, UNSAVED_COMPANY)).to_have_count(1)


def test_signing_out_clears_private_data_and_signing_back_in_does_not_duplicate_filters(owner_page):
    with owner_page.expect_response(
        lambda response: response.url.endswith("/api/v1/session") and response.request.method == "DELETE"
    ):
        owner_page.click("#logout-button")
    expect(owner_page.locator("#auth-gate")).to_have_class("auth-gate is-visible")
    expect(owner_page.locator("#results .opportunity-card")).to_have_count(0)
    expect(owner_page.locator("#user-name")).to_have_text("")
    assert "Acme Robotics" not in owner_page.content()

    sign_in_as_owner(owner_page)
    wait_for_results(owner_page)
    for select_id in ("role-filter", "region-filter", "source-filter", "term-filter"):
        values = owner_page.locator(f"#{select_id} option").evaluate_all("(options) => options.map((option) => option.value)")
        assert len(values) == len(set(values)), f"{select_id} has duplicate options: {values}"


@pytest.mark.allow_page_errors
def test_an_expired_session_hides_the_previous_workspace(owner_page):
    expect(owner_page.locator("#user-name")).to_have_text("Test Student")
    owner_page.context.clear_cookies()
    owner_page.click("#saved-nav")
    expect(owner_page.locator("#auth-gate")).to_have_class("auth-gate is-visible")
    expect(owner_page.locator("#results .opportunity-card")).to_have_count(0)
    expect(owner_page.locator("#user-name")).to_have_text("")
    expect(owner_page.locator("#stat-score")).to_have_text("—")


def test_deadlines_keep_their_calendar_day_west_of_utc(browser, base_url):
    """Acme's posting says "Apply by September 1"; UTC midnight read as an
    instant in Chicago used to render as Aug 31."""
    context = browser.new_context(base_url=base_url, timezone_id="America/Chicago")
    try:
        page = context.new_page()
        page.goto("/")
        sign_in_as_owner(page)
        wait_for_results(page)
        page.click("#saved-nav")
        card = _card_for(page, SAVED_COMPANY)
        expect(card).to_contain_text("Deadline passed · Sep 1, 2026")
        card.locator(".card-button").click()
        expect(page.locator("#detail-panel")).to_contain_text("Sep 1, 2026 (passed)")
    finally:
        context.close()


def test_the_score_explanation_accounts_for_the_whole_score(owner_page):
    owner_page.request.put(
        "/api/v1/profile",
        data={"updates": {"skills": ["SolidWorks"]}, "confirmed_fields": ["skills"]},
    )
    owner_page.reload()
    wait_for_results(owner_page)
    owner_page.click("#saved-nav")
    _card_for(owner_page, SAVED_COMPANY).locator(".card-button").click()
    panel = owner_page.locator("#detail-panel")
    expect(panel.locator(".reason-list li").first).to_have_text("Starting score +35")

    score = int(panel.locator(".detail-score strong").inner_text().split("/")[0])
    listed = sum(
        int(match.group(1) + match.group(2))
        for text in panel.locator(".reason-list li").all_inner_texts()
        if (match := re.match(r"^(?:Starting score )?([+-])(\d+)", text))
    )
    assert listed == score or panel.locator(".score-note").count() == 1


def test_single_counts_read_as_singular(owner_page):
    owner_page.click("#saved-nav")
    expect(owner_page.locator("#result-count")).to_have_text("1 saved role")
    owner_page.click("#applications-nav")
    expect(owner_page.locator("#result-count")).to_have_text("1 application")


def test_the_applications_tile_matches_the_applications_view(owner_page):
    tile = owner_page.locator("#stat-tracked")
    expect(tile).not_to_have_text("—")
    owner_page.click("#applications-nav")
    expect(owner_page.locator(".application-card")).to_have_count(int(tile.inner_text()))


def test_saving_tracker_details_without_edits_keeps_the_follow_up(owner_page):
    """The seeded follow-up is date-only, which a datetime-local input cannot
    display; the empty field used to be saved back as "clear the reminder"."""
    before = owner_page.request.get("/api/v1/applications").json()["items"][0]
    assert before["follow_up_at"], "fixture should seed a follow-up"

    owner_page.click("#applications-nav")
    card = owner_page.locator(".application-card").filter(has_text=UNSAVED_COMPANY)
    follow_up = card.get_by_label("Follow up")
    expect(follow_up).not_to_have_value("")

    card.get_by_role("button", name="Save details").click()
    refreshed = owner_page.locator(".application-card").filter(has_text=UNSAVED_COMPANY)
    expect(refreshed.locator(".form-status")).to_have_text("Saved.")
    expect(refreshed).to_contain_text("Follow up")

    after = owner_page.request.get("/api/v1/applications").json()["items"][0]
    assert after["follow_up_at"] == before["follow_up_at"]


LIST_ROUTE = re.compile(r".*/api/v1/opportunities\?.*")


def test_jumping_to_a_page_requests_that_page_and_clamps_out_of_range(owner_page):
    """The seed holds two roles, so the list response is stretched to five pages."""
    requested_offsets: list[int] = []

    def five_pages(route):
        response = route.fetch()
        payload = response.json()
        offset = int(re.search(r"offset=(\d+)", route.request.url).group(1))
        requested_offsets.append(offset)
        payload.update(total=120, offset=offset)
        route.fulfill(response=response, json=payload)

    owner_page.route(LIST_ROUTE, five_pages)
    owner_page.select_option("#sort-filter", "company")
    expect(owner_page.locator("#pagination-label")).to_have_text("Page 1 of 5")

    owner_page.fill("#page-jump-input", "3")
    owner_page.press("#page-jump-input", "Enter")
    expect(owner_page.locator("#pagination-label")).to_have_text("Page 3 of 5")
    assert requested_offsets[-1] == 48

    owner_page.fill("#page-jump-input", "99")
    owner_page.locator("#pagination").get_by_role("button", name="Go", exact=True).click()
    expect(owner_page.locator("#pagination-label")).to_have_text("Page 5 of 5")
    expect(owner_page.locator("#page-jump-input")).to_have_value("5")
    assert requested_offsets[-1] == 96


def test_the_top_pagination_bar_mirrors_and_drives_the_bottom_one(owner_page):
    """Changing pages must not require scrolling past the whole deck."""
    def five_pages(route):
        response = route.fetch()
        offset = int(re.search(r"offset=(\d+)", route.request.url).group(1))
        route.fulfill(response=response, json={**response.json(), "total": 120, "offset": offset})

    owner_page.route(LIST_ROUTE, five_pages)
    owner_page.select_option("#sort-filter", "company")
    top = owner_page.locator("#pagination-top")
    bottom = owner_page.locator("#pagination")
    expect(top.locator("[data-page-label]")).to_have_text("Page 1 of 5")
    expect(top.get_by_role("button", name="← Previous")).to_be_disabled()

    top.get_by_role("button", name="Next →").click()
    expect(top.locator("[data-page-label]")).to_have_text("Page 2 of 5")
    expect(bottom.locator("[data-page-label]")).to_have_text("Page 2 of 5")

    top.get_by_label("Go to page").fill("4")
    top.get_by_label("Go to page").press("Enter")
    expect(bottom.locator("[data-page-label]")).to_have_text("Page 4 of 5")
    expect(bottom.get_by_label("Go to page")).to_have_value("4")


REFRESH_ROUTE = "**/api/v1/refresh"
REFRESH_LABELS = [
    ("pull", "Pull new postings"),
    ("liveness", "Check posting links"),
    ("purge-legacy", "Purge expired postings (daily database)"),
    ("sync", "Sync postings into this app"),
    ("purge-app", "Purge expired postings (this app)"),
]


def _refresh_payload(state, progress):
    """``progress`` maps a step key to (state, done, total, detail)."""
    steps = []
    for key, label in REFRESH_LABELS:
        step_state, done, total, detail = progress.get(key, ("pending", 0, 0, ""))
        steps.append({"key": key, "label": label, "state": step_state, "done": done, "total": total, "detail": detail})
    return {
        "available": True,
        "state": state,
        "started_at": "2026-09-15T20:00:00+00:00",
        "finished_at": "2026-09-15T20:05:00+00:00" if state == "succeeded" else None,
        "error": None,
        "steps": steps,
    }


def test_the_sandbox_database_offers_no_live_refresh(owner_page):
    """The test server runs over a temporary database; it must never fetch for real."""
    owner_page.click("#refresh-open")
    dialog = owner_page.locator("#refresh-dialog")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("button", name="Start refresh")).to_be_disabled()
    expect(owner_page.locator("#refresh-status")).to_contain_text("only available")


def test_a_manual_refresh_shows_step_progress_and_reloads_when_done(owner_page):
    responses = [
        _refresh_payload("running", {
            "pull": ("running", 2, 4, "Fetching Orbit"),
        }),
        _refresh_payload("running", {
            "pull": ("done", 4, 4, "Fetched 3 sources"),
            "liveness": ("done", 1, 1, "Checked 2; retired 0"),
            "purge-legacy": ("done", 1, 1, "Deleted 1"),
            "sync": ("running", 0, 0, ""),
        }),
        _refresh_payload("succeeded", {key: ("done", 1, 1, "ok") for key, _ in REFRESH_LABELS}),
    ]
    idle = {**_refresh_payload("idle", {}), "started_at": None, "finished_at": None}
    polls = {"started": False}

    def refresh(route):
        if route.request.method == "POST":
            polls["started"] = True
            route.fulfill(status=202, json=responses[0])
        elif not polls["started"]:
            route.fulfill(json=idle)
        else:
            route.fulfill(json=responses.pop(0) if len(responses) > 1 else responses[0])

    owner_page.route(REFRESH_ROUTE, refresh)
    owner_page.click("#refresh-open")
    dialog = owner_page.locator("#refresh-dialog")
    dialog.get_by_role("button", name="Start refresh").click()

    pull = dialog.locator('[data-step="pull"]')
    expect(pull).to_have_class("is-running")
    expect(pull.locator("progress")).to_have_attribute("value", "50")
    expect(pull).to_contain_text("Fetching Orbit")
    expect(dialog.get_by_role("button", name="Refreshing…")).to_be_disabled()

    # A running step of unknown size is indeterminate rather than stuck at 0%.
    sync = dialog.locator('[data-step="sync"]')
    expect(sync).to_have_class("is-running")
    expect(sync.locator("progress")).not_to_have_attribute("value", re.compile(".*"))
    expect(owner_page.locator("#refresh-overall-percent")).to_have_text("60%")

    with owner_page.expect_response("**/api/v1/opportunities?*"):
        expect(owner_page.locator("#refresh-status")).to_contain_text("Finished")
    expect(owner_page.locator("#refresh-overall")).to_have_attribute("value", "100")
    expect(dialog.get_by_role("button", name="Run again")).to_be_enabled()
