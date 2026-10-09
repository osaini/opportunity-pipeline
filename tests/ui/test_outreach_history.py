"""The Replies and history tab: events grouped by day, newest first, each with its time and what it recorded under its name.

The history used to be one flat list in which each event's detail sat as close to the next event's name as to its own, and
a few events showed their raw key or the JSON a contact-form attempt keeps. These tests seed the live database directly with
the events the server writes (outreach.targets.log_event) and read the tab as the student sees it.
"""

from __future__ import annotations

import json
import re

from playwright.sync_api import expect

from ui_helpers import assert_accessible, card_for, db, open_outreach, seed_target

USER = "local-user"
REPLY = "\n".join(["Thanks for writing!", "", "We would like to talk next week.", "Does Tuesday at 2pm work?", "", "Sam will join.", "", "Best,", "Jane"])
# Oldest first, as they happened; mid-day in UTC so each stays on its own calendar day in any time zone near the US.
EVENTS = [
    ("2026-10-03T15:00:00+00:00", "created", None, None, ""),
    ("2026-10-03T15:01:00+00:00", "location_recorded", None, None,
     "San Jose, CA from the company's site (https://www.orbital.example/about); replaced Campbell, CA from the deep search"),
    ("2026-10-03T15:02:00+00:00", "contact_applied", None, None, "info@orbital.example (confirmed, site generic)"),
    ("2026-10-03T15:03:00+00:00", "email_search", None, None, "No addresses proposed"),
    ("2026-10-03T15:04:00+00:00", "status", "not_started", "drafted", ""),
    ("2026-10-05T15:00:00+00:00", "form_unconfirmed", None, None, json.dumps({
        "kind": "initial", "page_url": "https://www.orbital.example/contact", "confirmation": "",
        "note": "Their page did not show a confirmation", "in_browser": False, "filled": ["name", "email"],
    })),
    ("2026-10-05T15:01:00+00:00", "reply_logged", None, None, REPLY),
]


def history_tab(page, base_url, live_server):
    target = seed_target(page, base_url, company="Orbital Demo", website="https://www.orbital.example")
    with db(live_server) as conn, conn:
        conn.execute("DELETE FROM outreach_events WHERE target_id=?", (target["id"],))
        for number, (stamp, event_type, before, after, detail) in enumerate(EVENTS):
            conn.execute(
                "INSERT INTO outreach_events(id, target_id, user_id, event_type, from_status, to_status, detail, created_at) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (f"ev-history-{number}", target["id"], USER, event_type, before, after, detail, stamp),
            )
    open_outreach(page, "all")
    card = card_for(page, "Orbital Demo")
    card.get_by_role("tab", name="Replies and history", exact=True).click()
    timeline = card.locator(".outreach-timeline")
    expect(timeline).to_be_visible()
    return card, timeline


def event(timeline, title):
    return timeline.locator(".outreach-event", has=timeline.page.locator(".outreach-event-title", has_text=re.compile(f"^{re.escape(title)}$")))


def test_the_history_groups_events_by_day_newest_first(owner_page, base_url, live_server):
    card, timeline = history_tab(owner_page, base_url, live_server)
    expect(card.locator(".outreach-history-count")).to_have_text("7 events, newest first")
    days = timeline.locator(".outreach-history-day")
    expect(days).to_have_count(2)
    expect(days.nth(0).locator(".outreach-event-title")).to_have_text(["Reply logged", "Contact form may have been sent"])
    expect(days.nth(1).locator(".outreach-event-title")).to_have_text([
        "Not started → Drafted", "Looked for an email on other sites", "Contact applied", "Location recorded", "Added",
    ])
    first, second = days.nth(0).locator(".outreach-history-date").inner_text(), days.nth(1).locator(".outreach-history-date").inner_text()
    assert first and second and first != second
    expect(days.nth(1).locator(".outreach-event.is-status")).to_have_count(1)
    expect(event(timeline, "Added").locator("time")).to_have_attribute("datetime", "2026-10-03T15:00:00+00:00")


def test_each_detail_sits_under_its_own_event_in_words(owner_page, base_url, live_server):
    _, timeline = history_tab(owner_page, base_url, live_server)
    location = event(timeline, "Location recorded")
    expect(location.locator(".outreach-event-detail")).to_have_text(
        "San Jose, CA from the company's site (orbital.example/about); replaced Campbell, CA from the deep search")
    expect(location.get_by_role("link", name="orbital.example/about")).to_have_attribute("href", "https://www.orbital.example/about")
    expect(event(timeline, "Contact applied").locator(".outreach-event-detail")).to_have_text(
        "info@orbital.example · Shared inbox on their site · confirmed")
    form = event(timeline, "Contact form may have been sent").locator(".outreach-event-detail")
    expect(form).to_have_text("Their page did not show a confirmation · orbital.example/contact")
    expect(timeline).not_to_contain_text("{")
    expect(event(timeline, "Added").locator(".outreach-event-detail")).to_have_count(0)


def test_a_long_reply_is_quoted_and_folded_until_asked(owner_page, base_url, live_server):
    card, timeline = history_tab(owner_page, base_url, live_server)
    reply = event(timeline, "Reply logged")
    quote = reply.locator("blockquote.outreach-event-quote")
    expect(quote).to_contain_text("Does Tuesday at 2pm work?")
    expect(quote).to_have_class(re.compile(r"\bis-folded\b"))
    more = reply.get_by_role("button", name="Show the whole reply")
    expect(more).to_have_attribute("aria-expanded", "false")
    more.click()
    expect(quote).not_to_have_class(re.compile(r"\bis-folded\b"))
    expect(reply.get_by_role("button", name="Show less")).to_have_attribute("aria-expanded", "true")
    assert_accessible(owner_page, "the outreach history tab")
