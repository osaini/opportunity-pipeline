"""The confirmed/guess boundary in contact finding, and everything that feeds it.

The product promise is that a guessed address is never shown as confirmed.
These tests pin where that line sits: what counts as published, what a mail
server's answer can and cannot do, what a page on another site must show, and
what an unattended run is allowed to address a draft to.
"""

import base64
import contextlib
import copy
import email
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx

from opportunity_app import schema
from opportunity_app.outreach import DraftChangedError, _draft_fingerprint, approve_draft, create_target, get_target, update_target
from opportunity_app.outreach_contacts import (
    apply_candidate,
    apply_choice,
    choose_contact,
    decode_cloudflare_email,
    discover_candidates,
    find_contacts,
    guess_strength,
    list_candidates,
)
from opportunity_app.outreach_discovery import run_discovery
from opportunity_app.outreach_email_search import check_person, search_emails
from opportunity_app.outreach_gmail import _mime
from opportunity_app.outreach_recontact import RecontactManager, apply_recontact, eligible_targets, recontact_targets
from opportunity_app.outreach_smtp import ACCEPTED, CATCH_ALL, REJECTED, UNKNOWN, SmtpVerifier, classify
from opportunity_app.schema import connect_product, ensure_product_schema

from helpers_platform import build_and_migrate
from test_outreach_discovery import USER, company, only_for, proposals, safe_fetcher, site_transport


def candidate(email="", *, name="", role="", method="pattern_guess", confidence="unverified",
              verification="", pattern_observed=False, cid=None):
    return {
        "id": cid or f"c-{email or name}", "name": name, "role": role, "email": email, "method": method,
        "confidence": confidence, "evidence_url": "https://acme.test/team", "verification": verification,
        "pattern_observed": pattern_observed, "note": "",
    }


PUBLISHED = candidate("jane.doe@acme.test", name="Jane Doe", role="CTO", method="site_published", confidence="confirmed")
INBOX = candidate("hello@acme.test", method="site_generic", confidence="confirmed")
CAREERS = candidate("careers@acme.test", method="site_generic", confidence="confirmed")
PRESS = candidate("press@acme.test", method="site_generic", confidence="confirmed")
WEAK = candidate("sam@acme.test", name="Sam Lee", role="CEO")
ACCEPTED_GUESS = candidate("sam.lee@acme.test", name="Sam Lee", role="CEO", verification="smtp_accepted")
REJECTED_GUESS = candidate("sam.lee@acme.test", name="Sam Lee", role="CEO", verification="smtp_rejected")
OBSERVED_GUESS = candidate("sam.lee@acme.test", name="Sam Lee", role="CEO", pattern_observed=True)
ELSEWHERE = candidate("sam@acme.test", name="Sam Lee", role="CEO", method="published_elsewhere")
PERSON_ONLY = candidate(name="Rita Moreno", role="Founder", method="site_person", confidence="unknown")
AI_NAMED = candidate(name="Rita Moreno", role="Founder", method="ai_research", confidence="unknown")


class ChooseContactTests(unittest.TestCase):
    """choose_contact() is the only thing that decides what an unattended run may use."""

    def test_a_confirmed_personal_address_wins_and_needs_no_cc(self):
        choice = choose_contact([ACCEPTED_GUESS, ELSEWHERE, INBOX, PUBLISHED])
        self.assertEqual((choice["to"]["email"], choice["cc"], choice["basis"]), ("jane.doe@acme.test", None, "confirmed"))

    def test_a_strong_guess_goes_to_the_person_with_the_shared_inbox_in_cc(self):
        for strong in (ACCEPTED_GUESS, ELSEWHERE, OBSERVED_GUESS):
            with self.subTest(strong=strong["method"] + strong["verification"] + str(strong["pattern_observed"])):
                choice = choose_contact([INBOX, strong])
                self.assertEqual(choice["to"]["email"], strong["email"])
                self.assertEqual(choice["cc"]["email"], "hello@acme.test")
                self.assertEqual(choice["basis"], "strong_guess")

    def test_a_strong_guess_without_an_inbox_still_goes_to_the_person(self):
        choice = choose_contact([ACCEPTED_GUESS])
        self.assertEqual((choice["to"]["email"], choice["cc"]), ("sam.lee@acme.test", None))

    def test_a_weak_guess_is_used_only_with_an_inbox_to_catch_it(self):
        self.assertIsNone(choose_contact([WEAK]))
        choice = choose_contact([WEAK, INBOX])
        self.assertEqual((choice["to"]["email"], choice["cc"]["email"], choice["basis"]), ("sam@acme.test", "hello@acme.test", "weak_guess"))

    def test_a_catch_all_or_silent_server_leaves_a_guess_weak(self):
        for verification in ("catch_all", "smtp_unknown", ""):
            with self.subTest(verification=verification):
                guess = candidate("sam@acme.test", name="Sam Lee", verification=verification)
                self.assertEqual(guess_strength(guess), 2)
                self.assertIsNone(choose_contact([guess]))

    def test_a_guess_the_mail_server_refused_is_never_used(self):
        self.assertIsNone(guess_strength(REJECTED_GUESS))
        choice = choose_contact([REJECTED_GUESS, INBOX])
        self.assertEqual((choice["to"]["email"], choice["basis"]), ("hello@acme.test", "shared_inbox"))
        self.assertIsNone(choose_contact([REJECTED_GUESS]))
        refused_elsewhere = {**ELSEWHERE, "verification": "smtp_rejected"}
        self.assertIsNone(choose_contact([refused_elsewhere]))

    def test_people_without_an_address_are_never_chosen(self):
        self.assertIsNone(choose_contact([PERSON_ONLY, AI_NAMED]))
        self.assertEqual(choose_contact([PERSON_ONLY, AI_NAMED, INBOX])["to"]["email"], "hello@acme.test")

    def test_the_stronger_evidence_wins_between_guesses(self):
        observed = candidate("rita.m@acme.test", name="Rita Moreno", role="Head of Talent", pattern_observed=True)
        choice = choose_contact([observed, ACCEPTED_GUESS, INBOX])
        self.assertEqual(choice["to"]["email"], "sam.lee@acme.test", "a server's acceptance outranks a format match")

    def test_the_hiring_person_wins_between_equal_guesses(self):
        talent = candidate("rita@acme.test", name="Rita Moreno", role="Head of Talent", verification="smtp_accepted")
        self.assertEqual(choose_contact([ACCEPTED_GUESS, talent])["to"]["email"], "rita@acme.test")

    def test_the_careers_inbox_is_preferred_over_press(self):
        self.assertEqual(choose_contact([PRESS, INBOX, CAREERS])["to"]["email"], "careers@acme.test")
        self.assertEqual(choose_contact([PRESS, WEAK, CAREERS])["cc"]["email"], "careers@acme.test")

    def test_only_a_confirmed_site_address_counts_as_personal(self):
        unconfirmed = {**PUBLISHED, "confidence": "unverified"}
        choice = choose_contact([unconfirmed, INBOX])
        self.assertNotEqual(choice["basis"], "confirmed")

    def test_an_unconfirmed_generic_address_is_not_a_shared_inbox(self):
        unconfirmed = {**INBOX, "confidence": "unverified"}
        self.assertIsNone(choose_contact([unconfirmed, WEAK]))

    def test_a_guess_is_never_relabeled_confirmed(self):
        for strong in (ACCEPTED_GUESS, ELSEWHERE, OBSERVED_GUESS, WEAK):
            choice = choose_contact([strong, INBOX])
            self.assertEqual(choice["to"]["confidence"], "unverified")


class ParsingTests(unittest.TestCase):
    def discover(self, pages, mx=False, **kwargs):
        transport, _ = site_transport({"acme.test": {"/robots.txt": "", **pages}}, mx=mx)
        with httpx.Client(transport=transport) as client:
            return discover_candidates("https://acme.test", fetcher=safe_fetcher(client), delay=0, **kwargs)

    @staticmethod
    def cloudflare(address, key=0x5a):
        return (bytes([key]) + bytes(byte ^ key for byte in address.encode())).hex()

    def test_cloudflare_protected_addresses_are_published_and_keep_their_owner(self):
        self.assertEqual(decode_cloudflare_email(self.cloudflare("jane@acme.test")), "jane@acme.test")
        self.assertEqual(decode_cloudflare_email("zz"), "")
        link = f'<h3><a href="/cdn-cgi/l/email-protection#{self.cloudflare("jane.doe@acme.test")}">Jane Doe</a></h3><p>CTO</p>'
        span = f'<p>Write <span class="__cf_email__" data-cfemail="{self.cloudflare("hello@acme.test")}">[email&#160;protected]</span></p>'
        result = self.discover({"/": link + span})
        by_email = {item["email"]: item for item in result["candidates"]}
        self.assertEqual((by_email["jane.doe@acme.test"]["name"], by_email["jane.doe@acme.test"]["confidence"]), ("Jane Doe", "confirmed"))
        self.assertEqual(by_email["hello@acme.test"]["method"], "site_generic")
        self.assertFalse(any("protected" in address for address in by_email))

    def test_spelled_out_addresses_are_published(self):
        result = self.discover({"/": "<p>jane [at] acme [dot] test</p><p>sam(at)acme.test</p><p>lee at acme dot test</p>"})
        self.assertEqual({item["email"] for item in result["candidates"]}, {"jane@acme.test", "sam@acme.test", "lee@acme.test"})
        self.assertTrue(all(item["confidence"] == "confirmed" for item in result["candidates"]))

    def test_a_named_person_without_an_address_is_a_site_person_not_a_published_address(self):
        result = self.discover({"/": "<h3>Rita Moreno</h3><p>Founder and CEO</p>"})
        (rita,) = result["candidates"]
        self.assertEqual((rita["method"], rita["email"], rita["confidence"]), ("site_person", "", "unknown"))

    def test_guesses_say_where_their_format_came_from(self):
        pages = {"/": '<h3><a href="mailto:jane.doe@acme.test">Jane Doe</a></h3><p>CTO</p><h3>Sam Lee</h3><p>CEO</p>'}
        observed = [item for item in self.discover(pages, mx=True)["candidates"] if item["method"] == "pattern_guess"]
        self.assertEqual([(item["email"], item["pattern_observed"]) for item in observed], [("sam.lee@acme.test", True)])
        self.assertIn("first.last@ format", observed[0]["note"])
        self.assertNotIn("jane.doe@acme.test", observed[0]["note"], "one address on screen per row")

        common = [item for item in self.discover({"/": "<h3>Sam Lee</h3><p>CEO</p>"}, mx=True)["candidates"] if item["method"] == "pattern_guess"]
        self.assertEqual({item["email"] for item in common}, {"sam@acme.test", "sam.lee@acme.test", "slee@acme.test"})
        self.assertTrue(all(not item["pattern_observed"] and item["confidence"] == "unverified" for item in common))

    def test_a_verifier_answer_is_recorded_on_each_guess_and_never_confirms_it(self):
        class Verifier:
            asked = None

            def check(self, domain, addresses):
                Verifier.asked = (domain, list(addresses))
                return {"sam@acme.test": ACCEPTED, "sam.lee@acme.test": REJECTED}

        result = self.discover({"/": "<h3>Sam Lee</h3><p>CEO</p><p>hello@acme.test</p>"}, mx=True, verifier=Verifier())
        guesses = {item["email"]: item for item in result["candidates"] if item["method"] == "pattern_guess"}
        self.assertEqual(Verifier.asked[0], "acme.test")
        self.assertNotIn("hello@acme.test", Verifier.asked[1], "published addresses are not put to the server")
        self.assertEqual(guesses["sam@acme.test"]["verification"], ACCEPTED)
        self.assertEqual(guesses["sam.lee@acme.test"]["verification"], REJECTED)
        self.assertEqual(guesses["slee@acme.test"]["verification"], "")
        self.assertTrue(all(item["confidence"] == "unverified" for item in guesses.values()))
        order = [item["email"] for item in result["candidates"] if item["method"] == "pattern_guess"]
        self.assertEqual(order[0], "sam@acme.test")
        self.assertEqual(order[-1], "sam.lee@acme.test", "a refused guess sorts last")

    def test_a_script_built_team_page_is_read_in_a_browser(self):
        class Renderer:
            unavailable = ""

            def render(self, url):
                if url.rstrip("/") == "https://acme.test":
                    return url, '<a href="/team">Team</a>'
                if url.rstrip("/") == "https://acme.test/team":
                    return url, '<h3><a href="mailto:rita@acme.test">Rita Moreno</a></h3><p>CEO</p>'
                return None

        shell = {"/": '<div id="root"></div><a href="/team">Team</a>', "/team": '<div id="root"></div>'}
        plain = self.discover(shell)
        self.assertEqual((plain["candidates"], plain["rendered"]), ([], False))
        rendered = self.discover(shell, renderer=Renderer())
        self.assertTrue(rendered["rendered"])
        self.assertEqual([(item["email"], item["name"], item["confidence"]) for item in rendered["candidates"]],
                         [("rita@acme.test", "Rita Moreno", "confirmed")])

    def test_a_site_that_names_people_is_not_rendered(self):
        class Renderer:
            calls = 0

            def render(self, url):
                Renderer.calls += 1
                return None

        self.discover({"/": "<h3>Rita Moreno</h3><p>CEO</p>"}, renderer=Renderer())
        self.assertEqual(Renderer.calls, 0)


class FakeSession:
    def __init__(self, replies, log, *, probe=(550, b"5.1.1 <x>: Recipient address rejected: User unknown")):
        self.replies, self.log, self.probe = replies, log, probe

    def ehlo(self, name="localhost"):
        self.log.append(("ehlo", name))
        return 250, b"hello"

    def helo(self, name="localhost"):
        return 250, b"hello"

    def mail(self, sender, options=()):
        self.log.append(("mail", sender))
        return 250, b"ok"

    def rcpt(self, recip, options=()):
        self.log.append(("rcpt", recip))
        if recip.startswith("no-such-mailbox-"):
            return self.probe
        return self.replies.get(recip, (550, b"5.1.1 user unknown"))

    def quit(self):
        self.log.append(("quit",))

    def close(self):
        pass


def dns_client(mx="10 mx.acme.test."):
    def handler(request):
        answer = [{"type": 15, "data": mx}] if mx else []
        return httpx.Response(200, json={"Status": 0, "Answer": answer})

    return httpx.Client(transport=httpx.MockTransport(handler))


class SmtpVerifierTests(unittest.TestCase):
    def verify(self, replies, addresses, *, probe=None, resolve=lambda host: ["93.184.216.34"], mx="10 mx.acme.test."):
        log, connected = [], []

        def connect(ip, timeout):
            connected.append(ip)
            return FakeSession(replies, log, **({"probe": probe} if probe else {}))

        verifier = SmtpVerifier(dns_client(mx), connect=connect, resolve=resolve)
        return verifier.check("acme.test", addresses), log, connected

    def test_accepted_and_missing_mailboxes_are_told_apart(self):
        results, log, _ = self.verify({"sam@acme.test": (250, b"ok")}, ["sam@acme.test", "sam.lee@acme.test"])
        self.assertEqual(results, {"sam@acme.test": ACCEPTED, "sam.lee@acme.test": REJECTED})
        self.assertEqual(log[1], ("mail", ""), "the null sender")
        self.assertEqual(log[-1], ("quit",))
        self.assertFalse(any(step[0] not in {"ehlo", "mail", "rcpt", "quit"} for step in log), "no message is ever sent")

    def test_a_server_that_accepts_a_made_up_mailbox_proves_nothing(self):
        results, _, _ = self.verify({"sam@acme.test": (250, b"ok")}, ["sam@acme.test", "x@acme.test"], probe=(250, b"ok"))
        self.assertEqual(set(results.values()), {CATCH_ALL})

    def test_a_policy_refusal_is_not_a_missing_mailbox(self):
        blocked = (550, b"5.7.1 Service unavailable; client host blocked using Spamhaus")
        results, _, _ = self.verify({"sam@acme.test": blocked}, ["sam@acme.test"], probe=blocked)
        self.assertEqual(results, {"sam@acme.test": UNKNOWN})
        self.assertEqual(classify(550, b"5.7.606 Access denied, banned sending IP"), UNKNOWN)
        self.assertEqual(classify(550, b"5.1.1 The email account that you tried to reach does not exist"), REJECTED)
        self.assertEqual(classify(451, b"4.7.1 greylisted, try again"), UNKNOWN)
        # Found in review: wording about "recipient" or "rejected" is not enough.
        self.assertEqual(classify(550, b"5.7.1 <sam@acme.test>: Recipient address rejected: Access denied"), UNKNOWN)
        self.assertEqual(classify(550, b"5.1.8 <>: Sender address rejected: Domain not found"), UNKNOWN)
        self.assertEqual(classify(550, b"Sender verify failed, user unknown"), UNKNOWN)
        self.assertEqual(classify(550, b"Recipient address rejected: User unknown in virtual mailbox table"), REJECTED)

    def test_an_unclear_probe_makes_every_answer_unknown(self):
        results, _, _ = self.verify({"sam@acme.test": (250, b"ok")}, ["sam@acme.test"], probe=(451, b"4.7.1 greylisted"))
        self.assertEqual(results, {"sam@acme.test": UNKNOWN})

    def test_an_mx_on_a_private_network_is_never_contacted(self):
        results, _, connected = self.verify({}, ["sam@acme.test"], resolve=lambda host: ["93.184.216.34", "10.0.0.8"])
        self.assertEqual((results, connected), ({"sam@acme.test": UNKNOWN}, []))

    def test_a_domain_without_mx_is_not_contacted(self):
        results, _, connected = self.verify({}, ["sam@acme.test"], mx="")
        self.assertEqual((results, connected), ({"sam@acme.test": UNKNOWN}, []))

    def test_only_the_domains_own_addresses_are_asked_about_and_only_a_few(self):
        addresses = ["someone@other.test"] + [f"person{index}@acme.test" for index in range(10)]
        results, log, _ = self.verify({}, addresses)
        asked = [step[1] for step in log if step[0] == "rcpt" and not step[1].startswith("no-such-mailbox-")]
        self.assertEqual(asked, [f"person{index}@acme.test" for index in range(6)])
        self.assertNotIn("someone@other.test", results)

    def test_a_refused_connection_is_unknown(self):
        def connect(ip, timeout):
            raise ConnectionRefusedError("port 25 blocked")

        verifier = SmtpVerifier(dns_client(), connect=connect, resolve=lambda host: ["93.184.216.34"])
        self.assertEqual(verifier.check("acme.test", ["sam@acme.test"]), {"sam@acme.test": UNKNOWN})


NEWS = {
    "news.test": {
        "/robots.txt": "",
        "/acme-raises": "<h1>Acme raises a seed round</h1><p>Media contact: Jane Doe, jane@acme.test</p>",
        "/masked": "<p>Jane Doe, j***@acme.test</p>",
        "/no-name": "<p>Contact jane@acme.test</p>",
    },
    "blocked.test": {"/robots.txt": "User-agent: *\nDisallow: /\n", "/page": "<p>Jane Doe jane@acme.test</p>"},
    "rocketreach.co": {"/robots.txt": "", "/jane": "<p>Jane Doe jane@acme.test</p>"},
    "acme.test": {
        "/robots.txt": "",
        "/blog/launch": "<p>Written by Jane Doe (jane@acme.test)</p>",
        "/team/masked": "<p>Jane Doe, j***doe@acme.test</p><p>Sam Lee, s\u2026lee@acme.test</p>",
        "/joann": "<p>Joann Lee, ann@acme.test</p>",
    },
}
REDIRECTS = {
    "https://news.test/via-broker": "https://rocketreach.co/jane",
    "https://news.test/to-private": "https://blocked.test/page",
    "https://news.test/moved": "https://news.test/acme-raises",
}


def redirecting_transport(sites, redirects):
    plain, requested = site_transport(sites)

    def handler(request):
        target = redirects.get(str(request.url))
        if target:
            requested.append(str(request.url))
            return httpx.Response(302, headers={"location": target})
        return plain.handle_request(request)

    return httpx.MockTransport(handler), requested
TARGET = {"id": "t", "company": "Acme", "website": "https://acme.test"}


class EmailSearchTests(unittest.TestCase):
    def check(self, **proposal):
        transport, requested = redirecting_transport(NEWS, REDIRECTS)
        base = {"name": "Jane Doe", "role": "CTO", "email": "jane@acme.test", "source_url": "https://news.test/acme-raises"}
        with httpx.Client(transport=transport) as client:
            return check_person({**base, **proposal}, TARGET, fetcher=safe_fetcher(client)), requested

    def test_an_address_printed_with_the_name_on_another_site_is_kept_unverified(self):
        kept, _ = self.check()
        self.assertEqual(kept["reason"], "")
        self.assertEqual((kept["method"], kept["confidence"]), ("published_elsewhere", "unverified"))
        self.assertEqual(kept["evidence_url"], "https://news.test/acme-raises")

    def test_the_companys_own_page_counts_as_published(self):
        kept, _ = self.check(source_url="https://acme.test/blog/launch")
        self.assertEqual((kept["method"], kept["confidence"]), ("site_published", "confirmed"))

    def test_what_the_page_does_not_show_is_refused(self):
        cases = {
            "masked": ({"source_url": "https://news.test/masked"}, "does not print"),
            "no name": ({"source_url": "https://news.test/no-name"}, "does not name"),
            "made up": ({"email": "jane.doe@acme.test"}, "does not print"),
            "other domain": ({"email": "jane@gmail.com"}, "is not on acme.test"),
            "shared inbox": ({"email": "press@acme.test"}, "shared inbox"),
            "dead page": ({"source_url": "https://news.test/gone"}, "did not load"),
            "private": ({"source_url": "http://127.0.0.1/page"}, "not a public"),
        }
        for label, (proposal, reason) in cases.items():
            with self.subTest(label):
                refused, _ = self.check(**proposal)
                self.assertIn(reason, refused["reason"])
                self.assertNotIn("email", refused)

    def test_data_brokers_and_robots_txt_are_respected_before_any_page_is_read(self):
        refused, requested = self.check(source_url="https://rocketreach.co/jane")
        self.assertIn("people-search", refused["reason"])
        self.assertFalse(any("rocketreach" in url for url in requested))
        refused, requested = self.check(source_url="https://blocked.test/page")
        self.assertIn("robots.txt", refused["reason"])
        self.assertNotIn("https://blocked.test/page", requested)
        refused, _ = self.check(source_url="https://www.linkedin.com/in/jane")
        self.assertIn("people-search", refused["reason"])

    def test_every_redirect_hop_is_checked(self):
        refused, requested = self.check(source_url="https://news.test/via-broker")
        self.assertIn("redirects through", refused["reason"])
        self.assertFalse(any("rocketreach" in url for url in requested), "the blocked host is never contacted")
        refused, requested = self.check(source_url="https://news.test/to-private")
        self.assertIn("robots.txt", refused["reason"])
        self.assertNotIn("https://blocked.test/page", requested)
        kept, _ = self.check(source_url="https://news.test/moved")
        self.assertEqual((kept["reason"], kept["evidence_url"]), ("", "https://news.test/acme-raises"))

    def test_a_masked_address_never_yields_its_tail(self):
        for email_address, name in (("doe@acme.test", "Jane Doe"), ("lee@acme.test", "Sam Lee")):
            with self.subTest(email_address):
                refused, _ = self.check(email=email_address, name=name, source_url="https://acme.test/team/masked")
                self.assertIn("does not print", refused["reason"])

    def test_the_name_must_appear_as_whole_words(self):
        refused, _ = self.check(email="ann@acme.test", name="Ann Lee", source_url="https://acme.test/joann")
        self.assertIn("does not name Ann Lee", refused["reason"])
        kept, _ = self.check(email="ann@acme.test", name="Joann Lee", source_url="https://acme.test/joann")
        self.assertEqual(kept["reason"], "")


class DatabaseCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        _, platform_path = build_and_migrate(Path(self.tmp.name))
        self.platform_path = platform_path
        self.conn = connect_product(platform_path)
        ensure_product_schema(self.conn)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.conn.close()
        self.tmp.cleanup()

    SITE = {"acme.test": {"/robots.txt": "", "/": "<h3>Sam Lee</h3><p>CEO</p><p>hello@acme.test</p>"}}

    def find(self, target_id, sites=None, verifier=None):
        transport, _ = site_transport(sites or copy.deepcopy(self.SITE))
        with httpx.Client(transport=transport) as client:
            return find_contacts(self.conn, target_id, user_id=USER, fetcher=safe_fetcher(client), delay=0, verifier=verifier)


class StoredContactTests(DatabaseCase):

    def test_applying_a_guess_with_a_cc_keeps_it_unverified_and_says_why(self):
        target = create_target(self.conn, {"company": "Acme", "website": "https://acme.test"}, user_id=USER)
        found = self.find(target["id"])
        choice = choose_contact(found["candidates"])
        self.assertEqual(choice["basis"], "weak_guess")
        applied = apply_choice(self.conn, target["id"], choice, user_id=USER)
        self.assertEqual((applied["contact_email"], applied["contact_cc"]), ("sam@acme.test", "hello@acme.test"))
        self.assertEqual((applied["contact_name"], applied["contact_confidence"]), ("Sam Lee", "unverified"))
        self.assertIn("not confirmed", applied["contact_route"])
        self.assertIn("Cc hello@acme.test", applied["contact_route"])

        inbox = next(item for item in found["candidates"] if item["method"] == "site_generic")
        by_hand = apply_candidate(self.conn, target["id"], inbox["id"], user_id=USER)
        self.assertEqual((by_hand["contact_email"], by_hand["contact_cc"]), ("hello@acme.test", ""), "choosing by hand clears the Cc")

    def test_addresses_from_other_sites_survive_a_new_crawl_and_a_confirmed_one_replaces_them(self):
        target = create_target(self.conn, {"company": "Acme", "website": "https://acme.test"}, user_id=USER)
        sites = {**copy.deepcopy(self.SITE), "news.test": {"/robots.txt": "", "/a": "<p>Sam Lee, sam.lee@acme.test</p>"}}
        reply = json.dumps({"companies": [{"company": "Acme", "people": [
            {"name": "Sam Lee", "role": "CEO", "email": "sam.lee@acme.test", "source_url": "https://news.test/a"},
        ]}]})
        transport, _ = site_transport(sites)
        with httpx.Client(transport=transport) as client:
            found = search_emails(self.conn, user_id=USER, runner=lambda prompt: reply, fetcher=safe_fetcher(client), target_ids=[target["id"]])
        self.assertEqual(found["found"], 1)
        self.find(target["id"], sites)
        rows = {(item["email"], item["method"]) for item in list_candidates(self.conn, target["id"], user_id=USER)}
        self.assertIn(("sam.lee@acme.test", "published_elsewhere"), rows)
        self.assertNotIn(("sam.lee@acme.test", "pattern_guess"), rows, "the stronger row for the same address is kept")

        sites["acme.test"]["/"] = '<h3><a href="mailto:sam.lee@acme.test">Sam Lee</a></h3><p>CEO</p>'
        self.find(target["id"], sites)
        sam = [item for item in list_candidates(self.conn, target["id"], user_id=USER) if item["email"] == "sam.lee@acme.test"]
        self.assertEqual([(item["method"], item["confidence"]) for item in sam], [("site_published", "confirmed")])

    def test_the_latest_mail_server_answer_replaces_an_older_one(self):
        from opportunity_app.outreach_contacts import store_candidate

        target = create_target(self.conn, {"company": "Acme", "website": "https://acme.test"}, user_id=USER)
        found = candidate("sam@acme.test", name="Sam Lee", method="published_elsewhere", verification=ACCEPTED)
        with self.conn:
            store_candidate(self.conn, target["id"], USER, found, "t1")
            store_candidate(self.conn, target["id"], USER, {**found, "verification": REJECTED}, "t2")
        (row,) = list_candidates(self.conn, target["id"], user_id=USER)
        self.assertEqual(row["verification"], REJECTED)
        # A guess of the same address, refused later, updates the stronger row too.
        with self.conn:
            store_candidate(self.conn, target["id"], USER, {**found, "verification": ACCEPTED}, "t3")
            store_candidate(self.conn, target["id"], USER, {**found, "method": "pattern_guess", "verification": REJECTED}, "t4")
        (row,) = list_candidates(self.conn, target["id"], user_id=USER)
        self.assertEqual((row["method"], row["verification"]), ("published_elsewhere", REJECTED))
        self.assertIsNone(choose_contact([row]))

    def test_a_guessed_recipient_must_be_acknowledged_before_approval(self):
        target = create_target(self.conn, {
            "company": "Acme", "contact_email": "sam@acme.test", "contact_confidence": "unverified",
            "contact_cc": "hello@acme.test", "email_subject": "Internship", "email_body": "Hi Sam, I build robots.",
        }, user_id=USER)
        target = get_target(self.conn, target["id"], user_id=USER)
        with self.assertRaisesRegex(ValueError, "sam@acme.test is a guessed address, not confirmed; hello@acme.test is in Cc"):
            approve_draft(self.conn, target["id"], user_id=USER, fingerprint=target["draft_fingerprint"])
        approved = approve_draft(self.conn, target["id"], user_id=USER, fingerprint=target["draft_fingerprint"], acknowledge_warnings=True)
        self.assertEqual(approved["draft_status"], "approved")

    def test_a_confirmed_recipient_needs_no_acknowledgement(self):
        target = create_target(self.conn, {
            "company": "Acme", "contact_email": "jane@acme.test", "contact_confidence": "confirmed",
            "email_subject": "Internship", "email_body": "Hi Jane, I build robots.",
        }, user_id=USER)
        approved = approve_draft(self.conn, target["id"], user_id=USER, fingerprint=get_target(self.conn, target["id"], user_id=USER)["draft_fingerprint"])
        self.assertEqual(approved["draft_status"], "approved")

    def test_changing_the_cc_withdraws_approval_and_changes_the_fingerprint(self):
        target = create_target(self.conn, {
            "company": "Acme", "contact_email": "jane@acme.test", "contact_confidence": "confirmed",
            "email_subject": "Internship", "email_body": "Hi Jane, I build robots.",
        }, user_id=USER)
        before = get_target(self.conn, target["id"], user_id=USER)
        approve_draft(self.conn, target["id"], user_id=USER, fingerprint=before["draft_fingerprint"])
        after = update_target(self.conn, target["id"], {"contact_cc": "hello@acme.test"}, user_id=USER)
        self.assertEqual(after["draft_status"], "generated")
        self.assertNotEqual(after["draft_fingerprint"], before["draft_fingerprint"])
        with self.assertRaises(DraftChangedError):
            approve_draft(self.conn, target["id"], user_id=USER, fingerprint=before["draft_fingerprint"])

    def test_a_draft_without_a_cc_keeps_the_fingerprint_it_had_before_cc_existed(self):
        target = create_target(self.conn, {
            "company": "Acme", "contact_email": "jane@acme.test", "email_subject": "S", "email_body": "B",
        }, user_id=USER)
        item = get_target(self.conn, target["id"], user_id=USER)
        # The formula as it was before Cc existed, written out rather than called.
        fields = ["initial", "S", "B", "jane@acme.test", "[]", item["draft_generated_by"]]
        old_formula = hashlib.sha256(json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
        self.assertEqual(item["draft_fingerprint"], old_formula)
        self.assertNotEqual(_draft_fingerprint(*fields[:6], "hello@acme.test"), old_formula)

    def test_a_cc_must_look_like_an_address(self):
        target = create_target(self.conn, {"company": "Acme"}, user_id=USER)
        with self.assertRaisesRegex(ValueError, "Cc"):
            update_target(self.conn, target["id"], {"contact_cc": "not an address"}, user_id=USER)

    def test_the_gmail_draft_carries_the_cc(self):
        raw = _mime("student@example.edu", "sam@acme.test", "Hi", "Body", None, cc="hello@acme.test")
        message = email.message_from_bytes(base64.urlsafe_b64decode(raw))
        self.assertEqual((message["To"], message["Cc"]), ("sam@acme.test", "hello@acme.test"))
        plain = email.message_from_bytes(base64.urlsafe_b64decode(_mime("student@example.edu", "sam@acme.test", "Hi", "Body", None)))
        self.assertIsNone(plain["Cc"])


class RecontactTests(DatabaseCase):
    class Verifier:
        def check(self, domain, addresses):
            return {address: (ACCEPTED if address == "sam@acme.test" else REJECTED) for address in addresses}

    def target(self, **fields):
        return create_target(self.conn, {"company": fields.pop("company", "Acme"), "website": "https://acme.test", **fields}, user_id=USER)

    def recontact(self, **kwargs):
        transport, _ = site_transport(copy.deepcopy(self.SITE))
        with httpx.Client(transport=transport) as client:
            return recontact_targets(self.conn, user_id=USER, fetcher=safe_fetcher(client), verifier=self.Verifier(), contact_delay=0, **kwargs)

    def test_a_shared_inbox_target_is_upgraded_only_when_asked(self):
        target = self.target(contact_email="hello@acme.test", contact_confidence="confirmed")
        report = self.recontact()
        (result,) = report["results"]
        self.assertEqual((result["to"], result["cc"], result["basis"], result["applied"]),
                         ("sam@acme.test", "hello@acme.test", "strong_guess", False))
        self.assertEqual(get_target(self.conn, target["id"], user_id=USER)["contact_email"], "hello@acme.test")

        self.recontact(apply=True)
        upgraded = get_target(self.conn, target["id"], user_id=USER)
        self.assertEqual((upgraded["contact_email"], upgraded["contact_cc"], upgraded["contact_confidence"]),
                         ("sam@acme.test", "hello@acme.test", "unverified"))
        self.assertIn("their mail server accepted it", upgraded["contact_route"])

    def test_a_contact_changed_while_the_search_ran_is_left_alone(self):
        target = self.target(contact_email="hello@acme.test", contact_confidence="confirmed")
        conn = self.conn

        class Verifier:
            def check(self, domain, addresses):
                # The student picks an address while the pass is still running.
                update_target(conn, target["id"], {"contact_email": "rita@acme.test"}, user_id=USER)
                return {address: ACCEPTED for address in addresses}

        self.Verifier = Verifier
        report = self.recontact(apply=True)
        (result,) = report["results"]
        self.assertEqual((result["applied"], result["skipped"]), (False, "changed while the search ran"))
        self.assertEqual(get_target(self.conn, target["id"], user_id=USER)["contact_email"], "rita@acme.test")

    def test_committed_or_personal_contacts_are_left_alone(self):
        self.target(company="Personal", contact_email="rita@acme.test")
        self.target(company="Sent", contact_email="hello@acme.test", status="sent", sent_at="2026-09-01")
        approved = self.target(company="Approved", contact_email="hello@acme.test", contact_confidence="confirmed",
                               email_subject="S", email_body="B")
        approve_draft(self.conn, approved["id"], user_id=USER, fingerprint=get_target(self.conn, approved["id"], user_id=USER)["draft_fingerprint"])
        report = self.recontact(apply=True)
        self.assertEqual(report["checked"], 0)


    def test_applying_a_report_changes_only_what_the_student_saw(self):
        shown = self.target(company="Shown", contact_email="hello@acme.test", contact_confidence="confirmed")
        moved = self.target(company="Moved", contact_email="hello@acme.test", contact_confidence="confirmed")
        unticked = self.target(company="Unticked", contact_email="hello@acme.test", contact_confidence="confirmed")
        self.assertEqual(len(eligible_targets(self.conn, user_id=USER)), 3)
        report = self.recontact()
        self.assertEqual({result["to"] for result in report["results"]}, {"sam@acme.test"})

        # The student picks someone else for one of them before applying.
        update_target(self.conn, moved["id"], {"contact_email": "rita@acme.test"}, user_id=USER)
        applied = apply_recontact(
            self.conn, {shown["id"]: "SAM@acme.test", moved["id"]: "sam@acme.test", "gone": "sam@acme.test"}, user_id=USER,
        )
        outcome = {result["target_id"]: result for result in applied["results"]}
        self.assertTrue(outcome[shown["id"]]["applied"])
        self.assertEqual(outcome[moved["id"]]["skipped"], "changed while the search ran")
        self.assertEqual(outcome["gone"]["skipped"], "no longer tracked")
        self.assertEqual(applied["upgraded"], 1)
        self.assertEqual(get_target(self.conn, shown["id"], user_id=USER)["contact_email"], "sam@acme.test")
        self.assertEqual(get_target(self.conn, moved["id"], user_id=USER)["contact_email"], "rita@acme.test")
        self.assertEqual(get_target(self.conn, unticked["id"], user_id=USER)["contact_email"], "hello@acme.test")

    def test_an_apply_never_uses_an_address_the_report_did_not_show(self):
        target = self.target(contact_email="hello@acme.test", contact_confidence="confirmed")
        self.recontact()
        applied = apply_recontact(self.conn, {target["id"]: "someone.else@acme.test"}, user_id=USER)
        (result,) = applied["results"]
        self.assertEqual((result["applied"], result["skipped"]), (False, "the suggested contact changed since the report"))
        self.assertEqual(get_target(self.conn, target["id"], user_id=USER)["contact_email"], "hello@acme.test")

    def test_the_web_app_reports_first_and_applies_only_the_ticked_targets(self):
        from fastapi.testclient import TestClient
        from opportunity_app.api import STATIC_DIR, create_app

        target = self.target(contact_email="hello@acme.test", contact_confidence="confirmed")
        transport, _ = site_transport(copy.deepcopy(self.SITE))
        manager = RecontactManager(
            self.platform_path, email_search=False,
            client_factory=lambda: safe_fetcher(httpx.Client(transport=transport)),
            verifier_factory=lambda: contextlib.nullcontext(self.Verifier()), contact_delay=0,
        )
        root = Path(self.tmp.name)
        app = create_app(
            db_path=self.platform_path, access_token="recontact-owner", static_dir=STATIC_DIR,
            resume_storage=root / "resumes", capture_storage=root / "captures", interview_storage=root / "interviews",
            outreach_recontact_manager=manager,
        )
        headers = {"Authorization": "Bearer recontact-owner"}
        with TestClient(app) as client:
            listing = client.get("/api/v1/outreach", headers=headers).json()
            self.assertEqual((listing["recontact"]["available"], listing["recontact"]["eligible"]), (True, 1))
            started = client.post("/api/v1/outreach/recontact", headers=headers)
            self.assertEqual(started.status_code, 202, started.text)
            manager.wait(30)
            report = client.get("/api/v1/outreach/recontact", headers=headers).json()["active"]
            self.assertEqual((report["state"], report["mode"]), ("succeeded", "report"), report)
            (result,) = report["result"]["results"]
            self.assertEqual(result["to"], "sam@acme.test")
            self.assertEqual(get_target(self.conn, target["id"], user_id=USER)["contact_email"], "hello@acme.test")

            applied = client.post("/api/v1/outreach/recontact/apply", headers=headers,
                                  json={"choices": [{"target_id": target["id"], "to": result["to"]}]})
            self.assertEqual(applied.status_code, 202, applied.text)
            manager.wait(30)
            done = client.get("/api/v1/outreach/recontact", headers=headers).json()
            self.assertEqual((done["active"]["mode"], done["active"]["result"]["upgraded"]), ("apply", 1), done)
            self.assertEqual(done["eligible"], 0)
            self.assertEqual(client.post("/api/v1/outreach/recontact/apply", headers=headers, json={"choices": []}).status_code, 422)


class DeepSearchTests(unittest.TestCase):
    EMAIL_PROMPT = "published work email"

    def test_a_new_company_gets_an_address_from_another_site_before_it_is_drafted(self):
        sites = {
            "acme.test": {
                "/robots.txt": "", "/": '<a href="/team">Team</a><p>hello@acme.test</p>',
                "/team": "<h3>Jane Doe</h3><p>Co-Founder and CTO</p>", "/about": "<p>About Acme</p>",
            },
            "news.test": {"/robots.txt": "", "/raise": "<p>Media contact: Jane Doe, jane@acme.test</p>"},
        }
        email_reply = json.dumps({"companies": [{"company": "Acme", "people": [
            {"name": "Jane Doe", "role": "CTO", "email": "jane@acme.test", "source_url": "https://news.test/raise"},
        ]}]})
        search = only_for(proposals(company("Acme", "https://acme.test")))
        prompts = []

        def runner(prompt):
            prompts.append(prompt)
            return email_reply if self.EMAIL_PROMPT in prompt else search(prompt)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, platform_path = build_and_migrate(root)
            conn = connect_product(platform_path)
            try:
                ensure_product_schema(conn)
                transport, _ = site_transport(sites)
                with httpx.Client(transport=transport) as client:
                    result = run_discovery(conn, user_id=USER, runner=runner, fetcher=safe_fetcher(client),
                                           report_dir=root / "reports", contact_delay=0, email_runner=runner)
                (item,) = result["follow_through"]
                self.assertEqual((item["contact"], item["cc"], item["contact_basis"]), ("jane@acme.test", "hello@acme.test", "strong_guess"))
                target = get_target(conn, item["target_id"], user_id=USER)
                self.assertEqual((target["contact_email"], target["contact_confidence"]), ("jane@acme.test", "unverified"))
                self.assertIn("news.test", target["contact_route"])
                self.assertEqual(sum(self.EMAIL_PROMPT in prompt for prompt in prompts), 1)
            finally:
                conn.close()

    def test_without_the_email_search_a_run_behaves_as_before(self):
        sites = {"acme.test": {"/robots.txt": "", "/": '<p>hello@acme.test</p>', "/about": "<p>About Acme</p>"}}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, platform_path = build_and_migrate(root)
            conn = connect_product(platform_path)
            try:
                ensure_product_schema(conn)
                transport, _ = site_transport(sites)
                with httpx.Client(transport=transport) as client:
                    result = run_discovery(conn, user_id=USER, runner=only_for(proposals(company("Acme", "https://acme.test"))),
                                           fetcher=safe_fetcher(client), report_dir=root / "reports", contact_delay=0)
                (item,) = result["follow_through"]
                self.assertEqual((item["contact"], item["cc"], item["contact_basis"]), ("hello@acme.test", None, "shared_inbox"))
            finally:
                conn.close()


class MigrationTests(unittest.TestCase):
    def test_people_stored_as_published_addresses_become_site_people(self):
        migrations = sorted(schema.MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql"))
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / "migrations"
            staged.mkdir()
            for migration in migrations:
                if migration.name < "0022":
                    shutil.copy(migration, staged / migration.name)
            conn = connect_product(Path(tmp) / "platform.db")
            try:
                with mock.patch.object(schema, "MIGRATIONS_DIR", staged):
                    ensure_product_schema(conn)
                    now = "2026-09-21T00:00:00+00:00"
                    conn.execute("INSERT INTO users(id, display_name, created_at, updated_at) VALUES('u', '', ?, ?)", (now, now))
                    conn.execute("INSERT INTO outreach_targets(id, user_id, company, created_at, updated_at) VALUES('t', 'u', 'Acme', ?, ?)", (now, now))
                    rows = [
                        ("a", "Rita Moreno", "", "site_published", "unknown"),
                        ("b", "Jane Doe", "jane@acme.test", "site_published", "confirmed"),
                        ("c", "Sam Lee", "sam@acme.test", "pattern_guess", "unverified"),
                    ]
                    conn.executemany(
                        "INSERT INTO outreach_contact_candidates(id, target_id, user_id, name, email, method, confidence, created_at) "
                        "VALUES(?, 't', 'u', ?, ?, ?, ?, ?)", [(*row, now) for row in rows],
                    )
                    conn.commit()
                    for migration in migrations:
                        if migration.name >= "0022":
                            shutil.copy(migration, staged / migration.name)
                    ensure_product_schema(conn)
                methods = dict(conn.execute("SELECT id, method FROM outreach_contact_candidates").fetchall())
                self.assertEqual(methods, {"a": "site_person", "b": "site_published", "c": "pattern_guess"})
                conn.execute(
                    "INSERT INTO outreach_contact_candidates(id, target_id, user_id, email, method, confidence, verification, created_at) "
                    "VALUES('d', 't', 'u', 'x@acme.test', 'published_elsewhere', 'unverified', 'smtp_accepted', 'now')"
                )
                for column, value in (("method", "made_up"), ("verification", "probably")):
                    with self.subTest(column), self.assertRaises(sqlite3.IntegrityError):
                        conn.execute(f"UPDATE outreach_contact_candidates SET {column}=? WHERE id='d'", (value,))
                self.assertEqual(conn.execute("SELECT contact_cc FROM outreach_targets").fetchone()[0], "")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
