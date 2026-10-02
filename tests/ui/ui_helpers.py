"""Helpers the browser tests share: the axe scan, the outreach tab's openers, the live database, a Gmail listing, the apply fixtures.

Not a test module and not conftest: tests import what they need from here instead of from one another."""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import closing

import pytest
from axe_core_python.sync_playwright import Axe
from playwright.sync_api import expect

from apply_fake_ats import JOB_URL
from conftest import OWNER_TOKEN, record_quarantined, wait_for_results
from opportunity_app.core.database import connect_product
from opportunity_app.core.timestamps import utc_now
from outreach_fakes import COMPOSE_ACCOUNT


AXE_OPTIONS = {
    "runOnly": {"type": "tag", "values": ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]},
}


# Keep this explicit: a future quarantine must name and document a real defect.
# The phase-verification pass on 2026-08-23 cleared the previous backlog.
KNOWN_VIOLATIONS: dict[str, str] = {}


def scan(page, context=None) -> list[dict]:
    results = Axe().run(page, context=context, options=AXE_OPTIONS)
    return results.get("violations", [])


def describe(violations: list[dict]) -> str:
    lines = []
    for violation in violations:
        targets = ", ".join(
            str(target) for node in violation["nodes"][:4] for target in node.get("target", [])
        )
        lines.append(
            f"[{violation['impact'] or 'unknown'}] {violation['id']}: {violation['help']}\n"
            f"    affects: {targets}\n"
            f"    docs: {violation['helpUrl']}"
        )
    return "\n".join(lines)


def assert_accessible(page, label: str) -> None:
    violations = scan(page)
    unexpected = []
    for violation in violations:
        if violation["id"] in KNOWN_VIOLATIONS:
            record_quarantined(violation["id"], label)
        else:
            unexpected.append(violation)
    if unexpected:
        pytest.fail(
            f"axe found {len(unexpected)} violation(s) on {label}:\n{describe(unexpected)}",
            pytrace=False,
        )


BEARER = {"Authorization": f"Bearer {OWNER_TOKEN}"}


def seed_target(page, base_url, **overrides):
    body = {
        "company": "Bovi",
        "channel": "Local accelerators",
        "priority": "P1",
        "website": "https://bovi.example",
        "summary": "Dairy robotics for small farms",
        "source_urls": ["https://bovi.example/"],
        **overrides,
    }
    response = page.request.post(f"{base_url}/api/v1/outreach", headers=BEARER, data=body)
    assert response.status == 201, response.text()
    return response.json()


def wait_for_outreach_results(page):
    page.wait_for_function("() => document.getElementById('results')?.getAttribute('aria-busy') === 'false'")


def open_tab(page, subtab):
    """Pick an outreach subtab from the rail, e.g. "awaiting" or "deep-search"."""
    button = page.locator(f'#subnav [data-subtab="{subtab}"]')
    button.click()
    expect(button).to_have_attribute("aria-current", "true")
    wait_for_outreach_results(page)


def open_outreach(page, subtab=None):
    page.click("#outreach-nav")
    expect(page.locator("#outreach-nav")).to_have_class("nav-item is-active")
    wait_for_outreach_results(page)
    if subtab:
        open_tab(page, subtab)


def row_for(page, company):
    name = page.locator(".outreach-row-company", has_text=re.compile(f"^{re.escape(company)}$"))
    return page.locator(".outreach-row", has=name)


def card_for(page, company):
    """The split view's pane for a company, picking its row in the list first."""
    row = row_for(page, company)
    if row.count() and row.get_attribute("aria-pressed") != "true":
        row.click()
    return page.locator(".outreach-pane", has=page.get_by_role("heading", name=company, exact=True))


def open_details(card, tab=None):
    """The pane's form area, switched to one of its tabs when named."""
    if tab:
        card.get_by_role("tab", name=tab, exact=True).click()
    return card


def gmail_listing(page, **gmail):
    connected = {"configured": True, "connected": True, "needs_reconnect": False, "bounce_check": False,
                 "account": COMPOSE_ACCOUNT, "attachment": "", "attachment_problem": "", **gmail}

    def listing(route):
        response = route.fetch()
        route.fulfill(response=response, json={**response.json(), "gmail_drafts": connected})

    page.route(re.compile(r".*/api/v1/outreach(\?.*)?$"), listing)
    # With read access the tab looks for bounces on load; there is no Gmail here to look in.
    page.route("**/api/v1/outreach/inbox-check", lambda route: route.fulfill(json={}))


USER = "local-user"


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


def open_saved_role(page, company="Acme Robotics"):
    page.click("#saved-nav")
    wait_for_results(page)
    page.locator(".opportunity-card", has_text=company).locator(".card-button").click()
    page.wait_for_selector("#detail-panel.is-open")


def confirm_posting(section):
    """The sandbox's fake board answers every role with Example Robotics' listing, so the saved Acme role never matches it:
    the student says it is the right posting before an answer is saved."""
    expect(section.locator(".apply-mismatch")).to_contain_text("Greenhouse's form is for Robotics Software Intern at Example Robotics, not Acme Robotics")
    section.get_by_label("This is the right posting").check()
