"""Possible replies in the browser: an email that may be the company's answer, waiting for the student to say.

outreach/inbox.py keeps such an email (kind 'possible') instead of logging it
or dropping it, and every automatic step that assumes silence waits for it.
These tests seed one exactly as a Gmail check would, through the same writer
(outreach_inbox._record_possible), into the live test database, then drive the
Outreach card: what it shows, what each answer sends, and that the answer that
lets a follow-up go asks first. Nothing here reaches Gmail; the reading of a
confirmed reply is answered by tests/ui/outreach_fakes.py.

Buttons are found by the words they show, and their accessible names are
asserted separately, so a change to a name's wording fails only the test
about names.

The server's side (judging, holds, the decision route) is covered by
tests/test_outreach_inbox.py and tests/test_outreach_schedule.py.
"""

from __future__ import annotations

import re
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from conftest import OWNER_TOKEN
from ui_helpers import assert_accessible, card_for, describe, open_details, open_outreach, row_for, seed_target
from opportunity_app.outreach.targets import get_target
from opportunity_app.outreach.inbox import _record_possible, _record_reply
from opportunity_app.core.database import connect_product
from opportunity_app.core.timestamps import utc_now

BEARER = {"Authorization": f"Bearer {OWNER_TOKEN}"}
USER = "local-user"
DECISION = re.compile(r".*/api/v1/outreach/[^/]+/possible-replies/[^/]+$")
LISTING = re.compile(r".*/api/v1/outreach(\?.*)?$")

SENDER = "careers@bovi.example"
SUBJECT = "Re: Internship question"
WORDS = "Thanks for writing!\n\nWe would like to talk next week.   Does Tuesday work?"
PREVIEW = "Thanks for writing! We would like to talk next week. Does Tuesday work?"
ASKING = "Not a reply: let the follow-up go?"


def sent_target(page, base_url, company="Bovi", domain="bovi.example", **overrides):
    """A company the student already wrote to, as the Awaiting reply tab lists it."""
    body = {
        "company": company, "website": f"https://{domain}", "source_urls": [f"https://{domain}/"],
        "contact_email": f"jane@{domain}", "contact_name": "Jane Doe", "status": "sent",
        "email_subject": "Internship question", "email_body": "Hi Jane,\n\nWould you be open to a call?\n\nTest Student",
    }
    return seed_target(page, base_url, **{**body, **overrides})


def seed_possible(live_server, targets, gmail_id, *, sender=SENDER, subject=SUBJECT, text=WORDS,
                  reason="shared_address", via="domain", in_spam=False):
    """Keep one email as a possible reply of ``targets`` (the first owns it; the rest could also have sent it)."""
    received = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(timespec="seconds")
    with closing(connect_product(live_server.live_path)) as conn:
        kept = _record_possible(
            conn, [{"id": target["id"], "company": target["company"]} for target in targets], user_id=USER,
            gmail_id=gmail_id, sender=sender, received=received, via=via, reason=reason, thread_id="",
            subject=subject, text=text, message_id="", from_name="", notify=False, in_spam=in_spam,
        )
    assert kept is not None, "the writer refused the seed"


def schedule_follow_up(live_server, target, state="scheduled", error=""):
    """An automatic follow-up queued for the company (outreach/schedule.py); 'scheduled' with an error is a held one."""
    now = utc_now()
    later = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(timespec="seconds")
    with closing(connect_product(live_server.live_path)) as conn:
        with conn:
            conn.execute(
                "INSERT INTO outreach_scheduled_sends(target_id, user_id, kind, fingerprint, send_at, timezone, label, state, error, "
                "created_at, updated_at) VALUES(?, ?, 'follow_up', 'f', ?, 'America/Chicago', ?, ?, ?, ?, ?)",
                (target["id"], USER, later, "Wed, Sep 30, 9:12 AM CDT (their time, from Austin, TX)", state, error, now, now),
            )


def stored_message(live_server, gmail_id):
    with closing(connect_product(live_server.live_path)) as conn:
        row = conn.execute(
            "SELECT target_id, kind, text, candidates_json FROM outreach_inbox_messages WHERE user_id=? AND gmail_id=?",
            (USER, gmail_id),
        ).fetchone()
    return dict(row) if row else None


def fetch_target(page, base_url, target):
    response = page.request.get(f"{base_url}/api/v1/outreach/{target['id']}", headers=BEARER)
    assert response.ok, response.text()
    return response.json()


def possible_block(card, sender=SENDER):
    return card.get_by_role("region", name=f"Possible reply from {sender}")


def answers(block):
    """The block's two answers, found by the words they show."""
    actions = block.locator(".outreach-possible-reply-actions")
    return (actions.locator("button", has_text=re.compile(r"^It's a reply, log it$")),
            actions.locator("button", has_text=re.compile(r"^Not a reply")))


def naming(*parts):
    """An accessible name that contains every one of ``parts``, in any order."""
    return re.compile("".join(f"(?=.*{re.escape(part)})" for part in parts), re.DOTALL)


def record_decisions(page):
    """Every decision the page posts, as (url path, body), in order."""
    posted: list[tuple[str, object]] = []
    page.on("request", lambda request: posted.append((urlsplit(request.url).path, request.post_data_json))
            if request.method == "POST" and DECISION.match(request.url) else None)
    return posted


# --- What the card shows -------------------------------------------------------------


def test_a_possible_reply_shows_on_the_card_with_why_it_waits_and_both_answers(owner_page, base_url, live_server):
    target = sent_target(owner_page, base_url)
    seed_possible(live_server, [target], "pr-1")
    open_outreach(owner_page, "awaiting")

    # The list row already says what to do.
    chip = row_for(owner_page, "Bovi").locator(".chip")
    expect(chip).to_have_text("Check a possible reply")
    expect(chip).to_have_class(re.compile("is-warning"))

    card = card_for(owner_page, "Bovi")
    block = possible_block(card)
    expect(block).to_be_visible()
    expect(block.locator(".outreach-possible-reply-head strong")).to_have_text("Possible reply:")
    expect(block.locator(".outreach-possible-reply-head")).to_have_text(
        re.compile(r"^Possible reply: careers@bovi\.example wrote \w{3} \d{1,2}, \d{4}, “Re: Internship question”\.$"))
    # Its own words, whitespace folded.
    expect(block.locator("blockquote.outreach-possible-reply-text")).to_have_text(PREVIEW)
    expect(block.locator(".outreach-possible-reply-why")).to_have_text(
        "Not counted as a reply yet: careers@ is a shared inbox, not one person. "
        "Follow-ups and closing as No response wait until you say.")

    # Two answers, each naming the email it answers for (and the company a reply is logged for),
    # so a second possible reply's buttons are never confused with these.
    yes, no = answers(block)
    expect(yes).to_have_count(1)
    expect(no).to_have_text("Not a reply")
    expect(yes).to_have_accessible_name(naming(SENDER, f"“{SUBJECT}”", "Bovi"))
    expect(no).to_have_accessible_name(naming(SENDER, f"“{SUBJECT}”"))
    expect(no).to_have_accessible_name(re.compile("not a reply", re.IGNORECASE))
    expect(yes).to_be_enabled()
    expect(no).to_be_enabled()

    gmail = block.get_by_role("link", name=naming("Open in Gmail", SUBJECT))
    expect(gmail).to_have_text("Open in Gmail")
    expect(gmail).to_have_attribute("href", "https://mail.google.com/mail/?authuser=student%40school.example#all/pr-1")
    expect(gmail).to_have_attribute("target", "_blank")
    expect(gmail).to_have_attribute("rel", "noopener noreferrer")

    # The next step asks about the email before anything else.
    bar = card.locator(".outreach-next")
    expect(bar).to_have_class(re.compile("is-warning"))
    expect(bar.locator(".outreach-next-text strong")).to_have_text("Next: Check a possible reply")
    expect(bar.locator(".outreach-next-text")).to_contain_text(
        "An email from them may be a reply. Say whether it is on the card; follow-ups wait until you do.")
    expect(card.locator(".application-heading")).to_contain_text("Sent")


def test_a_company_with_no_possible_reply_shows_no_block(owner_page, base_url, live_server):
    """The block is the waiting email's alone: a sibling company shows none and still waits for a reply."""
    bovi = sent_target(owner_page, base_url)
    sent_target(owner_page, base_url, company="Wren Motion", domain="wren-motion.example")
    seed_possible(live_server, [bovi], "pr-1")
    open_outreach(owner_page, "awaiting")
    wren = card_for(owner_page, "Wren Motion")
    expect(wren.locator(".outreach-possible-replies")).to_have_count(0)
    expect(wren.locator(".outreach-next-text strong")).to_have_text("Next: Wait for a reply")
    expect(row_for(owner_page, "Wren Motion").locator(".chip")).to_have_text("Wait for a reply")


def test_an_emails_words_are_shown_as_text_never_as_markup(owner_page, base_url, live_server):
    """Anyone can email the student: their subject and words must never become page markup."""
    target = sent_target(owner_page, base_url)
    hostile = '<img src=x onerror="window.__pwned=1"><b>Bold</b>'
    seed_possible(live_server, [target], "pr-x", subject=f"Re: {hostile}", text=f"Hello {hostile}")
    open_outreach(owner_page, "awaiting")
    block = possible_block(card_for(owner_page, "Bovi"))
    expect(block.locator("blockquote")).to_have_text(f"Hello {hostile}")
    expect(block.locator(".outreach-possible-reply-head")).to_contain_text(f"“Re: {hostile}”")
    expect(block.locator("img, b")).to_have_count(0)
    assert owner_page.evaluate("() => window.__pwned") is None


def test_several_possible_replies_each_get_their_own_answers_and_only_gmail_links_open(owner_page, base_url, live_server):
    """One card, several emails: plural wording, names that tell each email's buttons apart, and a link only to Gmail.

    The server always builds a mail.google.com link; the listing is rewritten
    here so the page's own check (https://mail.google.com/ only) is what is tested.
    """
    target = sent_target(owner_page, base_url)
    seed_possible(live_server, [target], "pr-spam", sender="talent@bovi.example", subject="Spam folder note", reason="spam", in_spam=True)
    unsafe = {
        "pr-script": "javascript:alert(document.domain)",
        "pr-http": "http://mail.google.com/mail/?authuser=0#all/pr-http",
        "pr-lookalike": "https://mail.google.com.evil.example/mail/#all/pr-lookalike",
        "pr-elsewhere": "https://evil.example/https://mail.google.com/",
        "pr-missing": None,
    }
    for index, gmail_id in enumerate(unsafe):
        seed_possible(live_server, [target], gmail_id, sender=f"person{index}@bovi.example", subject=f"Note {index}")

    def listing(route):
        response = route.fetch()
        body = response.json()
        for item in body["items"]:
            for mail in item.get("possible_replies") or []:
                if mail["gmail_id"] in unsafe:
                    mail["gmail_url"] = unsafe[mail["gmail_id"]]
        route.fulfill(response=response, json=body)

    owner_page.route(LISTING, listing)
    open_outreach(owner_page, "awaiting")
    card = card_for(owner_page, "Bovi")
    expect(card.locator(".outreach-possible-reply")).to_have_count(6)
    expect(card.locator(".outreach-next-text strong")).to_have_text("Next: Check possible replies")
    expect(card.locator(".outreach-next-text")).to_contain_text(
        "Emails from them may be a reply. Say whether each is on the card; follow-ups wait until you do.")
    expect(row_for(owner_page, "Bovi").locator(".chip")).to_have_text("Check possible replies")

    for index in range(len(unsafe)):
        sender, subject = f"person{index}@bovi.example", f"Note {index}"
        block = possible_block(card, sender)
        yes, no = answers(block)
        expect(yes).to_have_accessible_name(naming(sender, f"“{subject}”", "Bovi"))
        expect(no).to_have_accessible_name(naming(sender, f"“{subject}”"))
        # Exactly this email's two answers carry its sender and subject.
        expect(card.get_by_role("button", name=naming(sender, f"“{subject}”"))).to_have_count(2)
        expect(block.get_by_role("link")).to_have_count(0)

    # The one in Spam says so, and its link opens Gmail's Spam folder.
    spam = possible_block(card, "talent@bovi.example")
    expect(spam.locator(".outreach-possible-reply-head")).to_contain_text("“Spam folder note”. Gmail put it in Spam.")
    expect(spam.locator(".outreach-possible-reply-why")).to_contain_text("Not counted as a reply yet: Gmail put it in Spam.")
    links = card.locator(".outreach-possible-replies").get_by_role("link")
    expect(links).to_have_count(1)
    expect(links).to_have_attribute("href", "https://mail.google.com/mail/?authuser=student%40school.example#spam/pr-spam")
    owner_page.unroute_all(behavior="ignoreErrors")


# --- Answering ---------------------------------------------------------------------


def test_its_a_reply_posts_the_decision_logs_the_reply_and_reloads(owner_page, base_url, live_server):
    target = sent_target(owner_page, base_url)
    seed_possible(live_server, [target], "pr-1")
    open_outreach(owner_page, "awaiting")
    card = card_for(owner_page, "Bovi")
    yes, _ = answers(possible_block(card))

    with owner_page.expect_request(lambda request: request.method == "POST" and bool(DECISION.match(request.url))) as posted:
        yes.click()
    assert urlsplit(posted.value.url).path == f"/api/v1/outreach/{target['id']}/possible-replies/pr-1"
    assert posted.value.post_data_json == {"decision": "reply"}
    assert posted.value.headers.get("x-csrf-token"), "a cookie-authenticated write carries the CSRF header"

    expect(owner_page.locator("#action-status")).to_have_text("Logged careers@bovi.example's email as Bovi's reply.")
    card = card_for(owner_page, "Bovi")
    expect(card.locator(".outreach-possible-replies")).to_have_count(0)
    expect(card.locator(".application-heading")).to_contain_text("Replied")
    expect(card.locator(".outreach-next-text strong")).not_to_have_text(re.compile("possible repl"))

    after = fetch_target(owner_page, base_url, target)
    assert (after["status"], after["reply_count"], after["possible_reply_count"]) == ("replied", 1, 0)
    stored = stored_message(live_server, "pr-1")
    assert (stored["kind"], stored["text"]) == ("reply", ""), "settled, and its words live only in the history"

    timeline = open_details(card, "Replies and history").locator(".outreach-timeline")
    expect(timeline).to_contain_text("Possible reply found in Gmail")
    expect(timeline).to_contain_text("Thanks for writing! We would like to talk next week.")


def test_not_a_reply_without_a_follow_up_waiting_settles_it_at_once(owner_page, base_url, live_server):
    target = sent_target(owner_page, base_url)
    seed_possible(live_server, [target], "pr-1")
    posted = record_decisions(owner_page)
    open_outreach(owner_page, "awaiting")
    _, no = answers(possible_block(card_for(owner_page, "Bovi")))
    no.click()

    expect(owner_page.locator("#action-status")).to_have_text("Set aside careers@bovi.example's email; it is not a reply.")
    assert posted == [(f"/api/v1/outreach/{target['id']}/possible-replies/pr-1", {"decision": "not_reply"})]
    card = card_for(owner_page, "Bovi")
    expect(card.locator(".outreach-possible-replies")).to_have_count(0)
    expect(card.locator(".application-heading")).to_contain_text("Sent")
    expect(card.locator(".outreach-next-text strong")).to_have_text("Next: Wait for a reply")

    after = fetch_target(owner_page, base_url, target)
    assert (after["status"], after["reply_count"], after["possible_reply_count"]) == ("sent", 0, 0)
    stored = stored_message(live_server, "pr-1")
    assert (stored["kind"], stored["text"]) == ("dismissed", ""), "a dismissed email's words are not kept"
    timeline = open_details(card, "Replies and history").locator(".outreach-timeline")
    expect(timeline).to_contain_text("Not a reply, you said")


@pytest.mark.parametrize(("state", "error"), [
    ("scheduled", ""),
    ("scheduled", "Held: Bovi may have replied. Say whether it is a reply on the company's card"),
    ("sending", ""),
], ids=["scheduled", "held", "sending"])
def test_not_a_reply_asks_first_when_it_would_let_a_follow_up_go(owner_page, base_url, live_server, state, error):
    """A misclick would send an automatic follow-up to someone who may have answered, so the first click only asks."""
    target = sent_target(owner_page, base_url)
    seed_possible(live_server, [target], "pr-1")
    schedule_follow_up(live_server, target, state, error)
    posted = record_decisions(owner_page)
    open_outreach(owner_page, "awaiting")
    _, no = answers(possible_block(card_for(owner_page, "Bovi")))
    expect(no).to_have_text("Not a reply")

    no.click()
    expect(no).to_have_text(ASKING)
    expect(no).to_be_enabled()
    expect(no).to_be_focused()
    # Said aloud too, as Send's question is.
    expect(owner_page.locator("#action-status")).to_contain_text("let the follow-up go")
    assert posted == [], "the first click only asks"
    assert stored_message(live_server, "pr-1")["kind"] == "possible"

    no.click()
    expect(owner_page.locator("#action-status")).to_have_text("Set aside careers@bovi.example's email; it is not a reply.")
    assert posted == [(f"/api/v1/outreach/{target['id']}/possible-replies/pr-1", {"decision": "not_reply"})]
    expect(card_for(owner_page, "Bovi").locator(".outreach-possible-replies")).to_have_count(0)
    assert stored_message(live_server, "pr-1")["kind"] == "dismissed"


def test_its_a_reply_never_asks_first_even_with_a_follow_up_scheduled(owner_page, base_url, live_server):
    """Saying it is a reply stops the follow-up, so there is nothing to confirm."""
    target = sent_target(owner_page, base_url)
    seed_possible(live_server, [target], "pr-1")
    schedule_follow_up(live_server, target)
    posted = record_decisions(owner_page)
    open_outreach(owner_page, "awaiting")
    yes, _ = answers(possible_block(card_for(owner_page, "Bovi")))
    yes.click()
    expect(owner_page.locator("#action-status")).to_have_text("Logged careers@bovi.example's email as Bovi's reply.")
    assert posted == [(f"/api/v1/outreach/{target['id']}/possible-replies/pr-1", {"decision": "reply"})]


@pytest.mark.allow_page_errors  # the server's 409 is the point of the test
def test_an_answer_already_given_in_another_tab_shows_the_card_as_it_is_now(owner_page, base_url, live_server):
    """The student said "Not a reply" in another tab; "It's a reply" on this stale card must not log it, and the card
    shows what the server holds now."""
    target = sent_target(owner_page, base_url)
    seed_possible(live_server, [target], "pr-1")
    open_outreach(owner_page, "awaiting")
    yes, _ = answers(possible_block(card_for(owner_page, "Bovi")))
    expect(yes).to_be_visible()
    elsewhere = owner_page.request.post(f"{base_url}/api/v1/outreach/{target['id']}/possible-replies/pr-1",
                                        headers=BEARER, data={"decision": "not_reply"})
    assert elsewhere.ok, elsewhere.text()

    yes.click()
    expect(owner_page.locator("#error-banner")).to_contain_text("You already said whether this email is a reply")
    card = card_for(owner_page, "Bovi")
    expect(card.locator(".outreach-possible-replies")).to_have_count(0)
    expect(card.locator(".application-heading")).to_contain_text("Sent")
    after = fetch_target(owner_page, base_url, target)
    assert (after["status"], after["reply_count"]) == ("sent", 0), "a stale click logged nothing"
    assert stored_message(live_server, "pr-1")["kind"] == "dismissed"


@pytest.mark.allow_page_errors  # the 500 is the point of the test
def test_an_answer_that_fails_can_be_given_again(owner_page, base_url, live_server):
    """The server failed: nothing changed, so the error shows, both answers come back, and focus stays on the one pressed."""
    target = sent_target(owner_page, base_url)
    seed_possible(live_server, [target], "pr-1")
    open_outreach(owner_page, "awaiting")
    owner_page.route(DECISION, lambda route: route.fulfill(
        status=500, content_type="application/json", body='{"detail": "Could not reach the database"}'))
    yes, no = answers(possible_block(card_for(owner_page, "Bovi")))
    no.click()
    expect(owner_page.locator("#error-banner")).to_contain_text("Could not reach the database")
    expect(yes).to_be_enabled()
    expect(no).to_be_enabled()
    expect(no).to_be_focused()
    assert stored_message(live_server, "pr-1")["kind"] == "possible"
    owner_page.unroute_all(behavior="ignoreErrors")


# --- An email more than one company could have sent -----------------------------------


def test_an_email_two_companies_could_have_sent_names_the_other_on_each_card(owner_page, base_url, live_server):
    bovi = sent_target(owner_page, base_url)
    wren = sent_target(owner_page, base_url, company="Wren Motion", domain="wren-motion.example")
    # Written to Wren Motion last, so the row is Wren Motion's and Bovi is the other candidate.
    seed_possible(live_server, [wren, bovi], "pr-amb", sender="dana@shared-studio.example", reason="ambiguous", via="address")
    open_outreach(owner_page, "awaiting")

    for company, other in (("Bovi", "Wren Motion"), ("Wren Motion", "Bovi")):
        card = card_for(owner_page, company)
        block = possible_block(card, "dana@shared-studio.example")
        expect(block.locator(".outreach-possible-reply-why")).to_have_text(
            "Not counted as a reply yet: more than one company you wrote to could have sent it. "
            "Follow-ups and closing as No response wait until you say. "
            f"It could also be from {other}; saying it is a reply here logs it for {company}.")
        yes, _ = answers(block)
        expect(yes).to_have_accessible_name(naming("dana@shared-studio.example", f"“{SUBJECT}”", company))
        expect(card.locator(".outreach-next-text strong")).to_have_text("Next: Check a possible reply")

    # Settled on Bovi's card: it is Bovi's reply, and Wren Motion is no longer held by it.
    posted = record_decisions(owner_page)
    yes, _ = answers(possible_block(card_for(owner_page, "Bovi"), "dana@shared-studio.example"))
    yes.click()
    expect(owner_page.locator("#action-status")).to_have_text("Logged dana@shared-studio.example's email as Bovi's reply.")
    assert posted == [(f"/api/v1/outreach/{bovi['id']}/possible-replies/pr-amb", {"decision": "reply"})]
    expect(card_for(owner_page, "Bovi").locator(".outreach-possible-replies")).to_have_count(0)
    wren_card = card_for(owner_page, "Wren Motion")
    expect(wren_card.locator(".outreach-possible-replies")).to_have_count(0)
    expect(wren_card.locator(".outreach-next-text strong")).to_have_text("Next: Wait for a reply")
    assert stored_message(live_server, "pr-amb")["target_id"] == bovi["id"], "the email moved to the company it was said to be from"
    assert (fetch_target(owner_page, base_url, wren)["status"], fetch_target(owner_page, base_url, bovi)["status"]) == ("sent", "replied")


def test_not_a_reply_asks_first_on_any_card_whose_answer_lets_a_follow_up_go(owner_page, base_url, live_server):
    """An email Bovi or Wren Motion could have sent holds Bovi's follow-up. Saying "Not a reply" on Wren Motion's
    card releases that follow-up just the same, so it must ask first there too."""
    bovi = sent_target(owner_page, base_url)
    wren = sent_target(owner_page, base_url, company="Wren Motion", domain="wren-motion.example")
    seed_possible(live_server, [wren, bovi], "pr-amb", sender="dana@shared-studio.example", reason="ambiguous", via="address")
    schedule_follow_up(live_server, bovi)
    posted = record_decisions(owner_page)
    open_outreach(owner_page, "awaiting")
    _, no = answers(possible_block(card_for(owner_page, "Wren Motion"), "dana@shared-studio.example"))
    no.click()
    # Long enough for a one-click dismissal to post; the passing path only waits.
    owner_page.wait_for_timeout(750)
    assert posted == [], (
        f"one click on Wren Motion's card dismissed the email and released Bovi's scheduled follow-up: {posted}")
    expect(no).to_have_text(ASKING)
    assert stored_message(live_server, "pr-amb")["kind"] == "possible"


# --- Found by a Gmail check while the page is open ------------------------------------


def gmail_check(page, *answers_in_turn):
    """A connected Gmail with read access, whose checks answer ``answers_in_turn`` in order and then find nothing."""
    connected = {"configured": True, "connected": True, "needs_reconnect": False, "bounce_check": True,
                 "account": "student@school.example", "attachment": "", "attachment_problem": ""}
    queue = list(answers_in_turn)

    def listing(route):
        response = route.fetch()
        route.fulfill(response=response, json={**response.json(), "gmail_drafts": connected})

    def check(route):
        found = queue.pop(0) if queue else {}
        route.fulfill(json={"state": "ok", "bounced": [], "replies": [], "automatic": [], "possible": [],
                            "sent_in_gmail": [], "scheduled_in_gmail": [], **found})

    page.route(LISTING, listing)
    page.route("**/api/v1/outreach/inbox-check", check)


def test_a_gmail_check_that_finds_a_possible_reply_says_so_and_shows_it(owner_page, base_url, live_server):
    target = sent_target(owner_page, base_url)
    seed_possible(live_server, [target], "pr-1")
    gmail_check(owner_page, {"possible": [{"target_id": target["id"], "company": "Bovi", "from": SENDER,
                                           "reason": "shared_address", "companies": ["Bovi"]}]})
    open_outreach(owner_page, "awaiting")
    expect(owner_page.locator("#action-status")).to_have_text(
        "Maybe a reply from Bovi (careers@bovi.example). Say whether it is on the company's card; follow-ups wait until you do.")
    expect(possible_block(card_for(owner_page, "Bovi"))).to_be_visible()
    owner_page.unroute_all(behavior="ignoreErrors")


def test_a_gmail_check_names_every_company_an_ambiguous_email_could_be_from(owner_page, base_url, live_server):
    """The notice for an email two companies could have sent is neutral (outreach_inbox._record_possible); the page
    must not pin it on the one written to last."""
    bovi = sent_target(owner_page, base_url)
    wren = sent_target(owner_page, base_url, company="Wren Motion", domain="wren-motion.example")
    seed_possible(live_server, [wren, bovi], "pr-amb", sender="dana@shared-studio.example", reason="ambiguous", via="address")
    gmail_check(owner_page, {"possible": [{"target_id": wren["id"], "company": "Wren Motion", "from": "dana@shared-studio.example",
                                           "reason": "ambiguous", "companies": ["Wren Motion", "Bovi"]}]})
    open_outreach(owner_page, "awaiting")
    status = owner_page.locator("#action-status")
    expect(status).to_contain_text("dana@shared-studio.example")
    expect(status).to_contain_text("Wren Motion")
    expect(status).to_contain_text("Bovi")
    owner_page.unroute_all(behavior="ignoreErrors")


# --- What else the card says while one waits, and after -------------------------------


def test_a_follow_up_date_that_passes_while_a_possible_reply_waits_is_not_called_due(owner_page, base_url, live_server):
    """Urgent drops the follow-up row for such a company (urgent._outreach_rows: "same rule as Outreach's own"), and
    the next step says follow-ups wait; the card's chip and the Follow-ups due tab must not say the opposite."""
    target = sent_target(owner_page, base_url)
    with closing(connect_product(live_server.live_path)) as conn:
        with conn:
            conn.execute("UPDATE outreach_targets SET follow_up_at=? WHERE id=?",
                         ((date.today() - timedelta(days=3)).isoformat(), target["id"]))
    seed_possible(live_server, [target], "pr-1")
    open_outreach(owner_page, "awaiting")
    card = card_for(owner_page, "Bovi")
    expect(card.locator(".outreach-next-text strong")).to_have_text("Next: Check a possible reply")
    expect(card.locator(".chip", has_text=re.compile(r"^Follow-up due"))).to_have_count(0)
    expect(owner_page.locator('#subnav [data-subtab="follow-ups-due"] .subnav-count')).not_to_have_text("1")


def test_the_latest_reply_found_in_gmail_says_how_it_was_matched_even_beside_a_reading(owner_page, base_url, live_server):
    """design-v2: the latest Gmail-found reply's provenance line is always shown, so a reply matched by the
    company's domain (not the address written to) never reads as if it came from the contact."""
    target = sent_target(owner_page, base_url)
    received = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
    # Logged exactly as a Gmail check logs one matched by domain evidence; the keyword rules read it as a call.
    with closing(connect_product(live_server.live_path)) as conn:
        logged = _record_reply(
            conn, get_target(conn, target["id"], user_id=USER), user_id=USER, gmail_id="r-1", sender="dana@bovi.example",
            received=received, text="Thanks for reaching out! Could we set up a call next week?", decisions=None,
            via="domain", reason="domain_person", addresses={"jane@bovi.example"}, notify=False,
        )
    assert logged and logged["suggestion"]["status"] == "call_scheduled", logged
    open_outreach(owner_page, "replied")
    # The card's head, not its history: the reply_found event there says the same, but only once opened.
    head = card_for(owner_page, "Bovi").locator(".outreach-pane-head")
    expect(head).to_contain_text("dana@bovi.example replied")
    expect(head).to_contain_text("It reads as Call scheduled")
    expect(head).to_contain_text(
        "dana@bovi.example wrote to you from the company's own domain, as themselves and verified by Gmail")


def test_a_reply_the_student_confirmed_says_so_where_the_card_says_how_it_was_found(owner_page, base_url, live_server):
    """Once the student says a possible reply is one, the card's "Latest reply found in Gmail" line must not offer the
    reason it was doubted (a shared inbox) as how the reply was matched: it counts because the student said so."""
    target = sent_target(owner_page, base_url)
    seed_possible(live_server, [target], "pr-1")
    open_outreach(owner_page, "awaiting")
    yes, _ = answers(possible_block(card_for(owner_page, "Bovi")))
    yes.click()
    expect(owner_page.locator("#action-status")).to_have_text("Logged careers@bovi.example's email as Bovi's reply.")
    found = card_for(owner_page, "Bovi").locator(".outreach-fit", has_text="Latest reply found in Gmail")
    expect(found).to_have_count(1)
    expect(found).to_contain_text(re.compile("you said", re.IGNORECASE))


# --- Accessibility and layout ---------------------------------------------------------


def _card_with_every_part(page, base_url, live_server):
    bovi = sent_target(page, base_url)
    wren = sent_target(page, base_url, company="Wren Motion", domain="wren-motion.example")
    seed_possible(live_server, [bovi], "pr-1")
    seed_possible(live_server, [bovi], "pr-spam", sender="talent@bovi.example", subject="Spam folder note", reason="spam", in_spam=True)
    seed_possible(live_server, [wren, bovi], "pr-amb", sender="dana@shared-studio.example", reason="ambiguous", via="address")
    schedule_follow_up(live_server, bovi)
    open_outreach(page, "awaiting")
    card = card_for(page, "Bovi")
    expect(card.locator(".outreach-possible-reply")).to_have_count(3)
    return card


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_a_card_with_possible_replies_is_accessible(owner_page, base_url, live_server, theme):
    if theme == "dark":
        owner_page.evaluate("document.documentElement.dataset.theme = 'dark'")
    card = _card_with_every_part(owner_page, base_url, live_server)
    assert_accessible(owner_page, f"an outreach card with possible replies ({theme})")
    # Asking to confirm changes the button; the page must stay accessible while it asks.
    _, no = answers(possible_block(card))
    no.click()
    expect(no).to_have_text(ASKING)
    assert_accessible(owner_page, f"an outreach card asking to confirm Not a reply ({theme})")


def test_the_answers_are_named_by_the_words_they_show(owner_page, base_url, live_server):
    """WCAG 2.5.3 Label in Name (level A): a control's accessible name contains its visible words, so speech users
    can say what they see, and a screen reader hears the question "Not a reply" asks before it lets a follow-up go.

    axe keeps this rule experimental, so the page-wide scans in test_accessibility.py do not run it; it runs here on
    the possible replies alone.
    """
    from axe_core_python.sync_playwright import Axe

    card = _card_with_every_part(owner_page, base_url, live_server)
    options = {"runOnly": {"type": "rule", "values": ["label-content-name-mismatch"]}}

    def mismatches():
        results = Axe().run(owner_page, context=".outreach-possible-replies", options=options)
        assert results.get("passes") or results.get("violations"), "axe checked nothing: the rule or context is wrong"
        return results.get("violations", [])

    before = mismatches()
    _, no = answers(possible_block(card))
    no.click()
    expect(no).to_have_text(ASKING)
    asking = mismatches()
    assert not (before or asking), (
        f"before any click:\n{describe(before) or '  none'}\nwhile asking to confirm Not a reply:\n{describe(asking) or '  none'}")


def test_escape_or_leaving_backs_out_of_the_not_a_reply_question(owner_page, base_url, live_server):
    """Like every other ask-first button here (Send, Schedule), Escape or moving away takes the question back."""
    target = sent_target(owner_page, base_url)
    seed_possible(live_server, [target], "pr-1")
    schedule_follow_up(live_server, target)
    posted = record_decisions(owner_page)
    open_outreach(owner_page, "awaiting")
    yes, no = answers(possible_block(card_for(owner_page, "Bovi")))
    no.click()
    expect(no).to_have_text(ASKING)
    owner_page.keyboard.press("Escape")
    expect(no).to_have_text("Not a reply")

    no.click()
    expect(no).to_have_text(ASKING)
    yes.focus()
    expect(no).to_have_text("Not a reply")
    assert posted == [], "backing out posts nothing"


def test_a_possible_reply_card_fits_a_phone(owner_page, base_url, live_server):
    owner_page.set_viewport_size({"width": 375, "height": 812})
    target = sent_target(owner_page, base_url)
    sender = "a-very-long-shared-inbox-address-for-careers-and-internships@bovi.example"
    seed_possible(live_server, [target], "pr-long", sender=sender,
                  subject="Re: " + "An unusually long subject line that keeps going " * 4, text="word " * 200)
    open_outreach(owner_page, "awaiting")
    expect(possible_block(card_for(owner_page, "Bovi"), sender)).to_be_visible()
    overflow = owner_page.evaluate("() => document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 0, f"the outreach view scrolls sideways by {overflow}px at 375px wide"
