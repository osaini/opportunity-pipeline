"""Lever in the ATS registry (apply/ats.py), its page client (apply/schema_client.py) and its request policy (apply/checks.py), read only.

docs/phase5-lever-handoff-spec.md 5.2, 5.3 and 6.0 step 4. No browser and no network: the page client's one open is replaced by a fake response,
and the pages are the sanitized fixtures in tests/fixtures/apply/lever/. Every company and posting is fictional.
"""

import dataclasses
import gzip
import io
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import realdata_guard

realdata_guard.install()

from opportunity_app.apply import ats as apply_ats, checks as apply_checks, lever, schema_client
from opportunity_app.apply.lever_form import parse_lever_form
from opportunity_app.apply.schema_client import LeverListings, LeverPageClient, SchemaUnavailable
from opportunity_app.opportunities.legacy import USER_AGENT

from apply_fake_ats import FakeLeverPageClient, LEVER_COMPANY, LEVER_JOB_ID, LEVER_ROLE_ID, LEVER_SITE, LEVER_TITLE, lever_fixture_text, seed_lever_role
from helpers_apply import USER, ApplyCase, setUpModule, tearDownModule  # noqa: F401

LEVER = apply_ats.LEVER


class Response:
    """What ``urlopen`` gives back, enough of it: a body, headers, ``read(limit)`` and the context manager."""

    def __init__(self, body, *, encoding=""):
        self.body = body
        self.headers = {"Content-Encoding": encoding} if encoding else {}
        self.limit = None

    def read(self, limit=None):
        self.limit = limit
        return self.body if limit is None else self.body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def http_error(code):
    return urllib.error.HTTPError("https://jobs.lever.co/x/y/apply", code, "no", {}, io.BytesIO(b""))


class RegistryTests(unittest.TestCase):
    def test_lever_is_registered_after_greenhouse_with_its_own_values(self):
        self.assertEqual(apply_ats.keys(), ("greenhouse", "lever"))
        self.assertIs(apply_ats.spec_for("lever"), LEVER)
        self.assertEqual((LEVER.key, LEVER.display_name, LEVER.adapter_version), ("lever", "Lever", "lever-1"))
        self.assertEqual(LEVER.supported_modes, ("handoff",))
        self.assertEqual(LEVER.claim_modes, ("handoff",))
        self.assertEqual(LEVER.switch, "apply_agent_lever")
        self.assertFalse(LEVER.adapter_built, "the driver is LV3")
        self.assertIs(LEVER.route_policy, apply_checks.LEVER_ROUTE_POLICY)
        self.assertIsInstance(LEVER.schema_client(), LeverPageClient)

    def test_greenhouse_has_no_switch_of_its_own_and_every_mode_it_had(self):
        self.assertEqual((apply_ats.GREENHOUSE.switch, apply_ats.GREENHOUSE.adapter_built), ("", True))
        self.assertEqual(apply_ats.GREENHOUSE.claim_modes, ("one_click", "handoff", "unattended"))

    def test_the_confirmation_sender_is_lever_and_its_subdomains_only(self):
        for domain, expected in (("hire.lever.co", True), ("lever.co", True), ("mail.hire.lever.co", True), ("HIRE.LEVER.CO.", True),
                                 ("greenhouse.io", False), ("evillever.co", False), ("lever.co.evil.example.test", False), ("", False)):
            with self.subTest(domain=domain):
                self.assertEqual(LEVER.is_confirmation_sender(domain), expected)

    def test_the_names_are_said_as_words(self):
        self.assertEqual(apply_ats.supported_names(), "Greenhouse and Lever")
        self.assertEqual(apply_ats.name_of("lever"), "Lever")

    def test_a_mode_the_ats_does_not_do_is_refused_with_a_sentence_and_one_it_does_but_cannot_yet_with_another(self):
        self.assertEqual(apply_ats.mode_refusal(LEVER, "rehearse"), ("ats_mode", "Lever supports Finish in browser only, for now"))
        self.assertEqual(apply_ats.mode_refusal(LEVER, "lookup"), ("ats_mode", "Lever supports Finish in browser only, for now"))
        self.assertEqual(apply_ats.mode_refusal(LEVER, "handoff"), ("ats_not_built", "Finish in browser for Lever postings is not available yet"))
        built = dataclasses.replace(LEVER, adapter_built=True)
        self.assertIsNone(apply_ats.mode_refusal(built, "handoff"))
        for mode in ("lookup", "rehearse", "handoff"):
            self.assertIsNone(apply_ats.mode_refusal(apply_ats.GREENHOUSE, mode))

    def test_a_claim_mode_is_judged_by_the_spec_and_an_unregistered_ats_is_not_judged(self):
        self.assertEqual(apply_ats.claim_refusal("lever", "handoff"), "")
        self.assertEqual(apply_ats.claim_refusal("lever", "one_click"), "Lever supports Finish in browser only, for now")
        self.assertEqual(apply_ats.claim_refusal("lever", "unattended"), "Lever supports Finish in browser only, for now")
        self.assertEqual([apply_ats.claim_refusal("greenhouse", mode) for mode in ("handoff", "one_click", "unattended")], ["", "", ""])
        self.assertEqual(apply_ats.claim_refusal("ashby", "one_click"), "")


class IdentifyTests(ApplyCase):
    def role(self, url):
        self.opportunity("lv-role")
        with self.conn:
            self.conn.execute("UPDATE opportunities SET url=? WHERE id='lv-role'", (url,))

    def test_a_lever_role_is_found_by_the_registry_and_keeps_the_host_it_lives_on(self):
        for host in ("jobs.lever.co", "jobs.eu.lever.co"):
            with self.subTest(host=host):
                self.role(f"https://{host}/{LEVER_SITE}/{LEVER_JOB_ID}/apply?lever-source=x")
                found = apply_ats.identify(self.conn, "lv-role")
                self.assertIs(found[0], LEVER)
                self.assertEqual(tuple(found[1]), (LEVER_SITE, LEVER_JOB_ID), "the identity is still the two names")
                self.assertEqual(found[1].host, host)
                self.assertEqual(apply_ats.canonical_url_of(LEVER, found[1]), f"https://{host}/{LEVER_SITE}/{LEVER_JOB_ID}/apply")
                with self.conn:
                    self.conn.execute("DELETE FROM opportunities WHERE id='lv-role'")
                self.companies.pop("lv-role", None)

    def test_a_greenhouse_role_is_still_greenhouses_and_has_no_host(self):
        self.role("https://job-boards.greenhouse.io/bluefin/jobs/4000000001")
        found = apply_ats.identify(self.conn, "lv-role")
        self.assertIs(found[0], apply_ats.GREENHOUSE)
        self.assertEqual((tuple(found[1]), getattr(found[1], "host", "")), (("bluefin", "4000000001"), ""))
        self.assertEqual(apply_ats.canonical_url_of(found[0], found[1]), "https://job-boards.greenhouse.io/bluefin/jobs/4000000001")

    def test_a_company_page_that_embeds_lever_is_neither(self):
        self.role(f"https://careers.example.test/{LEVER_SITE}/{LEVER_JOB_ID}")
        self.assertIsNone(apply_ats.identify(self.conn, "lv-role"))


class PageClientTests(unittest.TestCase):
    def fetch(self, response=None, *, error=None, host="jobs.lever.co"):
        client = LeverPageClient()
        seen = []

        def opened(request):
            seen.append(request)
            if error is not None:
                raise error
            return response

        with mock.patch.object(client, "_open", side_effect=opened):
            try:
                return client.fetch(LEVER_SITE, LEVER_JOB_ID, host), seen
            except SchemaUnavailable as exc:
                return exc, seen

    def test_it_makes_one_get_of_the_postings_application_page_with_the_pipelines_user_agent(self):
        page = lever_fixture_text("demo_eeo_survey.html")
        found, [request] = self.fetch(Response(page.encode("utf-8")))
        self.assertEqual(found, page)
        self.assertEqual((request.get_method(), request.full_url), ("GET", f"https://jobs.lever.co/{LEVER_SITE}/{LEVER_JOB_ID}/apply"))
        self.assertEqual(request.get_header("User-agent"), USER_AGENT)
        self.assertIsNone(request.data, "nothing is sent")

    def test_an_eu_posting_is_read_from_the_eu_host(self):
        _, [request] = self.fetch(Response(b"<html></html>"), host="jobs.eu.lever.co")
        self.assertEqual(request.full_url, f"https://jobs.eu.lever.co/{LEVER_SITE}/{LEVER_JOB_ID}/apply")

    def test_only_a_404_is_none_and_every_other_answer_or_failure_is_unavailable(self):
        self.assertIsNone(self.fetch(error=http_error(404))[0])
        for code in (301, 400, 403, 429, 500, 502, 503):
            with self.subTest(code=code):
                found, _ = self.fetch(error=http_error(code))
                self.assertIsInstance(found, SchemaUnavailable)
                self.assertIn(str(code), str(found))
        for error in (urllib.error.URLError("down"), TimeoutError("slow"), OSError("reset"), ValueError("bad")):
            with self.subTest(error=type(error).__name__):
                self.assertIsInstance(self.fetch(error=error)[0], SchemaUnavailable)

    def test_a_gzip_page_is_read_and_a_broken_one_is_unavailable(self):
        page = "<html><title>Fixture Co - Role</title></html>"
        found, _ = self.fetch(Response(gzip.compress(page.encode("utf-8")), encoding="gzip"))
        self.assertEqual(found, page)
        self.assertIsInstance(self.fetch(Response(b"not gzip at all", encoding="gzip"))[0], SchemaUnavailable)

    def test_the_read_is_capped_at_four_megabytes(self):
        response = Response(b"x" * (schema_client.MAX_BYTES + 10))
        found, _ = self.fetch(response)
        self.assertIsInstance(found, SchemaUnavailable)
        self.assertEqual(response.limit, schema_client.MAX_BYTES + 1, "it never reads more than one byte past the cap")
        exact = Response(b"y" * schema_client.MAX_BYTES)
        self.assertEqual(len(self.fetch(exact)[0]), schema_client.MAX_BYTES)

    def test_an_address_that_is_not_lever_is_never_asked(self):
        found, seen = self.fetch(Response(b"x"), host="jobs.example.test")
        self.assertIsInstance(found, SchemaUnavailable)
        self.assertEqual(seen, [])

    def test_the_open_uses_the_default_verifying_opener_with_a_twenty_second_timeout_and_only_the_redirect_rule(self):
        client = LeverPageClient()
        opener = mock.Mock()
        with mock.patch.object(urllib.request, "build_opener", return_value=opener) as built:
            client._open(urllib.request.Request("https://jobs.lever.co/x/y/apply"))
        built.assert_called_once_with(schema_client._SameHostRedirects)
        self.assertEqual(opener.open.call_args.kwargs, {"timeout": 20})
        self.assertEqual(schema_client.TIMEOUT_SECONDS, 20)
        text = Path(schema_client.__file__).read_text(encoding="utf-8")
        for weak in ("_create_unverified_context", "CERT_NONE", "check_hostname = False", "verify=False"):
            self.assertNotIn(weak, text)

    def test_a_redirect_is_followed_only_to_lever_over_https(self):
        handler = schema_client._SameHostRedirects()
        request = urllib.request.Request("https://jobs.lever.co/a/b/apply")
        for target in ("https://jobs.eu.lever.co/a/b/apply", "https://jobs.lever.co/a/b/apply?x=1"):
            with self.subTest(target=target):
                self.assertIsNotNone(handler.redirect_request(request, io.BytesIO(b""), 302, "Found", {}, target))
        for target in ("https://careers.example.test/apply", "http://jobs.lever.co/a/b/apply", "https://jobs.lever.co:8443/a", "https://jobs.lever.co.evil.test/a",
                       "https://user@jobs.lever.co.evil.test/", "ftp://jobs.lever.co/a"):
            with self.subTest(target=target), self.assertRaises(urllib.error.HTTPError):
                handler.redirect_request(request, io.BytesIO(b""), 302, "Found", {}, target)


class ListingsTests(unittest.TestCase):
    def test_a_page_with_a_form_is_a_listing_that_carries_the_form_and_the_pages_title(self):
        listing = LeverListings(FakeLeverPageClient(any_posting=True), "jobs.lever.co").fetch(LEVER_SITE, LEVER_JOB_ID)
        self.assertEqual(listing["title"], f"{LEVER_COMPANY} - {LEVER_TITLE}")
        self.assertTrue(listing["lever_form"].fields)
        fields = LEVER.parse_schema(listing)
        self.assertIn("name", [item.name for item in fields])

    def test_a_404_is_none_and_a_page_with_no_form_is_unavailable_never_closed(self):
        self.assertIsNone(LeverListings(FakeLeverPageClient(closed=True), "jobs.lever.co").fetch(LEVER_SITE, LEVER_JOB_ID))
        for name in ("cloudflare_interstitial.html", "thanks.html", "closed.html"):
            with self.subTest(page=name), self.assertRaises(SchemaUnavailable):
                LeverListings(FakeLeverPageClient(pages={f"{LEVER_SITE}/{LEVER_JOB_ID}": name}), "jobs.lever.co").fetch(LEVER_SITE, LEVER_JOB_ID)

    def test_the_host_is_the_postings_own_and_defaults_to_the_global_one(self):
        pages = FakeLeverPageClient(any_posting=True)
        LeverListings(pages, "jobs.eu.lever.co").fetch(LEVER_SITE, LEVER_JOB_ID)
        LeverListings(pages, "").fetch(LEVER_SITE, LEVER_JOB_ID)
        self.assertEqual([call[2] for call in pages.calls], ["jobs.eu.lever.co", "jobs.lever.co"])

    def test_the_spec_builds_a_reader_from_the_page_client_and_none_without_one(self):
        ident = apply_ats.Ident(LEVER_SITE, LEVER_JOB_ID, "jobs.eu.lever.co")
        self.assertIsNone(LEVER.listings(None, None, ident))
        self.assertIsInstance(LEVER.listings(None, FakeLeverPageClient(), ident), LeverListings)
        schema = mock.Mock()
        self.assertIs(apply_ats.GREENHOUSE.listings(schema, FakeLeverPageClient(), ("bluefin", "1")), schema)


class ParseSchemaTests(unittest.TestCase):
    def test_what_the_page_lists_but_the_app_cannot_read_becomes_a_field_the_plan_leaves_to_the_student(self):
        page = lever_fixture_text("variants.html").replace("</form>", '<input type="date" name="startDate" required><input type="color" name="shade"></form>', 1)
        fields = {item.name: item for item in LEVER.parse_schema({"lever_form": parse_lever_form(page)})}
        self.assertEqual((fields["startDate"].type, fields["startDate"].required), (lever.UNKNOWN_TYPE, True))
        self.assertEqual((fields["shade"].type, fields["shade"].required), (lever.UNKNOWN_TYPE, False))
        self.assertIn("the app does not know", fields["startDate"].description)

    def test_a_disabled_unknown_control_is_never_required(self):
        page = lever_fixture_text("variants.html").replace("</form>", '<input type="date" name="startDate" required disabled></form>', 1)
        field = next(item for item in LEVER.parse_schema({"lever_form": parse_lever_form(page)}) if item.name == "startDate")
        self.assertFalse(field.required)
        self.assertIn("turned it off", field.description)

    def test_a_question_the_page_and_its_description_disagree_about_keeps_its_requirement_and_its_reason(self):
        # The description says the question is required and the page's control is not: the page and its description disagree.
        page = lever_fixture_text("cards_files_consent.html").replace(
            '<input required="required" class="card-field-input" type="text" placeholder="Type your response" value="" name="cards[121e46f7-97d6-5d1e-af50-20b95cf80ab2][field3]" />',
            '<input class="card-field-input" type="text" placeholder="Type your response" value="" name="cards[121e46f7-97d6-5d1e-af50-20b95cf80ab2][field3]" />', 1)
        fields = LEVER.parse_schema({"lever_form": parse_lever_form(page)})
        [bad] = [item for item in fields if item.type == lever.UNREADABLE_TYPE]
        self.assertEqual((bad.name, bad.required), ("cards[121e46f7-97d6-5d1e-af50-20b95cf80ab2][field3]", True))
        self.assertIn("disagree about whether this question is required", bad.description)

    def test_the_posting_difference_reads_the_pages_title(self):
        listing = {"lever_form": parse_lever_form(lever_fixture_text("variants.html"))}
        same = LEVER.posting_difference("Tidewater Games", "Associate Producer - Summer Intern", listing, ats_name="Lever")
        self.assertEqual(same, "")
        other = LEVER.posting_difference("Orbit Systems", "Associate Producer - Summer Intern", listing, ats_name="Lever")
        self.assertEqual(other, "Lever's page is titled \"Tidewater Games - Associate Producer - Summer Intern\", not Associate Producer - Summer Intern at Orbit Systems")
        empty = {"lever_form": parse_lever_form(lever_fixture_text("variants.html").replace("<title>Tidewater Games - Associate Producer - Summer Intern</title>", "<title></title>"))}
        self.assertIn("no title", LEVER.posting_difference("Tidewater Games", "Associate Producer", empty, ats_name="Lever"))


class RoutePolicyTests(unittest.TestCase):
    policy = apply_checks.LEVER_ROUTE_POLICY

    def test_the_hosts_are_the_two_lever_hosts_and_the_lookup_is_the_one_the_location_field_makes(self):
        hosts = {"jobs.lever.co", "jobs.eu.lever.co"}
        self.assertEqual((set(self.policy.navigation_hosts), set(self.policy.submit_hosts), set(self.policy.form_post_hosts)), (hosts, hosts, hosts))
        self.assertEqual({(endpoint.host, endpoint.path_prefix, endpoint.kind) for endpoint in self.policy.lookup_endpoints},
                         {(host, "/searchLocations", "location") for host in hosts})
        self.assertEqual(set(self.policy.resolvable_hosts), hosts)
        self.assertEqual(self.policy.display_name, "Lever")

    def test_a_main_frame_navigation_to_the_posting_is_allowed_and_to_the_companys_own_site_is_not(self):
        def decide(url):
            request = apply_checks.RouteRequest(method="GET", url=url, resource_type="document", is_navigation=True, public=True)
            return apply_checks.route_decision("handoff", "before_hand_over", request, apply_checks.RouteState(), self.policy)

        for url in ("https://jobs.lever.co/a/b/apply", "https://jobs.eu.lever.co/a/b/apply"):
            with self.subTest(url=url):
                self.assertIsInstance(decide(url), apply_checks.Allow)
        for url in ("https://careers.example.test/apply", "https://boards.greenhouse.io/a/jobs/1"):
            with self.subTest(url=url):
                self.assertIsInstance(decide(url), apply_checks.Abort)

    def test_a_write_to_a_lever_host_before_hand_over_is_aborted(self):
        request = apply_checks.RouteRequest(method="POST", url="https://jobs.lever.co/a/b/apply", resource_type="fetch", public=True, body=b"x")
        decision = apply_checks.route_decision("handoff", "before_hand_over", request, apply_checks.RouteState(), self.policy)
        self.assertIsInstance(decision, apply_checks.Abort)

    def test_the_confirmation_path_is_this_postings_own_thanks_page_on_a_lever_host(self):
        def seen(path, **kwargs):
            return apply_checks.Observation(main_path=path, board_token=LEVER_SITE, job_id=LEVER_JOB_ID, **kwargs)

        self.assertTrue(self.policy.confirmation_reached(seen(f"/{LEVER_SITE}/{LEVER_JOB_ID}/thanks")))
        self.assertTrue(self.policy.confirmation_reached(seen(f"/{LEVER_SITE}/{LEVER_JOB_ID}/thanks/")))
        for path in (f"/{LEVER_SITE}/{LEVER_JOB_ID}/apply", f"/{LEVER_SITE}/other-posting/thanks", f"/other/{LEVER_JOB_ID}/thanks", "/thanks", "", f"/{LEVER_SITE}/{LEVER_JOB_ID}/thanks/x"):
            with self.subTest(path=path):
                self.assertFalse(self.policy.confirmation_reached(seen(path)))
        self.assertFalse(self.policy.confirmation_reached(apply_checks.Observation(main_path="/a/b/thanks")), "no posting, no confirmation")


if __name__ == "__main__":
    unittest.main()
