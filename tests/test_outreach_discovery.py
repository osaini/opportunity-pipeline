"""Contact finding from first-party sites, and the deep search that feeds outreach."""

import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import httpx
from fastapi.testclient import TestClient

from opportunity_app import STATIC_DIR
from opportunity_app.api import create_app
from opportunity_app.outreach import create_target, get_target, list_targets
from opportunity_app.outreach_contacts import SafeFetcher, apply_candidate, crawl_site, discover_candidates, find_contacts
from opportunity_app.outreach_discovery import DiscoveryBusy, DiscoveryManager, _RunLock, _scope_brief, run_discovery, scope_definitions, validate_proposals
from opportunity_app.schema import connect_product, ensure_product_schema

from helpers_platform import build_and_migrate, use_profile_regions

USER = "local-user"

ACME = {
    "acme.com": {
        "/robots.txt": "User-agent: *\nDisallow: /private\n",
        "/": '<nav><a href="/team">Our team</a><a href="/private/people">Staff</a><a href="https://evil.example/team">x</a></nav>'
             '<p>Say <a href="mailto:hello@acme.com">hello@acme.com</a></p>',
        "/team": '<div><h3><a href="mailto:jane.doe@acme.com">Jane Doe</a></h3><p>Co-Founder &amp; CTO</p></div><div><h3>Sam Lee</h3><p>Head of Talent</p></div>'
                 "<p>Press: jane.doe@acme.com. Personal: someone@gmail.com. logo@2x.png</p>",
        "/private/people": "<h3>Secret Person</h3><p>CEO</p><p>secret.person@acme.com</p>",
    },
}


def site_transport(sites, *, mx=True, dead=()):
    requested = []

    def handler(request):
        requested.append(str(request.url))
        host = request.url.host.removeprefix("www.")
        if host == "cloudflare-dns.com":
            answer = [{"type": 15, "data": "10 mx.example."}] if mx else []
            return httpx.Response(200, json={"Status": 0, "Answer": answer})
        if str(request.url) in dead:
            return httpx.Response(404)
        pages = sites.get(host)
        if pages is None:
            return httpx.Response(404)
        body = pages.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        kind = "text/plain" if request.url.path.endswith(".txt") else "text/html"
        return httpx.Response(200, text=body, headers={"content-type": kind})

    return httpx.MockTransport(handler), requested


def safe_fetcher(client):
    return SafeFetcher(client, resolve=lambda _host: ["93.184.216.34"])


class ContactFindingTests(unittest.TestCase):
    def test_published_addresses_are_confirmed_and_guesses_follow_the_sites_pattern(self):
        transport, requested = site_transport(ACME)
        with httpx.Client(transport=transport) as client:
            result = discover_candidates("https://www.acme.com", fetcher=safe_fetcher(client), delay=0)
        by_email = {candidate["email"]: candidate for candidate in result["candidates"]}
        self.assertEqual(by_email["jane.doe@acme.com"]["confidence"], "confirmed")
        self.assertEqual(by_email["jane.doe@acme.com"]["name"], "Jane Doe")
        self.assertEqual(by_email["hello@acme.com"]["method"], "site_generic")
        # Jane's address shows the first.last pattern, so only that guess is made for Sam.
        self.assertEqual(by_email["sam.lee@acme.com"]["method"], "pattern_guess")
        self.assertEqual(by_email["sam.lee@acme.com"]["confidence"], "unverified")
        self.assertNotIn("sam@acme.com", by_email)
        self.assertNotIn("someone@gmail.com", by_email, "off-domain addresses are ignored")
        self.assertFalse(any("secret" in email for email in by_email), "robots.txt disallowed that page")
        self.assertFalse(any("evil.example" in url for url in requested), "the crawl stays on the company site")

    def test_no_guesses_when_the_domain_does_not_accept_mail(self):
        transport, _ = site_transport(ACME, mx=False)
        with httpx.Client(transport=transport) as client:
            result = discover_candidates("https://acme.com", fetcher=safe_fetcher(client), delay=0)
        self.assertFalse(result["mail_domain_ok"])
        self.assertFalse(any(candidate["method"] == "pattern_guess" for candidate in result["candidates"]))

    def test_applying_a_guess_labels_the_contact_unverified(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, platform_path = build_and_migrate(Path(tmp))
            conn = connect_product(platform_path)
            try:
                ensure_product_schema(conn)
                target = create_target(conn, {"company": "Acme", "website": "https://acme.com"}, user_id=USER)
                transport, _ = site_transport(ACME)
                with httpx.Client(transport=transport) as client:
                    found = find_contacts(conn, target["id"], user_id=USER, fetcher=safe_fetcher(client), delay=0)
                guess = next(candidate for candidate in found["candidates"] if candidate["method"] == "pattern_guess")
                applied = apply_candidate(conn, target["id"], guess["id"], user_id=USER)
                self.assertEqual(applied["contact_confidence"], "unverified")
                self.assertIn("not confirmed", applied["contact_route"])
                self.assertIn("https://acme.com/team", applied["source_urls"])
                self.assertTrue(get_target(conn, target["id"], user_id=USER)["mail_domain_ok"])
            finally:
                conn.close()

    def test_a_shared_inbox_never_inherits_a_named_persons_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp:
            _, platform_path = build_and_migrate(Path(tmp))
            conn = connect_product(platform_path)
            try:
                ensure_product_schema(conn)
                target = create_target(conn, {
                    "company": "Acme", "website": "https://acme.com", "contact_name": "Rita Moreno", "contact_role": "CEO",
                }, user_id=USER)
                transport, _ = site_transport(ACME)
                with httpx.Client(transport=transport) as client:
                    found = find_contacts(conn, target["id"], user_id=USER, fetcher=safe_fetcher(client), delay=0)
                inbox = next(candidate for candidate in found["candidates"] if candidate["method"] == "site_generic")
                applied = apply_candidate(conn, target["id"], inbox["id"], user_id=USER)
                self.assertEqual((applied["contact_email"], applied["contact_confidence"]), ("hello@acme.com", "confirmed"))
                self.assertEqual((applied["contact_name"], applied["contact_role"]), ("", ""))
                self.assertIn("Rita Moreno (CEO) is named on the site", applied["contact_route"])
                events = [event["event_type"] for event in get_target(conn, target["id"], user_id=USER, include_events=True)["events"]]
                self.assertIn("contact_applied", events)
            finally:
                conn.close()

    def test_parent_domain_addresses_are_rejected_but_exact_and_child_domains_are_kept(self):
        sites = {
            "acme.webflow.io": {"/robots.txt": "", "/": "support@webflow.io"},
            "acme.someplatform.test": {"/robots.txt": "", "/": "help@someplatform.test"},
            "acme.test": {"/robots.txt": "", "/": "jobs@careers.acme.test"},
        }
        transport, _ = site_transport(sites, mx=False)
        with httpx.Client(transport=transport) as client:
            for website in ("https://acme.webflow.io", "https://acme.someplatform.test"):
                result = discover_candidates(website, fetcher=safe_fetcher(client), delay=0)
                self.assertEqual(result["candidates"], [])
            child = discover_candidates("https://acme.test", fetcher=safe_fetcher(client), delay=0)
        self.assertEqual(child["candidates"][0]["email"], "jobs@careers.acme.test")

        exact_sites = {"acme.test": {"/robots.txt": "", "/": "hello@acme.test"}}
        transport, _ = site_transport(exact_sites, mx=False)
        with httpx.Client(transport=transport) as client:
            exact = discover_candidates("https://www.acme.test", fetcher=safe_fetcher(client), delay=0)
        self.assertEqual(exact["candidates"][0]["email"], "hello@acme.test")

    def test_name_patterns_never_confirm_ownership_without_explicit_association(self):
        sites = {"acme.test": {
            "/robots.txt": "", "/": '<a href="/team">Team</a><a href="/contact">Contact</a>',
            "/team": "<p>Jane Doe</p><p>CTO</p><p>Sam Lee</p><p>CEO</p>",
            "/contact": "<footer>jane.doe@acme.test</footer>",
        }}
        transport, _ = site_transport(sites)
        with httpx.Client(transport=transport) as client:
            result = discover_candidates("https://acme.test", fetcher=safe_fetcher(client), delay=0)
        published = next(item for item in result["candidates"] if item["method"] == "site_published" and item["email"] == "jane.doe@acme.test")
        guessed = next(item for item in result["candidates"] if item["method"] == "pattern_guess" and item["email"] == "jane.doe@acme.test")
        self.assertEqual(published["name"], "")
        self.assertEqual((guessed["name"], guessed["confidence"]), ("Jane Doe", "unverified"))

        sites["acme.test"]["/team"] = '<p><a href="mailto:jane.doe@acme.test">Jane Doe</a></p><p>CTO</p>'
        transport, _ = site_transport(sites)
        with httpx.Client(transport=transport) as client:
            explicit = discover_candidates("https://acme.test", fetcher=safe_fetcher(client), delay=0)
        jane = next(item for item in explicit["candidates"] if item["email"] == "jane.doe@acme.test" and item["confidence"] == "confirmed")
        self.assertEqual(jane["name"], "Jane Doe")

        sites["acme.test"]["/team"] = (
            '<script type="application/ld+json">'
            '{"@type":"Person","name":"Jane Doe","jobTitle":"CTO","email":"jane.doe@acme.test"}'
            "</script>"
        )
        transport, _ = site_transport(sites)
        with httpx.Client(transport=transport) as client:
            json_ld = discover_candidates("https://acme.test", fetcher=safe_fetcher(client), delay=0)
        jane = next(item for item in json_ld["candidates"] if item["email"] == "jane.doe@acme.test" and item["confidence"] == "confirmed")
        self.assertEqual(jane["name"], "Jane Doe")

    def test_hidden_comment_and_script_addresses_are_not_published(self):
        sites = {"visible.test": {"/robots.txt": "", "/": "<!-- hidden@visible.test --><script>script@visible.test</script><p>shown@visible.test</p>"}}
        transport, _ = site_transport(sites, mx=False)
        with httpx.Client(transport=transport) as client:
            result = discover_candidates("https://visible.test", fetcher=safe_fetcher(client), delay=0)
        emails = {item["email"] for item in result["candidates"]}
        self.assertEqual(emails, {"shown@visible.test"})

    def test_non_html_pages_are_not_parsed_for_addresses(self):
        requested = []

        def handler(request):
            requested.append(str(request.url))
            if request.url.host == "cloudflare-dns.com":
                return httpx.Response(200, json={"Status": 0, "Answer": []})
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="", headers={"content-type": "text/plain"})
            if request.url.path == "/":
                return httpx.Response(
                    200,
                    text='<a href="/team">Team</a><p>visible@acme.test</p>',
                    headers={"content-type": "text/html; charset=utf-8"},
                )
            if request.url.path == "/team":
                return httpx.Response(
                    200, content=b"%PDF-1.7 hidden@acme.test", headers={"content-type": "application/pdf"},
                )
            return httpx.Response(404)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = discover_candidates("https://acme.test", fetcher=safe_fetcher(client), delay=0)
        self.assertIn("https://acme.test/team", requested)
        self.assertEqual({item["email"] for item in result["candidates"]}, {"visible@acme.test"})

    def test_safe_fetcher_blocks_private_redirects_dns_failures_and_large_streams(self):
        requested = []

        class CountingStream(httpx.SyncByteStream):
            def __init__(self):
                self.yielded = 0

            def __iter__(self):
                for _ in range(48):
                    self.yielded += 65536
                    yield b"x" * 65536

        stream = CountingStream()

        def handler(request):
            requested.append(str(request.url))
            if request.url.path == "/redirect-private":
                return httpx.Response(302, headers={"location": "http://127.0.0.1:9000/secret"})
            if request.url.path == "/leave":
                return httpx.Response(302, headers={"location": "https://parked.example/"})
            if request.url.path == "/www":
                return httpx.Response(302, headers={"location": "https://www.public.test/final"})
            if request.url.path == "/large":
                return httpx.Response(200, stream=stream)
            return httpx.Response(200, text="ok")

        with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            fetcher = safe_fetcher(client)
            blocked = fetcher.fetch("https://public.test/redirect-private", same_host_only=True)
            left = fetcher.fetch("https://public.test/leave", same_host_only=True)
            www = fetcher.fetch("https://public.test/www", same_host_only=True)
            large = fetcher.fetch("https://public.test/large", same_host_only=True)
        self.assertEqual(blocked.error, "private")
        self.assertFalse(any("127.0.0.1" in url for url in requested))
        self.assertEqual(left.error, "left site")
        self.assertFalse(any("parked.example" in url for url in requested))
        self.assertIsNone(www.error)
        self.assertIn("https://www.public.test/final", requested)
        self.assertEqual((large.error, large.text), ("too_large", ""))
        self.assertLessEqual(stream.yielded, 2 * 1024 * 1024 + 65536)

        failing = SafeFetcher(httpx.Client(transport=httpx.MockTransport(handler)), resolve=lambda _host: (_ for _ in ()).throw(OSError("dns")))
        try:
            self.assertEqual(failing.fetch("https://public.test/", same_host_only=True).error, "dns")
        finally:
            failing.client.close()

    def test_private_deep_search_urls_are_rejected_without_requests(self):
        requested = []

        def handler(request):
            requested.append(str(request.url))
            return httpx.Response(200, text="should not load")

        with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False) as client:
            accepted, rejected, _ = validate_proposals(
                proposals(company(
                    "Private", "http://127.0.0.1:8799",
                    source_urls=["http://192.168.1.1/admin"],
                )),
                scopes=["local-accelerators"], existing_names=set(), existing_domains=set(),
                fetcher=safe_fetcher(client), limit=25, today=date(2026, 9, 17),
            )
        self.assertEqual(accepted, [])
        self.assertIn("private or local", rejected[0]["reason"])
        self.assertEqual(requested, [])

    def test_crawl_preserves_http_scheme_and_the_supplied_path(self):
        sites = {"legacy.test": {"/robots.txt": "", "/": "<p>home</p>", "/team": "<p>team</p>"}}
        transport, requested = site_transport(sites, mx=False)
        with httpx.Client(transport=transport) as client:
            crawl_site("http://legacy.test/team", fetcher=safe_fetcher(client), delay=0)
        for expected in ("http://legacy.test/robots.txt", "http://legacy.test/", "http://legacy.test/team"):
            self.assertIn(expected, requested)


def proposals(*companies):
    return "Here you go:\n" + json.dumps({"companies": list(companies)})


# The second kind of prompt a deep search sends its runner: where a new company
# it imported is based (outreach_locate.py).
LOCATE_PROMPT = "finding where each of these companies is based"


def scope_of(prompt):
    """The one scope a deep search prompt asks about."""
    return prompt.split("## What to look for\n- ", 1)[1].split(" (", 1)[0]


def only_for(reply, scope="local-accelerators", locations=None):
    """A runner that answers one scope's search with reply and finds nothing for the others."""
    return lambda prompt: (locations or proposals()) if LOCATE_PROMPT in prompt else (
        reply if scope_of(prompt) == scope else proposals()
    )


def company(name, website, **overrides):
    return {
        "company": name,
        "website": website,
        "scope": "local-accelerators",
        "summary": f"{name} builds robots",
        "fit_rationale": "Matches the student's robotics interest",
        "activity_signal": "Raised a seed round (June 2026)",
        "priority": "P1",
        "source_urls": [f"{website}/about"],
        **overrides,
    }


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        use_profile_regions(self)
        self.root = Path(self.tempdir.name)
        _, platform_path = build_and_migrate(self.root)
        self.conn = connect_product(platform_path)
        ensure_product_schema(self.conn)
        create_target(self.conn, {"company": "Existing Co", "website": "https://existing.com"}, user_id=USER)
        self.sites = {
            "acme.com": {**ACME["acme.com"], "/about": "<p>About Acme</p>"},
            "bovi.com": {"/": "<p>Bovi</p>", "/about": "<p>About</p>", "/robots.txt": ""},
            "ghost.com": {"/": "<p>Ghost</p>"},
            "existing.com": {"/": "<p>Existing</p>", "/about": "<p>About</p>"},
        }
        self.prompts = []

    def tearDown(self):
        self.conn.close()
        self.tempdir.cleanup()

    def run_with(self, reply, **kwargs):
        """Answer the local accelerators search with reply; the other scopes find nothing."""
        answer = kwargs.pop("runner", None) or only_for(reply)

        def runner(prompt):
            self.prompts.append(prompt)
            return answer(prompt)

        transport, _ = site_transport(self.sites)
        today = kwargs.pop("today", date(2026, 9, 17))
        with httpx.Client(transport=transport) as client:
            return run_discovery(
                self.conn, user_id=USER, runner=runner, fetcher=safe_fetcher(client), report_dir=self.root / "reports",
                contact_delay=0, today=today, **kwargs,
            )

    def test_only_verified_new_companies_are_imported(self):
        result = self.run_with(proposals(
            company("Acme", "https://acme.com", contact_name="Jane Doe", contact_role="CTO", contact_source_url="https://acme.com/team"),
            company("Ghost", "https://ghost.com"),  # its only source URL 404s
            company("Existing Company", "https://www.existing.com"),
            company("Nosource", "https://bovi.com", source_urls=[]),
            company("Wrong scope", "https://bovi.com", scope="biotech"),
        ))
        self.assertEqual(result["imported"], 1)
        reasons = {item["company"]: item["reason"] for item in result["rejected"]}
        self.assertEqual(reasons["Ghost"], "none of its source URLs loaded")
        self.assertEqual(reasons["Existing Company"], "already tracked")
        self.assertIn("source URLs", reasons["Nosource"])
        self.assertIn("not requested", reasons["Wrong scope"])

        acme = next(item for item in list_targets(self.conn, user_id=USER) if item["company"] == "Acme")
        from opportunity_app.outreach import outreach_summary

        summary = outreach_summary(list_targets(self.conn, user_id=USER))
        self.assertEqual(summary["new_from_search"], 1)
        self.assertEqual(summary["unverified_contacts"], 0, "a company with no contact has nothing to verify")
        self.assertEqual((acme["origin"], acme["channel"], acme["researched_at"]), ("discovery", "Local accelerators", "2026-09-17"))
        self.assertEqual(acme["contact_email"], "jane.doe@acme.com", "a site-published address is applied")
        self.assertEqual(acme["contact_confidence"], "confirmed")
        self.assertEqual(acme["draft_status"], "none", "no draft without a draft provider")
        self.assertIn("Existing Co", self.prompts[0], "the model is told what is already tracked")

        report = json.loads(Path(result["report_path"]).read_text(encoding="utf-8"))
        self.assertEqual(len(report["items"]), 1)
        run = self.conn.execute("SELECT status, imported, proposed FROM outreach_discovery_runs").fetchone()
        self.assertEqual(tuple(run), ("succeeded", 1, 5))

    def test_a_legal_suffix_does_not_hide_a_company_its_own_site_names(self):
        """A source that writes the plain name still mentions "Name, Inc."

        The comma is part of the suffix, not the name. Stripping the suffix but
        leaving the comma searched every page for "acme robotics," and rejected
        real companies with "no source mentions the company".
        """
        from opportunity_app.outreach_discovery import _mentions_company

        page = "About Acme Robotics -- we build robots."
        for name in (
            "Acme Robotics",
            "Acme Robotics Inc.",
            "Acme Robotics, Inc.",
            "Acme Robotics, LLC",
            "Acme Robotics, L.L.C.",
            "Acme Robotics, Incorporated",
            "Acme Robotics Ltd",
            "Acme Robotics Company",
        ):
            with self.subTest(name=name):
                self.assertTrue(_mentions_company(page, name, "acme.example"))

    def test_a_different_company_is_still_not_a_mention(self):
        from opportunity_app.outreach_discovery import _mentions_company

        page = "About Acme Robotics -- we build robots."
        self.assertFalse(_mentions_company(page, "Bovi Robotics, Inc.", "bovi.com"))
        self.assertTrue(
            _mentions_company("Careers at bovi.com", "Bovi Robotics, Inc.", "bovi.com"),
            "the domain is still an independent way to match",
        )

    def test_blank_sources_are_rejected_and_deadlines_stay_unverified_notes(self):
        self.sites["blank.test"] = {"/": "<p>Blank</p>", "/about": "", "/robots.txt": ""}
        blank = self.run_with(proposals(company("Blank", "https://blank.test")), dry_run=True)
        self.assertEqual(blank["rejected"][0]["reason"], "no source mentions the company")

        accepted = self.run_with(proposals(company("Acme", "https://acme.com", deadline_label="Apply Friday")))
        target = next(item for item in list_targets(self.conn, user_id=USER) if item["company"] == "Acme")
        self.assertEqual(target["research_confidence"], "unverified")
        self.assertEqual(target["deadline_label"], "")
        self.assertIn("Deep search reported a deadline (unverified): Apply Friday", target["notes"])
        self.assertEqual(accepted["imported"], 1)

    def test_the_companys_location_is_asked_for_and_kept(self):
        self.sites["bovi.com"]["/about"] = "<p>About Bovi</p>"
        self.run_with(proposals(
            company("Acme", "https://acme.com", location="  San Carlos,   CA "),
            company("Bovi", "https://bovi.com"),
        ))
        self.assertIn('"location": "City, ST"', self.prompts[0])
        targets = {item["company"]: item for item in list_targets(self.conn, user_id=USER)}
        self.assertEqual((targets["Acme"]["location"], targets["Acme"]["location_region"]), ("San Carlos, CA", "Bay Area"))
        self.assertEqual((targets["Bovi"]["location"], targets["Bovi"]["location_region"]), ("", ""), "no page, no location")

    def test_a_new_company_its_site_does_not_place_is_searched_for(self):
        self.sites["ycombinator.example"] = {"/companies/bovi": "<p>Bovi is based in Cedar Park, TX</p>", "/robots.txt": ""}
        self.sites["bovi.com"]["/about"] = "<p>About Bovi</p>"
        locations = json.dumps({"companies": [{
            "company": "Bovi", "location": "Cedar Park, TX",
            "source_url": "https://ycombinator.example/companies/bovi", "note": "",
        }]})

        def runner(prompt):
            self.prompts.append(prompt)
            if LOCATE_PROMPT in prompt:
                return locations
            return proposals(company("Bovi", "https://bovi.com")) if scope_of(prompt) == "local-accelerators" else proposals()

        transport, _ = site_transport(self.sites)
        with httpx.Client(transport=transport) as client:
            result = run_discovery(
                self.conn, user_id=USER, runner=runner, fetcher=safe_fetcher(client), report_dir=self.root / "reports",
                contact_delay=0, today=date(2026, 9, 17), locate_runner=runner,
            )
        target = next(item for item in list_targets(self.conn, user_id=USER) if item["company"] == "Bovi")
        self.assertEqual((target["location"], target["location_basis"]), ("Cedar Park, TX", "web_search"))
        self.assertEqual(result["located"]["recorded"], 1)

    def test_a_new_company_is_drafted_only_after_the_search_that_places_it(self):
        # Seen 2026-09-21: drafts were written before the web search placed their
        # companies in the Bay Area, so none said the student lives there.
        for field, value in (("name", "Test Student"), ("break_location", "Bay Area")):
            self.conn.execute(
                "INSERT INTO profile_facts(user_id, field_path, value_json, source, confirmed, created_at, updated_at) "
                "VALUES(?, ?, ?, 'user', 1, '2026-09-17', '2026-09-17') "
                "ON CONFLICT(user_id, field_path) DO UPDATE SET value_json=excluded.value_json, confirmed=1",
                (USER, field, json.dumps(value)),
            )
        self.conn.commit()
        self.sites["ycombinator.example"] = {"/companies/acme": "<p>Acme is based in San Carlos, CA</p>", "/robots.txt": ""}
        locations = json.dumps({"companies": [{
            "company": "Acme", "location": "San Carlos, CA",
            "source_url": "https://ycombinator.example/companies/acme", "note": "",
        }]})
        drafted = []

        class Recorder:
            name, model = "anthropic", "test-model"

            def create(self, *, instructions, messages, tools, max_output_tokens):
                from opportunity_app.agent_providers import ProviderReply

                drafted.append(json.loads(messages[-1]["content"].split("\n\nYour previous draft")[0]))
                return ProviderReply(text="no draft")

        runner = only_for(proposals(company("Acme", "https://acme.com")), locations=locations)
        result = self.run_with(None, runner=runner, locate_runner=runner,
                               provider_factory=lambda *_: Recorder(), draft_provider="anthropic")
        self.assertEqual(result["located"]["recorded"], 1)
        self.assertTrue(drafted, "the draft model was asked")
        self.assertEqual(drafted[0]["location_line"], "I'm based in the Bay Area during breaks and summers.")

    def test_a_dry_run_changes_no_rows(self):
        before = len(list_targets(self.conn, user_id=USER))
        result = self.run_with(proposals(company("Acme", "https://acme.com")), dry_run=True)
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(len(list_targets(self.conn, user_id=USER)), before)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM outreach_discovery_runs").fetchone()[0], 0)
        self.assertTrue(result["report_path"].endswith("-dry-run.json"))

    def test_the_run_limit_caps_imports(self):
        result = self.run_with(proposals(company("Acme", "https://acme.com"), company("Bovi", "https://bovi.com")), max_targets=1)
        self.assertEqual(result["imported"], 1)
        self.assertIn("limit", result["rejected"][0]["reason"])

    def test_same_second_runs_keep_distinct_reports(self):
        frozen = datetime(2026, 9, 17, 12, 34, 56, tzinfo=timezone.utc)
        first = self.run_with(proposals(), dry_run=True, now=frozen)
        second = self.run_with(proposals(), dry_run=True, now=frozen)
        self.assertNotEqual(first["report_path"], second["report_path"])
        self.assertTrue(Path(first["report_path"]).exists())
        self.assertTrue(Path(second["report_path"]).exists())

    def test_researched_at_uses_the_students_local_date(self):
        instant = datetime(2026, 9, 17, 3, 55, tzinfo=timezone.utc)
        with mock.patch.dict("os.environ", {"PIPELINE_TIMEZONE": "America/Chicago"}):
            result = self.run_with(proposals(company("Acme", "https://acme.com")), today=None, now=instant)
        target = next(item for item in list_targets(self.conn, user_id=USER) if item["company"] == "Acme")
        self.assertEqual(target["researched_at"], "2026-09-16")
        self.assertIn("outreach-discovered-2026-09-16-", result["report_path"])

    def test_a_scheduled_run_is_skipped_soon_after_a_success(self):
        self.run_with(proposals())
        skipped = self.run_with(proposals(company("Acme", "https://acme.com")), trigger="scheduled",
                                now=datetime.now(timezone.utc))
        self.assertTrue(skipped["skipped"])

    def test_an_unreadable_reply_fails_the_run_visibly(self):
        with self.assertRaisesRegex(RuntimeError, "Every search failed. local-accelerators: "):
            self.run_with("I could not find anything, sorry.", scopes=["local-accelerators"])
        self.assertEqual(self.conn.execute("SELECT status FROM outreach_discovery_runs").fetchone()[0], "failed")

    def test_each_scope_is_its_own_search_and_later_ones_know_what_was_found(self):
        def runner(prompt):
            if scope_of(prompt) == "local-accelerators":
                return proposals(company("Acme", "https://acme.com"))
            # The later search proposes the same company again, under another name.
            return proposals(company("Acme, Inc.", "https://www.acme.com", scope=scope_of(prompt)))

        result = self.run_with("", runner=runner, max_targets=4)
        self.assertEqual([scope_of(prompt) for prompt in self.prompts], ["local-accelerators", "us-startups", "recently-funded"])
        self.assertTrue(all("Propose at most 4 companies" in prompt for prompt in self.prompts))
        self.assertNotIn("- Acme (https://acme.com)", self.prompts[0])
        self.assertIn("- Acme (https://acme.com)", self.prompts[1], "a later search is told what an earlier one found")
        self.assertEqual((result["imported"], result["proposed"]), (1, 3))
        self.assertEqual({item["scope"]: item["reason"] for item in result["rejected"]},
                         {"us-startups": "already tracked", "recently-funded": "already tracked"})
        self.assertEqual([item["accepted"] for item in result["scope_results"]], [1, 0, 0])

    def test_a_failed_scope_does_not_lose_the_others(self):
        def runner(prompt):
            if scope_of(prompt) == "us-startups":
                raise RuntimeError("Claude Code exited 1: usage limit")
            return proposals(company("Acme", "https://acme.com")) if scope_of(prompt) == "local-accelerators" else proposals()

        result = self.run_with("", runner=runner)
        self.assertEqual(result["imported"], 1)
        failed = next(item for item in result["scope_results"] if item["scope"] == "us-startups")
        self.assertIn("usage limit", failed["error"])
        status, error = self.conn.execute("SELECT status, error FROM outreach_discovery_runs").fetchone()
        self.assertEqual(status, "succeeded")
        self.assertIn("The US startups in your field search failed: Claude Code exited 1: usage limit", error)

    def test_only_one_run_holds_the_lock(self):
        lock_path = self.root / "reports" / "outreach-discovery.lock"
        with _RunLock(lock_path):
            with self.assertRaises(DiscoveryBusy):
                self.run_with(proposals(), lock_path=lock_path)


class ScopeDefinitionTests(unittest.TestCase):
    def test_briefs_are_filled_from_the_students_profile(self):
        brief = _scope_brief(
            "Near {regions}; working on {interests}.",
            {"regions": [{"name": "Atlanta"}], "break_location": "Savannah, GA", "interest_keywords": ["catalysis"]},
        )
        self.assertEqual(brief, "Near Atlanta, Savannah, GA; working on catalysis.")
        self.assertEqual(
            _scope_brief("Near {regions}; {interests}.", {}),
            "Near the student's school and preferred locations; see the profile above.",
        )

    def test_a_student_can_replace_a_brief_but_not_invent_a_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp) / "sources.local.json"
            local.write_text(json.dumps({"outreach_scopes": {
                "local-accelerators": {"brief": "ATDC and Engage in Atlanta."},
                "made-up": {"brief": "ignored"},
            }}), encoding="utf-8")
            with mock.patch("opportunity_app.outreach_discovery.SOURCES_LOCAL_PATH", local):
                definitions = scope_definitions()
        self.assertEqual(definitions["local-accelerators"]["brief"], "ATDC and Engage in Atlanta.")
        self.assertEqual(definitions["local-accelerators"]["channel"], "Local accelerators")
        self.assertNotIn("made-up", definitions)


class CommandLineTests(unittest.TestCase):
    """The scheduled task reaches the pipeline through this entry point only."""

    def test_remind_runs_against_a_database_path(self):
        from opportunity_app import outreach_cli

        with tempfile.TemporaryDirectory() as tmp:
            _, platform_path = build_and_migrate(Path(tmp))
            conn = connect_product(platform_path)
            try:
                ensure_product_schema(conn)
                create_target(conn, {"company": "Align", "status": "sent", "follow_up_at": "2026-01-01"}, user_id=USER)
            finally:
                conn.close()
            self.assertEqual(outreach_cli.main(["--db", str(platform_path), "remind"]), 0)

    def test_locate_runs_against_a_database_path(self):
        from opportunity_app import outreach_cli

        with tempfile.TemporaryDirectory() as tmp:
            _, platform_path = build_and_migrate(Path(tmp))
            conn = connect_product(platform_path)
            try:
                ensure_product_schema(conn)
                create_target(conn, {"company": "Align", "website": "https://align.example"}, user_id=USER)
            finally:
                conn.close()
            transport, _ = site_transport({"yc.example": {"/align": "<p>Align is based in Austin, TX</p>", "/robots.txt": ""}})
            found = json.dumps({"companies": [
                {"company": "Align", "location": "Austin, TX", "source_url": "https://yc.example/align", "note": ""},
            ]})
            # The command's own wiring is what this covers, so it runs for real
            # against a stub runner and a fetcher with no network behind it.
            with (
                mock.patch.dict("opportunity_app.outreach_cli.RUNNERS", {"claude-code": lambda _prompt: found}),
                mock.patch("opportunity_app.outreach_cli.default_fetcher",
                           lambda: safe_fetcher(httpx.Client(transport=transport))),
            ):
                exit_code = outreach_cli.main(["--db", str(platform_path), "locate", "--provider", "claude-code"])
        self.assertEqual(exit_code, 0)

    def test_a_busy_lock_reports_a_temporary_failure(self):
        from opportunity_app import outreach_cli
        from opportunity_app.outreach_discovery import REPORT_DIR, _RunLock

        with tempfile.TemporaryDirectory() as tmp:
            _, platform_path = build_and_migrate(Path(tmp))
            try:
                lock = _RunLock(REPORT_DIR / "outreach-discovery.lock").__enter__()
            except DiscoveryBusy:
                self.skipTest("a real deep search holds the lock right now")
            try:
                exit_code = outreach_cli.main(["--db", str(platform_path), "discover", "--dry-run"])
            finally:
                lock.__exit__()
        self.assertEqual(exit_code, outreach_cli.TEMPFAIL_EXIT)


class DiscoveryApiTests(unittest.TestCase):
    def test_owner_starts_a_deep_search_and_sees_the_new_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, platform_path = build_and_migrate(root)
            sites = {"acme.com": {**ACME["acme.com"], "/about": "<p>About Acme</p>"}}
            transport, _ = site_transport(sites)
            manager = DiscoveryManager(
                platform_path,
                runner=only_for(proposals(company("Acme", "https://acme.com"))),
                client_factory=lambda: safe_fetcher(httpx.Client(transport=transport)),
                report_dir=root / "reports",
                contact_delay=0,
                form_d_fetcher_factory=lambda: None,
                renderer_factory=lambda: None,
            )
            app = create_app(
                db_path=platform_path, access_token="discovery-owner", static_dir=STATIC_DIR,
                resume_storage=root / "resumes", capture_storage=root / "captures", interview_storage=root / "interviews",
                outreach_discovery_manager=manager,
            )
            headers = {"Authorization": "Bearer discovery-owner"}
            with TestClient(app) as client:
                started = client.post("/api/v1/outreach/discovery", headers=headers, json={"scopes": ["local-accelerators"]})
                self.assertEqual(started.status_code, 202, started.text)
                manager.wait(30)
                listing = client.get("/api/v1/outreach", headers=headers).json()
                self.assertEqual(listing["discovery"]["active"]["state"], "succeeded", listing["discovery"]["active"])
                self.assertEqual(listing["discovery"]["runs"][0]["imported"], 1)
                self.assertEqual(listing["summary"]["new_from_search"], 1)
                bad_scope = client.post("/api/v1/outreach/discovery", headers=headers, json={"scopes": ["biotech"]})
                self.assertEqual(bad_scope.status_code, 422)


if __name__ == "__main__":
    unittest.main()
