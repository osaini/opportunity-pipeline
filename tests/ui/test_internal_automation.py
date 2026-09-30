"""Phase 2 automation in the browser: résumé variants, the pick on a role, silence rows in Urgent, and Restore.

The server runs in this process, so a test seeds the live database directly,
the same way the uploads, the switches, and the automatic steps would.
"""

from __future__ import annotations

import json
import re
from contextlib import closing
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from playwright.sync_api import expect

from conftest import OWNER_TOKEN, native_selects, wait_for_results
from opportunity_app import auto_triage, automation
from opportunity_app.schema import connect_product, utc_now
from opportunity_app.user_time import user_timezone

BEARER = {"Authorization": f"Bearer {OWNER_TOKEN}"}
USER = "local-user"
VARIANTS = [
    {"label": "Hardware", "keywords": ["CAD", "SolidWorks", "mechanical"]},
    {"label": "Software", "keywords": ["Python", "React", "backend"]},
]


def db(live_server):
    return closing(connect_product(live_server.live_path))


def set_profile(live_server, **values):
    with db(live_server) as conn:
        row = conn.execute("SELECT profile_json FROM profiles WHERE user_id=?", (USER,)).fetchone()
        with conn:
            conn.execute("UPDATE profiles SET profile_json=? WHERE user_id=?",
                         (json.dumps({**json.loads(row[0] or "{}"), **values}), USER))


def add_resume(live_server, *, name, label="", status="confirmed", text="A resume with plenty of words in it."):
    file_id, version_id = f"resume-file-{uuid4().hex}", f"resume-{uuid4().hex}"
    now = utc_now()
    with db(live_server) as conn, conn:
        conn.execute(
            "INSERT INTO resume_files(id, user_id, original_name, media_type, byte_size, sha256, storage_path, created_at, variant_label) "
            "VALUES(?, ?, ?, 'application/pdf', 2048, ?, ?, ?, ?)",
            (file_id, USER, name, uuid4().hex, f"{file_id}.pdf", now, label),
        )
        conn.execute(
            "INSERT INTO resume_versions(id, resume_file_id, user_id, extracted_text, parsed_json, confirmed_json, status, created_at, confirmed_at) "
            "VALUES(?, ?, ?, ?, ?, '{}', ?, ?, ?)",
            (version_id, file_id, USER, text, json.dumps({"profile_suggestions": {"skills": ["Fortran"]}}), status, now,
             now if status == "confirmed" else None),
        )
    return {"file_id": file_id, "version_id": version_id}


def open_saved_role(page, company="Acme Robotics"):
    """Acme Robotics is saved in the fixture, so it is on the Saved view."""
    page.click("#saved-nav")
    wait_for_results(page)
    page.locator(".opportunity-card", has_text=company).locator(".card-button").click()
    page.wait_for_selector("#detail-panel.is-open")


def open_profile(page):
    page.click("#profile-nav")
    wait_for_results(page)
    expect(page.locator(".automation-section")).to_be_visible()


def test_a_resume_is_used_as_a_variant_without_touching_profile_facts(owner_page, base_url, live_server):
    add_resume(live_server, name="hardware-resume.pdf", status="draft")
    facts_before = owner_page.request.get(f"{base_url}/api/v1/profile", headers=BEARER).json()["facts"]
    open_profile(owner_page)
    section = owner_page.locator(".resume-section")
    expect(section.locator(".resume-variant-summary")).to_contain_text("No résumé variants yet")
    card = section.locator(".resume-card", has_text="hardware-resume.pdf")
    expect(card.get_by_role("button", name="Confirm selected facts")).to_be_visible()
    card.get_by_label("Variant label").fill("Hardware")
    card.get_by_role("button", name="Use as a variant").click()
    card = owner_page.locator(".resume-section .resume-card", has_text="hardware-resume.pdf")
    expect(card.locator(".chip", has_text="Variant: Hardware")).to_be_visible()
    expect(card.locator(".chip", has_text="Confirmed")).to_be_visible()
    expect(card.get_by_role("button", name="Confirm selected facts")).to_have_count(0)
    expect(card.get_by_role("button", name="Save variant label")).to_be_visible()
    # The profile lists no resume_variants, so the label is not counted as a variant that can be picked.
    expect(owner_page.locator(".resume-variant-summary")).to_contain_text(
        "1 résumé carries a variant label, but your profile file lists no resume_variants yet, so nothing is picked."
    )
    set_profile(live_server, resume_variants=VARIANTS)
    owner_page.reload()
    wait_for_results(owner_page)
    expect(owner_page.locator(".resume-variant-summary")).to_contain_text("1 résumé variant ready: Hardware.")
    after = owner_page.request.get(f"{base_url}/api/v1/profile", headers=BEARER).json()["facts"]
    assert after == facts_before, "a variant confirms a document to send, never profile facts"


def test_the_role_shows_its_resume_pick_and_the_student_can_change_it(owner_page, base_url, live_server):
    hardware = add_resume(live_server, name="hardware.pdf", label="Hardware", text="Built robots with CAD.")
    add_resume(live_server, name="software.pdf", label="Software", text="Python services and React apps.")
    set_profile(live_server, resume_variants=VARIANTS, default_variant="Software")
    with db(live_server) as conn, conn:
        conn.execute(
            "INSERT INTO profile_facts(user_id, field_path, value_json, source, confirmed, created_at, updated_at) "
            "VALUES(?, 'skills', ?, 'user', 1, ?, ?) ON CONFLICT(user_id, field_path) DO UPDATE SET value_json=excluded.value_json",
            (USER, json.dumps(["SolidWorks", "Kubernetes"]), utc_now(), utc_now()),
        )
    open_saved_role(owner_page)
    section = owner_page.locator(".resume-pick")
    expect(section.locator(".resume-pick-current")).to_have_text(re.compile(r"^Suggested: Hardware \(matched SolidWorks, mechanical\)\."))
    # Acme Robotics was saved before any switch was on, so no pick is coming for it.
    expect(section.locator(".resume-pick-current")).to_contain_text("Nothing was picked when this role was saved.")
    expect(section.locator(".resume-check li")).to_have_text(
        "This posting asks for SolidWorks. Your profile lists SolidWorks, but your Hardware résumé doesn't mention it."
    )
    select = section.get_by_label("Change résumé")
    select.select_option(label="Software (software.pdf)")
    expect(section.locator(".form-status")).to_have_text("Using Software for this role. The app will not change it.")
    expect(section.locator(".resume-pick-current")).to_have_text("Résumé: Software (your choice)")
    # The card behind the panel follows at once, without a reload.
    expect(owner_page.locator(".opportunity-card", has_text="Acme Robotics").locator(".card-resume")).to_have_text("Résumé: Software (your choice)")
    view = owner_page.request.get(f"{base_url}/api/v1/opportunities/job-a/resume-pick", headers=BEARER).json()
    assert (view["pick"]["label"], view["pick"]["picked_by"]) == ("Software", "student")
    # The student's pick sticks: saving again with the switch on leaves it.
    with db(live_server) as conn:
        automation.set_mode(conn, USER, "resume_variant_pick", "on")
    for action in ("undo", "saved"):
        response = owner_page.request.post(f"{base_url}/api/v1/opportunities/job-a/actions", headers=BEARER, data={"action": action})
        assert response.status == 200, response.text()
    view = owner_page.request.get(f"{base_url}/api/v1/opportunities/job-a/resume-pick", headers=BEARER).json()
    assert view["pick"]["resume_file_id"] != hardware["file_id"]
    owner_page.locator("#detail-close").click()
    owner_page.click("#saved-nav")
    wait_for_results(owner_page)
    expect(owner_page.locator(".opportunity-card", has_text="Acme Robotics").locator(".card-resume")).to_have_text("Résumé: Software (your choice)")


def test_the_new_controls_are_accessible_in_both_themes(owner_page, live_server):
    from test_accessibility import _assert_accessible

    add_resume(live_server, name="hardware.pdf", label="Hardware")
    add_resume(live_server, name="draft.pdf", status="draft")
    set_profile(live_server, resume_variants=VARIANTS, default_variant="Hardware")
    for theme in ("light", "dark"):
        owner_page.evaluate(f"document.documentElement.dataset.theme = '{theme}'")
        open_profile(owner_page)
        expect(owner_page.locator(".resume-variant")).to_have_count(2)
        expect(owner_page.locator(".automation-auto-passed")).to_contain_text("Nothing was passed on automatically")
        _assert_accessible(owner_page, f"the résumé variants and Auto-passed list ({theme})")
        open_saved_role(owner_page)
        expect(owner_page.locator(".resume-pick").get_by_label("Change résumé")).to_be_visible()
        _assert_accessible(owner_page, f"the résumé pick on a role ({theme})")
        owner_page.locator("#detail-close").click()


def test_with_no_variants_the_role_says_so_honestly(owner_page, live_server):
    add_resume(live_server, name="only-resume.pdf")
    open_saved_role(owner_page)
    expect(owner_page.locator(".resume-pick .resume-pick-current")).to_have_text(
        "No résumé variants are listed under resume_variants in your profile file, so the app uses your confirmed résumé, as before."
    )
    expect(owner_page.locator(".resume-pick").get_by_label("Change résumé")).to_be_visible()


def test_an_application_with_no_reply_shows_in_urgent(owner_page, base_url, live_server):
    with db(live_server) as conn:
        zone = user_timezone(conn, USER)
        day = zone.today() - timedelta(days=25)
        applied_at = zone.localize(datetime(day.year, day.month, day.day, 12)).isoformat()
        with conn:
            conn.execute("UPDATE applications SET stage='applied', applied_at=?, follow_up_at=NULL WHERE id='app-job-b'", (applied_at,))
            conn.execute("DELETE FROM application_tasks")
    kinds = [item["kind"] for item in owner_page.request.get(f"{base_url}/api/v1/urgent", headers=BEARER).json()["items"]]
    assert "application_silence" not in kinds, "off until the student turns it on"
    open_profile(owner_page)
    owner_page.locator("#automation-mode-application_silence").check()
    expect(owner_page.locator(".automation-group", has=owner_page.locator("#automation-mode-application_silence")).locator(".form-status")) \
        .to_have_text("Flag applications with no reply: on.")
    owner_page.click("#urgent-nav")
    wait_for_results(owner_page)
    row = owner_page.locator(".urgent-row", has=owner_page.locator(".urgent-kind", has_text="No reply yet"))
    expect(row).to_have_count(1)
    expect(row.locator("h4")).to_have_text("Controls Co-op")
    expect(row.locator(".urgent-context")).to_have_text("Orbit Systems · No reply 21 days after you applied")
    expect(row).to_contain_text("4 days overdue")
    row.get_by_role("button", name="Open the application for Orbit Systems").click()
    wait_for_results(owner_page)
    expect(owner_page.locator("#applications-nav")).to_have_attribute("aria-current", "page")


def test_a_triage_switch_says_what_it_needs(owner_page):
    open_profile(owner_page)
    reason = owner_page.locator('[data-automation-feature="auto_save"] .automation-reason')
    expect(reason).to_have_text("On is not available yet: Set automation.auto_save_at in your profile to a score from 0 to 100 first.")
    expect(owner_page.locator("#automation-mode-auto_save")).to_have_accessible_description(re.compile("auto_save_at"))


def seed_auto_pass(live_server):
    set_profile(live_server, automation={"auto_save_at": 90, "auto_pass_below": 40})
    first_seen = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(timespec="seconds")
    with db(live_server) as conn:
        automation.set_mode(conn, USER, "auto_pass", "on")
        with conn:
            conn.execute(
                "INSERT INTO opportunities(id, company, title, location, url, description, first_seen_at, last_seen_at, active, "
                "fingerprint, created_at, updated_at) VALUES('job-low', 'Quill Works', 'Sales Intern', 'Remote', "
                "'https://example.com/low', 'Cold calling and lead lists.', ?, ?, 1, 'fp-low', ?, ?)",
                (first_seen, first_seen, first_seen, first_seen),
            )
            conn.execute(
                "INSERT INTO fit_scores(opportunity_id, user_id, ruleset_version, score, explanation_json, created_at) "
                "VALUES('job-low', ?, 'legacy-v1', 22, ?, ?)",
                (USER, json.dumps(["35 base", "-13 lower-priority discipline: sales"]), utc_now()),
            )
        report = auto_triage.run_auto_triage(conn, user_id=USER)
    assert [item["opportunity_id"] for item in report["passed"]] == ["job-low"]


def test_restore_puts_back_a_role_the_app_passed_on(owner_page, base_url, live_server):
    seed_auto_pass(live_server)
    open_profile(owner_page)
    block = owner_page.locator(".automation-auto-passed")
    expect(block.get_by_role("heading", name="Auto-passed this week")).to_be_visible()
    row = block.locator(".automation-action", has_text="Sales Intern at Quill Works")
    expect(row).to_contain_text("Score 22, below your 40")
    expect(row).to_contain_text("-13 lower-priority discipline: sales")
    row.get_by_role("button", name="Restore Sales Intern at Quill Works").click()
    expect(block.locator(".form-status")).to_have_text("Restored Sales Intern at Quill Works.")
    expect(block).to_contain_text("Nothing was passed on automatically in the last 7 days.")
    expect(owner_page.locator(".automation-recent .automation-action", has_text="Passed on Sales Intern")).to_contain_text("Undone")
    with db(live_server) as conn:
        latest = conn.execute(
            "SELECT action FROM opportunity_interactions WHERE opportunity_id='job-low' ORDER BY id DESC LIMIT 1",
        ).fetchone()[0]
    assert latest == "undo", "back to neither saved nor passed"


def test_undo_in_recent_activity_also_leaves_auto_passed_this_week(owner_page, live_server):
    seed_auto_pass(live_server)
    open_profile(owner_page)
    block = owner_page.locator(".automation-auto-passed")
    expect(block.locator(".automation-action", has_text="Sales Intern at Quill Works")).to_be_visible()
    recent = owner_page.locator(".automation-recent .automation-action", has_text="Passed on Sales Intern")
    recent.get_by_role("button", name=re.compile("^Undo: Passed on Sales Intern")).click()
    expect(recent).to_contain_text("Undone")
    expect(block).to_contain_text("Nothing was passed on automatically in the last 7 days.")


def test_undo_from_the_announcement_also_leaves_auto_passed_this_week(owner_page, live_server):
    # The page announces each new automatic change once, with Undo (application mail's announceWithUndo);
    # taking an auto-pass back from there refreshes Auto-passed this week too, not only the ledger lists.
    open_profile(owner_page)  # the page has seen what there was when the student signed in
    seed_auto_pass(live_server)
    owner_page.click("#discover-nav")
    wait_for_results(owner_page)
    open_profile(owner_page)
    block = owner_page.locator(".automation-auto-passed")
    expect(block.locator(".automation-action", has_text="Sales Intern at Quill Works")).to_be_visible()
    status = owner_page.locator("#action-status")
    expect(status).to_contain_text("Automatic: Passed on Sales Intern")
    status.get_by_role("button", name="Undo").click()
    expect(status).to_have_text("Undid the automatic change.")
    expect(block).to_contain_text("Nothing was passed on automatically in the last 7 days.")


@pytest.mark.allow_page_errors  # the 500 below is the point
def test_a_restore_that_failed_can_be_tried_again(owner_page, live_server):
    seed_auto_pass(live_server)
    open_profile(owner_page)
    owner_page.route("**/api/v1/automation/actions/*/undo", lambda route: route.fulfill(
        status=500, content_type="application/json", body='{"detail": "The database was busy"}',
    ))
    block = owner_page.locator(".automation-auto-passed")
    restore = block.get_by_role("button", name="Restore Sales Intern at Quill Works")
    restore.click()
    expect(block.locator(".form-status")).to_have_text("The database was busy")
    expect(block.get_by_role("button", name="Restore Sales Intern at Quill Works")).to_be_enabled()
    owner_page.unroute("**/api/v1/automation/actions/*/undo")
    block.get_by_role("button", name="Restore Sales Intern at Quill Works").click()
    expect(block.locator(".form-status")).to_have_text("Restored Sales Intern at Quill Works.")


def test_leaving_the_resume_choice_by_keyboard_keeps_focus_where_the_student_went(owner_page, base_url, live_server):
    native_selects(owner_page)
    add_resume(live_server, name="hardware.pdf", label="Hardware")
    add_resume(live_server, name="software.pdf", label="Software")
    set_profile(live_server, resume_variants=VARIANTS, default_variant="Hardware")
    open_saved_role(owner_page)
    select = owner_page.locator(".resume-pick").get_by_label("Change résumé")
    select.focus()
    owner_page.keyboard.press("ArrowDown")
    owner_page.keyboard.press("Tab")
    left_to = owner_page.evaluate("document.activeElement.outerHTML")
    expect(owner_page.locator(".resume-pick .form-status")).to_contain_text("for this role. The app will not change it.")
    assert owner_page.evaluate("document.activeElement.tagName") != "SELECT", "focus stays where the student tabbed to"
    assert owner_page.evaluate("document.activeElement.outerHTML") == left_to
    view = owner_page.request.get(f"{base_url}/api/v1/opportunities/job-a/resume-pick", headers=BEARER).json()
    assert view["pick"]["picked_by"] == "student", "leaving the select saved the choice"


def test_the_skills_check_says_when_it_cannot_run(owner_page, live_server):
    add_resume(live_server, name="hardware.pdf", label="Hardware")
    set_profile(live_server, resume_variants=VARIANTS, default_variant="Hardware")
    with db(live_server) as conn, conn:
        conn.execute("DELETE FROM profile_facts WHERE user_id=? AND field_path='skills'", (USER,))
    open_saved_role(owner_page)
    expect(owner_page.locator(".resume-pick .resume-check-note")).to_have_text(
        "Your profile has no confirmed skills yet, so there is nothing to check."
    )


def test_the_variant_label_warning_is_announced_and_the_chips_sit_in_a_row(owner_page, live_server):
    add_resume(live_server, name="plain-resume.pdf")
    open_profile(owner_page)
    card = owner_page.locator(".resume-section .resume-card", has_text="plain-resume.pdf")
    chips = card.locator(".resume-chips")
    assert chips.evaluate("node => getComputedStyle(node).display") == "flex"
    field = card.get_by_label("Variant label")
    card.get_by_role("button", name="Use as a variant").click()
    expect(field).to_be_focused()
    expect(field).to_have_attribute("aria-invalid", "true")
    expect(field).to_have_accessible_description(re.compile("Type a label first"))
    field.fill("Hardware")
    expect(field).not_to_have_attribute("aria-invalid", "true")
