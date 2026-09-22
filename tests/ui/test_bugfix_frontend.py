"""Front-end regressions from the 2026-09-21 bug-fix pass.

Each test drives a real page against the seeded sandbox:

* auto-saving selects (Programs status, application stage) commit on Enter,
  blur, or a pointer choice, never on each value arrow keys pass through;
* focus survives the Programs list being rebuilt after a save;
* a FastAPI 422 reads as a sentence, never "[object Object]";
* a closed program reads "Closed", never "N days left";
* an application import reports its result after the reload, with the rows it
  skipped.

The Programs rows come from tests/fixtures/early_programs.json.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

import pytest
from playwright.sync_api import expect

from conftest import OWNER_TOKEN

ROLLING = "sandbox-rolling-internship"
FUTURE = "sandbox-future-research"
CLOSED = "sandbox-closed-program"
PROGRAMS_LIST = re.compile(r".*/api/v1/early-programs(\?.*)?$")
PROGRAM_STATUS = re.compile(r".*/api/v1/early-programs/[^/]+/status$")
APPLICATION_PATCH = re.compile(r".*/api/v1/applications/[^/?]+$")


def open_programs(page) -> None:
    """Open Programs on "All programs" (the tab itself opens on "Open now")."""
    page.click("#programs-nav")
    expect(page.locator(".program-row")).to_have_count(1)
    page.locator('#subnav [data-subtab="all"]').click()
    expect(page.locator(".program-row")).to_have_count(3)


def program_select(page, program_id: str):
    return page.locator(f'.program-row[data-program-id="{program_id}"] select.program-status')


def record(page, method: str, pattern: re.Pattern) -> list:
    seen: list = []
    page.on("request", lambda request: seen.append(request) if request.method == method and pattern.match(request.url) else None)
    return seen


def focused_control(page) -> dict | None:
    return page.evaluate(
        """() => {
            const active = document.activeElement;
            const row = active && active.closest('.program-row');
            if (!row) return null;
            return { id: row.dataset.programId, role: active.dataset.programControl || null, stale: Boolean(active.__stale) };
        }"""
    )


def program_status(page, program_id: str) -> str:
    payload = page.request.get("/api/v1/early-programs").json()
    return next(item["status"] for item in payload["items"] if item["id"] == program_id)


def test_arrow_keys_on_a_program_status_do_not_save_and_enter_saves_once(owner_page):
    open_programs(owner_page)
    puts = record(owner_page, "PUT", PROGRAM_STATUS)
    select = program_select(owner_page, ROLLING)
    expect(select).to_have_value("todo")

    select.focus()
    owner_page.keyboard.press("ArrowDown")
    owner_page.keyboard.press("ArrowDown")
    expect(select).to_have_value("skipped")
    owner_page.keyboard.press("ArrowUp")
    expect(select).to_have_value("applied")
    owner_page.wait_for_timeout(400)
    assert puts == [], "keyboard browsing saved an intermediate status"
    assert program_status(owner_page, ROLLING) == "todo"

    with owner_page.expect_response(PROGRAMS_LIST):
        owner_page.keyboard.press("Enter")
    expect(owner_page.locator(f'.is-programs-done .program-row[data-program-id="{ROLLING}"]')).to_be_visible()
    owner_page.wait_for_timeout(300)
    assert len(puts) == 1
    assert json.loads(puts[0].post_data) == {"status": "applied"}
    assert program_status(owner_page, ROLLING) == "applied"
    # The rebuilt row's select has focus back, so Enter never strands the user.
    assert focused_control(owner_page) == {"id": ROLLING, "role": "status", "stale": False}


def test_escape_restores_the_saved_status_and_leaving_then_saves_nothing(owner_page):
    open_programs(owner_page)
    puts = record(owner_page, "PUT", PROGRAM_STATUS)
    select = program_select(owner_page, ROLLING)
    select.focus()
    owner_page.keyboard.press("ArrowDown")
    expect(select).to_have_value("applied")
    owner_page.keyboard.press("Escape")
    expect(select).to_have_value("todo")
    owner_page.keyboard.press("Tab")
    owner_page.wait_for_timeout(400)
    assert puts == []


def test_a_pointer_choice_still_saves_at_once(owner_page):
    open_programs(owner_page)
    with owner_page.expect_request(PROGRAM_STATUS) as request:
        program_select(owner_page, ROLLING).select_option("skipped")
    assert json.loads(request.value.post_data) == {"status": "skipped"}
    expect(owner_page.locator(f'.is-programs-done .program-row[data-program-id="{ROLLING}"]')).to_be_visible()
    assert focused_control(owner_page) == {"id": ROLLING, "role": "status", "stale": False}


def test_tabbing_on_during_a_slow_save_keeps_focus_where_the_user_went(owner_page):
    open_programs(owner_page)
    held: list = []
    owner_page.route(PROGRAM_STATUS, lambda route: held.append(route))

    program_select(owner_page, ROLLING).focus()
    owner_page.keyboard.press("ArrowDown")
    # Leaving the select commits it; the save is held in flight while the
    # keyboard user moves on to another row's select.
    owner_page.keyboard.press("Tab")
    for _ in range(20):
        if held:
            break
        owner_page.wait_for_timeout(50)
    assert len(held) == 1, "leaving the select did not save it"
    assert focused_control(owner_page) == {"id": FUTURE, "role": "official", "stale": False}
    owner_page.keyboard.press("Tab")
    assert focused_control(owner_page) == {"id": FUTURE, "role": "status", "stale": False}
    owner_page.evaluate("() => { document.activeElement.__stale = true; }")

    with owner_page.expect_response(PROGRAMS_LIST):
        held[0].continue_()
    expect(owner_page.locator(f'.is-programs-done .program-row[data-program-id="{ROLLING}"]')).to_be_visible()
    # Focus is on the rebuilt counterpart of the control the user tabbed to.
    assert focused_control(owner_page) == {"id": FUTURE, "role": "status", "stale": False}
    assert program_status(owner_page, ROLLING) == "applied"


def test_an_unsaved_choice_on_another_row_survives_a_slow_save(owner_page):
    open_programs(owner_page)
    held: list = []
    owner_page.route(PROGRAM_STATUS, lambda route: held.append(route))

    program_select(owner_page, ROLLING).focus()
    owner_page.keyboard.press("ArrowDown")
    owner_page.keyboard.press("Tab")
    for _ in range(20):
        if held:
            break
        owner_page.wait_for_timeout(50)
    assert len(held) == 1, "leaving the select did not save it"
    owner_page.keyboard.press("Tab")
    assert focused_control(owner_page) == {"id": FUTURE, "role": "status", "stale": False}
    before = program_select(owner_page, FUTURE).input_value()
    owner_page.keyboard.press("ArrowDown")
    pending = program_select(owner_page, FUTURE).input_value()
    assert pending != before

    with owner_page.expect_response(PROGRAMS_LIST):
        held[0].continue_()
    held.clear()
    owner_page.unroute(PROGRAM_STATUS)
    # The rebuilt select still shows the unsaved keyboard choice, and Enter saves it.
    expect(program_select(owner_page, FUTURE)).to_have_value(pending)
    with owner_page.expect_response(PROGRAM_STATUS):
        owner_page.keyboard.press("Enter")
    assert program_status(owner_page, FUTURE) == pending


def test_a_row_that_leaves_the_sub_tab_hands_focus_to_the_next_row(owner_page):
    owner_page.click("#programs-nav")
    expect(owner_page.locator('#subnav [data-subtab="open"]')).to_have_attribute("aria-current", "true")
    expect(owner_page.locator(".program-row")).to_have_count(1)
    select = program_select(owner_page, ROLLING)
    select.focus()
    owner_page.keyboard.press("ArrowDown")
    with owner_page.expect_response(PROGRAMS_LIST):
        owner_page.keyboard.press("Enter")
    expect(owner_page.locator(".program-row")).to_have_count(0)
    # No rows remain under Open now, so focus lands on the active sub-tab.
    expect(owner_page.locator("#subnav .subnav-item.is-active")).to_be_focused()


def test_arrow_keys_on_an_application_stage_send_no_patch(owner_page):
    owner_page.click("#applications-nav")
    card = owner_page.locator(".application-card").first
    stage = card.get_by_label("Stage")
    original = stage.input_value()
    patches = record(owner_page, "PATCH", APPLICATION_PATCH)

    stage.focus()
    owner_page.keyboard.press("ArrowDown")
    owner_page.keyboard.press("ArrowDown")
    assert stage.input_value() != original
    owner_page.wait_for_timeout(400)
    assert patches == [], "keyboard browsing saved (and audited) an intermediate stage"

    chosen = stage.input_value()
    with owner_page.expect_response(APPLICATION_PATCH):
        owner_page.keyboard.press("Enter")
    owner_page.wait_for_timeout(300)
    assert len(patches) == 1
    assert json.loads(patches[0].post_data) == {"stage": chosen}


def test_an_unsaved_stage_on_another_card_survives_a_slow_save(owner_page):
    # A second application, so one card's reload can meet another card's edit.
    status = owner_page.evaluate(
        """async () => {
            const csrf = document.cookie.split("; ").find((c) => c.startsWith("pipeline_csrf=")).split("=")[1];
            const response = await fetch("/api/v1/opportunities/job-a/actions", {
                method: "POST",
                headers: { "Content-Type": "application/json", "X-CSRF-Token": decodeURIComponent(csrf) },
                body: JSON.stringify({ action: "apply_opened" }),
            });
            return response.status;
        }"""
    )
    assert status == 200, status
    owner_page.click("#applications-nav")
    expect(owner_page.locator(".application-card")).to_have_count(2)
    first, second = owner_page.locator(".application-card").nth(0), owner_page.locator(".application-card").nth(1)
    first_id, second_id = first.get_attribute("data-application-id"), second.get_attribute("data-application-id")
    held: list = []
    owner_page.route(APPLICATION_PATCH, lambda route: held.append(route) if route.request.method == "PATCH" else route.fallback())

    first.get_by_label("Stage").focus()
    owner_page.keyboard.press("ArrowDown")
    owner_page.locator(f'[data-application-id="{second_id}"]').get_by_label("Stage").focus()
    for _ in range(20):
        if held:
            break
        owner_page.wait_for_timeout(50)
    assert len(held) == 1, "leaving the first select did not save it"
    second_stage = owner_page.locator(f'[data-application-id="{second_id}"]').get_by_label("Stage")
    before = second_stage.input_value()
    owner_page.keyboard.press("ArrowDown")
    pending = second_stage.input_value()
    assert pending != before

    with owner_page.expect_response(re.compile(r".*/api/v1/applications$")):
        held[0].continue_()
    owner_page.unroute(APPLICATION_PATCH)
    rebuilt = owner_page.locator(f'[data-application-id="{second_id}"]').get_by_label("Stage")
    expect(rebuilt).to_have_value(pending)
    expect(rebuilt).to_be_focused()
    assert first_id != second_id


@pytest.mark.allow_page_errors  # the refused save is a 409 by design
def test_a_refused_stage_change_puts_the_saved_stage_back(owner_page):
    owner_page.click("#applications-nav")
    card = owner_page.locator(".application-card").first
    stage = card.get_by_label("Stage")
    original = stage.input_value()
    target = "withdrawn" if original != "withdrawn" else "applied"
    owner_page.route(
        APPLICATION_PATCH,
        lambda route: route.fulfill(status=409, content_type="application/json", body=json.dumps({"detail": "Stage change refused"}))
        if route.request.method == "PATCH" else route.fallback(),
    )
    stage.select_option(target)
    expect(owner_page.locator("#error-banner")).to_have_text("Stage change refused")
    expect(stage).to_have_value(original)


@pytest.mark.allow_page_errors  # the 422 is the point
def test_a_validation_error_reads_as_a_sentence(page):
    page.goto("/")
    page.wait_for_selector("#auth-gate.is-visible")
    page.locator("summary", has_text="Register this local owner").click()
    page.fill("#register-name", "Sandbox Student")
    page.fill("#register-email", "student@example.com")
    page.fill("#register-password", "short")
    page.click("#register-submit")
    error = page.locator("#auth-error")
    expect(error).to_have_text(re.compile(r"at least 12 characters", re.I))
    expect(error).not_to_contain_text("[object Object]")
    expect(error).to_contain_text("Password")


@pytest.mark.allow_page_errors  # public signup is off in the sandbox (403)
def test_a_blank_invitation_is_sent_as_no_invitation(page):
    page.goto("/")
    page.wait_for_selector("#auth-gate.is-visible")
    page.locator("summary", has_text="Register this local owner").click()
    page.fill("#register-name", "Sandbox Student")
    page.fill("#register-email", "student@example.com")
    page.fill("#register-password", "Sandbox-Passw0rd-long")
    with page.expect_request("**/api/v1/auth/register") as request:
        page.click("#register-submit")
    assert json.loads(request.value.post_data)["invite_token"] is None
    expect(page.locator("#auth-error")).to_have_text("Public signup is disabled")


def test_a_closed_program_reads_closed_never_days_left(owner_page):
    def closed_early(route):
        response = route.fetch()
        payload = response.json()
        for item in payload["items"]:
            if item["id"] == FUTURE:
                # A closed note ends a program before its listed deadline.
                item.update(bucket="closed", closed_note="Closed early this cycle", days_left=30)
        route.fulfill(response=response, json=payload)

    owner_page.route(PROGRAMS_LIST, closed_early)
    open_programs(owner_page)
    for program_id in (CLOSED, FUTURE):
        when = owner_page.locator(f'.program-row[data-program-id="{program_id}"] .urgent-when span')
        expect(when).to_contain_text("Closed")
        expect(when).not_to_contain_text("left")
    expect(owner_page.locator(f'.program-row[data-program-id="{FUTURE}"]')).to_contain_text("Closed early this cycle")


def test_the_programs_lede_does_not_claim_host_published_dates(owner_page):
    owner_page.click("#programs-nav")
    lede = owner_page.locator("#page-lede")
    expect(lede).to_contain_text("your own research")
    expect(lede).not_to_contain_text("host published")


def test_an_import_reports_its_result_after_the_reload(owner_page, tmp_path):
    items = owner_page.request.get("/api/v1/applications").json()["items"]
    upload = tmp_path / "applications.json"
    upload.write_text(json.dumps([
        {"opportunity_id": items[0]["opportunity_id"], "stage": items[0]["stage"]},
        {"opportunity_id": "no-such-opportunity", "stage": "applied"},
    ]), encoding="utf-8")

    owner_page.click("#applications-nav")
    expect(owner_page.locator(".application-card").first).to_be_visible()
    with owner_page.expect_response("**/api/v1/applications/analytics"):
        owner_page.locator('input[type="file"][aria-label="Import applications from CSV or JSON"]').set_input_files(str(upload))
    expect(owner_page.locator("#page-status")).to_have_text("Imported 1; skipped 1")
    report = owner_page.locator(".import-report")
    expect(report).to_contain_text("Row 2: Opportunity not found: no-such-opportunity")


def test_an_urgent_date_note_is_shown_under_its_source(owner_page, base_url):
    """A researched date's caveat travels with its Urgent row (`date_note`)."""
    opportunity_id = owner_page.request.get("/api/v1/applications").json()["items"][0]["opportunity_id"]
    seeded = owner_page.request.put(
        f"{base_url}/api/v1/opportunities/{opportunity_id}/deadline",
        headers={"Authorization": f"Bearer {OWNER_TOKEN}"},
        data={"deadline_on": (date.today() + timedelta(days=3)).isoformat(), "note": ""},
    )
    assert seeded.status == 200, seeded.text()

    def with_note(route):
        response = route.fetch()
        payload = response.json()
        assert payload["items"], "the seeded deadline should be in Urgent"
        payload["items"][0]["date_note"] = "Estimated from last cycle"
        route.fulfill(response=response, json=payload)

    owner_page.route(re.compile(r".*/api/v1/urgent(\?.*)?$"), with_note)
    owner_page.click("#urgent-nav")
    note = owner_page.locator(".urgent-row .urgent-date-note").first
    expect(note).to_have_text("Estimated from last cycle")
    # The note sits right under the row's date source, in the same muted style.
    assert note.evaluate("el => el.previousElementSibling.classList.contains('urgent-source')")
