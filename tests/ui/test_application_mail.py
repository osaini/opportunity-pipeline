"""Update applications from job emails, in the browser: the Emails list, the proposal picker, trusted domains, and Urgent.

The server runs in this process, so each test seeds what the Gmail reader
would have recorded straight into the live database. Every company, address
and message is invented.
"""

from __future__ import annotations

import json
import re
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from playwright.sync_api import expect

from conftest import wait_for_results
from opportunity_app import automation
from opportunity_app.actions import record_intent
from opportunity_app.schema import connect_product, utc_now

USER = "local-user"
FEATURE = "application_mail"
ORBIT = "app-job-b"


def switch(conn, mode):
    """The switch set directly: the 48-hour shadow gate is automation's, and tested there."""
    with conn:
        conn.execute(
            "INSERT INTO user_settings(user_id, key, value, updated_at) VALUES(?, ?, ?, ?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (USER, FEATURE, mode, utc_now()),
        )


def acme_application(conn):
    """The seeded Acme Robotics posting, applied to (stage 'applying')."""
    return record_intent(conn, "job-a", "apply_opened", user_id=USER)["application_id"]


def propose(conn, subject_id, *, after, candidates, reasons, summary, action_type="application.stage"):
    return automation.perform(
        conn, user_id=USER, feature=FEATURE, action_type=action_type, subject_kind="application", subject_id=subject_id,
        after=after, summary=summary, basis="classify:rules;match:ambiguous", confidence=0.9,
        evidence={"gmail_id": f"m-{uuid4().hex[:6]}", "sender_domain": "greenhouse-mail.io", "subject": "An update on your application",
                  "match": {"tier": "ambiguous", "candidates": candidates}, "why_proposal": reasons},
        idempotency_key=f"gmail:{uuid4().hex}:{subject_id}:{action_type}", auto=False,
    )


def open_profile(page):
    page.click("#profile-nav")
    wait_for_results(page)
    section = page.locator(".automation-section")
    expect(section).to_be_visible()
    return section


def open_application(page, application_id):
    page.click("#applications-nav")
    wait_for_results(page)
    card = page.locator(f'.application-card[data-application-id="{application_id}"]')
    card.locator("summary", has_text="Tasks, contacts, and timeline").click()
    return card


def test_an_application_lists_its_emails_with_a_link_to_each_gmail_thread(owner_page, live_server):
    with closing(connect_product(live_server.live_path)) as conn:
        with conn:
            conn.execute(
                """
                INSERT INTO application_mail_messages(user_id, gmail_id, thread_id, application_id, kind, matched_by, state, subject,
                    sender_domain, received_at, recorded_at)
                VALUES(?, 'm-ui-1', '18c0ffee', ?, 'interview', 'company_title', 'done', 'Interview invitation: Orbit Systems',
                    'lever.co', ?, ?)
                """,
                (USER, ORBIT, utc_now(), utc_now()),
            )
    card = open_application(owner_page, ORBIT)
    emails = card.locator(".tracker-emails")
    expect(emails.get_by_role("heading", name="Emails")).to_be_visible()
    expect(emails).to_contain_text("Interview invitation: Orbit Systems")
    expect(emails).to_contain_text("lever.co")
    link = emails.get_by_role("link", name="Open in Gmail: Interview invitation: Orbit Systems")
    expect(link).to_have_attribute("href", "https://mail.google.com/mail/u/0/#all/18c0ffee")
    expect(link).to_have_attribute("target", "_blank")
    expect(link).to_have_attribute("rel", "noopener noreferrer")


def test_an_application_with_no_emails_shows_no_emails_section(owner_page):
    card = open_application(owner_page, ORBIT)
    expect(card.locator(".timeline-list")).to_be_visible()
    expect(card.locator(".tracker-emails")).to_have_count(0)


def test_an_assessment_task_opens_its_page_from_the_app_only(owner_page, live_server):
    with closing(connect_product(live_server.live_path)) as conn:
        with conn:
            conn.execute(
                "INSERT INTO application_tasks(id, application_id, user_id, title, status, created_at, updated_at, origin, origin_ref, link) "
                "VALUES('task-ui-1', ?, ?, 'Complete the HackerRank assessment', 'open', ?, ?, 'email', 'm-ui-2', 'https://www.hackerrank.com/test/abc/login')",
                (ORBIT, USER, utc_now(), utc_now()),
            )
    card = open_application(owner_page, ORBIT)
    link = card.get_by_role("link", name="Open the page for Complete the HackerRank assessment")
    expect(link).to_have_attribute("href", "https://www.hackerrank.com/test/abc/login")
    expect(card.locator(".tracker-check", has_text="Complete the HackerRank assessment")).to_contain_text("From an email")


def test_a_proposal_is_approved_for_the_application_picked_instead(owner_page, live_server):
    with closing(connect_product(live_server.live_path)) as conn:
        switch(conn, "on")
        acme = acme_application(conn)
        propose(conn, ORBIT, after={"stage": "interview"}, candidates=[ORBIT, acme],
                reasons=["2 open applications, so it could be any of them"], summary="Orbit Systems (Controls Co-op): move to interview")
    section = open_profile(owner_page)
    waiting = section.locator(".automation-waiting")
    expect(waiting).to_contain_text("Waiting for you because 2 open applications, so it could be any of them.")
    picker = waiting.get_by_role("combobox", name="Application for: Orbit Systems (Controls Co-op): move to interview")
    expect(picker).to_have_value(ORBIT)
    options = picker.locator("option").all_inner_texts()
    assert options[0].startswith("Orbit Systems") and options[1].startswith("Acme Robotics"), options
    picker.select_option(acme)
    waiting.get_by_role("button", name="Approve", exact=True).click()
    expect(waiting.locator(".form-status")).to_have_text(
        "Approved for Acme Robotics (Mechanical Engineering Intern) instead: Orbit Systems (Controls Co-op): move to interview.")
    with closing(connect_product(live_server.live_path)) as conn:
        stages = dict(conn.execute("SELECT id, stage FROM applications WHERE id IN (?, ?)", (ORBIT, acme)).fetchall())
        action = conn.execute("SELECT subject_id, note FROM automation_actions WHERE feature=?", (FEATURE,)).fetchone()
    assert stages == {ORBIT: "applied", acme: "interview"}, stages
    assert action["subject_id"] == acme and "different application" in action["note"]


def test_a_company_domain_is_trusted_from_the_automation_section(owner_page, live_server):
    with closing(connect_product(live_server.live_path)) as conn:
        switch(conn, "shadow")
        with conn:
            conn.execute(
                "INSERT INTO employer_domains(id, user_id, company_key, company, domain, status, source, evidence, created_at) "
                "VALUES('domain-ui', ?, 'acme robotics', 'Acme Robotics', 'acme-robotics.com', 'suggested', 'outreach', "
                "'Your outreach record for Acme Robotics lists the website acme-robotics.com.', ?)",
                (USER, utc_now()),
            )
    section = open_profile(owner_page)
    domains = section.locator(".automation-domains")
    expect(domains.get_by_role("heading", name="Trusted company mail domains")).to_be_visible()
    row = domains.locator(".automation-domain")
    expect(row).to_contain_text("Trust mail from @acme-robotics.com for Acme Robotics?")
    expect(row).to_contain_text("Your outreach record for Acme Robotics lists the website acme-robotics.com.")
    domains.get_by_role("button", name="Trust mail from acme-robotics.com for Acme Robotics").click()
    expect(domains.locator(".form-status")).to_contain_text("Trusted @acme-robotics.com for Acme Robotics.")
    expect(row).to_contain_text("Trusted: mail from @acme-robotics.com for Acme Robotics")
    expect(domains.get_by_role("button", name="Stop trusting acme-robotics.com for Acme Robotics")).to_be_visible()
    with closing(connect_product(live_server.live_path)) as conn:
        assert conn.execute("SELECT status FROM employer_domains WHERE id='domain-ui'").fetchone()[0] == "trusted"
    domains.get_by_role("button", name="Stop trusting acme-robotics.com for Acme Robotics").click()
    expect(domains).to_contain_text("No company domains to review yet.")


def test_the_domain_list_stays_out_of_sight_while_the_switch_is_off(owner_page):
    section = open_profile(owner_page)
    expect(section.locator(".automation-domains")).to_be_hidden()
    expect(section.locator(".automation-application-mail")).to_be_hidden()


def test_the_job_email_block_reports_the_backfill_and_approves_it_all(owner_page, live_server):
    with closing(connect_product(live_server.live_path)) as conn:
        switch(conn, "on")
        acme = acme_application(conn)
        with conn:
            conn.execute(
                "INSERT INTO application_mail_sync(user_id, history_id, enabled_at, backfill_state, updated_at) VALUES(?, '100', ?, 'done', ?)",
                (USER, utc_now(), utc_now()),
            )
        row = propose(conn, acme, after={"stage": "applied", "applied_at": "2026-09-01T10:00:00+00:00"}, candidates=[acme],
                      reasons=["it arrived before you turned this on"], summary="Acme Robotics (Mechanical Engineering Intern): move to applied")
        with conn:
            conn.execute("UPDATE automation_actions SET basis=? WHERE id=?", ("classify:rules;match:company_title;window:before_enabled", row["id"]))
    section = open_profile(owner_page)
    block = section.locator(".automation-application-mail")
    expect(block).to_contain_text("Found 1 update from the last 60 days.")
    block.get_by_role("button", name="Approve all").click()
    expect(block.locator(".form-status")).to_have_text("Approved 1 update.")
    expect(section.locator(".automation-waiting")).to_contain_text("Nothing is waiting for you.")
    with closing(connect_product(live_server.live_path)) as conn:
        assert conn.execute("SELECT stage FROM applications WHERE id=?", (acme,)).fetchone()[0] == "applied"


def test_the_timeline_names_the_email_behind_an_automatic_change(owner_page, live_server):
    with closing(connect_product(live_server.live_path)) as conn:
        switch(conn, "on")
        automation.perform(
            conn, user_id=USER, feature=FEATURE, action_type="application.stage", subject_kind="application", subject_id=ORBIT,
            after={"stage": "interview"}, summary="Orbit Systems (Controls Co-op): move to interview", basis="classify:rules;match:company_title",
            confidence=0.9, evidence={"gmail_id": "m-ui-3", "sender_domain": "lever.co"}, idempotency_key="gmail:m-ui-3:app-job-b:application.stage",
            auto=True,
        )
    card = open_application(owner_page, ORBIT)
    author = card.locator(".timeline-list li", has_text="applied → interview").locator(".timeline-author")
    expect(author).to_have_text("Automatic: from an email by lever.co")


def test_a_new_automatic_change_is_announced_once_with_undo(owner_page, live_server):
    open_profile(owner_page)  # the page has seen what there was when the student signed in
    with closing(connect_product(live_server.live_path)) as conn:
        switch(conn, "on")
        automation.perform(
            conn, user_id=USER, feature=FEATURE, action_type="application.stage", subject_kind="application", subject_id=ORBIT,
            after={"stage": "interview"}, summary="Orbit Systems (Controls Co-op): move to interview, from an email by lever.co",
            basis="classify:rules;match:company_title", confidence=0.9, evidence={"gmail_id": "m-ui-6", "sender_domain": "lever.co"},
            idempotency_key="gmail:m-ui-6:app-job-b:application.stage", auto=True,
        )
    owner_page.click("#discover-nav")
    wait_for_results(owner_page)
    open_profile(owner_page)
    status = owner_page.locator("#action-status")
    expect(status).to_contain_text("Automatic: Orbit Systems (Controls Co-op): move to interview, from an email by lever.co.")
    status.get_by_role("button", name="Undo").click()
    expect(status).to_have_text("Undid the automatic change.")
    with closing(connect_product(live_server.live_path)) as conn:
        assert conn.execute("SELECT stage FROM applications WHERE id=?", (ORBIT,)).fetchone()[0] == "applied"
    owner_page.click("#discover-nav")
    wait_for_results(owner_page)
    open_profile(owner_page)
    expect(status).not_to_contain_text("Automatic:")


def test_the_email_card_preselects_the_matched_application_first(owner_page, live_server):
    with closing(connect_product(live_server.live_path)) as conn:
        acme = acme_application(conn)
        payload = {"source": "application_mail", "subject": "Update on your application", "body_preview": "", "sender": "no-reply@hire.lever.co",
                   "sender_domain": "lever.co", "classified_by": {"source": "rules"}, "gmail_id": "m-ui-4", "candidates": [acme, ORBIT],
                   "matched_by": "ambiguous"}
        with conn:
            conn.execute(
                "INSERT INTO monitored_events(id, user_id, connector_id, external_id, event_type, confidence, payload_json, status, created_at) "
                "VALUES('event-ui', ?, NULL, 'gmail:m-ui-4', 'interview', 0.9, ?, 'pending', ?)",
                (USER, json.dumps(payload), utc_now()),
            )
    open_profile(owner_page)
    card = owner_page.locator(".monitored-event")
    picker = card.get_by_role("combobox", name="Application this email is about")
    expect(picker).to_have_value(acme)
    options = picker.locator("option").all_inner_texts()
    assert options[0].startswith("Acme Robotics") and "(matched)" in options[0], options
    expect(card).to_contain_text("From lever.co")


def test_an_email_deadline_shows_in_urgent_with_where_it_came_from(owner_page, live_server):
    due = (date.today() + timedelta(days=3)).isoformat()
    with closing(connect_product(live_server.live_path)) as conn:
        with conn:
            conn.execute(
                "INSERT INTO email_deadlines(id, user_id, application_id, gmail_id, deadline_on, quote, sender_domain, received_at, created_at) "
                "VALUES('email-deadline-ui', ?, ?, 'm-ui-5', ?, 'Please return the form by the date below.', 'lever.co', ?, ?)",
                (USER, ORBIT, due, datetime.now(timezone.utc).isoformat(), utc_now()),
            )
    owner_page.click("#urgent-nav")
    wait_for_results(owner_page)
    row = owner_page.locator(".urgent-row", has_text="Deadline from an email")
    expect(row).to_contain_text("From an email")
    expect(row).to_contain_text(re.compile(r"From lever\.co, received \d{4}-\d{2}-\d{2}: 'Please return the form by the date below\.'"))
    row.get_by_role("button", name="Open the application for Orbit Systems").click()
    wait_for_results(owner_page)
    expect(owner_page.locator(f'.application-card[data-application-id="{ORBIT}"]')).to_be_visible()


def test_the_job_email_parts_of_the_automation_section_are_accessible_in_both_themes(owner_page, live_server):
    from test_accessibility import _assert_accessible

    with closing(connect_product(live_server.live_path)) as conn:
        switch(conn, "on")
        acme = acme_application(conn)
        propose(conn, ORBIT, after={"stage": "interview"}, candidates=[ORBIT, acme], reasons=["the sender is not a job system or a company domain you trusted"],
                summary="Orbit Systems (Controls Co-op): move to interview")
        with conn:
            conn.execute(
                "INSERT INTO employer_domains(id, user_id, company_key, company, domain, status, source, evidence, created_at) "
                "VALUES('domain-ui-2', ?, 'orbit systems', 'Orbit Systems', 'orbit-systems.com', 'suggested', 'email', 'An email passed Gmail''s sender check.', ?)",
                (USER, utc_now()),
            )
    section = open_profile(owner_page)
    expect(section.locator(".automation-domain")).to_be_visible()
    expect(section.locator(".automation-picker")).to_be_visible()
    _assert_accessible(owner_page, "the job-email parts of the automation section")
    owner_page.evaluate("document.documentElement.dataset.theme = 'dark'")
    _assert_accessible(owner_page, "the job-email parts of the automation section in dark mode")
