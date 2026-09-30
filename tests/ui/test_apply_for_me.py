"""Apply for me's "what's missing" view on a saved Greenhouse role, its settings, and the name written on an application.

The server runs in this process with the fictional listing (tests/apply_fake_ats.py) and an agent that only says a
window could open, so nothing here reaches Greenhouse and no browser is opened by the app. A test seeds the live
database directly, the way the switches and uploads would.
"""

from __future__ import annotations

import hashlib
import json
from contextlib import closing

import pytest
from axe_core_python.sync_playwright import Axe
from playwright.sync_api import expect

from apply_fake_ats import JOB_URL
from conftest import OWNER_TOKEN, wait_for_results
from opportunity_app import actions
from opportunity_app.schema import connect_product, utc_now

BEARER = {"Authorization": f"Bearer {OWNER_TOKEN}"}
USER = "local-user"
AXE_OPTIONS = {"runOnly": {"type": "tag", "values": ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]}}


def db(live_server):
    return closing(connect_product(live_server.live_path))


def fact(conn, path, value):
    conn.execute(
        "INSERT INTO profile_facts(user_id, field_path, value_json, source, confirmed, created_at, updated_at) VALUES(?, ?, ?, 'user', 1, ?, ?) "
        "ON CONFLICT(user_id, field_path) DO UPDATE SET value_json=excluded.value_json, confirmed=1",
        (USER, path, json.dumps(value), utc_now(), utc_now()))


def prepare(live_server, *, turn_on=True):
    """Acme Robotics (saved) becomes a Greenhouse role; the student has a name for applications, an email and a résumé."""
    data = b"%PDF-1.4 a fictional resume for the UI suite"
    resumes = live_server.live_path.parent / "resumes"
    resumes.mkdir(parents=True, exist_ok=True)
    (resumes / "resume-file-ui.pdf").write_bytes(data)
    with db(live_server) as conn, conn:
        conn.execute("UPDATE opportunities SET url=? WHERE company='Acme Robotics'", (JOB_URL,))
        fact(conn, "name_parts", {"first": "Sam", "last": "Rivera", "preferred": ""})
        fact(conn, "contact", {"email": "sam.rivera@example.test"})
        stamp = utc_now()
        conn.execute(
            "INSERT INTO resume_files(id, user_id, original_name, media_type, byte_size, sha256, storage_path, created_at) VALUES('resume-file-ui', ?, 'Sam Rivera Resume.pdf', 'application/pdf', ?, ?, 'resume-file-ui.pdf', ?)",
            (USER, len(data), hashlib.sha256(data).hexdigest(), stamp))
        conn.execute("INSERT INTO resume_versions(id, resume_file_id, user_id, extracted_text, status, created_at, confirmed_at) VALUES('resume-ui', 'resume-file-ui', ?, 't', 'confirmed', ?, ?)", (USER, stamp, stamp))


@pytest.fixture
def apply_ready(live_server, base_url, pristine_database):
    """Seeded and switched on before the page loads, so the page reads the switch as on."""
    import httpx

    prepare(live_server)
    response = httpx.put(f"{base_url}/api/v1/automation/settings", headers=BEARER, json={"modes": {"apply_agent": "on"}})
    assert response.status_code == 200, response.text


def open_saved_role(page, company="Acme Robotics"):
    page.click("#saved-nav")
    wait_for_results(page)
    page.locator(".opportunity-card", has_text=company).locator(".card-button").click()
    page.wait_for_selector("#detail-panel.is-open")


def tracker_rows(live_server):
    with db(live_server) as conn:
        return [tuple(row) for row in conn.execute("SELECT opportunity_id, stage FROM applications ORDER BY opportunity_id")], \
            conn.execute("SELECT COUNT(*) FROM opportunity_interactions").fetchone()[0], conn.execute("SELECT COUNT(*) FROM apply_runs").fetchone()[0]


def test_a_saved_greenhouse_role_shows_what_is_missing_and_changes_nothing_in_the_tracker(apply_ready, owner_page, live_server):
    before = tracker_rows(live_server)
    open_saved_role(owner_page)
    section = owner_page.locator(".apply-for-me")
    expect(section).to_be_visible()
    expect(section.locator(".apply-summary")).to_contain_text("questions need an answer first")
    expect(section.locator('[data-apply-key="question_4000000101"] strong')).to_have_text("Why do you want to work at Example Robotics?")
    expect(section.locator('[data-apply-key="question_4000000103"] select option')).to_have_text(["Choose an option", "Perception", "Controls", "Firmware"])
    # A sensitive question is named, and gets no answer box: the app never answers these for the student.
    sensitive = section.locator('[data-apply-key="question_4000000105"]')
    expect(sensitive).to_contain_text("The app doesn't answer this kind of question for you")
    expect(sensitive.locator("textarea, select, input")).to_have_count(0)
    expect(section.locator(".apply-note")).to_contain_text("Nothing in your tracker has changed")
    assert tracker_rows(live_server) == before, "opening the section wrote nothing"


def test_a_text_and_a_select_question_are_answered_once_and_the_answer_carries_over(apply_ready, owner_page, live_server):
    open_saved_role(owner_page)
    section = owner_page.locator(".apply-for-me")
    expect(section).to_be_visible()
    text = section.locator('[data-apply-key="question_4000000101"]')
    text.locator("textarea").fill("I build small robot arms")
    text.get_by_role("button", name="Save and use for this question").click()
    expect(section.locator('[data-apply-key="question_4000000101"]')).to_have_count(0)
    expect(section.locator(".apply-summary")).to_contain_text("Saved for Acme Robotics.")
    team = section.locator('[data-apply-key="question_4000000103"]')
    team.locator("select").select_option("Controls")
    team.get_by_label("Use for any company").check()
    team.get_by_role("button", name="Save and use for this question").click()
    expect(section.locator('[data-apply-key="question_4000000103"]')).to_have_count(0)
    # The saved answers are id-free rows for this company, and the reusable tick added its tag.
    with db(live_server) as conn:
        rows = {row["question"]: (row["answer"], row["company"], row["tags_json"]) for row in conn.execute("SELECT question, answer, company, tags_json FROM answer_library")}
    assert rows["Why do you want to work at Example Robotics?"] == ("I build small robot arms", "Acme Robotics", "[]")
    assert rows["Which team are you most interested in?"] == ("Controls", "Acme Robotics", '["reusable"]')
    # Closed and opened again, the two are no longer missing.
    owner_page.locator("#detail-close").click()
    open_saved_role(owner_page)
    expect(owner_page.locator(".apply-for-me")).to_be_visible()
    expect(owner_page.locator('.apply-for-me [data-apply-key="question_4000000101"]')).to_have_count(0)
    expect(owner_page.locator('.apply-for-me [data-apply-key="question_4000000103"]')).to_have_count(0)
    expect(owner_page.locator('.apply-for-me [data-apply-key="question_4000000111"]')).to_have_count(1)


def test_the_reusable_tick_is_hidden_for_a_question_that_depends_on_the_company(apply_ready, owner_page):
    open_saved_role(owner_page)
    section = owner_page.locator(".apply-for-me")
    expect(section).to_be_visible()
    worked = section.locator('[data-apply-key="question_4000000111"]')
    expect(worked).to_contain_text("saved for this company only")
    expect(worked.get_by_label("Use for any company")).to_have_count(0)
    expect(section.locator('[data-apply-key="question_4000000101"]').get_by_label("Use for any company")).to_have_count(1)


def test_a_wrong_option_is_refused_in_words_and_nothing_is_saved(apply_ready, owner_page, live_server):
    open_saved_role(owner_page)
    section = owner_page.locator(".apply-for-me")
    expect(section).to_be_visible()
    team = section.locator('[data-apply-key="question_4000000103"]')
    team.get_by_role("button", name="Save and use for this question").click()
    expect(team.locator(".form-status")).to_have_text("Give an answer first.")
    with db(live_server) as conn:
        assert conn.execute("SELECT COUNT(*) FROM answer_library").fetchone()[0] == 0


def test_a_saved_role_that_is_not_on_greenhouse_says_so(apply_ready, owner_page, live_server):
    with db(live_server) as conn:
        actions.record_intent(conn, "job-b", "saved", user_id=USER)
    open_saved_role(owner_page, "Orbit Systems")
    expect(owner_page.locator(".apply-for-me .apply-summary")).to_have_text("Apply for me works with Greenhouse postings only, for now")
    expect(owner_page.locator(".apply-for-me button")).to_have_count(0)


def test_nothing_is_asked_and_nothing_shows_until_the_switch_is_on(owner_page, live_server):
    prepare(live_server)
    asked = []
    owner_page.on("request", lambda request: asked.append(request.url) if "/apply-agent/" in request.url else None)
    open_saved_role(owner_page)
    owner_page.wait_for_timeout(700)
    assert asked == []
    expect(owner_page.locator(".apply-for-me")).to_have_count(0)


@pytest.mark.parametrize("width", (1280, 390))
def test_the_section_and_its_forms_are_accessible_and_do_not_overflow(apply_ready, owner_page, width):
    owner_page.set_viewport_size({"width": width, "height": 900})
    open_saved_role(owner_page)
    expect(owner_page.locator(".apply-for-me")).to_be_visible()
    violations = Axe().run(owner_page, context=".apply-for-me", options=AXE_OPTIONS).get("violations", [])
    assert not violations, [(item["id"], item["help"]) for item in violations]
    assert owner_page.evaluate("() => document.documentElement.scrollWidth <= document.documentElement.clientWidth")


def test_the_settings_show_the_limits_and_keep_an_exact_option_the_student_saves(apply_ready, owner_page, live_server):
    owner_page.click("#profile-nav")
    wait_for_results(owner_page)
    block = owner_page.locator(".automation-apply-agent")
    expect(block).to_contain_text("Applications a day: 5")
    expect(block).to_contain_text("Days before applying again to the same company: 30 days")
    expect(block).to_contain_text("No options saved yet.")
    block.get_by_label("List").select_option("school")
    block.get_by_label("Exact option").fill("University of Example - City")
    block.get_by_role("button", name="Save this option").click()
    expect(block).to_contain_text("School: University of Example - City")
    with db(live_server) as conn:
        assert conn.execute("SELECT label FROM apply_ats_labels WHERE field='school'").fetchone()[0] == "University of Example - City"
    block.get_by_role("button", name="Remove the saved School option").click()
    expect(block).to_contain_text("No options saved yet.")
    violations = Axe().run(owner_page, context=".automation-apply-agent", options=AXE_OPTIONS).get("violations", [])
    assert not violations, [(item["id"], item["help"]) for item in violations]


def test_the_profile_keeps_the_name_written_on_an_application(owner_page, live_server):
    owner_page.click("#profile-nav")
    wait_for_results(owner_page)
    fields = owner_page.locator(".profile-fieldset")
    expect(fields.locator("legend")).to_have_text("Name for applications")
    fields.get_by_label("First name").fill("Ana María")
    fields.get_by_label("Last name").fill("de la Cruz")
    fields.get_by_label("Preferred name (optional)").fill("Ana")
    owner_page.get_by_role("button", name="Save and confirm profile").click()
    expect(owner_page.locator(".form-status", has_text="Profile saved and confirmed.")).to_be_visible()
    with db(live_server) as conn:
        stored = json.loads(conn.execute("SELECT value_json FROM profile_facts WHERE field_path='name_parts' AND confirmed=1").fetchone()[0])
    assert stored == {"first": "Ana María", "last": "de la Cruz", "preferred": "Ana"}
    owner_page.reload()
    wait_for_results(owner_page)
    owner_page.click("#profile-nav")
    wait_for_results(owner_page)
    expect(owner_page.locator(".profile-fieldset").get_by_label("Last name")).to_have_value("de la Cruz")
