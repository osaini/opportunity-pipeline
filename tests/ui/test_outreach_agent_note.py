"""The note that Claude Code ran in place of the Codex the student chose is shown where the student looks, not only in a log.

The note is stored with the deep search run (so the scheduled task's runs carry it) and rides on the contact search's result.
"""

from __future__ import annotations

import json
import re
from contextlib import closing

from playwright.sync_api import expect

from opportunity_app.core.database import connect_product
from opportunity_app.core.timestamps import utc_now
from ui_helpers import open_outreach, open_tab, seed_target

NOTE = "Codex cannot be limited to web search, so Claude Code ran this search. Set PIPELINE_OUTREACH_RESEARCH_ALLOW_CODEX=1 in .env to let Codex do it."


def test_the_deep_search_panel_shows_the_note_stored_with_the_last_run(owner_page, live_server):
    # A run the scheduled task made through the command line: there is no manager result in this server, only the stored run.
    with closing(connect_product(live_server.live_path)) as conn:
        with conn:
            conn.execute(
                "INSERT INTO outreach_discovery_runs(id, user_id, run_trigger, status, scopes_json, imported, started_at, finished_at, agent_note) "
                "VALUES('discovery-note', 'local-user', 'scheduled', 'succeeded', '[]', 2, ?, ?, ?)",
                (utc_now(), utc_now(), NOTE),
            )
    open_outreach(owner_page, "deep-search")
    panel = owner_page.locator("details.outreach-deep-search")
    expect(panel.locator("summary")).to_contain_text("2 companies added")
    expect(panel.locator(".outreach-agent-note")).to_have_text(NOTE)


def test_a_run_that_ran_as_chosen_shows_no_note(owner_page, live_server):
    with closing(connect_product(live_server.live_path)) as conn:
        with conn:
            conn.execute(
                "INSERT INTO outreach_discovery_runs(id, user_id, run_trigger, status, scopes_json, imported, started_at, finished_at) "
                "VALUES('discovery-plain', 'local-user', 'scheduled', 'succeeded', '[]', 1, ?, ?)",
                (utc_now(), utc_now()),
            )
    open_outreach(owner_page, "deep-search")
    expect(owner_page.locator("details.outreach-deep-search summary")).to_contain_text("1 company added")
    expect(owner_page.locator(".outreach-agent-note")).to_have_count(0)


def test_the_finished_announcement_says_which_agent_ran(owner_page):
    def with_note(route):
        response = route.fetch()
        body = response.json()
        if (body.get("active") or {}).get("state") == "succeeded":
            body["active"]["result"]["agent_note"] = NOTE
        route.fulfill(response=response, body=json.dumps(body))

    owner_page.route(re.compile(r"/api/v1/outreach/discovery$"), with_note)
    open_outreach(owner_page, "deep-search")
    owner_page.locator("details.outreach-deep-search").get_by_role("button", name="Run deep search now").click()
    expect(owner_page.locator("#action-status")).to_contain_text("Deep search finished", timeout=30_000)
    expect(owner_page.locator("#action-status")).to_contain_text("Claude Code ran this search")


def test_a_search_that_fails_before_its_run_is_stored_does_not_borrow_an_older_runs_note(owner_page):
    # The newest stored run is an older search's (it carries a note); this search failed before it stored a run of its own.
    older = {"id": "discovery-older", "run_trigger": "scheduled", "status": "succeeded", "scopes": [], "imported": 2,
             "started_at": "2026-01-01T09:00:00+00:00", "finished_at": "2026-01-01T09:05:00+00:00", "agent_note": NOTE}

    def failed_without_a_run(route):
        if route.request.method != "GET":
            route.continue_()
            return
        response = route.fetch()
        body = response.json()
        if (body.get("active") or {}).get("state") in ("succeeded", "failed"):
            body["active"] = {**body["active"], "state": "failed", "result": None, "error": "A deep search is already running"}
            body["runs"] = [older]
        route.fulfill(response=response, body=json.dumps(body))

    owner_page.route(re.compile(r"/api/v1/outreach/discovery$"), failed_without_a_run)
    open_outreach(owner_page, "deep-search")
    owner_page.locator("details.outreach-deep-search").get_by_role("button", name="Run deep search now").click()
    status = owner_page.locator("#action-status")
    expect(status).to_contain_text("The deep search failed", timeout=30_000)
    expect(status).not_to_contain_text("Claude Code ran this search")


def test_the_contact_search_report_shows_the_note(owner_page, base_url):
    seed_target(owner_page, base_url, contact_email="hello@bovi.example", contact_confidence="confirmed")

    def with_report(route):
        response = route.fetch()
        body = response.json()
        body["recontact"]["active"] = {
            "state": "succeeded", "mode": "report", "started_at": utc_now(), "finished_at": utc_now(), "error": None,
            "result": {"checked": 1, "upgraded": 0, "results": [], "agent_note": NOTE},
        }
        route.fulfill(response=response, body=json.dumps(body))

    owner_page.route(re.compile(r"/api/v1/outreach$"), with_report)
    open_outreach(owner_page)
    open_tab(owner_page, "find-people")
    expect(owner_page.locator("section.outreach-recontact .outreach-agent-note")).to_have_text(NOTE)
